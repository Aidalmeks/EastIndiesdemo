"""Low-frequency integrity watchdog for US 5-minute market bars.

``livedata.py`` owns the live tick-derived bar stream. This process is not in
the trading signal path; it only audits the archival database and invokes
``data_repair`` when closed 5-minute slots are missing.
"""

import asyncio
from datetime import datetime, timedelta, timezone

asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import IB, Stock
import pymysql

from data_repair import RepairJob, repair_job
from program_factory.market_schedule import (
    BJ_TZ,
    NY_TZ,
    get_current_or_next_session,
    wait_until,
)
from program_factory.port_listener import choose_connection


CLIENT_ID = 9
PREFERRED_PORT_ORDER = (4001, 4002)
PORT_ACCOUNT_MAP = {
    4001: "U18559662",
    4002: "DUO671947",
}
IB_HOST = "127.0.0.1"
USE_RTH = True
AUDIT_INTERVAL_SECONDS = 15 * 60
HISTORICAL_VISIBILITY_LAG_SECONDS = 15 * 60
FIRST_AUDIT_DELAY_SECONDS = 5 * 60 + HISTORICAL_VISIBILITY_LAG_SECONDS + 10
FINAL_AUDIT_BUFFER_SECONDS = HISTORICAL_VISIBILITY_LAG_SECONDS + 10

MEMORY_DB_CONFIG = dict(
    host="localhost",
    user="quant",
    password="1q2w3e",
    database="quant_memory",
    cursorclass=pymysql.cursors.DictCursor,
)

DATA_DB_CONFIG = dict(
    host="localhost",
    user="quant",
    password="1q2w3e",
    database="quant_data",
    autocommit=True,
)


ib = IB()
CONNECT_PORT = None
TARGET_ACCOUNT = None
SESSION_CONTRACT = Stock("SPY", "SMART", "USD")


def select_ib_connection():
    selection = choose_connection(
        port_account_map=PORT_ACCOUNT_MAP,
        preferred_order=PREFERRED_PORT_ORDER,
    )
    print(
        f"[PORT] selected={selection.port}, account={selection.account}, "
        f"pid={selection.pid}, process={selection.process_name}, reason={selection.reason}"
    )
    return selection


def connect_ib():
    global CONNECT_PORT, TARGET_ACCOUNT
    if ib.isConnected():
        return
    selection = select_ib_connection()
    CONNECT_PORT = selection.port
    TARGET_ACCOUNT = selection.account
    ib.connect(IB_HOST, CONNECT_PORT, clientId=CLIENT_ID)
    ib.qualifyContracts(SESSION_CONTRACT)
    print(f"✅ LiveRepair watchdog connected | Port={CONNECT_PORT} | Account={TARGET_ACCOUNT}")


def reconnect_ib(max_retry=9999):
    retry = 0
    while retry < max_retry:
        try:
            if ib.isConnected():
                ib.disconnect()
            connect_ib()
            if ib.isConnected():
                print("✅ LiveRepair watchdog reconnected")
                return
        except Exception as exc:
            print(f"❌ LiveRepair reconnect failed: {exc}")
        retry += 1
        sleep_seconds = min(60, 2 + retry * 2)
        ib.sleep(sleep_seconds)


def on_disconnect():
    # Do not reconnect synchronously from inside ib_insync's event callback;
    # the main loop performs reconnection without re-entering the event loop.
    print("⚠ LiveRepair watchdog disconnected")


ib.disconnectedEvent += on_disconnect


def symbols_from_trade_key(value):
    return {
        part.strip().upper()
        for part in str(value or "").split("_")
        if part.strip()
    }


