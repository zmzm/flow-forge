"""Atomic files, short queue transactions and canonical execution locks."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


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


def atomic_json(path: Path, value) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


@contextmanager
def file_transaction(path: Path):
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".mutation.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def execution_lock_paths(context) -> list[Path]:
    # One lock namespace per OS user, independent of config/state overrides and cwd.
    root = Path("/tmp") / f"flowforge-locks-{os.getuid()}"
    def keyed(kind, path):
        digest = hashlib.sha256(os.fsencode(path.resolve())).hexdigest()
        return root / f"{kind}-{digest}.lock"
    return [keyed("workspace", context.workspace), keyed("queue", context.tasks_file), context.runs_dir / ".flowforge.lock"]


class ExecutionLocks:
    def __init__(self, handles):
        self.handles = handles

    def close(self):
        for handle in reversed(self.handles):
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        self.handles.clear()


def acquire_execution(context):
    from pipeline_runner import acquire_lock
    handles = []
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


def busy(context) -> bool:
    return any(lock_held(path) for path in execution_lock_paths(context))


def update_task(path: Path, task_id: str, updates: dict, remove=()) -> None:
    import pipeline_runner as pr
    with file_transaction(path):
        tasks = pr.read_tasks_jsonl(path)
        index = pr.find_task_index(tasks, task_id)
        if index is None:
            raise ValueError(f"Task no longer exists: {task_id}")
        row = tasks[index]
        if "outputs" in updates:
            updates = {**updates, "outputs": {**row.get("outputs", {}), **updates["outputs"]}}
        row.update(updates)
        for key in remove:
            row.pop(key, None)
        pr.write_tasks_jsonl(path, tasks)


def append_history(path: Path, task_id: str, attempt: dict) -> None:
    import pipeline_runner as pr
    with file_transaction(path):
        tasks = pr.read_tasks_jsonl(path)
        index = pr.find_task_index(tasks, task_id)
        if index is not None:
            tasks[index].setdefault("history", []).append(attempt)
            pr.write_tasks_jsonl(path, tasks)
