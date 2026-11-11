"""Read-only project status with project-scoped OpenCode correlation."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import flowforge_inspection as inspection
import flowforge_storage as storage
from flowforge_inspection import session_prefix
from flowforge_storage import busy, execution_lock_paths, lock_held


def status(context, db_path=None) -> dict:
    from flowforge_storage import validate_queue

    tasks = storage.read_tasks_jsonl(context.tasks_file) if context.tasks_file.exists() else []
    validate_queue(tasks)
    held = busy(context)
    lock_paths = execution_lock_paths(context)
    selected_active = lock_held(lock_paths[1]) or lock_held(lock_paths[2])
    counts = {
        state: sum(t.get("status", "todo") == state for t in tasks)
        for state in ("todo", "running", "done", "failed")
    }
    rows = []
    db = inspection.connect(db_path or inspection.DEFAULT_DB)
    try:
        for task in tasks:
            row = {
                key: task.get(key)
                for key in (
                    "id",
                    "status",
                    "title",
                    "session_id",
                    "attempt_id",
                    "source_key",
                    "source_changed",
                    "source_missing",
                    "last_run_log",
                )
            }
            row["status"] = row["status"] or "todo"
            log = Path(task["last_run_log"]) if task.get("last_run_log") else None
            try:
                row["stage"] = task.get("stage") or inspection.detect_stage(
                    task, context.steps(task), log
                )
            except (OSError, ValueError) as exc:
                row["stage"] = None
                row["configuration_issue"] = str(exc)
            if log:
                start = inspection.run_start_ms(log.name)
                if start is not None:
                    row["duration_sec"] = (
                        max(0.0, (time.time() * 1000 - start) / 1000)
                        if task.get("status") == "running"
                        else None
                    )
            row["session"] = None
            belongs = (
                not task.get("execution_workspace")
                or Path(task["execution_workspace"]).resolve() == context.workspace
            )
            if not belongs:
                row["session_issue"] = "This queue entry was executed in another workspace"
            if db is not None and task.get("status") == "running" and belongs:
                try:
                    session = (
                        inspection.fetch_session(db, task["session_id"])
                        if task.get("session_id")
                        else None
                    )
                    prefix = session_prefix(context) + str(task["id"]) + ":"
                    # A saved ID is authoritative, but verify workspace when the DB exposes it.
                    columns = {r[1] for r in db.execute("PRAGMA table_info(session)")}
                    if session and "directory" in columns:
                        directory = db.execute(
                            "SELECT directory FROM session WHERE id = ?", (session["id"],)
                        ).fetchone()[0]
                        if directory and Path(directory).resolve() != context.workspace:
                            session = None
                    if session is None:
                        found = db.execute(
                            "SELECT id FROM session WHERE substr(title,1,?) = ? ORDER BY time_updated DESC LIMIT 1",
                            (len(prefix), prefix),
                        ).fetchone()
                        session = inspection.fetch_session(db, found[0]) if found else None
                    if session:
                        row["session"] = {
                            "id": session["id"],
                            "agent": session["agent"],
                            "model": session["model"],
                            "children": inspection.fetch_child_sessions(db, session["id"]),
                        }
                except (KeyError, sqlite3.Error):
                    row["session_issue"] = "OpenCode database schema unavailable or unsupported"
            rows.append(row)
    finally:
        if db is not None:
            db.close()
    return {
        "busy": held,
        "counts": counts,
        "queue_total": counts["todo"],
        "tasks": rows,
        "stale_running": [row["id"] for row in rows if row["status"] == "running"]
        if not selected_active
        else [],
        "running": [row for row in rows if row["status"] == "running"] if selected_active else [],
    }
