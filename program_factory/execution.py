from ib_insync import *
import json
import math
import os
import pymysql
from decimal import Decimal, ROUND_HALF_UP
import re
from datetime import datetime
from program_factory.market_schedule import NY_TZ
from program_factory.port_listener import choose_connection
from program_factory.trade_event_log import log_trade_event
from runtime_settings import database_config, ib_host, ib_port_account_map


MEMORY_DB_CONFIG = database_config("memory")



TRADING_ENABLED = True
MAX_TOTAL_GROSS_EXPOSURE_PCT = 160.0
GROSS_EXPOSURE_PRICE_TIMEOUT_SECONDS = 3.0
# Exit-rule formats:
# Stop loss:
# 1) fixed number: -4.0
# 2) sigma cutoff: "-1.0*sigma"
# 3) trailing stop: "trailing(-0.5*sigma)"
# Take profit:
# 1) fixed return: 4.0
# 2) sigma return: "1.0*sigma"
# 3) trailing profit: "trailing(-0.5*sigma)" (arms only after max_return > 0)
# 4) z target: "z(0)", "z(1)", "z(1.5)"
# 5) NY time profit: "NY(15:55)" exits after HH:MM New York time only when return_pct > 0
# Note: in board.py, sigma == 1/2 * two_sigma from DB.
EXIT_RULES_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "pair_exit_rules.json")
DEFAULT_STOP_LOSS_RULE = "trailing(-0.5*sigma)"
DEFAULT_TAKE_PROFIT_RULE = None
DEFAULT_SINGLE_STOP_LOSS_RULE = None
DEFAULT_SINGLE_TAKE_PROFIT_RULE = None
_EXIT_RULES_CACHE = {
    "mtime": None,
    "config": {"stop_loss": {}, "take_profit": {}, "single_stop_loss": {}, "single_take_profit": {}},
}

SIGMA_EXPR_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)?\*?sigma$")
Z_TARGET_PATTERN = re.compile(r"^z\(([^)]+)\)$", re.IGNORECASE)
TIME_TARGET_PATTERN = re.compile(r"^NY\(([^)]+)\)$", re.IGNORECASE)


def _normalize_rule_map(raw_rules):
    normalized = {}

    if not isinstance(raw_rules, dict):
        return normalized

    for raw_pair_name, raw_rule in raw_rules.items():
        pair_name = str(raw_pair_name).strip().lower()
        if not pair_name:
            continue

        if isinstance(raw_rule, bool):
            normalized[pair_name] = int(raw_rule)
            continue

        if isinstance(raw_rule, (int, float)):
            number = float(raw_rule)
            normalized[pair_name] = int(number) if number.is_integer() else number
            continue

        if raw_rule is None:
            continue

        rule_text = str(raw_rule).strip()
        if not rule_text:
            continue

        normalized[pair_name] = rule_text

    return normalized


def _default_exit_rules_config():
    return {"stop_loss": {}, "take_profit": {}, "single_stop_loss": {}, "single_take_profit": {}}


def get_pair_exit_rules_config():
    default_config = _default_exit_rules_config()

    try:
        current_mtime = os.path.getmtime(EXIT_RULES_CONFIG_PATH)
    except OSError:
        config = default_config
        _EXIT_RULES_CACHE["mtime"] = None
        _EXIT_RULES_CACHE["config"] = config
        return config

    if _EXIT_RULES_CACHE["mtime"] == current_mtime:
        return _EXIT_RULES_CACHE["config"]

    config = default_config
    try:
        with open(EXIT_RULES_CONFIG_PATH, "r", encoding="utf-8") as fh:
            payload = json.load(fh)

        if not isinstance(payload, dict):
            raise ValueError("exit-rules config must be a JSON object")

        config = {
            "stop_loss": _normalize_rule_map(payload.get("stop_loss", {})),
            "take_profit": _normalize_rule_map(payload.get("take_profit", {})),
            "single_stop_loss": _normalize_rule_map(payload.get("single_stop_loss", {})),
            "single_take_profit": _normalize_rule_map(payload.get("single_take_profit", {})),
        }
    except Exception as exc:
        print(f"[execution] Failed to load exit-rules config from {EXIT_RULES_CONFIG_PATH}: {exc}")
        config = default_config

    _EXIT_RULES_CACHE["mtime"] = current_mtime
    _EXIT_RULES_CACHE["config"] = config
    return config


