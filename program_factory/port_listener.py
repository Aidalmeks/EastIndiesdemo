from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from runtime_settings import ib_port_account_map

try:
    import psutil
except Exception:
    psutil = None

IB_PROCESS_HINTS: Tuple[str, ...] = ("ibgateway", "tws", "java", "javaw")


@dataclass(frozen=True)
class ListenerInfo:
    port: int
    pid: int
    process_name: Optional[str]
    local_address: str


@dataclass(frozen=True)
class PortSelection:
    port: int
    account: str
    pid: Optional[int]
    process_name: Optional[str]
    reason: str


def _run_cmd(command: Sequence[str], timeout_sec: int = 4) -> subprocess.CompletedProcess:
    kwargs = dict(
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
        check=False,
    )

    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs["startupinfo"] = startupinfo

    return subprocess.run(
        list(command),
        **kwargs,
    )


def _extract_port(endpoint: str) -> Optional[int]:
    text = endpoint.strip()
    if not text:
        return None

    if text.startswith("["):
        if "]:" not in text:
            return None
        port_text = text.rsplit("]:", 1)[-1]
    else:
        if ":" not in text:
            return None
        port_text = text.rsplit(":", 1)[-1]

    try:
        return int(port_text)
    except ValueError:
        return None


def _process_name_from_pid(pid: int) -> Optional[str]:
    if pid <= 0:
        return None

    if psutil is not None:
        try:
            return psutil.Process(pid).name()
        except (psutil.Error, OSError, ValueError):
            return None

    if os.name != "nt":
        cp = _run_cmd(["ps", "-p", str(pid), "-o", "comm="])
        if cp.returncode != 0:
            return None
        return cp.stdout.strip() or None

    cp = _run_cmd(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
    if cp.returncode != 0:
        return None

    line = cp.stdout.strip()
    if not line or line.startswith("INFO:"):
        return None

    try:
        row = next(csv.reader([line]))
    except Exception:
        return None

    if not row:
        return None
    return row[0].strip() or None


def _scan_with_psutil(ports: Iterable[int]) -> Optional[List[ListenerInfo]]:
    if psutil is None:
        return None

    target_ports = set(int(p) for p in ports)
    listeners: List[ListenerInfo] = []
    seen = set()

    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.Error, OSError):
        return None

    for conn in connections:
        if conn.status != psutil.CONN_LISTEN or not conn.laddr:
            continue

        port = int(getattr(conn.laddr, "port", 0))
        if port not in target_ports:
            continue

        pid = int(conn.pid or -1)
        host = str(getattr(conn.laddr, "ip", "") or getattr(conn.laddr, "host", ""))
        local_addr = f"{host}:{port}" if host else str(port)
        key = (port, pid, local_addr)
        if key in seen:
            continue
        seen.add(key)

        listeners.append(
            ListenerInfo(
                port=port,
                pid=pid,
                process_name=_process_name_from_pid(pid),
                local_address=local_addr,
            )
        )

    return listeners


def _scan_with_get_nettcpconnection(ports: Iterable[int]) -> Optional[List[ListenerInfo]]:
    if os.name != "nt":
        return None

    ports = tuple(sorted(set(int(p) for p in ports)))
    if not ports:
        return []

    port_arg = ",".join(str(p) for p in ports)
    script = (
        f"Get-NetTCPConnection -State Listen -LocalPort {port_arg} "
        "| Select-Object LocalAddress,LocalPort,OwningProcess "
        "| ConvertTo-Json -Compress"
    )
    cp = _run_cmd(["powershell", "-NoProfile", "-Command", script])
    if cp.returncode != 0:
        return None

    raw = cp.stdout.strip()
    if not raw:
        return []

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if isinstance(payload, dict):
        rows = [payload]
    elif isinstance(payload, list):
        rows = payload
    else:
        return None

    listeners: List[ListenerInfo] = []
    for row in rows:
        try:
            port = int(row.get("LocalPort"))
            pid = int(row.get("OwningProcess"))
        except (TypeError, ValueError):
            continue
        if port not in ports:
            continue

        local_addr = str(row.get("LocalAddress", ""))
        listeners.append(
            ListenerInfo(
                port=port,
                pid=pid,
                process_name=_process_name_from_pid(pid),
                local_address=local_addr,
            )
        )

    return listeners


def _scan_with_ss(ports: Iterable[int]) -> Optional[List[ListenerInfo]]:
    if os.name == "nt":
        return None

    target_ports = set(int(p) for p in ports)
    cp = _run_cmd(["ss", "-ltnp"])
    if cp.returncode != 0:
        return None

    listeners: List[ListenerInfo] = []
    seen = set()
    for line in cp.stdout.splitlines():
        text = line.strip()
        if not text or text.startswith("State "):
            continue

        parts = text.split()
        if len(parts) < 4 or parts[0].upper() != "LISTEN":
            continue

        local = parts[3]
        port = _extract_port(local)
        if port is None or port not in target_ports:
            continue

        pid = -1
        process_name = None
        users_match = re.search(r'users:\(\("([^"]+)",pid=(\d+),', text)
        if users_match:
            process_name = users_match.group(1)
            pid = int(users_match.group(2))

        key = (port, pid, local)
        if key in seen:
            continue
        seen.add(key)

        listeners.append(
            ListenerInfo(
                port=port,
                pid=pid,
                process_name=process_name or _process_name_from_pid(pid),
                local_address=local,
            )
        )

    return listeners


