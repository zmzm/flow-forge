# FlowForge

<img width="1200" height="600" alt="flowForge" src="https://github.com/user-attachments/assets/852880d7-1a0e-48bc-8982-bad8b6a14cdc" />


FlowForge runs configurable, multi-step AI workflows through the OpenCode CLI. It manages a task queue in `tasks.jsonl`, chains agents through output files, and records task status, sessions, and logs.

Use it interactively in a terminal, run batches from the command line, or schedule autonomous runs on a server. The Textual TUI is one interface to the same execution engine.

## Ways to Use It

| Interface | Command | Purpose |
|---|---|---|
| Headless runner | `python3 run-task.py` | Process queued tasks, manually or on a schedule |
| Terminal UI | `python3 tasks-ui.py` | Browse tasks, edit agents and steps, run or stop tasks, and follow live events |
| Status watcher | `python3 watch-task.py` | Monitor running tasks and OpenCode sessions, including subagents |
| Machine-readable status | `python3 watch-task.py --json` | Expose queue and runner status to integrations |

The headless runner supports batch limits, per-step timeouts, heartbeat logs, and optional Matrix notifications. Example systemd units are included for scheduled server runs. The TUI and CLI share a lock to prevent overlapping execution when configured with the same runs directory.

## Quick Start

Requirements:

- Python 3.10+ on Linux or another Unix-like system with `flock` support.
- `opencode` installed, authenticated, and available in `PATH` (or configured with `OPENCODE_BIN`).
- OpenCode agents matching your pipeline configuration, available in the target project's `.opencode/agents/` or `~/.config/opencode/agents/`.
- For the TUI only: `pip install textual rich`. The headless runner and status watcher use the Python standard library.

Run commands from this repository's root. Set the two required paths in your shell or in a `.env` file in the working directory:

```bash
export PROJECT_DIR="/absolute/path/to/project"
export TASKS_FILE="/absolute/path/to/tasks.jsonl"
```

`PROJECT_DIR` is the repository where OpenCode executes. `TASKS_FILE` is the queue; `steps.json` belongs alongside it. Neither required path has a fallback. A local `.env` is loaded automatically without overriding existing environment variables; `.env.example` provides a configuration template.

Create a Markdown task description and a queue file containing one JSON object per line:

```json
{"id":"task-1","task_file":"/absolute/path/to/task.md","status":"todo"}
```

Configure the agents in `steps.json` as described below, then choose an interface:

```bash
# Run the next eligible todo task
python3 run-task.py

# Process up to three queued tasks
python3 run-task.py --max-tasks 3

# Run a specific task, including a failed task
python3 run-task.py --task-id task-1

# Open the interactive task manager
python3 tasks-ui.py

# Inspect a running task from another terminal
python3 watch-task.py --once
```

For scheduled execution and Matrix setup, follow the [headless server guide](docs/headless-server.md). The example [service](deploy/flowforge.service) and [timer](deploy/flowforge.timer) run a batch daily at 03:00 after you configure the installation paths and service user.

## Pipeline Configuration

The default workflow is **Business plan → Technical plan → Execution**. Define the ordered steps in `steps.json` next to `tasks.jsonl`:

```json
{
  "steps": [
    {"id": "concept", "label": "Business plan", "agent": "concept-plan"},
    {"id": "grounded", "label": "Technical plan", "agent": "grounded-hard"},
    {"id": "execution", "label": "Execution", "agent": "execution-plan"}
  ]
}
```

Each step runs in its own OpenCode session. The first step receives the task description; subsequent steps receive the preceding step's output file. Built-in step ids produce `concept-plan.md`, `grounded-plan.md`, and `execution.md` in the task description's directory and use specialized prompts. Custom steps write `<step-id>.md` and use a generic prompt. Step ids must be unique and contain only letters, digits, underscores, or hyphens; labels and agents must be non-empty.

Models normally come from the selected OpenCode agent's frontmatter or OpenCode's default. Add an optional `model` field to a step for an explicit override; there is no global model override in FlowForge.

Tasks can override agents or select particular steps:

```json
{"id":"task-1","task_file":"/absolute/path/to/task.md","status":"failed","run_steps":["execution"],"agents":{"execution":"custom-execution"}}
```

`run_steps` runs only the listed step ids, in the listed order. Each selected step still needs its normal input file. Without `run_steps`, non-failed tasks run the full pipeline; failed tasks resume from the first step whose output file is missing. Explicit step selection takes precedence over automatic resume.

The TUI editor lets you edit agents and `run_steps` per task. If an agent is unknown for its step, it offers to add it to the step's optional `agents` list in `steps.json`. The global configuration editor validates and saves the pipeline and can insert a custom step template.

## Headless Execution and Recovery

Automatic queue selection picks only `todo` tasks, up to the batch limit (one by default). Failed tasks require explicit selection with `--task-id` or through the TUI.

Task ids matching `f<N>-d<M>` also act as a month gate for automatic headless selection: `f9` means September 2026, `f13` means January 2027, and so on. Future-month tasks wait until their month arrives; other ids stay eligible. If only future tasks remain, the batch exits successfully with reason `month_gated`. An explicit `--task-id` and TUI selection bypass this gate.

