import logging
import json
import os
import shlex
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta
from zoneinfo import ZoneInfo

import schedule


PROJECT_DIR = os.path.abspath(os.path.dirname(__file__))
LOCAL_ZONEINFO_DIR = os.path.join(PROJECT_DIR, "zoneinfo")
LOG_DIR = os.path.join(PROJECT_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "scheduler.log")
SCRIPT_LOG_DIR = os.path.join(LOG_DIR, "scheduled_scripts")
CALENDAR_CACHE_FILE = os.path.join(LOG_DIR, "ib_us_market_calendar.json")

SERVER_TIMEZONE = "Asia/Shanghai"
DISPLAY_TZ = ZoneInfo(SERVER_TIMEZONE)
POLL_SECONDS = 15
IB_HOST = "127.0.0.1"
IB_CLIENT_ID = 88
IB_CALENDAR_SYMBOL = "SPY"
IB_CALENDAR_DAYS = 21
DYNAMIC_CALENDAR_TAG = "ib_us_market_calendar"

BEIJING_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
BEIJING_POST_CLOSE_DAYS = ("tuesday", "wednesday", "thursday", "friday", "saturday")
DYNAMIC_JOBS = []
STATIC_JOBS = []
CALENDAR_CACHE_METADATA = {}


@dataclass(frozen=True)
class ScriptJob:
    time_text: str
    label: str
    script_name: str
    script_args: tuple = ()
    hold_on_exit: bool = True
    open_terminal: bool = True


STARTUP_SEQUENCE = (
    ScriptJob("21:23", "waiting_entry", "waiting_entry.py", hold_on_exit=False, open_terminal=False),
    ScriptJob("21:24", "live_data", "livedata.py", hold_on_exit=False, open_terminal=False),
    ScriptJob("21:25", "flagship", "flagship_board.py", hold_on_exit=False, open_terminal=False),
)

SHUTDOWN_SEQUENCE = (
    ScriptJob("04:02", "waiting_entry", "waiting_entry.py"),
    ScriptJob("04:03", "live_data", "livedata.py"),
    ScriptJob("04:04", "flagship", "flagship_board.py"),
)

PUBLIC_DASHBOARD_TUNNEL_JOB = ScriptJob(
    "",
    "public_dashboard_tunnel",
    "public_dashboard_tunnel.py",
    hold_on_exit=False,
    open_terminal=False,
)

FILL_DB_JOB = ScriptJob("05:01", "fill_db", "fill_db.py", hold_on_exit=False, open_terminal=False)
OPEN_REPORT_JOB = ScriptJob(
    "21:31",
    "position_report_open",
    "position_reporter.py",
    script_args=("open",),
    hold_on_exit=False,
    open_terminal=False,
)
MIDNIGHT_REPORT_JOB = ScriptJob(
    "00:00",
    "position_report_midnight",
    "position_reporter.py",
    script_args=("midnight",),
    hold_on_exit=False,
    open_terminal=False,
)
CLOSE_REPORT_JOB = ScriptJob(
    "04:00",
    "position_report_close",
    "position_reporter.py",
    script_args=("close",),
    hold_on_exit=False,
    open_terminal=False,
)