def get_pair_stop_loss_rules():
    return dict(get_pair_exit_rules_config().get("stop_loss", {}))


def get_pair_take_profit_rules():
    return dict(get_pair_exit_rules_config().get("take_profit", {}))


def get_single_stop_loss_rules():
    return dict(get_pair_exit_rules_config().get("single_stop_loss", {}))


def get_single_take_profit_rules():
    return dict(get_pair_exit_rules_config().get("single_take_profit", {}))


def _get_pair_rule(rule_map, pair_name):
    normalized_pair_name = str(pair_name).strip().lower()
    if pair_name in rule_map:
        return rule_map[pair_name]
    return rule_map.get(normalized_pair_name)

def _eval_stop_expr(expr, sigma_value):
    if isinstance(expr, (int, float)):
        return float(expr)

    text = str(expr).strip()

    try:
        return float(text)
    except (TypeError, ValueError):
        pass

    compact = text.lower().replace(" ", "")
    if compact in ("sigma", "+sigma"):
        return None if sigma_value is None else float(sigma_value)
    if compact == "-sigma":
        return None if sigma_value is None else -float(sigma_value)

    if not SIGMA_EXPR_PATTERN.match(compact):
        return None

    if sigma_value is None:
        return None

    coeff = compact.replace("*sigma", "").replace("sigma", "")
    if coeff in ("", "+"):
        n = 1.0
    elif coeff == "-":
        n = -1.0
    else:
        n = float(coeff)

    return n * float(sigma_value)


def _parse_hhmm(value):
    text = str(value or "").strip()
    match = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _now_ny():
    return datetime.now(NY_TZ)


def _resolve_time_take_profit(rule_text, ret, now_ny=None):
    match = TIME_TARGET_PATTERN.match(str(rule_text or "").strip().replace(" ", ""))
    if not match:
        return None

    parsed = _parse_hhmm(match.group(1))
    if parsed is None:
        return None

    hour, minute = parsed
    current_ny = now_ny or _now_ny()
    target_ny = current_ny.replace(hour=hour, minute=minute, second=0, microsecond=0)
    is_profitable = ret is not None and float(ret) > 0
    is_time_reached = current_ny >= target_ny

    return {
        "mode": "time",
        "threshold": f"{hour:02d}:{minute:02d}",
        "metric": "time_ny",
        "armed": bool(is_profitable and is_time_reached),
        "comparator": ">=",
        "exit_reason": "TAKE_PROFIT_TIME",
        "rule_text": str(rule_text).strip(),
        "now_ny": current_ny.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "requires_positive_return": True,
    }

def _default_trailing_threshold(max_ret, sigma_value):
    if sigma_value is not None:
        return max_ret - (0.5 * float(sigma_value))
    return max_ret - 1.0


def resolve_pair_stop_loss(pair_name, ret, max_ret, sigma_value=None):
    raw_rule = _get_pair_rule(get_pair_stop_loss_rules(), pair_name)
    if raw_rule is None:
        raw_rule = DEFAULT_STOP_LOSS_RULE

    fallback_rule = "default: trailing(-0.5*sigma)" if sigma_value is not None else "default: trailing(-1.0)"

    if isinstance(raw_rule, (int, float)):
        return {
            "mode": "fixed",
            "threshold": float(raw_rule),
            "exit_reason": "STOP_LOSS",
            "rule_text": str(raw_rule),
        }

    rule_text = str(raw_rule).strip()
    lower = rule_text.lower()

    if lower.startswith("trailing(") and lower.endswith(")"):
        inner = rule_text[rule_text.find("(") + 1 : -1]
        offset = _eval_stop_expr(inner, sigma_value)
        if offset is None:
            return {
                "mode": "trailing",
                "threshold": _default_trailing_threshold(max_ret, sigma_value),
                "exit_reason": "TRAILING_STOP",
                "rule_text": f"{rule_text} ({fallback_rule})",
            }

        return {
            "mode": "trailing",
            "threshold": max_ret + offset,
            "exit_reason": "TRAILING_STOP",
            "rule_text": rule_text,
        }

    threshold = _eval_stop_expr(rule_text, sigma_value)
    if threshold is None:
        return {
            "mode": "trailing",
            "threshold": _default_trailing_threshold(max_ret, sigma_value),
            "exit_reason": "TRAILING_STOP",
            "rule_text": f"{rule_text} ({fallback_rule})",
        }

    return {
        "mode": "fixed",
        "threshold": threshold,
        "exit_reason": "STOP_LOSS",
        "rule_text": rule_text,
    }


