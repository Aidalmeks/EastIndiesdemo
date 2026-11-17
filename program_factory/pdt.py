import json
import os
from datetime import datetime

import pymysql
import pytz
from program_factory.trade_event_log import TRADE_EVENT_LOG_PATH, log_trade_event
from runtime_settings import database_config


EASTERN_TZ = pytz.timezone("US/Eastern")
BEIJING_TZ = pytz.timezone("Asia/Shanghai")
DAY_TRADE_LIMIT_ENABLED = False

DB_CONFIG = database_config("memory", cursorclass=pymysql.cursors.DictCursor)
DATA_DB_CONFIG = database_config("data", cursorclass=pymysql.cursors.DictCursor)

SINGLE_REENTRY_STRATEGIES = {"LOG_RESIDUAL", "SINGLE_LOG_RESIDUAL", "NEW_HIGH"}
METAPAIR_SYMBOL_CACHE = {
    "loaded_at": None,
    "symbols": {},
}
TAKE_PROFIT_REENTRY_COOLDOWN_SECONDS = 10 * 60


def get_db_connection():
    return pymysql.connect(**DB_CONFIG)


def get_data_db_connection():
    return pymysql.connect(**DATA_DB_CONFIG)


def normalize_symbol(value):
    return str(value or "").strip().upper()


def normalize_pair_key(value):
    return str(value or "").strip().lower()


def get_position_qty(positions, symbol):
    symbol_key = normalize_symbol(symbol)
    if not symbol_key:
        return 0

    for key in (symbol_key, symbol_key.lower()):
        if key in positions:
            try:
                return float((positions.get(key) or {}).get("qty", 0) or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def load_metapair_symbol_map(force=False):
    now = datetime.now()
    loaded_at = METAPAIR_SYMBOL_CACHE.get("loaded_at")
    if (
        not force
        and loaded_at is not None
        and (now - loaded_at).total_seconds() < 30
    ):
        return METAPAIR_SYMBOL_CACHE.get("symbols", {})

    symbols = {}
    conn = get_data_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT pair_name, ticker_a, ticker_b
                FROM metapairs
                WHERE is_active = 1
                """
            )
            for row in cursor.fetchall():
                pair_key = normalize_pair_key(row.get("pair_name"))
                y = normalize_symbol(row.get("ticker_a"))
                x = normalize_symbol(row.get("ticker_b"))
                if pair_key and y and x:
                    symbols[pair_key] = {"y": y, "x": x}
    finally:
        conn.close()

    METAPAIR_SYMBOL_CACHE["loaded_at"] = now
    METAPAIR_SYMBOL_CACHE["symbols"] = symbols
    return symbols


def _row_exists(cursor, sql, params):
    cursor.execute(sql, params)
    return cursor.fetchone() is not None


def _latest_holding_created_at(cursor, pair_name, is_single):
    if is_single:
        cursor.execute(
            """
            SELECT created_at
            FROM single_scheduled_orders
            WHERE LOWER(single_name) = LOWER(%s)
              AND status = 'HOLDING'
            ORDER BY id DESC
            LIMIT 1
            """,
            (pair_name,),
        )
    else:
        cursor.execute(
            """
            SELECT created_at
            FROM scheduled_orders
            WHERE pair_name = %s
              AND status = 'HOLDING'
            ORDER BY id DESC
            LIMIT 1
            """,
            (pair_name,),
        )
    row = cursor.fetchone()
    return None if not row else row.get("created_at")


def _latest_order_status(cursor, pair_name, is_single):
    """Return the newest order state checked by the reentry-lock trigger."""
    if is_single:
        cursor.execute(
            """
            SELECT status
            FROM single_scheduled_orders
            WHERE LOWER(single_name) = LOWER(%s)
            ORDER BY id DESC
            LIMIT 1
            """,
            (pair_name,),
        )
    else:
        cursor.execute(
            """
            SELECT status
            FROM scheduled_orders
            WHERE pair_name = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (pair_name,),
        )
    row = cursor.fetchone()
    return None if not row else str(row.get("status") or "").upper()


def _holding_is_past_grace(created_at, minimum_age_seconds):
    if created_at is None:
        return False
    if minimum_age_seconds <= 0:
        return True
    try:
        return (datetime.now() - created_at).total_seconds() >= minimum_age_seconds
    except TypeError:
        return False


