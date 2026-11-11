"""Execution regressions using isolated projects and local OpenCode fixtures."""

import subprocess
import time
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tests import _bootstrap as _bootstrap

# isort: split

import flowforge_cli as cli
import flowforge_opencode as opencode
import flowforge_storage as storage
import flowforge_sync as discovery
import headless_runner as hr
import pipeline_runner as pr
from flowforge_execution import Execution
from notifications import NullNotifier
from tests.support.projects import FAKE, ProjectCase


class ExecutionRegressions(ProjectCase):
    def setUp(self):
        super().setUp()
        self.workflow("single", 1, "review")
        self.context = replace(self.context, workflow="single", heartbeat=None)
        self.source_file = self.task()
        discovery.sync(self.context)
        self.task_id = pr.read_tasks_jsonl(self.context.tasks_file)[0]["id"]

    def run_one(self, callback=None):
        lock = storage.acquire_execution(self.context)
        self.assertIsNotNone(lock)
        try:
            return pr.run_selected_task(self.task_id, callback=callback, context=self.context)
        finally:
            lock.close()

    def calls(self):
        path = self.workspace / "calls.jsonl"
        return len(path.read_text().splitlines()) if path.exists() else 0

    def test_partial_output_from_failed_step_is_reexecuted(self):
        self.binary.write_text(FAKE.replace("if not (workspace / 'fail').exists():", "if True:"))
        (self.workspace / "fail").touch()
        self.assertFalse(self.run_one().ok)
        (self.workspace / "fail").unlink()
        self.assertTrue(self.run_one().ok)
        self.assertEqual(self.calls(), 2)

    def test_successful_checkpoint_is_reused_but_modified_output_is_not(self):
        self.assertTrue(self.run_one().ok)
        storage.update_task(self.context.tasks_file, self.task_id, {"status": "failed"})
        self.assertTrue(self.run_one().ok)
        self.assertEqual(self.calls(), 1)
        row = pr.read_tasks_jsonl(self.context.tasks_file)[0]
        Path(row["outputs"]["s0"]).write_text("damaged output")
        storage.update_task(self.context.tasks_file, self.task_id, {"status": "failed"})
        self.assertTrue(self.run_one().ok)
        self.assertEqual(self.calls(), 2)

    def test_workflow_revision_invalidates_checkpoint(self):
        self.assertTrue(self.run_one().ok)
        storage.atomic_json(
            self.root / "workflows/single.json",
            {
                "steps": [
                    {"id": "s0", "label": "Changed", "agent": "review", "model": "fixture/other"},
                ]
            },
        )
        storage.update_task(self.context.tasks_file, self.task_id, {"status": "failed"})
        self.assertTrue(self.run_one().ok)
        self.assertEqual(self.calls(), 2)
        self.assertNotIn("--session", (self.workspace / "calls.jsonl").read_text().splitlines()[-1])

    def test_source_revision_invalidates_jsonl_checkpoint(self):
        self.context = replace(self.context, tasks_dir=None)
        self.assertTrue(self.run_one().ok)
        self.source_file.write_text("# Changed source")
        storage.update_task(self.context.tasks_file, self.task_id, {"status": "failed"})
        self.assertTrue(self.run_one().ok)
        self.assertEqual(self.calls(), 2)
        self.assertNotIn("--session", (self.workspace / "calls.jsonl").read_text().splitlines()[-1])

    def test_agent_revision_invalidates_checkpoint(self):
        self.assertTrue(self.run_one().ok)
        agent = self.library / "agents/review.md"
        agent.write_text(agent.read_text() + "\nChanged instructions\n")
        storage.update_task(self.context.tasks_file, self.task_id, {"status": "failed"})
        self.assertTrue(self.run_one().ok)
        self.assertEqual(self.calls(), 2)

    def test_explicit_failed_rerun_invalidates_previous_checkpoint(self):
        self.assertTrue(self.run_one().ok)
        storage.update_task(self.context.tasks_file, self.task_id, {"run_steps": ["s0"]})
        (self.workspace / "fail").touch()
        self.assertFalse(self.run_one().ok)
        (self.workspace / "fail").unlink()
        storage.update_task(self.context.tasks_file, self.task_id, {}, remove=("run_steps",))
        self.assertTrue(self.run_one().ok)
        self.assertEqual(self.calls(), 3)

    def test_execution_lease_records_success_and_observer_failure_once(self):
        owner = Execution.open(self.context)
        self.assertIsNotNone(owner)
        with owner:
            self.assertIsNone(Execution.open(self.context))
            self.assertTrue(owner.run_task(self.task_id).ok)

            def observer(event):
                if event["kind"] == "run_start":
                    raise RuntimeError("UI unavailable")

            with self.assertRaisesRegex(RuntimeError, "UI unavailable"):
                owner.run_task(self.task_id, callback=observer)
        row = pr.read_tasks_jsonl(self.context.tasks_file)[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual([attempt["ok"] for attempt in row["history"]], [True, False])
        self.assertNotEqual(row["history"][0]["attempt_id"], row["history"][1]["attempt_id"])
        self.assertFalse(storage.busy(self.context))

    def test_batch_uses_lease_context_without_separate_context_argument(self):
        with Execution.open(self.context) as owner:
            outcome = hr.run_batch(hr.BatchConfig(), NullNotifier(), execution=owner, quiet=True)
        self.assertEqual(outcome.completed, 1)
        row = pr.read_tasks_jsonl(self.context.tasks_file)[0]
        self.assertEqual(len(row["history"]), 1)

    def test_rejected_source_change_does_not_create_duplicate_attempt(self):
        with Execution.open(self.context) as owner:
            self.assertTrue(owner.run_task(self.task_id).ok)
            self.source_file.write_text("# Changed without syncing")
            with self.assertRaisesRegex(ValueError, "Task source changed"):
                owner.run_task(self.task_id)
        row = pr.read_tasks_jsonl(self.context.tasks_file)[0]
        self.assertEqual(row["status"], "done")
        self.assertEqual(len(row["history"]), 1)

    def test_stop_escalates_when_process_ignores_sigterm(self):
        self.binary.write_text("""#!/usr/bin/env python3
import json, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(json.dumps({'type': 'event', 'sessionID': 'ses-stubborn'}), flush=True)
time.sleep(60)
""")
        control = pr.RunControl()

        def observer(event):
            if event["kind"] == "session":
                control.request_stop()

        started = time.monotonic()
        with mock.patch.object(opencode, "GRACE_SECONDS", 0.1):
            with Execution.open(self.context) as owner:
                result = owner.run_task(self.task_id, callback=observer, control=control)
        self.assertFalse(result.ok)
        self.assertIn("stopped", result.message)
        self.assertLess(time.monotonic() - started, 5)
        row = pr.read_tasks_jsonl(self.context.tasks_file)[0]
        self.assertEqual(row["session_id"], "ses-stubborn")
        self.assertEqual(row["checkpoints"], {})

    def test_timeout_fails_even_if_signal_handler_exits_zero(self):
        self.binary.write_text("""#!/usr/bin/env python3
import json, signal, sys, time
signal.signal(signal.SIGTERM, lambda *args: sys.exit(0))
print(json.dumps({'type': 'event', 'sessionID': 'ses-timeout'}), flush=True)
time.sleep(60)
""")
        self.context = replace(self.context, step_timeout=0.2)
        control = pr.RunControl()
        events = []
        with Execution.open(self.context) as owner:
            result = owner.run_task(self.task_id, callback=events.append, control=control)
        self.assertFalse(result.ok)
        self.assertFalse(control.stop_requested)
        self.assertTrue(any("timed out" in event.get("text", "") for event in events))

    def test_observer_exception_reaps_child_and_preserves_log(self):
        (self.workspace / "pause").touch()
        children = []
        real_popen = subprocess.Popen

        def capture(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            children.append(proc)
            return proc

        def observer(event):
            if event["kind"] in ("opencode_event", "line"):
                raise RuntimeError("observer unavailable")

        try:
            with mock.patch.object(pr.subprocess, "Popen", side_effect=capture):
                with self.assertRaisesRegex(RuntimeError, "observer unavailable"):
                    self.run_one(observer)
            self.assertIsNotNone(children[0].poll(), "child survived execution ownership")
            row = pr.read_tasks_jsonl(self.context.tasks_file)[0]
            self.assertEqual(row["status"], "failed")
            self.assertIn("ses-live", Path(row["last_run_log"]).read_text())
        finally:
            for proc in children:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)

    def test_events_are_in_log_before_observer_receives_them(self):
        seen = []
        log_path = None

        def observer(event):
            nonlocal log_path
            if event["kind"] == "run_start":
                log_path = Path(event["log_path"])
            if event["kind"] == "opencode_event":
                seen.append("ses-review" in log_path.read_text())

        self.assertTrue(self.run_one(observer).ok)
        self.assertEqual(seen, [True])

    def test_explicit_run_ignores_missing_completed_source(self):
        self.assertTrue(self.run_one().ok)
        self.source_file.unlink()
        self.task("b")
        discovery.sync(self.context)
        other_id = pr.read_tasks_jsonl(self.context.tasks_file)[1]["id"]
        args = cli.parser().parse_args(["run", "--project", "one", "--task-id", other_id, "--json"])
        result, code = cli.execute(args, self.context)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(len(pr.read_tasks_jsonl(self.context.tasks_file)[1]["history"]), 1)
        self.assertFalse(
            cli.preflight(self.context)["ok"], "doctor must still report missing sources"
        )