def resolve_pair_take_profit(pair_name, ret, max_ret, sigma_value=None, z_now=None, direction=None, now_ny=None):
    raw_rule = _get_pair_rule(get_pair_take_profit_rules(), pair_name)
    if raw_rule is None and DEFAULT_TAKE_PROFIT_RULE is None:
        return None
    if raw_rule is None:
        raw_rule = DEFAULT_TAKE_PROFIT_RULE

    if isinstance(raw_rule, (int, float)):
        return {
            "mode": "fixed",
            "threshold": float(raw_rule),
            "metric": "return_pct",
            "armed": True,
            "comparator": ">=",
            "exit_reason": "TAKE_PROFIT",
            "rule_text": str(raw_rule),
        }

    rule_text = str(raw_rule).strip()
    lower = rule_text.lower()

    time_cfg = _resolve_time_take_profit(rule_text, ret, now_ny=now_ny)
    if time_cfg is not None:
        return time_cfg

    if lower.startswith("trailing(") and lower.endswith(")"):
        inner = rule_text[rule_text.find("(") + 1 : -1]
        offset = _eval_stop_expr(inner, sigma_value)
        if offset is None:
            offset = -0.5 * float(sigma_value) if sigma_value is not None else -1.0
            rule_text = f"{rule_text} (fallback: trailing(-0.5*sigma))" if sigma_value is not None else f"{rule_text} (fallback: trailing(-1.0))"

        armed = max_ret is not None and max_ret > 0
        threshold = (max_ret + offset) if armed else None
        return {
            "mode": "trailing",
            "threshold": threshold,
            "metric": "return_pct",
            "armed": armed,
            "comparator": "<=",
            "exit_reason": "TAKE_PROFIT",
            "rule_text": rule_text,
        }

    z_match = Z_TARGET_PATTERN.match(lower.replace(" ", ""))
    if z_match:
        try:
            z_target = abs(float(z_match.group(1)))
        except (TypeError, ValueError):
            return None

        threshold = None
        comparator = None
        if direction == 1:
            threshold = z_target
            comparator = ">="
        elif direction == -1:
            threshold = -z_target
            comparator = "<="

        return {
            "mode": "z_target",
            "threshold": threshold,
            "metric": "z_now",
            "armed": threshold is not None and z_now is not None,
            "comparator": comparator,
            "exit_reason": "TAKE_PROFIT",
            "rule_text": rule_text,
        }

    threshold = _eval_stop_expr(rule_text, sigma_value)
    if threshold is None:
        return None

    return {
        "mode": "fixed",
        "threshold": threshold,
        "metric": "return_pct",
        "armed": True,
        "comparator": ">=",
        "exit_reason": "TAKE_PROFIT",
        "rule_text": rule_text,
    }


def _eval_single_return_rule(expr):
    try:
        return float(str(expr).strip())
    except (TypeError, ValueError):
        return None


