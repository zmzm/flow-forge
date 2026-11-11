#!/usr/bin/env python3
"""
Headless pipeline runner.
Contains task selection, pipeline execution, and tasks.jsonl updates.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess as subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import flowforge_opencode as opencode
import flowforge_workflows as workflows
from flowforge_models import EventCallback as EventCallback
from flowforge_models import RunResult as RunResult
from flowforge_models import StepConfig
from flowforge_opencode import RunControl, parse_opencode_error_from_json_events
from flowforge_opencode import _proc_stat_snapshot as _proc_stat_snapshot
from flowforge_opencode import (
    parse_session_id_from_json_events as parse_session_id_from_json_events,
)
from flowforge_storage import _persist_task_update, find_task_index, read_tasks_jsonl
from flowforge_storage import acquire_lock as acquire_lock
from flowforge_storage import recover_stale_running_tasks as recover_stale_running_tasks
from flowforge_storage import release_lock as release_lock
from flowforge_storage import write_tasks_jsonl as write_tasks_jsonl
from flowforge_workflows import AGENT_ID_RE as AGENT_ID_RE
from flowforge_workflows import DEFAULT_STEP_CONFIGS as DEFAULT_STEP_CONFIGS
from flowforge_workflows import STEP_ID_RE as STEP_ID_RE
from flowforge_workflows import SUPPORTED_STEP_IDS as SUPPORTED_STEP_IDS
from flowforge_workflows import normalize_step_configs as normalize_step_configs

PROJECT_DIR = (
    Path(os.environ["PROJECT_DIR"]).expanduser() if os.environ.get("PROJECT_DIR") else None
)
TASKS_FILE = Path(os.environ["TASKS_FILE"]).expanduser() if os.environ.get("TASKS_FILE") else None
STEPS_FILE = TASKS_FILE.with_name("steps.json") if TASKS_FILE else None
RUNS_DIR = Path(os.getenv("FLOWFORGE_RUNS_DIR", "runs")).expanduser()

# Models are normally selected by each OpenCode agent's frontmatter. A model in
# steps.json is an explicit per-step override; there is no global CLI override.
MODEL: str | None = None
VARIANT: str | None = None
USE_ATTACH = False
ATTACH_URL = "http://localhost:4096"
STEP_TIMEOUT_SEC: int | None = 3600
HEARTBEAT_SEC: int | None = 30
SHARE_SESSION = False


def emit(callback: EventCallback, payload: dict[str, Any]) -> None:
    if callback:
        callback(payload)


def read_step_configs(path: Path | None = None) -> list[StepConfig]:
    return workflows.read_step_configs(path or STEPS_FILE)


def write_step_configs(step_configs: list[StepConfig], path: Path | None = None) -> None:
    workflows.write_step_configs(step_configs, path or STEPS_FILE)


def task_agents(
    task: dict[str, Any], step_configs: list[StepConfig] | None = None
) -> dict[str, str]:
    return workflows.task_agents(
        task, step_configs if step_configs is not None else read_step_configs()
    )


def agent_configured_model(agent: str, project_dir: Path | None = None) -> str | None:
    """Best-effort lookup of the model declared in an OpenCode agent file."""
    project_dir = project_dir or PROJECT_DIR or Path.cwd()
    candidates = [
        project_dir / ".opencode" / "agents" / f"{agent}.md",
        Path.home() / ".config" / "opencode" / "agents" / f"{agent}.md",
        Path.home() / ".opencode" / "agents" / f"{agent}.md",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        frontmatter = content.split("---", 2)
        if len(frontmatter) < 3:
            continue
        match = re.search(r"(?m)^model\s*:\s*['\"]?([^'\"\s#]+)", frontmatter[1])
        if match:
            return match.group(1).strip()
    return None


def task_run_step_ids(task: dict[str, Any]) -> list[str] | None:
    raw_steps = task.get("run_steps")
    if raw_steps is None:
        return None
    if isinstance(raw_steps, str):
        steps = [item.strip() for item in raw_steps.split(",") if item.strip()]
    elif isinstance(raw_steps, list):
        steps = [str(item).strip() for item in raw_steps if str(item).strip()]
    else:
        raise ValueError(
            "Task field 'run_steps' must be a list or comma-separated string if provided"
        )
    if not steps:
        return None
    duplicates = sorted({step_id for step_id in steps if steps.count(step_id) > 1})
    if duplicates:
        raise ValueError(f"Duplicate run_steps: {duplicates}")
    return steps


def select_steps_for_task(
    task: dict[str, Any], steps: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], str]:
    requested_ids = task_run_step_ids(task)
    if requested_ids:
        by_id = {str(step["id"]): step for step in steps}
        unknown = [step_id for step_id in requested_ids if step_id not in by_id]
        if unknown:
            allowed = ", ".join(str(step["id"]) for step in steps)
            raise ValueError(f"Unknown run_steps: {unknown}. Allowed: {allowed}")
        return [by_id[step_id] for step_id in requested_ids], "explicit"

    if str(task.get("status", "todo")) != "failed":
        return steps, "full"

    for index, step in enumerate(steps):
        if not checkpoint_valid(task.get("checkpoints", {}).get(step["id"]), step):
            return steps[index:], "resume"

    return [], "already_complete"


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checkpoint_valid(checkpoint, step) -> bool:
    if not isinstance(checkpoint, dict) or not step.get("fingerprint"):
        return False
    if checkpoint.get("fingerprint") != step["fingerprint"]:
        return False
    try:
        return checkpoint.get("output_hash") == file_hash(step["output_file"]) and checkpoint.get(
            "input_hash"
        ) == file_hash(step["input_file"])
    except OSError:
        return False


MONTH_BASE_YEAR = 2026
TASK_ID_MONTH_RE = re.compile(r"^f(\d+)-")


def task_month_number(task_id: Any) -> int | None:
    match = TASK_ID_MONTH_RE.match(str(task_id or ""))
    return int(match.group(1)) if match else None


def current_month_number(now: datetime | None = None) -> int:
    now = now or datetime.now()
    return (now.year - MONTH_BASE_YEAR) * 12 + now.month


def find_next_todo(tasks: list[dict[str, Any]], max_month: int | None = None) -> int | None:
    for i, t in enumerate(tasks):
        if t.get("status", "todo") != "todo":
            continue
        if max_month is not None:
            month = task_month_number(t.get("id"))
            if month is not None and month > max_month:
                continue
        return i
    return None


def resolve_file(path_like: str, base: Path) -> Path:
    p = Path(path_like).expanduser()
    if not p.is_absolute():
        p = (base / p).resolve()
    return p


def require_exists(p: Path, err_msg: str) -> None:
    if not p.exists():
        raise FileNotFoundError(f"{err_msg}: {p}")


def opencode_bin() -> str:
    bin_path = os.getenv("OPENCODE_BIN", "").strip() or "opencode"
    if "/" in bin_path:
        bin_path = str(Path(bin_path).expanduser().resolve())
    return bin_path


def build_cmd_base(
    files: list[Path],
    model: str | None = None,
    cwd: Path | None = None,
    executable: str | None = None,
) -> list[str]:
    cmd = [
        executable or opencode_bin(),
        "run",
        "--format",
        "json",
        "--print-logs",
        "--log-level",
        "ERROR",
    ]
    if cwd:
        # OpenCode may use the inherited PWD instead of the subprocess cwd when
        # resolving the project. Be explicit so sessions belong to PROJECT_DIR.
        cmd += ["--dir", str(cwd)]
    if USE_ATTACH:
        cmd += ["--attach", ATTACH_URL]
    if model:
        cmd += ["--model", model]
    if VARIANT:
        cmd += ["--variant", VARIANT]
    if SHARE_SESSION:
        cmd += ["--share"]
    for f in files:
        cmd += ["--file", str(f)]
    return cmd


def step_output_file(step_id: str, task_dir: Path) -> Path:
    fixed_names = {
        "concept": "concept-plan.md",
        "grounded": "grounded-plan.md",
        "execution": "execution.md",
    }
    return task_dir / fixed_names.get(step_id, f"{step_id}.md")


def step_message(step_id: str, label: str, output_file: Path) -> str:
    output_name = str(output_file.resolve())
    if step_id == "concept":
        return (
            "Read the attached task file and produce a CONCEPT PLAN.\n"
            "Hard requirements:\n"
            f"- Save your output ONLY to the absolute path '{output_name}'.\n"
            f"After writing the file, briefly confirm that '{output_name}' was created."
        )
    if step_id == "grounded":
        return (
            "Read ONLY the attached previous plan and produce a GROUNDED PLAN.\n"
            "Hard requirements:\n"
            f"- Save your output ONLY to '{output_name}' at that absolute path.\n"
            f"After writing the file, briefly confirm that '{output_name}' was created."
        )
    if step_id == "execution":
        return (
            "Read ONLY the attached previous plan and execute it in the repository.\n"
            "Hard requirements:\n"
            f"- At the end, save a concise execution report ONLY to '{output_name}' at that absolute path.\n"
            "The execution report must include:\n"
            "- What files were changed (paths)\n"
            "- What endpoints/DTOs were added or updated\n"
            "- How to verify manually (curl/examples)\n"
            "- Any follow-ups or known limitations\n"
            f"After writing the file, briefly confirm that '{output_name}' was created."
        )
    return (
        f"Read ONLY the attached input and produce this pipeline step: {label}.\n"
        "Hard requirements:\n"
        f"- Save your output ONLY to '{output_name}' at that absolute path.\n"
        f"After writing the file, briefly confirm that '{output_name}' was created."
    )


def build_pipeline_steps(
    *,
    task: dict[str, Any],
    task_file: Path,
    context=None,
) -> list[dict[str, Any]]:
    step_configs = context.steps(task) if context else read_step_configs()
    agents = task_agents(task, step_configs)
    task_dir = task_file.parent
    if context and context.artifacts_dir:
        from flowforge_sync import source_id

        task_dir = context.artifacts_dir / source_id(task.get("source_key", str(task["id"])))
    effective_catalog = None
    if context and not context.legacy:
        from flowforge_agents import validate_agents

        effective_catalog = validate_agents(context, agents.values())
    input_file = task_file
    revision = file_hash(task_file)
    steps: list[dict[str, Any]] = []
    for step in step_configs:
        step_id = step["id"]
        output_file = step_output_file(step_id, task_dir)
        model_override = str(step.get("model") or "").strip() or None
        configured_model = (
            effective_catalog[agents[step_id]]["model"]
            if effective_catalog is not None
            else agent_configured_model(agents[step_id], context.workspace if context else None)
        )
        steps.append(
            {
                "id": step_id,
                "label": step["label"],
                "agent": agents[step_id],
                "model": model_override,
                "display_model": model_override or configured_model or "OpenCode default",
                "model_source": "step override"
                if model_override
                else ("agent" if configured_model else "OpenCode"),
                "input_file": input_file,
                "output_file": output_file,
                "message": step_message(step_id, step["label"], output_file),
            }
        )
        definition = effective_catalog[agents[step_id]] if effective_catalog is not None else None
        signature = {key: str(value) for key, value in steps[-1].items()}
        if definition:
            from flowforge_agents import transformed

            signature["definition"] = transformed(
                context, Path(definition["path"]).read_text(encoding="utf-8")
            )
        revision = hashlib.sha256(
            (revision + json.dumps(signature, sort_keys=True)).encode()
        ).hexdigest()
        steps[-1]["fingerprint"] = revision
        input_file = output_file
    return steps


def run_opencode_step_stream(
    *,
    agent: str,
    model: str | None,
    message: str,
    files: list[Path],
    cwd: Path,
    log_path: Path,
    callback: EventCallback = None,
    control: RunControl | None = None,
    title: str | None = None,
    session_id: str | None = None,
    context=None,
    environment: dict | None = None,
) -> tuple[int, str, str | None, bool]:
    cmd = build_cmd_base(
        files, model=model, cwd=cwd, executable=context.opencode if context else None
    ) + ["--agent", agent]
    if title:
        cmd += ["--title", title]
    if session_id:
        cmd += ["--session", session_id]
    cmd.append(message)
    return opencode.run_step(
        cmd,
        cwd=cwd,
        log_path=log_path,
        callback=callback,
        control=control,
        agent=agent,
        session_id=session_id,
        environment=environment,
        timeout=context.step_timeout if context else STEP_TIMEOUT_SEC,
        heartbeat=context.heartbeat if context else HEARTBEAT_SEC,
    )


def run_selected_task(
    task_id: str | None = None,
    callback: EventCallback = None,
    control: RunControl | None = None,
    context=None,
) -> RunResult:
    if context is None:
        from flowforge_config import legacy_context

        context = legacy_context(load_dotenv=False)
    project_dir, tasks_file, runs_dir = context.workspace, context.tasks_file, context.runs_dir
    require_exists(project_dir, "project_dir does not exist")
    runs_dir.mkdir(parents=True, exist_ok=True)

    tasks = read_tasks_jsonl(tasks_file)
    idx = find_task_index(tasks, task_id) if task_id else find_next_todo(tasks)
    if idx is None:
        msg = f"Task id not found: {task_id}" if task_id else "No TODO tasks."
        return RunResult(False, task_id, None, None, msg)

    task = tasks[idx]
    selected_id = str(task.get("id") or f"task{idx + 1}")
    if "task_file" not in task:
        raise ValueError(f"Task {selected_id} missing 'task_file'")

    task_file = resolve_file(task["task_file"], project_dir)
    require_exists(task_file, "Task file not found")
    if context.tasks_dir:
        source_root = context.tasks_dir.resolve()
        if not task_file.resolve().is_relative_to(source_root):
            raise ValueError(f"Task source escapes configured tree: {task_file}")
        relative = task_file.relative_to(source_root)
        current = source_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"Task source is a symlink: {current}")
        if task.get("source_hash") != hashlib.sha256(task_file.read_bytes()).hexdigest():
            raise ValueError(f"Task source changed; sync and explicitly reset: {selected_id}")
    if task.get("source_missing"):
        raise ValueError(f"Task source disappeared; sync before running: {selected_id}")
    if task.get("source_changed"):
        raise ValueError(f"Task source changed; explicitly reset before running: {selected_id}")
    all_steps = build_pipeline_steps(
        task=task,
        task_file=task_file,
        context=context,
    )
    steps, run_mode = select_steps_for_task(task, all_steps)

    log_task_id = re.sub(r"[^A-Za-z0-9_-]", "_", selected_id)
    run_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}_{log_task_id}"
    log_path = runs_dir / f"{run_id}.log"
    from flowforge_inspection import session_prefix

    title = (
        f"{selected_id}: {task_file.name}"
        if context.legacy
        else f"{session_prefix(context)}{selected_id}: {task_file.name}"
    )
    environment = {}
    if not context.legacy:
        from flowforge_agents import prepare_opencode

        environment = prepare_opencode(context, runs_dir / (run_id + ".opencode"))
    for step in all_steps:
        step["output_file"].parent.mkdir(parents=True, exist_ok=True)
    session_id: str | None = str(task.get("session_id") or "") or None
    resume_session_id: str | None = None
    if task.get("status") == "failed":
        resume_session_id = str(task.get("session_id") or "") or None
        if steps:
            previous = task.get("checkpoints", {}).get(steps[0]["id"], {})
            revision = task.get("stage_fingerprint") or previous.get("fingerprint")
            if (task.get("stage_id") and task["stage_id"] != steps[0]["id"]) or (
                revision and revision != steps[0]["fingerprint"]
            ):
                resume_session_id = None
    if steps:
        session_id = resume_session_id
    failed_stage: str | None = None

    checkpoints = dict(task.get("checkpoints") or {})
    if steps:
        first = min(all_steps.index(step) for step in steps)
        for step in all_steps[first:]:
            checkpoints.pop(step["id"], None)
    log_path.write_text(f"Task {selected_id}, attempt {run_id}\n", encoding="utf-8")
    task["status"] = "running"
    _persist_task_update(
        tasks_file,
        selected_id,
        {
            "status": "running",
            "attempt_id": run_id,
            "last_run_log": str(log_path),
            "execution_workspace": str(project_dir),
            "execution_project": context.project_id,
            "stage": None,
            "session_id": session_id,
            "checkpoints": checkpoints,
        },
    )

    def observe(event):
        nonlocal session_id
        if event.get("kind") == "session":
            session_id = event["session_id"]
            _persist_task_update(tasks_file, selected_id, {"session_id": session_id})
        emit(callback, event)

    try:
        emit(
            callback,
            {
                "kind": "run_start",
                "task_id": selected_id,
                "attempt_id": run_id,
                "project": context.project_id,
                "model": "per-agent",
                "steps_total": len(steps),
                "run_mode": run_mode,
                "log_path": str(log_path),
            },
        )

        ok = True
        if not steps:
            if run_mode == "already_complete":
                log_path.write_text(
                    f"Task {selected_id} already has outputs for all configured steps; nothing to run.\n",
                    encoding="utf-8",
                )
                _persist_task_update(
                    tasks_file,
                    selected_id,
                    {
                        "status": "done",
                        "last_run_log": str(log_path),
                        "outputs": {
                            str(step["id"]): str(step["output_file"]) for step in all_steps
                        },
                        "stage": None,
                        "stage_id": None,
                        "stage_fingerprint": None,
                    },
                )
                emit(
                    callback,
                    {
                        "kind": "run_done",
                        "ok": True,
                        "task_id": selected_id,
                        "session_id": session_id,
                    },
                )
                return RunResult(
                    True,
                    selected_id,
                    session_id,
                    log_path,
                    f"Task {selected_id} -> already complete",
                    attempt_id=run_id,
                )
            ok = False
            emit(callback, {"kind": "error", "text": "No steps selected"})

        for step_index, step in enumerate(steps):
            if not ok:
                break
            if control and control.stop_requested:
                ok = False
                emit(callback, {"kind": "error", "text": "Run stopped by user"})
                break

            label = str(step["label"])
            failed_stage = label
            _persist_task_update(
                tasks_file,
                selected_id,
                {
                    "stage": label,
                    "stage_id": step["id"],
                    "stage_fingerprint": step["fingerprint"],
                },
            )
            input_file: Path = step["input_file"]
            output_file: Path = step["output_file"]
            agent = str(step["agent"])
            model = str(step.get("model") or "")

            emit(
                callback,
                {
                    "kind": "step_start",
                    "index": step_index,
                    "label": label,
                    "agent": agent,
                    "model": str(step.get("display_model") or "OpenCode default"),
                    "model_source": str(step.get("model_source") or "OpenCode"),
                    "total": len(steps),
                },
            )

            if not input_file.exists():
                ok = False
                emit(callback, {"kind": "error", "text": f"Missing input file: {input_file}"})
                break

            input_hash = file_hash(input_file)

            rc, step_output, sid, step_timed_out = run_opencode_step_stream(
                agent=agent,
                model=model,
                message=str(step["message"]),
                files=[input_file],
                cwd=project_dir,
                log_path=log_path,
                callback=observe,
                control=control,
                title=title,
                session_id=resume_session_id,
                context=context,
                environment=environment,
            )
            resume_session_id = None
            session_id = sid or session_id
            if session_id:
                _persist_task_update(tasks_file, selected_id, {"session_id": session_id})

            provider_error = parse_opencode_error_from_json_events(step_output)
            if (
                rc != 0
                or provider_error
                or step_timed_out
                or (control and control.stop_requested)
                or not output_file.is_file()
            ):
                ok = False
                if control and control.stop_requested:
                    emit(callback, {"kind": "error", "text": "Run stopped by user"})
                    break
                if step_timed_out:
                    error_text = (
                        f"Step '{label}' timed out after {context.step_timeout}s and was terminated"
                    )
                elif provider_error:
                    error_text = f"Step '{label}' failed: {provider_error}"
                elif rc != 0:
                    error_text = f"Step '{label}' failed: OpenCode exited with code {rc}"
                else:
                    error_text = f"Step '{label}' completed but did not create {output_file}"
                emit(callback, {"kind": "error", "text": error_text})
                break

            checkpoints[step["id"]] = {
                "fingerprint": step["fingerprint"],
                "input_hash": input_hash,
                "output_hash": file_hash(output_file),
                "attempt_id": run_id,
            }
            _persist_task_update(
                tasks_file,
                selected_id,
                {
                    "checkpoints": checkpoints,
                    "outputs": {step["id"]: str(output_file)},
                },
            )
            failed_stage = None
            emit(
                callback,
                {
                    "kind": "step_done",
                    "index": step_index,
                    "label": label,
                    "output_file": str(output_file),
                },
            )
        if not ok:
            _persist_task_update(
                tasks_file,
                selected_id,
                {"status": "failed", "last_run_log": str(log_path), "session_id": session_id},
            )
            emit(
                callback,
                {"kind": "run_done", "ok": False, "task_id": selected_id, "session_id": session_id},
            )
            if control and control.stop_requested:
                return RunResult(
                    False,
                    selected_id,
                    session_id,
                    log_path,
                    f"Task {selected_id} -> stopped",
                    failed_stage,
                    run_id,
                )
            return RunResult(
                False,
                selected_id,
                session_id,
                log_path,
                f"Task {selected_id} -> failed",
                failed_stage,
                run_id,
            )

        existing_outputs = task.get("outputs")
        outputs = existing_outputs if isinstance(existing_outputs, dict) else {}
        outputs.update({str(step["id"]): str(step["output_file"]) for step in steps})
        _persist_task_update(
            tasks_file,
            selected_id,
            {
                "status": "done",
                "session_id": session_id,
                "last_run_log": str(log_path),
                "outputs": outputs,
                "stage": None,
                "stage_id": None,
                "stage_fingerprint": None,
            },
        )
        emit(
            callback,
            {"kind": "run_done", "ok": True, "task_id": selected_id, "session_id": session_id},
        )
        return RunResult(
            True,
            selected_id,
            session_id,
            log_path,
            f"Task {selected_id} -> done",
            attempt_id=run_id,
        )
    except BaseException:
        _persist_task_update(
            tasks_file,
            selected_id,
            {
                "status": "failed",
                "last_run_log": str(log_path),
                "session_id": session_id,
            },
        )
        raise
