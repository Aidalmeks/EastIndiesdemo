#!/usr/bin/env python3
import argparse
import itertools
import math
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import pymysql
from statsmodels.tsa.stattools import adfuller


DATA_DB_CONFIG = {
    "host": "localhost",
    "user": "quant",
    "password": "1q2w3e",
    "database": "quant_data",
}

MEMORY_DB_CONFIG = {
    "host": "localhost",
    "user": "quant",
    "password": "1q2w3e",
    "database": "quant_memory",
}

PRICE_LOOKUP_INDEX = "idx_us_market_prices_ticker_time_price"
PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "logs" / "pair_adf_finder"

warnings.filterwarnings(
    "ignore",
    message="pandas only supports SQLAlchemy connectable",
    category=UserWarning,
)


def normalize_symbol(value) -> str:
    return str(value or "").strip().upper()


def normalize_pair_key(value) -> str:
    return str(value or "").strip().lower().replace(" / ", "_").replace("/", "_")


def db_connect(config: Dict[str, str], dict_cursor: bool = False):
    cursorclass = pymysql.cursors.DictCursor if dict_cursor else pymysql.cursors.Cursor
    return pymysql.connect(**config, cursorclass=cursorclass)


def pair_from_key(pair_key: str) -> Optional[Tuple[str, str]]:
    parts = normalize_pair_key(pair_key).split("_")
    if len(parts) != 2:
        return None
    y = normalize_symbol(parts[0])
    x = normalize_symbol(parts[1])
    if not y or not x or y == x:
        return None
    return y, x


