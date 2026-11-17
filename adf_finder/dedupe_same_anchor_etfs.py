#!/usr/bin/env python3
"""Find economically redundant ETFs and optionally deactivate lower-ranked copies.

The default mode is read-only and writes an audit report. ``--apply`` changes
only ``quant_data.us_day_info.is_active`` for ETF members that:

* have enough aligned adjusted daily prices;
* have near-identical daily returns;
* have very small normalized-price divergence;
* have very small OLS two-sigma gross spread space; and
* are not referenced by an active metapair, reentry rule, position, or order.

Within each connected ETF family, the lowest ``base_rank`` is retained.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import pymysql


DB_CONFIG = {
    "host": "127.0.0.1",
    "user": "quant",
    "password": "1q2w3e",
    "database": "quant_data",
}
REPORT_DIR = Path(__file__).resolve().parents[1] / "logs" / "etf_dedupe"


@dataclass(frozen=True)
class Thresholds:
    min_observations: int = 100
    min_return_correlation: float = 0.999
    max_tracking_error_pct: float = 0.80
    max_normalized_gap_pct: float = 0.25
    max_two_sigma_pct: float = 0.25


class UnionFind:
    def __init__(self, values: Iterable[str]):
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def connect(database: str = "quant_data"):
    config = dict(DB_CONFIG)
    config["database"] = database
    return pymysql.connect(**config, cursorclass=pymysql.cursors.DictCursor)


def load_active_etfs_and_prices() -> tuple[pd.DataFrame, pd.DataFrame]:
    conn = connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT ticker, base_rank, name, security_type, is_active
                FROM us_day_info
                WHERE security_type = 'ETF'
                  AND is_active = 1
                ORDER BY base_rank
                """
            )
            info = pd.DataFrame(cursor.fetchall())
            cursor.execute(
                """
                SELECT d.trade_date, d.ticker, d.price
                FROM us_market_days d
                JOIN us_day_info i ON i.ticker = d.ticker
                WHERE i.security_type = 'ETF'
                  AND i.is_active = 1
                ORDER BY d.trade_date, d.ticker
                """
            )
            prices = pd.DataFrame(cursor.fetchall())
    finally:
        conn.close()

    if info.empty or prices.empty:
        raise RuntimeError("No active ETF daily data is available.")
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    prices["price"] = pd.to_numeric(prices["price"], errors="coerce")
    matrix = prices.pivot(index="trade_date", columns="ticker", values="price")
    return info, matrix.sort_index()


def pair_metrics(
    prices: pd.DataFrame,
    left: str,
    right: str,
) -> dict[str, float]:
    pair = prices[[left, right]].dropna()
    left_values = pair[left].to_numpy(dtype=float)
    right_values = pair[right].to_numpy(dtype=float)
    left_log = np.log(left_values)
    right_log = np.log(right_values)
    relative_log = left_log - right_log
    normalized_gap = relative_log - relative_log[0]
    relative_returns = np.diff(relative_log)
    left_returns = np.diff(left_log)
    right_returns = np.diff(right_log)

    beta, intercept = np.polyfit(right_values, left_values, 1)
    spread = left_values - (beta * right_values + intercept)
    notional = abs(float(left_values[-1] + beta * right_values[-1]))
    two_sigma_pct = (
        float(2.0 * np.std(spread, ddof=1) / notional * 100.0)
        if notional > 1e-12
        else math.inf
    )
    return {
        "observations": int(len(pair)),
        "return_correlation": float(np.corrcoef(left_returns, right_returns)[0, 1]),
        "tracking_error_pct": float(
            np.std(relative_returns, ddof=1) * math.sqrt(252.0) * 100.0
        ),
        "normalized_rmse_pct": float(
            np.sqrt(np.mean(np.square(normalized_gap))) * 100.0
        ),
        "normalized_max_gap_pct": float(
            np.max(np.abs(normalized_gap)) * 100.0
        ),
        "two_sigma_pct": two_sigma_pct,
    }


