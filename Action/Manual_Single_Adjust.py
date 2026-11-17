from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from Action.Manual_Single_Open import (
    CLIENT_ID,
    _ensure_event_loop,
    _place_pending_single_order_core,
    get_db_connection,
    log,
    resolve_account_context,
)
from runtime_settings import ib_host


def load_holding_single_order_by_id(order_id: int) -> Dict[str, Any] | None:
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
                WHERE id = %s AND status = 'HOLDING'
                LIMIT 1
                """,
                (int(order_id),),
            )
            return cursor.fetchone()
    finally:
        conn.close()


def manual_adjust_single(order_id: int) -> Dict[str, Any]:
    log(f"[MANUAL SINGLE ADJUST] requested order_id={order_id}")
    order_row = load_holding_single_order_by_id(order_id)
    if not order_row:
        log(f"[ERROR] HOLDING single order id={order_id} not found")
        return {
            "success": False,
            "order_id": order_id,
            "message": f"HOLDING single order id={order_id} not found",
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
        result["message"] = f"Adjusted {result['single_name']} to current exposure"
        log(f"[DONE] {result['single_name']} adjusted")
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
    parser = argparse.ArgumentParser(description="Adjust one HOLDING single_scheduled_orders row.")
    parser.add_argument("--order-id", type=int, required=True, help="Single HOLDING order id.")
    args = parser.parse_args()

    log("===== Manual Single Adjust Console =====")
    print(manual_adjust_single(args.order_id))


if __name__ == "__main__":
    main()