A task becomes `running` when execution starts, then `done` on success or `failed` on an error, stop, or timeout. Success requires an exit code of zero, no error event from OpenCode, and the expected output file. FlowForge persists `session_id`, `last_run_log`, and successful step `outputs` back to the queue.

If a runner dies mid-task, the next headless run marks stale `running` tasks as `failed`. Select the failed task explicitly to resume it. Its recorded OpenCode session is reused for the first re-executed step; later steps start separate sessions.

```bash
# Resume a timed-out task with a two-hour timeout per step
python3 run-task.py --task-id task-1 --step-timeout 7200

# Disable the per-step timeout for this invocation
python3 run-task.py --task-id task-1 --step-timeout 0
```

Logs are stored in `runs/<timestamp>_<task_id>.log` by default. Quiet steps emit heartbeat lines with elapsed time and process information. Queue writes use an atomic file replacement, and execution updates merge into a freshly read queue to preserve concurrent edits.

CLI exit codes: `0` for successful completion, an empty or month-gated queue, or an already-held lock; `1` for task failure; `2` for configuration/startup errors. Matrix delivery failures are logged and do not change task results or exit codes.

## Runtime Settings

| Variable | Default | Purpose |
|---|---|---|
| `PROJECT_DIR` | Required | Repository where OpenCode executes |
| `TASKS_FILE` | Required | Path to the JSONL task queue |
| `OPENCODE_BIN` | `opencode` | OpenCode executable path |
| `FLOWFORGE_RUNS_DIR` | `runs` | Logs and `.flowforge.lock`; relative to the working directory unless absolute |
| `FLOWFORGE_MAX_TASKS_PER_RUN` | `1` | Maximum tasks per headless batch; CLI override: `--max-tasks` |
| `FLOWFORGE_STOP_ON_FAILURE` | `true` | Stop the batch after a task fails |
| `FLOWFORGE_MAX_RUNTIME_SECONDS` | `14400` | Stop starting new tasks after this duration; `0` disables the limit |
| `FLOWFORGE_STEP_TIMEOUT_SECONDS` | `3600` | Per-step timeout; `0` disables it; CLI override: `--step-timeout` |
| `FLOWFORGE_HEARTBEAT_SECONDS` | `30` | Heartbeat interval during quiet steps; `0` disables it |
| `MATRIX_ENABLED` | `false` | Enable headless lifecycle notifications |
| `MATRIX_HOMESERVER`, `MATRIX_ROOM_ID`, `MATRIX_ACCESS_TOKEN` | — | Required when Matrix notifications are enabled |
| `MATRIX_TIMEOUT_SECONDS` | `10` | Matrix HTTP request timeout |

The batch runtime limit lets the current task finish. The per-step timeout terminates a hung OpenCode process, escalating to SIGKILL after 10 seconds if necessary. Keep `.env`, credentials, task queues, and run logs out of version control.

## Monitoring

```bash
python3 watch-task.py                 # refresh every five seconds; Ctrl-C to exit
python3 watch-task.py --once          # one report
python3 watch-task.py --interval 10   # change the refresh interval
python3 watch-task.py --json          # one machine-readable status report
```

The text report combines queue state and logs with active OpenCode sessions, including subagents, from `~/.local/share/opencode/opencode.db`. Use `--db` to point to another database. It falls back to task-id session titles when a recorded session is stale.

JSON output uses schema version 1 and includes lock state, the running task and stage, task rows, queue total, and failure details (including quota reset time when found in the log). It is consumed by the separate `matrix-flowforge-bot` integration.

## Terminal UI

![FlowForge terminal UI](assets/Screenshot.png)

The TUI provides a task table with status filters, task details, a live run header, and a streaming LLM/tool/system event feed.

| Key | Action |
|---|---|
| `r` | Run selected task |
| `s` | Stop active run |
| `e` | Edit task agents and run steps |
| `g` | Edit global `steps.json` |
| `f` | Cycle filter (`all/todo/failed/done`) |
| `u` | Refresh table |
| `c` | Clear events feed |
| `q` | Quit |

## Project Layout

| Path | Responsibility |
|---|---|
| `pipeline_runner.py` | Shared execution engine, step configuration, queue persistence, sessions, and event streaming |
| `run-task.py` | Headless CLI, timeout override, locking, and exit codes |
| `headless_runner.py` | Batch selection, limits, stale-task recovery, lifecycle logging, and notifications |
| `tasks-ui.py` | Textual interface and task/pipeline editors |
| `watch-task.py` | Read-only task and OpenCode session monitoring |
| `notifications/` | Notifier interface and Matrix HTTP implementation |
| `deploy/` | Example systemd service and timer |
| `docs/headless-server.md` | Server deployment and notification setup |
| `tests/` | Standard-library unit tests with isolated queues and mocked Matrix HTTP |

## Development Checks

```bash
python3 -m py_compile tasks-ui.py pipeline_runner.py run-task.py headless_runner.py watch-task.py
python3 -m unittest discover -s tests
```

Tests set their own `PROJECT_DIR` and `TASKS_FILE` and do not contact the network.