def configure_runtime():
    os.environ.setdefault("TZ", SERVER_TIMEZONE)
    if hasattr(time, "tzset"):
        time.tzset()

    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(SCRIPT_LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def now_text():
    return datetime.now(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def build_child_env():
    env = os.environ.copy()
    env["TZ"] = SERVER_TIMEZONE
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = env.get("PYTHONUNBUFFERED", "1")
    if os.path.isdir(LOCAL_ZONEINFO_DIR):
        existing_tzpath = env.get("PYTHONTZPATH")
        env["PYTHONTZPATH"] = (
            f"{LOCAL_ZONEINFO_DIR}{os.pathsep}{existing_tzpath}"
            if existing_tzpath
            else LOCAL_ZONEINFO_DIR
        )
    return env


def script_output_log_path(job):
    safe_label = "".join(
        char if char.isalnum() or char in ("-", "_") else "_"
        for char in job.label
    )
    return os.path.join(SCRIPT_LOG_DIR, f"{safe_label}.log")


def build_terminal_shell_command(script_name, script_args=(), hold_on_exit=True):
    command = " ".join(
        [
            shlex.quote(sys.executable),
            "-X",
            "utf8",
            "-u",
            shlex.quote(script_name),
            *(shlex.quote(str(arg)) for arg in script_args),
        ]
    )
    project_dir = shlex.quote(PROJECT_DIR)
    tz_export = f"export TZ={shlex.quote(SERVER_TIMEZONE)}; "
    if os.path.isdir(LOCAL_ZONEINFO_DIR):
        tz_export += f"export PYTHONTZPATH={shlex.quote(LOCAL_ZONEINFO_DIR)}; "
    shell_command = (
        f"cd {project_dir}; "
        f"{tz_export}"
        f"echo '[quant] cwd='$(pwd); "
        "echo '[quant] TZ='$TZ; "
        "echo '[quant] PYTHONTZPATH='$PYTHONTZPATH; "
        f"echo '[quant] command: {command}'; "
        f"{command}"
    )
    if hold_on_exit:
        shell_command += (
            "; "
            "status=$?; "
            "echo; "
            "echo '[quant] process exited with status' $status; "
            "echo '[quant] close this terminal when finished reviewing output.'; "
            "exec bash"
        )
    return shell_command


def terminal_command_candidates(title, shell_command):
    candidates = []

    ptyxis = shutil.which("ptyxis")
    if ptyxis:
        candidates.append(
            [
                ptyxis,
                "--new-window",
                "--title",
                title,
                "--working-directory",
                PROJECT_DIR,
                "--",
                "bash",
                "-lc",
                shell_command,
            ]
        )

    x_terminal = shutil.which("x-terminal-emulator")
    if x_terminal and os.path.realpath(x_terminal) != os.path.realpath(ptyxis or ""):
        candidates.append([x_terminal, "-T", title, "-e", "bash", "-lc", shell_command])

    gnome_terminal = shutil.which("gnome-terminal")
    if gnome_terminal:
        candidates.append(
            [
                gnome_terminal,
                "--title",
                title,
                "--working-directory",
                PROJECT_DIR,
                "--",
                "bash",
                "-lc",
                shell_command,
            ]
        )

    for program in (
        "konsole",
        "xfce4-terminal",
        "mate-terminal",
        "lxterminal",
        "tilix",
        "terminator",
        "xterm",
    ):
        path = shutil.which(program)
        if not path:
            continue
        if program == "konsole":
            candidates.append(
                [
                    path,
                    "--new-tab",
                    "--workdir",
                    PROJECT_DIR,
                    "-p",
                    f"tabtitle={title}",
                    "-e",
                    "bash",
                    "-lc",
                    shell_command,
                ]
            )
        elif program == "xfce4-terminal":
            candidates.append(
                [
                    path,
                    "--title",
                    title,
                    "--working-directory",
                    PROJECT_DIR,
                    "--command",
                    f"bash -lc {shlex.quote(shell_command)}",
                ]
            )
        elif program == "xterm":
            candidates.append([path, "-T", title, "-e", "bash", "-lc", shell_command])
        else:
            candidates.append([path, "-t", title, "-e", "bash", "-lc", shell_command])

    return candidates


def command_contains_script(command_line, script_name):
    try:
        argv = shlex.split(command_line)
    except ValueError:
        argv = command_line.split()
    if not argv:
        return False

    executable = os.path.basename(argv[0]).lower()
    if not (executable.startswith("python") or executable.startswith("pythonw")):
        return False

    script_basename = os.path.basename(script_name)
    script_abs = os.path.abspath(os.path.join(PROJECT_DIR, script_name))

    for arg in argv[1:]:
        normalized = os.path.abspath(os.path.join(PROJECT_DIR, arg))
        if os.path.basename(arg) == script_basename and normalized == script_abs:
            return True
    return False


def running_script_pids(script_name):
    if os.name == "nt":
        ps_command = (
            "Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.CommandLine -and $_.CommandLine.ToLower().Contains('{script_name.lower()}') }} | "
            "Select-Object -ExpandProperty ProcessId"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
        if proc.returncode != 0:
            return []
        pids = []
        for line in proc.stdout.splitlines():
            try:
                pids.append(int(line.strip()))
            except ValueError:
                continue
        return [pid for pid in pids if pid != os.getpid()]

    proc = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        check=False,
    )
    if proc.returncode != 0:
        logging.warning("Unable to inspect running processes for %s", script_name)
        return []

    current_pid = os.getpid()
    pids = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid_text, command_line = parts
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == current_pid:
            continue
        if command_contains_script(command_line, script_name):
            pids.append(pid)
    return pids


def launch_script_terminal(job):
    env = build_child_env()
    title = f"quant {job.label}"

    if os.name == "nt":
        python_exe = sys.executable
        script_path = os.path.join(PROJECT_DIR, job.script_name)
        safe_title = title.replace("'", "''")
        full_cmd = (
            f"$Host.UI.RawUI.WindowTitle='{safe_title}';"
            f"$env:TZ='{SERVER_TIMEZONE}';"
            "$env:PYTHONUNBUFFERED='1';"
            "$env:PYTHONUTF8='1';"
            "$env:PYTHONIOENCODING='utf-8';"
            f"& '{python_exe}' -X utf8 -u '{script_path}'"
        )
        for arg in job.script_args:
            safe_arg = str(arg).replace("'", "''")
            full_cmd += f" '{safe_arg}'"
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        powershell_args = ["powershell", "-Command", full_cmd]
        if job.hold_on_exit:
            powershell_args = ["powershell", "-NoExit", "-Command", full_cmd]
        subprocess.Popen(
            powershell_args,
            cwd=PROJECT_DIR,
            creationflags=flags,
            close_fds=True,
        )
        return True

    shell_command = build_terminal_shell_command(
        job.script_name,
        script_args=job.script_args,
        hold_on_exit=job.hold_on_exit,
    )
    for command in terminal_command_candidates(title, shell_command):
        try:
            subprocess.Popen(
                command,
                cwd=PROJECT_DIR,
                env=env,
                close_fds=True,
                start_new_session=True,
            )
            return True
        except (FileNotFoundError, OSError):
            continue

    return False


def launch_script(job):
    existing_pids = running_script_pids(job.script_name)
    if existing_pids:
        logging.info(
            "Skip launch: %s already running as pid(s) %s",
            job.script_name,
            ", ".join(str(pid) for pid in existing_pids),
        )
        return

    logging.info("Launching %s: %s | %s", job.label, job.script_name, now_text())
    if job.open_terminal and launch_script_terminal(job):
        return

    output_log = script_output_log_path(job)
    with open(output_log, "a", encoding="utf-8") as log_file:
        log_file.write(f"\n[{now_text()}] starting {job.script_name}\n")
        subprocess.Popen(
            [sys.executable, "-X", "utf8", "-u", job.script_name, *map(str, job.script_args)],
            env=build_child_env(),
            cwd=PROJECT_DIR,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )


def stop_script(job):
    pids = running_script_pids(job.script_name)
    if not pids:
        logging.info("Stop skipped: %s is not running | %s", job.script_name, now_text())
        return

    logging.info(
        "Stopping %s: %s pid(s)=%s | %s",
        job.label,
        job.script_name,
        ", ".join(str(pid) for pid in pids),
        now_text(),
    )

    if os.name == "nt":
        for pid in pids:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        return

    current_pgid = os.getpgrp()
    signaled_groups = set()
    for pid in pids:
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError) as exc:
            logging.warning("Unable to inspect pid %s for %s: %s", pid, job.script_name, exc)
            continue

        try:
            if pgid > 0 and pgid != current_pgid and pgid not in signaled_groups:
                os.killpg(pgid, signal.SIGTERM)
                signaled_groups.add(pgid)
            else:
                os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as exc:
            logging.warning("Unable to stop pid %s for %s: %s", pid, job.script_name, exc)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not running_script_pids(job.script_name):
            return
        time.sleep(0.5)

    remaining_pids = running_script_pids(job.script_name)
    if not remaining_pids:
        return

    logging.warning(
        "Force stopping %s: %s pid(s)=%s | %s",
        job.label,
        job.script_name,
        ", ".join(str(pid) for pid in remaining_pids),
        now_text(),
    )
    for pid in remaining_pids:
        try:
            pgid = os.getpgid(pid)
            if pgid > 0 and pgid != current_pgid:
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError) as exc:
            logging.warning("Unable to force stop pid %s for %s: %s", pid, job.script_name, exc)


