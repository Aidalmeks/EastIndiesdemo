import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import json
import os
import math
import time
from datetime import datetime, time as datetime_time, timezone

import pandas as pd
import pymysql
import redis
import requests
from ib_insync import IB
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from Action.Manual_Single_Open import manual_place_single_order
from program_factory import pdt
from program_factory.CombosLib import place_pair_order
from program_factory.order_scheduling import create_pair_scheduled_order, create_single_scheduled_order
from program_factory.port_listener import choose_connection
from program_factory.trade_event_log import log_trade_event
from program_factory.live_bars import (
    BAR_CHANNEL,
    BAR_SECONDS,
    load_bar_history,
    load_latest_aligned_bars,
)
from program_factory.market_schedule import NY_TZ
from runtime_settings import api_base_url, database_config, ib_port_account_map, redis_config

# ===============================
# CONFIG
# ===============================
BASE_URL = api_base_url()
PARAMS_TIMEOUT_SECONDS = 8
# A just-closed live bar can arrive a few seconds before FillDB persists the
# corresponding us_market_prices row.  Retry briefly until the API baseline
# catches up; do not use a process-start 5-minute timer.
ROLLING_PARAMS_RETRY_SECONDS = 10
ROLLING_PARAMS_FAILSAFE_REFRESH_SECONDS = 15 * 60

MEMORY_DB_CONFIG = database_config("memory")
DATA_DB_CONFIG = database_config("data")

CLIENT_ID = 10
PREFERRED_PORT_ORDER = (4001, 4002)

PORT_ACCOUNT_MAP = ib_port_account_map()
selection = choose_connection(
    port_account_map=PORT_ACCOUNT_MAP,
    preferred_order=PREFERRED_PORT_ORDER
)
CONNECT_PORT = selection.port
TARGET_ACCOUNT = selection.account
print(
    f"[PORT] selected={CONNECT_PORT}, account={TARGET_ACCOUNT}, pid={selection.pid}, "
    f"process={selection.process_name}, reason={selection.reason}"
)

DEFAULT_TRIGGER_Z = 2.0
LIVE_BAR_CHANNEL = BAR_CHANNEL
WAITING_ENTRY_SNAPSHOT_KEY = "dashboard:waiting_entry"
WAITING_ENTRY_SNAPSHOT_TTL_SECONDS = 10
REENTRY_PREV_SIGNAL_BUFFER = 1e-4

r = redis.Redis(**redis_config())
ib = IB()

LAST_EVENT = "monitoring"
REENTRY_TRIGGER_COUNT = 0
ORDERS_PLACED_COUNT = 0
PAIR_LATEST_BAR_CACHE = {}
PAIR_LATEST_BAR_CACHE_TTL_SECONDS = 10.0
SINGLE_LOG_SESSION_SEED_CACHE = {}


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def set_event(msg):
    global LAST_EVENT
    LAST_EVENT = msg
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def db_connect(dict_cursor=False):
    kwargs = dict(MEMORY_DB_CONFIG)
    if dict_cursor:
        kwargs["cursorclass"] = pymysql.cursors.DictCursor
    return pymysql.connect(**kwargs)


def data_db_connect(dict_cursor=False):
    kwargs = {
        **DATA_DB_CONFIG,
        "connect_timeout": 2,
        "read_timeout": 2,
        "write_timeout": 2,
    }
    if dict_cursor:
        kwargs["cursorclass"] = pymysql.cursors.DictCursor
    return pymysql.connect(**kwargs)


def normalize_threshold(value):
    try:
        threshold = float(value)
        return threshold if threshold > 0 else DEFAULT_TRIGGER_Z
    except (TypeError, ValueError):
        return DEFAULT_TRIGGER_Z


# ===============================
# Reentry / Orders DB
# ===============================
def load_reentry_pairs():
    conn = db_connect(dict_cursor=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    pr.pair_name,
                    pr.train_start,
                    pr.split_time,
                    pr.strategy,
                    pr.param,
                    pr.intercept,
                    pr.residual,
                    COALESCE(pr.reentry_lock, 0) AS reentry_lock,
                    pr.threshold,
                    COALESCE(pr.auto_schedule_enabled, 0) AS auto_schedule_enabled,
                    pr.exposure_pct AS auto_exposure_pct,
                    pr.order_position_type AS auto_order_position_type,
                    lrc.train_start AS log_train_start,
                    lrc.split_time AS log_split_time,
                    lrc.k AS log_k,
                    lrc.intercept AS log_intercept,
                    lrc.threshold AS log_threshold
                FROM pair_reentry pr
                LEFT JOIN log_residual_config lrc
                  ON UPPER(lrc.stock_name) = UPPER(pr.pair_name)
                """
            )
            rows = cursor.fetchall()
    finally:
        conn.close()

    configs = {}
    for row in rows:
        pair_name = row["pair_name"]
        strategy = None if row.get("strategy") is None else str(row.get("strategy")).strip().upper()
        is_single_log = "_" not in str(pair_name or "") and strategy in {"LOG_RESIDUAL", "SINGLE_LOG_RESIDUAL"}

        configs[pair_name] = {
            "pair_name": pair_name,
            "train_start": row.get("log_train_start") if is_single_log else row.get("train_start"),
            "split_time": row.get("log_split_time") if is_single_log else row.get("split_time"),
            "strategy": strategy,
            "param": row.get("log_k") if is_single_log else row.get("param"),
            "intercept": row.get("log_intercept") if is_single_log else row.get("intercept"),
            "residual": row.get("log_threshold") if is_single_log else row.get("residual"),
            "reentry_lock": int(row["reentry_lock"] or 0),
            "threshold": normalize_threshold(row.get("threshold")),
            "auto_schedule_enabled": int(row.get("auto_schedule_enabled") or 0),
            "auto_exposure_pct": safe_float(row.get("auto_exposure_pct")),
            "auto_order_position_type": normalize_position_type(row.get("auto_order_position_type")),
        }

    return configs


def get_reentry_lock(pair_name):
    conn = db_connect(dict_cursor=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(reentry_lock, 0) AS reentry_lock
                FROM pair_reentry
                WHERE pair_name = %s
                """,
                (pair_name,),
            )
            row = cursor.fetchone()
            return int(row["reentry_lock"] or 0) if row else 0
    finally:
        conn.close()


def set_reentry_lock(pair_name, lock_value):
    conn = db_connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pair_reentry
                SET reentry_lock = %s
                WHERE pair_name = %s
                """,
                (int(lock_value), pair_name),
            )
        conn.commit()
    finally:
        conn.close()


def load_pending_orders_for_pair(pair_name):
    conn = db_connect(dict_cursor=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, pair_name, beta, exposure_pct, position_type
                FROM scheduled_orders
                WHERE status = 'PENDING'
                  AND pair_name = %s
                ORDER BY id ASC
                """,
                (pair_name,),
            )
            return cursor.fetchall()
    finally:
        conn.close()


def normalize_position_type(value):
    return str(value or "").strip().upper()


def is_auto_position_type(value):
    return normalize_position_type(value) == "AUTO_MODE"


def infer_reentry_position_type(z_prev, z_now):
    signal_z = z_now if z_now != 0 else z_prev
    return "LONG_SPREAD" if signal_z < 0 else "SHORT_SPREAD"


