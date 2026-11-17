"""Fixed-beta pairs dashboard with a causal 20-trading-day spread baseline.

This is intentionally independent from ``quant_dashboard.py``.  It never
re-estimates beta on a rolling basis and it has no train/test split controls.
The default beta is the endpoint-detrended global beta described in
``Mathematics/fixed_beta_pair_spread_lesson.ipynb``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import pymysql
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from batteries_navigator_component import render_overview_navigator
from program_factory.order_scheduling import create_pair_scheduled_order


DB_CONFIG = {
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
PAIR_CONFIG_TABLE = "pair_config"
BACKTEST_ENABLED_STATE_KEY = "fixed_beta_backtest_enabled"
PENDING_BACKTEST_ENABLED_STATE_KEY = "fixed_beta_pending_backtest_enabled"
INTRADAY_INDEX = "idx_us_market_prices_ticker_time_price"
DAILY_TABLE = "us_market_days"
INTRADAY_TABLE = "us_market_prices"
BARS_PER_TRADING_DAY = 79  # Regular US equity session: 6.5 hours / 5 minutes.
ROLLING_DAYS = 20
KAPPA_NEUTRAL_BOUNDARY = 0.25
KAPPA_DERIVATIVE_BARS_PER_TRADING_DAY = 77
# A rolling κ estimate needs enough non-neutral observations to identify its
# OLS slope.  Without this guard, the first window can contain one return to
# the neutral band and mechanically report κ=-1.
KAPPA_MIN_EFFECTIVE_TRANSITIONS = 150
KAPPA_VISUAL_OUTLIER_ABS_LIMIT = 0.50
KAPPA_VISUAL_OUTLIER_WINDOW = 21
EXIT_RULES_CONFIG_PATH = Path(__file__).resolve().parent / "program_factory" / "pair_exit_rules.json"
DEFAULT_STOP_LOSS_RULE = "trailing(-0.5*sigma)"


def normalize_ticker(value: str) -> str:
    return str(value or "").strip().upper()


def quote_identifier(value: str) -> str:
    return "`" + str(value).replace("`", "``") + "`"


def fixed_beta_source_key(source: str) -> str:
    """Keep persisted source values stable even if the UI wording changes."""
    return "daily" if str(source).startswith("日线") else "5m"


def fixed_beta_pair_key(ticker_y: str, ticker_x: str) -> str:
    return f"{normalize_ticker(ticker_y)}_{normalize_ticker(ticker_x)}".lower()


def ensure_pair_config_table(conn) -> None:
    """Create the production fixed-beta, per-source configuration table."""
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {quote_identifier(PAIR_CONFIG_TABLE)} (
                pair_name VARCHAR(64) NOT NULL,
                data_source VARCHAR(16) NOT NULL,
                train_start_bj DATETIME NOT NULL,
                test_end_bj DATETIME NOT NULL,
                fixed_beta DOUBLE NOT NULL,
                backtest_enabled TINYINT(1) NOT NULL DEFAULT 0,
                entry_threshold DOUBLE NULL,
                stop_loss_rule VARCHAR(255) NULL,
                take_profit_rule VARCHAR(255) NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (pair_name, data_source)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
    conn.commit()


def load_pair_config(pair_name: str, data_source: str) -> dict | None:
    """Load one production fixed-beta dashboard configuration, if present."""
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        ensure_pair_config_table(conn)
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT train_start_bj, test_end_bj, fixed_beta, backtest_enabled,
                       entry_threshold, stop_loss_rule, take_profit_rule
                FROM {quote_identifier(PAIR_CONFIG_TABLE)}
                WHERE pair_name = %s AND data_source = %s
                """,
                (pair_name, data_source),
            )
            row = cursor.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    fields = (
        "train_start_bj", "test_end_bj", "fixed_beta", "backtest_enabled",
        "entry_threshold", "stop_loss_rule", "take_profit_rule",
    )
    return dict(zip(fields, row))


def save_pair_config(
    pair_name: str,
    data_source: str,
    train_start_bj,
    test_end_bj,
    fixed_beta: float,
    backtest_enabled: bool,
    entry_threshold: float | None = None,
    stop_loss_rule: str | None = None,
    take_profit_rule: str | None = None,
) -> None:
    """Upsert the production fixed-beta configuration used by this dashboard."""
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        ensure_pair_config_table(conn)
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {quote_identifier(PAIR_CONFIG_TABLE)}
                    (pair_name, data_source, train_start_bj, test_end_bj, fixed_beta,
                     backtest_enabled, entry_threshold, stop_loss_rule, take_profit_rule)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    train_start_bj = VALUES(train_start_bj),
                    test_end_bj = VALUES(test_end_bj),
                    fixed_beta = VALUES(fixed_beta),
                    backtest_enabled = VALUES(backtest_enabled),
                    entry_threshold = VALUES(entry_threshold),
                    stop_loss_rule = VALUES(stop_loss_rule),
                    take_profit_rule = VALUES(take_profit_rule)
                """,
                (
                    pair_name, data_source, pd.Timestamp(train_start_bj).to_pydatetime(),
                    pd.Timestamp(test_end_bj).to_pydatetime(), float(fixed_beta),
                    int(bool(backtest_enabled)), entry_threshold, stop_loss_rule, take_profit_rule,
                ),
            )
        conn.commit()
    finally:
        conn.close()


@st.cache_data(ttl=300)
def load_registered_pairs() -> dict[str, tuple[str, str]]:
    """Load every active metapair; fixed-beta config availability is optional."""
    try:
        conn = pymysql.connect(**DB_CONFIG)
        try:
            rows = pd.read_sql(
                """
                SELECT ticker_a, ticker_b
                FROM metapairs
                WHERE COALESCE(is_active, 1) = 1
                ORDER BY ticker_a, ticker_b
                """,
                conn,
            )
        finally:
            conn.close()
    except Exception:
        return {}

    pairs = {}
    for _, row in rows.iterrows():
        y, x = normalize_ticker(row["ticker_a"]), normalize_ticker(row["ticker_b"])
        if y and x and y != x:
            pairs[f"{y} / {x}"] = (y, x)
    return pairs


@st.cache_data(ttl=60, show_spinner=False)
def load_registered_pair_zscores(
    pair_items: tuple[tuple[str, str, str], ...], source: str,
) -> dict[str, float]:
    """Compute each saved pair's latest causal fixed-beta z-score for sorting."""
    window = ROLLING_DAYS if source.startswith("日线") else ROLLING_DAYS * BARS_PER_TRADING_DAY
    source_key = fixed_beta_source_key(source)
    zscores: dict[str, float] = {}
    for label, ticker_y, ticker_x in pair_items:
        try:
            config = load_pair_config(fixed_beta_pair_key(ticker_y, ticker_x), source_key)
            if config is None:
                zscores[label] = np.nan
                continue
            history, _ = load_pair_data(ticker_y, ticker_x, source)
            indicators = add_fixed_beta_indicators(history, float(config["fixed_beta"]), window)
            zscores[label] = float(indicators["zscore"].iloc[-1])
        except Exception:
            zscores[label] = np.nan
    return zscores


def sort_fixed_beta_pairs(
    pairs: dict[str, tuple[str, str]], zscores: dict[str, float], mode: str,
) -> dict[str, tuple[str, str]]:
    if mode == "A-Z":
        return dict(sorted(pairs.items(), key=lambda item: item[0]))

    def by_absolute_z(item: tuple[str, tuple[str, str]]) -> tuple[int, float, str]:
        value = zscores.get(item[0], np.nan)
        return (0, -abs(float(value)), item[0]) if np.isfinite(value) else (1, 0.0, item[0])

    return dict(sorted(pairs.items(), key=by_absolute_z))


def format_fixed_beta_pair_option(label: str, zscores: dict[str, float]) -> str:
    zscore = zscores.get(label, np.nan)
    return f"{label} | z: {float(zscore):+.2f}" if np.isfinite(zscore) else label


def first_present(columns: Iterable[str], candidates: Iterable[str]) -> str | None:
    lookup = {str(column).lower(): str(column) for column in columns}
    for candidate in candidates:
        if candidate.lower() in lookup:
            return lookup[candidate.lower()]
    return None


def us_daily_close_time_bj(session_dates: pd.Series) -> pd.Series:
    """Map NY session dates to Beijing close times without a tzdata dependency."""
    close_times = []
    for value in pd.to_datetime(session_dates):
        session_date = pd.Timestamp(value).normalize()
        march_first = pd.Timestamp(year=session_date.year, month=3, day=1)
        november_first = pd.Timestamp(year=session_date.year, month=11, day=1)
        dst_start = march_first + pd.Timedelta(days=(6 - march_first.dayofweek) % 7 + 7)
        dst_end = november_first + pd.Timedelta(days=(6 - november_first.dayofweek) % 7)
        close_hour_bj = 4 if dst_start <= session_date < dst_end else 5
        close_times.append(session_date + pd.Timedelta(days=1, hours=close_hour_bj))
    return pd.Series(close_times, index=session_dates.index)


