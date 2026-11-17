import json
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List

import pymysql
from runtime_settings import database_config

try:
    import psutil
except Exception:
    psutil = None

from Action.Manual_Open import (
    RECENT_DAYS,
    delete_pending_order,
    load_orders_grouped,
    manual_place_all_pending,
)


IS_FROZEN = bool(getattr(sys, "frozen", False))


def detect_project_dir() -> str:
    if IS_FROZEN:
        exe_dir = os.path.abspath(os.path.dirname(sys.executable))
        candidates = [exe_dir, os.path.abspath(os.path.join(exe_dir, "..")), os.getcwd()]
    else:
        src_dir = os.path.abspath(os.path.dirname(__file__))
        candidates = [src_dir, os.getcwd()]

    seen = set()
    uniq = []
    for c in candidates:
        c = os.path.abspath(c)
        if c not in seen:
            uniq.append(c)
            seen.add(c)

    for c in uniq:
        if os.path.exists(os.path.join(c, "app.py")):
            return c
    return uniq[0]


PROJECT_DIR = detect_project_dir()
API_HOST = os.getenv("QUANT_API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("QUANT_API_PORT", "8000"))
API_BASE = f"http://{API_HOST}:{API_PORT}"
STREAMLIT_PORT = int(os.getenv("QUANT_DASHBOARD_PORT", "8501"))
STREAMLIT_URL = f"http://127.0.0.1:{STREAMLIT_PORT}"
PAIR_GAME_PORT = int(os.getenv("QUANT_PAIR_GAME_PORT", "8502"))
SINGLE_STOCK_GAME_PORT = int(os.getenv("QUANT_SINGLE_STOCK_GAME_PORT", "8503"))
MEMORY_DB_CONFIG = database_config("memory", cursorclass=pymysql.cursors.DictCursor)
DATA_DB_CONFIG = database_config("data", cursorclass=pymysql.cursors.DictCursor)
EXIT_RULES_CONFIG_PATH = os.path.join(PROJECT_DIR, "program_factory", "pair_exit_rules.json")
ORDERS_MANAGEMENT_STYLE_PATH = os.path.join(PROJECT_DIR, "orders_management.css")
SERVICE_LOG_DIR = os.path.join(PROJECT_DIR, "logs")
ADF_FINDER_LOG_DIR = os.path.join(SERVICE_LOG_DIR, "pair_adf_finder")
LOCAL_ZONEINFO_DIR = os.path.join(PROJECT_DIR, "zoneinfo")
SIGMA_RULE_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)?\*?sigma$", re.IGNORECASE)
Z_TARGET_RULE_PATTERN = re.compile(r"^z\(([^)]+)\)$", re.IGNORECASE)
TIME_TARGET_RULE_PATTERN = re.compile(r"^NY\(([^)]+)\)$", re.IGNORECASE)
EXIT_RULE_SECTION_ITEMS = [
    ("stop_loss", "Stop Loss"),
    ("take_profit", "Take Profit"),
]
STOP_LOSS_MODE_ITEMS = [
    ("fixed", "Fixed"),
    ("sigma", "Sigma"),
    ("trailing", "Trailing"),
    ("raw", "Raw"),
]
TAKE_PROFIT_MODE_ITEMS = [
    ("fixed", "Fixed"),
    ("sigma", "Sigma"),
    ("trailing", "Trailing"),
    ("ztarget", "Z Target"),
    ("time", "Time"),
    ("raw", "Raw"),
]

os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def normalize_pair_name(name: str) -> str:
    return str(name or "").strip().lower()


def parse_pair_input_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    if "/" in raw:
        parts = [part.strip() for part in raw.split("/") if part.strip()]
        if len(parts) >= 2:
            return normalize_pair_name(f"{parts[0]}_{parts[1]}")
    return normalize_pair_name(raw)


def format_pair_display(name: str) -> str:
    pair = normalize_pair_name(name)
    if not pair:
        return ""
    if "_" in pair:
        left, right = pair.split("_", 1)
        return f"{left.upper()} / {right.upper()}"
    return pair.upper()


def is_pair_key(name: str) -> bool:
    return "_" in normalize_pair_name(name)


def is_single_key(name: str) -> bool:
    value = str(name or "").strip()
    return bool(value) and "_" not in value


def parse_stop_loss_rule(text: str):
    value = str(text or "").strip()
    if not value:
        raise ValueError("Stop-loss rule cannot be empty.")

    try:
        number = float(value)
    except ValueError:
        return value

    return int(number) if number.is_integer() else number


def format_stop_loss_rule(rule) -> str:
    if isinstance(rule, float) and rule.is_integer():
        return str(int(rule))
    return str(rule)


def format_stop_loss_number(value_text: str) -> str:
    value = str(value_text or "").strip()
    if not value:
        raise ValueError("Stop-loss value cannot be empty.")

    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


def sigma_coeff_from_rule(rule_text: str) -> str:
    compact = str(rule_text or "").strip().lower().replace(" ", "")
    coeff = compact.replace("*sigma", "").replace("sigma", "")
    if coeff in ("", "+"):
        return "1"
    if coeff == "-":
        return "-1"
    return coeff


def normalize_sigma_rule_text(value_text: str) -> str:
    value = str(value_text or "").strip()
    if not value:
        raise ValueError("Sigma value cannot be empty.")

    compact = value.lower().replace(" ", "")
    if SIGMA_RULE_PATTERN.match(compact):
        return sigma_coeff_from_rule(value) + "*sigma"

    return f"{format_stop_loss_number(value)}*sigma"


def require_negative_trailing_stop_value(value_text: str):
    value = str(value_text or "").strip()
    if not value:
        raise ValueError("Trailing stop value cannot be empty.")

    compact = value.lower().replace(" ", "")
    if SIGMA_RULE_PATTERN.match(compact):
        coeff = float(sigma_coeff_from_rule(value))
    else:
        coeff = float(value)

    if coeff >= 0:
        raise ValueError("Stop-loss trailing value must be negative, for example -1 or -0.5*sigma.")


def validate_stop_loss_raw_rule(value_text: str):
    value = str(value_text or "").strip()
    compact = value.lower().replace(" ", "")
    if compact.startswith("trailing(") and compact.endswith(")"):
        inner = value[value.find("(") + 1 : -1].strip()
        require_negative_trailing_stop_value(inner)


def split_stop_loss_rule(rule) -> tuple[str, str]:
    if isinstance(rule, bool):
        return "fixed", format_stop_loss_rule(int(rule))

    if isinstance(rule, (int, float)):
        return "fixed", format_stop_loss_rule(rule)

    text = str(rule or "").strip()
    compact = text.lower().replace(" ", "")

    if compact.startswith("trailing(") and compact.endswith(")"):
        inner = text[text.find("(") + 1 : -1].strip()
        return "trailing", inner

    if SIGMA_RULE_PATTERN.match(compact):
        return "sigma", sigma_coeff_from_rule(text)

    return "raw", text


def build_stop_loss_rule(mode_key: str, value_text: str):
    value = str(value_text or "").strip()
    if not value:
        raise ValueError("Stop-loss value cannot be empty.")

    if mode_key == "fixed":
        return parse_stop_loss_rule(format_stop_loss_number(value))

    if mode_key == "sigma":
        return normalize_sigma_rule_text(value)

    if mode_key == "trailing":
        require_negative_trailing_stop_value(value)
        compact = value.lower().replace(" ", "")
        if SIGMA_RULE_PATTERN.match(compact):
            inner = sigma_coeff_from_rule(value) + "*sigma"
        else:
            inner = format_stop_loss_number(value)
        return f"trailing({inner})"

    if mode_key == "raw":
        validate_stop_loss_raw_rule(value)
        return value

    raise ValueError(f"Unsupported stop-loss mode: {mode_key}")


def stop_loss_mode_label(mode_key: str) -> str:
    for key, label in STOP_LOSS_MODE_ITEMS:
        if key == mode_key:
            return label
    return "Raw"


def split_take_profit_rule(rule) -> tuple[str, str]:
    if isinstance(rule, bool):
        return "fixed", format_stop_loss_rule(int(rule))

    if isinstance(rule, (int, float)):
        return "fixed", format_stop_loss_rule(rule)

    text = str(rule or "").strip()
    compact = text.lower().replace(" ", "")

    if compact.startswith("trailing(") and compact.endswith(")"):
        inner = text[text.find("(") + 1 : -1].strip()
        return "trailing", inner

    z_match = Z_TARGET_RULE_PATTERN.match(compact)
    if z_match:
        return "ztarget", z_match.group(1).strip()

    time_match = TIME_TARGET_RULE_PATTERN.match(compact)
    if time_match:
        return "time", time_match.group(1).strip()

    if SIGMA_RULE_PATTERN.match(compact):
        return "sigma", sigma_coeff_from_rule(text)

    return "raw", text


def build_take_profit_rule(mode_key: str, value_text: str):
    value = str(value_text or "").strip()
    if not value:
        raise ValueError("Take-profit value cannot be empty.")

    if mode_key == "fixed":
        return parse_stop_loss_rule(format_stop_loss_number(value))

    if mode_key == "sigma":
        return normalize_sigma_rule_text(value)

    if mode_key == "trailing":
        compact = value.lower().replace(" ", "")
        if SIGMA_RULE_PATTERN.match(compact):
            inner = sigma_coeff_from_rule(value) + "*sigma"
        else:
            inner = format_stop_loss_number(value)
        return f"trailing({inner})"

    if mode_key == "ztarget":
        return f"z({format_stop_loss_number(value)})"

    if mode_key == "time":
        if not re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", value):
            raise ValueError("NY time must be HH:MM, for example 15:55.")
        hour, minute = value.split(":", 1)
        return f"NY({int(hour):02d}:{int(minute):02d})"

    if mode_key == "raw":
        return value

    raise ValueError(f"Unsupported take-profit mode: {mode_key}")


def take_profit_mode_label(mode_key: str) -> str:
    for key, label in TAKE_PROFIT_MODE_ITEMS:
        if key == mode_key:
            return label
    return "Raw"


def _normalize_rule_config_map(payload) -> Dict[str, object]:
    normalized: Dict[str, object] = {}

    if not isinstance(payload, dict):
        return normalized

    for raw_pair, raw_rule in payload.items():
        pair = normalize_pair_name(raw_pair)
        if not pair or raw_rule is None:
            continue

        if isinstance(raw_rule, str):
            rule = raw_rule.strip()
            if not rule:
                continue
            normalized[pair] = rule
            continue

        if isinstance(raw_rule, bool):
            normalized[pair] = int(raw_rule)
            continue

        if isinstance(raw_rule, (int, float)):
            number = float(raw_rule)
            normalized[pair] = int(number) if number.is_integer() else number
            continue

        rule = str(raw_rule).strip()
        if rule:
            normalized[pair] = rule

    return dict(sorted(normalized.items()))


def _normalize_single_rule_config_map(payload) -> Dict[str, object]:
    normalized = _normalize_rule_config_map(payload)
    return {str(name or "").strip().upper(): rule for name, rule in normalized.items() if str(name or "").strip()}


def load_exit_rules_config() -> Dict[str, Dict[str, object]]:
    if os.path.exists(EXIT_RULES_CONFIG_PATH):
        with open(EXIT_RULES_CONFIG_PATH, "r", encoding="utf-8") as fh:
            payload = json.load(fh)

        if not isinstance(payload, dict):
            raise ValueError("pair_exit_rules.json must contain a JSON object.")

        return {
            "stop_loss": _normalize_rule_config_map(payload.get("stop_loss", {})),
            "take_profit": _normalize_rule_config_map(payload.get("take_profit", {})),
            "single_stop_loss": _normalize_single_rule_config_map(payload.get("single_stop_loss", {})),
            "single_take_profit": _normalize_single_rule_config_map(payload.get("single_take_profit", {})),
        }

    return {
        "stop_loss": {},
        "take_profit": {},
        "single_stop_loss": {},
        "single_take_profit": {},
    }


def save_exit_rules_config(config: Dict[str, Dict[str, object]]):
    payload = {
        "stop_loss": _normalize_rule_config_map(config.get("stop_loss", {})),
        "take_profit": _normalize_rule_config_map(config.get("take_profit", {})),
        "single_stop_loss": _normalize_single_rule_config_map(config.get("single_stop_loss", {})),
        "single_take_profit": _normalize_single_rule_config_map(config.get("single_take_profit", {})),
    }

    with open(EXIT_RULES_CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def load_stop_loss_config() -> Dict[str, object]:
    return load_exit_rules_config().get("stop_loss", {})


def save_stop_loss_config(config: Dict[str, object]):
    payload = load_exit_rules_config()
    payload["stop_loss"] = dict(config)
    save_exit_rules_config(payload)


def load_take_profit_config() -> Dict[str, object]:
    return load_exit_rules_config().get("take_profit", {})


def save_take_profit_config(config: Dict[str, object]):
    payload = load_exit_rules_config()
    payload["take_profit"] = dict(config)
    save_exit_rules_config(payload)


def fetch_available_pairs() -> List[str]:
    try:
        with urllib.request.urlopen(f"{API_BASE}/api/pairs", timeout=2) as resp:
            payload = json.load(resp)
    except Exception:
        return []

    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        pairs = [
            normalize_pair_name(item.get("name") or item.get("pair_name"))
            for item in payload.get("items", [])
            if isinstance(item, dict) and str(item.get("kind") or "pair").strip().lower() == "pair"
        ]
        return sorted({pair for pair in pairs if pair and is_pair_key(pair)})

    raw_pairs = payload.get("pairs", []) if isinstance(payload, dict) else []
    pairs = [normalize_pair_name(item) for item in raw_pairs]
    return sorted({pair for pair in pairs if pair and is_pair_key(pair)})


def fetch_available_pair_sources() -> Dict[str, str]:
    try:
        with urllib.request.urlopen(f"{API_BASE}/api/pairs", timeout=2) as resp:
            payload = json.load(resp)
    except Exception:
        return {}

    if not isinstance(payload, dict):
        return {}

    items = payload.get("items", [])
    if isinstance(items, list) and items:
        result: Dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "pair").strip().lower() != "pair":
                continue
            pair_name = normalize_pair_name(item.get("name") or item.get("pair_name"))
            source = str(item.get("source") or "").strip().upper()
            if pair_name and is_pair_key(pair_name) and source:
                result[pair_name] = source
        if result:
            return result

    raw_sources = payload.get("sources", {})
    if not isinstance(raw_sources, dict):
        return {}

    return {
        normalize_pair_name(pair_name): str(source or "").strip().upper()
        for pair_name, source in raw_sources.items()
        if normalize_pair_name(pair_name) and is_pair_key(pair_name) and str(source or "").strip()
    }


def normalize_single_source_label(source: str) -> str:
    parts = [
        part.strip().upper().replace("SINGLE_", "")
        for part in str(source or "").replace(",", "+").split("+")
        if part.strip()
    ]
    if "REENTRY" in parts:
        return "REENTRY"
    return "+".join(dict.fromkeys(parts))


def fetch_available_single_sources() -> Dict[str, str]:
    result: Dict[str, str] = {}
    try:
        with urllib.request.urlopen(f"{API_BASE}/api/pairs", timeout=2) as resp:
            payload = json.load(resp)
    except Exception:
        payload = {}

    if isinstance(payload, dict):
        for item in payload.get("items", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "").strip().lower() != "single":
                continue
            single_name = str(item.get("name") or "").strip().upper()
            source = normalize_single_source_label(item.get("source") or "")
            if single_name and is_single_key(single_name):
                result[single_name] = source

    try:
        conn = pymysql.connect(**MEMORY_DB_CONFIG)
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT pair_name
                    FROM pair_reentry
                    WHERE pair_name IS NOT NULL
                      AND pair_name <> ''
                      AND pair_name NOT LIKE '%\\_%' ESCAPE '\\'
                    """
                )
                for row in cursor.fetchall():
                    single_name = str(row.get("pair_name") or "").strip().upper()
                    if single_name and is_single_key(single_name):
                        result[single_name] = "REENTRY"
        finally:
            conn.close()
    except Exception:
        pass

    return {symbol: source for symbol, source in result.items() if source}


def fetch_pending_order_zscores(refresh: bool = False) -> Dict[str, dict]:
    url = f"{API_BASE}/api/orders/pending-zscores"
    if refresh:
        url += "?refresh=1"

    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            payload = json.load(resp)
    except Exception:
        return {}

    items = payload.get("items", []) if isinstance(payload, dict) else []
    result: Dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        pair_name = normalize_pair_name(item.get("pair_name"))
        if pair_name:
            result[pair_name] = item
    return result


def is_regular_us_market_open() -> bool:
    """Use IB's cached regular-session calendar to gate automatic GUI polling.

    The calendar is shared with app.py, includes holidays and early closes, and
    lets the PyQt timer stay entirely local outside market hours.
    """
    calendar_path = os.path.join(PROJECT_DIR, "logs", "ib_us_market_calendar.json")
    try:
        with open(calendar_path, "r", encoding="utf-8") as fh:
            sessions = json.load(fh).get("sessions", [])
    except (OSError, ValueError, AttributeError):
        return False

    now = datetime.now().astimezone()
    for session in sessions:
        try:
            opens_at = datetime.fromisoformat(session["open_bj"])
            closes_at = datetime.fromisoformat(session["close_bj"])
        except (KeyError, TypeError, ValueError):
            continue
        if opens_at <= now < closes_at:
            return True
    return False


def load_single_orders_grouped(days: int = RECENT_DAYS) -> Dict[str, List[dict]]:
    grouped: Dict[str, List[dict]] = {
        "PENDING": [],
        "HOLDING": [],
        "CLOSED": [],
    }
    base_select = """
        SELECT
            id,
            single_name,
            train_start,
            split_time,
            strategy,
            param,
            position_type,
            exposure_pct,
            status,
            created_at,
            result,
            max_return,
            close_time
        FROM single_scheduled_orders
    """
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            for status_name in ("PENDING", "HOLDING"):
                cursor.execute(
                    base_select + """
                    WHERE status = %s
                    ORDER BY created_at DESC, id DESC
                    """,
                    (status_name,),
                )
                grouped[status_name] = cursor.fetchall()

            cursor.execute(
                base_select + """
                WHERE status = 'CLOSED'
                  AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                ORDER BY created_at DESC, id DESC
                """,
                (int(days),),
            )
            grouped["CLOSED"] = cursor.fetchall()
    finally:
        conn.close()

    return grouped


def delete_single_pending_order(order_id: int) -> Dict[str, object]:
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, single_name
                FROM single_scheduled_orders
                WHERE id = %s AND status = 'PENDING'
                """,
                (int(order_id),),
            )
            row = cursor.fetchone()
            if not row:
                return {
                    "success": False,
                    "message": f"Pending single order id={order_id} was not found.",
                }

            cursor.execute(
                """
                DELETE FROM single_scheduled_orders
                WHERE id = %s AND status = 'PENDING'
                """,
                (int(order_id),),
            )
            deleted = cursor.rowcount > 0
        conn.commit()
    finally:
        conn.close()

    return {
        "success": deleted,
        "single_name": row.get("single_name") if row else "",
        "message": "" if deleted else f"Pending single order id={order_id} could not be deleted.",
    }


