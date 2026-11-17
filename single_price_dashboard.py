import re
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pymysql
import streamlit as st
import streamlit.components.v1 as components
from program_factory.order_scheduling import create_single_scheduled_order


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
    cursorclass=pymysql.cursors.DictCursor,
)

TIME_COL = "time_bj"
IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
WHOLE_SHARE_BUDGET_USD = 15000.0


st.set_page_config(
    page_title="Single Price Dashboard",
    layout="wide",
)

st.markdown(
    """
    <style>
    section[data-testid="stSidebar"] div.stButton > button {
        background-color: #d7ecff;
        border: 1px solid #8ec5ff;
        color: #174a7c;
    }
    section[data-testid="stSidebar"] div.stButton > button:hover {
        background-color: #c4e3ff;
        border-color: #74b9ff;
        color: #123f68;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def validate_identifier(name: str, label: str) -> str:
    value = str(name).strip()
    if not IDENTIFIER_RE.match(value):
        raise ValueError(f"Invalid {label}: {name!r}")
    return value


def quote_identifier(name: str) -> str:
    return f"`{validate_identifier(name, 'identifier')}`"


def render_plotly_with_hover_spike(fig: go.Figure, height: int = 740):
    post_script = """
    const gd = document.getElementById('{plot_id}');
    if (!gd) return;

    const baseShapes = (gd.layout.shapes || []).map((shape) => Object.assign({}, shape));
    const hoverLine = (xValue) => ({
        type: 'line',
        xref: 'x',
        yref: 'paper',
        x0: xValue,
        x1: xValue,
        y0: 0,
        y1: 1,
        line: {
            color: '#6c757d',
            width: 1,
            dash: 'dot'
        },
        layer: 'above'
    });

    gd.on('plotly_hover', (eventData) => {
        if (!eventData || !eventData.points || !eventData.points.length) return;
        const xValue = eventData.points[0].x;
        Plotly.relayout(gd, { shapes: baseShapes.concat([hoverLine(xValue)]) });
    });

    gd.on('plotly_unhover', () => {
        Plotly.relayout(gd, { shapes: baseShapes });
    });
    """
    html = fig.to_html(
        include_plotlyjs=True,
        full_html=False,
        config={"responsive": True, "displayModeBar": True},
        post_script=post_script,
    )
    components.html(html, height=height, scrolling=False)


def to_mysql_datetime(value):
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).to_pydatetime().replace(tzinfo=None)


def parse_sidebar_datetime(value, fallback):
    try:
        parsed = pd.Timestamp(str(value).strip())
        if pd.isna(parsed):
            return pd.Timestamp(fallback)
        return parsed
    except Exception:
        return pd.Timestamp(fallback)


def load_log_residual_config(stock_name: str) -> Optional[Dict[str, object]]:
    normalized_stock = str(stock_name or "").strip().upper()
    if not normalized_stock:
        return None

    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    stock_name,
                    train_start,
                    split_time,
                    alpha,
                    intercept,
                    k,
                    threshold,
                    trailing_stop,
                    created_time
                FROM log_residual_config
                WHERE UPPER(stock_name) = %s
                LIMIT 1
                """,
                (normalized_stock,),
            )
            return cursor.fetchone()
    finally:
        conn.close()


def save_log_residual_config(
    stock_name: str,
    train_start,
    split_time,
    alpha: float,
    intercept: float,
    k: float,
    threshold: float,
    trailing_stop: float,
) -> None:
    normalized_stock = str(stock_name or "").strip().upper()
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO log_residual_config
                    (stock_name, train_start, split_time, alpha, intercept, k, threshold, trailing_stop)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    train_start = VALUES(train_start),
                    split_time = VALUES(split_time),
                    alpha = VALUES(alpha),
                    intercept = VALUES(intercept),
                    k = VALUES(k),
                    threshold = VALUES(threshold),
                    trailing_stop = VALUES(trailing_stop)
                """,
                (
                    normalized_stock,
                    to_mysql_datetime(train_start),
                    to_mysql_datetime(split_time),
                    round(float(alpha), 4),
                    round(float(intercept), 4),
                    round(float(k), 6),
                    round(float(threshold), 4),
                    round(float(trailing_stop), 2),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def create_log_residual_waiting_entry(
    stock_name: str,
    train_start,
    split_time,
    strategy: str,
    param: float,
    intercept: float,
    residual: float,
    auto_schedule_enabled: bool = False,
    exposure_pct: float | None = None,
    order_position_type: str | None = None,
) -> Dict[str, object]:
    stored_stock_name = str(stock_name or "").strip().lower()
    if not stored_stock_name:
        raise ValueError("stock_name is required")

    rounded_residual = round(float(residual), 4)
    auto_enabled = 1 if auto_schedule_enabled else 0
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO pair_reentry
                    (
                        pair_name,
                        train_start,
                        split_time,
                        strategy,
                        param,
                        intercept,
                        residual,
                        reentry_lock,
                        threshold,
                        auto_schedule_enabled,
                        exposure_pct,
                        order_position_type
                    )
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, 2, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    train_start = VALUES(train_start),
                    split_time = VALUES(split_time),
                    strategy = VALUES(strategy),
                    param = VALUES(param),
                    intercept = VALUES(intercept),
                    residual = VALUES(residual),
                    reentry_lock = VALUES(reentry_lock),
                    threshold = VALUES(threshold),
                    auto_schedule_enabled = VALUES(auto_schedule_enabled),
                    exposure_pct = VALUES(exposure_pct),
                    order_position_type = VALUES(order_position_type)
                """,
                (
                    stored_stock_name,
                    to_mysql_datetime(train_start),
                    to_mysql_datetime(split_time),
                    strategy,
                    round(float(param), 6),
                    round(float(intercept), 4),
                    rounded_residual,
                    rounded_residual,
                    auto_enabled,
                    round(float(exposure_pct), 2) if auto_enabled and exposure_pct is not None else None,
                    str(order_position_type or "").strip().upper() if auto_enabled else None,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "success": True,
        "pair_name": stored_stock_name,
        "reentry_lock": 2,
        "auto_schedule_enabled": auto_enabled,
    }


def create_new_high_waiting_entry(
    stock_name: str,
    train_start,
    split_time,
    previous_high: float,
    auto_schedule_enabled: bool = False,
    exposure_pct: float | None = None,
    order_position_type: str | None = None,
) -> Dict[str, object]:
    stored_stock_name = str(stock_name or "").strip().lower()
    if not stored_stock_name:
        raise ValueError("stock_name is required")

    trigger_price = round(float(previous_high), 4)
    auto_enabled = 1 if auto_schedule_enabled else 0
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO pair_reentry
                    (
                        pair_name,
                        train_start,
                        split_time,
                        strategy,
                        param,
                        intercept,
                        residual,
                        reentry_lock,
                        threshold,
                        auto_schedule_enabled,
                        exposure_pct,
                        order_position_type
                    )
                VALUES
                    (%s, %s, %s, 'NEW_HIGH', %s, NULL, NULL, 2, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    train_start = VALUES(train_start),
                    split_time = VALUES(split_time),
                    strategy = VALUES(strategy),
                    param = VALUES(param),
                    intercept = VALUES(intercept),
                    residual = VALUES(residual),
                    reentry_lock = VALUES(reentry_lock),
                    threshold = VALUES(threshold),
                    auto_schedule_enabled = VALUES(auto_schedule_enabled),
                    exposure_pct = VALUES(exposure_pct),
                    order_position_type = VALUES(order_position_type)
                """,
                (
                    stored_stock_name,
                    to_mysql_datetime(train_start),
                    to_mysql_datetime(split_time),
                    trigger_price,
                    trigger_price,
                    auto_enabled,
                    round(float(exposure_pct), 2) if auto_enabled and exposure_pct is not None else None,
                    str(order_position_type or "").strip().upper() if auto_enabled else None,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "success": True,
        "pair_name": stored_stock_name,
        "reentry_lock": 2,
        "trigger_price": trigger_price,
        "auto_schedule_enabled": auto_enabled,
    }


def create_drawdown_repair_scheduled_order(
    single_name: str,
    train_start,
    split_time,
    trigger_price: float,
    position_type: str,
    exposure_pct: float,
) -> Dict[str, object]:
    stored_single_name = str(single_name or "").strip().lower()
    if not stored_single_name:
        raise ValueError("single_name is required")

    normalized_position_type = str(position_type or "").strip().upper()
    if normalized_position_type != "LONG_SPREAD":
        raise ValueError("Drawdown repair currently supports LONG_SPREAD only.")

    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO single_scheduled_orders
                    (
                        single_name,
                        train_start,
                        split_time,
                        strategy,
                        param,
                        position_type,
                        exposure_pct,
                        status
                    )
                VALUES
                    (%s, %s, %s, 'DRAWDOWN_REPAIR', %s, %s, %s, 'PENDING')
                """,
                (
                    stored_single_name,
                    to_mysql_datetime(train_start),
                    to_mysql_datetime(split_time),
                    round(float(trigger_price), 4),
                    normalized_position_type,
                    round(float(exposure_pct), 2),
                ),
            )
            order_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    return {
        "success": True,
        "order_id": int(order_id),
        "single_name": stored_single_name,
        "strategy": "DRAWDOWN_REPAIR",
        "position_type": normalized_position_type,
    }


def create_drawdown_repair_waiting_entry(
    stock_name: str,
    train_start,
    split_time,
    peak_price: float,
    trough_price: float,
    repair_ratio: float,
    trigger_price: float,
    auto_schedule_enabled: bool = False,
    exposure_pct: float | None = None,
    order_position_type: str | None = None,
) -> Dict[str, object]:
    stored_stock_name = str(stock_name or "").strip().lower()
    if not stored_stock_name:
        raise ValueError("stock_name is required")

    normalized_position_type = str(order_position_type or "LONG_SPREAD").strip().upper()
    if normalized_position_type != "LONG_SPREAD":
        raise ValueError("Drawdown repair currently supports LONG_SPREAD only.")

    auto_enabled = 1 if auto_schedule_enabled else 0
    rounded_peak = round(float(peak_price), 4)
    rounded_trough = round(float(trough_price), 4)
    rounded_ratio = round(float(repair_ratio), 6)
    rounded_trigger = round(float(trigger_price), 4)

    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO pair_reentry
                    (
                        pair_name,
                        train_start,
                        split_time,
                        strategy,
                        param,
                        intercept,
                        residual,
                        reentry_lock,
                        threshold,
                        auto_schedule_enabled,
                        exposure_pct,
                        order_position_type
                    )
                VALUES
                    (%s, %s, %s, 'DRAWDOWN_REPAIR', %s, %s, %s, 2, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    train_start = VALUES(train_start),
                    split_time = VALUES(split_time),
                    strategy = VALUES(strategy),
                    param = VALUES(param),
                    intercept = VALUES(intercept),
                    residual = VALUES(residual),
                    reentry_lock = VALUES(reentry_lock),
                    threshold = VALUES(threshold),
                    auto_schedule_enabled = VALUES(auto_schedule_enabled),
                    exposure_pct = VALUES(exposure_pct),
                    order_position_type = VALUES(order_position_type)
                """,
                (
                    stored_stock_name,
                    to_mysql_datetime(train_start),
                    to_mysql_datetime(split_time),
                    rounded_peak,
                    rounded_trough,
                    rounded_ratio,
                    rounded_trigger,
                    auto_enabled,
                    round(float(exposure_pct), 2) if auto_enabled and exposure_pct is not None else None,
                    normalized_position_type if auto_enabled else None,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "success": True,
        "pair_name": stored_stock_name,
        "reentry_lock": 2,
        "peak_price": rounded_peak,
        "trough_price": rounded_trough,
        "repair_ratio": rounded_ratio,
        "trigger_price": rounded_trigger,
        "auto_schedule_enabled": auto_enabled,
    }


@st.cache_data(ttl=60)
def load_price_sources() -> pd.DataFrame:
    conn = pymysql.connect(**DB_CONFIG)

    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT ticker, exchange, currency
            FROM us_info
            ORDER BY ticker
            """
        )
        rows = [
            {
                "ticker": str(ticker).strip().upper(),
                "exchange": str(exchange or "SMART").strip(),
                "currency": str(currency or "USD").strip(),
                "table_name": "us_market_prices",
                "price_col": "price",
                "source_kind": "registered",
                "has_volume": False,
            }
            for ticker, exchange, currency in cursor.fetchall()
            if ticker
        ]
    finally:
        conn.close()

    return pd.DataFrame(rows)


@st.cache_data(ttl=30)
def load_price_history(ticker: str) -> pd.DataFrame:
    conn = pymysql.connect(**DB_CONFIG)

    try:
        query = """
        SELECT
            time_utc,
            price,
            NULL AS volume,
            source
        FROM us_market_prices
        WHERE ticker = %s
          AND price IS NOT NULL
        ORDER BY time_utc
        """
        df = pd.read_sql(query, conn, params=(ticker,))
    finally:
        conn.close()

    if df.empty:
        return df

    df["time_utc"] = pd.to_datetime(df["time_utc"])
    df[TIME_COL] = df["time_utc"] + pd.Timedelta(hours=8)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df = df.sort_values("time_utc").dropna(subset=["price"]).reset_index(drop=True)
    return df


def derive_window_start(latest_time: pd.Timestamp, window_key: str) -> pd.Timestamp:
    if window_key == "1M":
        return latest_time - pd.Timedelta(days=30)
    if window_key == "3M":
        return latest_time - pd.Timedelta(days=90)
    if window_key == "6M":
        return latest_time - pd.Timedelta(days=180)
    if window_key == "1Y":
        return latest_time - pd.Timedelta(days=365)
    if window_key == "2Y":
        return latest_time - pd.Timedelta(days=730)
    return pd.Timestamp("1900-01-01")


