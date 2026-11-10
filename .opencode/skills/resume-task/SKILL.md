---
name: resume-task
description: Resume a FlowForge pipeline task that failed because of a step timeout. Use when the user says "resume <task-id>", "restart f9-d1", "continue the timed-out task", or asks to retry a task killed by the step timeout. Use ONLY for timeout-killed tasks; real errors need user attention first.
---

# Resume a timed-out FlowForge task

A timed-out step marks the task `failed` and preserves its files and recorded
OpenCode session. Resume verifies successful checkpoints and starts at the first
invalid or missing checkpoint. A partial output file does not prove success.
The first resumed step reuses the saved session unless its recorded step or
revision differs. Do not reset the task, invent checkpoints or delete files.

## Steps

1. Resolve the intended project. Use the exact supplied project id; do not guess
   a workspace from the task id. If no project is supplied, use the configured
   legacy environment and invocation-directory `.env`.

   Inspect the queue through the shared configuration and storage helpers:

```bash
python3 - "<TASK_ID>" "<PROJECT_ID>" <<'PY'
import json
import sys
from flowforge_config import resolve_context
from flowforge_storage import find_task_index, read_tasks_jsonl

context = resolve_context(project=sys.argv[2] if len(sys.argv) > 2 else None)
tasks = read_tasks_jsonl(context.tasks_file)
index = find_task_index(tasks, sys.argv[1])
if index is None:
    raise SystemExit(f"Task not found: {sys.argv[1]}")
task = tasks[index]
print(json.dumps({
    "project": context.project_id,
    "runs_dir": str(context.runs_dir),
    **{key: task.get(key) for key in (
        "id", "status", "stage", "stage_id", "session_id", "last_run_log", "run_steps"
    )},
    "checkpoint_steps": sorted(task.get("checkpoints", {})),
}, indent=2))
PY
```

   Omit the `"<PROJECT_ID>"` argument for legacy mode. Status must be `failed`.
   Confirm an explicit "timed out" message in captured runner stderr, the service
   journal or other retained execution diagnostics. Inspect `last_run_log` for
   context, but the timeout diagnostic may be in runner stderr rather than the
   OpenCode transcript. A failed status or long elapsed time alone is insufficient.
   For a provider/model error or an unconfirmed failure, stop and report the cause
   instead of automatically resuming.

2. Check status in the same context:

```bash
./flowforge status --project "<PROJECT_ID>" --json
# Legacy mode:
python3 watch-task.py --json
```

   If project status is busy or an active run is shown, report it and do not
   launch. The runner's canonical workspace/queue/runs locks remain authoritative
   if another process starts after this check.

3. Launch detached using the selected context and a one-off two-hour step timeout
   (7200). Pass through another timeout supplied by the user, including `0` for
   unlimited. Create a unique capture directory under the resolved runs directory
   reported in step 1:

```bash
mkdir -p /absolute/resolved/runs
resume_dir="$(mktemp -d /absolute/resolved/runs/resume.XXXXXX)"
nohup ./flowforge run --project "<PROJECT_ID>" --task-id "<TASK_ID>" \
  --step-timeout 7200 --json \
  > "$resume_dir/result.json" 2> "$resume_dir/runner.log" < /dev/null &
resume_pid=$!
```

   In legacy mode, use this launch command with the same capture directory:

```bash
nohup python3 run-task.py --task-id "<TASK_ID>" --step-timeout 7200 --json \
  > "$resume_dir/result.json" 2> "$resume_dir/runner.log" < /dev/null &
resume_pid=$!
```

4. Retain the process id and capture paths. After roughly 15 seconds, inspect
   status again. Confirm the actual stage and session rather than assuming the
   execution step resumed. Check the new `last_run_log`; lines are appended as
   OpenCode emits them and heartbeats appear during quiet steps.

   The task may already have finished. Inspect `result.json` and `runner.log` if
   the process exited. Exit 0 can mean busy; do not report successful execution
   without checking the outcome. A fresh session is expected when the saved step
   or revision no longer matches.

5. Report the selected project/task, actual resumed stage, session id, log and
   capture paths, current status and applied timeout. Report startup/configuration
   errors directly. Further progress can be checked with the same scoped status
   command and the incrementally written run log.

## Notes

- Never edit `tasks.jsonl` by hand for a resume; `run-task.py` handles status.
- Old failed rows without checkpoints rerun all unverified steps, even if output
  files exist. Existing `run_steps` takes precedence over automatic resume.
- Source changes require an explicit reset decision; this skill never resets.
- If the lock is held, another run is active — report and do nothing.
- Exit codes: 0 successful/empty/month-gated/busy outcome, 1 task failure, 2
  configuration error. Inspect the captured JSON reason and runner stderr.
- See [project behavior](../../../docs/projects.md),
  [migration](../../../docs/migration.md#checkpoint-and-module-upgrade) and
  [the server guide](../../../docs/headless-server.md).
