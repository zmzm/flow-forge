# AGENTS.md

Guidance for coding agents working in this repository.

## Project Overview

FlowForge TUI is a Python terminal task manager (Textual UI) for running a multi-agent AI pipeline via `opencode` CLI. Tasks live in a `tasks.jsonl` file; each run executes pipeline steps (Business plan -> Technical plan -> Execution), streams JSON events, and persists results back to `tasks.jsonl`.

## Commands

```bash
# Run the TUI (requires env vars, see below)
python3 tasks-ui.py

# Headless run: next task with status "todo"
python3 run-task.py

# Headless run: specific task
python3 run-task.py --task-id f2-d8

# Resume a timed-out task (continues same session, 2h step timeout)
python3 run-task.py --task-id f2-d8 --step-timeout 7200

# Live status of running tasks + opencode sessions (incl. subagents)
python3 watch-task.py            # refresh loop, Ctrl-C to exit
python3 watch-task.py --once     # single report
python3 watch-task.py --json     # machine-readable status (consumed by the Matrix bot)

# Install dependencies
pip install textual rich
```

There is no linter config or `pyproject.toml`/`requirements.txt`. Python 3.10+; stdlib only plus `textual` and `rich`. `mypy` may be available on the system but is not configured. When asked to verify, at minimum run `python3 -m py_compile tasks-ui.py pipeline_runner.py run-task.py headless_runner.py watch-task.py` and the test suite `python3 -m unittest discover -s tests` (stdlib `unittest`; tests set their own `PROJECT_DIR`/`TASKS_FILE` env in `tests/_bootstrap.py`, never contact the network).

## Required Environment

Legacy CLI/TUI entry points validate these at startup when no project is selected. Module imports and --help do not load dotenv or require project variables:

- `PROJECT_DIR` — target repository where pipeline steps run (cwd for `opencode`)
- `TASKS_FILE` — path to `tasks.jsonl`

In legacy mode a `.env` file in the invocation directory is loaded at startup (simple `KEY=value` parsing; process environment wins, without mutating it). `.env`, `runs/`, and `tasks.jsonl` are gitignored — never commit them.

Note: `STEPS_FILE` is derived as `steps.json` next to `TASKS_FILE`; `RUNS_DIR` is a relative `runs/` dir in the current working directory. Logs are written to `runs/<timestamp>_<task_id>.log`.

## Architecture

Flat modules, no packaging:

- `tasks-ui.py` — Textual app. Tasks table + details panel + live events feed. Modal screens: `EditTaskScreen` (workflow, catalog agents + run steps), `EditStepsConfigScreen`. Runner executes in a daemon thread; events are marshaled to the UI via `call_from_thread`. Entry point: `TasksUI`.
- `pipeline_runner.py` — headless engine. Task selection, step config normalization, `opencode run --format json` subprocess streaming, session-id/error parsing, `tasks.jsonl` persistence. Also the shared config/data layer the UI imports (all file reads/writes go through it).
- `run-task.py` — argparse CLI: `--task-id` (single task), `--max-tasks` (batch override), `--step-timeout N` (one-off per-step timeout override, `0` = unlimited). Exit codes: 0 ok / 1 task failure / 2 config error. Takes the `runs/.flowforge.lock` flock before doing work.
- `watch-task.py` — read-only status CLI: running tasks from `tasks.jsonl` plus active opencode sessions (incl. `task`-tool subagents via `session.parent_id`) from `~/.local/share/opencode/opencode.db`. Falls back to title matching (`{task_id}: ...`) when a task's recorded `session_id` is stale; resolves the newest `runs/*_<task_id>.log`. `--json` (`build_status`, `schema_version` 1) is the machine-readable contract consumed by `matrix-flowforge-bot` (`../matrix-flowforge-bot`): lock state, running task (id/title/stage/duration/session), all task rows (id/title/status), queue total, and `last_failed` incl. `quota_reset_at` parsed from the run log. Change the schema only with a version bump and update the bot in lockstep.
- `headless_runner.py` — batch loop around `run_selected_task` (`FLOWFORGE_MAX_TASKS_PER_RUN`, `FLOWFORGE_STOP_ON_FAILURE`, `FLOWFORGE_MAX_RUNTIME_SECONDS`), lifecycle logging (`flowforge.runner`), single-instance flock, notification dispatch. Notifications never affect task status or exit codes.
- `notifications/` — `base.py` (`Notifier` ABC, `NullNotifier`, `SafeNotifier` that swallows+logs failures, `parse_bool`), `matrix.py` (`MatrixNotifier` over stdlib `urllib`, no SDK), `__init__.py` (`build_notifier` from `MATRIX_*` env; enabled-but-incomplete config raises ValueError -> exit 2).

