#!/usr/bin/env python3
"""Scan every active unordered US daily pair on the latest 80 trading dates."""

import argparse
import csv
import itertools
import math
import os
import shutil
import subprocess
import time
import warnings
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Iterator, Sequence

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import numpy as np
import pandas as pd
import pymysql
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    Task,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.text import Text
from statsmodels.tsa.stattools import adfuller


DB_CONFIG = {
    "host": "127.0.0.1",
    "user": "quant",
    "password": "1q2w3e",
    "database": "quant_data",
}
OUTPUT_DIR = Path(__file__).resolve().parent
TRADING_DAYS = 80
ADF_ABS_MIN = 3.0
FIELDNAMES = [
    "pair_name",
    "y",
    "x",
    "train_start",
    "train_end",
    "train_trading_days",
    "obs",
    "beta",
    "intercept",
    "adf_stat",
    "adf_abs",
    "adf_pvalue",
    "spread_mean",
    "spread_sigma",
    "two_sigma_return_pct",
    "latest_z",
]

_VALUES: np.ndarray
_SYMBOLS: Sequence[str]
_TRAIN_START: str
_TRAIN_END: str

warnings.filterwarnings(
    "ignore",
    message="pandas only supports SQLAlchemy connectable",
    category=UserWarning,
)


class PairSpeedColumn(ProgressColumn):
    def render(self, task: Task) -> Text:
        if task.speed is None:
            return Text("-- pairs/s")
        return Text(f"{task.speed:,.0f} pairs/s")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan C(N,2) unordered pairs using the latest 80 daily observations."
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--chunk-size", type=int, default=500)
    parser.add_argument(
        "--max-pairs",
        type=int,
        default=0,
        help="Optional validation cap; 0 scans every pair.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=10_000,
        help="Minimum reporting interval for non-interactive output.",
    )
    parser.add_argument("--output", help="Optional final CSV path inside adf_finder.")
    return parser.parse_args()


def load_latest_daily_matrix() -> tuple[pd.DataFrame, list[str]]:
    conn = pymysql.connect(**DB_CONFIG)
    try:
        active_symbols = pd.read_sql(
            """
            SELECT ticker
            FROM us_day_info
            WHERE is_active = TRUE
            ORDER BY ticker
            """,
            conn,
        )
        dates = pd.read_sql(
            """
            SELECT DISTINCT d.trade_date
            FROM us_market_days d
            JOIN us_day_info i
              ON i.ticker = d.ticker
             AND i.is_active = TRUE
            ORDER BY d.trade_date DESC
            LIMIT %s
            """,
            conn,
            params=(TRADING_DAYS,),
        )
        if len(dates) != TRADING_DAYS:
            raise RuntimeError(
                f"Expected {TRADING_DAYS} market dates, found {len(dates)}."
            )
        train_start = pd.to_datetime(dates["trade_date"]).min().date()
        raw = pd.read_sql(
            """
            SELECT d.trade_date, d.ticker, d.price
            FROM us_market_days d
            JOIN us_day_info i
              ON i.ticker = d.ticker
             AND i.is_active = TRUE
            WHERE d.trade_date >= %s
            ORDER BY d.trade_date, d.ticker
            """,
            conn,
            params=(train_start,),
        )
    finally:
        conn.close()

    raw["trade_date"] = pd.to_datetime(raw["trade_date"])
    matrix = raw.pivot(index="trade_date", columns="ticker", values="price").sort_index()
    symbols = [
        str(symbol).strip().upper()
        for symbol in active_symbols["ticker"].dropna()
        if str(symbol).strip()
    ]
    matrix = matrix.reindex(columns=symbols)
    return matrix, symbols


def init_worker(
    values: np.ndarray,
    symbols: Sequence[str],
    train_start: str,
    train_end: str,
) -> None:
    global _VALUES, _SYMBOLS, _TRAIN_START, _TRAIN_END
    _VALUES = values
    _SYMBOLS = symbols
    _TRAIN_START = train_start
    _TRAIN_END = train_end


