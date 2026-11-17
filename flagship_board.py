import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())
import json
from datetime import datetime
import pytz
from ib_insync import *
import requests
import os
import time
import redis
import math
import urllib.parse
from rich.text import Text
from rich.live import Live
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel
from rich.columns import Columns
import pymysql
from program_factory import execution
import program_factory.pdt as pdt
from program_factory.port_listener import choose_connection
from program_factory.live_bars import load_latest_bar
from runtime_settings import api_base_url, database_config, ib_port_account_map, redis_config

# Local app.py calls must never inherit an HTTP/SOCKS proxy from the shell.
# A proxy makes the portfolio monitor fail before it can render its first board.
LOCAL_API_SESSION = requests.Session()
LOCAL_API_SESSION.trust_env = False

DATA_DB_CONFIG = database_config("data", cursorclass=pymysql.cursors.DictCursor)
MEMORY_DB_CONFIG = database_config("memory", cursorclass=pymysql.cursors.DictCursor)


def fetch_pair_stats(pair_key, force_refresh=False):
    # The app publisher owns calculation and publication of the rolling
    # baseline. Flagship only consumes that compact, per-bar snapshot.
    url = f"{api_base_url()}/rolling-baseline/{pair_key}"
    res = LOCAL_API_SESSION.get(url, timeout=10)
    res.raise_for_status()
    data = res.json()
    if data.get("kind") == "single":
        return None

    # app.py now serves every pair from production pair_config. Its spread is
    # Y - beta*X, so alpha is exactly zero; mean and sigma are causal rolling
    # values from the latest completed 5-minute bar.
    if data.get("calculation") == "fixed_beta_rolling":
        beta = data.get("fixed_beta", data.get("beta"))
        mean = data.get("rolling_mean", data.get("mu"))
        sigma = data.get("rolling_sigma", data.get("sigma"))
        try:
            beta, mean, sigma = float(beta), float(mean), float(sigma)
        except (TypeError, ValueError):
            return None
        if not all(math.isfinite(value) for value in (beta, mean, sigma)) or sigma <= 0:
            return None
        return {
            "calculation": "fixed_beta_rolling",
            "alpha": 0.0,
            "beta_reg": beta,
            "mu": mean,
            "sigma": sigma,
            "baseline_bar_time_bj": data.get("bar_time_bj"),
        }

    if "alpha" not in data:
        return None
    return {
        "calculation": "legacy_train_segment",
        "alpha": data["alpha"],
        "beta_reg": data["beta"],
        "mu": data["mu"],
        "sigma": data["sigma"]
    }

def fetch_usdcnh():
    try:
        res = requests.get("https://open.er-api.com/v6/latest/USD")
        data = res.json()
        return data["rates"]["CNH"]
    except:
        return 6.89
    
triggered_stop = set()
MAX_POSITION_MULTIPLIER = 2 * 0.8
DEBUG_PREMARKET = False

def dbg(msg):
    if DEBUG_PREMARKET:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[DBG {ts}] {msg}")

# ===== 连接 IB =====
ib = IB()
PREFERRED_PORT_ORDER = (4001, 4002)

redis_clent = redis.Redis(**redis_config())
FLAGSHIP_SNAPSHOT_KEY = "dashboard:flagship_board"
LATEST_BAR_PRICE_CACHE_SECONDS = 5.0
LIVE_BAR_MAX_AGE_SECONDS = 15 * 60
ROLLING_SNAPSHOT_POLL_SECONDS = 2.0
LATEST_BAR_PRICE_CACHE = {
    "updated_at": 0.0,
    "symbols": set(),
    "prices": {},
}


def ensure_redis_available():
    try:
        redis_clent.ping()
    except redis.exceptions.RedisError as exc:
        raise SystemExit("Redis is unavailable; configure QUANT_REDIS_HOST/QUANT_REDIS_PORT before launching flagship_board.py.") from None


ensure_redis_available()
#============================
PORT_ACCOUNT_MAP = ib_port_account_map()
selection = choose_connection(
    port_account_map=PORT_ACCOUNT_MAP,
    preferred_order=PREFERRED_PORT_ORDER
)
connect = selection.port
TARGET_ACCOUNT = selection.account
ib.connect("127.0.0.1", connect, clientId=6)
# IB.connect() starts the positions subscription. The loop reads the cache that
# ib_insync keeps updated instead of sending another request every cycle.
USDCNH = 1.0 if connect == 4001 else fetch_usdcnh()
print(
    f"[PORT] selected={connect}, account={TARGET_ACCOUNT}, pid={selection.pid}, "
    f"process={selection.process_name}, reason={selection.reason}"
)
print(f"[PDT] day trade limit enabled={pdt.DAY_TRADE_LIMIT_ENABLED}")

def clear():
    os.system('cls' if os.name == 'nt' else 'clear')

def wait_price(ticker, timeout=100):
    start = time.time()

    while True:
        price = ticker.marketPrice()

        if price is not None and not math.isnan(price):
            return price

        if time.time() - start > timeout:
            return None

        ib.sleep(0.2)
# ===== 你的 pairs 定义 =====
def load_pairs_from_db():
    pairs = {}
    memory_conn = pymysql.connect(**MEMORY_DB_CONFIG)
    try:
        with memory_conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT LOWER(pair_name) AS pair_key
                FROM pair_config
                WHERE data_source = '5m'
                """
            )
            configured_pair_keys = {
                str(row.get("pair_key") or "").strip().lower()
                for row in cursor.fetchall()
                if str(row.get("pair_key") or "").strip()
            }
    finally:
        memory_conn.close()

    conn = pymysql.connect(**DATA_DB_CONFIG)

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT pair_name, ticker_a, ticker_b
                FROM metapairs
                WHERE is_active = 1
            """)

            rows = cursor.fetchall()

            for row in rows:
                pair_key = str(row.get("pair_name") or "").strip().lower()
                # Production pair monitoring follows pair_config exclusively.
                # Active metapairs without fixed-beta settings (e.g. QQQ/TLT)
                # are not valid pair models and remain on the single path.
                if pair_key not in configured_pair_keys:
                    continue
                y = row["ticker_a"]
                x = row["ticker_b"]

                display_name = f"{y} / {x}"

                pairs[display_name] = {
                    "pair_key": pair_key,
                    "y": y,
                    "x": x
                }

    finally:
        conn.close()

    return pairs