def resolve_single_stop_loss(single_name, ret, max_ret):
    raw_rule = _get_pair_rule(get_single_stop_loss_rules(), str(single_name or "").strip().upper())
    if raw_rule is None:
        raw_rule = DEFAULT_SINGLE_STOP_LOSS_RULE
    if raw_rule is None:
        return None

    if isinstance(raw_rule, (int, float)):
        return {
            "mode": "fixed",
            "threshold": float(raw_rule),
            "metric": "return_pct",
            "armed": True,
            "comparator": "<=",
            "exit_reason": "STOP_LOSS",
            "rule_text": str(raw_rule),
        }

    rule_text = str(raw_rule).strip()
    lower = rule_text.lower()
    if lower.startswith("trailing(") and lower.endswith(")"):
        inner = rule_text[rule_text.find("(") + 1 : -1]
        offset = _eval_single_return_rule(inner)
        if offset is None:
            offset = -1.0
            rule_text = f"{rule_text} (fallback: trailing(-1.0))"
        return {
            "mode": "trailing",
            "threshold": float(max_ret) + float(offset),
            "metric": "return_pct",
            "armed": True,
            "comparator": "<=",
            "exit_reason": "TRAILING_STOP",
            "rule_text": rule_text,
        }

    threshold = _eval_single_return_rule(rule_text)
    if threshold is None:
        return None
    return {
        "mode": "fixed",
        "threshold": threshold,
        "metric": "return_pct",
        "armed": True,
        "comparator": "<=",
        "exit_reason": "STOP_LOSS",
        "rule_text": rule_text,
    }


def resolve_single_take_profit(single_name, ret, max_ret, now_ny=None):
    raw_rule = _get_pair_rule(get_single_take_profit_rules(), str(single_name or "").strip().upper())
    if raw_rule is None:
        raw_rule = DEFAULT_SINGLE_TAKE_PROFIT_RULE
    if raw_rule is None:
        return None

    if isinstance(raw_rule, (int, float)):
        return {
            "mode": "fixed",
            "threshold": float(raw_rule),
            "metric": "return_pct",
            "armed": True,
            "comparator": ">=",
            "exit_reason": "TAKE_PROFIT",
            "rule_text": str(raw_rule),
        }

    rule_text = str(raw_rule).strip()
    lower = rule_text.lower()

    time_cfg = _resolve_time_take_profit(rule_text, ret, now_ny=now_ny)
    if time_cfg is not None:
        return time_cfg

    if lower.startswith("trailing(") and lower.endswith(")"):
        inner = rule_text[rule_text.find("(") + 1 : -1]
        offset = _eval_single_return_rule(inner)
        if offset is None:
            offset = -1.0
            rule_text = f"{rule_text} (fallback: trailing(-1.0))"
        armed = max_ret is not None and max_ret > 0
        return {
            "mode": "trailing",
            "threshold": (float(max_ret) + float(offset)) if armed else None,
            "metric": "return_pct",
            "armed": armed,
            "comparator": "<=",
            "exit_reason": "TAKE_PROFIT",
            "rule_text": rule_text,
        }

    threshold = _eval_single_return_rule(rule_text)
    if threshold is None:
        return None
    return {
        "mode": "fixed",
        "threshold": threshold,
        "metric": "return_pct",
        "armed": True,
        "comparator": ">=",
        "exit_reason": "TAKE_PROFIT",
        "rule_text": rule_text,
    }

def get_pair_max_return(pair_key):
    conn = pymysql.connect(**{**MEMORY_DB_CONFIG, "cursorclass": pymysql.cursors.DictCursor})

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT max_return
                FROM scheduled_orders
                WHERE pair_name = %s
                AND status = 'HOLDING'
                ORDER BY id DESC
                LIMIT 1
            """, (pair_key,))
            
            row = cursor.fetchone()
            return float(row["max_return"]) if row and row["max_return"] is not None else None
    finally:
        conn.close()

def update_pair_max_return(pair_key, new_value):
    conn = pymysql.connect(**MEMORY_DB_CONFIG)

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE scheduled_orders
                SET max_return = %s
                WHERE pair_name = %s
                AND status = 'HOLDING'
                ORDER BY id DESC
                LIMIT 1
            """, (new_value, pair_key))

            conn.commit()
    finally:
        conn.close()


