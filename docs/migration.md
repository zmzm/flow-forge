# Migration and rollback

No production jobs, server settings or external repositories are migrated by
installing this change. Perform cutover deliberately after reviewing the preview.
Private agent prompts must remain outside this public repository.

## Register an existing queue in place

1. Stop the old timer/cron/manual workers and TUI before changing lock/state layout.
   Back up tasks.jsonl, steps.json, existing outputs, runs and the old service/env.
2. Register the existing paths without moving queue or outputs:

   ```bash
   flowforge project add existing --workspace /srv/projects/existing \
     --tasks-file /srv/projects/existing/tasks.jsonl \
     --workflow-file /srv/projects/existing/steps.json \
     --runs-dir /opt/flowforge/runs --month-gate
   ```

   Use the **actual current absolute runs directory** during cutover so both old
   and new processes see the same `.flowforge.lock`. New code additionally locks
   the canonical workspace and queue. Do not restart old workers pointing at
   another runs directory. Legacy `.env` remains usable by old invocations;
   explicitly selected projects do not load it. Move notification settings into a
   private central notifications.env or preserve them in the service environment.
3. Import the complete agent library as described below. Run doctor and inspect
   all dependencies before scheduling execution.
4. Compare `flowforge status --project existing --json` with legacy
   `watch-task.py --json`. Check existing output paths and failed-task resume on a
   disposable copy first. Registered JSONL projects preserve beside-source output
   discovery and require no artifact migration. Do not set artifacts_dir merely
   to rename storage; existing outputs will not appear there automatically.
5. Verify notifications with a disposable task/room. Use an explicit bounded command
   in the new service. JSONL projects must **omit --sync**. Confirm manual/TUI/service
   lock conflicts and inspect new status after a run before enabling the timer.
6. Roll back by stopping the new worker/timer and restoring the previous service
   and environment against the **same current queue and outputs**. New fields are
   additive; do not replace the queue with an old backup after useful work without
   reconciling new results. Keep current logs and imported library backups.

## Import the complete Mentori library

The local implementation audit found **11 Markdown agents**, rather than the
estimated six or seven: code-review, concept-plan, execution-plan, grounded-hard,
grounded-lite, grounded, orchestrator-simple, orchestrator, post-check, quick-fix,
and repair-mode. Their definitions were inspected, not published or replaced.

Real cutover is pending: definitions contain external knowledge-router/vault
references, project-specific documents and `.opencode/tasks` output permissions.
Two orchestration definitions also use `disabled`/`disbled` instead of OpenCode's
`disable`. These issues are reported without silently changing prompts/settings.
Resolve them in a private staging copy with owner review. Do not delete originals.

```bash
# SOURCE is the verified directory containing all Markdown agent definitions.
flowforge agent import /path/to/Mentori/.opencode/agents --json
# Default is preview; nothing is written, even on validation failure.
```

Preview returns every agent/source/destination/hash, identical/name-collision state,
dependencies, skipped/unsupported files, validation issues and ready. Nonzero exit 2
means it is not ready. Missing dependencies, symlinks, unsupported files and changed
destination contents block apply. Repeat imports never overwrite local edits.

Supply reusable resources explicitly, never the whole Mentori `.opencode` tree:

```bash
flowforge agent import /private/staging/agents \
  --resource /private/staging/prompts=prompts \
  --resource /private/staging/skills=skills --json
# Apply exactly the reviewed input after preview is ready:
flowforge agent import /private/staging/agents \
  --resource /private/staging/prompts=prompts \
  --resource /private/staging/skills=skills --apply --json
```

Resources must target resources/, prompts/ or skills/ under the library. Secret-
looking filenames, environment files, private keys, databases, caches and symlinks
are refused. Review resource contents; filename checks are not a secret scanner.
Supported agent files are Markdown. Every unsupported source file is reported,
never silently skipped. Complete agent bytes, including all YAML metadata, are
preserved in the authoritative library. The manifest reconciles all imported files.

For project-specific references use explicit substitutions, either in the import
or the selected project's `agent_paths` object. Import-time substitutions live in
private library/paths.json; original agent definitions remain byte-for-byte intact.
They are applied to execution snapshots and dependency validation only:

```bash
flowforge agent import /private/staging/agents \
  --path-map 'docs/PROJECT-RULES.md={{workspace}}/docs/PROJECT-RULES.md' --json
```

Mapped dependencies may be deferred until a project is selected. A successful import
is therefore **not** a claim that every project is portable: doctor must validate
the effective references for each target. Use `{{workspace}}` and
`{{artifacts_dir}}` in mapping values. Review mapping keys carefully: replacements
are literal across prompt/frontmatter. Map old knowledge-router/vault locations to
reviewed independent resources or explicitly configured project resources; merely
keeping an absolute dependency on Mentori is not a completed migration.

Apply stages a complete library next to its destination, verifies hashes, retains
an existing library as library.backup-UUID, then renames the stage into place. A
failed cutover restores the previous library. Stop workers while updating libraries
so no attempt reads a partially changed library. The first import has no prior
central library to back up; originals remain available at source. Imported resources
are copied, never symlinked. paths.json relocates references to the central copies.

After import:

1. Run `flowforge agent list --json` and reconcile every name/hash with preview.
   Inspect individual effective models/origins using agent show.
2. Run doctor for each registered project. Check edit permissions allow the new
   artifact paths; old planning-agent permissions may only allow Mentori outputs.
   Verify project documents, skills and MCP configuration separately.
3. Make the original directory unavailable **in a disposable fixture/copy** and
   run a non-default migrated agent against another disposable workspace using a
   one-step workflow. Confirm prompt/resources/models come from central library.
   The automated suite covers this with six fixture agents and no network.
4. After an independently verified live smoke test and approved cutover, archive
   the old definitions outside project agent discovery. Keep a recoverable backup.
   Future edits belong only in FlowForge's library. Do not automatically delete
   Mentori originals or continue loading them as the central source.
5. To roll back library changes, stop workers, preserve the current library under
   another name, restore library.backup-UUID to library, and rerun catalog/doctor.
   Restoring Mentori discovery is a separate deliberate rollback of the cutover.

## Scheduling and Hermes

See [project configuration and locks](projects.md), [Hermes interface](hermes.md)
and the [server guide](headless-server.md). The existing service/timer remain valid.
The new template service/timer serve directory-mode projects individually. Customize
paths, service user, PATH and environment first; no deployment is performed by the
repository. Cron can invoke the same absolute `flowforge run --project ...` command.
