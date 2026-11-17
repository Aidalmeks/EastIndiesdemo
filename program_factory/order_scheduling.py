from __future__ import annotations

from typing import Any, Dict

import pandas as pd
import pymysql
from runtime_settings import database_config


MEMORY_DB_CONFIG = database_config("memory", cursorclass=pymysql.cursors.DictCursor)

PAIR_POSITION_TYPES = {"LONG_SPREAD", "SHORT_SPREAD", "AUTO_MODE"}
SINGLE_POSITION_TYPES = {"LONG_SPREAD", "SHORT_SPREAD"}


def to_mysql_datetime(value):
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).to_pydatetime().replace(tzinfo=None)


def normalize_pair_name(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_single_name(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_position_type(value: Any, allowed: set[str]) -> str:
    normalized = str(value or "").strip().upper()
    if normalized not in allowed:
        raise ValueError(f"Unsupported position_type: {value!r}")
    return normalized


def create_pair_scheduled_order(
    pair_name: str,
    train_start,
    split_time,
    two_sigma: float | None,
    position_type: str,
    beta: float,
    exposure_pct: float,
) -> Dict[str, object]:
    stored_pair_name = normalize_pair_name(pair_name)
    if not stored_pair_name:
        raise ValueError("pair_name is required")

    normalized_position_type = normalize_position_type(position_type, PAIR_POSITION_TYPES)
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO scheduled_orders
                    (
                        pair_name,
                        train_start,
                        split_time,
                        two_sigma,
                        position_type,
                        beta,
                        exposure_pct,
                        status
                    )
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, 'PENDING')
                """,
                (
                    stored_pair_name,
                    to_mysql_datetime(train_start),
                    to_mysql_datetime(split_time),
                    None if two_sigma is None else round(float(two_sigma), 4),
                    normalized_position_type,
                    round(float(beta), 4),
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
        "pair_name": stored_pair_name,
        "position_type": normalized_position_type,
    }


def create_single_scheduled_order(
    single_name: str,
    train_start,
    split_time,
    strategy: str,
    param: float,
    position_type: str,
    exposure_pct: float,
) -> Dict[str, object]:
    stored_single_name = normalize_single_name(single_name)
    if not stored_single_name:
        raise ValueError("single_name is required")

    normalized_position_type = normalize_position_type(position_type, SINGLE_POSITION_TYPES)
    normalized_strategy = str(strategy or "").strip().upper()
    if normalized_strategy not in {"LOG_RESIDUAL", "NEW_HIGH"}:
        raise ValueError(f"Unsupported single strategy: {strategy!r}")

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
                    (%s, %s, %s, %s, %s, %s, %s, 'PENDING')
                """,
                (
                    stored_single_name,
                    to_mysql_datetime(train_start),
                    to_mysql_datetime(split_time),
                    normalized_strategy,
                    round(float(param), 4),
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
        "strategy": normalized_strategy,
        "position_type": normalized_position_type,
    }
