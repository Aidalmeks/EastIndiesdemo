"""Build data.js for the standalone, file:// compatible listings page."""

import csv
import json
import os
import re
from datetime import date, datetime
from decimal import Decimal

import pymysql


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(BASE_DIR, "data.js")
STANDALONE_FILE = os.path.join(BASE_DIR, "stock_listings.html")
NASDAQ_FILE = os.getenv(
    "NASDAQ_LISTED_FILE", "/home/quant/Downloads/nasdaqlisted.txt"
)
OTHER_FILE = os.getenv(
    "OTHER_LISTED_FILE", "/home/quant/Downloads/otherlisted.txt"
)
TRADINGVIEW_STOCKS_FILE = os.getenv(
    "TRADINGVIEW_STOCKS_FILE",
    os.path.join(BASE_DIR, "data", "All stocks_2026-07.csv"),
)
TRADINGVIEW_ETFS_FILE = os.getenv(
    "TRADINGVIEW_ETFS_FILE",
    os.path.join(BASE_DIR, "data", "ETF vault_2026-07.csv"),
)
DB_CONFIG = {
    "host": os.getenv("STOCK_DB_HOST", "127.0.0.1"),
    "user": os.getenv("STOCK_DB_USER", "quant"),
    "password": os.getenv("STOCK_DB_PASSWORD", "1q2w3e"),
    "database": os.getenv("STOCK_DB_NAME", "quant_data"),
}

NASDAQ_MARKETS = {
    "Q": "NASDAQ Global Select",
    "G": "NASDAQ Global Market",
    "S": "NASDAQ Capital Market",
}
OTHER_EXCHANGES = {
    "A": "NYSE American",
    "N": "NYSE",
    "P": "NYSE Arca",
    "Z": "Cboe BZX",
    "V": "IEX",
}
LEVERAGED_ETF_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:1\.25|1\.5|2|3|4)x(?![A-Za-z0-9])"
    r"|^ProShares Ultra(?:Pro|Short)?\b"
    r"|\bleveraged\b"
)


def json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ")
    if isinstance(value, Decimal):
        return float(value)
    return value


def infer_security_type(name, is_etf=False):
    text = str(name or "").lower()
    if is_etf:
        return "ETF"
    if "warrant" in text:
        return "Warrant"
    if " unit" in text or text.endswith("units"):
        return "Unit"
    if " right" in text:
        return "Right"
    if "preferred" in text or "preference" in text:
        return "Preferred"
    if "depositary" in text:
        return "Depositary"
    return "Stock"


def read_file(path, source):
    items = []
    creation_time = None
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="|"):
            first_value = next(iter(row.values()), "") or ""
            if str(first_value).startswith("File Creation Time:"):
                creation_time = str(first_value).split(":", 1)[1].strip()
                continue

            if source == "NASDAQ":
                symbol = str(row.get("Symbol") or "").strip().upper()
                name = str(row.get("Security Name") or "").strip()
                market_code = str(row.get("Market Category") or "").strip()
                exchange = "NASDAQ"
                market = NASDAQ_MARKETS.get(market_code, market_code or "NASDAQ")
                alt_symbol = symbol
            else:
                symbol = str(row.get("ACT Symbol") or "").strip().upper()
                name = str(row.get("Security Name") or "").strip()
                exchange_code = str(row.get("Exchange") or "").strip()
                exchange = OTHER_EXCHANGES.get(exchange_code, exchange_code or "Other")
                market = exchange
                alt_symbol = str(row.get("NASDAQ Symbol") or symbol).strip().upper()

            if not symbol:
                continue
            is_etf = str(row.get("ETF") or "").strip().upper() == "Y"
            is_test = str(row.get("Test Issue") or "").strip().upper() == "Y"
            items.append({
                "symbol": symbol,
                "alt_symbol": alt_symbol,
                "name": name,
                "source": source,
                "exchange": exchange,
                "market": market,
                "security_type": infer_security_type(name, is_etf),
                "is_etf": is_etf,
                "is_test": is_test,
                "round_lot": int(row.get("Round Lot Size") or 0),
                "financial_status": str(row.get("Financial Status") or "").strip(),
            })
    return items, creation_time