def _has_position_history_or_old_holding(
    cursor,
    pair_name,
    is_single,
    target,
    minimum_holding_age_seconds,
):
    if is_single:
        if _row_exists(
            cursor,
            """
            SELECT 1
            FROM single_positions
            WHERE single_name = %s
            LIMIT 1
            """,
            (target,),
        ):
            return True
    else:
        y, x = target
        if _row_exists(
            cursor,
            """
            SELECT 1
            FROM pair_positions
            WHERE ticker_y = %s AND ticker_x = %s
            LIMIT 1
            """,
            (y, x),
        ):
            return True

    created_at = _latest_holding_created_at(cursor, pair_name, is_single)
    return _holding_is_past_grace(created_at, minimum_holding_age_seconds)


def _parse_event_ts(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _event_matches_reentry_name(event, pair_name, is_single):
    if str(event.get("event") or "").upper() != "CLOSE":
        return False

    if is_single:
        return (
            str(event.get("kind") or "").lower() == "single"
            and normalize_symbol(event.get("single_name")) == normalize_symbol(pair_name)
        )

    return (
        str(event.get("kind") or "").lower() == "pair"
        and normalize_pair_key(event.get("pair_name")) == normalize_pair_key(pair_name)
    )


def _latest_close_event(pair_name, is_single):
    if not os.path.exists(TRADE_EVENT_LOG_PATH):
        return None

    try:
        with open(TRADE_EVENT_LOG_PATH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None

    for line in reversed(lines):
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if _event_matches_reentry_name(event, pair_name, is_single):
            return event

    return None


def _safe_return_pct(event):
    try:
        value = event.get("return_pct")
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _take_profit_cooldown_remaining_seconds(event, cooldown_seconds):
    exit_reason = str((event or {}).get("exit_reason") or "").upper()
    if not exit_reason.startswith("TAKE_PROFIT"):
        return 0

    event_ts = _parse_event_ts((event or {}).get("ts"))
    if event_ts is None:
        return 0

    elapsed = (datetime.now() - event_ts).total_seconds()
    remaining = float(cooldown_seconds) - elapsed
    return max(0, int(remaining))


def take_profit_cooldown_remaining_seconds(
    pair_name,
    is_single=False,
    cooldown_seconds=TAKE_PROFIT_REENTRY_COOLDOWN_SECONDS,
):
    latest_close = _latest_close_event(pair_name, is_single)
    return _take_profit_cooldown_remaining_seconds(latest_close, cooldown_seconds)


def _is_negative_trailing_stop_close(event):
    exit_reason = str((event or {}).get("exit_reason") or "").upper()
    return exit_reason == "TRAILING_STOP" and (_safe_return_pct(event) or 0) < 0


def reset_auto_reentry_locks_for_missing_positions(
    positions,
    pair_symbol_map=None,
    minimum_holding_age_seconds=60,
    take_profit_cooldown_seconds=TAKE_PROFIT_REENTRY_COOLDOWN_SECONDS,
):
    """
    Reset Auto=Yes reentry locks after the corresponding IB position is gone.

    Pair rows are unlocked when both configured legs have zero qty. Single rows
    are unlocked when the single ticker has zero qty. A take-profit close keeps
    lock=1 for a cooldown window; a losing trailing stop moves lock=2.
    """
    pair_symbol_map = pair_symbol_map or load_metapair_symbol_map()
    unlocked = []

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    pair_name,
                    strategy,
                    COALESCE(reentry_lock, 0) AS reentry_lock,
                    COALESCE(auto_schedule_enabled, 0) AS auto_schedule_enabled
                FROM pair_reentry
                WHERE COALESCE(auto_schedule_enabled, 0) = 1
                  AND COALESCE(reentry_lock, 0) = 1
                """
            )
            rows = cursor.fetchall()

            for row in rows:
                pair_name = str(row.get("pair_name") or "").strip()
                pair_key = normalize_pair_key(pair_name)
                strategy = str(row.get("strategy") or "").strip().upper()
                is_single = "_" not in pair_key or strategy in SINGLE_REENTRY_STRATEGIES

                if is_single:
                    symbol = normalize_symbol(pair_name)
                    if not symbol or get_position_qty(positions, symbol) != 0:
                        continue
                    target = symbol
                    evidence_target = symbol
                else:
                    pair_symbols = pair_symbol_map.get(pair_key)
                    if not pair_symbols:
                        continue
                    y = pair_symbols["y"]
                    x = pair_symbols["x"]
                    if get_position_qty(positions, y) != 0 or get_position_qty(positions, x) != 0:
                        continue
                    target = f"{y}/{x}"
                    evidence_target = (y, x)

                if not _has_position_history_or_old_holding(
                    cursor,
                    pair_name=pair_name,
                    is_single=is_single,
                    target=evidence_target,
                    minimum_holding_age_seconds=minimum_holding_age_seconds,
                ):
                    continue

                # IB can report a manual close before Manual_Close commits
                # HOLDING -> CLOSED and then records the intended lock=2.
                # Leave that transition alone rather than treating the brief
                # interval as an eligible reentry unlock.  This mirrors the
                # database trigger and avoids stopping the monitor on its
                # expected rejection.
                if _latest_order_status(cursor, pair_name, is_single) == "HOLDING":
                    continue

                latest_close = _latest_close_event(pair_name, is_single)
                if _is_negative_trailing_stop_close(latest_close):
                    cursor.execute(
                        """
                        UPDATE pair_reentry
                        SET reentry_lock = 2
                        WHERE pair_name = %s
                          AND COALESCE(auto_schedule_enabled, 0) = 1
                          AND COALESCE(reentry_lock, 0) = 1
                        """,
                        (pair_name,),
                    )
                    continue

                cooldown_remaining = _take_profit_cooldown_remaining_seconds(
                    latest_close,
                    take_profit_cooldown_seconds,
                )
                if cooldown_remaining > 0:
                    continue

                cursor.execute(
                    """
                    UPDATE pair_reentry
                    SET reentry_lock = 0
                    WHERE pair_name = %s
                      AND COALESCE(auto_schedule_enabled, 0) = 1
                      AND COALESCE(reentry_lock, 0) = 1
                    """,
                    (pair_name,),
                )
                if cursor.rowcount > 0:
                    unlocked.append({"pair_name": pair_name, "target": target})

        conn.commit()
    finally:
        conn.close()

    return unlocked


def to_eastern_naive(dt, source_tz):
    if dt is None:
        return None

    if dt.tzinfo is None:
        dt = source_tz.localize(dt)
    else:
        dt = dt.astimezone(source_tz)

    return dt.astimezone(EASTERN_TZ).replace(tzinfo=None)


def get_entry_time(pair_name):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT entry_time
                FROM pair_positions
                WHERE pair_name = %s
                """,
                (pair_name,),
            )
            row = cursor.fetchone()

            if not row:
                return None

            entry = row["entry_time"]
            if entry is not None and entry.tzinfo is None:
                entry = EASTERN_TZ.localize(entry)
            return entry
    finally:
        conn.close()


def get_single_entry_time(single_name):
    symbol = str(single_name or "").strip().upper()
    if not symbol:
        return None

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT entry_time
                FROM single_positions
                WHERE single_name = %s
                """,
                (symbol,),
            )
            row = cursor.fetchone()

            if not row:
                return None

            entry = row["entry_time"]
            if entry is not None and entry.tzinfo is None:
                entry = EASTERN_TZ.localize(entry)
            return entry
    finally:
        conn.close()


def get_latest_created_time_from_scheduled_orders(pair_key):
    if not pair_key:
        return None

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT created_at
                FROM scheduled_orders
                WHERE pair_name = %s
                  AND status = 'HOLDING'
                ORDER BY id DESC
                LIMIT 1
                """,
                (pair_key,),
            )
            row = cursor.fetchone()

            if not row:
                return None

            return to_eastern_naive(row["created_at"], BEIJING_TZ)
    finally:
        conn.close()


