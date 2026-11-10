"""Atomic files, short queue transactions and canonical execution locks."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from flowforge_config import RuntimeContext


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if path.exists():
            os.fchmod(fd, path.stat().st_mode & 0o777)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_json(path: Path, value: object) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


@contextmanager
def file_transaction(path: Path) -> Iterator[None]:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".mutation.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def execution_lock_paths(context: RuntimeContext) -> list[Path]:
    # One lock namespace per OS user, independent of config/state overrides and cwd.
    root = Path("/tmp") / f"flowforge-locks-{os.getuid()}"

    def keyed(kind: str, path: Path) -> Path:
        digest = hashlib.sha256(os.fsencode(path.resolve())).hexdigest()
        return root / f"{kind}-{digest}.lock"

    return [
        keyed("workspace", context.workspace),
        keyed("queue", context.tasks_file),
        context.runs_dir / ".flowforge.lock",
    ]


class ExecutionLocks:
    def __init__(self, handles: list[IO[str]]) -> None:
        self.handles = handles

    def close(self) -> None:
        for handle in reversed(self.handles):
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        self.handles.clear()


def acquire_execution(context: RuntimeContext) -> ExecutionLocks | None:
    handles: list[IO[str]] = []
    try:
        for path in execution_lock_paths(context):
            handle = acquire_lock(path)
            if handle is None:
                ExecutionLocks(handles).close()
                return None
            handles.append(handle)
        return ExecutionLocks(handles)
    except BaseException:
        ExecutionLocks(handles).close()
        raise


def lock_held(path: Path) -> bool:
    # Never create a directory, lock file, or truncate a worker's PID during inspection.
    try:
        handle = path.open("r")
    except FileNotFoundError:
        return False
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def busy(context: RuntimeContext) -> bool:
    return any(lock_held(path) for path in execution_lock_paths(context))


def update_task(
    path: Path, task_id: str, updates: dict[str, Any], remove: Iterable[str] = ()
) -> None:
    with file_transaction(path):
        tasks = read_tasks_jsonl(path)
        index = find_task_index(tasks, task_id)
        if index is None:
            raise ValueError(f"Task no longer exists: {task_id}")
        row = tasks[index]
        if "outputs" in updates:
            updates = {**updates, "outputs": {**row.get("outputs", {}), **updates["outputs"]}}
        row.update(updates)
        for key in remove:
            row.pop(key, None)
        write_tasks_jsonl(path, tasks)


def append_history(path: Path, task_id: str, attempt: dict) -> None:
    with file_transaction(path):
        tasks = read_tasks_jsonl(path)
        index = find_task_index(tasks, task_id)
        if index is not None:
            tasks[index].setdefault("history", []).append(attempt)
            write_tasks_jsonl(path, tasks)


def read_tasks_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Tasks file not found: {path}")
    tasks: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Queue line must be a JSON object: {path}")
            tasks.append(value)
    return tasks


def write_tasks_jsonl(path: Path, tasks: list[dict[str, Any]]) -> None:
    data = "\n".join(json.dumps(t, ensure_ascii=False) for t in tasks) + "\n"
    atomic_text(path, data)


def _persist_task_update(path: Path, task_id: str, updates: dict[str, Any]) -> None:
    """Merge a task's field updates into a freshly read tasks.jsonl (atomic)."""
    with file_transaction(path):
        tasks = read_tasks_jsonl(path)
        pos = find_task_index(tasks, task_id)
        if pos is None:
            tasks.append({"id": task_id})
            pos = len(tasks) - 1
        if "outputs" in updates:
            updates = {
                **updates,
                "outputs": {**tasks[pos].get("outputs", {}), **updates["outputs"]},
            }
        tasks[pos].update(updates)
        write_tasks_jsonl(path, tasks)


def recover_stale_running_tasks(path: Path) -> list[str]:
    """Mark 'running' tasks (left over from a dead process) as failed so they auto-resume."""
    if not path.exists():
        return []
    with file_transaction(path):
        tasks = read_tasks_jsonl(path)
        recovered = []
        for task in tasks:
            if task.get("status") == "running":
                task["status"] = "failed"
                recovered.append(str(task.get("id") or "-"))
        if recovered:
            write_tasks_jsonl(path, tasks)
        return recovered


def acquire_lock(path: Path) -> IO[str] | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def release_lock(handle: IO[str]) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        handle.close()


def find_task_index(tasks: list[dict[str, Any]], task_id: str) -> int | None:
    for i, t in enumerate(tasks):
        if (t.get("id") or f"task{i + 1}") == task_id:
            return i
    return None


def validate_queue(tasks: list[dict]) -> None:
    ids, sources = set(), set()
    for row in tasks:
        task_id = row.get("id")
        if not isinstance(task_id, str) or not task_id or task_id in ids:
            raise ValueError(f"Missing, invalid or duplicate task id: {task_id!r}")
        ids.add(task_id)
        for key in ("agents", "outputs", "checkpoints"):
            if key in row and not isinstance(row[key], dict):
                raise ValueError(f"Task {task_id}: {key} must be an object")
        if "history" in row and not isinstance(row["history"], list):
            raise ValueError(f"Task {task_id}: history must be a list")
        if "status" in row and not isinstance(row["status"], str):
            raise ValueError(f"Task {task_id}: status must be a string")
        source = row.get("source_key")
        if source is not None:
            if not isinstance(source, str) or source in sources:
                raise ValueError(f"Invalid or duplicate source_key: {source!r}")
            sources.add(source)
        if "task_file" not in row or not isinstance(row["task_file"], str):
            raise ValueError(f"Task {task_id} requires a task_file string")