def score_pair(i: int, j: int) -> tuple[str, list[object] | None]:
    y = _VALUES[:, i]
    x = _VALUES[:, j]
    valid = np.isfinite(y) & np.isfinite(x)
    obs = int(valid.sum())
    if obs != TRADING_DAYS:
        return "incomplete", None

    y = y[valid]
    x = x[valid]
    if float(np.var(x)) <= 1e-12:
        return "invalid", None

    try:
        beta, intercept = np.polyfit(x, y, 1)
        spread = y - (beta * x + intercept)
        adf_result = adfuller(spread, autolag="AIC")
    except Exception:
        return "invalid", None

    adf_stat = float(adf_result[0])
    if not math.isfinite(adf_stat):
        return "invalid", None
    adf_abs = abs(adf_stat)
    if adf_abs <= ADF_ABS_MIN:
        return "below_threshold", None

    spread_mean = float(np.mean(spread))
    spread_sigma = float(np.std(spread, ddof=1))
    latest_z = (
        float((spread[-1] - spread_mean) / spread_sigma)
        if math.isfinite(spread_sigma) and spread_sigma > 1e-12
        else math.nan
    )
    # Gross deployed notional is positive on both legs regardless of hedge
    # direction. A negative beta means the legs trade in the same direction;
    # it must not subtract X notional from the return denominator.
    denominator = float(y[-1] + abs(beta) * x[-1])
    two_sigma_return_pct = (
        float(2.0 * spread_sigma / denominator * 100.0)
        if math.isfinite(denominator) and abs(denominator) > 1e-12
        else math.nan
    )
    y_symbol = _SYMBOLS[i]
    x_symbol = _SYMBOLS[j]
    row = [
        f"{y_symbol.lower()}_{x_symbol.lower()}",
        y_symbol,
        x_symbol,
        _TRAIN_START,
        _TRAIN_END,
        TRADING_DAYS,
        obs,
        float(beta),
        float(intercept),
        adf_stat,
        adf_abs,
        float(adf_result[1]),
        spread_mean,
        spread_sigma,
        two_sigma_return_pct,
        latest_z,
    ]
    return "hit", row


def score_chunk(chunk: list[tuple[int, int]]) -> tuple[int, int, int, int, list[list[object]]]:
    incomplete = 0
    invalid = 0
    below_threshold = 0
    rows: list[list[object]] = []
    for i, j in chunk:
        status, row = score_pair(i, j)
        if status == "hit" and row is not None:
            rows.append(row)
        elif status == "incomplete":
            incomplete += 1
        elif status == "invalid":
            invalid += 1
        else:
            below_threshold += 1
    return len(chunk), incomplete, invalid, below_threshold, rows


def pair_chunks(
    symbol_count: int,
    chunk_size: int,
    max_pairs: int,
) -> Iterator[list[tuple[int, int]]]:
    pairs: Iterator[tuple[int, int]] = itertools.combinations(range(symbol_count), 2)
    if max_pairs:
        pairs = itertools.islice(pairs, max_pairs)
    while True:
        chunk = list(itertools.islice(pairs, chunk_size))
        if not chunk:
            return
        yield chunk


def sort_csv_by_adf_abs(partial_path: Path, final_path: Path) -> int:
    body_path = partial_path.with_suffix(".body")
    sorted_body_path = partial_path.with_suffix(".sorted_body")
    with partial_path.open("r", newline="", encoding="utf-8") as source, body_path.open(
        "w", newline="", encoding="utf-8"
    ) as body:
        next(source, None)
        shutil.copyfileobj(source, body)

    env = dict(os.environ)
    env["LC_ALL"] = "C"
    subprocess.run(
        [
            "sort",
            "-t,",
            "-k11,11gr",
            str(body_path),
            "-o",
            str(sorted_body_path),
        ],
        check=True,
        env=env,
    )
    with final_path.open("w", newline="", encoding="utf-8") as final_file:
        writer = csv.writer(final_file)
        writer.writerow(FIELDNAMES)
        with sorted_body_path.open("r", newline="", encoding="utf-8") as sorted_body:
            shutil.copyfileobj(sorted_body, final_file)

    row_count = 0
    with final_path.open("r", newline="", encoding="utf-8") as result:
        row_count = max(sum(1 for _ in result) - 1, 0)
    partial_path.unlink()
    body_path.unlink()
    sorted_body_path.unlink()
    return row_count


