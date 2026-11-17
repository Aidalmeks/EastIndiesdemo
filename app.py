import gzip
import hashlib
import json
import os
import threading
import time
import warnings
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pymysql
import redis
from flask import Flask, jsonify, request, send_from_directory
from program_factory.live_bars import BAR_CHANNEL
from runtime_settings import database_config, redis_config

warnings.filterwarnings("ignore", category=UserWarning)

app = Flask(__name__)


@app.after_request
def attach_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    accept_encoding = request.headers.get("Accept-Encoding", "")
    content_type = resp.headers.get("Content-Type", "")
    should_compress = (
        "gzip" in accept_encoding.lower()
        and resp.status_code == 200
        and not resp.direct_passthrough
        and "Content-Encoding" not in resp.headers
        and any(
            content_type.startswith(prefix)
            for prefix in ("application/json", "application/javascript", "text/html", "text/css")
        )
    )
    if should_compress:
        data = resp.get_data()
        if len(data) > 1024:
            resp.set_data(gzip.compress(data))
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers["Content-Length"] = str(len(resp.get_data()))
            resp.headers["Vary"] = "Accept-Encoding"
    return resp


DB_CONFIG = database_config("data")
MEMORY_DB_CONFIG = database_config("memory")
REDIS_CLIENT = redis.Redis(**redis_config())
LIVE_PRICE_TTL_SECONDS = 10.0
PAIR_CACHE_AUTO_REFRESH_SECONDS = 60.0
DEFAULT_TRIGGER_Z = 2.0
WAITING_ENTRY_SNAPSHOT_KEY = "dashboard:waiting_entry"
WAITING_ENTRY_SNAPSHOT_MAX_AGE_SECONDS = 15.0
# When the live waiting-entry publisher is stopped, a dashboard request used to
# rebuild every pending signal from MySQL every three seconds. Keep one
# after-hours fallback snapshot instead: prices and signals are not expected to
# change outside the regular session.
WAITING_ENTRY_AFTER_HOURS_CACHE = None
WAITING_ENTRY_AFTER_HOURS_CACHE_LOCK = threading.Lock()
FLAGSHIP_SNAPSHOT_KEY = "dashboard:flagship_board"
FLAGSHIP_SNAPSHOT_MAX_AGE_SECONDS = 15.0
FIXED_BETA_ROLLING_SNAPSHOT_PREFIX = "fixed_beta:rolling:"
FIXED_BETA_ROLLING_PUBLISH_RETRY_SECONDS = 10.0
FIXED_BETA_ROLLING_PUBLISH_MAX_RETRIES = 12
PROJECT_DIR = os.path.abspath(os.path.dirname(__file__))
PUBLIC_ASSETS = {
    "bank.ico": PROJECT_DIR,
    "vue_quant_demo.css": PROJECT_DIR,
    "components/vendor/vue.global.prod.js": os.path.join(PROJECT_DIR, "components", "vendor"),
    "components/draggable_train_chart/plotly.min.js": os.path.join(
        PROJECT_DIR, "components", "draggable_train_chart"
    ),
}
CALENDAR_CACHE_FILE = os.path.join(PROJECT_DIR, "logs", "ib_us_market_calendar.json")
FLAGSHIP_HIDDEN_FILE = os.path.join(PROJECT_DIR, "logs", "flagship_hidden_positions.json")
IB_CALENDAR_SYMBOL = "SPY"
DISPLAY_TZ = ZoneInfo("Asia/Shanghai")
MARKET_TZ = ZoneInfo("America/New_York")
MARKET_CALENDAR_WINDOW_DAYS = 7