def job_fill_db():
    logging.info("Running fill_db job | %s", now_text())
    launch_script(FILL_DB_JOB)


def run_report(job):
    logging.info("Running report job %s | %s", job.label, now_text())
    launch_script(job)


def _set_event_loop_for_ib_insync():
    try:
        import asyncio

        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())
    except Exception:
        pass


def _session_cache_payload(sessions):
    rows = []
    for open_ny, close_ny in sessions:
        open_bj = open_ny.astimezone(DISPLAY_TZ)
        close_bj = close_ny.astimezone(DISPLAY_TZ)
        rows.append(
            {
                "open_ny": open_ny.isoformat(),
                "close_ny": close_ny.isoformat(),
                "open_bj": open_bj.isoformat(),
                "close_bj": close_bj.isoformat(),
            }
        )
    return {
        "source": "IB ContractDetails.liquidHours",
        "symbol": IB_CALENDAR_SYMBOL,
        "saved_at_bj": datetime.now(DISPLAY_TZ).isoformat(),
        "closed_days": CALENDAR_CACHE_METADATA.get("closed_days", []),
        "raw_part_count": CALENDAR_CACHE_METADATA.get("raw_part_count"),
        "first_date": CALENDAR_CACHE_METADATA.get("first_date"),
        "last_date": CALENDAR_CACHE_METADATA.get("last_date"),
        "sessions": rows,
    }


