import json
import math
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import pymysql
import streamlit as st
import streamlit.components.v1 as components
from statsmodels.tsa.stattools import adfuller


DB_CONFIG = dict(
    host="localhost",
    user="quant",
    password="1q2w3e",
    database="quant_data",
)

MEMORY_DB_CONFIG = dict(
    host="localhost",
    user="quant",
    password="1q2w3e",
    database="quant_memory",
)

TIME_COL = "time_bj"
PRICE_LOOKUP_INDEX = "idx_us_market_prices_ticker_time_price"
DEFAULT_REVEAL_STEP = 1
EXIT_RULES_CONFIG_PATH = Path(__file__).resolve().parent / "program_factory" / "pair_exit_rules.json"
DEFAULT_STOP_LOSS_RULE = "trailing(-0.5*sigma)"
SIGMA_EXPR_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)?\*?sigma$")
Z_TARGET_PATTERN = re.compile(r"^z\(([^)]+)\)$", re.IGNORECASE)
TIME_TARGET_PATTERN = re.compile(r"^NY\(([^)]+)\)$", re.IGNORECASE)


st.set_page_config(
    page_title="Pairs Backtest Board",
    layout="wide",
)

_DRAGGABLE_CHART = components.declare_component(
    "draggable_train_chart",
    path=str(Path(__file__).resolve().parent / "components" / "draggable_train_chart"),
)


def render_draggable_chart(figure: go.Figure, end_idx: int, height: int = 560, key: str = "draggable_train_chart"):
    return _DRAGGABLE_CHART(
        figure=figure.to_dict(),
        end_idx=int(end_idx),
        height=int(height),
        train_shape_index=0,
        split_shape_index=1,
        key=key,
        default=None,
    )


