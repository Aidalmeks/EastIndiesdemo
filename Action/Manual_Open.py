from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List

import pymysql
import requests

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from program_factory.port_listener import choose_connection
from program_factory.trade_event_log import log_trade_event
from runtime_settings import api_base_url, database_config, ib_host


CLIENT_ID = 11
PREFERRED_PORT_ORDER = (4001, 4002)
RECENT_DAYS = 14

DB_CONFIG = database_config("memory", cursorclass=pymysql.cursors.DictCursor)

ACCOUNT_BASE_CCY_BY_PORT = {
    4001: "USD",
    4002: "CNH",
}

FAILED_ORDER_STATUSES = {"Cancelled", "Inactive", "ApiCancelled"}


def log(message: str):
    print(message, flush=True)


def _ensure_event_loop():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _normalize_value(value: Any):
    if isinstance(value, Decimal):
        number = float(value)
        return int(number) if number.is_integer() else number
    return value


def _serialize_order_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[key] = value.strftime("%Y-%m-%d %H:%M:%S")
        else:
            out[key] = _normalize_value(value)
    return out


def get_db_connection():
    return pymysql.connect(**DB_CONFIG)


def load_recent_orders(days: int = RECENT_DAYS) -> List[Dict[str, Any]]:
    cutoff = datetime.now() - timedelta(days=days)

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    pair_name,
                    train_start,
                    split_time,
                    two_sigma,
                    position_type,
                    beta,
                    exposure_pct,
                    status,
                    created_at,
                    result,
                    max_return,
                    close_time
                FROM scheduled_orders
                WHERE created_at >= %s
                ORDER BY created_at DESC, id DESC
                """,
                (cutoff,),
            )
            rows = cursor.fetchall()
    finally:
        conn.close()

    return [_serialize_order_row(row) for row in rows]


def load_orders_grouped(days: int = RECENT_DAYS) -> Dict[str, List[Dict[str, Any]]]:
    grouped = {"PENDING": [], "HOLDING": [], "CLOSED": []}
    cutoff = datetime.now() - timedelta(days=days)
    base_select = """
        SELECT
            id,
            pair_name,
            train_start,
            split_time,
            two_sigma,
            position_type,
            beta,
            exposure_pct,
            status,
            created_at,
            result,
            max_return,
            close_time
        FROM scheduled_orders
    """

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            for status in ("PENDING", "HOLDING"):
                cursor.execute(
                    base_select + """
                    WHERE status = %s
                    ORDER BY created_at DESC, id DESC
                    """,
                    (status,),
                )
                grouped[status] = [_serialize_order_row(row) for row in cursor.fetchall()]

            cursor.execute(
                base_select + """
                WHERE status = 'CLOSED'
                  AND created_at >= %s
                ORDER BY created_at DESC, id DESC
                """,
                (cutoff,),
            )
            grouped["CLOSED"] = [_serialize_order_row(row) for row in cursor.fetchall()]
    finally:
        conn.close()

    return grouped


def load_pending_orders() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    pair_name,
                    beta,
                    exposure_pct,
                    position_type,
                    status,
                    created_at
                FROM scheduled_orders
                WHERE status = 'PENDING'
                ORDER BY id ASC
                """
            )
            rows = cursor.fetchall()
    finally:
        conn.close()

    return [_serialize_order_row(row) for row in rows]