def load_repair_symbols():
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT pair_name AS trade_key
                FROM pair_reentry
                WHERE pair_name IS NOT NULL AND pair_name <> ''

                UNION

                SELECT pair_name AS trade_key
                FROM scheduled_orders
                WHERE status IN ('PENDING', 'HOLDING')
                  AND pair_name IS NOT NULL AND pair_name <> ''

                UNION

                SELECT single_name AS trade_key
                FROM single_scheduled_orders
                WHERE status IN ('PENDING', 'HOLDING')
                  AND single_name IS NOT NULL AND single_name <> ''
                """
            )
            symbols = set()
            for row in cursor.fetchall():
                symbols |= symbols_from_trade_key(row.get("trade_key"))
            return sorted(symbols)
    finally:
        conn.close()


def audit_session(data_conn, session_open, as_of):
    symbols = load_repair_symbols()
    if not symbols:
        print("ℹ️ LiveRepair audit skipped: no active symbols")
        return 0

    data_conn.ping(reconnect=True)
    beijing_session_date = session_open.astimezone(BJ_TZ).date()
    historical_as_of = as_of - timedelta(seconds=HISTORICAL_VISIBILITY_LAG_SECONDS)
    if historical_as_of <= session_open:
        print(
            "ℹ️ LiveRepair audit skipped: no bar has reached the "
            "15-minute historical-visibility window"
        )
        return 0
    job = RepairJob(beijing_session_date, tuple(symbols))
    print(
        f"\n🔎 LiveRepair audit | BJ date={beijing_session_date} | "
        f"symbols={','.join(symbols)} | now={as_of.astimezone(BJ_TZ):%H:%M:%S} BJ | "
        f"historical_cutoff={historical_as_of.astimezone(BJ_TZ):%H:%M:%S} BJ"
    )
    with data_conn.cursor() as cursor:
        return repair_job(
            ib,
            cursor,
            job,
            dry_run=False,
            as_of_utc=historical_as_of.astimezone(timezone.utc),
        )


def collect_loop():
    print("🚀 LiveRepair gap watchdog started; livedata.py is the primary bar writer")
    connect_ib()
    data_conn = pymysql.connect(**DATA_DB_CONFIG)

    try:
        while True:
            try:
                if not ib.isConnected():
                    reconnect_ib()

                now_ny = datetime.now(NY_TZ)
                state, session_open, session_close = get_current_or_next_session(
                    ib=ib,
                    contract=SESSION_CONTRACT,
                    use_rth=USE_RTH,
                    now_ny=now_ny,
                    days_ahead=7,
                )

                if state is None:
                    print("⚠ LiveRepair could not determine a market session; retry in 60s")
                    ib.sleep(60)
                    continue

                if state == "BEFORE_SESSION":
                    wait_until(ib, session_open, label="LIVE_REPAIR_AUDIT_WAIT")
                    continue

                first_audit_at = session_open + timedelta(seconds=FIRST_AUDIT_DELAY_SECONDS)
                if now_ny < first_audit_at:
                    wait_until(ib, first_audit_at, label="LIVE_REPAIR_FIRST_AUDIT")
                    continue

                audit_session(data_conn, session_open, now_ny)

                now_ny = datetime.now(NY_TZ)
                if now_ny < session_close:
                    wake_at = min(
                        now_ny + timedelta(seconds=AUDIT_INTERVAL_SECONDS),
                        session_close + timedelta(seconds=FINAL_AUDIT_BUFFER_SECONDS),
                    )
                    ib.sleep(max(0, (wake_at - now_ny).total_seconds()))
                    if datetime.now(NY_TZ) >= session_close:
                        audit_session(
                            data_conn,
                            session_open,
                            session_close + timedelta(seconds=FINAL_AUDIT_BUFFER_SECONDS),
                        )

            except KeyboardInterrupt:
                print("\n🛑 LiveRepair watchdog stopped")
                break
            except Exception as exc:
                print(f"⚠ LiveRepair audit error: {exc}")
                if not ib.isConnected():
                    reconnect_ib()
                ib.sleep(5)
    finally:
        data_conn.close()
        if ib.isConnected():
            ib.disconnect()


if __name__ == "__main__":
    collect_loop()