def _scan_with_netstat(ports: Iterable[int]) -> List[ListenerInfo]:
    target_ports = set(int(p) for p in ports)
    cp = _run_cmd(["netstat", "-ano", "-p", "tcp"])
    if cp.returncode != 0:
        return []

    listeners: List[ListenerInfo] = []
    seen = set()
    for line in cp.stdout.splitlines():
        text = line.strip()
        if not text.upper().startswith("TCP"):
            continue

        parts = text.split()
        if len(parts) < 5:
            continue

        local = parts[1]
        state = parts[3].upper()
        pid_text = parts[4]
        if state != "LISTENING":
            continue

        port = _extract_port(local)
        if port is None or port not in target_ports:
            continue

        try:
            pid = int(pid_text)
        except ValueError:
            pid = -1

        key = (port, pid, local)
        if key in seen:
            continue
        seen.add(key)

        listeners.append(
            ListenerInfo(
                port=port,
                pid=pid,
                process_name=_process_name_from_pid(pid),
                local_address=local,
            )
        )

    return listeners


def scan_listening_ports(ports: Iterable[int] = (4001, 4002)) -> Dict[int, List[ListenerInfo]]:
    port_list = tuple(sorted(set(int(p) for p in ports)))
    listeners = _scan_with_psutil(port_list)
    if listeners is None and os.name == "nt":
        listeners = _scan_with_get_nettcpconnection(port_list)
    if listeners is None and os.name != "nt":
        listeners = _scan_with_ss(port_list)
    if listeners is None and os.name == "nt":
        listeners = _scan_with_netstat(port_list)
    if listeners is None:
        listeners = []

    by_port: Dict[int, List[ListenerInfo]] = {p: [] for p in port_list}
    for item in listeners:
        by_port.setdefault(item.port, []).append(item)

    return by_port


def _looks_like_ib_process(process_name: Optional[str], hints: Sequence[str] = IB_PROCESS_HINTS) -> bool:
    if not process_name:
        return False
    name = process_name.lower()
    return any(h.lower() in name for h in hints)


def _resolve_port_account_map(port_account_map: Optional[Dict[int, str]]) -> Dict[int, str]:
    resolved = dict(port_account_map) if port_account_map is not None else ib_port_account_map()
    if not resolved:
        raise RuntimeError("IBKR port/account mapping is empty. Set QUANT_IB_PORT_ACCOUNTS in .env.")
    return resolved


def choose_connection(
    port_account_map: Optional[Dict[int, str]] = None,
    preferred_order: Sequence[int] = (4001, 4002),
    require_ib_process_hint: bool = False,
) -> PortSelection:
    port_account_map = _resolve_port_account_map(port_account_map)
    listeners = scan_listening_ports(port_account_map.keys())

    available_ports = [p for p in preferred_order if listeners.get(p)]
    if not available_ports:
        raise RuntimeError(f"No listener found on configured ports: {sorted(port_account_map)}.")

    if require_ib_process_hint:
        hinted_ports = [
            p for p in available_ports if any(_looks_like_ib_process(x.process_name) for x in listeners[p])
        ]
        if hinted_ports:
            available_ports = hinted_ports
        else:
            raise RuntimeError("Ports are open but no listener looks like IB/TWS process.")

    selected_port = available_ports[0]
    selected_account = port_account_map[selected_port]
    candidates = listeners[selected_port]

    selected_listener = next(
        (x for x in candidates if _looks_like_ib_process(x.process_name)),
        candidates[0] if candidates else None,
    )

    if len(available_ports) > 1:
        reason = f"Both ports are listening. Selected {selected_port} by preference order {list(preferred_order)}."
    else:
        reason = f"Only port {selected_port} is listening."

    return PortSelection(
        port=selected_port,
        account=selected_account,
        pid=selected_listener.pid if selected_listener else None,
        process_name=selected_listener.process_name if selected_listener else None,
        reason=reason,
    )


def wait_and_choose_connection(
    timeout_sec: int = 30,
    poll_interval_sec: float = 1.0,
    port_account_map: Optional[Dict[int, str]] = None,
    preferred_order: Sequence[int] = (4001, 4002),
    require_ib_process_hint: bool = False,
) -> PortSelection:
    port_account_map = _resolve_port_account_map(port_account_map)
    deadline = time.time() + max(0, timeout_sec)
    last_error: Optional[Exception] = None

    while True:
        try:
            return choose_connection(
                port_account_map=port_account_map,
                preferred_order=preferred_order,
                require_ib_process_hint=require_ib_process_hint,
            )
        except RuntimeError as exc:
            last_error = exc
            if time.time() >= deadline:
                break
            time.sleep(max(0.1, poll_interval_sec))

    raise TimeoutError(f"Could not find a valid listening port within {timeout_sec}s: {last_error}")


def _main() -> int:
    parser = argparse.ArgumentParser(description="Detect listener on 4001/4002 and select account mapping.")
    parser.add_argument("--timeout", type=int, default=0, help="Wait timeout in seconds. 0 means single scan.")
    parser.add_argument("--poll", type=float, default=1.0, help="Polling interval in seconds when waiting.")
    parser.add_argument(
        "--prefer",
        default="4001,4002",
        help="Preferred port order, comma separated. Example: 4001,4002",
    )
    parser.add_argument(
        "--require-ib-process",
        action="store_true",
        help="Require process name to look like ibgateway/tws/java.",
    )
    args = parser.parse_args()

    preferred_order = tuple(int(x.strip()) for x in args.prefer.split(",") if x.strip())
    try:
        if args.timeout > 0:
            result = wait_and_choose_connection(
                timeout_sec=args.timeout,
                poll_interval_sec=args.poll,
                preferred_order=preferred_order,
                require_ib_process_hint=args.require_ib_process,
            )
        else:
            result = choose_connection(
                preferred_order=preferred_order,
                require_ib_process_hint=args.require_ib_process,
            )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1

    print(json.dumps({"ok": True, "selection": asdict(result)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