def get_single_max_return(single_name):
    conn = pymysql.connect(**{**MEMORY_DB_CONFIG, "cursorclass": pymysql.cursors.DictCursor})

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT max_return
                FROM single_scheduled_orders
                WHERE LOWER(single_name) = LOWER(%s)
                AND status = 'HOLDING'
                ORDER BY id DESC
                LIMIT 1
            """, (str(single_name or "").strip().upper(),))

            row = cursor.fetchone()
            return float(row["max_return"]) if row and row["max_return"] is not None else None
    finally:
        conn.close()


def update_single_max_return(single_name, new_value):
    conn = pymysql.connect(**MEMORY_DB_CONFIG)

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE single_scheduled_orders
                SET max_return = %s
                WHERE LOWER(single_name) = LOWER(%s)
                AND status = 'HOLDING'
                ORDER BY id DESC
                LIMIT 1
            """, (new_value, str(single_name or "").strip().upper()))

            conn.commit()
    finally:
        conn.close()

# ===== 全局 IB 单例（避免重复连接）=====
ib = IB()

CONNECTED = False
CLIENT_ID = 7
PREFERRED_PORT_ORDER = (4001, 4002)
PORT_ACCOUNT_MAP = ib_port_account_map()
CONNECT_PORT = None

def resolve_account_by_port(port):
    if port not in PORT_ACCOUNT_MAP:
        raise ValueError(f"Unsupported port: {port}")
    return PORT_ACCOUNT_MAP[port]

def ensure_connected(port=None, clientId=CLIENT_ID):
    global CONNECTED, CONNECT_PORT, account

    if port is None:
        if CONNECT_PORT is None:
            selection = choose_connection(
                port_account_map=PORT_ACCOUNT_MAP,
                preferred_order=PREFERRED_PORT_ORDER
            )
            CONNECT_PORT = selection.port
        port = CONNECT_PORT

    if not CONNECTED:
        ib.connect(ib_host(), port, clientId=clientId)
        CONNECTED = True

    if account is None:
        account = resolve_account_by_port(port)

account = None

