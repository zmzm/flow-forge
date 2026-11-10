"""Workflow parsing and persistence with explicit file selection."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from flowforge_models import StepConfig

DEFAULT_STEP_CONFIGS: list[StepConfig] = [
    {"id": "concept", "label": "Business plan", "agent": "concept-plan"},
    {"id": "grounded", "label": "Technical plan", "agent": "grounded-hard"},
    {"id": "execution", "label": "Execution", "agent": "execution-plan"},
]
SUPPORTED_STEP_IDS = {step["id"] for step in DEFAULT_STEP_CONFIGS}
STEP_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$")


def normalize_step_configs(raw_steps: Any, source: str = "steps config") -> list[StepConfig]:
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError(f"{source} must contain a non-empty 'steps' list")

    configs: list[StepConfig] = []
    seen = set()
    defaults = {step["id"]: step for step in DEFAULT_STEP_CONFIGS}
    for index, raw_step in enumerate(raw_steps, 1):
        if isinstance(raw_step, str):
            step_id = raw_step.strip()
            default = defaults.get(step_id, {"id": step_id, "label": step_id, "agent": ""})
            label = str(default.get("label") or step_id)
            agent = str(default.get("agent") or "").strip()
        elif isinstance(raw_step, dict):
            step_id = str(raw_step.get("id", "")).strip()
            default = defaults.get(step_id, {"id": step_id, "label": step_id, "agent": ""})
            label = str(raw_step.get("label") or default.get("label") or step_id).strip()
            agent = str(raw_step.get("agent") or default.get("agent") or "").strip()
            model = str(raw_step.get("model") or default.get("model") or "").strip()
            extra_agents = raw_step.get("agents", [])
        else:
            raise ValueError(f"Invalid step config at index {index} in {source}")
        if isinstance(raw_step, str):
            extra_agents = []
            model = ""

        if not step_id:
            raise ValueError(f"Step id cannot be empty at index {index} in {source}")
        if not STEP_ID_RE.fullmatch(step_id):
            raise ValueError(
                f"Step id '{step_id}' in {source} must use only letters, numbers, '_' or '-'"
            )
        if step_id in seen:
            raise ValueError(f"Duplicate step id '{step_id}' in {source}")
        if not label:
            raise ValueError(f"Step label cannot be empty for '{step_id}' in {source}")
        if not agent:
            raise ValueError(f"Step agent cannot be empty for '{step_id}' in {source}")
        if not AGENT_ID_RE.fullmatch(agent):
            raise ValueError(f"Invalid agent identifier for step {step_id}: {agent!r}")
        if not isinstance(extra_agents, list):
            raise ValueError(f"Step agents must be a list for '{step_id}' in {source}")

        agents = sorted({str(item).strip() for item in extra_agents if str(item).strip()} - {agent})
        config: StepConfig = {"id": step_id, "label": label, "agent": agent}
        if model:
            config["model"] = model
        if agents:
            config["agents"] = agents
        configs.append(config)
        seen.add(step_id)

    return configs


def read_step_configs(path: Path | None = None) -> list[StepConfig]:
    if path is None or not path.exists():
        return [step.copy() for step in DEFAULT_STEP_CONFIGS]

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw_steps = raw.get("steps") if isinstance(raw, dict) else raw
    return normalize_step_configs(raw_steps, str(path))


def write_step_configs(step_configs: list[StepConfig], path: Path | None = None) -> None:
    if path is None:
        raise ValueError("No workflow file selected")
    serializable = []
    for step in step_configs:
        item: StepConfig = {
            "id": str(step["id"]),
            "label": str(step["label"]),
            "agent": str(step["agent"]),
        }
        model = str(step.get("model") or "").strip()
        if model:
            item["model"] = model
        agents = sorted(
            {str(agent).strip() for agent in step.get("agents", []) if str(agent).strip()}
            - {item["agent"]}
        )
        if agents:
            item["agents"] = agents
        serializable.append(item)
    from flowforge_storage import atomic_json, file_transaction

    with file_transaction(path):
        atomic_json(path, {"steps": serializable})


def task_agents(task: dict[str, Any], step_configs: list[StepConfig]) -> dict[str, str]:
    overrides = task.get("agents") or {}
    if not isinstance(overrides, dict):
        raise ValueError("Task field 'agents' must be an object if provided")
    result = {step["id"]: str(overrides.get(step["id"]) or step["agent"]) for step in step_configs}

    for agent in result.values():
        if not AGENT_ID_RE.fullmatch(agent):
            raise ValueError(f"Invalid agent identifier: {agent!r}")
    return result
