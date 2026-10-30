# One installation, multiple projects

FlowForge is a finite foreground process. It launches the existing OpenCode engine,
keeps its sessions, timeout/heartbeat behavior, recovery and notifications, and exits
after a bounded batch. It has no daemon, embedded scheduler or Hermes dependency.
Python 3.10+ and Unix `flock` are required. The headless commands use only the
standard library. The TUI additionally uses the existing `textual`/`rich` packages.

## Paths and configuration

Keep code/resources in one installation (for example `/opt/flowforge`). Add that
directory to PATH or invoke `/opt/flowforge/flowforge` by absolute path. The executable
wrapper does not require installing a Python package.

Defaults:

| Location | Contents |
|---|---|
| `~/.config/flowforge/config.json` | Optional `{"defaults": {...}}` |
| `~/.config/flowforge/projects/<id>.json` | Registered project |
| `~/.config/flowforge/workflows/<name>.json` | Named workflow, existing step schema |
| `~/.config/flowforge/library/agents/` | Authoritative private Markdown agents |
| `~/.config/flowforge/library/resources/`, `prompts/`, `skills/` | Explicitly imported resources |
| `~/.config/flowforge/notifications.env` | Private Matrix settings; use mode 0600 |
| `~/.local/state/flowforge/<id>/tasks.jsonl` | Directory-mode queue |
| `~/.local/state/flowforge/<id>/artifacts/<source-hash>/` | Stable task artifacts |
| `~/.local/state/flowforge/<id>/runs/` | Unique attempt logs and OpenCode snapshots |

`FLOWFORGE_CONFIG_DIR` and `FLOWFORGE_STATE_DIR` override the two roots and **must
be absolute**. New-mode paths never depend implicitly on the launch directory.
Relative paths declared in JSON resolve against that JSON file. CLI paths resolve
against the invocation directory. Paths expand `~`, resolve symlinks and normalize
before execution. New-mode relative `task_file` values in existing queues resolve
against the workspace, matching legacy queues.

Settings precedence is CLI overrides > selected project JSON > central defaults >
built-in defaults. `--project NAME` and `--project-config FILE` are mutually exclusive.
No project is guessed; unknown names fail with exit 2. Explicit project selection
ignores legacy `PROJECT_DIR`, `TASKS_FILE`, `OPENCODE_BIN`, `.env` and batch/timeout
variables. Notification credentials still come from the service environment or
private `notifications.env`; they are never included in resolved configuration.
Process `MATRIX_*` settings override the private file. Project `"notifications":
false` disables notifications for that project.

Supported JSON settings: `workspace`, `tasks_dir` or `tasks_file`, optional
`source_mode` (`directory` or `jsonl`), `runs_dir`, `artifacts_dir`, `workflow`,
`workflow_file`, `agents_dir` (library root), `project_agents`, `agent_paths`,
`step_timeout`, `heartbeat`, `month_gate`, `max_tasks`, `stop_on_failure`,
`max_runtime`, `opencode_bin`, `notifications`. In directory mode an explicit
`tasks_file` optionally places the managed queue elsewhere. In JSONL mode it is
the authoritative queue and `tasks_dir` must be absent. Boolean settings must be
JSON booleans. Timeout/heartbeat values <= 0 disable those features; max_tasks
must be >= 1 and max_runtime >= 0. Batch runtime limits stop starting new tasks,
allowing the current task to finish.

See [central defaults](../examples/flowforge-config.json),
[directory project](../examples/directory-project.json) and
[existing queue](../examples/existing-queue-project.json).

## Clean setup with two projects

Supply your existing agent definitions through the import process below first.
No replacement prompts are bundled or invented. The bundled `steps.json` remains
the standard three-step workflow, including its explicit model overrides.

```bash
/opt/flowforge/flowforge project add smart-home \
  --workspace /srv/projects/smart-home --tasks-dir /srv/task-inbox/smart-home
/opt/flowforge/flowforge project add website \
  --workspace /srv/projects/website --tasks-dir /srv/task-inbox/website
/opt/flowforge/flowforge project list --json
/opt/flowforge/flowforge project show smart-home --json
/opt/flowforge/flowforge doctor --project smart-home --json
/opt/flowforge/flowforge sync --project smart-home --dry-run --json
/opt/flowforge/flowforge sync --project smart-home --json
/opt/flowforge/flowforge run --project smart-home --sync --max-tasks 3 --json
/opt/flowforge/flowforge status --project smart-home --json
python3 /opt/flowforge/tasks-ui.py --project website
```