def compute_pair_two_sigma_pct(cfg, row):
    sigma = safe_float((cfg or {}).get("sigma"))
    beta = safe_float((cfg or {}).get("beta"))
    px = safe_float((row or {}).get("px"))
    py = safe_float((row or {}).get("py"))
    if sigma is None or beta is None or px is None or py is None:
        return None

    # Gross leg notional: beta's sign determines the direction of the X leg,
    # not whether its capital requirement cancels the Y leg.  This matches
    # Quant Dashboard's 2 Sigma Return display and scheduled_orders values.
    denominator = py + abs(beta) * px
    if not math.isfinite(denominator) or math.isclose(denominator, 0.0, abs_tol=1e-12):
        return None
    return (2.0 * sigma) / denominator * 100.0


def build_auto_pair_order_payload(pair_name, reentry_cfg, cfg, row, target_position_type):
    if not int((reentry_cfg or {}).get("auto_schedule_enabled") or 0):
        return None, "auto_schedule_disabled"

    exposure_pct = safe_float((reentry_cfg or {}).get("auto_exposure_pct"))
    if exposure_pct is None or exposure_pct <= 0:
        return None, "missing auto exposure_pct"

    beta = safe_float((cfg or {}).get("beta"))
    if beta is None:
        return None, "missing beta"

    two_sigma = compute_pair_two_sigma_pct(cfg, row)
    if two_sigma is None:
        return None, "missing two_sigma"

    position_type = normalize_position_type((reentry_cfg or {}).get("auto_order_position_type")) or "AUTO_MODE"
    return {
        "pair_name": pair_name,
        "train_start": (reentry_cfg or {}).get("train_start"),
        "split_time": (reentry_cfg or {}).get("split_time"),
        "two_sigma": two_sigma,
        "position_type": position_type,
        "beta": beta,
        "exposure_pct": exposure_pct,
        "target_position_type": target_position_type,
    }, None


def create_auto_pair_pending_order(pair_name, reentry_cfg, cfg, row, target_position_type):
    payload, error = build_auto_pair_order_payload(
        pair_name=pair_name,
        reentry_cfg=reentry_cfg,
        cfg=cfg,
        row=row,
        target_position_type=target_position_type,
    )
    if error:
        set_event(f"{pair_name}: auto schedule skipped: {error}")
        return None

    try:
        result = create_pair_scheduled_order(
            pair_name=payload["pair_name"],
            train_start=payload["train_start"],
            split_time=payload["split_time"],
            two_sigma=payload["two_sigma"],
            position_type=payload["position_type"],
            beta=payload["beta"],
            exposure_pct=payload["exposure_pct"],
        )
    except pymysql.err.IntegrityError as exc:
        set_event(f"{pair_name}: auto schedule found existing active order: {exc}")
        return None

    set_event(
        f"{pair_name}: auto PENDING created id={result['order_id']} "
        f"two_sigma={payload['two_sigma']:.4f} beta={payload['beta']:.4f} "
        f"exposure={payload['exposure_pct']:.2f}% mode={payload['position_type']}"
    )
    return result


def resolve_auto_single_position_type(reentry_cfg, target_position_type):
    configured = normalize_position_type((reentry_cfg or {}).get("auto_order_position_type"))
    if configured in {"LONG_SPREAD", "SHORT_SPREAD"}:
        return configured
    return target_position_type


def create_auto_single_pending_order(single_name, reentry_cfg, strategy, param, target_position_type):
    if not int((reentry_cfg or {}).get("auto_schedule_enabled") or 0):
        set_event(f"{single_name}: auto single schedule skipped: auto_schedule_disabled")
        return None

    exposure_pct = safe_float((reentry_cfg or {}).get("auto_exposure_pct"))
    if exposure_pct is None or exposure_pct <= 0:
        set_event(f"{single_name}: auto single schedule skipped: missing auto exposure_pct")
        return None

    order_param = safe_float(param)
    if order_param is None:
        set_event(f"{single_name}: auto single schedule skipped: missing order param")
        return None

    position_type = resolve_auto_single_position_type(reentry_cfg, target_position_type)
    try:
        result = create_single_scheduled_order(
            single_name=single_name,
            train_start=(reentry_cfg or {}).get("train_start"),
            split_time=(reentry_cfg or {}).get("split_time"),
            strategy=str(strategy or "").strip().upper(),
            param=order_param,
            position_type=position_type,
            exposure_pct=exposure_pct,
        )
    except pymysql.err.IntegrityError as exc:
        set_event(f"{single_name}: auto single schedule found existing active order: {exc}")
        return None

    set_event(
        f"{single_name}: auto single PENDING created id={result['order_id']} "
        f"strategy={result['strategy']} param={order_param:.4f} "
        f"exposure={exposure_pct:.2f}% mode={position_type}"
    )
    return result


def update_order_position_type(order_id, position_type):
    conn = db_connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE scheduled_orders
                SET position_type = %s
                WHERE id = %s AND status = 'PENDING'
                """,
                (position_type, order_id),
            )
            updated = cursor.rowcount
        conn.commit()
        return updated > 0
    finally:
        conn.close()


def load_pending_single_orders(single_name):
    conn = db_connect(dict_cursor=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, single_name, position_type, exposure_pct
                FROM single_scheduled_orders
                WHERE status = 'PENDING'
                  AND single_name = %s
                ORDER BY id ASC
                """,
                (str(single_name or "").strip().lower(),),
            )
            return cursor.fetchall()
    finally:
        conn.close()


