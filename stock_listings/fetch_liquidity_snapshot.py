"""Download a resumable 7-session dollar-volume snapshot with yfinance."""

import argparse
import csv
import json
import math
import os
import statistics
import time
from datetime import datetime

import pandas as pd
import yfinance as yf


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NASDAQ_FILE = os.getenv(
    "NASDAQ_LISTED_FILE", "/home/quant/Downloads/nasdaqlisted.txt"
)
OTHER_FILE = os.getenv(
    "OTHER_LISTED_FILE", "/home/quant/Downloads/otherlisted.txt"
)
OUTPUT_FILE = os.path.join(BASE_DIR, "liquidity_snapshot.csv")
META_FILE = os.path.join(BASE_DIR, "liquidity_snapshot.meta.json")


def load_symbols():
    items = []
    seen = set()
    sources = (
        (NASDAQ_FILE, "Symbol", "NASDAQ"),
        (OTHER_FILE, "ACT Symbol", "OTHER"),
    )
    for path, symbol_field, source in sources:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle, delimiter="|"):
                symbol = str(row.get(symbol_field) or "").strip().upper()
                if not symbol or symbol.startswith("FILE CREATION TIME:"):
                    continue
                if str(row.get("Test Issue") or "").strip().upper() == "Y":
                    continue
                key = (symbol, source)
                if key in seen:
                    continue
                seen.add(key)
                items.append({
                    "symbol": symbol,
                    "yahoo_symbol": yahoo_symbol(symbol),
                    "source": source,
                })
    items.sort(key=lambda item: (item["symbol"], item["source"]))
    return items


def yahoo_symbol(symbol):
    """Translate common CTA symbols into Yahoo's punctuation conventions."""
    value = str(symbol or "").strip().upper()
    if "$" in value:
        base, series = value.split("$", 1)
        return f"{base}-P{series}"
    suffixes = {
        ".U": "-UN",
        ".W": "-WT",
        ".R": "-RT",
    }
    for suffix, replacement in suffixes.items():
        if value.endswith(suffix):
            return value[:-len(suffix)] + replacement
    return value.replace(".", "-")


def finite_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def symbol_frame(download, symbol, symbol_count):
    if download is None or download.empty:
        return pd.DataFrame()
    if symbol_count == 1 and not isinstance(download.columns, pd.MultiIndex):
        return download.copy()
    try:
        return download[symbol].copy()
    except (KeyError, TypeError):
        return pd.DataFrame()


def summarize_frame(frame, sessions):
    if frame.empty or "Close" not in frame or "Volume" not in frame:
        return None
    clean = frame[["Close", "Volume"]].copy()
    clean["Close"] = pd.to_numeric(clean["Close"], errors="coerce")
    clean["Volume"] = pd.to_numeric(clean["Volume"], errors="coerce")
    clean = clean.dropna(subset=["Close", "Volume"])
    clean = clean[(clean["Close"] > 0) & (clean["Volume"] >= 0)].tail(sessions)
    if clean.empty:
        return None

    dollar_volume = (clean["Close"] * clean["Volume"]).tolist()
    last = clean.iloc[-1]
    return {
        "status": "ok",
        "sessions": int(len(clean)),
        "asof": clean.index[-1].date().isoformat(),
        "close_last": round(float(last["Close"]), 6),
        "volume_last": int(last["Volume"]),
        "dollar_volume_last": round(float(dollar_volume[-1]), 2),
        "dollar_volume_avg_7d": round(float(statistics.fmean(dollar_volume)), 2),
        "dollar_volume_median_7d": round(float(statistics.median(dollar_volume)), 2),
        "dollar_volume_total_7d": round(float(sum(dollar_volume)), 2),
    }


def existing_results(resume):
    if not resume or not os.path.exists(OUTPUT_FILE):
        return {}
    frame = pd.read_csv(OUTPUT_FILE, dtype={"symbol": str, "source": str})
    return {
        (str(row["symbol"]), str(row["source"])): {
            key: (None if pd.isna(value) else value)
            for key, value in row.items()
        }
        for _, row in frame.iterrows()
        if str(row.get("status") or "") == "ok"
    }