def resample_prices(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if df.empty or rule == "Raw":
        return df

    agg_map = {"price": "last"}
    if "source_bar_index" in df.columns:
        agg_map["source_bar_index"] = "last"
    if "volume" in df.columns:
        agg_map["volume"] = "sum"

    sampled = df.set_index(TIME_COL).resample(rule).agg(agg_map).dropna(subset=["price"]).reset_index()
    return sampled


def prepare_trading_axis(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy().reset_index(drop=True)
    out["plot_x"] = np.arange(len(out))
    if "time_ny" in out.columns:
        session_time = pd.to_datetime(out["time_ny"])
    else:
        session_time = pd.to_datetime(out[TIME_COL]) - pd.Timedelta(hours=12)
    out["session_date"] = session_time.dt.strftime("%Y-%m-%d")
    out["trade_date"] = out["session_date"]
    out["time_str"] = out[TIME_COL].dt.strftime("%Y-%m-%d %H:%M")
    return out


def build_trading_day_ticks(df: pd.DataFrame, max_labels: int = 10) -> tuple[list[int], list[str]]:
    if df.empty:
        return [], []

    day_starts = (
        df.groupby("trade_date", sort=False)["plot_x"]
        .first()
        .reset_index()
    )
    if day_starts.empty:
        return [], []

    total_days = len(day_starts)
    step = max(1, int(np.ceil(total_days / max_labels)))
    sampled = day_starts.iloc[::step].copy()

    last_row = day_starts.iloc[[-1]]
    if int(sampled["plot_x"].iloc[-1]) != int(last_row["plot_x"].iloc[0]):
        sampled = pd.concat([sampled, last_row], ignore_index=True)

    return sampled["plot_x"].astype(int).tolist(), sampled["trade_date"].tolist()


def add_alternating_day_background(
    fig: go.Figure,
    plot_df: pd.DataFrame,
    row: int = 1,
    col: int = 1,
):
    if plot_df.empty or "trade_date" not in plot_df.columns or "hover_x" not in plot_df.columns:
        return

    day_spans = (
        plot_df.groupby("trade_date", sort=False)
        .agg(start=("hover_x", "first"), end=("hover_x", "last"))
        .reset_index()
    )
    colors = ("rgba(255, 243, 205, 0.28)", "rgba(210, 244, 222, 0.24)")

    for idx, row_data in day_spans.iterrows():
        fig.add_vrect(
            x0=row_data["start"],
            x1=row_data["end"],
            fillcolor=colors[idx % len(colors)],
            opacity=1.0,
            line_width=0,
            layer="below",
            row=row,
            col=col,
        )


def build_price_chart(
    df: pd.DataFrame,
    ticker: str,
    table_name: str,
    show_ma20: bool,
    show_ma50: bool,
    log_scale: bool,
) -> go.Figure:
    fig = go.Figure()

    fig.add_trace(
        go.Scatter(
            x=df["plot_x"],
            y=df["price"],
            mode="lines",
            name=ticker,
            line=dict(color="#1f5fbf", width=2),
            customdata=df["time_str"],
            hovertemplate="%{customdata}<br>price=%{y:.4f}<extra></extra>",
        )
    )

    if show_ma20 and len(df) >= 20:
        fig.add_trace(
            go.Scatter(
                x=df["plot_x"],
                y=df["price"].rolling(20).mean(),
                mode="lines",
                name="MA20",
                line=dict(color="#2f9e44", width=1.4),
                customdata=df["time_str"],
                hovertemplate="%{customdata}<br>MA20=%{y:.4f}<extra></extra>",
            )
        )

    if show_ma50 and len(df) >= 50:
        fig.add_trace(
            go.Scatter(
                x=df["plot_x"],
                y=df["price"].rolling(50).mean(),
                mode="lines",
                name="MA50",
                line=dict(color="#f08c00", width=1.4),
                customdata=df["time_str"],
                hovertemplate="%{customdata}<br>MA50=%{y:.4f}<extra></extra>",
            )
        )

    tickvals, ticktext = build_trading_day_ticks(df)

    fig.update_layout(
        title=f"{ticker} Price History",
        template="plotly_white",
        hovermode="x unified",
        margin=dict(l=24, r=24, t=56, b=24),
        height=560,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1.0),
    )
    fig.update_xaxes(
        title_text="Trading Days",
        rangeslider_visible=False,
        showgrid=True,
        gridcolor="#eceff4",
        tickmode="array",
        tickvals=tickvals,
        ticktext=ticktext,
    )
    fig.update_yaxes(
        title_text="Price",
        type="log" if log_scale else "linear",
        showgrid=True,
        gridcolor="#eceff4",
    )
    fig.add_annotation(
        xref="paper",
        yref="paper",
        x=1.0,
        y=1.12,
        showarrow=False,
        text=f"Source: {table_name}",
        font=dict(size=12, color="#5c6773"),
    )
    return fig


def compute_max_single_day_drop_pct(df: pd.DataFrame) -> float:
    return float(compute_max_single_day_drop_info(df).get("drop_pct", 0.0))


def compute_max_single_day_drop_info(df: pd.DataFrame) -> Dict[str, object]:
    if df.empty:
        return {"drop_pct": 0.0}

    work = df[[TIME_COL, "price"]].copy().dropna()
    work[TIME_COL] = pd.to_datetime(work[TIME_COL])
    work["price"] = pd.to_numeric(work["price"], errors="coerce")
    work = work.dropna(subset=["price"]).sort_values(TIME_COL)
    if len(work) < 2:
        return {"drop_pct": 0.0}

    daily_close_rows = work.groupby(work[TIME_COL].dt.date, sort=True).tail(1).reset_index(drop=True)
    if len(daily_close_rows) < 2:
        return {"drop_pct": 0.0}

    daily_returns = daily_close_rows["price"].pct_change()
    if daily_returns.empty:
        return {"drop_pct": 0.0}

    max_drop = float(daily_returns.min())
    if max_drop >= 0:
        return {"drop_pct": 0.0}

    trough_pos = int(daily_returns.idxmin())
    prev_pos = max(0, trough_pos - 1)
    trough_row = daily_close_rows.iloc[trough_pos]
    prev_row = daily_close_rows.iloc[prev_pos]
    return {
        "drop_pct": abs(max_drop) * 100.0,
        "trough_time": pd.Timestamp(trough_row[TIME_COL]),
        "trough_price": float(trough_row["price"]),
        "prev_close_time": pd.Timestamp(prev_row[TIME_COL]),
        "prev_close_price": float(prev_row["price"]),
    }


def compute_max_pullback_after_new_high_pct(df: pd.DataFrame) -> float:
    return float(compute_max_pullback_after_new_high_info(df).get("pullback_pct", 0.0))


def compute_max_pullback_after_new_high_info(df: pd.DataFrame) -> Dict[str, object]:
    if df.empty or "price" not in df.columns:
        return {"pullback_pct": 0.0}

    work = df[[TIME_COL, "price"]].copy().dropna()
    work[TIME_COL] = pd.to_datetime(work[TIME_COL])
    work["price"] = pd.to_numeric(work["price"], errors="coerce")
    work = work.dropna(subset=["price"]).sort_values(TIME_COL).reset_index(drop=True)
    work = work[work["price"] > 0].reset_index(drop=True)
    if len(work) < 2:
        return {"pullback_pct": 0.0}

    best = {
        "pullback_pct": 0.0,
    }
    peak_pos = 0
    peak_price = float(work.at[peak_pos, "price"])
    trough_pos = 0
    trough_price = peak_price

    def close_segment(end_pos: int):
        nonlocal best, peak_pos, peak_price, trough_pos, trough_price
        if end_pos <= peak_pos or peak_price <= 0:
            return
        pullback_pct = (1.0 - trough_price / peak_price) * 100.0
        if pullback_pct > float(best.get("pullback_pct", 0.0)):
            peak_row = work.iloc[peak_pos]
            trough_row = work.iloc[trough_pos]
            best = {
                "pullback_pct": pullback_pct,
                "trough_time": pd.Timestamp(trough_row[TIME_COL]),
                "trough_price": float(trough_row["price"]),
                "peak_time": pd.Timestamp(peak_row[TIME_COL]),
                "peak_price": float(peak_row["price"]),
                "segment_end_time": pd.Timestamp(work.iloc[end_pos][TIME_COL]),
            }

    for pos in range(1, len(work)):
        price = float(work.at[pos, "price"])
        if price > peak_price:
            close_segment(pos - 1)
            peak_pos = pos
            peak_price = price
            trough_pos = pos
            trough_price = price
            continue

        if price < trough_price:
            trough_pos = pos
            trough_price = price

    close_segment(len(work) - 1)
    if float(best.get("pullback_pct", 0.0)) <= 0:
        return {"pullback_pct": 0.0}
    return best


def run_breakout_trailing_backtest(
    df: pd.DataFrame,
    initial_cash: float = WHOLE_SHARE_BUDGET_USD,
    trailing_stop_pct: float = 0.02,
    train_ratio: float = 0.35,
    trend_mode: str = "train_fit",
    manual_slope: Optional[float] = None,
    intercept_shift: float = 0.0,
    entry_residual_threshold: float = 0.0,
    entry_residual_mean_window: int = 78,
    fit_start=None,
    fit_end=None,
    position_type: str = "LONG_SPREAD",
) -> Dict[str, object]:
    position_type = str(position_type or "LONG_SPREAD").strip().upper()
    is_short_strategy = position_type == "SHORT_SPREAD"
    empty_result = {
        "trades": [],
        "equity_curve": pd.DataFrame(),
        "strategy_frame": pd.DataFrame(),
        "final_equity": float(initial_cash),
        "total_return_pct": 0.0,
        "buy_hold_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "win_rate_pct": 0.0,
        "trade_count": 0,
        "train_size": 0,
        "train_ratio": float(train_ratio),
        "residual_mu": 0.0,
        "residual_sigma": 0.0,
        "used_slope": 0.0,
        "used_intercept": 0.0,
        "intercept_shift": float(intercept_shift),
        "entry_residual": float(entry_residual_threshold),
        "entry_residual_mean_window": int(entry_residual_mean_window),
        "position_type": position_type,
        "trend_mode": trend_mode,
    }
    if df.empty:
        return empty_result

    work_cols = [TIME_COL, "price"]
    if "source_bar_index" in df.columns:
        work_cols.append("source_bar_index")
    work = df[work_cols].copy().dropna(subset=[TIME_COL, "price"]).reset_index(drop=True)
    if len(work) < 8 or (work["price"] <= 0).any():
        return empty_result

    total_bars = len(work)
    if "source_bar_index" in work.columns:
        idx = pd.to_numeric(work["source_bar_index"], errors="coerce").to_numpy(dtype=float)
        if not np.isfinite(idx).all():
            idx = np.arange(total_bars, dtype=float)
    else:
        idx = np.arange(total_bars, dtype=float)
    log_price = np.log(work["price"].astype(float).to_numpy())

    fit_mask = pd.Series(True, index=work.index)
    fit_start_ts = None if fit_start is None or pd.isna(fit_start) else pd.Timestamp(fit_start)
    fit_end_ts = None if fit_end is None or pd.isna(fit_end) else pd.Timestamp(fit_end)
    if fit_start_ts is not None:
        fit_mask &= pd.to_datetime(work[TIME_COL]) >= fit_start_ts
    if fit_end_ts is not None:
        fit_mask &= pd.to_datetime(work[TIME_COL]) <= fit_end_ts
    if int(fit_mask.sum()) < 2:
        fit_mask = pd.Series(True, index=work.index)

    fit_idx = idx[fit_mask.to_numpy()]
    fit_log_price = log_price[fit_mask.to_numpy()]

    if trend_mode == "full_fit":
        train_size = 0
        slope, intercept = np.polyfit(fit_idx, fit_log_price, 1) if len(fit_idx) >= 2 else (0.0, float(log_price[0]))
        log_trend = intercept + float(intercept_shift) + slope * idx
    elif trend_mode == "fixed_k_fit_intercept":
        train_size = 0
        slope = float(manual_slope or 0.0)
        intercept = float(np.mean(fit_log_price - slope * fit_idx)) if len(fit_idx) else float(log_price[0])
        log_trend = intercept + float(intercept_shift) + slope * idx
    elif trend_mode == "manual_k":
        train_size = 0
        slope = float(manual_slope or 0.0)
        intercept = float(log_price[0])
        log_trend = intercept + float(intercept_shift) + slope * idx
    else:
        raw_train_size = int(np.floor(total_bars * float(train_ratio)))
        train_size = max(5, min(raw_train_size, total_bars - 2))
        slope, intercept = np.polyfit(idx[:train_size], log_price[:train_size], 1)
        log_trend = intercept + float(intercept_shift) + slope * idx

    used_intercept = float(intercept) + float(intercept_shift)
    residual = log_price - log_trend
    trend_price = np.exp(log_trend)

    work["log_price"] = log_price
    work["trend_log_price"] = log_trend
    work["trend_price"] = trend_price
    work["residual"] = residual
    work["entry_residual_threshold"] = float(entry_residual_threshold)
    residual_mean_window = max(1, int(entry_residual_mean_window))
    work["entry_residual_mean"] = work["residual"].shift(1).rolling(residual_mean_window, min_periods=residual_mean_window).mean()
    work["phase"] = np.where(np.arange(total_bars) < train_size, "train", "test") if train_size > 0 else "test"

    train_residual = work["residual"].iloc[:train_size]
    residual_mu = float(train_residual.mean()) if not train_residual.empty else 0.0
    residual_sigma = float(train_residual.std(ddof=0)) if len(train_residual) > 1 else 0.0
    if residual_sigma > 0:
        work["residual_z"] = (work["residual"] - residual_mu) / residual_sigma
    else:
        work["residual_z"] = 0.0

    if trend_mode == "manual_k":
        test_df = work.copy().reset_index(drop=True)
    else:
        test_df = work.iloc[train_size:].copy().reset_index(drop=True)

    if test_df.empty:
        result = dict(empty_result)
        result.update(
            {
                "strategy_frame": work,
                "train_size": train_size,
                "train_ratio": float(train_size / total_bars),
                "residual_mu": residual_mu,
                "residual_sigma": residual_sigma,
                "used_slope": float(slope),
                "used_intercept": used_intercept,
                "intercept_shift": float(intercept_shift),
                "entry_residual": float(entry_residual_threshold),
                "entry_residual_mean_window": residual_mean_window,
                "position_type": position_type,
                "trend_mode": trend_mode,
            }
        )
        return result

    cash = float(initial_cash)
    shares = 0
    entry_price: Optional[float] = None
    entry_time = None
    entry_residual: Optional[float] = None
    best_price_since_entry: Optional[float] = None
    previous_residual: Optional[float] = None
    trades: List[Dict[str, object]] = []
    equity_rows: List[Dict[str, object]] = []

    for ts_value, price_value, residual_value, residual_mean_value in zip(
        test_df[TIME_COL].to_numpy(),
        test_df["price"].to_numpy(),
        test_df["residual"].to_numpy(),
        test_df["entry_residual_mean"].to_numpy(),
    ):
        ts = pd.Timestamp(ts_value)
        price = float(price_value)
        current_residual = float(residual_value)
        prior_residual_mean = float(residual_mean_value) if pd.notna(residual_mean_value) else np.nan

        if shares != 0:
            if is_short_strategy:
                best_price_since_entry = price if best_price_since_entry is None else min(best_price_since_entry, price)
                stop_price = best_price_since_entry * (1.0 + trailing_stop_pct)
                stop_hit = price >= stop_price
            else:
                best_price_since_entry = price if best_price_since_entry is None else max(best_price_since_entry, price)
                stop_price = best_price_since_entry * (1.0 - trailing_stop_pct)
                stop_hit = price <= stop_price
            if stop_hit:
                exit_value = shares * price
                cash += exit_value
                pnl = (price - float(entry_price)) * shares
                ret_pct = (
                    0.0
                    if entry_price in (None, 0)
                    else ((price / float(entry_price) - 1.0) * 100.0 * (1 if shares > 0 else -1))
                )
                trades.append(
                    {
                        "entry_time": entry_time,
                        "exit_time": ts,
                        "entry_price": float(entry_price),
                        "exit_price": price,
                        "entry_residual": float(entry_residual) if entry_residual is not None else np.nan,
                        "exit_residual": current_residual,
                        "shares": shares,
                        "pnl": pnl,
                        "return_pct": ret_pct,
                        "exit_reason": "TRAIL_STOP",
                    }
                )
                shares = 0
                entry_price = None
                entry_time = None
                entry_residual = None
                best_price_since_entry = None

        crossed_entry = (
            previous_residual is not None
            and (
                (
                    not is_short_strategy
                    and previous_residual < float(entry_residual_threshold)
                    and current_residual >= float(entry_residual_threshold)
                )
                or (
                    is_short_strategy
                    and previous_residual > float(entry_residual_threshold)
                    and current_residual <= float(entry_residual_threshold)
                )
            )
        )
        residual_mean_ok = (
            pd.notna(prior_residual_mean)
            and (
                prior_residual_mean > float(entry_residual_threshold)
                if is_short_strategy
                else prior_residual_mean < float(entry_residual_threshold)
            )
        )
        if shares == 0 and crossed_entry and residual_mean_ok:
            entry_shares = int(cash // price)
            if entry_shares > 0:
                shares = -entry_shares if is_short_strategy else entry_shares
                cash -= shares * price
                entry_price = price
                entry_time = ts
                entry_residual = current_residual
                best_price_since_entry = price

        previous_residual = current_residual
        equity = cash + shares * price
        equity_rows.append(
            {
                TIME_COL: ts,
                "equity": equity,
                "cash": cash,
                "shares": shares,
                "price": price,
                "residual": current_residual,
                "entry_residual_mean": prior_residual_mean,
            }
        )

    last_price = float(test_df["price"].iloc[-1])
    last_time = pd.Timestamp(test_df[TIME_COL].iloc[-1])
    last_residual = float(test_df["residual"].iloc[-1])
    if shares != 0 and entry_price is not None and entry_time is not None:
        exit_value = shares * last_price
        cash += exit_value
        pnl = (last_price - float(entry_price)) * shares
        ret_pct = (
            0.0
            if entry_price == 0
            else ((last_price / float(entry_price) - 1.0) * 100.0 * (1 if shares > 0 else -1))
        )
        trades.append(
            {
                "entry_time": entry_time,
                "exit_time": last_time,
                "entry_price": float(entry_price),
                "exit_price": last_price,
                "entry_residual": float(entry_residual) if entry_residual is not None else np.nan,
                "exit_residual": last_residual,
                "shares": shares,
                "pnl": pnl,
                "return_pct": ret_pct,
                "exit_reason": "END_OF_TEST",
            }
        )
        if equity_rows:
            equity_rows[-1]["equity"] = cash
            equity_rows[-1]["cash"] = cash
            equity_rows[-1]["shares"] = 0
        shares = 0

    equity_curve = pd.DataFrame(equity_rows)
    if not equity_curve.empty:
        equity_curve["peak_equity"] = equity_curve["equity"].cummax()
        equity_curve["drawdown_pct"] = np.where(
            equity_curve["peak_equity"] > 0,
            (equity_curve["equity"] / equity_curve["peak_equity"] - 1.0) * 100.0,
            0.0,
        )

    final_equity = float(cash)
    total_return_pct = 0.0 if initial_cash <= 0 else ((final_equity / initial_cash) - 1.0) * 100.0
    test_first_price = float(test_df["price"].iloc[0])
    buy_hold_return_pct = 0.0 if test_first_price <= 0 else ((last_price / test_first_price) - 1.0) * 100.0
    max_drawdown_pct = float(equity_curve["drawdown_pct"].min()) if not equity_curve.empty else 0.0
    win_count = sum(1 for trade in trades if float(trade["pnl"]) > 0)
    win_rate_pct = (win_count / len(trades) * 100.0) if trades else 0.0

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "strategy_frame": work,
        "final_equity": final_equity,
        "total_return_pct": total_return_pct,
        "buy_hold_return_pct": buy_hold_return_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "win_rate_pct": win_rate_pct,
        "trade_count": len(trades),
        "train_size": train_size,
        "train_ratio": float(train_size / total_bars),
        "residual_mu": residual_mu,
        "residual_sigma": residual_sigma,
        "used_slope": float(slope),
        "used_intercept": used_intercept,
        "intercept_shift": float(intercept_shift),
        "entry_residual": float(entry_residual_threshold),
        "entry_residual_mean_window": residual_mean_window,
        "position_type": position_type,
        "trend_mode": trend_mode,
    }


def compute_waiting_entry_intercept(
    df: pd.DataFrame,
    train_start,
    split_time,
    k: float,
    alpha: float,
) -> float:
    work = df[[TIME_COL, "price"]].copy().dropna().sort_values(TIME_COL).reset_index(drop=True)
    if work.empty:
        raise ValueError("no raw prices available for waiting-entry intercept")

    work[TIME_COL] = pd.to_datetime(work[TIME_COL])
    work["price"] = pd.to_numeric(work["price"], errors="coerce")
    work = work.dropna(subset=["price"])
    work = work[work["price"] > 0]

    train_start_ts = pd.Timestamp(train_start)
    split_time_ts = pd.Timestamp(split_time)
    work = work[work[TIME_COL] >= train_start_ts].reset_index(drop=True)
    if work.empty:
        raise ValueError("no raw prices after train_start for waiting-entry intercept")

    fit_mask = work[TIME_COL] <= split_time_ts
    if int(fit_mask.sum()) < 2:
        raise ValueError("not enough raw fit bars for waiting-entry intercept")

    idx = np.arange(len(work), dtype=float)
    fit_idx = idx[fit_mask.to_numpy()]
    fit_log_price = np.log(work.loc[fit_mask, "price"].astype(float).to_numpy())
    base_intercept = float(np.mean(fit_log_price - float(k) * fit_idx))
    return base_intercept + float(alpha)


def run_macd_backtest(
    df: pd.DataFrame,
    initial_cash: float = WHOLE_SHARE_BUDGET_USD,
    fast_span: int = 12,
    slow_span: int = 26,
    signal_span: int = 9,
    use_trailing_stop: bool = True,
    trailing_stop_pct: float = 0.02,
) -> Dict[str, object]:
    empty_result = {
        "trades": [],
        "equity_curve": pd.DataFrame(),
        "strategy_frame": pd.DataFrame(),
        "final_equity": float(initial_cash),
        "total_return_pct": 0.0,
        "buy_hold_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "win_rate_pct": 0.0,
        "trade_count": 0,
        "train_size": 0,
        "train_ratio": 0.0,
        "residual_mu": 0.0,
        "residual_sigma": 0.0,
        "used_slope": 0.0,
        "trend_mode": "macd",
    }
    if df.empty:
        return empty_result

    work = df[[TIME_COL, "price"]].copy().dropna().reset_index(drop=True)
    if len(work) < max(slow_span, signal_span) + 3 or (work["price"] <= 0).any():
        return empty_result

    fast_span = max(2, int(fast_span))
    slow_span = max(fast_span + 1, int(slow_span))
    signal_span = max(2, int(signal_span))

    price = work["price"].astype(float)
    work["ema_fast"] = price.ewm(span=fast_span, adjust=False).mean()
    work["ema_slow"] = price.ewm(span=slow_span, adjust=False).mean()
    work["macd"] = work["ema_fast"] - work["ema_slow"]
    work["macd_signal"] = work["macd"].ewm(span=signal_span, adjust=False).mean()
    work["macd_hist"] = work["macd"] - work["macd_signal"]
    work["phase"] = "test"

    cash = float(initial_cash)
    shares = 0
    entry_price: Optional[float] = None
    entry_time = None
    entry_macd: Optional[float] = None
    peak_price_since_entry: Optional[float] = None
    trades: List[Dict[str, object]] = []
    equity_rows: List[Dict[str, object]] = []

    for i, row in work.iterrows():
        ts = pd.Timestamp(row[TIME_COL])
        current_price = float(row["price"])
        macd_value = float(row["macd"])
        signal_value = float(row["macd_signal"])
        hist_value = float(row["macd_hist"])
        prev_hist = float(work["macd_hist"].iloc[i - 1]) if i > 0 else np.nan

        cross_up = i > 0 and prev_hist <= 0 and hist_value > 0
        cross_down = i > 0 and prev_hist >= 0 and hist_value < 0
        exit_reason = None

        if shares > 0:
            peak_price_since_entry = (
                current_price
                if peak_price_since_entry is None
                else max(peak_price_since_entry, current_price)
            )
            if cross_down:
                exit_reason = "MACD_CROSS_DOWN"
            elif use_trailing_stop and peak_price_since_entry is not None:
                stop_price = peak_price_since_entry * (1.0 - trailing_stop_pct)
                if current_price <= stop_price:
                    exit_reason = "TRAIL_STOP"

            if exit_reason:
                cash += shares * current_price
                pnl = (current_price - float(entry_price)) * shares
                ret_pct = 0.0 if entry_price in (None, 0) else ((current_price / float(entry_price)) - 1.0) * 100.0
                trades.append(
                    {
                        "entry_time": entry_time,
                        "exit_time": ts,
                        "entry_price": float(entry_price),
                        "exit_price": current_price,
                        "entry_residual": float(entry_macd) if entry_macd is not None else np.nan,
                        "exit_residual": macd_value,
                        "shares": shares,
                        "pnl": pnl,
                        "return_pct": ret_pct,
                        "exit_reason": exit_reason,
                    }
                )
                shares = 0
                entry_price = None
                entry_time = None
                entry_macd = None
                peak_price_since_entry = None

        if shares == 0 and cross_up:
            buy_shares = int(cash // current_price)
            if buy_shares > 0:
                cash -= buy_shares * current_price
                shares = buy_shares
                entry_price = current_price
                entry_time = ts
                entry_macd = macd_value
                peak_price_since_entry = current_price

        equity_rows.append(
            {
                TIME_COL: ts,
                "equity": cash + shares * current_price,
                "cash": cash,
                "shares": shares,
                "price": current_price,
                "macd": macd_value,
                "macd_signal": signal_value,
                "macd_hist": hist_value,
            }
        )

    last_price = float(work["price"].iloc[-1])
    last_time = pd.Timestamp(work[TIME_COL].iloc[-1])
    last_macd = float(work["macd"].iloc[-1])
    if shares > 0 and entry_price is not None and entry_time is not None:
        cash += shares * last_price
        pnl = (last_price - float(entry_price)) * shares
        ret_pct = 0.0 if entry_price == 0 else ((last_price / float(entry_price)) - 1.0) * 100.0
        trades.append(
            {
                "entry_time": entry_time,
                "exit_time": last_time,
                "entry_price": float(entry_price),
                "exit_price": last_price,
                "entry_residual": float(entry_macd) if entry_macd is not None else np.nan,
                "exit_residual": last_macd,
                "shares": shares,
                "pnl": pnl,
                "return_pct": ret_pct,
                "exit_reason": "END_OF_TEST",
            }
        )
        if equity_rows:
            equity_rows[-1]["equity"] = cash
            equity_rows[-1]["cash"] = cash
            equity_rows[-1]["shares"] = 0

    equity_curve = pd.DataFrame(equity_rows)
    if not equity_curve.empty:
        equity_curve["peak_equity"] = equity_curve["equity"].cummax()
        equity_curve["drawdown_pct"] = np.where(
            equity_curve["peak_equity"] > 0,
            (equity_curve["equity"] / equity_curve["peak_equity"] - 1.0) * 100.0,
            0.0,
        )

    final_equity = float(cash)
    total_return_pct = 0.0 if initial_cash <= 0 else ((final_equity / initial_cash) - 1.0) * 100.0
    first_price = float(work["price"].iloc[0])
    buy_hold_return_pct = 0.0 if first_price <= 0 else ((last_price / first_price) - 1.0) * 100.0
    max_drawdown_pct = float(equity_curve["drawdown_pct"].min()) if not equity_curve.empty else 0.0
    win_count = sum(1 for trade in trades if float(trade["pnl"]) > 0)
    win_rate_pct = (win_count / len(trades) * 100.0) if trades else 0.0

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "strategy_frame": work,
        "final_equity": final_equity,
        "total_return_pct": total_return_pct,
        "buy_hold_return_pct": buy_hold_return_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "win_rate_pct": win_rate_pct,
        "trade_count": len(trades),
        "train_size": 0,
        "train_ratio": 0.0,
        "residual_mu": 0.0,
        "residual_sigma": 0.0,
        "used_slope": 0.0,
        "trend_mode": "macd",
    }


def run_volume_breakout_backtest(
    df: pd.DataFrame,
    initial_cash: float = WHOLE_SHARE_BUDGET_USD,
    breakout_window: int = 20,
    volume_window: int = 20,
    volume_multiple: float = 1.5,
    exit_ma_window: int = 10,
    use_trailing_stop: bool = True,
    trailing_stop_pct: float = 0.02,
) -> Dict[str, object]:
    empty_result = {
        "trades": [],
        "equity_curve": pd.DataFrame(),
        "strategy_frame": pd.DataFrame(),
        "final_equity": float(initial_cash),
        "total_return_pct": 0.0,
        "buy_hold_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "win_rate_pct": 0.0,
        "trade_count": 0,
        "train_size": 0,
        "train_ratio": 0.0,
        "residual_mu": 0.0,
        "residual_sigma": 0.0,
        "used_slope": 0.0,
        "trend_mode": "volume_breakout",
    }
    if df.empty or "volume" not in df.columns:
        return empty_result

    work = df[[TIME_COL, "price", "volume"]].copy().dropna().reset_index(drop=True)
    if len(work) < max(breakout_window, volume_window, exit_ma_window) + 3 or (work["price"] <= 0).any():
        return empty_result

    breakout_window = max(2, int(breakout_window))
    volume_window = max(2, int(volume_window))
    exit_ma_window = max(2, int(exit_ma_window))
    volume_multiple = max(0.1, float(volume_multiple))

    work["breakout_high"] = work["price"].shift(1).rolling(breakout_window).max()
    work["volume_ma"] = work["volume"].shift(1).rolling(volume_window).mean()
    work["exit_ma"] = work["price"].rolling(exit_ma_window).mean()
    work["volume_ratio"] = np.where(
        work["volume_ma"] > 0,
        work["volume"] / work["volume_ma"],
        np.nan,
    )
    work["phase"] = "test"

    cash = float(initial_cash)
    shares = 0
    entry_price: Optional[float] = None
    entry_time = None
    entry_volume_ratio: Optional[float] = None
    peak_price_since_entry: Optional[float] = None
    trades: List[Dict[str, object]] = []
    equity_rows: List[Dict[str, object]] = []

    for _, row in work.iterrows():
        ts = pd.Timestamp(row[TIME_COL])
        price = float(row["price"])
        volume_ratio = float(row["volume_ratio"]) if pd.notna(row["volume_ratio"]) else np.nan
        breakout_high = float(row["breakout_high"]) if pd.notna(row["breakout_high"]) else np.nan
        exit_ma = float(row["exit_ma"]) if pd.notna(row["exit_ma"]) else np.nan
        buy_signal = (
            shares == 0
            and np.isfinite(breakout_high)
            and np.isfinite(volume_ratio)
            and price > breakout_high
            and volume_ratio >= volume_multiple
        )
        exit_reason = None

        if shares > 0:
            peak_price_since_entry = price if peak_price_since_entry is None else max(peak_price_since_entry, price)
            if np.isfinite(exit_ma) and price < exit_ma:
                exit_reason = "EXIT_MA_BREAK"
            elif use_trailing_stop and peak_price_since_entry is not None:
                stop_price = peak_price_since_entry * (1.0 - trailing_stop_pct)
                if price <= stop_price:
                    exit_reason = "TRAIL_STOP"

            if exit_reason:
                cash += shares * price
                pnl = (price - float(entry_price)) * shares
                ret_pct = 0.0 if entry_price in (None, 0) else ((price / float(entry_price)) - 1.0) * 100.0
                trades.append(
                    {
                        "entry_time": entry_time,
                        "exit_time": ts,
                        "entry_price": float(entry_price),
                        "exit_price": price,
                        "entry_residual": float(entry_volume_ratio) if entry_volume_ratio is not None else np.nan,
                        "exit_residual": volume_ratio,
                        "shares": shares,
                        "pnl": pnl,
                        "return_pct": ret_pct,
                        "exit_reason": exit_reason,
                    }
                )
                shares = 0
                entry_price = None
                entry_time = None
                entry_volume_ratio = None
                peak_price_since_entry = None

        if buy_signal:
            buy_shares = int(cash // price)
            if buy_shares > 0:
                cash -= buy_shares * price
                shares = buy_shares
                entry_price = price
                entry_time = ts
                entry_volume_ratio = volume_ratio
                peak_price_since_entry = price

        equity_rows.append(
            {
                TIME_COL: ts,
                "equity": cash + shares * price,
                "cash": cash,
                "shares": shares,
                "price": price,
                "volume": float(row["volume"]),
                "volume_ratio": volume_ratio,
            }
        )

    last_price = float(work["price"].iloc[-1])
    last_time = pd.Timestamp(work[TIME_COL].iloc[-1])
    last_volume_ratio = float(work["volume_ratio"].iloc[-1]) if pd.notna(work["volume_ratio"].iloc[-1]) else np.nan
    if shares > 0 and entry_price is not None and entry_time is not None:
        cash += shares * last_price
        pnl = (last_price - float(entry_price)) * shares
        ret_pct = 0.0 if entry_price == 0 else ((last_price / float(entry_price)) - 1.0) * 100.0
        trades.append(
            {
                "entry_time": entry_time,
                "exit_time": last_time,
                "entry_price": float(entry_price),
                "exit_price": last_price,
                "entry_residual": float(entry_volume_ratio) if entry_volume_ratio is not None else np.nan,
                "exit_residual": last_volume_ratio,
                "shares": shares,
                "pnl": pnl,
                "return_pct": ret_pct,
                "exit_reason": "END_OF_TEST",
            }
        )
        if equity_rows:
            equity_rows[-1]["equity"] = cash
            equity_rows[-1]["cash"] = cash
            equity_rows[-1]["shares"] = 0

    equity_curve = pd.DataFrame(equity_rows)
    if not equity_curve.empty:
        equity_curve["peak_equity"] = equity_curve["equity"].cummax()
        equity_curve["drawdown_pct"] = np.where(
            equity_curve["peak_equity"] > 0,
            (equity_curve["equity"] / equity_curve["peak_equity"] - 1.0) * 100.0,
            0.0,
        )

    final_equity = float(cash)
    total_return_pct = 0.0 if initial_cash <= 0 else ((final_equity / initial_cash) - 1.0) * 100.0
    first_price = float(work["price"].iloc[0])
    buy_hold_return_pct = 0.0 if first_price <= 0 else ((last_price / first_price) - 1.0) * 100.0
    max_drawdown_pct = float(equity_curve["drawdown_pct"].min()) if not equity_curve.empty else 0.0
    win_count = sum(1 for trade in trades if float(trade["pnl"]) > 0)
    win_rate_pct = (win_count / len(trades) * 100.0) if trades else 0.0

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "strategy_frame": work,
        "final_equity": final_equity,
        "total_return_pct": total_return_pct,
        "buy_hold_return_pct": buy_hold_return_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "win_rate_pct": win_rate_pct,
        "trade_count": len(trades),
        "train_size": 0,
        "train_ratio": 0.0,
        "residual_mu": 0.0,
        "residual_sigma": 0.0,
        "used_slope": 0.0,
        "trend_mode": "volume_breakout",
    }


def run_new_high_backtest(
    df: pd.DataFrame,
    initial_cash: float = WHOLE_SHARE_BUDGET_USD,
    trailing_stop_pct: float = 0.02,
) -> Dict[str, object]:
    empty_result = {
        "trades": [],
        "equity_curve": pd.DataFrame(),
        "strategy_frame": pd.DataFrame(),
        "final_equity": float(initial_cash),
        "total_return_pct": 0.0,
        "buy_hold_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "win_rate_pct": 0.0,
        "trade_count": 0,
        "train_size": 0,
        "train_ratio": 0.0,
        "residual_mu": 0.0,
        "residual_sigma": 0.0,
        "used_slope": 0.0,
        "trend_mode": "new_high",
    }
    if df.empty:
        return empty_result

    work = df[[TIME_COL, "price"]].copy().dropna().reset_index(drop=True)
    if len(work) < 3 or (work["price"] <= 0).any():
        return empty_result

    work["prev_high"] = work["price"].astype(float).cummax().shift(1)
    work["new_high_signal"] = work["price"].astype(float) > work["prev_high"]
    work["phase"] = "test"

    cash = float(initial_cash)
    shares = 0
    entry_price: Optional[float] = None
    entry_time = None
    entry_prev_high: Optional[float] = None
    peak_price_since_entry: Optional[float] = None
    trades: List[Dict[str, object]] = []
    equity_rows: List[Dict[str, object]] = []

    for _, row in work.iterrows():
        ts = pd.Timestamp(row[TIME_COL])
        price = float(row["price"])
        prev_high = float(row["prev_high"]) if pd.notna(row["prev_high"]) else np.nan
        new_high_signal = bool(row["new_high_signal"]) and np.isfinite(prev_high)
        exit_reason = None

        if shares > 0:
            peak_price_since_entry = price if peak_price_since_entry is None else max(peak_price_since_entry, price)
            stop_price = peak_price_since_entry * (1.0 - trailing_stop_pct)
            if price <= stop_price:
                exit_reason = "TRAIL_STOP"

            if exit_reason:
                cash += shares * price
                pnl = (price - float(entry_price)) * shares
                ret_pct = 0.0 if entry_price in (None, 0) else ((price / float(entry_price)) - 1.0) * 100.0
                trades.append(
                    {
                        "entry_time": entry_time,
                        "exit_time": ts,
                        "entry_price": float(entry_price),
                        "exit_price": price,
                        "entry_residual": float(entry_prev_high) if entry_prev_high is not None else np.nan,
                        "exit_residual": prev_high,
                        "shares": shares,
                        "pnl": pnl,
                        "return_pct": ret_pct,
                        "exit_reason": exit_reason,
                    }
                )
                shares = 0
                entry_price = None
                entry_time = None
                entry_prev_high = None
                peak_price_since_entry = None

        if shares == 0 and new_high_signal:
            buy_shares = int(cash // price)
            if buy_shares > 0:
                cash -= buy_shares * price
                shares = buy_shares
                entry_price = price
                entry_time = ts
                entry_prev_high = prev_high
                peak_price_since_entry = price

        equity_rows.append(
            {
                TIME_COL: ts,
                "equity": cash + shares * price,
                "cash": cash,
                "shares": shares,
                "price": price,
                "prev_high": prev_high,
                "new_high_signal": new_high_signal,
            }
        )

    last_price = float(work["price"].iloc[-1])
    last_time = pd.Timestamp(work[TIME_COL].iloc[-1])
    last_prev_high = float(work["prev_high"].iloc[-1]) if pd.notna(work["prev_high"].iloc[-1]) else np.nan
    if shares > 0 and entry_price is not None and entry_time is not None:
        cash += shares * last_price
        pnl = (last_price - float(entry_price)) * shares
        ret_pct = 0.0 if entry_price == 0 else ((last_price / float(entry_price)) - 1.0) * 100.0
        trades.append(
            {
                "entry_time": entry_time,
                "exit_time": last_time,
                "entry_price": float(entry_price),
                "exit_price": last_price,
                "entry_residual": float(entry_prev_high) if entry_prev_high is not None else np.nan,
                "exit_residual": last_prev_high,
                "shares": shares,
                "pnl": pnl,
                "return_pct": ret_pct,
                "exit_reason": "END_OF_TEST",
            }
        )
        if equity_rows:
            equity_rows[-1]["equity"] = cash
            equity_rows[-1]["cash"] = cash
            equity_rows[-1]["shares"] = 0

    equity_curve = pd.DataFrame(equity_rows)
    if not equity_curve.empty:
        equity_curve["peak_equity"] = equity_curve["equity"].cummax()
        equity_curve["drawdown_pct"] = np.where(
            equity_curve["peak_equity"] > 0,
            (equity_curve["equity"] / equity_curve["peak_equity"] - 1.0) * 100.0,
            0.0,
        )

    final_equity = float(cash)
    total_return_pct = 0.0 if initial_cash <= 0 else ((final_equity / initial_cash) - 1.0) * 100.0
    first_price = float(work["price"].iloc[0])
    buy_hold_return_pct = 0.0 if first_price <= 0 else ((last_price / first_price) - 1.0) * 100.0
    max_drawdown_pct = float(equity_curve["drawdown_pct"].min()) if not equity_curve.empty else 0.0
    win_count = sum(1 for trade in trades if float(trade["pnl"]) > 0)
    win_rate_pct = (win_count / len(trades) * 100.0) if trades else 0.0

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "strategy_frame": work,
        "final_equity": final_equity,
        "total_return_pct": total_return_pct,
        "buy_hold_return_pct": buy_hold_return_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "win_rate_pct": win_rate_pct,
        "trade_count": len(trades),
        "train_size": 0,
        "train_ratio": 0.0,
        "residual_mu": 0.0,
        "residual_sigma": 0.0,
        "used_slope": 0.0,
        "trend_mode": "new_high",
    }


def run_drawdown_repair_backtest(
    df: pd.DataFrame,
    initial_cash: float = WHOLE_SHARE_BUDGET_USD,
    repair_ratio: float = 0.50,
    trailing_stop_pct: float = 0.02,
) -> Dict[str, object]:
    empty_result = {
        "trades": [],
        "equity_curve": pd.DataFrame(),
        "strategy_frame": pd.DataFrame(),
        "final_equity": float(initial_cash),
        "total_return_pct": 0.0,
        "buy_hold_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "win_rate_pct": 0.0,
        "trade_count": 0,
        "train_size": 0,
        "train_ratio": 0.0,
        "residual_mu": 0.0,
        "residual_sigma": 0.0,
        "used_slope": 0.0,
        "trend_mode": "drawdown_repair",
        "repair_ratio": float(repair_ratio),
    }
    if df.empty:
        return empty_result

    work = df[[TIME_COL, "price"]].copy().dropna().reset_index(drop=True)
    if len(work) < 3 or (work["price"] <= 0).any():
        return empty_result

    repair_ratio = max(0.01, min(float(repair_ratio), 1.0))
    work["prev_high"] = np.nan
    work["drawdown_pct"] = 0.0
    work["repair_pct"] = 0.0
    work["repair_threshold_pct"] = repair_ratio * 100.0
    work["repair_target_price"] = np.nan
    work["drawdown_peak_price"] = np.nan
    work["drawdown_trough_price"] = np.nan
    work["new_high_signal"] = False
    work["phase"] = "test"

    cash = float(initial_cash)
    shares = 0
    entry_price: Optional[float] = None
    entry_time = None
    entry_repair_pct: Optional[float] = None
    entry_drawdown_pct: Optional[float] = None
    peak_price_since_entry: Optional[float] = None
    trades: List[Dict[str, object]] = []
    equity_rows: List[Dict[str, object]] = []

    peak_price = float(work["price"].iloc[0])
    trough_price = peak_price
    drawdown_id = 0
    entered_drawdown_id: Optional[int] = None

    for pos, row in work.iterrows():
        ts = pd.Timestamp(row[TIME_COL])
        price = float(row["price"])
        prev_high = peak_price
        new_high_signal = pos > 0 and price > peak_price

        if new_high_signal:
            peak_price = price
            trough_price = price
            drawdown_id += 1
            entered_drawdown_id = None
            prev_high = peak_price
        elif price < trough_price:
            trough_price = price
            drawdown_id += 1
            entered_drawdown_id = None

        drawdown_amount = max(peak_price - trough_price, 0.0)
        drawdown_pct = 0.0 if peak_price <= 0 else (drawdown_amount / peak_price) * 100.0
        repair_pct = 0.0
        repair_target_price = np.nan
        repair_signal = False
        if drawdown_amount > 0:
            repair_amount = max(price - trough_price, 0.0)
            repair_pct = min(repair_amount / drawdown_amount, 1.0) * 100.0
            repair_target_price = trough_price + drawdown_amount * repair_ratio
            repair_signal = (
                price >= repair_target_price
                and entered_drawdown_id != drawdown_id
            )

        work.at[pos, "prev_high"] = prev_high
        work.at[pos, "drawdown_pct"] = drawdown_pct
        work.at[pos, "repair_pct"] = repair_pct
        work.at[pos, "repair_target_price"] = repair_target_price
        work.at[pos, "drawdown_peak_price"] = peak_price
        work.at[pos, "drawdown_trough_price"] = trough_price
        work.at[pos, "new_high_signal"] = repair_signal

        exit_reason = None
        if shares > 0:
            peak_price_since_entry = price if peak_price_since_entry is None else max(peak_price_since_entry, price)
            stop_price = peak_price_since_entry * (1.0 - trailing_stop_pct)
            if price <= stop_price:
                exit_reason = "TRAIL_STOP"

            if exit_reason:
                cash += shares * price
                pnl = (price - float(entry_price)) * shares
                ret_pct = 0.0 if entry_price in (None, 0) else ((price / float(entry_price)) - 1.0) * 100.0
                trades.append(
                    {
                        "entry_time": entry_time,
                        "exit_time": ts,
                        "entry_price": float(entry_price),
                        "exit_price": price,
                        "entry_residual": float(entry_repair_pct) if entry_repair_pct is not None else np.nan,
                        "exit_residual": repair_pct,
                        "shares": shares,
                        "pnl": pnl,
                        "return_pct": ret_pct,
                        "exit_reason": exit_reason,
                    }
                )
                shares = 0
                entry_price = None
                entry_time = None
                entry_repair_pct = None
                entry_drawdown_pct = None
                peak_price_since_entry = None

        if shares == 0 and repair_signal:
            buy_shares = int(cash // price)
            if buy_shares > 0:
                cash -= buy_shares * price
                shares = buy_shares
                entry_price = price
                entry_time = ts
                entry_repair_pct = repair_pct
                entry_drawdown_pct = drawdown_pct
                peak_price_since_entry = price
                entered_drawdown_id = drawdown_id

        equity_rows.append(
            {
                TIME_COL: ts,
                "equity": cash + shares * price,
                "cash": cash,
                "shares": shares,
                "price": price,
                "prev_high": prev_high,
                "drawdown_pct": drawdown_pct,
                "repair_pct": repair_pct,
                "repair_target_price": repair_target_price,
                "drawdown_peak_price": peak_price,
                "drawdown_trough_price": trough_price,
                "new_high_signal": repair_signal,
            }
        )

    last_price = float(work["price"].iloc[-1])
    last_time = pd.Timestamp(work[TIME_COL].iloc[-1])
    last_repair_pct = float(work["repair_pct"].iloc[-1])
    if shares > 0 and entry_price is not None and entry_time is not None:
        cash += shares * last_price
        pnl = (last_price - float(entry_price)) * shares
        ret_pct = 0.0 if entry_price == 0 else ((last_price / float(entry_price)) - 1.0) * 100.0
        trades.append(
            {
                "entry_time": entry_time,
                "exit_time": last_time,
                "entry_price": float(entry_price),
                "exit_price": last_price,
                "entry_residual": float(entry_repair_pct) if entry_repair_pct is not None else np.nan,
                "exit_residual": last_repair_pct,
                "shares": shares,
                "pnl": pnl,
                "return_pct": ret_pct,
                "exit_reason": "END_OF_TEST",
            }
        )
        if equity_rows:
            equity_rows[-1]["equity"] = cash
            equity_rows[-1]["cash"] = cash
            equity_rows[-1]["shares"] = 0

    equity_curve = pd.DataFrame(equity_rows)
    if not equity_curve.empty:
        equity_curve["peak_equity"] = equity_curve["equity"].cummax()
        equity_curve["drawdown_pct"] = np.where(
            equity_curve["peak_equity"] > 0,
            (equity_curve["equity"] / equity_curve["peak_equity"] - 1.0) * 100.0,
            0.0,
        )

    final_equity = float(cash)
    total_return_pct = 0.0 if initial_cash <= 0 else ((final_equity / initial_cash) - 1.0) * 100.0
    first_price = float(work["price"].iloc[0])
    buy_hold_return_pct = 0.0 if first_price <= 0 else ((last_price / first_price) - 1.0) * 100.0
    max_drawdown_pct = float(equity_curve["drawdown_pct"].min()) if not equity_curve.empty else 0.0
    win_count = sum(1 for trade in trades if float(trade["pnl"]) > 0)
    win_rate_pct = (win_count / len(trades) * 100.0) if trades else 0.0

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "strategy_frame": work,
        "final_equity": final_equity,
        "total_return_pct": total_return_pct,
        "buy_hold_return_pct": buy_hold_return_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "win_rate_pct": win_rate_pct,
        "trade_count": len(trades),
        "train_size": 0,
        "train_ratio": 0.0,
        "residual_mu": 0.0,
        "residual_sigma": 0.0,
        "used_slope": 0.0,
        "trend_mode": "drawdown_repair",
        "repair_ratio": repair_ratio,
    }


def run_long_short_combo_backtest(
    df: pd.DataFrame,
    initial_cash: float = WHOLE_SHARE_BUDGET_USD,
    short_drawdown_pct: float = 3.0,
    long_rebound_pct: float = 2.0,
    use_trailing_stop: bool = True,
    trailing_stop_pct: float = 0.02,
) -> Dict[str, object]:
    short_drawdown_pct = max(0.01, float(short_drawdown_pct))
    long_rebound_pct = max(0.01, float(long_rebound_pct))
    empty_result = {
        "trades": [],
        "equity_curve": pd.DataFrame(),
        "strategy_frame": pd.DataFrame(),
        "final_equity": float(initial_cash),
        "total_return_pct": 0.0,
        "buy_hold_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "win_rate_pct": 0.0,
        "trade_count": 0,
        "train_size": 0,
        "train_ratio": 0.0,
        "residual_mu": 0.0,
        "residual_sigma": 0.0,
        "used_slope": 0.0,
        "trend_mode": "long_short_combo",
        "short_drawdown_pct": short_drawdown_pct,
        "long_rebound_pct": long_rebound_pct,
    }
    if df.empty:
        return empty_result

    work = df[[TIME_COL, "price"]].copy().dropna().reset_index(drop=True)
    if len(work) < 3 or (work["price"] <= 0).any():
        return empty_result

    work["round_high"] = np.nan
    work["round_low"] = np.nan
    work["short_trigger_price"] = np.nan
    work["long_trigger_price"] = np.nan
    work["drawdown_pct"] = 0.0
    work["rebound_pct"] = 0.0
    work["short_signal"] = False
    work["long_signal"] = False
    work["short_drawdown_pct"] = short_drawdown_pct
    work["long_rebound_pct"] = long_rebound_pct
    work["phase"] = "test"

    cash = float(initial_cash)
    shares = 0
    entry_price: Optional[float] = None
    entry_time = None
    entry_signal_value: Optional[float] = None
    best_price_since_entry: Optional[float] = None
    round_high = float(work["price"].iloc[0])
    round_low = round_high
    trades: List[Dict[str, object]] = []
    equity_rows: List[Dict[str, object]] = []

    def close_position(ts, current_price: float, signal_value: float, reason: str) -> None:
        nonlocal cash, shares, entry_price, entry_time, entry_signal_value, best_price_since_entry
        cash += shares * current_price
        pnl = (current_price - float(entry_price)) * shares
        ret_pct = (
            0.0
            if entry_price in (None, 0)
            else ((current_price / float(entry_price) - 1.0) * 100.0 * (1 if shares > 0 else -1))
        )
        trades.append(
            {
                "entry_time": entry_time,
                "exit_time": ts,
                "entry_price": float(entry_price),
                "exit_price": current_price,
                "entry_residual": float(entry_signal_value) if entry_signal_value is not None else np.nan,
                "exit_residual": signal_value,
                "shares": shares,
                "pnl": pnl,
                "return_pct": ret_pct,
                "exit_reason": reason,
            }
        )
        shares = 0
        entry_price = None
        entry_time = None
        entry_signal_value = None
        best_price_since_entry = None

    def open_position(ts, current_price: float, signal_value: float, side: int) -> None:
        nonlocal cash, shares, entry_price, entry_time, entry_signal_value, best_price_since_entry
        gross_shares = int(cash // current_price)
        if gross_shares <= 0:
            return
        shares = gross_shares if side > 0 else -gross_shares
        cash -= shares * current_price
        entry_price = current_price
        entry_time = ts
        entry_signal_value = signal_value
        best_price_since_entry = current_price

    for pos, row in work.iterrows():
        ts = pd.Timestamp(row[TIME_COL])
        current_price = float(row["price"])

        if current_price > round_high:
            round_high = current_price
            round_low = current_price
        elif current_price < round_low:
            round_low = current_price

        short_trigger_price = round_high * (1.0 - short_drawdown_pct / 100.0)
        long_trigger_price = round_low * (1.0 + long_rebound_pct / 100.0)
        drawdown_pct = 0.0 if round_high <= 0 else (round_high / current_price - 1.0) * 100.0
        rebound_pct = 0.0 if round_low <= 0 else (current_price / round_low - 1.0) * 100.0
        short_signal = current_price <= short_trigger_price
        long_signal = current_price >= long_trigger_price and round_low < round_high
        signal_value = drawdown_pct if short_signal else rebound_pct

        work.at[pos, "round_high"] = round_high
        work.at[pos, "round_low"] = round_low
        work.at[pos, "short_trigger_price"] = short_trigger_price
        work.at[pos, "long_trigger_price"] = long_trigger_price
        work.at[pos, "drawdown_pct"] = drawdown_pct
        work.at[pos, "rebound_pct"] = rebound_pct
        work.at[pos, "short_signal"] = short_signal
        work.at[pos, "long_signal"] = long_signal

        exit_reason = None

        if shares != 0:
            if shares > 0:
                best_price_since_entry = (
                    current_price
                    if best_price_since_entry is None
                    else max(best_price_since_entry, current_price)
                )
                if use_trailing_stop and current_price <= best_price_since_entry * (1.0 - trailing_stop_pct):
                    exit_reason = "TRAIL_STOP"
            else:
                best_price_since_entry = (
                    current_price
                    if best_price_since_entry is None
                    else min(best_price_since_entry, current_price)
                )
                if use_trailing_stop and current_price >= best_price_since_entry * (1.0 + trailing_stop_pct):
                    exit_reason = "TRAIL_STOP"

            if exit_reason:
                close_position(ts, current_price, signal_value, exit_reason)

        if shares == 0:
            if long_signal:
                open_position(ts, current_price, rebound_pct, side=1)
            elif short_signal:
                open_position(ts, current_price, drawdown_pct, side=-1)

        equity_rows.append(
            {
                TIME_COL: ts,
                "equity": cash + shares * current_price,
                "cash": cash,
                "shares": shares,
                "price": current_price,
                "drawdown_pct": drawdown_pct,
                "rebound_pct": rebound_pct,
            }
        )

    last_price = float(work["price"].iloc[-1])
    last_time = pd.Timestamp(work[TIME_COL].iloc[-1])
    last_signal_value = (
        float(work["drawdown_pct"].iloc[-1])
        if shares < 0
        else float(work["rebound_pct"].iloc[-1])
    )
    if shares != 0 and entry_price is not None and entry_time is not None:
        close_position(last_time, last_price, last_signal_value, "END_OF_TEST")
        if equity_rows:
            equity_rows[-1]["equity"] = cash
            equity_rows[-1]["cash"] = cash
            equity_rows[-1]["shares"] = 0

    equity_curve = pd.DataFrame(equity_rows)
    if not equity_curve.empty:
        equity_curve["peak_equity"] = equity_curve["equity"].cummax()
        equity_curve["drawdown_pct"] = np.where(
            equity_curve["peak_equity"] > 0,
            (equity_curve["equity"] / equity_curve["peak_equity"] - 1.0) * 100.0,
            0.0,
        )

    final_equity = float(cash)
    total_return_pct = 0.0 if initial_cash <= 0 else ((final_equity / initial_cash) - 1.0) * 100.0
    first_price = float(work["price"].iloc[0])
    buy_hold_return_pct = 0.0 if first_price <= 0 else ((last_price / first_price) - 1.0) * 100.0
    max_drawdown_pct = float(equity_curve["drawdown_pct"].min()) if not equity_curve.empty else 0.0
    win_count = sum(1 for trade in trades if float(trade["pnl"]) > 0)
    win_rate_pct = (win_count / len(trades) * 100.0) if trades else 0.0

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "strategy_frame": work,
        "final_equity": final_equity,
        "total_return_pct": total_return_pct,
        "buy_hold_return_pct": buy_hold_return_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "win_rate_pct": win_rate_pct,
        "trade_count": len(trades),
        "train_size": 0,
        "train_ratio": 0.0,
        "residual_mu": 0.0,
        "residual_sigma": 0.0,
        "used_slope": 0.0,
        "trend_mode": "long_short_combo",
        "short_drawdown_pct": short_drawdown_pct,
        "long_rebound_pct": long_rebound_pct,
    }


def build_strategy_chart(
    strategy_df: pd.DataFrame,
    trades: List[Dict[str, object]],
    ticker: str,
    train_size: int,
) -> go.Figure:
    plot_df = prepare_trading_axis(strategy_df[[TIME_COL, "price"]].copy())
    if "trend_price" in strategy_df.columns:
        plot_df["trend_price"] = strategy_df["trend_price"].to_numpy()
    if "prev_high" in strategy_df.columns:
        plot_df["prev_high"] = strategy_df["prev_high"].to_numpy()
    if "new_high_signal" in strategy_df.columns:
        plot_df["new_high_signal"] = strategy_df["new_high_signal"].astype(int).to_numpy()
    if "phase" in strategy_df.columns:
        plot_df["phase"] = strategy_df["phase"].to_numpy()
    plot_df["hover_x"] = plot_df["time_str"]
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.68, 0.32],
        vertical_spacing=0.07,
    )
    fig.add_trace(
        go.Scatter(
            x=plot_df["hover_x"],
            y=plot_df["price"],
            mode="lines",
            name=ticker,
            line=dict(color="#495057", width=1.8),
            hovertemplate="price=%{y:.4f}<extra></extra>",
        ),
        row=1,
        col=1,
    )

    if "trend_price" in plot_df.columns:
        fig.add_trace(
            go.Scatter(
                x=plot_df["hover_x"],
                y=plot_df["trend_price"],
                mode="lines",
                name="Train Trend",
                line=dict(color="#1f78b4", width=1.4, dash="dash"),
                hovertemplate="trend=%{y:.4f}<extra></extra>",
            ),
            row=1,
            col=1,
        )

    if "prev_high" in plot_df.columns:
        fig.add_trace(
            go.Scatter(
                x=plot_df["hover_x"],
                y=plot_df["prev_high"],
                mode="lines",
                name="Previous High",
                line=dict(color="#f08c00", width=1.2, dash="dash"),
                hovertemplate="prev high=%{y:.4f}<extra></extra>",
            ),
            row=1,
            col=1,
        )

    if "repair_target_price" in strategy_df.columns:
        plot_df["repair_target_price"] = strategy_df["repair_target_price"].to_numpy()
        fig.add_trace(
            go.Scatter(
                x=plot_df["hover_x"],
                y=plot_df["repair_target_price"],
                mode="lines",
                name="Repair Trigger",
                line=dict(color="#2f9e44", width=1.2, dash="dot"),
                hovertemplate="repair trigger=%{y:.4f}<extra></extra>",
            ),
            row=1,
            col=1,
        )

    if "residual" in strategy_df.columns:
        plot_df["residual"] = strategy_df["residual"].to_numpy()
        fig.add_trace(
            go.Scatter(
                x=plot_df["hover_x"],
                y=plot_df["residual"],
                mode="lines",
                name="Residual",
                line=dict(color="#7c2d12", width=1.35),
                hovertemplate="residual=%{y:.6f}<extra></extra>",
            ),
            row=2,
            col=1,
        )
        fig.add_hline(y=0, line_width=1, line_color="#868e96", row=2, col=1)
        if "entry_residual_threshold" in strategy_df.columns:
            threshold_value = float(strategy_df["entry_residual_threshold"].iloc[0])
            fig.add_hline(
                y=threshold_value,
                line_width=1,
                line_dash="dash",
                line_color="#2f9e44",
                annotation_text=f"Entry {threshold_value:.4f}",
                annotation_position="top left",
                row=2,
                col=1,
            )
    elif "repair_pct" in strategy_df.columns:
        plot_df["repair_pct"] = strategy_df["repair_pct"].to_numpy()
        threshold_value = (
            float(strategy_df["repair_threshold_pct"].iloc[0])
            if "repair_threshold_pct" in strategy_df.columns
            else np.nan
        )
        fig.add_trace(
            go.Scatter(
                x=plot_df["hover_x"],
                y=plot_df["repair_pct"],
                mode="lines",
                name="Repair %",
                line=dict(color="#1f5fbf", width=1.4),
                hovertemplate="repair=%{y:.2f}%<extra></extra>",
            ),
            row=2,
            col=1,
        )
        if np.isfinite(threshold_value):
            fig.add_hline(
                y=threshold_value,
                line_width=1,
                line_dash="dash",
                line_color="#2f9e44",
                annotation_text=f"Entry {threshold_value:.0f}%",
                annotation_position="top left",
                row=2,
                col=1,
            )
    elif "new_high_signal" in plot_df.columns:
        signal_points = plot_df[plot_df["new_high_signal"] > 0]
        signal_x = []
        signal_y = []
        for x_value in signal_points["hover_x"]:
            signal_x.extend([x_value, x_value, None])
            signal_y.extend([0, 1, None])
        fig.add_trace(
            go.Scatter(
                x=signal_x,
                y=signal_y,
                mode="lines",
                name="New High Signal",
                line=dict(color="#2f9e44", width=3),
                hovertemplate="new high=%{y}<extra></extra>",
            ),
            row=2,
            col=1,
        )

    if trades:
        entry_points = []
        exit_points = []
        residual_entry_points = []
        residual_exit_points = []
        time_to_x = dict(zip(plot_df[TIME_COL], plot_df["hover_x"]))
        has_residual_chart = "residual" in plot_df.columns
        for trade in trades:
            entry_time = pd.Timestamp(trade["entry_time"])
            exit_time = pd.Timestamp(trade["exit_time"])
            if entry_time in time_to_x:
                entry_x = time_to_x[entry_time]
                entry_text = f"BUY {int(trade['shares'])} @ {float(trade['entry_price']):.2f}"
                entry_points.append(
                    {
                        "x": entry_x,
                        "y": float(trade["entry_price"]),
                        "text": entry_text,
                    }
                )
                if has_residual_chart and pd.notna(trade.get("entry_residual")):
                    residual_entry_points.append(
                        {
                            "x": entry_x,
                            "y": float(trade["entry_residual"]),
                            "text": entry_text,
                        }
                    )
            if exit_time in time_to_x:
                exit_x = time_to_x[exit_time]
                exit_text = (
                    f"SELL @ {float(trade['exit_price']):.2f}<br>"
                    f"ret {float(trade['return_pct']):+.2f}%"
                )
                exit_points.append(
                    {
                        "x": exit_x,
                        "y": float(trade["exit_price"]),
                        "text": exit_text,
                        "pnl": float(trade["pnl"]),
                    }
                )
                if has_residual_chart and pd.notna(trade.get("exit_residual")):
                    residual_exit_points.append(
                        {
                            "x": exit_x,
                            "y": float(trade["exit_residual"]),
                            "text": exit_text,
                            "pnl": float(trade["pnl"]),
                        }
                    )

        if entry_points:
            fig.add_trace(
                go.Scatter(
                    x=[item["x"] for item in entry_points],
                    y=[item["y"] for item in entry_points],
                    mode="markers",
                    name="Buy",
                    marker=dict(color="#868e96", size=9, symbol="triangle-up"),
                    text=[item["text"] for item in entry_points],
                    hovertemplate="%{text}<extra></extra>",
                ),
                row=1,
                col=1,
            )

        if exit_points:
            fig.add_trace(
                go.Scatter(
                    x=[item["x"] for item in exit_points],
                    y=[item["y"] for item in exit_points],
                    mode="markers",
                    name="Sell",
                    marker=dict(
                        color=["#2b8a3e" if item["pnl"] >= 0 else "#e67700" for item in exit_points],
                        size=9,
                        symbol=["triangle-up" if item["pnl"] >= 0 else "triangle-down" for item in exit_points],
                    ),
                    text=[item["text"] for item in exit_points],
                    hovertemplate="%{text}<extra></extra>",
                ),
                row=1,
                col=1,
            )

        if residual_entry_points:
            fig.add_trace(
                go.Scatter(
                    x=[item["x"] for item in residual_entry_points],
                    y=[item["y"] for item in residual_entry_points],
                    mode="markers",
                    name="Buy Residual",
                    marker=dict(color="#868e96", size=9, symbol="triangle-up"),
                    text=[item["text"] for item in residual_entry_points],
                    hovertemplate="%{text}<br>residual=%{y:.6f}<extra></extra>",
                    showlegend=False,
                ),
                row=2,
                col=1,
            )

        if residual_exit_points:
            fig.add_trace(
                go.Scatter(
                    x=[item["x"] for item in residual_exit_points],
                    y=[item["y"] for item in residual_exit_points],
                    mode="markers",
                    name="Sell Residual",
                    marker=dict(
                        color=["#2b8a3e" if item["pnl"] >= 0 else "#e67700" for item in residual_exit_points],
                        size=9,
                        symbol=["triangle-up" if item["pnl"] >= 0 else "triangle-down" for item in residual_exit_points],
                    ),
                    text=[item["text"] for item in residual_exit_points],
                    hovertemplate="%{text}<br>residual=%{y:.6f}<extra></extra>",
                    showlegend=False,
                ),
                row=2,
                col=1,
            )

    tick_positions, ticktext = build_trading_day_ticks(plot_df)
    tickvals = [plot_df["hover_x"].iloc[pos] for pos in tick_positions if 0 <= pos < len(plot_df)]
    is_new_high_chart = "prev_high" in plot_df.columns and "residual" not in strategy_df.columns
    is_drawdown_repair_chart = "repair_pct" in strategy_df.columns
    if "residual" in strategy_df.columns and not is_new_high_chart:
        add_alternating_day_background(fig, plot_df, row=1, col=1)
    if train_size > 0 and train_size < len(plot_df):
        split_x = plot_df["hover_x"].iloc[train_size - 1]
        fig.add_vline(x=split_x, line_width=1, line_dash="dot", line_color="#6c757d", row=1, col=1)
        fig.add_vline(x=split_x, line_width=1, line_dash="dot", line_color="#6c757d", row=2, col=1)
        fig.add_annotation(
            x=split_x,
            y=1.03,
            yref="paper",
            text="Train/Test Split",
            showarrow=False,
            font=dict(size=11, color="#6c757d"),
        )
    fig.update_layout(
        title=(
            f"{ticker} Drawdown Repair Game"
            if is_drawdown_repair_chart
            else (f"{ticker} New High Game" if is_new_high_chart else f"{ticker} Detrended Low Game")
        ),
        template="plotly_white",
        hovermode="x unified",
        hoversubplots="axis",
        margin=dict(l=24, r=24, t=56, b=24),
        height=720,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1.0),
    )
    fig.update_xaxes(
        type="category",
        tickmode="array",
        tickvals=tickvals,
        ticktext=ticktext,
        showgrid=True,
        gridcolor="#eceff4",
        rangeslider_visible=False,
        showspikes=True,
        spikemode="across",
        spikesnap="cursor",
        spikedash="dot",
        spikecolor="#6c757d",
        spikethickness=1,
        row=1,
        col=1,
    )
    fig.update_xaxes(
        title_text="Trading Days",
        type="category",
        tickmode="array",
        tickvals=tickvals,
        ticktext=ticktext,
        showgrid=True,
        gridcolor="#eceff4",
        rangeslider_visible=False,
        showspikes=True,
        spikemode="across",
        spikesnap="cursor",
        spikedash="dot",
        spikecolor="#6c757d",
        spikethickness=1,
        row=2,
        col=1,
    )
    fig.update_layout(
        xaxis=dict(
            showspikes=True,
            spikemode="across",
            spikesnap="cursor",
            spikedash="dot",
            spikecolor="#6c757d",
            spikethickness=1,
        ),
        xaxis2=dict(
            showspikes=True,
            spikemode="across",
            spikesnap="cursor",
            spikedash="dot",
            spikecolor="#6c757d",
            spikethickness=1,
        ),
    )
    fig.update_yaxes(
        title_text="Price" if is_new_high_chart else "Price (log)",
        type="linear" if is_new_high_chart else "log",
        showgrid=True,
        gridcolor="#eceff4",
        row=1,
        col=1,
    )
    fig.update_yaxes(
        title_text="Repair %" if is_drawdown_repair_chart else ("Signal" if is_new_high_chart else "Residual"),
        zeroline=True,
        showgrid=True,
        gridcolor="#eceff4",
        range=[-5, 105] if is_drawdown_repair_chart else ([-0.05, 1.08] if is_new_high_chart else None),
        row=2,
        col=1,
    )
    return fig


