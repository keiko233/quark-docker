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
    """Legacy proc-tracking check. UNRELIABLE under the spark runtime: the
    `wine start.exe /Unix LNK` launcher exits almost immediately after
    handing off to Chromium (Chromium itself lives on as a wineserver child,
    in a different pgid). Don't use this for "is Quark serving" decisions —
    use `_is_quark_alive()` instead. Kept around as the fast path inside
    `_is_quark_alive()` and for places that genuinely care about the
    wrapper's exit status."""
    with _lock:
        return proc is not None and proc.poll() is None


def _chromium_listening() -> bool:
    """Authoritative liveness check: is Chromium actually accepting CDP
    connections on 127.0.0.1:<quark_cdp_port>?

    A 250 ms TCP connect is fast enough to call on every API request, and
    it's the only signal that survives the spark runtime's early-exit
    launcher (where `proc.poll()` returns non-None while Chromium is happily
    serving). Failure modes (connection refused, timeout) all return False —
    the caller treats that as "not alive"."""
    import socket
    port = CONFIG["quark_cdp_port"]
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=0.25)
        s.close()
        return True
    except OSError:
        return False


def _is_quark_alive() -> bool:
    """True when Quark is actually serving CDP. Fast path: trust proc when
    its wrapper is still up (cheapest answer). Slow path: TCP-probe the CDP
    port (authoritative when the wrapper has exited but Chromium hasn't).

    Use THIS predicate for every user-facing "is Quark up" check — start
    short-circuit, minimize/restore guard, status reporting, idle monitor.
    Reserve `_is_quark_process_alive()` for the rare case where you actually
    care about the launcher process itself (currently: nowhere)."""
    if _is_quark_process_alive():
        return True
    return _chromium_listening()