@st.cache_data(ttl=300)
def daily_table_columns() -> tuple[str, ...]:
    """Discover the daily schema so legacy daily tables remain readable."""
    conn = pymysql.connect(**DB_CONFIG)
    try:
        result = pd.read_sql(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            conn,
            params=(DB_CONFIG["database"], DAILY_TABLE),
        )
    finally:
        conn.close()
    return tuple(result["COLUMN_NAME"].tolist())


@st.cache_data(ttl=120)
def load_pair_data(ticker_y: str, ticker_x: str, source: str) -> tuple[pd.DataFrame, str]:
    """Return exactly aligned legs from the selected canonical data source."""
    ticker_y, ticker_x = normalize_ticker(ticker_y), normalize_ticker(ticker_x)
    time_is_utc = source != "日线（us_market_days）"
    conn = pymysql.connect(**DB_CONFIG)
    try:
        if source == "日线（us_market_days）":
            columns = daily_table_columns()
            time_col = first_present(columns, ("time_utc", "time_bj", "date", "day", "time"))
            ticker_col = first_present(columns, ("ticker", "symbol"))
            price_col = first_present(columns, ("close", "price", "adj_close", "adjusted_close"))
            # Support both a standard long table (ticker/close) and the
            # project's legacy wide layout (time_bj, SBLK_price, BHP_price).
            y_price_col = first_present(columns, (f"{ticker_y}_price", ticker_y))
            x_price_col = first_present(columns, (f"{ticker_x}_price", ticker_x))
            if all((time_col, y_price_col, x_price_col)):
                query = f"""
                    SELECT {quote_identifier(time_col)} AS time_value,
                           {quote_identifier(y_price_col)} AS y_price,
                           {quote_identifier(x_price_col)} AS x_price
                    FROM {quote_identifier(DAILY_TABLE)}
                    WHERE {quote_identifier(y_price_col)} IS NOT NULL
                      AND {quote_identifier(x_price_col)} IS NOT NULL
                    ORDER BY {quote_identifier(time_col)}
                """
                data = pd.read_sql(query, conn)
                source_detail = f"us_market_days 宽表（{time_col}, {y_price_col}, {x_price_col}）"
                time_is_utc = "utc" in time_col.lower()
            elif all((time_col, ticker_col, price_col)):
                try:
                    query = f"""
                        SELECT a.{quote_identifier(time_col)} AS time_value,
                               a.{quote_identifier(price_col)} AS y_price,
                               b.{quote_identifier(price_col)} AS x_price
                        FROM {quote_identifier(DAILY_TABLE)} a
                        JOIN {quote_identifier(DAILY_TABLE)} b
                          ON b.{quote_identifier(ticker_col)} = %s
                         AND b.{quote_identifier(time_col)} = a.{quote_identifier(time_col)}
                        WHERE a.{quote_identifier(ticker_col)} = %s
                        ORDER BY a.{quote_identifier(time_col)}
                    """
                    data = pd.read_sql(query, conn, params=(ticker_x, ticker_y))
                    source_detail = "us_market_days"
                    time_is_utc = "utc" in time_col.lower()
                except Exception:
                    data = load_daily_from_intraday(conn, ticker_y, ticker_x)
                    source_detail = "us_market_prices 每日最后一个 5 分钟 bar（us_market_days 不可读）"
                    time_is_utc = True
            else:
                data = load_daily_from_intraday(conn, ticker_y, ticker_x)
                source_detail = "us_market_prices 每日最后一个 5 分钟 bar（us_market_days 不存在或不含该 pair 的字段）"
                time_is_utc = True
        else:
            query = f"""
                SELECT a.time_utc AS time_value, a.price AS y_price, b.price AS x_price
                FROM {INTRADAY_TABLE} a FORCE INDEX ({INTRADAY_INDEX})
                JOIN {INTRADAY_TABLE} b FORCE INDEX ({INTRADAY_INDEX})
                  ON b.ticker = %s AND b.time_utc = a.time_utc
                WHERE a.ticker = %s
                ORDER BY a.time_utc
            """
            data = pd.read_sql(query, conn, params=(ticker_x, ticker_y))
            source_detail = "us_market_prices 5-minute bars"
    finally:
        conn.close()

    data["time"] = pd.to_datetime(data.pop("time_value"), errors="coerce")
    data["y_price"] = pd.to_numeric(data["y_price"], errors="coerce")
    data["x_price"] = pd.to_numeric(data["x_price"], errors="coerce")
    data = data.dropna().sort_values("time").drop_duplicates("time").reset_index(drop=True)
    if data.empty:
        raise ValueError("所选 pair 在该数据源没有共同时间戳的价格。")
    if source == "日线（us_market_days）":
        # A daily observation represents the US regular-session close, rather
        # than midnight UTC.  16:00 New York converts to 04:00 Beijing during
        # DST and 05:00 Beijing during standard time, on the following date.
        data["time_bj"] = us_daily_close_time_bj(data["time"])
    else:
        data["time_bj"] = data["time"] + (pd.Timedelta(hours=8) if time_is_utc else pd.Timedelta(0))
    return data, source_detail


def load_daily_from_intraday(conn, ticker_y: str, ticker_x: str) -> pd.DataFrame:
    """Derive close-to-close daily bars when the optional daily table is absent.

    US regular trading hours all fall within one UTC date, so DATE(time_utc)
    identifies the session for the 5-minute equity data stored in this project.
    """
    query = f"""
        SELECT y_day.trading_date AS time_value,
               y.price AS y_price,
               x.price AS x_price
        FROM (
            SELECT DATE(time_utc) AS trading_date, MAX(time_utc) AS close_time
            FROM {INTRADAY_TABLE} FORCE INDEX ({INTRADAY_INDEX})
            WHERE ticker = %s
            GROUP BY DATE(time_utc)
        ) y_day
        JOIN {INTRADAY_TABLE} y FORCE INDEX ({INTRADAY_INDEX})
          ON y.ticker = %s AND y.time_utc = y_day.close_time
        JOIN (
            SELECT DATE(time_utc) AS trading_date, MAX(time_utc) AS close_time
            FROM {INTRADAY_TABLE} FORCE INDEX ({INTRADAY_INDEX})
            WHERE ticker = %s
            GROUP BY DATE(time_utc)
        ) x_day ON x_day.trading_date = y_day.trading_date
        JOIN {INTRADAY_TABLE} x FORCE INDEX ({INTRADAY_INDEX})
          ON x.ticker = %s AND x.time_utc = x_day.close_time
        ORDER BY y_day.trading_date
    """
    return pd.read_sql(query, conn, params=(ticker_y, ticker_y, ticker_x, ticker_x))


