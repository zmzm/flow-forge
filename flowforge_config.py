"""Explicit project configuration. Reading configuration never creates state."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from flowforge_models import StepConfig

INSTALL_DIR = Path(__file__).resolve().parent
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")
PATH_KEYS = {
    "workspace",
    "tasks_file",
    "tasks_dir",
    "runs_dir",
    "artifacts_dir",
    "agents_dir",
    "workflow_file",
}


def identifier(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid identifier: {value!r}; use letters, digits, '_' or '-'")
    return value


def absolute(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def config_home() -> Path:
    value = Path(
        os.environ.get("FLOWFORGE_CONFIG_DIR", str(Path.home() / ".config/flowforge"))
    ).expanduser()
    if not value.is_absolute():
        raise ValueError("FLOWFORGE_CONFIG_DIR must be absolute")
    return value.resolve()


def state_home() -> Path:
    value = Path(
        os.environ.get("FLOWFORGE_STATE_DIR", str(Path.home() / ".local/state/flowforge"))
    ).expanduser()
    if not value.is_absolute():
        raise ValueError("FLOWFORGE_STATE_DIR must be absolute")
    return value.resolve()


def read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def path_values(values: dict, base: Path) -> dict:
    result = dict(values)
    for key in PATH_KEYS:
        if key in result and result[key] is None:
            result.pop(key)
        if result.get(key) is not None:
            result[key] = absolute(str(result[key]), base)
    if result.get("opencode_bin") and "/" in result["opencode_bin"]:
        result["opencode_bin"] = str(absolute(result["opencode_bin"], base))
    return result


def positive(value: Any, label: str) -> int | None:
    try:
        if isinstance(value, (bool, float)):
            raise ValueError
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be an integer") from None
    return number if number > 0 else None


@dataclass(frozen=True)
class RuntimeContext:
    project_id: str
    workspace: Path
    tasks_file: Path
    runs_dir: Path
    config_dir: Path
    tasks_dir: Path | None = None
    artifacts_dir: Path | None = None
    workflow: str = "default"
    workflow_file: Path | None = None
    workflow_override: str | None = None
    agents_dir: Path | None = None
    project_agents: bool = False
    agent_paths: dict = field(default_factory=dict)
    step_timeout: int | None = 3600
    heartbeat: int | None = 30
    month_gate: bool = False
    max_tasks: int = 1
    stop_on_failure: bool = True
    max_runtime: int = 14400
    opencode: str = "opencode"
    notification_env: dict = field(default_factory=dict, repr=False)
    legacy: bool = False
    config_file: Path | None = None

    def workflow_path(self, task: dict | None = None) -> Path:
        selected = self.workflow_override or (task or {}).get("workflow")
        if selected:
            return self.named_workflow(str(selected))
        if self.workflow_file:
            return self.workflow_file
        return self.named_workflow(self.workflow)

    def named_workflow(self, name: str) -> Path:
        identifier(name)
        path = self.config_dir / "workflows" / f"{name}.json"
        if path.exists():
            return path
        if name == "default":
            return INSTALL_DIR / "steps.json"
        raise ValueError(f"Workflow not found: {name} ({path})")

    def steps(self, task: dict | None = None) -> list[StepConfig]:
        import flowforge_workflows as workflows

        path = self.workflow_path(task)
        if not path.is_file():
            if self.legacy:
                return [step.copy() for step in workflows.DEFAULT_STEP_CONFIGS]
            raise ValueError(f"Workflow file not found: {path}")
        return workflows.read_step_configs(path)

    def public(self) -> dict:
        keys = (
            "project_id",
            "workspace",
            "tasks_file",
            "tasks_dir",
            "runs_dir",
            "artifacts_dir",
            "workflow",
            "workflow_file",
            "agents_dir",
            "project_agents",
            "step_timeout",
            "heartbeat",
            "month_gate",
            "max_tasks",
            "stop_on_failure",
            "max_runtime",
            "opencode",
            "legacy",
            "config_file",
        )
        result = {
            key: str(getattr(self, key))
            if isinstance(getattr(self, key), Path)
            else getattr(self, key)
            for key in keys
        }
        result["workflow_override"] = self.workflow_override
        return result


def central_defaults(root: Path) -> dict:
    path = root / "config.json"
    if not path.exists():
        return {}
    raw = read_object(path)
    defaults = raw.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("Central 'defaults' must be an object")
    return path_values(defaults, path.parent)


def resolve_context(
    project: str | None = None,
    project_config: str | None = None,
    overrides: dict | None = None,
    root: Path | None = None,
) -> RuntimeContext:
    if project and project_config:
        raise ValueError("Choose --project or --project-config, not both")
    if not project and not project_config:
        return legacy_context(overrides=overrides)
    root = (root or config_home()).resolve()
    defaults = central_defaults(root)
    if project_config:
        path = absolute(project_config, Path.cwd())
    else:
        assert project is not None
        path = root / "projects" / f"{identifier(project)}.json"
    if not path.is_file():
        raise ValueError(f"Unknown project configuration: {path}")
    raw = read_object(path)
    name = identifier(str(raw.get("id") or project or path.stem))
    if "workflow" in raw and "workflow_file" not in raw:
        defaults.pop("workflow_file", None)
    if "tasks_file" in raw and "tasks_dir" not in raw:
        defaults.pop("tasks_dir", None)
    values = {
        **defaults,
        **path_values(raw, path.parent),
        **path_values({k: v for k, v in (overrides or {}).items() if v is not None}, Path.cwd()),
    }
    if not values.get("workspace"):
        raise ValueError("Project requires 'workspace'")
    directory = values.get("tasks_dir")
    queue = values.get("tasks_file")
    mode = values.get("source_mode", "directory" if directory else "jsonl")
    if (
        mode not in {"directory", "jsonl"}
        or (mode == "directory" and not directory)
        or (mode == "jsonl" and (not queue or directory))
    ):
        raise ValueError(
            "Choose tasks_dir (directory mode) or tasks_file (JSONL mode); source_mode must agree"
        )
    state = state_home() / name
    boolean_keys = ("project_agents", "month_gate", "stop_on_failure")
    for key in boolean_keys:
        if key in values and not isinstance(values[key], bool):
            raise ValueError(f"{key} must be a JSON boolean")
    try:
        if any(isinstance(values.get(key), (bool, float)) for key in ("max_tasks", "max_runtime")):
            raise ValueError
        max_tasks = int(values.get("max_tasks", 1))
        max_runtime = int(values.get("max_runtime", 14400))
    except (TypeError, ValueError):
        raise ValueError("max_tasks and max_runtime must be integers") from None
    if (
        not isinstance(values.get("opencode_bin", "opencode"), str)
        or not values.get("opencode_bin", "opencode").strip()
    ):
        raise ValueError("opencode_bin must be a non-empty string")
    if max_tasks < 1 or max_runtime < 0:
        raise ValueError("max_tasks must be >= 1 and max_runtime >= 0")
    identifier(str(values.get("workflow", "default")))
    agent_paths = values.get("agent_paths", {})
    if not isinstance(agent_paths, dict) or any(
        not isinstance(k, str) or not k or not isinstance(v, str) for k, v in agent_paths.items()
    ):
        raise ValueError("agent_paths must map non-empty literal references to replacement strings")
    return RuntimeContext(
        name,
        values["workspace"],
        queue or state / "tasks.jsonl",
        values.get("runs_dir", state / "runs"),
        root,
        tasks_dir=directory,
        artifacts_dir=values.get("artifacts_dir", state / "artifacts")
        if directory
        else values.get("artifacts_dir"),
        workflow=str(values.get("workflow", "default")),
        workflow_file=values.get("workflow_file"),
        workflow_override=values.get("workflow_override"),
        agents_dir=values.get("agents_dir", root / "library"),
        project_agents=values.get("project_agents", False),
        agent_paths=agent_paths,
        step_timeout=positive(values.get("step_timeout", 3600), "step_timeout"),
        heartbeat=positive(values.get("heartbeat", 30), "heartbeat"),
        month_gate=values.get("month_gate", False),
        max_tasks=max_tasks,
        stop_on_failure=values.get("stop_on_failure", True),
        max_runtime=max_runtime,
        opencode=values.get("opencode_bin", "opencode"),
        notification_env=notification_settings(root, values),
        config_file=path,
    )


def dotenv(path: Path) -> dict:
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def notification_settings(root: Path, values: dict) -> dict:
    # Credentials stay in the process environment or a separate private file.
    settings = dotenv(root / "notifications.env")
    settings.update({k: v for k, v in os.environ.items() if k.startswith("MATRIX_")})
    if values.get("notifications") is False:
        settings["MATRIX_ENABLED"] = "false"
    return settings


def legacy_context(overrides: dict | None = None, load_dotenv: bool = True) -> RuntimeContext:
    import pipeline_runner as pr

    env = {**(dotenv(Path.cwd() / ".env") if load_dotenv else {}), **os.environ}
    values = {k: v for k, v in (overrides or {}).items() if v is not None}
    # Module attributes are retained only for old Python callers/tests, never project selection.
    workspace = values.get("workspace") or (
        pr.PROJECT_DIR if not load_dotenv else env.get("PROJECT_DIR")
    )
    queue = values.get("tasks_file") or (
        pr.TASKS_FILE if not load_dotenv else env.get("TASKS_FILE")
    )
    if workspace is None or queue is None or str(workspace) == "" or str(queue) == "":
        raise ValueError("Legacy mode requires PROJECT_DIR and TASKS_FILE, or select --project")
    base = Path.cwd()
    queue = absolute(str(queue), base)
    return RuntimeContext(
        "legacy",
        absolute(str(workspace), base),
        queue,
        absolute(
            str(
                values.get("runs_dir")
                or (pr.RUNS_DIR if not load_dotenv else env.get("FLOWFORGE_RUNS_DIR", "runs"))
            ),
            base,
        ),
        config_home(),
        workflow_file=absolute(
            str(
                values.get("workflow_file")
                or (
                    pr.STEPS_FILE
                    if not load_dotenv and pr.STEPS_FILE
                    else queue.with_name("steps.json")
                )
            ),
            base,
        ),
        step_timeout=positive(
            values.get("step_timeout", env.get("FLOWFORGE_STEP_TIMEOUT_SECONDS", 3600)),
            "step_timeout",
        )
        if load_dotenv
        else pr.STEP_TIMEOUT_SEC,
        heartbeat=positive(env.get("FLOWFORGE_HEARTBEAT_SECONDS", 30), "heartbeat")
        if load_dotenv
        else pr.HEARTBEAT_SEC,
        month_gate=True,
        opencode=env.get("OPENCODE_BIN", "opencode"),
        notification_env=env,
        legacy=True,
    )


def list_projects(root: Path | None = None) -> list[dict]:
    root = root or config_home()
    return [
        {"id": path.stem, "config_file": str(path)}
        for path in sorted((root / "projects").glob("*.json"))
    ]


def add_project(name: str, values: dict, root: Path | None = None) -> Path:
    from flowforge_storage import atomic_json, file_transaction

    root = root or config_home()
    path = root / "projects" / f"{identifier(name)}.json"
    raw = {
        "id": name,
        **{
            k: str(v) if isinstance(v, Path) else v
            for k, v in path_values(values, Path.cwd()).items()
            if v is not None
        },
    }
    with file_transaction(path):
        if path.exists():
            raise ValueError(f"Project already registered: {name}")
        atomic_json(path, raw)
    return path


def save_project_steps(context: RuntimeContext, steps: list[StepConfig]) -> RuntimeContext:
    """TUI edits fork the selected workflow into this project's configuration."""
    import flowforge_workflows as workflows
    from flowforge_storage import atomic_json, file_transaction

    if context.legacy:
        workflows.write_step_configs(steps, context.workflow_file)
        return context
    if context.config_file is None:
        raise ValueError("Project configuration file is required to save workflow edits")
    target = (
        context.config_file.parent / (context.config_file.stem + ".flowforge") / "workflow.json"
    )
    with file_transaction(context.config_file):
        raw = read_object(context.config_file)
        atomic_json(target, {"steps": workflows.normalize_step_configs(steps)})
        raw["workflow_file"] = str(target)
        atomic_json(context.config_file, raw)
    return replace(context, workflow_file=target, workflow_override=None)
