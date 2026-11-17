#!/usr/bin/env python3
"""Keep an outbound-only SSH tunnel for the public read-only dashboard."""

from __future__ import annotations

import subprocess
import time


REMOTE = "root@45.32.95.94"
REMOTE_PORT = "18000"
LOCAL_TARGET = "127.0.0.1:8000"
RECONNECT_SECONDS = 5


def command():
    return [
        "ssh",
        "-N",
        "-R", f"127.0.0.1:{REMOTE_PORT}:{LOCAL_TARGET}",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=60",
        "-o", "ServerAliveCountMax=3",
        "-o", "TCPKeepAlive=yes",
        REMOTE,
    ]


def main():
    print(f"[public-tunnel] forwarding {REMOTE}:127.0.0.1:{REMOTE_PORT} -> {LOCAL_TARGET}", flush=True)
    while True:
        result = subprocess.run(command(), check=False)
        print(f"[public-tunnel] ssh exited={result.returncode}; reconnecting in {RECONNECT_SECONDS}s", flush=True)
        time.sleep(RECONNECT_SECONDS)


if __name__ == "__main__":
    main()
