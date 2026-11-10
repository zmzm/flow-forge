# Foreground machine interface

Use the same CLI as operators/systemd. Hermes does not need an SDK or an LLM routing
path. Always select an exact project id or explicit config file; never guess a cwd.

Suggested Hermes instructions:

> Call `flowforge project list --json`, resolve the requested exact id, and call
> `flowforge project show ID --json`. If no unique id is selected, ask the user;
> do not execute in a guessed directory. Call `flowforge doctor --project ID --json`.
> For a directory-source project, inspect `sync --dry-run --json`, then launch
> `flowforge run --project ID --sync --max-tasks 3 --max-runtime 14400 --json`
> through the existing process manager. For JSONL-only projects omit --sync.
> Retain the process handle and exit status. While it runs inspect
> `flowforge status --project ID --json`. Report completed/failed counts, batch
> reason and task log locations. A busy result means another worker owns the
> project/queue; do not claim processing succeeded. Failed tasks require an explicit
> --task-id; source changes require an explicit reset decision.

Failed-task resume uses persisted successful checkpoints and verifies source/workflow
revisions and input/output hashes. Do not infer completion from output-file existence.
Older failed rows without checkpoints rerun unverified steps. Report the actual
resumed stage and session; a changed step or revision starts a fresh session.
Existing `run_steps` takes precedence over automatic resume. See
[migration behavior](migration.md#checkpoint-and-module-upgrade) before retrying
older work that changes a repository.

After argument parsing, --json commands print one JSON document to stdout.
Argument-parser usage errors exit 2 with diagnostics on stderr before an envelope
is created. Other logs/diagnostics also go to stderr. The top-level
`schema_version: 1` is the **FlowForge CLI envelope**, distinct
from the legacy watcher schema 1. It includes `command`, `project`, `workspace`
when resolved and command-specific fields. Errors include reason and error/issues.
Run outcomes include run_id, per-task attempt_id/session_id, log_path, message,
duration, completed/failed counts and busy. Project metadata is inspectable without
exposing notification credentials or agent prompts.
If configuration validation stops a later batch selection, earlier completed
attempts remain persisted. Inspect status and task history when reporting a partial
batch; a configuration-error envelope does not necessarily include those outcomes.

| Exit | Meaning |
|---|---|
| 0 | Successful batch, queue_empty, month_gated, or explicit busy outcome |
| 1 | At least one attempted task failed |
| 2 | Invalid/missing configuration, source/queue/workflow/agent, or preflight failure |

Run reasons: `busy`, `queue_empty`, `month_gated`, `completed` (explicit task),
`max_tasks`, `max_runtime`, `stop_on_failure`, `configuration_error`. Check failed
counts even when a batch stops at a limit. Failed provider delivery to Matrix is
logged and never changes these task outcomes. Empty queues remain notification-quiet.

Status returns counts, queue_total, busy, tasks, running and stale_running. It is
read-only. A stale running row is only recovered under execution locks on the next
execution lease. CLI and TUI attempts both append history. Run ids identify batch responses;
persisted task attempt ids/logs identify
individual attempts across resumes. Batches run in the foreground and exit;
max-runtime does not interrupt an already running task. Use --step-timeout to bound
individual OpenCode steps (default 3600 seconds).
Logs are appended as events arrive and session ids are persisted immediately;
polling does not need to wait for a step to finish. Process cleanup and attempt
finalization finish before execution locks are released.

Legacy commands keep their environment/.env path semantics. `run-task.py --project
ID` delegates to the same engine/context/locks; `watch-task.py --project ID --json`
uses this CLI status envelope. `watch-task.py --json` without project selection keeps
the bot's legacy contract unchanged.
