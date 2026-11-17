import json
import os
from datetime import datetime


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TRADE_EVENT_LOG_PATH = os.path.join(ROOT_DIR, "logs", "flagship_trade_events.log")


def log_trade_event(event):
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        **event,
    }
    os.makedirs(os.path.dirname(TRADE_EVENT_LOG_PATH), exist_ok=True)
    with open(TRADE_EVENT_LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
