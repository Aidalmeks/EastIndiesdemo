from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict

import pymysql
import redis
import requests

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from program_factory.port_listener import choose_connection
from program_factory.trade_event_log import log_trade_event
from runtime_settings import database_config, ib_host, redis_config


CLIENT_ID = 12
PREFERRED_PORT_ORDER = (4001, 4002)

DB_CONFIG = database_config("memory", cursorclass=pymysql.cursors.DictCursor)

ACCOUNT_BASE_CCY_BY_PORT = {
    4001: "USD",
    4002: "CNH",
}
MAX_POSITION_MULTIPLIER = 1.6
REDIS_PRICE_TTL_SECONDS = 10.0
REDIS_CLIENT = redis.Redis(**redis_config())

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


def load_pending_single_order_by_id(order_id: int) -> Dict[str, Any] | None:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    single_name,
                    strategy,
                    param,
                    position_type,
                    exposure_pct,
                    status,
                    created_at
                FROM single_scheduled_orders
                WHERE id = %s
                LIMIT 1
                """,
                (int(order_id),),
            )
            row = cursor.fetchone()
    finally:
        conn.close()

    return _serialize_order_row(row) if row else None


def mark_single_order_executed(order_id: int) -> bool:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE single_scheduled_orders
                SET status = 'HOLDING',
                    created_at = NOW()
                WHERE id = %s AND status = 'PENDING'
                """,
                (int(order_id),),
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


def wait_for_order_trade(ib, trade, symbol: str, label: str, timeout: int = 120):
    start = time.time()

    while True:
        status = str(trade.orderStatus.status or "")
        filled = trade.orderStatus.filled
        remaining = trade.orderStatus.remaining
        log(f"[{label} STATUS] {symbol} | Status: {status} | Filled: {filled} | Remaining: {remaining}")

        if status in FAILED_ORDER_STATUSES:
            raise RuntimeError(f"{symbol}: {status}")

        if status == "Filled":
            log(f"[{label}] filled")
            return

        if time.time() - start > timeout:
            raise RuntimeError(f"Timeout waiting {label.lower()} fill for {symbol}")

        ib.sleep(1)


def wait_price(ib, ticker, timeout: int = 120):
    start = time.time()
    last_log_at = 0.0
    while True:
        for attr_name in ("last", "marketPrice", "close"):
            try:
                if attr_name == "marketPrice":
                    price = ticker.marketPrice()
                else:
                    price = getattr(ticker, attr_name, None)
                if price is not None and not math.isnan(price) and float(price) > 0:
                    return float(price)
            except Exception:
                pass

        elapsed = time.time() - start
        if elapsed > timeout:
            return None
        if elapsed - last_log_at >= 10:
            log(f"[PRICE WAIT] waiting realtime price... {elapsed:.0f}/{timeout}s")
            last_log_at = elapsed

        ib.sleep(0.2)


def get_redis_price(symbol: str, max_age_seconds: float = REDIS_PRICE_TTL_SECONDS, allow_stale: bool = False):
    symbol_key = str(symbol or "").strip().upper()
    if not symbol_key:
        return None

    try:
        price_raw = REDIS_CLIENT.get(f"price:{symbol_key}")
        ts_raw = REDIS_CLIENT.get(f"ts:{symbol_key}")
    except Exception as exc:
        log(f"[REDIS PRICE] unavailable for {symbol_key}: {exc}")
        return None

    if price_raw is None:
        return None

    try:
        price = float(price_raw)
    except (TypeError, ValueError):
        return None

    if price <= 0:
        return None

    if ts_raw is not None:
        try:
            age = time.time() - float(ts_raw)
        except (TypeError, ValueError):
            age = None
        if age is not None and age > max_age_seconds and not allow_stale:
            log(f"[REDIS PRICE] {symbol_key} stale age={age:.1f}s")
            return None
        if age is not None and age > max_age_seconds:
            log(f"[REDIS PRICE] {symbol_key} using stale fallback age={age:.1f}s")

    return price


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

    if base_ccy in {"RMB", "CNY"}:
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