def find_redundant_edges(
    info: pd.DataFrame,
    prices: pd.DataFrame,
    thresholds: Thresholds,
) -> pd.DataFrame:
    eligible = [
        str(column)
        for column in prices.columns
        if int(prices[column].notna().sum()) >= thresholds.min_observations
    ]
    log_returns = np.log(prices[eligible]).diff()
    correlation = log_returns.corr(
        min_periods=max(2, thresholds.min_observations - 1)
    )
    ranks = info.set_index("ticker")["base_rank"].astype(int).to_dict()
    names = info.set_index("ticker")["name"].fillna("").astype(str).to_dict()

    edges: list[dict[str, object]] = []
    for left_index, left in enumerate(eligible):
        correlations = correlation.loc[left, eligible[left_index + 1 :]]
        candidates = correlations[
            correlations >= thresholds.min_return_correlation
        ]
        for right, _correlation in candidates.items():
            metrics = pair_metrics(prices, left, str(right))
            if metrics["observations"] < thresholds.min_observations:
                continue
            if metrics["return_correlation"] < thresholds.min_return_correlation:
                continue
            if metrics["tracking_error_pct"] > thresholds.max_tracking_error_pct:
                continue
            if (
                metrics["normalized_max_gap_pct"]
                > thresholds.max_normalized_gap_pct
            ):
                continue
            if metrics["two_sigma_pct"] > thresholds.max_two_sigma_pct:
                continue

            right = str(right)
            keep = left if ranks[left] < ranks[right] else right
            drop = right if keep == left else left
            edges.append(
                {
                    "left": left,
                    "right": right,
                    "left_rank": ranks[left],
                    "right_rank": ranks[right],
                    "left_name": names[left],
                    "right_name": names[right],
                    "edge_keep": keep,
                    "edge_drop": drop,
                    **metrics,
                }
            )

    columns = [
        "left",
        "right",
        "left_rank",
        "right_rank",
        "left_name",
        "right_name",
        "edge_keep",
        "edge_drop",
        "observations",
        "return_correlation",
        "tracking_error_pct",
        "normalized_rmse_pct",
        "normalized_max_gap_pct",
        "two_sigma_pct",
    ]
    return pd.DataFrame(edges, columns=columns).sort_values(
        ["tracking_error_pct", "normalized_max_gap_pct"],
        kind="stable",
    )


def parse_pair_symbols(pair_name: object) -> set[str]:
    parts = str(pair_name or "").strip().upper().replace("/", "_").split("_")
    return {part.strip() for part in parts if part.strip()}


