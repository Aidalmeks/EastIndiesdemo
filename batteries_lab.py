"""Interactive fixed-beta laboratory for selecting trading batteries.

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

from batteries_navigator_component import render_overview_navigator


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
FIXED_BETA_CONFIG_TABLE = "fixed_beta_pair_config"
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
# These windows are deliberately explicit research hypotheses, not a
# fitted rule.  They correspond to the first clearly identifiable positive
# daily-equivalent κ̂ regimes in the saved AA/CENX 5-minute configuration.
AA_CENX_KAPPA_PAUSE_WINDOWS = (
    (pd.Timestamp("2022-09-10 00:00"), pd.Timestamp("2022-10-22 00:00")),
    (pd.Timestamp("2024-05-15 00:00"), pd.Timestamp("2024-08-21 00:00")),
    (pd.Timestamp("2025-07-14 21:30"), pd.Timestamp("2025-08-28 00:15")),
    (pd.Timestamp("2025-09-12 21:30"), pd.Timestamp("2025-11-01 00:15")),
)
AA_CENX_KAPPA_INTEGRAL_BARS = BARS_PER_TRADING_DAY
AA_CENX_KAPPA_INTEGRAL_C = 11.64


def normalize_ticker(value: str) -> str:
    return str(value or "").strip().upper()


def quote_identifier(value: str) -> str:
    return "`" + str(value).replace("`", "``") + "`"


def fixed_beta_source_key(source: str) -> str:
    """Keep persisted source values stable even if the UI wording changes."""
    return "daily" if str(source).startswith("日线") else "5m"


def fixed_beta_pair_key(ticker_y: str, ticker_x: str) -> str:
    return f"{normalize_ticker(ticker_y)}_{normalize_ticker(ticker_x)}".lower()


def ensure_fixed_beta_pair_config_table(conn) -> None:
    """Create the dashboard's independent, per-source saved-config table."""
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {quote_identifier(FIXED_BETA_CONFIG_TABLE)} (
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


def load_fixed_beta_pair_config(pair_name: str, data_source: str) -> dict | None:
    """Load one saved Fixed Beta dashboard configuration, if present."""
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        ensure_fixed_beta_pair_config_table(conn)
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT train_start_bj, test_end_bj, fixed_beta, backtest_enabled,
                       entry_threshold, stop_loss_rule, take_profit_rule
                FROM {quote_identifier(FIXED_BETA_CONFIG_TABLE)}
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


def save_fixed_beta_pair_config(
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
    """Upsert the exact UI configuration used for a pair and bar frequency."""
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        ensure_fixed_beta_pair_config_table(conn)
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {quote_identifier(FIXED_BETA_CONFIG_TABLE)}
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
    """Load active metapairs; an empty result keeps the custom-pair mode usable."""
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
    """Add the notebook's causal fixed-window episode-level OLS κ̂ estimate.

    The 150-transition guard is appropriate for a 20-day 5-minute window, but
    cannot be met by a 20-bar daily window.  Scale the guard down for short
    windows while retaining at least three non-neutral transitions.
    """
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
    min_effective_transitions = min(
        KAPPA_MIN_EFFECTIVE_TRANSITIONS,
        max(3, int(np.ceil(rolling_window / 4))),
    )
    result["kappa_hat"] = (
        result["kappa_numerator"] / result["kappa_denominator"]
    ).where(
        (result["kappa_denominator"] > 1e-12)
        & (result["kappa_transition_count"] >= min_effective_transitions)
    )
    return result


def kappa_min_effective_transitions(rolling_window: int) -> int:
    """Return the same transition guard used by add_cumulative_kappa_hat."""
    return min(
        KAPPA_MIN_EFFECTIVE_TRANSITIONS,
        max(3, int(np.ceil(rolling_window / 4))),
    )


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


def entry_is_paused(timestamp, pause_windows: Iterable[tuple[pd.Timestamp, pd.Timestamp]]) -> bool:
    """Return whether a proposed entry close belongs to a research pause window."""
    moment = pd.Timestamp(timestamp)
    return any(pd.Timestamp(start) <= moment < pd.Timestamp(end) for start, end in pause_windows)


def add_kappa_integral_switch(
    data: pd.DataFrame,
    integration_bars: int,
    constant_c: float,
    intraday_daily_equivalent: bool,
) -> tuple[pd.DataFrame, tuple[tuple[pd.Timestamp, pd.Timestamp], ...]]:
    """Build the causal negative-battery gate ``I(t) + C > 0``.

    ``I(t)`` is the trailing signed κ̂ integral.  It remains negative in the
    normal mean-reverting regime; rising toward zero therefore activates the
    gate without needing a fitted percentile or any future bars.
    """
    result = data.copy()
    kappa = pd.to_numeric(result["kappa_hat"], errors="coerce")
    if intraday_daily_equivalent:
        kappa = np.expm1(KAPPA_DERIVATIVE_BARS_PER_TRADING_DAY * np.log1p(kappa))
    result["kappa_integral"] = kappa.rolling(
        int(integration_bars), min_periods=int(integration_bars)
    ).sum()
    result["kappa_integral_plus_c"] = result["kappa_integral"] + float(constant_c)
    result["negative_battery"] = result["kappa_integral_plus_c"].gt(0.0).fillna(False)
    result["positive_battery"] = result["kappa_integral_plus_c"].lt(0.0).fillna(False)

    windows: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    active_start = None
    for _, row in result.iterrows():
        active = bool(row["negative_battery"])
        if active and active_start is None:
            active_start = pd.Timestamp(row["time_bj"])
        elif not active and active_start is not None:
            windows.append((active_start, pd.Timestamp(row["time_bj"])))
            active_start = None
    if active_start is not None:
        windows.append((active_start, pd.Timestamp(result["time_bj"].iloc[-1]) + pd.Timedelta(minutes=5)))
    return result, tuple(windows)


def run_zscore_backtest(
    data: pd.DataFrame,
    beta: float,
    entry_z: float,
    stop_rule: str,
    take_rule: str,
    entry_pause_windows: Iterable[tuple[pd.Timestamp, pd.Timestamp]] = (),
    entry_filter_column: str | None = None,
) -> list[dict]:
    """Run a discrete close-to-close backtest with no interpolated prices.

    Every record is an executable bar close.  A re-entry requires the previous
    close to be outside the z-score band and this close to be back inside it.
    Stops and profits are evaluated only at later bar closes, so a daily run
    never invents an intraday crossing or a fractional timestamp.
    """
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
            paused = entry_is_paused(row["time_bj"], entry_pause_windows)
            entry_allowed = True if entry_filter_column is None else bool(row.get(entry_filter_column, False))
            if previous_z >= entry_z and is_inside and not paused and entry_allowed:
                side = "Short spread"
            elif previous_z <= -entry_z and is_inside and not paused and entry_allowed:
                side = "Long spread"
            else:
                previous_z = zscore
                previous_row = row
                continue
            open_trade = {"side": side, "entry_plot_x": int(row["plot_x"]), "entry_day": int(row["trading_day_number"]), "entry_time": row["time_bj"], "entry_z": float(zscore), "entry_spread": float(row["spread"]), "entry_y": float(row["y_price"]), "entry_x": float(row["x_price"])}
            gross = open_trade["entry_y"] + abs(float(beta)) * open_trade["entry_x"]
            entry_sigma = float(row["spread_sigma"])
            open_trade["entry_sigma"] = entry_sigma
            open_trade["sigma_pct"] = 0.0 if gross <= 0 or not np.isfinite(entry_sigma) else entry_sigma / gross * 100.0
            open_trade["peak_return_pct"] = 0.0
            open_trade["last_return_pct"] = 0.0
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
        # A negative battery is a hard capital-off state.  If it starts while
        # a trade is open, flatten at this causal bar close before evaluating
        # the strategy's ordinary stop or take-profit rules.
        if entry_is_paused(row["time_bj"], entry_pause_windows):
            open_trade.update({
                "exit_plot_x": int(row["plot_x"]),
                "exit_time": row["time_bj"],
                "exit_z": float(zscore),
                "exit_spread": float(row["spread"]),
                "exit_reason": "NEGATIVE_BATTERY_PAUSE",
                "pnl": pnl,
                "return_pct": return_pct,
            })
            trades.append(open_trade)
            open_trade = None
            previous_z = zscore
            previous_row = row
            continue
        exit_reason = None
        can_exit = int(row["trading_day_number"]) > int(open_trade["entry_day"])
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


def build_immediate_threshold_equity(
    data: pd.DataFrame, beta: float, entry_z: float, initial_equity: float = 10_000.0,
) -> tuple[pd.DataFrame, list[dict]]:
    """Compound the notebook's simple ±z entry / zero-crossing exit strategy.

    A signal observed at a bar close takes risk from the following bar.  This
    deliberately has no trailing stop or take-profit rule, so it is a useful
    comparison baseline rather than the default Batteries Lab execution model.
    """
    result = data[["plot_x", "time_bj", "y_price", "x_price", "spread", "zscore"]].copy()
    position = np.zeros(len(result), dtype=float)
    current_position = 0.0
    session_codes, _ = pd.factorize(result["time_bj"].dt.normalize(), sort=False)
    result["trading_day_number"] = session_codes
    trades: list[dict] = []
    open_trade: dict | None = None
    for index, zscore in enumerate(result["zscore"]):
        row = result.iloc[index]
        if not np.isfinite(zscore):
            current_position = 0.0
        elif current_position == 0.0 and zscore <= -float(entry_z):
            current_position = 1.0
            open_trade = {
                "side": "Long spread",
                "entry_plot_x": int(row["plot_x"]),
                "entry_day": int(row["trading_day_number"]),
                "entry_time": row["time_bj"],
                "entry_z": float(zscore),
                "entry_spread": float(row["spread"]),
                "entry_y": float(row["y_price"]),
                "entry_x": float(row["x_price"]),
            }
        elif current_position == 0.0 and zscore >= float(entry_z):
            current_position = -1.0
            open_trade = {
                "side": "Short spread",
                "entry_plot_x": int(row["plot_x"]),
                "entry_day": int(row["trading_day_number"]),
                "entry_time": row["time_bj"],
                "entry_z": float(zscore),
                "entry_spread": float(row["spread"]),
                "entry_y": float(row["y_price"]),
                "entry_x": float(row["x_price"]),
            }
        elif current_position > 0.0 and zscore >= 0.0:
            current_position = 0.0
            if open_trade is not None:
                gross = open_trade["entry_y"] + abs(float(beta)) * open_trade["entry_x"]
                pnl = float(row["spread"]) - open_trade["entry_spread"]
                open_trade.update({
                    "exit_plot_x": int(row["plot_x"]), "exit_time": row["time_bj"],
                    "exit_z": float(zscore), "exit_spread": float(row["spread"]),
                    "exit_reason": "ZERO_CROSSING", "pnl": pnl,
                    "return_pct": 100.0 * pnl / gross if gross > 0 else 0.0,
                })
                trades.append(open_trade)
                open_trade = None
        elif current_position < 0.0 and zscore <= 0.0:
            current_position = 0.0
            if open_trade is not None:
                gross = open_trade["entry_y"] + abs(float(beta)) * open_trade["entry_x"]
                pnl = -(float(row["spread"]) - open_trade["entry_spread"])
                open_trade.update({
                    "exit_plot_x": int(row["plot_x"]), "exit_time": row["time_bj"],
                    "exit_z": float(zscore), "exit_spread": float(row["spread"]),
                    "exit_reason": "ZERO_CROSSING", "pnl": pnl,
                    "return_pct": 100.0 * pnl / gross if gross > 0 else 0.0,
                })
                trades.append(open_trade)
                open_trade = None
        position[index] = current_position

    if open_trade is not None:
        row = result.iloc[-1]
        direction = 1.0 if open_trade["side"] == "Long spread" else -1.0
        gross = open_trade["entry_y"] + abs(float(beta)) * open_trade["entry_x"]
        pnl = direction * (float(row["spread"]) - open_trade["entry_spread"])
        open_trade.update({
            "exit_plot_x": int(row["plot_x"]), "exit_time": row["time_bj"],
            "exit_z": float(row["zscore"]) if np.isfinite(row["zscore"]) else np.nan,
            "exit_spread": float(row["spread"]), "exit_reason": "END_OF_BACKTEST", "pnl": pnl,
            "return_pct": 100.0 * pnl / gross if gross > 0 else 0.0,
        })
        trades.append(open_trade)

    gross_notional = result["y_price"].shift(1) + abs(float(beta)) * result["x_price"].shift(1)
    spread_return = result["spread"].diff() / gross_notional
    result["strategy_return"] = (
        pd.Series(position, index=result.index).shift(1).fillna(0.0) * spread_return.fillna(0.0)
    )
    result["equity"] = float(initial_equity) * (1.0 + result["strategy_return"]).cumprod()
    return result, trades


def run_reentry_zero_backtest(data: pd.DataFrame, beta: float, entry_z: float) -> list[dict]:
    """Enter on a causal re-entry inside ±z, then hold until the z=0 crossing.

    This intentionally has neither a stop loss nor a minimum holding-period
    constraint.  It is the re-entry counterpart to the immediate-threshold
    research baseline.
    """
    trades: list[dict] = []
    open_trade: dict | None = None
    previous_z = np.nan
    for _, row in data.iterrows():
        zscore = row["zscore"]
        if not np.isfinite(zscore):
            continue
        if open_trade is None:
            is_inside = -float(entry_z) < zscore < float(entry_z)
            if previous_z >= float(entry_z) and is_inside:
                side = "Short spread"
            elif previous_z <= -float(entry_z) and is_inside:
                side = "Long spread"
            else:
                previous_z = zscore
                continue
            open_trade = {
                "side": side,
                "entry_plot_x": int(row["plot_x"]),
                "entry_day": int(row["trading_day_number"]),
                "entry_time": row["time_bj"],
                "entry_z": float(zscore),
                "entry_spread": float(row["spread"]),
                "entry_y": float(row["y_price"]),
                "entry_x": float(row["x_price"]),
            }
            previous_z = zscore
            continue

        direction = 1.0 if open_trade["side"] == "Long spread" else -1.0
        exits_at_zero = (direction > 0.0 and zscore >= 0.0) or (
            direction < 0.0 and zscore <= 0.0
        )
        if exits_at_zero:
            gross = open_trade["entry_y"] + abs(float(beta)) * open_trade["entry_x"]
            pnl = direction * (float(row["spread"]) - open_trade["entry_spread"])
            open_trade.update({
                "exit_plot_x": int(row["plot_x"]),
                "exit_time": row["time_bj"],
                "exit_z": float(zscore),
                "exit_spread": float(row["spread"]),
                "exit_reason": "ZERO_CROSSING",
                "pnl": pnl,
                "return_pct": 100.0 * pnl / gross if gross > 0 else 0.0,
            })
            trades.append(open_trade)
            open_trade = None
        previous_z = zscore

    if open_trade is not None:
        row = data.iloc[-1]
        direction = 1.0 if open_trade["side"] == "Long spread" else -1.0
        gross = open_trade["entry_y"] + abs(float(beta)) * open_trade["entry_x"]
        pnl = direction * (float(row["spread"]) - open_trade["entry_spread"])
        open_trade.update({
            "exit_plot_x": int(row["plot_x"]),
            "exit_time": row["time_bj"],
            "exit_z": float(row["zscore"]) if np.isfinite(row["zscore"]) else np.nan,
            "exit_spread": float(row["spread"]),
            "exit_reason": "END_OF_BACKTEST",
            "pnl": pnl,
            "return_pct": 100.0 * pnl / gross if gross > 0 else 0.0,
        })
        trades.append(open_trade)
    return trades


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


def annualized_daily_sharpe(equity_data: pd.DataFrame, periods_per_year: int = 252) -> tuple[float, int]:
    """Annualized zero-risk-free-rate Sharpe from daily closing equity."""
    if equity_data.empty:
        return float("nan"), 0
    daily_equity = (
        equity_data.assign(trading_date=equity_data["time_bj"].dt.date)
        .groupby("trading_date", sort=True)["equity"]
        .last()
    )
    daily_returns = daily_equity.pct_change().dropna()
    volatility = float(daily_returns.std(ddof=1)) if len(daily_returns) > 1 else float("nan")
    if not np.isfinite(volatility) or volatility <= 0:
        return float("nan"), len(daily_returns)
    return float(daily_returns.mean() / volatility * np.sqrt(periods_per_year)), len(daily_returns)


def beta_grid(lower: float, upper: float, step: float) -> np.ndarray:
    """Return an inclusive, numerically stable beta grid."""
    if not np.isfinite(lower) or not np.isfinite(upper) or not np.isfinite(step) or step <= 0 or upper <= lower:
        return np.array([], dtype=float)
    count = int(np.floor((upper - lower) / step + 1e-9))
    values = lower + step * np.arange(count + 1)
    if values[-1] < upper - step * 1e-7:
        values = np.append(values, upper)
    return np.round(values, 10)


def run_beta_sweep(
    raw_data: pd.DataFrame,
    betas: np.ndarray,
    window: int,
    episode_window: int,
    entry_z: float,
    stop_rule: str,
    take_rule: str,
    initial_equity: float = 10_000.0,
    entry_pause_windows: Iterable[tuple[pd.Timestamp, pd.Timestamp]] = (),
    positive_battery_only: bool = False,
    kappa_integral_c: float = AA_CENX_KAPPA_INTEGRAL_C,
    intraday_daily_equivalent: bool = False,
    backtest_mode: str = "lab",
) -> pd.DataFrame:
    """Evaluate fixed beta values using the dashboard's selected evaluation mode."""
    rows: list[dict[str, float | int]] = []
    for candidate_beta in betas:
        candidate_data = add_fixed_beta_indicators(
            raw_data, float(candidate_beta), window, mean_derivative_window=episode_window
        )
        candidate_data = add_trading_day_numbers(candidate_data, 0)
        entry_filter_column = None
        if positive_battery_only:
            candidate_data = add_cumulative_kappa_hat(candidate_data, rolling_window=episode_window)
            candidate_data, _ = add_kappa_integral_switch(
                candidate_data,
                AA_CENX_KAPPA_INTEGRAL_BARS,
                float(kappa_integral_c),
                intraday_daily_equivalent=intraday_daily_equivalent,
            )
            entry_filter_column = "positive_battery"
        if backtest_mode == "immediate":
            equity, trades = build_immediate_threshold_equity(
                candidate_data, float(candidate_beta), float(entry_z), initial_equity
            )
        elif backtest_mode == "reentry_zero":
            trades = run_reentry_zero_backtest(
                candidate_data, float(candidate_beta), float(entry_z)
            )
            equity = build_equity_curve(candidate_data, trades, float(candidate_beta), initial_equity)
        else:
            trades = run_zscore_backtest(
                candidate_data, float(candidate_beta), float(entry_z), stop_rule, take_rule,
                entry_pause_windows=entry_pause_windows,
                entry_filter_column=entry_filter_column,
            )
            equity = build_equity_curve(candidate_data, trades, float(candidate_beta), initial_equity)
        max_drawdown_pct, _, _ = maximum_drawdown_details(equity)
        final_equity = float(equity["equity"].iloc[-1])
        rows.append(
            {
                "beta": float(candidate_beta),
                "final_equity": final_equity,
                "return_pct": 100.0 * (final_equity / initial_equity - 1.0),
                "max_drawdown_pct": max_drawdown_pct,
                "trades": len(trades),
            }
        )
    return pd.DataFrame(rows)


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


def add_trade_return_fills(
    figure: go.Figure,
    data: pd.DataFrame,
    trades: list[dict],
    value_column: str,
    baseline_column: str | None = None,
    baseline_value: float = 0.0,
    fill_opacity: float = 0.34,
) -> None:
    """Embed each completed trade into a curve with outcome-colored fills."""
    for trade in trades:
        segment = data.loc[
            data["plot_x"].between(trade["entry_plot_x"], trade["exit_plot_x"])
        ]
        if len(segment) < 2:
            continue
        x_values = segment["plot_x"].astype(float).tolist()
        y_values = pd.to_numeric(segment[value_column], errors="coerce").tolist()
        baseline = (
            float(segment[baseline_column].iloc[0])
            if baseline_column is not None
            else float(baseline_value)
        )
        fill_color = (
            f"rgba(132, 204, 22, {fill_opacity:.2f})"  # yellow-green: winning trade
            if float(trade["return_pct"]) >= 0.0
            else f"rgba(250, 204, 21, {min(1.0, fill_opacity + 0.02):.2f})"  # yellow: losing trade
        )
        figure.add_trace(go.Scatter(
            x=[*x_values, *reversed(x_values)],
            y=[*y_values, *([baseline] * len(y_values))],
            mode="lines",
            line=dict(width=0),
            fill="toself",
            fillcolor=fill_color,
            hoverinfo="skip",
            showlegend=False,
            name="Winning trade" if float(trade["return_pct"]) >= 0.0 else "Losing trade",
        ))


def add_entry_pause_curve_masks(
    figure: go.Figure,
    data: pd.DataFrame,
    value_column: str,
    baseline: float,
    pause_windows: Iterable[tuple[pd.Timestamp, pd.Timestamp]],
) -> None:
    """Embed pause state in the curve rather than covering it with rectangles."""
    for start, end in pause_windows:
        paused = data.loc[
            (data["time_bj"] >= pd.Timestamp(start)) & (data["time_bj"] < pd.Timestamp(end))
        ]
        if paused.empty:
            continue
        x_values = paused["plot_x"].astype(float).tolist()
        y_values = pd.to_numeric(paused[value_column], errors="coerce").tolist()
        figure.add_trace(go.Scatter(
            x=[*x_values, *reversed(x_values)],
            y=[*y_values, *([float(baseline)] * len(y_values))],
            mode="lines",
            line=dict(width=0),
            fill="toself",
            # A translucent neutral-gray fill retains the paused curve's
            # shape without overpowering the surrounding active region.
            fillcolor="rgba(128, 128, 128, 0.42)",
            hoverinfo="skip",
            showlegend=False,
            name="Entry pause",
        ))
        figure.add_trace(go.Scatter(
            x=x_values,
            y=y_values,
            mode="lines",
            line=dict(color="#4a4a4a", width=2.8),
            hoverinfo="skip",
            showlegend=False,
            name="Entry pause curve",
        ))


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


def box_selection_bounds(selection) -> tuple[float, float] | None:
    """Extract Plotly's x-range from the most recent rectangular selection."""
    boxes = selection.get("box", []) if selection else []
    if not boxes:
        return None
    box = boxes[-1]
    x_values = box.get("x") or box.get("xrange")
    if x_values is None and isinstance(box.get("range"), dict):
        x_values = box["range"].get("x")
    if not isinstance(x_values, (list, tuple)) or len(x_values) != 2:
        return None
    try:
        left, right = sorted(float(value) for value in x_values)
    except (TypeError, ValueError):
        return None
    return left, right


def times_in_box(plot_data: pd.DataFrame, bounds: tuple[float, float] | None) -> tuple[str, str] | None:
    """Map a selected x-range to the first and last actual bars it covers."""
    if bounds is None:
        return None
    left_x, right_x = bounds
    selected = plot_data.loc[
        plot_data["plot_x"].between(np.ceil(left_x), np.floor(right_x))
    ]
    if selected.empty:
        selected = plot_data.iloc[
            [
                int((plot_data["plot_x"] - left_x).abs().to_numpy().argmin()),
                int((plot_data["plot_x"] - right_x).abs().to_numpy().argmin()),
            ]
        ]
    return (
        selected["time_bj"].iloc[0].strftime("%Y-%m-%d %H:%M:%S"),
        selected["time_bj"].iloc[-1].strftime("%Y-%m-%d %H:%M:%S"),
    )


st.set_page_config(page_title="Trading Batteries Lab", layout="wide")

# Apply a point selected on the spread chart before its date widgets are
# created.  This keeps slicing interaction deterministic across reruns.
pending_slice = st.session_state.pop("batteries_pending_slice", None)
if pending_slice:
    boundary = pending_slice[0]
    if boundary == "range":
        _, start_timestamp, end_timestamp, start_key, end_key = pending_slice
        st.session_state[start_key] = start_timestamp
        st.session_state[end_key] = end_timestamp
    else:
        _, timestamp, start_key, end_key = pending_slice
        st.session_state[start_key if boundary == "start" else end_key] = timestamp

if PENDING_BACKTEST_ENABLED_STATE_KEY in st.session_state:
    st.session_state[BACKTEST_ENABLED_STATE_KEY] = bool(st.session_state.pop(PENDING_BACKTEST_ENABLED_STATE_KEY))

st.sidebar.header("Pair")
backtest_requested = st.sidebar.toggle("开启评估", value=False, key=BACKTEST_ENABLED_STATE_KEY)
pairs = load_registered_pairs()
pair_source = st.sidebar.radio("Pair source", ["Registered", "Custom"], horizontal=True)
if pair_source == "Registered" and pairs:
    pair_label = st.sidebar.selectbox("Select pair", list(pairs))
    ticker_y, ticker_x = pairs[pair_label]
else:
    if pair_source == "Registered":
        st.sidebar.info("没有可读取的 metapairs；请使用 Custom。")
    ticker_y = normalize_ticker(st.sidebar.text_input("Y ticker", value="AA"))
    ticker_x = normalize_ticker(st.sidebar.text_input("X ticker", value="CENX"))
    pair_label = f"{ticker_y} / {ticker_x}"

if not ticker_y or not ticker_x or ticker_y == ticker_x:
    st.error("请输入两只不同的 ticker。")
    st.stop()

data_source = st.sidebar.radio(
    "Data source",
    ["日线（us_market_days）", "5-minute（us_market_prices）"],
    help="两种模式都只保留两条腿共同存在的时间戳。",
)
window = ROLLING_DAYS if data_source.startswith("日线") else ROLLING_DAYS * BARS_PER_TRADING_DAY
window_label = f"{ROLLING_DAYS} 个交易日" if data_source.startswith("日线") else f"{ROLLING_DAYS} 个交易日（{window:,} 个 5 分钟 bars）"
episode_window = ROLLING_DAYS if data_source.startswith("日线") else ROLLING_DAYS * KAPPA_DERIVATIVE_BARS_PER_TRADING_DAY
episode_window_label = f"{ROLLING_DAYS} 个交易日" if data_source.startswith("日线") else f"{ROLLING_DAYS}×{KAPPA_DERIVATIVE_BARS_PER_TRADING_DAY} = {episode_window:,} 个 5 分钟 bars"
is_daily = data_source.startswith("日线")
backtest_enabled = bool(backtest_requested)
if backtest_requested and is_daily:
    st.sidebar.info("日线严格按离散收盘点回测：不在两次收盘之间插值，不生成日内虚构成交。")

try:
    raw_data, actual_source = load_pair_data(ticker_y, ticker_x, data_source)
except Exception as exc:
    st.error(f"无法加载 {pair_label}：{exc}")
    st.stop()

if len(raw_data) < window + 1:
    st.error(f"共同数据只有 {len(raw_data)} 行；{window_label}至少需要 {window + 1} 行才能显示均线变化率。")
    st.stop()

display_start_key = f"batteries_start_text_bj::{ticker_y}::{ticker_x}"
if display_start_key not in st.session_state:
    st.session_state[display_start_key] = raw_data["time_bj"].iloc[0].strftime("%Y-%m-%d %H:%M")
display_end_key = f"batteries_end_text_bj::{ticker_y}::{ticker_x}"
if display_end_key not in st.session_state:
    st.session_state[display_end_key] = raw_data["time_bj"].iloc[-1].strftime("%Y-%m-%d %H:%M")
beta_state_key = f"batteries_beta::{ticker_y}::{ticker_x}"
# Preserve the dashboard's existing all-history initialization.  Only the
# explicit Fit action below switches to the selected-range minimum-variance fit.
full_history_beta = fit_endpoint_detrended_beta(raw_data["y_price"], raw_data["x_price"])
if beta_state_key not in st.session_state:
    st.session_state[beta_state_key] = float(full_history_beta) if np.isfinite(full_history_beta) else 1.0
# The overview uses one frozen beta so it remains a stable navigation map while
# the user experiments with the local slice's beta.
initial_beta_key = f"batteries_initial_beta::{ticker_y}::{ticker_x}"
if initial_beta_key not in st.session_state:
    st.session_state[initial_beta_key] = float(full_history_beta) if np.isfinite(full_history_beta) else 1.0
initial_beta = float(st.session_state[initial_beta_key])

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
        saved_config = load_fixed_beta_pair_config(saved_config_pair, saved_config_source)
        if saved_config is None:
            st.sidebar.info("当前 pair 与数据频率没有已储存的配置。")
        else:
            st.session_state[display_start_key] = pd.Timestamp(saved_config["train_start_bj"]).strftime("%Y-%m-%d %H:%M")
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
        save_fixed_beta_pair_config(
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
st.sidebar.caption("beta 始终以原始价格单位计算。修改数值或切片后，图表和回测即时按该固定 beta 重算。")
fit_full_col, fit_slice_col = st.sidebar.columns(2)
if fit_full_col.button("端点去趋势拟合"):
    st.session_state[beta_state_key] = float(full_history_beta) if np.isfinite(full_history_beta) else 1.0
if fit_slice_col.button("当前切片最小方差"):
    if np.isfinite(selected_history_beta):
        st.session_state[beta_state_key] = float(selected_history_beta)
    else:
        st.sidebar.warning("当前区间无法拟合 beta；请扩大区间。")
beta = st.sidebar.number_input(
    "Fixed beta（高亮参数）",
    step=0.001,
    format="%.6f",
    key=beta_state_key,
    help="可直接键入任意固定 beta；不会自动滚动重估。",
)
st.sidebar.metric("当前 beta", f"{beta:.6f}")

data = add_fixed_beta_indicators(selected_history, beta, window, mean_derivative_window=episode_window)
data = add_trading_day_numbers(data, 0)
kappa_data = add_cumulative_kappa_hat(data, rolling_window=episode_window)
plot_data = data.copy()
global_plot_data = raw_data[["time_bj", "y_price", "x_price"]].copy()
global_plot_data["plot_x"] = np.arange(len(global_plot_data), dtype=int)
global_plot_data["spread"] = (
    global_plot_data["y_price"] - initial_beta * global_plot_data["x_price"]
)
hover_times_bj = plot_data["time_bj"].dt.strftime("%Y-%m-%d %H:%M:%S")
entry_z, initial_equity = 2.0, 10_000.0
entry_pause_windows: tuple[tuple[pd.Timestamp, pd.Timestamp], ...] = ()
entry_filter_column: str | None = None
backtest_mode = "lab"
trade_count = 0
if backtest_enabled:
    st.sidebar.subheader("日线回测规则" if is_daily else "回测规则")
    entry_z = st.sidebar.number_input("入场阈值 |z|", min_value=0.1, value=2.0, step=0.1, key=f"{backtest_key}::entry")
    backtest_mode = st.sidebar.radio(
        "回测执行模式",
        [
            "Batteries Lab（默认）",
            "即时阈值 / 0 出场（基础策略）",
            "回穿阈值 / 0 出场（基础策略）",
        ],
        horizontal=False,
        key=f"{backtest_key}::mode",
    )
    backtest_mode = (
        "immediate" if backtest_mode.startswith("即时")
        else "reentry_zero" if backtest_mode.startswith("回穿")
        else "lab"
    )
    if backtest_mode == "immediate":
        stop_rule, take_rule = "", ""
        st.sidebar.caption(
            f"研究基线：z 触及 ±{entry_z:.2f} 即入场，回到 0 出场；无止损、无止盈。"
            "信号在收盘确认，下一根 bar 才承担价格变化。"
        )
        equity_data, trades = build_immediate_threshold_equity(
            plot_data, beta, float(entry_z), initial_equity
        )
        trade_count = len(trades)
    elif backtest_mode == "reentry_zero":
        stop_rule, take_rule = "", "z(0)"
        st.sidebar.caption(
            f"基础策略：z 先在 ±{entry_z:.2f} 外，再回到带内时入场；"
            "无止损，直到 z 穿过 0 才平仓。"
        )
        trades = run_reentry_zero_backtest(plot_data, beta, float(entry_z))
        equity_data = build_equity_curve(plot_data, trades, beta, initial_equity)
        trade_count = len(trades)
    else:
        stop_rule = st.sidebar.text_input("Stop loss rule", value=default_stop_rule, key=f"{backtest_key}::stop_rule", help="支持 trailing(-1*sigma) 或百分比数值。")
        take_rule = st.sidebar.text_input("Take profit rule", value=default_take_rule, key=f"{backtest_key}::take_rule", help="支持 z(0)、trailing(-0.5*sigma) 或百分比数值；留空表示不设止盈。")
        if is_daily:
            st.sidebar.caption(f"日线：前一收盘在 ±{entry_z:.2f} 外、当前收盘回到带内时入场；持仓至少跨越一个完整交易日，所有出场只在后续收盘点成交。")
        else:
            st.sidebar.caption(f"5 分钟：z 先在 ±{entry_z:.2f} 外，再回穿至带内；从上方回穿做空 spread，从下方回穿做多 spread。")
        st.sidebar.caption(f"出场：每根 bar 先执行 Stop loss `{stop_rule}`，再执行 Take profit `{take_rule or '未配置'}`。")
        trades = run_zscore_backtest(
            plot_data, beta, float(entry_z), stop_rule, take_rule,
            entry_pause_windows=entry_pause_windows,
            entry_filter_column=entry_filter_column,
        )
        equity_data = build_equity_curve(plot_data, trades, beta, initial_equity)
        trade_count = len(trades)

    scan_scope = (
        f"{ticker_y}:{ticker_x}:{data_source}:{display_start}:{display_end}:"
        f"{window}:{episode_window}:{backtest_mode}:{entry_z}:{stop_rule}:{take_rule}"
    )
    scan_results_key = f"batteries_beta_scan::{scan_scope}"
    scan_state = st.session_state.get(scan_results_key, {})
    st.sidebar.markdown("---")
    price_ratio = float(selected_history["y_price"].iloc[-1] / selected_history["x_price"].iloc[-1])
    if price_ratio < 1.0:
        default_scan_min, default_scan_max = 0.0, 1.0
    else:
        default_scan_min = 1.0
        default_scan_max = float(max(2, np.ceil(price_ratio)))
    st.sidebar.subheader("Beta 日线扫描" if is_daily else "Beta 扫描")
    st.sidebar.caption(
        f"最新价格比 Y/X = {price_ratio:.3f}；默认扫描范围为 "
        f"{default_scan_min:.2f}–{default_scan_max:.2f}。"
        + ("日线扫描只使用离散收盘点，不含任何日内插值。" if is_daily else "扫描仅运行 z-score 回测，不计算 kappa。")
        + (
            "当前使用即时阈值 / 0 出场基础策略。" if backtest_mode == "immediate"
            else "当前使用回穿阈值 / 0 出场基础策略。" if backtest_mode == "reentry_zero"
            else ""
        )
    )
    scan_min = st.sidebar.number_input("扫描 beta 下限", value=default_scan_min, step=0.05, format="%.6f", key=f"batteries_scan_min::{scan_scope}")
    scan_max = st.sidebar.number_input("扫描 beta 上限", value=default_scan_max, step=0.05, format="%.6f", key=f"batteries_scan_max::{scan_scope}")
    scan_step = st.sidebar.number_input("扫描步长", min_value=0.0001, value=0.0500, step=0.01, format="%.6f", key=f"batteries_scan_step::{scan_scope}")

    if st.sidebar.button("运行 beta 扫描", key=f"batteries_run_scan::{scan_scope}"):
        candidates = beta_grid(scan_min, scan_max, scan_step)
        if len(candidates) > 160:
            st.sidebar.error("当前扫描超过 160 个 beta；请缩小区间或增大步长。")
        elif len(candidates) < 2:
            st.sidebar.error("请提供至少两个 beta 候选值。")
        else:
            with st.spinner(f"正在扫描 {len(candidates)} 个 beta…"):
                scan_result = run_beta_sweep(
                    selected_history, candidates, window, episode_window, entry_z, stop_rule,
                    take_rule, initial_equity, entry_pause_windows=entry_pause_windows,
                    backtest_mode=backtest_mode,
                )
            st.session_state[scan_results_key] = {"result": scan_result}
            st.rerun()
else:
    trades, equity_data = [], None
last = data.iloc[-1]
ready = data.dropna(subset=["spread_mean", "spread_sigma"])

st.title(f"{pair_label} · Trading Batteries Lab")
st.caption(
    f"数据：{actual_source} · **Fixed beta = {beta:.6f}** · "
    f"切片：{plot_data['time_bj'].iloc[0]:%Y-%m-%d %H:%M} 至 {plot_data['time_bj'].iloc[-1]:%Y-%m-%d %H:%M} · "
    f"trailing window：{window_label}"
)

metric_1, metric_2, metric_3, metric_4, metric_5 = st.columns(5)
metric_1.metric(ticker_y, f"{last.y_price:.2f}")
metric_2.metric(ticker_x, f"{last.x_price:.2f}")
metric_3.metric("Spread", f"{last.spread:.5f}")
metric_4.metric("Rolling z-score", "n/a" if pd.isna(last.zscore) else f"{last.zscore:.3f}")
metric_5.metric("Fixed beta", f"{beta:.6f}")

if backtest_enabled and is_daily:
    st.success("严格日线回测：每个点都是一个独立的日收盘；入场、止损和止盈绝不在相邻收盘之间插值。")

if backtest_enabled and scan_state:
    scan_result = scan_state.get("result")
    if isinstance(scan_result, pd.DataFrame) and not scan_result.empty:
        scan_best = scan_result.loc[scan_result["return_pct"].idxmax()]
        st.subheader("Beta 日线扫描结果" if is_daily else "Beta 扫描结果")
        result_col, action_col = st.columns([4, 1])
        result_col.metric("最高日线回测收益 beta" if is_daily else "最高回测收益 beta", f"{scan_best['beta']:.6f}", f"{scan_best['return_pct']:.2f}%")
        if action_col.button("采用此 beta", key=f"batteries_use_scanned_beta::{scan_scope}"):
            st.session_state[beta_state_key] = float(scan_best["beta"])
            st.rerun()
        scan_fig = go.Figure()
        scan_fig.add_trace(go.Scatter(
            x=scan_result["beta"], y=scan_result["return_pct"], mode="lines+markers",
            name="回测收益", line=dict(color="#38bdf8"),
            hovertemplate="beta %{x:.6f}<br>收益 %{y:.2f}%<extra></extra>",
        ))
        scan_fig.add_trace(go.Scatter(
            x=scan_result["beta"], y=scan_result["max_drawdown_pct"], mode="lines+markers",
            name="最大回撤", line=dict(color="#f97316"), yaxis="y2",
            hovertemplate="beta %{x:.6f}<br>最大回撤 %{y:.2f}%<extra></extra>",
        ))
        scan_fig.update_layout(
            template="plotly_dark", height=300, xaxis_title="Fixed beta", yaxis_title="收益 (%)",
            yaxis2=dict(title="最大回撤 (%)", overlaying="y", side="right"),
            legend_orientation="h", margin=dict(l=20, r=20, t=30, b=20),
        )
        st.plotly_chart(scan_fig, use_container_width=True)
        st.dataframe(
            scan_result.sort_values("return_pct", ascending=False).reset_index(drop=True),
            use_container_width=True,
            hide_index=True,
            column_config={
                "beta": st.column_config.NumberColumn("beta", format="%.6f"),
                "final_equity": st.column_config.NumberColumn("期末权益", format="$%.2f"),
                "return_pct": st.column_config.NumberColumn("收益", format="%.2f%%"),
                "max_drawdown_pct": st.column_config.NumberColumn("最大回撤", format="%.2f%%"),
                "trades": st.column_config.NumberColumn("交易数"),
            },
        )

st.caption(
    f"全局导航（frozen initial beta = {initial_beta:.6f}）：拖动深蓝框的边缘或重新拖出范围；"
    "仅点击“应用区间”才重算页面。"
)
global_range = render_overview_navigator(
    global_plot_data["spread"].to_numpy(),
    global_plot_data["time_bj"].dt.strftime("%Y-%m-%d %H:%M").tolist(),
    display_start,
    display_end,
    # The displayed time range is shared across frequencies, but a component
    # selection is an index range and must not be replayed onto another bar grid.
    key=f"batteries_global_navigator::{data_source}",
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
        st.session_state["batteries_pending_slice"] = (
            "range",
            raw_data["time_bj"].iloc[global_start].strftime("%Y-%m-%d %H:%M:%S"),
            raw_data["time_bj"].iloc[global_end].strftime("%Y-%m-%d %H:%M:%S"),
            display_start_key,
            display_end_key,
        )
        st.rerun()

fig = go.Figure()
fig.add_trace(go.Scatter(
    x=plot_data["plot_x"],
    y=plot_data["spread"],
    customdata=hover_times_bj,
    name="Fixed-beta spread",
    mode="lines",
    line=dict(color="#1f77b4"),
    hovertemplate="北京时间 %{customdata}<br>Spread %{y:.5f}<extra></extra>",
))
fig.add_trace(go.Scatter(x=plot_data["plot_x"], y=plot_data["spread_mean"], customdata=hover_times_bj, name=f"Trailing mean ({window_label})", line=dict(color="#9ca3af", width=2, dash="dash"), hoverinfo="skip"))
fig.add_trace(go.Scatter(x=plot_data["plot_x"], y=plot_data["upper_2sigma"], customdata=hover_times_bj, name="Mean + 2σ", line=dict(color="#ef4444", dash="dot"), hoverinfo="skip"))
fig.add_trace(go.Scatter(x=plot_data["plot_x"], y=plot_data["lower_2sigma"], customdata=hover_times_bj, name="Mean − 2σ", line=dict(color="#ef4444", dash="dot"), hoverinfo="skip"))
fig.update_layout(
    template="plotly_dark",
    height=560,
    yaxis_title="Fixed-beta price spread",
    legend_orientation="h",
    title=f"Fixed beta = {beta:.6f}",
    dragmode="select",
)
use_trading_day_axis(fig, plot_data)
if backtest_enabled:
    add_backtest_trade_overlays(fig, plot_data, trades, "spread")
st.caption("切片：在 spread 图上拖出一个框。设为数据起点时使用框的左端；设为数据终点时使用框的右端。")
spread_event = st.plotly_chart(
    fig,
    use_container_width=True,
    key="batteries_spread_chart",
    on_select="rerun",
    selection_mode="box",
)
selected_points = spread_event.selection.get("points", []) if spread_event else []
selected_times = sorted(
    str(point["customdata"])
    for point in selected_points
    if point.get("customdata")
)
selected_bounds = box_selection_bounds(spread_event.selection if spread_event else None)
if selected_bounds is not None:
    left_x, right_x = selected_bounds
    selected_slice = plot_data.loc[
        plot_data["plot_x"].between(np.ceil(left_x), np.floor(right_x))
    ]
    if selected_slice.empty:
        selected_slice = plot_data.iloc[
            [
                int((plot_data["plot_x"] - left_x).abs().to_numpy().argmin()),
                int((plot_data["plot_x"] - right_x).abs().to_numpy().argmin()),
            ]
        ]
    selected_times = [
        selected_slice["time_bj"].iloc[0].strftime("%Y-%m-%d %H:%M:%S"),
        selected_slice["time_bj"].iloc[-1].strftime("%Y-%m-%d %H:%M:%S"),
    ]
if selected_times:
    selected_start_time, selected_end_time = selected_times[0], selected_times[-1]
    st.caption(f"已框选范围：{selected_start_time} 至 {selected_end_time}。")
    set_start_col, set_end_col, set_range_col = st.columns(3)
    if set_start_col.button("设为数据起点", key="batteries_set_slice_start"):
        st.session_state["batteries_pending_slice"] = (
            "start",
            selected_start_time,
            display_start_key,
            display_end_key,
        )
        st.rerun()
    if set_end_col.button("设为数据终点", key="batteries_set_slice_end"):
        st.session_state["batteries_pending_slice"] = (
            "end",
            selected_end_time,
            display_start_key,
            display_end_key,
        )
        st.rerun()
    if set_range_col.button("同时设置区间", key="batteries_set_slice_range"):
        st.session_state["batteries_pending_slice"] = (
            "range",
            selected_start_time,
            selected_end_time,
            display_start_key,
            display_end_key,
        )
        st.rerun()

zfig = go.Figure()
zfig.add_trace(go.Scatter(x=plot_data["plot_x"], y=plot_data["zscore"], customdata=hover_times_bj, name="Causal rolling z-score", line=dict(color="#a78bfa"), hovertemplate="北京时间 %{customdata}<br>z-score %{y:.3f}<extra></extra>"))
zfig.add_hline(y=0, line_dash="dash", line_color="gray")
zfig.add_hline(y=entry_z, line_dash="dot", line_color="#ef4444")
zfig.add_hline(y=-entry_z, line_dash="dot", line_color="#ef4444")
zfig.update_layout(height=320, yaxis_title="z-score", showlegend=False)
use_trading_day_axis(zfig, plot_data)
if backtest_enabled:
    add_backtest_trade_overlays(zfig, plot_data, trades, "zscore")
st.plotly_chart(zfig, use_container_width=True)

if backtest_enabled and equity_data is not None:
    equity_times_bj = equity_data["time_bj"].dt.strftime("%Y-%m-%d %H:%M:%S")
    equity_fig = go.Figure()
    # Use one visible floor for every trade, so its result color reaches the
    # chart's horizontal baseline rather than becoming a thin entry-to-exit band.
    equity_fill_floor = max(1.0, float(equity_data["equity"].min()) * 0.98)
    add_trade_return_fills(
        equity_fig, equity_data, trades, "equity",
        baseline_value=equity_fill_floor, fill_opacity=0.42,
    )
    equity_fig.add_trace(go.Scatter(x=equity_data["plot_x"], y=equity_data["equity"], customdata=equity_times_bj, name="Equity", line=dict(color="#2563eb", width=2.2), hovertemplate="北京时间 %{customdata}<br>Equity $%{y:,.2f}<extra></extra>"))
    equity_fig.add_hline(y=initial_equity, line_dash="dash", line_color="#6b7280")
    equity_fig.update_layout(template="plotly_dark", height=360, yaxis_title="Equity", showlegend=False)
    use_trading_day_axis(equity_fig, plot_data)
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
    sharpe_ratio, sharpe_days = annualized_daily_sharpe(equity_data)
    evaluation_prefix = "日线 " if is_daily else ""
    backtest_metrics = st.columns(6)
    backtest_metrics[0].metric(
        f"{evaluation_prefix}{'Trades' if backtest_mode == 'lab' else 'Entries'}", trade_count
    )
    final_return = (equity_data["equity"].iloc[-1] / initial_equity - 1.0) * 100.0
    backtest_metrics[1].metric(f"{evaluation_prefix}Final Equity", f"${equity_data['equity'].iloc[-1]:,.2f}", delta=f"{final_return:+.2f}%")
    winning_trades = sum(
        float(trade["return_pct"]) >= 0.0 if backtest_mode != "lab"
        else float(trade["return_pct"]) > 0.0
        for trade in trades
    )
    total_trades = len(trades)
    win_rate = 100.0 * winning_trades / total_trades if total_trades else None
    win_rate_text = "n/a" if win_rate is None else f"{win_rate:.2f}".rstrip("0").rstrip(".") + "%"
    win_count_text = f"{winning_trades}/{total_trades}"
    backtest_metrics[2].markdown(
        f"<div style='font-size:0.875rem; color:var(--secondary-text-color); margin-bottom:0.25rem'>Win Rate</div>"
        f"<div style='font-size:2.25rem; font-weight:400; line-height:1.2'>{win_count_text}&nbsp;&nbsp;{win_rate_text}</div>",
        unsafe_allow_html=True,
    )
    backtest_metrics[3].metric(f"{evaluation_prefix}Max Drawdown", f"{max_drawdown_pct:.2f}%", help=drawdown_help)
    sharpe_text = "n/a" if not np.isfinite(sharpe_ratio) else f"{sharpe_ratio:.2f}"
    backtest_metrics[4].metric(
        f"{evaluation_prefix}Sharpe（年化）",
        sharpe_text,
        help=(
            f"以 {sharpe_days} 个交易日的日末 equity 收益计算，年化因子为 √252，"
            "无风险利率暂按 0 处理。使用日末收益可避免 5 分钟权益曲线中的大量零变动影响解释。"
        ),
    )
    average_trade_return = float(np.mean([trade["return_pct"] for trade in trades])) if trades else float("nan")
    backtest_metrics[5].metric(
        "Average Trade Return",
        "n/a" if not np.isfinite(average_trade_return) else f"{average_trade_return:+.2f}%",
        help="所有已完成交易的算术平均 return；用于检验正电池是否提高单笔质量。",
    )

kappa_fig = go.Figure()
kappa_plot_data = kappa_data[kappa_data["plot_x"] >= plot_data["plot_x"].iloc[0]].copy()
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
if backtest_enabled:
    add_trade_return_fills(kappa_fig, kappa_plot_data, trades, "kappa_display", baseline_value=0.0, fill_opacity=0.58)
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
    # κ̂ outliers are informative but should not flatten the ordinary
    # mean-reversion region.  Keep the positive observation ceiling stable
    # across pairs and slices; the lower bound still expands for negative data.
    kappa_floor = min(0.0, float(kappa_visible.min()))
    kappa_padding = max(0.025, (0.5 - kappa_floor) * 0.04)
    kappa_fig.update_yaxes(range=[kappa_floor - kappa_padding, 0.5])
use_trading_day_axis(kappa_fig, plot_data)
st.plotly_chart(kappa_fig, use_container_width=True)
st.caption(
    f"Rolling κ̂：中性区 ±{KAPPA_NEUTRAL_BOUNDARY:.2f}，固定 rolling OLS 窗口为 {episode_window_label}"
    f"；至少需要 {kappa_min_effective_transitions(episode_window)} 次有效 transition 才显示"
    f"{kappa_caption_scale}。负值表示窗口内距离收缩证据，正值表示窗口内距离扩张证据。"
    "纵轴上限固定为 0.5，超过上限的正向极值会被裁剪。"
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
if backtest_enabled:
    st.subheader("日线交易记录" if is_daily else "交易记录")
    if trades:
        exit_reason_labels = {
            "TRAILING_STOP": "Trailing Stop",
            "STOP_LOSS": "Stop Loss",
            "TAKE_PROFIT": "Take Profit",
            "NEGATIVE_BATTERY_PAUSE": "Negative Battery Pause",
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
