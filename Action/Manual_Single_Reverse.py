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
import requests

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import IB, MarketOrder, Stock

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from program_factory.port_listener import choose_connection
from program_factory.execution import cap_target_gross_to_total_exposure
from runtime_settings import database_config, ib_host


CLIENT_ID = 16
PREFERRED_PORT_ORDER = (4001, 4002)

DB_CONFIG = database_config("memory", cursorclass=pymysql.cursors.DictCursor)

ACCOUNT_BASE_CCY_BY_PORT = {
    4001: "USD",
    4002: "CNH",
}
MAX_POSITION_MULTIPLIER = 1.6
FAILED_ORDER_STATUSES = {"Cancelled", "Inactive", "ApiCancelled"}
POSITION_TYPE_REVERSE = {
    "LONG_SPREAD": "SHORT_SPREAD",
    "SHORT_SPREAD": "LONG_SPREAD",
}


def _ensure_event_loop():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def wait_price(ib, ticker, timeout: int = 120):
    start = time.time()
    while True:
        price = ticker.marketPrice()
        if price is not None and not math.isnan(price) and float(price) > 0:
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
        print(f"[REVERSE STATUS] {symbol} | Status: {status} | Filled: {filled} | Remaining: {remaining}", flush=True)

        if status in FAILED_ORDER_STATUSES:
            raise RuntimeError(f"{symbol}: {status}")

        if status == "Filled":
            print("[REVERSE] filled", flush=True)
            return

        if time.time() - start > timeout:
            raise RuntimeError(f"Timeout waiting reverse fill for {symbol}")

        ib.sleep(1)


def fetch_usdcnh() -> float:
    try:
        res = requests.get("https://open.er-api.com/v6/latest/USD", timeout=3)
        data = res.json()
        return float(data["rates"]["CNH"])
    except Exception:
        return 6.89


def resolve_net_liq_usd(ib: IB, account: str, connect_port: int) -> float:
    base_ccy = ACCOUNT_BASE_CCY_BY_PORT.get(connect_port, "USD").upper()
    if base_ccy in {"RMB", "CNY"}:
        base_ccy = "CNH"

    net_liq_native = None
    for value in ib.accountSummary():
        if value.tag != "NetLiquidation" or value.account != account:
            continue

        value_ccy = str(getattr(value, "currency", "")).upper()
        if base_ccy == "USD" and value_ccy == "USD":
            net_liq_native = float(value.value)
            break
        if base_ccy == "CNH" and value_ccy in ("CNH", "CNY"):
            net_liq_native = float(value.value)
            break
        if net_liq_native is None:
            net_liq_native = float(value.value)

    if net_liq_native is None:
        raise RuntimeError("Cannot get NetLiquidation")

    if base_ccy == "USD":
        net_liq_usd = net_liq_native
        fx = 1.0
    elif base_ccy == "CNH":
        fx = fetch_usdcnh()
        net_liq_usd = net_liq_native / fx
    else:
        raise ValueError(f"Unsupported base currency config: {base_ccy}")

    print(
        f"[ACCOUNT] Net Liq: {net_liq_native:.0f} {base_ccy} | "
        f"{net_liq_usd:.0f} USD | FX={fx:.3f}",
        flush=True,
    )
    return net_liq_usd


def normalize_result_value(return_pct):
    if return_pct is None:
        return None
    return Decimal(str(return_pct)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def load_holding_single_order(order_id: int) -> Dict[str, Any] | None:
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, single_name, status, position_type, exposure_pct, result
                FROM single_scheduled_orders
                WHERE id = %s AND status = 'HOLDING'
                LIMIT 1
                """,
                (int(order_id),),
            )
            return cursor.fetchone()
    finally:
        conn.close()


def update_single_scheduled_order_reversed(order_id: int, return_pct, new_position_type: str) -> bool:
    result_value = normalize_result_value(return_pct)

    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE single_scheduled_orders
                SET result = COALESCE(result, 0) + %s,
                    position_type = %s,
                    created_at = NOW()
                WHERE id = %s
                  AND status = 'HOLDING'
                LIMIT 1
                """,
                (result_value, new_position_type, int(order_id)),
            )
            updated = cursor.rowcount > 0
        conn.commit()
        if updated:
            print(
                f"[DB] REVERSED single order id={order_id} | add_result={result_value} | type={new_position_type}",
                flush=True,
            )
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