@st.cache_data(ttl=60)
def load_pairs_from_metapairs() -> Dict[str, Tuple[str, str, str]]:
    try:
        conn = pymysql.connect(**DB_CONFIG)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT pair_name, ticker_a, ticker_b
            FROM metapairs
            WHERE COALESCE(is_active, 1) = 1
            """
        )
        rows = cursor.fetchall()
        conn.close()
    except Exception:
        return {}

    pairs: Dict[str, Tuple[str, str, str]] = {}
    for pair_key, ticker_a, ticker_b in rows:
        if not pair_key or not ticker_a or not ticker_b:
            continue

        pair_name = f"{str(ticker_a).strip().upper()} / {str(ticker_b).strip().upper()}"
        pair_key = str(pair_key).strip().lower()
        pairs[pair_name] = (
            pair_key,
            f"{str(ticker_a).strip().upper()}_price",
            f"{str(ticker_b).strip().upper()}_price",
        )
    return dict(sorted(pairs.items()))


@st.cache_data(ttl=30)
def load_pair_data(ticker_y: str, ticker_x: str) -> pd.DataFrame:
    conn = pymysql.connect(**DB_CONFIG)
    try:
        query = f"""
            SELECT
                a.time_utc,
                a.price AS `{ticker_y}_price`,
                b.price AS `{ticker_x}_price`
            FROM us_market_prices a FORCE INDEX ({PRICE_LOOKUP_INDEX})
            JOIN us_market_prices b FORCE INDEX ({PRICE_LOOKUP_INDEX})
                ON b.ticker = %s
               AND b.time_utc = a.time_utc
            WHERE a.ticker = %s
            ORDER BY a.time_utc
        """
        df = pd.read_sql(query, conn, params=(ticker_x, ticker_y))
    finally:
        conn.close()

    if "time_utc" not in df.columns:
        raise ValueError("us_market_prices query missing time_utc")

    df["time_utc"] = pd.to_datetime(df["time_utc"])
    df[TIME_COL] = df["time_utc"] + pd.Timedelta(hours=8)
    df = df.sort_values("time_utc").reset_index(drop=True)
    return df


def pair_name_candidates(pair_key: str, display_name: str = "") -> List[str]:
    pair_key = str(pair_key or "").strip().lower()
    display_name = str(display_name or "").strip().lower()
    candidates: List[str] = []
    for value in (pair_key, display_name):
        normalized = value.replace(" ", "")
        slash_as_key = normalized.replace("/", "_")
        for candidate in (value, normalized, slash_as_key):
            candidate = candidate.strip().lower()
            if candidate and candidate not in candidates:
                candidates.append(candidate)
    return candidates


@st.cache_data(ttl=30)
def load_pair_config(pair_key: str, display_name: str = "") -> Dict[str, Any]:
    candidates = pair_name_candidates(pair_key, display_name)
    if not candidates:
        return {}

    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        placeholders = ",".join(["%s"] * len(candidates))
        cursor.execute(
            f"""
            SELECT pair_name, train_start_bj, test_end_bj, fixed_beta
            FROM pair_config
            WHERE data_source = '5m'
              AND (
                   LOWER(pair_name) IN ({placeholders})
               OR LOWER(REPLACE(REPLACE(pair_name, ' ', ''), '/', '_')) IN ({placeholders})
              )
            LIMIT 1
            """,
            tuple(candidates + candidates),
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    if not row:
        return {}
    row = dict(row)
    # This legacy backtest board still uses its own train/test regression. The
    # production fixed-beta config supplies only its default time range here.
    row["train_time"] = row.get("train_start_bj")
    row["split_time"] = row.get("test_end_bj")
    return row


def save_kelly_portfolio_asset(asset: Dict[str, Any]) -> None:
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS kelly_portfolio_assets (
                asset_id VARCHAR(255) PRIMARY KEY,
                pair_key VARCHAR(128) NOT NULL,
                display_name VARCHAR(255) NOT NULL,
                strategy_rule VARCHAR(64) NOT NULL,
                scope VARCHAR(64) NOT NULL,
                train_start DATETIME NULL,
                split_time DATETIME NULL,
                end_time DATETIME NULL,
                beta DOUBLE NULL,
                initial_equity DOUBLE NOT NULL,
                final_equity DOUBLE NOT NULL,
                annualized_sharpe DOUBLE NULL,
                max_drawdown_pct DOUBLE NULL,
                daily_returns_json LONGTEXT NOT NULL,
                bar_returns_json LONGTEXT NULL,
                scope_returns_json LONGTEXT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
            """
        )
        try:
            cursor.execute(
                "ALTER TABLE kelly_portfolio_assets "
                "ADD COLUMN bar_returns_json LONGTEXT NULL"
            )
        except pymysql.err.OperationalError as exc:
            # MySQL raises duplicate-column error after the one-time migration.
            if exc.args and int(exc.args[0]) != 1060:
                raise
        try:
            cursor.execute(
                "ALTER TABLE kelly_portfolio_assets "
                "ADD COLUMN scope_returns_json LONGTEXT NULL"
            )
        except pymysql.err.OperationalError as exc:
            if exc.args and int(exc.args[0]) != 1060:
                raise
        cursor.execute(
            """
            INSERT INTO kelly_portfolio_assets (
                asset_id, pair_key, display_name, strategy_rule, scope,
                train_start, split_time, end_time, beta,
                initial_equity, final_equity, annualized_sharpe,
                max_drawdown_pct, daily_returns_json, bar_returns_json, scope_returns_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                display_name = VALUES(display_name),
                train_start = VALUES(train_start),
                split_time = VALUES(split_time),
                end_time = VALUES(end_time),
                beta = VALUES(beta),
                initial_equity = VALUES(initial_equity),
                final_equity = VALUES(final_equity),
                annualized_sharpe = VALUES(annualized_sharpe),
                max_drawdown_pct = VALUES(max_drawdown_pct),
                daily_returns_json = VALUES(daily_returns_json),
                bar_returns_json = VALUES(bar_returns_json),
                scope_returns_json = VALUES(scope_returns_json)
            """,
            (
                asset["asset_id"],
                asset["pair_key"],
                asset["display_name"],
                asset["strategy_rule"],
                asset["scope"],
                asset["train_start"],
                asset["split_time"],
                asset["end_time"],
                asset["beta"],
                asset["initial_equity"],
                asset["final_equity"],
                asset["annualized_sharpe"],
                asset["max_drawdown_pct"],
                json.dumps(asset["daily_returns"], ensure_ascii=False, allow_nan=False),
                json.dumps(asset.get("bar_returns") or {}, ensure_ascii=False, allow_nan=False),
                json.dumps(asset["scope_returns"], ensure_ascii=False, allow_nan=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def load_kelly_portfolio_assets() -> List[Dict[str, Any]]:
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        try:
            cursor.execute(
                """
                SELECT asset_id, pair_key, display_name, strategy_rule, scope,
                       annualized_sharpe, max_drawdown_pct, daily_returns_json, bar_returns_json,
                       scope_returns_json, updated_at
                FROM kelly_portfolio_assets
                ORDER BY display_name, strategy_rule, scope
                """,
            )
        except pymysql.err.OperationalError as exc:
            # Assets saved before bar-path support remain usable at daily precision.
            if not exc.args or int(exc.args[0]) != 1054:
                raise
            cursor.execute(
                """
                SELECT asset_id, pair_key, display_name, strategy_rule, scope,
                       annualized_sharpe, max_drawdown_pct, daily_returns_json, updated_at
                FROM kelly_portfolio_assets
                ORDER BY display_name, strategy_rule, scope
                """,
            )
        rows = cursor.fetchall()
    except Exception:
        return []
    finally:
        conn.close()

    assets_by_identity: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        try:
            daily_returns = json.loads(row.get("daily_returns_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(daily_returns, dict) or not daily_returns:
            continue
        daily_returns = {
            str(day): float(value)
            for day, value in daily_returns.items()
            if value is not None and np.isfinite(float(value))
        }
        try:
            bar_returns = json.loads(row.get("bar_returns_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            bar_returns = {}
        bar_returns = {
            str(timestamp): float(value)
            for timestamp, value in bar_returns.items()
            if value is not None and np.isfinite(float(value))
        }
        try:
            scope_returns = json.loads(row.get("scope_returns_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            scope_returns = {}
        identity = (str(row["pair_key"]), str(row["strategy_rule"]))
        asset = assets_by_identity.setdefault(identity, {
            "asset_id": f"pair:{identity[0]}:{identity[1]}",
            "pair_key": identity[0], "display_name": row["display_name"],
            "strategy_rule": identity[1], "scope_returns": {},
        })
        if scope_returns:
            asset["scope_returns"].update(scope_returns)
        elif daily_returns:
            # Do not let a stale legacy row overwrite a bundled asset path.
            legacy_scope = str(row.get("scope") or "test")
            asset["scope_returns"].setdefault(
                legacy_scope,
                {"daily": daily_returns, "bar": bar_returns},
            )
    return [asset for asset in assets_by_identity.values() if asset["scope_returns"]]


def build_kelly_portfolio(
    assets: List[Dict[str, Any]],
    initial_equity: float = 10_000.0,
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    return_series = {}
    labels = {}
    for asset in assets:
        asset_id = str(asset["asset_id"])
        labels[asset_id] = (
            f"{asset['display_name']} | {asset['strategy_rule']} | {asset.get('scope', 'test')}"
        )
        return_series[asset_id] = pd.Series(
            asset["daily_returns"], dtype=float, name=asset_id
        )
    returns = pd.DataFrame(return_series).sort_index().fillna(0.0)
    if returns.empty:
        raise ValueError("凯利资产组合没有可用的逐日收益。")

    mean_returns = returns.mean().to_numpy(dtype=float)
    covariance = returns.cov().to_numpy(dtype=float, copy=True)
    covariance = covariance + np.eye(len(assets)) * 1e-10
    raw_kelly = np.linalg.pinv(covariance) @ mean_returns
    long_only_kelly = np.clip(raw_kelly, 0.0, None)
    if not np.isfinite(long_only_kelly).all() or float(long_only_kelly.sum()) <= 0:
        weights = np.repeat(1.0 / len(assets), len(assets))
    else:
        weights = long_only_kelly / long_only_kelly.sum()

    daily_portfolio_returns = returns.to_numpy(dtype=float) @ weights

    # Use every saved 5-minute bar for the equity path; daily data remains the
    # stable sampling frequency used to derive Kelly weights and annualized Sharpe.
    bar_series = {
        str(asset["asset_id"]): pd.Series(
            asset.get("bar_returns") or asset["daily_returns"], dtype=float
        )
        for asset in assets
    }
    bar_returns = pd.DataFrame(bar_series).sort_index().fillna(0.0)
    portfolio_returns = bar_returns.to_numpy(dtype=float) @ weights
    equity = float(initial_equity) * np.cumprod(1.0 + portfolio_returns)
    portfolio_df = pd.DataFrame(
        {
            "time": bar_returns.index.astype(str),
            "bar_return": portfolio_returns,
            "equity": equity,
        }
    )
    # Older Kelly assets only contain daily keys (YYYY-MM-DD), while newly
    # saved assets use 5-minute timestamps.  Parse both in the same portfolio.
    try:
        portfolio_df["time_dt"] = pd.to_datetime(portfolio_df["time"], format="mixed")
    except (TypeError, ValueError):
        portfolio_df["time_dt"] = portfolio_df["time"].map(
            lambda value: pd.to_datetime(value, errors="coerce")
        )
    if portfolio_df["time_dt"].isna().any():
        bad_times = portfolio_df.loc[portfolio_df["time_dt"].isna(), "time"].head(3).tolist()
        raise ValueError(f"凯利资产包含无法解析的时间键：{bad_times}")
    portfolio_df["plot_x"] = np.arange(len(portfolio_df))
    portfolio_df["trade_date"] = portfolio_df["time_dt"].dt.strftime("%Y-%m-%d")
    daily_std = float(np.std(daily_portfolio_returns, ddof=1)) if len(daily_portfolio_returns) > 1 else math.nan
    sharpe = (
        math.nan
        if not np.isfinite(daily_std) or daily_std <= 0
        else float(math.sqrt(252.0) * np.mean(daily_portfolio_returns) / daily_std)
    )
    running_peak = portfolio_df["equity"].cummax()
    drawdown = portfolio_df["equity"] / running_peak - 1.0
    trough_pos = int(drawdown.to_numpy().argmin())
    peak_pos = int(portfolio_df["equity"].iloc[: trough_pos + 1].to_numpy().argmax())
    weights_df = pd.DataFrame(
        {
            "Asset": [labels[str(asset["asset_id"])] for asset in assets],
            "Kelly Score": raw_kelly,
            "Portfolio Weight %": weights * 100.0,
        }
    )
    stats = {
        "initial_equity": float(initial_equity),
        "final_equity": float(portfolio_df["equity"].iloc[-1]),
        "total_return_pct": float((portfolio_df["equity"].iloc[-1] / initial_equity - 1.0) * 100.0),
        "sharpe": sharpe,
        "max_drawdown_pct": float(drawdown.iloc[trough_pos] * 100.0),
        "drawdown_peak_time": str(portfolio_df.iloc[peak_pos]["time"]),
        "drawdown_trough_time": str(portfolio_df.iloc[trough_pos]["time"]),
    }
    return portfolio_df, weights_df, stats


def build_kelly_equity_chart(portfolio_df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=portfolio_df["plot_x"],
            y=portfolio_df["equity"],
            mode="lines",
            line=dict(color="#7c3aed", width=2.5),
            fill="tozeroy",
            fillcolor="rgba(124, 58, 237, 0.10)",
            customdata=portfolio_df["time"],
            hovertemplate="%{customdata}<br>portfolio equity=$%{y:,.2f}<extra></extra>",
        )
    )
    fig.add_hline(y=10_000.0, line_dash="dash", line_color="#6b7280")
    fig.update_layout(
        title="$10,000 Kelly / Maximum-Sharpe Portfolio Net Equity",
        height=400,
        margin=dict(l=20, r=20, t=45, b=20),
        xaxis_title="Trading Days",
        yaxis_title="Portfolio Net Equity (USD, log scale)",
        showlegend=False,
    )
    day_ticks = portfolio_df.groupby("trade_date", sort=False)["plot_x"].first().reset_index()
    if len(day_ticks) > 15:
        tick_step = int(math.ceil(len(day_ticks) / 15))
        day_ticks = day_ticks.iloc[::tick_step]
    fig.update_xaxes(
        tickmode="array",
        tickvals=day_ticks["plot_x"].tolist(),
        ticktext=day_ticks["trade_date"].tolist(),
        tickangle=-35,
    )
    fig.update_yaxes(type="log")
    return fig


def nearest_time_index(df: pd.DataFrame, t) -> Optional[int]:
    if t is None or pd.isna(t) or df.empty:
        return None
    ts = pd.to_datetime(t)
    idx = (pd.to_datetime(df[TIME_COL]) - ts).abs().idxmin()
    return int(idx)


def add_train_relative_trading_days(
    frame: pd.DataFrame,
    train_start: int,
) -> pd.DataFrame:
    """Number US trading sessions from Train Start, whose session is zero."""

    result = frame.copy()
    timestamps = pd.to_datetime(result[TIME_COL])
    session_dates = timestamps.dt.normalize()
    # US regular trading hours cross midnight in Beijing. Bars before noon
    # belong to the US session that started on the previous Beijing date.
    session_dates = session_dates.where(
        timestamps.dt.hour >= 12,
        session_dates - pd.Timedelta(days=1),
    )
    session_codes, _ = pd.factorize(session_dates, sort=False)

    if "global_index" in result.columns:
        matches = np.flatnonzero(
            result["global_index"].to_numpy(dtype=int) == int(train_start)
        )
        origin_pos = int(matches[0]) if len(matches) else int(train_start)
    else:
        origin_pos = int(train_start)
    origin_pos = clamp_int(origin_pos, 0, len(result) - 1)
    origin_code = int(session_codes[origin_pos])

    result["session_date"] = session_dates.dt.strftime("%Y-%m-%d")
    # The session containing Train Start is day 0; later sessions are 1, 2, …
    result["trading_day_number"] = session_codes - origin_code
    return result


def train_relative_day_ticks(
    plot_df: pd.DataFrame,
    max_ticks: int = 15,
) -> pd.DataFrame:
    """Return sparse zero-based Train Start trading-day ticks for a bar chart."""

    if plot_df.empty or "trading_day_number" not in plot_df.columns:
        return pd.DataFrame(columns=["plot_x", "tick_text"])
    eligible = plot_df[plot_df["trading_day_number"] >= 0]
    if eligible.empty:
        return pd.DataFrame(columns=["plot_x", "tick_text"])
    ticks = (
        eligible.groupby("trading_day_number", sort=False)["plot_x"]
        .first()
        .reset_index()
    )
    if len(ticks) > max_ticks:
        step = int(math.ceil(len(ticks) / max_ticks))
        selected = list(range(0, len(ticks), step))
        if selected[-1] != len(ticks) - 1:
            selected.append(len(ticks) - 1)
        ticks = ticks.iloc[selected]
    ticks["tick_text"] = ticks["trading_day_number"].map(
        lambda value: f"{int(value)} day"
    )
    return ticks


def shared_backtest_xaxis(
    plot_df: pd.DataFrame,
    start_idx: int,
    end_idx: int,
    max_ticks: int = 15,
) -> Dict[str, Any]:
    """Build the identical, independently drawn x-axis for both backtest charts."""

    day_ticks = train_relative_day_ticks(plot_df, max_ticks=max_ticks)
    return {
        "domain": [0.0, 1.0],
        "range": [float(start_idx) - 0.5, float(end_idx) + 0.5],
        "tickmode": "array",
        "tickvals": day_ticks["plot_x"].tolist(),
        "ticktext": day_ticks["tick_text"].tolist(),
        "tickangle": -35,
    }


def safe_adf(series: pd.Series) -> Tuple[Optional[float], Optional[float]]:
    values = pd.Series(series).dropna()
    if len(values) < 20 or float(values.std()) == 0.0:
        return None, None

    try:
        result = adfuller(values)
        return float(result[0]), float(result[1])
    except Exception:
        return None, None


def clamp_int(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def get_row_by_abs_idx(frame: pd.DataFrame, abs_idx: int) -> pd.Series:
    return frame.iloc[int(abs_idx)]


@st.cache_data(ttl=5)
def load_exit_rules_config() -> Dict[str, Dict[str, Any]]:
    if not EXIT_RULES_CONFIG_PATH.exists():
        return {"entry_rule": {}, "stop_loss": {}, "take_profit": {}}

    try:
        payload = json.loads(EXIT_RULES_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"entry_rule": {}, "stop_loss": {}, "take_profit": {}}

    if not isinstance(payload, dict):
        return {"entry_rule": {}, "stop_loss": {}, "take_profit": {}}

    def normalize_rule_map(raw_rules: Any) -> Dict[str, Any]:
        if not isinstance(raw_rules, dict):
            return {}

        normalized: Dict[str, Any] = {}
        for raw_pair_name, raw_rule in raw_rules.items():
            pair_name = str(raw_pair_name).strip().lower()
            if not pair_name or raw_rule is None:
                continue
            if isinstance(raw_rule, bool):
                normalized[pair_name] = int(raw_rule)
            elif isinstance(raw_rule, (int, float)):
                normalized[pair_name] = float(raw_rule)
            else:
                rule_text = str(raw_rule).strip()
                if rule_text:
                    normalized[pair_name] = rule_text
        return normalized

    return {
        "entry_rule": normalize_rule_map(payload.get("entry_rule", {})),
        "stop_loss": normalize_rule_map(payload.get("stop_loss", {})),
        "take_profit": normalize_rule_map(payload.get("take_profit", {})),
    }


def load_entry_rules() -> Dict[str, Any]:
    return dict(load_exit_rules_config().get("entry_rule", {}))


def load_stop_loss_rules() -> Dict[str, Any]:
    return dict(load_exit_rules_config().get("stop_loss", {}))


def load_take_profit_rules() -> Dict[str, Any]:
    return dict(load_exit_rules_config().get("take_profit", {}))


def eval_stop_expr(expr: Any, sigma_value: Optional[float]) -> Optional[float]:
    if isinstance(expr, (int, float)):
        return float(expr)

    text = str(expr).strip()
    try:
        return float(text)
    except (TypeError, ValueError):
        pass

    compact = text.lower().replace(" ", "")
    if compact in ("sigma", "+sigma"):
        return None if sigma_value is None else float(sigma_value)
    if compact == "-sigma":
        return None if sigma_value is None else -float(sigma_value)
    if not SIGMA_EXPR_PATTERN.match(compact):
        return None
    if sigma_value is None:
        return None

    coeff = compact.replace("*sigma", "").replace("sigma", "")
    if coeff in ("", "+"):
        n = 1.0
    elif coeff == "-":
        n = -1.0
    else:
        n = float(coeff)
    return n * float(sigma_value)


def parse_hhmm(value: Any) -> Optional[Tuple[int, int]]:
    match = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", str(value or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def to_ny_timestamp(value) -> Optional[pd.Timestamp]:
    if value is None or pd.isna(value):
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("Asia/Shanghai")
    else:
        ts = ts.tz_convert("Asia/Shanghai")
    return ts.tz_convert("US/Eastern")


def default_trailing_threshold(max_ret: float, sigma_value: Optional[float]) -> float:
    if sigma_value is not None:
        return float(max_ret) - (0.5 * float(sigma_value))
    return float(max_ret) - 1.0


def resolve_game_stop_loss(pair_key: str, max_ret: float, sigma_value: Optional[float]) -> Dict[str, Any]:
    raw_rule = load_stop_loss_rules().get(str(pair_key).strip().lower(), DEFAULT_STOP_LOSS_RULE)
    fallback_rule = "default: trailing(-0.5*sigma)" if sigma_value is not None else "default: trailing(-1.0)"

    if isinstance(raw_rule, (int, float)):
        return {
            "mode": "fixed",
            "threshold": float(raw_rule),
            "exit_reason": "STOP_LOSS",
            "rule_text": str(raw_rule),
        }

    rule_text = str(raw_rule).strip()
    lower = rule_text.lower()
    if lower.startswith("trailing(") and lower.endswith(")"):
        inner = rule_text[rule_text.find("(") + 1 : -1]
        offset = eval_stop_expr(inner, sigma_value)
        if offset is None:
            return {
                "mode": "trailing",
                "threshold": default_trailing_threshold(max_ret, sigma_value),
                "exit_reason": "TRAILING_STOP",
                "rule_text": f"{rule_text} ({fallback_rule})",
            }
        return {
            "mode": "trailing",
            "threshold": float(max_ret) + float(offset),
            "exit_reason": "TRAILING_STOP",
            "rule_text": rule_text,
        }

    threshold = eval_stop_expr(rule_text, sigma_value)
    if threshold is None:
        return {
            "mode": "trailing",
            "threshold": default_trailing_threshold(max_ret, sigma_value),
            "exit_reason": "TRAILING_STOP",
            "rule_text": f"{rule_text} ({fallback_rule})",
        }
    return {
        "mode": "fixed",
        "threshold": float(threshold),
        "exit_reason": "STOP_LOSS",
        "rule_text": rule_text,
    }


def resolve_game_take_profit(
    pair_key: str,
    max_ret: float,
    sigma_value: Optional[float],
    z_now: Optional[float],
    direction: Optional[int],
    entry_z: Optional[float] = None,
    ret: Optional[float] = None,
    current_time=None,
) -> Optional[Dict[str, Any]]:
    raw_rule = load_take_profit_rules().get(str(pair_key).strip().lower())
    if raw_rule is None:
        return None

    if isinstance(raw_rule, (int, float)):
        return {
            "mode": "fixed",
            "threshold": float(raw_rule),
            "metric": "return_pct",
            "armed": True,
            "comparator": ">=",
            "exit_reason": "TAKE_PROFIT",
            "rule_text": str(raw_rule),
        }

    rule_text = str(raw_rule).strip()
    lower = rule_text.lower()

    time_match = TIME_TARGET_PATTERN.match(lower.replace(" ", ""))
    if time_match:
        parsed = parse_hhmm(time_match.group(1))
        if parsed is None:
            return None
        hour, minute = parsed
        current_ny = to_ny_timestamp(current_time)
        target_reached = False
        if current_ny is not None:
            target_ny = current_ny.replace(hour=hour, minute=minute, second=0, microsecond=0)
            target_reached = current_ny >= target_ny
        return {
            "mode": "time",
            "threshold": f"{hour:02d}:{minute:02d}",
            "metric": "time_ny",
            "armed": bool(ret is not None and float(ret) > 0 and target_reached),
            "comparator": ">=",
            "exit_reason": "TAKE_PROFIT_TIME",
            "rule_text": rule_text,
        }

    if lower.startswith("trailing(") and lower.endswith(")"):
        inner = rule_text[rule_text.find("(") + 1 : -1]
        offset = eval_stop_expr(inner, sigma_value)
        if offset is None:
            offset = -0.5 * float(sigma_value) if sigma_value is not None else -1.0
            rule_text = (
                f"{rule_text} (fallback: trailing(-0.5*sigma))"
                if sigma_value is not None
                else f"{rule_text} (fallback: trailing(-1.0))"
            )

        armed = max_ret is not None and max_ret > 0
        threshold = (float(max_ret) + float(offset)) if armed else None
        return {
            "mode": "trailing",
            "threshold": threshold,
            "metric": "return_pct",
            "armed": armed,
            "comparator": "<=",
            "exit_reason": "TAKE_PROFIT",
            "rule_text": rule_text,
        }

    z_match = Z_TARGET_PATTERN.match(lower.replace(" ", ""))
    if z_match:
        try:
            z_target = abs(float(z_match.group(1)))
        except (TypeError, ValueError):
            return None

        threshold = None
        comparator = None
        armed = False
        if direction == 1:
            threshold = z_target
            comparator = ">="
            armed = threshold is not None and z_now is not None and entry_z is not None and float(entry_z) < float(threshold)
        elif direction == -1:
            threshold = -z_target
            comparator = "<="
            armed = threshold is not None and z_now is not None and entry_z is not None and float(entry_z) > float(threshold)

        return {
            "mode": "z_target",
            "threshold": threshold,
            "metric": "z_now",
            "armed": armed,
            "comparator": comparator,
            "exit_reason": "TAKE_PROFIT",
            "rule_text": rule_text,
        }

    threshold = eval_stop_expr(rule_text, sigma_value)
    if threshold is None:
        return None

    return {
        "mode": "fixed",
        "threshold": float(threshold),
        "metric": "return_pct",
        "armed": True,
        "comparator": ">=",
        "exit_reason": "TAKE_PROFIT",
        "rule_text": rule_text,
    }


def fmt_signed_pct(value: Optional[float]) -> str:
    if value is None:
        return "-"
    try:
        if not np.isfinite(float(value)):
            return "-"
    except (TypeError, ValueError):
        return "-"
    return f"{float(value):+.2f}%"


def fmt_time_value(value) -> str:
    if value is None or pd.isna(value):
        return "-"
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")


def ny_trade_date(value) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("Asia/Shanghai")
    else:
        ts = ts.tz_convert("Asia/Shanghai")
    return ts.tz_convert("US/Eastern").strftime("%Y-%m-%d")


def rule_source_text(pair_key: str, kind: str) -> str:
    pair_key = str(pair_key).strip().lower()
    if kind == "stop_loss":
        rules = load_stop_loss_rules()
        raw_rule = rules.get(pair_key, DEFAULT_STOP_LOSS_RULE)
        source = "pair_exit_rules.json" if pair_key in rules else "default"
    else:
        rules = load_take_profit_rules()
        raw_rule = rules.get(pair_key)
        source = "pair_exit_rules.json" if pair_key in rules else "none"
    return f"{raw_rule}" if raw_rule is not None else f"{source}: 未配置"


def describe_stop_loss_rule(pair_key: str, sigma_for_stop_pct: Optional[float]) -> str:
    cfg = resolve_game_stop_loss(
        pair_key=pair_key,
        max_ret=0.0,
        sigma_value=sigma_for_stop_pct,
    )
    mode = str(cfg.get("mode", "-"))
    threshold = cfg.get("threshold")
    if mode == "trailing":
        return (
            f"Stop Loss: `{cfg.get('rule_text')}` -> trailing stop；"
            f"实时线 = peak_return + offset，当前入场初始线约 `{fmt_signed_pct(threshold)}`，"
            f"触发条件为 current_return <= stop_line。"
        )
    return (
        f"Stop Loss: `{cfg.get('rule_text')}` -> fixed stop；"
        f"触发线 `{fmt_signed_pct(threshold)}`，触发条件为 current_return <= stop_line。"
    )


def describe_take_profit_rule(
    pair_key: str,
    sigma_for_stop_pct: Optional[float],
    sample_z: Optional[float],
) -> str:
    raw_rule = load_take_profit_rules().get(str(pair_key).strip().lower())
    if raw_rule is None:
        return "Take Profit: 未配置；不会自动止盈，只靠 stop loss / END_OF_BACKTEST / 手动出场。"

    long_cfg = resolve_game_take_profit(
        pair_key=pair_key,
        max_ret=0.0,
        sigma_value=sigma_for_stop_pct,
        z_now=sample_z,
        direction=1,
        entry_z=sample_z,
    )
    short_cfg = resolve_game_take_profit(
        pair_key=pair_key,
        max_ret=0.0,
        sigma_value=sigma_for_stop_pct,
        z_now=sample_z,
        direction=-1,
        entry_z=sample_z,
    )

    cfg = long_cfg or short_cfg
    if cfg is None:
        return f"Take Profit: `{raw_rule}` 当前无法解析；实盘/回测不会使用这条止盈。"

    mode = str(cfg.get("mode", "-"))
    if mode == "z_target":
        return (
            f"Take Profit: `{cfg.get('rule_text')}` -> z 目标止盈；"
            f"long spread 触发方向为 z >= +target，short spread 触发方向为 z <= -target。"
        )
    if mode == "trailing":
        return (
            f"Take Profit: `{cfg.get('rule_text')}` -> 盈利回撤止盈；"
            f"只有 peak_return > 0 后 armed，触发条件为 current_return <= peak_return + offset。"
        )
    if mode == "time":
        return (
            f"Take Profit: `{cfg.get('rule_text')}` -> 纽约时间止盈；"
            f"当 bar 的纽约时间 >= `{cfg.get('threshold')}` 且 current_return > 0 时触发。"
        )
    return (
        f"Take Profit: `{cfg.get('rule_text')}` -> fixed take profit；"
        f"触发条件为 current_return >= `{fmt_signed_pct(cfg.get('threshold'))}`。"
    )


def build_analysis_frame(
    df: pd.DataFrame,
    y_col: str,
    x_col: str,
    train_start: int,
    split_idx: int,
    end_idx: int,
) -> Tuple[pd.DataFrame, Dict[str, float]]:
    frame = df.iloc[: end_idx + 1].copy().reset_index(drop=True)
    split_local = split_idx - train_start
    if split_local <= 1:
        raise ValueError("split 必须在 train start 之后")

    train_seg = frame.iloc[train_start:split_idx].copy()
    x = train_seg[x_col].astype(float).to_numpy()
    y = train_seg[y_col].astype(float).to_numpy()

    beta, alpha = np.polyfit(x, y, 1)
    frame["spread"] = frame[y_col] - (beta * frame[x_col] + alpha)
    train_spread = frame.iloc[train_start:split_idx]["spread"]
    mu = float(train_spread.mean())
    sigma = float(train_spread.std())

    if not np.isfinite(sigma) or sigma <= 0:
        raise ValueError("训练段 sigma 无效，无法构建 z-score")

    frame["zscore"] = (frame["spread"] - mu) / sigma
    frame["global_index"] = np.arange(0, end_idx + 1)
    frame["time_str"] = frame[TIME_COL].dt.strftime("%Y-%m-%d %H:%M")

    adf_stat, adf_p = safe_adf(train_spread)
    stats = {
        "beta": float(beta),
        "alpha": float(alpha),
        "mu": mu,
        "sigma": sigma,
        "adf_stat": adf_stat if adf_stat is not None else math.nan,
        "adf_p": adf_p if adf_p is not None else math.nan,
    }
    return frame, stats


def apply_model_override(
    frame: pd.DataFrame,
    stats: Dict[str, float],
    y_col: str,
    x_col: str,
    train_start: int,
    split_idx: int,
    beta: Optional[float] = None,
    alpha: Optional[float] = None,
    mu_shift: float = 0.0,
    sigma_multiplier: float = 1.0,
) -> Tuple[pd.DataFrame, Dict[str, float]]:
    adjusted = frame.copy()
    adjusted_stats = dict(stats)

    beta_value = float(stats["beta"] if beta is None else beta)
    alpha_value = float(stats["alpha"] if alpha is None else alpha)
    sigma_multiplier = max(float(sigma_multiplier), 0.01)

    adjusted["spread"] = adjusted[y_col] - (beta_value * adjusted[x_col] + alpha_value)
    train_spread = adjusted.iloc[train_start:split_idx]["spread"]
    mu_base = float(train_spread.mean())
    sigma_base = float(train_spread.std())

    if not np.isfinite(sigma_base) or sigma_base <= 0:
        raise ValueError("模型微调后的训练段 sigma 无效，无法构建 z-score")

    mu_value = mu_base + float(mu_shift)
    sigma_value = sigma_base * sigma_multiplier
    adjusted["zscore"] = (adjusted["spread"] - mu_value) / sigma_value

    adf_stat, adf_p = safe_adf(train_spread)
    adjusted_stats.update(
        {
            "beta": beta_value,
            "alpha": alpha_value,
            "mu": mu_value,
            "sigma": sigma_value,
            "base_beta": float(stats["beta"]),
            "base_alpha": float(stats["alpha"]),
            "base_mu": float(stats["mu"]),
            "base_sigma": float(stats["sigma"]),
            "mu_shift": float(mu_shift),
            "sigma_multiplier": float(sigma_multiplier),
            "override_enabled": True,
            "adf_stat": adf_stat if adf_stat is not None else math.nan,
            "adf_p": adf_p if adf_p is not None else math.nan,
        }
    )
    return adjusted, adjusted_stats


def scenario_key(pair_name: str, train_start: int, split_idx: int, end_idx: int, model_key: str = "") -> str:
    return f"{pair_name}|{train_start}|{split_idx}|{end_idx}|{model_key}"


def fresh_game_state(split_idx: int, end_idx: int) -> Dict[str, Any]:
    return {
        "cursor_abs": split_idx,
        "split_abs": split_idx,
        "end_abs": end_idx,
        "open_position": None,
        "trades": [],
        "realized_return_pct": 0.0,
    }


def ensure_game_state(key: str, split_idx: int, end_idx: int) -> Dict[str, Any]:
    if st.session_state.get("game_scenario_key") != key:
        st.session_state.game_scenario_key = key
        st.session_state.backtest_game = fresh_game_state(split_idx, end_idx)
    return st.session_state.backtest_game


def pair_leg_directions(side: str, beta: float) -> Tuple[int, int]:
    """Return (Y, X) share directions for a spread position.

    Long spread is +Y - beta*X.  Therefore a negative beta makes both legs
    long; short spread reverses both legs.
    """
    spread_direction = 1 if str(side).lower() == "long" else -1
    y_direction = spread_direction
    x_direction = -spread_direction if float(beta) > 0 else spread_direction
    return y_direction, x_direction


def position_mark(
    position: Dict[str, Any],
    row: pd.Series,
    beta: float,
    alpha: float,
    y_col: str,
    x_col: str,
) -> Dict[str, float]:
    y_now = float(row[y_col])
    x_now = float(row[x_col])
    entry_y = float(position["entry_y"])
    entry_x = float(position["entry_x"])

    spread_direction = 1.0 if position["side"] == "long" else -1.0
    pnl = spread_direction * ((y_now - entry_y) - float(beta) * (x_now - entry_x))

    entry_notional = float(position["entry_notional"])
    ret_pct = 0.0 if entry_notional == 0 else (pnl / entry_notional) * 100.0
    spread_now = y_now - (float(beta) * x_now + float(alpha))
    return {
        "return_pct": float(ret_pct),
        "spread_now": float(spread_now),
        "zscore_now": float(row["zscore"]),
    }


def close_trade(
    game: Dict[str, Any],
    position: Dict[str, Any],
    row: pd.Series,
    beta: float,
    alpha: float,
    y_col: str,
    x_col: str,
    reason: str,
) -> None:
    mark = position_mark(position, row, beta, alpha, y_col, x_col)
    trade = {
        "side": position["side"],
        "entry_time": position["entry_time"],
        "exit_time": row["time_str"],
        "entry_z": float(position["entry_z"]),
        "exit_z": float(mark["zscore_now"]),
        "entry_idx": int(position["entry_abs_idx"]),
        "exit_idx": int(row["global_index"]),
        "hold_bars": int(row["global_index"] - position["entry_abs_idx"]),
        "peak_return_pct": float(position["peak_return_pct"]),
        "return_pct": float(mark["return_pct"]),
        "exit_reason": reason,
        "entry_y_price": float(position["entry_y"]),
        "entry_x_price": float(position["entry_x"]),
        "exit_y_price": float(row[y_col]),
        "exit_x_price": float(row[x_col]),
        "beta": float(beta),
    }
    game["trades"].append(trade)
    game["realized_return_pct"] += float(mark["return_pct"])
    game["open_position"] = None


def can_exit_position(position: Dict[str, Any], current_row: pd.Series) -> bool:
    current_abs_idx = int(current_row["global_index"])
    entry_abs_idx = int(position["entry_abs_idx"])
    if current_abs_idx <= entry_abs_idx:
        return False

    entry_trade_date = str(position.get("entry_trade_date", ""))
    current_trade_date = ny_trade_date(current_row[TIME_COL])
    if not entry_trade_date:
        return current_trade_date != str(position.get("entry_time", ""))[:10]
    return current_trade_date != entry_trade_date


def sync_open_position(
    game: Dict[str, Any],
    current_row: pd.Series,
    beta: float,
    alpha: float,
    y_col: str,
    x_col: str,
) -> None:
    position = game.get("open_position")
    if not position:
        return

    mark = position_mark(position, current_row, beta, alpha, y_col, x_col)
    position["last_return_pct"] = float(mark["return_pct"])
    position["last_z"] = float(mark["zscore_now"])
    position["last_time"] = current_row["time_str"]
    max_ret = float(position["peak_return_pct"])
    if max_ret < 0:
        max_ret = 0.0
    if float(mark["return_pct"]) > max_ret:
        max_ret = max(float(mark["return_pct"]), 0.0)
    position["peak_return_pct"] = max_ret

    stop_cfg = resolve_game_stop_loss(
        pair_key=position["pair_key"],
        max_ret=max_ret,
        sigma_value=position["sigma_for_stop_pct"],
    )
    position["stop_level_pct"] = float(stop_cfg["threshold"])
    position["stop_rule_text"] = str(stop_cfg["rule_text"])
    position["stop_exit_reason"] = str(stop_cfg["exit_reason"])
    direction = 1 if position["side"] == "long" else -1 if position["side"] == "short" else 0
    take_profit_cfg = resolve_game_take_profit(
        pair_key=position["pair_key"],
        max_ret=max_ret,
        sigma_value=position["sigma_for_stop_pct"],
        z_now=float(mark["zscore_now"]),
        direction=direction,
        entry_z=float(position["entry_z"]),
        ret=float(mark["return_pct"]),
        current_time=current_row[TIME_COL],
    )
    position["take_profit_rule_text"] = None if take_profit_cfg is None else str(take_profit_cfg["rule_text"])
    position["take_profit_mode"] = None if take_profit_cfg is None else str(take_profit_cfg["mode"])
    position["take_profit_metric"] = None if take_profit_cfg is None else str(take_profit_cfg["metric"])
    position["take_profit_level"] = None if take_profit_cfg is None else take_profit_cfg.get("threshold")
    can_exit_this_bar = can_exit_position(position, current_row)

    if (
        can_exit_this_bar
        and float(mark["return_pct"]) <= float(position["stop_level_pct"])
    ):
        close_trade(
            game=game,
            position=position,
            row=current_row,
            beta=beta,
            alpha=alpha,
            y_col=y_col,
            x_col=x_col,
            reason=position.get("stop_exit_reason", "TRAILING_STOP"),
        )
        return

    if can_exit_this_bar and take_profit_cfg and take_profit_cfg.get("armed"):
        metric = take_profit_cfg.get("metric")
        threshold = take_profit_cfg.get("threshold")
        comparator = take_profit_cfg.get("comparator")
        should_exit = False

        if metric == "time_ny" and threshold is not None:
            should_exit = True
        elif metric == "return_pct" and threshold is not None:
            current_value = float(mark["return_pct"])
            if comparator == ">=" and current_value >= float(threshold):
                should_exit = True
            elif comparator == "<=" and current_value <= float(threshold):
                should_exit = True
        elif metric == "z_now" and threshold is not None:
            current_value = float(mark["zscore_now"])
            if comparator == ">=" and current_value >= float(threshold):
                should_exit = True
            elif comparator == "<=" and current_value <= float(threshold):
                should_exit = True

        if should_exit:
            close_trade(
                game=game,
                position=position,
                row=current_row,
                beta=beta,
                alpha=alpha,
                y_col=y_col,
                x_col=x_col,
                reason=str(take_profit_cfg.get("exit_reason", "TAKE_PROFIT")),
            )
            return

    if int(current_row["global_index"]) >= int(game["end_abs"]):
        close_trade(
            game=game,
            position=position,
            row=current_row,
            beta=beta,
            alpha=alpha,
            y_col=y_col,
            x_col=x_col,
            reason="END_OF_BACKTEST",
        )


def open_trade(
    game: Dict[str, Any],
    pair_key: str,
    side: str,
    row: pd.Series,
    beta: float,
    sigma: float,
    y_col: str,
    x_col: str,
) -> None:
    beta_abs = abs(float(beta))
    entry_notional = float(row[y_col]) + beta_abs * float(row[x_col])
    two_sigma_pct = 0.0 if entry_notional == 0 else ((2.0 * float(sigma)) / entry_notional) * 100.0
    sigma_for_stop_pct = two_sigma_pct / 2.0
    initial_stop_cfg = resolve_game_stop_loss(
        pair_key=pair_key,
        max_ret=0.0,
        sigma_value=sigma_for_stop_pct,
    )
    game["open_position"] = {
        "pair_key": str(pair_key).strip().lower(),
        "side": side,
        "entry_abs_idx": int(row["global_index"]),
        "entry_time": row["time_str"],
        "entry_trade_date": ny_trade_date(row[TIME_COL]),
        "entry_y": float(row[y_col]),
        "entry_x": float(row[x_col]),
        "entry_z": float(row["zscore"]),
        "entry_notional": entry_notional,
        "entry_two_sigma_pct": float(two_sigma_pct),
        "sigma_for_stop_pct": float(sigma_for_stop_pct),
        "peak_return_pct": 0.0,
        "stop_level_pct": float(initial_stop_cfg["threshold"]),
        "stop_rule_text": str(initial_stop_cfg["rule_text"]),
        "stop_exit_reason": str(initial_stop_cfg["exit_reason"]),
        "last_return_pct": 0.0,
        "last_z": float(row["zscore"]),
        "last_time": row["time_str"],
    }


def advance_game_cursor(
    game: Dict[str, Any],
    frame: pd.DataFrame,
    target_cursor_abs: int,
    beta: float,
    alpha: float,
    y_col: str,
    x_col: str,
) -> None:
    start_cursor = int(game["cursor_abs"])
    started_with_position = game.get("open_position") is not None
    target_cursor_abs = clamp_int(target_cursor_abs, start_cursor, int(game["end_abs"]))
    if target_cursor_abs == start_cursor:
        return

    if not started_with_position:
        game["cursor_abs"] = target_cursor_abs
        return

    for abs_idx in range(start_cursor + 1, target_cursor_abs + 1):
        game["cursor_abs"] = abs_idx
        row = get_row_by_abs_idx(frame, abs_idx)
        sync_open_position(
            game=game,
            current_row=row,
            beta=beta,
            alpha=alpha,
            y_col=y_col,
            x_col=x_col,
        )
        if started_with_position and game.get("open_position") is None and abs_idx > start_cursor:
            break


def fast_forward_to_exit(
    game: Dict[str, Any],
    frame: pd.DataFrame,
    beta: float,
    alpha: float,
    y_col: str,
    x_col: str,
) -> None:
    if not game.get("open_position"):
        return

    end_abs = int(game["end_abs"])
    for abs_idx in range(int(game["cursor_abs"]) + 1, end_abs + 1):
        game["cursor_abs"] = abs_idx
        row = get_row_by_abs_idx(frame, abs_idx)
        sync_open_position(
            game=game,
            current_row=row,
            beta=beta,
            alpha=alpha,
            y_col=y_col,
            x_col=x_col,
        )
        if game.get("open_position") is None:
            break


def fast_forward_to_z_band_reentry(
    game: Dict[str, Any],
    frame: pd.DataFrame,
    upper_band: float,
    lower_band: float,
    beta: float,
    alpha: float,
    y_col: str,
    x_col: str,
) -> None:
    upper_band = float(upper_band)
    lower_band = float(lower_band)
    end_abs = int(game["end_abs"])
    start_abs = int(game["cursor_abs"])

    if not game.get("open_position"):
        z_values = frame["zscore"].to_numpy(dtype=float)
        window = z_values[start_abs : end_abs + 1]
        if len(window) <= 1:
            return
        prev_outside = (window[:-1] >= upper_band) | (window[:-1] <= lower_band)
        curr_inside = (window[1:] < upper_band) & (window[1:] > lower_band)
        crossings = np.where(prev_outside & curr_inside)[0]
        if len(crossings) > 0:
            game["cursor_abs"] = start_abs + 1 + int(crossings[0])
        else:
            game["cursor_abs"] = end_abs
        return

    prev_row = get_row_by_abs_idx(frame, start_abs)
    prev_z = float(prev_row["zscore"])

    for abs_idx in range(start_abs + 1, end_abs + 1):
        game["cursor_abs"] = abs_idx
        row = get_row_by_abs_idx(frame, abs_idx)
        sync_open_position(
            game=game,
            current_row=row,
            beta=beta,
            alpha=alpha,
            y_col=y_col,
            x_col=x_col,
        )
        current_z = float(row["zscore"])
        prev_outside = prev_z >= upper_band or prev_z <= lower_band
        curr_inside = lower_band < current_z < upper_band
        prev_z = current_z
        if prev_outside and curr_inside:
            break


def run_auto_reentry_backtest(
    frame: pd.DataFrame,
    pair_key: str,
    split_idx: int,
    end_idx: int,
    beta: float,
    alpha: float,
    sigma: float,
    y_col: str,
    x_col: str,
    upper_band: float = 2.0,
    lower_band: float = -2.0,
    entry_rule: str = "reentry",
) -> Dict[str, Any]:
    entry_rule = str(entry_rule or "reentry").strip().lower()
    game = fresh_game_state(split_idx, end_idx)
    prev_z = float(get_row_by_abs_idx(frame, split_idx)["zscore"])

    for abs_idx in range(split_idx + 1, end_idx + 1):
        game["cursor_abs"] = abs_idx
        row = get_row_by_abs_idx(frame, abs_idx)
        current_z = float(row["zscore"])

        if game.get("open_position"):
            sync_open_position(
                game=game,
                current_row=row,
                beta=beta,
                alpha=alpha,
                y_col=y_col,
                x_col=x_col,
            )
            if entry_rule == "band_flip" and game.get("open_position"):
                position = game["open_position"]
                side = str(position.get("side", ""))
                exit_reason = None
                if side == "short" and current_z <= 0:
                    exit_reason = "Z_ZERO_CROSS_SHORT_EXIT"
                elif side == "long" and current_z >= 0:
                    exit_reason = "Z_ZERO_CROSS_LONG_EXIT"

                if exit_reason and can_exit_position(position, row):
                    close_trade(
                        game=game,
                        position=position,
                        row=row,
                        beta=beta,
                        alpha=alpha,
                        y_col=y_col,
                        x_col=x_col,
                        reason=exit_reason,
                    )

            if game.get("open_position"):
                prev_z = current_z
                continue

        if entry_rule == "band_flip":
            if current_z >= upper_band:
                open_trade(
                    game=game,
                    pair_key=pair_key,
                    side="short",
                    row=row,
                    beta=beta,
                    sigma=sigma,
                    y_col=y_col,
                    x_col=x_col,
                )
            elif current_z <= lower_band:
                open_trade(
                    game=game,
                    pair_key=pair_key,
                    side="long",
                    row=row,
                    beta=beta,
                    sigma=sigma,
                    y_col=y_col,
                    x_col=x_col,
                )
        else:
            came_back_inside = lower_band < current_z < upper_band
            if prev_z >= upper_band and came_back_inside:
                open_trade(
                    game=game,
                    pair_key=pair_key,
                    side="short",
                    row=row,
                    beta=beta,
                    sigma=sigma,
                    y_col=y_col,
                    x_col=x_col,
                )
            elif prev_z <= lower_band and came_back_inside:
                open_trade(
                    game=game,
                    pair_key=pair_key,
                    side="long",
                    row=row,
                    beta=beta,
                    sigma=sigma,
                    y_col=y_col,
                    x_col=x_col,
                )
        prev_z = current_z

    if game.get("open_position"):
        close_trade(
            game=game,
            position=game["open_position"],
            row=get_row_by_abs_idx(frame, end_idx),
            beta=beta,
            alpha=alpha,
            y_col=y_col,
            x_col=x_col,
            reason="END_OF_BACKTEST",
        )

    trade_df = pd.DataFrame(game["trades"])
    if trade_df.empty:
        return {
            "trade_count": 0,
            "win_rate_pct": math.nan,
            "total_return_pct": 0.0,
            "avg_trade_pct": math.nan,
            "best_trade_pct": math.nan,
            "worst_trade_pct": math.nan,
            "trades": [],
        }

    return {
        "trade_count": int(len(trade_df)),
        "win_rate_pct": float((trade_df["return_pct"] > 0).mean() * 100.0),
        "total_return_pct": float(trade_df["return_pct"].sum()),
        "avg_trade_pct": float(trade_df["return_pct"].mean()),
        "best_trade_pct": float(trade_df["return_pct"].max()),
        "worst_trade_pct": float(trade_df["return_pct"].min()),
        "trades": game["trades"],
    }


def add_alternating_day_background(fig: go.Figure, plot_df: pd.DataFrame) -> None:
    if plot_df.empty or TIME_COL not in plot_df.columns or "plot_x" not in plot_df.columns:
        return

    work = plot_df[[TIME_COL, "plot_x"]].copy()
    work[TIME_COL] = pd.to_datetime(work[TIME_COL])
    session_start_date = work[TIME_COL].dt.normalize()
    session_start_date = session_start_date.where(
        work[TIME_COL].dt.hour >= 12,
        session_start_date - pd.Timedelta(days=1),
    )
    work["session_date"] = session_start_date.dt.strftime("%Y-%m-%d")
    day_spans = (
        work.groupby("session_date", sort=False)
        .agg(start=("plot_x", "first"), end=("plot_x", "last"))
        .reset_index()
    )
    colors = ("rgba(255, 243, 205, 0.28)", "rgba(210, 244, 222, 0.24)")
    shapes = []
    for idx, row_data in day_spans.iterrows():
        shapes.append(
            {
                "type": "rect",
                "xref": "x",
                "yref": "paper",
                "x0": row_data["start"],
                "x1": row_data["end"],
                "y0": 0,
                "y1": 1,
                "fillcolor": colors[idx % len(colors)],
                "opacity": 1.0,
                "line": {"width": 0},
                "layer": "below",
            }
        )
    if shapes:
        existing_shapes = list(fig.layout.shapes) if fig.layout.shapes else []
        fig.update_layout(shapes=existing_shapes + shapes)


def build_auto_backtest_chart(
    frame: pd.DataFrame,
    trades: List[Dict[str, Any]],
    train_start: int,
    split_idx: int,
    end_idx: int,
) -> go.Figure:
    upper_band = 2.0
    lower_band = -2.0
    visible = frame[
        (frame["global_index"] >= int(train_start))
        & (frame["global_index"] <= int(end_idx))
    ].copy()
    visible["plot_x"] = visible["global_index"].astype(int)
    visible["trade_date"] = visible[TIME_COL].dt.strftime("%Y-%m-%d")

    fig = go.Figure()
    add_alternating_day_background(fig, visible)

    train_part = visible[visible["global_index"] < int(split_idx)]
    test_part = visible[visible["global_index"] >= int(split_idx)]
    if not train_part.empty:
        fig.add_trace(
            go.Scatter(
                x=train_part["plot_x"],
                y=train_part["zscore"],
                mode="lines",
                name="Train Z",
                line=dict(color="#2a6f97", width=2.0),
                customdata=np.column_stack(
                    (train_part["time_str"], train_part["trading_day_number"])
                ),
                hovertemplate=(
                    "%{customdata[0]}<br>Trading Day: %{customdata[1]}<br>"
                    "z=%{y:.2f}<extra></extra>"
                ),
            )
        )
    if not test_part.empty:
        fig.add_trace(
            go.Scatter(
                x=test_part["plot_x"],
                y=test_part["zscore"],
                mode="lines",
                name="Test Z",
                line=dict(color="#9ca3af", width=2.0),
                customdata=np.column_stack(
                    (test_part["time_str"], test_part["trading_day_number"])
                ),
                hovertemplate=(
                    "%{customdata[0]}<br>Trading Day: %{customdata[1]}<br>"
                    "z=%{y:.2f}<extra></extra>"
                ),
            )
        )

    z_map = dict(zip(visible["time_str"], visible["zscore"]))
    x_map = dict(zip(visible["time_str"], visible["plot_x"]))
    profit_legend_shown = False
    loss_legend_shown = False
    entry_x: List[int] = []
    entry_y: List[float] = []
    entry_text: List[str] = []
    exit_x: List[int] = []
    exit_y: List[float] = []
    exit_text: List[str] = []

    for trade in trades:
        entry_idx = int(trade.get("entry_idx", -1))
        exit_idx = int(trade.get("exit_idx", -1))
        if entry_idx < int(train_start) or exit_idx < entry_idx:
            continue
        segment = visible[
            (visible["global_index"] >= entry_idx)
            & (visible["global_index"] <= exit_idx)
        ]
        if not segment.empty:
            trade_return = float(trade.get("return_pct", 0.0))
            is_profit = trade_return > 0
            show_legend = (is_profit and not profit_legend_shown) or ((not is_profit) and not loss_legend_shown)
            profit_legend_shown = profit_legend_shown or is_profit
            loss_legend_shown = loss_legend_shown or (not is_profit)
            fig.add_trace(
                go.Scatter(
                    x=segment["plot_x"],
                    y=segment["zscore"],
                    mode="lines",
                    name="Winning Trades" if is_profit else "Losing Trades",
                    showlegend=show_legend,
                    line=dict(color="#16a34a" if is_profit else "#f59e0b", width=2.6),
                    customdata=[
                        [
                            time_str,
                            trading_day,
                            trade_return,
                            str(trade.get("exit_reason", "")),
                        ]
                        for time_str, trading_day in zip(
                            segment["time_str"], segment["trading_day_number"]
                        )
                    ],
                    hovertemplate=(
                        "%{customdata[0]}<br>Trading Day: %{customdata[1]}<br>"
                        "z=%{y:.2f}<br>trade return=%{customdata[2]:+.2f}%<br>"
                        "%{customdata[3]}<extra></extra>"
                    ),
                )
            )

        if trade.get("entry_time") in x_map:
            entry_x.append(int(x_map[trade["entry_time"]]))
            entry_y.append(float(z_map[trade["entry_time"]]))
            entry_text.append(f"{trade.get('side', '')} entry")
        if trade.get("exit_time") in x_map:
            exit_x.append(int(x_map[trade["exit_time"]]))
            exit_y.append(float(z_map[trade["exit_time"]]))
            exit_text.append(str(trade.get("exit_reason", "exit")))

    if entry_x:
        fig.add_trace(
            go.Scatter(
                x=entry_x,
                y=entry_y,
                mode="markers",
                name="Entries",
                marker=dict(color="#059669", size=9, symbol="triangle-up"),
                text=entry_text,
                hovertemplate="%{text}<br>z=%{y:.2f}<extra></extra>",
            )
        )
    if exit_x:
        fig.add_trace(
            go.Scatter(
                x=exit_x,
                y=exit_y,
                mode="markers",
                name="Exits",
                marker=dict(color="#dc2626", size=9, symbol="x"),
                text=exit_text,
                hovertemplate="%{text}<br>z=%{y:.2f}<extra></extra>",
            )
        )

    fig.add_vline(x=int(train_start), line_dash="dash", line_color="#2563eb")
    fig.add_vline(x=int(split_idx), line_dash="dash", line_color="#111827")
    fig.add_hline(y=0, line_dash="dash", line_color="#6b7280")
    fig.add_hline(y=upper_band, line_dash="dot", line_color="#ef4444")
    fig.add_hline(y=lower_band, line_dash="dot", line_color="#10b981")
    fig.update_layout(
        title="Auto Backtest Z-Score Path",
        height=460,
        margin=dict(l=90, r=30, t=80, b=20),
        xaxis_title="Trading Day Since Train Start",
        yaxis_title="Z-Score",
        # Keep the legend out of the right margin so this plot uses the same
        # horizontal drawing area as the compound-equity chart below it.
        legend=dict(
            orientation="h",
            x=0,
            xanchor="left",
            y=1.02,
            yanchor="bottom",
        ),
    )
    fig.update_xaxes(**shared_backtest_xaxis(visible, train_start, end_idx))
    fig.update_yaxes(automargin=False)
    return fig


def build_compound_equity_curve(
    frame: pd.DataFrame,
    trades: List[Dict[str, Any]],
    y_col: str,
    x_col: str,
    start_idx: int,
    end_idx: int,
    initial_equity: float = 10_000.0,
) -> Tuple[pd.DataFrame, Dict[str, Any], List[Dict[str, Any]]]:
    visible = frame[
        (frame["global_index"] >= int(start_idx))
        & (frame["global_index"] <= int(end_idx))
    ].copy()
    visible["equity"] = float(initial_equity)
    realized_equity = float(initial_equity)
    sized_trades: List[Dict[str, Any]] = []

    for trade in sorted(trades, key=lambda item: int(item.get("entry_idx", 0))):
        entry_idx = int(trade["entry_idx"])
        exit_idx = int(trade["exit_idx"])
        entry_y = float(trade["entry_y_price"])
        entry_x = float(trade["entry_x_price"])
        beta_abs = abs(float(trade.get("beta", 0.0)))
        unit_gross = entry_y + beta_abs * entry_x
        scale = 0.0 if unit_gross <= 0 else realized_equity / unit_gross
        qty_y = max(0, int(math.floor(scale)))
        qty_x = max(0, int(math.floor(beta_abs * scale)))
        invested_gross = qty_y * entry_y + qty_x * entry_x

        segment_mask = (
            (visible["global_index"] >= entry_idx)
            & (visible["global_index"] <= exit_idx)
        )
        segment = visible.loc[segment_mask]
        if segment.empty or qty_y == 0 or qty_x == 0:
            sized_trade = dict(trade)
            sized_trade.update(
                {
                    "qty_y": qty_y,
                    "qty_x": qty_x,
                    "invested_gross": invested_gross,
                    "capital_before": realized_equity,
                    "dollar_pnl": 0.0,
                    "capital_after": realized_equity,
                }
            )
            sized_trades.append(sized_trade)
            continue

        y_change = segment[y_col].astype(float) - entry_y
        x_change = segment[x_col].astype(float) - entry_x
        y_direction, x_direction = pair_leg_directions(
            str(trade.get("side")),
            float(trade.get("beta", 0.0)),
        )
        pnl_path = y_direction * qty_y * y_change + x_direction * qty_x * x_change
        visible.loc[segment_mask, "equity"] = realized_equity + pnl_path.to_numpy()

        dollar_pnl = float(pnl_path.iloc[-1])
        capital_before = realized_equity
        realized_equity += dollar_pnl
        visible.loc[visible["global_index"] > exit_idx, "equity"] = realized_equity
        sized_trade = dict(trade)
        sized_trade.update(
            {
                "qty_y": qty_y,
                "qty_x": qty_x,
                "invested_gross": float(invested_gross),
                "capital_before": float(capital_before),
                "dollar_pnl": dollar_pnl,
                "capital_after": float(realized_equity),
            }
        )
        sized_trades.append(sized_trade)

    # Keep the equity chart on the exact same bar/date axis as the Z-score chart.
    visible["plot_x"] = visible["global_index"].astype(int)
    visible["trade_date"] = visible[TIME_COL].dt.strftime("%Y-%m-%d")
    daily_equity = visible.groupby("trade_date", sort=False)["equity"].last().astype(float)
    daily_returns = daily_equity.pct_change().dropna()
    return_std = float(daily_returns.std(ddof=1)) if len(daily_returns) > 1 else math.nan
    sharpe = (
        math.nan
        if not np.isfinite(return_std) or return_std <= 0
        else float(math.sqrt(252.0) * daily_returns.mean() / return_std)
    )

    running_peak = visible["equity"].cummax()
    drawdown = visible["equity"] / running_peak - 1.0
    trough_pos = int(drawdown.to_numpy().argmin())
    max_drawdown_pct = float(drawdown.iloc[trough_pos] * 100.0)
    peak_pos = int(visible["equity"].iloc[: trough_pos + 1].to_numpy().argmax())
    stats = {
        "initial_equity": float(initial_equity),
        "final_equity": float(visible["equity"].iloc[-1]),
        "total_return_pct": float((visible["equity"].iloc[-1] / initial_equity - 1.0) * 100.0),
        "sharpe": sharpe,
        "max_drawdown_pct": max_drawdown_pct,
        "drawdown_peak_time": visible.iloc[peak_pos]["time_str"],
        "drawdown_trough_time": visible.iloc[trough_pos]["time_str"],
        "daily_returns": {
            str(trade_date): float(value)
            for trade_date, value in daily_returns.items()
            if np.isfinite(value)
        },
        "bar_returns": {
            str(timestamp): float(value)
            for timestamp, value in visible.set_index("time_str")["equity"].pct_change().fillna(0.0).items()
            if np.isfinite(value)
        },
    }
    return visible, stats, sized_trades


def build_kelly_scope_returns(
    frame: pd.DataFrame,
    pair_key: str,
    start_idx: int,
    end_idx: int,
    beta: float,
    alpha: float,
    sigma: float,
    y_col: str,
    x_col: str,
    entry_rule: str,
) -> Dict[str, Dict[str, float]]:
    result = run_auto_reentry_backtest(
        frame=frame, pair_key=pair_key, split_idx=start_idx, end_idx=end_idx,
        beta=beta, alpha=alpha, sigma=sigma, y_col=y_col, x_col=x_col,
        entry_rule=entry_rule,
    )
    _, scope_stats, _ = build_compound_equity_curve(
        frame=frame, trades=result["trades"], y_col=y_col, x_col=x_col,
        start_idx=start_idx, end_idx=end_idx,
    )
    return {"daily": scope_stats["daily_returns"], "bar": scope_stats["bar_returns"]}


def build_equity_chart(
    equity_df: pd.DataFrame,
    start_idx: int,
    end_idx: int,
    initial_equity: float = 10_000.0,
) -> go.Figure:
    fig = go.Figure()
    add_alternating_day_background(fig, equity_df)
    fig.add_trace(
        go.Scatter(
            x=equity_df["plot_x"],
            y=equity_df["equity"],
            mode="lines",
            name="Net Equity",
            line=dict(color="#2563eb", width=2.2),
            fill="tozeroy",
            fillcolor="rgba(37, 99, 235, 0.10)",
            customdata=np.column_stack(
                (equity_df["time_str"], equity_df["trading_day_number"])
            ),
            hovertemplate=(
                "%{customdata[0]}<br>Trading Day: %{customdata[1]}<br>"
                "equity=$%{y:,.2f}<extra></extra>"
            ),
        )
    )
    fig.add_hline(y=float(initial_equity), line_dash="dash", line_color="#6b7280")
    fig.update_layout(
        title="$10,000 Integer-Share Compound Equity",
        height=380,
        margin=dict(l=90, r=30, t=45, b=20),
        xaxis_title="Trading Day Since Train Start",
        yaxis_title="Net Equity (USD)",
        showlegend=False,
    )
    fig.update_xaxes(**shared_backtest_xaxis(equity_df, start_idx, end_idx))
    fig.update_yaxes(type="log", automargin=False)
    return fig


def build_chart(
    frame: pd.DataFrame,
    game: Dict[str, Any],
    train_start: int,
    split_idx: int,
    focus_train_test_only: bool = False,
) -> go.Figure:
    base_line_color = "#9ca3af"
    train_line_color = "#2a6f97"
    profit_trade_color = "#16a34a"
    loss_trade_color = "#f59e0b"

    frame = frame.copy()
    frame["plot_x"] = np.arange(len(frame))
    frame["trade_date"] = frame[TIME_COL].dt.strftime("%Y-%m-%d")
    upper_band = 2.0
    lower_band = -2.0

    cursor_abs = int(game["cursor_abs"])
    if focus_train_test_only:
        revealed = frame[
            (frame["global_index"] >= int(train_start))
            & (frame["global_index"] <= cursor_abs)
        ].copy()
    else:
        revealed = frame[frame["global_index"] <= cursor_abs].copy()
    pretrain_part = revealed[revealed["global_index"] < train_start]
    train_part = revealed[(revealed["global_index"] >= train_start) & (revealed["global_index"] < split_idx)]
    test_part = revealed[revealed["global_index"] >= split_idx]

    fig = go.Figure()
    if not pretrain_part.empty:
        fig.add_trace(
            go.Scatter(
                x=pretrain_part["plot_x"],
                y=pretrain_part["zscore"],
                mode="lines",
                name="Pre-Train",
                line=dict(color=base_line_color, width=1.5),
                customdata=[
                    [time_str, int(global_index)]
                    for time_str, global_index in zip(pretrain_part["time_str"], pretrain_part["global_index"])
                ],
                hovertemplate="%{customdata[0]}<br>idx=%{customdata[1]}<br>z=%{y:.2f}<extra></extra>",
            )
        )

    if not train_part.empty:
        fig.add_trace(
            go.Scatter(
                x=train_part["plot_x"],
                y=train_part["zscore"],
                mode="lines",
                name="Train Z",
                line=dict(color=train_line_color, width=2.2),
                customdata=[
                    [time_str, int(global_index)]
                    for time_str, global_index in zip(train_part["time_str"], train_part["global_index"])
                ],
                hovertemplate="%{customdata[0]}<br>idx=%{customdata[1]}<br>z=%{y:.2f}<extra></extra>",
            )
        )

    if not test_part.empty:
        fig.add_trace(
            go.Scatter(
                x=test_part["plot_x"],
                y=test_part["zscore"],
                mode="lines",
                name="Revealed Test Z",
                line=dict(color=base_line_color, width=2),
                customdata=[
                    [time_str, int(global_index)]
                    for time_str, global_index in zip(test_part["time_str"], test_part["global_index"])
                ],
                hovertemplate="%{customdata[0]}<br>idx=%{customdata[1]}<br>z=%{y:.2f}<extra></extra>",
            )
        )

    all_trades = list(game.get("trades", []))
    completed_trades = [trade for trade in all_trades if trade.get("entry_idx") is not None and trade.get("exit_idx") is not None]
    open_position = game.get("open_position")
    if open_position:
        all_trades.append(
            {
                "side": open_position["side"],
                "entry_time": open_position["entry_time"],
                "entry_z": open_position["entry_z"],
                "exit_time": None,
                "exit_z": None,
            }
        )

    profit_legend_shown = False
    loss_legend_shown = False
    for trade in completed_trades:
        entry_idx = int(trade["entry_idx"])
        exit_idx = int(trade["exit_idx"])
        trade_segment = frame.iloc[entry_idx : min(exit_idx, cursor_abs) + 1].copy()
        if trade_segment.empty:
            continue

        trade_return = float(trade.get("return_pct", 0.0))
        is_profit = trade_return > 0
        line_color = profit_trade_color if is_profit else loss_trade_color
        trace_name = "Winning Trades" if is_profit else "Losing Trades"
        show_legend = False
        if is_profit and not profit_legend_shown:
            show_legend = True
            profit_legend_shown = True
        if (not is_profit) and not loss_legend_shown:
            show_legend = True
            loss_legend_shown = True

        fig.add_trace(
            go.Scatter(
                x=trade_segment["plot_x"],
                y=trade_segment["zscore"],
                mode="lines",
                name=trace_name,
                showlegend=show_legend,
                line=dict(color=line_color, width=2.4),
                opacity=0.85,
                customdata=[
                    [
                        time_str,
                        int(global_index),
                        float(trade_return),
                        str(trade.get("exit_reason", "")),
                    ]
                    for time_str, global_index in zip(trade_segment["time_str"], trade_segment["global_index"])
                ],
                hovertemplate=(
                    "%{customdata[0]}<br>"
                    "idx=%{customdata[1]}<br>"
                    "z=%{y:.2f}<br>"
                    "trade return=%{customdata[2]:+.2f}%<br>"
                    "%{customdata[3]}<extra></extra>"
                ),
            )
        )

    entry_x: List[pd.Timestamp] = []
    entry_y: List[float] = []
    entry_text: List[str] = []
    exit_x: List[pd.Timestamp] = []
    exit_y: List[float] = []
    exit_text: List[str] = []

    z_map = dict(zip(frame["time_str"], frame["zscore"]))
    ts_map = dict(zip(frame["time_str"], frame[TIME_COL]))
    x_map = dict(zip(frame["time_str"], frame["plot_x"]))

    for trade in all_trades:
        if trade["entry_time"] in x_map:
            entry_x.append(x_map[trade["entry_time"]])
            entry_y.append(float(z_map[trade["entry_time"]]))
            entry_text.append(f"{trade['side']} entry")

        exit_time = trade.get("exit_time")
        if exit_time and exit_time in x_map:
            exit_x.append(x_map[exit_time])
            exit_y.append(float(z_map[exit_time]))
            reason = trade.get("exit_reason", "exit")
            exit_text.append(reason)

    if entry_x:
        fig.add_trace(
            go.Scatter(
                x=entry_x,
                y=entry_y,
                mode="markers",
                name="Entries",
                marker=dict(color="#059669", size=10, symbol="triangle-up"),
                text=entry_text,
                customdata=[frame["time_str"].iloc[int(x)] for x in entry_x],
                hovertemplate="%{customdata}<br>%{text}<br>z=%{y:.2f}<extra></extra>",
            )
        )

    if exit_x:
        fig.add_trace(
            go.Scatter(
                x=exit_x,
                y=exit_y,
                mode="markers",
                name="Exits",
                marker=dict(color="#dc2626", size=10, symbol="x"),
                text=exit_text,
                customdata=[frame["time_str"].iloc[int(x)] for x in exit_x],
                hovertemplate="%{customdata}<br>%{text}<br>z=%{y:.2f}<extra></extra>",
            )
        )

    split_x = int(split_idx)
    current_x = int(cursor_abs)
    train_start_x = int(train_start)

    fig.add_vline(x=train_start_x, line_dash="dash", line_color="#2563eb")
    fig.add_vline(x=split_x, line_dash="dash", line_color="#111827")
    fig.add_vline(x=current_x, line_dash="dot", line_color="#6b7280")
    fig.add_hline(y=0, line_dash="dash", line_color="#6b7280")
    fig.add_hline(y=upper_band, line_dash="dot", line_color="#ef4444")
    fig.add_hline(y=lower_band, line_dash="dot", line_color="#10b981")

    if focus_train_test_only:
        visible_start = max(0, train_start_x - 1)
        visible_end = min(len(frame) - 1, max(current_x + 1, split_x + 1))
    tick_source = revealed[revealed["global_index"] >= int(train_start)]
    day_ticks = train_relative_day_ticks(tick_source, max_ticks=12)

    fig.update_layout(
        title=f"Revealed Z-Score Path | train start={train_start} | split={split_idx} | band={upper_band:+.2f} / {lower_band:+.2f}",
        height=520,
        margin=dict(l=20, r=20, t=50, b=20),
        xaxis_title="Trading Day Since Train Start",
        yaxis_title="Z-Score",
        legend_title="Series",
        clickmode="event+select",
    )
    fig.update_xaxes(
        tickmode="array",
        tickvals=day_ticks["plot_x"].tolist(),
        ticktext=day_ticks["tick_text"].tolist(),
        showgrid=False,
    )
    if focus_train_test_only:
        fig.update_xaxes(range=[visible_start, visible_end])
    return fig


st.title("Pairs Backtest Board")
st.caption("选择区间和规则后一次性运行完整统计套利回测；可选择是否把 Train 样本内交易纳入结果。")

pair_map = load_pairs_from_metapairs()
pair_source_options = ["Registered", "Temporary"] if pair_map else ["Temporary"]
pair_source = st.sidebar.radio(
    "Pair Source",
    pair_source_options,
    horizontal=True,
)

if pair_source == "Registered":
    pair_name = st.sidebar.selectbox("Pair", list(pair_map.keys()))
    pair_key, y_col, x_col = pair_map[pair_name]
    y_ticker = y_col[: -len("_price")]
    x_ticker = x_col[: -len("_price")]
    saved_pair_config = load_pair_config(pair_key, pair_name)
else:
    y_ticker = str(st.sidebar.text_input("Temporary Y ticker", value="V") or "").strip().upper()
    x_ticker = str(st.sidebar.text_input("Temporary X ticker", value="MA") or "").strip().upper()
    valid_ticker = re.compile(r"^[A-Z0-9.-]+$")
    if (
        not y_ticker
        or not x_ticker
        or y_ticker == x_ticker
        or not valid_ticker.fullmatch(y_ticker)
        or not valid_ticker.fullmatch(x_ticker)
    ):
        st.warning("请输入两个不同且有效的临时 ticker（仅支持字母、数字、点和连字符）。")
        st.stop()
    pair_name = f"{y_ticker} / {x_ticker} (Temporary)"
    pair_key = f"temp_{y_ticker.lower()}_{x_ticker.lower()}"
    y_col = f"{y_ticker}_price"
    x_col = f"{x_ticker}_price"
    config_pair_key = f"{y_ticker.lower()}_{x_ticker.lower()}"
    saved_pair_config = load_pair_config(config_pair_key, f"{y_ticker} / {x_ticker}")

try:
    raw_df = load_pair_data(y_ticker, x_ticker)
except Exception as exc:
    st.error(f"读取 us_market_prices 的 {pair_name} 失败: {exc}")
    st.stop()

required_cols = [TIME_COL, y_col, x_col]
missing_cols = [c for c in required_cols if c not in raw_df.columns]
if missing_cols:
    st.error(f"us_market_prices 查询结果缺少字段: {', '.join(missing_cols)}")
    st.stop()

raw_df = raw_df.dropna(subset=[y_col, x_col]).reset_index(drop=True)
if len(raw_df) < 80:
    st.error("数据太少，至少需要 80 根 bar。")
    st.stop()

min_end_idx = 40
default_end_idx = len(raw_df) - 1
saved_train_idx = nearest_time_index(raw_df, saved_pair_config.get("train_time"))
saved_split_idx = nearest_time_index(raw_df, saved_pair_config.get("split_time"))
end_idx = st.sidebar.slider(
    "数据终点 (bar index)",
    min_value=min_end_idx,
    max_value=len(raw_df) - 1,
    value=default_end_idx,
)
st.sidebar.caption(f"终点时间: {raw_df.iloc[end_idx][TIME_COL].strftime('%Y-%m-%d %H:%M:%S')}")

default_train = max(0, end_idx - min(1200, end_idx - 20))
default_split = min(end_idx - 1, max(default_train + 20, default_train + min(400, end_idx - default_train - 1)))
if saved_train_idx is not None and saved_train_idx <= end_idx - 20:
    default_train = clamp_int(saved_train_idx, 0, end_idx - 20)
if saved_split_idx is not None:
    default_split = clamp_int(saved_split_idx, default_train + 10, end_idx - 1)
control_scenario_key = (
    f"{pair_name}|{end_idx}|"
    f"{saved_pair_config.get('train_time')}|{saved_pair_config.get('split_time')}"
)

if st.session_state.get("control_scenario_key") != control_scenario_key:
    st.session_state.control_scenario_key = control_scenario_key
    st.session_state.train_start_control = default_train
    st.session_state.split_idx_control = default_split
    st.session_state.pending_train_start_control = None
    st.session_state.pending_split_idx_control = None

pending_train_start = st.session_state.get("pending_train_start_control")
pending_split_idx = st.session_state.get("pending_split_idx_control")
if pending_train_start is not None:
    st.session_state.train_start_control = int(pending_train_start)
    st.session_state.pending_train_start_control = None
if pending_split_idx is not None:
    st.session_state.split_idx_control = int(pending_split_idx)
    st.session_state.pending_split_idx_control = None

st.session_state.train_start_control = clamp_int(st.session_state.train_start_control, 0, end_idx - 20)
st.sidebar.slider(
    "Train Start",
    min_value=0,
    max_value=end_idx - 20,
    key="train_start_control",
)
train_start = int(st.session_state.train_start_control)

split_low = train_start + 10
split_high = end_idx - 1
st.session_state.split_idx_control = clamp_int(st.session_state.split_idx_control, split_low, split_high)
st.sidebar.slider(
    "Train / Test Split",
    min_value=split_low,
    max_value=split_high,
    key="split_idx_control",
)
split_idx = int(st.session_state.split_idx_control)
upper_band = 2.0
lower_band = -2.0

with st.sidebar.expander("Backtest", expanded=True):
    if saved_pair_config:
        st.caption(
            "使用 quant_memory.pair_config 的 fixed-beta 时间范围："
            f"起点 `{fmt_time_value(saved_pair_config.get('train_start_bj'))}`，"
            f"终点 `{fmt_time_value(saved_pair_config.get('test_end_bj'))}`。"
        )
    else:
        st.caption("当前 pair 在 quant_memory.pair_config 里没有 5-minute fixed-beta 设置，使用侧栏当前 Train/Split。")
    entry_rule_options = {
        "Reentry Inside Band": "reentry",
        "Band Flip": "band_flip",
    }
    saved_entry_rule_key = str(
        load_entry_rules().get(str(pair_key).strip().lower(), "reentry")
    ).strip().lower()
    saved_entry_label = next(
        (
            label
            for label, rule_key in entry_rule_options.items()
            if rule_key == saved_entry_rule_key
        ),
        "Reentry Inside Band",
    )
    auto_entry_rule = st.selectbox(
        "Entry / Exit Rule",
        list(entry_rule_options),
        index=list(entry_rule_options).index(saved_entry_label),
        help=(
            "Reentry Inside Band: z 从 +2/-2 外侧回到通道内时入场。"
            "Band Flip: z>=+2 做空，跨过 0 平空；z<=-2 做多，跨过 0 平多。"
        ),
    )
    auto_entry_rule_key = entry_rule_options[auto_entry_rule]
    if str(pair_key).strip().lower() in load_entry_rules():
        st.caption(
            f"使用 pair_exit_rules.json 保存的入场规则：`{saved_entry_rule_key}`。"
        )
    include_train = st.checkbox(
        "包含 Train 区间（样本内）",
        value=False,
        help="关闭时只回测 Split 之后的样本外 Test；开启时从 Train Start 开始回测，但模型仍使用完整 Train 拟合。",
    )
    auto_rule_caption = (
        "Band Flip：z>=+2 short spread，z<=0 平 short；"
        "z<=-2 long spread，z>=0 平 long；同一交易日不平当日新开的仓。"
        if auto_entry_rule_key == "band_flip"
        else "Reentry Inside Band：z 从 +2 以上回到通道内时 short spread；z 从 -2 以下回到通道内时 long spread。"
    )
    st.caption(f"{auto_rule_caption} 出场仍会先经过下方 Active Exit Rules 的 stop/take 检查。")

try:
    analysis_df, stats = build_analysis_frame(
        df=raw_df,
        y_col=y_col,
        x_col=x_col,
        train_start=train_start,
        split_idx=split_idx,
        end_idx=end_idx,
    )
except Exception as exc:
    st.error(str(exc))
    st.stop()

with st.sidebar.expander("Model Override", expanded=False):
    override_enabled = st.checkbox("启用模型微调", value=False)
    override_beta = st.number_input(
        "beta",
        value=float(stats["beta"]),
        step=0.0001,
        format="%.6f",
        disabled=not override_enabled,
    )
    override_alpha = st.number_input(
        "alpha",
        value=float(stats["alpha"]),
        step=0.0001,
        format="%.6f",
        disabled=not override_enabled,
    )
    override_mu_shift = st.number_input(
        "mu shift",
        value=0.0,
        step=0.01,
        format="%.4f",
        disabled=not override_enabled,
    )
    override_sigma_multiplier = st.number_input(
        "sigma multiplier",
        min_value=0.01,
        max_value=10.0,
        value=1.0,
        step=0.05,
        format="%.4f",
        disabled=not override_enabled,
    )

model_key = "base"
if override_enabled:
    try:
        analysis_df, stats = apply_model_override(
            frame=analysis_df,
            stats=stats,
            y_col=y_col,
            x_col=x_col,
            train_start=train_start,
            split_idx=split_idx,
            beta=float(override_beta),
            alpha=float(override_alpha),
            mu_shift=float(override_mu_shift),
            sigma_multiplier=float(override_sigma_multiplier),
        )
    except Exception as exc:
        st.error(str(exc))
        st.stop()
    model_key = (
        f"override|b={float(override_beta):.8f}|a={float(override_alpha):.8f}|"
        f"m={float(override_mu_shift):.8f}|s={float(override_sigma_multiplier):.8f}"
    )

analysis_df = add_train_relative_trading_days(analysis_df, train_start)

current_run_signature = {
    "calculation_version": 2,
    "pair_name": pair_name,
    "train_start_idx": int(train_start),
    "split_idx": int(split_idx),
    "end_idx": int(end_idx),
    "include_train": bool(include_train),
    "rule_key": auto_entry_rule_key,
    "model_key": model_key,
}
previous_run = st.session_state.get("pair_batch_window", {})
should_run_backtest = any(
    previous_run.get(key) != value for key, value in current_run_signature.items()
)

if should_run_backtest:
    with st.spinner(f"Running {pair_name}..."):
        backtest_start_idx = int(train_start if include_train else split_idx)
        auto_result = run_auto_reentry_backtest(
            frame=analysis_df,
            pair_key=pair_key,
            split_idx=backtest_start_idx,
            end_idx=int(end_idx),
            beta=float(stats["beta"]),
            alpha=float(stats["alpha"]),
            sigma=float(stats["sigma"]),
            y_col=y_col,
            x_col=x_col,
            upper_band=float(upper_band),
            lower_band=float(lower_band),
            entry_rule=auto_entry_rule_key,
        )
        split_row = analysis_df.iloc[int(split_idx)]
        end_row = analysis_df.iloc[int(end_idx)]
        st.session_state.pair_batch_results = pd.DataFrame(
            [
                {
                    "Pair": pair_name,
                    "pair_key": pair_key,
                    "Train Start": analysis_df.iloc[int(train_start)]["time_str"],
                    "Split": split_row["time_str"],
                    "End": end_row["time_str"],
                    "Scope": "Train + Test (in-sample included)" if include_train else "Test only (out-of-sample)",
                    "Trades": auto_result["trade_count"],
                    "Win Rate %": auto_result["win_rate_pct"],
                    "Total Return %": auto_result["total_return_pct"],
                    "Avg Trade %": auto_result["avg_trade_pct"],
                    "Best %": auto_result["best_trade_pct"],
                    "Worst %": auto_result["worst_trade_pct"],
                    "Beta": float(stats["beta"]),
                    "Sigma": float(stats["sigma"]),
                    "Stop Loss": rule_source_text(pair_key, "stop_loss"),
                    "Take Profit": rule_source_text(pair_key, "take_profit"),
                    "Rule": auto_entry_rule,
                    "Status": "OK",
                }
            ]
        )
        st.session_state.pair_batch_trades = list(auto_result.get("trades") or [])
        st.session_state.pair_batch_window = {
            "calculation_version": 2,
            "pair_name": pair_name,
            "train_start": analysis_df.iloc[int(train_start)]["time_str"],
            "split": split_row["time_str"],
            "end": end_row["time_str"],
            "backtest_start_idx": backtest_start_idx,
            "train_start_idx": int(train_start),
            "split_idx": int(split_idx),
            "end_idx": int(end_idx),
            "include_train": bool(include_train),
            "scope": "Train + Test（包含样本内）" if include_train else "Test only（样本外）",
            "rule": auto_entry_rule,
            "rule_key": auto_entry_rule_key,
            "model_key": model_key,
            "rule_caption": auto_rule_caption,
        }

batch_results = st.session_state.get("pair_batch_results")
batch_window = st.session_state.get("pair_batch_window", {})
result_ready = (
    isinstance(batch_results, pd.DataFrame)
    and not batch_results.empty
    and batch_window.get("pair_name") == pair_name
    and batch_window.get("train_start_idx") == int(train_start)
    and batch_window.get("split_idx") == int(split_idx)
    and batch_window.get("end_idx") == int(end_idx)
    and batch_window.get("include_train") == bool(include_train)
    and batch_window.get("rule_key") == auto_entry_rule_key
    and batch_window.get("model_key") == model_key
)

model_cols = st.columns(6)
model_cols[0].metric("Train Start", analysis_df.iloc[int(train_start)]["time_str"])
model_cols[1].metric("Split", analysis_df.iloc[int(split_idx)]["time_str"])
model_cols[2].metric("End", analysis_df.iloc[int(end_idx)]["time_str"])
model_cols[3].metric("Beta", f"{float(stats['beta']):.4f}")
model_cols[4].metric("Spread Sigma", f"{float(stats['sigma']):.4f}")
model_cols[5].metric(
    "ADF p-value",
    "n/a" if stats.get("adf_p") is None else f"{float(stats['adf_p']):.4f}",
)

if stats.get("override_enabled"):
    st.caption(
        "Model Override ON | "
        f"base beta={float(stats['base_beta']):.4f}, beta={float(stats['beta']):.4f} | "
        f"base sigma={float(stats['base_sigma']):.4f}, sigma={float(stats['sigma']):.4f} | "
        f"mu shift={float(stats['mu_shift']):+.4f}, sigma x{float(stats['sigma_multiplier']):.4f}"
    )

st.subheader("Active Exit Rules")
end_row_for_rules = analysis_df.iloc[int(end_idx)]
end_notional = float(end_row_for_rules[y_col]) + abs(float(stats["beta"])) * float(end_row_for_rules[x_col])
sigma_for_stop_pct = (
    math.nan
    if end_notional == 0
    else (float(stats["sigma"]) / end_notional) * 100.0
)
st.markdown(f"**Pair key:** `{pair_key}`")
st.markdown(describe_stop_loss_rule(pair_key, sigma_for_stop_pct))
st.markdown(
    describe_take_profit_rule(
        pair_key,
        sigma_for_stop_pct,
        float(end_row_for_rules["zscore"]),
    )
)
st.caption("每根 bar 先检查 stop loss，再检查 take profit；同一入场日不自动出场。")

if not result_ready:
    st.info("正在根据当前设置生成完整回测结果。")
    st.stop()

result_row = batch_results.iloc[0]
st.subheader("Backtest Results")
st.caption(
    f"{batch_window.get('scope', '-')} | {batch_window.get('rule_caption', '-')} | "
    f"Stop: `{result_row.get('Stop Loss', '-')}` | Take profit: `{result_row.get('Take Profit', '-')}`"
)

summary_cols = st.columns(6)
summary_cols[0].metric("Trades", int(result_row["Trades"]))
summary_cols[1].metric(
    "Win Rate",
    "n/a" if pd.isna(result_row["Win Rate %"]) else f"{float(result_row['Win Rate %']):.1f}%",
)
summary_cols[2].metric("Total Return", f"{float(result_row['Total Return %']):+.2f}%")
summary_cols[3].metric(
    "Avg Trade",
    "n/a" if pd.isna(result_row["Avg Trade %"]) else f"{float(result_row['Avg Trade %']):+.2f}%",
)
summary_cols[4].metric(
    "Best Trade",
    "n/a" if pd.isna(result_row["Best %"]) else f"{float(result_row['Best %']):+.2f}%",
)
summary_cols[5].metric(
    "Worst Trade",
    "n/a" if pd.isna(result_row["Worst %"]) else f"{float(result_row['Worst %']):+.2f}%",
)

auto_trades = st.session_state.get("pair_batch_trades") or []
chart_start_idx = int(batch_window.get("backtest_start_idx", split_idx))
auto_chart = build_auto_backtest_chart(
    frame=analysis_df,
    trades=auto_trades,
    train_start=chart_start_idx,
    split_idx=split_idx,
    end_idx=end_idx,
)
st.plotly_chart(auto_chart, use_container_width=True)

equity_df, equity_stats, sized_trades = build_compound_equity_curve(
    frame=analysis_df,
    trades=auto_trades,
    y_col=y_col,
    x_col=x_col,
    start_idx=chart_start_idx,
    end_idx=end_idx,
    initial_equity=10_000.0,
)
equity_chart = build_equity_chart(
    equity_df,
    start_idx=chart_start_idx,
    end_idx=end_idx,
    initial_equity=10_000.0,
)
st.plotly_chart(equity_chart, use_container_width=True)

equity_metric_cols = st.columns(4)
equity_metric_cols[0].metric("Initial Equity", "$10,000.00")
equity_metric_cols[1].metric(
    "Final Equity",
    f"${float(equity_stats['final_equity']):,.2f}",
    delta=f"{float(equity_stats['total_return_pct']):+.2f}%",
)
equity_metric_cols[2].metric(
    "Annualized Sharpe",
    "n/a" if not np.isfinite(equity_stats["sharpe"]) else f"{float(equity_stats['sharpe']):.2f}",
    help="按每个交易日最后一根 bar 的净资产收益计算，假设无风险利率为 0，按 252 个交易日年化。",
)
equity_metric_cols[3].metric(
    "Maximum Drawdown",
    f"{float(equity_stats['max_drawdown_pct']):.2f}%",
    help=(
        "最大回撤区间\n\n"
        f"峰值时间：{equity_stats['drawdown_peak_time']}\n\n"
        f"谷值时间：{equity_stats['drawdown_trough_time']}"
    ),
)
st.caption(
    "每笔交易按当时净资产和 beta 比例配置两腿整数股，未能整除的资金保留为现金，平仓后全部权益用于下一笔交易。"
)

st.subheader("Trade Log")
if not sized_trades:
    st.write("当前区间和规则没有产生交易。")
else:
    auto_trade_df = pd.DataFrame(sized_trades)
    st.dataframe(
        auto_trade_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "entry_z": st.column_config.NumberColumn(format="%+.2f"),
            "exit_z": st.column_config.NumberColumn(format="%+.2f"),
            "peak_return_pct": st.column_config.NumberColumn(format="%+.2f%%"),
            "return_pct": st.column_config.NumberColumn(format="%+.2f%%"),
            "invested_gross": st.column_config.NumberColumn(format="$%.2f"),
            "capital_before": st.column_config.NumberColumn(format="$%.2f"),
            "dollar_pnl": st.column_config.NumberColumn(format="$%+.2f"),
            "capital_after": st.column_config.NumberColumn(format="$%.2f"),
        },
    )

st.divider()
st.subheader("凯利资产组合")
st.caption(
    "加入后保存逐日收益（用于 Kelly 权重）及 5 分钟 bar 收益路径（用于更精确的组合净值曲线）。"
)
kelly_asset_id = f"pair:{pair_key}:{auto_entry_rule_key}"
add_to_kelly = st.button(
    "添加到凯利资产组合",
    type="primary",
    use_container_width=True,
    disabled=not bool(equity_stats.get("daily_returns")),
)
if add_to_kelly:
    try:
        scope_returns = {
            "test": build_kelly_scope_returns(
                analysis_df, pair_key, int(split_idx), int(end_idx), float(stats["beta"]),
                float(stats["alpha"]), float(stats["sigma"]), y_col, x_col, auto_entry_rule_key,
            ),
            "train_test": build_kelly_scope_returns(
                analysis_df, pair_key, int(train_start), int(end_idx), float(stats["beta"]),
                float(stats["alpha"]), float(stats["sigma"]), y_col, x_col, auto_entry_rule_key,
            ),
        }
        test_returns = scope_returns["test"]
        save_kelly_portfolio_asset(
            {
                "asset_id": kelly_asset_id,
                "pair_key": pair_key,
                "display_name": pair_name,
                "strategy_rule": auto_entry_rule_key,
                "scope": "both",
                "train_start": analysis_df.iloc[int(train_start)][TIME_COL].to_pydatetime(),
                "split_time": analysis_df.iloc[int(split_idx)][TIME_COL].to_pydatetime(),
                "end_time": analysis_df.iloc[int(end_idx)][TIME_COL].to_pydatetime(),
                "beta": float(stats["beta"]),
                "initial_equity": 10_000.0,
                "final_equity": float(equity_stats["final_equity"]),
                "annualized_sharpe": None,
                "max_drawdown_pct": float(equity_stats["max_drawdown_pct"]),
                "daily_returns": test_returns["daily"],
                "bar_returns": test_returns["bar"],
                "scope_returns": scope_returns,
            }
        )
    except Exception as exc:
        st.error(f"添加到凯利资产组合失败：{exc}")
    else:
        st.success(f"已添加 `{pair_name}`；Test only 与 Train + Test 路径已保存到同一资产。")

available_kelly_assets = load_kelly_portfolio_assets()
selected_kelly_scopes: Dict[str, str] = {}
if available_kelly_assets:
    st.caption("每个策略资产只保存一份，并可独立选择加载 Test only 或 Train + Test 路径。")
    for asset in available_kelly_assets:
        asset_id = str(asset["asset_id"])
        available_scopes = list(asset["scope_returns"].keys())
        available_scopes.sort(key=lambda scope: 0 if scope == "train_test" else 1)
        available_scopes.append("none")
        labels = {"test": "Test only", "train_test": "Train + Test", "none": "None"}
        selected_kelly_scopes[asset_id] = st.selectbox(
            f"{asset['display_name']} · {asset['strategy_rule']}",
            options=available_scopes,
            format_func=lambda scope, label_map=labels: label_map.get(scope, scope),
            key=f"kelly_scope_v2:{asset_id}",
        )

load_kelly_portfolio = st.button(
    "加载并计算凯利组合",
    use_container_width=True,
)
if not load_kelly_portfolio:
    st.caption("凯利组合默认不加载；点击上方按钮后才会读取资产、计算权重并生成 Net Equity。")
    st.stop()

with st.spinner("正在加载并计算凯利资产组合..."):
    kelly_assets = []
    for asset in available_kelly_assets:
        selected_scope = selected_kelly_scopes.get(str(asset["asset_id"]))
        if selected_scope == "none":
            continue
        returns = asset["scope_returns"].get(selected_scope or "")
        if returns:
            selected_asset = dict(asset)
            selected_asset["scope"] = selected_scope
            selected_asset["daily_returns"] = returns.get("daily") or {}
            selected_asset["bar_returns"] = returns.get("bar") or {}
            kelly_assets.append(selected_asset)
if not kelly_assets:
    st.info("凯利资产组合目前为空。")
else:
    assets_without_bar_path = [
        asset["display_name"] for asset in kelly_assets if not asset.get("bar_returns")
    ]
    if assets_without_bar_path:
        st.warning(
            "以下旧资产没有保存 5 分钟 bar 路径，只能按日兼容显示："
            f"{', '.join(assets_without_bar_path)}。"
            "请在对应 Pair 页面点击一次“添加到凯利资产组合”以更新为精细路径。"
        )
    try:
        kelly_equity_df, kelly_weights_df, kelly_stats = build_kelly_portfolio(
            kelly_assets,
            initial_equity=10_000.0,
        )
    except Exception as exc:
        st.warning(f"暂时无法生成凯利组合净资产：{exc}")
    else:
        st.markdown(f"#### 凯利组合 Net Equity · {len(kelly_assets)} 个策略资产")
        kelly_chart = build_kelly_equity_chart(kelly_equity_df)
        st.plotly_chart(kelly_chart, use_container_width=True)

        kelly_metric_cols = st.columns(4)
        kelly_metric_cols[0].metric(
            "Portfolio Final Equity",
            f"${float(kelly_stats['final_equity']):,.2f}",
            delta=f"{float(kelly_stats['total_return_pct']):+.2f}%",
        )
        kelly_metric_cols[1].metric(
            "Portfolio Sharpe",
            "n/a" if not np.isfinite(kelly_stats["sharpe"]) else f"{float(kelly_stats['sharpe']):.2f}",
            help="组合逐日收益的年化夏普率，无风险利率为 0，按 252 个交易日年化。",
        )
        kelly_metric_cols[2].metric(
            "Portfolio Max Drawdown",
            f"{float(kelly_stats['max_drawdown_pct']):.2f}%",
            help=(
                "最大回撤区间\n\n"
                f"峰值时间：{kelly_stats['drawdown_peak_time']}\n\n"
                f"谷值时间：{kelly_stats['drawdown_trough_time']}"
            ),
        )
        kelly_metric_cols[3].metric("Assets", len(kelly_assets))
        st.dataframe(
            kelly_weights_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Kelly Score": st.column_config.NumberColumn(format="%.4f"),
                "Portfolio Weight %": st.column_config.ProgressColumn(
                    format="%.2f%%",
                    min_value=0.0,
                    max_value=100.0,
                ),
            },
        )
        st.caption(
            "权重由逐日收益均值与协方差的 Kelly 方向 Σ⁻¹μ 得出，负权重截为 0，"
            "再归一化为 100% long-only 最大夏普配置；Net Equity 使用 5 分钟 bar 路径，缺失 bar 按空仓、收益 0 处理。"
        )

st.caption(
    "模型的 beta / alpha / mu / sigma 始终只由 Train Start → Split 拟合。"
    "选择 Train + Test 时，Train 部分收益属于样本内结果；Test only 才是样本外结果。"
)
st.stop()

game = ensure_game_state(
    key=scenario_key(pair_name, train_start, split_idx, end_idx, model_key),
    split_idx=split_idx,
    end_idx=end_idx,
)

cursor_abs = clamp_int(game["cursor_abs"], split_idx, end_idx)
game["cursor_abs"] = cursor_abs
current_row = get_row_by_abs_idx(analysis_df, cursor_abs)
sync_open_position(
    game=game,
    current_row=current_row,
    beta=float(stats["beta"]),
    alpha=float(stats["alpha"]),
    y_col=y_col,
    x_col=x_col,
)

current_row = get_row_by_abs_idx(analysis_df, int(game["cursor_abs"]))
open_position_state = game.get("open_position")
current_notional_qd = float(current_row[y_col]) + abs(float(stats["beta"])) * float(current_row[x_col])
current_two_sigma_return_pct = (
    math.nan
    if current_notional_qd == 0
    else (2.0 * float(stats["sigma"]) / current_notional_qd) * 100.0
)
current_stop_notional = float(current_row[y_col]) + abs(float(stats["beta"])) * float(current_row[x_col])
current_sigma_for_stop_pct = (
    math.nan
    if current_stop_notional == 0
    else (float(stats["sigma"]) / current_stop_notional) * 100.0
)

with st.sidebar.expander("Active Exit Rules", expanded=True):
    st.markdown(f"**Pair key:** `{pair_key}`")
    stop_rules = load_stop_loss_rules()
    take_profit_rules = load_take_profit_rules()
    raw_stop_rule = stop_rules.get(pair_key, DEFAULT_STOP_LOSS_RULE)
    raw_take_profit_rule = take_profit_rules.get(pair_key)
    stop_source = "pair_exit_rules.json" if pair_key in stop_rules else "default"
    take_profit_source = "pair_exit_rules.json" if pair_key in take_profit_rules else "not configured"
    st.caption(f"Raw stop_loss ({stop_source}): `{raw_stop_rule}`")
    st.caption(f"Raw take_profit ({take_profit_source}): `{raw_take_profit_rule if raw_take_profit_rule is not None else '-'}`")
    st.caption(
        f"当前估算 notional = Y + abs(beta) * X = `{current_stop_notional:.4f}`；"
        f"sigma_for_stop = spread_sigma / notional * 100 = `{fmt_signed_pct(current_sigma_for_stop_pct)}`。"
    )
    st.markdown(describe_stop_loss_rule(pair_key, current_sigma_for_stop_pct))
    st.markdown(describe_take_profit_rule(pair_key, current_sigma_for_stop_pct, float(current_row["zscore"])))
    st.caption(
        "执行顺序：每根 bar 先更新 peak_return 和 trailing 线；同一入场日不自动出场；"
        "之后先检查 stop loss，再检查 take profit。"
    )

metric_cols = st.columns(6)
metric_cols[0].metric("Current Bar", int(game["cursor_abs"]))
metric_cols[1].metric("Current Time", current_row["time_str"])
metric_cols[2].metric("Current Z", f"{float(current_row['zscore']):+.2f}")
metric_cols[3].metric("Beta", f"{float(stats['beta']):.4f}")
metric_cols[4].metric(
    "2σ Return",
    "n/a" if not np.isfinite(current_two_sigma_return_pct) else f"{current_two_sigma_return_pct:.2f}%",
)
metric_cols[5].metric("Z Channel", f"{upper_band:+.2f} / {lower_band:+.2f}")

if stats.get("override_enabled"):
    st.caption(
        "Model Override ON | "
        f"base beta={float(stats['base_beta']):.4f}, beta={float(stats['beta']):.4f} | "
        f"base sigma={float(stats['base_sigma']):.4f}, sigma={float(stats['sigma']):.4f} | "
        f"mu shift={float(stats['mu_shift']):+.4f}, sigma x{float(stats['sigma_multiplier']):.4f}"
    )

status_cols = st.columns(4)
status_cols[0].metric(
    "Train Start Time",
    analysis_df["time_str"].iloc[int(train_start)],
)
status_cols[1].metric(
    "Split Time",
    analysis_df["time_str"].iloc[int(split_idx)],
)
status_cols[2].metric("Revealed Test Bars", int(game["cursor_abs"] - split_idx + 1))
status_cols[3].metric("Realized Return", f"{float(game['realized_return_pct']):+.2f}%")

batch_results = st.session_state.get("pair_batch_results")
batch_window = st.session_state.get("pair_batch_window", {})
if (
    isinstance(batch_results, pd.DataFrame)
    and not batch_results.empty
    and batch_window.get("pair_name") == pair_name
):
    st.subheader("Auto Backtest")
    st.caption(
        f"Pair `{batch_window.get('pair_name', '-')}` | "
        f"train `{batch_window.get('train_start', '-')}` | "
        f"split `{batch_window.get('split', '-')}` | "
        f"end `{batch_window.get('end', '-')}`. "
        f"Rule: {batch_window.get('rule_caption', '-')}"
    )
    visible_batch_cols = [
        "Pair",
        "Rule",
        "Trades",
        "Win Rate %",
        "Total Return %",
        "Avg Trade %",
        "Best %",
        "Worst %",
        "Split",
        "End",
        "Stop Loss",
        "Take Profit",
        "Status",
    ]
    visible_batch_cols = [col for col in visible_batch_cols if col in batch_results.columns]
    st.dataframe(
        batch_results[visible_batch_cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            "Win Rate %": st.column_config.NumberColumn(format="%.1f%%"),
            "Total Return %": st.column_config.NumberColumn(format="%+.2f%%"),
            "Avg Trade %": st.column_config.NumberColumn(format="%+.2f%%"),
            "Best %": st.column_config.NumberColumn(format="%+.2f%%"),
            "Worst %": st.column_config.NumberColumn(format="%+.2f%%"),
        },
    )
    auto_trades = st.session_state.get("pair_batch_trades") or []
    if auto_trades:
        with st.expander("Auto Trade Log", expanded=False):
            auto_trade_df = pd.DataFrame(auto_trades)
            st.dataframe(
                auto_trade_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "entry_z": st.column_config.NumberColumn(format="%+.2f"),
                    "exit_z": st.column_config.NumberColumn(format="%+.2f"),
                    "peak_return_pct": st.column_config.NumberColumn(format="%+.2f%%"),
                    "return_pct": st.column_config.NumberColumn(format="%+.2f%%"),
                },
            )
        auto_chart = build_auto_backtest_chart(
            frame=analysis_df,
            trades=auto_trades,
            train_start=train_start,
            split_idx=split_idx,
            end_idx=end_idx,
        )
        st.plotly_chart(auto_chart, use_container_width=True)

left, right = st.columns([1.8, 1.1])

with left:
    focus_train_test_only = st.toggle("只看 Train Start 之后", value=True, key="focus_train_test_only")
    main_chart = build_chart(
        frame=analysis_df,
        game=game,
        train_start=train_start,
        split_idx=split_idx,
        focus_train_test_only=focus_train_test_only,
    )
    drag_value = render_draggable_chart(
        figure=main_chart,
        end_idx=end_idx,
        height=560,
        key=f"draggable_train_chart_{pair_name}_{end_idx}",
    )
    if isinstance(drag_value, dict) and "train_start" in drag_value:
        selected_global_index = clamp_int(int(drag_value["train_start"]), 0, end_idx - 20)
        if selected_global_index != train_start:
            st.session_state.pending_train_start_control = selected_global_index
            if st.session_state.split_idx_control <= selected_global_index + 9:
                st.session_state.pending_split_idx_control = min(end_idx - 1, selected_global_index + 10)
            st.rerun()
    if isinstance(drag_value, dict) and "split_idx" in drag_value:
        selected_split_index = clamp_int(int(drag_value["split_idx"]), train_start + 10, end_idx - 1)
        if selected_split_index != split_idx:
            st.session_state.pending_split_idx_control = selected_split_index
            st.rerun()
    st.caption("拖动蓝色线可改 train start；拖动黑虚线可改 split；工具栏可框选/滚轮缩放以观察局部。")

with right:
    st.subheader("Current Decision")
    st.write(
        f"{y_col.replace('_price', '')} = `{float(current_row[y_col]):.2f}`  |  "
        f"{x_col.replace('_price', '')} = `{float(current_row[x_col]):.2f}`"
    )
    st.write(
        f"spread = `{float(current_row['spread']):.4f}`  |  "
        f"mu = `{float(stats['mu']):.4f}`  |  "
        f"spread sigma = `{float(stats['sigma']):.4f}`"
    )

    if open_position_state:
        st.info(
            f"持仓中: `{open_position_state['side']}` | "
            f"entry z `{open_position_state['entry_z']:+.2f}` | "
            f"current return `{open_position_state['last_return_pct']:+.2f}%`"
        )
        st.write(
            f"peak return `{open_position_state['peak_return_pct']:+.2f}%`  |  "
            f"stop line `{open_position_state['stop_level_pct']:+.2f}%`"
        )
        st.write(
            f"2σ return `{open_position_state['entry_two_sigma_pct']:.2f}%`  |  "
            f"sigma_for_stop `{open_position_state['sigma_for_stop_pct']:.2f}%`"
        )
        st.caption(f"Live stop rule: {open_position_state['stop_rule_text']}")
        if open_position_state.get("take_profit_rule_text"):
            tp_level = open_position_state.get("take_profit_level")
            if tp_level is None:
                st.caption(f"Live take profit rule: {open_position_state['take_profit_rule_text']}")
            elif open_position_state.get("take_profit_metric") == "time_ny":
                st.caption(
                    f"Live take profit rule: {open_position_state['take_profit_rule_text']} | "
                    f"NY time {tp_level}"
                )
            else:
                metric_label = "z" if open_position_state.get("take_profit_metric") == "z_now" else "%"
                st.caption(
                    f"Live take profit rule: {open_position_state['take_profit_rule_text']} | "
                    f"level {tp_level:+.2f}{metric_label}"
                )
    else:
        st.success("当前空仓，可以选择进场，也可以继续观察。")

    action_cols = st.columns(3)
    long_clicked = action_cols[0].button("Long Spread", use_container_width=True, disabled=bool(open_position_state))
    short_clicked = action_cols[1].button("Short Spread", use_container_width=True, disabled=bool(open_position_state))
    exit_clicked = action_cols[2].button("Manual Exit", use_container_width=True, disabled=not bool(open_position_state))

    nav_cols = st.columns(5)
    step_clicked = nav_cols[0].button(f"+{reveal_step} Bar", use_container_width=True)
    step5_clicked = nav_cols[1].button("+10 Bar", use_container_width=True)
    week_clicked = nav_cols[2].button("+1 Week", use_container_width=True)
    to_exit_clicked = nav_cols[3].button("Go To Exit", use_container_width=True, disabled=not bool(open_position_state))
    finish_clicked = nav_cols[4].button("Go To End", use_container_width=True)

    utility_cols = st.columns(3)
    to_z2_clicked = utility_cols[0].button(f"Go To Back Inside {upper_band:+.2f}/{lower_band:+.2f}", use_container_width=True)
    reset_clicked = utility_cols[1].button("Reset Run", use_container_width=True)
    reset_cursor_clicked = utility_cols[2].button("Back To Split", use_container_width=True)

    if long_clicked:
        open_trade(
            game=game,
            pair_key=pair_key,
            side="long",
            row=current_row,
            beta=float(stats["beta"]),
            sigma=float(stats["sigma"]),
            y_col=y_col,
            x_col=x_col,
        )
        st.rerun()

    if short_clicked:
        open_trade(
            game=game,
            pair_key=pair_key,
            side="short",
            row=current_row,
            beta=float(stats["beta"]),
            sigma=float(stats["sigma"]),
            y_col=y_col,
            x_col=x_col,
        )
        st.rerun()

    if exit_clicked and open_position_state:
        close_trade(
            game=game,
            position=open_position_state,
            row=current_row,
            beta=float(stats["beta"]),
            alpha=float(stats["alpha"]),
            y_col=y_col,
            x_col=x_col,
            reason="MANUAL_EXIT",
        )
        st.rerun()

    if step_clicked:
        advance_game_cursor(
            game=game,
            frame=analysis_df,
            target_cursor_abs=clamp_int(int(game["cursor_abs"]) + int(reveal_step), split_idx, end_idx),
            beta=float(stats["beta"]),
            alpha=float(stats["alpha"]),
            y_col=y_col,
            x_col=x_col,
        )
        st.rerun()

    if step5_clicked:
        advance_game_cursor(
            game=game,
            frame=analysis_df,
            target_cursor_abs=clamp_int(int(game["cursor_abs"]) + 10, split_idx, end_idx),
            beta=float(stats["beta"]),
            alpha=float(stats["alpha"]),
            y_col=y_col,
            x_col=x_col,
        )
        st.rerun()

    if to_exit_clicked:
        fast_forward_to_exit(
            game=game,
            frame=analysis_df,
            beta=float(stats["beta"]),
            alpha=float(stats["alpha"]),
            y_col=y_col,
            x_col=x_col,
        )
        st.rerun()

    if finish_clicked:
        advance_game_cursor(
            game=game,
            frame=analysis_df,
            target_cursor_abs=end_idx,
            beta=float(stats["beta"]),
            alpha=float(stats["alpha"]),
            y_col=y_col,
            x_col=x_col,
        )
        st.rerun()

    if week_clicked:
        target_time = pd.Timestamp(current_row[TIME_COL]) + pd.Timedelta(days=7)
        raw_target_idx = int(analysis_df[TIME_COL].searchsorted(target_time, side="left"))
        target_idx = clamp_int(raw_target_idx, split_idx, end_idx)
        advance_game_cursor(
            game=game,
            frame=analysis_df,
            target_cursor_abs=target_idx,
            beta=float(stats["beta"]),
            alpha=float(stats["alpha"]),
            y_col=y_col,
            x_col=x_col,
        )
        st.rerun()

    if to_z2_clicked:
        fast_forward_to_z_band_reentry(
            game=game,
            frame=analysis_df,
            upper_band=float(upper_band),
            lower_band=float(lower_band),
            beta=float(stats["beta"]),
            alpha=float(stats["alpha"]),
            y_col=y_col,
            x_col=x_col,
        )
        st.rerun()

    if reset_clicked:
        st.session_state.backtest_game = fresh_game_state(split_idx, end_idx)
        st.rerun()

    if reset_cursor_clicked:
        game["cursor_abs"] = split_idx
        game["open_position"] = None
        st.rerun()

trade_df = pd.DataFrame(game["trades"])

summary_cols = st.columns(4)
summary_cols[0].metric("Trade Count", len(trade_df))
summary_cols[1].metric(
    "Win Rate",
    "n/a" if trade_df.empty else f"{(trade_df['return_pct'] > 0).mean() * 100:.1f}%",
)
summary_cols[2].metric(
    "Avg Trade",
    "n/a" if trade_df.empty else f"{trade_df['return_pct'].mean():+.2f}%",
)
summary_cols[3].metric(
    "Best Trade",
    "n/a" if trade_df.empty else f"{trade_df['return_pct'].max():+.2f}%",
)

st.subheader("Trade Log")
if trade_df.empty:
    st.write("还没有完成的交易。")
else:
    display_df = trade_df.rename(
        columns={
            "side": "方向",
            "entry_time": "入场时间",
            "exit_time": "出场时间",
            "entry_z": "入场Z",
            "exit_z": "出场Z",
            "hold_bars": "持有Bars",
            "peak_return_pct": "峰值收益%",
            "return_pct": "实现收益%",
            "exit_reason": "出场原因",
        }
    )
    st.dataframe(display_df, use_container_width=True, hide_index=True)

st.subheader("Rules")
st.markdown(
    """
- `beta / alpha / mu / spread sigma` 只用训练段 `train_start -> split` 估计。
- 顶部 `2σ Return` 对齐 Quant Dashboard：`2 * spread_sigma / (Y + |beta| * X) * 100`。
- 测试段从 `split` 开始逐 bar 揭示；未揭示的未来不会显示在主图里。
- `Long Spread` 代表做多 `Y - beta * X`，`Short Spread` 代表做空 `Y - beta * X`。因此 beta>0 时两腿反向；beta<0 时 Long Spread 两腿都做多、Short Spread 两腿都做空。
- 收益口径对齐 `flagship_board`：`return = pnl / notional * 100`。
- 止损/止盈口径对齐 `flagship_board + execution`：
  先算 `two_sigma_return = 2 * sigma / notional * 100`，
  再算 `sigma_for_stop = two_sigma_return / 2`，
  默认规则 `trailing(-0.5*sigma)` 最终变成 `stop = max_ret - 0.5 * sigma_for_stop`。
- 若 `program_factory/pair_exit_rules.json` 给该 pair 配了自定义 `stop_loss` / `take_profit`，小游戏也会按同一规则解释。
"""
)
