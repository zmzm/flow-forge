#!/usr/bin/env python3
"""
Headless batch runner around pipeline_runner.
Adds batch limits, single-instance locking, lifecycle logging, and notifications.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pipeline_runner as pr
from flowforge_execution import ConfigurationError
from notifications import Notifier
from notifications.base import parse_bool

logger = logging.getLogger("flowforge.runner")

acquire_lock = pr.acquire_lock
release_lock = pr.release_lock

DEFAULT_MAX_TASKS_PER_RUN = 1
DEFAULT_STOP_ON_FAILURE = True
DEFAULT_MAX_RUNTIME_SECONDS = 14400

EXIT_OK = 0
EXIT_TASK_FAILED = 1

TITLE_MAX_CHARS = 100
TITLE_READ_CHARS = 4096
LOG_TEXT_LIMIT = 300


@dataclass
class BatchConfig:
    max_tasks: int = DEFAULT_MAX_TASKS_PER_RUN
    stop_on_failure: bool = DEFAULT_STOP_ON_FAILURE
    max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS


@dataclass
class TaskOutcome:
    task_id: str
    title: str | None
    ok: bool
    message: str
    log_path: str | None
    duration_sec: float
    attempt_id: str | None = None
    session_id: str | None = None


@dataclass
class BatchOutcome:
    outcomes: list[TaskOutcome] = field(default_factory=list)
    reason: str = "queue_empty"
    duration_sec: float = 0.0

    @property
    def completed(self) -> int:
        return sum(1 for o in self.outcomes if o.ok)

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if not o.ok)

    @property
    def exit_code(self) -> int:
        return EXIT_TASK_FAILED if self.failed else EXIT_OK


def _int_env(env: dict[str, str], name: str, default: int, minimum: int) -> int:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got: {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got: {value}")
    return value


def batch_config_from_env(
    env: dict[str, str] | None = None,
    max_tasks_override: int | None = None,
) -> BatchConfig:
    env = dict(os.environ if env is None else env)
    if max_tasks_override is not None:
        if max_tasks_override < 1:
            raise ValueError("--max-tasks must be >= 1")
        max_tasks = max_tasks_override
    else:
        max_tasks = _int_env(env, "FLOWFORGE_MAX_TASKS_PER_RUN", DEFAULT_MAX_TASKS_PER_RUN, 1)
    stop_raw = str(env.get("FLOWFORGE_STOP_ON_FAILURE", "")).strip()
    return BatchConfig(
        max_tasks=max_tasks,
        stop_on_failure=parse_bool(stop_raw) if stop_raw else DEFAULT_STOP_ON_FAILURE,
        max_runtime_seconds=_int_env(
            env, "FLOWFORGE_MAX_RUNTIME_SECONDS", DEFAULT_MAX_RUNTIME_SECONDS, 0
        ),
    )


def task_title(task: dict[str, Any], project_dir: Path) -> str | None:
    """Best-effort task title: first Markdown heading of the task file."""
    raw = task.get("task_file")
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = project_dir / path
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            chunk = fh.read(TITLE_READ_CHARS)
    except OSError:
        return None
    for line in chunk.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("#"):
            title = text.lstrip("#").strip()
            if title:
                return title[:TITLE_MAX_CHARS]
        break
    return None


class _RunTracker:
    """Event callback that records the current stage/error and logs lifecycle events."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.stage: str | None = None
        self.error: str | None = None
        self.log_path: str | None = None
        self.attempt_id: str | None = None

    def __call__(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "run_start":
            self.log_path = str(event.get("log_path") or "") or None
            self.attempt_id = event.get("attempt_id")
            logger.info(
                "Task %s started (mode=%s, steps=%s, log=%s)",
                self.task_id,
                event.get("run_mode"),
                event.get("steps_total"),
                self.log_path or "-",
            )
        elif kind == "step_start":
            self.stage = str(event.get("label") or "") or None
            logger.info(
                "Stage started: %s (%s/%s)",
                self.stage,
                int(event.get("index", 0)) + 1,
                event.get("total", "?"),
            )
        elif kind == "step_done":
            logger.info("Stage completed: %s", event.get("label"))
        elif kind == "error":
            self.error = str(event.get("text") or "") or None
            logger.warning("Pipeline error: %s", (self.error or "")[:LOG_TEXT_LIMIT])
        elif kind == "run_done":
            logger.info("Task %s finished ok=%s", self.task_id, bool(event.get("ok")))


def run_batch(
    config: BatchConfig,
    notifier: Notifier,
    task_id: str | None = None,
    run_task: Callable[..., pr.RunResult] | None = None,
    clock: Callable[[], float] = time.monotonic,
    now: datetime | None = None,
    context=None,
    quiet: bool = False,
    execution=None,
) -> BatchOutcome:
    """Process queued tasks. With task_id set, runs exactly that one task.

    Automatic selection only picks tasks for the current month (task id
    prefix f<N>, N = months since MONTH_BASE_YEAR); ids without that prefix
    stay eligible. When only future-month tasks remain, the batch stops.

    Owns execution locks unless an existing execution lease is supplied.
    Injected run_task callbacks remain a scheduling-only test seam.
    """
    if execution is not None:
        if context is not None and context != execution.context:
            raise ValueError("Batch context differs from its execution lease")
        context = execution.context
    if execution is None and run_task is None:
        from flowforge_config import legacy_context
        from flowforge_execution import Execution

        context = context or legacy_context(load_dotenv=False)
        execution = Execution.open(context)
        if execution is None:
            return BatchOutcome(reason="busy")
        try:
            return run_batch(
                config,
                notifier,
                task_id,
                clock=clock,
                now=now,
                context=context,
                quiet=quiet,
                execution=execution,
            )
        finally:
            execution.close()
    tasks_file = context.tasks_file if context else pr.TASKS_FILE
    project_dir = context.workspace if context else pr.PROJECT_DIR
    if tasks_file is None or project_dir is None:
        raise ValueError("Legacy mode requires PROJECT_DIR and TASKS_FILE, or select a project")
    project_fields = {"project": context.project_id} if context and not context.legacy else {}
    run_one = run_task or execution.run_task
    single = task_id is not None
    max_month = (
        None if single or (context and not context.month_gate) else pr.current_month_number(now)
    )
    started = clock()
    outcomes: list[TaskOutcome] = []
    reason = "queue_empty"
    processed = 0

    try:
        recovered = execution.recover() if execution else pr.recover_stale_running_tasks(tasks_file)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(f"Cannot read tasks file {tasks_file}: {exc}") from exc
    if recovered:
        logger.info("Recovered stale running tasks (marked failed): %s", ", ".join(recovered))

    while True:
        if single:
            if processed >= 1:
                reason = "completed"
                break
        else:
            if processed >= config.max_tasks:
                reason = "max_tasks"
                break
            if (
                processed > 0
                and config.max_runtime_seconds
                and (clock() - started) >= config.max_runtime_seconds
            ):
                reason = "max_runtime"
                logger.info(
                    "Max runtime (%ss) reached; not starting another task",
                    config.max_runtime_seconds,
                )
                break

        try:
            tasks = pr.read_tasks_jsonl(tasks_file)
        except (FileNotFoundError, ValueError) as exc:
            raise ValueError(f"Cannot read tasks file {tasks_file}: {exc}") from exc

        idx = (
            pr.find_task_index(tasks, task_id)
            if task_id is not None
            else pr.find_next_todo(tasks, max_month)
        )
        if idx is None:
            if single:
                raise ValueError(f"Task id not found: {task_id}")
            if any(t.get("status", "todo") == "todo" for t in tasks):
                reason = "month_gated"
            else:
                reason = "queue_empty"
            break

        task = tasks[idx]
        selected_id = str(task.get("id") or f"task{idx + 1}")
        title = task_title(task, project_dir)
        logger.info("Task selected: %s", selected_id)
        notifier.task_started(
            {
                **project_fields,
                "id": selected_id,
                "title": title,
                "status": task.get("status"),
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
        )

        tracker = _RunTracker(selected_id)
        task_started_at = clock()
        result: pr.RunResult | None = None
        failure: BaseException | None = None
        try:
            result = run_one(task_id=selected_id, callback=tracker)
        except ConfigurationError:
            raise
        except Exception as exc:
            failure = exc
            logger.exception("Task %s raised an exception", selected_id)
        duration = clock() - task_started_at

        if result is not None and result.ok:
            message = result.message
            log_path = str(result.log_path) if result.log_path else None
            if not quiet:
                print(message)
                if log_path:
                    print(f"Log: {log_path}")
                if result.session_id:
                    print(f"Session: {result.session_id}")
            notifier.task_completed(
                {**project_fields, "id": selected_id, "title": title},
                {"status": "done", "duration_sec": duration, "log_path": log_path},
            )
            outcomes.append(
                TaskOutcome(
                    selected_id,
                    title,
                    True,
                    message,
                    log_path,
                    duration,
                    result.attempt_id,
                    result.session_id,
                )
            )
        else:
            if result is not None:
                message = result.message
                log_path = str(result.log_path) if result.log_path else tracker.log_path
            else:
                message = f"{type(failure).__name__}: {failure}"
                log_path = tracker.log_path
            logger.error("Task %s failed: %s", selected_id, message[:LOG_TEXT_LIMIT])
            stage = result.stage if (result is not None and result.stage) else tracker.stage
            notifier.task_failed(
                {**project_fields, "id": selected_id, "title": title},
                {
                    "stage": stage,
                    "message": tracker.error or message,
                    "duration_sec": duration,
                    "log_path": log_path,
                },
            )
            outcomes.append(
                TaskOutcome(
                    selected_id,
                    title,
                    False,
                    message,
                    log_path,
                    duration,
                    result.attempt_id if result else tracker.attempt_id,
                    result.session_id if result else None,
                )
            )

        processed += 1
        if not outcomes[-1].ok and config.stop_on_failure:
            reason = "stop_on_failure"
            break

    duration = clock() - started
    if outcomes:
        notifier.run_completed(
            {
                **project_fields,
                "reason": reason,
                "results": [{"id": o.task_id, "title": o.title, "ok": o.ok} for o in outcomes],
                "completed": sum(1 for o in outcomes if o.ok),
                "failed": sum(1 for o in outcomes if not o.ok),
                "duration_sec": duration,
            }
        )
    elif reason == "month_gated":
        held = sum(1 for t in tasks if t.get("status", "todo") == "todo")
        message = f"No active tasks for month f{max_month}; {held} future task(s) on hold."
        if not quiet:
            print(message)
        logger.info("Month gate: stopping (%s)", message)
        notifier.run_completed(
            {
                **project_fields,
                "results": [],
                "completed": 0,
                "failed": 0,
                "duration_sec": duration,
                "reason": reason,
                "message": message,
            }
        )
    elif reason == "queue_empty":
        if not quiet:
            print("No TODO tasks.")
    logger.info(
        "Batch finished: reason=%s processed=%d completed=%d failed=%d duration=%.0fs",
        reason,
        len(outcomes),
        sum(1 for o in outcomes if o.ok),
        sum(1 for o in outcomes if not o.ok),
        duration,
    )
    return BatchOutcome(outcomes, reason, duration)