def _place_pending_single_order_core(ib, account: str, order_row: Dict[str, Any]) -> Dict[str, Any]:
    from ib_insync import MarketOrder, Stock
    from program_factory.execution import cap_target_gross_to_total_exposure

    order_id = int(order_row["id"])
    symbol = str(order_row["single_name"]).strip().upper()
    exposure_pct = float(order_row["exposure_pct"])
    position_type = str(order_row["position_type"] or "").strip().upper()
    gross_target = (
        MAX_POSITION_MULTIPLIER
        * float(order_row["net_liq_usd"])
        * (exposure_pct / 100.0)
    )

    if position_type not in {"LONG_SPREAD", "SHORT_SPREAD"}:
        raise RuntimeError(f"Unsupported position_type for single order: {position_type}")

    log("\n===== MANUAL SINGLE OPEN =====")
    log(f"[ORDER] id={order_id} | stock={symbol} | type={position_type}")
    log(
        f"[CFG] exposure={exposure_pct:.2f}% of {MAX_POSITION_MULTIPLIER:.1f}x net liq "
        f"| gross_target={gross_target:.2f} USD"
    )

    contract = Stock(symbol, "SMART", "USD")
    log(f"[CONTRACT] qualifying {symbol}")
    ib.qualifyContracts(contract)

    price = get_redis_price(symbol)
    if price is not None:
        log(f"[PRICE] {symbol}={price:.4f} from Redis")
    else:
        log(f"[PRICE] requesting realtime price for {symbol}")
        ticker = ib.reqMktData(contract, "", False, False)
        price = wait_price(ib, ticker, timeout=120)
        try:
            ib.cancelMktData(contract)
        except Exception:
            pass
        if price is None:
            price = get_redis_price(symbol, allow_stale=True)
            if price is not None:
                log(f"[PRICE] {symbol}={price:.4f} from stale Redis fallback")

    if price is None or price <= 0:
        raise RuntimeError(f"Price unavailable for {symbol}: {price}")
    log(f"[PRICE] {symbol}={price:.4f}")

    current_qty = 0
    positions = ib.positions()
    for pos in positions:
        if getattr(pos, "account", None) != account:
            continue
        if pos.contract.symbol == symbol:
            current_qty = pos.position
            break

    capped_gross_target, risk_info = cap_target_gross_to_total_exposure(
        ib_client=ib,
        account=account,
        requested_gross=gross_target,
        net_liq_usd=float(order_row["net_liq_usd"]),
        target_symbols=[symbol],
        target_prices={symbol: price},
        positions=positions,
    )
    log(
        "[RISK] current gross="
        f"{risk_info['current_total_gross']:.2f} USD "
        f"({risk_info['current_total_pct']:.2f}%) | "
        f"allowed target={risk_info['allowed_target_gross']:.2f} USD"
    )
    if risk_info["capped"]:
        log(
            f"[RISK] gross target capped: {gross_target:.2f} -> "
            f"{capped_gross_target:.2f} USD"
        )
        gross_target = capped_gross_target

    target_abs_qty = int(gross_target / price)
    if target_abs_qty <= 0:
        raise RuntimeError(f"Target share size too small for {symbol}: qty={target_abs_qty}")

    target_qty = target_abs_qty if position_type == "LONG_SPREAD" else -target_abs_qty
    order_delta = int(target_qty - current_qty)
    action, qty_final = get_action(order_delta)

    mode = "OPEN" if abs(current_qty) < 1 else "TARGET"
    log(f"[POSITION] current -> {symbol}={current_qty}")
    log(f"[MODE] {mode}")
    log(f"[TARGET] desired -> {symbol}={target_qty}")
    log(f"[ORDER] delta -> {symbol}={order_delta}")

    if not action:
        raise RuntimeError(f"No valid share quantity to place for {symbol}")

    order = MarketOrder(action, qty_final)
    order.account = account
    log(f"[SUBMIT] {symbol}: {action} {qty_final}")
    trade = ib.placeOrder(contract, order)
    wait_for_order_trade(ib, trade, symbol, mode)

    return {
        "success": True,
        "order_id": order_id,
        "single_name": symbol,
        "mode": mode,
        "position_type": position_type,
        "exposure_pct": exposure_pct,
        "gross_target": gross_target,
        "price": price,
        "current_qty": current_qty,
        "target_qty": target_qty,
        "order_delta": order_delta,
        "message": f"Placed {mode} order for {symbol}",
    }


def manual_place_single_order(order_id: int) -> Dict[str, Any]:
    log(f"[MANUAL SINGLE OPEN] requested order_id={order_id}")
    order_row = load_pending_single_order_by_id(order_id)
    if not order_row:
        log(f"[ERROR] single order id={order_id} not found")
        return {
            "success": False,
            "order_id": order_id,
            "message": f"Single order id={order_id} not found",
        }

    if str(order_row.get("status") or "").upper() != "PENDING":
        log(f"[ERROR] single order id={order_id} is not PENDING")
        return {
            "success": False,
            "order_id": order_id,
            "single_name": order_row.get("single_name"),
            "message": f"Single order id={order_id} is not PENDING",
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
        result = _place_pending_single_order_core(ib, account_ctx["account"], payload)
        updated = mark_single_order_executed(order_id)
        result["db_updated"] = updated
        if not updated:
            log(f"[DB] single order id={order_id} NOT updated to HOLDING")
            result["success"] = False
            result["message"] = f"Single order placed but DB row id={order_id} was not updated to HOLDING"
        else:
            log(f"[DB] single order id={order_id} -> HOLDING")
            log(f"[DONE] {result['single_name']} is now HOLDING")
            log_trade_event({
                "event": "OPEN",
                "kind": "single",
                "single_name": result.get("single_name"),
                "exposure_pct": result.get("exposure_pct"),
            })
        return result
    except Exception as exc:
        log(f"[ERROR] {exc}")
        return {
            "success": False,
            "order_id": order_id,
            "single_name": order_row.get("single_name"),
            "message": str(exc),
        }
    finally:
        ib.disconnect()
        log("[IB] disconnected")


def main():
    parser = argparse.ArgumentParser(description="Manual open one single_scheduled_orders row.")
    parser.add_argument("--order-id", type=int, required=True, help="Single PENDING order id.")
    args = parser.parse_args()

    log("===== Manual Single Open Console =====")
    print(manual_place_single_order(args.order_id))


if __name__ == "__main__":
    main()