Use `--project-config /path/project.json` on doctor/sync/run/status/reset/TUI as an
alternative to registration. Registration fails on an existing id. Inspect/edit
the documented JSON directly to update project settings. Neither repository needs
FlowForge files or copies of shared agents. Project `.opencode`/MCP settings and
project instructions remain available because cwd, `--dir` and `PWD` all point to
the workspace. Project files cannot change FlowForge's central settings.

## Task discovery and artifacts

Directory mode discovers **only** regular `task.md` files recursively, one task
per parent directory. Root-level `task.md` is supported with source key `.`. The
full relative parent path is the source key. IDs use the first 20 hex characters
of SHA-256 of that key, prefixed `task-`; both `a/common` and `b/common` are distinct.
New tasks are appended in lexicographic source-key order with status `todo`.
IDs remain stable when another task is inserted earlier. Renaming a source creates
a new task and retains the old entry as missing. Collisions, empty descriptions,
invalid UTF-8 and malformed/duplicate queue IDs fail before replacing the queue.
Directory and file symlinks are skipped; execution rechecks containment and refuses
changed sources and symlinks. There is no configurable discovery pattern in v1.

Sync preserves status, sessions, outputs, run_steps, workflow/agent overrides,
history and unknown fields. Source changes set `source_changed` and report the
observed hash; they do not reset status or rerun completed work. Missing sources
set `source_missing`; entries are retained and cannot execute. Restore the source
and sync to clear missing state. Explicitly acknowledge a changed source with:

```bash
flowforge reset --project smart-home --task-id task-HASH --json
```

Reset requires execution locks, records prior state in history, archives the
stable artifact directory to a `.reset-<uuid>` sibling, clears session/outputs and
sets `todo` using the observed source hash. For JSONL projects with beside-source
outputs, reset clears queue metadata but leaves old files for manual review;
explicitly remove/archive obsolete files before relying on failed-task resume.

`sync --dry-run` creates no directories, queues, lock files or artifacts. Doctor
and status are also read-only and never execute agents. `run --sync` synchronizes
under execution locks, then processes tasks. Sync on a JSONL-only project is an
explicit configuration error, including `run --sync`; omit it for that mode.

Directory-mode source files are read-only **from FlowForge's perspective**.
Generated plans and reports use the stable artifact directory, with absolute
output paths in prompts. The first input is task.md; every later input is the
preceding output. These paths are a storage convention, not a process sandbox;
OpenCode retains its OS permissions. Review existing agent edit permissions when
moving outputs outside `.opencode/tasks`. New JSONL registrations keep beside-source
outputs unless `artifacts_dir` is explicitly set. No existing output is moved.

## Workflows and agents

Workflow priority: invocation `--workflow NAME` > task `"workflow": "NAME"` >
project workflow (`workflow_file` when explicitly set, otherwise `workflow`) >
central defaults > bundled default. A project named workflow overrides an inherited
central workflow_file. Explicitly missing/invalid selections fail, never fall back.
An explicit workflow_file must exist. The legacy missing steps.json fallback remains.

The default is exactly concept/concept-plan, grounded/grounded-hard,
execution/execution-plan, in that order. Its shipped per-step model overrides are
preserved. Importing 11 or 100 agents does not add workflow steps. A task may select
a shorter/longer named workflow; `run_steps` still only selects existing IDs and
wins over automatic failed-task resume. Task `agents` replace individual step
agents. One agent may serve multiple steps. Optional step `agents` lists are
suggestions, never whitelists.

```bash
flowforge workflow list --json
flowforge workflow show default --json
flowforge workflow save review-only --from-file /path/review-steps.json --json
# Updating a shared named workflow requires an explicit --replace.
flowforge agent list --project smart-home --json
flowforge agent show grounded-lite --json
flowforge run --project smart-home --workflow review-only --max-tasks 2 --json
```