def get_latest_created_time_from_single_scheduled_orders(single_name):
    symbol = str(single_name or "").strip()
    if not symbol:
        return None

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT created_at
                FROM single_scheduled_orders
                WHERE LOWER(single_name) = LOWER(%s)
                  AND status = 'HOLDING'
                ORDER BY id DESC
                LIMIT 1
                """,
                (symbol,),
            )
            row = cursor.fetchone()

            if not row:
                return None

            return to_eastern_naive(row["created_at"], BEIJING_TZ)
    finally:
        conn.close()


def load_latest_scheduled_order_beta_map():
    conn = get_db_connection()
    beta_map = {}

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT pair_name, beta
                FROM scheduled_orders
                ORDER BY id DESC
                """
            )
            for row in cursor.fetchall():
                pair_key = str(row.get("pair_name") or "").strip().lower()
                if not pair_key or pair_key in beta_map:
                    continue
                beta_value = row.get("beta")
                try:
                    beta_map[pair_key] = float(beta_value)
                except (TypeError, ValueError):
                    continue
    finally:
        conn.close()

    return beta_map


def pair_key_for_symbols(y, x, pair_symbol_map=None):
    y_key = normalize_symbol(y)
    x_key = normalize_symbol(x)
    pair_symbol_map = pair_symbol_map or load_metapair_symbol_map()
    for pair_key, symbols in pair_symbol_map.items():
        if normalize_symbol(symbols.get("y")) == y_key and normalize_symbol(symbols.get("x")) == x_key:
            return pair_key
    return f"{y_key.lower()}_{x_key.lower()}" if y_key and x_key else ""


