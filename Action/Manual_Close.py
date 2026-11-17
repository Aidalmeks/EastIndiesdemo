import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())
import argparse
import os
import sys
from decimal import Decimal, ROUND_HALF_UP
import math
import pymysql
from ib_insync import IB, Stock, MarketOrder
import time

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from program_factory.port_listener import choose_connection
from runtime_settings import database_config, ib_host

# ===== Manual Config (edit in VSCode) =====
CLIENT_ID = 4
PREFERRED_PORT_ORDER = (4001, 4002)

SYMBOL_Y = "AA"
SYMBOL_X = "CENX"

# For manual close, keep TOTAL_NOTIONAL = 0
TOTAL_NOTIONAL = 0

# Optional override. If None, uses f"{SYMBOL_Y.lower()}_{SYMBOL_X.lower()}"
PAIR_NAME = None


def wait_price(ib, ticker, timeout=999):
    start = time.time()
    while True:
        price = ticker.marketPrice()
        if price is not None and not math.isnan(price):
            return float(price)

        if time.time() - start > timeout:
            return None
        ib.sleep(0.2)


def normalize_result_value(return_pct):
    if return_pct is None:
        return None
    return Decimal(str(return_pct)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def compute_pair_return_pct(pos_y, pos_x, price_y, price_x):
    qty_y = float(pos_y.get("qty", 0))
    qty_x = float(pos_x.get("qty", 0))
    avg_y = float(pos_y.get("avg_price", 0))
    avg_x = float(pos_x.get("avg_price", 0))

    pnl_y = qty_y * (price_y - avg_y)
    pnl_x = qty_x * (price_x - avg_x)
    total_pnl = pnl_y + pnl_x

    entry_notional = abs(qty_y * avg_y) + abs(qty_x * avg_x)
    if entry_notional <= 0:
        return 0.0

    return total_pnl / entry_notional * 100.0


def update_scheduled_order_closed(pair_name_candidates, return_pct):
    result_value = normalize_result_value(return_pct)

    conn = pymysql.connect(**database_config("memory"))

    try:
        with conn.cursor() as cursor:
            sql = """
            UPDATE scheduled_orders
            SET status = 'CLOSED',
                result = %s,
                close_time = NOW()
            WHERE pair_name = %s
              AND status = 'HOLDING'
            LIMIT 1
            """

            for pair_name in pair_name_candidates:
                cursor.execute(sql, (result_value, pair_name))
                if cursor.rowcount > 0:
                    conn.commit()
                    print(f"[DB] CLOSED {pair_name} | result={result_value}")
                    return True

            conn.commit()
            print(f"[DB] no HOLDING row matched in {pair_name_candidates}")
            return False
    finally:
        conn.close()


def delete_pair_reentry_row(pair_name_candidates):
    """DELETE matching row from pair_reentry when position is manually closed."""
    conn = pymysql.connect(**database_config("memory"))

    try:
        with conn.cursor() as cursor:
            sql = "DELETE FROM pair_reentry WHERE pair_name = %s LIMIT 1"
            for pair_name in pair_name_candidates:
                cursor.execute(sql, (pair_name,))
                if cursor.rowcount > 0:
                    conn.commit()
                    print(f"[DB] DELETED pair_reentry {pair_name}")
                    return True

            conn.commit()
            print(f"[DB] no pair_reentry row matched in {pair_name_candidates}")
            return False
    finally:
        conn.close()


def manual_close_pair(pair_name: str):
    normalized_pair_name = str(pair_name or "").strip().lower()
    if not normalized_pair_name or "_" not in normalized_pair_name:
        return {
            "success": False,
            "pair_name": pair_name,
            "message": "pair_name must look like aaa_bbb",
        }

    symbol_y, symbol_x = normalized_pair_name.split("_", 1)
    symbol_y = symbol_y.upper()
    symbol_x = symbol_x.upper()

    pair_name_primary = normalized_pair_name
    pair_name_reverse = f"{symbol_x.lower()}_{symbol_y.lower()}"
    pair_name_candidates = [pair_name_primary]
    if pair_name_reverse != pair_name_primary:
        pair_name_candidates.append(pair_name_reverse)

    selection = choose_connection(preferred_order=PREFERRED_PORT_ORDER)
    connect_port = selection.port
    account = selection.account

    ib = IB()
    ib.connect(ib_host(), connect_port, clientId=CLIENT_ID)
    print(
        f"[PORT] selected={connect_port}, account={account}, pid={selection.pid}, "
        f"process={selection.process_name}, reason={selection.reason}"
    )

    try:
        positions = {}
        for p in ib.positions():
            positions[p.contract.symbol] = {
                "qty": p.position,
                "avg_price": p.avgCost
            }

        pos_y = positions.get(symbol_y, {"qty": 0, "avg_price": 0})
        pos_x = positions.get(symbol_x, {"qty": 0, "avg_price": 0})

        qty_y = pos_y["qty"]
        qty_x = pos_x["qty"]
        print(f"[POS] {symbol_y}: {qty_y}, {symbol_x}: {qty_x}")

        if qty_y == 0 and qty_x == 0:
            return {
                "success": False,
                "pair_name": pair_name_primary,
                "message": f"No open position found for {pair_name_primary}",
            }

        contract_y = Stock(symbol_y, "SMART", "USD")
        contract_x = Stock(symbol_x, "SMART", "USD")
        ib.qualifyContracts(contract_y, contract_x)

        ticker_y = ib.reqMktData(contract_y, "", False, False)
        ticker_x = ib.reqMktData(contract_x, "", False, False)
        price_y = wait_price(ib, ticker_y)
        price_x = wait_price(ib, ticker_x)

        if price_y is None:
            price_y = float(pos_y["avg_price"] or 0)
        if price_x is None:
            price_x = float(pos_x["avg_price"] or 0)

        return_pct = compute_pair_return_pct(pos_y, pos_x, price_y, price_x)
        print(f"[RET] {pair_name_primary}: {return_pct:.4f}%")

        trades = []
        if qty_y != 0:
            action_y = "SELL" if qty_y > 0 else "BUY"
            order_y = MarketOrder(action_y, abs(qty_y))
            order_y.account = account
            trades.append(ib.placeOrder(contract_y, order_y))
            print(f"[ORDER] {symbol_y}: {action_y} {abs(qty_y)}")

        if qty_x != 0:
            action_x = "SELL" if qty_x > 0 else "BUY"
            order_x = MarketOrder(action_x, abs(qty_x))
            order_x.account = account
            trades.append(ib.placeOrder(contract_x, order_x))
            print(f"[ORDER] {symbol_x}: {action_x} {abs(qty_x)}")

        ib.sleep(2)

        all_ok = True
        for t in trades:
            status = t.orderStatus.status
            print(f"[STATUS] {status}")
            if status in ["Cancelled", "Inactive", "ApiCancelled"]:
                all_ok = False

        if all_ok:
            updated = update_scheduled_order_closed(pair_name_candidates, return_pct)
            if updated:
                deleted = delete_pair_reentry_row(pair_name_candidates)
                lock_note = "pair_reentry deleted" if deleted else "pair_reentry not deleted"
                return {
                    "success": True,
                    "pair_name": pair_name_primary,
                    "return_pct": round(float(return_pct), 4),
                    "message": f"Closed {pair_name_primary}; {lock_note}",
                }

            return {
                "success": False,
                "pair_name": pair_name_primary,
                "return_pct": round(float(return_pct), 4),
                "message": f"Orders placed but scheduled_orders was not updated for {pair_name_primary}",
            }

        print("[WARN] close order not fully successful, DB not updated")
        return {
            "success": False,
            "pair_name": pair_name_primary,
            "return_pct": round(float(return_pct), 4),
            "message": f"Close order not fully successful for {pair_name_primary}",
        }
    except Exception as exc:
        return {
            "success": False,
            "pair_name": pair_name_primary,
            "message": str(exc),
        }
    finally:
        ib.disconnect()


def main():
    parser = argparse.ArgumentParser(description="Manually close one HOLDING pair.")
    parser.add_argument("--pair-name", help="Pair name like sblk_bhp")
    args = parser.parse_args()

    if TOTAL_NOTIONAL != 0:
        raise ValueError("Manual_Close requires TOTAL_NOTIONAL = 0")

    if args.pair_name:
        pair_name = args.pair_name.strip().lower()
    else:
        symbol_y = SYMBOL_Y.upper()
        symbol_x = SYMBOL_X.upper()
        pair_name = PAIR_NAME or f"{symbol_y.lower()}_{symbol_x.lower()}"

    print(manual_close_pair(pair_name))


if __name__ == "__main__":
    main()
