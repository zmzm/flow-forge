"""Deterministic task.md discovery and idempotent queue synchronization."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, TypedDict

import flowforge_storage as storage
from flowforge_storage import file_transaction, validate_queue


class SyncReport(TypedDict):
    added: list[str]
    changed: list[str]
    missing: list[str]
    restored: list[str]
    unchanged: list[str]
    applied: bool
    queue_total: int


def discover(root: Path) -> dict[str, dict]:
    if not root.is_dir():
        raise ValueError(f"Task source directory not found: {root}")
    root = root.resolve()
    result = {}
    for parent, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(parent) / d).is_symlink())
        if "task.md" not in names:
            continue
        path = Path(parent) / "task.md"
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            continue
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError(f"Empty task description: {path}")
        key = path.parent.relative_to(root).as_posix()  # '.' is the root-level task.
        title = next((s.lstrip("#").strip() for s in text.splitlines() if s.strip()), key)
        result[key] = {
            "task_file": str(path.resolve()),
            "source_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
            "title": title[:100],
        }
    return dict(sorted(result.items()))


def source_id(key: str) -> str:
    # The whole relative path participates, so repeated basenames remain distinct.
    return "task-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def plan_sync(context) -> tuple[list[dict[str, Any]], SyncReport]:
    if context.tasks_dir is None:
        raise ValueError(
            "sync requires a directory-source project; JSONL queues are already authoritative"
        )
    sources = discover(context.tasks_dir)
    tasks = storage.read_tasks_jsonl(context.tasks_file) if context.tasks_file.exists() else []
    validate_queue(tasks)
    tasks = [dict(task) for task in tasks]
    by_source = {task["source_key"]: task for task in tasks if "source_key" in task}
    by_id = {task["id"]: task for task in tasks}
    report: SyncReport = {
        "added": [],
        "changed": [],
        "missing": [],
        "restored": [],
        "unchanged": [],
        "applied": False,
        "queue_total": len(tasks),
    }
    for key, source in sources.items():
        row = by_source.get(key)
        if row is None:
            task_id = source_id(key)
            if task_id in by_id:
                raise ValueError(f"Discovered task ID collides with queue entry: {task_id}")
            row = {"id": task_id, "source_key": key, "status": "todo", **source}
            tasks.append(row)
            by_id[task_id] = row
            report["added"].append(task_id)
            continue
        if row.get("source_missing"):
            report["restored"].append(row["id"])
        row["source_missing"] = False
        row["task_file"] = source["task_file"]
        if row.get("source_hash") != source["source_hash"]:
            report["changed"].append(row["id"])
            row["source_changed"] = True
            row["observed_source_hash"] = source["source_hash"]
        else:
            row.pop("source_changed", None)
            row.pop("observed_source_hash", None)
            report["unchanged"].append(row["id"])
    for key, row in by_source.items():
        if key not in sources:
            row["source_missing"] = True
            report["missing"].append(row["id"])
    report["queue_total"] = len(tasks)
    return tasks, report


def sync(context, dry_run: bool = False) -> SyncReport:
    if context.tasks_dir is None:
        raise ValueError(
            "sync requires a directory-source project; JSONL queues are already authoritative"
        )
    if dry_run:
        return plan_sync(context)[1]
    with file_transaction(context.tasks_file):
        tasks, report = plan_sync(context)
        storage.write_tasks_jsonl(context.tasks_file, tasks)
        report["applied"] = True
        return report


def reset_task(context, task_id: str) -> None:
    """Explicit source-change policy: retain history, invalidate outputs and session."""
    import shutil
    import uuid
    from datetime import datetime, timezone

    with file_transaction(context.tasks_file):
        tasks = storage.read_tasks_jsonl(context.tasks_file)
        index = storage.find_task_index(tasks, task_id)
        if index is None:
            raise ValueError(f"Task not found: {task_id}")
        task = tasks[index]
        if task.get("status") == "running":
            raise ValueError("Cannot reset a running task")
        task.setdefault("history", []).append(
            {
                **{
                    key: task.get(key)
                    for key in (
                        "status",
                        "outputs",
                        "checkpoints",
                        "session_id",
                        "last_run_log",
                        "source_hash",
                    )
                },
                "reset_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if context.artifacts_dir:
            directory = context.artifacts_dir / source_id(task.get("source_key", task_id))
            if directory.exists():
                backup = directory.with_name(directory.name + ".reset-" + uuid.uuid4().hex)
                shutil.move(directory, backup)
                task["history"][-1]["artifacts_backup"] = str(backup)
        task["status"] = "todo"
        for key in (
            "outputs",
            "checkpoints",
            "session_id",
            "source_changed",
            "stage",
            "stage_id",
            "stage_fingerprint",
        ):
            task.pop(key, None)
        if task.get("observed_source_hash"):
            task["source_hash"] = task.pop("observed_source_hash")
        storage.write_tasks_jsonl(context.tasks_file, tasks)