def has_holding_pair_order(cursor, pair_key):
    if not pair_key:
        return False
    cursor.execute(
        """
        SELECT 1
        FROM scheduled_orders
        WHERE pair_name = %s
          AND status = 'HOLDING'
        ORDER BY id DESC
        LIMIT 1
        """,
        (pair_key,),
    )
    return cursor.fetchone() is not None


def has_holding_single_order(cursor, symbol):
    symbol_key = normalize_symbol(symbol)
    if not symbol_key:
        return False
    cursor.execute(
        """
        SELECT 1
        FROM single_scheduled_orders
        WHERE LOWER(single_name) = LOWER(%s)
          AND status = 'HOLDING'
        ORDER BY id DESC
        LIMIT 1
        """,
        (symbol_key,),
    )
    return cursor.fetchone() is not None


def sync_pair_entry_time(cursor, pair_name, pair_key):
    cursor.execute(
        """
        SELECT entry_time
        FROM pair_positions
        WHERE pair_name = %s
        """,
        (pair_name,),
    )
    row = cursor.fetchone()

    if not row:
        return False, False

    desired_entry_time = get_latest_created_time_from_scheduled_orders(pair_key)
    if desired_entry_time is None:
        return True, False

    current_entry_time = row["entry_time"]
    if current_entry_time != desired_entry_time:
        cursor.execute(
            """
            UPDATE pair_positions
            SET entry_time = %s
            WHERE pair_name = %s
            """,
            (desired_entry_time, pair_name),
        )
        print(f"ENTRY TIME SYNCED: {pair_name} -> {desired_entry_time}")
        return True, True

    return True, False


def get_pair_state(pair_name):
    entry = get_entry_time(pair_name)
    if entry is None:
        return "NONE"
    if not DAY_TRADE_LIMIT_ENABLED:
        return "HOLD"

    now = datetime.now(EASTERN_TZ)
    if entry.date() == now.date():
        return "NEW"
    return "HOLD"


def get_single_state(single_name):
    entry = get_single_entry_time(single_name)
    if entry is None:
        return "NONE"
    if not DAY_TRADE_LIMIT_ENABLED:
        return "HOLD"

    now = datetime.now(EASTERN_TZ)
    if entry.date() == now.date():
        return "NEW"
    return "HOLD"


def sync_pair_positions_with_db(positions):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT pair_name, ticker_y, ticker_x FROM pair_positions")
            rows = cursor.fetchall()
            pair_symbol_map = load_metapair_symbol_map()

            for row in rows:
                name = row["pair_name"]
                y = row["ticker_y"]
                x = row["ticker_x"]

                pos_y = positions.get(y, {}).get("qty", 0)
                pos_x = positions.get(x, {}).get("qty", 0)

                if pos_y == 0 and pos_x == 0:
                    print(f"CLEAN: {name} removed (fully closed)")
                    pair_key = pair_key_for_symbols(y, x, pair_symbol_map=pair_symbol_map)
                    if has_holding_pair_order(cursor, pair_key):
                        log_trade_event({
                            "event": "CLOSE",
                            "kind": "pair",
                            "pair_name": pair_key,
                            "exit_reason": "MANUAL_CLOSE_DETECTED",
                            "return_pct": None,
                            "trigger_line": None,
                        })
                    cursor.execute(
                        """
                        DELETE FROM pair_positions
                        WHERE pair_name = %s
                        """,
                        (name,),
                    )
                else:
                    y_info = positions.get(y, {})
                    x_info = positions.get(x, {})
                    cursor.execute(
                        """
                        UPDATE pair_positions
                        SET qty_y = %s,
                            avg_y = %s,
                            qty_x = %s,
                            avg_x = %s
                        WHERE pair_name = %s
                        """,
                        (
                            y_info.get("qty", 0),
                            y_info.get("avg_price"),
                            x_info.get("qty", 0),
                            x_info.get("avg_price"),
                            name,
                        ),
                    )

        conn.commit()
    finally:
        conn.close()