def load_pending_order_by_id(order_id: int) -> Dict[str, Any] | None:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    pair_name,
                    beta,
                    exposure_pct,
                    position_type,
                    status,
                    created_at
                FROM scheduled_orders
                WHERE id = %s
                LIMIT 1
                """,
                (order_id,),
            )
            row = cursor.fetchone()
    finally:
        conn.close()

    return _serialize_order_row(row) if row else None


def delete_pending_order(order_id: int) -> Dict[str, Any]:
    order_row = load_pending_order_by_id(order_id)
    if not order_row:
        return {
            "success": False,
            "order_id": order_id,
            "message": f"Order id={order_id} not found",
        }

    if str(order_row.get("status") or "").upper() != "PENDING":
        return {
            "success": False,
            "order_id": order_id,
            "pair_name": order_row.get("pair_name"),
            "message": f"Order id={order_id} is not PENDING",
        }

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM scheduled_orders
                WHERE id = %s AND status = 'PENDING'
                """,
                (order_id,),
            )
            deleted = cursor.rowcount > 0
        conn.commit()
    finally:
        conn.close()

    if not deleted:
        return {
            "success": False,
            "order_id": order_id,
            "pair_name": order_row.get("pair_name"),
            "message": f"Order id={order_id} could not be deleted",
        }

    return {
        "success": True,
        "order_id": order_id,
        "pair_name": order_row.get("pair_name"),
        "message": f"Deleted pending order id={order_id}",
    }


