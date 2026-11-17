from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import pytz

NY_TZ = pytz.timezone("US/Eastern")
BJ_TZ = pytz.timezone("Asia/Shanghai")

Session = Tuple[datetime, datetime]


def status_line(msg: str) -> None:
    width = max(20, shutil.get_terminal_size((120, 20)).columns)
    text = str(msg)
    if len(text) >= width:
        text = text[: width - 1]
    print("\r\033[K" + text, end="", flush=True)


def next_5min_boundary(now: datetime) -> datetime:
    """Return the next 5-minute boundary after `now`."""
    base = now.replace(second=0, microsecond=0)
    add_min = (5 - (base.minute % 5)) % 5
    nxt = base + timedelta(minutes=add_min)
    if nxt <= now:
        nxt += timedelta(minutes=5)
    return nxt


def _pick_hours_field(contract_details, use_rth: bool) -> str:
    """
    Prefer liquidHours when use_rth=True, because it matches regular trading hours.
    Fall back to tradingHours if liquidHours is missing.
    """
    if use_rth:
        return getattr(contract_details, "liquidHours", "") or getattr(contract_details, "tradingHours", "")
    return getattr(contract_details, "tradingHours", "") or getattr(contract_details, "liquidHours", "")


def parse_hours_string(
    hours_str: str,
    now_ny: Optional[datetime] = None,
    days_ahead: int = 7,
    tz=NY_TZ,
) -> List[Session]:
    """
    Parse IB hours strings like:
        20260407:0930-20260407:1600;20260408:CLOSED;...

    Returns a sorted list of timezone-aware (start_dt, end_dt) tuples in NY time.
    Only keeps sessions from today through `days_ahead` days ahead.
    """
    if now_ny is None:
        now_ny = datetime.now(tz)

    today_str = now_ny.strftime("%Y%m%d")
    end_day_str = (now_ny + timedelta(days=days_ahead)).strftime("%Y%m%d")

    sessions: List[Session] = []

    for raw_part in (hours_str or "").split(";"):
        part = raw_part.strip()
        if not part:
            continue
        if part.endswith(":CLOSED") or "CLOSED" in part:
            continue

        try:
            left, right = part.split("-")
            left_day, left_hm = left.split(":")
            right_day, right_hm = right.split(":")

            if right_day < today_str:
                continue
            if left_day > end_day_str:
                continue

            start_dt = tz.localize(datetime.strptime(left_day + left_hm, "%Y%m%d%H%M"))
            end_dt = tz.localize(datetime.strptime(right_day + right_hm, "%Y%m%d%H%M"))

            if end_dt <= start_dt:
                continue

            sessions.append((start_dt, end_dt))

        except Exception as e:
            print(f"⚠ tradingHours 解析失败: {part} | {e}")

    sessions.sort(key=lambda x: x[0])
    return sessions


def get_trading_sessions(
    ib,
    contract,
    use_rth: bool = True,
    days_ahead: int = 7,
    tz=NY_TZ,
) -> List[Session]:
    """
    Read contract details from IB and return upcoming sessions.

    - use_rth=True: prefer liquidHours (regular session)
    - use_rth=False: use tradingHours (extended + regular session)
    """
    details = ib.reqContractDetails(contract)
    if not details:
        return []

    cd = details[0]
    hours_str = _pick_hours_field(cd, use_rth=use_rth)
    now_ny = datetime.now(tz)
    return parse_hours_string(hours_str, now_ny=now_ny, days_ahead=days_ahead, tz=tz)


def get_current_or_next_session(
    ib,
    contract,
    now_ny: Optional[datetime] = None,
    use_rth: bool = True,
    days_ahead: int = 7,
    tz=NY_TZ,
) -> Tuple[Optional[str], Optional[datetime], Optional[datetime]]:
    """
    Returns one of:
        ("IN_SESSION", open_dt, close_dt)
        ("BEFORE_SESSION", open_dt, close_dt)
        (None, None, None)
    """
    if now_ny is None:
        now_ny = datetime.now(tz)

    sessions = get_trading_sessions(
        ib=ib,
        contract=contract,
        use_rth=use_rth,
        days_ahead=days_ahead,
        tz=tz,
    )

    if not sessions:
        return None, None, None

    for open_dt, close_dt in sessions:
        if open_dt <= now_ny < close_dt:
            return "IN_SESSION", open_dt, close_dt
        if now_ny < open_dt:
            return "BEFORE_SESSION", open_dt, close_dt

    return None, None, None


def wait_until(
    ib,
    target_time: datetime,
    label: str = "WAIT",
    ny_tz=NY_TZ,
    bj_tz=BJ_TZ,
) -> None:
    """Sleep with a live countdown until target_time."""
    while True:
        now_ny = datetime.now(ny_tz)
        now_bj = datetime.now(bj_tz)

        remain = target_time - now_ny
        remain_sec = remain.total_seconds()
        if remain_sec <= 0:
            print("\r\033[K", end="", flush=True)
            print()
            break

        h = int(remain_sec // 3600)
        m = int((remain_sec % 3600) // 60)
        s = int(remain_sec % 60)

        status_line(
            f"⏳ {label} | BJ {now_bj:%H:%M:%S} | "
            f"NY {now_ny:%H:%M:%S} | "
            f"next {target_time:%Y-%m-%d %H:%M:%S} | "
            f"sleep {h:02}:{m:02}:{s:02}"
        )

        ib.sleep(1)


def is_in_session(
    ib,
    contract,
    now_ny: Optional[datetime] = None,
    use_rth: bool = True,
    days_ahead: int = 7,
    tz=NY_TZ,
) -> bool:
    state, _, _ = get_current_or_next_session(
        ib=ib,
        contract=contract,
        now_ny=now_ny,
        use_rth=use_rth,
        days_ahead=days_ahead,
        tz=tz,
    )
    return state == "IN_SESSION"


def get_next_open(
    ib,
    contract,
    now_ny: Optional[datetime] = None,
    use_rth: bool = True,
    days_ahead: int = 7,
    tz=NY_TZ,
) -> Optional[datetime]:
    state, open_dt, _ = get_current_or_next_session(
        ib=ib,
        contract=contract,
        now_ny=now_ny,
        use_rth=use_rth,
        days_ahead=days_ahead,
        tz=tz,
    )
    if state == "BEFORE_SESSION":
        return open_dt
    return None
