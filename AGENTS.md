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

# Install UI dependencies
python3 -m pip install -r requirements-ui.txt

# Install development tools and run all quality checks
python3 -m pip install -r requirements-dev.txt
python3 scripts/check.py
```

Python 3.10+; headless runtime uses the standard library, with `textual` and `rich` for the UI. `pyproject.toml` configures Ruff and mypy; pinned UI and development tools are in `requirements-ui.txt` and `requirements-dev.txt`. Use the project's virtual environment for checks: `.venv/bin/python scripts/check.py` runs lint, format verification, type checking, compilation and the full suite. Without development tools, `python3 scripts/check.py --stdlib-only` compiles all application/test modules and runs unittest. `python3 -m unittest discover -s tests` remains supported. Tests set isolated project variables in `tests/_bootstrap.py` before application imports and never contact the network. See `docs/development.md`.

## Required Environment

Legacy CLI/TUI entry points validate these at startup when no project is selected. Module imports and --help do not load dotenv or require project variables:

- `PROJECT_DIR` — target repository where pipeline steps run (cwd for `opencode`)
- `TASKS_FILE` — path to `tasks.jsonl`

In legacy mode a `.env` file in the invocation directory is loaded at startup (simple `KEY=value` parsing; process environment wins, without mutating it). `.env`, `runs/`, and `tasks.jsonl` are gitignored — never commit them.

Note: in legacy mode `STEPS_FILE` is derived as `steps.json` next to `TASKS_FILE`; `RUNS_DIR` defaults to a relative `runs/` directory in the invocation directory and honors `FLOWFORGE_RUNS_DIR`. Logs are written to `<runs_dir>/<timestamp>_<uuid>_<task_id>.log`.

## Architecture

Flat modules, no packaging:

- `tasks-ui.py` — compatibility launcher; `flowforge_ui.py` contains the importable Textual app. Tasks table + details panel + live events feed. Modal screens: `EditTaskScreen` (workflow, catalog agents + run steps), `EditStepsConfigScreen`. Runner executes in a daemon thread; events are marshaled to the UI via `call_from_thread`. CLI argument parsing and lazy UI import live in `flowforge_cli.ui_main`.
- `pipeline_runner.py` — task/step planning, checkpoint validation, lifecycle and runner events. Retains legacy imports/wrappers for storage, workflow and OpenCode helpers.
- `flowforge_execution.py` — shared CLI/TUI execution lease. Owns execution locks, stale recovery, scoped preflight and per-attempt history. Production callers execute through this module; direct engine callers must already hold execution locks.
- `flowforge_opencode.py` — subprocess groups, termination/reaping on every exit, cancellation escalation, incremental logs and bounded session/error summaries.
- `flowforge_workflows.py` — explicit-path workflow parsing, normalization, persistence and effective task agents.
- `flowforge_inspection.py` — shared read-only OpenCode database and log inspection; status must not load executable scripts dynamically.
- `flowforge_models.py` — shared result/event aliases, `RunResult`, `StepResult` and normalized `StepConfig`. Keep legacy runner re-exports compatible.
- `run-task.py` — argparse CLI: `--task-id` (single task), `--max-tasks` (batch override), `--step-timeout N` (one-off per-step timeout override, `0` = unlimited). Exit codes: 0 ok / 1 task failure / 2 config error. Takes the `runs/.flowforge.lock` flock before doing work.
- `watch-task.py` — compatibility launcher for importable `flowforge_watch.py`: running tasks from `tasks.jsonl` plus active opencode sessions (incl. `task`-tool subagents via `session.parent_id`) from `~/.local/share/opencode/opencode.db`. Falls back to title matching (`{task_id}: ...`) when a task's recorded `session_id` is stale; resolves the newest `runs/*_<task_id>.log`. `--json` (`build_status`, `schema_version` 1) is the machine-readable contract consumed by `matrix-flowforge-bot` (`../matrix-flowforge-bot`): lock state, running task (id/title/stage/duration/session), all task rows (id/title/status), queue total, and `last_failed` incl. `quota_reset_at` parsed from the run log. Change the schema only with a version bump and update the bot in lockstep.
- `headless_runner.py` — batch scheduling through the shared execution lease (`FLOWFORGE_MAX_TASKS_PER_RUN`, `FLOWFORGE_STOP_ON_FAILURE`, `FLOWFORGE_MAX_RUNTIME_SECONDS`), lifecycle logging (`flowforge.runner`) and notification dispatch. Owns a lease unless supplied one. Notifications never affect task status or exit codes.
- `notifications/` — `base.py` (`Notifier` ABC, `NullNotifier`, `SafeNotifier` that swallows+logs failures, `parse_bool`), `matrix.py` (`MatrixNotifier` over stdlib `urllib`, no SDK), `__init__.py` (`build_notifier` from `MATRIX_*` env; enabled-but-incomplete config raises ValueError -> exit 2).

Support files:

- `steps.json` — pipeline step definitions (`id`, `label`, `agent`, optional `model` and `agents` list). Lives next to `tasks.jsonl`.
- `fake-tasks.jsonl` — user-retained queue-format example, not used by the code. Preserve it; its legacy `steps`/`step_labels` fields document older sample data and must not be restored in runtime code.
- `assets/` — README screenshot.
- `deploy/` — example systemd service/timer for the headless server; `docs/headless-server.md` — server deployment guide.
- `tests/` — stdlib `unittest` suite; `_bootstrap.py` forces isolated `PROJECT_DIR`/`TASKS_FILE` before any project import; Matrix HTTP is always mocked. Shared fixtures live in `tests/support`; use absolute `tests.*` imports so package and legacy discovery both work.

## Standalone project mode

- `flowforge` / `flowforge_cli.py`: registered project/config selection, doctor, sync, run, status, reset, agent import/catalog and workflow commands; JSON envelope version 1, separate from watcher v1.
- `flowforge_config.py`: immutable RuntimeContext; precedence CLI > selected JSON > central defaults > built-ins. Legacy env adapters remain for old callers/tests. Explicit projects ignore stale legacy variables and never switch module globals/process environment.
- `flowforge_storage.py`: queue serialization/validation, canonical workspace -> queue -> runs execution locks, short queue mutation transactions and atomic persistence. Does not import the execution engine. Every interface must use these protocols. Never recover stale rows without execution locks.
- `flowforge_sync.py`: recursive regular task.md discovery, stable relative-path IDs, source hashes/missing flags and explicit reset. Directory sources use state artifacts; JSONL keeps old output locations by default.
- `flowforge_agents.py`: private central Markdown library, staged import with dependency/collision preview, native OPENCODE_CONFIG_DIR injection. Never commit private prompts. Real Mentory cutover remains pending unresolved dependencies; see docs/migration.md.
- `flowforge_status.py`: read-only scoped status and workspace/queue-hashed session titles; never correlate a project using bare task IDs.
- See docs/projects.md, docs/migration.md, docs/hermes.md. Runtime roots default to ~/.config/flowforge and ~/.local/state/flowforge; root environment overrides must be absolute.
- Verification also compiles all new modules and runs tests/test_projects.py; optional Textual pilot tests in tests/test_project_ui.py run when Textual is installed. Existing test fixtures still patch compatibility globals for legacy engine callers.

## Key Behaviors and Invariants

- **Model selection**: models come from the OpenCode agent's frontmatter (`agent_configured_model` searches `.opencode/agents/` and `~/.config/opencode/agents/`). A `model` in `steps.json` is an explicit per-step override. There is deliberately no global model/CLI override (`MODEL`, `VARIANT`, `USE_ATTACH`, `SHARE_SESSION` are vestigial module globals — keep behavior unless asked to change it).
- **Step selection** (`select_steps_for_task`): task `run_steps` (explicit) > full run for non-failed tasks > auto-resume for `failed` tasks, starting from the first invalid/missing successful checkpoint. Checkpoints record source/workflow fingerprints and input/output hashes, and are persisted per successful step. Old failed rows without checkpoints rerun unverified steps; file existence alone is insufficient. Explicit reruns invalidate checkpoints from the earliest selected step onward. Reset clears checkpoints.
- **Sessions**: every step runs in its own opencode session (legacy titles use `--title "{task_id}: ..."`; project titles are scoped). Steps are chained via output files, not shared conversation context. On failed-task resume, the recorded session is passed to the first re-executed step only, unless its recorded step/revision differs. Session events are persisted immediately.
- **Task lifecycle**: a task is marked `running` when execution starts; success -> `done`, failure/stop/timeout/exception -> `failed` (persisted even when `run_selected_task` raises). Stale `running` tasks (dead-process leftovers) are converted to `failed` by `recover_stale_running_tasks`, which the batch runner calls once per run while holding the lock. `find_next_todo` only picks `todo`.
- **Month gate**: task ids `f<N>-d<M>` encode a month (`N`, linear across years: f9 = Sep 2026, f13 = Jan 2027; `current_month_number`, `MONTH_BASE_YEAR = 2026`). Legacy automatic headless selection enables the gate; registered projects are ungated unless `month_gate: true`. A gated batch only auto-selects tasks with `N <= current month` (ids without the prefix stay eligible); when only future-month tasks remain it stops with reason `month_gated`, exits 0, and notifies via `run_completed` (payload gains `reason`/`message`). Explicit `--task-id` and TUI selection bypass the gate.
- **Step timeout**: `STEP_TIMEOUT_SEC` comes from `FLOWFORGE_STEP_TIMEOUT_SECONDS` (default 3600, `0`/negative = unlimited). A hung OpenCode process gets SIGTERM, then SIGKILL after 10s; the step fails with a "timed out" error.
- **Heartbeat**: `HEARTBEAT_SEC` comes from `FLOWFORGE_HEARTBEAT_SECONDS` (default 30, `0`/negative = disabled). While a step emits no output for at least that long, the runner emits a `line` event and appends a `[heartbeat]` line to the run log (step elapsed, silence duration, subprocess pid/cpu/rss).
- **Built-in step ids** (`concept`, `grounded`, `execution`) have fixed output filenames (`concept-plan.md`, `grounded-plan.md`, `execution.md` in the task file's directory) and specialized prompts in `step_message`. New/custom step ids run generically and write `<step-id>.md`. Each step's input is the previous step's output; the first step's input is `task_file`.
- **Step success** requires: exit code 0, no `type: "error"` event in the JSON stream, and the output file existing. Any failure marks the task `failed`; success marks `done` and merges `outputs` (step id -> path). `RunResult.stage` records the failing step label.
- **Persistence**: `read_tasks_jsonl`/`write_tasks_jsonl` rewrite the whole file atomically (temp file + `os.replace` + fsync); every line is a JSON object. Update `tasks.jsonl` only through these helpers or `_persist_task_update` (merges into a freshly read file so concurrent edits survive).
- **Locking**: `Execution` owns canonical workspace -> queue -> runs locks for CLI batches and TUI attempts. Recovery requires that execution lease. Storage implements low-level `acquire_lock`/`release_lock`, re-exported by `pipeline_runner` for compatibility; acquiring only the runs lock is insufficient for new execution callers. Queue edits use their separate short mutation transaction.
- **Legacy task fields** `steps` and `step_labels` are obsolete; the UI editor removes them on save. Do not reintroduce them.
- **Step id validation**: ids must match `^[A-Za-z0-9_-]+$`, be unique, and have non-empty label/agent (`normalize_step_configs`).
- **Agent selection**: step `agents` lists are optional suggestions, never a whitelist. The TUI accepts any compatible effective catalog agent. New-mode workflow edits fork a project-specific copy; shared workflows are only changed explicitly.

## Conventions

- Flat importable modules with thin executable launchers. Keep code concise; use type hints, dataclasses/typed records, Ruff formatting (100 columns, Python 3.10 target) and the conventions in `docs/development.md`. Public resource-owning interfaces require explicit types. Preserve explicit compatibility re-exports. Keep new runtime dependencies out of routine refactors; development tools are configured separately.
- Runner ↔ UI contract is the event dict stream (`kind`: `run_start`, `session`, `step_start`, `step_done`, `command`, `line`, `error`, `opencode_event`, `run_done`). Keep payloads backward compatible when touching `pipeline_runner.py`.
- Rich markup in the UI is escaped with `rich.markup.escape` for all dynamic text; preserve that.