def load_registered():
    connection = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT ticker, sector, asset_class, instrument_type, currency,
                       exchange, is_active, created_at
                FROM us_info
                """
            )
            return {
                str(row["ticker"]).strip().upper(): {
                    key: json_value(value) for key, value in row.items()
                }
                for row in cursor.fetchall()
            }
    finally:
        connection.close()


def optional_float(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def tradingview_date(*paths):
    for path in paths:
        match = re.search(
            r"(\d{4}-\d{2}(?:-\d{2})?)", os.path.basename(path)
        )
        if match:
            return match.group(1)
    return None


def read_tradingview(path, category):
    """Read a TradingView export and collapse its duplicated turnover columns."""
    items = {}
    duplicate_turnover_columns = False
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        headers = next(reader)
        positions = {}
        for index, header in enumerate(headers):
            positions.setdefault(header, []).append(index)

        monthly_turnover_name = "Price × volume (turnover), 1 month"
        daily_turnover_name = "Price × volume (turnover), 1 day"
        turnover_name = (
            monthly_turnover_name
            if positions.get(monthly_turnover_name)
            else daily_turnover_name
        )
        turnover_period = (
            "1 month" if turnover_name == monthly_turnover_name else "1 day"
        )
        turnover_currency_name = f"{turnover_name} - Currency"
        turnover_indexes = positions.get(turnover_name, [])
        turnover_currency_indexes = positions.get(turnover_currency_name, [])
        if not turnover_indexes:
            raise ValueError(f"{path} 缺少成交额列")
        duplicate_turnover_columns = len(turnover_indexes) > 1

        def value(row, name):
            indexes = positions.get(name, [])
            return row[indexes[0]].strip() if indexes and indexes[0] < len(row) else ""

        for rank, row in enumerate(reader, start=1):
            symbol = value(row, "Symbol").upper()
            if not symbol:
                continue
            if len(turnover_indexes) > 1:
                values = {row[index].strip() for index in turnover_indexes}
                if len(values) > 1:
                    raise ValueError(f"{path} 的重复成交额列数值不一致：{symbol}")
            if len(turnover_currency_indexes) > 1:
                values = {row[index].strip() for index in turnover_currency_indexes}
                if len(values) > 1:
                    raise ValueError(f"{path} 的重复成交额币种列数值不一致：{symbol}")

            market_value_name = (
                "Market capitalization" if category == "Stock"
                else "Assets under management"
            )
            market_value_currency_name = f"{market_value_name} - Currency"
            items[symbol] = {
                "category": category,
                "rank": rank,
                "description": value(row, "Description"),
                "price": optional_float(value(row, "Price")),
                "currency": value(row, "Price - Currency"),
                "change_pct": optional_float(value(row, "Price change %, 1 day")),
                "volume": optional_float(value(row, "Volume, 1 day")),
                "relative_volume": optional_float(value(row, "Relative volume, 1 day")),
                "turnover": optional_float(row[turnover_indexes[0]]),
                "turnover_period": turnover_period,
                "turnover_currency": (
                    row[turnover_currency_indexes[0]].strip()
                    if turnover_currency_indexes else ""
                ),
                "market_value": optional_float(value(row, market_value_name)),
                "market_value_currency": value(row, market_value_currency_name),
                "sector": value(row, "Sector"),
                "analyst_rating": value(row, "Analyst rating"),
                "asset_class": value(row, "Asset class"),
                "focus": value(row, "Focus"),
                "expense_ratio": optional_float(value(row, "Expense ratio")),
            }
    return items, {
        "rows": len(items),
        "duplicate_turnover_columns_collapsed": duplicate_turnover_columns,
        "turnover_available": sum(
            item["turnover"] is not None for item in items.values()
        ),
        "turnover_period": turnover_period,
    }


def is_cboe_leveraged_etf(item):
    market_data = item.get("market_data") or {}
    if item.get("exchange") != "Cboe BZX":
        return False
    if market_data.get("category") != "ETF":
        return False
    name = market_data.get("description") or item.get("name") or ""
    return bool(LEVERAGED_ETF_PATTERN.search(str(name)))


def read_cached_tradingview(category):
    """Preserve an embedded snapshot when its original CSV is no longer present."""
    if not os.path.exists(OUTPUT_FILE):
        raise FileNotFoundError(
            f"{TRADINGVIEW_ETFS_FILE} 不存在，且没有可继承的 data.js"
        )
    with open(OUTPUT_FILE, "r", encoding="utf-8") as handle:
        encoded = handle.read().strip()
    prefix = "window.STOCK_LISTINGS_DATA="
    if not encoded.startswith(prefix) or not encoded.endswith(";"):
        raise ValueError("现有 data.js 格式不正确，无法继承 ETF 快照")
    payload = json.loads(encoded[len(prefix):-1])
    cached = {}
    for item in payload.get("items", []):
        market_data = item.get("market_data")
        if market_data and market_data.get("category") == category:
            market_data = dict(market_data)
            market_data.pop("global_rank", None)
            cached[str(item["symbol"]).strip().upper()] = market_data
    if not cached:
        raise ValueError(f"现有 data.js 中没有可继承的 {category} 快照")

    key = "etfs" if category == "ETF" else "stocks"
    previous_stats = dict(
        payload.get("liquidity_summary", {}).get(key, {})
    )
    previous_stats.setdefault("rows", len(cached))
    previous_stats.setdefault(
        "turnover_available",
        sum(item.get("turnover") is not None for item in cached.values()),
    )
    previous_stats["cached_from_embedded_data"] = True
    previous_stats["outside_directory"] = max(
        0, previous_stats["rows"] - len(cached)
    )
    return cached, previous_stats


def main():
    nasdaq_items, nasdaq_created = read_file(NASDAQ_FILE, "NASDAQ")
    other_items, other_created = read_file(OTHER_FILE, "OTHER")
    stock_snapshot, stock_stats = read_tradingview(
        TRADINGVIEW_STOCKS_FILE, "Stock"
    )
    if os.path.exists(TRADINGVIEW_ETFS_FILE):
        etf_snapshot, etf_stats = read_tradingview(
            TRADINGVIEW_ETFS_FILE, "ETF"
        )
    else:
        etf_snapshot, etf_stats = read_cached_tradingview("ETF")
    tradingview = {**stock_snapshot, **etf_snapshot}
    registered = load_registered()

    all_items = []
    for item in nasdaq_items + other_items:
        if item["is_test"]:
            continue
        registration = registered.get(item["symbol"])
        all_items.append({
            **item,
            "registered": registration is not None,
            "registration": registration,
            "market_data": tradingview.get(item["symbol"]),
        })

    excluded_no_turnover = [
        item for item in all_items
        if item["market_data"] is None
        or item["market_data"]["turnover"] is None
    ]
    excluded_cboe_leveraged = [
        item for item in all_items if is_cboe_leveraged_etf(item)
    ]
    excluded_symbols = {
        item["symbol"]
        for item in excluded_no_turnover + excluded_cboe_leveraged
    }
    items = [
        item for item in all_items
        if item["symbol"] not in excluded_symbols
    ]

    ranked_items = sorted(
        items,
        key=lambda item: (-item["market_data"]["turnover"], item["symbol"]),
    )
    for global_rank, item in enumerate(ranked_items, start=1):
        item["market_data"]["global_rank"] = global_rank

    items.sort(
        key=lambda item: (
            (item["market_data"] or {}).get("turnover") is None,
            -((item["market_data"] or {}).get("turnover") or 0),
            item["symbol"],
        )
    )

    matched_symbols = {
        item["symbol"] for item in all_items if item["market_data"] is not None
    }
    turnover_matched = sum(
        item["market_data"] is not None
        and item["market_data"]["turnover"] is not None
        for item in all_items
    )
    registered_with_snapshot = sum(
        item["registered"] and item["market_data"] is not None
        for item in all_items
    )
    registered_count = sum(item["registered"] for item in all_items)
    active_count = sum(
        item["registered"] and bool(item["registration"].get("is_active"))
        for item in all_items
    )
    eligible_registered_count = sum(item["registered"] for item in items)
    eligible_active_count = sum(
        item["registered"] and bool(item["registration"].get("is_active"))
        for item in items
    )

    payload = {
        "items": items,
        "source_created_at": nasdaq_created or other_created,
        "liquidity_as_of": tradingview_date(
            TRADINGVIEW_STOCKS_FILE, TRADINGVIEW_ETFS_FILE
        ),
        "liquidity_summary": {
            "provider": "TradingView",
            "stocks": stock_stats,
            "etfs": etf_stats,
            "exported_total": stock_stats["rows"] + etf_stats["rows"],
            "matched": len(matched_symbols),
            "turnover_matched": turnover_matched,
            "unmatched_directory": len(all_items) - len(matched_symbols),
            "outside_directory": (
                len(set(tradingview) - {item["symbol"] for item in all_items})
                + etf_stats.get("outside_directory", 0)
            ),
            "registered_with_snapshot": registered_with_snapshot,
        },
        "universe_summary": {
            "listed_total": len(all_items),
            "eligible_total": len(items),
            "excluded_total": len(all_items) - len(items),
            "excluded_no_turnover": len(excluded_no_turnover),
            "excluded_cboe_leveraged": len(excluded_cboe_leveraged),
            "registered_total": registered_count,
            "active_total": active_count,
            "eligible_registered": eligible_registered_count,
            "eligible_active": eligible_active_count,
        },
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with open(OUTPUT_FILE, "w", encoding="utf-8") as handle:
        handle.write("window.STOCK_LISTINGS_DATA=")
        handle.write(encoded)
        handle.write(";\n")

    with open(os.path.join(BASE_DIR, "index.html"), encoding="utf-8") as handle:
        standalone = handle.read()
    with open(os.path.join(BASE_DIR, "listings.css"), encoding="utf-8") as handle:
        stylesheet = handle.read()
    with open(os.path.join(BASE_DIR, "listings.js"), encoding="utf-8") as handle:
        application = handle.read()

    safe_encoded = encoded.replace("</", "<\\/")
    standalone = standalone.replace(
        '<link rel="stylesheet" href="listings.css">',
        f"<style>\n{stylesheet}\n</style>",
    )
    standalone = standalone.replace(
        '<script src="data.js"></script>',
        f"<script>window.STOCK_LISTINGS_DATA={safe_encoded};</script>",
    )
    standalone = standalone.replace(
        '<script src="listings.js" defer></script>',
        f"<script>\n{application}\n</script>",
    )
    with open(STANDALONE_FILE, "w", encoding="utf-8") as handle:
        handle.write(standalone)

    print(
        f"Generated {len(items):,} eligible listings from {len(all_items):,}, "
        f"excluded {len(excluded_no_turnover):,} without monthly turnover and "
        f"{len(excluded_cboe_leveraged):,} Cboe leveraged ETFs, "
        f"{eligible_registered_count} registered ({eligible_active_count} active) "
        f"-> {STANDALONE_FILE}"
    )


if __name__ == "__main__":
    main()