def mark_order_executed(order_id: int) -> bool:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE scheduled_orders
                SET status = 'HOLDING',
                    created_at = NOW()
                WHERE id = %s AND status = 'PENDING'
                """,
                (order_id,),
            )
            updated = cursor.rowcount > 0
        conn.commit()
        return updated
    finally:
        conn.close()


def fetch_usdcnh() -> float:
    try:
        res = requests.get("https://open.er-api.com/v6/latest/USD", timeout=3)
        data = res.json()
        return float(data["rates"]["CNH"])
    except Exception:
        return 6.89


def get_action(qty: int):
    if qty > 0:
        return "BUY", qty
    if qty < 0:
        return "SELL", abs(qty)
    return None, 0


def wait_for_order_trades(ib, trades, pair_name: str, label: str, timeout: int = 120):
    start = time.time()

    while True:
        all_filled = True
        failures = []

        for symbol, trade in trades:
            status = str(trade.orderStatus.status or "")
            filled = trade.orderStatus.filled
            remaining = trade.orderStatus.remaining
            log(f"[{label} STATUS] {symbol} | Status: {status} | Filled: {filled} | Remaining: {remaining}")

            if status in FAILED_ORDER_STATUSES:
                failures.append(f"{symbol}: {status}")
            if status != "Filled":
                all_filled = False

        if failures:
            raise RuntimeError("; ".join(failures))

        if all_filled:
            log(f"[{label}] all legs filled")
            return

        if time.time() - start > timeout:
            raise RuntimeError(f"Timeout waiting {label.lower()} fills for {pair_name}")

        ib.sleep(1)


def wait_price(ib, ticker, timeout: int = 120):
    start = time.time()
    while True:
        price = ticker.marketPrice()
        if price is not None and not math.isnan(price):
            return float(price)

        if time.time() - start > timeout:
            return None

        ib.sleep(0.2)


def fetch_pair_entry_z(pair_name: str, price_y: float, price_x: float):
    try:
        payload = requests.get(f"{api_base_url()}/{pair_name}", timeout=3).json()
        sigma = float(payload["sigma"])
        if sigma == 0:
            return None
        spread = float(price_y) - (float(payload["beta"]) * float(price_x) + float(payload["alpha"]))
        return (spread - float(payload["mu"])) / sigma
    except Exception:
        return None


def resolve_account_context():
    _ensure_event_loop()

    from ib_insync import IB

    selection = choose_connection(preferred_order=PREFERRED_PORT_ORDER)
    connect_port = selection.port
    account = selection.account
    base_ccy = ACCOUNT_BASE_CCY_BY_PORT.get(connect_port, "USD").upper()

    log(
        f"[PORT] selected={connect_port}, account={account}, pid={selection.pid}, "
        f"process={selection.process_name}, reason={selection.reason}"
    )

    if base_ccy == "RMB":
        base_ccy = "CNH"
    elif base_ccy == "CNY":
        base_ccy = "CNH"

    ib = IB()
    ib.connect(ib_host(), connect_port, clientId=CLIENT_ID)

    try:
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
    finally:
        ib.disconnect()

    if net_liq_native is None:
        raise RuntimeError("Cannot get NetLiquidation")

    if base_ccy == "USD":
        usdcnh = 1.0
        net_liq_usd = net_liq_native
    elif base_ccy == "CNH":
        usdcnh = fetch_usdcnh()
        net_liq_usd = net_liq_native / usdcnh
    else:
        raise ValueError(f"Unsupported base currency config: {base_ccy}")

    log(
        f"[ACCOUNT] Net Liq: {net_liq_native:.0f} {base_ccy} | "
        f"{net_liq_usd:.0f} USD | FX={usdcnh:.3f}"
    )

    return {
        "port": connect_port,
        "account": account,
        "base_ccy": base_ccy,
        "net_liq_native": net_liq_native,
        "net_liq_usd": net_liq_usd,
        "fx": usdcnh,
        "selection": selection,
    }


def _place_pending_order_core(ib, account: str, order_row: Dict[str, Any]) -> Dict[str, Any]:
    from ib_insync import MarketOrder, Stock

    order_id = int(order_row["id"])
    pair_name = str(order_row["pair_name"])
    beta = float(order_row["beta"])
    exposure_pct = float(order_row["exposure_pct"])
    position_type = str(order_row["position_type"])

    symbol_y, symbol_x = pair_name.upper().split("_")
    gross_target = 1.6 * float(order_row["net_liq_usd"]) * (exposure_pct / 100.0)

    log("\n===== MANUAL OPEN =====")
    log(f"[ORDER] id={order_id} | pair={pair_name} | type={position_type}")
    log(f"[CFG] beta={beta:.4f} | exposure={exposure_pct:.2f}% | gross_target={gross_target:.2f} USD")

    contract_y = Stock(symbol_y, "SMART", "USD")
    contract_x = Stock(symbol_x, "SMART", "USD")
    log(f"[CONTRACT] qualifying {symbol_y} and {symbol_x}")
    ib.qualifyContracts(contract_y, contract_x)

    log(f"[PRICE] requesting realtime prices for {symbol_y} / {symbol_x}")
    ticker_y = ib.reqMktData(contract_y, "", False, False)
    ticker_x = ib.reqMktData(contract_x, "", False, False)
    price_y = wait_price(ib, ticker_y)
    price_x = wait_price(ib, ticker_x)

    if price_y is None or price_x is None:
        raise RuntimeError(f"Price unavailable for {pair_name}: {symbol_y}={price_y}, {symbol_x}={price_x}")

    log(f"[PRICE] {symbol_y}={price_y:.4f} | {symbol_x}={price_x:.4f}")

    beta_abs = abs(beta)
    single_leg_mode = math.isclose(beta_abs, 0.0, abs_tol=1e-12)
    pair_gross_per_share = price_y + beta_abs * price_x
    if pair_gross_per_share <= 0:
        raise RuntimeError(f"Invalid gross-per-share for {pair_name}: {pair_gross_per_share}")

    qty_y = int(gross_target / pair_gross_per_share)
    qty_x = int(qty_y * beta)
    actual_gross = qty_y * price_y + abs(qty_x) * price_x
    log(f"[TARGET] raw qty -> {symbol_y}={qty_y}, {symbol_x}={qty_x}")
    log(f"[TARGET] gross actual -> {actual_gross:.2f} USD")
    if single_leg_mode:
        log(f"[MODE] beta=0 detected -> single-leg mode on {symbol_y}; {symbol_x} target forced to flat")

    if qty_y <= 0 or (not single_leg_mode and abs(qty_x) <= 0):
        raise RuntimeError(f"Target share size too small for {pair_name}: {symbol_y}={qty_y}, {symbol_x}={qty_x}")

    positions = ib.positions()
    pos_y = 0
    pos_x = 0
    for pos in positions:
        if getattr(pos, "account", None) != account:
            continue
        if pos.contract.symbol == symbol_y:
            pos_y = pos.position
        elif pos.contract.symbol == symbol_x:
            pos_x = pos.position

    mode = "OPEN" if abs(pos_y) < 1 and abs(pos_x) < 1 else "TARGET"
    log(f"[POSITION] current -> {symbol_y}={pos_y}, {symbol_x}={pos_x}")
    log(f"[MODE] {mode}")

    if position_type == "LONG_SPREAD":
        target_y, target_x = qty_y, (-qty_x if not single_leg_mode else 0)
    else:
        target_y, target_x = -qty_y, (qty_x if not single_leg_mode else 0)

    order_y = int(target_y - pos_y)
    order_x = int(target_x - pos_x)
    log(f"[TARGET] desired -> {symbol_y}={target_y}, {symbol_x}={target_x}")
    log(f"[ORDER] delta -> {symbol_y}={order_y}, {symbol_x}={order_x}")

    action_y, qty_y_final = get_action(order_y)
    action_x, qty_x_final = get_action(order_x)

    trades = []
    if action_y:
        order_y_obj = MarketOrder(action_y, qty_y_final)
        order_y_obj.account = account
        log(f"[SUBMIT] {symbol_y}: {action_y} {qty_y_final}")
        trades.append((symbol_y, ib.placeOrder(contract_y, order_y_obj)))
    if action_x:
        order_x_obj = MarketOrder(action_x, qty_x_final)
        order_x_obj.account = account
        log(f"[SUBMIT] {symbol_x}: {action_x} {qty_x_final}")
        trades.append((symbol_x, ib.placeOrder(contract_x, order_x_obj)))

    if not trades:
        raise RuntimeError(f"No valid share quantity to place for {pair_name}")

    wait_for_order_trades(ib, trades, pair_name, mode)
    entry_z = fetch_pair_entry_z(pair_name, price_y, price_x)

    return {
        "success": True,
        "order_id": order_id,
        "pair_name": pair_name,
        "mode": mode,
        "symbol_y": symbol_y,
        "symbol_x": symbol_x,
        "position_type": position_type,
        "beta": beta,
        "exposure_pct": exposure_pct,
        "gross_target": gross_target,
        "actual_gross": actual_gross,
        "price_y": price_y,
        "price_x": price_x,
        "target_y": target_y,
        "target_x": target_x,
        "order_y": order_y,
        "order_x": order_x,
        "entry_z": entry_z,
        "message": f"Placed {mode} order for {pair_name}",
    }


def manual_place_order(order_id: int) -> Dict[str, Any]:
    log(f"[MANUAL OPEN] requested order_id={order_id}")
    order_row = load_pending_order_by_id(order_id)
    if not order_row:
        log(f"[ERROR] order id={order_id} not found")
        return {
            "success": False,
            "order_id": order_id,
            "message": f"Order id={order_id} not found",
        }

    if str(order_row.get("status") or "").upper() != "PENDING":
        log(f"[ERROR] order id={order_id} is not PENDING")
        return {
            "success": False,
            "order_id": order_id,
            "pair_name": order_row.get("pair_name"),
            "message": f"Order id={order_id} is not PENDING",
        }

    _ensure_event_loop()
    from ib_insync import IB

    account_ctx = resolve_account_context()
    ib = IB()
    ib.connect(ib_host(), account_ctx["port"], clientId=CLIENT_ID)
    log(f"[IB] connected to configured gateway:{account_ctx['port']} with clientId={CLIENT_ID}")

    try:
        payload = dict(order_row)
        payload["net_liq_usd"] = account_ctx["net_liq_usd"]
        result = _place_pending_order_core(ib, account_ctx["account"], payload)
        updated = mark_order_executed(order_id)
        result["db_updated"] = updated
        if not updated:
            log(f"[DB] order id={order_id} NOT updated to HOLDING")
            result["success"] = False
            result["message"] = f"Order placed but DB row id={order_id} was not updated to HOLDING"
        else:
            log(f"[DB] order id={order_id} -> HOLDING")
            log(f"[DONE] {result['pair_name']} is now HOLDING")
            log_trade_event({
                "event": "OPEN",
                "kind": "pair",
                "pair_name": result["pair_name"],
                "beta": result.get("beta"),
                "exposure_pct": result.get("exposure_pct"),
                "entry_z": result.get("entry_z"),
            })
        return result
    except Exception as exc:
        log(f"[ERROR] {exc}")
        return {
            "success": False,
            "order_id": order_id,
            "pair_name": order_row.get("pair_name"),
            "message": str(exc),
        }
    finally:
        ib.disconnect()
        log("[IB] disconnected")


def manual_place_all_pending() -> Dict[str, Any]:
    pending_orders = load_pending_orders()
    if not pending_orders:
        log("[MANUAL OPEN] no pending orders found")
        return {
            "success": True,
            "placed_count": 0,
            "failed_count": 0,
            "results": [],
            "message": "No pending orders found",
        }

    _ensure_event_loop()
    from ib_insync import IB

    account_ctx = resolve_account_context()
    ib = IB()
    ib.connect(ib_host(), account_ctx["port"], clientId=CLIENT_ID)
    log(f"[IB] connected to configured gateway:{account_ctx['port']} with clientId={CLIENT_ID}")

    results = []
    try:
        for row in pending_orders:
            log(f"\n[QUEUE] processing order id={row['id']} | pair={row['pair_name']}")
            payload = dict(row)
            payload["net_liq_usd"] = account_ctx["net_liq_usd"]
            try:
                result = _place_pending_order_core(ib, account_ctx["account"], payload)
                result["db_updated"] = mark_order_executed(int(row["id"]))
                if not result["db_updated"]:
                    log(f"[DB] order id={row['id']} NOT updated to HOLDING")
                    result["success"] = False
                    result["message"] = f"Order placed but DB row id={row['id']} was not updated to HOLDING"
                else:
                    log(f"[DB] order id={row['id']} -> HOLDING")
                    log_trade_event({
                        "event": "OPEN",
                        "kind": "pair",
                        "pair_name": result.get("pair_name"),
                        "beta": result.get("beta"),
                        "exposure_pct": result.get("exposure_pct"),
                        "entry_z": result.get("entry_z"),
                    })
            except Exception as exc:
                log(f"[ERROR] order id={row['id']} failed: {exc}")
                result = {
                    "success": False,
                    "order_id": int(row["id"]),
                    "pair_name": row.get("pair_name"),
                    "message": str(exc),
                }
            results.append(result)
    finally:
        ib.disconnect()
        log("[IB] disconnected")

    placed_count = sum(1 for item in results if item.get("success"))
    failed_count = len(results) - placed_count
    return {
        "success": failed_count == 0,
        "placed_count": placed_count,
        "failed_count": failed_count,
        "results": results,
        "message": f"Processed {len(results)} pending orders",
    }


def _print_grouped_orders(days: int):
    grouped = load_orders_grouped(days=days)
    for status in ["PENDING", "HOLDING", "CLOSED"]:
        rows = grouped.get(status, [])
        print(f"\n[{status}] count={len(rows)}")
        for row in rows:
            print(row)


def main():
    parser = argparse.ArgumentParser(description="Manual open orders for scheduled_orders.")
    parser.add_argument("--list", action="store_true", help="List recent orders grouped by status.")
    parser.add_argument("--days", type=int, default=RECENT_DAYS, help="Recent days for listing orders.")
    parser.add_argument("--order-id", type=int, help="Manually place a single PENDING order.")
    parser.add_argument("--all-pending", action="store_true", help="Place all PENDING orders.")
    args = parser.parse_args()

    if args.list:
        _print_grouped_orders(days=args.days)
        return

    if args.order_id is not None:
        log("===== Manual Open Console =====")
        print(manual_place_order(args.order_id))
        return

    if args.all_pending:
        log("===== Manual Open Console =====")
        print(manual_place_all_pending())
        return

    parser.print_help()


if __name__ == "__main__":
    main()
