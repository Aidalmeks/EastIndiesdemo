"""Shared non-secret runtime configuration for the local operations stack."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent


def _load_local_env() -> None:
    path = PROJECT_DIR / ".env"
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() and key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


_load_local_env()


def env(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)


def database_config(kind: str, **extra: Any) -> dict[str, Any]:
    """Return a PyMySQL config without embedding credentials in source code."""
    normalized = kind.strip().upper()
    password = env(f"QUANT_{normalized}_DB_PASSWORD") or env("QUANT_DB_PASSWORD") or env("MYSQL_PASSWORD")
    if not password:
        raise RuntimeError(f"Missing database password for {kind}. Set QUANT_{normalized}_DB_PASSWORD or QUANT_DB_PASSWORD in .env.")
    config: dict[str, Any] = {
        "host": env("QUANT_DB_HOST", "127.0.0.1"),
        "user": env("QUANT_DB_USER", "quant"),
        "password": password,
        "database": env(f"QUANT_{normalized}_DB_NAME", f"quant_{kind.strip().lower()}"),
    }
    config.update(extra)
    return config


def redis_config() -> dict[str, Any]:
    return {"host": env("QUANT_REDIS_HOST", "127.0.0.1"), "port": int(env("QUANT_REDIS_PORT", "6379") or 6379), "db": int(env("QUANT_REDIS_DB", "0") or 0), "decode_responses": True}


def api_base_url() -> str:
    return f"http://{env('QUANT_API_HOST', '127.0.0.1')}:{env('QUANT_API_PORT', '8000')}/api"


def ib_host() -> str:
    return str(env("QUANT_IB_HOST", "127.0.0.1"))


def ib_port_account_map() -> dict[int, str]:
    raw = env("QUANT_IB_PORT_ACCOUNTS")
    if not raw:
        raise RuntimeError("Missing QUANT_IB_PORT_ACCOUNTS; configure it in .env before enabling IBKR services.")
    try:
        values = json.loads(raw)
        return {int(port): str(account) for port, account in values.items() if str(account).strip()}
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("QUANT_IB_PORT_ACCOUNTS must be JSON, for example {\"4002\": \"YOUR_PAPER_ACCOUNT\"}.") from exc
