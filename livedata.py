# ===============================
# Livedata.py
# IB → Redis 实时价格数据源
# 接入 market_schedule 交易时段节律
# ===============================

import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import *
import json
import redis
import pymysql
import time
import sys
import math
from datetime import datetime, timezone

from program_factory.market_schedule import (
    NY_TZ,
    get_current_or_next_session,
    wait_until,
)
from program_factory.port_listener import choose_connection
from program_factory.live_bars import (
    BAR_CHANNEL,
    BAR_SECONDS,
    BAR_TTL_SECONDS,
    HISTORY_LIMIT,
    current_key as live_current_key,
    history_key as live_history_key,
    latest_key as live_latest_key,
    load_latest_bar,
)
from runtime_settings import database_config, ib_port_account_map, redis_config

# ===============================
# 1️⃣ 配置
# ===============================

CLIENT_ID = 5
PREFERRED_PORT_ORDER = (4001, 4002)
USE_RTH = True

REDIS_SETTINGS = redis_config()
REDIS_HOST = REDIS_SETTINGS["host"]
REDIS_PORT = REDIS_SETTINGS["port"]

IB_HOST = "127.0.0.1"
SUBSCRIPTION_REFRESH_SECONDS = 30
PRICE_CLEAR_DATE_KEY = "livedata:price_clear_date"

MEMORY_DB_CONFIG = database_config("memory", cursorclass=pymysql.cursors.DictCursor)
DATA_DB_CONFIG = database_config("data",
    autocommit=True,
    connect_timeout=2,
    read_timeout=2,
    write_timeout=2,
)

# ===============================
# 2️⃣ IB / Redis
# ===============================

ib = IB()

r = redis.Redis(**REDIS_SETTINGS)


def ensure_redis_available():
    try:
        r.ping()
    except redis.exceptions.RedisError as exc:
        raise SystemExit(
            f"Redis is not running at {REDIS_HOST}:{REDIS_PORT}. "
            "Install/start redis-server before launching livedata.py."
        ) from None
    print("✅ Redis 已连接")


def clear_redis_prices_once_per_day():
    today_key = datetime.now(NY_TZ).strftime("%Y-%m-%d")
    last_cleared = r.get(PRICE_CLEAR_DATE_KEY)
    if last_cleared == today_key:
        print(f"[REDIS CLEAR] price cache already cleared for {today_key}")
        return

    deleted = 0
    for pattern in ("price:*", "ts:*"):
        batch = []
        for key in r.scan_iter(match=pattern, count=500):
            batch.append(key)
            if len(batch) >= 500:
                deleted += r.delete(*batch)
                batch = []
        if batch:
            deleted += r.delete(*batch)

    r.set(PRICE_CLEAR_DATE_KEY, today_key)
    print(f"[REDIS CLEAR] removed {deleted} price/timestamp keys for {today_key}")

# ===============================
# 3️⃣ 账户
# ===============================

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
# ===============================
# 4️⃣ 标的集合
# ===============================

def symbols_from_trade_key(value):
    raw = str(value or "").strip()
    if not raw:
        return set()
    return {
        part.strip().upper()
        for part in raw.split("_")
        if part.strip()
    }


def load_active_pair_symbols():
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT pair_name
                FROM pair_reentry
                WHERE pair_name IS NOT NULL
                  AND pair_name <> ''

                UNION

                SELECT pair_name
                FROM scheduled_orders
                WHERE status IN ('PENDING', 'HOLDING')
                  AND pair_name IS NOT NULL
                  AND pair_name <> ''
                """
            )
            rows = cursor.fetchall()
    finally:
        conn.close()

    symbols = set()
    for row in rows:
        symbols |= symbols_from_trade_key(row.get("pair_name"))
    return symbols


def load_active_single_symbols():
    conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT single_name
                FROM single_scheduled_orders
                WHERE status IN ('PENDING', 'HOLDING')
                  AND single_name IS NOT NULL
                  AND single_name <> ''
                """
            )
            rows = cursor.fetchall()
    finally:
        conn.close()

    return {
        str(row.get("single_name") or "").strip().upper()
        for row in rows
        if str(row.get("single_name") or "").strip()
    }


def build_symbol_set():
    return load_active_pair_symbols() | load_active_single_symbols()


symbols = []
contracts = {}
tickers = {}
last_subscription_refresh = 0
tick_bar_buckets = {}
pending_tick_bar_archives = {}
last_tick_bar_archive_error_at = 0.0
tick_bar_session_open_ts = None
tick_bar_session_close_ts = None