def load_pair_position_symbols(cursor):
    cursor.execute(
        """
        SELECT ticker_y, ticker_x
        FROM pair_positions
        """
    )
    symbols = set()
    for row in cursor.fetchall():
        for field in ("ticker_y", "ticker_x"):
            symbol = str(row.get(field) or "").strip().upper()
            if symbol:
                symbols.add(symbol)
    return symbols


def sync_single_positions_with_db(positions):
    normalized_positions = {
        str(symbol or "").strip().upper(): info
        for symbol, info in positions.items()
    }
    now = datetime.now(EASTERN_TZ).replace(tzinfo=None)
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            pair_symbols = load_pair_position_symbols(cursor)
            cursor.execute("SELECT single_name, entry_time, avg, qty FROM single_positions")
            rows = cursor.fetchall()

            for row in rows:
                symbol = str(row["single_name"] or "").strip().upper()
                qty = normalized_positions.get(symbol, {}).get("qty", 0)

                if symbol in pair_symbols:
                    print(f"CLEAN SINGLE POSITION: {symbol} removed (belongs to pair)")
                    cursor.execute(
                        """
                        DELETE FROM single_positions
                        WHERE single_name = %s
                        """,
                        (symbol,),
                    )
                elif qty == 0:
                    print(f"CLEAN SINGLE POSITION: {symbol} removed (fully closed)")
                    if has_holding_single_order(cursor, symbol):
                        log_trade_event({
                            "event": "CLOSE",
                            "kind": "single",
                            "single_name": symbol,
                            "exit_reason": "MANUAL_CLOSE_DETECTED",
                            "return_pct": None,
                            "trigger_line": None,
                        })
                    cursor.execute(
                        """
                        DELETE FROM single_positions
                        WHERE single_name = %s
                        """,
                        (symbol,),
                    )
                else:
                    info = normalized_positions.get(symbol, {})
                    current_qty = info.get("qty", 0)
                    current_avg = info.get("avg_price")
                    previous_qty = row.get("qty")
                    previous_avg = row.get("avg")
                    entry_time = row.get("entry_time")
                    entry_date = entry_time.date() if entry_time is not None else None
                    try:
                        qty_changed = abs(float(previous_qty) - float(current_qty)) > 1e-9
                    except (TypeError, ValueError):
                        qty_changed = previous_qty != current_qty
                    try:
                        avg_changed = abs(float(previous_avg) - float(current_avg)) > 1e-6
                    except (TypeError, ValueError):
                        avg_changed = previous_avg != current_avg
                    should_mark_new = (
                        DAY_TRADE_LIMIT_ENABLED
                        and entry_date != now.date()
                        and (qty_changed or avg_changed)
                    )

                    cursor.execute(
                        """
                        UPDATE single_positions
                        SET qty = %s,
                            avg = %s,
                            entry_time = CASE WHEN %s THEN %s ELSE entry_time END
                        WHERE single_name = %s
                        """,
                        (
                            current_qty,
                            current_avg,
                            should_mark_new,
                            now,
                            symbol,
                        ),
                    )
                    if should_mark_new:
                        print(f"SINGLE ENTRY TIME MARKED NEW: {symbol} -> {now}")

        conn.commit()
    finally:
        conn.close()