def load_pair_reentry_rows() -> List[dict]:
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    pair_name,
                    COALESCE(reentry_lock, 0) AS reentry_lock,
                    COALESCE(auto_schedule_enabled, 0) AS auto_schedule_enabled,
                    exposure_pct,
                    order_position_type
                FROM pair_reentry
                WHERE pair_name IS NOT NULL AND pair_name <> ''
                ORDER BY pair_name ASC
                """
            )
            rows = cursor.fetchall()
    finally:
        conn.close()

    result: List[dict] = []
    for row in rows:
        pair_name = normalize_pair_name(row.get("pair_name"))
        if not pair_name:
            continue
        result.append(
            {
                "pair_name": pair_name,
                "reentry_lock": int(row.get("reentry_lock") or 0),
                "auto_schedule_enabled": int(row.get("auto_schedule_enabled") or 0),
                "exposure_pct": row.get("exposure_pct"),
                "order_position_type": str(row.get("order_position_type") or "").strip().upper(),
            }
        )
    return result


def update_pair_reentry_lock(pair_name: str, lock_value: int) -> bool:
    normalized_pair = normalize_pair_name(pair_name)
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pair_reentry
                SET reentry_lock = %s
                WHERE pair_name = %s
                """,
                (int(lock_value), normalized_pair),
            )
            updated = cursor.rowcount > 0
        conn.commit()
    finally:
        conn.close()

    return updated


def delete_pair_reentry_row(pair_name: str) -> bool:
    normalized_pair = normalize_pair_name(pair_name)
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM pair_reentry
                WHERE pair_name = %s
                """,
                (normalized_pair,),
            )
            deleted = cursor.rowcount > 0
        conn.commit()
    finally:
        conn.close()

    return deleted


def load_subscription_stocks() -> List[dict]:
    conn = pymysql.connect(**DATA_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    ui.ticker,
                    ui.sector,
                    gs.emoji AS sector_emoji,
                    asset_class,
                    instrument_type,
                    currency,
                    exchange,
                    COALESCE(ui.is_active, 0) AS is_active
                FROM us_info ui
                LEFT JOIN gics_sectors gs
                  ON gs.sector = ui.sector
                ORDER BY ui.ticker ASC
                """
            )
            return cursor.fetchall()
    finally:
        conn.close()


def load_subscription_metapairs() -> List[dict]:
    conn = pymysql.connect(**DATA_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    mp.pair_name,
                    mp.ticker_a,
                    mp.ticker_b,
                    mp.pair_type,
                    mp.sector_a,
                    gsa.emoji AS sector_a_emoji,
                    mp.sector_b,
                    gsb.emoji AS sector_b_emoji,
                    COALESCE(mp.is_active, 0) AS is_active
                FROM metapairs mp
                LEFT JOIN gics_sectors gsa
                  ON gsa.sector = mp.sector_a
                LEFT JOIN gics_sectors gsb
                  ON gsb.sector = mp.sector_b
                ORDER BY mp.pair_name ASC
                """
            )
            return cursor.fetchall()
    finally:
        conn.close()


def update_subscription_stock_active(ticker: str, is_active: bool) -> bool:
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        return False

    conn = pymysql.connect(**DATA_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE us_info
                SET is_active = %s
                WHERE ticker = %s
                """,
                (1 if is_active else 0, symbol),
            )
            updated = cursor.rowcount > 0
        conn.commit()
    finally:
        conn.close()

    return updated


