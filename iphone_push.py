import argparse
import json
import os
import sys
from datetime import datetime
from urllib.parse import quote

import requests


PROJECT_DIR = os.path.abspath(os.path.dirname(__file__))
LOCAL_ENV_FILE = os.path.join(PROJECT_DIR, ".env.local")
DEFAULT_BARK_SERVER = "https://api.day.app"
DEFAULT_GROUP = "Quant"
DEFAULT_TIMEOUT_SECONDS = 10


class PushConfigError(RuntimeError):
    pass


def redact_secret(value):
    text = str(value or "")
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}...{text[-4:]}"


def clean_base_url(value):
    return str(value or "").strip().rstrip("/")


def load_local_env(path=LOCAL_ENV_FILE):
    if not os.path.exists(path):
        return []

    loaded_keys = []
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if not key or key in os.environ:
                continue

            os.environ[key] = value
            loaded_keys.append(key)
    return loaded_keys


def bark_endpoint_from_env():
    explicit_url = clean_base_url(os.getenv("BARK_PUSH_URL"))
    if explicit_url:
        return explicit_url, "BARK_PUSH_URL"

    device_key = str(os.getenv("BARK_DEVICE_KEY") or "").strip().strip("/")
    if not device_key:
        raise PushConfigError(
            "Missing Bark config. Set BARK_DEVICE_KEY, or set BARK_PUSH_URL to "
            "a complete endpoint such as https://api.day.app/<your_key>."
        )

    server = clean_base_url(os.getenv("BARK_SERVER") or DEFAULT_BARK_SERVER)
    return f"{server}/{device_key}", "BARK_DEVICE_KEY"


def check_bark_response(response):
    if response.status_code >= 400:
        return False, f"HTTP {response.status_code}: {response.text[:300]}"

    try:
        payload = response.json()
    except json.JSONDecodeError:
        return True, response.text[:300]

    code = payload.get("code")
    if code in (None, 0, 200):
        return True, payload.get("message") or "ok"
    return False, json.dumps(payload, ensure_ascii=False)[:300]


def send_bark(title, body, group=DEFAULT_GROUP, sound=None, timeout=DEFAULT_TIMEOUT_SECONDS):
    endpoint, source = bark_endpoint_from_env()
    payload = {
        "title": title,
        "body": body,
        "group": group,
    }
    if sound:
        payload["sound"] = sound

    try:
        response = requests.post(endpoint, json=payload, timeout=timeout)
        ok, detail = check_bark_response(response)
        if ok:
            return {
                "ok": True,
                "method": "POST",
                "endpoint_source": source,
                "detail": detail,
            }
    except requests.RequestException as exc:
        detail = str(exc)

    query = f"group={quote(group)}"
    if sound:
        query += f"&sound={quote(sound)}"
    fallback_url = (
        f"{endpoint}/{quote(title, safe='')}/{quote(body, safe='')}?{query}"
    )
    try:
        response = requests.get(fallback_url, timeout=timeout)
    except requests.RequestException as exc:
        return {
            "ok": False,
            "method": "GET",
            "endpoint_source": source,
            "detail": f"POST failed: {detail}; GET failed: {exc}",
        }

    ok, get_detail = check_bark_response(response)
    return {
        "ok": ok,
        "method": "GET",
        "endpoint_source": source,
        "detail": get_detail if ok else f"POST failed: {detail}; GET failed: {get_detail}",
    }


def build_parser():
    parser = argparse.ArgumentParser(description="Send a test push notification to iPhone via Bark.")
    parser.add_argument("--title", default="Quant Push Test")
    parser.add_argument(
        "--body",
        default=None,
        help="Message body. Defaults to a timestamped test message.",
    )
    parser.add_argument("--group", default=DEFAULT_GROUP)
    parser.add_argument("--sound", default=None)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv=None):
    loaded_keys = load_local_env()
    args = build_parser().parse_args(argv)
    body = args.body or f"iPhone push channel is reachable at {datetime.now():%Y-%m-%d %H:%M:%S}."

    try:
        endpoint, source = bark_endpoint_from_env()
    except PushConfigError as exc:
        print(f"[iphone_push] config error: {exc}", file=sys.stderr)
        return 2

    print(f"[iphone_push] provider=bark source={source} endpoint={redact_secret(endpoint)}")
    if loaded_keys:
        print(f"[iphone_push] loaded_env={','.join(loaded_keys)}")
    print(f"[iphone_push] title={args.title!r} group={args.group!r}")

    if args.dry_run:
        print("[iphone_push] dry run only; no request sent.")
        return 0

    result = send_bark(
        args.title,
        body,
        group=args.group,
        sound=args.sound,
        timeout=args.timeout,
    )
    print(f"[iphone_push] result={json.dumps(result, ensure_ascii=False)}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
