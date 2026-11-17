"""Repair missing US RTH 5-minute bars in ``us_market_prices``.

Dates are Beijing dates on which the US session opens.  Configure REPAIR_JOBS
below, or pass jobs on the command line, for example:

    python data_repair.py --job 2026-07-17:ADM,MOS
    python data_repair.py --job 2026-07-14:HCC,CLF,TLT,RKT --dry-run
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from typing import Iterable

asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import IB, Stock, util
import pymysql
import pytz

from program_factory.port_listener import choose_connection


# Edit this list when running without command-line arguments.
# Example: (("2026-07-17", ("ADM", "MOS")),)
REPAIR_JOBS = ()

CLIENT_ID = 87
PREFERRED_PORT_ORDER = (4001, 4002)
PORT_ACCOUNT_MAP = {4001: "U18559662", 4002: "DUO671947"}
BAR_SIZE = "5 mins"
HISTORICAL_ATTEMPTS = 3
REQUEST_PAUSE_SECONDS = 0.5
SOURCE = "IBRepair"

NY_TZ = pytz.timezone("US/Eastern")
BJ_TZ = pytz.timezone("Asia/Shanghai")

DATA_DB_CONFIG = dict(
    host="localhost",
    user="quant",
    password="1q2w3e",
    database="quant_data",
    autocommit=True,
)


@dataclass(frozen=True)
class RepairJob:
    beijing_date: date
    tickers: tuple[str, ...]


def normalize_tickers(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({str(value).strip().upper() for value in values if str(value).strip()}))


def parse_job(value: str) -> RepairJob:
    try:
        date_text, ticker_text = value.split(":", 1)
        job_date = date.fromisoformat(date_text.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "job must look like 2026-07-17:ADM,MOS"
        ) from exc
    tickers = normalize_tickers(ticker_text.split(","))
    if not tickers:
        raise argparse.ArgumentTypeError("a repair job needs at least one ticker")
    return RepairJob(job_date, tickers)


def configured_jobs() -> list[RepairJob]:
    jobs = []
    for date_text, tickers in REPAIR_JOBS:
        jobs.append(RepairJob(date.fromisoformat(str(date_text)), normalize_tickers(tickers)))
    return jobs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Repair missing US RTH 5-minute bars; dates use Beijing time.",
    )
    parser.add_argument(
        "--job",
        action="append",
        type=parse_job,
        default=[],
        metavar="BJ_DATE:TICKER,TICKER",
        help="repeat for multiple Beijing dates",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="inspect and fetch missing bars without inserting them",
    )
    return parser


def session_bounds(job_date: date) -> tuple[datetime, datetime]:
    """Return the normal NY RTH bounds for the Beijing session-open date."""
    open_ny = NY_TZ.localize(datetime.combine(job_date, datetime_time(9, 30)))
    close_ny = NY_TZ.localize(datetime.combine(job_date, datetime_time(16, 0)))
    return open_ny, close_ny


def bar_time_to_utc(bar_date) -> datetime:
    value = util.parseIBDatetime(bar_date) if isinstance(bar_date, str) else bar_date
    if value.tzinfo is None:
        value = NY_TZ.localize(value)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def request_day_bars(ib: IB, contract, close_ny: datetime, attempt: int):
    # Shift retries by a second so IB does not see byte-for-byte identical
    # historical requests inside its pacing window.  Five-minute bars are
    # unchanged by this offset.
    request_end = close_ny + timedelta(seconds=attempt - 1)
    return ib.reqHistoricalData(
        contract,
        endDateTime=request_end.astimezone(timezone.utc),
        durationStr="1 D",
        barSizeSetting=BAR_SIZE,
        whatToShow="TRADES",
        useRTH=True,
        formatDate=2,
        keepUpToDate=False,
        timeout=30,
    )


def collect_ib_rows(
    ib: IB,
    contract,
    open_ny: datetime,
    close_ny: datetime,
    needed_times: set[datetime] | None = None,
    expected_count: int | None = None,
):
    open_utc = open_ny.astimezone(timezone.utc).replace(tzinfo=None)
    close_utc = close_ny.astimezone(timezone.utc).replace(tzinfo=None)
    collected = {}
    for attempt in range(1, HISTORICAL_ATTEMPTS + 1):
        bars = request_day_bars(ib, contract, close_ny, attempt)
        for bar in bars:
            time_utc = bar_time_to_utc(bar.date)
            if open_utc <= time_utc < close_utc:
                collected[time_utc] = float(bar.close)
        print(f"    IB attempt {attempt}: collected {len(collected)} unique bars")
        if needed_times is not None and needed_times <= collected.keys():
            break
        if expected_count is not None and len(collected) >= expected_count:
            break
        # Normal RTH has 78 bars and a standard 13:00 early close has 42.
        if needed_times is None and len(collected) in (42, 78):
            break
        if attempt < HISTORICAL_ATTEMPTS:
            ib.sleep(REQUEST_PAUSE_SECONDS)
    return collected


def expected_times_from_spy(
    ib: IB,
    job_date: date,
    as_of_utc: datetime | None = None,
) -> tuple[list[datetime], datetime, datetime]:
    open_ny, normal_close_ny = session_bounds(job_date)
    request_close_ny = normal_close_ny
    expected_count = None
    if as_of_utc is not None:
        if as_of_utc.tzinfo is None:
            as_of_utc = as_of_utc.replace(tzinfo=timezone.utc)
        as_of_ny = as_of_utc.astimezone(NY_TZ)
        elapsed_bars = max(
            0,
            int((min(as_of_ny, normal_close_ny) - open_ny).total_seconds() // 300),
        )
        request_close_ny = min(
            normal_close_ny,
            open_ny + timedelta(minutes=5 * elapsed_bars),
        )
        expected_count = elapsed_bars
        if request_close_ny <= open_ny:
            return [], open_ny, request_close_ny

    spy = Stock("SPY", "SMART", "USD")
    ib.qualifyContracts(spy)
    rows = collect_ib_rows(
        ib,
        spy,
        open_ny,
        request_close_ny,
        expected_count=expected_count,
    )
    if not rows:
        raise RuntimeError(
            f"SPY has no RTH bars for Beijing date {job_date}; "
            "the date may be a US market holiday"
        )

    # The last liquid SPY bar identifies an early close.  Building a regular
    # grid from open to that point also detects a bar omitted by a single SPY
    # historical response.
    detected_close_utc = max(rows) + timedelta(minutes=5)
    open_utc = open_ny.astimezone(timezone.utc).replace(tzinfo=None)
    request_close_utc = request_close_ny.astimezone(timezone.utc).replace(tzinfo=None)
    close_utc = min(detected_close_utc, request_close_utc)
    expected = []
    current = open_utc
    while current < close_utc:
        expected.append(current)
        current += timedelta(minutes=5)
    return expected, open_ny, close_utc.replace(tzinfo=timezone.utc).astimezone(NY_TZ)


def load_contract_info(cursor, ticker: str):
    cursor.execute(
        "SELECT exchange, currency FROM us_info WHERE ticker=%s",
        (ticker,),
    )
    row = cursor.fetchone()
    if not row:
        raise RuntimeError(f"{ticker} is missing from us_info")
    return row[0] or "SMART", row[1] or "USD"


def load_existing_times(cursor, ticker: str, expected: list[datetime]) -> set[datetime]:
    cursor.execute(
        """
        SELECT time_utc
        FROM us_market_prices
        WHERE ticker=%s
          AND time_utc BETWEEN %s AND %s
        """,
        (ticker, expected[0], expected[-1]),
    )
    return {row[0] for row in cursor.fetchall()}


def insert_missing_rows(cursor, ticker: str, rows: dict[datetime, float], missing: set[datetime]) -> int:
    inserted = 0
    for time_utc in sorted(missing & rows.keys()):
        cursor.execute(
            """
            INSERT IGNORE INTO us_market_prices
                (ticker, time_utc, price, source)
            VALUES (%s, %s, %s, %s)
            """,
            (ticker, time_utc, rows[time_utc], SOURCE),
        )
        inserted += cursor.rowcount
    return inserted


def format_bj_times(times: Iterable[datetime]) -> str:
    values = []
    for value in sorted(times):
        aware_utc = value.replace(tzinfo=timezone.utc)
        values.append(aware_utc.astimezone(BJ_TZ).strftime("%m-%d %H:%M"))
    return ", ".join(values)


def repair_job(
    ib: IB,
    cursor,
    job: RepairJob,
    dry_run: bool,
    as_of_utc: datetime | None = None,
) -> int:
    expected, open_ny, close_ny = expected_times_from_spy(
        ib,
        job.beijing_date,
        as_of_utc=as_of_utc,
    )
    if not expected:
        print(f"ℹ️ Beijing {job.beijing_date}: no closed RTH bars to audit yet")
        return 0
    print(
        f"\n===== Beijing {job.beijing_date} | "
        f"NY {open_ny:%Y-%m-%d %H:%M}-{close_ny:%H:%M} | "
        f"expected={len(expected)} ====="
    )
    unresolved_total = 0

    for ticker in job.tickers:
        existing = load_existing_times(cursor, ticker, expected)
        missing = set(expected) - existing
        if not missing:
            print(f"✅ {ticker}: complete ({len(expected)}/{len(expected)})")
            continue

        print(f"🔧 {ticker}: missing {len(missing)} | BJ {format_bj_times(missing)}")
        exchange, currency = load_contract_info(cursor, ticker)
        contract = Stock(ticker, exchange, currency)
        ib.qualifyContracts(contract)
        ib_rows = collect_ib_rows(ib, contract, open_ny, close_ny, missing)

        inserted = 0 if dry_run else insert_missing_rows(cursor, ticker, ib_rows, missing)
        repaired = missing & ib_rows.keys()
        unresolved = missing - ib_rows.keys()
        if dry_run:
            print(f"    available={len(repaired)}, unresolved={len(unresolved)}")
        else:
            print(f"    inserted={inserted}, unresolved_from_IB={len(unresolved)}")
        if unresolved:
            print(f"    ⚠ unresolved BJ: {format_bj_times(unresolved)}")
        unresolved_total += len(unresolved)

        if not dry_run:
            remaining = set(expected) - load_existing_times(cursor, ticker, expected)
            if len(remaining) != len(unresolved):
                print(f"    ⚠ DB verification mismatch: remaining={len(remaining)}")
                unresolved_total += abs(len(remaining) - len(unresolved))
            else:
                print(f"    DB verified: {len(expected) - len(remaining)}/{len(expected)}")
        ib.sleep(REQUEST_PAUSE_SECONDS)

    return unresolved_total


def main() -> int:
    args = build_parser().parse_args()
    jobs = args.job or configured_jobs()
    if not jobs:
        print("No repair jobs. Edit REPAIR_JOBS or pass --job 2026-07-17:ADM,MOS")
        return 2

    selection = choose_connection(
        port_account_map=PORT_ACCOUNT_MAP,
        preferred_order=PREFERRED_PORT_ORDER,
    )
    print(
        f"[PORT] selected={selection.port}, account={selection.account}, "
        f"reason={selection.reason}"
    )
    ib = IB()
    conn = None
    try:
        ib.connect("127.0.0.1", selection.port, clientId=CLIENT_ID)
        conn = pymysql.connect(**DATA_DB_CONFIG)
        with conn.cursor() as cursor:
            unresolved = sum(repair_job(ib, cursor, job, args.dry_run) for job in jobs)
    finally:
        if conn is not None:
            conn.close()
        if ib.isConnected():
            ib.disconnect()

    if unresolved:
        print(f"\n⚠ Repair finished with {unresolved} unresolved bar(s)")
        return 1
    print("\n✅ Repair finished; all requested sessions are complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
