"""Backfill adjusted US daily closes from IB into quant_data.us_market_days.

The symbols are read from quant_data.us_day_info.  The default run processes
the active base-rank 1..4154 universe and stores completed daily bars from
2026-01-01 onward.
"""

import argparse
import asyncio
import csv
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

asyncio.set_event_loop(asyncio.new_event_loop())

import pymysql
import pytz
from ib_insync import IB, Stock


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


MYSQL_CONFIG = {
    "host": "127.0.0.1",
    "user": "quant",
    "password": "1q2w3e",
    "database": "quant_data",
    "autocommit": False,
}

IB_HOST = "127.0.0.1"
IB_PORT = 4001
DEFAULT_CLIENT_ID = 1

START_DATE = date(2026, 1, 1)
BAR_SIZE = "1 day"
DURATION = "1 Y"
WHAT_TO_SHOW = "ADJUSTED_LAST"
USE_RTH = True
SOURCE = "adjusted_last"
FALLBACK_SOURCE = "trades_fallback"
REQUEST_PAUSE_SECONDS = 0.75
DEFAULT_END_RANK = 4154
LOG_DIR = Path(__file__).resolve().parent / "logs" / "build_quantdatanew"
PRIMARY_EXCHANGE_MAP = {
    "NASDAQ": "NASDAQ",
    "NYSE": "NYSE",
    "NYSE Arca": "ARCA",
    "NYSE American": "AMEX",
    "Cboe BZX": "BATS",
    "IEX": "IEX",
}
DIRECT_EXCHANGE_MAP = {
    "NASDAQ": "ISLAND",
    "NYSE": "NYSE",
    "NYSE Arca": "ARCA",
    "NYSE American": "AMEX",
    "Cboe BZX": "BATS",
    "IEX": "IEX",
}

NY = pytz.timezone("America/New_York")


def parse_args():
    parser = argparse.ArgumentParser(
        description="从 IB Gateway 回填基排名前 4154 名证券的调整后日线收盘价。"
    )
    parser.add_argument("--start-rank", type=int, default=1, help="起始基排名")
    parser.add_argument(
        "--end-rank",
        type=int,
        default=DEFAULT_END_RANK,
        help=f"结束基排名，默认 {DEFAULT_END_RANK}",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="只处理前 N 个候选，适合先做小批量测试",
    )
    parser.add_argument(
        "--client-id",
        type=int,
        default=DEFAULT_CLIENT_ID,
        help="IB API clientId",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="只检查并列出候选池，不连接 IB Gateway",
    )
    return parser.parse_args()


def load_symbols(cursor, start_rank, end_rank, limit=None):
    sql = """
        SELECT ticker, listing_exchange, base_rank
        FROM us_day_info
        WHERE is_active = TRUE
          AND base_rank BETWEEN %s AND %s
        ORDER BY base_rank
    """
    params = [start_rank, end_rank]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(max(0, limit))

    cursor.execute(sql, params)
    return cursor.fetchall()


def latest_stored_day(cursor, ticker):
    cursor.execute(
        """
        SELECT MAX(trade_date)
        FROM us_market_days
        WHERE ticker = %s
        """,
        (ticker,),
    )
    return cursor.fetchone()[0]


def completed_day_cutoff():
    """Return the latest completed US weekday, excluding weekends.

    This deliberately excludes today's candle even after the close.  A daily
    run therefore picks it up on the next US weekday without ever treating a
    Saturday or Sunday as a missing market date.
    """
    cutoff = datetime.now(NY).date() - timedelta(days=1)
    while cutoff.weekday() >= 5:
        cutoff -= timedelta(days=1)
    return cutoff


def bar_trade_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:8], "%Y%m%d").date()


def make_contract(ticker, listing_exchange):
    # IB uses a space for class shares such as BRK B, while the listing uses BRK.B.
    ib_symbol = ticker.replace(".", " ")
    primary_exchange = PRIMARY_EXCHANGE_MAP.get(listing_exchange)
    kwargs = (
        {"primaryExchange": primary_exchange}
        if primary_exchange
        else {}
    )
    return Stock(ib_symbol, "SMART", "USD", **kwargs)


