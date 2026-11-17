import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())
from ib_insync import *
import pymysql
import pandas as pd
import numpy as np
import warnings
from datetime import datetime, timedelta
import pytz
from program_factory.port_listener import choose_connection

# ================== IB ==================
CLIENT_ID = 8
PREFERRED_PORT_ORDER = (4001, 4002)
PORT_ACCOUNT_MAP = {
    4001: "U18559662",
    4002: "DUO671947",
}
selection = choose_connection(
    port_account_map=PORT_ACCOUNT_MAP,
    preferred_order=PREFERRED_PORT_ORDER
)
CONNECT_PORT = selection.port
TARGET_ACCOUNT = selection.account

ib = IB()
ib.connect("127.0.0.1", CONNECT_PORT, clientId=CLIENT_ID)
print(
    f"[PORT] selected={CONNECT_PORT}, account={TARGET_ACCOUNT}, pid={selection.pid}, "
    f"process={selection.process_name}, reason={selection.reason}"
)

# ================== DB ==================
conn = pymysql.connect(
    host="localhost",
    user="quant",
    password="1q2w3e",
    database="quant_data",
    autocommit=True
)
cur = conn.cursor()

# ================== 时区 ==================
NY_TZ = pytz.timezone("US/Eastern")
BJ_TZ = pytz.timezone("Asia/Shanghai")

# ================== 工具函数 ==================

def to_bj_time(ib_bar_date):
    t = pd.Timestamp(ib_bar_date)
    if t.tzinfo is None:
        t = t.tz_localize(NY_TZ)
    return t.tz_convert(BJ_TZ).to_pydatetime().replace(tzinfo=None)

def to_utc_time(ib_bar_date):
    t = pd.Timestamp(ib_bar_date)
    if t.tzinfo is None:
        t = t.tz_localize(NY_TZ)
    return t.tz_convert("UTC").to_pydatetime().replace(tzinfo=None)

def get_last_time(ticker):
    cur.execute(
        """
        SELECT MAX(time_utc)
        FROM us_market_prices
        WHERE ticker=%s
        """,
        (ticker,)
    )
    row = cur.fetchone()
    return row[0] if row and row[0] else None

def insert_stock_price(ticker, t_utc, price):
    sql = """
    INSERT INTO us_market_prices
    (ticker, time_utc, price, source)
    VALUES (%s,%s,%s,'IB')
    ON DUPLICATE KEY UPDATE
        price=VALUES(price),
        source=VALUES(source)
    """
    cur.execute(sql, (ticker, t_utc, float(price)))

# ================== 核心逻辑 ==================

def load_metapairs():
    cur.execute("""
        SELECT pair_name, ticker_a, ticker_b
        FROM metapairs
        WHERE COALESCE(is_active, 1) != 0
    """)
    return cur.fetchall()


def load_us_info():
    cur.execute("""
        SELECT ticker, exchange, currency
        FROM us_info
        WHERE COALESCE(is_active, 1) = 1
        ORDER BY ticker
    """)
    return cur.fetchall()


def load_pair_config_map():
    memory_conn = pymysql.connect(
        host="localhost",
        user="quant",
        password="1q2w3e",
        database="quant_memory"
    )
    memory_cur = memory_conn.cursor()

    try:
        memory_cur.execute("""
            SELECT pair_name, train_start_bj, test_end_bj, fixed_beta
            FROM pair_config
            WHERE data_source = '5m'
        """)
        rows = memory_cur.fetchall()
    finally:
        memory_conn.close()

    return {
        str(pair_name).strip().lower(): {
            "train_start_bj": train_start_bj,
            "test_end_bj": test_end_bj,
            "fixed_beta": float(fixed_beta),
        }
        for pair_name, train_start_bj, test_end_bj, fixed_beta in rows
    }


def nearest_time_index(df, t):
    if t is None:
        return None

    ts = pd.to_datetime(t)
    idx = (df["time_bj"] - ts).abs().idxmin()
    return int(idx)