def mark_order_executed(order_id):
    conn = db_connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE scheduled_orders
                SET status = 'HOLDING'
                WHERE id = %s AND status = 'PENDING'
                """,
                (order_id,),
            )
            updated = cursor.rowcount
        conn.commit()
        return updated > 0
    finally:
        conn.close()


# ===============================
# Params / Market data
# ===============================
def fetch_params_from_api(pair, force_refresh=False):
    try:
        # app.py's publisher is the sole rolling-baseline calculator.  This
        # endpoint is a compact Redis snapshot, not a per-consumer recompute.
        url = f"{BASE_URL}/rolling-baseline/{pair}"
        resp = requests.get(url, timeout=PARAMS_TIMEOUT_SECONDS)

        if resp.status_code != 200:
            set_event(f"fetch_params {pair}: HTTP {resp.status_code}")
            return None

        data = resp.json()
        if "error" in data:
            set_event(f"fetch_params {pair}: {data.get('error')}")
            return None

        # Fixed-beta pairs use spread = Y - beta*X.  The dashboard returns the
        # current causal rolling baseline in rolling_mean / rolling_sigma, so
        # there is no regression intercept to add.
        if data.get("calculation") == "fixed_beta_rolling":
            beta = safe_float(data.get("fixed_beta", data.get("beta")))
            mu = safe_float(data.get("rolling_mean", data.get("mu")))
            sigma = safe_float(data.get("rolling_sigma", data.get("sigma")))
            if beta is None or mu is None or sigma is None or sigma <= 0:
                set_event(f"fetch_params {pair}: invalid fixed-beta rolling values")
                return None
            return {
                "pair": pair,
                "calculation": "fixed_beta_rolling",
                "beta": beta,
                "alpha": 0.0,
                "mu": mu,
                "sigma": sigma,
                "x_label": data["x_label"],
                "y_label": data["y_label"],
                "baseline_bar_time_bj": data.get("bar_time_bj") or (data.get("last_bar") or {}).get("time"),
            }

        return {
            "pair": pair,
            "calculation": "legacy_train_segment",
            "beta": data["beta"],
            "alpha": data["alpha"],
            "mu": data["mu"],
            "sigma": data["sigma"],
            "x_label": data["x_label"],
            "y_label": data["y_label"],
            "baseline_bar_time_bj": (data.get("last_bar") or {}).get("time"),
        }
    except Exception as e:
        set_event(f"fetch_params failed {pair}: {e}")
        return None


def fetch_params(pair, reentry_cfg=None, force_refresh=False):
    # Live trading may consume model parameters produced by research, but it
    # must not silently rebuild them from an incomplete live-price database.
    return fetch_params_from_api(pair, force_refresh=force_refresh)


def compute_5min_bar_z(cfg):
    """Compute a pair signal from the latest aligned, completed 5-minute bar."""
    pair = cfg["pair"]

    x_symbol = str(cfg["x_label"]).upper()
    y_symbol = str(cfg["y_label"]).upper()
    latest_bar = load_latest_aligned_pair_bar(y_symbol, x_symbol)
    px = latest_bar.get("px")
    py = latest_bar.get("py")
    bar_time_utc = latest_bar.get("bar_time_utc")

    if px is None or py is None:
        return {
            "pair_key": pair,
            "pair": pair.upper(),
            "z": None,
            "px": px,
            "py": py,
            "rolling_mean": safe_float(cfg.get("mu")),
            "rolling_sigma": safe_float(cfg.get("sigma")),
            "bar_time_utc": bar_time_utc,
        }

    z = compute_pair_z_value(cfg, py, px)

    if z is None:
        return {
            "pair_key": pair,
            "pair": pair.upper(),
            "z": None,
            "px": px,
            "py": py,
            "rolling_mean": safe_float(cfg.get("mu")),
            "rolling_sigma": safe_float(cfg.get("sigma")),
            "bar_time_utc": bar_time_utc,
        }

    return {
        "pair_key": pair,
        "pair": pair.upper(),
        "z": z,
        "px": px,
        "py": py,
        "rolling_mean": safe_float(cfg.get("mu")),
        "rolling_sigma": safe_float(cfg.get("sigma")),
        "bar_time_utc": bar_time_utc,
    }


def compute_pair_z_value(cfg, py, px):
    py = safe_float(py)
    px = safe_float(px)
    sigma = safe_float(cfg.get("sigma"))
    if py is None or px is None or sigma in (None, 0):
        return None
    spread = py - (float(cfg["beta"]) * px + float(cfg["alpha"]))
    return (spread - float(cfg["mu"])) / sigma


def compute_realtime_z(cfg):
    """Compute the dashboard signal from live ticks; never used for entry timing."""
    pair = cfg["pair"]
    x_symbol = str(cfg["x_label"]).upper()
    y_symbol = str(cfg["y_label"]).upper()

    px_raw = r.get(f"price:{x_symbol}")
    py_raw = r.get(f"price:{y_symbol}")
    ts_x = safe_float(r.get(f"ts:{x_symbol}"))
    ts_y = safe_float(r.get(f"ts:{y_symbol}"))

    if px_raw is None or py_raw is None:
        px_raw = px_raw if px_raw is not None else r.get(f"{pair}:{cfg['x_label']}_price")
        py_raw = py_raw if py_raw is not None else r.get(f"{pair}:{cfg['y_label']}_price")

    px = safe_float(px_raw)
    py = safe_float(py_raw)
    tick_ts = max((ts for ts in (ts_x, ts_y) if ts is not None), default=None)
    row = {
        "pair_key": pair,
        "pair": pair.upper(),
        "z": None,
        "px": px,
        "py": py,
        "tick_ts": tick_ts,
        "tick_ts_x": ts_x,
        "tick_ts_y": ts_y,
        # ``mu`` and ``sigma`` come from app.py's fixed-beta rolling baseline.
        # Include them in the published snapshot so the Home page shows the
        # actual 5-minute rolling mean rather than the old AVG78 placeholder.
        "rolling_mean": safe_float(cfg.get("mu")),
        "rolling_sigma": safe_float(cfg.get("sigma")),
    }
    if px is None or py is None or cfg["sigma"] == 0:
        return row

    spread = py - (cfg["beta"] * px + cfg["alpha"])
    row["z"] = (spread - cfg["mu"]) / cfg["sigma"]
    return row


def load_latest_aligned_pair_bar(y_symbol, x_symbol):
    """Return the newest aligned bars built exclusively from live ticks."""
    cache_key = (str(y_symbol).upper(), str(x_symbol).upper())
    cached = PAIR_LATEST_BAR_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < PAIR_LATEST_BAR_CACHE_TTL_SECONDS:
        return dict(cached[1])

    y_bar, x_bar = load_latest_aligned_bars(r, y_symbol, x_symbol)
    result = {
        "bar_time_utc": (
            datetime.utcfromtimestamp(y_bar["start_ts"])
            if y_bar and x_bar
            else None
        ),
        "py": safe_float(y_bar.get("close")) if y_bar else None,
        "px": safe_float(x_bar.get("close")) if x_bar else None,
    }
    PAIR_LATEST_BAR_CACHE[cache_key] = (time.monotonic(), result)
    return dict(result)


def completed_pair_bar_time_bj(cfg):
    """Return the latest common completed live bar in the API's BJ format."""
    latest_bar = load_latest_aligned_pair_bar(cfg["y_label"], cfg["x_label"])
    bar_time_utc = latest_bar.get("bar_time_utc")
    if bar_time_utc is None or pd.isna(bar_time_utc):
        return None
    timestamp = pd.Timestamp(bar_time_utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")


def load_preceding_aligned_pair_bar(y_symbol, x_symbol, before_time_utc):
    """Load the one archival predecessor used to seed the first live crossing."""
    conn = data_db_connect(dict_cursor=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT y.time_utc AS bar_time_utc, y.price AS py, x.price AS px
                FROM us_market_prices y FORCE INDEX (idx_us_market_prices_ticker_time_price)
                JOIN us_market_prices x FORCE INDEX (idx_us_market_prices_ticker_time_price)
                  ON x.ticker = %s
                 AND x.time_utc = y.time_utc
                WHERE y.ticker = %s
                  AND y.time_utc < %s
                  AND y.price IS NOT NULL
                  AND x.price IS NOT NULL
                ORDER BY y.time_utc DESC
                LIMIT 1
                """,
                (
                    str(x_symbol).upper(),
                    str(y_symbol).upper(),
                    pd.Timestamp(before_time_utc).to_pydatetime(),
                ),
            )
            row = cursor.fetchone() or {}
    finally:
        conn.close()
    return {
        "bar_time_utc": row.get("bar_time_utc"),
        "py": safe_float(row.get("py")),
        "px": safe_float(row.get("px")),
    }


def load_predecessor_z(cfg, live_bar_time_utc):
    predecessor = load_preceding_aligned_pair_bar(
        cfg["y_label"],
        cfg["x_label"],
        live_bar_time_utc,
    )
    return compute_pair_z_value(cfg, predecessor["py"], predecessor["px"]), predecessor


def is_single_log_residual_config(pair_name, cfg):
    strategy = str(cfg.get("strategy") or "").strip().upper()
    return "_" not in str(pair_name or "") and strategy in {"LOG_RESIDUAL", "SINGLE_LOG_RESIDUAL"}


def is_single_new_high_config(pair_name, cfg):
    strategy = str(cfg.get("strategy") or "").strip().upper()
    return "_" not in str(pair_name or "") and strategy in {"NEW_HIGH", "NEW HIGH"}


def safe_float(value):
    try:
        if value is None:
            return None
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def signal_side(value):
    return -1.0 if float(value) < 0 else 1.0


def cooldown_prev_z(z_prev, z_now, threshold):
    threshold = normalize_threshold(threshold)
    if z_now is None or abs(float(z_now)) > threshold:
        return z_now

    reference = z_prev if z_prev is not None else z_now
    return signal_side(reference) * (threshold + REENTRY_PREV_SIGNAL_BUFFER)


def is_take_profit_cooling(pair_name):
    try:
        return pdt.take_profit_cooldown_remaining_seconds(pair_name, is_single=False) > 0
    except Exception as exc:
        set_event(f"{pair_name}: take-profit cooldown check skipped: {exc}")
        return False


def open_live_bar_pubsub():
    try:
        pubsub = r.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(LIVE_BAR_CHANNEL)
        return pubsub
    except Exception as exc:
        set_event(f"live bar event subscribe failed: {exc}")
        return None


def drain_live_bar_events(pubsub):
    if pubsub is None:
        return

    while True:
        try:
            message = pubsub.get_message(timeout=0)
        except Exception as exc:
            set_event(f"live bar event read failed: {exc}")
            return

        if not message:
            break
        if message.get("type") != "message":
            continue

        try:
            payload = json.loads(message.get("data") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue

        ticker = str(payload.get("ticker") or "").strip().upper()
        if not ticker:
            continue

        for cache_key in list(PAIR_LATEST_BAR_CACHE):
            if ticker in cache_key:
                PAIR_LATEST_BAR_CACHE.pop(cache_key, None)


def get_redis_tick_price(ticker):
    ticker = ticker.upper()
    price_raw = r.get(f"price:{ticker}")
    ts_raw = r.get(f"ts:{ticker}")
    price = safe_float(price_raw)
    ts = safe_float(ts_raw)
    return price, ts


def current_rth_session_open_utc(tick_ts):
    """Return today's 09:30 New York open for a live RTH tick."""
    if tick_ts is None:
        return None
    tick_ny = datetime.fromtimestamp(float(tick_ts), timezone.utc).astimezone(NY_TZ)
    open_ny = NY_TZ.localize(datetime.combine(tick_ny.date(), datetime_time(9, 30)))
    close_ny = NY_TZ.localize(datetime.combine(tick_ny.date(), datetime_time(16, 0)))
    if not (open_ny <= tick_ny < close_ny):
        return None
    return open_ny.astimezone(timezone.utc).replace(tzinfo=None)


def load_single_log_session_seed(ticker, train_start, session_open_utc):
    """Load the model's pre-session index anchor and preceding price window once."""
    if train_start is None or pd.isna(train_start):
        return None, "missing train_start"

    train_start_utc = (pd.Timestamp(train_start) - pd.Timedelta(hours=8)).to_pydatetime()
    cache_key = (ticker, train_start_utc, session_open_utc)
    cached = SINGLE_LOG_SESSION_SEED_CACHE.get(cache_key)
    if cached is not None:
        return cached, None

    conn = data_db_connect(dict_cursor=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS n
                FROM us_market_prices
                WHERE ticker = %s
                  AND price IS NOT NULL
                  AND time_utc >= %s
                  AND time_utc < %s
                """,
                (ticker, train_start_utc, session_open_utc),
            )
            base_index = int((cursor.fetchone() or {}).get("n") or 0)
            cursor.execute(
                """
                SELECT time_utc, price
                FROM (
                    SELECT time_utc, price
                    FROM us_market_prices
                    WHERE ticker = %s
                      AND price IS NOT NULL
                      AND time_utc >= %s
                      AND time_utc < %s
                    ORDER BY time_utc DESC
                    LIMIT 78
                ) preceding
                ORDER BY time_utc
                """,
                (ticker, train_start_utc, session_open_utc),
            )
            rows = cursor.fetchall()
    finally:
        conn.close()

    first_index = base_index - len(rows)
    seed_rows = []
    for offset, row in enumerate(rows):
        price = safe_float(row.get("price"))
        if price is None or price <= 0 or row.get("time_utc") is None:
            continue
        seed_rows.append(
            {
                "time_utc": pd.Timestamp(row["time_utc"]),
                "price": price,
                "bar_index": first_index + offset,
            }
        )

    seed = {"base_index": base_index, "rows": seed_rows}
    SINGLE_LOG_SESSION_SEED_CACHE[cache_key] = seed
    return seed, None


def build_single_log_live_window(ticker, cfg, k, intercept, tick_ts):
    """Combine the archival predecessor with today's time-indexed live bars."""
    session_open_utc = current_rth_session_open_utc(tick_ts)
    if session_open_utc is None:
        return None, "no current RTH tick"

    seed, seed_error = load_single_log_session_seed(
        ticker,
        cfg.get("train_start"),
        session_open_utc,
    )
    if seed_error:
        return None, seed_error

    session_open_ts = int(session_open_utc.replace(tzinfo=timezone.utc).timestamp())
    by_start_ts = {
        int(
            pd.Timestamp(row["time_utc"])
            .to_pydatetime()
            .replace(tzinfo=timezone.utc)
            .timestamp()
        ): dict(row)
        for row in seed["rows"]
    }
    for bar in load_bar_history(r, ticker):
        start_ts = int(bar["start_ts"])
        if start_ts < session_open_ts:
            continue
        slot = (start_ts - session_open_ts) // BAR_SECONDS
        if slot < 0 or slot >= 78:
            continue
        by_start_ts[start_ts] = {
            "time_utc": pd.Timestamp(datetime.fromtimestamp(start_ts, timezone.utc).replace(tzinfo=None)),
            "price": float(bar["close"]),
            "bar_index": int(seed["base_index"] + slot),
        }

    rows = [by_start_ts[key] for key in sorted(by_start_ts)][-78:]
    residuals = [
        math.log(float(row["price"]))
        - (float(intercept) + float(k) * int(row["bar_index"]))
        for row in rows
        if float(row["price"]) > 0
    ]
    if len(residuals) < 78:
        return None, f"live window incomplete ({len(residuals)}/78)"

    tick_slot = int((float(tick_ts) - session_open_ts) // BAR_SECONDS)
    if tick_slot < 0 or tick_slot >= 78:
        return None, "tick outside RTH slots"
    return {
        "avg78_residual": sum(residuals[-78:]) / 78,
        "bars": 78,
        "bar_index": int(seed["base_index"] + tick_slot),
        "latest_time_utc": rows[-1]["time_utc"],
    }, None


def compute_single_log_residual(pair_name, cfg):
    ticker = str(pair_name or "").strip().upper()
    k = safe_float(cfg.get("param"))
    intercept = safe_float(cfg.get("intercept"))
    entry_residual = safe_float(cfg.get("residual"))
    tick_price, tick_ts = get_redis_tick_price(ticker) if ticker else (None, None)

    row_base = {
        "kind": "single_log_residual",
        "pair_key": pair_name,
        "pair": ticker,
        "reentry_lock": int(cfg.get("reentry_lock") or 0),
        "threshold": entry_residual,
        "z": None,
        "px": tick_price,
        "py": None,
        "tick_price": tick_price,
        "tick_ts": tick_ts,
        "current_residual": None,
        "avg78_residual": None,
        "bars": 0,
        "error": None,
        "train_start": cfg.get("train_start"),
        "split_time": cfg.get("split_time"),
        "auto_schedule_enabled": int(cfg.get("auto_schedule_enabled") or 0),
        "auto_exposure_pct": safe_float(cfg.get("auto_exposure_pct")),
        "auto_order_position_type": normalize_position_type(cfg.get("auto_order_position_type")),
    }

    if not ticker or k is None or intercept is None or entry_residual is None:
        row_base["error"] = "missing model params"
        return row_base

    if tick_price is None or tick_price <= 0 or tick_ts is None:
        return row_base

    try:
        window, window_error = build_single_log_live_window(
            ticker,
            cfg,
            k,
            intercept,
            tick_ts,
        )
    except Exception as exc:
        row_base["error"] = f"live window failed: {exc}"
        return row_base
    if window_error:
        row_base["error"] = window_error
        return row_base

    current_bar_index = int(window["bar_index"])
    row_base.update(
        {
            "current_residual": math.log(tick_price)
            - (intercept + k * current_bar_index),
            "avg78_residual": float(window["avg78_residual"]),
            "bars": int(window["bars"]),
            "bar_index": current_bar_index,
            "bar_index_mode": "rth_session_time",
            "avg78_time_bj": pd.Timestamp(window["latest_time_utc"])
            + pd.Timedelta(hours=8),
        }
    )
    return row_base

def compute_single_new_high(pair_name, cfg):
    ticker = str(pair_name or "").strip().upper()
    trigger_price = safe_float(cfg.get("threshold"))
    if trigger_price is None:
        trigger_price = safe_float(cfg.get("param"))

    tick_price, tick_ts = get_redis_tick_price(ticker) if ticker else (None, None)
    return {
        "kind": "single_new_high",
        "pair_key": pair_name,
        "pair": ticker,
        "reentry_lock": int(cfg.get("reentry_lock") or 0),
        "threshold": trigger_price,
        "z": None,
        "px": tick_price,
        "py": None,
        "tick_price": tick_price,
        "tick_ts": tick_ts,
        "current_residual": None,
        "avg78_residual": None,
        "bars": 0,
        "error": None if trigger_price is not None else "missing trigger price",
        "train_start": cfg.get("train_start"),
        "split_time": cfg.get("split_time"),
        "auto_schedule_enabled": int(cfg.get("auto_schedule_enabled") or 0),
        "auto_exposure_pct": safe_float(cfg.get("auto_exposure_pct")),
        "auto_order_position_type": normalize_position_type(cfg.get("auto_order_position_type")),
    }


# ===============================
# IB / Execution
# ===============================
def ensure_ib_connected():
    if ib.isConnected():
        return

    ib.connect("127.0.0.1", CONNECT_PORT, clientId=CLIENT_ID)
    set_event(f"IB connected: port={CONNECT_PORT} account={TARGET_ACCOUNT}")


def collect_account_positions():
    ensure_ib_connected()
    positions_by_symbol = {}
    for pos in ib.positions():
        if getattr(pos, "account", None) != TARGET_ACCOUNT:
            continue
        symbol = str(pos.contract.symbol or "").strip().upper()
        if not symbol:
            continue
        positions_by_symbol[symbol] = {
            "qty": pos.position,
            "avg_price": pos.avgCost,
        }
    return positions_by_symbol


def fetch_usdcnh():
    try:
        res = requests.get("https://open.er-api.com/v6/latest/USD", timeout=3)
        data = res.json()
        return float(data["rates"]["CNH"])
    except Exception:
        return 6.89


def get_net_liq_usd():
    account_values = ib.accountSummary()

    net_liq_native = None
    for v in account_values:
        if v.tag == "NetLiquidation" and v.account == TARGET_ACCOUNT:
            net_liq_native = float(v.value)
            break

    if net_liq_native is None:
        raise RuntimeError(f"Cannot get NetLiquidation for {TARGET_ACCOUNT}")

    if CONNECT_PORT == 4001:
        return net_liq_native

    fx = fetch_usdcnh()
    return net_liq_native / fx


def try_reentry_trigger(pair_name, z_prev, z_now, threshold, reentry_cfg=None, cfg=None, row=None):
    global REENTRY_TRIGGER_COUNT, ORDERS_PLACED_COUNT

    crossed = abs(z_prev) > threshold and abs(z_now) <= threshold
    if not crossed:
        return

    lock_value = get_reentry_lock(pair_name)
    if lock_value >= 1:
        set_event(
            f"{pair_name}: crossed threshold {threshold:.2f} |z| {abs(z_prev):.2f}->{abs(z_now):.2f} but locked ({lock_value})"
        )
        return

    target_position_type = infer_reentry_position_type(z_prev, z_now)
    pending_orders = load_pending_orders_for_pair(pair_name)
    if not pending_orders:
        set_event(f"{pair_name}: crossed threshold {threshold:.2f} but no PENDING orders")
        create_auto_pair_pending_order(
            pair_name=pair_name,
            reentry_cfg=reentry_cfg or {},
            cfg=cfg or {},
            row=row or {},
            target_position_type=target_position_type,
        )
        pending_orders = load_pending_orders_for_pair(pair_name)
        if not pending_orders:
            set_event(f"{pair_name}: no executable PENDING orders after auto schedule check")
            return

    set_event(
        f"{pair_name}: trigger fired threshold {threshold:.2f} |z| {abs(z_prev):.2f}->{abs(z_now):.2f}, target={target_position_type}, pending={len(pending_orders)}"
    )
    REENTRY_TRIGGER_COUNT += 1

    ensure_ib_connected()
    net_liq_usd = get_net_liq_usd()

    any_placed = False

    for order in pending_orders:
        order_id = int(order["id"])
        current_position_type = normalize_position_type(order.get("position_type"))
        if current_position_type != target_position_type:
            if not is_auto_position_type(current_position_type):
                set_event(
                    f"{pair_name}: order id={order_id} explicit direction {current_position_type or 'N/A'} "
                    f"does not match trigger target {target_position_type}; skipped"
                )
                continue
            if update_order_position_type(order_id, target_position_type):
                set_event(
                    f"{pair_name}: order id={order_id} AUTO_MODE resolved -> {target_position_type}"
                )
                order["position_type"] = target_position_type
            else:
                set_event(f"{pair_name}: order id={order_id} AUTO_MODE resolve failed")
                continue

        cfg = {
            "beta": float(order["beta"]),
            "exposure_pct": float(order["exposure_pct"]),
            "position_type": normalize_position_type(order.get("position_type")) or target_position_type,
        }

        try:
            place_pair_order(
                ib=ib,
                pair_name=pair_name,
                cfg=cfg,
                net_liq_usd=net_liq_usd,
                account=TARGET_ACCOUNT,
            )

            if mark_order_executed(order_id):
                ORDERS_PLACED_COUNT += 1
                any_placed = True
                set_event(f"{pair_name}: order id={order_id} set HOLDING")
                log_trade_event({
                    "event": "OPEN",
                    "kind": "pair",
                    "pair_name": pair_name,
                    "beta": cfg.get("beta"),
                    "exposure_pct": cfg.get("exposure_pct"),
                    "entry_z": None if z_now is None else float(z_now),
                })
            else:
                set_event(f"{pair_name}: order id={order_id} not updated (already changed)")
        except Exception as e:
            set_event(f"{pair_name}: order id={order_id} failed: {e}")

    if any_placed:
        set_reentry_lock(pair_name, 1)
        set_event(f"{pair_name}: reentry_lock set to 1")


def try_single_log_residual_trigger(row):
    global REENTRY_TRIGGER_COUNT, ORDERS_PLACED_COUNT

    pair_name = str(row.get("pair_key") or "").strip()
    ticker = str(row.get("pair") or pair_name).strip().upper()
    current_residual = row.get("current_residual")
    avg78_residual = row.get("avg78_residual")
    entry_residual = row.get("threshold")

    if current_residual is None or avg78_residual is None or entry_residual is None:
        return

    long_triggered = current_residual >= entry_residual and avg78_residual < entry_residual
    short_triggered = current_residual <= entry_residual and avg78_residual > entry_residual
    if not long_triggered and not short_triggered:
        return

    lock_value = get_reentry_lock(pair_name)
    if lock_value >= 1:
        set_event(
            f"{ticker}: single signal current={current_residual:+.5f}, avg78={avg78_residual:+.5f}, "
            f"entry={entry_residual:+.5f} but locked ({lock_value})"
        )
        return

    target_position_types = set()
    if long_triggered:
        target_position_types.add("LONG_SPREAD")
    if short_triggered:
        target_position_types.add("SHORT_SPREAD")

    pending_orders = load_pending_single_orders(ticker)
    if not pending_orders:
        create_auto_single_pending_order(
            single_name=ticker,
            reentry_cfg=row,
            strategy="LOG_RESIDUAL",
            param=current_residual,
            target_position_type=next(iter(target_position_types)),
        )
        pending_orders = load_pending_single_orders(ticker)

    if not pending_orders:
        set_event(f"{ticker}: single signal fired but no PENDING single orders")
        return

    pending_orders = [
        order
        for order in pending_orders
        if normalize_position_type(order.get("position_type")) in target_position_types
    ]
    if not pending_orders:
        set_event(
            f"{ticker}: single signal fired but no matching PENDING single orders "
            f"for {','.join(sorted(target_position_types))}"
        )
        return

    set_event(
        f"{ticker}: single trigger fired current={current_residual:+.5f}, "
        f"avg78={avg78_residual:+.5f}, entry={entry_residual:+.5f}, "
        f"target={','.join(sorted(target_position_types))}, pending={len(pending_orders)}"
    )
    REENTRY_TRIGGER_COUNT += 1

    any_placed = False
    for order in pending_orders:
        order_id = int(order["id"])
        try:
            result = manual_place_single_order(order_id)
            if result.get("success"):
                ORDERS_PLACED_COUNT += 1
                any_placed = True
                set_event(f"{ticker}: single order id={order_id} set HOLDING")
            else:
                set_event(f"{ticker}: single order id={order_id} failed: {result.get('message')}")
        except Exception as exc:
            set_event(f"{ticker}: single order id={order_id} failed: {exc}")

    if any_placed:
        set_reentry_lock(pair_name, 1)
        set_event(f"{ticker}: reentry_lock set to 1")


def try_single_new_high_trigger(row):
    global REENTRY_TRIGGER_COUNT, ORDERS_PLACED_COUNT

    pair_name = str(row.get("pair_key") or "").strip()
    ticker = str(row.get("pair") or pair_name).strip().upper()
    tick_price = safe_float(row.get("tick_price"))
    trigger_price = safe_float(row.get("threshold"))
    if tick_price is None or trigger_price is None or tick_price <= trigger_price:
        return

    lock_value = get_reentry_lock(pair_name)
    if lock_value >= 1:
        set_event(f"{ticker}: new high price={tick_price:.4f} trigger={trigger_price:.4f} but locked ({lock_value})")
        return

    pending_orders = load_pending_single_orders(ticker)
    if not pending_orders:
        create_auto_single_pending_order(
            single_name=ticker,
            reentry_cfg=row,
            strategy="NEW_HIGH",
            param=trigger_price,
            target_position_type="LONG_SPREAD",
        )
        pending_orders = load_pending_single_orders(ticker)

    pending_orders = [
        order
        for order in pending_orders
        if normalize_position_type(order.get("position_type")) == "LONG_SPREAD"
    ]
    if not pending_orders:
        set_event(f"{ticker}: new high fired but no LONG_SPREAD PENDING single orders")
        return

    set_event(f"{ticker}: new high trigger fired price={tick_price:.4f} > {trigger_price:.4f}, pending={len(pending_orders)}")
    REENTRY_TRIGGER_COUNT += 1

    any_placed = False
    for order in pending_orders:
        order_id = int(order["id"])
        try:
            result = manual_place_single_order(order_id)
            if result.get("success"):
                ORDERS_PLACED_COUNT += 1
                any_placed = True
                set_event(f"{ticker}: new high single order id={order_id} set HOLDING")
            else:
                set_event(f"{ticker}: new high single order id={order_id} failed: {result.get('message')}")
        except Exception as exc:
            set_event(f"{ticker}: new high single order id={order_id} failed: {exc}")

    if any_placed:
        set_reentry_lock(pair_name, 1)
        set_event(f"{ticker}: reentry_lock set to 1")


# ===============================
# UI
# ===============================
def build_table(results):
    table = Table(expand=True)

    table.add_column("Name", style="bold")
    table.add_column("Lock", justify="right")
    table.add_column("Signal", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Rolling Mean", justify="right")
    table.add_column("Entry", justify="right")
    table.add_column("Bars", justify="right")

    for row in results:
        lock_text = str(row.get("reentry_lock", "-"))

        if row.get("kind") == "single_log_residual":
            current_residual = row.get("current_residual")
            avg78_residual = row.get("avg78_residual")
            entry_residual = row.get("threshold")
            tick_price = row.get("tick_price")
            error = row.get("error")

            if error:
                signal_str = f"[dim]{error}[/dim]"
            elif current_residual is None:
                signal_str = "[dim]no tick[/dim]"
            elif avg78_residual is None or entry_residual is None:
                signal_str = f"[yellow]{current_residual:+.5f}[/yellow]"
            else:
                if current_residual > entry_residual:
                    color = "green"
                elif current_residual > avg78_residual:
                    color = "yellow"
                else:
                    color = "red"
                signal_str = f"[{color}]{current_residual:+.5f}[/{color}]"

            price_str = "-" if tick_price is None else f"{tick_price:.2f}"
            avg_str = "-" if avg78_residual is None else f"{avg78_residual:+.5f}"
            entry_str = "-" if entry_residual is None else f"{entry_residual:+.5f}"
            bars_str = str(row.get("bars") or 0)
            table.add_row(row["pair"], lock_text, signal_str, price_str, avg_str, entry_str, bars_str)
            continue

        if row.get("kind") == "single_new_high":
            tick_price = row.get("tick_price")
            trigger_price = row.get("threshold")
            error = row.get("error")
            if error:
                signal_str = f"[dim]{error}[/dim]"
            elif tick_price is None:
                signal_str = "[dim]no tick[/dim]"
            elif trigger_price is None:
                signal_str = f"[yellow]{tick_price:.2f}[/yellow]"
            elif tick_price < trigger_price * 0.90:
                signal_str = f"[red]{tick_price:.2f}[/red]"
            elif tick_price < trigger_price * 0.95:
                signal_str = f"[yellow]{tick_price:.2f}[/yellow]"
            else:
                signal_str = f"[green]{tick_price:.2f}[/green]"

            price_str = "-" if tick_price is None else f"{tick_price:.2f}"
            entry_str = "-" if trigger_price is None else f"{trigger_price:.2f}"
            table.add_row(row["pair"], lock_text, signal_str, price_str, "-", entry_str, "0")
            continue

        rolling_mean = row.get("rolling_mean")
        avg_str = "-" if rolling_mean is None else f"{float(rolling_mean):.5f}"
        if row["z"] is None:
            z_str = "[dim]N/A[/dim]"
            price_str = "-"
            entry_str = "-"
            bars_str = "-"
        else:
            z_val = row["z"]
            threshold = normalize_threshold(row.get("threshold"))

            if abs(z_val) > threshold:
                color = "red"
            elif abs(z_val) > 1:
                color = "yellow"
            else:
                color = "green"

            z_str = f"[{color}]{z_val:.2f}[/{color}]"
            price_str = f"Y {row['py']:.2f} / X {row['px']:.2f}"
            entry_str = f"{threshold:.2f}"
            bars_str = "-"

        def format_pair(pair_str: str) -> str:
            if "_" in pair_str:
                y, x = pair_str.split("_", 1)
                return f"{y.upper()} / {x.upper()}"
            return pair_str.upper()
        table.add_row(
            format_pair(row["pair"]),
            lock_text,
            z_str,
            price_str,
            avg_str,
            entry_str,
            bars_str,
        )

    return table


def build_layout():
    layout = Layout()
    layout.split_column(
        Layout(name="top", size=5),
        Layout(name="body", ratio=1),
    )
    return layout


def build_header_panel(total_pairs, live_pairs):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    missing_pairs = total_pairs - live_pairs
    text = (
        f"Reentry Monitor | Time: {now}\n"
        f"Pairs: {total_pairs} | Live: {live_pairs} | Missing: {missing_pairs} | "
        f"Triggers: {REENTRY_TRIGGER_COUNT} | Orders: {ORDERS_PLACED_COUNT}\n"
        f"Last Event: {LAST_EVENT}"
    )
    return Panel(text, title="Runtime", padding=(0, 1))


def format_display_pair(pair_str):
    pair_str = str(pair_str or "").strip()
    if "_" in pair_str:
        y, x = pair_str.split("_", 1)
        return f"{y.upper()} / {x.upper()}"
    return pair_str.upper()


def waiting_entry_signal_state(row):
    if row.get("kind") == "single_log_residual":
        current = row.get("current_residual")
        avg78 = row.get("avg78_residual")
        entry = row.get("threshold")
        if row.get("error") or current is None:
            return "muted"
        if entry is not None and avg78 is not None:
            if current > entry:
                return "ok"
            if current > avg78:
                return "warn"
            return "trigger"
        return "muted"

    if row.get("kind") == "single_new_high":
        tick_price = row.get("tick_price")
        trigger_price = row.get("threshold")
        if row.get("error") or tick_price is None:
            return "muted"
        if trigger_price is None:
            return "warn"
        if tick_price < trigger_price * 0.90:
            return "trigger"
        if tick_price < trigger_price * 0.95:
            return "warn"
        return "ok"

    z_value = row.get("z")
    if z_value is None:
        return "muted"
    threshold = normalize_threshold(row.get("threshold"))
    if abs(z_value) > threshold:
        return "trigger"
    if abs(z_value) > 1:
        return "warn"
    return "ok"


def waiting_entry_signal_label(row):
    if row.get("kind") == "single_log_residual":
        current = row.get("current_residual")
        if row.get("error"):
            return str(row.get("error"))
        return "no tick" if current is None else f"{current:+.5f}"

    if row.get("kind") == "single_new_high":
        tick_price = row.get("tick_price")
        if row.get("error"):
            return str(row.get("error"))
        if tick_price is None:
            return "no tick"
        return f"{tick_price:.2f}"

    z_value = row.get("z")
    return "-" if z_value is None else f"{z_value:+.3f}"


def waiting_entry_item(row):
    kind = row.get("kind") or "pair"
    pair_key = str(row.get("pair_key") or row.get("pair") or "").strip()
    name = format_display_pair(row.get("pair") or pair_key)
    state = waiting_entry_signal_state(row)
    item = {
        "name": name,
        "pair_key": pair_key,
        "kind": kind,
        "strategy": "PAIR" if kind == "pair" else kind.replace("single_", "").upper(),
        "lock": int(row.get("reentry_lock") or 0),
        "signal": (
            row.get("current_residual")
            if kind == "single_log_residual"
            else row.get("tick_price") if kind == "single_new_high" else row.get("z")
        ),
        "signal_label": waiting_entry_signal_label(row),
        "signal_state": state,
        "price": row.get("tick_price"),
        "price_x": row.get("px"),
        "price_y": row.get("py"),
        "tick_ts_x": row.get("tick_ts_x"),
        "tick_ts_y": row.get("tick_ts_y"),
        "rolling_mean": (
            row.get("rolling_mean")
            if kind == "pair"
            else row.get("avg78_residual")
        ),
        "entry": row.get("threshold"),
        "bars": row.get("bars"),
        "updated_at": None,
    }
    bar_time_utc = row.get("bar_time_utc")
    if bar_time_utc is not None and not pd.isna(bar_time_utc):
        item["updated_at"] = pd.Timestamp(bar_time_utc).strftime("%Y-%m-%d %H:%M:%S")
        return item
    tick_ts = row.get("tick_ts")
    if tick_ts is not None:
        try:
            item["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(tick_ts)))
        except (TypeError, ValueError):
            item["updated_at"] = None
    return item


def publish_waiting_entry_snapshot(results, total_pairs, live_pairs):
    items = [waiting_entry_item(row) for row in results]
    items.sort(
        key=lambda item: (
            int(item.get("lock") or 0),
            0 if item.get("kind") == "pair" else 1,
            str(item.get("name") or "").upper(),
        )
    )
    payload = {
        "ok": True,
        "source": "waiting_entry",
        "count": len(items),
        "live_count": live_pairs,
        "trigger_count": REENTRY_TRIGGER_COUNT,
        "locked_count": sum(1 for item in items if int(item.get("lock") or 0) >= 1),
        "reentry_trigger_count": REENTRY_TRIGGER_COUNT,
        "orders_placed_count": ORDERS_PLACED_COUNT,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_ts": time.time(),
        "last_event": LAST_EVENT,
        "items": items,
    }
    r.set(
        WAITING_ENTRY_SNAPSHOT_KEY,
        json.dumps(payload, ensure_ascii=False, default=str),
        ex=WAITING_ENTRY_SNAPSHOT_TTL_SECONDS,
    )


# ===============================
# MAIN LOOP
# ===============================
def main():
    param_cache = {}
    param_refresh_attempted_at = {}
    last_z = {}
    last_pair_bar_time = {}
    predecessor_seeded_pairs = set()
    last_auto_unlock_error_at = 0

    clear()
    layout = build_layout()
    layout["top"].update(Panel("Initializing...", title="Runtime"))
    layout["body"].update(Panel("Loading data...", title="Reentry Pairs"))
    live_bar_pubsub = open_live_bar_pubsub()
    try:
        with Live(layout, refresh_per_second=4, screen=True) as live:
            while True:
                drain_live_bar_events(live_bar_pubsub)
                pair_config_map = load_reentry_pairs()
                try:
                    position_snapshot = collect_account_positions()
                    unlocked_reentry = pdt.reset_auto_reentry_locks_for_missing_positions(position_snapshot)
                    if unlocked_reentry:
                        unlocked_names = ", ".join(item["pair_name"] for item in unlocked_reentry)
                        set_event(f"auto reentry lock reset -> 0: {unlocked_names}")
                        pair_config_map = load_reentry_pairs()
                except Exception as exc:
                    now = time.time()
                    if now - last_auto_unlock_error_at >= 30:
                        set_event(f"auto reentry unlock skipped: {exc}")
                        last_auto_unlock_error_at = now
                pairs = list(pair_config_map.keys())

                # Fixed-beta mean/sigma must advance with the completed 5m
                # bar, not with a timer that happens to start mid-bar.
                now_monotonic = time.monotonic()
                for pair in pairs:
                    if is_single_log_residual_config(pair, pair_config_map.get(pair, {})):
                        continue
                    if is_single_new_high_config(pair, pair_config_map.get(pair, {})):
                        continue
                    cached_cfg = param_cache.get(pair)
                    completed_bar_bj = (
                        completed_pair_bar_time_bj(cached_cfg)
                        if cached_cfg else None
                    )
                    cached_baseline_bj = (
                        cached_cfg.get("baseline_bar_time_bj")
                        if cached_cfg else None
                    )
                    bar_advanced = (
                        cached_cfg is not None
                        and completed_bar_bj is not None
                        and completed_bar_bj != cached_baseline_bj
                    )
                    retry_due = (
                        now_monotonic - param_refresh_attempted_at.get(pair, 0.0)
                        >= ROLLING_PARAMS_RETRY_SECONDS
                    )
                    failsafe_due = (
                        cached_cfg is not None
                        and now_monotonic - param_refresh_attempted_at.get(pair, 0.0)
                        >= ROLLING_PARAMS_FAILSAFE_REFRESH_SECONDS
                    )
                    if pair not in param_cache or (bar_advanced and retry_due) or failsafe_due:
                        param_refresh_attempted_at[pair] = now_monotonic
                        cfg = fetch_params(
                            pair, pair_config_map.get(pair, {}), force_refresh=True,
                        )
                        if cfg:
                            param_cache[pair] = cfg
                            set_event(
                                f"rolling params refreshed {pair} "
                                f"at {cfg.get('baseline_bar_time_bj') or 'unknown bar'} BJ"
                            )
                        else:
                            set_event(f"no params for {pair}")

                results = []

                for pair in pairs:
                    reentry_cfg = pair_config_map.get(pair, {})
                    if is_single_log_residual_config(pair, reentry_cfg):
                        results.append(compute_single_log_residual(pair, reentry_cfg))
                        continue
                    if is_single_new_high_config(pair, reentry_cfg):
                        results.append(compute_single_new_high(pair, reentry_cfg))
                        continue

                    cfg = param_cache.get(pair)
                    if not cfg:
                        results.append(
                            {
                                "pair_key": pair,
                                "pair": pair.upper(),
                                "reentry_lock": reentry_cfg.get("reentry_lock", 0),
                                "threshold": reentry_cfg.get("threshold", DEFAULT_TRIGGER_Z),
                                "z": None,
                                "px": None,
                                "py": None,
                            }
                        )
                        continue

                    row = compute_realtime_z(cfg)
                    if not row:
                        row = {
                            "pair_key": pair,
                            "pair": pair.upper(),
                            "z": None,
                            "px": None,
                            "py": None,
                        }

                    row["reentry_lock"] = reentry_cfg.get("reentry_lock", 0)
                    row["threshold"] = reentry_cfg.get("threshold", DEFAULT_TRIGGER_Z)
                    results.append(row)

                # trigger evaluation
                latest_avg78_updates = []
                for row in results:
                    if row.get("kind") == "single_log_residual":
                        avg78_time_bj = row.get("avg78_time_bj")
                        if avg78_time_bj is not None and not pd.isna(avg78_time_bj):
                            latest_avg78_updates.append(
                                (
                                    str(row.get("pair") or row.get("pair_key") or "").upper(),
                                    pd.Timestamp(avg78_time_bj),
                                )
                            )
                        try_single_log_residual_trigger(row)
                        continue
                    if row.get("kind") == "single_new_high":
                        try_single_new_high_trigger(row)
                        continue
                    pair_name = row["pair_key"]
                    current_cfg = param_cache.get(pair_name)
                    bar_row = compute_5min_bar_z(current_cfg) if current_cfg else None
                    z_now = bar_row.get("z") if bar_row else None
                    if z_now is None:
                        continue

                    bar_time = bar_row.get("bar_time_utc")
                    if bar_time is None or pd.isna(bar_time):
                        continue
                    bar_time = pd.Timestamp(bar_time)
                    if last_pair_bar_time.get(pair_name) == bar_time:
                        continue

                    z_prev = last_z.get(pair_name)
                    threshold = normalize_threshold(row.get("threshold"))
                    lock_value = int(row.get("reentry_lock") or 0)
                    if lock_value >= 1:
                        if is_take_profit_cooling(pair_name):
                            last_z[pair_name] = cooldown_prev_z(z_prev, z_now, threshold)
                        else:
                            last_z[pair_name] = z_now
                        last_pair_bar_time[pair_name] = bar_time
                        continue

                    if z_prev is None and pair_name not in predecessor_seeded_pairs:
                        try:
                            z_prev, predecessor = load_predecessor_z(
                                current_cfg,
                                bar_time,
                            )
                            predecessor_time = predecessor.get("bar_time_utc")
                            if z_prev is not None:
                                set_event(
                                    f"{pair_name}: seeded predecessor "
                                    f"{predecessor_time} z={z_prev:+.4f}"
                                )
                            else:
                                set_event(f"{pair_name}: archival predecessor unavailable")
                        except Exception as exc:
                            set_event(f"{pair_name}: predecessor seed failed: {exc}")
                            z_prev = None
                        predecessor_seeded_pairs.add(pair_name)

                    if z_prev is not None:
                        current_reentry_cfg = pair_config_map.get(pair_name, {})
                        try_reentry_trigger(
                            pair_name,
                            z_prev,
                            z_now,
                            threshold,
                            reentry_cfg=current_reentry_cfg,
                            cfg=current_cfg,
                            row=bar_row,
                        )

                    last_z[pair_name] = z_now
                    last_pair_bar_time[pair_name] = bar_time

                if latest_avg78_updates:
                    latest_avg78_updates.sort(key=lambda item: item[1], reverse=True)
                    ticker, latest_time_bj = latest_avg78_updates[0]
                    set_event(f"avg78 latest {ticker}: {latest_time_bj.strftime('%Y-%m-%d %H:%M')} BJ")

                results.sort(
                    key=lambda x: (
                        int(x.get("reentry_lock", 0)),
                        0 if (x.get("kind") or "pair") == "pair" else 1,
                        str(x.get("pair_key") or x.get("pair") or "").upper(),
                    )
                )

                live_pairs = sum(
                    1
                    for row in results
                    if row.get("z") is not None
                    or row.get("current_residual") is not None
                    or row.get("tick_price") is not None
                )
                publish_waiting_entry_snapshot(results, len(pairs), live_pairs)
                layout["top"].update(build_header_panel(len(pairs), live_pairs))
                layout["body"].update(Panel(build_table(results), title="Reentry Pairs"))
                live.update(layout)

                time.sleep(0.5)
    finally:
        if ib.isConnected():
            ib.disconnect()


if __name__ == "__main__":
    main()
