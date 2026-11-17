#!/usr/bin/env python3
"""Streamlit reader for fixed-window daily ADF Finder result CSV files."""

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import pymysql
import streamlit as st
from plotly.subplots import make_subplots
from statsmodels.tsa.stattools import adfuller


APP_DIR = Path(__file__).resolve().parent
RESULT_PATTERN = "adf_80d_*pairs_*.csv"
DB_CONFIG = {
    "host": "127.0.0.1",
    "user": "quant",
    "password": "1q2w3e",
    "database": "quant_data",
}
RESULT_COLUMNS = [
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
NUMERIC_COLUMNS = [
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


def list_result_files() -> list[Path]:
    return sorted(
        APP_DIR.glob(RESULT_PATTERN),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )


@st.cache_data(ttl=60, show_spinner=False)
def load_active_day_symbols() -> list[str]:
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT ticker
                FROM us_day_info
                WHERE is_active = 1
                ORDER BY ticker
                """
            )
            return [
                str(row[0]).strip().upper()
                for row in cursor.fetchall()
                if row and str(row[0]).strip()
            ]
    finally:
        conn.close()


@st.cache_data(show_spinner="Loading ADF result CSV...")
def load_result_csv(path_text: str, mtime_ns: int) -> pd.DataFrame:
    del mtime_ns
    path = Path(path_text)
    frame = pd.read_csv(
        path,
        usecols=lambda column: column in RESULT_COLUMNS,
        dtype={"pair_name": "string", "y": "string", "x": "string"},
    )
    missing = set(RESULT_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"CSV missing required columns: {sorted(missing)}")

    for column in NUMERIC_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["train_start"] = pd.to_datetime(frame["train_start"], errors="coerce")
    frame["train_end"] = pd.to_datetime(frame["train_end"], errors="coerce")
    frame = frame.dropna(
        subset=["pair_name", "y", "x", "train_start", "train_end", "adf_abs"]
    ).reset_index(drop=True)
    frame.insert(0, "rank", np.arange(1, len(frame) + 1, dtype=np.int64))
    return frame


@st.cache_data(ttl=300, show_spinner="Loading selected daily pair...")
def load_daily_pair(
    y_symbol: str,
    x_symbol: str,
    train_start: str,
    train_end: str,
) -> pd.DataFrame:
    conn = pymysql.connect(**DB_CONFIG)
    try:
        sql = """
            SELECT
                y.trade_date,
                y.price AS y_price,
                x.price AS x_price
            FROM us_market_days y
            JOIN us_market_days x
              ON x.ticker = %s
             AND x.trade_date = y.trade_date
            WHERE y.ticker = %s
              AND y.trade_date >= %s
              AND y.trade_date <= %s
            ORDER BY y.trade_date
        """
        frame = pd.read_sql(
            sql,
            conn,
            params=(x_symbol, y_symbol, train_start, train_end),
        )
    finally:
        conn.close()

    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame["y_price"] = pd.to_numeric(frame["y_price"], errors="coerce")
    frame["x_price"] = pd.to_numeric(frame["x_price"], errors="coerce")
    return frame.dropna().reset_index(drop=True)


def recompute_pair(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, float]]:
    if len(frame) < 20:
        raise ValueError(f"Only {len(frame)} aligned observations are available.")

    y = frame["y_price"].to_numpy(dtype=float)
    x = frame["x_price"].to_numpy(dtype=float)
    if float(np.var(x)) <= 1e-12:
        raise ValueError("X price variance is zero.")

    beta, intercept = np.polyfit(x, y, 1)
    spread = y - (beta * x + intercept)
    result = adfuller(spread, autolag="AIC")
    spread_mean = float(np.mean(spread))
    spread_sigma = float(np.std(spread, ddof=1))
    denominator = float(y[-1] + abs(beta) * x[-1])
    two_sigma_return_pct = (
        float(2.0 * spread_sigma / denominator * 100.0)
        if abs(denominator) > 1e-12
        else np.nan
    )

    analysis = frame.copy()
    analysis["spread"] = spread
    analysis["zscore"] = (
        (analysis["spread"] - spread_mean) / spread_sigma
        if spread_sigma > 1e-12
        else np.nan
    )
    analysis["y_normalized"] = analysis["y_price"] / analysis["y_price"].iloc[0] * 100.0
    analysis["x_normalized"] = analysis["x_price"] / analysis["x_price"].iloc[0] * 100.0
    stats = {
        "beta": float(beta),
        "intercept": float(intercept),
        "adf_stat": float(result[0]),
        "adf_abs": abs(float(result[0])),
        "adf_pvalue": float(result[1]),
        "spread_mean": spread_mean,
        "spread_sigma": spread_sigma,
        "two_sigma_return_pct": two_sigma_return_pct,
        "latest_z": float(analysis["zscore"].iloc[-1]),
    }
    return analysis, stats


def parse_ticker_tokens(ticker_text: str) -> list[str]:
    return [
        token.strip().upper()
        for token in str(ticker_text).replace(",", " ").replace("/", " ").split()
        if token.strip()
    ]


def clear_text_input(key: str) -> None:
    st.session_state[key] = ""


def filter_results(
    frame: pd.DataFrame,
    ticker_text: str,
    min_adf_abs: float,
    max_pvalue: float,
    beta_sign: str,
) -> pd.DataFrame:
    filtered = frame[
        (frame["adf_abs"] >= float(min_adf_abs))
        & (frame["adf_pvalue"] <= float(max_pvalue))
    ]

    tokens = parse_ticker_tokens(ticker_text)
    if len(tokens) == 1:
        filtered = filtered[
            (filtered["y"].str.upper() == tokens[0])
            | (filtered["x"].str.upper() == tokens[0])
        ]
    elif len(tokens) >= 2:
        first, second = tokens[0], tokens[1]
        y_upper = filtered["y"].str.upper()
        x_upper = filtered["x"].str.upper()
        filtered = filtered[
            ((y_upper == first) & (x_upper == second))
            | ((y_upper == second) & (x_upper == first))
        ]

    if beta_sign == "Positive":
        filtered = filtered[filtered["beta"] > 0]
    elif beta_sign == "Negative":
        filtered = filtered[filtered["beta"] < 0]
    return filtered


def metric_text(value: Any, digits: int = 4) -> str:
    number = pd.to_numeric(value, errors="coerce")
    return "n/a" if pd.isna(number) else f"{float(number):.{digits}f}"


def daily_background_intervals(observation_count: int) -> list[tuple[float, float, int]]:
    """Return close-to-close spans so every daily point sits on a color boundary."""
    return [
        (float(index), float(index + 1), index % 2)
        for index in range(max(0, int(observation_count) - 1))
    ]


def build_pair_chart(
    frame: pd.DataFrame,
    y_symbol: str,
    x_symbol: str,
) -> go.Figure:
    plot_frame = frame.copy()
    plot_frame["trade_index"] = np.arange(len(plot_frame), dtype=int)
    plot_frame["trade_date_label"] = pd.to_datetime(
        plot_frame["trade_date"]
    ).dt.strftime("%Y-%m-%d")
    figure = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        row_heights=[0.34, 0.38, 0.28],
        subplot_titles=("Z-score", "Normalized daily prices", "OLS spread"),
    )
    figure.add_trace(
        go.Scatter(
            x=plot_frame["trade_index"],
            y=plot_frame["zscore"],
            name="Z-score",
            line={"color": "#0f766e"},
            customdata=plot_frame["trade_date_label"],
            hovertemplate="%{customdata}<br>Z=%{y:.3f}<extra></extra>",
        ),
        row=1,
        col=1,
    )
    background_colors = (
        "rgba(255, 243, 205, 0.28)",
        "rgba(210, 244, 222, 0.24)",
    )
    for x0, x1, color_index in daily_background_intervals(len(plot_frame)):
        figure.add_vrect(
            x0=x0,
            x1=x1,
            fillcolor=background_colors[color_index % len(background_colors)],
            opacity=1.0,
            line_width=0,
            layer="below",
            row=1,
            col=1,
        )
    for level, color in ((2.0, "#dc2626"), (-2.0, "#dc2626"), (0.0, "#64748b")):
        figure.add_hline(
            y=level,
            line_dash="dash" if level else "dot",
            line_color=color,
            row=1,
            col=1,
        )
    figure.add_trace(
        go.Scatter(
            x=plot_frame["trade_index"],
            y=plot_frame["y_normalized"],
            name=f"{y_symbol} normalized",
            customdata=plot_frame["trade_date_label"],
            hovertemplate="%{customdata}<br>"
            + f"{y_symbol} normalized"
            + "=%{y:.3f}<extra></extra>",
        ),
        row=2,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=plot_frame["trade_index"],
            y=plot_frame["x_normalized"],
            name=f"{x_symbol} normalized",
            customdata=plot_frame["trade_date_label"],
            hovertemplate="%{customdata}<br>"
            + f"{x_symbol} normalized"
            + "=%{y:.3f}<extra></extra>",
        ),
        row=2,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=plot_frame["trade_index"],
            y=plot_frame["spread"],
            name="Spread",
            line={"color": "#7c3aed"},
            customdata=plot_frame["trade_date_label"],
            hovertemplate="%{customdata}<br>Spread=%{y:.4f}<extra></extra>",
        ),
        row=3,
        col=1,
    )
    figure.add_hline(y=0.0, line_dash="dot", line_color="#64748b", row=3, col=1)
    figure.update_layout(
        height=820,
        hovermode="x unified",
        margin={"l": 45, "r": 25, "t": 70, "b": 40},
        legend={"orientation": "h", "y": 1.03},
    )
    tick_step = max(1, int(np.ceil(len(plot_frame) / 12)))
    tick_rows = plot_frame.iloc[::tick_step]
    if not plot_frame.empty and tick_rows.index[-1] != plot_frame.index[-1]:
        tick_rows = pd.concat([tick_rows, plot_frame.iloc[[-1]]])
    figure.update_xaxes(
        tickmode="array",
        tickvals=tick_rows["trade_index"].tolist(),
        ticktext=tick_rows["trade_date_label"].tolist(),
        tickangle=-35,
        title_text="Trading day",
        row=3,
        col=1,
    )
    return figure


def main() -> None:
    st.set_page_config(page_title="Daily ADF Finder Reader", layout="wide")
    st.title("Daily ADF Finder Reader")
    st.caption(
        "Reads fixed 80-trading-day all-pairs results from adf_finder and "
        "reconstructs the selected pair from quant_data.us_market_days."
    )

    files = list_result_files()
    if not files:
        st.error(f"No files matching {RESULT_PATTERN} were found in {APP_DIR}.")
        st.stop()

    selected_file = st.sidebar.selectbox(
        "ADF result CSV",
        files,
        format_func=lambda path: path.name,
    )
    results = load_result_csv(str(selected_file), selected_file.stat().st_mtime_ns)

    st.sidebar.header("Filters")
    active_symbols_only = st.sidebar.checkbox(
        "Current active symbols only",
        value=True,
        help=(
            "Hide historical CSV rows when either leg is currently inactive "
            "in quant_data.us_day_info."
        ),
    )
    if active_symbols_only:
        active_symbols = set(load_active_day_symbols())
        before_active_filter = len(results)
        results = results[
            results["y"].str.upper().isin(active_symbols)
            & results["x"].str.upper().isin(active_symbols)
        ].copy()
        st.sidebar.caption(
            f"Active filter removed {before_active_filter - len(results):,} historical rows."
        )

    ticker_text = st.sidebar.text_input(
        "Ticker or pair",
        help="Examples: SPY or SPY VOO",
    )
    max_adf = max(float(results["adf_abs"].max()), 3.0)
    min_adf_abs = st.sidebar.number_input(
        "Minimum |ADF|",
        min_value=2.0,
        max_value=max_adf,
        value=3.0,
        step=0.1,
    )
    max_pvalue = st.sidebar.number_input(
        "Maximum p-value",
        min_value=0.0,
        max_value=1.0,
        value=1.0,
        step=0.01,
        format="%.4f",
    )
    beta_sign = st.sidebar.selectbox("Beta sign", ["Any", "Positive", "Negative"])
    filtered = filter_results(
        results,
        ticker_text=ticker_text,
        min_adf_abs=min_adf_abs,
        max_pvalue=max_pvalue,
        beta_sign=beta_sign,
    )
    st.sidebar.metric("Matching rows", f"{len(filtered):,}")
    if filtered.empty:
        st.warning("No pairs match the current filters.")
        st.stop()

    search_tokens = parse_ticker_tokens(ticker_text)
    if search_tokens:
        # A searched ticker is the group, regardless of whether the fixed CSV
        # regression stored it as Y or X. This also bypasses the group limit so
        # an exact query can never disappear from Level 1.
        selected_group = (
            search_tokens[0]
            if len(search_tokens) == 1
            else f"{search_tokens[0]} / {search_tokens[1]}"
        )
        best = filtered.iloc[0]
        group_label = (
            f"{selected_group} · best #{int(best['rank']):,} "
            f"{best['y']}/{best['x']} · |ADF| {float(best['adf_abs']):.4f} · "
            f"{len(filtered):,} pairs"
        )
        st.selectbox(
            "Level 1 · Exact ticker search",
            [selected_group],
            format_func=lambda _group: group_label,
        )
        candidates = filtered.copy()
    else:
        y_counts = filtered["y"].value_counts()
        x_counts = filtered["x"].value_counts()
        group_counts = y_counts.add(x_counts, fill_value=0).astype(int)

        # The stored Y/X orientation is an implementation detail. Build the
        # Level 1 directory from both legs so every ticker is searchable.
        y_best = filtered.drop_duplicates(subset=["y"], keep="first").copy()
        y_best["group_ticker"] = y_best["y"].astype(str)
        x_best = filtered.drop_duplicates(subset=["x"], keep="first").copy()
        x_best["group_ticker"] = x_best["x"].astype(str)
        best_by_group = (
            pd.concat([y_best, x_best], ignore_index=True)
            .sort_values("rank", kind="stable")
            .drop_duplicates(subset=["group_ticker"], keep="first")
        )
        group_labels = {
            str(row.group_ticker): (
                f"{row.group_ticker} · best #{int(row.rank):,} {row.y}/{row.x} · "
                f"|ADF| {float(row.adf_abs):.4f} · "
                f"{int(group_counts.loc[row.group_ticker]):,} pairs"
            )
            for row in best_by_group.itertuples(index=False)
        }
        level_1_columns = st.columns([8, 1])
        level_1_search = level_1_columns[0].text_input(
            "Level 1 search",
            key="adf_level_1_search",
            placeholder="输入 ticker，例如 BHP",
        )
        level_1_columns[1].button(
            "清空",
            key="clear_adf_level_1_search",
            on_click=clear_text_input,
            args=("adf_level_1_search",),
            use_container_width=True,
        )
        group_options = best_by_group["group_ticker"].astype(str).tolist()
        normalized_level_1 = str(level_1_search or "").strip().upper()
        if normalized_level_1:
            group_options = sorted(
                [
                    ticker
                    for ticker in group_options
                    if normalized_level_1 in ticker.upper()
                ],
                key=lambda ticker: (
                    ticker.upper() != normalized_level_1,
                    not ticker.upper().startswith(normalized_level_1),
                    ticker.upper(),
                ),
            )
        if not group_options:
            st.warning(f"Level 1没有找到代码 `{normalized_level_1}`。")
            st.stop()
        selected_group = st.selectbox(
            "Level 1 · Ticker group (either leg)",
            group_options,
            format_func=lambda ticker: group_labels[str(ticker)],
        )
        candidates = filtered[
            (filtered["y"] == selected_group)
            | (filtered["x"] == selected_group)
        ].copy()

    level_2_columns = st.columns([8, 1])
    level_2_search = level_2_columns[0].text_input(
        "Level 2 search",
        key="adf_level_2_search",
        placeholder="输入另一条腿，例如 SBLK",
    )
    level_2_columns[1].button(
        "清空",
        key="clear_adf_level_2_search",
        on_click=clear_text_input,
        args=("adf_level_2_search",),
        use_container_width=True,
    )
    level_2_tokens = parse_ticker_tokens(level_2_search)
    if len(level_2_tokens) == 1:
        query = level_2_tokens[0]
        y_upper = candidates["y"].str.upper()
        x_upper = candidates["x"].str.upper()
        candidates = candidates[
            y_upper.str.contains(query, regex=False)
            | x_upper.str.contains(query, regex=False)
        ].copy()
    elif len(level_2_tokens) >= 2:
        first, second = level_2_tokens[0], level_2_tokens[1]
        y_upper = candidates["y"].str.upper()
        x_upper = candidates["x"].str.upper()
        candidates = candidates[
            ((y_upper == first) & (x_upper == second))
            | ((y_upper == second) & (x_upper == first))
        ].copy()
    if candidates.empty:
        st.warning(f"Level 2没有找到 `{str(level_2_search).strip()}`。")
        st.stop()

    pair_labels = {
        int(row.rank): (
            f"#{int(row.rank):,} {row.y}/{row.x} | "
            f"ADF {float(row.adf_stat):+.4f} | p {float(row.adf_pvalue):.3g}"
        )
        for row in candidates.itertuples(index=False)
    }
    selected_rank = st.selectbox(
        f"Level 2 · {selected_group} pairs",
        candidates["rank"].astype(int).tolist(),
        format_func=lambda rank: pair_labels[int(rank)],
    )
    selected = candidates[candidates["rank"] == selected_rank].iloc[0]

    overview_columns = [
        "rank",
        "pair_name",
        "adf_stat",
        "adf_abs",
        "adf_pvalue",
        "beta",
        "two_sigma_return_pct",
        "latest_z",
        "train_start",
        "train_end",
    ]
    with st.expander(f"{selected_group} group ranking", expanded=False):
        st.dataframe(
            candidates[overview_columns],
            width="stretch",
            hide_index=True,
        )

    y_symbol = str(selected["y"])
    x_symbol = str(selected["x"])
    train_start = pd.Timestamp(selected["train_start"]).date().isoformat()
    train_end = pd.Timestamp(selected["train_end"]).date().isoformat()
    raw_pair = load_daily_pair(y_symbol, x_symbol, train_start, train_end)
    try:
        analysis, recomputed = recompute_pair(raw_pair)
    except ValueError as exc:
        st.error(str(exc))
        st.stop()

    st.subheader(f"#{int(selected['rank']):,} · {y_symbol} / {x_symbol}")
    st.caption(
        f"Training window {train_start} → {train_end} · "
        f"{len(analysis)} aligned daily observations · Y={y_symbol}, X={x_symbol}"
    )
    metrics = st.columns(6)
    metrics[0].metric("Beta", metric_text(recomputed["beta"]))
    metrics[1].metric("ADF", metric_text(recomputed["adf_stat"]))
    metrics[2].metric("p-value", f"{recomputed['adf_pvalue']:.3g}")
    metrics[3].metric("2σ Return", f"{recomputed['two_sigma_return_pct']:.4f}%")
    metrics[4].metric("Latest Z", metric_text(recomputed["latest_z"]))
    metrics[5].metric("Observations", str(len(analysis)))

    comparison = pd.DataFrame(
        {
            "Metric": [
                "beta",
                "intercept",
                "ADF",
                "p-value",
                "spread sigma",
                "2σ return %",
                "latest z",
            ],
            "Stored CSV": [
                selected["beta"],
                selected["intercept"],
                selected["adf_stat"],
                selected["adf_pvalue"],
                selected["spread_sigma"],
                selected["two_sigma_return_pct"],
                selected["latest_z"],
            ],
            "Recomputed": [
                recomputed["beta"],
                recomputed["intercept"],
                recomputed["adf_stat"],
                recomputed["adf_pvalue"],
                recomputed["spread_sigma"],
                recomputed["two_sigma_return_pct"],
                recomputed["latest_z"],
            ],
        }
    )
    comparison["Difference"] = comparison["Recomputed"] - comparison["Stored CSV"]
    with st.expander("Stored vs recomputed", expanded=False):
        st.dataframe(comparison, width="stretch", hide_index=True)

    st.plotly_chart(
        build_pair_chart(analysis, y_symbol, x_symbol),
        width="stretch",
    )
    with st.expander("Daily observations", expanded=False):
        st.dataframe(
            analysis[
                [
                    "trade_date",
                    "y_price",
                    "x_price",
                    "spread",
                    "zscore",
                ]
            ],
            width="stretch",
            hide_index=True,
        )


if __name__ == "__main__":
    main()