def build_macd_strategy_chart(
    strategy_df: pd.DataFrame,
    trades: List[Dict[str, object]],
    ticker: str,
) -> go.Figure:
    plot_df = prepare_trading_axis(
        strategy_df[[TIME_COL, "price", "macd", "macd_signal", "macd_hist"]].copy()
    )
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.68, 0.32],
        vertical_spacing=0.06,
    )
    fig.add_trace(
        go.Scatter(
            x=plot_df["plot_x"],
            y=plot_df["price"],
            mode="lines",
            name=ticker,
            line=dict(color="#495057", width=1.8),
            customdata=plot_df["time_str"],
            hovertemplate="%{customdata}<br>price=%{y:.4f}<extra></extra>",
        ),
        row=1,
        col=1,
    )

    if trades:
        entry_points = []
        exit_points = []
        time_to_x = dict(zip(plot_df[TIME_COL], plot_df["plot_x"]))
        for trade in trades:
            entry_time = pd.Timestamp(trade["entry_time"])
            exit_time = pd.Timestamp(trade["exit_time"])
            if entry_time in time_to_x:
                entry_points.append(
                    {
                        "x": int(time_to_x[entry_time]),
                        "y": float(trade["entry_price"]),
                        "text": f"BUY {int(trade['shares'])} @ {float(trade['entry_price']):.2f}",
                        "time": entry_time.strftime("%Y-%m-%d %H:%M"),
                    }
                )
            if exit_time in time_to_x:
                exit_points.append(
                    {
                        "x": int(time_to_x[exit_time]),
                        "y": float(trade["exit_price"]),
                        "text": (
                            f"SELL @ {float(trade['exit_price']):.2f}<br>"
                            f"ret {float(trade['return_pct']):+.2f}%"
                        ),
                        "time": exit_time.strftime("%Y-%m-%d %H:%M"),
                        "pnl": float(trade["pnl"]),
                    }
                )

        if entry_points:
            fig.add_trace(
                go.Scatter(
                    x=[item["x"] for item in entry_points],
                    y=[item["y"] for item in entry_points],
                    mode="markers",
                    name="Buy",
                    marker=dict(color="#2f9e44", size=9, symbol="triangle-up"),
                    text=[item["text"] for item in entry_points],
                    customdata=[item["time"] for item in entry_points],
                    hovertemplate="%{customdata}<br>%{text}<extra></extra>",
                ),
                row=1,
                col=1,
            )

        if exit_points:
            fig.add_trace(
                go.Scatter(
                    x=[item["x"] for item in exit_points],
                    y=[item["y"] for item in exit_points],
                    mode="markers",
                    name="Sell",
                    marker=dict(
                        color=["#2b8a3e" if item["pnl"] >= 0 else "#e67700" for item in exit_points],
                        size=9,
                        symbol="triangle-down",
                    ),
                    text=[item["text"] for item in exit_points],
                    customdata=[item["time"] for item in exit_points],
                    hovertemplate="%{customdata}<br>%{text}<extra></extra>",
                ),
                row=1,
                col=1,
            )

    hist_colors = np.where(plot_df["macd_hist"] >= 0, "#2f9e44", "#e03131")
    fig.add_trace(
        go.Bar(
            x=plot_df["plot_x"],
            y=plot_df["macd_hist"],
            name="Histogram",
            marker_color=hist_colors,
            opacity=0.45,
            customdata=plot_df["time_str"],
            hovertemplate="%{customdata}<br>hist=%{y:.4f}<extra></extra>",
        ),
        row=2,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=plot_df["plot_x"],
            y=plot_df["macd"],
            mode="lines",
            name="MACD",
            line=dict(color="#1f5fbf", width=1.5),
            customdata=plot_df["time_str"],
            hovertemplate="%{customdata}<br>MACD=%{y:.4f}<extra></extra>",
        ),
        row=2,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=plot_df["plot_x"],
            y=plot_df["macd_signal"],
            mode="lines",
            name="Signal",
            line=dict(color="#f08c00", width=1.3),
            customdata=plot_df["time_str"],
            hovertemplate="%{customdata}<br>signal=%{y:.4f}<extra></extra>",
        ),
        row=2,
        col=1,
    )

    tickvals, ticktext = build_trading_day_ticks(plot_df)
    fig.update_layout(
        title=f"{ticker} MACD Strategy",
        template="plotly_white",
        hovermode="x unified",
        margin=dict(l=24, r=24, t=56, b=24),
        height=640,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1.0),
    )
    fig.update_xaxes(
        title_text="Trading Days",
        tickmode="array",
        tickvals=tickvals,
        ticktext=ticktext,
        showgrid=True,
        gridcolor="#eceff4",
        rangeslider_visible=False,
        row=2,
        col=1,
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eceff4", row=1, col=1)
    fig.update_yaxes(title_text="Price", showgrid=True, gridcolor="#eceff4", row=1, col=1)
    fig.update_yaxes(title_text="MACD", showgrid=True, gridcolor="#eceff4", row=2, col=1)
    return fig


def build_long_short_combo_chart(
    strategy_df: pd.DataFrame,
    trades: List[Dict[str, object]],
    ticker: str,
) -> go.Figure:
    plot_df = prepare_trading_axis(
        strategy_df[
            [
                TIME_COL,
                "price",
                "round_high",
                "round_low",
                "short_trigger_price",
                "long_trigger_price",
                "drawdown_pct",
                "rebound_pct",
            ]
        ].copy()
    )
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.66, 0.34],
        vertical_spacing=0.06,
    )
    fig.add_trace(
        go.Scatter(
            x=plot_df["plot_x"],
            y=plot_df["price"],
            mode="lines",
            name=ticker,
            line=dict(color="#495057", width=1.8),
            customdata=plot_df["time_str"],
            hovertemplate="%{customdata}<br>price=%{y:.4f}<extra></extra>",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=plot_df["plot_x"],
            y=plot_df["round_high"],
            mode="lines",
            name="Round High",
            line=dict(color="#1f5fbf", width=1.2),
            customdata=plot_df["time_str"],
            hovertemplate="%{customdata}<br>round high=%{y:.4f}<extra></extra>",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=plot_df["plot_x"],
            y=plot_df["round_low"],
            mode="lines",
            name="Round Low",
            line=dict(color="#2f9e44", width=1.2),
            customdata=plot_df["time_str"],
            hovertemplate="%{customdata}<br>round low=%{y:.4f}<extra></extra>",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=plot_df["plot_x"],
            y=plot_df["short_trigger_price"],
            mode="lines",
            name="Short Band",
            line=dict(color="#e03131", width=1.1, dash="dash"),
            customdata=plot_df["time_str"],
            hovertemplate="%{customdata}<br>short trigger=%{y:.4f}<extra></extra>",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=plot_df["plot_x"],
            y=plot_df["long_trigger_price"],
            mode="lines",
            name="Long Band",
            line=dict(color="#2f9e44", width=1.1, dash="dash"),
            customdata=plot_df["time_str"],
            hovertemplate="%{customdata}<br>long trigger=%{y:.4f}<extra></extra>",
        ),
        row=1,
        col=1,
    )

    if trades:
        long_entries = []
        short_entries = []
        exits = []
        time_to_x = dict(zip(plot_df[TIME_COL], plot_df["plot_x"]))
        for trade in trades:
            entry_time = pd.Timestamp(trade["entry_time"])
            exit_time = pd.Timestamp(trade["exit_time"])
            shares = int(trade["shares"])
            if entry_time in time_to_x:
                target = long_entries if shares > 0 else short_entries
                target.append(
                    {
                        "x": int(time_to_x[entry_time]),
                        "y": float(trade["entry_price"]),
                        "text": (
                            f"{'LONG' if shares > 0 else 'SHORT'} {abs(shares)} @ "
                            f"{float(trade['entry_price']):.2f}<br>"
                            f"{'rebound' if shares > 0 else 'drawdown'} {float(trade['entry_residual']):.2f}%"
                        ),
                        "time": entry_time.strftime("%Y-%m-%d %H:%M"),
                    }
                )
            if exit_time in time_to_x:
                exits.append(
                    {
                        "x": int(time_to_x[exit_time]),
                        "y": float(trade["exit_price"]),
                        "text": (
                            f"EXIT @ {float(trade['exit_price']):.2f}<br>"
                            f"ret {float(trade['return_pct']):+.2f}%"
                        ),
                        "time": exit_time.strftime("%Y-%m-%d %H:%M"),
                        "pnl": float(trade["pnl"]),
                    }
                )

        if long_entries:
            fig.add_trace(
                go.Scatter(
                    x=[item["x"] for item in long_entries],
                    y=[item["y"] for item in long_entries],
                    mode="markers",
                    name="Long Entry",
                    marker=dict(color="#2f9e44", size=10, symbol="triangle-up"),
                    text=[item["text"] for item in long_entries],
                    customdata=[item["time"] for item in long_entries],
                    hovertemplate="%{customdata}<br>%{text}<extra></extra>",
                ),
                row=1,
                col=1,
            )
        if short_entries:
            fig.add_trace(
                go.Scatter(
                    x=[item["x"] for item in short_entries],
                    y=[item["y"] for item in short_entries],
                    mode="markers",
                    name="Short Entry",
                    marker=dict(color="#e03131", size=10, symbol="triangle-down"),
                    text=[item["text"] for item in short_entries],
                    customdata=[item["time"] for item in short_entries],
                    hovertemplate="%{customdata}<br>%{text}<extra></extra>",
                ),
                row=1,
                col=1,
            )
        if exits:
            fig.add_trace(
                go.Scatter(
                    x=[item["x"] for item in exits],
                    y=[item["y"] for item in exits],
                    mode="markers",
                    name="Exit",
                    marker=dict(
                        color=["#2b8a3e" if item["pnl"] >= 0 else "#e67700" for item in exits],
                        size=8,
                        symbol="circle",
                    ),
                    text=[item["text"] for item in exits],
                    customdata=[item["time"] for item in exits],
                    hovertemplate="%{customdata}<br>%{text}<extra></extra>",
                ),
                row=1,
                col=1,
            )

    fig.add_trace(
        go.Scatter(
            x=plot_df["plot_x"],
            y=plot_df["drawdown_pct"],
            mode="lines",
            name="Drawdown %",
            line=dict(color="#e03131", width=1.35),
            customdata=plot_df["time_str"],
            hovertemplate="%{customdata}<br>drawdown=%{y:.2f}%<extra></extra>",
        ),
        row=2,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=plot_df["plot_x"],
            y=plot_df["rebound_pct"],
            mode="lines",
            name="Rebound %",
            line=dict(color="#2f9e44", width=1.35),
            customdata=plot_df["time_str"],
            hovertemplate="%{customdata}<br>rebound=%{y:.2f}%<extra></extra>",
        ),
        row=2,
        col=1,
    )
    short_drawdown_pct = float(strategy_df.get("short_drawdown_pct", pd.Series([np.nan])).iloc[0]) if "short_drawdown_pct" in strategy_df.columns else np.nan
    long_rebound_pct = float(strategy_df.get("long_rebound_pct", pd.Series([np.nan])).iloc[0]) if "long_rebound_pct" in strategy_df.columns else np.nan
    if np.isfinite(short_drawdown_pct):
        fig.add_hline(y=short_drawdown_pct, line_width=1, line_dash="dash", line_color="#e03131", row=2, col=1)
    if np.isfinite(long_rebound_pct):
        fig.add_hline(y=long_rebound_pct, line_width=1, line_dash="dash", line_color="#2f9e44", row=2, col=1)

    tickvals, ticktext = build_trading_day_ticks(plot_df)
    fig.update_layout(
        title=f"{ticker} New High Pullback / Low Rebound Combo",
        template="plotly_white",
        hovermode="x unified",
        margin=dict(l=24, r=24, t=56, b=24),
        height=680,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1.0),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eceff4", rangeslider_visible=False, row=1, col=1)
    fig.update_xaxes(
        title_text="Trading Days",
        tickmode="array",
        tickvals=tickvals,
        ticktext=ticktext,
        showgrid=True,
        gridcolor="#eceff4",
        rangeslider_visible=False,
        row=2,
        col=1,
    )
    fig.update_yaxes(title_text="Price", showgrid=True, gridcolor="#eceff4", row=1, col=1)
    fig.update_yaxes(title_text="Move %", zeroline=True, showgrid=True, gridcolor="#eceff4", row=2, col=1)
    return fig


