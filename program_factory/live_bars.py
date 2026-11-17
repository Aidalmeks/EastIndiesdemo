"""Small Redis-backed store for bars aggregated from live market-data ticks."""

import json
import math
import time


BAR_SECONDS = 5 * 60
BAR_CHANNEL = "livebar:5m:closed"
CURRENT_KEY_PREFIX = "livebar:5m:current:"
LATEST_KEY_PREFIX = "livebar:5m:latest:"
HISTORY_KEY_PREFIX = "livebar:5m:history:"
HISTORY_LIMIT = 78
BAR_TTL_SECONDS = 3 * 24 * 60 * 60


def current_key(symbol):
    return f"{CURRENT_KEY_PREFIX}{str(symbol).upper()}"


def latest_key(symbol):
    return f"{LATEST_KEY_PREFIX}{str(symbol).upper()}"


def history_key(symbol):
    return f"{HISTORY_KEY_PREFIX}{str(symbol).upper()}"


def decode_bar(raw):
    if not raw:
        return None
    try:
        bar = json.loads(raw)
        for field in ("open", "high", "low", "close"):
            value = float(bar[field])
            if not math.isfinite(value) or value <= 0:
                return None
            bar[field] = value
        bar["start_ts"] = int(bar["start_ts"])
        bar["close_ts"] = int(bar["close_ts"])
        bar["ticks"] = int(bar.get("ticks") or 0)
        return bar
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None


def load_latest_bar(redis_client, symbol, max_age_seconds=None):
    bar = decode_bar(redis_client.get(latest_key(symbol)))
    if not bar:
        return None
    if max_age_seconds is not None and time.time() - bar["close_ts"] > max_age_seconds:
        return None
    return bar


def load_bar_history(redis_client, symbol, limit=HISTORY_LIMIT):
    bars = []
    for raw in redis_client.lrange(history_key(symbol), 0, max(0, int(limit) - 1)):
        bar = decode_bar(raw)
        if bar:
            bars.append(bar)
    return bars


def load_latest_aligned_bars(redis_client, first_symbol, second_symbol):
    """Return the newest common completed bucket for two symbols."""
    first = {bar["start_ts"]: bar for bar in load_bar_history(redis_client, first_symbol)}
    second = {bar["start_ts"]: bar for bar in load_bar_history(redis_client, second_symbol)}
    common = first.keys() & second.keys()
    if not common:
        return None, None
    start_ts = max(common)
    return first[start_ts], second[start_ts]
