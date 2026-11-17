from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict

import pymysql

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import IB, MarketOrder, Stock

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from program_factory.port_listener import choose_connection
from runtime_settings import database_config, ib_host


CLIENT_ID = 14
PREFERRED_PORT_ORDER = (4001, 4002)

DB_CONFIG = database_config("memory", cursorclass=pymysql.cursors.DictCursor)

FAILED_ORDER_STATUSES = {"Cancelled", "Inactive", "ApiCancelled"}


def _ensure_event_loop():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def wait_price(ib, ticker, timeout: int = 120):
    start = time.time()
    while True:
        price = ticker.marketPrice()
        if price is not None and not math.isnan(price):
            return float(price)

        if time.time() - start > timeout:
            return None
        ib.sleep(0.2)


def wait_for_order_trade(ib, trade, symbol: str, timeout: int = 120):
    start = time.time()
    while True:
        status = str(trade.orderStatus.status or "")
        filled = trade.orderStatus.filled
        remaining = trade.orderStatus.remaining
        print(f"[CLOSE STATUS] {symbol} | Status: {status} | Filled: {filled} | Remaining: {remaining}", flush=True)

        if status in FAILED_ORDER_STATUSES:
            raise RuntimeError(f"{symbol}: {status}")

        if status == "Filled":
            print("[CLOSE] filled", flush=True)
            return

        if time.time() - start > timeout:
            raise RuntimeError(f"Timeout waiting close fill for {symbol}")

        ib.sleep(1)


def normalize_result_value(return_pct):
    if return_pct is None:
        return None
    return Decimal(str(return_pct)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def load_holding_single_order(order_id: int | None = None, single_name: str | None = None) -> Dict[str, Any] | None:
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            if order_id is not None:
                cursor.execute(
                    """
                    SELECT id, single_name, status
                    FROM single_scheduled_orders
                    WHERE id = %s AND status = 'HOLDING'
                    LIMIT 1
                    """,
                    (int(order_id),),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, single_name, status
                    FROM single_scheduled_orders
                    WHERE LOWER(single_name) = LOWER(%s) AND status = 'HOLDING'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (str(single_name or "").strip().upper(),),
                )
            return cursor.fetchone()
    finally:
        conn.close()


def update_single_scheduled_order_closed(order_id: int, return_pct) -> bool:
    result_value = normalize_result_value(return_pct)

    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE single_scheduled_orders
                SET status = 'CLOSED',
                    result = %s,
                    close_time = NOW()
                WHERE id = %s
                  AND status = 'HOLDING'
                LIMIT 1
                """,
                (result_value, int(order_id)),
            )
            updated = cursor.rowcount > 0
        conn.commit()
        if updated:
            print(f"[DB] CLOSED single order id={order_id} | result={result_value}", flush=True)
        else:
            print(f"[DB] no HOLDING row matched id={order_id}", flush=True)
        return updated
    finally:
        conn.close()


def compute_single_return_pct(position: Dict[str, Any], price: float) -> float:
    qty = float(position.get("qty", 0))
    avg_price = float(position.get("avg_price", 0))
    entry_notional = abs(qty * avg_price)
    if entry_notional <= 0:
        return 0.0

    pnl = qty * (float(price) - avg_price)
    return pnl / entry_notional * 100.0


def delete_pair_reentry_row(single_name: str):
    """DELETE matching row from pair_reentry when single position is manually closed."""
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM pair_reentry WHERE pair_name = %s LIMIT 1",
                (single_name.upper(),),
            )
            if cursor.rowcount > 0:
                conn.commit()
                print(f"[DB] DELETED pair_reentry {single_name.upper()}", flush=True)
                return True
        conn.commit()
        print(f"[DB] no pair_reentry row matched for {single_name.upper()}", flush=True)
        return False
    finally:
        conn.close()


def manual_close_single(order_id: int | None = None, single_name: str | None = None) -> Dict[str, Any]:
    order_row = load_holding_single_order(order_id=order_id, single_name=single_name)
    if not order_row:
        return {
            "success": False,
            "order_id": order_id,
            "single_name": single_name,
            "message": "No HOLDING single_scheduled_orders row found",
        }

    resolved_order_id = int(order_row["id"])
    symbol = str(order_row["single_name"] or "").strip().upper()
    if not symbol:
        return {
            "success": False,
            "order_id": resolved_order_id,
            "message": "single_name is empty",
        }

    _ensure_event_loop()
    selection = choose_connection(preferred_order=PREFERRED_PORT_ORDER)
    connect_port = selection.port
    account = selection.account

    ib = IB()
    ib.connect(ib_host(), connect_port, clientId=CLIENT_ID)
    print(
        f"[PORT] selected={connect_port}, account={account}, pid={selection.pid}, "
        f"process={selection.process_name}, reason={selection.reason}",
        flush=True,
    )

    try:
        position = {"qty": 0, "avg_price": 0}
        for pos in ib.positions():
            if getattr(pos, "account", None) != account:
                continue
            if pos.contract.symbol == symbol:
                position = {
                    "qty": pos.position,
                    "avg_price": pos.avgCost,
                }
                break

        qty = float(position["qty"])
        print(f"[POS] {symbol}: {qty}", flush=True)
        if qty == 0:
            return {
                "success": False,
                "order_id": resolved_order_id,
                "single_name": symbol,
                "message": f"No open position found for {symbol}",
            }

        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)

        ticker = ib.reqMktData(contract, "", False, False)
        price = wait_price(ib, ticker)
        if price is None:
            price = float(position["avg_price"] or 0)
        if price <= 0:
            raise RuntimeError(f"Price unavailable for {symbol}: {price}")

        return_pct = compute_single_return_pct(position, price)
        print(f"[RET] {symbol}: {return_pct:.4f}%", flush=True)

        action = "SELL" if qty > 0 else "BUY"
        close_qty = abs(int(qty))
        if close_qty <= 0:
            raise RuntimeError(f"Invalid close quantity for {symbol}: {qty}")

        order = MarketOrder(action, close_qty)
        order.account = account
        trade = ib.placeOrder(contract, order)
        print(f"[ORDER] {symbol}: {action} {close_qty}", flush=True)
        wait_for_order_trade(ib, trade, symbol)

        updated = update_single_scheduled_order_closed(resolved_order_id, return_pct)
        if updated:
            delete_pair_reentry_row(symbol)
            return {
                "success": True,
                "order_id": resolved_order_id,
                "single_name": symbol,
                "return_pct": round(float(return_pct), 4),
                "message": f"Closed {symbol}",
            }

        return {
            "success": False,
            "order_id": resolved_order_id,
            "single_name": symbol,
            "return_pct": round(float(return_pct), 4),
            "message": f"Order placed but DB row id={resolved_order_id} was not updated",
        }
    except Exception as exc:
        return {
            "success": False,
            "order_id": resolved_order_id,
            "single_name": symbol,
            "message": str(exc),
        }
    finally:
        ib.disconnect()


def main():
    parser = argparse.ArgumentParser(description="Manually close one HOLDING single stock.")
    parser.add_argument("--order-id", type=int, help="Single HOLDING order id.")
    parser.add_argument("--single-name", help="Stock symbol fallback, like INTC.")
    args = parser.parse_args()

    if args.order_id is None and not args.single_name:
        parser.error("--order-id or --single-name is required")

    print(manual_close_single(order_id=args.order_id, single_name=args.single_name))


if __name__ == "__main__":
    main()
