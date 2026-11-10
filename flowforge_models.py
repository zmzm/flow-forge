"""Shared execution results and normalized workflow contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, TypedDict

Event = dict[str, Any]
EventCallback = Callable[[Event], None] | None


class _StepRequired(TypedDict):
    id: str
    label: str
    agent: str


class StepConfig(_StepRequired, total=False):
    model: str
    agents: list[str]


class StepResult(NamedTuple):
    return_code: int
    events_summary: str
    session_id: str | None
    timed_out: bool


@dataclass
class RunResult:
    ok: bool
    task_id: str | None
    session_id: str | None
    log_path: Path | None
    message: str
    stage: str | None = None
    attempt_id: str | None = None