def make_direct_contract(ticker, listing_exchange):
    """Build a direct-listed contract for symbols unsupported by SMART/BEST."""
    ib_symbol = ticker.replace(".", " ")
    exchange = DIRECT_EXCHANGE_MAP.get(listing_exchange)
    if not exchange:
        return None
    return Stock(ib_symbol, exchange, "USD")


class AnomalyLog:
    def __init__(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = LOG_DIR / f"anomalies_{timestamp}.csv"
        self.handle = self.path.open("w", encoding="utf-8-sig", newline="")
        self.writer = csv.writer(self.handle)
        self.writer.writerow(
            ["logged_at", "base_rank", "ticker", "status", "details"]
        )
        self.handle.flush()
        self.count = 0

    def write(self, base_rank, ticker, status, details):
        self.writer.writerow(
            [
                datetime.now().isoformat(timespec="seconds"),
                base_rank,
                ticker,
                status,
                details,
            ]
        )
        self.handle.flush()
        self.count += 1

    def close(self):
        self.handle.close()


def install_ib_error_collector(ib):
    errors = defaultdict(list)

    def on_error(req_id, error_code, error_string, contract):
        symbol = getattr(contract, "symbol", "") if contract else ""
        if symbol:
            errors[symbol].append(
                f"IB error {error_code}, reqId {req_id}: {error_string}"
            )

    ib.errorEvent += on_error
    return errors


def pop_ib_errors(errors, ticker):
    ib_symbol = ticker.replace(".", " ")
    return " | ".join(errors.pop(ib_symbol, []))


def request_daily_bars(ib, contract, what_to_show):
    return ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=DURATION,
        barSizeSetting=BAR_SIZE,
        whatToShow=what_to_show,
        useRTH=USE_RTH,
        formatDate=1,
        keepUpToDate=False,
    )


def fetch_and_store(ib, conn, cursor, symbol_row, cutoff, ib_errors):
    ticker, listing_exchange, base_rank = symbol_row
    latest = latest_stored_day(cursor, ticker)
    wanted_from = max(START_DATE, latest + timedelta(days=1)) if latest else START_DATE

    if wanted_from > cutoff:
        print(f"[{base_rank:04d}] {ticker:<8} 已是最新")
        return "current", 0, ""

    contract = make_contract(ticker, listing_exchange)
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        print(f"[{base_rank:04d}] {ticker:<8} 合约解析失败")
        details = pop_ib_errors(ib_errors, ticker) or "IB 无法解析该合约"
        return "failed", 0, details

    bars = request_daily_bars(ib, qualified[0], WHAT_TO_SHOW)
    bar_source = SOURCE
    request_route = "smart_adjusted"
    retry_details = []

    if not bars:
        smart_error = pop_ib_errors(ib_errors, ticker)
        retry_details.append(
            smart_error or "SMART ADJUSTED_LAST 未返回数据"
        )

        direct_contract = make_direct_contract(ticker, listing_exchange)
        direct_qualified = (
            ib.qualifyContracts(direct_contract)
            if direct_contract is not None
            else []
        )
        if direct_qualified:
            ib.sleep(0.25)
            bars = request_daily_bars(
                ib, direct_qualified[0], WHAT_TO_SHOW
            )
            request_route = "direct_adjusted"

            if not bars:
                direct_adjusted_error = pop_ib_errors(ib_errors, ticker)
                retry_details.append(
                    direct_adjusted_error
                    or "直接交易所 ADJUSTED_LAST 未返回数据"
                )
                ib.sleep(0.25)
                bars = request_daily_bars(
                    ib, direct_qualified[0], "TRADES"
                )
                bar_source = FALLBACK_SOURCE
                request_route = "direct_trades"
        else:
            retry_details.append("直接上市交易所合约解析失败")

    rows = []
    for bar in bars:
        trade_date = bar_trade_date(bar.date)
        if wanted_from <= trade_date <= cutoff:
            rows.append((ticker, trade_date, float(bar.close), bar_source))

    if not rows:
        final_error = pop_ib_errors(ib_errors, ticker)
        if final_error:
            retry_details.append(final_error)
        details = " | ".join(retry_details) or (
            f"IB 未返回 {wanted_from} 至 {cutoff} 的日线"
        )
        print(
            f"[{base_rank:04d}] {ticker:<8} 未返回所需日期 "
            f"({wanted_from} 至 {cutoff})"
        )
        return "empty", 0, details

    cursor.executemany(
        """
        INSERT INTO us_market_days (ticker, trade_date, price, source)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            price = VALUES(price),
            source = VALUES(source)
        """,
        rows,
    )
    conn.commit()

    print(
        f"[{base_rank:04d}] {ticker:<8} "
        f"{rows[0][1]} → {rows[-1][1]}，写入 {len(rows)} 条 "
        f"({bar_source}, {request_route})"
    )
    remaining_error = pop_ib_errors(ib_errors, ticker)
    if request_route != "smart_adjusted":
        details = " | ".join(retry_details) or (
            f"SMART 失败，已通过 {request_route} 获取"
        )
        if remaining_error:
            details = f"{details} | {remaining_error}"
        return "fallback", len(rows), details
    return "stored", len(rows), remaining_error