def load_protected_symbols() -> set[str]:
    protected: set[str] = set()
    conn = connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT ticker_a, ticker_b
                FROM metapairs
                WHERE COALESCE(is_active, 1) = 1
                """
            )
            for row in cursor.fetchall():
                protected.update(
                    {
                        str(row.get("ticker_a") or "").strip().upper(),
                        str(row.get("ticker_b") or "").strip().upper(),
                    }
                )
    finally:
        conn.close()

    memory = connect("quant_memory")
    try:
        with memory.cursor() as cursor:
            cursor.execute("SELECT pair_name FROM pair_reentry")
            for row in cursor.fetchall():
                protected.update(parse_pair_symbols(row.get("pair_name")))

            cursor.execute("SELECT ticker_y, ticker_x FROM pair_positions")
            for row in cursor.fetchall():
                protected.update(
                    {
                        str(row.get("ticker_y") or "").strip().upper(),
                        str(row.get("ticker_x") or "").strip().upper(),
                    }
                )

            cursor.execute(
                """
                SELECT pair_name
                FROM scheduled_orders
                WHERE status IN ('PENDING', 'HOLDING')
                """
            )
            for row in cursor.fetchall():
                protected.update(parse_pair_symbols(row.get("pair_name")))
    finally:
        memory.close()

    protected.discard("")
    return protected


def build_plan(
    info: pd.DataFrame,
    edges: pd.DataFrame,
    protected: set[str],
) -> pd.DataFrame:
    if edges.empty:
        return pd.DataFrame(
            columns=[
                "family_id",
                "keep_ticker",
                "keep_rank",
                "ticker",
                "base_rank",
                "name",
                "action",
                "protected",
            ]
        )

    members = set(edges["left"]).union(edges["right"])
    union = UnionFind(members)
    for row in edges.itertuples(index=False):
        union.union(str(row.left), str(row.right))

    groups: dict[str, list[str]] = {}
    for ticker in members:
        groups.setdefault(union.find(ticker), []).append(ticker)

    ranks = info.set_index("ticker")["base_rank"].astype(int).to_dict()
    names = info.set_index("ticker")["name"].fillna("").astype(str).to_dict()
    ordered_groups = sorted(
        groups.values(),
        key=lambda group: min(ranks[ticker] for ticker in group),
    )

    rows = []
    for family_number, group in enumerate(ordered_groups, start=1):
        ordered = sorted(group, key=lambda ticker: ranks[ticker])
        keep = ordered[0]
        for ticker in ordered:
            is_protected = ticker in protected
            if ticker == keep:
                action = "KEEP_BEST_RANK"
            elif is_protected:
                action = "SKIP_PROTECTED"
            else:
                action = "DEACTIVATE"
            rows.append(
                {
                    "family_id": family_number,
                    "keep_ticker": keep,
                    "keep_rank": ranks[keep],
                    "ticker": ticker,
                    "base_rank": ranks[ticker],
                    "name": names[ticker],
                    "action": action,
                    "protected": is_protected,
                }
            )
    return pd.DataFrame(rows)


def write_reports(
    edges: pd.DataFrame,
    plan: pd.DataFrame,
    thresholds: Thresholds,
    apply_requested: bool,
) -> tuple[Path, Path, Path, dict[str, object]]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    edges_path = REPORT_DIR / f"etf_redundant_edges_{timestamp}.csv"
    plan_path = REPORT_DIR / f"etf_deactivation_plan_{timestamp}.csv"
    metadata_path = REPORT_DIR / f"etf_dedupe_run_{timestamp}.json"
    edges.to_csv(edges_path, index=False)
    plan.to_csv(plan_path, index=False)

    drops = plan.loc[plan["action"] == "DEACTIVATE", "ticker"].astype(str).tolist()
    metadata = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "apply_requested": bool(apply_requested),
        "thresholds": asdict(thresholds),
        "edge_count": len(edges),
        "family_count": int(plan["family_id"].nunique()) if not plan.empty else 0,
        "deactivation_count": len(drops),
        "deactivate_tickers": drops,
        "rollback_sql": (
            "UPDATE quant_data.us_day_info SET is_active=1 "
            f"WHERE ticker IN ({','.join(repr(ticker) for ticker in drops)});"
            if drops
            else ""
        ),
        "edges_csv": str(edges_path),
        "plan_csv": str(plan_path),
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return edges_path, plan_path, metadata_path, metadata


def apply_plan(plan: pd.DataFrame) -> int:
    drops = plan.loc[plan["action"] == "DEACTIVATE", "ticker"].astype(str).tolist()
    if not drops:
        return 0

    placeholders = ",".join(["%s"] * len(drops))
    conn = connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE us_day_info
                SET is_active = 0
                WHERE security_type = 'ETF'
                  AND is_active = 1
                  AND ticker IN ({placeholders})
                """,
                tuple(drops),
            )
            changed = int(cursor.rowcount)
        if changed != len(drops):
            conn.rollback()
            raise RuntimeError(
                f"Expected to deactivate {len(drops)} ETFs, database changed {changed}."
            )
        conn.commit()
        return changed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--min-observations", type=int, default=100)
    parser.add_argument("--min-return-correlation", type=float, default=0.999)
    parser.add_argument("--max-tracking-error-pct", type=float, default=0.80)
    parser.add_argument("--max-normalized-gap-pct", type=float, default=0.25)
    parser.add_argument("--max-two-sigma-pct", type=float, default=0.25)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    thresholds = Thresholds(
        min_observations=int(args.min_observations),
        min_return_correlation=float(args.min_return_correlation),
        max_tracking_error_pct=float(args.max_tracking_error_pct),
        max_normalized_gap_pct=float(args.max_normalized_gap_pct),
        max_two_sigma_pct=float(args.max_two_sigma_pct),
    )
    info, prices = load_active_etfs_and_prices()
    edges = find_redundant_edges(info, prices, thresholds)
    protected = load_protected_symbols()
    plan = build_plan(info, edges, protected)
    edges_path, plan_path, metadata_path, metadata = write_reports(
        edges, plan, thresholds, apply_requested=bool(args.apply)
    )

    changed = apply_plan(plan) if args.apply else 0
    print(plan.to_string(index=False))
    print()
    print(f"edges: {len(edges)} -> {edges_path}")
    print(f"families: {metadata['family_count']}")
    print(f"planned deactivations: {metadata['deactivation_count']} -> {plan_path}")
    print(f"metadata: {metadata_path}")
    print(f"database rows changed: {changed}")
    print("mode: APPLY" if args.apply else "mode: DRY RUN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