def load_metapairs(active_only: bool = True) -> Dict[str, Tuple[str, str, str]]:
    sql = """
        SELECT pair_name, ticker_a, ticker_b
        FROM metapairs
    """
    if active_only:
        sql += " WHERE COALESCE(is_active, 1) = 1"
    sql += " ORDER BY pair_name"

    pairs: Dict[str, Tuple[str, str, str]] = {}
    conn = db_connect(DATA_DB_CONFIG, dict_cursor=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            for row in cursor.fetchall():
                pair_key = normalize_pair_key(row.get("pair_name"))
                y = normalize_symbol(row.get("ticker_a"))
                x = normalize_symbol(row.get("ticker_b"))
                if pair_key and y and x and y != x:
                    pairs[pair_key] = (pair_key, y, x)
    finally:
        conn.close()
    return pairs


def load_reentry_pairs() -> Dict[str, Tuple[str, str, str]]:
    pairs: Dict[str, Tuple[str, str, str]] = {}
    conn = db_connect(MEMORY_DB_CONFIG, dict_cursor=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT pair_name
                FROM pair_reentry
                WHERE pair_name IS NOT NULL
                  AND pair_name <> ''
                ORDER BY pair_name
                """
            )
            for row in cursor.fetchall():
                pair_key = normalize_pair_key(row.get("pair_name"))
                symbols = pair_from_key(pair_key)
                if symbols:
                    pairs[pair_key] = (pair_key, symbols[0], symbols[1])
    finally:
        conn.close()
    return pairs


def load_registered_pairs(source: str, active_only: bool = True) -> List[Tuple[str, str, str]]:
    source = str(source or "all").strip().lower()
    pairs: Dict[str, Tuple[str, str, str]] = {}
    if source in {"all", "metapairs"}:
        pairs.update(load_metapairs(active_only=active_only))
    if source in {"all", "reentry"}:
        pairs.update(load_reentry_pairs())
    return sorted(pairs.values())


def load_us_info_symbols(active_only: bool = True) -> List[str]:
    sql = "SELECT ticker FROM us_info"
    if active_only:
        sql += " WHERE COALESCE(is_active, 0) = 1"
    sql += " ORDER BY ticker"

    conn = db_connect(DATA_DB_CONFIG, dict_cursor=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            return [normalize_symbol(row.get("ticker")) for row in cursor.fetchall() if row.get("ticker")]
    finally:
        conn.close()


def parse_symbols(raw: str) -> List[str]:
    symbols = []
    for item in str(raw or "").replace(";", ",").split(","):
        symbol = normalize_symbol(item)
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def build_combo_pairs(symbols: Sequence[str], max_pairs: Optional[int] = None) -> List[Tuple[str, str, str]]:
    clean_symbols = sorted({normalize_symbol(symbol) for symbol in symbols if normalize_symbol(symbol)})
    pairs = []
    for y, x in itertools.combinations(clean_symbols, 2):
        pairs.append((f"{y.lower()}_{x.lower()}", y, x))
        if max_pairs and len(pairs) >= max_pairs:
            break
    return pairs


def cutoff_utc(days: int) -> datetime:
    return (datetime.now(timezone.utc) - timedelta(days=int(days))).replace(tzinfo=None)


def load_pair_prices(pair: Tuple[str, str, str], days: int) -> pd.DataFrame:
    _, y, x = pair
    conn = db_connect(DATA_DB_CONFIG)
    try:
        sql = f"""
            SELECT
                a.time_utc,
                a.price AS y_price,
                b.price AS x_price
            FROM us_market_prices a FORCE INDEX ({PRICE_LOOKUP_INDEX})
            JOIN us_market_prices b FORCE INDEX ({PRICE_LOOKUP_INDEX})
              ON b.ticker = %s
             AND b.time_utc = a.time_utc
            WHERE a.ticker = %s
              AND a.time_utc >= %s
            ORDER BY a.time_utc
        """
        df = pd.read_sql(sql, conn, params=(x, y, cutoff_utc(days)))
    finally:
        conn.close()

    if df.empty:
        return df
    df["time_utc"] = pd.to_datetime(df["time_utc"])
    return df.sort_values("time_utc").reset_index(drop=True)


def load_price_matrix(symbols: Sequence[str], days: int, bar: str) -> pd.DataFrame:
    symbols = sorted({normalize_symbol(symbol) for symbol in symbols if normalize_symbol(symbol)})
    if not symbols:
        return pd.DataFrame()

    placeholders = ",".join(["%s"] * len(symbols))
    conn = db_connect(DATA_DB_CONFIG)
    try:
        sql = f"""
            SELECT time_utc, ticker, price
            FROM us_market_prices FORCE INDEX ({PRICE_LOOKUP_INDEX})
            WHERE ticker IN ({placeholders})
              AND time_utc >= %s
            ORDER BY time_utc, ticker
        """
        df = pd.read_sql(sql, conn, params=tuple(symbols) + (cutoff_utc(days),))
    finally:
        conn.close()

    if df.empty:
        return pd.DataFrame()

    df["time_utc"] = pd.to_datetime(df["time_utc"])
    if bar == "daily":
        df["date_utc"] = df["time_utc"].dt.date
        idx = df.groupby(["ticker", "date_utc"])["time_utc"].idxmax()
        df = df.loc[idx].sort_values(["time_utc", "ticker"])
        pivot_index = "date_utc"
    else:
        pivot_index = "time_utc"

    matrix = df.pivot_table(index=pivot_index, columns="ticker", values="price", aggfunc="last")
    return matrix.sort_index()


def maybe_resample_pair_df(df: pd.DataFrame, bar: str) -> pd.DataFrame:
    if df.empty or bar != "daily":
        return df
    frame = df.copy()
    frame["date_utc"] = frame["time_utc"].dt.date
    idx = frame.groupby("date_utc")["time_utc"].idxmax()
    return frame.loc[idx].sort_values("time_utc").reset_index(drop=True)


def daily_pair_prices(df: pd.DataFrame) -> pd.DataFrame:
    """Return the last aligned pair price observed on each UTC trading date."""
    if df.empty:
        return df
    frame = df.copy()
    frame["time_utc"] = pd.to_datetime(frame["time_utc"])
    frame["date_utc"] = frame["time_utc"].dt.date
    idx = frame.groupby("date_utc", sort=True)["time_utc"].idxmax()
    return frame.loc[idx].sort_values("time_utc").reset_index(drop=True)


def prepare_prices(y_values: pd.Series, x_values: pd.Series, price_mode: str) -> Tuple[pd.Series, pd.Series]:
    y = pd.to_numeric(y_values, errors="coerce")
    x = pd.to_numeric(x_values, errors="coerce")
    if price_mode == "log":
        y = y.where(y > 0).map(math.log)
        x = x.where(x > 0).map(math.log)
    return y, x


def ols_spread(y: pd.Series, x: pd.Series) -> Tuple[Optional[float], Optional[float], pd.Series]:
    frame = pd.DataFrame({"y": y, "x": x}).replace([np.inf, -np.inf], np.nan).dropna()
    if len(frame) < 3:
        return None, None, pd.Series(dtype=float)

    x_var = float(frame["x"].var())
    if not math.isfinite(x_var) or abs(x_var) < 1e-12:
        return None, None, pd.Series(dtype=float)

    beta = float(frame["x"].cov(frame["y"]) / x_var)
    intercept = float(frame["y"].mean() - beta * frame["x"].mean())
    spread = frame["y"] - (intercept + beta * frame["x"])
    return intercept, beta, spread


def safe_adf(values: pd.Series, min_obs: int) -> Tuple[Optional[float], Optional[float]]:
    clean = pd.Series(values).replace([np.inf, -np.inf], np.nan).dropna()
    if len(clean) < min_obs:
        return None, None
    if float(clean.std(ddof=0)) <= 1e-12:
        return None, None
    try:
        result = adfuller(clean, autolag="AIC")
        return float(result[0]), float(result[1])
    except Exception:
        return None, None


def half_life(spread: pd.Series) -> Optional[float]:
    s = pd.Series(spread).replace([np.inf, -np.inf], np.nan).dropna()
    if len(s) < 10:
        return None
    lag = s.shift(1).dropna()
    delta = s.diff().dropna()
    aligned = pd.DataFrame({"lag": lag, "delta": delta}).dropna()
    if len(aligned) < 10:
        return None
    var = float(aligned["lag"].var())
    if not math.isfinite(var) or abs(var) < 1e-12:
        return None
    coef = float(aligned["lag"].cov(aligned["delta"]) / var)
    if coef >= 0:
        return None
    return float(-math.log(2) / coef)


def format_bj_time(value) -> str:
    if value is None or pd.isna(value):
        return ""
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return (ts + pd.Timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")


def score_pair(
    pair_key: str,
    y_symbol: str,
    x_symbol: str,
    y_values: pd.Series,
    x_values: pd.Series,
    time_values,
    price_mode: str,
    min_obs: int,
    source: str,
    raw_y_values: Optional[pd.Series] = None,
    raw_x_values: Optional[pd.Series] = None,
) -> Optional[Dict[str, object]]:
    y, x = prepare_prices(y_values, x_values, price_mode)
    frame = pd.DataFrame(
        {
            "time": list(time_values),
            "y": pd.Series(y).to_numpy(),
            "x": pd.Series(x).to_numpy(),
        }
    ).replace([np.inf, -np.inf], np.nan).dropna()
    if len(frame) < min_obs:
        return None

    intercept, beta, spread = ols_spread(frame["y"], frame["x"])
    if beta is None or spread.empty:
        return None

    adf_stat, adf_p = safe_adf(spread, min_obs=min_obs)
    if adf_stat is None:
        return None

    spread_std = float(spread.std(ddof=0))
    spread_sample_std = float(spread.std(ddof=1))
    latest_z = None
    if spread_std > 1e-12:
        latest_z = float((spread.iloc[-1] - spread.mean()) / spread_std)

    returns_corr = None
    ret_frame = frame[["y", "x"]].pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    if len(ret_frame) >= 3:
        corr_value = ret_frame["y"].corr(ret_frame["x"])
        if pd.notna(corr_value):
            returns_corr = float(corr_value)

    train_start = format_bj_time(frame["time"].iloc[0])
    split_time = format_bj_time(frame["time"].iloc[-1])
    two_sigma_return_pct = None
    latest_raw_y = pd.to_numeric(
        raw_y_values if raw_y_values is not None else y_values, errors="coerce"
    ).dropna()
    latest_raw_x = pd.to_numeric(
        raw_x_values if raw_x_values is not None else x_values, errors="coerce"
    ).dropna()
    if (
        price_mode == "linear"
        and not latest_raw_y.empty
        and not latest_raw_x.empty
        and math.isfinite(spread_sample_std)
    ):
        notional = (
            float(latest_raw_y.iloc[-1])
            + abs(float(beta)) * float(latest_raw_x.iloc[-1])
        )
        if math.isfinite(notional) and abs(notional) > 1e-12:
            # Match the backtest dashboards, where pandas' sample sigma (ddof=1)
            # is used for the displayed 2-sigma return.
            two_sigma_return_pct = float(2.0 * spread_sample_std / notional * 100.0)

    return {
        "pair_name": normalize_pair_key(pair_key),
        "y": y_symbol,
        "x": x_symbol,
        "source": source,
        "train_start": train_start,
        "split_time": split_time,
        "split": split_time,
        "obs": int(len(spread)),
        "adf_stat": float(adf_stat),
        "adf_abs": abs(float(adf_stat)),
        "adf_pvalue": float(adf_p),
        "beta": float(beta),
        "intercept": float(intercept),
        "spread_mean": float(spread.mean()),
        "spread_std": spread_std,
        "two_sigma_return_pct": two_sigma_return_pct,
        "latest_z": latest_z,
        "half_life_bars": half_life(spread),
        "return_corr": returns_corr,
    }


def scan_sampled_train_starts(
    pair: Tuple[str, str, str],
    days: int,
    train_start_step: int,
    min_train_days: int,
    price_mode: str,
    min_obs: int,
) -> List[Dict[str, object]]:
    """Scan an expanding-to-now training window on a trading-day start grid.

    Prices are reduced to one aligned close per trading day. Candidate starts
    move forward by ``train_start_step`` trading days, while every daily
    observation from the candidate start through the latest close remains in
    that candidate's training set.
    """
    pair_key, y_symbol, x_symbol = pair
    daily = daily_pair_prices(load_pair_prices(pair, days=days))
    required_days = max(int(min_train_days), int(min_obs))
    if len(daily) < required_days:
        return []

    last_start = len(daily) - required_days
    start_indices = list(range(0, last_start + 1, int(train_start_step)))
    if start_indices[-1] != last_start:
        start_indices.append(last_start)

    results: List[Dict[str, object]] = []
    total = len(start_indices)
    for position, start_idx in enumerate(start_indices, start=1):
        if position == 1 or position % 25 == 0 or position == total:
            print(
                f"[sampled-start] {position}/{total} {pair_key} "
                f"start={daily['time_utc'].iloc[start_idx]:%Y-%m-%d}"
            )
        window = daily.iloc[start_idx:].reset_index(drop=True)
        row = score_pair(
            pair_key=pair_key,
            y_symbol=y_symbol,
            x_symbol=x_symbol,
            y_values=window["y_price"],
            x_values=window["x_price"],
            time_values=window["time_utc"],
            price_mode=price_mode,
            min_obs=min_obs,
            source="sampled_train_start",
            raw_y_values=window["y_price"],
            raw_x_values=window["x_price"],
        )
        if row:
            row["train_trading_days"] = int(len(window))
            row["train_start_step_days"] = int(train_start_step)
            results.append(row)
    return results


def score_registered_pairs(
    pairs: Sequence[Tuple[str, str, str]],
    days: int,
    bar: str,
    price_mode: str,
    min_obs: int,
) -> List[Dict[str, object]]:
    results = []
    total = len(pairs)
    for index, pair in enumerate(pairs, start=1):
        pair_key, y, x = pair
        if index == 1 or index % 10 == 0 or index == total:
            print(f"[registered] {index}/{total} {pair_key}")
        df = maybe_resample_pair_df(load_pair_prices(pair, days=days), bar=bar)
        if df.empty:
            continue
        row = score_pair(
            pair_key=pair_key,
            y_symbol=y,
            x_symbol=x,
            y_values=df["y_price"],
            x_values=df["x_price"],
            time_values=df["time_utc"],
            price_mode=price_mode,
            min_obs=min_obs,
            source="registered",
        )
        if row:
            results.append(row)
    return results


def score_combo_pairs(
    pairs: Sequence[Tuple[str, str, str]],
    matrix: pd.DataFrame,
    price_mode: str,
    min_obs: int,
) -> List[Dict[str, object]]:
    results = []
    total = len(pairs)
    for index, (pair_key, y, x) in enumerate(pairs, start=1):
        if index == 1 or index % 500 == 0 or index == total:
            print(f"[combo] {index}/{total} {pair_key}")
        if y not in matrix.columns or x not in matrix.columns:
            continue
        row = score_pair(
            pair_key=pair_key,
            y_symbol=y,
            x_symbol=x,
            y_values=matrix[y],
            x_values=matrix[x],
            time_values=matrix.index,
            price_mode=price_mode,
            min_obs=min_obs,
            source="combo",
        )
        if row:
            results.append(row)
    return results


def format_table(df: pd.DataFrame, limit: int) -> str:
    cols = [
        "pair_name",
        "source",
        "train_start",
        "split_time",
        "obs",
        "adf_stat",
        "adf_abs",
        "adf_pvalue",
        "beta",
        "two_sigma_return_pct",
        "train_trading_days",
        "latest_z",
        "half_life_bars",
        "return_corr",
    ]
    shown = df.head(limit).copy()
    for col in [
        "adf_stat",
        "adf_abs",
        "adf_pvalue",
        "beta",
        "two_sigma_return_pct",
        "latest_z",
        "half_life_bars",
        "return_corr",
    ]:
        if col in shown.columns:
            shown[col] = shown[col].map(lambda v: "" if pd.isna(v) else f"{float(v):.4f}")
    return shown[[col for col in cols if col in shown.columns]].to_string(index=False)


def write_output(df: pd.DataFrame, label: str = "") -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"_{label}" if label else ""
    path = OUTPUT_DIR / f"pair_adf_finder{suffix}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    df.to_csv(path, index=False)
    latest_path = OUTPUT_DIR / (f"latest_{label}.csv" if label else "latest.csv")
    df.to_csv(latest_path, index=False)
    return path


def resolve_combo_symbols(args, registered_pairs: Sequence[Tuple[str, str, str]]) -> List[str]:
    if args.symbols:
        return parse_symbols(args.symbols)
    if args.universe == "us-info":
        return load_us_info_symbols(active_only=not args.include_inactive)
    if args.universe == "registered-symbols":
        symbols = []
        for _, y, x in registered_pairs:
            symbols.extend([y, x])
        return sorted(set(symbols))
    raise ValueError(f"Unsupported universe: {args.universe}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find pairs with strong recent ADF stationarity on OLS residual spread."
    )
    parser.add_argument(
        "--mode",
        choices=["registered", "combo", "both", "sampled-pair"],
        default="registered",
    )
    parser.add_argument("--registered-source", choices=["all", "metapairs", "reentry"], default="all")
    parser.add_argument("--universe", choices=["registered-symbols", "us-info"], default="registered-symbols")
    parser.add_argument("--symbols", help="Comma-separated symbols for combo mode, e.g. SBLK,BHP,MU,NVDA")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--bar", choices=["5min", "daily"], default="5min")
    parser.add_argument("--price-mode", choices=["linear", "log"], default="linear")
    parser.add_argument("--min-obs", type=int, default=80)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--max-symbols", type=int, default=80)
    parser.add_argument("--max-pairs", type=int, default=0, help="0 means no explicit cap.")
    parser.add_argument("--pvalue-max", type=float, default=1.0)
    parser.add_argument("--min-abs-corr", type=float, default=0.0)
    parser.add_argument("--include-inactive", action="store_true")
    parser.add_argument(
        "--train-start-step",
        type=int,
        default=5,
        help="Trading-day spacing between candidate starts in sampled-pair mode.",
    )
    parser.add_argument(
        "--min-train-days",
        type=int,
        default=80,
        help="Minimum daily observations retained in sampled-pair mode.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.days <= 0:
        raise ValueError("--days must be positive")
    if args.min_obs <= 5:
        raise ValueError("--min-obs should be greater than 5")
    if args.train_start_step <= 0:
        raise ValueError("--train-start-step must be positive")
    if args.min_train_days <= 5:
        raise ValueError("--min-train-days should be greater than 5")

    if args.mode == "sampled-pair":
        symbols = parse_symbols(args.symbols)
        if len(symbols) != 2:
            raise ValueError("--mode sampled-pair requires exactly two --symbols")
        pair = (f"{symbols[0].lower()}_{symbols[1].lower()}", symbols[0], symbols[1])
        sampled_results = scan_sampled_train_starts(
            pair=pair,
            days=args.days,
            train_start_step=args.train_start_step,
            min_train_days=args.min_train_days,
            price_mode=args.price_mode,
            min_obs=args.min_obs,
        )
        if not sampled_results:
            print("No sampled train starts produced valid ADF results.")
            return 2
        sampled_df = pd.DataFrame(sampled_results)
        sampled_df = sampled_df[sampled_df["adf_pvalue"] <= float(args.pvalue_max)]
        if sampled_df.empty:
            print("No sampled train starts matched the configured filters.")
            return 2
        sampled_df = sampled_df.sort_values(
            ["adf_abs", "adf_pvalue"], ascending=[False, True]
        ).reset_index(drop=True)
        output_path = write_output(sampled_df, label="sampled")
        print()
        print(format_table(sampled_df, limit=args.top))
        print()
        print(f"rows: {len(sampled_df)}")
        print(f"csv: {output_path}")
        print(f"latest: {OUTPUT_DIR / 'latest_sampled.csv'}")
        return 0

    registered_pairs = load_registered_pairs(
        source=args.registered_source,
        active_only=not args.include_inactive,
    )
    print(f"registered pairs: {len(registered_pairs)}")

    all_results: List[Dict[str, object]] = []
    if args.mode in {"registered", "both"}:
        all_results.extend(
            score_registered_pairs(
                pairs=registered_pairs,
                days=args.days,
                bar=args.bar,
                price_mode=args.price_mode,
                min_obs=args.min_obs,
            )
        )

    if args.mode in {"combo", "both"}:
        symbols = resolve_combo_symbols(args, registered_pairs)
        if args.max_symbols and len(symbols) > args.max_symbols:
            print(f"combo symbols capped: {len(symbols)} -> {args.max_symbols}")
            symbols = symbols[: args.max_symbols]
        print(f"combo symbols: {len(symbols)}")
        matrix = load_price_matrix(symbols=symbols, days=args.days, bar=args.bar)
        combo_pairs = build_combo_pairs(symbols, max_pairs=args.max_pairs or None)
        all_results.extend(
            score_combo_pairs(
                pairs=combo_pairs,
                matrix=matrix,
                price_mode=args.price_mode,
                min_obs=args.min_obs,
            )
        )

    if not all_results:
        print("No pairs produced valid ADF results.")
        return 2

    df = pd.DataFrame(all_results)
    df = df[df["adf_pvalue"] <= float(args.pvalue_max)]
    if float(args.min_abs_corr) > 0:
        corr = pd.to_numeric(df["return_corr"], errors="coerce").abs()
        df = df[corr >= float(args.min_abs_corr)]
    if df.empty:
        print("No pairs matched the configured filters.")
        return 2

    df = df.sort_values(["adf_abs", "adf_pvalue"], ascending=[False, True]).reset_index(drop=True)
    output_path = write_output(df)
    print()
    print(format_table(df, limit=args.top))
    print()
    print(f"rows: {len(df)}")
    print(f"csv: {output_path}")
    print(f"latest: {OUTPUT_DIR / 'latest.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