def safe_float(value):
    try:
        if value is None:
            return None
        value = float(value)
        return value if np.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def load_flagship_hidden_keys():
    try:
        with open(FLAGSHIP_HIDDEN_FILE, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()

    if isinstance(payload, list):
        return {str(item).strip().upper() for item in payload if str(item).strip()}
    if isinstance(payload, dict):
        keys = payload.get("keys") if isinstance(payload.get("keys"), list) else []
        return {str(item).strip().upper() for item in keys if str(item).strip()}
    return set()


def save_flagship_hidden_keys(keys):
    os.makedirs(os.path.dirname(FLAGSHIP_HIDDEN_FILE), exist_ok=True)
    payload = {
        "keys": sorted({str(item).strip().upper() for item in keys if str(item).strip()}),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp_path = FLAGSHIP_HIDDEN_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp_path, FLAGSHIP_HIDDEN_FILE)


def flagship_hidden_key(item):
    kind = str(item.get("kind") or "").strip().lower()
    if kind == "pair":
        name = str(item.get("name") or "").strip().upper().replace(" / ", "_")
        return f"PAIR:{name}"
    symbol = str(item.get("symbol") or item.get("name") or "").strip().upper()
    return f"SINGLE:{symbol}"


def annotate_flagship_hidden_items(snapshot):
    hidden_keys = load_flagship_hidden_keys()
    items = []
    hidden_count = 0
    for source_item in snapshot.get("items") or []:
        item = dict(source_item)
        key = flagship_hidden_key(item)
        hidden = key in hidden_keys
        item["hidden_key"] = key
        item["hidden"] = hidden
        if hidden:
            hidden_count += 1
        items.append(item)

    snapshot = dict(snapshot)
    snapshot["items"] = items
    snapshot["count"] = len(items)
    snapshot["hidden_count"] = hidden_count
    snapshot["visible_count"] = len(items) - hidden_count
    return snapshot


def normalize_threshold(value):
    threshold = safe_float(value)
    return threshold if threshold is not None and threshold > 0 else DEFAULT_TRIGGER_Z


def redis_tick_price(ticker):
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        return None, None
    price = safe_float(REDIS_CLIENT.get(f"price:{symbol}"))
    ts = safe_float(REDIS_CLIENT.get(f"ts:{symbol}"))
    return price, ts


def format_bj_timestamp(value):
    if value is None or pd.isna(value):
        return None
    try:
        return (pd.Timestamp(value) + pd.Timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def load_waiting_entry_snapshot_from_redis():
    try:
        raw = REDIS_CLIENT.get(WAITING_ENTRY_SNAPSHOT_KEY)
    except Exception:
        return None
    if not raw:
        return None

    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None

    updated_ts = safe_float(payload.get("updated_ts"))
    if updated_ts is None:
        return None

    age = time.time() - updated_ts
    if age > WAITING_ENTRY_SNAPSHOT_MAX_AGE_SECONDS:
        return None

    payload["snapshot_age_seconds"] = round(age, 3)
    payload["source"] = payload.get("source") or "waiting_entry"
    return payload


def load_json_snapshot_from_redis(key, max_age_seconds, source_name):
    try:
        raw = REDIS_CLIENT.get(key)
    except Exception:
        return None
    if not raw:
        return None

    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None

    updated_ts = safe_float(payload.get("updated_ts"))
    if updated_ts is None:
        return None

    age = time.time() - updated_ts
    if age > max_age_seconds:
        return None

    payload["snapshot_age_seconds"] = round(age, 3)
    payload["source"] = payload.get("source") or source_name
    return payload


def load_flagship_snapshot_from_redis():
    try:
        raw = REDIS_CLIENT.get(FLAGSHIP_SNAPSHOT_KEY)
    except redis.exceptions.RedisError:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None

    updated_ts = safe_float(payload.get("updated_ts"))
    age = None if updated_ts is None else max(0.0, time.time() - updated_ts)
    payload["snapshot_age_seconds"] = None if age is None else round(age, 3)
    payload["stale"] = age is None or age > FLAGSHIP_SNAPSHOT_MAX_AGE_SECONDS
    payload["source"] = "flagship_board"
    return payload


def normalize_calendar_session(row):
    try:
        open_bj = row["open_bj"]
        close_bj = row["close_bj"]
        open_ny = row["open_ny"]
        close_ny = row["close_ny"]
    except KeyError:
        return None

    return {
        "date_ny": str(open_ny)[:10],
        "open_ny": str(open_ny),
        "close_ny": str(close_ny),
        "open_bj": str(open_bj),
        "close_bj": str(close_bj),
        "open_bj_text": str(open_bj).replace("T", " ")[:16],
        "close_bj_text": str(close_bj).replace("T", " ")[:16],
        "open_ny_text": str(open_ny).replace("T", " ")[:16],
        "close_ny_text": str(close_ny).replace("T", " ")[:16],
    }


def load_market_calendar_cache():
    try:
        with open(CALENDAR_CACHE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    now_bj = datetime.now(DISPLAY_TZ)
    now_market = now_bj.astimezone(MARKET_TZ)
    last_market_date = now_market.date() + timedelta(days=MARKET_CALENDAR_WINDOW_DAYS - 1)
    sessions = []
    for row in payload.get("sessions", []):
        item = normalize_calendar_session(row)
        if not item:
            continue
        try:
            close_bj = datetime.fromisoformat(item["close_bj"])
            open_ny = datetime.fromisoformat(item["open_ny"])
        except ValueError:
            continue
        if close_bj > now_bj and open_ny.date() <= last_market_date:
            sessions.append(item)

    return {
        "ok": True,
        "source": "cache",
        "symbol": payload.get("symbol") or IB_CALENDAR_SYMBOL,
        "saved_at_bj": payload.get("saved_at_bj"),
        "sessions": sessions,
        "session_count": len(sessions),
        "closed_days": payload.get("closed_days", []),
        "raw_part_count": payload.get("raw_part_count"),
        "first_date": payload.get("first_date"),
        "last_date": payload.get("last_date"),
        "window_days": MARKET_CALENDAR_WINDOW_DAYS,
    }


def load_market_calendar():
    cached = load_market_calendar_cache()
    if cached:
        return cached
    return {
        "ok": False,
        "source": "unavailable",
        "symbol": IB_CALENDAR_SYMBOL,
        "error": f"Calendar cache not found: {CALENDAR_CACHE_FILE}",
        "sessions": [],
        "session_count": 0,
        "closed_days": [],
    }


def is_regular_market_session_open():
    """Return whether ``now`` is inside a cached US regular-session window.

    A missing or invalid calendar deliberately returns False. That conservative
    choice prevents an unavailable calendar from enabling expensive after-hours
    fallback calculations.
    """
    calendar = load_market_calendar_cache()
    if not calendar:
        return False

    now_bj = datetime.now(DISPLAY_TZ)
    for session in calendar.get("sessions", []):
        try:
            open_bj = datetime.fromisoformat(session["open_bj"])
            close_bj = datetime.fromisoformat(session["close_bj"])
        except (KeyError, TypeError, ValueError):
            continue
        if open_bj <= now_bj < close_bj:
            return True
    return False


def latest_closed_market_session_key():
    """Return the latest cached US regular-session close as an after-hours key.

    The key deliberately represents a completed session rather than the
    process lifetime.  That lets an after-hours fallback be calculated once
    per close, even when nobody visits the dashboard during the next RTH.
    """
    try:
        with open(CALENDAR_CACHE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        payload = {}

    now_bj = datetime.now(DISPLAY_TZ)
    latest_close = None
    for row in payload.get("sessions", []):
        item = normalize_calendar_session(row)
        if not item:
            continue
        try:
            close_bj = datetime.fromisoformat(item["close_bj"])
        except (KeyError, TypeError, ValueError):
            continue
        if close_bj <= now_bj and (latest_close is None or close_bj > latest_close):
            latest_close = close_bj

    if latest_close is not None:
        return latest_close.isoformat()

    # A missing calendar must not make an old cache immortal.  The market-date
    # fallback is less precise around a holiday, but it still rolls forward.
    return f"market-date:{now_bj.astimezone(MARKET_TZ).date().isoformat()}"


def waiting_entry_rows_fingerprint(rows):
    """Hash the monitor configuration without recalculating any signals."""
    encoded = json.dumps(rows, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_pairs_from_db():
    """Load production pairs with Reentry taking precedence over config fallback.

    A Reentry row is the authoritative monitor registration and supplies its
    own train start, split time, and fixed beta in ``param``.  Active orders
    without such a registration remain visible, using pair_config only as the
    temporary calculation fallback.
    """
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        reentry_rows = pd.read_sql(
            """
            SELECT pair_name, train_start, split_time, param
            FROM pair_reentry
            WHERE pair_name IS NOT NULL AND pair_name <> ''
            """,
            conn,
        )
        active_order_rows = pd.read_sql(
            """
            SELECT DISTINCT pair_name
            FROM scheduled_orders
            WHERE status IN ('PENDING', 'HOLDING')
              AND pair_name IS NOT NULL AND pair_name <> ''
            """,
            conn,
        )
        config_rows = pd.read_sql(
            """
            SELECT pair_name, train_start_bj, test_end_bj, fixed_beta
            FROM pair_config
            WHERE data_source = '5m'
            """,
            conn,
        )
    finally:
        conn.close()

    def pair_key(value):
        key = str(value or "").strip().lower()
        return key if "_" in key else None

    def timestamp_or_none(value):
        return None if pd.isna(value) else pd.Timestamp(value)

    pairs = {}
    for _, row in reentry_rows.iterrows():
        key = pair_key(row["pair_name"])
        if key is None:
            continue
        beta = safe_float(row["param"])
        train_start = timestamp_or_none(row["train_start"])
        split_time = timestamp_or_none(row["split_time"])
        if beta is None or train_start is None:
            # Do not silently substitute pair_config for a registered pair:
            # a partially saved Reentry record needs correcting at its source.
            continue
        pairs[key] = {
            "train_start": train_start,
            "split": split_time,
            "test_end": split_time,
            "fixed_beta": beta,
            "source": "Pair Reentry",
        }

    config_by_pair = {}
    for _, row in config_rows.iterrows():
        key = pair_key(row["pair_name"])
        beta = safe_float(row["fixed_beta"])
        train_start = timestamp_or_none(row["train_start_bj"])
        if key is not None and beta is not None and train_start is not None:
            config_by_pair[key] = {
                "train_start": train_start,
                "split": timestamp_or_none(row["test_end_bj"]),
                "test_end": timestamp_or_none(row["test_end_bj"]),
                "fixed_beta": beta,
                "source": "Pair Config",
            }

    for _, row in active_order_rows.iterrows():
        key = pair_key(row["pair_name"])
        if key is not None and key not in pairs and key in config_by_pair:
            pairs[key] = config_by_pair[key]
    return pairs


PAIRS = load_pairs_from_db()
SINGLES = {}
PAIRS_LOADED_AT = time.time()
PAIR_PAYLOAD_CACHE = {}
SINGLE_PAYLOAD_CACHE = {}
PRICE_LOOKUP_INDEX = "idx_us_market_prices_ticker_time_price"
DETAIL_PAYLOAD_CACHE_SECONDS = 300.0
PAIR_CONFIG_TABLE = "pair_config"
FIXED_BETA_SOURCE_5M = "5m"
ROLLING_DAYS = 20
BARS_PER_TRADING_DAY = 79
KAPPA_BARS_PER_TRADING_DAY = 77
KAPPA_NEUTRAL_BOUNDARY = 0.25
KAPPA_MIN_EFFECTIVE_TRANSITIONS = 150
ROLLING_WARMUP_CALENDAR_DAYS = 90


def kappa_daily_equivalent(kappa_per_bar):
    """Convert the per-5-minute-bar kappa estimate to a trading-day equivalent.

    The estimator itself remains per bar for all decision-layer consumers.  This
    conversion is only a presentation/comparison unit, matching Batteries Lab.
    """
    kappa_per_bar = safe_float(kappa_per_bar)
    if kappa_per_bar is None or kappa_per_bar <= -1.0:
        return None
    return float(np.expm1(KAPPA_BARS_PER_TRADING_DAY * np.log1p(kappa_per_bar)))


def load_singles_from_db():
    def normalize_single_name(value):
        return str(value or "").strip().upper()

    def normalize_dt(value):
        if pd.isna(value):
            return None
        return str(value)

    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        sql = """
        SELECT
            sso.id,
            sso.single_name,
            sso.train_start,
            sso.split_time,
            sso.strategy,
            sso.param,
            sso.position_type,
            sso.exposure_pct,
            sso.status,
            sso.created_at
        FROM single_scheduled_orders sso
        JOIN (
            SELECT single_name, MAX(id) AS max_id
            FROM single_scheduled_orders
            WHERE status IN ('PENDING', 'HOLDING')
              AND single_name IS NOT NULL
              AND single_name <> ''
            GROUP BY single_name
        ) latest ON latest.max_id = sso.id
        WHERE sso.status IN ('PENDING', 'HOLDING')
        ORDER BY sso.id DESC
        """
        df = pd.read_sql(sql, conn)

        sql_reentry = """
        SELECT
            pair_name AS single_name,
            train_start,
            split_time,
            strategy,
            param,
            order_position_type,
            exposure_pct,
            COALESCE(reentry_lock, 0) AS reentry_lock
        FROM pair_reentry
        WHERE pair_name IS NOT NULL
          AND pair_name <> ''
          AND pair_name NOT LIKE '%\\_%'
          AND strategy IS NOT NULL
        """
        reentry_df = pd.read_sql(sql_reentry, conn)
    finally:
        conn.close()

    singles = {}
    for _, row in df.iterrows():
        single_name = normalize_single_name(row["single_name"])
        if not single_name:
            continue
        singles[single_name] = {
            "id": int(row["id"]),
            "single_name": single_name,
            "train_start": normalize_dt(row["train_start"]),
            "split": normalize_dt(row["split_time"]),
            "strategy": None if pd.isna(row["strategy"]) else str(row["strategy"]),
            "param": None if pd.isna(row["param"]) else float(row["param"]),
            "position_type": None if pd.isna(row["position_type"]) else str(row["position_type"]),
            "exposure_pct": None if pd.isna(row["exposure_pct"]) else float(row["exposure_pct"]),
            "status": None if pd.isna(row["status"]) else str(row["status"]),
            "source": f"SINGLE_{str(row['status']).upper()}",
        }

    for _, row in reentry_df.iterrows():
        single_name = normalize_single_name(row["single_name"])
        if not single_name:
            continue

        existing = singles.get(single_name, {})
        existing_source = str(existing.get("source") or "")
        source = "SINGLE_REENTRY"
        if existing_source and existing_source not in source.split("+"):
            source = f"{source}+{existing_source}"

        singles[single_name] = {
            "id": existing.get("id"),
            "single_name": single_name,
            "train_start": normalize_dt(row["train_start"]) or existing.get("train_start"),
            "split": normalize_dt(row["split_time"]),
            "strategy": existing.get("strategy") if pd.isna(row["strategy"]) else str(row["strategy"]),
            "param": existing.get("param") if pd.isna(row["param"]) else float(row["param"]),
            "position_type": existing.get("position_type") if pd.isna(row["order_position_type"]) else str(row["order_position_type"]),
            "exposure_pct": existing.get("exposure_pct") if pd.isna(row["exposure_pct"]) else float(row["exposure_pct"]),
            "status": existing.get("status"),
            "source": source,
            "reentry_lock": int(row.get("reentry_lock") or 0),
        }

    return singles


SINGLES = load_singles_from_db()


def enrich_pair_config(pair, cfg):
    left, right = str(pair).split("_", 1)
    left = left.upper()
    right = right.upper()

    return {
        "ticker_y": left,
        "ticker_x": right,
        "y": f"{left}_price",
        "x": f"{right}_price",
        **cfg,
    }


def load_pair_config(pair_name, data_source=FIXED_BETA_SOURCE_5M):
    """Read the production fixed-beta dashboard settings for one pair/source."""
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT train_start_bj, test_end_bj, fixed_beta
                FROM {quote_sql_identifier(PAIR_CONFIG_TABLE)}
                WHERE pair_name = %s AND data_source = %s
                """,
                (str(pair_name).strip().lower(), data_source),
            )
            row = cursor.fetchone()
    finally:
        conn.close()

    if row is None:
        return None
    return {
        "train_start": pd.Timestamp(row[0]),
        "test_end": pd.Timestamp(row[1]),
        "fixed_beta": float(row[2]),
        "data_source": data_source,
    }


def add_fixed_beta_rolling_indicators(data, beta, rolling_window, kappa_window):
    """Match quant_dashboard_fixed_beta's causal rolling pair indicators."""
    result = data.copy()
    result["spread"] = result["y_price"] - float(beta) * result["x_price"]
    result["rolling_mean"] = result["spread"].rolling(rolling_window, min_periods=rolling_window).mean()
    result["rolling_sigma"] = result["spread"].rolling(rolling_window, min_periods=rolling_window).std(ddof=0)
    result["upper_2sigma"] = result["rolling_mean"] + 2 * result["rolling_sigma"]
    result["lower_2sigma"] = result["rolling_mean"] - 2 * result["rolling_sigma"]
    safe_sigma = result["rolling_sigma"].where(result["rolling_sigma"] > 0)
    result["zscore"] = (result["spread"] - result["rolling_mean"]) / safe_sigma

    distance = (result["zscore"].abs() - KAPPA_NEUTRAL_BOUNDARY).clip(lower=0.0)
    previous_z = result["zscore"].shift(1)
    previous_sigma = result["rolling_sigma"].shift(1)
    z_reference = np.sign(previous_z) * np.maximum(previous_z.abs(), KAPPA_NEUTRAL_BOUNDARY)
    z_reference = z_reference.where(previous_z.notna() & previous_z.ne(0.0))
    crossing_event = (result["spread"].diff() / (z_reference * previous_sigma)).le(-1.0).fillna(False)
    previous_distance = distance.shift(1)
    transition_used = (
        ~crossing_event
        & previous_distance.notna()
        & distance.notna()
        & previous_distance.gt(0.0)
    )
    numerator = (previous_distance * (distance - previous_distance)).where(transition_used, 0.0)
    denominator = (previous_distance ** 2).where(transition_used, 0.0)
    rolling_numerator = numerator.rolling(kappa_window, min_periods=kappa_window).sum()
    rolling_denominator = denominator.rolling(kappa_window, min_periods=kappa_window).sum()
    transition_count = transition_used.astype(int).rolling(kappa_window, min_periods=kappa_window).sum()
    result["kappa_hat"] = (rolling_numerator / rolling_denominator).where(
        (rolling_denominator > 1e-12)
        & (transition_count >= KAPPA_MIN_EFFECTIVE_TRANSITIONS)
    )
    return result


def json_safe_number_list(values, digits=None):
    """Emit strict JSON nulls instead of NaN/Infinity in indicator series."""
    result = []
    for value in values:
        number = safe_float(value)
        result.append(None if number is None else (round(number, digits) if digits is not None else number))
    return result


def add_daily_spread_bands(data, beta):
    """Daily-close display overlay; separate from the 5-minute execution model."""
    daily = (
        data.sort_values("time_bj")
        .groupby(data["time_bj"].dt.normalize(), sort=True)
        .tail(1)
        .copy()
        .reset_index(drop=True)
    )
    daily["spread"] = daily["y_price"] - float(beta) * daily["x_price"]
    daily["daily_rolling_mean"] = daily["spread"].rolling(ROLLING_DAYS, min_periods=ROLLING_DAYS).mean()
    daily["daily_rolling_sigma"] = daily["spread"].rolling(ROLLING_DAYS, min_periods=ROLLING_DAYS).std(ddof=0)
    daily["daily_upper_2sigma"] = daily["daily_rolling_mean"] + 2 * daily["daily_rolling_sigma"]
    daily["daily_lower_2sigma"] = daily["daily_rolling_mean"] - 2 * daily["daily_rolling_sigma"]
    daily["time_str"] = daily["time_bj"].dt.strftime("%Y-%m-%d %H:%M")
    return daily


def quote_sql_identifier(value):
    return "`" + str(value).replace("`", "``") + "`"


def load_data(ticker_y, ticker_x, start_time=None, end_time=None):
    conn = pymysql.connect(**DB_CONFIG)
    try:
        y_price_col = quote_sql_identifier(f"{ticker_y}_price")
        x_price_col = quote_sql_identifier(f"{ticker_x}_price")

        sql = f"""
        SELECT
            a.time_utc,
            DATE_ADD(a.time_utc, INTERVAL 8 HOUR) AS time_bj,
            a.price AS {y_price_col},
            b.price AS {x_price_col}
        FROM us_market_prices a FORCE INDEX ({PRICE_LOOKUP_INDEX})
        JOIN us_market_prices b FORCE INDEX ({PRICE_LOOKUP_INDEX})
            ON b.ticker = %s
           AND b.time_utc = a.time_utc
        WHERE a.ticker = %s
        """

        params = [ticker_x, ticker_y]

        if start_time is not None:
            sql += " AND DATE_ADD(a.time_utc, INTERVAL 8 HOUR) >= %s"
            params.append(start_time)

        if end_time is not None:
            sql += " AND DATE_ADD(a.time_utc, INTERVAL 8 HOUR) <= %s"
            params.append(end_time)

        sql += " ORDER BY a.time_utc"

        df = pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()

    df["time_utc"] = pd.to_datetime(df["time_utc"])
    df["time_bj"] = pd.to_datetime(df["time_bj"])
    return df


def load_single_data(ticker, start_time=None, end_time=None):
    conn = pymysql.connect(**DB_CONFIG)
    try:
        sql = f"""
        SELECT
            time_utc,
            DATE_ADD(time_utc, INTERVAL 8 HOUR) AS time_bj,
            price
        FROM us_market_prices FORCE INDEX ({PRICE_LOOKUP_INDEX})
        WHERE ticker = %s
          AND price IS NOT NULL
        """
        params = [ticker]

        if start_time is not None:
            sql += " AND DATE_ADD(time_utc, INTERVAL 8 HOUR) >= %s"
            params.append(start_time)

        if end_time is not None:
            sql += " AND DATE_ADD(time_utc, INTERVAL 8 HOUR) <= %s"
            params.append(end_time)

        sql += " ORDER BY time_utc"
        df = pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()

    df["time_utc"] = pd.to_datetime(df["time_utc"])
    df["time_bj"] = pd.to_datetime(df["time_bj"])
    return df


def fetch_pending_orders():
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        sql = """
        SELECT
            id,
            pair_name,
            train_start,
            split_time,
            two_sigma,
            position_type,
            beta,
            exposure_pct,
            created_at
        FROM scheduled_orders
        WHERE status = 'PENDING'
        ORDER BY created_at DESC, id DESC
        """
        df = pd.read_sql(sql, conn)
    finally:
        conn.close()

    items = []
    for row in df.to_dict(orient="records"):
        item = {}
        for key, value in row.items():
            item[key] = None if pd.isna(value) else value
        item["pair_name"] = str(item.get("pair_name") or "").strip().lower()
        items.append(item)
    return items


def serialize_db_value(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, Decimal):
        number = float(value)
        return int(number) if number.is_integer() else number
    if pd.isna(value):
        return None
    return value


def serialize_db_row(row):
    return {key: serialize_db_value(value) for key, value in row.items()}


def normalize_history_limit(value, default=8, maximum=100):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(limit, maximum))


def fetch_trade_history(name, kind, limit=100):
    kind_key = str(kind or "").strip().lower()
    if kind_key == "single":
        symbol = str(name or "").strip().upper()
        if not symbol:
            return []

        sql = """
        SELECT
            id,
            single_name,
            strategy,
            param,
            position_type,
            exposure_pct,
            status,
            result,
            max_return,
            created_at,
            close_time
        FROM single_scheduled_orders
        WHERE UPPER(single_name) = %s
        ORDER BY COALESCE(close_time, created_at) DESC, id DESC
        LIMIT %s
        """
        params = (symbol, int(limit))
    else:
        pair_name = str(name or "").strip().lower().replace(" / ", "_")
        if not pair_name:
            return []

        sql = """
        SELECT
            id,
            pair_name,
            train_start,
            split_time,
            position_type,
            beta,
            two_sigma,
            exposure_pct,
            status,
            result,
            max_return,
            created_at,
            close_time
        FROM scheduled_orders
        WHERE LOWER(pair_name) = %s
        ORDER BY COALESCE(close_time, created_at) DESC, id DESC
        LIMIT %s
        """
        params = (pair_name, int(limit))

    conn = pymysql.connect(**MEMORY_DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return [serialize_db_row(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def resolve_pair_config(pair, refresh=False):
    global PAIRS, PAIRS_LOADED_AT

    pair_key = str(pair or "").strip().lower()
    if refresh or pair_key not in PAIRS:
        PAIRS = load_pairs_from_db()
        PAIRS_LOADED_AT = time.time()
    return pair_key, PAIRS.get(pair_key)


def resolve_single_config(single_name, refresh=False):
    global SINGLES, PAIRS_LOADED_AT

    single_key = str(single_name or "").strip().upper()
    if refresh or single_key not in SINGLES:
        SINGLES = load_singles_from_db()
        PAIRS_LOADED_AT = time.time()
    return single_key, SINGLES.get(single_key)


def refresh_pairs_if_stale(force=False):
    global PAIRS, SINGLES, PAIRS_LOADED_AT, PAIR_PAYLOAD_CACHE, SINGLE_PAYLOAD_CACHE

    now = time.time()
    if force or now - PAIRS_LOADED_AT > PAIR_CACHE_AUTO_REFRESH_SECONDS:
        PAIRS = load_pairs_from_db()
        SINGLES = load_singles_from_db()
        PAIRS_LOADED_AT = now
        PAIR_PAYLOAD_CACHE = {}
        SINGLE_PAYLOAD_CACHE = {}


def build_fixed_beta_pair_payload(pair_key, config=None, test_end_raw=None, include_series=True):
    """Build the 5-minute rolling payload used by the Fixed Beta dashboard."""
    try:
        ticker_y, ticker_x = str(pair_key).split("_", 1)
    except ValueError:
        return None, "invalid fixed-beta pair name"
    ticker_y, ticker_x = ticker_y.upper(), ticker_x.upper()
    if config is None:
        _, config = resolve_pair_config(pair_key)
    if config is None:
        return None, f"pair is not registered or active: {pair_key}"

    # The saved start is the chart start, not the calculation start.  Load all
    # prior bars so the rolling mean, sigma, and kappa have the same history as
    # quant_dashboard_fixed_beta.
    # A saved end date is a dashboard/view configuration, not a live-trading
    # cutoff. The default API payload follows the newest completed bar; an
    # explicit test_end still provides historical slicing when requested.
    end_time = pd.to_datetime(test_end_raw) if test_end_raw not in (None, "null") else None
    # The displayed start is a view choice.  Only retain enough earlier data
    # to warm the 20-day rolling mean/sigma and kappa, rather than loading the
    # database's entire history for every detail-page request.
    calculation_start = config["train_start"] - pd.Timedelta(days=ROLLING_WARMUP_CALENDAR_DAYS)
    raw = load_data(ticker_y, ticker_x, start_time=calculation_start, end_time=end_time)
    if raw.empty:
        return None, "not enough fixed-beta data"

    raw = raw.rename(columns={f"{ticker_y}_price": "y_price", f"{ticker_x}_price": "x_price"})
    rolling_window = ROLLING_DAYS * BARS_PER_TRADING_DAY
    kappa_window = ROLLING_DAYS * KAPPA_BARS_PER_TRADING_DAY
    data = add_fixed_beta_rolling_indicators(raw, config["fixed_beta"], rolling_window, kappa_window)
    data = data[data["time_bj"] >= config["train_start"]].copy().reset_index(drop=True)
    if data.empty:
        return None, "fixed-beta config start is after available data"
    valid_kappa = data["kappa_hat"].where(data["kappa_hat"] > -1.0)
    data["kappa_hat_daily_equivalent"] = np.expm1(
        KAPPA_BARS_PER_TRADING_DAY * np.log1p(valid_kappa)
    )
    data["bar_index"] = np.arange(len(data))
    data["time_str"] = data["time_bj"].dt.strftime("%Y-%m-%d %H:%M")
    daily_bands = add_daily_spread_bands(data, config["fixed_beta"])

    last = data.iloc[-1]
    last_bar = {
        "time": last["time_str"],
        "priceX": float(last["x_price"]),
        "priceY": float(last["y_price"]),
        "spread": float(last["spread"]),
        "zscore": safe_float(last["zscore"]),
        "rolling_mean": safe_float(last["rolling_mean"]),
        "rolling_sigma": safe_float(last["rolling_sigma"]),
        "kappa_hat": safe_float(last["kappa_hat"]),
        "kappa_hat_daily_equivalent": kappa_daily_equivalent(last["kappa_hat"]),
    }
    base = {
        "kind": "pair",
        "calculation": "fixed_beta_rolling",
        "x_label": ticker_x,
        "y_label": ticker_y,
        "beta": config["fixed_beta"],
        "fixed_beta": config["fixed_beta"],
        "alpha": None,
        # Compatibility aliases; for these two pairs they always mean the
        # latest causal rolling values, never a train-segment estimate.
        "mu": safe_float(last["rolling_mean"]),
        "sigma": safe_float(last["rolling_sigma"]),
        "rolling_mean": safe_float(last["rolling_mean"]),
        "rolling_sigma": safe_float(last["rolling_sigma"]),
        "kappa_hat": safe_float(last["kappa_hat"]),
        "kappa_hat_daily_equivalent": kappa_daily_equivalent(last["kappa_hat"]),
        "kappa_bars_per_trading_day": KAPPA_BARS_PER_TRADING_DAY,
        "source": config.get("source"),
        "last_bar": last_bar,
        "train_start": config["train_start"].strftime("%Y-%m-%d %H:%M"),
        "split": config["split"].strftime("%Y-%m-%d %H:%M") if config.get("split") is not None else None,
        "test_end": last["time_str"],
        "N": len(data),
        "bars_per_day": BARS_PER_TRADING_DAY,
        "rolling_window_bars": rolling_window,
        "rolling_window_days": ROLLING_DAYS,
        "kappa_window_bars": kappa_window,
        "kappa_neutral_boundary": KAPPA_NEUTRAL_BOUNDARY,
    }
    if not include_series:
        return base, None

    base.update({
        # The spread remains a 5-minute price series. Its visual mean/bands
        # are intentionally daily-close indicators, not decision-layer data.
        "spread_time": data["time_str"].tolist(),
        "spread": json_safe_number_list(data["spread"], digits=6),
        "spread_band_time": daily_bands["time_str"].tolist(),
        "daily_rolling_mean": json_safe_number_list(daily_bands["daily_rolling_mean"], digits=6),
        "daily_upper_2sigma": json_safe_number_list(daily_bands["daily_upper_2sigma"], digits=6),
        "daily_lower_2sigma": json_safe_number_list(daily_bands["daily_lower_2sigma"], digits=6),
        "kappa_hat_daily_equivalent_series": json_safe_number_list(
            data["kappa_hat_daily_equivalent"], digits=8
        ),
        "time": data["time_str"].tolist(),
    })
    return base, None


def build_pair_payload(pair, test_end_raw=None, refresh_pairs=False, include_series=True):
    pair_key, base_cfg = resolve_pair_config(pair, refresh=refresh_pairs)
    if not base_cfg:
        return None, "pair not found"
    return build_fixed_beta_pair_payload(
        pair_key, config=base_cfg, test_end_raw=test_end_raw, include_series=include_series
    )


def fixed_beta_rolling_snapshot_key(pair_key):
    return f"{FIXED_BETA_ROLLING_SNAPSHOT_PREFIX}{str(pair_key).strip().lower()}"


def load_fixed_beta_rolling_snapshot(pair_key):
    """Read app.py's authoritative published rolling baseline for one pair."""
    try:
        raw = REDIS_CLIENT.get(fixed_beta_rolling_snapshot_key(pair_key))
        payload = json.loads(raw) if raw else None
    except (redis.exceptions.RedisError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("calculation") != "fixed_beta_rolling":
        return None
    return payload


def publish_fixed_beta_rolling_snapshot(pair_key):
    """Publish one bar's baseline and prewarm the matching dashboard payload."""
    payload, error = build_fixed_beta_pair_payload(pair_key, include_series=False)
    if error:
        return None, error

    last_bar = payload.get("last_bar") or {}
    snapshot = {
        "ok": True,
        "source": "app.py_fixed_beta_rolling",
        "pair": str(pair_key).strip().lower(),
        "calculation": "fixed_beta_rolling",
        "fixed_beta": safe_float(payload.get("fixed_beta")),
        "beta": safe_float(payload.get("fixed_beta")),
        "alpha": 0.0,
        "rolling_mean": safe_float(payload.get("rolling_mean")),
        "rolling_sigma": safe_float(payload.get("rolling_sigma")),
        "kappa_hat": safe_float(payload.get("kappa_hat")),
        "kappa_hat_daily_equivalent": safe_float(payload.get("kappa_hat_daily_equivalent")),
        "mu": safe_float(payload.get("rolling_mean")),
        "sigma": safe_float(payload.get("rolling_sigma")),
        "x_label": payload.get("x_label"),
        "y_label": payload.get("y_label"),
        "bar_time_bj": last_bar.get("time"),
        "last_bar": last_bar,
        "updated_at": datetime.now(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "updated_ts": time.time(),
    }
    if snapshot["rolling_mean"] is None or snapshot["rolling_sigma"] in (None, 0):
        return None, "invalid rolling baseline"

    previous = load_fixed_beta_rolling_snapshot(pair_key)
    changed = (
        previous is None
        or previous.get("bar_time_bj") != snapshot["bar_time_bj"]
        or safe_float(previous.get("fixed_beta")) != snapshot["fixed_beta"]
    )
    detail_cache_key_value = detail_cache_key("pair", pair_key, None)
    if changed:
        REDIS_CLIENT.set(
            fixed_beta_rolling_snapshot_key(pair_key),
            json.dumps(snapshot, ensure_ascii=False, default=str),
        )
    if changed or detail_cache_key_value not in PAIR_PAYLOAD_CACHE:
        # Only a genuinely new database bar needs the heavier full chart
        # payload. A restarted app also warms an empty in-memory cache once;
        # retry attempts while FillDB is catching up stay compact.
        detail_payload, detail_error = build_fixed_beta_pair_payload(pair_key, include_series=True)
        if detail_error is None:
            PAIR_PAYLOAD_CACHE[detail_cache_key_value] = {
                "created_at": time.time(),
                "payload": detail_payload,
                "error": None,
            }
    return snapshot if changed else previous, None


def configured_fixed_beta_pairs_for_ticker(ticker):
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        return []
    refresh_pairs_if_stale()
    matches = []
    for pair_key in PAIRS:
        try:
            ticker_y, ticker_x = pair_key.upper().split("_", 1)
        except ValueError:
            continue
        if symbol in {ticker_y, ticker_x}:
            matches.append(pair_key)
    return matches


def run_fixed_beta_rolling_publisher():
    """Keep one Redis baseline per configured pair aligned to closed 5m bars.

    A closed live-bar event can precede the us_market_prices write.  Therefore
    an unchanged database bar is retried briefly instead of being published as
    the new baseline.
    """
    pending = {pair_key: (time.monotonic(), 0) for pair_key in PAIRS}
    pubsub = None
    while True:
        try:
            if pubsub is None:
                pubsub = REDIS_CLIENT.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(BAR_CHANNEL)

            while True:
                message = pubsub.get_message(timeout=0)
                if not message:
                    break
                if message.get("type") != "message":
                    continue
                try:
                    event = json.loads(message.get("data") or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                for pair_key in configured_fixed_beta_pairs_for_ticker(event.get("ticker")):
                    pending[pair_key] = (time.monotonic(), 0)

            now = time.monotonic()
            for pair_key, (due_at, attempts) in list(pending.items()):
                if now < due_at:
                    continue
                previous = load_fixed_beta_rolling_snapshot(pair_key)
                snapshot, error = publish_fixed_beta_rolling_snapshot(pair_key)
                if error:
                    if attempts < FIXED_BETA_ROLLING_PUBLISH_MAX_RETRIES:
                        pending[pair_key] = (now + FIXED_BETA_ROLLING_PUBLISH_RETRY_SECONDS, attempts + 1)
                    else:
                        pending.pop(pair_key, None)
                    continue

                # On a bar-close event, keep retrying until the database's
                # completed bar advances.  Initial startup has no prior
                # snapshot, so its first valid baseline is immediately usable.
                if previous is not None and snapshot.get("bar_time_bj") == previous.get("bar_time_bj"):
                    if attempts < FIXED_BETA_ROLLING_PUBLISH_MAX_RETRIES:
                        pending[pair_key] = (now + FIXED_BETA_ROLLING_PUBLISH_RETRY_SECONDS, attempts + 1)
                    else:
                        pending.pop(pair_key, None)
                else:
                    pending.pop(pair_key, None)
            time.sleep(0.25)
        except redis.exceptions.RedisError:
            if pubsub is not None:
                try:
                    pubsub.close()
                except redis.exceptions.RedisError:
                    pass
                pubsub = None
            time.sleep(2)
        except Exception:
            # The web API remains available even if a transient publisher
            # failure occurs; the next loop reconnects and resumes retries.
            time.sleep(2)


def start_fixed_beta_rolling_publisher():
    worker = threading.Thread(
        target=run_fixed_beta_rolling_publisher,
        name="fixed-beta-rolling-publisher",
        daemon=True,
    )
    worker.start()
    return worker


def build_single_payload(single_name, test_end_raw=None, refresh_singles=False, include_series=True):
    single_key, config = resolve_single_config(single_name, refresh=refresh_singles)
    if not config:
        return None, "single not found"

    train_start_time = pd.to_datetime(config.get("train_start"))
    split_time = pd.to_datetime(config.get("split"))
    strategy = str(config.get("strategy") or "").strip().upper()
    is_log_residual = strategy == "LOG_RESIDUAL"

    if pd.isna(train_start_time):
        return None, "missing train_start"

    if test_end_raw is None or test_end_raw == "null":
        test_end_time = None
    else:
        test_end_time = pd.to_datetime(test_end_raw)

    df = load_single_data(
        single_key,
        start_time=train_start_time,
        end_time=test_end_time,
    )

    total_rows = len(df)
    if total_rows < 2:
        return None, "not enough data"

    df_slice = df[df["time_bj"] >= train_start_time].copy()
    if test_end_time is not None:
        df_slice = df_slice[df_slice["time_bj"] <= test_end_time]
    if df_slice.empty:
        return None, "not enough data"
    if test_end_time is None:
        test_end_time = df_slice["time_bj"].iloc[-1]

    df_slice = df_slice.reset_index(drop=True)
    df_slice["time_str"] = df_slice["time_bj"].dt.strftime("%Y-%m-%d %H:%M")
    df_slice["bar_index"] = np.arange(len(df_slice))
    first_price = float(df_slice["price"].iloc[0])
    df_slice["return_pct"] = np.where(
        first_price != 0,
        (df_slice["price"].astype(float) / first_price - 1.0) * 100.0,
        0.0,
    )
    if is_log_residual:
        df_slice["log_price"] = np.log(df_slice["price"].astype(float))

    last = df_slice.iloc[-1]
    last_bar = {
        "time": last["time_bj"].strftime("%Y-%m-%d %H:%M"),
        "price": float(last["price"]),
        "return_pct": float(last["return_pct"]),
    }
    if is_log_residual:
        last_bar["log_price"] = float(last["log_price"])

    split_value = None if pd.isna(split_time) else split_time.strftime("%Y-%m-%d %H:%M")
    payload_base = {
        "kind": "single",
        "symbol": single_key,
        "order_id": config.get("id"),
        "source": config.get("source"),
        "strategy": config.get("strategy"),
        "price_scale": "log" if is_log_residual else "linear",
        "param": config.get("param"),
        "position_type": config.get("position_type"),
        "exposure_pct": config.get("exposure_pct"),
        "last_bar": last_bar,
        "train_start": train_start_time.strftime("%Y-%m-%d %H:%M"),
        "split": split_value,
        "N": total_rows,
        "test_end": test_end_time.strftime("%Y-%m-%d %H:%M"),
        "bars_per_day": 79,
    }

    if not include_series:
        return payload_base, None

    payload = {
        **payload_base,
        "index": df_slice["bar_index"].tolist(),
        "time": df_slice["time_str"].tolist(),
        "price": df_slice["price"].tolist(),
        "return_pct": df_slice["return_pct"].tolist(),
    }
    if is_log_residual:
        payload["log_price"] = df_slice["log_price"].tolist()
    return payload, None


def get_cached_pair_payload(pair_name):
    pair_key = str(pair_name or "").strip().lower()
    cached = PAIR_PAYLOAD_CACHE.get(("summary", pair_key))
    if cached:
        return cached["payload"], cached["error"]

    payload, error = build_pair_payload(pair_key, refresh_pairs=False, include_series=False)
    PAIR_PAYLOAD_CACHE[("summary", pair_key)] = {
        "created_at": time.time(),
        "payload": payload,
        "error": error,
    }
    return payload, error


def get_cached_single_payload(single_name):
    single_key = str(single_name or "").strip().upper()
    cached = SINGLE_PAYLOAD_CACHE.get(("summary", single_key))
    if cached:
        return cached["payload"], cached["error"]

    payload, error = build_single_payload(single_key, refresh_singles=False, include_series=False)
    SINGLE_PAYLOAD_CACHE[("summary", single_key)] = {
        "created_at": time.time(),
        "payload": payload,
        "error": error,
    }
    return payload, error


def detail_cache_key(kind, name, test_end_raw):
    normalized_name = (
        str(name or "").strip().upper()
        if kind == "single"
        else str(name or "").strip().lower()
    )
    normalized_test_end = "" if test_end_raw in (None, "null") else str(test_end_raw)
    return ("detail", normalized_name, normalized_test_end)


def get_cached_detail_payload(kind, name, builder, test_end_raw=None, force_refresh=False):
    cache = SINGLE_PAYLOAD_CACHE if kind == "single" else PAIR_PAYLOAD_CACHE
    cache_key = detail_cache_key(kind, name, test_end_raw)
    cached = cache.get(cache_key)
    now = time.time()
    if not force_refresh and cached and now - cached.get("created_at", 0) <= DETAIL_PAYLOAD_CACHE_SECONDS:
        return cached["payload"], cached["error"]

    payload, error = builder(name, test_end_raw=test_end_raw)
    cache[cache_key] = {
        "created_at": now,
        "payload": payload,
        "error": error,
    }
    return payload, error


def load_latest_pair_prices_from_db(y_symbol, x_symbol):
    symbols = [str(y_symbol or "").strip().upper(), str(x_symbol or "").strip().upper()]
    symbols = [symbol for symbol in symbols if symbol]
    if not symbols:
        return {}

    placeholders = ",".join(["%s"] * len(symbols))
    conn = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT p.ticker, p.price
                FROM us_market_prices p
                JOIN (
                    SELECT ticker, MAX(time_utc) AS max_time
                    FROM us_market_prices
                    WHERE ticker IN ({placeholders})
                      AND price IS NOT NULL
                    GROUP BY ticker
                ) latest
                  ON latest.ticker = p.ticker
                 AND latest.max_time = p.time_utc
                """,
                tuple(symbols),
            )
            rows = cursor.fetchall()
    finally:
        conn.close()

    return {
        str(row.get("ticker") or "").strip().upper(): safe_float(row.get("price"))
        for row in rows
    }


def load_latest_market_prices(symbols):
    normalized = []
    for symbol in symbols:
        symbol = str(symbol or "").strip().upper()
        if symbol and symbol not in normalized:
            normalized.append(symbol)
    if not normalized:
        return {}

    placeholders = ",".join(["%s"] * len(normalized))
    conn = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT p.ticker, p.price, p.time_utc
                FROM us_market_prices p
                JOIN (
                    SELECT ticker, MAX(time_utc) AS max_time
                    FROM us_market_prices
                    WHERE ticker IN ({placeholders})
                      AND price IS NOT NULL
                    GROUP BY ticker
                ) latest
                  ON latest.ticker = p.ticker
                 AND latest.max_time = p.time_utc
                """,
                tuple(normalized),
            )
            rows = cursor.fetchall()
    finally:
        conn.close()

    return {
        str(row.get("ticker") or "").strip().upper(): {
            "price": safe_float(row.get("price")),
            "updated_at": format_bj_timestamp(row.get("time_utc")),
        }
        for row in rows
    }


def build_dashboard_items():
    merged = {}

    def add_item(item):
        name = str(item.get("name") or "").strip()
        if not name:
            return
        key = name.upper()
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
            return

        existing_source = str(existing.get("source") or "")
        new_source = str(item.get("source") or "")
        sources = [x for x in existing_source.split("+") if x]
        if new_source and new_source not in sources:
            sources.append(new_source)

        if existing.get("kind") != "single" and item.get("kind") == "single":
            item = dict(item)
            item["source"] = "+".join(sources) if sources else item.get("source")
            merged[key] = item
            return

        if sources:
            existing["source"] = "+".join(sources)
        for field in ("train_start", "split", "order_id"):
            if existing.get(field) in (None, "") and item.get(field) not in (None, ""):
                existing[field] = item.get(field)

    for pair_name in sorted(PAIRS.keys()):
        add_item(
            {
                "name": pair_name,
                "kind": "pair",
                "source": PAIRS[pair_name].get("source"),
                "train_start": PAIRS[pair_name].get("train_start"),
                "split": PAIRS[pair_name].get("split"),
                "reentry_lock": PAIRS[pair_name].get("reentry_lock"),
            }
        )

    for single_name in sorted(SINGLES.keys()):
        add_item(
            {
                "name": single_name,
                "kind": "single",
                "source": SINGLES[single_name].get("source"),
                "train_start": SINGLES[single_name].get("train_start"),
                "split": SINGLES[single_name].get("split"),
                "order_id": SINGLES[single_name].get("id"),
                "reentry_lock": SINGLES[single_name].get("reentry_lock"),
            }
        )

    def dashboard_sort_key(item):
        source = str(item.get("source") or "").upper()
        if "REENTRY" in source:
            source_rank = 0
        elif "HOLDING" in source:
            source_rank = 1
        elif "PENDING" in source:
            source_rank = 2
        else:
            source_rank = 3

        try:
            lock_rank = int(item.get("reentry_lock"))
        except (TypeError, ValueError):
            lock_rank = 99

        kind_rank = 0 if item.get("kind") == "pair" else 1
        return (kind_rank, source_rank, lock_rank, str(item.get("name") or "").upper())

    return sorted(merged.values(), key=dashboard_sort_key)


def safe_float(value):
    try:
        value = float(value)
        return value if np.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def payload_spread(pair_payload, price_y, price_x):
    """Use the same spread definition as the payload's calculation mode."""
    beta = float(pair_payload["beta"])
    if pair_payload.get("calculation") == "fixed_beta_rolling":
        return float(price_y) - beta * float(price_x)
    return float(price_y) - (beta * float(price_x) + float(pair_payload["alpha"]))


def compute_live_pending_zscore(pair_name, pair_payload):
    x_label = str(pair_payload.get("x_label") or "").strip()
    y_label = str(pair_payload.get("y_label") or "").strip()
    x_symbol = x_label.upper()
    y_symbol = y_label.upper()
    if not x_symbol or not y_symbol:
        return None

    try:
        px_raw = REDIS_CLIENT.get(f"price:{x_symbol}")
        py_raw = REDIS_CLIENT.get(f"price:{y_symbol}")
        if px_raw is None or py_raw is None:
            px_raw = px_raw if px_raw is not None else REDIS_CLIENT.get(f"{pair_name}:{x_label}_price")
            py_raw = py_raw if py_raw is not None else REDIS_CLIENT.get(f"{pair_name}:{y_label}_price")

        ts_x_raw = REDIS_CLIENT.get(f"ts:{x_symbol}")
        ts_y_raw = REDIS_CLIENT.get(f"ts:{y_symbol}")
    except Exception:
        return None

    px = safe_float(px_raw)
    py = safe_float(py_raw)
    ts_x = safe_float(ts_x_raw)
    ts_y = safe_float(ts_y_raw)

    if px is None or py is None:
        latest_prices = load_latest_pair_prices_from_db(y_symbol, x_symbol)
        if px is None:
            px = latest_prices.get(x_symbol)
        if py is None:
            py = latest_prices.get(y_symbol)

    if px is None or py is None:
        return None

    sigma = safe_float(pair_payload.get("sigma"))
    mu = safe_float(pair_payload.get("mu"))
    if sigma in (None, 0) or mu is None:
        return None

    spread = payload_spread(pair_payload, py, px)
    zscore = (spread - mu) / sigma
    latest_ts = max(ts for ts in (ts_x, ts_y) if ts is not None) if ts_x is not None or ts_y is not None else None
    return {
        "zscore": float(zscore),
        "zscore_source": "live",
        "zscore_time": (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest_ts))
            if latest_ts is not None
            else None
        ),
        "priceX": px,
        "priceY": py,
    }


def build_pending_zscore_item(order_row):
    pair_name = str(order_row.get("pair_name") or "").strip().lower()
    pair_payload, error = get_cached_pair_payload(pair_name)
    if error:
        return {
            "id": int(order_row["id"]),
            "pair_name": pair_name,
            "pair_source": None,
            "zscore": None,
            "zscore_source": None,
            "zscore_time": None,
            "priceX": None,
            "priceY": None,
            "x_label": None,
            "y_label": None,
            "error": error,
        }

    live_result = compute_live_pending_zscore(pair_name, pair_payload)
    if live_result is None:
        last_bar = pair_payload.get("last_bar") or {}
        live_result = {
            "zscore": last_bar.get("zscore"),
            "zscore_source": "last_close",
            "zscore_time": last_bar.get("time"),
            "priceX": last_bar.get("priceX"),
            "priceY": last_bar.get("priceY"),
        }

    return {
        "id": int(order_row["id"]),
        "pair_name": pair_name,
        "pair_source": pair_payload.get("source"),
        "zscore": live_result.get("zscore"),
        "zscore_source": live_result.get("zscore_source"),
        "zscore_time": live_result.get("zscore_time"),
        "priceX": live_result.get("priceX"),
        "priceY": live_result.get("priceY"),
        "x_label": pair_payload.get("x_label"),
        "y_label": pair_payload.get("y_label"),
        "error": None,
    }


def load_waiting_entry_rows():
    conn = pymysql.connect(**MEMORY_DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    pr.pair_name,
                    pr.strategy,
                    pr.param,
                    pr.intercept,
                    pr.residual,
                    pr.threshold,
                    COALESCE(pr.reentry_lock, 0) AS reentry_lock,
                    lrc.train_start AS log_train_start,
                    lrc.k AS log_k,
                    lrc.intercept AS log_intercept,
                    lrc.threshold AS log_threshold
                FROM pair_reentry pr
                LEFT JOIN log_residual_config lrc
                  ON UPPER(lrc.stock_name) = UPPER(pr.pair_name)
                WHERE pr.pair_name IS NOT NULL
                  AND pr.pair_name <> ''
                ORDER BY COALESCE(pr.reentry_lock, 0), pr.pair_name
                """
            )
            return cursor.fetchall()
    finally:
        conn.close()


def latest_single_window(ticker, train_start):
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        return None

    conn = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT time_utc, price
                FROM (
                    SELECT time_utc, price
                    FROM us_market_prices
                    WHERE ticker = %s
                      AND price IS NOT NULL
                    ORDER BY time_utc DESC
                    LIMIT 78
                ) latest_window
                ORDER BY time_utc ASC
                """,
                (symbol,),
            )
            rows = cursor.fetchall()
            if not rows:
                return None

            offset = 0
            if train_start is not None and rows[0].get("time_utc") is not None:
                train_start_utc = pd.Timestamp(train_start) - pd.Timedelta(hours=8)
                cursor.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM us_market_prices
                    WHERE ticker = %s
                      AND price IS NOT NULL
                      AND time_utc >= %s
                      AND time_utc < %s
                    """,
                    (
                        symbol,
                        train_start_utc.to_pydatetime(),
                        pd.Timestamp(rows[0]["time_utc"]).to_pydatetime(),
                    ),
                )
                offset = int((cursor.fetchone() or {}).get("n") or 0)
    finally:
        conn.close()

    return {
        "rows": rows,
        "offset": offset,
        "bars": len(rows),
        "latest_time": rows[-1].get("time_utc"),
    }


def build_waiting_entry_single_log(row, fallback_db=False):
    name = str(row.get("pair_name") or "").strip()
    ticker = name.upper()
    k = safe_float(row.get("log_k"))
    if k is None:
        k = safe_float(row.get("param"))
    intercept = safe_float(row.get("log_intercept"))
    if intercept is None:
        intercept = safe_float(row.get("intercept"))
    entry = safe_float(row.get("log_threshold"))
    if entry is None:
        entry = safe_float(row.get("residual"))

    price, ts = (None, None) if fallback_db else redis_tick_price(ticker)
    result = {
        "name": ticker,
        "kind": "single_log_residual",
        "strategy": "LOG_RESIDUAL",
        "lock": int(row.get("reentry_lock") or 0),
        "signal": None,
        "signal_label": "no tick",
        "signal_state": "muted",
        "price": price,
        "rolling_mean": None,
        "entry": entry,
        "bars": 0,
        "updated_at": None if ts is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
    }
    if k is None or intercept is None or entry is None:
        result["signal_label"] = "missing model"
        return result

    window = latest_single_window(ticker, row.get("log_train_start") or row.get("train_start"))
    if not window:
        result["signal_label"] = "no history"
        return result

    residuals = []
    for index, item in enumerate(window["rows"]):
        item_price = safe_float(item.get("price"))
        if item_price is not None and item_price > 0:
            residuals.append(np.log(item_price) - (intercept + k * (window["offset"] + index)))
    if residuals:
        result["rolling_mean"] = float(sum(residuals) / len(residuals))
    result["bars"] = int(window["bars"])

    if fallback_db and window["rows"]:
        latest_row = window["rows"][-1]
        price = safe_float(latest_row.get("price"))
        result["price"] = price
        result["updated_at"] = format_bj_timestamp(latest_row.get("time_utc"))
        current_bar_index = window["offset"] + window["bars"] - 1
    else:
        current_bar_index = window["offset"] + window["bars"]

    if price is not None and price > 0:
        current = np.log(price) - (intercept + k * current_bar_index)
        result["signal"] = float(current)
        result["signal_label"] = f"{current:+.5f}"
        if fallback_db:
            result["signal_state"] = "fallback"
        elif result["rolling_mean"] is None:
            result["signal_state"] = "muted"
        elif current > entry:
            result["signal_state"] = "ok"
        elif current > result["rolling_mean"]:
            result["signal_state"] = "warn"
        else:
            result["signal_state"] = "trigger"
    return result


def build_waiting_entry_single_new_high(row, fallback_db=False):
    name = str(row.get("pair_name") or "").strip()
    ticker = name.upper()
    trigger = safe_float(row.get("threshold"))
    if trigger is None:
        trigger = safe_float(row.get("param"))
    price, ts = (None, None) if fallback_db else redis_tick_price(ticker)
    updated_at = None if ts is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    if fallback_db:
        window = latest_single_window(ticker, None)
        if window and window["rows"]:
            latest_row = window["rows"][-1]
            price = safe_float(latest_row.get("price"))
            updated_at = format_bj_timestamp(latest_row.get("time_utc"))
    return {
        "name": ticker,
        "kind": "single_new_high",
        "strategy": "NEW_HIGH",
        "lock": int(row.get("reentry_lock") or 0),
        "signal": price,
        "signal_label": f"{price:.2f}" if price is not None else "no tick",
        "signal_state": "fallback" if fallback_db else ("ok" if price is not None else "muted"),
        "price": price,
        "rolling_mean": None,
        "entry": trigger,
        "bars": 0,
        "updated_at": updated_at,
    }


def build_waiting_entry_pair(row, fallback_db=False):
    pair_name = str(row.get("pair_name") or "").strip().lower()
    threshold = normalize_threshold(row.get("threshold"))
    item = {
        "name": pair_name.upper().replace("_", " / "),
        "pair_key": pair_name,
        "kind": "pair",
        "strategy": str(row.get("strategy") or "PAIR").upper(),
        "lock": int(row.get("reentry_lock") or 0),
        "signal": None,
        "signal_label": "N/A",
        "signal_state": "muted",
        "price": None,
        "price_x": None,
        "price_y": None,
        "rolling_mean": None,
        "entry": threshold,
        "bars": None,
        "updated_at": None,
    }
    try:
        payload, error = build_pair_payload(pair_name, include_series=False)
        if error:
            item["signal_label"] = error
            return item
        item["rolling_mean"] = payload.get("rolling_mean")
        if fallback_db:
            live = {"zscore": None}
        else:
            live = compute_live_pending_zscore(pair_name, payload)
        if live.get("zscore") is None:
            last_bar = payload.get("last_bar") or {}
            zscore = last_bar.get("zscore")
            item["signal_label"] = "-" if zscore is None else f"{float(zscore):+.3f}"
            item["signal"] = zscore
            item["price_x"] = last_bar.get("priceX")
            item["price_y"] = last_bar.get("priceY")
            item["updated_at"] = last_bar.get("time")
            item["signal_state"] = "fallback" if fallback_db else "muted"
            return item
        zscore = float(live["zscore"])
        item["signal"] = zscore
        item["signal_label"] = f"{zscore:+.3f}"
        abs_zscore = abs(zscore)
        if abs_zscore > threshold:
            item["signal_state"] = "trigger"
        elif abs_zscore > 1:
            item["signal_state"] = "warn"
        else:
            item["signal_state"] = "ok"
        item["price_x"] = live.get("priceX")
        item["price_y"] = live.get("priceY")
        item["updated_at"] = live.get("zscore_time")
        return item
    except Exception as exc:
        item["signal_label"] = str(exc)
        return item


def build_waiting_entry_item(row, fallback_db=False):
    name = str(row.get("pair_name") or "").strip()
    strategy = str(row.get("strategy") or "").strip().upper()
    if "_" not in name and strategy in {"LOG_RESIDUAL", "SINGLE_LOG_RESIDUAL"}:
        return build_waiting_entry_single_log(row, fallback_db=fallback_db)
    if "_" not in name and strategy in {"NEW_HIGH", "NEW HIGH"}:
        return build_waiting_entry_single_new_high(row, fallback_db=fallback_db)
    return build_waiting_entry_pair(row, fallback_db=fallback_db)


def load_flagship_position_rows():
    conn = pymysql.connect(**MEMORY_DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT pair_name, ticker_y, ticker_x, entry_time
                     , avg_y, qty_y, avg_x, qty_x
                FROM pair_positions
                ORDER BY pair_name
                """
            )
            pairs = cursor.fetchall()
            cursor.execute(
                """
                SELECT single_name, entry_time, avg, qty
                FROM single_positions
                WHERE UPPER(single_name) NOT IN (
                    SELECT UPPER(ticker_y) FROM pair_positions WHERE ticker_y IS NOT NULL AND ticker_y <> ''
                    UNION
                    SELECT UPPER(ticker_x) FROM pair_positions WHERE ticker_x IS NOT NULL AND ticker_x <> ''
                )
                ORDER BY single_name
                """
            )
            singles = cursor.fetchall()

    finally:
        conn.close()
    return pairs, singles


def flagship_position_maps():
    pair_rows, single_rows = load_flagship_position_rows()
    pair_map = {
        str(row.get("pair_name") or "").strip().upper(): row
        for row in pair_rows
    }
    single_map = {
        str(row.get("single_name") or "").strip().upper(): row
        for row in single_rows
    }
    return pair_map, single_map


def apply_pair_position_fields(item, row):
    if not row:
        return item

    qty_y = safe_float(row.get("qty_y"))
    qty_x = safe_float(row.get("qty_x"))
    avg_y = safe_float(row.get("avg_y"))
    avg_x = safe_float(row.get("avg_x"))
    py = safe_float(item.get("price_y"))
    px = safe_float(item.get("price_x"))

    pnl_y = (py - avg_y) * qty_y if py is not None and avg_y is not None and qty_y is not None else None
    pnl_x = (px - avg_x) * qty_x if px is not None and avg_x is not None and qty_x is not None else None
    total = (pnl_y or 0) + (pnl_x or 0) if pnl_y is not None or pnl_x is not None else None

    entry_notional = 0
    if avg_y is not None and qty_y is not None:
        entry_notional += abs(avg_y * qty_y)
    if avg_x is not None and qty_x is not None:
        entry_notional += abs(avg_x * qty_x)

    current_notional = 0
    if py is not None and qty_y is not None:
        current_notional += abs(py * qty_y)
    if px is not None and qty_x is not None:
        current_notional += abs(px * qty_x)

    item.update({
        "qty_y": qty_y,
        "qty_x": qty_x,
        "avg_y": avg_y,
        "avg_x": avg_x,
        "pnl_y": pnl_y,
        "pnl_x": pnl_x,
        "total": total,
        "return_pct": (total / entry_notional * 100) if total is not None and entry_notional > 0 else None,
        "current_notional": current_notional if current_notional > 0 else None,
        "beta": abs(qty_x / qty_y) if qty_y not in (None, 0) and qty_x is not None else item.get("beta"),
        "exposure_pct": safe_float(row.get("exposure_pct")) if item.get("exposure_pct") is None else item.get("exposure_pct"),
        "entry_time": None if row.get("entry_time") is None else str(row.get("entry_time")),
    })

    if avg_y is not None and avg_x is not None:
        pair_key = str(item.get("name") or "").strip().lower().replace(" / ", "_")
        try:
            payload, error = build_pair_payload(pair_key, include_series=False)
            if not error:
                spread_entry = payload_spread(payload, avg_y, avg_x)
                mu, sigma = safe_float(payload.get("mu")), safe_float(payload.get("sigma"))
                if mu is not None and sigma not in (None, 0):
                    item["z_entry"] = (spread_entry - mu) / sigma
        except Exception:
            pass
    return item


def apply_single_position_fields(item, row):
    if not row:
        return item

    qty = safe_float(row.get("qty"))
    avg = safe_float(row.get("avg"))
    price = safe_float(item.get("price"))
    pnl = (price - avg) * qty if price is not None and avg is not None and qty is not None else None
    entry_notional = abs(avg * qty) if avg is not None and qty is not None else 0
    current_notional = abs(price * qty) if price is not None and qty is not None else None

    position_type = str(row.get("position_type") or "").strip().upper()
    side = "LONG" if qty is not None and qty > 0 else "SHORT" if qty is not None and qty < 0 else None
    if side is None:
        if position_type == "LONG_SPREAD":
            side = "LONG"
        elif position_type == "SHORT_SPREAD":
            side = "SHORT"

    item.update({
        "qty": qty,
        "avg_price": avg,
        "pnl": pnl,
        "return_pct": (pnl / entry_notional * 100) if pnl is not None and entry_notional > 0 else None,
        "current_notional": current_notional,
        "exposure_pct": safe_float(row.get("exposure_pct")) if item.get("exposure_pct") is None else item.get("exposure_pct"),
        "side": side,
        "entry_time": None if row.get("entry_time") is None else str(row.get("entry_time")),
    })
    return item


def enrich_flagship_snapshot_with_positions(snapshot):
    pair_map, single_map = flagship_position_maps()
    enriched = []
    for source_item in snapshot.get("items") or []:
        item = dict(source_item)
        if item.get("kind") == "pair":
            key = str(item.get("name") or "").strip().upper()
            enriched.append(apply_pair_position_fields(item, pair_map.get(key)))
        else:
            key = str(item.get("symbol") or item.get("name") or "").strip().upper()
            enriched.append(apply_single_position_fields(item, single_map.get(key)))
    snapshot = dict(snapshot)
    snapshot["items"] = enriched
    snapshot["count"] = len(enriched)
    return annotate_flagship_hidden_items(snapshot)


def fallback_flagship_snapshot():
    pair_rows, single_rows = load_flagship_position_rows()
    symbols = []
    for row in pair_rows:
        symbols.extend([row.get("ticker_y"), row.get("ticker_x")])
    for row in single_rows:
        symbols.append(row.get("single_name"))
    prices = load_latest_market_prices(symbols)

    items = []
    for row in pair_rows:
        y = str(row.get("ticker_y") or "").strip().upper()
        x = str(row.get("ticker_x") or "").strip().upper()
        y_price = prices.get(y, {})
        x_price = prices.get(x, {})
        py = safe_float(y_price.get("price"))
        px = safe_float(x_price.get("price"))
        qty_y = safe_float(row.get("qty_y"))
        qty_x = safe_float(row.get("qty_x"))
        avg_y = safe_float(row.get("avg_y"))
        avg_x = safe_float(row.get("avg_x"))
        pnl_y = (py - avg_y) * qty_y if py is not None and avg_y is not None and qty_y is not None else None
        pnl_x = (px - avg_x) * qty_x if px is not None and avg_x is not None and qty_x is not None else None
        total = (pnl_y or 0) + (pnl_x or 0) if pnl_y is not None or pnl_x is not None else None
        entry_notional = 0
        if avg_y is not None and qty_y is not None:
            entry_notional += abs(avg_y * qty_y)
        if avg_x is not None and qty_x is not None:
            entry_notional += abs(avg_x * qty_x)
        current_notional = 0
        if py is not None and qty_y is not None:
            current_notional += abs(py * qty_y)
        if px is not None and qty_x is not None:
            current_notional += abs(px * qty_x)
        return_pct = (total / entry_notional * 100) if total is not None and entry_notional > 0 else None
        beta = abs(qty_x / qty_y) if qty_y not in (None, 0) and qty_x is not None else None
        if beta is None:
            beta = safe_float(row.get("beta"))
        z_now = None
        z_entry = None
        pair_key = str(row.get("pair_name") or "").strip().lower().replace(" / ", "_")
        try:
            payload, error = build_pair_payload(pair_key, include_series=False)
            if not error and py is not None and px is not None:
                spread_now = payload_spread(payload, py, px)
                mu, sigma = safe_float(payload.get("mu")), safe_float(payload.get("sigma"))
                if mu is not None and sigma not in (None, 0):
                    z_now = (spread_now - mu) / sigma
                if avg_y is not None and avg_x is not None:
                    spread_entry = payload_spread(payload, avg_y, avg_x)
                    if mu is not None and sigma not in (None, 0):
                        z_entry = (spread_entry - mu) / sigma
        except Exception:
            pass
        items.append({
            "kind": "pair",
            "name": str(row.get("pair_name") or "").strip(),
            "y": y,
            "x": x,
            "qty_y": qty_y,
            "qty_x": qty_x,
            "avg_y": avg_y,
            "avg_x": avg_x,
            "price_y": py,
            "price_x": px,
            "pnl_y": pnl_y,
            "pnl_x": pnl_x,
            "total": total,
            "beta": beta,
            "z_now": z_now,
            "z_entry": z_entry,
            "return_pct": return_pct,
            "current_notional": current_notional if current_notional > 0 else None,
            "exposure_pct": safe_float(row.get("exposure_pct")),
            "updated_at": max(
                [value for value in (y_price.get("updated_at"), x_price.get("updated_at")) if value],
                default=None,
            ),
            "entry_time": None if row.get("entry_time") is None else str(row.get("entry_time")),
        })

    for row in single_rows:
        symbol = str(row.get("single_name") or "").strip().upper()
        item_price = prices.get(symbol, {})
        price = safe_float(item_price.get("price"))
        avg = safe_float(row.get("avg"))
        qty = safe_float(row.get("qty"))
        pnl = (price - avg) * qty if price is not None and avg is not None and qty is not None else None
        entry_notional = abs(avg * qty) if avg is not None and qty is not None else 0
        current_notional = abs(price * qty) if price is not None and qty is not None else None
        return_pct = (pnl / entry_notional * 100) if pnl is not None and entry_notional > 0 else None
        position_type = str(row.get("position_type") or "").strip().upper()
        side = "LONG" if qty is not None and qty > 0 else "SHORT" if qty is not None and qty < 0 else None
        if side is None:
            if position_type == "LONG_SPREAD":
                side = "LONG"
            elif position_type == "SHORT_SPREAD":
                side = "SHORT"
        items.append({
            "kind": "single",
            "name": symbol,
            "symbol": symbol,
            "qty": qty,
            "avg_price": avg,
            "price": price,
            "pnl": pnl,
            "return_pct": return_pct,
            "current_notional": current_notional,
            "exposure_pct": safe_float(row.get("exposure_pct")),
            "side": side,
            "updated_at": item_price.get("updated_at"),
            "entry_time": None if row.get("entry_time") is None else str(row.get("entry_time")),
        })

    items.sort(key=lambda item: (0 if item.get("kind") == "pair" else 1, str(item.get("name") or "").upper()))
    return annotate_flagship_hidden_items({
        "ok": True,
        "source": "app_fallback",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(items),
        "account": {},
        "items": items,
        "last_event": "flagship_board.py unavailable; using last us_market_prices bar",
    })


@app.route("/api/waiting-entry")
def waiting_entry_snapshot():
    global WAITING_ENTRY_AFTER_HOURS_CACHE
    try:
        snapshot = load_waiting_entry_snapshot_from_redis()
        if snapshot is not None:
            return jsonify(snapshot)

        # Do the database fallback once per completed RTH session.  The cache
        # is keyed by that session's close, not by process lifetime, so an old
        # after-hours result cannot survive into a later trading day merely
        # because nobody opened the dashboard during RTH.
        if is_regular_market_session_open():
            with WAITING_ENTRY_AFTER_HOURS_CACHE_LOCK:
                WAITING_ENTRY_AFTER_HOURS_CACHE = None
        else:
            # This inexpensive metadata read keeps the fallback's monitored
            # names/configuration aligned with waiting_entry.py.  Prices and
            # signals are only recalculated if that configuration changed.
            rows = load_waiting_entry_rows()
            session_key = latest_closed_market_session_key()
            config_fingerprint = waiting_entry_rows_fingerprint(rows)
            with WAITING_ENTRY_AFTER_HOURS_CACHE_LOCK:
                cached = WAITING_ENTRY_AFTER_HOURS_CACHE
                if (
                    cached is not None
                    and cached.get("market_session_close") == session_key
                    and cached.get("config_fingerprint") == config_fingerprint
                ):
                    return jsonify(WAITING_ENTRY_AFTER_HOURS_CACHE)

                refresh_pairs_if_stale()
                items = [build_waiting_entry_item(row, fallback_db=True) for row in rows]
                items.sort(
                    key=lambda item: (
                        int(item.get("lock") or 0),
                        0 if item.get("kind") == "pair" else 1,
                        str(item.get("name") or "").upper(),
                    )
                )
                locked_count = sum(1 for item in items if int(item.get("lock") or 0) >= 1)
                WAITING_ENTRY_AFTER_HOURS_CACHE = {
                    "ok": True,
                    "source": "app_after_hours_fallback",
                    "count": len(items),
                    "live_count": 0,
                    "trigger_count": 0,
                    "locked_count": locked_count,
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "market_session_close": session_key,
                    "config_fingerprint": config_fingerprint,
                    "last_event": "market closed; cached per completed RTH session from us_market_prices",
                    "items": items,
                }
                return jsonify(WAITING_ENTRY_AFTER_HOURS_CACHE)

        refresh_pairs_if_stale()
        rows = load_waiting_entry_rows()
        items = [build_waiting_entry_item(row, fallback_db=True) for row in rows]
        items.sort(
            key=lambda item: (
                int(item.get("lock") or 0),
                0 if item.get("kind") == "pair" else 1,
                str(item.get("name") or "").upper(),
            )
        )
        locked_count = sum(1 for item in items if int(item.get("lock") or 0) >= 1)
        return jsonify({
            "ok": True,
            "source": "app_fallback",
            "count": len(items),
            "live_count": 0,
            "trigger_count": 0,
            "locked_count": locked_count,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_event": "waiting_entry.py unavailable; using last us_market_prices bar",
            "items": items,
        })
    except Exception as exc:
        import traceback

        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/flagship-board")
def flagship_board_snapshot():
    try:
        snapshot = load_flagship_snapshot_from_redis()
        if snapshot is not None:
            # The board snapshot already contains IB quantities and average
            # prices. Do not overwrite pair state from asynchronous DB mirrors.
            return jsonify(annotate_flagship_hidden_items(snapshot))
        return jsonify({
            "ok": True,
            "source": "flagship_board",
            "available": False,
            "stale": True,
            "updated_at": None,
            "count": 0,
            "account": {},
            "items": [],
            "last_event": "flagship_board has not published a snapshot yet",
        })
    except Exception as exc:
        import traceback

        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/flagship-hidden", methods=["GET", "POST", "OPTIONS"])
def flagship_hidden_positions():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    try:
        if request.method == "GET":
            keys = sorted(load_flagship_hidden_keys())
            return jsonify({"ok": True, "keys": keys, "count": len(keys)})

        payload = request.get_json(silent=True) or {}
        key = str(payload.get("key") or "").strip().upper()
        if not key:
            key = flagship_hidden_key(payload)
        if not key or ":" not in key:
            return jsonify({"ok": False, "error": "Missing hidden position key"}), 400

        hidden = bool(payload.get("hidden", True))
        keys = load_flagship_hidden_keys()
        if hidden:
            keys.add(key)
        else:
            keys.discard(key)
        save_flagship_hidden_keys(keys)
        return jsonify({"ok": True, "key": key, "hidden": hidden, "count": len(keys)})
    except Exception as exc:
        import traceback

        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/market-calendar")
def market_calendar():
    return jsonify(load_market_calendar())


@app.route("/api/rolling-baseline/<pair>")
def rolling_baseline(pair):
    """Return the publisher-owned fixed-beta baseline; build once on a cold start."""
    pair_key = str(pair or "").strip().lower()
    if "_" not in pair_key:
        return jsonify({"error": "invalid pair"}), 400

    snapshot = load_fixed_beta_rolling_snapshot(pair_key)
    if snapshot is None:
        snapshot, error = publish_fixed_beta_rolling_snapshot(pair_key)
        if error:
            return jsonify({"error": error}), 404
    return jsonify(snapshot)


@app.route("/api/<pair>")
def api(pair):
    try:
        item_key = str(pair or "").strip()
        test_end_raw = request.args.get("test_end")
        refresh_flag = str(request.args.get("refresh", "")).strip().lower()
        force_refresh = refresh_flag in {"1", "true", "yes", "y"}
        if "_" not in item_key and item_key.upper() in SINGLES:
            payload, error = get_cached_detail_payload(
                "single",
                item_key,
                build_single_payload,
                test_end_raw=test_end_raw,
                force_refresh=force_refresh,
            )
        else:
            payload, error = get_cached_detail_payload(
                "pair",
                item_key,
                build_pair_payload,
                test_end_raw=test_end_raw,
                force_refresh=force_refresh,
            )
        if error:
            return jsonify({"error": error})
        return jsonify(payload)
    except Exception as exc:
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(exc)})


@app.route("/api/orders/pending-zscores")
def pending_zscores():
    try:
        refresh_flag = str(request.args.get("refresh", "")).strip().lower()
        refresh_pairs_if_stale(force=refresh_flag in {"1", "true", "yes", "y"})
        items = [build_pending_zscore_item(row) for row in fetch_pending_orders()]
        return jsonify({
            "ok": True,
            "count": len(items),
            "items": items,
        })
    except Exception as exc:
        import traceback

        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/orders/history/<path:name>")
def trade_history(name):
    try:
        kind = request.args.get("kind", "pair")
        limit = normalize_history_limit(request.args.get("limit"))
        items = fetch_trade_history(name, kind, limit=limit)
        return jsonify({
            "ok": True,
            "kind": "single" if str(kind).strip().lower() == "single" else "pair",
            "name": name,
            "count": len(items),
            "items": items,
        })
    except Exception as exc:
        import traceback

        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/pairs")
def get_pairs():
    refresh_flag = str(request.args.get("refresh", "")).strip().lower()
    refresh_pairs_if_stale(force=refresh_flag in {"1", "true", "yes", "y"})

    items = build_dashboard_items()
    names = [item["name"] for item in items]
    return jsonify({
        "ok": True,
        "count": len(items),
        "pairs": names,
        "sources": {item["name"]: item.get("source") for item in items},
        "items": items,
    })


@app.route("/api/pairs/refresh", methods=["GET", "POST"])
def refresh_pairs():
    refresh_pairs_if_stale(force=True)
    items = build_dashboard_items()
    names = [item["name"] for item in items]
    return jsonify({
        "ok": True,
        "count": len(items),
        "pairs": names,
        "sources": {item["name"]: item.get("source") for item in items},
        "items": items,
    })


@app.route("/")
def index():
    return send_from_directory(".", "simple_dashboard.html")


@app.route("/<path:filename>")
def public_static_files(filename):
    """Serve only the dashboard assets; never expose project files by URL."""
    directory = PUBLIC_ASSETS.get(filename)
    if directory is None:
        return ("Not found", 404)
    return send_from_directory(directory, os.path.basename(filename))


if __name__ == "__main__":
    api_port = int(os.getenv("QUANT_API_PORT", "8000"))
    bind_host = os.getenv("QUANT_BIND_HOST", "127.0.0.1")
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1"
    if os.getenv("QUANT_ROLLING_PUBLISHER", "1") == "1":
        start_fixed_beta_rolling_publisher()
    app.run(host=bind_host, port=api_port, debug=debug_mode, use_reloader=False)