def calculate_latest_zscore(pair_name, A, B, config):
    """Return the latest causal fixed-beta rolling z-score for one configured pair."""
    if not config:
        return np.nan

    y_col = f"{A}_price"
    x_col = f"{B}_price"

    sql = f"""
        SELECT
            a.time_utc,
            DATE_ADD(a.time_utc, INTERVAL 8 HOUR) AS time_bj,
            a.price AS `{y_col}`,
            b.price AS `{x_col}`
        FROM us_market_prices a FORCE INDEX (idx_us_market_prices_ticker_time_price)
        JOIN us_market_prices b FORCE INDEX (idx_us_market_prices_ticker_time_price)
            ON b.ticker = %s
           AND b.time_utc = a.time_utc
        WHERE a.ticker = %s
        ORDER BY a.time_utc
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="pandas only supports SQLAlchemy connectable",
            category=UserWarning,
        )
        df = pd.read_sql(sql, conn, params=(B, A))

    rolling_window = 20 * 79
    if len(df) < rolling_window:
        return np.nan

    df["time_bj"] = pd.to_datetime(df["time_bj"])
    df = df.sort_values("time_bj").reset_index(drop=True)
    beta = float(config["fixed_beta"])
    df["spread"] = df[y_col].astype(float) - beta * df[x_col].astype(float)
    df["rolling_mean"] = df["spread"].rolling(rolling_window, min_periods=rolling_window).mean()
    df["rolling_sigma"] = df["spread"].rolling(rolling_window, min_periods=rolling_window).std(ddof=0)
    last = df.iloc[-1]
    mean = float(last["rolling_mean"])
    sigma = float(last["rolling_sigma"])
    if not np.isfinite(mean) or not np.isfinite(sigma) or sigma <= 0:
        return np.nan
    return (float(last["spread"]) - mean) / sigma


def refresh_all_zscores(pairs):
    print("\n===== 刷新 metapairs.zscore =====")

    configs = load_pair_config_map()
    updates = []

    # Remove legacy fixed-window z-scores from pairs that have no production
    # fixed-beta configuration, so no stale signal remains visible.
    cur.execute("""
        UPDATE metapairs
        SET zscore = NULL
        WHERE COALESCE(is_active, 1) != 0
    """)

    for pair_name, A, B in pairs:
        config = configs.get(str(pair_name).strip().lower())
        if config is None:
            continue

        try:
            zscore = calculate_latest_zscore(
                pair_name,
                A.upper(),
                B.upper(),
                config,
            )
        except Exception as e:
            print(f"⚠️ {pair_name} zscore 计算失败:", e)
            continue

        if not np.isfinite(zscore):
            print(f"⚠️ {pair_name} zscore 无效，跳过")
            continue

        cached_zscore = round(float(np.clip(zscore, -99.99, 99.99)), 2)
        updates.append((cached_zscore, pair_name))
        print(f"{pair_name}: zscore={cached_zscore:+.2f}")

    if not updates:
        print("没有可更新的 zscore")
        return

    cur.executemany(
        """
        UPDATE metapairs
        SET zscore=%s
        WHERE pair_name=%s
        """,
        updates
    )
    print(f"✅ zscore 更新完成: {len(updates)} pairs")


def backfill_ticker(ticker, exchange, currency):
    ticker = str(ticker).strip().upper()
    exchange = str(exchange or "SMART").strip()
    currency = str(currency or "USD").strip()

    print(f"\n===== 更新 {ticker} =====")

    last_time = get_last_time(ticker)
    print("数据库最新 UTC:", last_time)

    contract = Stock(ticker, exchange, currency)
    ib.qualifyContracts(contract)

    end_time = ""   # IB: "" = now

    CHUNK = "1 D"   # ⭐ 可以改成 1 W

    finished = False

    while not finished:

        print(f"\n⬇ 拉取 {CHUNK} 历史...")

        bars = ib.reqHistoricalData(
            contract,
            end_time,
            CHUNK,
            "5 mins",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )

        if not bars:
            print("⚠ 无数据，停止")
            break

        rows = [(to_utc_time(b.date), b) for b in bars]
        rows.sort(key=lambda item: item[0])

        # ✅ 写入数据
        inserted = 0
        for t_utc, bar in rows:
            if last_time and t_utc <= last_time:
                finished = True
                continue

            insert_stock_price(ticker, t_utc, bar.close)
            inserted += 1

        print(f"写入/更新 {inserted} bars")

        # ⭐ 核心：往前推进
        oldest = rows[0][0]

        # IB 需要字符串格式
        end_time = oldest.strftime("%Y%m%d %H:%M:%S")

        print("下一轮 end_time:", end_time)

        # 防止死循环
        if last_time and oldest <= last_time:
            print("✅ 已追溯到数据库断点")
            break


def main():
    stocks = load_us_info()
    pairs = load_metapairs()

    for ticker, exchange, currency in stocks:
        try:
            backfill_ticker(ticker, exchange, currency)
        except Exception as e:
            print(f"❌ {ticker} 失败:", e)
    refresh_all_zscores(pairs)
    ib.disconnect()
    conn.close()

if __name__ == "__main__":
    main()
