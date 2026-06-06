#!/usr/bin/env python3
"""Quark process manager: REST API + CDP proxy + idle monitor.

Hosts three things in a single asyncio event loop:
  1. FastAPI/uvicorn on QUARK_API_PORT for control + OpenAPI.
  2. The CDP proxy (9223 → 9222) imported from cdp_proxy. Any traffic on it
     counts as user activity and resets the idle timer.
  3. A periodic idle monitor that:
       - samples the Quark process tree's CPU usage (psutil)
       - sleeps until the two-stage thresholds (minimize, then stop) elapse
       - uses xdotool to minimize the window first, kill the process group second

State machine: STOPPED ⇄ RUNNING_VISIBLE ⇄ RUNNING_MINIMIZED.

Env configuration (see plan.md for details):
  QUARK_API_PORT, QUARK_AUTOSTART,
  QUARK_IDLE_MINIMIZE_TIMEOUT, QUARK_IDLE_STOP_TIMEOUT, QUARK_IDLE_CHECK_INTERVAL,
  QUARK_CPU_BUSY_THRESHOLD, QUARK_RESTORE_ON_CDP,
  CDP_PROXY_PORT (9223), QUARK_CDP_PORT (9222).
"""
import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from enum import Enum
from typing import Optional

import psutil
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Local module: the CDP proxy library refactored from cdp-proxy.py.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp_proxy  # noqa: E402

# ── Configuration ──────────────────────────────────────────────────────────────

def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


CONFIG = {
    "api_port":          _env_int("QUARK_API_PORT", 8080),
    "autostart":         _env_bool("QUARK_AUTOSTART", True),
    "minimize_timeout":  _env_int("QUARK_IDLE_MINIMIZE_TIMEOUT", 300),
    "stop_timeout":      _env_int("QUARK_IDLE_STOP_TIMEOUT", 1800),
    "check_interval":    _env_int("QUARK_IDLE_CHECK_INTERVAL", 10),
    "cpu_busy_threshold": _env_float("QUARK_CPU_BUSY_THRESHOLD", 15.0),
    "restore_on_cdp":    _env_bool("QUARK_RESTORE_ON_CDP", True),
    "cdp_proxy_port":    _env_int("CDP_PROXY_PORT", 9223),
    "quark_cdp_port":    _env_int("QUARK_CDP_PORT", 9222),
}

WINE_USER = os.environ.get("WINE_USER", "wineuser")
LAUNCH_SCRIPT = "/usr/local/bin/launch-quark.sh"
WINESERVER_BIN = os.environ.get(
    "WINESERVER_BIN", "/opt/deepin-wine8-stable/bin/wineserver"
)

log = logging.getLogger("quark-manager")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)


# ── State machine ──────────────────────────────────────────────────────────────

class State(str, Enum):
    STOPPED = "stopped"
    RUNNING_VISIBLE = "running_visible"
    RUNNING_MINIMIZED = "running_minimized"


# Lock guards every Supervisor field. Async-friendly: we never hold it across
# an `await`, only across short synchronous mutations and `psutil` calls.
_lock = threading.RLock()

# Activity timestamps. Idle = now - max(last_cdp_activity, last_cpu_busy).
_last_cdp_activity: float = 0.0
_last_cpu_busy: float = 0.0
# Suppresses the (legitimate) low CPU we get right after minimizing so the
# idle timer doesn't keep ticking from the previous high-CPU timestamp.
_pending_minimize_at: Optional[float] = None

state: State = State.STOPPED
proc: Optional[subprocess.Popen] = None
pgid: Optional[int] = None
started_at: Optional[float] = None

minimize_count = 0
stop_count = 0
start_count = 0


def _now() -> float:
    return time.monotonic()


def mark_activity():
    """Reset the idle clock from CDP proxy."""
    global _last_cdp_activity
    with _lock:
        _last_cdp_activity = _now()


def _busy_ts() -> float:
    with _lock:
        return max(_last_cdp_activity, _last_cpu_busy)