def detect_new_positions_and_record(positions, pairs):
    conn = get_db_connection()
    now = datetime.now(EASTERN_TZ).replace(tzinfo=None)
    beta_map = load_latest_scheduled_order_beta_map()

    try:
        with conn.cursor() as cursor:
            for name, info in pairs.items():
                y = info["y"]
                x = info["x"]
                pair_key = info.get("pair_key")
                beta_value = beta_map.get(str(pair_key).strip().lower(), None)
                single_leg_mode = beta_value is not None and abs(beta_value) < 1e-12

                pos_y = positions.get(y, {}).get("qty", 0)
                pos_x = positions.get(x, {}).get("qty", 0)

                if pos_y != 0 and (pos_x != 0 or single_leg_mode):
                    desired_entry_time = get_latest_created_time_from_scheduled_orders(pair_key) or now
                    exists, _ = sync_pair_entry_time(
                        cursor,
                        pair_name=name,
                        pair_key=pair_key,
                    )

                    if not exists:
                        print(f"NEW POSITION DETECTED: {name}")
                        cursor.execute(
                            """
                            INSERT INTO pair_positions
                            (pair_name, ticker_y, ticker_x, avg_y, qty_y, avg_x, qty_x, entry_time)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                name,
                                y,
                                x,
                                positions.get(y, {}).get("avg_price"),
                                positions.get(y, {}).get("qty", 0),
                                positions.get(x, {}).get("avg_price"),
                                positions.get(x, {}).get("qty", 0),
                                desired_entry_time,
                            ),
                        )
                        log_trade_event({
                            "event": "OPEN",
                            "kind": "pair",
                            "pair_name": pair_key,
                            "display_name": name,
                            "ticker_y": y,
                            "ticker_x": x,
                            "qty_y": positions.get(y, {}).get("qty", 0),
                            "avg_y": positions.get(y, {}).get("avg_price"),
                            "qty_x": positions.get(x, {}).get("qty", 0),
                            "avg_x": positions.get(x, {}).get("avg_price"),
                            "entry_time": desired_entry_time.isoformat() if desired_entry_time else None,
                        })

        conn.commit()
    finally:
        conn.close()


def detect_new_single_positions_and_record(positions, single_symbols):
    normalized_positions = {
        str(symbol or "").strip().upper(): info
        for symbol, info in positions.items()
    }
    conn = get_db_connection()
    now = datetime.now(EASTERN_TZ).replace(tzinfo=None)

    try:
        with conn.cursor() as cursor:
            pair_symbols = load_pair_position_symbols(cursor)
            for symbol in single_symbols:
                symbol_key = str(symbol or "").strip().upper()
                if not symbol_key:
                    continue
                if symbol_key in pair_symbols:
                    continue

                qty = normalized_positions.get(symbol_key, {}).get("qty", 0)
                if qty == 0:
                    continue

                holding_entry_time = get_latest_created_time_from_single_scheduled_orders(symbol_key)
                cursor.execute(
                    """
                    SELECT entry_time
                    FROM single_positions
                    WHERE single_name = %s
                    """,
                    (symbol_key,),
                )
                row = cursor.fetchone()

                if not row:
                    desired_entry_time = holding_entry_time or now
                    print(f"NEW SINGLE POSITION DETECTED: {symbol_key}")
                    cursor.execute(
                        """
                        INSERT INTO single_positions
                        (single_name, entry_time, avg, qty)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (
                            symbol_key,
                            desired_entry_time,
                            normalized_positions.get(symbol_key, {}).get("avg_price"),
                            normalized_positions.get(symbol_key, {}).get("qty", 0),
                        ),
                    )
                    log_trade_event({
                        "event": "OPEN",
                        "kind": "single",
                        "single_name": symbol_key,
                        "qty": normalized_positions.get(symbol_key, {}).get("qty", 0),
                        "avg": normalized_positions.get(symbol_key, {}).get("avg_price"),
                        "entry_time": desired_entry_time.isoformat() if desired_entry_time else None,
                    })
                elif holding_entry_time is not None and row["entry_time"] != holding_entry_time:
                    cursor.execute(
                        """
                        UPDATE single_positions
                        SET entry_time = %s,
                            avg = %s,
                            qty = %s
                        WHERE single_name = %s
                        """,
                        (
                            holding_entry_time,
                            normalized_positions.get(symbol_key, {}).get("avg_price"),
                            normalized_positions.get(symbol_key, {}).get("qty", 0),
                            symbol_key,
                        ),
                    )
                    print(f"SINGLE ENTRY TIME SYNCED: {symbol_key} -> {holding_entry_time}")

        conn.commit()
    finally:
        conn.close()