# ===============================
# 5️⃣ 动画
# ===============================

def gen_wave(n=20, width=10):
    waves = []
    for t in range(n):
        s = ""
        for x in range(width):
            y = math.sin((x + t) * 2 * math.pi / width)
            level = int((y + 1) / 2 * 7)
            chars = "▁▂▃▄▅▆▇█"
            s += chars[level]
        waves.append(s)
    return waves

wave_states = gen_wave()
wave_idx = 0

# ===============================
# 6️⃣ IB 连接 / 重连 / 订阅
# ===============================

def connect_ib():
    if ib.isConnected():
        return

    ib.connect(IB_HOST, CONNECT_PORT, clientId=CLIENT_ID)
    print(f"✅ IB 已连接 | Port={CONNECT_PORT} | Account={TARGET_ACCOUNT}")

def qualify_all_contracts():
    ib.qualifyContracts(*contracts.values())
    print("✅ Contracts 已 qualify")


def format_symbol_list(values):
    return f"[{', '.join(sorted(values))}]"

def refresh_subscriptions(force=False):
    global symbols, contracts, last_subscription_refresh

    now_ts = time.time()
    if not force and now_ts - last_subscription_refresh < SUBSCRIPTION_REFRESH_SECONDS:
        return

    next_symbols = set(build_symbol_set())
    current_symbols = set(symbols)
    added_symbols = sorted(next_symbols - current_symbols)
    removed_symbols = sorted(current_symbols - next_symbols)

    # Keep unchanged subscriptions alive.  This avoids a full req/cancel burst
    # whenever one pair or scheduled order changes.
    for symbol in removed_symbols:
        cancel_symbol_subscriptions(symbol)
        contracts.pop(symbol, None)

    if added_symbols:
        print(f"\n📡 Add subscriptions: {format_symbol_list(added_symbols)}")
        new_contracts = {symbol: Stock(symbol, 'SMART', 'USD') for symbol in added_symbols}
        ib.qualifyContracts(*new_contracts.values())
        contracts.update(new_contracts)
        subscribe_market_data(added_symbols)

    # A reconnect clears IB-side tick requests.
    missing_ticks = sorted(next_symbols - set(tickers))
    if missing_ticks:
        subscribe_market_data(missing_ticks)

    if removed_symbols:
        print(f"\n📡 Remove subscriptions: {format_symbol_list(removed_symbols)}")
    symbols = sorted(next_symbols)
    last_subscription_refresh = now_ts


def subscribe_market_data(symbols_to_subscribe=None):
    symbols_to_subscribe = symbols_to_subscribe or sorted(contracts)
    for s in symbols_to_subscribe:
        if s in tickers:
            continue
        contract = contracts.get(s)
        if contract is None:
            continue
        ticker = ib.reqMktData(contract, '', False, False)
        ticker.updateEvent += lambda updated_ticker, symbol=s: on_live_ticker_update(
            symbol, updated_ticker
        )
        tickers[s] = ticker
        ib.sleep(0.1)  # 防 pacing

    if symbols_to_subscribe:
        print(f"🚀 reqMktData 已订阅: {format_symbol_list(symbols_to_subscribe)}")


def cancel_symbol_subscriptions(symbol):
    ticker = tickers.pop(symbol, None)
    if ticker is not None:
        try:
            ib.cancelMktData(ticker.contract)
        except Exception as exc:
            print(f"⚠ cancelMktData 失败 {symbol}: {exc}")

def cancel_all_market_data():
    for symbol in list(tickers):
        cancel_symbol_subscriptions(symbol)

def reconnect_ib(max_retry=9999):
    global tickers
    retry = 0

    while True:
        try:
            print(f"\n🔌 IB断线，尝试重连... (第{retry + 1}次)")

            try:
                ib.disconnect()
            except Exception:
                pass

            ib.connect(IB_HOST, CONNECT_PORT, clientId=CLIENT_ID)

            if ib.isConnected():
                tickers = {}
                qualify_all_contracts()
                subscribe_market_data(symbols)
                print("✅ IB重连成功\n")
                return

        except Exception as e:
            print(f"❌ 重连失败: {e}")

        retry += 1
        sleep_sec = min(60, 2 + retry * 2)
        print(f"⏳ {sleep_sec}s 后重试...")
        ib.sleep(sleep_sec)

def on_disconnect():
    print("⚠ IB已断开连接")
    # disconnectedEvent is emitted from inside ib_insync's event loop.  A
    # synchronous reconnect here re-enters that loop and raises
    # "This event loop is already running".  main_loop owns reconnection.

