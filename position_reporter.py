import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from iphone_push import DEFAULT_GROUP, load_local_env, send_bark


PROJECT_DIR = os.path.abspath(os.path.dirname(__file__))
LOG_DIR = os.path.join(PROJECT_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "position_reporter.log")
TRADE_EVENT_LOG_FILE = os.path.join(LOG_DIR, "flagship_trade_events.log")
REPORT_STATE_FILE = os.path.join(LOG_DIR, "position_reporter_state.json")

DEFAULT_API_URL = "http://127.0.0.1:8000/api/flagship-board"
SERVER_TIMEZONE = "Asia/Shanghai"
DISPLAY_TZ = ZoneInfo(SERVER_TIMEZONE)


REPORT_LABELS = {
    "open": "21:30",
    "midnight": "00:00",
    "close": "04:00",
    "test": "持仓报告测试",
}


def configure_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def now_local():
    return datetime.now(DISPLAY_TZ)


def parse_datetime(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(DISPLAY_TZ).replace(tzinfo=None)
    return dt


def load_report_state():
    try:
        with open(REPORT_STATE_FILE, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_report_state(reported_at):
    os.makedirs(LOG_DIR, exist_ok=True)
    payload = {"last_report_at": reported_at.isoformat(timespec="seconds")}
    with open(REPORT_STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def default_event_cutoff(mode, now_dt):
    today = now_dt.date()
    if mode == "midnight":
        return datetime.combine(today, datetime.strptime("21:30", "%H:%M").time())
    if mode == "close":
        return datetime.combine(today, datetime.strptime("00:00", "%H:%M").time())
    if mode == "open":
        cutoff = datetime.combine(today, datetime.strptime("04:00", "%H:%M").time())
        if now_dt < cutoff:
            cutoff -= timedelta(days=1)
        return cutoff
    return now_dt - timedelta(hours=12)


def event_cutoff(mode, now_dt):
    state_time = parse_datetime(load_report_state().get("last_report_at"))
    if state_time is not None:
        return state_time
    return default_event_cutoff(mode, now_dt)


def load_trade_events(since_dt, until_dt):
    if not os.path.exists(TRADE_EVENT_LOG_FILE):
        return []

    events = []
    try:
        with open(TRADE_EVENT_LOG_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                ts = parse_datetime(event.get("ts"))
                if ts is None:
                    continue
                if since_dt < ts <= until_dt:
                    event["_ts"] = ts
                    events.append(event)
    except OSError:
        logging.exception("Unable to read trade event log")
        return []

    events.sort(key=lambda item: item["_ts"])
    return events


def safe_float(value):
    try:
        if value is None:
            return None
        value = float(value)
        return value
    except (TypeError, ValueError):
        return None


def money(value):
    number = safe_float(value)
    if number is None:
        return "n/a"
    return f"-${abs(number):,.2f}" if number < 0 else f"${number:,.2f}"


def signed_money(value):
    number = safe_float(value)
    if number is None:
        return "n/a"
    sign = "+" if number >= 0 else "-"
    return f"{sign}${abs(number):,.2f}"


def percent(value):
    number = safe_float(value)
    if number is None:
        return "n/a"
    return f"{number:+.2f}%"


def plain_percent(value):
    number = safe_float(value)
    if number is None:
        return "n/a"
    return f"{number:.1f}%"


def number_text(value, digits=2):
    number = safe_float(value)
    if number is None:
        return "n/a"
    return f"{number:.{digits}f}"


def signed_qty(value):
    number = safe_float(value)
    if number is None:
        return "n/a"
    if abs(number - round(number)) < 1e-9:
        return f"{int(round(number)):+d}"
    return f"{number:+.2f}"


def fetch_flagship_snapshot(api_url=DEFAULT_API_URL, timeout=8):
    response = requests.get(api_url, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok", True):
        raise RuntimeError(payload.get("error") or "flagship snapshot returned ok=false")
    return payload


def report_title(mode):
    label = REPORT_LABELS.get(mode, REPORT_LABELS["test"])
    return f"Quant {label}"


def item_sort_key(item):
    pnl = item.get("total") if item.get("kind") == "pair" else item.get("pnl")
    return abs(safe_float(pnl) or 0.0)


def pair_return_light(value):
    number = safe_float(value)
    if number is None:
        return "⚪"
    if number >= 0:
        return "🟢"
    if number >= -0.4:
        return "🟡"
    if number >= -0.8:
        return "🟠"
    return "🔴"


def single_return_light(value):
    number = safe_float(value)
    if number is None:
        return "⚪"
    if number >= 0:
        return "🟢"
    if number > -0.8:
        return "🟡"
    if number > -2.0:
        return "🟠"
    return "🔴"


def square_light(round_light):
    return {
        "🟢": "🟩",
        "🟡": "🟨",
        "🟠": "🟧",
        "🔴": "🟥",
        "⚪": "⬜",
    }.get(round_light, "⬜")


def normalize_position_name(value):
    return str(value or "").replace(" / ", "/").replace("_", "/").upper()


def item_name(item):
    if item.get("kind") == "pair":
        return normalize_position_name(item.get("name") or item.get("pair_name"))
    return normalize_position_name(item.get("symbol") or item.get("name"))


def format_pair_line(item):
    name = item_name(item)
    pnl_value = item.get("total")
    return_pct = item.get("return_pct")
    light = pair_return_light(return_pct)
    pnl_light = square_light(light)
    total = signed_money(pnl_value)
    ret = percent(return_pct)
    return f"{name} {light} {ret} {pnl_light} {total}"


def format_single_line(item):
    symbol = item_name(item)
    pnl_value = item.get("pnl")
    return_pct = item.get("return_pct")
    light = single_return_light(return_pct)
    pnl_light = square_light(light)
    pnl = signed_money(pnl_value)
    ret = percent(return_pct)
    return f"{symbol} {light} {ret} {pnl_light} {pnl}"


def event_name(event):
    if event.get("kind") == "pair":
        if event.get("display_name"):
            name = str(event.get("display_name"))
        elif event.get("ticker_y") or event.get("ticker_x"):
            name = f"{event.get('ticker_y', '')}/{event.get('ticker_x', '')}"
        else:
            name = str(event.get("pair_name") or "")
        return normalize_position_name(name)
    return normalize_position_name(event.get("single_name") or event.get("symbol"))


def format_open_event(event):
    name = event_name(event)
    return f"{name} OPENED"


def format_close_event(event):
    name = event_name(event)
    return_pct = event.get("return_pct")
    light = pair_return_light(return_pct) if event.get("kind") == "pair" else single_return_light(return_pct)
    pnl_light = square_light(light)
    ret = percent(return_pct)
    pnl_value = (
        event.get("total")
        if event.get("total") is not None
        else event.get("pnl")
        if event.get("pnl") is not None
        else event.get("realized_pnl")
    )
    pnl = signed_money(pnl_value)
    if pnl == "n/a":
        return f"{name} {light} {ret} CLOSED"
    return f"{name} {light} {ret} {pnl_light} {pnl} CLOSED"


def format_trade_events(events, since_dt, rendered_open_keys=None):
    rendered_open_keys = rendered_open_keys or set()
    open_events = [event for event in events if str(event.get("event") or "").upper() == "OPEN"]
    close_events = [event for event in events if str(event.get("event") or "").upper() == "CLOSE"]
    if not open_events and not close_events:
        return ""

    lines = []
    seen_open_keys = set()
    for event in events:
        event_type = str(event.get("event") or "").upper()
        if event_type == "OPEN":
            key = event_name(event)
            if key in rendered_open_keys or key in seen_open_keys:
                continue
            seen_open_keys.add(key)
            lines.append(format_open_event(event))
        elif event_type == "CLOSE":
            lines.append(format_close_event(event))
    return "\n".join(lines)


def format_report(snapshot, mode, max_items=12, trade_events=None, since_dt=None):
    items = [item for item in (snapshot.get("items") or []) if not item.get("hidden")]
    pair_items = [item for item in items if item.get("kind") == "pair"]
    single_items = [item for item in items if item.get("kind") != "pair"]
    pair_items.sort(key=item_sort_key, reverse=True)
    single_items.sort(key=item_sort_key, reverse=True)

    trade_events = trade_events or []
    opened_keys = {
        event_name(event)
        for event in trade_events
        if str(event.get("event") or "").upper() == "OPEN"
    }
    if not items:
        trade_section = format_trade_events(trade_events, since_dt) if since_dt else ""
        return "\n\n".join(part for part in ["No active positions.", trade_section] if part)

    lines = []
    rendered_open_keys = set()
    ordered_items = pair_items + single_items
    for item in ordered_items[:max_items]:
        formatter = format_pair_line if item.get("kind") == "pair" else format_single_line
        line = formatter(item)
        key = item_name(item)
        if key in opened_keys:
            line = f"{line} OPENED"
            rendered_open_keys.add(key)
        lines.append(line)

    if len(items) > max_items:
        lines.append(f"... plus {len(items) - max_items} more positions")

    trade_section = (
        format_trade_events(trade_events, since_dt, rendered_open_keys=rendered_open_keys)
        if since_dt
        else ""
    )
    position_section = "\n".join(lines)
    return "\n\n".join(part for part in [position_section, trade_section] if part)


def format_failure_report(mode, error):
    label = REPORT_LABELS.get(mode, REPORT_LABELS["test"])
    current_time = now_local().strftime("%Y-%m-%d %H:%M:%S %Z")
    return "\n".join(
        [
            f"{label}",
            current_time,
            "",
            "Position report source is unavailable.",
            str(error),
        ]
    )


def build_parser():
    parser = argparse.ArgumentParser(description="Send portfolio position reports to iPhone.")
    parser.add_argument("mode", choices=sorted(REPORT_LABELS), help="Report type.")
    parser.add_argument("--api-url", default=os.getenv("POSITION_REPORT_API_URL", DEFAULT_API_URL))
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--max-items", type=int, default=12)
    parser.add_argument("--group", default=os.getenv("POSITION_REPORT_GROUP", DEFAULT_GROUP))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-failure-push",
        action="store_true",
        help="Do not push an alert when the position source cannot be read.",
    )
    return parser


def main(argv=None):
    load_local_env()
    configure_logging()
    args = build_parser().parse_args(argv)

    title = report_title(args.mode)
    reported_at = now_local().replace(tzinfo=None)
    since_dt = event_cutoff(args.mode, reported_at)
    trade_events = load_trade_events(since_dt, reported_at)
    try:
        snapshot = fetch_flagship_snapshot(args.api_url, timeout=args.timeout)
        body = format_report(
            snapshot,
            args.mode,
            max_items=args.max_items,
            trade_events=trade_events,
            since_dt=since_dt,
        )
        logging.info(
            "Built %s report from %s with %s item(s), %s trade event(s) since %s",
            args.mode,
            args.api_url,
            len(snapshot.get("items") or []),
            len(trade_events),
            since_dt.isoformat(timespec="seconds"),
        )
    except Exception as exc:
        body = format_failure_report(args.mode, exc)
        logging.exception("Unable to build %s position report", args.mode)
        if args.no_failure_push:
            print(body)
            return 1

    if args.dry_run:
        print(f"TITLE: {title}")
        print(body)
        return 0

    result = send_bark(title, body, group=args.group)
    logging.info("Push result: %s", json.dumps(result, ensure_ascii=False))
    print(json.dumps(result, ensure_ascii=False))
    if result.get("ok"):
        save_report_state(reported_at)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