def update_subscription_metapair_active(pair_name: str, is_active: bool) -> bool:
    pair_key = normalize_pair_name(pair_name)
    if not pair_key:
        return False

    conn = pymysql.connect(**DATA_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE metapairs
                SET is_active = %s
                WHERE pair_name = %s
                """,
                (1 if is_active else 0, pair_key),
            )
            updated = cursor.rowcount > 0
        conn.commit()
    finally:
        conn.close()

    return updated


def load_subscription_options() -> Dict[str, List[str]]:
    conn = pymysql.connect(**DATA_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            option_queries = {
                "sectors": "SELECT sector AS value FROM gics_sectors ORDER BY sector ASC",
                "asset_classes": "SELECT asset_class AS value FROM asset_classes ORDER BY asset_class ASC",
                "instrument_types": "SELECT instrument_type AS value FROM instrument_types ORDER BY instrument_type ASC",
                "exchanges": "SELECT exchange AS value FROM us_exchanges ORDER BY exchange ASC",
                "tickers": "SELECT ticker AS value FROM us_info ORDER BY ticker ASC",
                "currencies": "SELECT DISTINCT currency AS value FROM us_info WHERE currency IS NOT NULL AND currency <> '' ORDER BY currency ASC",
                "pair_types": "SELECT DISTINCT pair_type AS value FROM metapairs WHERE pair_type IS NOT NULL AND pair_type <> '' ORDER BY pair_type ASC",
            }
            result: Dict[str, List[str]] = {}
            for key, sql in option_queries.items():
                cursor.execute(sql)
                result[key] = [
                    str(row.get("value") or "").strip()
                    for row in cursor.fetchall()
                    if str(row.get("value") or "").strip()
                ]
            return result
    finally:
        conn.close()


def load_subscription_stock_lookup() -> Dict[str, dict]:
    rows = load_subscription_stocks()
    return {
        str(row.get("ticker") or "").strip().upper(): row
        for row in rows
        if str(row.get("ticker") or "").strip()
    }


def insert_subscription_stock(
    ticker: str,
    sector: str,
    asset_class: str,
    instrument_type: str,
    currency: str,
    exchange: str,
    is_active: bool,
) -> None:
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        raise ValueError("Ticker is required.")

    conn = pymysql.connect(**DATA_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO us_info
                    (ticker, sector, asset_class, instrument_type, currency, exchange, is_active)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    symbol,
                    str(sector or "").strip() or None,
                    str(asset_class or "").strip() or None,
                    str(instrument_type or "").strip() or None,
                    str(currency or "").strip().upper() or "USD",
                    str(exchange or "").strip().upper() or None,
                    1 if is_active else 0,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def delete_subscription_stock(ticker: str) -> bool:
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        return False

    conn = pymysql.connect(**DATA_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM us_info WHERE ticker = %s", (symbol,))
            deleted = cursor.rowcount > 0
        conn.commit()
    finally:
        conn.close()

    return deleted


def insert_subscription_metapair(
    pair_name: str,
    ticker_a: str,
    ticker_b: str,
    pair_type: str,
    is_active: bool,
) -> str:
    left = str(ticker_a or "").strip().upper()
    right = str(ticker_b or "").strip().upper()
    if not left or not right:
        raise ValueError("Ticker A and Ticker B are required.")
    if left == right:
        raise ValueError("Ticker A and Ticker B must be different.")

    pair_key = normalize_pair_name(pair_name) or normalize_pair_name(f"{left}_{right}")
    stock_lookup = load_subscription_stock_lookup()
    if left not in stock_lookup:
        raise ValueError(f"Ticker A is not registered: {left}")
    if right not in stock_lookup:
        raise ValueError(f"Ticker B is not registered: {right}")

    sector_a = stock_lookup[left].get("sector")
    sector_b = stock_lookup[right].get("sector")

    conn = pymysql.connect(**DATA_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO metapairs
                    (pair_name, ticker_a, ticker_b, pair_type, sector_a, sector_b, is_active)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    pair_key,
                    left,
                    right,
                    str(pair_type or "").strip().lower() or None,
                    sector_a,
                    sector_b,
                    1 if is_active else 0,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return pair_key


def delete_subscription_metapair(pair_name: str) -> bool:
    pair_key = normalize_pair_name(pair_name)
    if not pair_key:
        return False

    conn = pymysql.connect(**DATA_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM metapairs WHERE pair_name = %s", (pair_key,))
            deleted = cursor.rowcount > 0
        conn.commit()
    finally:
        conn.close()

    return deleted


def load_optional_stylesheet(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


@dataclass
class ServiceSpec:
    key: str
    title: str
    tokens: List[str]
    mode: str  # "background" | "powershell"
    args: List[str] = field(default_factory=list)
    confirm_stop: bool = False
    env: Dict[str, str] = field(default_factory=dict)
    note: str = ""
    url: str = ""
    legacy_tokens: List[str] = field(default_factory=list)


SERVICE_ORDER = [
    "simple_dashboard",
    "quant_dashboard",
    "schedule",
    "livedata",
    "flagship",
    "live_repair",
    "waiting_entry",
    "fill_db",
    "pair_game",
    "single_stock_game",
]

SERVICES: Dict[str, ServiceSpec] = {
    "simple_dashboard": ServiceSpec(
        key="simple_dashboard",
        title="Simple Dashboard",
        tokens=["app.py"],
        mode="background",
        args=["app.py"],
        confirm_stop=True,
        env={
            "FLASK_DEBUG": "1",
            "QUANT_API_PORT": str(API_PORT),
            "QUANT_BIND_HOST": "0.0.0.0",
            "PYTHONUNBUFFERED": "1",
        },
        note="默认项",
    ),
    "quant_dashboard": ServiceSpec(
        key="quant_dashboard",
        title="Quant Dashboard",
        tokens=["streamlit", "quant_dashboard_fixed_beta.py"],
        mode="background",
        args=[
            "-m",
            "streamlit",
            "run",
            "quant_dashboard_fixed_beta.py",
            "--server.headless",
            "true",
            "--server.port",
            str(STREAMLIT_PORT),
        ],
        confirm_stop=True,
        env={"BROWSER": "none", "STREAMLIT_SERVER_HEADLESS": "true"},
        note="按需启动",
        url=STREAMLIT_URL,
        # One release migration: an existing legacy process can otherwise
        # keep port 8501 bound after the GUI itself is restarted.
        legacy_tokens=["streamlit", "quant_dashboard.py"],
    ),
    "pair_game": ServiceSpec(
        key="pair_game",
        title="Pair Backtest Board",
        tokens=["streamlit", "pair_backtest_game.py"],
        mode="background",
        args=[
            "-m",
            "streamlit",
            "run",
            "pair_backtest_game.py",
            "--server.headless",
            "true",
            "--server.port",
            str(PAIR_GAME_PORT),
        ],
        confirm_stop=False,
        env={"BROWSER": "none", "STREAMLIT_SERVER_HEADLESS": "true"},
        note=f"http://127.0.0.1:{PAIR_GAME_PORT}",
        url=f"http://127.0.0.1:{PAIR_GAME_PORT}",
    ),
    "single_stock_game": ServiceSpec(
        key="single_stock_game",
        title="Single Stock Game",
        tokens=["streamlit", "single_price_dashboard.py"],
        mode="background",
        args=[
            "-m",
            "streamlit",
            "run",
            "single_price_dashboard.py",
            "--server.headless",
            "true",
            "--server.port",
            str(SINGLE_STOCK_GAME_PORT),
        ],
        confirm_stop=False,
        env={"BROWSER": "none", "STREAMLIT_SERVER_HEADLESS": "true"},
        note=f"http://127.0.0.1:{SINGLE_STOCK_GAME_PORT}",
        url=f"http://127.0.0.1:{SINGLE_STOCK_GAME_PORT}",
    ),
    "schedule": ServiceSpec(
        key="schedule",
        title="Scheduler",
        tokens=["scheduler.pyw"],
        mode="background",
        args=["scheduler.pyw"],
        confirm_stop=True,
        note="默认项",
    ),
    "livedata": ServiceSpec(
        key="livedata",
        title="Live Data",
        tokens=["livedata.py"],
        mode="powershell",
        args=["livedata.py"],
        note="独立 PowerShell",
    ),
    "flagship": ServiceSpec(
        key="flagship",
        title="Flagship Board",
        tokens=["flagship_board.py"],
        mode="powershell",
        args=["flagship_board.py"],
        note="独立 PowerShell",
    ),
    "waiting_entry": ServiceSpec(
        key="waiting_entry",
        title="Waiting Entry",
        tokens=["waiting_entry.py"],
        mode="powershell",
        args=["waiting_entry.py"],
        note="独立 PowerShell",
    ),
    "live_repair": ServiceSpec(
        key="live_repair",
        title="Live Repair",
        tokens=["live_repair.py"],
        mode="powershell",
        args=["live_repair.py"],
        note="独立 PowerShell",
    ),
    "fill_db": ServiceSpec(
        key="fill_db",
        title="Fill DB",
        tokens=["fill_db.py"],
        mode="powershell",
        args=["fill_db.py"],
        note="独立 PowerShell",
    ),
}


def backend_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{API_BASE}/api/pairs", timeout=1.5) as resp:
            return resp.status < 500
    except Exception:
        return False


def resolve_python() -> str:
    if IS_FROZEN:
        return os.getenv("QUANT_PYTHON", "python")
    current = os.path.abspath(sys.executable)
    base = os.path.basename(current).lower()
    if base == "pythonw.exe":
        candidate = os.path.join(os.path.dirname(current), "python.exe")
        if os.path.exists(candidate):
            return candidate
    if os.path.exists(current):
        return current
    return "python"


def resolve_pythonw() -> str:
    if IS_FROZEN:
        return os.getenv("QUANT_PYTHONW", "pythonw")
    current = os.path.abspath(sys.executable)
    base = os.path.basename(current).lower()
    if base == "pythonw.exe":
        return current
    if base == "python.exe":
        candidate = os.path.join(os.path.dirname(current), "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    return "pythonw"


def query_processes() -> List[dict]:
    if psutil is not None:
        out = []
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                name = str(proc.info.get("name") or "")
                name_lower = name.lower()
                if os.name == "nt" and name_lower not in {"python.exe", "pythonw.exe", "powershell.exe"}:
                    continue
                if os.name != "nt" and not (
                    "python" in name_lower
                    or "streamlit" in name_lower
                    or name_lower in {"sh", "bash"}
                ):
                    continue
                cmdline = proc.info.get("cmdline") or []
                cmd = " ".join(str(part) for part in cmdline)
                out.append(
                    {
                        "pid": int(proc.pid),
                        "name": name,
                        "cmd": cmd,
                        "cmd_lower": cmd.lower(),
                    }
                )
            except (psutil.Error, OSError, ValueError):
                continue
        return out

    if os.name != "nt":
        try:
            proc = subprocess.run(
                ["ps", "-eo", "pid=,comm=,args="],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                check=False,
            )
        except Exception:
            return []
        if proc.returncode != 0:
            return []

        out = []
        for line in proc.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 2:
                continue
            pid_text, name = parts[0], parts[1]
            cmd = parts[2] if len(parts) > 2 else name
            try:
                pid = int(pid_text)
            except ValueError:
                continue
            out.append(
                {
                    "pid": pid,
                    "name": name,
                    "cmd": cmd,
                    "cmd_lower": cmd.lower(),
                }
            )
        return out

    ps_script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "$p = Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match '^(python|pythonw|powershell)\\.exe$' } | "
        "Select-Object ProcessId, Name, CommandLine; "
        "$p | ConvertTo-Json -Compress"
    )
    try:
        hidden_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0

        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
            creationflags=hidden_flags,
            startupinfo=startupinfo,
        )
    except Exception:
        return []

    if proc.returncode != 0 or not proc.stdout.strip():
        return []

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []

    rows = payload if isinstance(payload, list) else [payload]
    out = []
    for row in rows:
        cmd = str(row.get("CommandLine") or "")
        out.append(
            {
                "pid": int(row.get("ProcessId") or 0),
                "name": str(row.get("Name") or ""),
                "cmd": cmd,
                "cmd_lower": cmd.lower(),
            }
        )
    return out


def match_service_processes(spec: ServiceSpec, processes: List[dict]) -> List[dict]:
    tokens = [t.lower() for t in spec.tokens]
    matched = []
    for p in processes:
        cmd = p.get("cmd_lower", "")
        if cmd and all(t in cmd for t in tokens):
            matched.append(p)
    return matched


def match_legacy_service_processes(spec: ServiceSpec, processes: List[dict]) -> List[dict]:
    """Find an explicitly declared predecessor service during a cutover."""
    tokens = [token.lower() for token in spec.legacy_tokens]
    if not tokens:
        return []
    return [process for process in processes if process.get("cmd_lower", "") and all(token in process["cmd_lower"] for token in tokens)]


def is_service_running(spec: ServiceSpec, processes: List[dict]) -> bool:
    return len(match_service_processes(spec, processes)) > 0


def build_service_env(extra: Dict[str, str] | None = None) -> Dict[str, str]:
    env = os.environ.copy()
    env.update(extra or {})
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = env.get("PYTHONUNBUFFERED", "1")
    env["QUANT_API_PORT"] = str(API_PORT)
    if os.path.isdir(LOCAL_ZONEINFO_DIR):
        existing_tzpath = env.get("PYTHONTZPATH")
        env["PYTHONTZPATH"] = (
            f"{LOCAL_ZONEINFO_DIR}{os.pathsep}{existing_tzpath}"
            if existing_tzpath
            else LOCAL_ZONEINFO_DIR
        )
    return env


def open_service_log(script_name: str):
    os.makedirs(SERVICE_LOG_DIR, exist_ok=True)
    base = os.path.basename(script_name).replace(".pyw", "").replace(".py", "")
    safe_base = re.sub(r"[^A-Za-z0-9_.-]+", "_", base) or "service"
    path = os.path.join(SERVICE_LOG_DIR, f"{safe_base}.log")
    fh = open(path, "a", encoding="utf-8")
    fh.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] launch {script_name}\n")
    fh.flush()
    return fh


def build_terminal_shell_command(script_name: str, args: List[str] | None = None) -> str:
    command_parts = [
        shlex.quote(resolve_python()),
        "-X",
        "utf8",
        "-u",
        shlex.quote(script_name),
        *(shlex.quote(str(arg)) for arg in (args or [])),
    ]
    command = " ".join(command_parts)
    project_dir = shlex.quote(PROJECT_DIR)
    tz_export = ""
    if os.path.isdir(LOCAL_ZONEINFO_DIR):
        tz_export = f"export PYTHONTZPATH={shlex.quote(LOCAL_ZONEINFO_DIR)}; "
    return (
        f"cd {project_dir}; "
        f"{tz_export}"
        f"echo '[quant] cwd='$(pwd); "
        "echo '[quant] PYTHONTZPATH='$PYTHONTZPATH; "
        f"echo '[quant] command: {command}'; "
        f"{command}; "
        "status=$?; "
        "echo; "
        "echo '[quant] process exited with status' $status; "
        "echo '[quant] close this terminal when finished reviewing output.'; "
        "exec bash"
    )


def terminal_command_candidates(title: str, shell_command: str) -> List[List[str]]:
    candidates: List[List[str]] = []

    ptyxis = shutil.which("ptyxis")
    if ptyxis:
        candidates.append(
            [
                ptyxis,
                "--new-window",
                "--title",
                title,
                "--working-directory",
                PROJECT_DIR,
                "--",
                "bash",
                "-lc",
                shell_command,
            ]
        )

    x_terminal = shutil.which("x-terminal-emulator")
    if x_terminal and os.path.realpath(x_terminal) != os.path.realpath(ptyxis or ""):
        candidates.append([x_terminal, "-T", title, "-e", "bash", "-lc", shell_command])

    gnome_terminal = shutil.which("gnome-terminal")
    if gnome_terminal:
        candidates.append(
            [
                gnome_terminal,
                "--title",
                title,
                "--working-directory",
                PROJECT_DIR,
                "--",
                "bash",
                "-lc",
                shell_command,
            ]
        )

    for program in (
        "konsole",
        "xfce4-terminal",
        "mate-terminal",
        "lxterminal",
        "tilix",
        "terminator",
        "xterm",
    ):
        path = shutil.which(program)
        if not path:
            continue
        if program == "konsole":
            candidates.append(
                [
                    path,
                    "--new-tab",
                    "--workdir",
                    PROJECT_DIR,
                    "-p",
                    f"tabtitle={title}",
                    "-e",
                    "bash",
                    "-lc",
                    shell_command,
                ]
            )
        elif program == "xfce4-terminal":
            candidates.append(
                [
                    path,
                    "--title",
                    title,
                    "--working-directory",
                    PROJECT_DIR,
                    "--command",
                    f"bash -lc {shlex.quote(shell_command)}",
                ]
            )
        elif program == "xterm":
            candidates.append([path, "-T", title, "-e", "bash", "-lc", shell_command])
        else:
            candidates.append([path, "-t", title, "-e", "bash", "-lc", shell_command])

    return candidates


def start_python_script_terminal(
    script_name: str,
    args: List[str] | None = None,
    env_extra: Dict[str, str] | None = None,
    title: str = "",
) -> bool:
    env = build_service_env(env_extra)
    shell_command = build_terminal_shell_command(script_name, args)
    terminal_title = title or os.path.basename(script_name)

    for command in terminal_command_candidates(terminal_title, shell_command):
        try:
            subprocess.Popen(
                command,
                cwd=PROJECT_DIR,
                env=env,
                close_fds=True,
                start_new_session=True,
            )
            return True
        except (FileNotFoundError, OSError):
            continue

    return False


def start_python_script_service(
    script_name: str,
    args: List[str] | None = None,
    env_extra: Dict[str, str] | None = None,
    title: str = "",
):
    if os.name != "nt" and start_python_script_terminal(script_name, args, env_extra, title):
        return

    env = build_service_env(env_extra)
    log_fh = open_service_log(script_name)
    popen_kwargs = dict(
        cwd=PROJECT_DIR,
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    try:
        subprocess.Popen(
            [resolve_python(), "-X", "utf8", "-u", script_name, *(args or [])],
            **popen_kwargs,
        )
    finally:
        log_fh.close()


def start_background_service(spec: ServiceSpec):
    env = build_service_env(spec.env or {})

    flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )

    try:
        subprocess.Popen(
            [resolve_pythonw(), *spec.args],
            cwd=PROJECT_DIR,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    except FileNotFoundError:
        subprocess.Popen(
            [resolve_python(), *spec.args],
            cwd=PROJECT_DIR,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )


def start_powershell_service(spec: ServiceSpec):
    if os.name != "nt":
        start_python_script_service(spec.args[0], env_extra=spec.env or {}, title=spec.title)
        return

    python_exe = resolve_python()
    script = spec.args[0]
    env_setup = (
        "$env:PYTHONUNBUFFERED='1';"
        "$env:PYTHONUTF8='1';"
        "$env:PYTHONIOENCODING='utf-8';"
        f"$env:QUANT_API_PORT='{API_PORT}';"
    )
    run_cmd = f"& '{python_exe}' -X utf8 -u '{script}'"
    full_cmd = env_setup + run_cmd

    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )

    subprocess.Popen(
        ["powershell", "-NoExit", "-Command", full_cmd],
        cwd=PROJECT_DIR,
        creationflags=flags,
        close_fds=True,
    )


def launch_task_console(script_name: str, extra_args: List[str], title: str = ""):
    if os.name != "nt":
        start_python_script_service(script_name, extra_args, title=title)
        return

    python_exe = resolve_python()
    script_path = os.path.join(PROJECT_DIR, script_name)
    env_setup = (
        "$env:PYTHONUNBUFFERED='1';"
        "$env:PYTHONUTF8='1';"
        "$env:PYTHONIOENCODING='utf-8';"
        f"$env:QUANT_API_PORT='{API_PORT}';"
    )

    arg_parts = []
    for arg in extra_args:
        arg_text = str(arg).replace("'", "''")
        if any(ch.isspace() for ch in arg_text):
            arg_parts.append(f"'{arg_text}'")
        else:
            arg_parts.append(arg_text)

    title_cmd = ""
    if title:
        safe_title = title.replace("'", "''")
        title_cmd = f"$Host.UI.RawUI.WindowTitle='{safe_title}';"

    run_cmd = f"& '{python_exe}' -X utf8 -u '{script_path}'"
    if arg_parts:
        run_cmd += " " + " ".join(arg_parts)

    full_cmd = title_cmd + env_setup + run_cmd

    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )

    subprocess.Popen(
        ["powershell", "-NoExit", "-Command", full_cmd],
        cwd=PROJECT_DIR,
        creationflags=flags,
        close_fds=True,
    )


def start_service_if_needed(spec: ServiceSpec, processes: List[dict] | None = None):
    processes = query_processes() if processes is None else processes
    if is_service_running(spec, processes):
        return
    legacy_targets = match_legacy_service_processes(spec, processes)
    if legacy_targets:
        stop_service_processes(legacy_targets)
        time.sleep(1.0)
    if spec.mode == "background":
        start_background_service(spec)
    else:
        start_powershell_service(spec)


def open_service_url(spec: ServiceSpec):
    if not spec.url:
        return
    try:
        webbrowser.open(spec.url, new=2)
    except Exception:
        pass


def stop_service(spec: ServiceSpec):
    processes = query_processes()
    targets = match_service_processes(spec, processes)
    stop_service_processes(targets)


def stop_service_processes(targets: List[dict]):
    for proc in targets:
        pid = int(proc.get("pid", 0))
        if pid <= 0:
            continue
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                print(f"Permission denied stopping pid={pid}")


def ensure_single_service(spec: ServiceSpec):
    processes = query_processes()
    legacy_targets = match_legacy_service_processes(spec, processes)
    if legacy_targets:
        stop_service_processes(legacy_targets)
        time.sleep(1.0)
        processes = query_processes()
    targets = match_service_processes(spec, processes)
    if len(targets) == 1:
        return

    if len(targets) > 1:
        stop_service_processes(targets)
        time.sleep(1.0)

    if spec.mode == "background":
        start_background_service(spec)
    else:
        start_powershell_service(spec)


def ensure_default_services():
    processes = query_processes()
    # The fixed-beta Streamlit dashboard is an interactive research tool and
    # has a substantial idle memory footprint.  Keep it available from the
    # service card, but do not start it with the execution services.
    for key in ["simple_dashboard", "schedule"]:
        spec = SERVICES[key]
        legacy_targets = match_legacy_service_processes(spec, processes)
        if legacy_targets:
            stop_service_processes(legacy_targets)
            time.sleep(1.0)
            processes = query_processes()
        targets = match_service_processes(spec, processes)
        if len(targets) == 1:
            continue
        if len(targets) > 1:
            stop_service_processes(targets)
            time.sleep(1.0)
        if spec.mode == "background":
            start_background_service(spec)
        else:
            start_powershell_service(spec)


def compute_zoom(geo_height: int) -> float:
    if geo_height <= 800:
        return 0.82
    if geo_height <= 900:
        return 0.88
    if geo_height <= 1080:
        return 0.92
    return 0.98


class ServiceCard:
    def __init__(self, spec: ServiceSpec, parent, toggle_cb):
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QCursor
        from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

        self.spec = spec
        self.root = QFrame(parent)
        self.root.setFrameShape(QFrame.Shape.StyledPanel)
        self.root.setStyleSheet(
            "QFrame { border: 1px solid #d0d0d0; border-radius: 10px; background: #f7f7f7; }"
        )

        box = QHBoxLayout(self.root)
        box.setContentsMargins(12, 10, 12, 10)
        box.setSpacing(12)

        self.lamp = QPushButton("")
        self.lamp.setFixedSize(24, 24)
        self.lamp.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.lamp.clicked.connect(lambda: toggle_cb(spec.key))
        box.addWidget(self.lamp)

        text_col = QVBoxLayout()
        self.title = QLabel(spec.title)
        self.title.setStyleSheet("font-size: 14px; font-weight: 600;")
        self.meta = QLabel("")
        self.meta.setStyleSheet("font-size: 12px; color: #666;")
        text_col.addWidget(self.title)
        text_col.addWidget(self.meta)
        box.addLayout(text_col)
        box.addStretch(1)

    def set_running(self, running: bool, hint: str):
        color = "#2ecc71" if running else "#95a5a6"
        self.lamp.setStyleSheet(
            f"QPushButton {{ background: {color}; border: 1px solid #7f8c8d; border-radius: 12px; }}"
        )
        state = "RUNNING" if running else "STOPPED"
        suffix = f" | {self.spec.note}" if self.spec.note else ""
        self.meta.setText(f"{state}{suffix}{(' | ' + hint) if hint else ''}")


def main():
    try:
        from PyQt6.QtCore import QProcess, Qt, QTimer, QUrl
        from PyQt6.QtGui import QBrush, QColor, QIcon, QKeySequence, QPalette, QShortcut, QTextCursor
        from PyQt6.QtWebEngineCore import QWebEngineProfile
        from PyQt6.QtWebEngineWidgets import QWebEngineView
        from PyQt6.QtWidgets import (
            QAbstractItemView,
            QApplication,
            QCheckBox,
            QComboBox,
            QGridLayout,
            QHeaderView,
            QHBoxLayout,
            QLabel,
            QListView,
            QLineEdit,
            QMainWindow,
            QMessageBox,
            QPlainTextEdit,
            QPushButton,
            QGraphicsDropShadowEffect,
            QStyle,
            QStyledItemDelegate,
            QStyleOptionViewItem,
            QTabWidget,
            QTableWidget,
            QTableWidgetItem,
            QVBoxLayout,
            QWidget,
        )
    except Exception as exc:
        print("PyQt6 GUI dependencies missing.")
        print("Install with: pip install PyQt6 PyQt6-WebEngine")
        print(f"Detail: {exc}")
        return 1

    app = QApplication(sys.argv)
    app.setApplicationName("Aidalmeya Quant GUI")
    app.setDesktopFileName("aidalmeya-quant-gui")
    app_icon = QIcon(os.path.join(PROJECT_DIR, "bank.ico"))
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    webengine_root = os.path.join(PROJECT_DIR, ".qtwebengine")
    webengine_cache = os.path.join(webengine_root, "cache")
    webengine_storage = os.path.join(webengine_root, "storage")
    os.makedirs(webengine_cache, exist_ok=True)
    os.makedirs(webengine_storage, exist_ok=True)

    web_profile = QWebEngineProfile.defaultProfile()
    web_profile.setCachePath(webengine_cache)
    web_profile.setPersistentStoragePath(webengine_storage)
    web_profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
    web_profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies
    )

    window = QMainWindow()
    window.setWindowTitle("Aidalmeya Quant GUI")
    if not app_icon.isNull():
        window.setWindowIcon(app_icon)

    screen = app.primaryScreen()
    geo = screen.availableGeometry() if screen else None
    if geo:
        width = max(980, min(1220, int(geo.width() * 0.78)))
        height = max(620, min(760, int(geo.height() * 0.8)))
        window.resize(width, height)
        window.move(
            geo.x() + max(0, (geo.width() - width) // 2),
            geo.y() + max(0, (geo.height() - height) // 2),
        )
    else:
        window.resize(1120, 700)

    tabs = QTabWidget()
    window.setCentralWidget(tabs)
    statusbar = window.statusBar()

    stop_loss_source_role = int(Qt.ItemDataRole.UserRole) + 1

    class StopLossPairDelegate(QStyledItemDelegate):
        def paint(self, painter, option, index):
            source = str(index.data(stop_loss_source_role) or "").strip().upper()
            if not source:
                super().paint(painter, option, index)
                return

            pair_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
            suffix_text = f"  {source}"

            opt = QStyleOptionViewItem(option)
            self.initStyleOption(opt, index)
            opt.text = ""

            widget = opt.widget
            style = widget.style() if widget is not None else QApplication.style()
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

            text_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, widget)
            if not text_rect.isValid():
                text_rect = opt.rect.adjusted(8, 0, -8, 0)

            painter.save()
            normal_color = QColor("#0f172a")
            source_color = QColor("#64748b") if opt.state & QStyle.StateFlag.State_Selected else QColor("#94a3b8")

            metrics = painter.fontMetrics()
            suffix_width = metrics.horizontalAdvance(suffix_text)
            pair_width_limit = max(0, text_rect.width() - suffix_width - 6)
            elided_pair_text = metrics.elidedText(pair_text, Qt.TextElideMode.ElideRight, pair_width_limit)
            pair_width = metrics.horizontalAdvance(elided_pair_text)

            painter.setPen(normal_color)
            painter.drawText(
                text_rect,
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                elided_pair_text,
            )

            source_rect = text_rect.adjusted(pair_width + 6, 0, 0, 0)
            painter.setPen(source_color)
            painter.drawText(
                source_rect,
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                suffix_text,
            )
            painter.restore()

        def sizeHint(self, option, index):
            size = super().sizeHint(option, index)
            source = str(index.data(stop_loss_source_role) or "").strip().upper()
            if source:
                size.setWidth(size.width() + 64)
            return size

    class StopLossPairLineEdit(QLineEdit):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._source_label = QLabel(self)
            self._source_label.setStyleSheet("color: #94a3b8; background: transparent;")
            self._source_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self._source_label.hide()
            self._update_source_geometry()

        def setSourceText(self, source_text: str):
            text = str(source_text or "").strip().upper()
            if not text:
                self._source_label.clear()
                self._source_label.hide()
                self.setTextMargins(0, 0, 0, 0)
                return

            self._source_label.setText(text)
            self._source_label.adjustSize()
            self._source_label.show()
            self._update_source_geometry()

        def resizeEvent(self, event):
            super().resizeEvent(event)
            self._update_source_geometry()

        def _update_source_geometry(self):
            if not self._source_label.text():
                return

            self._source_label.adjustSize()
            label_width = self._source_label.sizeHint().width()
            label_height = self._source_label.sizeHint().height()
            right_padding = 10
            x = max(0, self.width() - label_width - right_padding)
            y = max(0, (self.height() - label_height) // 2)
            self._source_label.move(x, y)
            self.setTextMargins(0, 0, label_width + right_padding + 6, 0)

    dashboard = QWidget()
    dashboard_layout = QVBoxLayout(dashboard)
    dashboard_layout.setContentsMargins(12, 12, 12, 12)
    dashboard_layout.setSpacing(10)

    title = QLabel("Service Dashboard")
    title.setStyleSheet("font-size: 18px; font-weight: 700;")
    sub = QLabel("点击圆灯开关服务。绿色运行中，灰色已停止。")
    sub.setStyleSheet("font-size: 12px; color: #555;")
    dashboard_layout.addWidget(title)
    dashboard_layout.addWidget(sub)

    grid = QGridLayout()
    grid.setHorizontalSpacing(10)
    grid.setVerticalSpacing(10)
    dashboard_layout.addLayout(grid)
    dashboard_layout.addStretch(1)

    cards: Dict[str, ServiceCard] = {}

    def toggle_service(key: str):
        spec = SERVICES[key]
        processes = query_processes()
        running = is_service_running(spec, processes)

        if running:
            if spec.confirm_stop:
                ans = QMessageBox.question(
                    window,
                    "Confirm Stop",
                    f"确认要关闭 {spec.title} 吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if ans != QMessageBox.StandardButton.Yes:
                    return
            try:
                stop_service(spec)
            except Exception as exc:
                QMessageBox.critical(window, "Stop Failed", f"{spec.title} stop failed:\n\n{exc}")
                statusbar.showMessage(f"{spec.title} stop failed: {exc}", 8000)
                return
        else:
            try:
                start_service_if_needed(spec)
            except Exception as exc:
                QMessageBox.critical(window, "Start Failed", f"{spec.title} start failed:\n\n{exc}")
                statusbar.showMessage(f"{spec.title} start failed: {exc}", 8000)
                return
            if spec.url:
                QTimer.singleShot(1800, lambda spec=spec: open_service_url(spec))

        refresh_dashboard()

    for idx, key in enumerate(SERVICE_ORDER):
        card = ServiceCard(SERVICES[key], dashboard, toggle_service)
        row = idx // 2
        col = idx % 2
        grid.addWidget(card.root, row, col)
        cards[key] = card

    tabs.addTab(dashboard, "Dashboard")

    stop_loss_tab = QWidget()
    stop_loss_layout = QVBoxLayout(stop_loss_tab)
    stop_loss_layout.setContentsMargins(12, 12, 12, 12)
    stop_loss_layout.setSpacing(10)

    stop_loss_title = QLabel("Exit Rules")
    stop_loss_title.setStyleSheet("font-size: 18px; font-weight: 700;")
    stop_loss_desc = QLabel(
        "execution.py now reads pair_exit_rules.json with separate stop-loss and take-profit sections. "
        "The table shows both rules on the same row for each pair. Stop-loss defaults to trailing(-0.5*sigma). "
        "Take-profit supports return thresholds, trailing profit, z targets like z(0), and NY time exits like NY(15:55)."
    )
    stop_loss_desc.setWordWrap(True)
    stop_loss_desc.setStyleSheet("font-size: 12px; color: #555;")
    stop_loss_path = QLabel(EXIT_RULES_CONFIG_PATH)
    stop_loss_path.setStyleSheet("font-size: 12px; color: #666;")
    stop_loss_path.setTextInteractionFlags(stop_loss_path.textInteractionFlags())
    stop_loss_layout.addWidget(stop_loss_title)
    stop_loss_layout.addWidget(stop_loss_desc)
    stop_loss_layout.addWidget(stop_loss_path)

    editor_row = QHBoxLayout()
    editor_row.setSpacing(8)

    target_type_input = QComboBox()
    target_type_input.addItem("Pair", "pair")
    target_type_input.addItem("Single", "single")

    pair_input = QComboBox()
    pair_input.setEditable(True)
    pair_input.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    pair_input.setMinimumWidth(220)
    pair_input.setToolTip("Pair name. You can select a detected pair or type any pair manually, for example: sblk_bhp or SBLK / BHP")
    pair_input.setLineEdit(StopLossPairLineEdit(pair_input))
    pair_input.setView(QListView())
    pair_input.view().setItemDelegate(StopLossPairDelegate(pair_input.view()))
    stop_loss_pair_sources: Dict[str, str] = {}

    pair_line_edit = pair_input.lineEdit()
    if pair_line_edit is not None:
        pair_line_edit.setPlaceholderText("Any pair: sblk_bhp or SBLK / BHP")

    def current_exit_target_type() -> str:
        return str(target_type_input.currentData() or "pair")

    def current_stop_loss_pair_key() -> str:
        if current_exit_target_type() == "single":
            typed_single = str(pair_input.currentText() or "").strip().upper()
            if typed_single:
                return typed_single
            current_index = pair_input.currentIndex()
            if current_index >= 0:
                item_data = pair_input.itemData(current_index)
                single_key = str(item_data or "").strip().upper()
                if single_key:
                    return single_key
            return ""

        typed_pair = parse_pair_input_text(pair_input.currentText())
        if typed_pair:
            return typed_pair

        current_index = pair_input.currentIndex()
        if current_index >= 0:
            item_data = pair_input.itemData(current_index)
            pair_key = normalize_pair_name(item_data)
            if pair_key:
                return pair_key
        return ""

    rule_scope_input = QComboBox()
    for scope_key, scope_label in EXIT_RULE_SECTION_ITEMS:
        rule_scope_input.addItem(scope_label, scope_key)

    def current_rule_section() -> str:
        return str(rule_scope_input.currentData() or "stop_loss")

    def current_config_section() -> str:
        section = current_rule_section()
        if current_exit_target_type() == "single":
            return f"single_{section}"
        return section

    def current_rule_scope_label() -> str:
        prefix = "Single " if current_exit_target_type() == "single" else ""
        for key, label in EXIT_RULE_SECTION_ITEMS:
            if key == current_rule_section():
                return prefix + label
        return prefix + "Rule"

    def current_mode_items():
        if current_exit_target_type() == "single":
            if current_rule_section() == "take_profit":
                return [("fixed", "Fixed"), ("trailing", "Trailing"), ("time", "Time"), ("raw", "Raw")]
            return [("fixed", "Fixed"), ("trailing", "Trailing"), ("raw", "Raw")]
        return STOP_LOSS_MODE_ITEMS if current_rule_section() == "stop_loss" else TAKE_PROFIT_MODE_ITEMS

    mode_input = QComboBox()

    def reset_mode_input(preferred_mode: str = ""):
        current_mode = preferred_mode or str(mode_input.currentData() or "")
        mode_input.blockSignals(True)
        mode_input.clear()
        for mode_key, label in current_mode_items():
            mode_input.addItem(label, mode_key)
        target_mode = current_mode
        if not target_mode:
            target_mode = "trailing" if current_rule_section() == "stop_loss" else "fixed"
        mode_index = mode_input.findData(target_mode)
        if mode_index < 0:
            fallback_mode = "raw" if current_rule_section() == "take_profit" else "trailing"
            mode_index = mode_input.findData(fallback_mode)
        if mode_index < 0:
            mode_index = 0
        mode_input.setCurrentIndex(mode_index)
        mode_input.blockSignals(False)

    value_input = QLineEdit()
    value_hint = QLabel("")
    value_hint.setStyleSheet("font-size: 12px; color: #666;")

    save_rule_btn = QPushButton("Save / Update")
    delete_rule_btn = QPushButton("Delete")
    reload_rule_btn = QPushButton("Reload")

    editor_row.addWidget(QLabel("Target"))
    editor_row.addWidget(target_type_input)
    editor_row.addWidget(QLabel("Name"))
    editor_row.addWidget(pair_input)
    editor_row.addWidget(QLabel("Rule"))
    editor_row.addWidget(rule_scope_input)
    editor_row.addWidget(QLabel("Mode"))
    editor_row.addWidget(mode_input)
    editor_row.addWidget(QLabel("Value"))
    editor_row.addWidget(value_input, 1)
    editor_row.addWidget(save_rule_btn)
    editor_row.addWidget(delete_rule_btn)
    editor_row.addWidget(reload_rule_btn)
    stop_loss_layout.addLayout(editor_row)
    stop_loss_layout.addWidget(value_hint)

    stop_loss_table = QTableWidget(0, 3)
    stop_loss_table.setHorizontalHeaderLabels(["Name", "Stop Loss", "Take Profit"])
    stop_loss_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    stop_loss_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    stop_loss_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    stop_loss_table.verticalHeader().setVisible(False)
    stop_loss_table.horizontalHeader().setStretchLastSection(True)
    stop_loss_layout.addWidget(stop_loss_table, 1)

    def load_current_rule_config() -> Dict[str, object]:
        return load_exit_rules_config().get(current_config_section(), {})

    def save_current_rule_config(config: Dict[str, object]):
        payload = load_exit_rules_config()
        payload[current_config_section()] = dict(config)
        save_exit_rules_config(payload)

    def split_current_rule(rule) -> tuple[str, str]:
        return split_stop_loss_rule(rule) if current_rule_section() == "stop_loss" else split_take_profit_rule(rule)

    def build_current_rule(mode_key: str, value_text: str):
        return build_stop_loss_rule(mode_key, value_text) if current_rule_section() == "stop_loss" else build_take_profit_rule(mode_key, value_text)

    def current_mode_label(mode_key: str) -> str:
        return stop_loss_mode_label(mode_key) if current_rule_section() == "stop_loss" else take_profit_mode_label(mode_key)

    def update_stop_loss_mode_hint():
        section = current_rule_section()
        mode_key = str(mode_input.currentData() or ("trailing" if section == "stop_loss" else "fixed"))

        if current_exit_target_type() == "single":
            if mode_key == "fixed":
                value_input.setPlaceholderText("-4 or 8")
                value_hint.setText("Single rules use return_pct. Stop exits at <= value; take-profit exits at >= value.")
                return
            if mode_key == "trailing":
                value_input.setPlaceholderText("-1 or -0.5")
                value_hint.setText("Writes trailing(<value>) against max_return. Example: trailing(-1) exits 1% below peak.")
                return
            if section == "take_profit" and mode_key == "time":
                value_input.setPlaceholderText("15:55")
                value_hint.setText("Single take-profit time uses New York HH:MM and exits only when return_pct > 0.")
                return
            value_input.setPlaceholderText("Raw rule text")
            value_hint.setText("Raw mode writes exactly what you type. Single supports numbers, trailing(<value>), and NY(HH:MM).")
            return

        if section == "stop_loss":
            if mode_key == "fixed":
                value_input.setPlaceholderText("0 or -4")
                value_hint.setText("Writes JSON as a number, for example: 0 or -4")
                return
            if mode_key == "sigma":
                value_input.setPlaceholderText("-1 or -1*sigma")
                value_hint.setText("Accepts either -1 or -1*sigma. If you omit *sigma, the GUI adds it for you.")
                return
            if mode_key == "trailing":
                value_input.setPlaceholderText("-0.5 or -0.5*sigma")
                value_hint.setText("Writes JSON as trailing(<value>). Default stop is trailing(-0.5*sigma).")
                return
            value_input.setPlaceholderText("Raw rule text")
            value_hint.setText("Raw mode writes exactly what you type. Use only for advanced/manual rules.")
            return

        if mode_key == "fixed":
            value_input.setPlaceholderText("4 or 8")
            value_hint.setText("Writes JSON as a return threshold. Exit when return_pct reaches this value.")
            return
        if mode_key == "sigma":
            value_input.setPlaceholderText("1, 0.5, or 1*sigma")
            value_hint.setText("Accepts either 1 or 1*sigma. If you omit *sigma, the GUI adds it for you.")
            return
        if mode_key == "trailing":
            value_input.setPlaceholderText("-0.5 or -0.5*sigma")
            value_hint.setText("Writes JSON as trailing(<value>). It only arms after max_return > 0.")
            return
        if mode_key == "ztarget":
            value_input.setPlaceholderText("0, 1, 1.5")
            value_hint.setText("Writes JSON as z(<value>). LONG exits at z>=value; SHORT exits at z<=-value.")
            return
        if mode_key == "time":
            value_input.setPlaceholderText("15:55")
            value_hint.setText("Writes JSON as NY(HH:MM). HH:MM is New York time; exits only after that time when return_pct > 0.")
            return
        value_input.setPlaceholderText("Raw rule text")
        value_hint.setText("Raw mode lets you type values like z(0), NY(15:55), or trailing(-0.5*sigma).")

    def apply_default_editor_for_section(pair: str = ""):
        if pair:
            idx = pair_input.findData(pair)
            if idx >= 0:
                pair_input.setCurrentIndex(idx)
            else:
                pair_input.setEditText(pair if current_exit_target_type() == "single" else format_pair_display(pair))
                pair_input.setCurrentIndex(-1)

        if current_rule_section() == "stop_loss":
            reset_mode_input("trailing")
            value_input.setText("-1" if current_exit_target_type() == "single" else "-0.5*sigma")
        else:
            reset_mode_input("fixed" if current_exit_target_type() == "single" else "ztarget")
            value_input.clear()

        update_stop_loss_mode_hint()

    def set_stop_loss_editor(pair: str, rule):
        mode_key, value_text = split_current_rule(rule)
        idx = pair_input.findData(pair)
        if idx >= 0:
            pair_input.setCurrentIndex(idx)
        else:
            pair_input.setEditText(pair if current_exit_target_type() == "single" else format_pair_display(pair))
            pair_input.setCurrentIndex(-1)
        reset_mode_input(mode_key)
        value_input.setText(value_text)
        update_stop_loss_mode_hint()

    def sync_stop_loss_pair_source_badge():
        pair_key = current_stop_loss_pair_key()
        line_edit = pair_input.lineEdit()
        if isinstance(line_edit, StopLossPairLineEdit):
            line_edit.setSourceText(stop_loss_pair_sources.get(pair_key, ""))

    def refresh_stop_loss_tab(selected_pair: str = "", reset_editor: bool = True):
        try:
            exit_rules = load_exit_rules_config()
        except Exception as exc:
            QMessageBox.warning(window, f"{current_rule_scope_label()} Config Error", str(exc))
            return

        stop_loss_config = exit_rules.get("stop_loss", {})
        take_profit_config = exit_rules.get("take_profit", {})
        row_to_select = -1
        target_pair = normalize_pair_name(selected_pair) if selected_pair else current_stop_loss_pair_key()
        if current_exit_target_type() == "single":
            target_pair = str(selected_pair or current_stop_loss_pair_key()).strip().upper()
 
        pair_sources = fetch_available_single_sources() if current_exit_target_type() == "single" else fetch_available_pair_sources()
        stop_loss_pair_sources.clear()
        stop_loss_pair_sources.update(pair_sources)
        source_priority = {"HOLDING": 0, "PENDING": 1, "REENTRY": 2}
        stop_key = current_config_section() if current_rule_section() == "stop_loss" else ("single_stop_loss" if current_exit_target_type() == "single" else "stop_loss")
        take_key = "single_take_profit" if current_exit_target_type() == "single" else "take_profit"
        stop_loss_config = exit_rules.get(stop_key, {})
        take_profit_config = exit_rules.get(take_key, {})
        known_pairs = sorted(
            set(pair_sources.keys()) | set(stop_loss_config.keys()) | set(take_profit_config.keys()),
            key=lambda pair: (source_priority.get(str(pair_sources.get(pair) or "").upper(), 9), pair),
        )

        stop_loss_table.setRowCount(len(known_pairs))
        for row, pair in enumerate(known_pairs):
            stop_rule = stop_loss_config.get(pair)
            take_rule = take_profit_config.get(pair)
            pair_item = QTableWidgetItem(pair)
            stop_item = QTableWidgetItem(format_stop_loss_rule(stop_rule) if stop_rule is not None else "")
            take_item = QTableWidgetItem(format_stop_loss_rule(take_rule) if take_rule is not None else "")
            stop_loss_table.setItem(row, 0, pair_item)
            stop_loss_table.setItem(row, 1, stop_item)
            stop_loss_table.setItem(row, 2, take_item)
            if pair == target_pair:
                row_to_select = row

        current_text = target_pair or current_stop_loss_pair_key()
        pair_input.blockSignals(True)
        pair_input.clear()
        if known_pairs:
            for pair in known_pairs:
                pair_input.addItem(pair if current_exit_target_type() == "single" else format_pair_display(pair), pair)
                item_index = pair_input.count() - 1
                source_text = str(pair_sources.get(pair) or "").strip().upper()
                if source_text:
                    pair_input.setItemData(item_index, source_text, stop_loss_source_role)
        if current_text:
            idx = pair_input.findData(current_text)
            if idx >= 0:
                pair_input.setCurrentIndex(idx)
            else:
                pair_input.setEditText(current_text if current_exit_target_type() == "single" else format_pair_display(current_text))
                pair_input.setCurrentIndex(-1)
        pair_input.blockSignals(False)
        sync_stop_loss_pair_source_badge()

        if not reset_editor:
            if row_to_select >= 0:
                stop_loss_table.selectRow(row_to_select)
            return

        current_config = load_current_rule_config()
        if row_to_select >= 0:
            stop_loss_table.selectRow(row_to_select)
            if target_pair in current_config:
                set_stop_loss_editor(target_pair, current_config[target_pair])
            else:
                apply_default_editor_for_section(target_pair)
        else:
            apply_default_editor_for_section(target_pair)

    def apply_stop_loss_rule():
        pair = current_stop_loss_pair_key()
        if not pair:
            QMessageBox.warning(window, "Missing Pair", "Please enter a pair name.")
            return

        try:
            mode_key = str(mode_input.currentData() or ("trailing" if current_rule_section() == "stop_loss" else "fixed"))
            rule = build_current_rule(mode_key, value_input.text())
            config = load_current_rule_config()
            config[pair] = rule
            save_current_rule_config(config)
        except Exception as exc:
            QMessageBox.warning(window, "Save Failed", str(exc))
            return

        set_stop_loss_editor(pair, rule)
        refresh_stop_loss_tab(pair)
        statusbar.showMessage(
            f"{current_rule_scope_label()} saved: {pair} = {format_stop_loss_rule(rule)}",
            6000,
        )

    def delete_stop_loss_rule():
        rows = stop_loss_table.selectionModel().selectedRows()
        selected_pair = ""
        if rows:
            selected_pair = stop_loss_table.item(rows[0].row(), 0).text()
        else:
            selected_pair = current_stop_loss_pair_key()

        if not selected_pair:
            QMessageBox.warning(window, "Missing Pair", "Select a pair to delete.")
            return

        try:
            config = load_current_rule_config()
        except Exception as exc:
            QMessageBox.warning(window, "Delete Failed", str(exc))
            return

        if selected_pair not in config:
            QMessageBox.information(window, "Not Found", f"{selected_pair} is not in the config.")
            return

        del config[selected_pair]
        save_current_rule_config(config)
        pair_input.setEditText(selected_pair if current_exit_target_type() == "single" else format_pair_display(selected_pair))
        apply_default_editor_for_section(selected_pair)
        refresh_stop_loss_tab("")
        statusbar.showMessage(f"Removed {current_rule_scope_label()} for {selected_pair}", 6000)

    def sync_stop_loss_selection():
        rows = stop_loss_table.selectionModel().selectedRows()
        if not rows:
            return

        row = rows[0].row()
        pair_item = stop_loss_table.item(row, 0)
        if pair_item is None:
            return

        pair_name = pair_item.text()
        config = load_current_rule_config()
        if pair_name in config:
            set_stop_loss_editor(pair_name, config[pair_name])
        else:
            apply_default_editor_for_section(pair_name)

    def on_rule_scope_changed():
        pair_key = current_stop_loss_pair_key()
        reset_mode_input()
        refresh_stop_loss_tab(pair_key, reset_editor=False)
        update_stop_loss_mode_hint()

    def on_exit_target_type_changed():
        pair_line_edit = pair_input.lineEdit()
        if pair_line_edit is not None:
            if current_exit_target_type() == "single":
                pair_line_edit.setPlaceholderText("Any stock symbol: AAPL")
            else:
                pair_line_edit.setPlaceholderText("Any pair: sblk_bhp or SBLK / BHP")
        reset_mode_input()
        refresh_stop_loss_tab("", reset_editor=True)
        update_stop_loss_mode_hint()

    reset_mode_input("trailing")
    mode_input.currentIndexChanged.connect(update_stop_loss_mode_hint)
    rule_scope_input.currentIndexChanged.connect(on_rule_scope_changed)
    target_type_input.currentIndexChanged.connect(on_exit_target_type_changed)
    pair_input.currentTextChanged.connect(lambda _text: sync_stop_loss_pair_source_badge())
    save_rule_btn.clicked.connect(apply_stop_loss_rule)
    delete_rule_btn.clicked.connect(delete_stop_loss_rule)
    reload_rule_btn.clicked.connect(lambda: refresh_stop_loss_tab(pair_input.currentText()))
    stop_loss_table.itemSelectionChanged.connect(sync_stop_loss_selection)
    value_input.returnPressed.connect(apply_stop_loss_rule)

    update_stop_loss_mode_hint()
    refresh_stop_loss_tab("sblk_bhp")

    simple_web = QWebEngineView()
    if geo:
        simple_web.setZoomFactor(compute_zoom(geo.height()))
    tabs.addTab(simple_web, "Simple Dashboard")

    quant_web = QWebEngineView()
    if geo:
        quant_web.setZoomFactor(compute_zoom(geo.height()))
    tabs.addTab(quant_web, "Quant Dashboard")

    adf_tab = QWidget()
    adf_tab.setObjectName("ordersManagementTab")
    adf_layout = QVBoxLayout(adf_tab)
    adf_layout.setContentsMargins(12, 12, 12, 12)
    adf_layout.setSpacing(10)

    adf_title = QLabel("ADF Finder")
    adf_title.setObjectName("ordersManagementTitle")
    adf_desc = QLabel(
        "Run recent ADF scans from local market prices and manage generated CSV configs for Quant Dashboard."
    )
    adf_desc.setWordWrap(True)
    adf_desc.setObjectName("ordersManagementDesc")
    adf_layout.addWidget(adf_title)
    adf_layout.addWidget(adf_desc)

    adf_control_row = QHBoxLayout()
    adf_control_row.setSpacing(8)
    adf_days_combo = QComboBox()
    adf_days_combo.addItems(["30", "60", "90"])
    adf_days_combo.setCurrentText("60")
    adf_unexpected_checkbox = QCheckBox("Include unexpected combo pairs")
    adf_run_btn = QPushButton("Run ADF Finder")
    adf_refresh_btn = QPushButton("Refresh CSVs")
    adf_delete_btn = QPushButton("Delete Selected CSV")
    for btn in [adf_run_btn, adf_refresh_btn, adf_delete_btn]:
        btn.setProperty("role", "toolbarButton")
    adf_status = QLabel("")
    adf_status.setObjectName("ordersManagementHint")
    adf_control_row.addWidget(QLabel("Recent days"))
    adf_control_row.addWidget(adf_days_combo)
    adf_control_row.addWidget(adf_unexpected_checkbox)
    adf_control_row.addWidget(adf_run_btn)
    adf_control_row.addWidget(adf_refresh_btn)
    adf_control_row.addWidget(adf_delete_btn)
    adf_control_row.addWidget(adf_status, 1)
    adf_layout.addLayout(adf_control_row)

    adf_content_row = QHBoxLayout()
    adf_content_row.setSpacing(12)
    adf_layout.addLayout(adf_content_row, 1)

    adf_csv_table = QTableWidget(0, 5)
    adf_csv_table.setObjectName("ordersManagementTable")
    adf_csv_table.setHorizontalHeaderLabels(["File", "Generated", "Rows", "Size KB", "Latest"])
    adf_csv_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    adf_csv_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    adf_csv_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    adf_csv_table.verticalHeader().setVisible(False)
    adf_csv_table.horizontalHeader().setStretchLastSection(False)
    adf_csv_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    adf_csv_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
    adf_csv_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
    adf_csv_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
    adf_csv_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
    adf_csv_table.setColumnWidth(1, 150)
    adf_csv_table.setColumnWidth(2, 70)
    adf_csv_table.setColumnWidth(3, 80)
    adf_csv_table.setColumnWidth(4, 70)
    adf_content_row.addWidget(adf_csv_table, 2)

    adf_output = QPlainTextEdit()
    adf_output.setReadOnly(True)
    adf_output.setPlaceholderText("ADF Finder output will appear here.")
    adf_content_row.addWidget(adf_output, 3)

    adf_process_ref = {"process": None}

    def adf_python_executable() -> str:
        candidates = [
            os.path.join(PROJECT_DIR, "quant", "bin", "python3"),
            os.path.join(PROJECT_DIR, "quant", "Scripts", "python.exe"),
            sys.executable,
        ]
        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                return candidate
        return sys.executable

    def adf_csv_row_count(path: str) -> int:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return max(0, sum(1 for _line in handle) - 1)
        except Exception:
            return 0

    def list_adf_csv_files() -> List[str]:
        if not os.path.isdir(ADF_FINDER_LOG_DIR):
            return []
        files = [
            os.path.join(ADF_FINDER_LOG_DIR, name)
            for name in os.listdir(ADF_FINDER_LOG_DIR)
            if name.lower().endswith(".csv")
        ]
        return sorted(files, key=lambda path: os.path.getmtime(path), reverse=True)

    def refresh_adf_csv_table():
        files = list_adf_csv_files()
        adf_csv_table.setRowCount(len(files))
        for row_index, path in enumerate(files):
            adf_csv_table.setRowHeight(row_index, 32)
            filename = os.path.basename(path)
            generated = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")
            row_count = adf_csv_row_count(path)
            size_kb = os.path.getsize(path) / 1024.0
            latest_path = os.path.abspath(os.path.join(ADF_FINDER_LOG_DIR, "latest.csv"))
            values = [
                filename,
                generated,
                str(row_count),
                f"{size_kb:.1f}",
                "Yes" if os.path.abspath(path) == latest_path else "",
            ]
            for col_index, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col_index == 0:
                    item.setData(Qt.ItemDataRole.UserRole, path)
                    item.setToolTip(path)
                if col_index in {2, 3, 4}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                adf_csv_table.setItem(row_index, col_index, item)
        adf_status.setText(f"CSV files: {len(files)} | {ADF_FINDER_LOG_DIR}")

    def append_adf_output(text: str):
        if not text:
            return
        adf_output.moveCursor(QTextCursor.MoveOperation.End)
        adf_output.insertPlainText(text)
        adf_output.moveCursor(QTextCursor.MoveOperation.End)

    def run_adf_finder_now():
        existing = adf_process_ref.get("process")
        if existing is not None and existing.state() != QProcess.ProcessState.NotRunning:
            QMessageBox.information(window, "ADF Finder", "ADF Finder is already running.")
            return

        os.makedirs(ADF_FINDER_LOG_DIR, exist_ok=True)
        days = adf_days_combo.currentText().strip() or "60"
        include_unexpected = adf_unexpected_checkbox.isChecked()
        args = [
            os.path.join(PROJECT_DIR, "pair_adf_finder.py"),
            "--days",
            days,
            "--top",
            "50",
        ]
        if include_unexpected:
            args.extend([
                "--mode",
                "both",
                "--universe",
                "registered-symbols",
                "--pvalue-max",
                "0.05",
                "--min-abs-corr",
                "0.5",
                "--max-symbols",
                "80",
            ])
        else:
            args.extend(["--mode", "registered", "--pvalue-max", "1.0"])

        process = QProcess(window)
        process.setWorkingDirectory(PROJECT_DIR)
        process.setProgram(adf_python_executable())
        process.setArguments(args)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        adf_process_ref["process"] = process
        adf_output.clear()
        append_adf_output("$ " + shlex.join([process.program()] + args) + "\n\n")
        adf_status.setText(f"Running {days}d scan...")
        adf_run_btn.setEnabled(False)

        def handle_ready_read():
            data = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
            append_adf_output(data)

        def handle_finished(exit_code, _exit_status):
            adf_run_btn.setEnabled(True)
            refresh_adf_csv_table()
            if int(exit_code) == 0:
                adf_status.setText("ADF Finder completed.")
                statusbar.showMessage("ADF Finder completed.", 6000)
            else:
                adf_status.setText(f"ADF Finder failed: exit {exit_code}")
                statusbar.showMessage(f"ADF Finder failed: exit {exit_code}", 8000)

        process.readyReadStandardOutput.connect(handle_ready_read)
        process.finished.connect(handle_finished)
        process.start()

    def selected_adf_csv_path() -> str:
        rows = adf_csv_table.selectionModel().selectedRows()
        if not rows:
            return ""
        item = adf_csv_table.item(rows[0].row(), 0)
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""

    def delete_selected_adf_csv():
        path = selected_adf_csv_path()
        if not path:
            QMessageBox.information(window, "Delete ADF CSV", "Select a CSV row first.")
            return
        answer = QMessageBox.question(
            window,
            "Delete ADF CSV",
            f"Delete this CSV?\n\n{path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            os.remove(path)
        except Exception as exc:
            QMessageBox.warning(window, "Delete ADF CSV Failed", str(exc))
            return
        refresh_adf_csv_table()
        statusbar.showMessage(f"Deleted ADF CSV: {os.path.basename(path)}", 6000)

    adf_run_btn.clicked.connect(run_adf_finder_now)
    adf_refresh_btn.clicked.connect(refresh_adf_csv_table)
    adf_delete_btn.clicked.connect(delete_selected_adf_csv)
    refresh_adf_csv_table()
    adf_stylesheet = load_optional_stylesheet(ORDERS_MANAGEMENT_STYLE_PATH)
    if adf_stylesheet:
        adf_tab.setStyleSheet(adf_stylesheet)
    tabs.addTab(adf_tab, "ADF Finder")

    web_tabs_loaded = {"simple": False, "quant": False}

    def load_web_tab_if_needed(widget=None, force: bool = False):
        current = widget or tabs.currentWidget()
        if current is simple_web and (force or not web_tabs_loaded["simple"]):
            simple_web.setUrl(QUrl(f"{API_BASE}/"))
            web_tabs_loaded["simple"] = True
        elif current is quant_web and (force or not web_tabs_loaded["quant"]):
            quant_web.setUrl(QUrl(STREAMLIT_URL))
            web_tabs_loaded["quant"] = True

    tabs.currentChanged.connect(lambda _index: load_web_tab_if_needed())

    subscription_tab = QWidget()
    subscription_tab.setObjectName("ordersManagementTab")
    subscription_layout = QVBoxLayout(subscription_tab)
    subscription_layout.setContentsMargins(12, 12, 12, 12)
    subscription_layout.setSpacing(10)

    subscription_title = QLabel("Subscribtion")
    subscription_title.setObjectName("ordersManagementTitle")
    subscription_desc = QLabel(
        "Manage active stock and metapair subscriptions from quant_data.us_info and quant_data.metapairs."
    )
    subscription_desc.setWordWrap(True)
    subscription_desc.setObjectName("ordersManagementDesc")
    subscription_layout.addWidget(subscription_title)
    subscription_layout.addWidget(subscription_desc)

    subscription_button_row = QHBoxLayout()
    subscription_button_row.setSpacing(8)
    refresh_subscription_btn = QPushButton("Refresh")
    refresh_subscription_btn.setProperty("role", "toolbarButton")
    subscription_status = QLabel("")
    subscription_status.setObjectName("ordersManagementHint")
    subscription_button_row.addWidget(refresh_subscription_btn)
    subscription_button_row.addWidget(subscription_status)
    subscription_button_row.addStretch(1)
    subscription_layout.addLayout(subscription_button_row)

    subscription_register_row = QHBoxLayout()
    subscription_register_row.setSpacing(12)
    subscription_layout.addLayout(subscription_register_row)

    stock_register_panel = QWidget()
    stock_register_layout = QGridLayout(stock_register_panel)
    stock_register_layout.setContentsMargins(0, 0, 0, 0)
    stock_register_layout.setHorizontalSpacing(8)
    stock_register_layout.setVerticalSpacing(6)
    stock_register_title = QLabel("Quick Stock Register")
    stock_ticker_input = QLineEdit()
    stock_ticker_input.setPlaceholderText("Ticker")
    stock_sector_combo = QComboBox()
    stock_asset_combo = QComboBox()
    stock_type_combo = QComboBox()
    stock_currency_combo = QComboBox()
    stock_exchange_combo = QComboBox()
    for combo in [stock_sector_combo, stock_asset_combo, stock_type_combo, stock_currency_combo, stock_exchange_combo]:
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    stock_active_checkbox = QCheckBox("Active")
    stock_active_checkbox.setChecked(True)
    register_stock_btn = QPushButton("Register")
    delete_stock_btn = QPushButton("Delete")
    register_stock_btn.setProperty("role", "toolbarButton")
    delete_stock_btn.setProperty("role", "toolbarButton")

    stock_register_layout.addWidget(stock_register_title, 0, 0, 1, 6)
    stock_register_layout.addWidget(QLabel("Ticker"), 1, 0)
    stock_register_layout.addWidget(stock_ticker_input, 1, 1)
    stock_register_layout.addWidget(QLabel("Sector"), 1, 2)
    stock_register_layout.addWidget(stock_sector_combo, 1, 3, 1, 3)
    stock_register_layout.addWidget(QLabel("Asset"), 2, 0)
    stock_register_layout.addWidget(stock_asset_combo, 2, 1)
    stock_register_layout.addWidget(QLabel("Type"), 2, 2)
    stock_register_layout.addWidget(stock_type_combo, 2, 3)
    stock_register_layout.addWidget(QLabel("Currency"), 2, 4)
    stock_register_layout.addWidget(stock_currency_combo, 2, 5)
    stock_register_layout.addWidget(QLabel("Exchange"), 3, 0)
    stock_register_layout.addWidget(stock_exchange_combo, 3, 1)
    stock_register_layout.addWidget(stock_active_checkbox, 3, 2)
    stock_register_layout.addWidget(register_stock_btn, 3, 3)
    stock_register_layout.addWidget(delete_stock_btn, 3, 4)

    pair_register_panel = QWidget()
    pair_register_layout = QGridLayout(pair_register_panel)
    pair_register_layout.setContentsMargins(0, 0, 0, 0)
    pair_register_layout.setHorizontalSpacing(8)
    pair_register_layout.setVerticalSpacing(6)
    pair_register_title = QLabel("Quick Pair Register")
    pair_name_input = QLineEdit()
    pair_name_input.setPlaceholderText("Auto: ticker_a_ticker_b")
    pair_ticker_a_combo = QComboBox()
    pair_ticker_b_combo = QComboBox()
    pair_type_combo = QComboBox()
    for combo in [pair_ticker_a_combo, pair_ticker_b_combo, pair_type_combo]:
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    pair_active_checkbox = QCheckBox("Active")
    pair_active_checkbox.setChecked(True)
    register_pair_btn = QPushButton("Register")
    delete_pair_btn = QPushButton("Delete")
    register_pair_btn.setProperty("role", "toolbarButton")
    delete_pair_btn.setProperty("role", "toolbarButton")

    pair_register_layout.addWidget(pair_register_title, 0, 0, 1, 6)
    pair_register_layout.addWidget(QLabel("Pair"), 1, 0)
    pair_register_layout.addWidget(pair_name_input, 1, 1, 1, 2)
    pair_register_layout.addWidget(QLabel("Type"), 1, 3)
    pair_register_layout.addWidget(pair_type_combo, 1, 4)
    pair_register_layout.addWidget(pair_active_checkbox, 1, 5)
    pair_register_layout.addWidget(QLabel("Ticker A"), 2, 0)
    pair_register_layout.addWidget(pair_ticker_a_combo, 2, 1)
    pair_register_layout.addWidget(QLabel("Ticker B"), 2, 2)
    pair_register_layout.addWidget(pair_ticker_b_combo, 2, 3)
    pair_register_layout.addWidget(register_pair_btn, 2, 4)
    pair_register_layout.addWidget(delete_pair_btn, 2, 5)

    subscription_register_row.addWidget(stock_register_panel, 1)
    subscription_register_row.addWidget(pair_register_panel, 1)

    subscription_tables_row = QHBoxLayout()
    subscription_tables_row.setSpacing(12)
    subscription_layout.addLayout(subscription_tables_row, 1)

    stocks_panel = QWidget()
    stocks_layout = QVBoxLayout(stocks_panel)
    stocks_layout.setContentsMargins(0, 0, 0, 0)
    stocks_layout.setSpacing(6)
    stocks_title = QLabel("Stocks")
    stocks_title.setObjectName("ordersManagementTitle")
    stocks_table = QTableWidget(0, 6)
    stocks_table.setObjectName("ordersManagementTable")
    stocks_table.setHorizontalHeaderLabels(["Ticker", "Sector", "Asset", "Type", "Exchange", "Active"])
    stocks_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    stocks_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    stocks_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    stocks_table.verticalHeader().setVisible(False)
    stocks_table.horizontalHeader().setStretchLastSection(False)
    stocks_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
    stocks_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    stocks_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
    stocks_table.setColumnWidth(0, 82)
    stocks_table.setColumnWidth(5, 72)
    stocks_layout.addWidget(stocks_title)
    stocks_layout.addWidget(stocks_table, 1)

    metapairs_panel = QWidget()
    metapairs_layout = QVBoxLayout(metapairs_panel)
    metapairs_layout.setContentsMargins(0, 0, 0, 0)
    metapairs_layout.setSpacing(6)
    metapairs_title = QLabel("Meta Pairs")
    metapairs_title.setObjectName("ordersManagementTitle")
    metapairs_table = QTableWidget(0, 5)
    metapairs_table.setObjectName("ordersManagementTable")
    metapairs_table.setHorizontalHeaderLabels(["Ticker A", "Ticker B", "Sector A", "Sector B", "Active"])
    metapairs_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    metapairs_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    metapairs_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    metapairs_table.verticalHeader().setVisible(False)
    metapairs_table.horizontalHeader().setStretchLastSection(False)
    metapairs_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
    metapairs_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
    metapairs_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
    metapairs_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
    metapairs_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
    metapairs_table.setColumnWidth(0, 82)
    metapairs_table.setColumnWidth(1, 82)
    metapairs_table.setColumnWidth(4, 72)
    metapairs_layout.addWidget(metapairs_title)
    metapairs_layout.addWidget(metapairs_table, 1)

    subscription_tables_row.addWidget(stocks_panel, 1)
    subscription_tables_row.addWidget(metapairs_panel, 1)

    subscription_stylesheet = load_optional_stylesheet(ORDERS_MANAGEMENT_STYLE_PATH)
    if subscription_stylesheet:
        subscription_tab.setStyleSheet(subscription_stylesheet)

    def set_checkbox_checked_silently(checkbox: QCheckBox, checked: bool):
        checkbox.blockSignals(True)
        checkbox.setChecked(checked)
        checkbox.blockSignals(False)

    def make_active_checkbox(kind: str, key: str, active_value) -> QWidget:
        checked = bool(int(active_value or 0))
        checkbox = QCheckBox()
        checkbox.setChecked(checked)
        checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        checkbox.toggled.connect(
            lambda is_checked, target_kind=kind, target_key=key, cb=checkbox: apply_subscription_active_change(
                target_kind,
                target_key,
                is_checked,
                cb,
            )
        )

        host = QWidget()
        layout = QHBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addStretch(1)
        layout.addWidget(checkbox)
        layout.addStretch(1)
        return host

    def apply_subscription_active_change(kind: str, key: str, is_active: bool, checkbox: QCheckBox):
        try:
            if kind == "stock":
                updated = update_subscription_stock_active(key, is_active)
            else:
                updated = update_subscription_metapair_active(key, is_active)
        except Exception as exc:
            set_checkbox_checked_silently(checkbox, not is_active)
            QMessageBox.warning(window, "Update Failed", str(exc))
            return

        if not updated:
            set_checkbox_checked_silently(checkbox, not is_active)
            QMessageBox.warning(window, "Update Failed", f"No row was updated for {key}.")
            return

        statusbar.showMessage(f"Updated {key}: is_active={1 if is_active else 0}", 5000)

    def make_subscription_item(value, center: bool = False) -> QTableWidgetItem:
        item = QTableWidgetItem("" if value is None else str(value))
        if center:
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return item

    def make_sector_emoji_item(emoji_value, sector_value) -> QTableWidgetItem:
        item = make_subscription_item(str(emoji_value or "").strip(), center=True)
        sector_text = str(sector_value or "").strip()
        if sector_text:
            item.setToolTip(sector_text)
        return item

    def fill_subscription_stocks(rows: List[dict]):
        stocks_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            stocks_table.setRowHeight(row_index, 34)
            ticker = str(row.get("ticker") or "").strip().upper()
            values = [
                ticker,
                None,
                row.get("asset_class"),
                row.get("instrument_type"),
                row.get("exchange"),
            ]
            for col_index, value in enumerate(values):
                stocks_table.setItem(
                    row_index,
                    col_index,
                    make_subscription_item(value, center=col_index in {0, 2, 3, 4}),
                )
            stocks_table.setItem(
                row_index,
                1,
                make_sector_emoji_item(row.get("sector_emoji"), row.get("sector")),
            )
            stocks_table.setCellWidget(row_index, 5, make_active_checkbox("stock", ticker, row.get("is_active")))

    def fill_subscription_metapairs(rows: List[dict]):
        metapairs_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            metapairs_table.setRowHeight(row_index, 34)
            pair_name = normalize_pair_name(row.get("pair_name"))
            ticker_a = str(row.get("ticker_a") or "").strip().upper()
            ticker_b = str(row.get("ticker_b") or "").strip().upper()
            ticker_a_item = make_subscription_item(ticker_a, center=True)
            ticker_a_item.setData(Qt.ItemDataRole.UserRole, pair_name)
            if pair_name:
                ticker_a_item.setToolTip(pair_name)
            ticker_b_item = make_subscription_item(ticker_b, center=True)
            if pair_name:
                ticker_b_item.setToolTip(pair_name)
            metapairs_table.setItem(row_index, 0, ticker_a_item)
            metapairs_table.setItem(row_index, 1, ticker_b_item)
            metapairs_table.setItem(
                row_index,
                2,
                make_sector_emoji_item(row.get("sector_a_emoji"), row.get("sector_a")),
            )
            metapairs_table.setItem(
                row_index,
                3,
                make_sector_emoji_item(row.get("sector_b_emoji"), row.get("sector_b")),
            )
            metapairs_table.setCellWidget(row_index, 4, make_active_checkbox("metapair", pair_name, row.get("is_active")))

    def set_combo_values(combo: QComboBox, values: List[str], preferred: str = ""):
        current_text = str(preferred or combo.currentText() or "").strip()
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(values)
        if current_text:
            index = combo.findText(current_text)
            if index >= 0:
                combo.setCurrentIndex(index)
            else:
                combo.setEditText(current_text)
        elif values:
            combo.setCurrentIndex(0)
        combo.blockSignals(False)

    def refresh_subscription_register_options():
        options = load_subscription_options()
        set_combo_values(stock_sector_combo, options.get("sectors", []))
        set_combo_values(stock_asset_combo, options.get("asset_classes", []), "equity")
        set_combo_values(stock_type_combo, options.get("instrument_types", []), "stock")
        set_combo_values(stock_currency_combo, options.get("currencies", []) or ["USD"], "USD")
        set_combo_values(stock_exchange_combo, options.get("exchanges", []))
        set_combo_values(pair_ticker_a_combo, options.get("tickers", []))
        set_combo_values(pair_ticker_b_combo, options.get("tickers", []))
        set_combo_values(pair_type_combo, options.get("pair_types", []) or ["peer", "chain"], "peer")

    def selected_subscription_stock_key() -> str:
        rows = stocks_table.selectionModel().selectedRows()
        if rows:
            item = stocks_table.item(rows[0].row(), 0)
            if item is not None:
                return item.text().strip().upper()
        return stock_ticker_input.text().strip().upper()

    def selected_subscription_pair_key() -> str:
        rows = metapairs_table.selectionModel().selectedRows()
        if rows:
            item = metapairs_table.item(rows[0].row(), 0)
            if item is not None:
                return normalize_pair_name(item.data(Qt.ItemDataRole.UserRole))
        return normalize_pair_name(pair_name_input.text())

    def sync_pair_name_placeholder():
        left = pair_ticker_a_combo.currentText().strip().upper()
        right = pair_ticker_b_combo.currentText().strip().upper()
        if left and right:
            pair_name_input.setPlaceholderText(normalize_pair_name(f"{left}_{right}"))

    def register_subscription_stock_now():
        try:
            insert_subscription_stock(
                stock_ticker_input.text(),
                stock_sector_combo.currentText(),
                stock_asset_combo.currentText(),
                stock_type_combo.currentText(),
                stock_currency_combo.currentText(),
                stock_exchange_combo.currentText(),
                stock_active_checkbox.isChecked(),
            )
        except Exception as exc:
            QMessageBox.warning(window, "Register Stock Failed", str(exc))
            return

        symbol = stock_ticker_input.text().strip().upper()
        stock_ticker_input.clear()
        refresh_subscription_tab()
        statusbar.showMessage(f"Registered stock: {symbol}", 6000)

    def delete_subscription_stock_now():
        symbol = selected_subscription_stock_key()
        if not symbol:
            QMessageBox.warning(window, "Delete Stock", "Select a stock row or enter a ticker.")
            return

        answer = QMessageBox.question(
            window,
            "Delete Stock",
            f"Delete {symbol} from quant_data.us_info?\n\nThis will fail if metapairs still reference it.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            deleted = delete_subscription_stock(symbol)
        except Exception as exc:
            QMessageBox.warning(window, "Delete Stock Failed", str(exc))
            return

        if not deleted:
            QMessageBox.warning(window, "Delete Stock Failed", f"No row was deleted for {symbol}.")
            return

        stock_ticker_input.clear()
        refresh_subscription_tab()
        statusbar.showMessage(f"Deleted stock: {symbol}", 6000)

    def register_subscription_pair_now():
        try:
            pair_key = insert_subscription_metapair(
                pair_name_input.text(),
                pair_ticker_a_combo.currentText(),
                pair_ticker_b_combo.currentText(),
                pair_type_combo.currentText(),
                pair_active_checkbox.isChecked(),
            )
        except Exception as exc:
            QMessageBox.warning(window, "Register Pair Failed", str(exc))
            return

        pair_name_input.clear()
        refresh_subscription_tab()
        statusbar.showMessage(f"Registered pair: {pair_key}", 6000)

    def delete_subscription_pair_now():
        pair_key = selected_subscription_pair_key()
        if not pair_key:
            QMessageBox.warning(window, "Delete Pair", "Select a pair row or enter a pair name.")
            return

        answer = QMessageBox.question(
            window,
            "Delete Pair",
            f"Delete {pair_key} from quant_data.metapairs?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            deleted = delete_subscription_metapair(pair_key)
        except Exception as exc:
            QMessageBox.warning(window, "Delete Pair Failed", str(exc))
            return

        if not deleted:
            QMessageBox.warning(window, "Delete Pair Failed", f"No row was deleted for {pair_key}.")
            return

        pair_name_input.clear()
        refresh_subscription_tab()
        statusbar.showMessage(f"Deleted pair: {pair_key}", 6000)

    def refresh_subscription_tab():
        try:
            stock_rows = load_subscription_stocks()
            metapair_rows = load_subscription_metapairs()
            refresh_subscription_register_options()
        except Exception as exc:
            QMessageBox.warning(window, "Subscription Load Failed", str(exc))
            return

        fill_subscription_stocks(stock_rows)
        fill_subscription_metapairs(metapair_rows)
        subscription_status.setText(f"Stocks: {len(stock_rows)} | Meta Pairs: {len(metapair_rows)}")

    refresh_subscription_btn.clicked.connect(refresh_subscription_tab)
    register_stock_btn.clicked.connect(register_subscription_stock_now)
    delete_stock_btn.clicked.connect(delete_subscription_stock_now)
    register_pair_btn.clicked.connect(register_subscription_pair_now)
    delete_pair_btn.clicked.connect(delete_subscription_pair_now)
    pair_ticker_a_combo.currentTextChanged.connect(lambda _text: sync_pair_name_placeholder())
    pair_ticker_b_combo.currentTextChanged.connect(lambda _text: sync_pair_name_placeholder())
    tabs.addTab(subscription_tab, "Subscribtion")

    order_columns = [
        ("id", "ID"),
        ("pair_name", "Pair"),
        ("position_type", "Type"),
        ("beta", "Beta"),
        ("two_sigma", "Two Sigma"),
        ("exposure_pct", "Exposure%"),
        ("z_score", "z-score"),
        ("result", "Result"),
        ("max_return", "Max Return"),
        ("created_at", "Created At"),
        ("close_time", "Close Time"),
        ("action", "Action"),
    ]
    pair_name_col_index = next(i for i, (field_name, _) in enumerate(order_columns) if field_name == "pair_name")
    z_score_col_index = next(i for i, (field_name, _) in enumerate(order_columns) if field_name == "z_score")
    action_col_index = next(i for i, (field_name, _) in enumerate(order_columns) if field_name == "action")
    created_at_col_index = next(i for i, (field_name, _) in enumerate(order_columns) if field_name == "created_at")
    close_time_col_index = next(i for i, (field_name, _) in enumerate(order_columns) if field_name == "close_time")
    action_button_width = 112
    action_column_width = 126
    timestamp_column_width = 172
    order_fixed_column_widths = {
        "id": 32,
        "pair_name": 90,
        "position_type": 64,
        "beta": 78,
        "two_sigma": 92,
        "exposure_pct": 96,
        "z_score": 86,
        "result": 70,
        "max_return": 92,
    }
    orders_column_width_cache: Dict[str, List[int]] = {}

    orders_tab = QWidget()
    orders_tab.setObjectName("ordersManagementTab")
    orders_layout = QVBoxLayout(orders_tab)
    orders_layout.setContentsMargins(12, 12, 12, 12)
    orders_layout.setSpacing(10)

    orders_title = QLabel("Pair Management")
    orders_title.setObjectName("ordersManagementTitle")
    orders_desc = QLabel(
        f"Shows all Pending/Holding scheduled_orders and Closed orders from the last {RECENT_DAYS} days. "
        "Pending z-score is refreshed from app.py every 3 seconds only during the US regular session."
    )
    orders_desc.setWordWrap(True)
    orders_desc.setObjectName("ordersManagementDesc")
    orders_layout.addWidget(orders_title)
    orders_layout.addWidget(orders_desc)

    orders_button_row = QHBoxLayout()
    orders_button_row.setSpacing(8)
    refresh_orders_btn = QPushButton("Refresh")
    place_all_pending_btn = QPushButton("Place All Pending")
    hide_time_columns_checkbox = QCheckBox("Show Timestamps")
    hide_time_columns_checkbox.setChecked(False)
    refresh_orders_btn.setProperty("role", "toolbarButton")
    place_all_pending_btn.setProperty("role", "toolbarButton")
    hide_time_columns_checkbox.setProperty("role", "toolbarCheck")
    orders_hint = QLabel(
        "Open and Close actions now live on each row. Pending rows show Open 🔓, Holding rows show Close 🔒."
    )
    orders_hint.setObjectName("ordersManagementHint")

    orders_button_row.addWidget(refresh_orders_btn)
    orders_button_row.addWidget(place_all_pending_btn)
    orders_button_row.addWidget(hide_time_columns_checkbox)
    orders_button_row.addStretch(1)
    orders_layout.addLayout(orders_button_row)
    orders_layout.addWidget(orders_hint)

    orders_tabs = QTabWidget()
    orders_tabs.setObjectName("ordersManagementTabs")
    orders_layout.addWidget(orders_tabs, 1)

    order_tables: Dict[str, QTableWidget] = {}
    for status_name in ["PENDING", "HOLDING", "CLOSED"]:
        table = QTableWidget(0, len(order_columns))
        table.setObjectName("ordersManagementTable")
        table.setHorizontalHeaderLabels([label for _, label in order_columns])
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(created_at_col_index, QHeaderView.ResizeMode.Fixed)
        table.horizontalHeader().setSectionResizeMode(close_time_col_index, QHeaderView.ResizeMode.Fixed)
        table.horizontalHeader().setSectionResizeMode(action_col_index, QHeaderView.ResizeMode.Fixed)
        for col_index, (field_name, _) in enumerate(order_columns):
            if field_name in order_fixed_column_widths:
                table.horizontalHeader().setSectionResizeMode(col_index, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(created_at_col_index, timestamp_column_width)
        table.setColumnWidth(close_time_col_index, timestamp_column_width)
        table.setColumnWidth(action_col_index, action_column_width)
        for col_index, (field_name, _) in enumerate(order_columns):
            fixed_width = order_fixed_column_widths.get(field_name)
            if fixed_width:
                table.setColumnWidth(col_index, fixed_width)
        order_tables[status_name] = table
        orders_tabs.addTab(table, status_name)

    orders_stylesheet = load_optional_stylesheet(ORDERS_MANAGEMENT_STYLE_PATH)
    if orders_stylesheet:
        orders_tab.setStyleSheet(orders_stylesheet)

    def apply_orders_column_visibility():
        hidden = not hide_time_columns_checkbox.isChecked()
        for status_name, table in order_tables.items():
            for col_index, (field_name, _) in enumerate(order_columns):
                if field_name == "created_at":
                    table.setColumnHidden(col_index, hidden)
                    if not hidden:
                        table.setColumnWidth(col_index, timestamp_column_width)
                elif field_name == "close_time":
                    close_time_hidden = hidden or status_name != "CLOSED"
                    table.setColumnHidden(col_index, close_time_hidden)
                    if not close_time_hidden:
                        table.setColumnWidth(col_index, timestamp_column_width)
                elif field_name == "z_score":
                    table.setColumnHidden(col_index, status_name != "PENDING")
                elif field_name in {"result", "max_return"}:
                    table.setColumnHidden(col_index, status_name == "PENDING")
            table.setColumnHidden(action_col_index, status_name == "CLOSED")

    def apply_orders_column_widths(status_name: str, table: QTableWidget, rows_count: int):
        cached_widths = orders_column_width_cache.get(status_name)
        if cached_widths:
            for col_index, width in enumerate(cached_widths):
                table.setColumnWidth(col_index, width)
            for col_index, (field_name, _) in enumerate(order_columns):
                fixed_width = order_fixed_column_widths.get(field_name)
                if fixed_width:
                    table.setColumnWidth(col_index, fixed_width)
            return

        table.setColumnWidth(created_at_col_index, timestamp_column_width)
        table.setColumnWidth(close_time_col_index, timestamp_column_width)
        table.setColumnWidth(action_col_index, action_column_width)
        for col_index, (field_name, _) in enumerate(order_columns):
            fixed_width = order_fixed_column_widths.get(field_name)
            if fixed_width:
                table.setColumnWidth(col_index, fixed_width)

        if rows_count <= 0:
            return

        table.resizeColumnsToContents()
        table.setColumnWidth(created_at_col_index, timestamp_column_width)
        table.setColumnWidth(close_time_col_index, timestamp_column_width)
        table.setColumnWidth(action_col_index, action_column_width)
        for col_index, (field_name, _) in enumerate(order_columns):
            fixed_width = order_fixed_column_widths.get(field_name)
            if fixed_width:
                table.setColumnWidth(col_index, fixed_width)
        orders_column_width_cache[status_name] = [
            table.columnWidth(col_index)
            for col_index in range(table.columnCount())
        ]

    def format_order_value(field_name: str, value):
        if value is None:
            return ""

        if field_name == "pair_name":
            pair_text = str(value).strip().upper()
            if "_" in pair_text:
                left, right = pair_text.split("_", 1)
                return f"{left} / {right}"
            return pair_text

        if field_name == "position_type":
            position_text = str(value).strip().upper()
            if position_text == "AUTO_MODE":
                return "Auto 🤖"
            if position_text == "LONG_SPREAD":
                return "Long 📈"
            if position_text == "SHORT_SPREAD":
                return "Short 📉"
            return str(value)

        if field_name in {"result", "max_return"}:
            try:
                number = float(value)
                if field_name == "result":
                    if number > 0:
                        return f"{number:.2f} 🟩"
                    if -1 < number < 0:
                        return f"{number:.2f} 🟨"
                    if number < -2:
                        return f"{number:.2f} 🟥"
                    if -2 <= number <= -1:
                        return f"{number:.2f} 🟧"
                return f"{number:.2f}"
            except (TypeError, ValueError):
                return str(value)

        if field_name == "z_score":
            try:
                return f"{float(value):.3f}"
            except (TypeError, ValueError):
                return str(value)

        if isinstance(value, float):
            if value.is_integer():
                return str(int(value))
            if field_name in {"beta", "two_sigma"}:
                return f"{value:.2f}"
            if field_name == "exposure_pct":
                return f"{value:.2f}"
            return f"{value:.4f}"

        return str(value)

    def run_manual_open(order_id: int):
        answer = QMessageBox.question(
            window,
            "Manual Open",
            f"Open the selected PENDING order now?\n\nID: {order_id}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        launch_task_console(
            os.path.join("Action", "Manual_Open.py"),
            ["--order-id", str(order_id)],
            title=f"Manual Open {order_id}",
        )
        statusbar.showMessage(
            f"Manual Open launched in console: id={order_id}. Refresh after the order completes.",
            8000,
        )
        QTimer.singleShot(5000, refresh_orders_tab)
        QTimer.singleShot(15000, refresh_orders_tab)

    def run_manual_close(pair_name: str):
        answer = QMessageBox.question(
            window,
            "Manual Close",
            f"Close the selected HOLDING pair now?\n\n{pair_name}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        launch_task_console(
            os.path.join("Action", "Manual_Close.py"),
            ["--pair-name", pair_name],
            title=f"Manual Close {pair_name}",
        )
        statusbar.showMessage(
            f"Manual Close launched in console: {pair_name}. Refresh after the order completes.",
            8000,
        )
        QTimer.singleShot(5000, refresh_orders_tab)
        QTimer.singleShot(15000, refresh_orders_tab)

    def delete_selected_pending_order():
        pending_table = order_tables.get("PENDING")
        if pending_table is None:
            return

        rows = pending_table.selectionModel().selectedRows()
        if not rows:
            return

        row_index = rows[0].row()
        id_item = pending_table.item(row_index, 0)
        pair_item = pending_table.item(row_index, 1)
        if id_item is None:
            return

        try:
            order_id = int(id_item.text().strip())
        except (TypeError, ValueError):
            return

        pair_label = pair_item.text().strip() if pair_item is not None else ""
        answer = QMessageBox.question(
            window,
            "Delete Pending Order",
            "Delete the selected PENDING record from scheduled_orders?\n\n"
            f"ID: {order_id}\n"
            f"Pair: {pair_label}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            result = delete_pending_order(order_id)
        except Exception as exc:
            QMessageBox.warning(window, "Delete Failed", str(exc))
            return

        if not result.get("success"):
            QMessageBox.warning(
                window,
                "Delete Failed",
                str(result.get("message") or f"Order id={order_id} could not be deleted"),
            )
            return

        refresh_orders_tab()
        statusbar.showMessage(
            f"Deleted pending order: id={order_id} {result.get('pair_name', '')}".strip(),
            6000,
        )

    pending_zscore_force_refresh = False

    def merge_pending_zscores(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
        pending_zscores = fetch_pending_order_zscores()
        merged_rows: List[Dict[str, object]] = []
        for row in rows:
            merged = dict(row)
            pair_key = normalize_pair_name(row.get("pair_name"))
            zscore_payload = pending_zscores.get(pair_key, {})
            merged["z_score"] = zscore_payload.get("zscore")
            merged["z_score_source"] = zscore_payload.get("zscore_source")
            merged["z_score_time"] = zscore_payload.get("zscore_time")
            merged_rows.append(merged)
        return merged_rows

    def clear_pending_zscores(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
        cleared_rows: List[Dict[str, object]] = []
        for row in rows:
            cleared = dict(row)
            cleared["z_score"] = None
            cleared["z_score_source"] = ""
            cleared["z_score_time"] = ""
            cleared_rows.append(cleared)
        return cleared_rows

    def zscore_color_brush(value, source: str):
        if value is None:
            return QBrush(QColor("#94a3b8"))

        if source != "live":
            return QBrush(QColor("#94a3b8"))

        z_abs = abs(float(value))
        if z_abs <= 0.5:
            return QBrush(QColor("#22c55e"))
        if z_abs < 1.9:
            return QBrush(QColor("#facc15"))
        if z_abs < 2.1:
            return QBrush(QColor("#ef4444"))
        return QBrush(QColor("#a855f7"))

    def apply_zscore_item(item: QTableWidgetItem, row: Dict[str, object]):
        z_value = row.get("z_score")
        z_source = str(row.get("z_score_source") or "")
        item.setText(format_order_value("z_score", z_value))
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setForeground(zscore_color_brush(z_value, z_source))
        z_time = str(row.get("z_score_time") or "").strip()
        if z_time:
            item.setToolTip(f"{z_source or 'unknown'} | {z_time}")
        else:
            item.setToolTip(z_source or "")

    def refresh_pending_zscore_cells():
        nonlocal pending_zscore_force_refresh

        if tabs.currentWidget() is not orders_tab:
            return
        if orders_tabs.currentWidget() is not order_tables["PENDING"]:
            return
        if not is_regular_us_market_open():
            # Preserve the last displayed values, but never make an expensive
            # app.py request after the regular US session closes.
            pending_zscore_force_refresh = False
            return

        zscore_map = fetch_pending_order_zscores(refresh=pending_zscore_force_refresh)
        pending_zscore_force_refresh = False
        pending_table = order_tables["PENDING"]
        if not zscore_map or pending_table.rowCount() == 0:
            return

        for row_index in range(pending_table.rowCount()):
            pair_item = pending_table.item(row_index, pair_name_col_index)
            if pair_item is None:
                continue

            pair_key = normalize_pair_name(pair_item.data(Qt.ItemDataRole.UserRole))
            if not pair_key:
                continue

            zscore_item = pending_table.item(row_index, z_score_col_index)
            if zscore_item is None:
                zscore_item = QTableWidgetItem("")
                pending_table.setItem(row_index, z_score_col_index, zscore_item)

            apply_zscore_item(
                zscore_item,
                {
                    "z_score": zscore_map.get(pair_key, {}).get("zscore"),
                    "z_score_source": zscore_map.get(pair_key, {}).get("zscore_source"),
                    "z_score_time": zscore_map.get(pair_key, {}).get("zscore_time"),
                },
            )

    def fill_orders_table(status_name: str, table: QTableWidget, rows: List[Dict[str, object]]):
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            table.setRowHeight(row_index, 42)
            for col_index, (field_name, _) in enumerate(order_columns):
                if field_name == "action":
                    action_button = None
                    if status_name == "PENDING":
                        order_id_value = int(row.get("id"))
                        action_button = QPushButton("Open 🔓")
                        action_button.clicked.connect(
                            lambda _checked=False, order_id=order_id_value: run_manual_open(order_id)
                        )
                    elif status_name == "HOLDING":
                        pair_name_value = str(row.get("pair_name") or "").strip().lower()
                        action_button = QPushButton("Close 🔒")
                        action_button.clicked.connect(
                            lambda _checked=False, pair_name=pair_name_value: run_manual_close(pair_name)
                        )

                    if action_button is not None:
                        action_button.setCursor(Qt.CursorShape.PointingHandCursor)
                        action_button.setFixedSize(action_button_width, 30)
                        action_button.setProperty("role", "orderAction")
                        shadow = QGraphicsDropShadowEffect(action_button)
                        shadow.setBlurRadius(14)
                        shadow.setOffset(0, 3)
                        shadow.setColor(QColor(15, 23, 42, 70))
                        action_button.setGraphicsEffect(shadow)
                        action_host = QWidget()
                        action_host.setObjectName("ordersActionHost")
                        action_layout = QHBoxLayout(action_host)
                        action_layout.setContentsMargins(6, 4, 6, 4)
                        action_layout.setSpacing(0)
                        action_layout.addStretch(1)
                        action_layout.addWidget(action_button)
                        action_layout.addStretch(1)
                        table.setCellWidget(row_index, col_index, action_host)
                    else:
                        table.setCellWidget(row_index, col_index, QWidget())
                    continue

                item = QTableWidgetItem(format_order_value(field_name, row.get(field_name)))
                if field_name == "pair_name":
                    item.setData(Qt.ItemDataRole.UserRole, normalize_pair_name(row.get("pair_name")))
                if col_index == 0 or field_name == "pair_name":
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                elif field_name == "z_score":
                    apply_zscore_item(item, row)
                elif field_name in {"beta", "two_sigma", "exposure_pct", "max_return"}:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignCenter
                    )
                elif field_name in {"created_at", "close_time"}:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignCenter
                    )
                elif field_name == "result":
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignCenter
                    )
                table.setItem(row_index, col_index, item)

        apply_orders_column_widths(status_name, table, len(rows))
        apply_orders_column_visibility()

    def refresh_orders_tab():
        nonlocal pending_zscore_force_refresh

        try:
            grouped_orders = load_orders_grouped(days=RECENT_DAYS)
        except Exception as exc:
            QMessageBox.warning(window, "Orders Load Failed", str(exc))
            return

        pending_zscore_force_refresh = True

        for status_name, table in order_tables.items():
            rows = grouped_orders.get(status_name, [])
            if status_name == "PENDING":
                rows = clear_pending_zscores(rows)
            fill_orders_table(status_name, table, rows)
            tab_index = orders_tabs.indexOf(table)
            orders_tabs.setTabText(tab_index, f"{status_name} ({len(rows)})")

    pending_delete_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), order_tables["PENDING"])
    pending_delete_shortcut.activated.connect(delete_selected_pending_order)

    def place_all_pending_orders_now():
        answer = QMessageBox.question(
            window,
            "Place All Pending",
            "This will try to place every PENDING order in scheduled_orders.\n\n"
            "It may send multiple live orders immediately.\n\n"
            "Are you sure you want to continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = manual_place_all_pending()
        finally:
            QApplication.restoreOverrideCursor()

        refresh_orders_tab()

        placed_count = int(result.get("placed_count", 0))
        failed_count = int(result.get("failed_count", 0))
        detail_lines = []
        for item in result.get("results", []):
            if not item.get("success"):
                detail_lines.append(
                    f"id={item.get('order_id')} {item.get('pair_name', '')}: {item.get('message', '')}"
                )
            if len(detail_lines) >= 5:
                break

        summary = f"Placed: {placed_count}\nFailed: {failed_count}"
        if detail_lines:
            summary += "\n\n" + "\n".join(detail_lines)

        if failed_count == 0:
            statusbar.showMessage(f"Placed all pending orders: {placed_count}", 7000)
            QMessageBox.information(window, "Place All Pending", summary)
            return

        QMessageBox.warning(window, "Place All Pending", summary)

    def toggle_orders_timestamp_columns():
        orders_column_width_cache.clear()
        apply_orders_column_visibility()

    refresh_orders_btn.clicked.connect(refresh_orders_tab)
    place_all_pending_btn.clicked.connect(place_all_pending_orders_now)
    hide_time_columns_checkbox.toggled.connect(toggle_orders_timestamp_columns)
    pending_zscore_timer = QTimer()
    pending_zscore_timer.timeout.connect(refresh_pending_zscore_cells)
    pending_zscore_timer.start(3000)
    tabs.addTab(orders_tab, "Pair Management")

    single_order_columns = [
        ("id", "ID"),
        ("single_name", "Stock"),
        ("position_type", "Type"),
        ("strategy", "Strategy"),
        ("param", "Param"),
        ("exposure_pct", "Exposure%"),
        ("result", "Result"),
        ("max_return", "Max Return"),
        ("created_at", "Created At"),
        ("close_time", "Close Time"),
        ("action", "Action"),
    ]
    single_action_col_index = next(
        i for i, (field_name, _) in enumerate(single_order_columns) if field_name == "action"
    )
    single_exposure_col_index = next(
        i for i, (field_name, _) in enumerate(single_order_columns) if field_name == "exposure_pct"
    )
    single_action_column_width = 330
    single_action_button_width = 94

    def single_action_width_for_status(status_name: str) -> int:
        return single_action_column_width if status_name == "HOLDING" else action_column_width

    single_created_at_col_index = next(
        i for i, (field_name, _) in enumerate(single_order_columns) if field_name == "created_at"
    )
    single_close_time_col_index = next(
        i for i, (field_name, _) in enumerate(single_order_columns) if field_name == "close_time"
    )
    single_width_source_fields = {
        "id": "id",
        "single_name": "pair_name",
        "position_type": "position_type",
        "param": "z_score",
        "exposure_pct": "exposure_pct",
        "result": "result",
        "max_return": "max_return",
        "created_at": "created_at",
        "close_time": "close_time",
    }
    single_orders_column_width_cache: Dict[str, List[int]] = {}

    single_orders_tab = QWidget()
    single_orders_tab.setObjectName("ordersManagementTab")
    single_orders_layout = QVBoxLayout(single_orders_tab)
    single_orders_layout.setContentsMargins(12, 12, 12, 12)
    single_orders_layout.setSpacing(10)

    single_orders_title = QLabel("Single Management")
    single_orders_title.setObjectName("ordersManagementTitle")
    single_orders_desc = QLabel(
        f"Shows all Pending/Holding single_scheduled_orders and Closed orders from the last {RECENT_DAYS} days. "
        "Pending and holding rows remain visible."
    )
    single_orders_desc.setWordWrap(True)
    single_orders_desc.setObjectName("ordersManagementDesc")
    single_orders_layout.addWidget(single_orders_title)
    single_orders_layout.addWidget(single_orders_desc)

    single_orders_button_row = QHBoxLayout()
    single_orders_button_row.setSpacing(8)
    refresh_single_orders_btn = QPushButton("Refresh")
    hide_single_time_columns_checkbox = QCheckBox("Show Timestamps")
    hide_single_time_columns_checkbox.setChecked(False)
    refresh_single_orders_btn.setProperty("role", "toolbarButton")
    hide_single_time_columns_checkbox.setProperty("role", "toolbarCheck")
    single_orders_hint = QLabel(
        "Pending rows show Open 🔓. Holding rows allow Exposure% edits, then Adjust or Reverse. "
        "Delete removes the selected Pending row."
    )
    single_orders_hint.setObjectName("ordersManagementHint")

    single_orders_button_row.addWidget(refresh_single_orders_btn)
    single_orders_button_row.addWidget(hide_single_time_columns_checkbox)
    single_orders_button_row.addStretch(1)
    single_orders_layout.addLayout(single_orders_button_row)
    single_orders_layout.addWidget(single_orders_hint)

    single_orders_tabs = QTabWidget()
    single_orders_tabs.setObjectName("ordersManagementTabs")
    single_orders_layout.addWidget(single_orders_tabs, 1)

    single_order_tables: Dict[str, QTableWidget] = {}
    for status_name in ["PENDING", "HOLDING", "CLOSED"]:
        table = QTableWidget(0, len(single_order_columns))
        table.setObjectName("ordersManagementTable")
        table.setHorizontalHeaderLabels([label for _, label in single_order_columns])
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(single_action_col_index, QHeaderView.ResizeMode.Fixed)
        table.horizontalHeader().setSectionResizeMode(single_created_at_col_index, QHeaderView.ResizeMode.Fixed)
        table.horizontalHeader().setSectionResizeMode(single_close_time_col_index, QHeaderView.ResizeMode.Fixed)
        for col_index, (field_name, _) in enumerate(single_order_columns):
            if field_name in single_width_source_fields:
                table.horizontalHeader().setSectionResizeMode(col_index, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(single_created_at_col_index, timestamp_column_width)
        table.setColumnWidth(single_close_time_col_index, timestamp_column_width)
        table.setColumnWidth(single_action_col_index, single_action_width_for_status(status_name))
        single_order_tables[status_name] = table
        single_orders_tabs.addTab(table, status_name)

    if orders_stylesheet:
        single_orders_tab.setStyleSheet(orders_stylesheet)

    def apply_single_orders_column_visibility():
        hidden = not hide_single_time_columns_checkbox.isChecked()
        for status_name, table in single_order_tables.items():
            for col_index, (field_name, _) in enumerate(single_order_columns):
                if field_name == "created_at":
                    table.setColumnHidden(col_index, hidden)
                    if not hidden:
                        table.setColumnWidth(col_index, timestamp_column_width)
                elif field_name == "close_time":
                    close_time_hidden = hidden or status_name != "CLOSED"
                    table.setColumnHidden(col_index, close_time_hidden)
                    if not close_time_hidden:
                        table.setColumnWidth(col_index, timestamp_column_width)
                elif field_name in {"result", "max_return"}:
                    table.setColumnHidden(col_index, status_name == "PENDING")
            table.setColumnHidden(single_action_col_index, status_name == "CLOSED")

    def apply_single_orders_matching_widths(table: QTableWidget):
        for col_index, (single_field, _) in enumerate(single_order_columns):
            order_field = single_width_source_fields.get(single_field)
            if not order_field:
                continue

            order_col_index = next(
                i for i, (field_name, _) in enumerate(order_columns) if field_name == order_field
            )
            width = order_fixed_column_widths.get(order_field) or max(
                (
                    order_table.columnWidth(order_col_index)
                    for order_table in order_tables.values()
                    if order_table is not None
                ),
                default=0,
            )
            if width > 0:
                table.setColumnWidth(col_index, width)

    def apply_single_orders_column_widths(status_name: str, table: QTableWidget, rows_count: int):
        cached_widths = single_orders_column_width_cache.get(status_name)
        if cached_widths:
            for col_index, width in enumerate(cached_widths):
                table.setColumnWidth(col_index, width)
            table.setColumnWidth(single_action_col_index, single_action_width_for_status(status_name))
            apply_single_orders_matching_widths(table)
            return

        table.setColumnWidth(single_created_at_col_index, timestamp_column_width)
        table.setColumnWidth(single_close_time_col_index, timestamp_column_width)
        table.setColumnWidth(single_action_col_index, single_action_width_for_status(status_name))
        apply_single_orders_matching_widths(table)

        if rows_count <= 0:
            return

        table.resizeColumnsToContents()
        table.setColumnWidth(single_created_at_col_index, timestamp_column_width)
        table.setColumnWidth(single_close_time_col_index, timestamp_column_width)
        table.setColumnWidth(single_action_col_index, single_action_width_for_status(status_name))
        apply_single_orders_matching_widths(table)
        single_orders_column_width_cache[status_name] = [
            table.columnWidth(col_index)
            for col_index in range(table.columnCount())
        ]

    def format_single_order_value(field_name: str, value):
        if value is None:
            return ""

        if field_name == "single_name":
            return str(value).strip().upper()

        if field_name == "position_type":
            position_text = str(value).strip().upper()
            if position_text == "AUTO_MODE":
                return "Auto 🤖"
            if position_text == "LONG_SPREAD":
                return "Long 📈"
            if position_text == "SHORT_SPREAD":
                return "Short 📉"
            return str(value)

        if field_name == "strategy":
            strategy_text = str(value).strip().upper()
            if strategy_text == "LOG_RESIDUAL":
                return "Log Residual"
            if strategy_text in {"NEW_HIGH", "NEW HIGH"}:
                return "New High"
            return str(value)

        if field_name in {"result", "max_return", "param", "exposure_pct"}:
            try:
                number = float(value)
                if field_name == "result":
                    if number > 0:
                        return f"{number:.2f} 🟩"
                    if -1 < number < 0:
                        return f"{number:.2f} 🟨"
                    if number < -2:
                        return f"{number:.2f} 🟥"
                    if -2 <= number <= -1:
                        return f"{number:.2f} 🟧"
                    return f"{number:.2f}"
                if field_name == "exposure_pct":
                    return f"{number:.2f}"
                return f"{number:.4f}" if field_name == "param" else f"{number:.2f}"
            except (TypeError, ValueError):
                return str(value)

        return str(value)

    def run_single_manual_open(order_id: int):
        answer = QMessageBox.question(
            window,
            "Single Place Order",
            f"Open the selected PENDING single order now?\n\nID: {order_id}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        launch_task_console(
            os.path.join("Action", "Manual_Single_Open.py"),
            ["--order-id", str(order_id)],
            title=f"Manual Single Open {order_id}",
        )
        statusbar.showMessage(
            f"Manual Single Open launched in console: id={order_id}. Refresh after the order completes.",
            8000,
        )
        QTimer.singleShot(5000, refresh_single_orders_tab)
        QTimer.singleShot(15000, refresh_single_orders_tab)

    def run_single_manual_close(order_id: int, single_name: str):
        answer = QMessageBox.question(
            window,
            "Single Close Order",
            f"Close the selected HOLDING single stock now?\n\nID: {order_id}\nStock: {single_name}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        launch_task_console(
            os.path.join("Action", "Manual_Single_Close.py"),
            ["--order-id", str(order_id)],
            title=f"Manual Single Close {single_name}",
        )
        statusbar.showMessage(
            f"Manual Single Close launched in console: {single_name}. Refresh after the order completes.",
            8000,
        )
        QTimer.singleShot(5000, refresh_single_orders_tab)
        QTimer.singleShot(15000, refresh_single_orders_tab)

    def read_single_holding_exposure_from_table(row_index: int):
        holding_table = single_order_tables.get("HOLDING")
        if holding_table is None:
            raise ValueError("HOLDING table is not available.")

        exposure_item = holding_table.item(row_index, single_exposure_col_index)
        if exposure_item is None:
            raise ValueError("Exposure% cell is missing.")

        exposure_text = str(exposure_item.text() or "").strip()
        try:
            exposure_pct = float(exposure_text)
        except (TypeError, ValueError):
            raise ValueError(f"Exposure% must be a number: {exposure_text}") from None

        if exposure_pct < 0.01 or exposure_pct > 100.0:
            raise ValueError("Exposure% must be between 0.01 and 100.00.")

        exposure_item.setText(f"{exposure_pct:.2f}")
        return exposure_pct

    def update_single_holding_exposure(order_id: int, exposure_pct: float) -> bool:
        conn = pymysql.connect(**MEMORY_DB_CONFIG)
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE single_scheduled_orders
                    SET exposure_pct = %s
                    WHERE id = %s AND status = 'HOLDING'
                    LIMIT 1
                    """,
                    (round(float(exposure_pct), 2), int(order_id)),
                )
                updated = cursor.rowcount > 0
                if not updated:
                    cursor.execute(
                        """
                        SELECT id
                        FROM single_scheduled_orders
                        WHERE id = %s AND status = 'HOLDING'
                        LIMIT 1
                        """,
                        (int(order_id),),
                    )
                    updated = cursor.fetchone() is not None
            conn.commit()
            return updated
        finally:
            conn.close()

    def save_single_holding_exposure_from_row(order_id: int, row_index: int):
        exposure_pct = read_single_holding_exposure_from_table(row_index)
        if not update_single_holding_exposure(order_id, exposure_pct):
            raise RuntimeError(f"HOLDING single order id={order_id} was not updated.")
        return exposure_pct

    def run_single_manual_adjust(order_id: int, single_name: str, row_index: int):
        try:
            exposure_pct = read_single_holding_exposure_from_table(row_index)
        except Exception as exc:
            QMessageBox.warning(window, "Invalid Exposure%", str(exc))
            return

        answer = QMessageBox.question(
            window,
            "Single Adjust Order",
            "Adjust the selected HOLDING single stock to the edited Exposure% now?\n\n"
            f"ID: {order_id}\n"
            f"Stock: {single_name}\n"
            f"Exposure%: {exposure_pct:.2f}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            save_single_holding_exposure_from_row(order_id, row_index)
        except Exception as exc:
            QMessageBox.warning(window, "Save Exposure Failed", str(exc))
            return

        launch_task_console(
            os.path.join("Action", "Manual_Single_Adjust.py"),
            ["--order-id", str(order_id)],
            title=f"Manual Single Adjust {single_name}",
        )
        statusbar.showMessage(
            f"Manual Single Adjust launched in console: {single_name}. Refresh after the order completes.",
            8000,
        )
        QTimer.singleShot(5000, refresh_single_orders_tab)
        QTimer.singleShot(15000, refresh_single_orders_tab)

    def run_single_manual_reverse(order_id: int, single_name: str, row_index: int):
        try:
            exposure_pct = read_single_holding_exposure_from_table(row_index)
        except Exception as exc:
            QMessageBox.warning(window, "Invalid Exposure%", str(exc))
            return

        answer = QMessageBox.question(
            window,
            "Single Reverse Order",
            "Reverse the selected HOLDING single stock now?\n\n"
            f"ID: {order_id}\n"
            f"Stock: {single_name}\n"
            f"Exposure%: {exposure_pct:.2f}\n\n"
            "This targets the opposite side with the edited Exposure%, keeps the row HOLDING, "
            "and adds the current return to result.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            save_single_holding_exposure_from_row(order_id, row_index)
        except Exception as exc:
            QMessageBox.warning(window, "Save Exposure Failed", str(exc))
            return

        launch_task_console(
            os.path.join("Action", "Manual_Single_Reverse.py"),
            ["--order-id", str(order_id)],
            title=f"Manual Single Reverse {single_name}",
        )
        statusbar.showMessage(
            f"Manual Single Reverse launched in console: {single_name}. Refresh after the order completes.",
            8000,
        )
        QTimer.singleShot(5000, refresh_single_orders_tab)
        QTimer.singleShot(15000, refresh_single_orders_tab)

    def delete_selected_single_pending_order():
        pending_table = single_order_tables.get("PENDING")
        if pending_table is None:
            return

        rows = pending_table.selectionModel().selectedRows()
        if not rows:
            return

        row_index = rows[0].row()
        id_item = pending_table.item(row_index, 0)
        single_item = pending_table.item(row_index, 1)
        if id_item is None:
            return

        try:
            order_id = int(id_item.text().strip())
        except (TypeError, ValueError):
            return

        single_label = single_item.text().strip() if single_item is not None else ""
        answer = QMessageBox.question(
            window,
            "Delete Pending Single Order",
            "Delete the selected PENDING record from single_scheduled_orders?\n\n"
            f"ID: {order_id}\n"
            f"Stock: {single_label}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            result = delete_single_pending_order(order_id)
        except Exception as exc:
            QMessageBox.warning(window, "Delete Failed", str(exc))
            return

        if not result.get("success"):
            QMessageBox.warning(
                window,
                "Delete Failed",
                str(result.get("message") or f"Single order id={order_id} could not be deleted"),
            )
            return

        refresh_single_orders_tab()
        statusbar.showMessage(
            f"Deleted pending single order: id={order_id} {result.get('single_name', '')}".strip(),
            6000,
        )

    def fill_single_orders_table(status_name: str, table: QTableWidget, rows: List[Dict[str, object]]):
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            table.setRowHeight(row_index, 42)
            for col_index, (field_name, _) in enumerate(single_order_columns):
                if field_name == "action":
                    action_buttons = []
                    if status_name == "PENDING":
                        order_id_value = int(row.get("id"))
                        action_button = QPushButton("Open 🔓")
                        action_button.clicked.connect(
                            lambda _checked=False, order_id=order_id_value: run_single_manual_open(order_id)
                        )
                        action_buttons.append(action_button)
                    elif status_name == "HOLDING":
                        order_id_value = int(row.get("id"))
                        single_name_value = str(row.get("single_name") or "").strip().upper()
                        close_button = QPushButton("Close 🔒")
                        close_button.clicked.connect(
                            lambda _checked=False, order_id=order_id_value, single_name=single_name_value: (
                                run_single_manual_close(order_id, single_name)
                            )
                        )
                        adjust_button = QPushButton("↕️ Adjust")
                        adjust_button.clicked.connect(
                            lambda _checked=False,
                            order_id=order_id_value,
                            single_name=single_name_value,
                            source_row=row_index: (
                                run_single_manual_adjust(order_id, single_name, source_row)
                            )
                        )
                        reverse_button = QPushButton("Reverse 🔁")
                        reverse_button.clicked.connect(
                            lambda _checked=False,
                            order_id=order_id_value,
                            single_name=single_name_value,
                            source_row=row_index: (
                                run_single_manual_reverse(order_id, single_name, source_row)
                            )
                        )
                        action_buttons.extend([close_button, adjust_button, reverse_button])

                    if action_buttons:
                        action_host = QWidget()
                        action_host.setObjectName("ordersActionHost")
                        action_layout = QHBoxLayout(action_host)
                        action_layout.setContentsMargins(6, 4, 6, 4)
                        action_layout.setSpacing(6)
                        action_layout.addStretch(1)
                        for action_button in action_buttons:
                            action_button.setCursor(Qt.CursorShape.PointingHandCursor)
                            button_width = (
                                single_action_button_width
                                if status_name == "HOLDING"
                                else action_button_width
                            )
                            action_button.setFixedSize(button_width, 30)
                            action_button.setProperty("role", "orderAction")
                            shadow = QGraphicsDropShadowEffect(action_button)
                            shadow.setBlurRadius(14)
                            shadow.setOffset(0, 3)
                            shadow.setColor(QColor(15, 23, 42, 70))
                            action_button.setGraphicsEffect(shadow)
                            action_layout.addWidget(action_button)
                        action_layout.addStretch(1)
                        table.setCellWidget(row_index, col_index, action_host)
                    else:
                        table.setCellWidget(row_index, col_index, QWidget())
                    continue

                item = QTableWidgetItem(format_single_order_value(field_name, row.get(field_name)))
                item_flags = item.flags()
                if status_name == "HOLDING" and field_name == "exposure_pct":
                    item.setFlags(item_flags | Qt.ItemFlag.ItemIsEditable)
                else:
                    item.setFlags(item_flags & ~Qt.ItemFlag.ItemIsEditable)
                if field_name == "single_name":
                    item.setData(Qt.ItemDataRole.UserRole, str(row.get("single_name") or "").strip().upper())
                if col_index == 0 or field_name == "single_name":
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                elif field_name in {"param", "exposure_pct", "result", "max_return"}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                elif field_name in {"created_at", "close_time"}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                table.setItem(row_index, col_index, item)

        apply_single_orders_column_widths(status_name, table, len(rows))
        apply_single_orders_column_visibility()

    def refresh_single_orders_tab():
        try:
            grouped_orders = load_single_orders_grouped(days=RECENT_DAYS)
        except Exception as exc:
            QMessageBox.warning(window, "Single Orders Load Failed", str(exc))
            return

        for status_name, table in single_order_tables.items():
            rows = grouped_orders.get(status_name, [])
            fill_single_orders_table(status_name, table, rows)
            tab_index = single_orders_tabs.indexOf(table)
            single_orders_tabs.setTabText(tab_index, f"{status_name} ({len(rows)})")

    single_pending_delete_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), single_order_tables["PENDING"])
    single_pending_delete_shortcut.activated.connect(delete_selected_single_pending_order)
    def toggle_single_orders_timestamp_columns():
        single_orders_column_width_cache.clear()
        apply_single_orders_column_visibility()

    refresh_single_orders_btn.clicked.connect(refresh_single_orders_tab)
    hide_single_time_columns_checkbox.toggled.connect(toggle_single_orders_timestamp_columns)
    tabs.addTab(single_orders_tab, "Single Management")

    refresh_orders_tab()
    refresh_single_orders_tab()

    management_tabs_loaded = {
        "subscription": False,
        "orders": True,
        "single_orders": True,
    }

    def load_management_tab_if_needed(force: bool = False):
        current = tabs.currentWidget()
        if current is subscription_tab and (force or not management_tabs_loaded["subscription"]):
            refresh_subscription_tab()
            management_tabs_loaded["subscription"] = True
        elif current is orders_tab and (force or not management_tabs_loaded["orders"]):
            refresh_orders_tab()
            management_tabs_loaded["orders"] = True
        elif current is single_orders_tab and (force or not management_tabs_loaded["single_orders"]):
            refresh_single_orders_tab()
            management_tabs_loaded["single_orders"] = True

    tabs.currentChanged.connect(lambda _index: load_management_tab_if_needed())

    waiting_entry_tab = QWidget()
    waiting_entry_tab.setObjectName("ordersManagementTab")
    waiting_entry_layout = QVBoxLayout(waiting_entry_tab)
    waiting_entry_layout.setContentsMargins(12, 12, 12, 12)
    waiting_entry_layout.setSpacing(10)

    waiting_entry_title = QLabel("Waiting Entry")
    waiting_entry_title.setObjectName("ordersManagementTitle")
    waiting_entry_desc = QLabel(
        "Delete removes the selected row. Changing 1 or 2 to 0 marks it Ready and asks for confirmation once. "
        "Ready means you have checked this pair in quant_dashboard recently enough that beta and two-sigma are usable "
        "for an auto-created PENDING order."
    )
    waiting_entry_desc.setWordWrap(True)
    waiting_entry_desc.setObjectName("ordersManagementDesc")
    waiting_entry_layout.addWidget(waiting_entry_title)
    waiting_entry_layout.addWidget(waiting_entry_desc)

    waiting_entry_button_row = QHBoxLayout()
    waiting_entry_button_row.setSpacing(8)
    refresh_waiting_entry_btn = QPushButton("Refresh")
    refresh_waiting_entry_btn.setProperty("role", "toolbarButton")
    waiting_entry_button_row.addWidget(refresh_waiting_entry_btn)
    waiting_entry_button_row.addStretch(1)
    waiting_entry_layout.addLayout(waiting_entry_button_row)

    waiting_entry_lock_labels = {
        0: "0 | Ready",
        1: "1 | Locked",
        2: "2 | Watching",
    }

    def create_waiting_entry_table() -> QTableWidget:
        table = QTableWidget(0, 5)
        table.setObjectName("ordersManagementTable")
        table.setProperty("waitingEntryCombined", "true")
        table.setHorizontalHeaderLabels(["Pair", "Lock", "Auto", "Exposure%", "Order Mode"])
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(False)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(1, 170)
        table.setColumnWidth(2, 90)
        table.setColumnWidth(3, 110)
        table.setColumnWidth(4, 130)
        return table

    waiting_entry_table = create_waiting_entry_table()
    waiting_entry_layout.addWidget(waiting_entry_table, 1)

    if orders_stylesheet:
        waiting_entry_tab.setStyleSheet(orders_stylesheet)

    def add_waiting_entry_lock_options(combo: QComboBox, current_lock: int):
        if current_lock == 0:
            options = [0, 2]
        elif current_lock == 1:
            options = [1, 0, 2]
        elif current_lock == 2:
            options = [2, 0]
        else:
            options = [current_lock, 0, 2]

        seen = set()
        for value in options:
            if value in seen:
                continue
            label = waiting_entry_lock_labels.get(value, f"{value} | Current")
            combo.addItem(label, value)
            seen.add(value)

    def fill_waiting_entry_table(table: QTableWidget, rows: List[Dict[str, object]]):
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            table.setRowHeight(row_index, 40)

            pair_name = normalize_pair_name(row.get("pair_name"))
            lock_value = int(row.get("reentry_lock") or 0)
            auto_enabled = int(row.get("auto_schedule_enabled") or 0)
            exposure_pct = row.get("exposure_pct")
            order_mode = str(row.get("order_position_type") or "").strip().upper()

            pair_item = QTableWidgetItem(format_pair_display(pair_name))
            pair_item.setData(Qt.ItemDataRole.UserRole, pair_name)
            pair_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(row_index, 0, pair_item)

            lock_combo = QComboBox()
            add_waiting_entry_lock_options(lock_combo, lock_value)
            current_index = lock_combo.findData(lock_value)
            if current_index >= 0:
                lock_combo.setCurrentIndex(current_index)
            lock_combo.setProperty("pair_name", pair_name)
            lock_combo.setProperty("current_lock", lock_value)
            lock_combo.setProperty("waitingEntryLock", str(lock_value))
            lock_combo.activated.connect(
                lambda _index, combo=lock_combo: apply_waiting_entry_lock_change(combo)
            )
            table.setCellWidget(row_index, 1, lock_combo)

            auto_item = QTableWidgetItem("Yes" if auto_enabled else "No")
            auto_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(row_index, 2, auto_item)

            try:
                exposure_text = "" if exposure_pct is None else f"{float(exposure_pct):.2f}"
            except (TypeError, ValueError):
                exposure_text = str(exposure_pct or "")
            exposure_item = QTableWidgetItem(exposure_text)
            exposure_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(row_index, 3, exposure_item)

            mode_item = QTableWidgetItem(order_mode or "")
            mode_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(row_index, 4, mode_item)

        table.setColumnWidth(1, 170)
        table.setColumnWidth(2, 90)
        table.setColumnWidth(3, 110)
        table.setColumnWidth(4, 130)

    def waiting_entry_lock_sort_value(row: Dict[str, object]) -> int:
        try:
            lock_value = int(row.get("reentry_lock") or 0)
        except (TypeError, ValueError):
            return 2
        return lock_value if lock_value in {0, 1, 2} else 2

    def refresh_waiting_entry_tab():
        try:
            rows = load_pair_reentry_rows()
        except Exception as exc:
            QMessageBox.warning(window, "Waiting Entry Load Failed", str(exc))
            return

        rows.sort(
            key=lambda row: (
                waiting_entry_lock_sort_value(row),
                normalize_pair_name(row.get("pair_name")),
            )
        )
        fill_waiting_entry_table(waiting_entry_table, rows)

    def apply_waiting_entry_lock_change(combo: QComboBox):
        pair_name = normalize_pair_name(combo.property("pair_name"))
        previous_lock = int(combo.property("current_lock") or 0)
        next_lock = int(combo.currentData() or 0)

        if not pair_name or next_lock == previous_lock:
            return

        if previous_lock in {1, 2} and next_lock == 0:
            answer = QMessageBox.question(
                window,
                "Confirm Reentry Unlock",
                (
                    f"Move {format_pair_display(pair_name)} from "
                    f"{waiting_entry_lock_labels.get(previous_lock, str(previous_lock))} to 0 | Ready?\n\n"
                    "Ready should mean you have recently reviewed this pair in quant_dashboard, "
                    "and its beta / two-sigma parameters are acceptable for automatic PENDING order creation."
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                refresh_waiting_entry_tab()
                return

        try:
            updated = update_pair_reentry_lock(pair_name, next_lock)
        except Exception as exc:
            QMessageBox.warning(window, "Waiting Entry Update Failed", str(exc))
            refresh_waiting_entry_tab()
            return

        if not updated:
            QMessageBox.warning(
                window,
                "Waiting Entry Update Failed",
                f"Could not update pair_reentry for {format_pair_display(pair_name)}.",
            )
            refresh_waiting_entry_tab()
            return

        statusbar.showMessage(
            f"Waiting Entry updated: {format_pair_display(pair_name)} -> lock {next_lock}",
            5000,
        )
        refresh_waiting_entry_tab()

    def delete_waiting_entry_row(pair_name: str, current_lock: int):
        pair_name = normalize_pair_name(pair_name)
        if not pair_name:
            return

        answer = QMessageBox.question(
            window,
            "Delete Waiting Entry",
            "Delete the selected pair from pair_reentry?\n\n"
            f"Pair: {format_pair_display(pair_name)}\n"
            f"Lock: {waiting_entry_lock_labels.get(current_lock, str(current_lock))}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            deleted = delete_pair_reentry_row(pair_name)
        except Exception as exc:
            QMessageBox.warning(window, "Delete Waiting Entry Failed", str(exc))
            return

        if not deleted:
            QMessageBox.warning(
                window,
                "Delete Waiting Entry Failed",
                f"Could not delete {format_pair_display(pair_name)} from pair_reentry.",
            )
            return

        statusbar.showMessage(
            f"Deleted Waiting Entry: {format_pair_display(pair_name)}",
            5000,
        )
        refresh_waiting_entry_tab()

    def delete_selected_waiting_entry_row(table: QTableWidget):
        rows = table.selectionModel().selectedRows()
        if not rows:
            return

        row_index = rows[0].row()
        pair_item = table.item(row_index, 0)
        lock_combo = table.cellWidget(row_index, 1)
        if pair_item is None:
            return

        pair_name = normalize_pair_name(pair_item.data(Qt.ItemDataRole.UserRole))
        current_lock = 0
        if isinstance(lock_combo, QComboBox):
            current_lock = int(lock_combo.property("current_lock") or lock_combo.currentData() or 0)

        delete_waiting_entry_row(pair_name, current_lock)

    def delete_active_waiting_entry_row():
        if tabs.currentWidget() is not waiting_entry_tab:
            return

        focus_widget = QApplication.focusWidget()
        if isinstance(focus_widget, QComboBox):
            pair_name = normalize_pair_name(focus_widget.property("pair_name"))
            if pair_name:
                current_lock = int(
                    focus_widget.property("current_lock") or focus_widget.currentData() or 0
                )
                delete_waiting_entry_row(pair_name, current_lock)
                return

        rows = waiting_entry_table.selectionModel().selectedRows()
        if rows:
            delete_selected_waiting_entry_row(waiting_entry_table)
            return

    refresh_waiting_entry_btn.clicked.connect(refresh_waiting_entry_tab)
    waiting_entry_delete_shortcuts = []
    delete_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), waiting_entry_tab)
    delete_shortcut.activated.connect(delete_active_waiting_entry_row)
    waiting_entry_delete_shortcuts.append(delete_shortcut)
    refresh_waiting_entry_tab()
    tabs.addTab(waiting_entry_tab, "Waiting Entry")

    tabs.addTab(stop_loss_tab, "Exit Rules")

    def refresh_dashboard():
        processes = query_processes()
        api_status = "online" if backend_alive() else "offline"
        for key in SERVICE_ORDER:
            spec = SERVICES[key]
            matches = match_service_processes(spec, processes)
            running = len(matches) > 0
            hint = f"pid={matches[0]['pid']}" if running else ""
            cards[key].set_running(running, hint)
        statusbar.showMessage(f"API {api_status}: {API_BASE}")

    timer = QTimer()
    timer.timeout.connect(refresh_dashboard)
    timer.start(5000)

    def boot_default_services_after_show():
        statusbar.showMessage("Starting default services in background...")
        ensure_default_services()
        statusbar.showMessage("Default services requested. Waiting for dashboards to come online...", 6000)
        QTimer.singleShot(1500, refresh_dashboard)
        QTimer.singleShot(3500, lambda: load_web_tab_if_needed(force=True))
        QTimer.singleShot(6500, lambda: load_web_tab_if_needed(force=True))
        QTimer.singleShot(8000, refresh_dashboard)

    window.show()
    QTimer.singleShot(150, boot_default_services_after_show)
    QTimer.singleShot(250, load_web_tab_if_needed)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
