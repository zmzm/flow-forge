"""Shared execution ownership for CLI batches and interactive task attempts."""

from __future__ import annotations

import shutil
import time

import pipeline_runner as pr
from flowforge_config import RuntimeContext
from flowforge_storage import ExecutionLocks, acquire_execution, append_history


class ConfigurationError(ValueError):
    pass


def preflight(
    context: RuntimeContext, task_id: str | None = None, comprehensive: bool = True
) -> dict:
    from flowforge_agents import validate_agents
    from flowforge_storage import validate_queue
    from flowforge_sync import discover

    issues = []
    if not context.workspace.is_dir():
        issues.append(f"Workspace directory not found: {context.workspace}")
    if not shutil.which(context.opencode):
        issues.append(f"OpenCode executable not found: {context.opencode}")
    if context.tasks_dir and not context.tasks_dir.is_dir():
        issues.append(f"Task source directory not found: {context.tasks_dir}")
    try:
        if comprehensive and context.tasks_dir:
            discover(context.tasks_dir)
        tasks = pr.read_tasks_jsonl(context.tasks_file) if context.tasks_file.exists() else []
        if not context.tasks_dir and not context.tasks_file.is_file():
            issues.append(f"Queue not found: {context.tasks_file}")
        validate_queue(tasks)
        if not comprehensive:
            max_month = (
                pr.current_month_number() if context.month_gate and task_id is None else None
            )
            index = (
                pr.find_task_index(tasks, task_id)
                if task_id is not None
                else pr.find_next_todo(tasks, max_month)
            )
            if task_id is not None and index is None:
                raise ValueError(f"Task id not found: {task_id}")
            tasks = [tasks[index]] if index is not None else []
        for task in tasks:
            path = pr.resolve_file(task["task_file"], context.workspace)
            if not path.is_file() or task.get("source_missing"):
                issues.append(f"{task['id']}: source missing: {path}")
            if task.get("source_changed"):
                issues.append(f"{task['id']}: source changed; reset explicitly")
        for configured_task in [None] + tasks if comprehensive else tasks:
            steps = context.steps(configured_task)
            agents = pr.task_agents(configured_task or {}, steps)
            requested = pr.task_run_step_ids(configured_task or {})
            if requested and set(requested) - {step["id"] for step in steps}:
                raise ValueError(f"Unknown run_steps: {requested}")
            if not context.legacy:
                validate_agents(context, agents.values())
    except (OSError, ValueError) as exc:
        issues.append(str(exc))
    return {"ok": not issues, "issues": sorted(set(issues))}


class Execution:
    """An execution lease owns locks, stale recovery and durable attempt history."""

    def __init__(self, context: RuntimeContext, locks: ExecutionLocks) -> None:
        self.context = context
        self._locks: ExecutionLocks | None = locks
        self._recovered = False

    @classmethod
    def open(cls, context: RuntimeContext) -> Execution | None:
        locks = acquire_execution(context)
        return cls(context, locks) if locks is not None else None

    def _require_open(self) -> None:
        if self._locks is None:
            raise RuntimeError("Execution lease is closed")

    def recover(self) -> list[str]:
        self._require_open()
        if not self._recovered:
            recovered = pr.recover_stale_running_tasks(self.context.tasks_file)
            self._recovered = True
            return recovered
        return []

    def run_task(
        self, task_id: str, callback: pr.EventCallback = None, control: pr.RunControl | None = None
    ) -> pr.RunResult:
        self.recover()
        report = preflight(self.context, task_id, comprehensive=False)
        if not report["ok"]:
            raise ConfigurationError("; ".join(report["issues"]))
        started = time.monotonic()
        result = None
        error = None
        attempt = None

        def observe(event):
            nonlocal attempt
            if event.get("kind") == "run_start":
                attempt = event
            if callback:
                callback(event)

        try:
            result = pr.run_selected_task(
                task_id, callback=observe, control=control, context=self.context
            )
            return result
        except BaseException as exc:
            error = exc
            raise
        finally:
            if attempt is not None:
                rows = pr.read_tasks_jsonl(self.context.tasks_file)
                index = pr.find_task_index(rows, task_id)
                row = rows[index] if index is not None else {}
                append_history(
                    self.context.tasks_file,
                    task_id,
                    {
                        "attempt_id": attempt["attempt_id"],
                        "ok": result.ok if result else False,
                        "message": result.message if result else f"{type(error).__name__}: {error}",
                        "log_path": attempt["log_path"],
                        "session_id": result.session_id if result else row.get("session_id"),
                        "duration_sec": time.monotonic() - started,
                    },
                )

    def close(self) -> None:
        if self._locks is not None:
            self._locks.close()
            self._locks = None

    def __enter__(self) -> Execution:
        self._require_open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