def save_results(results):
    columns = [
        "symbol", "yahoo_symbol", "source", "status", "sessions", "asof",
        "close_last", "volume_last", "dollar_volume_last",
        "dollar_volume_avg_7d", "dollar_volume_median_7d",
        "dollar_volume_total_7d", "error",
    ]
    rows = sorted(results.values(), key=lambda row: (row["symbol"], row["source"]))
    temporary = OUTPUT_FILE + ".tmp"
    pd.DataFrame(rows, columns=columns).to_csv(temporary, index=False)
    os.replace(temporary, OUTPUT_FILE)


def download_batch(items, sessions, threads, timeout):
    yahoo_symbols = list(dict.fromkeys(item["yahoo_symbol"] for item in items))
    data = yf.download(
        yahoo_symbols,
        period="1mo",
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        actions=False,
        threads=threads,
        progress=False,
        timeout=timeout,
    )
    summaries = {}
    for symbol in yahoo_symbols:
        frame = symbol_frame(data, symbol, len(yahoo_symbols))
        summaries[symbol] = summarize_frame(frame, sessions)
    return summaries


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="Only process the first N symbols")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--sessions", type=int, default=7)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--sleep", type=float, default=2.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--min-batch-success",
        type=float,
        default=0.25,
        help="Abort when a batch success ratio falls below this threshold",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    universe = load_symbols()
    if args.limit > 0:
        universe = universe[:args.limit]

    results = existing_results(args.resume)
    pending = [
        item for item in universe
        if (item["symbol"], item["source"]) not in results
    ]
    batch_total = max(1, math.ceil(len(pending) / args.batch_size))
    started = time.time()
    print(
        f"Universe {len(universe):,}; already complete {len(results):,}; "
        f"pending {len(pending):,}; {batch_total} batches",
        flush=True,
    )

    for batch_index, start in enumerate(range(0, len(pending), args.batch_size), 1):
        items = pending[start:start + args.batch_size]
        summaries = {}
        last_error = ""
        for attempt in range(1, args.retries + 2):
            try:
                summaries = download_batch(
                    items, args.sessions, args.threads, args.timeout
                )
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt <= args.retries:
                    delay = args.sleep * (2 ** (attempt - 1))
                    print(
                        f"Batch {batch_index}/{batch_total} attempt {attempt} "
                        f"failed; retrying in {delay:.1f}s: {last_error}",
                        flush=True,
                    )
                    time.sleep(delay)

        ok_count = 0
        for item in items:
            summary = summaries.get(item["yahoo_symbol"])
            row = {**item}
            if summary:
                row.update(summary)
                row["error"] = ""
                ok_count += 1
            else:
                row.update({
                    "status": "no_data",
                    "sessions": 0,
                    "asof": "",
                    "close_last": None,
                    "volume_last": None,
                    "dollar_volume_last": None,
                    "dollar_volume_avg_7d": None,
                    "dollar_volume_median_7d": None,
                    "dollar_volume_total_7d": None,
                    "error": last_error or "No usable daily bars returned",
                })
            results[(item["symbol"], item["source"])] = row

        success_ratio = ok_count / len(items) if items else 0
        if success_ratio < args.min_batch_success:
            raise RuntimeError(
                f"Circuit breaker: batch {batch_index} returned only "
                f"{ok_count}/{len(items)} usable symbols. Results were not saved; "
                "wait and resume later."
            )

        save_results(results)
        elapsed = time.time() - started
        completed = min(start + len(items), len(pending))
        rate = completed / elapsed if elapsed > 0 else 0
        remaining = (len(pending) - completed) / rate if rate > 0 else 0
        print(
            f"Batch {batch_index}/{batch_total}: {ok_count}/{len(items)} ok; "
            f"saved {len(results):,}; ETA {remaining / 60:.1f} min",
            flush=True,
        )
        if start + args.batch_size < len(pending):
            time.sleep(args.sleep)

    ok_total = sum(row.get("status") == "ok" for row in results.values())
    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "universe_count": len(universe),
        "result_count": len(results),
        "ok_count": ok_total,
        "failed_count": len(results) - ok_total,
        "sessions": args.sessions,
        "metric": "unadjusted close × reported volume",
        "output": OUTPUT_FILE,
    }
    with open(META_FILE, "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2)
    print(json.dumps(meta, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
