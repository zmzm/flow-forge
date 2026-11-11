"""Read-only OpenCode database and run-log inspection shared by status commands."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from flowforge_models import StepConfig
from flowforge_workflows import task_agents

DEFAULT_DB = Path.home() / ".local/share/opencode/opencode.db"


def session_prefix(context) -> str:
    identity = str(context.workspace.resolve()) + "\0" + str(context.tasks_file.resolve())
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return f"flowforge:{digest}:"


def connect(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def model_label(raw: str | None) -> str:
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


def fetch_sessions(db: sqlite3.Connection, since_ms: int) -> list[dict[str, Any]]:
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
            "model": model_label(r[4]),
            "time_created": r[5],
            "time_updated": r[6],
        }
        for r in rows
    ]


def fetch_session(db: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
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
        "model": model_label(row[4]),
        "time_created": row[5],
        "time_updated": row[6],
    }


def fetch_child_sessions(db: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
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
            "model": model_label(r[4]),
            "time_created": r[5],
            "time_updated": r[6],
        }
        for r in rows
    ]


def part_summary(data: str) -> str:
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


def resolve_run_session(
    db: sqlite3.Connection, task_id: str, session_id: Any, since_ms: int
) -> dict[str, Any] | None:
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


def fetch_recent_parts(db: sqlite3.Connection, session_id: str, limit: int = 3) -> list[str]:
    rows = db.execute(
        "SELECT time_created, data FROM part WHERE session_id = ?"
        " ORDER BY time_created DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    lines: list[str] = []
    for ts, data in reversed(rows):
        stamp = datetime.fromtimestamp(ts / 1000).strftime("%H:%M:%S")
        summary = part_summary(data)
        lines.append(f"      {stamp}  {summary}" if summary else f"      {stamp}  ?")
    return lines


def run_start_ms(name: str) -> int | None:
    match = re.match(r"^(\d{8}_\d{6})_", name)
    if not match:
        return None
    try:
        start = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        return None
    return int(start.timestamp() * 1000)


def read_head(path: Path, max_bytes: int) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def read_tail(path: Path, max_bytes: int) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def detect_stage(
    task: dict[str, Any], step_configs: list[StepConfig], log: Path | None
) -> str | None:
    if log is None or not log.exists():
        return None
    commands = [line for line in read_head(log, 2_000_000).split("\n") if line.startswith("$ ")]
    if not commands:
        return None
    match = re.search(r"--agent (\S+)", commands[-1])
    if not match:
        return f"stage {len(commands)}"
    agent = match.group(1)
    try:
        resolved = task_agents(task, step_configs)
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