def save_calendar_cache(sessions):
    try:
        with open(CALENDAR_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_session_cache_payload(sessions), f, ensure_ascii=False, indent=2)
    except OSError as exc:
        logging.warning("Unable to write calendar cache %s: %s", CALENDAR_CACHE_FILE, exc)


def load_calendar_cache():
    try:
        with open(CALENDAR_CACHE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    now_bj = datetime.now(DISPLAY_TZ)
    sessions = []
    for row in payload.get("sessions", []):
        try:
            open_dt = datetime.fromisoformat(row["open_ny"])
            close_dt = datetime.fromisoformat(row["close_ny"])
        except (KeyError, TypeError, ValueError):
            continue
        if close_dt.astimezone(DISPLAY_TZ) > now_bj:
            sessions.append((open_dt, close_dt))
    sessions.sort(key=lambda x: x[0])
    if sessions:
        logging.info(
            "Loaded %s future market session(s) from cache %s",
            len(sessions),
            CALENDAR_CACHE_FILE,
        )
    return sessions


def fetch_ib_calendar_sessions():
    global CALENDAR_CACHE_METADATA

    _set_event_loop_for_ib_insync()

    from ib_insync import IB, Stock
    from program_factory.market_schedule import NY_TZ, parse_hours_string
    from program_factory.port_listener import choose_connection

    selection = choose_connection(preferred_order=(4001, 4002))
    ib = IB()
    try:
        ib.connect(IB_HOST, selection.port, clientId=IB_CLIENT_ID, timeout=8)
        contract = Stock(IB_CALENDAR_SYMBOL, "SMART", "USD")
        details = ib.reqContractDetails(contract)
        if not details:
            sessions = []
            CALENDAR_CACHE_METADATA = {}
        else:
            liquid_hours = getattr(details[0], "liquidHours", "") or ""
            parts = [part for part in liquid_hours.split(";") if part]
            dates = [part.split(":", 1)[0] for part in parts if ":" in part]
            CALENDAR_CACHE_METADATA = {
                "closed_days": [
                    {
                        "date_ny": part.split(":", 1)[0],
                        "label": part,
                    }
                    for part in parts
                    if "CLOSED" in part
                ],
                "raw_part_count": len(parts),
                "first_date": min(dates) if dates else None,
                "last_date": max(dates) if dates else None,
            }
            sessions = parse_hours_string(
                liquid_hours,
                now_ny=datetime.now(NY_TZ),
                days_ahead=IB_CALENDAR_DAYS,
                tz=NY_TZ,
            )
    finally:
        if ib.isConnected():
            ib.disconnect()

    logging.info(
        "Fetched %s US market session(s) from IB port %s account=%s symbol=%s",
        len(sessions),
        selection.port,
        selection.account,
        IB_CALENDAR_SYMBOL,
    )
    return sessions


def cancel_jobs(jobs):
    for job in list(jobs):
        try:
            schedule.cancel_job(job)
        except ValueError:
            pass
    jobs.clear()


def _record_static(job):
    STATIC_JOBS.append(job)
    return job


def _record_dynamic(job):
    try:
        job.tag(DYNAMIC_CALENDAR_TAG)
    except AttributeError:
        pass
    DYNAMIC_JOBS.append(job)
    return job


def _date_guarded_job(target_dt_bj, description, callback, *args):
    target_date = target_dt_bj.date()
    target_time = target_dt_bj.strftime("%H:%M")

    def runner():
        today = datetime.now(DISPLAY_TZ).date()
        if today < target_date:
            return None
        if today > target_date:
            logging.info("Cancel stale calendar job: %s target=%s", description, target_dt_bj)
            return schedule.CancelJob

        logging.info("Running calendar job: %s target=%s", description, target_dt_bj)
        callback(*args)
        return schedule.CancelJob

    job = schedule.every().day.at(target_time).do(runner)
    # Keep the absolute target on the schedule job.  ``schedule`` only stores
    # a time-of-day, which is not enough to distinguish a pending after-close
    # shutdown from a job that will be rebuilt for a future session.
    job.calendar_target_dt_bj = target_dt_bj
    job.calendar_description = description
    return _record_dynamic(job)


def _schedule_calendar_event(target_dt_bj, description, callback, *args):
    now_bj = datetime.now(DISPLAY_TZ)
    if target_dt_bj <= now_bj + timedelta(seconds=POLL_SECONDS):
        logging.info("Skip past calendar job: %s target=%s", description, target_dt_bj)
        return None

    job = _date_guarded_job(target_dt_bj, description, callback, *args)
    logging.info("Scheduled calendar job: %s at %s", description, target_dt_bj)
    return job


def _session_midnight_bj(close_bj):
    return datetime.combine(close_bj.date(), datetime_time(0, 0), tzinfo=DISPLAY_TZ)


def build_dynamic_jobs_from_sessions(sessions):
    now_bj = datetime.now(DISPLAY_TZ)
    future_sessions = [
        (open_ny, close_ny)
        for open_ny, close_ny in sessions
        if close_ny.astimezone(DISPLAY_TZ) > now_bj
    ]
    if not future_sessions:
        return []

    new_jobs = []
    previous_dynamic = list(DYNAMIC_JOBS)
    DYNAMIC_JOBS.clear()

    for open_ny, close_ny in future_sessions:
        open_bj = open_ny.astimezone(DISPLAY_TZ)
        close_bj = close_ny.astimezone(DISPLAY_TZ)
        session_label = f"{open_ny:%Y-%m-%d} RTH {open_ny:%H:%M}-{close_ny:%H:%M} NY"

        for offset_minutes, job in zip((-7, -6, -5), STARTUP_SEQUENCE):
            new_job = _schedule_calendar_event(
                open_bj + timedelta(minutes=offset_minutes),
                f"{session_label} startup {job.label}",
                launch_script,
                job,
            )
            if new_job:
                new_jobs.append(new_job)

        new_job = _schedule_calendar_event(
            open_bj + timedelta(minutes=1),
            f"{session_label} report open",
            run_report,
            OPEN_REPORT_JOB,
        )
        if new_job:
            new_jobs.append(new_job)

        midnight_bj = _session_midnight_bj(close_bj)
        if open_bj < midnight_bj < close_bj:
            new_job = _schedule_calendar_event(
                midnight_bj,
                f"{session_label} report midnight",
                run_report,
                MIDNIGHT_REPORT_JOB,
            )
            if new_job:
                new_jobs.append(new_job)

        new_job = _schedule_calendar_event(
            close_bj,
            f"{session_label} report close",
            run_report,
            CLOSE_REPORT_JOB,
        )
        if new_job:
            new_jobs.append(new_job)

        # Stop the real-time services in their configured post-close order.
        for offset_minutes, job in zip((2, 3, 4), SHUTDOWN_SEQUENCE):
            new_job = _schedule_calendar_event(
                close_bj + timedelta(minutes=offset_minutes),
                f"{session_label} shutdown {job.label}",
                stop_script,
                job,
            )
            if new_job:
                new_jobs.append(new_job)

        new_job = _schedule_calendar_event(
            close_bj + timedelta(minutes=61),
            f"{session_label} fill_db",
            job_fill_db,
        )
        if new_job:
            new_jobs.append(new_job)

        new_job = _schedule_calendar_event(
            close_bj,
            f"{session_label} refresh IB calendar",
            refresh_ib_calendar_cache,
        )
        if new_job:
            new_jobs.append(new_job)

    new_targets = {
        getattr(job, "calendar_target_dt_bj", None)
        for job in new_jobs
    }
    preserved_jobs = []
    for job in previous_dynamic:
        target_dt_bj = getattr(job, "calendar_target_dt_bj", None)
        if target_dt_bj and target_dt_bj > now_bj and target_dt_bj not in new_targets:
            preserved_jobs.append(job)
            logging.info(
                "Preserving pending calendar job during rebuild: %s target=%s",
                getattr(job, "calendar_description", "unknown"),
                target_dt_bj,
            )
            continue
        try:
            schedule.cancel_job(job)
        except ValueError:
            pass

    DYNAMIC_JOBS[:] = [*preserved_jobs, *new_jobs]
    logging.info(
        "Registered %s calendar job(s) for %s future US market session(s); preserved %s pending job(s)",
        len(DYNAMIC_JOBS),
        len(future_sessions),
        len(preserved_jobs),
    )
    return DYNAMIC_JOBS


def refresh_ib_calendar_jobs(allow_cache=True):
    try:
        sessions = fetch_ib_calendar_sessions()
        if sessions:
            save_calendar_cache(sessions)
            build_dynamic_jobs_from_sessions(sessions)
            cancel_jobs(STATIC_JOBS)
            return True
        logging.warning("IB returned no US market sessions")
    except Exception as exc:
        logging.warning("Unable to refresh IB US market calendar: %s", exc)

    if allow_cache:
        cached_sessions = load_calendar_cache()
        if cached_sessions:
            build_dynamic_jobs_from_sessions(cached_sessions)
            cancel_jobs(STATIC_JOBS)
            return True

    return False


def refresh_ib_calendar_cache():
    # A scheduler that starts after the pre-market window has passed uses the
    # static weekday fallback.  Its 04:00 calendar refresh used to cancel that
    # fallback before the 04:02--04:04 shutdown callbacks ran.  Promote those
    # still-pending one-off callbacks to date-guarded jobs first, so changing
    # from the fallback calendar to the IB calendar cannot skip post-close
    # cleanup for the current session.
    if STATIC_JOBS:
        _promote_pending_static_post_close_jobs()

    try:
        sessions = fetch_ib_calendar_sessions()
        if sessions:
            save_calendar_cache(sessions)
            build_dynamic_jobs_from_sessions(sessions)
            cancel_jobs(STATIC_JOBS)
            logging.info(
                "Refreshed IB US market calendar cache and rebuilt scheduled jobs after close"
            )
            return True
        logging.warning("IB returned no US market sessions for cache refresh")
    except Exception as exc:
        logging.warning("Unable to refresh IB US market calendar cache: %s", exc)
    return False


def _promote_pending_static_post_close_jobs():
    """Retain this session's post-close work while replacing static jobs.

    ``schedule`` static jobs recur weekly and therefore must be removed once
    an IB calendar is available.  At the 04:00 refresh, however, the current
    session's shutdown and fill-db callbacks have not happened yet.  Copying
    them to absolute, date-guarded jobs lets ``build_dynamic_jobs_from_sessions``
    preserve them while it removes the recurring fallback entries.
    """
    now_bj = datetime.now(DISPLAY_TZ)
    close_bj = datetime.combine(
        now_bj.date(),
        datetime_time.fromisoformat(CLOSE_REPORT_JOB.time_text),
        tzinfo=DISPLAY_TZ,
    )

    for offset_minutes, job in zip((2, 3, 4), SHUTDOWN_SEQUENCE):
        target_dt_bj = close_bj + timedelta(minutes=offset_minutes)
        _schedule_calendar_event(
            target_dt_bj,
            f"static fallback shutdown {job.label}",
            stop_script,
            job,
        )

    _schedule_calendar_event(
        close_bj + timedelta(minutes=61),
        "static fallback fill_db",
        job_fill_db,
    )


def register_weekly_jobs():
    cancel_jobs(STATIC_JOBS)
    for day in BEIJING_WEEKDAYS:
        for job in STARTUP_SEQUENCE:
            _record_static(getattr(schedule.every(), day).at(job.time_text).do(launch_script, job=job))
            logging.info("Scheduled %s startup on %s at %s CST", job.label, day, job.time_text)
        _record_static(
            getattr(schedule.every(), day).at(OPEN_REPORT_JOB.time_text).do(
                run_report,
                job=OPEN_REPORT_JOB,
            )
        )
        logging.info(
            "Scheduled %s on %s at %s CST",
            OPEN_REPORT_JOB.label,
            day,
            OPEN_REPORT_JOB.time_text,
        )
        _record_static(
            getattr(schedule.every(), day).at(MIDNIGHT_REPORT_JOB.time_text).do(
                run_report,
                job=MIDNIGHT_REPORT_JOB,
            )
        )
        logging.info(
            "Scheduled %s on %s at %s CST",
            MIDNIGHT_REPORT_JOB.label,
            day,
            MIDNIGHT_REPORT_JOB.time_text,
        )

    for day in BEIJING_POST_CLOSE_DAYS:
        _record_static(
            getattr(schedule.every(), day).at(CLOSE_REPORT_JOB.time_text).do(
                run_report,
                job=CLOSE_REPORT_JOB,
            )
        )
        logging.info(
            "Scheduled %s on %s at %s CST",
            CLOSE_REPORT_JOB.label,
            day,
            CLOSE_REPORT_JOB.time_text,
        )
        _record_static(getattr(schedule.every(), day).at(CLOSE_REPORT_JOB.time_text).do(refresh_ib_calendar_cache))
        logging.info("Scheduled IB calendar cache refresh on %s at %s CST", day, CLOSE_REPORT_JOB.time_text)
        for job in SHUTDOWN_SEQUENCE:
            _record_static(getattr(schedule.every(), day).at(job.time_text).do(stop_script, job=job))
            logging.info("Scheduled %s shutdown on %s at %s CST", job.label, day, job.time_text)
        _record_static(getattr(schedule.every(), day).at(FILL_DB_JOB.time_text).do(job_fill_db))
        logging.info("Scheduled %s on %s at %s CST", FILL_DB_JOB.label, day, FILL_DB_JOB.time_text)


def main():
    configure_runtime()
    # The public read-only dashboard is a server service, not a GUI feature.
    # Its process reconnects SSH by itself, while this launch makes sure it is
    # present after every scheduler restart without tying availability to GUI.
    launch_script(PUBLIC_DASHBOARD_TUNNEL_JOB)
    cached_sessions = load_calendar_cache()
    if cached_sessions:
        build_dynamic_jobs_from_sessions(cached_sessions)
        logging.info("Scheduler using cached IB US market calendar")
    else:
        register_weekly_jobs()
        logging.warning("Scheduler using static weekday fallback calendar; no IB query at startup")

    logging.info("Scheduler started | timezone=%s | project=%s", SERVER_TIMEZONE, PROJECT_DIR)

    while True:
        schedule.run_pending()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
