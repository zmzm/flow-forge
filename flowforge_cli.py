"""Versioned foreground CLI for manual, Hermes and scheduled operation."""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

import pipeline_runner as pr
from flowforge_config import (RuntimeContext, add_project, config_home, identifier,
                              list_projects, resolve_context)
from flowforge_storage import acquire_execution, busy

SCHEMA_VERSION = 1


def selection(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--project")
    group.add_argument("--project-config")
    parser.add_argument("--workflow", help="Named workflow override for this invocation")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--tasks-file", type=Path)
    parser.add_argument("--runs-dir", type=Path)
    parser.add_argument("--step-timeout", type=int)
    parser.add_argument("--heartbeat", type=int)


def output_option(parser):
    parser.add_argument("--json", action="store_true", help="Versioned JSON on stdout; diagnostics on stderr")


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    projects = sub.add_parser("project")
    project_sub = projects.add_subparsers(dest="action", required=True)
    listing = project_sub.add_parser("list")
    output_option(listing)
    show = project_sub.add_parser("show")
    show.add_argument("name")
    output_option(show)
    add = project_sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--workspace", required=True, type=Path)
    source = add.add_mutually_exclusive_group(required=True)
    source.add_argument("--tasks-dir", type=Path)
    source.add_argument("--tasks-file", type=Path)
    add.add_argument("--workflow", default=None)
    add.add_argument("--workflow-file", type=Path)
    add.add_argument("--runs-dir", type=Path)
    add.add_argument("--month-gate", action="store_true", default=None)
    output_option(add)
    for name in ("doctor", "sync", "run", "status", "reset"):
        cmd = sub.add_parser(name)
        selection(cmd)
        output_option(cmd)
        if name == "sync":
            cmd.add_argument("--dry-run", action="store_true")
        if name == "run":
            cmd.add_argument("--sync", action="store_true")
            cmd.add_argument("--task-id")
            cmd.add_argument("--max-tasks", type=int)
            cmd.add_argument("--max-runtime", type=int)
        if name == "status":
            cmd.add_argument("--db", type=Path)
        if name == "reset":
            cmd.add_argument("--task-id", required=True)
    agent = sub.add_parser("agent")
    agents = agent.add_subparsers(dest="action", required=True)
    for name in ("list", "show", "import"):
        cmd = agents.add_parser(name)
        output_option(cmd)
        if name == "import":
            cmd.add_argument("source", type=Path)
            cmd.add_argument("--library", type=Path)
            cmd.add_argument("--resource", action="append", default=[], metavar="SOURCE=DEST")
            cmd.add_argument("--path-map", action="append", default=[], metavar="OLD=NEW", help="Explicit reference substitution; supports {{workspace}} and {{artifacts_dir}}")
            cmd.add_argument("--apply", action="store_true", help="Default is a read-only preview")
        else:
            selection(cmd)
            if name == "show":
                cmd.add_argument("agent_id")
    workflow = sub.add_parser("workflow")
    workflows = workflow.add_subparsers(dest="action", required=True)
    for name in ("list", "show", "save"):
        cmd = workflows.add_parser(name)
        output_option(cmd)
        if name != "list":
            cmd.add_argument("name")
        if name == "save":
            cmd.add_argument("--from-file", required=True, type=Path)
            cmd.add_argument("--replace", action="store_true")
    return root


def context_for(args, allow_legacy=False):
    if not args.project and not args.project_config and not allow_legacy:
        raise ValueError("Select --project or --project-config explicitly")
    return resolve_context(args.project, args.project_config, {
        "workflow_override": args.workflow, "workspace": args.workspace, "tasks_file": args.tasks_file,
        "runs_dir": args.runs_dir, "step_timeout": args.step_timeout, "heartbeat": args.heartbeat,
    })


def preflight(context) -> dict:
    from flowforge_agents import validate_agents
    from flowforge_sync import discover, validate_queue
    issues = []
    if not context.workspace.is_dir():
        issues.append(f"Workspace directory not found: {context.workspace}")
    if not shutil.which(context.opencode):
        issues.append(f"OpenCode executable not found: {context.opencode}")
    try:
        if context.tasks_dir:
            discover(context.tasks_dir)
        tasks = pr.read_tasks_jsonl(context.tasks_file) if context.tasks_file.exists() else []
        if not context.tasks_dir and not context.tasks_file.is_file():
            issues.append(f"Queue not found: {context.tasks_file}")
        validate_queue(tasks)
        for task in tasks:
            path = pr.resolve_file(task["task_file"], context.workspace)
            if not path.is_file() or task.get("source_missing"):
                issues.append(f"{task['id']}: source missing: {path}")
            if task.get("source_changed"):
                issues.append(f"{task['id']}: source changed; reset explicitly")
        for task in [None] + tasks:
            steps = context.steps(task)
            agents = pr.task_agents(task or {}, steps)
            requested = pr.task_run_step_ids(task or {})
            if requested and set(requested) - {step["id"] for step in steps}:
                raise ValueError(f"Unknown run_steps: {requested}")
            if not context.legacy:
                validate_agents(context, agents.values())
    except (OSError, ValueError) as exc:
        issues.append(str(exc))
    return {"ok": not issues, "issues": sorted(set(issues))}


def execute(args, context) -> tuple[dict, int]:
    import headless_runner as hr
    from notifications import build_notifier
    from flowforge_sync import sync
    if context.legacy:
        config = hr.batch_config_from_env(context.notification_env, args.max_tasks)
    else:
        config = hr.BatchConfig(args.max_tasks if args.max_tasks is not None else context.max_tasks,
                                context.stop_on_failure, context.max_runtime)
    if args.max_runtime is not None:
        config.max_runtime_seconds = args.max_runtime
    if config.max_tasks < 1 or config.max_runtime_seconds < 0:
        raise ValueError("max-tasks must be >= 1 and max-runtime >= 0")
    notifier = build_notifier(context.notification_env)
    lock = acquire_execution(context)
    if lock is None:
        return {"reason": "busy", "busy": True, "outcomes": [], "completed": 0, "failed": 0}, 0
    try:
        if args.sync:
            sync(context)
        report = preflight(context)
        if not report["ok"]:
            return {"reason": "configuration_error", **report}, 2
        run_id = uuid.uuid4().hex
        outcome = hr.run_batch(config, notifier, task_id=args.task_id, context=context, quiet=args.json)
        return {**asdict(outcome), "run_id": run_id, "busy": False, "completed": outcome.completed, "failed": outcome.failed}, outcome.exit_code
    finally:
        lock.close()


def dispatch(args, allow_legacy=False):
    from flowforge_agents import catalog, import_library
    from flowforge_sync import reset_task, sync
    from flowforge_storage import atomic_json, file_transaction
    context = None
    if args.command == "project":
        if args.action == "list":
            return {"projects": list_projects()}, 0, context
        if args.action == "add":
            values = {key: getattr(args, key) for key in ("workspace", "tasks_dir", "tasks_file", "workflow", "workflow_file", "runs_dir", "month_gate")}
            path = add_project(args.name, values)
            return {"config_file": str(path)}, 0, context
        context = resolve_context(args.name)
        return {"configuration": context.public()}, 0, context
    if args.command == "workflow":
        directory = config_home() / "workflows"
        if args.action == "list":
            return {"workflows": sorted({"default"} | {path.stem for path in directory.glob("*.json")})}, 0, None
        name = identifier(args.name)
        target = directory / f"{name}.json"
        if args.action == "save":
            steps = pr.read_step_configs(args.from_file.resolve()) if args.from_file.is_file() else None
            if steps is None:
                raise ValueError(f"Workflow input missing: {args.from_file}")
            with file_transaction(target):
                if target.exists() and not args.replace:
                    raise ValueError("Workflow exists; use --replace to update a shared workflow explicitly")
                atomic_json(target, {"steps": steps})
        else:
            if not target.exists() and name == "default":
                target = Path(__file__).with_name("steps.json")
            if not target.exists():
                raise ValueError(f"Unknown workflow: {name}")
        return {"workflow": name, "path": str(target), "steps": pr.read_step_configs(target)}, 0, None
    if args.command == "agent" and args.action == "import":
        resources = {}
        for value in args.resource:
            if "=" not in value:
                raise ValueError("--resource expects SOURCE=DEST")
            source, dest = value.split("=", 1)
            resources[str(Path(source).expanduser().resolve())] = dest
        mappings = {}
        for value in args.path_map:
            if "=" not in value or not value.split("=", 1)[0]:
                raise ValueError("--path-map expects non-empty OLD=NEW")
            old, new = value.split("=", 1)
            mappings[old] = new
        result = import_library(args.source, args.library or config_home() / "library", resources, args.apply, mappings)
        return result, 0 if result["ready"] else 2, None
    if args.command == "agent" and not args.project and not args.project_config:
        # Central-only inspection needs no workspace or queue.
        context = RuntimeContext("central", Path("/nonexistent-flowforge-workspace"), Path("/nonexistent-flowforge-queue"), Path("/nonexistent-flowforge-runs"), config_home(), agents_dir=config_home() / "library")
    else:
        context = context_for(args, allow_legacy)
    if args.command == "agent":
        entries = catalog(context)
        if args.action == "show":
            item = next((item for item in entries if item["id"] == args.agent_id), None)
            if item is None:
                raise ValueError(f"Agent not found: {args.agent_id}")
            return {"agent": item}, 0, context
        return {"agents": entries}, 0, context
    if args.command == "doctor":
        result = preflight(context)
        return result, 0 if result["ok"] else 2, context
    if args.command == "sync":
        return sync(context, args.dry_run), 0, context
    if args.command == "reset":
        lock = acquire_execution(context)
        if lock is None:
            return {"reason": "busy", "busy": True}, 0, context
        try:
            reset_task(context, args.task_id)
        finally:
            lock.close()
        return {"reset": args.task_id}, 0, context
    if args.command == "status":
        from flowforge_status import status
        return status(context, args.db), 0, context
    result, code = execute(args, context)
    return result, code, context


def main(argv=None, allow_legacy=False):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        result, code, context = dispatch(args, allow_legacy)
        envelope = {"schema_version": SCHEMA_VERSION, "command": args.command,
                    "project": context.project_id if context else None,
                    "workspace": str(context.workspace) if context else None, **result}
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        envelope = {"schema_version": SCHEMA_VERSION, "command": args.command, "reason": "configuration_error", "error": str(exc)}
        code = 2
        if not args.json:
            print(f"Configuration error: {exc}", file=sys.stderr)
    if args.json:
        print(json.dumps(envelope, ensure_ascii=False, indent=2))
    elif code != 2 or "error" not in envelope:
        print(json.dumps(envelope, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