def _normalize_result_value(return_pct):
    if return_pct is None:
        return None
    value = Decimal(str(return_pct)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return value

def _mark_scheduled_order_closed(pair_name, return_pct=None, exit_reason=None):
    if not pair_name:
        return False

    conn = pymysql.connect(**MEMORY_DB_CONFIG)

    try:
        with conn.cursor() as cursor:
            sql = """
            UPDATE scheduled_orders so
            JOIN (
                SELECT id
                FROM scheduled_orders
                WHERE pair_name = %s
                  AND status = 'HOLDING'
                ORDER BY id DESC
                LIMIT 1
            ) t ON so.id = t.id
            SET so.status = 'CLOSED',
                so.result = %s,
                so.close_time = NOW()
            """
            result_value = _normalize_result_value(return_pct)
            cursor.execute(sql, (pair_name, result_value))
            conn.commit()
            updated = cursor.rowcount > 0

            if updated:
                print(f"[DB] scheduled_orders CLOSED: {pair_name} | result={result_value}")
            else:
                print(f"[DB] no HOLDING row to close: {pair_name}")

            return updated
    finally:
        conn.close()


def _mark_single_scheduled_order_closed(single_name, return_pct=None, exit_reason=None):
    if not single_name:
        return False

    conn = pymysql.connect(**MEMORY_DB_CONFIG)

    try:
        with conn.cursor() as cursor:
            sql = """
            UPDATE single_scheduled_orders sso
            JOIN (
                SELECT id
                FROM single_scheduled_orders
                WHERE LOWER(single_name) = LOWER(%s)
                  AND status = 'HOLDING'
                ORDER BY id DESC
                LIMIT 1
            ) t ON sso.id = t.id
            SET sso.status = 'CLOSED',
                sso.result = %s,
                sso.close_time = NOW()
            """
            result_value = _normalize_result_value(return_pct)
            cursor.execute(sql, (str(single_name or "").strip().upper(), result_value))
            conn.commit()
            updated = cursor.rowcount > 0

            if updated:
                print(f"[DB] single_scheduled_orders CLOSED: {single_name} | result={result_value}")
            else:
                print(f"[DB] no HOLDING single row to close: {single_name}")

            return updated
    finally:
        conn.close()
# ===== 获取当前仓位 =====
def get_positions(account_filter=None):
    pos = ib.positions()
    d = {}

    for p in pos:
        if account_filter is not None and getattr(p, "account", None) != account_filter:
            continue
        d[p.contract.symbol] = p.position

    return d


def _is_valid_price(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(price) and price > 0


def _position_price_from_market(ib_client, contract, timeout=GROSS_EXPOSURE_PRICE_TIMEOUT_SECONDS):
    ticker = ib_client.reqMktData(contract, "", False, False)
    start = datetime.now().timestamp()
    price = None

    while datetime.now().timestamp() - start <= timeout:
        price = ticker.marketPrice()
        if _is_valid_price(price):
            break
        ib_client.sleep(0.2)

    try:
        ib_client.cancelMktData(contract)
    except Exception:
        pass

    return float(price) if _is_valid_price(price) else None


def calculate_total_gross_exposure(ib_client, account, target_prices=None, positions=None):
    target_prices = {
        str(symbol).upper(): float(price)
        for symbol, price in (target_prices or {}).items()
        if _is_valid_price(price)
    }
    positions = positions if positions is not None else ib_client.positions()
    rows = []
    total_gross = 0.0

    for pos in positions:
        if getattr(pos, "account", None) != account:
            continue

        symbol = str(getattr(pos.contract, "symbol", "") or "").upper()
        qty = float(getattr(pos, "position", 0) or 0)
        if not symbol or math.isclose(qty, 0.0, abs_tol=1e-12):
            continue

        price = target_prices.get(symbol)
        if price is None:
            contract = Stock(symbol, "SMART", "USD")
            try:
                ib_client.qualifyContracts(contract)
                price = _position_price_from_market(ib_client, contract)
            except Exception:
                price = None

        if price is None and _is_valid_price(getattr(pos, "avgCost", None)):
            price = float(pos.avgCost)

        if price is None:
            print(f"[RISK] skip exposure price unavailable: {symbol}")
            continue

        gross = abs(qty) * price
        total_gross += gross
        rows.append({
            "symbol": symbol,
            "qty": qty,
            "price": price,
            "gross": gross,
        })

    return total_gross, rows


def cap_target_gross_to_total_exposure(
    ib_client,
    account,
    requested_gross,
    net_liq_usd,
    target_symbols,
    target_prices=None,
    max_exposure_pct=MAX_TOTAL_GROSS_EXPOSURE_PCT,
    positions=None,
):
    requested_gross = float(requested_gross)
    net_liq_usd = float(net_liq_usd)
    max_gross = net_liq_usd * (float(max_exposure_pct) / 100.0)

    if requested_gross <= 0 or net_liq_usd <= 0:
        return requested_gross, {
            "capped": False,
            "current_total_gross": 0.0,
            "current_total_pct": 0.0,
            "current_target_symbols_gross": 0.0,
            "allowed_target_gross": requested_gross,
            "max_gross": max_gross,
        }

    target_symbol_set = {str(symbol).upper() for symbol in target_symbols if symbol}
    total_gross, rows = calculate_total_gross_exposure(
        ib_client=ib_client,
        account=account,
        target_prices=target_prices,
        positions=positions,
    )
    current_target_symbols_gross = sum(
        row["gross"] for row in rows if row["symbol"] in target_symbol_set
    )
    other_gross = max(0.0, total_gross - current_target_symbols_gross)
    allowed_target_gross = max(0.0, max_gross - other_gross)
    capped_gross = min(requested_gross, allowed_target_gross)

    info = {
        "capped": capped_gross < requested_gross,
        "current_total_gross": total_gross,
        "current_total_pct": (total_gross / net_liq_usd * 100.0) if net_liq_usd > 0 else 0.0,
        "current_target_symbols_gross": current_target_symbols_gross,
        "allowed_target_gross": allowed_target_gross,
        "max_gross": max_gross,
        "max_exposure_pct": float(max_exposure_pct),
    }

    return capped_gross, info


# ===== 平仓函数（核心）=====
def close_pair(
    symbol_Y,
    symbol_X,
    pair_name=None,
    return_pct=None,
    exit_reason=None,
    account=None,
    port=None,
    event_context=None,
):
    ensure_connected(port=port, clientId=CLIENT_ID)
    if port is None:
        port = CONNECT_PORT
    if account is None:
        account = resolve_account_by_port(port)

    positions = get_positions(account_filter=account)

    pos_Y = positions.get(symbol_Y, 0)
    pos_X = positions.get(symbol_X, 0)

    print(f"[CLOSE] {symbol_Y}: {pos_Y}, {symbol_X}: {pos_X}")

    contract_Y = Stock(symbol_Y, 'SMART', 'USD')
    contract_X = Stock(symbol_X, 'SMART', 'USD')

    ib.qualifyContracts(contract_Y, contract_X)

    trades = []

    # ===== 平 Y =====
    if pos_Y != 0:
        action = 'SELL' if pos_Y > 0 else 'BUY'
        qty = abs(pos_Y)

        print(f"Closing {symbol_Y}: {action} {qty}")

        order = MarketOrder(action, qty)
        order.account = account  # 🔥 关键

        trade = ib.placeOrder(contract_Y, order)
        trades.append(trade)

    # ===== 平 X =====
    if pos_X != 0:
        action = 'SELL' if pos_X > 0 else 'BUY'
        qty = abs(pos_X)

        print(f"Closing {symbol_X}: {action} {qty}")

        order = MarketOrder(action, qty)
        order.account = account  # 🔥 关键

        trade = ib.placeOrder(contract_X, order)
        trades.append(trade)

    ib.sleep(2)

    all_ok = True
    for t in trades:
        status = t.orderStatus.status
        print("Status:", status)
        if status in ['Cancelled', 'Inactive', 'ApiCancelled']:
            all_ok = False

    if all_ok and pair_name:
        db_updated = _mark_scheduled_order_closed(
            pair_name=pair_name,
            return_pct=return_pct,
            exit_reason=exit_reason
        )
        log_trade_event({
            "event": "CLOSE",
            "kind": "pair",
            "pair_name": pair_name,
            "exit_reason": exit_reason,
            "return_pct": None if return_pct is None else float(return_pct),
            **(event_context or {}),
        })

    return all_ok


def close_single(symbol, return_pct=None, exit_reason=None, account=None, port=None, event_context=None):
    ensure_connected(port=port, clientId=CLIENT_ID)
    if port is None:
        port = CONNECT_PORT
    if account is None:
        account = resolve_account_by_port(port)

    positions = get_positions(account_filter=account)
    qty = positions.get(symbol, 0)

    print(f"[SINGLE CLOSE] {symbol}: {qty}")

    if qty == 0:
        print(f"[SINGLE CLOSE] no open position for {symbol}")
        return False

    contract = Stock(symbol, 'SMART', 'USD')
    ib.qualifyContracts(contract)

    action = 'SELL' if qty > 0 else 'BUY'
    close_qty = abs(qty)
    print(f"Closing {symbol}: {action} {close_qty}")

    order = MarketOrder(action, close_qty)
    order.account = account
    trade = ib.placeOrder(contract, order)

    ib.sleep(2)

    status = trade.orderStatus.status
    print("Status:", status)
    all_ok = status not in ['Cancelled', 'Inactive', 'ApiCancelled']

    if all_ok:
        db_updated = _mark_single_scheduled_order_closed(
            single_name=symbol,
            return_pct=return_pct,
            exit_reason=exit_reason
        )
        log_trade_event({
            "event": "CLOSE",
            "kind": "single",
            "single_name": symbol,
            "exit_reason": exit_reason,
            "return_pct": None if return_pct is None else float(return_pct),
            **(event_context or {}),
        })

    return all_ok
