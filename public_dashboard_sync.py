#!/usr/bin/env python3
"""Publish a read-only Simple Dashboard snapshot to the public viewer.

The home machine always initiates the SSH connection.  The public server only
serves a static JSON file and cannot reach this machine or execute trades.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


PROJECT_DIR = Path(__file__).resolve().parent
API_BASE = "http://127.0.0.1:8000"
PUBLIC_HOST = "root@45.32.95.94"
PUBLIC_REMOTE_DIR = "/var/www/html/finance/quant/data"
# The public mirror favors low bandwidth over low latency.
INTERVAL_SECONDS = 120
ENDPOINTS = {
    "pairs": "/api/pairs",
    "waiting_entry": "/api/waiting-entry",
    "flagship_board": "/api/flagship-board",
    "market_calendar": "/api/market-calendar",
    "pending_zscores": "/api/orders/pending-zscores",
}
SNAPSHOT_FILE = PROJECT_DIR / "logs" / "public_dashboard_snapshot.json"
SSH_OPTIONS = (
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "ConnectTimeout=60",
    "-o", "ControlMaster=auto",
    "-o", "ControlPersist=300",
    "-o", "ControlPath=/tmp/quant-public-dashboard-%r@%h:%p",
)


def fetch_json(path: str):
    request = Request(f"{API_BASE}{path}", headers={"Accept": "application/json"})
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def build_snapshot():
    payload = {
        "version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "home-quant-machine",
        "sections": {},
        "errors": {},
    }
    for name, path in ENDPOINTS.items():
        try:
            payload["sections"][name] = fetch_json(path)
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            payload["errors"][name] = str(exc)
    return payload


def publish(snapshot):
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    subprocess.run(
        [
            "scp",
            "-q",
            *SSH_OPTIONS,
            str(SNAPSHOT_FILE),
            f"{PUBLIC_HOST}:{PUBLIC_REMOTE_DIR}/dashboard.json",
        ],
        check=True,
        timeout=70,
    )


def main():
    print(f"[public-dashboard] publishing every {INTERVAL_SECONDS}s to {PUBLIC_HOST}", flush=True)
    while True:
        try:
            publish(build_snapshot())
            print(f"[public-dashboard] published {datetime.now().isoformat(timespec='seconds')}", flush=True)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[public-dashboard] publish failed: {exc}", file=sys.stderr, flush=True)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