def start_quark() -> dict:
    """Idempotently start Quark. Returns a status dict.

    Three branches:
      1. Wrapper still alive (`_is_quark_process_alive`) — no-op restore
         if currently MINIMIZED, otherwise return current status.
      2. Wrapper gone but Chromium serving (spark runtime) — re-adopt:
         scan psutil for the Quark main process, re-attach `pgid`, fix
         state. This is the case that follows a `/restart` whose stop
         half succeeded at setting state=STOPPED but didn't actually
         take down Chromium (see stop_quark comment on spark reparenting).
      3. Nothing alive — spawn a fresh launch-quark.sh as wineuser.
    """
    global state, proc, pgid, started_at, start_count
    # Probe Chromium OUTSIDE the lock — TCP connect is slow enough that
    # holding the lock would serialise every concurrent /start call.
    if _is_quark_alive():
        with _lock:
            if _is_quark_process_alive():
                if state == State.RUNNING_MINIMIZED:
                    restore_window()
                return current_status()
            # Branch 2: spark-runtime re-adoption. Quark is up, but our
            # wrapper handle is gone and `pgid` is stale. Find the main
            # Quark process so subsequent stop_quark can signal its group.
            found = _find_quark_main_pid()
            if found is not None:
                pid, new_pgid = found
                log.info(
                    "Re-adopting spark-runtime Quark: pid=%s pgid=%s "
                    "(was proc=%s pgid=%s state=%s)",
                    pid, new_pgid,
                    getattr(proc, "pid", None), pgid, state.value,
                )
                proc = None  # wrapper is gone; we operate by pgid from now on
                pgid = new_pgid
                # If we previously believed Quark was stopped (e.g. a
                # half-failed /restart set state=STOPPED while leaving
                # Chromium up), promote to RUNNING_VISIBLE.
                if state != State.RUNNING_MINIMIZED:
                    state = State.RUNNING_VISIBLE
                return current_status()
        # If we get here, alive=True but we couldn't find the process.
        # Fall through to a fresh start (and trust the launch script
        # to clean up the orphans).

    with _lock:
        env = os.environ.copy()
        # Launch as wineuser in a fresh session (process group).
        # We exec launch-quark.sh; it does the wineserver -k and the actual
        # wine invocation. The shell here is just to give us a controllable
        # process to kill later — though under the spark runtime it exits
        # quickly (see `_is_quark_alive()` for how we cope with that).
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
    """Stop Quark. Idempotent. Spark-runtime safe.

    Two-stage kill:
      1. Process-group SIGTERM/SIGKILL (works for the standard runtime
         where Quark stays in the wrapper's pgid).
      2. By-name SIGTERM/SIGKILL (catches the spark-runtime case where
         the wrapper exited early and Quark got reparented to init, so
         killpg hits an empty group).

    Then `wineserver -k` (as wineuser — see _wineserver_k) to make sure
    no orphan wineserver sticks around to respawn Quark.
    """
    global state, proc, pgid, started_at, stop_count
    with _lock:
        target_pgid = pgid
        if target_pgid is None and proc is not None:
            try:
                target_pgid = os.getpgid(proc.pid)
            except (ProcessLookupError, PermissionError):
                target_pgid = None
        target_pid = proc.pid if proc is not None else None

    # If we have no tracked handle at all, see if there's an orphan
    # Quark left over from a previous (failed) stop and adopt its pgid
    # so the kill reaches it.
    if target_pgid is None:
        found = _find_quark_main_pid()
        if found is not None:
            target_pgid = found[1]
            log.info("Adopting orphan Quark pgid=%s for stop", target_pgid)

    log.info("Stopping Quark: pid=%s pgid=%s", target_pid, target_pgid)

    # Stage 1: process-group kill (standard runtime).
    if target_pgid is not None:
        try:
            os.killpg(target_pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    # Stage 2: by-name kill (spark runtime). This catches everything that
    # got reparented to init, plus the wineserver itself.
    _kill_quark_processes(signal.SIGTERM)

    # Wait up to 5 s for everything to drain.
    deadline = time.time() + 5
    while time.time() < deadline:
        if not _quark_processes_alive():
            break
        if target_pgid is not None and not _pgid_alive(target_pgid):
            # PGID drained, but the by-name scan may still show survivors
            # (siblings from before pgid was set). Keep waiting until
            # they're all gone or we hit the deadline.
            time.sleep(0.1)
            continue
        time.sleep(0.2)

    # SIGKILL anything still alive.
    if target_pgid is not None:
        try:
            os.killpg(target_pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    _kill_quark_processes(signal.SIGKILL)

    # Final safety net: kill the wineserver so it can't respawn us. This
    # runs LAST so it doesn't kill the wineserver while clients are
    # still using it to die gracefully.
    _wineserver_k()

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


# Patterns for Quark/Wine user-mode processes. We match on the running
# process name (case-insensitive). NB: under Wine, the QuarkCloudDrive.exe
# binary runs as Chromium's "CrBrowserMain" process — Wine translates the
# Windows process names. The `--type=` sub-processes (gpu / utility /
# renderer) show up as `CrGpuMain` / `CrUtilityMain` / `CrRendererMain`.
# So the binary path "QuarkCloudDrive.exe" never appears in `psutil.name`;
# we have to use the Chromium names instead. This works for both the
# standard and spark runtimes; under spark the wrapper exits early and
# these processes get reparented to init, so `os.killpg(pgid, ...)`
# becomes a no-op and we need the by-name kill to reach them.
_QUARK_PROCESS_PATTERNS = (
    "crbrowsermain",     # Chromium main = QuarkCloudDrive.exe in Wine
    "crgpumain",
    "crutilitymain",
    "crrenderermain",
    "winedevice",
    "winemenubuilder",
    "services.exe",
    "plugplay",
    "svchost.exe",
    "explorer.exe",
    "rpcss.exe",
    "rundll32",
    "iexplore",
    "wineboot",
    "wineserver",
)


def _cmdline_mentions_quark(cmdline: list[str]) -> bool:
    """True if the process cmdline includes 'quark' (case-insensitive).
    Used as a defence-in-depth filter so we don't accidentally match a
    non-Quark Chromium process if another Wine app ever shares the
    container."""
    if not cmdline:
        return False
    return any("quark" in (arg or "").lower() for arg in cmdline)


def _find_quark_main_pid() -> Optional[tuple]:
    """Locate the QuarkCloudDrive.exe MAIN process (skipping --type= renderer
    / utility / gpu sub-processes). Returns (pid, pgid) or None.

    Used for spark-runtime re-adoption: when the wrapper (`wine start.exe
    /Unix LNK`) exits early, the Quark processes get reparented to init
    and our tracked `pgid` becomes stale. This scan lets us re-attach.

    NB: psutil's `as_dict(attrs=...)` doesn't accept `pgid` as an
    attribute name — we call `os.getpgid(p.pid)` instead."""
    for p in psutil.process_iter(attrs=["pid", "name"]):
        try:
            cmdline = " ".join(p.cmdline() or []).lower()
            if "quarkclouddrive" in cmdline and "--type=" not in cmdline:
                try:
                    pgid = os.getpgid(p.pid)
                except (ProcessLookupError, PermissionError):
                    continue
                return p.pid, pgid
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def _quark_processes_alive() -> bool:
    """True if any Quark/Wine user process (including wineserver) is still
    alive. Used by stop_quark to know when it's actually safe to declare
    Quark stopped."""
    for p in psutil.process_iter(attrs=["pid", "name"]):
        try:
            name = (p.info.get("name") or "").lower()
            if any(pat in name for pat in _QUARK_PROCESS_PATTERNS):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def _kill_quark_processes(sig: int) -> None:
    """Send `sig` to every Quark/Wine user process. Best-effort; swallows
    per-process errors. Skips ourselves and PID 1 (init).

    Matches by either:
      1. Process name in `_QUARK_PROCESS_PATTERNS` (covers Wine's name
         translation: `CrBrowserMain` for the Quark binary), OR
      2. Cmdline mentioning "quark" (catches the binary path
         `QuarkCloudDrive.exe` even when the proc name is something
         like `node` for the asar server, or just the basename
         `QuarkCloudDrive` for the launcher wrapper).

    Defence in depth: we require BOTH a name match AND a cmdline match
    to kill, except for plain Wine helpers (wineserver, services.exe,
    etc.) where the name match alone is enough (no other app in the
    container would be running them)."""
    my_pid = os.getpid()
    # Names that uniquely identify a Wine/Quark helper — name match is
    # enough, no cmdline check needed.
    _NAME_ONLY = {
        "wineserver", "winedevice", "winemenubuilder", "services.exe",
        "plugplay", "svchost.exe", "explorer.exe", "rpcss.exe",
        "rundll32", "iexplore", "wineboot",
    }
    for p in psutil.process_iter(attrs=["pid", "name"]):
        try:
            if p.pid in (my_pid, 1):
                continue
            name = (p.info.get("name") or "").lower()
            name_match = any(pat in name for pat in _QUARK_PROCESS_PATTERNS)
            if not name_match:
                continue
            # For Chromium-derived names (CrBrowserMain, CrGpuMain, etc.)
            # we also require the cmdline to mention quark — protects
            # against an unrelated Chromium app sharing the container.
            if name not in _NAME_ONLY:
                try:
                    cmdline = p.cmdline() or []
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                if not _cmdline_mentions_quark(cmdline):
                    continue
            try:
                os.kill(p.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def _wineserver_k() -> None:
    """`wineserver -k` for the WINEPREFIX this manager owns. The manager
    runs as root (it spawns wine via `su wineuser`), but `wineserver -k`
    refuses to operate on a prefix it doesn't own
    (`wineserver: <prefix> is not owned by you`). Run it as wineuser."""
    if os.geteuid() == 0:
        cmd = ["su", "-s", "/bin/bash", WINE_USER, "-c",
               f"{WINESERVER_BIN} -k"]
    else:
        cmd = [WINESERVER_BIN, "-k"]
    try:
        subprocess.run(
            cmd, timeout=10, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def _find_quark_windows() -> list[str]:
    """Return hex window IDs of the Quark main window(s) by title.

    Quark's main window title is `夸克网盘` (Quark Cloud Drive). Returns a
    list of hex IDs suitable for `xdotool windowunmap/windowmap <id>`.
    Empty list means the title didn't match — we fail loud in that case
    rather than risk the broad `--class Wine` fallback (which would also
    hit `Default IME`, `Wine System Tray`, `收银台`, `PDF转换器`, etc.).
    """
    r = subprocess.run(
        ["xdotool", "search", "--name", "夸克网盘"],
        capture_output=True, timeout=5, text=True,
    )
    if r.returncode != 0:
        log.warning("xdotool search failed: rc=%s stderr=%s",
                    r.returncode, r.stderr.strip())
        return []
    return [w for w in r.stdout.split() if w]


def minimize_window() -> dict:
    """Minimize the Quark window by unmapping it from the X server.

    We bypass xdotool's `windowminimize`, which sends WM_CHANGE_STATE and
    relies on a window manager to honor it. This container runs headless
    Xorg with no WM (no metacity/openbox/xfwm4 is started), so
    `windowminimize` silently no-ops and the window stays visible in VNC
    even though the call returns 0. The direct X11 `XUnmapWindow` (xdotool's
    `windowunmap`) actually hides the window while keeping the process
    alive for CDP traffic.
    """
    global state, minimize_count
    if not _is_quark_alive():
        raise HTTPException(status_code=409, detail="Quark is not running")
    with _lock:
        if state == State.RUNNING_MINIMIZED:
            return current_status()
    wids = _find_quark_windows()
    if not wids:
        raise HTTPException(
            status_code=500,
            detail="Quark window not found (title '夸克网盘' missing). "
                   "Refusing to fall back to a broad Wine-class match "
                   "that would also minimize IME / tray / dialog windows.",
        )
    for wid in wids:
        r = subprocess.run(
            ["xdotool", "windowunmap", wid],
            capture_output=True, timeout=5,
        )
        if r.returncode != 0:
            log.warning("windowunmap %s failed: rc=%s stderr=%s",
                        wid, r.returncode,
                        r.stderr[:200].decode(errors="replace"))
    with _lock:
        state = State.RUNNING_MINIMIZED
        minimize_count += 1
    log.info("Quark window minimized (unmapped %d window(s))", len(wids))
    return current_status()


def restore_window() -> dict:
    """Restore the Quark window by mapping it back onto the X server.

    Pairs with `minimize_window`'s `windowunmap` — we use the direct X11
    `XMapWindow` (xdotool's `windowmap`) for the same reason: there is no
    window manager in this container, so `windowactivate` (which tries to
    raise/raise-and-focus via EWMH `_NET_ACTIVE_WINDOW`) is a no-op.
    """
    global state
    if not _is_quark_alive():
        raise HTTPException(status_code=409, detail="Quark is not running")
    with _lock:
        if state == State.RUNNING_VISIBLE:
            return current_status()
    wids = _find_quark_windows()
    if not wids:
        raise HTTPException(
            status_code=500,
            detail="Quark window not found (title '夸克网盘' missing)",
        )
    for wid in wids:
        r = subprocess.run(
            ["xdotool", "windowmap", wid],
            capture_output=True, timeout=5,
        )
        if r.returncode != 0:
            log.warning("windowmap %s failed: rc=%s stderr=%s",
                        wid, r.returncode,
                        r.stderr[:200].decode(errors="replace"))
    with _lock:
        state = State.RUNNING_VISIBLE
    log.info("Quark window restored (mapped %d window(s))", len(wids))
    return current_status()


def current_status() -> dict:
    # Probe outside the lock — see _is_quark_alive() comment on TCP cost.
    alive = _is_quark_alive()
    with _lock:
        # Self-heal stale state in BOTH directions:
        # - alive=False + state=RUNNING_*: demote to STOPPED (Chromium
        #   crashed and we didn't notice).
        # - alive=True + state=STOPPED: cold-start races (spark runtime
        #   wrapper exits before 9222 is up; the idle monitor demoted us
        #   while 9222 was still closed) leave us stale. We can't
        #   re-adopt here without taking the lock for the psutil scan
        #   (slow), so we just promote the *reported* state to
        #   RUNNING_VISIBLE; the next /start or idle-monitor cycle
        #   re-attaches pgid for real.
        if alive and state == State.STOPPED:
            reported_state = State.RUNNING_VISIBLE
        elif not alive and state != State.STOPPED:
            reported_state = State.STOPPED
        else:
            reported_state = state
        pid = proc.pid if (proc is not None) else None
        uptime = (_now() - started_at) if (started_at is not None and alive) else 0.0
        cpu = 0.0
        if alive and pgid is not None:
            try:
                cpu = _tree_cpu(pgid)
            except Exception:
                cpu = 0.0
        idle = _now() - _busy_ts()
        minimize_in = (
            max(0, CONFIG["minimize_timeout"] - int(idle))
            if (CONFIG["minimize_timeout"] > 0
                and reported_state == State.RUNNING_VISIBLE
                and alive)
            else None
        )
        stop_in = (
            max(0, CONFIG["stop_timeout"] - int(idle))
            if (CONFIG["stop_timeout"] > 0
                and reported_state in (State.RUNNING_VISIBLE, State.RUNNING_MINIMIZED)
                and alive)
            else None
        )
        return {
            "state": reported_state.value,
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
    global _last_cpu_busy, state, proc, pgid, started_at
    interval = CONFIG["check_interval"]
    threshold = CONFIG["cpu_busy_threshold"]
    while True:
        await asyncio.sleep(interval)
        # CDP probe is the authoritative aliveness check; do it outside the
        # lock so a slow probe doesn't block API calls.
        alive = await asyncio.get_event_loop().run_in_executor(None, _is_quark_alive)
        with _lock:
            cur_pgid = pgid
            cur_state = state
            # Self-heal: if Chromium has gone away, demote state to STOPPED
            # so subsequent /status calls don't keep reporting RUNNING_*.
            if not alive and cur_state != State.STOPPED:
                log.info("Idle monitor: Quark not alive, demoting state to STOPPED")
                state = State.STOPPED
                proc = None
                pgid = None
                started_at = None
            elif alive and cur_state == State.STOPPED:
                # Self-heal the opposite direction: cold-start races can
                # leave us in STOPPED while Chromium is already serving
                # 9222 (the wrapper exited under spark runtime before
                # 9222 came up, so the first idle check demoted us).
                # Re-attach the running Quark by pgid and promote state.
                found = _find_quark_main_pid()
                if found is not None:
                    pid, new_pgid = found
                    proc = None
                    pgid = new_pgid
                    state = State.RUNNING_VISIBLE
                    started_at = started_at or _now()
                    log.info(
                        "Idle monitor: re-adopted running Quark "
                        "pid=%s pgid=%s (state was stale STOPPED)",
                        pid, new_pgid,
                    )
                    continue
        if not alive or cur_pgid is None:
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
async def lifespan(app: FastAPI):  # noqa: ARG001  (FastAPI requires this name)
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