def manual_reverse_single(order_id: int) -> Dict[str, Any]:
    order_row = load_holding_single_order(order_id)
    if not order_row:
        return {
            "success": False,
            "order_id": order_id,
            "message": "No HOLDING single_scheduled_orders row found",
        }

    symbol = str(order_row["single_name"] or "").strip().upper()
    if not symbol:
        return {
            "success": False,
            "order_id": order_id,
            "message": "single_name is empty",
        }

    current_position_type = str(order_row.get("position_type") or "").strip().upper()
    new_position_type = POSITION_TYPE_REVERSE.get(current_position_type)
    if not new_position_type:
        return {
            "success": False,
            "order_id": order_id,
            "single_name": symbol,
            "message": f"Unsupported position_type for reverse: {current_position_type}",
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
        positions = ib.positions()
        for pos in positions:
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
                "order_id": order_id,
                "single_name": symbol,
                "message": f"No open position found for {symbol}",
            }

        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)

        ticker = ib.reqMktData(contract, "", False, False)
        price = wait_price(ib, ticker)
        try:
            ib.cancelMktData(contract)
        except Exception:
            pass
        if price is None:
            price = float(position["avg_price"] or 0)
        if price <= 0:
            raise RuntimeError(f"Price unavailable for {symbol}: {price}")

        return_pct = compute_single_return_pct(position, price)
        print(f"[RET] {symbol}: {return_pct:.4f}%", flush=True)

        exposure_pct = float(order_row["exposure_pct"])
        net_liq_usd = resolve_net_liq_usd(ib, account, connect_port)
        gross_target = MAX_POSITION_MULTIPLIER * net_liq_usd * (exposure_pct / 100.0)
        capped_gross_target, risk_info = cap_target_gross_to_total_exposure(
            ib_client=ib,
            account=account,
            requested_gross=gross_target,
            net_liq_usd=net_liq_usd,
            target_symbols=[symbol],
            target_prices={symbol: price},
            positions=positions,
        )
        print(
            "[RISK] current gross="
            f"{risk_info['current_total_gross']:.2f} USD "
            f"({risk_info['current_total_pct']:.2f}%) | "
            f"allowed target={risk_info['allowed_target_gross']:.2f} USD",
            flush=True,
        )
        if risk_info["capped"]:
            print(
                f"[RISK] gross target capped: {gross_target:.2f} -> "
                f"{capped_gross_target:.2f} USD",
                flush=True,
            )
            gross_target = capped_gross_target

        target_abs_qty = int(gross_target / price)
        if target_abs_qty <= 0:
            raise RuntimeError(f"Target share size too small for {symbol}: qty={target_abs_qty}")

        target_qty = target_abs_qty if new_position_type == "LONG_SPREAD" else -target_abs_qty
        order_delta = int(target_qty - qty)
        action = "BUY" if order_delta > 0 else "SELL"
        reverse_qty = abs(order_delta)
        if reverse_qty <= 0:
            raise RuntimeError(f"Invalid reverse quantity for {symbol}: current={qty}, target={target_qty}")

        order = MarketOrder(action, reverse_qty)
        order.account = account
        trade = ib.placeOrder(contract, order)
        print(
            f"[ORDER] {symbol}: {action} {reverse_qty} | "
            f"exposure={exposure_pct:.2f}% | target={target_qty}",
            flush=True,
        )
        wait_for_order_trade(ib, trade, symbol)

        updated = update_single_scheduled_order_reversed(order_id, return_pct, new_position_type)
        if updated:
            return {
                "success": True,
                "order_id": order_id,
                "single_name": symbol,
                "return_pct": round(float(return_pct), 4),
                "position_type": new_position_type,
                "message": f"Reversed {symbol}",
            }

        return {
            "success": False,
            "order_id": order_id,
            "single_name": symbol,
            "return_pct": round(float(return_pct), 4),
            "message": f"Order placed but DB row id={order_id} was not updated",
        }
    except Exception as exc:
        return {
            "success": False,
            "order_id": order_id,
            "single_name": symbol,
            "message": str(exc),
        }
    finally:
        ib.disconnect()


def main():
    parser = argparse.ArgumentParser(description="Reverse one HOLDING single_scheduled_orders row.")
    parser.add_argument("--order-id", type=int, required=True, help="Single HOLDING order id.")
    args = parser.parse_args()

    print("===== Manual Single Reverse Console =====", flush=True)
    print(manual_reverse_single(args.order_id), flush=True)


if __name__ == "__main__":
    main()