Support files:

- `steps.json` — pipeline step definitions (`id`, `label`, `agent`, optional `model` and `agents` list). Lives next to `tasks.jsonl`.
- `fake-tasks.jsonl` — demo/sample data only, not used by the code.
- `assets/` — README screenshot.
- `deploy/` — example systemd service/timer for the headless server; `docs/headless-server.md` — server deployment guide.
- `tests/` — stdlib `unittest` suite; `_bootstrap.py` forces isolated `PROJECT_DIR`/`TASKS_FILE` before any project import; Matrix HTTP is always mocked.

## Standalone project mode

- `flowforge` / `flowforge_cli.py`: registered project/config selection, doctor, sync, run, status, reset, agent import/catalog and workflow commands; JSON envelope version 1, separate from watcher v1.
- `flowforge_config.py`: immutable RuntimeContext; precedence CLI > selected JSON > central defaults > built-ins. Legacy env adapters remain for old callers/tests. Explicit projects ignore stale legacy variables and never switch module globals/process environment.
- `flowforge_storage.py`: canonical workspace -> queue -> runs execution locks, plus short queue mutation transactions and atomic persistence. Every interface must use these protocols. Never recover stale rows without execution locks.
- `flowforge_sync.py`: recursive regular task.md discovery, stable relative-path IDs, source hashes/missing flags and explicit reset. Directory sources use state artifacts; JSONL keeps old output locations by default.
- `flowforge_agents.py`: private central Markdown library, staged import with dependency/collision preview, native OPENCODE_CONFIG_DIR injection. Never commit private prompts. Real Mentory cutover remains pending unresolved dependencies; see docs/migration.md.
- `flowforge_status.py`: read-only scoped status and workspace/queue-hashed session titles; never correlate a project using bare task IDs.
- See docs/projects.md, docs/migration.md, docs/hermes.md. Runtime roots default to ~/.config/flowforge and ~/.local/state/flowforge; root environment overrides must be absolute.
- Verification also compiles all new modules and runs tests/test_projects.py; optional Textual pilot tests in tests/test_project_ui.py run when Textual is installed. Existing test fixtures still patch compatibility globals for legacy engine callers.

## Key Behaviors and Invariants