Agent catalog/inspection expose identifiers, description, model, origin, dependency
paths and validation issues, never the prompt body, permissions or credentials.
The TUI task editor shows compatible catalog IDs, accepts any of them without
editing favorites, and allows changing the task's named workflow. Its step editor
supports any ordered number of steps. In new mode saving the default step editor
forks it into `<project-config-stem>.flowforge/workflow.json` next to the selected
project config and points only that project to it. It never edits a shared named
workflow. Task-specific named workflows are edited through workflow save --replace.
In legacy mode the step editor still edits the selected steps.json.

Each attempt snapshots the central library into a private runs directory and sets
**OPENCODE_CONFIG_DIR only in the child environment**. OpenCode's native Markdown
loader reads the complete YAML frontmatter and body, including tool, permission,
provider and model settings. FlowForge's scalar parser is only for catalog display;
it does not translate or strip native metadata. OpenCode resolves the full YAML
schema at execution; doctor catches missing definitions/frontmatter, common legacy
field errors and dependencies but is not a complete YAML/schema validator.

This mechanism was checked against the [OpenCode configuration documentation](https://opencode.ai/docs/config/#custom-directory)
and [v1.18.32 loader source](https://github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/config/config.ts).
An isolated `opencode debug config` probe with version 1.18.32 confirmed prompt,
model and permission loading. No authenticated live model run has been performed;
verify that separately in a disposable workspace with your providers and MCP tools.

Central agents are authoritative. A same-name project Markdown/JSON declaration
that differs from the central definition is rejected by preflight by default to
prevent silent native deep-merge collisions. Identical retained copies are allowed;
the central definition remains the source of the execution snapshot.
Explicit `"project_agents": true` enables project definitions and gives them
precedence over central names. Both `.opencode/agent` and `.opencode/agents` and
project OpenCode JSON/JSONC agent declarations are recognized. OpenCode's ordinary
user/provider defaults still apply to fields left unspecified; enforced managed
OpenCode policy can override ordinary settings. Existing global OpenCode config
is never overwritten. Model metadata comes from the same selected definition that
is snapshotted; a workflow model remains an explicit `--model` override. FlowForge rejects OPENCODE_CONFIG_CONTENT overrides of selected agents because
OpenCode loads that content later; unrelated environment configuration is preserved.

## Reliability and monitoring

Execution acquires locks in fixed order: canonical workspace, canonical queue,
then runs/.flowforge.lock. Workspace/queue hashes share a per-user namespace under
`/tmp/flowforge-locks-<uid>` (fixed independently of TMPDIR), independent of config,
registration, state directory or cwd. Aliases and alternate registrations conflict.
Distinct workspaces with distinct queues/runs can run concurrently. Do not remove
lock files while workers are active. Workers in separate containers/users need a
shared execution/locking deployment; these local locks do not coordinate hosts.
The runs lock also interoperates with the pre-migration runner during cutover.

Queue mutations (sync, execution status/history and TUI field edits) use a short
blocking `<canonical-queue>.mutation.lock` transaction, re-read the latest queue,
then atomically replace/fsync. They preserve unrelated edits. Queue readers never
create locks. Execution locks are held before stale-running recovery; recovery
only touches the selected queue. External writers must use the same transaction
protocol rather than directly replacing an old full-queue snapshot.

Automatic runs pick only `todo`; failed tasks require explicit selection. New
projects are ungated by default; `month_gate: true` restores legacy month behavior.
Each step has its own session; only the first resumed step reuses the saved one.
Unique timestamp/UUID attempt logs retain the `_<task-id>.log` suffix; queues expose
attempt_id, current session and log path during execution. Batches append attempt
summaries to history. A successful step requires exit 0, no OpenCode error event,
and its expected output. Stop/timeout/error/exception still fail the task.

Project status uses the recorded session, checks the workspace when the SQLite
schema exposes it, and only falls back to titles containing a hash of the canonical
workspace **and** queue. Matching task IDs in another project are insufficient.
Children of a correlated session are shown under that session. Unrelated sessions
are omitted from project status. Unknown SQLite schemas produce an inspection issue.

`watch-task.py --json` without project flags retains its original schema-version-1
contract for matrix-flowforge-bot. `watch-task.py --project NAME --json` uses the
new CLI envelope instead. The external bot needs no change.