ib.disconnectedEvent += on_disconnect

# ===============================
# 7️⃣ 价格获取
# ===============================

def get_ticker_price(ticker):
    """
    优先级：
    last -> marketPrice() -> close
    """
    try:
        if ticker.last is not None and not math.isnan(ticker.last):
            return float(ticker.last)
    except Exception:
        pass

    try:
        mp = ticker.marketPrice()
        if mp is not None and not math.isnan(mp):
            return float(mp)
    except Exception:
        pass

    try:
        if ticker.close is not None and not math.isnan(ticker.close):
            return float(ticker.close)
    except Exception:
        pass

    return None


def get_ticker_timestamp(ticker, fallback_ts):
    ticker_time = getattr(ticker, "time", None)
    if ticker_time is None:
        return fallback_ts
    try:
        return ticker_time.timestamp()
    except Exception:
        return fallback_ts


def set_tick_bar_session(session_open, session_close):
    """Enable aggregation only inside the current regular trading session."""
    global tick_bar_session_open_ts, tick_bar_session_close_ts
    open_ts = session_open.timestamp()
    close_ts = session_close.timestamp()
    if tick_bar_session_open_ts != open_ts or tick_bar_session_close_ts != close_ts:
        tick_bar_buckets.clear()
        tick_bar_session_open_ts = open_ts
        tick_bar_session_close_ts = close_ts


def persist_current_tick_bar(symbol, bucket):
    r.set(
        live_current_key(symbol),
        json.dumps(bucket, separators=(",", ":")),
        ex=BAR_SECONDS * 3,
    )


def queue_tick_bar_archive(payload):
    key = (str(payload["ticker"]).upper(), int(payload["start_ts"]))
    pending_tick_bar_archives[key] = (
        key[0],
        datetime.fromtimestamp(key[1], timezone.utc).replace(tzinfo=None),
        float(payload["close"]),
        "Livedata",
    )