def main():
    args = parse_args()
    if args.start_rank < 1 or args.end_rank < args.start_rank:
        raise SystemExit("排名范围无效")

    conn = pymysql.connect(**MYSQL_CONFIG)
    cursor = conn.cursor()
    ib = None
    anomaly_log = None

    try:
        symbols = load_symbols(
            cursor,
            start_rank=args.start_rank,
            end_rank=args.end_rank,
            limit=args.limit,
        )
        print(
            f"候选池：{len(symbols)} 个证券，"
            f"基排名 {args.start_rank}–{args.end_rank}"
        )

        if not symbols:
            raise SystemExit("us_day_info 中没有符合条件的证券")

        if args.list_only:
            for ticker, _listing_exchange, base_rank in symbols:
                print(f"#{base_rank:<4} {ticker}")
            return

        cutoff = completed_day_cutoff()
        print(
            f"目标日期：{START_DATE} → {cutoff}；"
            f"{BAR_SIZE} / {WHAT_TO_SHOW} / RTH={USE_RTH}"
        )

        ib = IB()
        ib.connect(IB_HOST, IB_PORT, clientId=args.client_id)
        ib_errors = install_ib_error_collector(ib)
        anomaly_log = AnomalyLog()
        print(f"异常日志：{anomaly_log.path}")

        stats = {
            "stored": 0,
            "fallback": 0,
            "current": 0,
            "empty": 0,
            "failed": 0,
        }
        total_rows = 0

        for symbol_row in symbols:
            ticker = symbol_row[0]
            try:
                status, row_count, details = fetch_and_store(
                    ib, conn, cursor, symbol_row, cutoff, ib_errors
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                conn.rollback()
                status, row_count = "failed", 0
                ib_detail = pop_ib_errors(ib_errors, ticker)
                details = f"{type(exc).__name__}: {exc}"
                if ib_detail:
                    details = f"{details} | {ib_detail}"
                print(f"[{symbol_row[2]:04d}] {ticker:<8} 失败：{exc}")

            stats[status] += 1
            total_rows += row_count
            if status in {"fallback", "empty", "failed"}:
                anomaly_log.write(
                    symbol_row[2], ticker, status, details
                )
            if status != "current":
                ib.sleep(REQUEST_PAUSE_SECONDS)

        print(
            "\n完成："
            f"调整日线 {stats['stored']}，TRADES回退 {stats['fallback']}，"
            f"已最新 {stats['current']}，"
            f"无数据 {stats['empty']}，失败 {stats['failed']}，"
            f"本次日线 {total_rows} 条"
        )
        print(f"异常记录 {anomaly_log.count} 条：{anomaly_log.path}")
    finally:
        if anomaly_log is not None:
            anomaly_log.close()
        cursor.close()
        conn.close()
        if ib is not None and ib.isConnected():
            ib.disconnect()


if __name__ == "__main__":
    main()