# ── Process control ────────────────────────────────────────────────────────────

def _resolve_pgid(proc_obj: subprocess.Popen) -> Optional[int]:
    """Return the OS process group id of the launched wine process."""
    try:
        return os.getpgid(proc_obj.pid)
    except (ProcessLookupError, PermissionError):
        return None


def _tree_cpu(pgid_value: int) -> float:
    """Sum cpu_percent across every process in the given process group."""
    total = 0.0
    try:
        # First, prime psutil so the next call returns deltas, not zero.
        psutil.cpu_percent(interval=None)
    except Exception:
        pass
    for p in psutil.process_iter(attrs=["pgid", "cpu_percent"]):
        try:
            if p.info.get("pgid") == pgid_value:
                total += float(p.info.get("cpu_percent") or 0.0)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return total


def _is_quark_process_alive() -> bool:
    with _lock:
        return proc is not None and proc.poll() is None


def start_quark() -> dict:
    """Idempotently start Quark. Returns a status dict."""
    global state, proc, pgid, started_at, start_count
    with _lock:
        if _is_quark_process_alive():
            if state == State.RUNNING_MINIMIZED:
                restore_window()
            return current_status()

        env = os.environ.copy()
        # Launch as wineuser in a fresh session (process group).
        # We exec launch-quark.sh; it does the wineserver -k and the actual
        # wine invocation. The shell here is just to give us a controllable
        # process to kill later.
        cmd = [LAUNCH_SCRIPT]
        if os.geteuid() == 0:
            cmd = ["su", "-s", "/bin/bash", WINE_USER, "-c", "exec " + LAUNCH_SCRIPT]
        try:
            proc = subprocess.Popen(
                cmd,
                env=env,
                start_new_session=True,  # new pgid
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
        except FileNotFoundError as e:
            raise HTTPException(status_code=500, detail=f"launch-quark failed: {e}")

        # Give the OS a moment to record the new pgid, then capture it.
        # We retry briefly because on busy systems the kernel may not have
        # registered the new session leader immediately.
        pgid = None
        for _ in range(20):
            pgid = _resolve_pgid(proc)
            if pgid is not None:
                break
            time.sleep(0.05)
        started_at = _now()
        start_count += 1
        state = State.RUNNING_VISIBLE
        log.info("Quark started: pid=%s pgid=%s", proc.pid, pgid)
        return current_status()


def stop_quark() -> dict:
    """Stop Quark and its entire process group. Idempotent."""
    global state, proc, pgid, started_at, stop_count
    with _lock:
        if proc is None:
            return current_status()
        target_pgid = pgid or os.getpgid(proc.pid)
        target_pid = proc.pid

    log.info("Stopping Quark: pid=%s pgid=%s", target_pid, target_pgid)
    try:
        os.killpg(target_pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    # Give it a moment, then SIGKILL the survivors.
    deadline = time.time() + 5
    while time.time() < deadline:
        if not _pgid_alive(target_pgid):
            break
        time.sleep(0.2)
    try:
        os.killpg(target_pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    # wineserver -k as a final safety net (root can run it).
    try:
        subprocess.run(
            [WINESERVER_BIN, "-k"],
            timeout=5, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    with _lock:
        state = State.STOPPED
        proc = None
        pgid = None
        started_at = None
        stop_count += 1
    log.info("Quark stopped")
    return current_status()


def _pgid_alive(gid: int) -> bool:
    try:
        os.killpg(gid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def minimize_window() -> dict:
    """Minimize the Quark window via xdotool. Idempotent."""
    global state, minimize_count
    with _lock:
        if not _is_quark_process_alive():
            raise HTTPException(status_code=409, detail="Quark is not running")
        if state == State.RUNNING_MINIMIZED:
            return current_status()
    # xdotool is non-blocking; a small timeout is plenty.
    r = subprocess.run(
        ["xdotool", "search", "--name", "夸克网盘", "windowminimize", "%@"],
        capture_output=True, timeout=5,
    )
    # Fall back to matching the Chromium window class if the title doesn't match.
    if r.returncode != 0 or not r.stdout:
        subprocess.run(
            ["xdotool", "search", "--class", "Wine", "windowminimize", "%@"],
            capture_output=True, timeout=5,
        )
    with _lock:
        state = State.RUNNING_MINIMIZED
        minimize_count += 1
    log.info("Quark window minimized")
    return current_status()


def restore_window() -> dict:
    """Restore the Quark window. Idempotent."""
    global state
    with _lock:
        if not _is_quark_process_alive():
            raise HTTPException(status_code=409, detail="Quark is not running")
        if state == State.RUNNING_VISIBLE:
            return current_status()
    r = subprocess.run(
        ["xdotool", "search", "--name", "夸克网盘", "windowactivate", "%@"],
        capture_output=True, timeout=5,
    )
    if r.returncode != 0 or not r.stdout:
        subprocess.run(
            ["xdotool", "search", "--class", "Wine", "windowactivate", "%@"],
            capture_output=True, timeout=5,
        )
    with _lock:
        state = State.RUNNING_VISIBLE
    log.info("Quark window restored")
    return current_status()


def current_status() -> dict:
    with _lock:
        running = _is_quark_process_alive()
        pid = proc.pid if (proc is not None) else None
        uptime = (_now() - started_at) if (started_at is not None and running) else 0.0
        cpu = 0.0
        if running and pgid is not None:
            try:
                cpu = _tree_cpu(pgid)
            except Exception:
                cpu = 0.0
        idle = _now() - _busy_ts()
        minimize_in = (
            max(0, CONFIG["minimize_timeout"] - int(idle))
            if (CONFIG["minimize_timeout"] > 0
                and state == State.RUNNING_VISIBLE
                and running)
            else None
        )
        stop_in = (
            max(0, CONFIG["stop_timeout"] - int(idle))
            if (CONFIG["stop_timeout"] > 0
                and state in (State.RUNNING_VISIBLE, State.RUNNING_MINIMIZED)
                and running)
            else None
        )
        return {
            "state": state.value,
            "pid": pid,
            "uptime_s": round(uptime, 1),
            "cpu_percent": round(cpu, 1),
            "last_cdp_activity_s_ago": (
                None if _last_cdp_activity == 0.0 else round(_now() - _last_cdp_activity, 1)
            ),
            "last_cpu_busy_s_ago": (
                None if _last_cpu_busy == 0.0 else round(_now() - _last_cpu_busy, 1)
            ),
            "idle_seconds": round(idle, 1),
            "minimize_timeout": CONFIG["minimize_timeout"],
            "stop_timeout": CONFIG["stop_timeout"],
            "busy_threshold": CONFIG["cpu_busy_threshold"],
            "restore_on_cdp": CONFIG["restore_on_cdp"],
            "minimizes_in_s": minimize_in,
            "stops_in_s": stop_in,
            "start_count": start_count,
            "stop_count": stop_count,
            "minimize_count": minimize_count,
        }


# ── Idle monitor (background task) ────────────────────────────────────────────

async def idle_monitor():
    """Periodically sample CPU, advance state per the two-stage idle policy."""
    global _last_cpu_busy
    interval = CONFIG["check_interval"]
    threshold = CONFIG["cpu_busy_threshold"]
    while True:
        await asyncio.sleep(interval)
        with _lock:
            running = _is_quark_process_alive()
            cur_pgid = pgid
            cur_state = state
        if not running or cur_pgid is None:
            continue
        # CPU sampling is synchronous and can take a moment; offload the thread.
        try:
            cpu = await asyncio.get_event_loop().run_in_executor(
                None, _tree_cpu, cur_pgid
            )
        except Exception as e:
            log.debug("CPU sample failed: %s", e)
            continue
        with _lock:
            if cpu >= threshold:
                _last_cpu_busy = _now()
        idle_for = _now() - _busy_ts()

        # CDP wake-up: if user just sent CDP traffic to a minimized window,
        # restore it so the browser doesn't fight throttling.
        if (CONFIG["restore_on_cdp"]
                and cur_state == State.RUNNING_MINIMIZED
                and _last_cdp_activity > _last_cpu_busy):
            try:
                restore_window()
            except Exception as e:
                log.warning("auto-restore failed: %s", e)

        # Stage 1: minimize.
        if (cur_state == State.RUNNING_VISIBLE
                and CONFIG["minimize_timeout"] > 0
                and idle_for >= CONFIG["minimize_timeout"]):
            log.info("Idle ≥ %ss, minimizing window", CONFIG["minimize_timeout"])
            try:
                minimize_window()
            except Exception as e:
                log.warning("minimize failed: %s", e)
            continue

        # Stage 2: stop.
        if (cur_state in (State.RUNNING_VISIBLE, State.RUNNING_MINIMIZED)
                and CONFIG["stop_timeout"] > 0
                and idle_for >= CONFIG["stop_timeout"]):
            log.info("Idle ≥ %ss, stopping process", CONFIG["stop_timeout"])
            try:
                stop_quark()
            except Exception as e:
                log.warning("stop failed: %s", e)


# ── FastAPI app ───────────────────────────────────────────────────────────────

class ActionResult(BaseModel):
    ok: bool = True
    status: dict


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bring up the CDP proxy in-process and start the idle monitor.
    proxy_server = await cdp_proxy.run_server(
        proxy_port=CONFIG["cdp_proxy_port"],
        quark_port=CONFIG["quark_cdp_port"],
        on_activity=mark_activity,
    )
    monitor_task = asyncio.create_task(idle_monitor())
    if CONFIG["autostart"]:
        try:
            start_quark()
        except Exception as e:
            log.error("autostart failed: %s", e)
    log.info("Manager ready on :%d", CONFIG["api_port"])
    try:
        yield
    finally:
        monitor_task.cancel()
        proxy_server.close()
        try:
            await monitor_task
        except (asyncio.CancelledError, Exception):
            pass
        # Best-effort shutdown of Quark on container exit.
        try:
            stop_quark()
        except Exception:
            pass


app = FastAPI(
    title="quark-docker Manager",
    version="1.0.0",
    description=(
        "Control API for the Wine/Electron Quark Cloud Drive instance running "
        "inside this container. Exposes process lifecycle, window state, "
        "two-stage idle policy, and an OpenAPI spec at /openapi.json."
    ),
    lifespan=lifespan,
)


@app.get("/healthz", tags=["meta"])
def healthz():
    """Liveness probe: returns 200 as long as the manager process is alive."""
    return {"ok": True}

@app.get("/status", response_model=dict, tags=["control"])
def get_status():
    """Snapshot of the current process state, CPU, idle timers, and counts."""
    return current_status()


@app.post("/start", response_model=ActionResult, tags=["control"])
def post_start():
    """Start Quark (or restore it from minimized). Idempotent."""
    s = start_quark()
    return {"ok": True, "status": s}


@app.post("/stop", response_model=ActionResult, tags=["control"])
def post_stop():
    """Stop Quark and free its process group. Idempotent."""
    s = stop_quark()
    return {"ok": True, "status": s}


@app.post("/restart", response_model=ActionResult, tags=["control"])
def post_restart():
    """Stop then start. Useful after config changes that need a cold start."""
    stop_quark()
    s = start_quark()
    return {"ok": True, "status": s}


@app.post("/minimize", response_model=ActionResult, tags=["window"])
def post_minimize():
    """Minimize the Quark window (keeps the process alive, throttles CPU)."""
    s = minimize_window()
    return {"ok": True, "status": s}


@app.post("/restore", response_model=ActionResult, tags=["window"])
def post_restore():
    """Restore the minimized Quark window."""
    s = restore_window()
    return {"ok": True, "status": s}


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main():
    cfg = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=CONFIG["api_port"],
        log_level=os.environ.get("UVICORN_LOG", "info"),
        access_log=False,
    )
    server = uvicorn.Server(cfg)
    server.run()


if __name__ == "__main__":
    main()