PAIRS = load_pairs_from_db()

def get_two_sigma_from_db(pair_key):
    conn = pymysql.connect(**MEMORY_DB_CONFIG)

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT two_sigma
                FROM scheduled_orders
                WHERE pair_name = %s
                ORDER BY id DESC
                LIMIT 1
            """, (pair_key,))

            row = cursor.fetchone()

            if row and row["two_sigma"] is not None:
                return float(row["two_sigma"])
            else:
                return None
    finally:
        conn.close()


def compute_live_two_sigma_pct(stats, price_y, price_x):
    sigma = float(stats["sigma"])
    beta = float(stats["beta_reg"])
    # beta's sign controls the X-leg direction.  Both legs still consume
    # gross capital, so their notionals add rather than cancel here.
    denominator = float(price_y) + abs(beta) * float(price_x)
    if not math.isfinite(denominator) or math.isclose(denominator, 0.0, abs_tol=1e-12):
        return None
    return (2.0 * sigma / denominator) * 100.0


def compute_live_sigma_for_stop_pct(stats, price_y, price_x):
    two_sigma = compute_live_two_sigma_pct(stats, price_y, price_x)
    return None if two_sigma is None else two_sigma / 2.0
# ===== 获取账户 summary =====
summary = ib.accountSummary()
data = {}

for item in summary:
    acc = item.account
    if acc not in data:
        data[acc] = {}
    data[acc][item.tag] = item.value

def cnh_to_usd(x, fx):
    try:
        return float(x) / fx
    except:
        return 0

def build_account_panel(account_data, pnl_data, fx, total_exposure_pct=0):
    net_liq = cnh_to_usd(account_data.get("NetLiquidation", 0), fx)
    avail = cnh_to_usd(account_data.get("AvailableFunds", 0), fx)

    unreal = cnh_to_usd(pnl_data.get("UnrealizedPnL", 0), fx)
    realized = cnh_to_usd(pnl_data.get("RealizedPnL", 0), fx)

    pnl_color = "green" if unreal > 0 else "red"

    def exposure_emoji(v):
        if v < 40:
            return "🟢"
        elif v < 60:
            return "🟡"
        elif v < 90:
            return "🟠"
        else:
            return "🔴"

    exposure_icon = exposure_emoji(total_exposure_pct)

    text = (
        f"NetLiquidation : ${net_liq:,.0f}\n"
        f"AvailableFunds : ${avail:,.0f}\n"
        f"Unrealized PnL : $[{pnl_color}]{unreal:+.2f}[/{pnl_color}]\n"
        f"Realized PnL   : ${realized:+.2f}\n"
        f"Exposure       : {exposure_icon} {total_exposure_pct:.1f}% used"
    )

    return Panel(text, title="ACCOUNT (USD)", padding=(0,1))

def get_account_data():
    summary = ib.accountSummary()
    data = {}

    for item in summary:
        acc = item.account
        if acc not in data:
            data[acc] = {}
        data[acc][item.tag] = item.value

    return data

def to_float(x):
    try:
        return float(str(x).replace(',', ''))
    except:
        return 0

if TARGET_ACCOUNT in data:
    net_liq = to_float(data[TARGET_ACCOUNT].get('NetLiquidation', 0))
else:
    print(f"[WARN] Account {TARGET_ACCOUNT} not found!")


def is_market_open():
    eastern = pytz.timezone('US/Eastern')
    now = datetime.now(eastern)

    # 周末
    if now.weekday() >= 5:
        return False

    # 时间 9:30 - 16:00
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return False
    if now.hour >= 16:
        return False

    return True


def collect_account_positions():
    """Return the latest positions delivered by the open IB subscription."""
    positions_by_symbol = {}
    for p in ib.positions():
        if getattr(p, "account", None) != TARGET_ACCOUNT:
            continue
        # IB reports FX balances/conversions as CASH contracts (for example
        # USD.CNH).  They are account cash, not tradable equity positions, so
        # keep them out of the dashboard's stock/pair position pipeline.
        if str(getattr(p.contract, "secType", "") or "").upper() == "CASH":
            continue
        symbol = p.contract.symbol
        positions_by_symbol[symbol] = {
            "qty": p.position,
            "avg_price": p.avgCost,
        }
    return positions_by_symbol


# ===== 获取持仓 =====

positions = collect_account_positions()

dbg(f"initial positions count={len(positions)}")
for _sym, _pos in positions.items():
    dbg(f"position {_sym}: qty={_pos['qty']}, avg={_pos['avg_price']:.4f}")


# ===== 获取实时价格 =====
def get_prices_auto(symbols, positions):
    if is_market_open():
        print("🟢 Using LIVE market data (RTH)")
        return get_prices_RTH(symbols, positions)
    else:
        print("🟡 Using HISTORICAL fallback (PRE/CLOSED)")
        return get_prices_NoRTH(symbols, positions)
    
def get_prices_RTH(symbols, positions):
    prices = {}
    changes = {}

    # RTH prices come from livedata via Redis.  Qualifying IB contracts here
    # adds no information, and a stalled qualification blocks the whole board
    # before it can publish its snapshot.
    for symbol in symbols:
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            continue
        print(f">>> requesting live price for {symbol}")

        raw = redis_clent.get(f"price:{symbol}")
        print(f"[REDIS READ] {symbol} -> {raw}")
        if raw is None:
            price = None
        else:
            price = float(raw)

        prices[symbol] = price

        if price is not None and symbol in positions:
            avg = positions[symbol]["avg_price"]
            changes[symbol] = price - avg
        else:
            changes[symbol] = 0

    return prices, changes

def get_price_with_retry(symbol, max_retry=10, delay=5):
    retry = 0

    while retry < max_retry:
        raw = redis_clent.get(f"price:{symbol}")
        print(f"[REDIS READ] {symbol} -> {raw}")

        if raw is not None:
            return float(raw)

        retry += 1
        print(f"⚠️ {symbol} price not ready, retry {retry}/{max_retry} after {delay}s...")
        time.sleep(delay)

    print(f"❌ {symbol} price fetch failed after {max_retry} retries")
    return None

def get_prices_NoRTH(symbols, positions):
    print(">>> ENTER get_prices")
    start_all = time.time()
    dbg(f"NoRTH start | symbols={list(symbols)} | useRTH=True")

    contracts = [Stock(sym, 'SMART', 'USD') for sym in symbols]
    print(">>> contracts built")

    bars_list = []
    req_timeout_sec = 15

    for c in contracts:
        print(f">>> requesting {c.symbol}")
        t_req = time.time()
        try:
            bars = ib.reqHistoricalData(
                c,
                endDateTime="",
                durationStr="100 S",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
                timeout=req_timeout_sec
            )
            dbg(
                f"{c.symbol}: reqHistoricalData done in {time.time()-t_req:.2f}s "
                f"(timeout={req_timeout_sec}s)"
            )
        except Exception as e:
            dbg(
                f"{c.symbol}: reqHistoricalData exception after "
                f"{time.time()-t_req:.2f}s | {e}"
            )
            bars = None

        bars_list.append(bars)

    prices = {}
    changes = {}

    for c, bars in zip(contracts, bars_list):
        if bars:
            price = bars[-1].close
            last_bar = bars[-1]
            dbg(
                f"{c.symbol}: bars={len(bars)}, "
                f"last_time={getattr(last_bar, 'date', 'NA')}, last_close={price}"
            )
        else:
            price = None
            dbg(f"{c.symbol}: bars=0 (empty with useRTH=True)")

        prices[c.symbol] = price

        if price is not None and c.symbol in positions:
            avg = positions[c.symbol]["avg_price"]
            changes[c.symbol] = price - avg
        else:
            changes[c.symbol] = 0

    dbg(f"NoRTH total elapsed: {time.time()-start_all:.2f}s")

    return prices, changes


def get_latest_bar_prices(symbols, max_age_seconds=LATEST_BAR_PRICE_CACHE_SECONDS):
    clean_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if symbol})
    if not clean_symbols:
        return {}

    now = time.time()
    cached_symbols = LATEST_BAR_PRICE_CACHE.get("symbols", set())
    cached_prices = LATEST_BAR_PRICE_CACHE.get("prices", {})
    cache_age = now - float(LATEST_BAR_PRICE_CACHE.get("updated_at", 0.0) or 0.0)
    if cache_age <= max_age_seconds and set(clean_symbols).issubset(cached_symbols):
        return {
            symbol: cached_prices[symbol]
            for symbol in clean_symbols
            if symbol in cached_prices
        }

    bar_prices = {}
    for ticker in clean_symbols:
        bar = load_latest_bar(
            redis_clent,
            ticker,
            max_age_seconds=LIVE_BAR_MAX_AGE_SECONDS,
        )
        if bar:
            bar_prices[ticker] = float(bar["close"])

    LATEST_BAR_PRICE_CACHE["updated_at"] = now
    LATEST_BAR_PRICE_CACHE["symbols"] = set(clean_symbols)
    LATEST_BAR_PRICE_CACHE["prices"] = bar_prices

    return bar_prices
    
# ===== 找 active pairs =====
def find_active_pairs(positions, pairs, beta_map=None, include_single_leg=False):
    active = {}

    for name, info in pairs.items():
        y = info["y"]
        x = info["x"]
        pair_key = str(info.get("pair_key") or "").strip().lower()
        beta_value = None if beta_map is None else beta_map.get(pair_key)
        single_leg_mode = include_single_leg and beta_value is not None and abs(float(beta_value)) < 1e-12

        y_qty = float(positions.get(y, {}).get("qty", 0) or 0)
        x_qty = float(positions.get(x, {}).get("qty", 0) or 0)
        # IB keeps zero-quantity symbols in its position snapshot. Membership
        # alone is not a held leg, so it must not turn QQQ + flat TLT into a
        # qqq_tlt pair.
        if y_qty != 0 and (x_qty != 0 or single_leg_mode):
            active[name] = info   # 🔥 直接存 info

    return active

# ===== 计算 beta =====
def compute_beta(pos_y, pos_x):
    if pos_y == 0:
        return None
    return abs(pos_x / pos_y)


# ===== PnL =====
def leg_pnl(qty, avg, price):
    return qty * (price - avg)


def get_position_snapshot(positions, symbol, fallback_price=0.0):
    if symbol in positions:
        return positions[symbol]
    return {
        "qty": 0.0,
        "avg_price": float(fallback_price),
    }

# ===== 主计算 =====
def compute_pair(pair_name, y, x, positions, prices):
    price_y = prices[y]
    price_x = prices[x]
    pos_y_info = get_position_snapshot(positions, y, price_y)
    pos_x_info = get_position_snapshot(positions, x, price_x)

    pos_y = pos_y_info["qty"]
    pos_x = pos_x_info["qty"]

    avg_y = pos_y_info["avg_price"]
    avg_x = pos_x_info["avg_price"]

    pnl_y = leg_pnl(pos_y, avg_y, price_y)
    pnl_x = leg_pnl(pos_x, avg_x, price_x)

    beta = compute_beta(pos_y, pos_x)

    total = pnl_y + pnl_x
    entry_notional = abs(avg_y * pos_y) + abs(avg_x * pos_x)
    current_notional = abs(price_y * pos_y) + abs(price_x * pos_x)
    return {
        "pair": pair_name,
        "beta": beta,
        "pnl_y": pnl_y,
        "pnl_x": pnl_x,
        "total": total,
        "entry_notional": entry_notional,
        "current_notional": current_notional
    }

def compute_zscore_and_return(y, x, prices, positions, stats, total):

    beta_reg = stats["beta_reg"]
    alpha = stats["alpha"]
    mu = stats["mu"]
    sigma = stats["sigma"]

    if sigma == 0:
        return {"z_now": 0, "z_entry": 0, "return_pct": 0}

    py = prices[y]
    px = prices[x]

    pos_y_info = get_position_snapshot(positions, y, py)
    pos_x_info = get_position_snapshot(positions, x, px)
    avg_y = pos_y_info["avg_price"]
    avg_x = pos_x_info["avg_price"]

    qty_y = pos_y_info["qty"]
    qty_x = pos_x_info["qty"]

    # ===== spread =====
    spread_now = py - (beta_reg * px + alpha)
    spread_entry = avg_y - (beta_reg * avg_x + alpha)

    # ===== z =====
    z_now = (spread_now - mu) / sigma
    z_entry = (spread_entry - mu) / sigma

    # ===== direction =====
    if qty_y > 0 and qty_x < 0:
        direction = 1   # long spread
    elif qty_y < 0 and qty_x > 0:
        direction = -1  # short spread
    elif qty_y > 0 and qty_x == 0:
        direction = 1
    elif qty_y < 0 and qty_x == 0:
        direction = -1
    else:
        direction = 0

    # ===== return =====

    notional = abs(qty_y * avg_y) + abs(qty_x * avg_x)
    ret = (total / notional * 100) if notional > 0 else 0.0

    return {
        "z_now": z_now,
        "z_entry": z_entry,
        "return_pct": ret,  
        "direction": direction
    }

def build_pair_panel(pair_name, y, x, r, prices, changes, positions, z, net_liq, USDCNH):
    table = Table(expand=True)
    current_notional = r.get("current_notional", 0)

    max_capital = MAX_POSITION_MULTIPLIER * net_liq if net_liq else 0

    ratio = current_notional / max_capital if max_capital > 0 else 0

    def exposure_emoji(v):
        if v < 0.3:
            return "🟢"
        elif v < 0.6:
            return "🟡"
        else:
            return "🔴"
    def return_style(v):
        if v >= 0:
            return "green", "🟢"
        elif v >= -0.4:
            return "yellow", "🟡"
        elif v >= -0.8:
            return "orange3", "🟠"
        else:
            return "red", "🔴"
    table.add_column("Symbol", justify="left")
    table.add_column("Pos", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Change", justify="right")
    table.add_column("PnL", justify="right")

    def color(v):
        return "green" if v > 0 else "red"
    def arrow(v):
        return "📈" if v > 0 else "📉" if v < 0 else "➖"
    table.add_row(
        y,
        f"{positions[y]['qty']:+}",
        f"{prices[y]:.2f}",
        f"{arrow(changes[y])} {changes[y]:+.2f}",
        f"[{color(r['pnl_y'])}]{r['pnl_y']:+.2f}[/{color(r['pnl_y'])}]"
    )

    table.add_row(
        x,
        f"{positions[x]['qty']:+}",
        f"{prices[x]:.2f}",
        f"{arrow(changes[x])} {changes[x]:+.2f}",
        f"[{color(r['pnl_x'])}]{r['pnl_x']:+.2f}[/{color(r['pnl_x'])}]"
    )

    # ✅ 解释信息（重点）
    ret = z['return_pct']
    ret_color, ret_emoji = return_style(ret)

    footer = (
        f"{z['z_now']:+.2f} "
        f"(entry {z['z_entry']:+.2f}) | "
        f" {ret_emoji} "
        f"[{ret_color}]{ret:+.2f}%[/{ret_color}]"
    )
    beta = r.get("beta")

    title = Text()
    title.append(f"📊 {pair_name}  {beta:.2f} {exposure_emoji(ratio)} "
                f"{ratio*100*USDCNH:.1f}% of ${max_capital/USDCNH:.2f}\n",
                style="bold")

    return Panel(
        table,
        title=title,
        title_align="left",
        subtitle=footer
    )


def build_single_panel(symbol, position, price, change_value, net_liq, USDCNH):
    qty = float(position.get("qty", 0))
    avg_price = float(position.get("avg_price", 0))
    market_price = float(price or 0)
    pnl_value = (market_price - avg_price) * qty
    current_notional = abs(qty) * market_price
    max_capital = MAX_POSITION_MULTIPLIER * net_liq if net_liq else 0
    exposure_pct = (current_notional / max_capital * 100 * USDCNH) if max_capital > 0 else 0.0
    entry_notional = abs(qty) * avg_price
    return_pct = (pnl_value / entry_notional * 100.0) if entry_notional > 0 else 0.0

    def color(v):
        if return_pct >= 0:
            return "green"
        if return_pct > -0.8:
            return "yellow"
        if return_pct > -2.0:
            return "orange3"
        return "red"

    def arrow(v):
        return "📈" if v > 0 else "📉" if v < 0 else "➖"

    def return_style(v):
        if v >= 0:
            return "green", "🟢"
        if v > -0.8:
            return "yellow", "🟡"
        if v > -2.0:
            return "orange3", "🟠"
        return "red", "🔴"

    table = Table(expand=True)
    table.add_column("Symbol", justify="left")
    table.add_column("Pos", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Change", justify="right")
    table.add_column("PnL", justify="right")
    table.add_row(
        symbol,
        f"{qty:+.0f}",
        f"{market_price:.2f}",
        f"{arrow(change_value)} {change_value:+.2f}",
        f"[{color(pnl_value)}]{pnl_value:+.2f}[/{color(pnl_value)}]",
    )

    side_label = "LONG" if qty > 0 else "SHORT"
    ret_color, ret_emoji = return_style(return_pct)
    title = f"🎯 {symbol} {side_label} | avg={avg_price:.2f} | exp={exposure_pct:.1f}%"
    footer = f"notional=${current_notional:,.0f} | {ret_emoji} [{ret_color}]{return_pct:+.2f}%[/{ret_color}]"

    return Panel(
        table,
        title=title,
        title_align="left",
        subtitle=footer,
    )


def flagship_pair_item(name, y, x, result, prices, changes, positions, z, net_liq, USDCNH):
    result = result or {}
    z = z or {}
    current_notional = result.get("current_notional", 0)
    max_capital = MAX_POSITION_MULTIPLIER * net_liq if net_liq else 0
    exposure_pct = (current_notional / max_capital * 100 * USDCNH) if max_capital > 0 else 0.0
    return {
        "kind": "pair",
        "name": name,
        "y": y,
        "x": x,
        "qty_y": float(positions[y].get("qty", 0)),
        "qty_x": float(positions[x].get("qty", 0)),
        "avg_y": float(positions[y].get("avg_price", 0)),
        "avg_x": float(positions[x].get("avg_price", 0)),
        "price_y": prices.get(y),
        "price_x": prices.get(x),
        "change_y": changes.get(y, 0),
        "change_x": changes.get(x, 0),
        "pnl_y": result.get("pnl_y"),
        "pnl_x": result.get("pnl_x"),
        "total": result.get("total"),
        "beta": result.get("beta"),
        "z_now": z.get("z_now"),
        "z_entry": z.get("z_entry"),
        "return_pct": z.get("return_pct"),
        "direction": z.get("direction"),
        "current_notional": current_notional,
        "exposure_pct": exposure_pct,
    }


def flagship_single_item(symbol, position, price, change_value, net_liq, USDCNH):
    qty = float(position.get("qty", 0))
    avg_price = float(position.get("avg_price", 0))
    market_price = None if price is None else float(price)
    pnl_value = None if market_price is None else (market_price - avg_price) * qty
    current_notional = None if market_price is None else abs(qty) * market_price
    max_capital = MAX_POSITION_MULTIPLIER * net_liq if net_liq else 0
    exposure_pct = (
        current_notional / max_capital * 100 * USDCNH
        if current_notional is not None and max_capital > 0
        else None
    )
    entry_notional = abs(qty) * avg_price
    return_pct = (
        pnl_value / entry_notional * 100.0
        if pnl_value is not None and entry_notional > 0
        else None
    )
    return {
        "kind": "single",
        "name": symbol,
        "symbol": symbol,
        "qty": qty,
        "avg_price": avg_price,
        "price": market_price,
        "change": change_value,
        "pnl": pnl_value,
        "return_pct": return_pct,
        "current_notional": current_notional,
        "exposure_pct": exposure_pct,
        "side": "LONG" if qty > 0 else "SHORT",
    }


def publish_flagship_snapshot(items, account_data, pnl_data, fx, total_exposure_pct):
    payload = {
        "ok": True,
        "source": "flagship_board",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_ts": time.time(),
        "count": len(items),
        "account": {
            "account": TARGET_ACCOUNT,
            "net_liq": cnh_to_usd(account_data.get("NetLiquidation", 0), fx),
            "available": cnh_to_usd(account_data.get("AvailableFunds", 0), fx),
            "unrealized_pnl": cnh_to_usd(pnl_data.get("UnrealizedPnL", 0), fx),
            "realized_pnl": cnh_to_usd(pnl_data.get("RealizedPnL", 0), fx),
            "exposure_pct": total_exposure_pct,
        },
        "items": items,
    }
    # Keep the last known board snapshot. Consumers use updated_ts/stale to
    # report liveness, so deleting the authoritative snapshot is unnecessary
    # and used to make the API silently fall back to database position tables.
    redis_clent.set(
        FLAGSHIP_SNAPSHOT_KEY,
        json.dumps(payload, ensure_ascii=False, default=str),
    )


def compute_single_return_pct(position, price):
    qty = float(position.get("qty", 0))
    avg_price = float(position.get("avg_price", 0))
    market_price = float(price or 0)
    entry_notional = abs(qty) * avg_price
    if entry_notional <= 0:
        return 0.0
    pnl_value = (market_price - avg_price) * qty
    return pnl_value / entry_notional * 100.0


def update_single_max_return(symbol, return_pct):
    max_ret = execution.get_single_max_return(symbol)
    if max_ret is not None and max_ret < 0:
        max_ret = 0.0
        execution.update_single_max_return(symbol, max_ret)

    if max_ret is None or return_pct > max_ret:
        max_ret = max(return_pct, 0.0)
        execution.update_single_max_return(symbol, max_ret)

    return max_ret if max_ret is not None else 0.0


def maybe_exit_single_position(symbol, return_pct, max_ret):
    stop_cfg = execution.resolve_single_stop_loss(
        single_name=symbol,
        ret=return_pct,
        max_ret=max_ret,
    )
    take_profit_cfg = execution.resolve_single_take_profit(
        single_name=symbol,
        ret=return_pct,
        max_ret=max_ret,
    )

    exit_cfg = None
    if stop_cfg and stop_cfg.get("armed") and stop_cfg.get("threshold") is not None:
        if return_pct <= float(stop_cfg["threshold"]):
            exit_cfg = stop_cfg

    if exit_cfg is None and take_profit_cfg and take_profit_cfg.get("armed") and take_profit_cfg.get("threshold") is not None:
        comparator = take_profit_cfg.get("comparator")
        metric = take_profit_cfg.get("metric")
        threshold = take_profit_cfg["threshold"]
        if metric == "time_ny":
            exit_cfg = take_profit_cfg
        elif comparator == ">=" and return_pct >= float(threshold):
            exit_cfg = take_profit_cfg
        elif comparator == "<=" and return_pct <= float(threshold):
            exit_cfg = take_profit_cfg

    if exit_cfg is None:
        return

    state = pdt.get_single_state(symbol)
    if state == "NEW":
        print(f"SKIP SINGLE EXIT PDT NEW: {symbol}")
        return

    trigger_key = f"single:{symbol}"
    if trigger_key in triggered_stop:
        return

    threshold_text = (
        str(exit_cfg["threshold"])
        if exit_cfg.get("metric") == "time_ny"
        else f"{float(exit_cfg['threshold']):.2f}"
    )
    print(
        f"SINGLE EXIT: {symbol} ret={return_pct:.2f} hit {threshold_text} "
        f"({exit_cfg['exit_reason']}, {exit_cfg['rule_text']})"
    )
    execution.close_single(
        symbol,
        return_pct=return_pct,
        exit_reason=exit_cfg["exit_reason"],
        event_context={
            "exit_mode": exit_cfg.get("mode"),
            "rule_text": exit_cfg.get("rule_text"),
            "trigger_line": exit_cfg.get("threshold"),
        },
    )
    triggered_stop.add(trigger_key)


def find_unpaired_symbols(positions, active_pairs):
    paired_symbols = set()
    for info in active_pairs.values():
        paired_symbols.add(info["y"])
        paired_symbols.add(info["x"])

    return [
        symbol
        for symbol, pos in positions.items()
        if abs(float(pos.get("qty", 0))) > 0 and symbol not in paired_symbols
    ]

def build_links_panel(active_pairs):
    table = Table(expand=True, show_header=False)

    table.add_column()

    for name in active_pairs.keys():
        url = "http://localhost:8501/?pair=" + urllib.parse.quote(name)

        table.add_row(
            f"[bold]{name}[/bold]  [bright_blue]{url}[bright_blue]"
        )

    return table

def build_layout():
    layout = Layout()

    layout.split_column(
        Layout(name="top", size=7),
        Layout(name="body",ratio=1),
        #Layout(name="bottom", size=5)
    )

    layout["body"].split_row()

    return layout

# ===== 分析 =====
beta_map = pdt.load_latest_scheduled_order_beta_map()
active_pairs = find_active_pairs(positions, PAIRS, beta_map=beta_map, include_single_leg=False)
monitored_pairs = find_active_pairs(positions, PAIRS, beta_map=beta_map, include_single_leg=True)
dbg(f"active pairs count={len(active_pairs)}")
dbg(f"monitored pairs count={len(monitored_pairs)}")
if not active_pairs:
    dbg("active pairs empty: pair shown only when both legs exist in current IB positions")
else:
    dbg(f"active pairs: {list(active_pairs.keys())}")
unpaired_symbols = find_unpaired_symbols(positions, active_pairs)
if unpaired_symbols:
    dbg(f"single-leg positions: {unpaired_symbols}")
last_prices = {}
all_symbols = set(unpaired_symbols)
for info in monitored_pairs.values():
    all_symbols.add(info["y"])
    all_symbols.add(info["x"])

def preload_stats(pairs):
    stats_cache = {}
    for info in pairs.values():
        pair_key = info["pair_key"]

        stats = fetch_pair_stats(pair_key)
        if stats:
            stats["snapshot_checked_at"] = time.monotonic()
            stats_cache[pair_key] = stats

    return stats_cache


def ensure_stats_cache(stats_cache, pairs, force_refresh=False):
    now_monotonic = time.monotonic()
    for info in pairs.values():
        pair_key = info["pair_key"]
        cached = stats_cache.get(pair_key)
        if (
            cached
            and not force_refresh
            and now_monotonic - cached.get("snapshot_checked_at", 0.0) < ROLLING_SNAPSHOT_POLL_SECONDS
        ):
            continue
        stats = fetch_pair_stats(pair_key, force_refresh=force_refresh)
        if stats:
            stats["snapshot_checked_at"] = now_monotonic
            stats_cache[pair_key] = stats
    return stats_cache

stats_cache = preload_stats(monitored_pairs)
layout = build_layout()

# 🔥 先算一次数据
prices, changes = get_prices_auto(list(all_symbols), positions)

for sym in prices:
    if prices[sym] is not None and prices[sym] != 0:
        last_prices[sym] = prices[sym]
    else:
        if sym in last_prices:
            prices[sym] = last_prices[sym]

panels = []
total_exposure_pct = 0.0
single_bar_prices = get_latest_bar_prices(unpaired_symbols)

for name, info in active_pairs.items():
    y = info["y"]
    x = info["x"]

    if y not in positions or x not in positions:
        continue
    if y not in prices or x not in prices:
        continue
    if prices[y] is None or prices[x] is None:
        continue

    pair_key = info["pair_key"]

    pair_result = compute_pair(name, y, x, positions, prices)
    stats = stats_cache[pair_key]
    z = compute_zscore_and_return(y, x, prices, positions, stats, pair_result["total"])

    current_notional = pair_result.get("current_notional", 0)
    max_capital = MAX_POSITION_MULTIPLIER * net_liq if net_liq else 0
    ratio = current_notional / max_capital if max_capital > 0 else 0
    total_exposure_pct += ratio * 100 * USDCNH

    panels.append(
        build_pair_panel(name, y, x, pair_result, prices, changes, positions, z, net_liq, USDCNH)
    )

for symbol in unpaired_symbols:
    if symbol not in positions or symbol not in prices or prices[symbol] is None:
        continue
    qty = float(positions[symbol]["qty"])
    current_notional = abs(qty) * float(prices[symbol])
    max_capital = MAX_POSITION_MULTIPLIER * net_liq if net_liq else 0
    ratio = current_notional / max_capital if max_capital > 0 else 0
    total_exposure_pct += ratio * 100 * USDCNH
    bar_price = single_bar_prices.get(symbol)
    if bar_price is not None:
        single_ret = compute_single_return_pct(positions[symbol], bar_price)
        update_single_max_return(symbol, single_ret)
    else:
        dbg(f"skip single max_return {symbol}: latest 5min bar missing")
    panels.append(
        build_single_panel(
            symbol,
            positions[symbol],
            prices[symbol],
            changes.get(symbol, 0),
            net_liq,
            USDCNH,
        )
    )

layout["top"].update(
    build_account_panel(
        data[TARGET_ACCOUNT],
        data.get("All", {}),
        USDCNH,
        total_exposure_pct
    )
)

layout["body"].update(Columns(panels, equal=True, expand=False))

with Live(layout, refresh_per_second=1, screen=True):
    loop_idx = 0

    while True:
        loop_idx += 1
        if connect == 4002:
            USDCNH = fetch_usdcnh()
        else:
            USDCNH = 1.0
        positions = collect_account_positions()
        try:
            unlocked_reentry = pdt.reset_auto_reentry_locks_for_missing_positions(positions)
        except Exception as exc:
            # Reentry maintenance must never take down the position monitor or
            # leave the dashboard serving its last snapshot.
            dbg(f"reentry lock maintenance skipped: {exc}")
            unlocked_reentry = []
        for item in unlocked_reentry:
            print(
                f"AUTO REENTRY UNLOCK: {item['pair_name']} lock -> 0 "
                f"(position missing: {item['target']})"
            )
        beta_map = pdt.load_latest_scheduled_order_beta_map()
        active_pairs = find_active_pairs(positions, PAIRS, beta_map=beta_map, include_single_leg=False)
        monitored_pairs = find_active_pairs(positions, PAIRS, beta_map=beta_map, include_single_leg=True)
        stats_cache = ensure_stats_cache(stats_cache, monitored_pairs)
        unpaired_symbols = find_unpaired_symbols(positions, active_pairs)
        all_symbols = set(unpaired_symbols)
        for info in monitored_pairs.values():
            all_symbols.add(info["y"])
            all_symbols.add(info["x"])
        prices, changes = get_prices_auto(list(all_symbols), positions)
        single_bar_prices = get_latest_bar_prices(unpaired_symbols)
        if loop_idx % 10 == 1:
            dbg(
                f"loop={loop_idx} | positions={len(positions)} | "
                f"active_pairs={len(active_pairs)} | monitored_pairs={len(monitored_pairs)} | "
                f"single_positions={len(unpaired_symbols)} | "
                f"symbols_for_price={len(all_symbols)}"
            )
        # ===== 同步数据库状态 =====
        pdt.sync_pair_positions_with_db(positions)
        pdt.sync_single_positions_with_db(positions)

        # ===== 自动识别新开仓 =====
        pdt.detect_new_positions_and_record(positions, PAIRS)
        pdt.detect_new_single_positions_and_record(positions, unpaired_symbols)
        for sym in prices:
            if prices[sym] is not None and prices[sym] != 0:
                last_prices[sym] = prices[sym]   # ✅ 更新缓存
            else:
                # ❗没有新数据 → 用旧数据
                if sym in last_prices:
                    prices[sym] = last_prices[sym]
                else:
                    prices[sym] = None

        # 保存最新价格
        get_prices_auto.last_prices = prices.copy()
        # ===== ACCOUNT =====
        layout["top"].update(
            build_account_panel(
                data[TARGET_ACCOUNT],
                data.get("All", {}),
                USDCNH,
                total_exposure_pct
            )
        )

        # ===== PAIRS =====
        panels = []
        flagship_items = []
        total_exposure_pct = 0.0
        for name, info in active_pairs.items():
            y = info["y"]
            x = info["x"]
            if prices.get(y) is None or prices.get(x) is None:
                dbg(f"skip pair {name}: price missing y={y}:{prices.get(y)} x={x}:{prices.get(x)}")
                flagship_items.append(
                    flagship_pair_item(
                        name, y, x, {}, prices, changes, positions, {}, net_liq, USDCNH
                    )
                )
                continue
            pair_key = info["pair_key"]

            r = compute_pair(name, y, x, positions, prices)
            current_notional = r.get("current_notional", 0)
            max_capital = MAX_POSITION_MULTIPLIER * net_liq if net_liq else 0

            ratio = current_notional / max_capital if max_capital > 0 else 0

            total_exposure_pct += ratio * 100 * USDCNH   # 👈 核心
            stats = stats_cache[pair_key]
            z = compute_zscore_and_return(y, x, prices, positions, stats, r["total"])

            panels.append(
                build_pair_panel(name, y, x, r, prices, changes, positions, z, net_liq, USDCNH)
            )
            flagship_items.append(
                flagship_pair_item(name, y, x, r, prices, changes, positions, z, net_liq, USDCNH)
            )
        for symbol in unpaired_symbols:
            if symbol not in prices or prices[symbol] is None:
                dbg(f"skip single {symbol}: price missing {prices.get(symbol)}")
                flagship_items.append(
                    flagship_single_item(
                        symbol,
                        positions[symbol],
                        prices.get(symbol),
                        changes.get(symbol, 0),
                        net_liq,
                        USDCNH,
                    )
                )
                continue
            qty = float(positions[symbol]["qty"])
            current_notional = abs(qty) * float(prices[symbol])
            max_capital = MAX_POSITION_MULTIPLIER * net_liq if net_liq else 0
            ratio = current_notional / max_capital if max_capital > 0 else 0
            total_exposure_pct += ratio * 100 * USDCNH
            bar_price = single_bar_prices.get(symbol)
            single_max_ret = execution.get_single_max_return(symbol)
            if bar_price is not None:
                bar_ret = compute_single_return_pct(positions[symbol], bar_price)
                single_max_ret = update_single_max_return(symbol, bar_ret)
            else:
                dbg(f"skip single max_return {symbol}: latest 5min bar missing")
            tick_ret = compute_single_return_pct(positions[symbol], prices[symbol])
            panels.append(
                build_single_panel(
                    symbol,
                    positions[symbol],
                    prices[symbol],
                    changes.get(symbol, 0),
                    net_liq,
                    USDCNH,
                )
            )
            flagship_items.append(
                flagship_single_item(
                    symbol,
                    positions[symbol],
                    prices[symbol],
                    changes.get(symbol, 0),
                    net_liq,
                    USDCNH,
                )
            )
            if execution.TRADING_ENABLED and is_market_open():
                maybe_exit_single_position(
                    symbol,
                    tick_ret,
                    single_max_ret if single_max_ret is not None else 0.0,
                )

        # ===== EXIT RULES =====
        if execution.TRADING_ENABLED and is_market_open():
            for name, info in monitored_pairs.items():
                y = info["y"]
                x = info["x"]
                if y not in prices or x not in prices:
                    continue
                if prices[y] is None or prices[x] is None:
                    continue

                pair_key = info["pair_key"]
                r = compute_pair(name, y, x, positions, prices)
                stats = stats_cache.get(pair_key)
                if not stats:
                    continue
                z = compute_zscore_and_return(y, x, prices, positions, stats, r["total"])
                ret = z["return_pct"]
                z_now = z.get("z_now")
                direction = z.get("direction")

                sigma_for_stop = compute_live_sigma_for_stop_pct(stats, prices[y], prices[x])

                max_ret = execution.get_pair_max_return(pair_key)
                if max_ret is not None and max_ret < 0:
                    max_ret = 0.0
                    execution.update_pair_max_return(pair_key, max_ret)

                if max_ret is None or ret > max_ret:
                    max_ret = max(ret, 0.0)
                    execution.update_pair_max_return(pair_key, max_ret)

                stop_cfg = execution.resolve_pair_stop_loss(
                    pair_name=pair_key,
                    ret=ret,
                    max_ret=max_ret,
                    sigma_value=sigma_for_stop
                )
                take_profit_cfg = execution.resolve_pair_take_profit(
                    pair_name=pair_key,
                    ret=ret,
                    max_ret=max_ret,
                    sigma_value=sigma_for_stop,
                    z_now=z_now,
                    direction=direction
                )

                state = pdt.get_pair_state(name)
                exit_cfg = None
                exit_value = None

                if ret <= stop_cfg["threshold"]:
                    exit_cfg = stop_cfg
                    exit_value = ret
                elif take_profit_cfg and take_profit_cfg.get("armed"):
                    metric = take_profit_cfg.get("metric")
                    threshold = take_profit_cfg.get("threshold")
                    comparator = take_profit_cfg.get("comparator")

                    if metric == "time_ny" and threshold is not None:
                        exit_cfg = take_profit_cfg
                        exit_value = ret
                    elif metric == "return_pct" and threshold is not None:
                        if comparator == ">=" and ret >= threshold:
                            exit_cfg = take_profit_cfg
                            exit_value = ret
                        elif comparator == "<=" and ret <= threshold:
                            exit_cfg = take_profit_cfg
                            exit_value = ret
                    elif metric == "z_now" and threshold is not None and z_now is not None:
                        if comparator == ">=" and z_now >= threshold:
                            exit_cfg = take_profit_cfg
                            exit_value = z_now
                        elif comparator == "<=" and z_now <= threshold:
                            exit_cfg = take_profit_cfg
                            exit_value = z_now

                if exit_cfg is not None:
                    if state == "NEW":
                        print(f"跳过退出（NEW）: {name}")
                        continue
                    if y in positions and x not in positions and pdt.get_single_state(y) == "NEW":
                        print(f"跳过单腿退出（SINGLE NEW）: {name} / {y}")
                        continue

                    if name not in triggered_stop:
                        metric_label = "ret" if exit_cfg.get("metric") != "z_now" else "z"
                        threshold_text = (
                            str(exit_cfg["threshold"])
                            if exit_cfg.get("metric") == "time_ny"
                            else f"{float(exit_cfg['threshold']):.2f}"
                        )
                        print(
                            f"EXIT: {name} {metric_label}={exit_value:.2f} hit {threshold_text} "
                            f"({exit_cfg['exit_reason']}, {exit_cfg['rule_text']})"
                        )

                        execution.close_pair(
                            y, x,
                            pair_name=pair_key,
                            return_pct=ret,
                            exit_reason=exit_cfg["exit_reason"],
                            event_context={
                                "display_name": name,
                                "ticker_y": y,
                                "ticker_x": x,
                                "total": r.get("total"),
                                "pnl": r.get("total"),
                                "trigger_line": exit_cfg.get("threshold"),
                                "exit_mode": exit_cfg.get("mode"),
                                "rule_text": exit_cfg.get("rule_text"),
                            },
                        )
                        triggered_stop.add(name)

        layout["body"].update(Columns(panels, equal=True, expand=False))
        publish_flagship_snapshot(
            flagship_items,
            data.get(TARGET_ACCOUNT, {}),
            data.get("All", {}),
            USDCNH,
            total_exposure_pct,
        )

        # Let ib_insync process position/order events raised by other clientIds.
        ib.sleep(1)