def build_volume_breakout_chart(
    strategy_df: pd.DataFrame,
    trades: List[Dict[str, object]],
    ticker: str,
) -> go.Figure:
    plot_df = prepare_trading_axis(
        strategy_df[[TIME_COL, "price", "volume", "volume_ma", "breakout_high", "exit_ma"]].copy()
    )
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.68, 0.32],
        vertical_spacing=0.06,
    )
    fig.add_trace(
        go.Scatter(
            x=plot_df["plot_x"],
            y=plot_df["price"],
            mode="lines",
            name=ticker,
            line=dict(color="#495057", width=1.8),
            customdata=plot_df["time_str"],
            hovertemplate="%{customdata}<br>price=%{y:.4f}<extra></extra>",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=plot_df["plot_x"],
            y=plot_df["breakout_high"],
            mode="lines",
            name="Breakout High",
            line=dict(color="#1f5fbf", width=1.2, dash="dash"),
            customdata=plot_df["time_str"],
            hovertemplate="%{customdata}<br>breakout=%{y:.4f}<extra></extra>",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=plot_df["plot_x"],
            y=plot_df["exit_ma"],
            mode="lines",
            name="Exit MA",
            line=dict(color="#f08c00", width=1.2),
            customdata=plot_df["time_str"],
            hovertemplate="%{customdata}<br>exit MA=%{y:.4f}<extra></extra>",
        ),
        row=1,
        col=1,
    )

    if trades:
        entry_points = []
        exit_points = []
        time_to_x = dict(zip(plot_df[TIME_COL], plot_df["plot_x"]))
        for trade in trades:
            entry_time = pd.Timestamp(trade["entry_time"])
            exit_time = pd.Timestamp(trade["exit_time"])
            if entry_time in time_to_x:
                entry_points.append(
                    {
                        "x": int(time_to_x[entry_time]),
                        "y": float(trade["entry_price"]),
                        "text": f"BUY {int(trade['shares'])} @ {float(trade['entry_price']):.2f}",
                        "time": entry_time.strftime("%Y-%m-%d %H:%M"),
                    }
                )
            if exit_time in time_to_x:
                exit_points.append(
                    {
                        "x": int(time_to_x[exit_time]),
                        "y": float(trade["exit_price"]),
                        "text": (
                            f"SELL @ {float(trade['exit_price']):.2f}<br>"
                            f"ret {float(trade['return_pct']):+.2f}%"
                        ),
                        "time": exit_time.strftime("%Y-%m-%d %H:%M"),
                        "pnl": float(trade["pnl"]),
                    }
                )

        if entry_points:
            fig.add_trace(
                go.Scatter(
                    x=[item["x"] for item in entry_points],
                    y=[item["y"] for item in entry_points],
                    mode="markers",
                    name="Buy",
                    marker=dict(color="#2f9e44", size=9, symbol="triangle-up"),
                    text=[item["text"] for item in entry_points],
                    customdata=[item["time"] for item in entry_points],
                    hovertemplate="%{customdata}<br>%{text}<extra></extra>",
                ),
                row=1,
                col=1,
            )
        if exit_points:
            fig.add_trace(
                go.Scatter(
                    x=[item["x"] for item in exit_points],
                    y=[item["y"] for item in exit_points],
                    mode="markers",
                    name="Sell",
                    marker=dict(
                        color=["#2b8a3e" if item["pnl"] >= 0 else "#e67700" for item in exit_points],
                        size=9,
                        symbol="triangle-down",
                    ),
                    text=[item["text"] for item in exit_points],
                    customdata=[item["time"] for item in exit_points],
                    hovertemplate="%{customdata}<br>%{text}<extra></extra>",
                ),
                row=1,
                col=1,
            )

    fig.add_trace(
        go.Bar(
            x=plot_df["plot_x"],
            y=plot_df["volume"],
            name="Volume",
            marker_color="#868e96",
            opacity=0.45,
            customdata=plot_df["time_str"],
            hovertemplate="%{customdata}<br>volume=%{y:,.0f}<extra></extra>",
        ),
        row=2,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=plot_df["plot_x"],
            y=plot_df["volume_ma"],
            mode="lines",
            name="Volume MA",
            line=dict(color="#d9480f", width=1.4),
            customdata=plot_df["time_str"],
            hovertemplate="%{customdata}<br>volume MA=%{y:,.0f}<extra></extra>",
        ),
        row=2,
        col=1,
    )

    tickvals, ticktext = build_trading_day_ticks(plot_df)
    fig.update_layout(
        title=f"{ticker} Volume Breakout Strategy",
        template="plotly_white",
        hovermode="x unified",
        margin=dict(l=24, r=24, t=56, b=24),
        height=640,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1.0),
    )
    fig.update_xaxes(
        title_text="Trading Days",
        tickmode="array",
        tickvals=tickvals,
        ticktext=ticktext,
        showgrid=True,
        gridcolor="#eceff4",
        rangeslider_visible=False,
        row=2,
        col=1,
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eceff4", row=1, col=1)
    fig.update_yaxes(title_text="Price", showgrid=True, gridcolor="#eceff4", row=1, col=1)
    fig.update_yaxes(title_text="Volume", showgrid=True, gridcolor="#eceff4", row=2, col=1)
    return fig


st.title("Single Price Dashboard")
st.caption("从 `us_info` 选择已注册标的，并从 `us_market_prices` 浏览统一历史价格。")

sources = load_price_sources()
if sources.empty:
    st.error("没有找到可用的价格来源。")
    st.stop()

ticker_options = sorted(sources["ticker"].dropna().unique().tolist())

sidebar = st.sidebar
sidebar.header("筛选")
selected_ticker = sidebar.selectbox("标的", ticker_options, index=0)

selected_source = sources.loc[sources["ticker"] == selected_ticker].iloc[0]

window_key = sidebar.selectbox("观察区间", ["1M", "3M", "6M", "1Y", "2Y", "All", "Custom"], index=2)
resample_label = sidebar.selectbox(
    "重采样",
    ["Raw", "15min", "30min", "1H", "4H", "1D"],
    index=0,
)
show_ma20 = sidebar.checkbox("显示 MA20", value=False)
show_ma50 = sidebar.checkbox("显示 MA50", value=False)
log_scale = sidebar.checkbox("对数纵轴", value=False)
strategy_type = sidebar.selectbox(
    "策略类型",
    ["无策略", "MACD", "放量突破", "创新高", "回撤修复", "多空组合", "对数低点"],
    index=0,
)

raw_df = load_price_history(
    selected_ticker,
)
if raw_df.empty:
    st.warning("这个标的当前没有可展示的数据。")
    st.stop()

raw_df = raw_df.reset_index(drop=True)
raw_df["source_bar_index"] = np.arange(len(raw_df), dtype=float)

latest_time = pd.Timestamp(raw_df[TIME_COL].max())
earliest_time = pd.Timestamp(raw_df[TIME_COL].min())

if window_key == "Custom":
    default_start = max(latest_time - pd.Timedelta(days=180), earliest_time)
    start_date = sidebar.date_input("开始日期", value=default_start.date(), min_value=earliest_time.date(), max_value=latest_time.date())
    end_date = sidebar.date_input("结束日期", value=latest_time.date(), min_value=earliest_time.date(), max_value=latest_time.date())
    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
else:
    start_ts = derive_window_start(latest_time, window_key)
    end_ts = latest_time

filtered_df = raw_df.loc[(raw_df[TIME_COL] >= start_ts) & (raw_df[TIME_COL] <= end_ts)].copy()
filtered_df = filtered_df.reset_index(drop=True)

if filtered_df.empty:
    st.warning("当前筛选区间没有数据。")
    st.stop()

saved_log_residual_config = load_log_residual_config(selected_ticker) if strategy_type == "对数低点" else None

macd_fast_span = 12
macd_slow_span = 26
macd_signal_span = 9
macd_use_trailing_stop = True
breakout_window = 20
breakout_volume_window = 20
breakout_volume_multiple = 1.50
breakout_exit_ma_window = 10
breakout_use_trailing_stop = True
drawdown_repair_ratio_pct = 50.00
combo_short_drawdown_pct = 3.00
combo_long_rebound_pct = 2.00
combo_use_trailing_stop = True
strategy_stop_mode = "固定百分比"
fixed_trailing_stop_pct = 2.00
residual_intercept_shift = 0.0
residual_entry_threshold = 0.0
manual_k_slope = 0.0
residual_position_type = "LONG_SPREAD"
save_log_residual_config_requested = False
residual_train_start = filtered_df[TIME_COL].iloc[0]
residual_split_time = filtered_df[TIME_COL].iloc[-1]
has_saved_log_residual_config = False

if strategy_type == "MACD":
    macd_fast_span = sidebar.number_input(
        "MACD fast",
        min_value=2,
        max_value=200,
        value=12,
        step=1,
    )
    macd_slow_span = sidebar.number_input(
        "MACD slow",
        min_value=3,
        max_value=300,
        value=26,
        step=1,
    )
    macd_signal_span = sidebar.number_input(
        "MACD signal",
        min_value=2,
        max_value=100,
        value=9,
        step=1,
    )
    macd_use_trailing_stop = sidebar.checkbox("MACD 使用 trailing stop", value=True)
elif strategy_type == "放量突破":
    breakout_window = sidebar.number_input(
        "突破窗口",
        min_value=2,
        max_value=260,
        value=20,
        step=1,
    )
    breakout_volume_window = sidebar.number_input(
        "成交量均线",
        min_value=2,
        max_value=260,
        value=20,
        step=1,
    )
    breakout_volume_multiple = sidebar.number_input(
        "放量倍数",
        min_value=0.10,
        max_value=10.0,
        value=1.50,
        step=0.10,
        format="%.2f",
    )
    breakout_exit_ma_window = sidebar.number_input(
        "跌破均线出场",
        min_value=2,
        max_value=260,
        value=10,
        step=1,
    )
    breakout_use_trailing_stop = sidebar.checkbox("放量突破使用 trailing stop", value=True)
elif strategy_type == "回撤修复":
    drawdown_repair_ratio_pct = sidebar.number_input(
        "修复入场%",
        min_value=1.00,
        max_value=100.00,
        value=50.00,
        step=1.00,
        format="%.2f",
        help="从当前最大回撤谷底向上修复达到最大回撤幅度的这个比例时入场。",
    )
elif strategy_type == "多空组合":
    combo_short_drawdown_pct = sidebar.number_input(
        "新高后做空跌幅%",
        min_value=0.01,
        max_value=80.00,
        value=3.00,
        step=0.10,
        format="%.2f",
        help="每轮创新高后，价格从本轮高点下跌达到该比例时做空。",
    )
    combo_long_rebound_pct = sidebar.number_input(
        "新低后做多反弹%",
        min_value=0.01,
        max_value=80.00,
        value=2.00,
        step=0.10,
        format="%.2f",
        help="同一轮里刷新新低后，价格从本轮低点反弹达到该比例时做多。",
    )
elif strategy_type == "对数低点":
    has_saved_log_residual_config = bool(saved_log_residual_config)
    saved_alpha = float(saved_log_residual_config["alpha"]) if saved_log_residual_config and saved_log_residual_config.get("alpha") is not None else 0.0
    saved_k = float(saved_log_residual_config["k"]) if saved_log_residual_config and saved_log_residual_config.get("k") is not None else None
    saved_threshold = float(saved_log_residual_config["threshold"]) if saved_log_residual_config and saved_log_residual_config.get("threshold") is not None else 0.0
    saved_trailing_stop = float(saved_log_residual_config["trailing_stop"]) if saved_log_residual_config and saved_log_residual_config.get("trailing_stop") is not None else None
    saved_train_start = saved_log_residual_config.get("train_start") if saved_log_residual_config else None
    saved_split_time = saved_log_residual_config.get("split_time") if saved_log_residual_config else None
    if saved_log_residual_config:
        saved_created = saved_log_residual_config.get("created_time")
        sidebar.caption(
            "已加载对数低点配置"
            + (f" | {pd.Timestamp(saved_created).strftime('%Y-%m-%d %H:%M')}" if saved_created is not None else "")
        )
    default_train_start = pd.Timestamp(saved_train_start) if saved_train_start is not None else pd.Timestamp(filtered_df[TIME_COL].iloc[0])
    default_split_time = pd.Timestamp(saved_split_time) if saved_split_time is not None else pd.Timestamp(filtered_df[TIME_COL].iloc[-1])
    train_start_text = sidebar.text_input(
        "训练开始",
        value=default_train_start.strftime("%Y-%m-%d %H:%M"),
        key=f"detrend_train_start_{selected_ticker}",
    )
    split_time_text = sidebar.text_input(
        "分割时间",
        value=default_split_time.strftime("%Y-%m-%d %H:%M"),
        key=f"detrend_split_time_{selected_ticker}",
    )
    residual_train_start = parse_sidebar_datetime(train_start_text, default_train_start)
    residual_split_time = parse_sidebar_datetime(split_time_text, default_split_time)
    residual_intercept_shift = sidebar.number_input(
        "趋势线 alpha / 截距调整",
        value=float(saved_alpha),
        step=0.0010,
        format="%.4f",
        help="加到 log(price) 拟合趋势线截距上。正数会抬高趋势线并压低 residual。",
        key=f"detrend_intercept_shift_{selected_ticker}",
    )
    full_idx = np.arange(len(filtered_df), dtype=float)
    full_log_price = np.log(filtered_df["price"].astype(float).to_numpy())
    default_manual_slope = float(np.polyfit(full_idx, full_log_price, 1)[0]) if len(filtered_df) >= 2 else 0.0
    manual_k_slope = sidebar.number_input(
        "k",
        value=float(saved_k if saved_k is not None else default_manual_slope),
        step=0.000020,
        format="%.6f",
        key=f"detrend_manual_k_{selected_ticker}",
    )
    residual_entry_threshold = sidebar.number_input(
        "入场 residual",
        value=float(saved_threshold),
        step=0.0010,
        format="%.4f",
        help="做多时 residual 从下向上穿越；做空时 residual 从上向下穿越。",
        key=f"detrend_entry_residual_{selected_ticker}",
    )
    residual_position_type = sidebar.selectbox(
        "回测方向",
        ["LONG_SPREAD", "SHORT_SPREAD"],
        index=0,
        help="LONG_SPREAD 做多：residual 上穿阈值且 avg78 低于阈值入场。SHORT_SPREAD 做空：residual 下穿阈值且 avg78 高于阈值入场。",
        key=f"detrend_position_type_{selected_ticker}",
    )

if strategy_type in ("MACD", "放量突破", "创新高", "回撤修复", "多空组合", "对数低点"):
    saved_trailing_stop_value = (
        float(saved_trailing_stop)
        if strategy_type == "对数低点" and "saved_trailing_stop" in locals() and saved_trailing_stop is not None
        else 2.00
    )
    strategy_stop_mode = sidebar.selectbox(
        "策略止损",
        ["固定百分比", "历史最大单日跌幅"],
        index=0,
    )
    fixed_trailing_stop_pct = sidebar.number_input(
        "固定止损%",
        min_value=0.10,
        max_value=50.0,
        value=saved_trailing_stop_value,
        step=0.10,
        format="%.2f",
        key=f"fixed_trailing_stop_pct_{strategy_type}_{selected_ticker}",
    )
    if strategy_type == "对数低点":
        save_log_residual_config_requested = sidebar.button(
            "保存对数低点配置",
            key=f"save_log_residual_config_sidebar_{selected_ticker}",
        )

historical_drop_info = compute_max_single_day_drop_info(filtered_df[[TIME_COL, "price"]].copy())
historical_pullback_info = compute_max_pullback_after_new_high_info(filtered_df[[TIME_COL, "price"]].copy())
historical_max_daily_drop_pct = float(historical_drop_info.get("drop_pct", 0.0))
historical_max_new_high_pullback_pct = float(historical_pullback_info.get("pullback_pct", 0.0))
effective_trailing_stop_pct = (
    historical_max_daily_drop_pct
    if strategy_stop_mode == "历史最大单日跌幅"
    else float(fixed_trailing_stop_pct)
)
effective_trailing_stop_pct = max(float(effective_trailing_stop_pct), 0.01)

chart_df = resample_prices(filtered_df, resample_label)
if chart_df.empty:
    st.warning("重采样后没有数据。")
    st.stop()

chart_df = prepare_trading_axis(chart_df)

latest_price = float(chart_df["price"].iloc[-1])
first_price = float(chart_df["price"].iloc[0])
change_pct = 0.0 if first_price == 0 else ((latest_price / first_price) - 1.0) * 100.0
window_high = float(chart_df["price"].max())
window_low = float(chart_df["price"].min())
whole_shares = 0 if latest_price <= 0 else int(WHOLE_SHARE_BUDGET_USD // latest_price)
whole_share_cost = whole_shares * latest_price
cash_left = WHOLE_SHARE_BUDGET_USD - whole_share_cost
budget_usage_pct = 0.0 if WHOLE_SHARE_BUDGET_USD <= 0 else (whole_share_cost / WHOLE_SHARE_BUDGET_USD) * 100.0

info_cols = st.columns(5)
info_cols[0].metric("标的", selected_ticker)
info_cols[1].metric("最新价", f"{latest_price:.4f}")
info_cols[2].metric("区间涨跌", f"{change_pct:+.2f}%")
info_cols[3].metric("区间最高", f"{window_high:.4f}")
info_cols[4].metric("区间最低", f"{window_low:.4f}")

size_cols = st.columns(4)
size_cols[0].metric("$15,000可买整股", f"{whole_shares:,} 股")
size_cols[1].metric("整股占用", f"${whole_share_cost:,.2f}")
size_cols[2].metric("占 $15,000 比例", f"{budget_usage_pct:.2f}%")
size_cols[3].metric("剩余现金", f"${cash_left:,.2f}")

st.caption(
    f"来源 `us_market_prices` | ticker `{selected_ticker}` | "
    f"数据范围 `{earliest_time.strftime('%Y-%m-%d %H:%M')}` -> `{latest_time.strftime('%Y-%m-%d %H:%M')}`"
)

figure = build_price_chart(
    df=chart_df,
    ticker=selected_ticker,
    table_name="us_market_prices",
    show_ma20=show_ma20,
    show_ma50=show_ma50,
    log_scale=log_scale,
)
st.plotly_chart(figure, use_container_width=True)

if strategy_type == "无策略":
    st.info("当前未选择策略，只显示价格图和基础统计。")
else:
    st.markdown("### 策略小游戏")
    backtest_cols = [TIME_COL, "price"]
    if "source_bar_index" in chart_df.columns:
        backtest_cols.append("source_bar_index")
    if "volume" in chart_df.columns:
        backtest_cols.append("volume")
    backtest_input_df = chart_df[backtest_cols].copy()

    if strategy_type == "MACD":
        macd_slow_span = max(int(macd_slow_span), int(macd_fast_span) + 1)
        st.caption(
            f"规则：MACD({int(macd_fast_span)}, {int(macd_slow_span)}, {int(macd_signal_span)}) "
            f"上穿 signal 时满仓买入，下穿 signal 时卖出；"
            f"{'同时使用 trailing stop ' + format(effective_trailing_stop_pct, '.2f') + '%' if macd_use_trailing_stop else '不使用 trailing stop'}。"
        )
        backtest_result = run_macd_backtest(
            backtest_input_df,
            initial_cash=WHOLE_SHARE_BUDGET_USD,
            fast_span=int(macd_fast_span),
            slow_span=int(macd_slow_span),
            signal_span=int(macd_signal_span),
            use_trailing_stop=bool(macd_use_trailing_stop),
            trailing_stop_pct=effective_trailing_stop_pct / 100.0,
        )
    elif strategy_type == "放量突破":
        has_usable_volume = "volume" in backtest_input_df.columns and backtest_input_df["volume"].notna().any()
        st.caption(
            f"规则：价格突破过去 {int(breakout_window)} 根最高价，且成交量 >= "
            f"过去 {int(breakout_volume_window)} 根均量的 {float(breakout_volume_multiple):.2f} 倍时满仓买入；"
            f"跌破 {int(breakout_exit_ma_window)} 根均线卖出；"
            f"{'同时使用 trailing stop ' + format(effective_trailing_stop_pct, '.2f') + '%' if breakout_use_trailing_stop else '不使用 trailing stop'}。"
        )
        if not has_usable_volume:
            st.warning("统一分钟价格表当前没有 volume 字段，放量突破策略需要先补充成交量数据。")
            backtest_result = {
                "trades": [],
                "equity_curve": pd.DataFrame(),
                "strategy_frame": pd.DataFrame(),
                "final_equity": WHOLE_SHARE_BUDGET_USD,
                "total_return_pct": 0.0,
                "buy_hold_return_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "win_rate_pct": 0.0,
                "trade_count": 0,
                "train_size": 0,
                "train_ratio": 0.0,
                "used_slope": 0.0,
            }
        else:
            backtest_result = run_volume_breakout_backtest(
                backtest_input_df,
                initial_cash=WHOLE_SHARE_BUDGET_USD,
                breakout_window=int(breakout_window),
                volume_window=int(breakout_volume_window),
                volume_multiple=float(breakout_volume_multiple),
                exit_ma_window=int(breakout_exit_ma_window),
                use_trailing_stop=bool(breakout_use_trailing_stop),
                trailing_stop_pct=effective_trailing_stop_pct / 100.0,
            )
    elif strategy_type == "创新高":
        st.caption(
            f"规则：价格突破当前区间此前最高价时满仓买入；"
            f"入场后从最高价回撤 {effective_trailing_stop_pct:.2f}% 时卖出。"
        )
        backtest_result = run_new_high_backtest(
            backtest_input_df,
            initial_cash=WHOLE_SHARE_BUDGET_USD,
            trailing_stop_pct=effective_trailing_stop_pct / 100.0,
        )
    elif strategy_type == "回撤修复":
        st.caption(
            f"规则：每次价格创新高后重置回撤段；段内刷新最大回撤谷底后，"
            f"价格从谷底修复达到该最大回撤幅度的 {float(drawdown_repair_ratio_pct):.2f}% 时满仓买入；"
            f"入场后从最高价回撤 {effective_trailing_stop_pct:.2f}% 时卖出。"
        )
        backtest_result = run_drawdown_repair_backtest(
            backtest_input_df,
            initial_cash=WHOLE_SHARE_BUDGET_USD,
            repair_ratio=float(drawdown_repair_ratio_pct) / 100.0,
            trailing_stop_pct=effective_trailing_stop_pct / 100.0,
        )
    elif strategy_type == "多空组合":
        st.caption(
            f"规则：每轮价格创新高后重置高低点；"
            f"从本轮高点下跌 {float(combo_short_drawdown_pct):.2f}% 时满仓做空；"
            f"同一轮刷新新低后，从本轮低点反弹 {float(combo_long_rebound_pct):.2f}% 时满仓做多；"
            f"持仓后忽略反向信号，只由 trailing stop {effective_trailing_stop_pct:.2f}% 结束。"
        )
        backtest_result = run_long_short_combo_backtest(
            backtest_input_df,
            initial_cash=WHOLE_SHARE_BUDGET_USD,
            short_drawdown_pct=float(combo_short_drawdown_pct),
            long_rebound_pct=float(combo_long_rebound_pct),
            use_trailing_stop=bool(combo_use_trailing_stop),
            trailing_stop_pct=effective_trailing_stop_pct / 100.0,
        )
    else:
        model_start_ts = min(pd.Timestamp(start_ts), pd.Timestamp(residual_train_start))
        model_source_df = raw_df.loc[
            (raw_df[TIME_COL] >= model_start_ts) & (raw_df[TIME_COL] <= end_ts)
        ].copy()
        model_chart_df = resample_prices(model_source_df, resample_label)
        model_backtest_cols = [TIME_COL, "price"]
        if "source_bar_index" in model_chart_df.columns:
            model_backtest_cols.append("source_bar_index")
        if "volume" in model_chart_df.columns:
            model_backtest_cols.append("volume")
        if not model_chart_df.empty:
            backtest_input_df = model_chart_df[model_backtest_cols].copy()

        residual_direction_label = "做空" if residual_position_type == "SHORT_SPREAD" else "做多"
        if residual_position_type == "SHORT_SPREAD":
            residual_cross_caption = (
                f"当 residual 从上向下穿越 {float(residual_entry_threshold):+.4f}，"
                f"且前 78 根 residual 均值高于该阈值时满仓做空；"
            )
            residual_stop_caption = f"入场后从最低价反弹 {effective_trailing_stop_pct:.2f}% 时买回。"
        else:
            residual_cross_caption = (
                f"当 residual 从下向上穿越 {float(residual_entry_threshold):+.4f}，"
                f"且前 78 根 residual 均值低于该阈值时满仓买入；"
            )
            residual_stop_caption = f"入场后从最高价回撤 {effective_trailing_stop_pct:.2f}% 时卖出。"
        trend_caption = (
            f"使用保存的 k={manual_k_slope:.6f}，并按训练开始/分割时间重新拟合截距；随后，"
            if has_saved_log_residual_config
            else f"当前没有保存配置，k={manual_k_slope:.6f} 来自可见区间全区域拟合默认值；随后，"
        )
        st.caption(
            f"规则：{trend_caption}alpha={float(residual_intercept_shift):+.4f}；"
            f"{residual_direction_label}；"
            f"{residual_cross_caption}"
            f"{residual_stop_caption}"
        )
        backtest_result = run_breakout_trailing_backtest(
            backtest_input_df,
            initial_cash=WHOLE_SHARE_BUDGET_USD,
            trailing_stop_pct=effective_trailing_stop_pct / 100.0,
            trend_mode="fixed_k_fit_intercept",
            manual_slope=float(manual_k_slope),
            intercept_shift=float(residual_intercept_shift),
            entry_residual_threshold=float(residual_entry_threshold),
            fit_start=residual_train_start,
            fit_end=residual_split_time,
            position_type=residual_position_type,
        )

    strategy_cols = st.columns(5)
    strategy_cols[0].metric("初始资金", f"${WHOLE_SHARE_BUDGET_USD:,.0f}")
    strategy_cols[1].metric("最终资金", f"${float(backtest_result['final_equity']):,.2f}")
    strategy_cols[2].metric("策略收益", f"{float(backtest_result['total_return_pct']):+.2f}%")
    strategy_cols[3].metric("买入持有", f"{float(backtest_result['buy_hold_return_pct']):+.2f}%")
    strategy_cols[4].metric("最大回撤", f"{float(backtest_result['max_drawdown_pct']):+.2f}%")

    strategy_cols_2 = st.columns(3)
    strategy_cols_2[0].metric("交易次数", int(backtest_result["trade_count"]))
    strategy_cols_2[1].metric("胜率", f"{float(backtest_result['win_rate_pct']):.2f}%")
    strategy_cols_2[2].metric("止损规则", f"Trailing {effective_trailing_stop_pct:.2f}%")

    strategy_cols_3 = st.columns(4)
    strategy_cols_3[0].metric("策略类型", strategy_type)
    daily_drop_help = "按每日最后一根价格与前一日最后一根价格比较。"
    if historical_drop_info.get("trough_time") is not None:
        daily_drop_help += (
            f"\n前一日收盘：{pd.Timestamp(historical_drop_info['prev_close_time']).strftime('%Y-%m-%d %H:%M')} "
            f"@ {float(historical_drop_info['prev_close_price']):.4f}"
            f"\n谷底收盘：{pd.Timestamp(historical_drop_info['trough_time']).strftime('%Y-%m-%d %H:%M')} "
            f"@ {float(historical_drop_info['trough_price']):.4f}"
        )
    new_high_pullback_help = "按新高分段计算：每次创新高开启一段，下一次新高出现时前一段结束；谷底只在本段内寻找。"
    if historical_pullback_info.get("trough_time") is not None:
        new_high_pullback_help += (
            f"\n峰值：{pd.Timestamp(historical_pullback_info['peak_time']).strftime('%Y-%m-%d %H:%M')} "
            f"@ {float(historical_pullback_info['peak_price']):.4f}"
            f"\n谷底：{pd.Timestamp(historical_pullback_info['trough_time']).strftime('%Y-%m-%d %H:%M')} "
            f"@ {float(historical_pullback_info['trough_price']):.4f}"
        )
        if historical_pullback_info.get("segment_end_time") is not None:
            new_high_pullback_help += (
                f"\n本段结束：{pd.Timestamp(historical_pullback_info['segment_end_time']).strftime('%Y-%m-%d %H:%M')}"
            )
    strategy_cols_3[1].metric(
        "历史最大单日跌幅",
        f"{historical_max_daily_drop_pct:.2f}%",
        help=daily_drop_help,
    )
    strategy_cols_3[2].metric(
        "创高后最大回撤",
        f"{historical_max_new_high_pullback_pct:.2f}%",
        help=new_high_pullback_help,
    )
    if strategy_type == "MACD":
        strategy_cols_3[3].metric(
            "MACD 参数",
            f"{int(macd_fast_span)}/{int(macd_slow_span)}/{int(macd_signal_span)}",
        )
    elif strategy_type == "放量突破":
        strategy_cols_3[3].metric(
            "突破/放量",
            f"{int(breakout_window)} / {float(breakout_volume_multiple):.2f}x",
        )
    elif strategy_type == "创新高":
        signal_count = 0
        strategy_frame_for_count = backtest_result.get("strategy_frame")
        if isinstance(strategy_frame_for_count, pd.DataFrame) and "new_high_signal" in strategy_frame_for_count.columns:
            signal_count = int(strategy_frame_for_count["new_high_signal"].sum())
        strategy_cols_3[3].metric("新高信号", signal_count)
    elif strategy_type == "回撤修复":
        signal_count = 0
        strategy_frame_for_count = backtest_result.get("strategy_frame")
        if isinstance(strategy_frame_for_count, pd.DataFrame) and "new_high_signal" in strategy_frame_for_count.columns:
            signal_count = int(strategy_frame_for_count["new_high_signal"].sum())
        strategy_cols_3[3].metric("修复信号", f"{signal_count} / {float(drawdown_repair_ratio_pct):.0f}%")
    elif strategy_type == "多空组合":
        long_count = sum(1 for trade in backtest_result.get("trades", []) if int(trade.get("shares") or 0) > 0)
        short_count = sum(1 for trade in backtest_result.get("trades", []) if int(trade.get("shares") or 0) < 0)
        strategy_cols_3[3].metric("多/空次数", f"{long_count} / {short_count}")
    else:
        strategy_cols_3[3].metric("当前 k", f"{float(backtest_result['used_slope']):.6f}")

    if strategy_type == "创新高":
        strategy_frame_for_order = backtest_result.get("strategy_frame")
        order_train_start = chart_df[TIME_COL].iloc[0]
        order_split_time = None
        order_previous_high = window_high
        if isinstance(strategy_frame_for_order, pd.DataFrame) and not strategy_frame_for_order.empty:
            order_train_start = strategy_frame_for_order[TIME_COL].iloc[0]
            if "prev_high" in strategy_frame_for_order.columns:
                last_prev_high = pd.to_numeric(strategy_frame_for_order["prev_high"], errors="coerce").dropna()
                if not last_prev_high.empty:
                    order_previous_high = float(last_prev_high.iloc[-1])

        st.markdown("#### Schedule / Reentry")
        order_cols = st.columns([1.0, 1.2, 1.2, 1.2])
        order_cols[0].metric("Stock", selected_ticker)
        order_cols[1].metric("Strategy", "New High")
        order_cols[2].metric("Trigger", f"{order_previous_high:.4f}")
        order_cols[3].metric("Trailing", f"{effective_trailing_stop_pct:.2f}%")

        with st.form(f"new_high_single_order_{selected_ticker}"):
            form_cols = st.columns([1.2, 1.2, 1.2, 2.0])
            new_high_position_type = form_cols[0].selectbox(
                "方向",
                ["LONG_SPREAD"],
                index=0,
                help="New High defaults to LONG_SPREAD for buying the single stock.",
            )
            new_high_exposure_pct = form_cols[1].number_input(
                "Exposure%",
                min_value=0.01,
                max_value=100.00,
                value=10.00,
                step=0.25,
                format="%.2f",
            )
            new_high_auto_pending = form_cols[2].checkbox(
                "Auto Pending",
                value=False,
                help="When the Waiting Entry fires, waiting_entry.py may create the PENDING order automatically.",
            )
            form_cols[3].caption(
                "Schedule creates a pending single order. Start Reentry writes a Waiting Entry "
                "that fires when price breaks the current previous high."
            )
            action_cols = st.columns([1.0, 1.0, 2.0])
            submit_new_high_order = action_cols[0].form_submit_button("Schedule")
            submit_new_high_reentry = action_cols[1].form_submit_button("Start Reentry")

        if submit_new_high_order:
            try:
                order_result = create_single_scheduled_order(
                    single_name=selected_ticker,
                    train_start=order_train_start,
                    split_time=order_split_time,
                    strategy="NEW_HIGH",
                    param=float(order_previous_high),
                    position_type=new_high_position_type,
                    exposure_pct=float(new_high_exposure_pct),
                )
            except pymysql.err.IntegrityError as exc:
                st.error(
                    f"{selected_ticker} already has an active single order. "
                    "Handle or close it in the GUI before adding another."
                )
                st.caption(str(exc))
            except Exception as exc:
                st.error(f"Failed to create New High single order: {exc}")
            else:
                st.success(
                    f"Created New High single order id={order_result['order_id']} | "
                    f"{order_result['single_name']} | {new_high_position_type} | "
                    f"trigger={order_previous_high:.4f} | exposure={float(new_high_exposure_pct):.2f}%"
                )

        if submit_new_high_reentry:
            try:
                waiting_result = create_new_high_waiting_entry(
                    stock_name=selected_ticker,
                    train_start=order_train_start,
                    split_time=order_split_time,
                    previous_high=float(order_previous_high),
                    auto_schedule_enabled=bool(new_high_auto_pending),
                    exposure_pct=float(new_high_exposure_pct),
                    order_position_type=new_high_position_type,
                )
            except Exception as exc:
                st.error(f"Failed to write New High Waiting Entry: {exc}")
            else:
                st.success(
                    f"Wrote New High Waiting Entry | {waiting_result['pair_name']} | "
                    f"lock={waiting_result['reentry_lock']} | trigger={waiting_result['trigger_price']:.4f}"
                    + (
                        f" | auto pending {new_high_position_type} {float(new_high_exposure_pct):.2f}%"
                        if new_high_auto_pending
                        else ""
                    )
                )

    if strategy_type == "回撤修复":
        strategy_frame_for_order = backtest_result.get("strategy_frame")
        order_train_start = chart_df[TIME_COL].iloc[0]
        order_split_time = None
        order_peak_price = np.nan
        order_trough_price = np.nan
        order_trigger_price = np.nan
        order_repair_ratio = float(drawdown_repair_ratio_pct) / 100.0
        if isinstance(strategy_frame_for_order, pd.DataFrame) and not strategy_frame_for_order.empty:
            order_train_start = strategy_frame_for_order[TIME_COL].iloc[0]
            last_row = strategy_frame_for_order.iloc[-1]
            if "drawdown_peak_price" in strategy_frame_for_order.columns:
                order_peak_price = float(last_row.get("drawdown_peak_price"))
            if "drawdown_trough_price" in strategy_frame_for_order.columns:
                order_trough_price = float(last_row.get("drawdown_trough_price"))
            if "repair_target_price" in strategy_frame_for_order.columns and pd.notna(last_row.get("repair_target_price")):
                order_trigger_price = float(last_row.get("repair_target_price"))

        has_repair_trigger = (
            np.isfinite(order_peak_price)
            and np.isfinite(order_trough_price)
            and np.isfinite(order_trigger_price)
            and order_peak_price > order_trough_price
        )

        st.markdown("#### Schedule / Reentry")
        order_cols = st.columns([1.0, 1.2, 1.1, 1.1, 1.1, 1.0])
        order_cols[0].metric("Stock", selected_ticker)
        order_cols[1].metric("Strategy", "Drawdown Repair")
        order_cols[2].metric("Peak", "-" if not np.isfinite(order_peak_price) else f"{order_peak_price:.4f}")
        order_cols[3].metric("Trough", "-" if not np.isfinite(order_trough_price) else f"{order_trough_price:.4f}")
        order_cols[4].metric("Trigger", "-" if not np.isfinite(order_trigger_price) else f"{order_trigger_price:.4f}")
        order_cols[5].metric("Repair", f"{float(drawdown_repair_ratio_pct):.2f}%")

        if not has_repair_trigger:
            st.info("当前段还没有有效回撤，暂时不能创建回撤修复的触发订单。")

        with st.form(f"drawdown_repair_single_order_{selected_ticker}"):
            form_cols = st.columns([1.2, 1.2, 1.2, 2.0])
            repair_position_type = form_cols[0].selectbox(
                "方向",
                ["LONG_SPREAD"],
                index=0,
                help="Drawdown Repair 当前按回撤修复做多单股。",
            )
            repair_exposure_pct = form_cols[1].number_input(
                "Exposure%",
                min_value=0.01,
                max_value=100.00,
                value=10.00,
                step=0.25,
                format="%.2f",
            )
            repair_auto_pending = form_cols[2].checkbox(
                "Auto Pending",
                value=False,
                help="When the Waiting Entry fires, waiting_entry.py may create the PENDING order automatically after it supports this strategy.",
            )
            form_cols[3].caption(
                "Schedule writes a pending single order with the current trigger. "
                "Start Reentry writes peak/trough/repair ratio/trigger into pair_reentry for waiting_entry.py."
            )
            action_cols = st.columns([1.0, 1.0, 2.0])
            submit_repair_order = action_cols[0].form_submit_button("Schedule")
            submit_repair_reentry = action_cols[1].form_submit_button("Start Reentry")

        if submit_repair_order:
            if not has_repair_trigger:
                st.error("当前没有有效触发价，无法创建 Drawdown Repair 单股订单。")
            else:
                try:
                    order_result = create_drawdown_repair_scheduled_order(
                        single_name=selected_ticker,
                        train_start=order_train_start,
                        split_time=order_split_time,
                        trigger_price=float(order_trigger_price),
                        position_type=repair_position_type,
                        exposure_pct=float(repair_exposure_pct),
                    )
                except pymysql.err.IntegrityError as exc:
                    st.error(
                        f"{selected_ticker} already has an active single order. "
                        "Handle or close it in the GUI before adding another."
                    )
                    st.caption(str(exc))
                except Exception as exc:
                    st.error(f"Failed to create Drawdown Repair single order: {exc}")
                else:
                    st.success(
                        f"Created Drawdown Repair single order id={order_result['order_id']} | "
                        f"{order_result['single_name']} | {repair_position_type} | "
                        f"trigger={order_trigger_price:.4f} | exposure={float(repair_exposure_pct):.2f}%"
                    )

        if submit_repair_reentry:
            if not has_repair_trigger:
                st.error("当前没有有效触发价，无法写入 Drawdown Repair Waiting Entry。")
            else:
                try:
                    waiting_result = create_drawdown_repair_waiting_entry(
                        stock_name=selected_ticker,
                        train_start=order_train_start,
                        split_time=order_split_time,
                        peak_price=float(order_peak_price),
                        trough_price=float(order_trough_price),
                        repair_ratio=float(order_repair_ratio),
                        trigger_price=float(order_trigger_price),
                        auto_schedule_enabled=bool(repair_auto_pending),
                        exposure_pct=float(repair_exposure_pct),
                        order_position_type=repair_position_type,
                    )
                except Exception as exc:
                    st.error(f"Failed to write Drawdown Repair Waiting Entry: {exc}")
                else:
                    st.success(
                        f"Wrote Drawdown Repair Waiting Entry | {waiting_result['pair_name']} | "
                        f"lock={waiting_result['reentry_lock']} | "
                        f"peak={waiting_result['peak_price']:.4f} | "
                        f"trough={waiting_result['trough_price']:.4f} | "
                        f"repair={waiting_result['repair_ratio'] * 100.0:.2f}% | "
                        f"trigger={waiting_result['trigger_price']:.4f}"
                        + (
                            f" | auto pending {repair_position_type} {float(repair_exposure_pct):.2f}%"
                            if repair_auto_pending
                            else ""
                        )
                    )

    if strategy_type == "对数低点":
        strategy_frame_for_order = backtest_result.get("strategy_frame")
        order_train_start = chart_df[TIME_COL].iloc[0]
        order_split_time = None
        order_last_residual = np.nan
        order_k = float(backtest_result.get("used_slope") or 0.0)
        order_intercept = float(backtest_result.get("used_intercept") or 0.0)
        order_waiting_intercept = order_intercept
        order_entry_residual = float(backtest_result.get("entry_residual") or 0.0)
        if isinstance(strategy_frame_for_order, pd.DataFrame) and not strategy_frame_for_order.empty:
            order_train_start = strategy_frame_for_order[TIME_COL].iloc[0]
            order_train_start = residual_train_start
            order_split_time = residual_split_time
            if "residual" in strategy_frame_for_order.columns:
                order_last_residual = float(strategy_frame_for_order["residual"].iloc[-1])

        st.markdown("#### 下单测试")
        try:
            order_waiting_intercept = compute_waiting_entry_intercept(
                raw_df,
                order_train_start,
                order_split_time,
                order_k,
                residual_intercept_shift,
            )
        except Exception:
            order_waiting_intercept = order_intercept

        order_cols = st.columns([1.0, 1.2, 1.0, 1.15, 1.15, 1.4])
        order_cols[0].metric("Stock", selected_ticker)
        order_cols[1].metric("Strategy", "对数低点")
        order_cols[2].metric(
            "k / Param",
            f"{order_k:.6f}",
        )
        order_cols[3].metric(
            "Intercept",
            f"{order_waiting_intercept:.6f}",
        )
        order_cols[4].metric(
            "Residual",
            f"{order_entry_residual:.4f}",
        )
        order_cols[5].metric(
            "Split Time",
            pd.Timestamp(order_split_time).strftime("%Y-%m-%d %H:%M") if order_split_time is not None else "-",
        )

        if save_log_residual_config_requested:
            try:
                save_log_residual_config(
                    stock_name=selected_ticker,
                    train_start=order_train_start,
                    split_time=order_split_time,
                    alpha=float(residual_intercept_shift),
                    intercept=float(order_waiting_intercept),
                    k=float(order_k),
                    threshold=float(order_entry_residual),
                    trailing_stop=float(effective_trailing_stop_pct),
                )
            except Exception as exc:
                st.error(f"保存对数低点配置失败：{exc}")
            else:
                st.success(
                    f"已保存 {selected_ticker} 对数低点配置 | "
                    f"alpha={float(residual_intercept_shift):+.4f} | "
                    f"intercept={order_waiting_intercept:.6f} | "
                    f"k={order_k:.6f} | threshold={order_entry_residual:+.4f} | "
                    f"trailing={effective_trailing_stop_pct:.2f}%"
                )

        with st.form(f"log_residual_single_order_{selected_ticker}"):
            form_cols = st.columns([1.2, 1.2, 1.2, 2.0])
            single_order_position_type = form_cols[0].selectbox(
                "方向",
                ["LONG_SPREAD", "SHORT_SPREAD"],
                index=1 if residual_position_type == "SHORT_SPREAD" else 0,
                help="LONG_SPREAD 会买入股票；SHORT_SPREAD 会做空股票。",
            )
            single_order_exposure_pct = form_cols[1].number_input(
                "Exposure%",
                min_value=0.01,
                max_value=100.00,
                value=10.00,
                step=0.25,
                format="%.2f",
            )
            single_auto_pending = form_cols[2].checkbox(
                "Auto Pending",
                value=False,
                help="When the Waiting Entry fires, waiting_entry.py may create the PENDING order automatically.",
            )
            form_cols[3].caption(
                "提交后会进入单股待执行订单，"
                "再到 GUI 的单股管理里执行开仓。"
            )
            action_cols = st.columns([1.0, 1.0, 2.0])
            submit_single_order = action_cols[0].form_submit_button("加入单股待执行订单")
            submit_waiting_entry = action_cols[1].form_submit_button("Waiting Entry")

        if submit_single_order:
            try:
                order_result = create_single_scheduled_order(
                    single_name=selected_ticker,
                    train_start=order_train_start,
                    split_time=order_split_time,
                    strategy="LOG_RESIDUAL",
                    param=float(order_last_residual),
                    position_type=single_order_position_type,
                    exposure_pct=float(single_order_exposure_pct),
                )
            except pymysql.err.IntegrityError as exc:
                st.error(
                    f"{selected_ticker} 已经有待执行或持仓中的单股订单，"
                    "请先在 GUI 里处理或关闭后再新增。"
                )
                st.caption(str(exc))
            except Exception as exc:
                st.error(f"写入单股订单失败：{exc}")
            else:
                st.success(
                    f"已创建单股订单 id={order_result['order_id']} | "
                    f"{order_result['single_name']} | {single_order_position_type} | "
                    f"param={order_last_residual:.4f} | exposure={float(single_order_exposure_pct):.2f}%"
                )

        if submit_waiting_entry:
            try:
                waiting_result = create_log_residual_waiting_entry(
                    stock_name=selected_ticker,
                    train_start=order_train_start,
                    split_time=order_split_time,
                    strategy="LOG_RESIDUAL",
                    param=float(order_k),
                    intercept=float(order_waiting_intercept),
                    residual=float(order_entry_residual),
                    auto_schedule_enabled=bool(single_auto_pending),
                    exposure_pct=float(single_order_exposure_pct),
                    order_position_type=single_order_position_type,
                )
            except Exception as exc:
                st.error(f"写入 Waiting Entry 失败：{exc}")
            else:
                st.success(
                    f"已写入 Waiting Entry | {waiting_result['pair_name']} | "
                    f"lock={waiting_result['reentry_lock']} | "
                    f"k={order_k:.6f} | intercept={order_waiting_intercept:.6f} | "
                    f"residual={order_entry_residual:.4f}"
                    + (
                        f" | auto pending {single_order_position_type} {float(single_order_exposure_pct):.2f}%"
                        if single_auto_pending
                        else ""
                    )
                )

    strategy_frame = backtest_result["strategy_frame"]
    if isinstance(strategy_frame, pd.DataFrame) and not strategy_frame.empty:
        strategy_frame = strategy_frame.loc[
            (pd.to_datetime(strategy_frame[TIME_COL]) >= pd.Timestamp(start_ts))
            & (pd.to_datetime(strategy_frame[TIME_COL]) <= pd.Timestamp(end_ts))
        ].copy()
    if isinstance(strategy_frame, pd.DataFrame) and strategy_frame.empty:
        st.warning("当前数据量不足，暂时无法运行这个策略。")
    elif strategy_type == "MACD":
        strategy_figure = build_macd_strategy_chart(
            strategy_frame,
            list(backtest_result["trades"]),
            selected_ticker,
        )
        st.plotly_chart(strategy_figure, use_container_width=True)
    elif strategy_type == "放量突破":
        strategy_figure = build_volume_breakout_chart(
            strategy_frame,
            list(backtest_result["trades"]),
            selected_ticker,
        )
        st.plotly_chart(strategy_figure, use_container_width=True)
    elif strategy_type == "多空组合":
        strategy_figure = build_long_short_combo_chart(
            strategy_frame,
            list(backtest_result["trades"]),
            selected_ticker,
        )
        st.plotly_chart(strategy_figure, use_container_width=True)
    elif strategy_type in ("创新高", "回撤修复"):
        strategy_figure = build_strategy_chart(
            strategy_frame,
            list(backtest_result["trades"]),
            selected_ticker,
            0,
        )
        render_plotly_with_hover_spike(strategy_figure, height=740)
    else:
        strategy_figure = build_strategy_chart(
            strategy_frame,
            list(backtest_result["trades"]),
            selected_ticker,
            int(backtest_result["train_size"]),
        )
        render_plotly_with_hover_spike(strategy_figure, height=740)

    strategy_trades = list(backtest_result["trades"])
    if strategy_trades:
        trade_log_df = pd.DataFrame(strategy_trades)
        trade_log_df["entry_time"] = pd.to_datetime(trade_log_df["entry_time"]).dt.strftime("%Y-%m-%d %H:%M")
        trade_log_df["exit_time"] = pd.to_datetime(trade_log_df["exit_time"]).dt.strftime("%Y-%m-%d %H:%M")
        signal_label = (
            "MACD"
            if strategy_type == "MACD"
            else (
                "量比"
                if strategy_type == "放量突破"
                else (
                    "前高"
                    if strategy_type == "创新高"
                    else ("修复%" if strategy_type == "回撤修复" else ("幅度%" if strategy_type == "多空组合" else "残差"))
                )
            )
        )
        trade_log_df = trade_log_df.rename(
            columns={
                "entry_time": "入场时间",
                "exit_time": "出场时间",
                "entry_price": "入场价",
                "exit_price": "出场价",
                "entry_residual": f"入场{signal_label}",
                "exit_residual": f"出场{signal_label}",
                "shares": "股数",
                "pnl": "盈亏$",
                "return_pct": "收益%",
                "exit_reason": "出场原因",
            }
        )
        st.dataframe(trade_log_df, use_container_width=True, hide_index=True)
    else:
        if strategy_type == "MACD":
            st.info("当前测试区间里还没有触发任何一次 MACD 上穿买入。")
        elif strategy_type == "放量突破":
            st.info("当前测试区间里还没有触发任何一次放量突破买入。")
        elif strategy_type == "创新高":
            st.info("当前测试区间里还没有触发任何一次创新高买入。")
        elif strategy_type == "多空组合":
            st.info("当前测试区间里还没有触发任何一次多空组合开仓。")
        else:
            st.info("当前测试区间里还没有触发任何一次 residual 从下向上穿越入场线。")

with st.expander("数据表"):
    preview_df = filtered_df.copy()
    preview_df[TIME_COL] = preview_df[TIME_COL].dt.strftime("%Y-%m-%d %H:%M")
    st.dataframe(preview_df, use_container_width=True, hide_index=True)
