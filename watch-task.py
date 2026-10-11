#!/usr/bin/env python3
"""Live status for FlowForge runs: running tasks plus opencode session activity.

Reads tasks.jsonl (via pipeline_runner) and the opencode SQLite DB read-only.
Never writes, never contacts the network.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pipeline_runner as pr

DEFAULT_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"

STATUS_SCHEMA_VERSION = 1
QUOTA_RESET_RE = re.compile(r"limit will reset at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
STEP_ERROR_RE = re.compile(r"(Step '[^']+' failed[^\n]{0,300})")


def _connect(db_path: Path) -> Optional[sqlite3.Connection]:
    if not db_path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _model_label(raw: Optional[str]) -> str:
    if not raw:
        return "-"
    try:
        model = json.loads(raw)
    except (ValueError, TypeError):
        return str(raw)
    if isinstance(model, dict):
        provider = str(model.get("providerID") or "")
        model_id = str(model.get("id") or "")
        return f"{provider}/{model_id}".lstrip("/") if model_id else provider or "-"
    return str(model)


def fetch_sessions(db: sqlite3.Connection, since_ms: int) -> List[Dict[str, Any]]:
    rows = db.execute(
        "SELECT id, parent_id, title, agent, model, time_created, time_updated"
        " FROM session WHERE time_archived IS NULL AND time_updated >= ?"
        " ORDER BY time_updated DESC",
        (since_ms,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "parent_id": r[1],
            "title": r[2] or "",
            "agent": r[3] or "-",
            "model": _model_label(r[4]),
            "time_created": r[5],
            "time_updated": r[6],
        }
        for r in rows
    ]


def fetch_session(db: sqlite3.Connection, session_id: str) -> Optional[Dict[str, Any]]:
    row = db.execute(
        "SELECT id, parent_id, title, agent, model, time_created, time_updated"
        " FROM session WHERE id = ?",
        (session_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "parent_id": row[1],
        "title": row[2] or "",
        "agent": row[3] or "-",
        "model": _model_label(row[4]),
        "time_created": row[5],
        "time_updated": row[6],
    }


def fetch_child_sessions(db: sqlite3.Connection, session_id: str) -> List[Dict[str, Any]]:
    rows = db.execute(
        "SELECT id, parent_id, title, agent, model, time_created, time_updated"
        " FROM session WHERE parent_id = ? AND time_archived IS NULL"
        " ORDER BY time_created",
        (session_id,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "parent_id": r[1],
            "title": r[2] or "",
            "agent": r[3] or "-",
            "model": _model_label(r[4]),
            "time_created": r[5],
            "time_updated": r[6],
        }
        for r in rows
    ]


def _part_summary(data: str) -> str:
    try:
        part = json.loads(data)
    except (ValueError, TypeError):
        return ""
    ptype = str(part.get("type") or "?")
    if ptype == "tool":
        state = part.get("state") or {}
        status = state.get("status", "") if isinstance(state, dict) else ""
        return f"tool {part.get('tool', '?')} [{status}]"
    if ptype == "text":
        text = (part.get("text") or "").replace("\n", " ")
        return f"text: {text[:90]}"
    if ptype == "reasoning":
        return "reasoning"
    return ptype


def _resolve_run_session(
    db: sqlite3.Connection, task_id: str, session_id: Any, since_ms: int
) -> Optional[Dict[str, Any]]:
    session = fetch_session(db, str(session_id)) if session_id else None
    if session is not None and session["time_updated"] >= since_ms:
        return session
    row = db.execute(
        "SELECT id FROM session WHERE time_archived IS NULL AND time_updated >= ?"
        " AND title LIKE ? ORDER BY time_updated DESC LIMIT 1",
        (since_ms, f"{task_id}:%"),
    ).fetchone()
    if row:
        return fetch_session(db, row[0])
    return session


def fetch_recent_parts(db: sqlite3.Connection, session_id: str, limit: int = 3) -> List[str]:
    rows = db.execute(
        "SELECT time_created, data FROM part WHERE session_id = ?"
        " ORDER BY time_created DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    lines: List[str] = []
    for ts, data in reversed(rows):
        stamp = datetime.fromtimestamp(ts / 1000).strftime("%H:%M:%S")
        summary = _part_summary(data)
        lines.append(f"      {stamp}  {summary}" if summary else f"      {stamp}  ?")
    return lines


def _age(ms: Optional[int], now_ms: int) -> str:
    if not ms:
        return "-"
    seconds = max(0, int((now_ms - ms) / 1000))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _short(session_id: str) -> str:
    return f"{session_id[:10]}..{session_id[-4:]}" if len(session_id) > 16 else session_id


def _current_run_log(task: Dict[str, Any]) -> Optional[Path]:
    task_id = str(task.get("id") or "")
    candidates: List[Path] = []
    if task_id:
        try:
            candidates = sorted(pr.RUNS_DIR.glob(f"*_{task_id}.log"), key=lambda p: p.stat().st_mtime)
        except OSError:
            candidates = []
    if candidates:
        return candidates[-1]
    raw = task.get("last_run_log")
    return Path(raw) if raw else None


def _task_title(task: Dict[str, Any]) -> Optional[str]:
    raw = task.get("task_file")
    if not raw:
        return None
    try:
        with pr.resolve_file(str(raw), pr.PROJECT_DIR).open("r", encoding="utf-8", errors="replace") as handle:
            chunk = handle.read(4096)
    except OSError:
        return None
    for line in chunk.split("\n"):
        text = line.strip()
        if not text:
            continue
        if text.startswith("#"):
            title = text.lstrip("#").strip()
            if title:
                return title[:100]
        break
    return None


def _lock_held() -> bool:
    handle = pr.acquire_lock(pr.RUNS_DIR / ".flowforge.lock")
    if handle is None:
        return True
    pr.release_lock(handle)
    return False


def _run_start_ms(name: str) -> Optional[int]:
    match = re.match(r"^(\d{8}_\d{6})_", name)
    if not match:
        return None
    try:
        start = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        return None
    return int(start.timestamp() * 1000)


def _read_head(path: Path, max_bytes: int) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _read_tail(path: Path, max_bytes: int) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _detect_stage(
    task: Dict[str, Any], step_configs: List[Dict[str, Any]], log: Optional[Path]
) -> Optional[str]:
    if log is None or not log.exists():
        return None
    commands = [line for line in _read_head(log, 2_000_000).split("\n") if line.startswith("$ ")]
    if not commands:
        return None
    match = re.search(r"--agent (\S+)", commands[-1])
    if not match:
        return f"stage {len(commands)}"
    agent = match.group(1)
    try:
        resolved = pr.task_agents(task, step_configs)
    except ValueError:
        resolved = {}
    for step in step_configs:
        names = {str(a) for a in (step.get("agents") or []) if a}
        names.add(str(step.get("agent") or ""))
        override = resolved.get(str(step.get("id") or ""))
        if override:
            names.add(override)
        if agent in names:
            return str(step.get("label") or agent)
    return agent


def build_status(*, tasks_file: Path, now_ms: Optional[int] = None) -> Dict[str, Any]:
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    try:
        tasks = pr.read_tasks_jsonl(tasks_file)
    except (FileNotFoundError, ValueError):
        tasks = []
    try:
        step_configs = pr.read_step_configs(tasks_file.with_name("steps.json"))
    except (OSError, ValueError):
        step_configs = []

    rows = [
        {
            "id": str(task.get("id") or "-"),
            "title": _task_title(task),
            "status": str(task.get("status") or "todo"),
        }
        for task in tasks
    ]
    lock_held = _lock_held()
    status: Dict[str, Any] = {
        "schema_version": STATUS_SCHEMA_VERSION,
        "lock_held": lock_held,
        "running": None,
        "stale_running_task_id": None,
        "tasks": rows,
        "queue_total": sum(1 for row in rows if row["status"] == "todo"),
        "last_failed": None,
    }

    running_task = next((t for t in tasks if str(t.get("status")) == "running"), None)
    if running_task is not None:
        task_id = str(running_task.get("id") or "-")
        log = _current_run_log(running_task)
        info: Dict[str, Any] = {
            "id": task_id,
            "title": _task_title(running_task),
            "session_id": str(running_task.get("session_id") or "") or None,
            "stage": _detect_stage(running_task, step_configs, log),
        }
        if log is not None:
            info["log_path"] = str(log)
            start_ms = _run_start_ms(log.name)
            if start_ms is not None:
                info["duration_sec"] = max(0.0, (now_ms - start_ms) / 1000)
        if lock_held:
            status["running"] = info
        else:
            status["stale_running_task_id"] = task_id

    failed_task = next((t for t in reversed(tasks) if str(t.get("status")) == "failed"), None)
    if failed_task is not None:
        entry: Dict[str, Any] = {
            "id": str(failed_task.get("id") or "-"),
            "title": _task_title(failed_task),
        }
        log = _current_run_log(failed_task)
        if log is not None and log.exists():
            entry["log_path"] = str(log)
            tail = _read_tail(log, 65_536)
            quota = QUOTA_RESET_RE.search(tail)
            if quota:
                entry["quota_reset_at"] = quota.group(1)
            step_error = STEP_ERROR_RE.search(tail)
            if step_error:
                entry["error"] = step_error.group(1).strip()
        status["last_failed"] = entry
    return status


def _log_info(task: Dict[str, Any], now_ms: int) -> str:
    path = _current_run_log(task)
    if not path:
        return "log: -"
    if not path.exists():
        return f"log: {path} (missing)"
    stat = path.stat()
    return f"log: {path} ({_age(int(stat.st_mtime * 1000), now_ms)} ago, {stat.st_size // 1024} KB)"


def _render_session(
    db: sqlite3.Connection,
    session: Dict[str, Any],
    now_ms: int,
    parts: int,
    depth: int = 0,
    rendered_ids: Optional[set] = None,
) -> List[str]:
    if rendered_ids is not None:
        rendered_ids.add(session["id"])
    prefix = "  └─ " if depth else "  "
    lines = [
        f"{prefix}{_short(session['id'])}  agent={session['agent']} model={session['model']}"
        f"  updated {_age(session['time_updated'], now_ms)} ago"
    ]
    if session["title"]:
        lines.append(f"{'    ' * (depth + 1)}title: {session['title'][:70]}")
    lines.extend(fetch_recent_parts(db, session["id"], parts))
    for child in fetch_child_sessions(db, session["id"]):
        lines.extend(_render_session(db, child, now_ms, parts, depth + 1, rendered_ids))
    return lines


def render_report(
    *,
    tasks: List[Dict[str, Any]],
    db: Optional[sqlite3.Connection],
    window_minutes: int,
    parts: int,
    notice: Optional[str] = None,
) -> str:
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - window_minutes * 60 * 1000
    out = [f"FlowForge watch  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]
    if notice:
        out.append(notice)
    out.append("")

    running = [t for t in tasks if str(t.get("status")) == "running"]
    out.append(f"Running tasks ({len(running)}):")
    if not running:
        out.append("  none")
    rendered_ids: set = set()
    for task in running:
        task_id = str(task.get("id") or "?")
        out.append(f"  {task_id}  {_log_info(task, now_ms)}")
        sid = task.get("session_id")
        if db is None or not sid:
            out.append("    session: unknown (no db or no session_id yet)")
            continue
        session = _resolve_run_session(db, task_id, sid, since_ms)
        if session is None:
            out.append(f"    session {_short(str(sid or '?'))}: not found in db (newer/other project?)")
            continue
        out.extend(_render_session(db, session, now_ms, parts, rendered_ids=rendered_ids))
    out.append("")

    if db is None:
        out.append("opencode db not found/readable; active sessions unavailable")
        return "\n".join(out)

    sessions = fetch_sessions(db, since_ms)
    others = [
        s for s in sessions
        if s["id"] not in rendered_ids and s["parent_id"] not in rendered_ids
    ]
    if others:
        out.append("Other active sessions (not tied to a running task):")
    for session in others:
        out.extend(_render_session(db, session, now_ms, parts))
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Watch FlowForge task runs and opencode session activity.")
    parser.add_argument("--tasks-file", type=Path, default=pr.TASKS_FILE)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="path to opencode.db")
    parser.add_argument("--interval", type=float, default=5.0, help="refresh interval in seconds (loop mode)")
    parser.add_argument("--once", action="store_true", help="print a single report and exit")
    parser.add_argument("--json", action="store_true", help="print machine-readable status JSON and exit")
    parser.add_argument("--window", type=int, default=15, help="active-session window in minutes")
    parser.add_argument("--parts", type=int, default=3, help="recent parts shown per session")
    args = parser.parse_args(argv)

    if args.json:
        print(json.dumps(build_status(tasks_file=args.tasks_file), ensure_ascii=False, indent=2))
        return 0

    def _report() -> str:
        notice: Optional[str] = None
        tasks: List[Dict[str, Any]] = []
        try:
            tasks = pr.read_tasks_jsonl(args.tasks_file)
        except (FileNotFoundError, ValueError) as exc:
            notice = f"tasks file unreadable: {exc}"
        db = _connect(args.db.expanduser())
        return render_report(
            tasks=tasks,
            db=db,
            window_minutes=args.window,
            parts=args.parts,
            notice=notice,
        )

    if args.once:
        print(_report())
        return 0

    try:
        while True:
            sys.stdout.write("\033[2J\033[H" + _report() + "\n")
            time.sleep(max(0.5, args.interval))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