def main() -> int:
    args = parse_args()
    if args.workers <= 0:
        raise ValueError("--workers must be positive")
    if args.chunk_size <= 0:
        raise ValueError("--chunk-size must be positive")
    if args.max_pairs < 0:
        raise ValueError("--max-pairs cannot be negative")

    load_started = time.perf_counter()
    matrix, symbols = load_latest_daily_matrix()
    values = np.ascontiguousarray(matrix.to_numpy(dtype=np.float64))
    train_start = matrix.index[0].date().isoformat()
    train_end = matrix.index[-1].date().isoformat()
    pair_total = len(symbols) * (len(symbols) - 1) // 2
    if args.max_pairs:
        pair_total = min(pair_total, args.max_pairs)
    load_seconds = time.perf_counter() - load_started

    if args.output:
        final_path = (OUTPUT_DIR / args.output).resolve()
        if final_path.parent != OUTPUT_DIR.resolve():
            raise ValueError("--output must resolve inside adf_finder")
    else:
        final_path = (
            OUTPUT_DIR
            / f"adf_80d_active_pairs_adf3_{datetime.now():%Y%m%d_%H%M%S}.csv"
        )
    partial_path = final_path.with_suffix(".partial.csv")

    console = Console()
    console.print(
        f"loaded symbols={len(symbols)} dates={len(matrix)} "
        f"range={train_start}..{train_end} pairs={pair_total} "
        f"workers={args.workers} adf_abs_min={ADF_ABS_MIN:.1f} "
        f"universe=us_day_info.is_active=1 load={load_seconds:.2f}s",
    )

    processed = incomplete = invalid = below_threshold = hits = 0
    scan_started = time.perf_counter()
    interactive_progress = console.is_terminal
    fallback_step = max(
        int(args.progress_every),
        int(math.ceil(pair_total / 10)),
        1,
    )
    next_fallback_progress = fallback_step
    progress = Progress(
        TextColumn("[bold cyan]ADF scan"),
        BarColumn(bar_width=32),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TextColumn("hits={task.fields[hits_text]}"),
        TextColumn("hit_rate={task.fields[hit_rate_text]}"),
        PairSpeedColumn(),
        TimeElapsedColumn(),
        TextColumn("ETA"),
        TimeRemainingColumn(),
        console=console,
        refresh_per_second=4,
        disable=not interactive_progress,
    )
    with partial_path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.writer(output_file)
        writer.writerow(FIELDNAMES)
        with progress:
            progress_task = progress.add_task(
                "scan",
                total=pair_total,
                hits_text="0",
                hit_rate_text="0.00%",
            )
            with Pool(
                processes=args.workers,
                initializer=init_worker,
                initargs=(values, symbols, train_start, train_end),
            ) as pool:
                for chunk_result in pool.imap_unordered(
                    score_chunk,
                    pair_chunks(len(symbols), args.chunk_size, args.max_pairs),
                    chunksize=1,
                ):
                    chunk_count, chunk_incomplete, chunk_invalid, chunk_below, rows = chunk_result
                    processed += chunk_count
                    incomplete += chunk_incomplete
                    invalid += chunk_invalid
                    below_threshold += chunk_below
                    hits += len(rows)
                    writer.writerows(rows)
                    progress.update(
                        progress_task,
                        advance=chunk_count,
                        hits_text=f"{hits:,}",
                        hit_rate_text=f"{hits / processed:.2%}",
                    )

                    if processed >= next_fallback_progress or processed == pair_total:
                        output_file.flush()
                        if not interactive_progress:
                            elapsed = time.perf_counter() - scan_started
                            rate = processed / elapsed if elapsed else 0.0
                            eta = (pair_total - processed) / rate if rate else math.nan
                            console.print(
                                f"progress={processed}/{pair_total} "
                                f"({processed / pair_total:.0%}) hits={hits:,} "
                                f"hit_rate={hits / processed:.2%} "
                                f"rate={rate:,.0f} pairs/s ETA={eta / 60:.1f}m"
                            )
                        next_fallback_progress = processed + fallback_step

    with console.status("[bold cyan]Sorting matched rows by |ADF|...", spinner="dots"):
        sorted_rows = sort_csv_by_adf_abs(partial_path, final_path)
    elapsed = time.perf_counter() - scan_started
    console.print(
        f"complete processed={processed} hits={hits} sorted_rows={sorted_rows} "
        f"incomplete={incomplete} invalid={invalid} below_threshold={below_threshold} "
        f"elapsed={elapsed / 60:.2f}m csv={final_path}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
