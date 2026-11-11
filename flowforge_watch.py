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
from typing import Any

import pipeline_runner as pr
from flowforge_inspection import (
    DEFAULT_DB,
    fetch_child_sessions,
    fetch_recent_parts,
    fetch_sessions,
)
from flowforge_inspection import (
    connect as _connect,
)
from flowforge_inspection import (
    detect_stage as _detect_stage,
)
from flowforge_inspection import (
    read_tail as _read_tail,
)
from flowforge_inspection import (
    resolve_run_session as _resolve_run_session,
)
from flowforge_inspection import (
    run_start_ms as _run_start_ms,
)

STATUS_SCHEMA_VERSION = 1
QUOTA_RESET_RE = re.compile(r"limit will reset at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
STEP_ERROR_RE = re.compile(r"(Step '[^']+' failed[^\n]{0,300})")


def _age(ms: int | None, now_ms: int) -> str:
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


def _current_run_log(task: dict[str, Any], context=None) -> Path | None:
    task_id = str(task.get("id") or "")
    candidates: list[Path] = []
    if task_id:
        try:
            candidates = sorted(
                (context.runs_dir if context else pr.RUNS_DIR).glob(f"*_{task_id}.log"),
                key=lambda p: p.stat().st_mtime,
            )
        except OSError:
            candidates = []
    if candidates:
        return candidates[-1]
    raw = task.get("last_run_log")
    return Path(raw) if raw else None


def _task_title(task: dict[str, Any], context=None) -> str | None:
    raw = task.get("task_file")
    if not raw:
        return None
    workspace = context.workspace if context else pr.PROJECT_DIR
    if workspace is None:
        return None
    try:
        with pr.resolve_file(str(raw), workspace).open(
            "r", encoding="utf-8", errors="replace"
        ) as handle:
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


def _lock_held(context=None) -> bool:
    from flowforge_storage import lock_held

    return lock_held((context.runs_dir if context else pr.RUNS_DIR) / ".flowforge.lock")


def build_status(*, tasks_file: Path, now_ms: int | None = None, context=None) -> dict[str, Any]:
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
            "title": _task_title(task, context),
            "status": str(task.get("status") or "todo"),
        }
        for task in tasks
    ]
    lock_held = _lock_held(context)
    status: dict[str, Any] = {
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
        log = _current_run_log(running_task, context)
        info: dict[str, Any] = {
            "id": task_id,
            "title": _task_title(running_task, context),
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
        entry: dict[str, Any] = {
            "id": str(failed_task.get("id") or "-"),
            "title": _task_title(failed_task, context),
        }
        log = _current_run_log(failed_task, context)
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


def _log_info(task: dict[str, Any], now_ms: int, context=None) -> str:
    path = _current_run_log(task, context)
    if not path:
        return "log: -"
    if not path.exists():
        return f"log: {path} (missing)"
    stat = path.stat()
    return f"log: {path} ({_age(int(stat.st_mtime * 1000), now_ms)} ago, {stat.st_size // 1024} KB)"


def _render_session(
    db: sqlite3.Connection,
    session: dict[str, Any],
    now_ms: int,
    parts: int,
    depth: int = 0,
    rendered_ids: set | None = None,
) -> list[str]:
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
    tasks: list[dict[str, Any]],
    db: sqlite3.Connection | None,
    window_minutes: int,
    parts: int,
    notice: str | None = None,
    context=None,
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
        out.append(f"  {task_id}  {_log_info(task, now_ms, context)}")
        sid = task.get("session_id")
        if db is None or not sid:
            out.append("    session: unknown (no db or no session_id yet)")
            continue
        session = _resolve_run_session(db, task_id, sid, since_ms)
        if session is None:
            out.append(
                f"    session {_short(str(sid or '?'))}: not found in db (newer/other project?)"
            )
            continue
        out.extend(_render_session(db, session, now_ms, parts, rendered_ids=rendered_ids))
    out.append("")

    if db is None:
        out.append("opencode db not found/readable; active sessions unavailable")
        return "\n".join(out)

    sessions = fetch_sessions(db, since_ms)
    others = [
        s for s in sessions if s["id"] not in rendered_ids and s["parent_id"] not in rendered_ids
    ]
    if others:
        out.append("Other active sessions (not tied to a running task):")
    for session in others:
        out.extend(_render_session(db, session, now_ms, parts))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Watch FlowForge task runs and opencode session activity."
    )
    parser.add_argument("--tasks-file", type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--project")
    group.add_argument("--project-config")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="path to opencode.db")
    parser.add_argument(
        "--interval", type=float, default=5.0, help="refresh interval in seconds (loop mode)"
    )
    parser.add_argument("--once", action="store_true", help="print a single report and exit")
    parser.add_argument(
        "--json", action="store_true", help="print machine-readable status JSON and exit"
    )
    parser.add_argument("--window", type=int, default=15, help="active-session window in minutes")
    parser.add_argument("--parts", type=int, default=3, help="recent parts shown per session")
    args = parser.parse_args(argv)

    if args.project or args.project_config:
        from flowforge_cli import main as project_cli

        selection_args = (
            ["--project", args.project]
            if args.project
            else ["--project-config", args.project_config]
        )
        return project_cli(
            ["status", *selection_args, "--db", str(args.db), *(["--json"] if args.json else [])]
        )
    from flowforge_config import legacy_context

    try:
        context = legacy_context(overrides={"tasks_file": args.tasks_file})
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    args.tasks_file = context.tasks_file
    if args.json:
        print(
            json.dumps(
                build_status(tasks_file=args.tasks_file, context=context),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    def _report() -> str:
        notice: str | None = None
        tasks: list[dict[str, Any]] = []
        try:
            tasks = pr.read_tasks_jsonl(args.tasks_file)
        except (FileNotFoundError, ValueError) as exc:
            notice = f"tasks file unreadable: {exc}"
        db = _connect(args.db.expanduser())
        try:
            return render_report(
                tasks=tasks,
                db=db,
                window_minutes=args.window,
                parts=args.parts,
                notice=notice,
                context=context,
            )
        finally:
            if db is not None:
                db.close()

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
