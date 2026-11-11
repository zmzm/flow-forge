"""OpenCode subprocess lifetime, incremental logs and bounded event summaries."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from flowforge_models import Event, EventCallback, StepResult

GRACE_SECONDS = 10
logger = logging.getLogger("flowforge.opencode")


def _signal_process(proc: subprocess.Popen[str], sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


class RunControl:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.stop_requested = False
        self._active_proc: subprocess.Popen[str] | None = None
        self._kill_timer: threading.Timer | None = None

    def set_active_proc(self, proc: subprocess.Popen[str] | None) -> None:
        with self._lock:
            if self._kill_timer:
                self._kill_timer.cancel()
                self._kill_timer = None
            self._active_proc = proc
            if proc and self.stop_requested:
                self._terminate(proc)

    def _terminate(self, proc: subprocess.Popen[str]) -> None:
        _signal_process(proc, signal.SIGTERM)
        if self._kill_timer is None:
            self._kill_timer = threading.Timer(
                GRACE_SECONDS, _signal_process, (proc, signal.SIGKILL)
            )
            self._kill_timer.daemon = True
            self._kill_timer.start()

    def request_stop(self) -> None:
        with self._lock:
            self.stop_requested = True
            if self._active_proc:
                self._terminate(self._active_proc)

    def terminate(self) -> None:
        with self._lock:
            if self._active_proc:
                self._terminate(self._active_proc)


def parse_session_id_from_json_events(output: str) -> str | None:
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(ev, dict):
            continue
        for key in ("sessionID", "sessionId", "session_id", "session"):
            val = ev.get(key)
            if isinstance(val, str) and val:
                return val

        part = ev.get("part")
        if isinstance(part, dict):
            sid = part.get("sessionID") or part.get("sessionId") or part.get("session_id")
            if isinstance(sid, str) and sid:
                return sid

        meta = ev.get("metadata")
        if isinstance(meta, dict):
            sid = (
                meta.get("sessionID")
                or meta.get("sessionId")
                or meta.get("session_id")
                or meta.get("id")
            )
            if isinstance(sid, str) and sid:
                return sid
    return None


def parse_opencode_error_from_json_events(output: str) -> str | None:
    """Return the most useful error reported by OpenCode's JSON event stream."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "error":
            continue

        error = event.get("error")
        if not isinstance(error, dict):
            return str(error or "Unknown OpenCode error")
        data = error.get("data")
        data = data if isinstance(data, dict) else {}
        name = str(error.get("name") or "OpenCode error")
        message = str(data.get("message") or error.get("message") or "").strip()
        reference = str(data.get("ref") or error.get("ref") or "").strip()

        details = f"{name}: {message}" if message else name
        if reference:
            details += f" (ref: {reference})"
        return details
    return None


def _proc_stat_snapshot(pid: int) -> tuple[float, int] | None:
    """Best-effort (cpu_seconds, rss_bytes) for a pid; None when unavailable."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = data.rsplit(")", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        cpu_seconds = (int(fields[11]) + int(fields[12])) / ticks
        rss_bytes = int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
        return cpu_seconds, rss_bytes
    except (OSError, ValueError, IndexError, KeyError, ZeroDivisionError):
        return None


def run_step(
    cmd: list[str],
    *,
    cwd: Path,
    log_path: Path,
    agent: str,
    timeout: int | None,
    heartbeat: int | None,
    callback: EventCallback = None,
    control: RunControl | None = None,
    session_id: str | None = None,
    environment: dict | None = None,
) -> StepResult:
    """Return exit code, session/error JSON summary, session id and timeout state."""

    def emit(event: Event) -> None:
        if callback:
            callback(event)

    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        command = "$ " + " ".join(cmd)
        log.write("\n" + command + "\n\n")
        emit({"kind": "command", "text": command})
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env={**os.environ, **(environment or {}), "PWD": str(cwd)},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        active = control or RunControl()
        timed_out = threading.Event()
        stop_beat = threading.Event()
        started = time.monotonic()
        last_activity = [started]
        timer = None
        beat_thread = None
        sid = session_id
        error_event = None

        def expire() -> None:
            timed_out.set()
            active.terminate()

        def beat() -> None:
            assert heartbeat is not None
            last_beat = started
            previous = None
            while not stop_beat.wait(heartbeat):
                now = time.monotonic()
                if now - last_activity[0] < heartbeat:
                    continue
                alive = proc.poll() is None
                stat = _proc_stat_snapshot(proc.pid) if alive else None
                detail = ""
                if stat and previous and now > last_beat:
                    cpu = (stat[0] - previous[0]) / (now - last_beat) * 100
                    detail = f", cpu {cpu:.1f}%, rss {stat[1] / (1024 * 1024):.0f}MB"
                previous, last_beat = stat, now
                text = (
                    f"[heartbeat] agent '{agent}' {int(now - started)}s in step, "
                    f"no output for {int(now - last_activity[0])}s "
                    f"(pid {proc.pid} {'alive' if alive else 'exited'}{detail})"
                )
                try:
                    with log_path.open("a", encoding="utf-8") as handle:
                        handle.write(text + "\n")
                    emit({"kind": "line", "text": text})
                except Exception:
                    logger.exception("Heartbeat observer or log unavailable")
                    return

        try:
            active.set_active_proc(proc)
            if timeout:
                timer = threading.Timer(timeout, expire)
                timer.daemon = True
                timer.start()
            if heartbeat:
                beat_thread = threading.Thread(target=beat, daemon=True)
                beat_thread.start()
            assert proc.stdout is not None
            for line in proc.stdout:
                last_activity[0] = time.monotonic()
                log.write(line)
                text = line.rstrip("\n")
                if not text.strip():
                    continue
                try:
                    event = json.loads(text)
                except ValueError:
                    event = None
                if not isinstance(event, dict):
                    emit({"kind": "line", "text": text})
                    continue
                found = parse_session_id_from_json_events(text)
                if found and found != sid:
                    sid = found
                    emit({"kind": "session", "session_id": sid})
                if event.get("type") == "error":
                    error = parse_opencode_error_from_json_events(text)
                    error_event = {
                        "type": "error",
                        "error": (error or "Unknown OpenCode error")[:16384],
                    }
                emit({"kind": "opencode_event", "event": event})
            try:
                rc = proc.wait(timeout=GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                _signal_process(proc, signal.SIGKILL)
                rc = proc.wait()
        finally:
            stop_beat.set()
            if timer:
                timer.cancel()
            _signal_process(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                _signal_process(proc, signal.SIGKILL)
                proc.wait()
            finally:
                _signal_process(proc, signal.SIGKILL)
                if proc.stdout is not None:
                    proc.stdout.close()
                active.set_active_proc(None)
                if beat_thread:
                    beat_thread.join(timeout=1)

    summary = [json.dumps({"sessionID": sid})] if sid else []
    if error_event:
        summary.append(json.dumps(error_event))
    return StepResult(rc, "\n".join(summary), sid, timed_out.is_set())