def endpoint_detrend(values: pd.Series | np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if len(values) < 2:
        return np.full(len(values), np.nan)
    return values - np.linspace(values[0], values[-1], len(values))


def fit_endpoint_detrended_beta(y: pd.Series, x: pd.Series) -> float:
    """beta = sum(D(x) D(y)) / sum(D(x)^2), from the mathematics lesson."""
    dy, dx = endpoint_detrend(y), endpoint_detrend(x)
    denominator = float(np.dot(dx, dx))
    if not np.isfinite(denominator) or np.isclose(denominator, 0.0):
        return np.nan
    return float(np.dot(dx, dy) / denominator)


def fit_min_variance_beta(y: pd.Series, x: pd.Series) -> float:
    """Minimize Var(Y - beta X) across the complete aligned history."""
    y_values, x_values = np.asarray(y, dtype=float), np.asarray(x, dtype=float)
    x_variance = float(np.var(x_values))
    if not np.isfinite(x_variance) or np.isclose(x_variance, 0.0):
        return np.nan
    return float(np.cov(x_values, y_values, ddof=0)[0, 1] / x_variance)


def add_fixed_beta_indicators(data: pd.DataFrame, beta: float, window: int, mean_derivative_window: int | None = None) -> pd.DataFrame:
    result = data.copy()
    # Keep the spread in price units.  In particular, do not rebase either leg
    # to the first visible observation: extending the history must not change
    # every historical spread value merely because its start date moved.
    result["spread"] = result["y_price"] - float(beta) * result["x_price"]
    result["spread_mean"] = result["spread"].rolling(window=window, min_periods=window).mean()
    result["spread_sigma"] = result["spread"].rolling(window=window, min_periods=window).std(ddof=0)
    result["upper_2sigma"] = result["spread_mean"] + 2 * result["spread_sigma"]
    result["lower_2sigma"] = result["spread_mean"] - 2 * result["spread_sigma"]
    safe_sigma = result["spread_sigma"].where(result["spread_sigma"] > 0)
    result["zscore"] = (result["spread"] - result["spread_mean"]) / safe_sigma
    # Equivalent to SMA(t) - SMA(t-1) for the chosen trailing window; causal.
    derivative_window = int(mean_derivative_window or window)
    result["mean_derivative"] = (result["spread"] - result["spread"].shift(derivative_window)) / derivative_window
    return result


def add_cumulative_kappa_hat(
    data: pd.DataFrame,
    rolling_window: int,
    neutral_boundary: float = KAPPA_NEUTRAL_BOUNDARY,
) -> pd.DataFrame:
    """Add the notebook's causal fixed-window episode-level OLS κ̂ estimate."""
    result = data.copy()
    result["kappa_distance"] = (result["zscore"].abs() - float(neutral_boundary)).clip(lower=0.0)
    previous_z = result["zscore"].shift(1)
    previous_sigma = result["spread_sigma"].shift(1)
    z_reference = np.sign(previous_z) * np.maximum(previous_z.abs(), float(neutral_boundary))
    z_reference = z_reference.where(previous_z.notna() & previous_z.ne(0.0))
    crossing_ratio = result["spread"].diff() / (z_reference * previous_sigma)
    result["kappa_crossing_event"] = crossing_ratio.le(-1.0).fillna(False)

    previous_distance = result["kappa_distance"].shift(1)
    delta_distance = result["kappa_distance"] - previous_distance
    transition_used = (
        ~result["kappa_crossing_event"]
        & previous_distance.notna()
        & result["kappa_distance"].notna()
        & previous_distance.gt(0.0)
    )
    result["kappa_transition_used"] = transition_used
    result["kappa_numerator_term"] = (previous_distance * delta_distance).where(transition_used, 0.0)
    result["kappa_denominator_term"] = (previous_distance ** 2).where(transition_used, 0.0)
    result["kappa_numerator"] = result["kappa_numerator_term"].rolling(rolling_window, min_periods=rolling_window).sum()
    result["kappa_denominator"] = result["kappa_denominator_term"].rolling(rolling_window, min_periods=rolling_window).sum()
    result["kappa_transition_count"] = (
        transition_used.astype(int).rolling(rolling_window, min_periods=rolling_window).sum()
    )
    result["kappa_hat"] = (
        result["kappa_numerator"] / result["kappa_denominator"]
    ).where(
        (result["kappa_denominator"] > 1e-12)
        & (result["kappa_transition_count"] >= KAPPA_MIN_EFFECTIVE_TRANSITIONS)
    )
    return result


def add_trading_day_numbers(data: pd.DataFrame, start_index: int) -> pd.DataFrame:
    """Add gap-free bar coordinates and session numbers from a chosen start."""
    result = data.copy()
    session_codes, _ = pd.factorize(result["time"].dt.normalize(), sort=False)
    start_index = int(np.clip(start_index, 0, len(result) - 1))
    result["plot_x"] = np.arange(len(result), dtype=int)
    result["trading_day_number"] = session_codes - int(session_codes[start_index])
    return result


def nearest_bar_index(times: pd.Series, selected_time) -> int:
    """Locate the closest aligned bar to a user-entered Beijing timestamp."""
    return int((times - pd.Timestamp(selected_time)).abs().to_numpy().argmin())


def parse_nearest_bar_index(times: pd.Series, value: str) -> int | None:
    """Parse YYYY-MM-DD HH:MM text and locate its nearest available bar."""
    timestamp = pd.to_datetime(str(value or "").strip(), errors="coerce")
    if pd.isna(timestamp):
        return None
    return nearest_bar_index(times, timestamp)


def load_pair_exit_rules(pair_key: str) -> tuple[str, str]:
    """Read the pair-game rules, retaining usable defaults when absent."""
    try:
        rules = json.loads(EXIT_RULES_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        rules = {}
    stop_rule = rules.get("stop_loss", {}).get(pair_key, DEFAULT_STOP_LOSS_RULE)
    take_rule = rules.get("take_profit", {}).get(pair_key, "")
    return str(stop_rule), "" if take_rule is None else str(take_rule)


def sigma_offset(rule: str, sigma_pct: float) -> float | None:
    """Interpret the trailing(offset) subset used by pair_exit_rules.json."""
    match = re.fullmatch(r"trailing\(\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+)?)\s*\*?\s*sigma\s*\)", str(rule).lower())
    if not match:
        return None
    coefficient = match.group(1)
    coefficient = 1.0 if coefficient in ("", "+") else -1.0 if coefficient == "-" else float(coefficient)
    return coefficient * float(sigma_pct)


def daily_interpolated_entry(previous_row: pd.Series, row: pd.Series, threshold: float, side: str) -> dict:
    """Place a daily threshold crossing inside the current US session."""
    previous_z, current_z = float(previous_row["zscore"]), float(row["zscore"])
    fraction = float(np.clip((threshold - previous_z) / (current_z - previous_z), 0.0, 1.0))
    close_time = pd.Timestamp(row["time_bj"])
    open_time = close_time - pd.Timedelta(hours=6.5)
    return {
        "side": side,
        "entry_plot_x": float(previous_row["plot_x"]) + fraction,
        "entry_day": int(row["trading_day_number"]),
        "entry_time": open_time + fraction * (close_time - open_time),
        "entry_z": threshold,
        "entry_spread": float(previous_row["spread"]) + fraction * (float(row["spread"]) - float(previous_row["spread"])),
        "entry_y": float(previous_row["y_price"]) + fraction * (float(row["y_price"]) - float(previous_row["y_price"])),
        "entry_x": float(previous_row["x_price"]) + fraction * (float(row["x_price"]) - float(previous_row["x_price"])),
    }


def daily_interpolated_exit(previous_row: pd.Series, row: pd.Series, trade: dict, fraction: float, reason: str) -> dict:
    """Place a daily exit at the exact intra-session crossing fraction."""
    fraction = float(np.clip(fraction, 0.0, 1.0))
    close_time = pd.Timestamp(row["time_bj"])
    open_time = close_time - pd.Timedelta(hours=6.5)
    return {
        "exit_plot_x": float(previous_row["plot_x"]) + fraction,
        "exit_time": open_time + fraction * (close_time - open_time),
        "exit_z": float(previous_row["zscore"]) + fraction * (float(row["zscore"]) - float(previous_row["zscore"])),
        "exit_spread": float(previous_row["spread"]) + fraction * (float(row["spread"]) - float(previous_row["spread"])),
        "exit_reason": reason,
    }


def run_zscore_backtest(data: pd.DataFrame, beta: float, entry_z: float, stop_rule: str, take_rule: str, continuous_daily: bool = False) -> list[dict]:
    """Backtest reentry bars, or continuous daily curves, with exit-rule exits."""
    trades: list[dict] = []
    open_trade: dict | None = None
    previous_z = np.nan
    previous_row: pd.Series | None = None
    for _, row in data.iterrows():
        zscore = row["zscore"]
        if not np.isfinite(zscore):
            continue
        if open_trade is None:
            is_inside = -entry_z < zscore < entry_z
            if continuous_daily and previous_row is not None and previous_z >= entry_z > zscore:
                open_trade = daily_interpolated_entry(previous_row, row, entry_z, "Short spread")
            elif continuous_daily and previous_row is not None and previous_z <= -entry_z < zscore:
                open_trade = daily_interpolated_entry(previous_row, row, -entry_z, "Long spread")
            elif previous_z >= entry_z and is_inside:
                side = "Short spread"
            elif previous_z <= -entry_z and is_inside:
                side = "Long spread"
            else:
                previous_z = zscore
                previous_row = row
                continue
            if open_trade is None:
                open_trade = {"side": side, "entry_plot_x": int(row["plot_x"]), "entry_day": int(row["trading_day_number"]), "entry_time": row["time_bj"], "entry_z": float(zscore), "entry_spread": float(row["spread"]), "entry_y": float(row["y_price"]), "entry_x": float(row["x_price"])}
            gross = open_trade["entry_y"] + abs(float(beta)) * open_trade["entry_x"]
            entry_sigma = float(row["spread_sigma"])
            if continuous_daily and previous_row is not None:
                entry_fraction = float(open_trade["entry_plot_x"] - previous_row["plot_x"])
                if 0.0 < entry_fraction < 1.0:
                    entry_sigma = float(previous_row["spread_sigma"]) + entry_fraction * (float(row["spread_sigma"]) - float(previous_row["spread_sigma"]))
            open_trade["entry_sigma"] = entry_sigma
            open_trade["sigma_pct"] = 0.0 if gross <= 0 or not np.isfinite(entry_sigma) else entry_sigma / gross * 100.0
            # A continuous daily entry may occur before the current session's
            # close.  Include the remaining entry-day path immediately;
            # otherwise its best return is lost by the continue below.
            entry_direction = 1.0 if open_trade["side"] == "Long spread" else -1.0
            entry_day_return = 0.0 if gross <= 0 else entry_direction * (float(row["spread"]) - open_trade["entry_spread"]) / gross * 100.0
            open_trade["peak_return_pct"] = max(entry_day_return, 0.0)
            open_trade["last_return_pct"] = entry_day_return
            open_trade["best_z"] = float(open_trade["entry_z"])
            previous_z = zscore
            previous_row = row
            continue

        previous_return_pct = float(open_trade.get("last_return_pct", 0.0))
        direction = 1.0 if open_trade["side"] == "Long spread" else -1.0
        gross = open_trade["entry_y"] + abs(float(beta)) * open_trade["entry_x"]
        pnl = direction * (float(row["spread"]) - open_trade["entry_spread"])
        return_pct = 100.0 * pnl / gross if gross > 0 else 0.0
        open_trade["peak_return_pct"] = max(float(open_trade["peak_return_pct"]), return_pct, 0.0)
        open_trade["last_return_pct"] = return_pct
        exit_reason = None
        can_exit = continuous_daily or int(row["trading_day_number"]) > int(open_trade["entry_day"])
        stop_offset = sigma_offset(stop_rule, open_trade["sigma_pct"])
        trailing_stop_trigger = False
        if can_exit:
            if stop_offset is not None:
                # Keep sigma fixed at the interpolated entry point and trail
                # actual pair return; rolling z-score changes do not move it.
                stop_level = float(open_trade["peak_return_pct"]) + float(stop_offset)
                trailing_stop_trigger = return_pct <= stop_level
            else:
                try:
                    stop_level = float(stop_rule)
                except ValueError:
                    stop_level = open_trade["peak_return_pct"] - 0.5 * open_trade["sigma_pct"]
                trailing_stop_trigger = return_pct <= stop_level
            if trailing_stop_trigger:
                exit_reason = "TRAILING_STOP" if stop_offset is not None else "STOP_LOSS"
            elif take_rule:
                take_offset = sigma_offset(take_rule, open_trade["sigma_pct"])
                z_match = re.fullmatch(r"z\(\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*\)", take_rule.lower())
                if take_offset is not None and open_trade["peak_return_pct"] > 0 and return_pct <= open_trade["peak_return_pct"] + take_offset:
                    exit_reason = "TAKE_PROFIT"
                elif z_match:
                    target = abs(float(z_match.group(1)))
                    if (direction > 0 and zscore >= target) or (direction < 0 and zscore <= -target):
                        exit_reason = "TAKE_PROFIT"
                else:
                    try:
                        if return_pct >= float(take_rule):
                            exit_reason = "TAKE_PROFIT"
                    except ValueError:
                        pass
        if exit_reason:
            exit_values = {"exit_plot_x": int(row["plot_x"]), "exit_time": row["time_bj"], "exit_z": float(zscore), "exit_spread": float(row["spread"]), "exit_reason": exit_reason}
            if continuous_daily and previous_row is not None and exit_reason in ("TRAILING_STOP", "STOP_LOSS") and previous_return_pct > stop_level > return_pct:
                crossing_fraction = (previous_return_pct - stop_level) / (previous_return_pct - return_pct)
                exit_values = daily_interpolated_exit(previous_row, row, open_trade, crossing_fraction, exit_reason)
            if exit_values["exit_plot_x"] != int(row["plot_x"]):
                pnl = direction * (exit_values["exit_spread"] - open_trade["entry_spread"])
                return_pct = 100.0 * pnl / gross if gross > 0 else 0.0
            open_trade.update(exit_values | {"pnl": pnl, "return_pct": return_pct})
            trades.append(open_trade)
            open_trade = None
        previous_z = zscore
        previous_row = row

    if open_trade is not None:
        row = data.iloc[-1]
        direction = 1.0 if open_trade["side"] == "Long spread" else -1.0
        gross = open_trade["entry_y"] + abs(float(beta)) * open_trade["entry_x"]
        pnl = direction * (float(row["spread"]) - open_trade["entry_spread"])
        open_trade.update({"exit_plot_x": int(row["plot_x"]), "exit_time": row["time_bj"], "exit_z": float(row["zscore"]) if np.isfinite(row["zscore"]) else np.nan, "exit_spread": float(row["spread"]), "pnl": pnl, "return_pct": 100.0 * pnl / gross if gross > 0 else 0.0, "exit_reason": "END_OF_BACKTEST"})
        trades.append(open_trade)
    return trades


def build_equity_curve(data: pd.DataFrame, trades: list[dict], beta: float, initial_equity: float = 10_000.0) -> pd.DataFrame:
    """Mark a one-unit fixed-beta pair position to market and compound each trade."""
    result = data[["plot_x", "time_bj", "y_price", "x_price"]].copy()
    result["equity"] = float(initial_equity)
    capital = float(initial_equity)
    for trade in trades:
        mask = (result["plot_x"] >= trade["entry_plot_x"]) & (result["plot_x"] <= trade["exit_plot_x"])
        segment = result.loc[mask]
        direction = 1.0 if trade["side"] == "Long spread" else -1.0
        gross = trade["entry_y"] + abs(float(beta)) * trade["entry_x"]
        pnl = direction * ((segment["y_price"] - trade["entry_y"]) - float(beta) * (segment["x_price"] - trade["entry_x"]))
        result.loc[mask, "equity"] = capital * (1.0 + pnl / gross) if gross > 0 else capital
        capital = float(result.loc[mask, "equity"].iloc[-1])
        result.loc[result["plot_x"] > trade["exit_plot_x"], "equity"] = capital
    return result


def maximum_drawdown_details(equity_data: pd.DataFrame) -> tuple[float, pd.Series | None, pd.Series | None]:
    """Return maximum peak-to-trough drawdown and the two points that define it."""
    if equity_data.empty:
        return 0.0, None, None
    equity = pd.to_numeric(equity_data["equity"], errors="coerce")
    running_peak = equity.cummax()
    drawdowns = equity.divide(running_peak).subtract(1.0)
    trough_position = int(drawdowns.to_numpy().argmin())
    peak_position = int(equity.iloc[:trough_position + 1].to_numpy().argmax())
    return float(drawdowns.iloc[trough_position] * 100.0), equity_data.iloc[peak_position], equity_data.iloc[trough_position]


def add_backtest_day_background(figure: go.Figure, data: pd.DataFrame) -> None:
    """Use the pair-game alternating yellow/green session bands on demand."""
    spans = data.groupby("trading_day_number", sort=False)["plot_x"].agg(["first", "last"])
    colors = ("rgba(255, 243, 205, 0.28)", "rgba(210, 244, 222, 0.24)")
    figure.update_layout(shapes=[{"type": "rect", "xref": "x", "yref": "paper", "x0": row["first"] - 0.5, "x1": row["last"] + 0.5, "y0": 0, "y1": 1, "fillcolor": colors[index % 2], "line": {"width": 0}, "layer": "below"} for index, (_, row) in enumerate(spans.iterrows())])


def add_backtest_trade_overlays(figure: go.Figure, data: pd.DataFrame, trades: list[dict], value_column: str) -> None:
    """Overlay each holding period plus entry/exit markers on a chart."""
    for trade in trades:
        segment = data[(data["plot_x"] > trade["entry_plot_x"]) & (data["plot_x"] < trade["exit_plot_x"])]
        color = "#16a34a" if trade["return_pct"] >= 0 else "#f59e0b"
        entry_value = trade["entry_spread"] if value_column == "spread" else trade["entry_z"]
        exit_value = trade["exit_spread"] if value_column == "spread" else trade["exit_z"]
        path_x = [trade["entry_plot_x"]] + segment["plot_x"].tolist() + [trade["exit_plot_x"]]
        path_y = [entry_value] + segment[value_column].tolist() + [exit_value]
        figure.add_trace(go.Scatter(x=path_x, y=path_y, mode="lines", name="Winning trade" if trade["return_pct"] >= 0 else "Losing trade", showlegend=False, line=dict(color=color, width=3), hoverinfo="skip"))
        figure.add_trace(go.Scatter(x=[trade["entry_plot_x"]], y=[entry_value], mode="markers", name="Entry", showlegend=False, marker=dict(color="#059669", size=9, symbol="triangle-up"), hovertemplate=f"{trade['side']} entry<br>北京时间 %{{customdata}}<br>%{{y:.5f}}<extra></extra>", customdata=[pd.Timestamp(trade["entry_time"]).strftime("%Y-%m-%d %H:%M:%S")]))
        figure.add_trace(go.Scatter(x=[trade["exit_plot_x"]], y=[exit_value], mode="markers", name="Exit", showlegend=False, marker=dict(color="#dc2626", size=9, symbol="x"), hovertemplate=f"{trade['exit_reason']}<br>北京时间 %{{customdata}}<br>%{{y:.5f}}<extra></extra>", customdata=[pd.Timestamp(trade["exit_time"]).strftime("%Y-%m-%d %H:%M:%S")]))


def trading_day_ticks(plot_data: pd.DataFrame, max_ticks: int = 15) -> pd.DataFrame:
    """Return a sparse, numeric-only tick per visible trading session."""
    ticks = (
        plot_data.groupby("trading_day_number", sort=False)["plot_x"]
        .first()
        .reset_index()
    )
    if len(ticks) > max_ticks:
        positions = np.linspace(0, len(ticks) - 1, num=max_ticks, dtype=int)
        ticks = ticks.iloc[np.unique(positions)]
    return ticks


def use_trading_day_axis(figure: go.Figure, plot_data: pd.DataFrame) -> None:
    """Draw compact bar spacing and adaptive, trading-day-only x ticks."""
    ticks = trading_day_ticks(plot_data)
    figure.update_xaxes(
        title="Trading days from selected start",
        range=[float(plot_data["plot_x"].iloc[0]) - 0.5, float(plot_data["plot_x"].iloc[-1]) + 0.5],
        tickmode="array",
        tickvals=ticks["plot_x"].tolist(),
        ticktext=ticks["trading_day_number"].astype(int).astype(str).tolist(),
    )


st.set_page_config(page_title="Fixed Beta Pairs Dashboard", layout="wide")
st_autorefresh(interval=300_000, key="fixed_beta_datarefresh")

if PENDING_BACKTEST_ENABLED_STATE_KEY in st.session_state:
    st.session_state[BACKTEST_ENABLED_STATE_KEY] = bool(st.session_state.pop(PENDING_BACKTEST_ENABLED_STATE_KEY))

# The overview navigator returns its selection only after the user clicks its
# Apply button.  Write that pending selection before the sidebar widgets are
# created, so their displayed values stay in sync on the rerun.
pending_slice = st.session_state.pop("fixed_beta_pending_slice", None)
if pending_slice:
    _, start_timestamp, end_timestamp, start_key, end_key = pending_slice
    st.session_state[start_key] = start_timestamp
    st.session_state[end_key] = end_timestamp

st.sidebar.header("Pair")
backtest_requested = st.sidebar.toggle("开启回测", value=False, key=BACKTEST_ENABLED_STATE_KEY)
data_source = st.sidebar.radio(
    "Data source",
    ["日线（us_market_days）", "5-minute（us_market_prices）"],
    help="两种模式都只保留两条腿共同存在的时间戳。",
)
pair_source = st.sidebar.radio("Pair source", ["Registered", "Custom"], horizontal=True)
pairs = load_registered_pairs()
if pair_source == "Registered" and pairs:
    pair_sort_mode = st.sidebar.radio("Sort pairs", ["By |z|", "A-Z"], horizontal=True)
    pair_items = tuple((label, values[0], values[1]) for label, values in pairs.items())
    pair_zscores = load_registered_pair_zscores(pair_items, data_source)
    pairs = sort_fixed_beta_pairs(pairs, pair_zscores, pair_sort_mode)
    pair_label = st.sidebar.selectbox(
        "Select pair",
        list(pairs),
        format_func=lambda label: format_fixed_beta_pair_option(label, pair_zscores),
    )
    ticker_y, ticker_x = pairs[pair_label]
else:
    if pair_source == "Registered":
        st.sidebar.info("没有可读取的 active metapairs；请使用 Custom。")
    ticker_y = normalize_ticker(st.sidebar.text_input("Y ticker", value=""))
    ticker_x = normalize_ticker(st.sidebar.text_input("X ticker", value=""))
    pair_label = f"{ticker_y} / {ticker_x}"

if not ticker_y or not ticker_x or ticker_y == ticker_x:
    st.error("请输入两只不同的 ticker。")
    st.stop()

window = ROLLING_DAYS if data_source.startswith("日线") else ROLLING_DAYS * BARS_PER_TRADING_DAY
window_label = f"{ROLLING_DAYS} 个交易日" if data_source.startswith("日线") else f"{ROLLING_DAYS} 个交易日（{window:,} 个 5 分钟 bars）"
episode_window = ROLLING_DAYS if data_source.startswith("日线") else ROLLING_DAYS * KAPPA_DERIVATIVE_BARS_PER_TRADING_DAY
episode_window_label = f"{ROLLING_DAYS} 个交易日" if data_source.startswith("日线") else f"{ROLLING_DAYS}×{KAPPA_DERIVATIVE_BARS_PER_TRADING_DAY} = {episode_window:,} 个 5 分钟 bars"
backtest_available = data_source.startswith("5-minute")
backtest_enabled = bool(backtest_requested and backtest_available)
if backtest_requested and not backtest_available:
    st.sidebar.info("日线仅用于观察；回测只支持 5-minute bars。")

try:
    raw_data, actual_source = load_pair_data(ticker_y, ticker_x, data_source)
except Exception as exc:
    st.error(f"无法加载 {pair_label}：{exc}")
    st.stop()

if len(raw_data) < window + 1:
    st.error(f"共同数据只有 {len(raw_data)} 行；{window_label}至少需要 {window + 1} 行才能显示均线变化率。")
    st.stop()

display_start_key = f"fixed_beta_start_text_bj::{ticker_y}::{ticker_x}"
if display_start_key not in st.session_state:
    first_day = raw_data["time_bj"].iloc[0].strftime("%Y-%m-%d")
    st.session_state[display_start_key] = f"{first_day} 22:30"
display_end_key = f"fixed_beta_end_text_bj::{ticker_y}::{ticker_x}"
if display_end_key not in st.session_state:
    st.session_state[display_end_key] = raw_data["time_bj"].iloc[-1].strftime("%Y-%m-%d %H:%M")
beta_state_key = f"fixed_beta::{ticker_y}::{ticker_x}::{data_source}"
# Preserve the dashboard's existing all-history initialization.  Only the
# explicit Fit action below switches to the selected-range minimum-variance fit.
full_history_beta = fit_endpoint_detrended_beta(raw_data["y_price"], raw_data["x_price"])
if beta_state_key not in st.session_state:
    st.session_state[beta_state_key] = float(full_history_beta) if np.isfinite(full_history_beta) else 1.0

backtest_key = f"fixed_beta_backtest::{ticker_y}::{ticker_x}::{data_source}"
pair_rule_key = fixed_beta_pair_key(ticker_y, ticker_x)
default_stop_rule, default_take_rule = load_pair_exit_rules(pair_rule_key)
saved_config_pair = fixed_beta_pair_key(ticker_y, ticker_x)
saved_config_source = fixed_beta_source_key(data_source)
saved_config_notice_key = f"fixed_beta_config_notice::{saved_config_pair}::{saved_config_source}"

st.sidebar.subheader("配置")
load_config_col, save_config_col = st.sidebar.columns(2)
if load_config_col.button("读取配置", key=f"fixed_beta_load_config::{saved_config_pair}::{saved_config_source}"):
    try:
        saved_config = load_pair_config(saved_config_pair, saved_config_source)
        if saved_config is None:
            st.sidebar.info("当前 pair 与数据频率没有已储存的配置。")
        else:
            st.session_state[display_start_key] = pd.Timestamp(saved_config["train_start_bj"]).strftime("%Y-%m-%d %H:%M")
            st.session_state[display_end_key] = pd.Timestamp(saved_config["test_end_bj"]).strftime("%Y-%m-%d %H:%M")
            st.session_state[beta_state_key] = float(saved_config["fixed_beta"])
            is_backtest = bool(saved_config["backtest_enabled"])
            st.session_state[PENDING_BACKTEST_ENABLED_STATE_KEY] = is_backtest
            if is_backtest:
                entry_key = f"{backtest_key}::entry"
                stop_key = f"{backtest_key}::stop_rule"
                take_key = f"{backtest_key}::take_rule"
                st.session_state[entry_key] = float(saved_config["entry_threshold"] if saved_config["entry_threshold"] is not None else 2.0)
                st.session_state[stop_key] = str(saved_config["stop_loss_rule"] or default_stop_rule)
                st.session_state[take_key] = str(saved_config["take_profit_rule"] or "")
            st.session_state[saved_config_notice_key] = "已读取储存配置。"
            st.rerun()
    except Exception as exc:
        st.sidebar.error(f"读取配置失败：{exc}")

if save_config_col.button("储存配置", key=f"fixed_beta_save_config::{saved_config_pair}::{saved_config_source}"):
    try:
        selected_start = parse_nearest_bar_index(raw_data["time_bj"], st.session_state.get(display_start_key, ""))
        if selected_start is None:
            raise ValueError("图表起点格式无效，无法储存。")
        selected_end = parse_nearest_bar_index(raw_data["time_bj"], st.session_state.get(display_end_key, ""))
        if selected_end is None or selected_end <= selected_start:
            raise ValueError("图表终点必须晚于起点，才能储存。")
        is_backtest = backtest_enabled
        entry_key = f"{backtest_key}::entry"
        stop_key = f"{backtest_key}::stop_rule"
        take_key = f"{backtest_key}::take_rule"
        save_pair_config(
            saved_config_pair,
            saved_config_source,
            raw_data["time_bj"].iloc[selected_start],
            raw_data["time_bj"].iloc[selected_end],
            float(st.session_state[beta_state_key]),
            is_backtest,
            float(st.session_state.get(entry_key, 2.0)) if is_backtest else None,
            str(st.session_state.get(stop_key, default_stop_rule)) if is_backtest else None,
            str(st.session_state.get(take_key, default_take_rule)) if is_backtest else None,
        )
        st.sidebar.success("配置已储存。")
    except Exception as exc:
        st.sidebar.error(f"储存配置失败：{exc}")

if saved_config_notice := st.session_state.pop(saved_config_notice_key, None):
    st.sidebar.success(saved_config_notice)

selected_start_text = st.sidebar.text_input(
    "图表起点（北京时间）",
    key=display_start_key,
    help="格式：YYYY-MM-DD HH:MM。系统会自动选取距离最近的共同 bar。",
)
selected_end_text = st.sidebar.text_input(
    "图表终点（北京时间）",
    key=display_end_key,
    help="格式：YYYY-MM-DD HH:MM。系统会自动选取距离最近的共同 bar。",
)
display_start = parse_nearest_bar_index(raw_data["time_bj"], selected_start_text)
if display_start is None:
    st.sidebar.warning("起点格式无效，请使用 YYYY-MM-DD HH:MM；已使用完整历史。")
    display_start = 0
display_end = parse_nearest_bar_index(raw_data["time_bj"], selected_end_text)
if display_end is None:
    st.sidebar.warning("终点格式无效，请使用 YYYY-MM-DD HH:MM；已使用完整历史终点。")
    display_end = len(raw_data) - 1
if display_end <= display_start:
    st.sidebar.warning("终点必须晚于起点；已使用完整历史终点。")
    display_end = len(raw_data) - 1
selected_history = raw_data.iloc[display_start : display_end + 1].copy()
selected_history_beta = fit_min_variance_beta(selected_history["y_price"], selected_history["x_price"])

st.sidebar.header("Fixed beta")
st.sidebar.caption("首次载入或切换 pair 时，以全历史 beta 初始化；调整图表时间片不会自动重算。")
if st.sidebar.button("Fit beta（当前区间最小方差）"):
    if np.isfinite(selected_history_beta):
        st.session_state[beta_state_key] = float(selected_history_beta)
    else:
        st.sidebar.warning("当前区间无法拟合 beta；请扩大区间。")
beta = st.sidebar.number_input("Fixed beta", step=0.01, format="%.6f", key=beta_state_key)

data = add_fixed_beta_indicators(raw_data, beta, window, mean_derivative_window=episode_window)
data = add_trading_day_numbers(data, display_start)
kappa_data = add_cumulative_kappa_hat(data, rolling_window=episode_window)
plot_data = data.iloc[display_start : display_end + 1].copy()
hover_times_bj = plot_data["time_bj"].dt.strftime("%Y-%m-%d %H:%M:%S")
entry_z, initial_equity = 2.0, 10_000.0
if backtest_enabled:
    st.sidebar.subheader("回测规则")
    entry_z = st.sidebar.number_input("入场阈值 |z|", min_value=0.1, value=2.0, step=0.1, key=f"{backtest_key}::entry")
    stop_rule = st.sidebar.text_input("Stop loss rule", value=default_stop_rule, key=f"{backtest_key}::stop_rule", help="支持 trailing(-1*sigma) 或百分比数值。")
    take_rule = st.sidebar.text_input("Take profit rule", value=default_take_rule, key=f"{backtest_key}::take_rule", help="支持 z(0)、trailing(-0.5*sigma) 或百分比数值；留空表示不设止盈。")
    st.sidebar.caption(f"5 分钟：z 先在 ±{entry_z:.2f} 外，再回穿至带内；从上方回穿做空 spread，从下方回穿做多 spread。")
    st.sidebar.caption(f"出场：每根 bar 先执行 Stop loss `{stop_rule}`，再执行 Take profit `{take_rule or '未配置'}`。")
    trades = run_zscore_backtest(plot_data, beta, float(entry_z), stop_rule, take_rule, continuous_daily=False)
    equity_data = build_equity_curve(plot_data, trades, beta, initial_equity)
else:
    trades, equity_data = [], None
last = plot_data.iloc[-1]
latest_bar = data.iloc[-1]
ready = plot_data.dropna(subset=["spread_mean", "spread_sigma"])
latest_gross_notional = float(latest_bar.y_price) + abs(float(beta)) * float(latest_bar.x_price)
latest_two_sigma_return_pct = (
    2.0 * float(latest_bar.spread_sigma) / latest_gross_notional * 100.0
    if pd.notna(latest_bar.spread_sigma)
    and np.isfinite(latest_gross_notional)
    and not np.isclose(latest_gross_notional, 0.0)
    else np.nan
)

st.title(f"{pair_label} · Fixed Beta Spread")
st.caption(f"数据：{actual_source} · beta 固定为 {beta:.6f} · trailing window：{window_label} · 不使用 train/test split。")

metric_1, metric_2, metric_3, metric_4, metric_5, metric_6 = st.columns(6)
metric_1.metric("Latest bar（北京时间）", latest_bar.time_bj.strftime("%Y-%m-%d %H:%M"))
metric_2.metric("2 Sigma Return", "n/a" if pd.isna(latest_two_sigma_return_pct) else f"{latest_two_sigma_return_pct:.2f}%")
metric_3.metric(ticker_y, f"{last.y_price:.2f}")
metric_4.metric(ticker_x, f"{last.x_price:.2f}")
metric_5.metric("Spread", f"{last.spread:.5f}")
metric_6.metric("Rolling z-score", "n/a" if pd.isna(last.zscore) else f"{last.zscore:.3f}")

# Keep the navigator's overview stable while the displayed fixed beta is
# adjusted.  This mirrors Batteries Lab: the global spread uses the initial
# all-history beta, while the detail charts use the selected Fixed beta above.
initial_beta = float(full_history_beta) if np.isfinite(full_history_beta) else float(beta)
global_plot_data = raw_data[["time_bj", "y_price", "x_price"]].copy()
global_plot_data["spread"] = global_plot_data["y_price"] - initial_beta * global_plot_data["x_price"]
st.caption(
    f"全局导航（冻结初始 beta = {initial_beta:.6f}）：拖动深蓝框的边缘或重新拖出范围，再点击“应用区间”。"
)
global_range = render_overview_navigator(
    global_plot_data["spread"].to_numpy(),
    global_plot_data["time_bj"].dt.strftime("%Y-%m-%d %H:%M").tolist(),
    display_start,
    display_end,
    key=f"fixed_beta_global_navigator::{ticker_y}::{ticker_x}::{data_source}",
)
if isinstance(global_range, dict):
    try:
        global_start = int(global_range["start_index"])
        global_end = int(global_range["end_index"])
    except (KeyError, TypeError, ValueError):
        global_start, global_end = -1, -1
    if (
        0 <= global_start < global_end < len(raw_data)
        and (global_start, global_end) != (display_start, display_end)
    ):
        st.session_state["fixed_beta_pending_slice"] = (
            "range",
            raw_data["time_bj"].iloc[global_start].strftime("%Y-%m-%d %H:%M:%S"),
            raw_data["time_bj"].iloc[global_end].strftime("%Y-%m-%d %H:%M:%S"),
            display_start_key,
            display_end_key,
        )
        st.rerun()

fig = go.Figure()
fig.add_trace(go.Scatter(x=plot_data["plot_x"], y=plot_data["spread"], customdata=hover_times_bj, name="Fixed-beta spread", line=dict(color="#1f77b4"), hovertemplate="北京时间 %{customdata}<br>Spread %{y:.5f}<extra></extra>"))
fig.add_trace(go.Scatter(x=plot_data["plot_x"], y=plot_data["spread_mean"], name=f"Trailing mean ({window_label})", line=dict(color="#9ca3af", width=2, dash="dash"), hoverinfo="skip"))
fig.add_trace(go.Scatter(x=plot_data["plot_x"], y=plot_data["upper_2sigma"], name="Mean + 2σ", line=dict(color="#ef4444", dash="dot"), hoverinfo="skip"))
fig.add_trace(go.Scatter(x=plot_data["plot_x"], y=plot_data["lower_2sigma"], name="Mean − 2σ", line=dict(color="#ef4444", dash="dot"), hoverinfo="skip"))
fig.update_layout(template="plotly_dark", height=560, yaxis_title="Fixed-beta price spread", legend_orientation="h")
use_trading_day_axis(fig, plot_data)
if backtest_enabled:
    add_backtest_day_background(fig, plot_data)
    add_backtest_trade_overlays(fig, plot_data, trades, "spread")
st.plotly_chart(fig, use_container_width=True)

zfig = go.Figure()
zfig.add_trace(go.Scatter(x=plot_data["plot_x"], y=plot_data["zscore"], customdata=hover_times_bj, name="Causal rolling z-score", line=dict(color="#a78bfa"), hovertemplate="北京时间 %{customdata}<br>z-score %{y:.3f}<extra></extra>"))
zfig.add_hline(y=0, line_dash="dash", line_color="gray")
zfig.add_hline(y=entry_z, line_dash="dot", line_color="#ef4444")
zfig.add_hline(y=-entry_z, line_dash="dot", line_color="#ef4444")
zfig.update_layout(height=320, yaxis_title="z-score", showlegend=False)
use_trading_day_axis(zfig, plot_data)
if backtest_enabled:
    add_backtest_day_background(zfig, plot_data)
    add_backtest_trade_overlays(zfig, plot_data, trades, "zscore")
st.plotly_chart(zfig, use_container_width=True)

if backtest_enabled and equity_data is not None:
    equity_times_bj = equity_data["time_bj"].dt.strftime("%Y-%m-%d %H:%M:%S")
    equity_fig = go.Figure()
    equity_fig.add_trace(go.Scatter(x=equity_data["plot_x"], y=equity_data["equity"], customdata=equity_times_bj, name="Equity", line=dict(color="#2563eb", width=2.2), fill="tozeroy", fillcolor="rgba(37, 99, 235, 0.10)", hovertemplate="北京时间 %{customdata}<br>Equity $%{y:,.2f}<extra></extra>"))
    equity_fig.add_hline(y=initial_equity, line_dash="dash", line_color="#6b7280")
    equity_fig.update_layout(template="plotly_dark", height=360, yaxis_title="Equity", showlegend=False)
    use_trading_day_axis(equity_fig, plot_data)
    add_backtest_day_background(equity_fig, plot_data)
    equity_fig.update_yaxes(type="log")
    st.plotly_chart(equity_fig, use_container_width=True)
    max_drawdown_pct, peak_row, trough_row = maximum_drawdown_details(equity_data)
    if peak_row is not None and trough_row is not None:
        drawdown_help = (
            "最大回撤为 equity 从历史峰值跌至随后最低点的幅度。\n\n"
            f"峰值：{pd.Timestamp(peak_row['time_bj']).strftime('%Y-%m-%d %H:%M:%S')}（${peak_row['equity']:,.2f}）\n\n"
            f"低点：{pd.Timestamp(trough_row['time_bj']).strftime('%Y-%m-%d %H:%M:%S')}（${trough_row['equity']:,.2f}）"
        )
    else:
        drawdown_help = "当前没有可计算最大回撤的权益数据。"
    backtest_metrics = st.columns(4)
    backtest_metrics[0].metric("Trades", len(trades))
    final_return = (equity_data["equity"].iloc[-1] / initial_equity - 1.0) * 100.0
    backtest_metrics[1].metric("Final Equity", f"${equity_data['equity'].iloc[-1]:,.2f}", delta=f"{final_return:+.2f}%")
    winning_trades = sum(float(trade["return_pct"]) > 0.0 for trade in trades)
    total_trades = len(trades)
    win_rate = 100.0 * winning_trades / total_trades if total_trades else None
    win_rate_text = "n/a" if win_rate is None else f"{win_rate:.2f}".rstrip("0").rstrip(".") + "%"
    backtest_metrics[2].markdown(
        f"<div style='font-size:0.875rem; color:var(--secondary-text-color); margin-bottom:0.25rem'>Win Rate</div>"
        f"<div style='font-size:2.25rem; font-weight:400; line-height:1.2'>{winning_trades}/{total_trades}&nbsp;&nbsp;{win_rate_text}</div>",
        unsafe_allow_html=True,
    )
    backtest_metrics[3].metric("Max Drawdown", f"{max_drawdown_pct:.2f}%", help=drawdown_help)

kappa_fig = go.Figure()
kappa_plot_data = kappa_data[
    (kappa_data["plot_x"] >= plot_data["plot_x"].iloc[0])
    & (kappa_data["plot_x"] <= plot_data["plot_x"].iloc[-1])
].copy()
kappa_times_bj = kappa_plot_data["time_bj"].dt.strftime("%Y-%m-%d %H:%M:%S")
if data_source.startswith("5-minute"):
    # The estimator is still per 5-minute bar.  Present it on a one-trading-day
    # equivalent scale so it is readable and directly comparable with the
    # daily screening notebook: r(t+77) = (1 + κ_5m)^77 r(t).
    kappa_plot_data["kappa_display"] = np.expm1(
        KAPPA_DERIVATIVE_BARS_PER_TRADING_DAY * np.log1p(kappa_plot_data["kappa_hat"])
    )
    kappa_display_name = "κ̂（日等效）"
    kappa_hover_label = "κ̂（日等效）"
    kappa_caption_scale = f"；图中按 {KAPPA_DERIVATIVE_BARS_PER_TRADING_DAY} 个 5 分钟 bars 换算为每交易日等效值"
else:
    kappa_plot_data["kappa_display"] = kappa_plot_data["kappa_hat"]
    kappa_display_name = "κ̂"
    kappa_hover_label = "κ̂"
    kappa_caption_scale = ""

kappa_plot_data["kappa_visual_outlier"] = False
if data_source.startswith("5-minute"):
    # Display-only robust spike filter. A sustained high-κ regime remains: its
    # local median follows it. Only an isolated, extreme jump is hidden.
    local_median = kappa_plot_data["kappa_display"].rolling(
        KAPPA_VISUAL_OUTLIER_WINDOW, center=True, min_periods=5
    ).median()
    local_mad = (kappa_plot_data["kappa_display"] - local_median).abs().rolling(
        KAPPA_VISUAL_OUTLIER_WINDOW, center=True, min_periods=5
    ).median()
    local_tolerance = np.maximum(0.15, 6.0 * local_mad)
    kappa_plot_data["kappa_visual_outlier"] = (
        kappa_plot_data["kappa_display"].abs().gt(KAPPA_VISUAL_OUTLIER_ABS_LIMIT)
        & (kappa_plot_data["kappa_display"] - local_median).abs().gt(local_tolerance)
    )
    kappa_plot_data.loc[kappa_plot_data["kappa_visual_outlier"], "kappa_display"] = np.nan

kappa_visual_outlier_count = int(kappa_plot_data["kappa_visual_outlier"].sum())
kappa_line_shape = "linear"

# Plotly can close a filled path across NaN-separated positive runs, which
# makes a positive (red) region spill over a negative interval.  Give every
# same-sign run its own polygon, explicitly returning to the zero line.
for sign, fill_color in (
    (-1, "rgba(76, 158, 217, 0.35)"),
    (1, "rgba(217, 95, 95, 0.35)"),
):
    signed = kappa_plot_data["kappa_display"] * sign
    run_ids = (signed.gt(0.0) != signed.gt(0.0).shift(fill_value=False)).cumsum()
    for _, run in kappa_plot_data.loc[signed.gt(0.0)].groupby(run_ids[signed.gt(0.0)]):
        x_values = run["plot_x"].to_list()
        y_values = run["kappa_display"].to_list()
        if len(x_values) == 1:
            # A one-bar run has no area.  It is still represented by the line.
            continue
        kappa_fig.add_trace(go.Scatter(
            x=[x_values[0], *x_values, x_values[-1]],
            y=[0.0, *y_values, 0.0],
            mode="lines",
            line=dict(width=0, shape=kappa_line_shape),
            fill="toself",
            fillcolor=fill_color,
            hoverinfo="skip",
            showlegend=False,
        ))
kappa_fig.add_trace(go.Scatter(
    x=kappa_plot_data["plot_x"], y=kappa_plot_data["kappa_display"], customdata=kappa_times_bj,
    name=kappa_display_name, line=dict(color="#6a3d9a", width=1.8, shape=kappa_line_shape),
    hovertemplate=(
        "北京时间 %{customdata}<br>"
        f"{kappa_hover_label} %{{y:.5f}}<extra></extra>"
    ),
))
kappa_fig.add_hline(y=0.0, line_color="gray", line_width=1)
kappa_fig.update_layout(template="plotly_dark", height=300, yaxis_title=kappa_display_name, showlegend=False)

# Plotly's automatic locator can leave this compact chart with only one or two
# labels.  Select a readable 1/2/2.5/5×10ⁿ step from the values presently in
# view, aiming for roughly eight labelled levels.
kappa_visible = kappa_plot_data["kappa_display"].dropna()
if not kappa_visible.empty:
    kappa_span = float(kappa_visible.max() - kappa_visible.min())
    raw_kappa_step = max(kappa_span / 8.0, 1e-6)
    kappa_magnitude = 10.0 ** np.floor(np.log10(raw_kappa_step))
    kappa_tick_step = next(
        multiplier * kappa_magnitude
        for multiplier in (1.0, 2.0, 2.5, 5.0, 10.0)
        if multiplier * kappa_magnitude >= raw_kappa_step
    )
    kappa_fig.update_yaxes(
        tickmode="linear",
        dtick=kappa_tick_step,
        tickformat=".3f",
        showgrid=True,
        gridcolor="rgba(128, 128, 128, 0.22)",
    )
use_trading_day_axis(kappa_fig, plot_data)
st.plotly_chart(kappa_fig, use_container_width=True)
st.caption(
    f"Rolling κ̂：中性区 ±{KAPPA_NEUTRAL_BOUNDARY:.2f}，固定 rolling OLS 窗口为 {episode_window_label}"
    f"；至少需要 {KAPPA_MIN_EFFECTIVE_TRANSITIONS} 次有效 transition 才显示"
    f"{kappa_caption_scale}。负值表示窗口内距离收缩证据，正值表示窗口内距离扩张证据。"
    + (f"图表已隐藏 {kappa_visual_outlier_count} 个孤立极端点（不影响 κ̂ 计算）。" if kappa_visual_outlier_count else "")
)

derivative_fig = go.Figure()
derivative_fig.add_trace(go.Scatter(
    x=plot_data["plot_x"],
    y=plot_data["mean_derivative"],
    customdata=hover_times_bj,
    name="Rolling-mean derivative / bar",
    line=dict(color="#0f766e", width=1.7),
    hovertemplate="北京时间 %{customdata}<br>Rolling-mean derivative / bar %{y:.7f}<extra></extra>",
))
derivative_fig.add_hline(y=0.0, line_color="gray", line_width=1)
derivative_fig.update_layout(template="plotly_dark", height=280, yaxis_title="Spread change / bar", showlegend=False)
use_trading_day_axis(derivative_fig, plot_data)
st.plotly_chart(derivative_fig, use_container_width=True)
st.caption(f"Rolling-mean derivative / bar：当前 spread 相对 {episode_window_label} 前 spread 的变化，除以窗口长度。")

latest_derivative = last.mean_derivative
col_a, col_b, col_c = st.columns(3)
col_a.metric("Rolling mean", "n/a" if pd.isna(last.spread_mean) else f"{last.spread_mean:.5f}")
col_b.metric("Rolling σ", "n/a" if pd.isna(last.spread_sigma) else f"{last.spread_sigma:.5f}")
col_c.metric("Rolling-mean derivative / bar", "n/a" if pd.isna(latest_derivative) else f"{latest_derivative:.7f}")

st.caption("均线与标准差均为 trailing window；均线导数 = (当前 spread − window 前 spread) / window，未使用未来数据。")

st.header("📝 Schedule Order")
if not data_source.startswith("5-minute"):
    st.info("实盘预约下单只使用 5-minute fixed-beta rolling 参数；日线模式仅供观察。")
else:
    schedule_pair_name = fixed_beta_pair_key(ticker_y, ticker_x)
    schedule_start = raw_data["time_bj"].iloc[display_start]
    schedule_end = raw_data["time_bj"].iloc[display_end]
    two_sigma_pct = latest_two_sigma_return_pct
    st.caption(
        f"订单参数快照：`{schedule_pair_name}` · 区间 {schedule_start:%Y-%m-%d %H:%M} 至 "
        f"{schedule_end:%Y-%m-%d %H:%M} · beta {float(beta):.6f} · "
        f"2 Sigma {'n/a' if pd.isna(two_sigma_pct) else f'{two_sigma_pct:.3f}%'}"
    )
    direction_col, exposure_col = st.columns(2)
    with direction_col:
        direction = st.selectbox(
            "Direction", ["Auto", "Long", "Short"], key=f"fixed_beta_direction::{schedule_pair_name}"
        )
        position_type = {
            "Auto": "AUTO_MODE",
            "Long": "LONG_SPREAD",
            "Short": "SHORT_SPREAD",
        }[direction]
    with exposure_col:
        exposure_pct = st.number_input(
            "Exposure (% of Net Liq)", min_value=0.0, max_value=160.0, value=10.0, step=0.5,
            key=f"fixed_beta_exposure::{schedule_pair_name}",
        )
        reentry_auto_pending = st.checkbox(
            "Auto Pending when Waiting Entry fires",
            value=False,
            key=f"fixed_beta_auto_pending::{schedule_pair_name}",
            help="启用后，Start Reentry 会保存方向与仓位；waiting_entry 在信号触发时自动创建 PENDING 订单。",
        )

    schedule_col, reentry_col = st.columns(2)
    with schedule_col:
        if st.button("📥 Schedule Order", key=f"fixed_beta_schedule::{schedule_pair_name}"):
            if not np.isfinite(two_sigma_pct):
                st.error("当前 Rolling Sigma 或价格无效，无法创建订单。")
            else:
                try:
                    order_result = create_pair_scheduled_order(
                        pair_name=schedule_pair_name,
                        train_start=schedule_start,
                        split_time=schedule_end,
                        two_sigma=float(two_sigma_pct),
                        position_type=position_type,
                        beta=float(beta),
                        exposure_pct=float(exposure_pct),
                    )
                    st.success(f"✅ Order Scheduled! id={order_result['order_id']}")
                except Exception as exc:
                    st.error(f"❌ Failed: {exc}")

    with reentry_col:
        reentry_threshold = st.number_input(
            "Reentry Threshold", min_value=0.01, max_value=99.99, value=2.00, step=0.05,
            format="%.2f", key=f"fixed_beta_reentry_threshold::{schedule_pair_name}",
        )
        if st.button("🔁 Start Reentry", key=f"fixed_beta_reentry::{schedule_pair_name}"):
            conn = None
            try:
                conn = pymysql.connect(**MEMORY_DB_CONFIG)
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO pair_reentry
                            (pair_name, train_start, split_time, reentry_lock, threshold,
                             param, auto_schedule_enabled, exposure_pct, order_position_type)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            train_start = VALUES(train_start),
                            split_time = VALUES(split_time),
                            reentry_lock = VALUES(reentry_lock),
                            threshold = VALUES(threshold),
                            param = VALUES(param),
                            auto_schedule_enabled = VALUES(auto_schedule_enabled),
                            exposure_pct = VALUES(exposure_pct),
                            order_position_type = VALUES(order_position_type)
                        """,
                        (
                            schedule_pair_name, schedule_start.to_pydatetime(), schedule_end.to_pydatetime(),
                            2, float(reentry_threshold), float(beta), int(reentry_auto_pending),
                            float(exposure_pct) if reentry_auto_pending else None,
                            position_type if reentry_auto_pending else None,
                        ),
                    )
                conn.commit()
                auto_text = f" | auto pending {position_type} {float(exposure_pct):.2f}%" if reentry_auto_pending else ""
                st.success(
                    f"🔁 Reentry Started! beta={float(beta):.6f} "
                    f"| threshold={float(reentry_threshold):.2f}{auto_text}"
                )
            except Exception as exc:
                if conn is not None:
                    conn.rollback()
                st.error(f"❌ Failed: {exc}")
            finally:
                if conn is not None:
                    conn.close()

if backtest_enabled:
    st.subheader("交易记录")
    if trades:
        exit_reason_labels = {
            "TRAILING_STOP": "Trailing Stop",
            "STOP_LOSS": "Stop Loss",
            "TAKE_PROFIT": "Take Profit",
            "END_OF_BACKTEST": "End of Backtest",
        }
        trade_rows = []
        for trade in trades:
            return_pct = float(trade["return_pct"])
            trade_rows.append(
                {
                    "方向": trade["side"],
                    "入场时间（北京时间）": trade["entry_time"].strftime("%Y-%m-%d %H:%M"),
                    "入场 z-score": f"{float(trade['entry_z']):.3f}",
                    "出场时间（北京时间）": trade["exit_time"].strftime("%Y-%m-%d %H:%M"),
                    "出场 z-score": "n/a" if pd.isna(trade["exit_z"]) else f"{float(trade['exit_z']):.3f}",
                    "出场原因": exit_reason_labels.get(trade["exit_reason"], trade["exit_reason"]),
                    "结果": f"{return_pct:+.2f}%",
                }
            )
        trade_table = pd.DataFrame(trade_rows)

        def result_style(value: object) -> str:
            """Use restrained, severity-based return colors in the trade ledger."""
            text = str(value)
            if not text.endswith("%"):
                return ""
            try:
                return_pct = float(text[:-1])
            except ValueError:
                return ""
            if return_pct >= 0:
                return "color: #008000; font-weight: 600"
            loss_pct = abs(return_pct)
            if loss_pct <= 0.8:
                return "color: #9a7b00; font-weight: 600"
            if loss_pct <= 2.0:
                return "color: #b45309; font-weight: 600"
            return "color: #b42318; font-weight: 600"

        st.dataframe(
            trade_table.style.map(
                result_style,
                subset=["结果"],
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("当前区间尚未出现符合回穿规则的交易。")