def flush_tick_bar_archives():
    """Batch archive completed bars after the Redis real-time path has finished."""
    global last_tick_bar_archive_error_at
    if not pending_tick_bar_archives:
        return 0
    snapshot = dict(pending_tick_bar_archives)
    rows = list(snapshot.values())
    try:
        conn = pymysql.connect(**DATA_DB_CONFIG)
        try:
            with conn.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT IGNORE INTO us_market_prices
                        (ticker, time_utc, price, source)
                    VALUES (%s, %s, %s, %s)
                    """,
                    rows,
                )
        finally:
            conn.close()
    except Exception as exc:
        now = time.monotonic()
        if now - last_tick_bar_archive_error_at >= 30:
            print(f"\n⚠ tick bar archive deferred ({len(rows)} bars): {exc}")
            last_tick_bar_archive_error_at = now
        return 0

    for key, row in snapshot.items():
        if pending_tick_bar_archives.get(key) == row:
            pending_tick_bar_archives.pop(key, None)
    return len(rows)


def finalize_tick_bar(symbol, bucket, ready_ts=None):
    """Publish one completed live bar and retain only the latest 78 bars."""
    ready_ts = float(ready_ts or time.time())
    start_ts = int(bucket["start_ts"])
    latest = load_latest_bar(r, symbol)
    if latest and int(latest["start_ts"]) >= start_ts:
        r.delete(live_current_key(symbol))
        return False

    payload = dict(bucket)
    payload.update(
        {
            "ticker": symbol,
            "close_ts": start_ts + BAR_SECONDS,
            "time_utc": datetime.fromtimestamp(start_ts, timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "price": float(bucket["close"]),
            "source": "Livedata",
            "ready_ts": ready_ts,
        }
    )
    encoded = json.dumps(payload, separators=(",", ":"))
    pipe = r.pipeline()
    pipe.set(live_latest_key(symbol), encoded, ex=BAR_TTL_SECONDS)
    pipe.lpush(live_history_key(symbol), encoded)
    pipe.ltrim(live_history_key(symbol), 0, HISTORY_LIMIT - 1)
    pipe.expire(live_history_key(symbol), BAR_TTL_SECONDS)
    pipe.delete(live_current_key(symbol))
    pipe.publish(BAR_CHANNEL, encoded)
    pipe.execute()
    queue_tick_bar_archive(payload)
    return True


def on_live_ticker_update(symbol, ticker):
    """Fold an incoming ticker update into one bounded in-memory 5-minute bucket."""
    if tick_bar_session_open_ts is None or tick_bar_session_close_ts is None:
        return
    price = get_ticker_price(ticker)
    if price is None or price <= 0:
        return
    tick_ts = get_ticker_timestamp(ticker, time.time())
    if not (tick_bar_session_open_ts <= tick_ts < tick_bar_session_close_ts):
        return

    start_ts = int(tick_ts // BAR_SECONDS * BAR_SECONDS)
    current = tick_bar_buckets.get(symbol)
    if current and start_ts < current["start_ts"]:
        return
    if current and start_ts > current["start_ts"]:
        finalize_tick_bar(symbol, current, ready_ts=tick_ts)
        current = None

    if current is None:
        current = {
            "ticker": symbol,
            "start_ts": start_ts,
            "open": float(price),
            "high": float(price),
            "low": float(price),
            "close": float(price),
            "ticks": 1,
            "first_tick_ts": tick_ts,
            "last_tick_ts": tick_ts,
        }
        tick_bar_buckets[symbol] = current
    else:
        current["high"] = max(float(current["high"]), float(price))
        current["low"] = min(float(current["low"]), float(price))
        current["close"] = float(price)
        current["ticks"] += 1
        current["last_tick_ts"] = tick_ts
    persist_current_tick_bar(symbol, current)


def flush_due_tick_bars(now_ts=None):
    """Close buckets on time even when a symbol has no tick at the boundary."""
    now_ts = float(now_ts or time.time())
    for symbol, bucket in list(tick_bar_buckets.items()):
        if now_ts >= int(bucket["start_ts"]) + BAR_SECONDS:
            finalize_tick_bar(symbol, bucket, ready_ts=now_ts)
            tick_bar_buckets.pop(symbol, None)


def redis_float(key):
    try:
        value = r.get(key)
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def write_prices_to_redis(ts_now):
    for s, ticker in tickers.items():
        price = get_ticker_price(ticker)

        if price is not None:
            price_key = f"price:{s}"
            ts_key = f"ts:{s}"
            old_price = redis_float(price_key)
            old_ts = redis_float(ts_key)
            if old_price is not None and math.isclose(old_price, price, rel_tol=0, abs_tol=1e-9):
                symbol_ts = old_ts if old_ts is not None else get_ticker_timestamp(ticker, ts_now)
            else:
                symbol_ts = get_ticker_timestamp(ticker, ts_now)

            r.set(price_key, price)
            r.set(ts_key, symbol_ts)

# ===============================
# 8️⃣ 主循环
# ===============================

def main_loop():
    global wave_idx

    if not symbols:
        print("⚠ 没有可订阅标的，退出")
        return

    # 用第一个合约作为交易时段基准
    session_contract = next(iter(contracts.values()))

    print("🚀 Livedata 主循环启动")

    while True:
        try:
            flush_due_tick_bars()
            flush_tick_bar_archives()
            if not ib.isConnected():
                reconnect_ib()

            refresh_subscriptions()

            now_ny = datetime.now(NY_TZ)

            state, session_open, session_close = get_current_or_next_session(
                ib=ib,
                contract=session_contract,
                use_rth=USE_RTH,
                now_ny=now_ny,
                days_ahead=7,
            )

            if state is None:
                print("⚠ 未获取到有效交易时段，60秒后重试")
                ib.sleep(60)
                continue

            # 非交易时段：等待到下一个 session
            if state == "BEFORE_SESSION":
                print(f"🌙 非交易时段，等待到 {session_open:%Y-%m-%d %H:%M:%S}")
                wait_until(
                    ib=ib,
                    target_time=session_open,
                    label="LIVE_WAIT"
                )
                continue

            # 盘中：每秒写一次 Redis
            if state == "IN_SESSION":
                if datetime.now(NY_TZ) >= session_close:
                    continue

                set_tick_bar_session(session_open, session_close)

                ib.sleep(1)

                ts_now = time.time()
                flush_due_tick_bars(ts_now)
                flush_tick_bar_archives()
                write_prices_to_redis(ts_now)

                sys.stdout.write(f"\r🌊 LIVE {wave_states[wave_idx % len(wave_states)]}")
                sys.stdout.flush()
                wave_idx += 1

        except KeyboardInterrupt:
            print("\n🛑 用户中断，正在退出...")
            break

        except Exception as e:
            print(f"\n⚠ 主循环异常: {e}")
            reconnect_ib()
            ib.sleep(2)

# ===============================
# 9️⃣ 启动
# ===============================

def startup():
    ensure_redis_available()
    clear_redis_prices_once_per_day()
    connect_ib()
    refresh_subscriptions(force=True)

if __name__ == "__main__":
    startup()
    main_loop()