- **Model selection**: models come from the OpenCode agent's frontmatter (`agent_configured_model` searches `.opencode/agents/` and `~/.config/opencode/agents/`). A `model` in `steps.json` is an explicit per-step override. There is deliberately no global model/CLI override (`MODEL`, `VARIANT`, `USE_ATTACH`, `SHARE_SESSION` are vestigial module globals — keep behavior unless asked to change it).
- **Step selection** (`select_steps_for_task`): task `run_steps` (explicit) > full run for non-failed tasks > auto-resume for `failed` tasks, starting from the first step whose output file is missing.
- **Sessions**: every step runs in its own opencode session (all steps get `--title "{task_id}: ..."` so `watch-task.py` title fallback matches the newest). Steps are chained via output files, not shared conversation context. When a `failed` task is rerun, its recorded `session_id` is passed (`--session`) to the first re-executed step only, continuing the interrupted conversation; the task's persisted `session_id` is always the latest step's session.
- **Task lifecycle**: a task is marked `running` when execution starts; success -> `done`, failure/stop/timeout/exception -> `failed` (persisted even when `run_selected_task` raises). Stale `running` tasks (dead-process leftovers) are converted to `failed` by `recover_stale_running_tasks`, which the batch runner calls once per run while holding the lock. `find_next_todo` only picks `todo`.
- **Month gate**: task ids `f<N>-d<M>` encode a month (`N`, linear across years: f9 = Sep 2026, f13 = Jan 2027; `current_month_number`, `MONTH_BASE_YEAR = 2026`). The headless batch runner only auto-selects tasks with `N <= current month` (ids without the prefix stay eligible); when only future-month tasks remain it stops with reason `month_gated`, exits 0, and notifies via `run_completed` (payload gains `reason`/`message`). Explicit `--task-id` bypasses the gate; the TUI's next-task selection is ungated.
- **Step timeout**: `STEP_TIMEOUT_SEC` comes from `FLOWFORGE_STEP_TIMEOUT_SECONDS` (default 3600, `0`/negative = unlimited). A hung OpenCode process gets SIGTERM, then SIGKILL after 10s; the step fails with a "timed out" error.
- **Heartbeat**: `HEARTBEAT_SEC` comes from `FLOWFORGE_HEARTBEAT_SECONDS` (default 30, `0`/negative = disabled). While a step emits no output for at least that long, the runner emits a `line` event and appends a `[heartbeat]` line to the run log (step elapsed, silence duration, subprocess pid/cpu/rss).
- **Built-in step ids** (`concept`, `grounded`, `execution`) have fixed output filenames (`concept-plan.md`, `grounded-plan.md`, `execution.md` in the task file's directory) and specialized prompts in `step_message`. New/custom step ids run generically and write `<step-id>.md`. Each step's input is the previous step's output; the first step's input is `task_file`.
- **Step success** requires: exit code 0, no `type: "error"` event in the JSON stream, and the output file existing. Any failure marks the task `failed`; success marks `done` and merges `outputs` (step id -> path). `RunResult.stage` records the failing step label.
- **Persistence**: `read_tasks_jsonl`/`write_tasks_jsonl` rewrite the whole file atomically (temp file + `os.replace` + fsync); every line is a JSON object. Update `tasks.jsonl` only through these helpers or `_persist_task_update` (merges into a freshly read file so concurrent edits survive).
- **Locking**: `acquire_lock`/`release_lock` (`pipeline_runner`) take a non-blocking flock on `RUNS_DIR/.flowforge.lock`. Used by `run-task.py` (whole run) and the TUI (per run thread); the lock holder may recover stale `running` tasks. `RUNS_DIR` honors `FLOWFORGE_RUNS_DIR` (default relative `runs/`).
- **Legacy task fields** `steps` and `step_labels` are obsolete; the UI editor removes them on save. Do not reintroduce them.
- **Step id validation**: ids must match `^[A-Za-z0-9_-]+$`, be unique, and have non-empty label/agent (`normalize_step_configs`).
- **Agent selection**: step `agents` lists are optional suggestions, never a whitelist. The TUI accepts any compatible effective catalog agent. New-mode workflow edits fork a project-specific copy; shared workflows are only changed explicitly.

## Conventions

- Single-file modules, flat repo, no comments-heavy style: keep code concise; existing code uses type hints and dataclasses (`RunResult`). Follow the existing style; do not add dependencies.
- Runner ↔ UI contract is the event dict stream (`kind`: `run_start`, `session`, `step_start`, `step_done`, `command`, `line`, `error`, `opencode_event`, `run_done`). Keep payloads backward compatible when touching `pipeline_runner.py`.
- Rich markup in the UI is escaped with `rich.markup.escape` for all dynamic text; preserve that.
