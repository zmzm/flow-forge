"""Optional Textual pilot checks; the headless suite does not require Textual."""

import unittest

from tests import _bootstrap as _bootstrap

# isort: split

import flowforge_sync as discovery
import pipeline_runner as pr
from tests.support.projects import ROOT, ProjectCase

try:
    from textual.widgets import Input
except ImportError:
    Input = None

if Input is not None:
    import flowforge_ui as ui


@unittest.skipIf(Input is None, "Textual is optional; run with the UI virtual environment")
class ProjectUITests(ProjectCase, unittest.IsolatedAsyncioTestCase):
    async def test_quit_waits_for_attempt_finalization(self):
        from flowforge_storage import busy

        self.task()
        discovery.sync(self.context)
        task_id = pr.read_tasks_jsonl(self.context.tasks_file)[0]["id"]
        (self.workspace / "pause").touch()
        app = ui.TasksUI(self.context)
        async with app.run_test() as pilot:
            app._start_task_run(task_id)
            for _ in range(50):
                await pilot.pause(0.05)
                if app.current_session == "ses-live":
                    break
            self.assertEqual(app.current_session, "ses-live")
            await pilot.press("q")
            for _ in range(50):
                await pilot.pause(0.05)
                if not app.runner_active:
                    break
            self.assertFalse(app.runner_active)
        row = pr.read_tasks_jsonl(self.context.tasks_file)[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(len(row["history"]), 1)
        self.assertFalse(row["history"][0]["ok"])
        self.assertFalse(busy(self.context))

    async def test_any_catalog_agent_can_override_a_step_without_allowlist_edit(self):
        self.task()
        discovery.sync(self.context)
        task = pr.read_tasks_jsonl(self.context.tasks_file)[0]
        task["agents"] = {"concept": "custom"}
        app = ui.TasksUI(self.context)
        async with app.run_test() as pilot:
            results = []
            screen = ui.EditTaskScreen(task, self.context)
            app.push_screen(screen, results.append)
            await pilot.pause()
            self.assertTrue(screen.query_one("#agents", Input).value.startswith("custom"))
            self.assertIn("custom", screen._catalog_hint())
            screen.query_one("#agents", Input).value = "custom, repair, review"
            screen._save()
            await pilot.pause()
            self.assertEqual(
                results[0]["agents"],
                {"concept": "custom", "grounded": "repair", "execution": "review"},
            )
        self.assertEqual(self.context.steps(), pr.read_step_configs(ROOT / "steps.json"))

    async def test_task_workflow_can_have_arbitrary_step_count_and_repeat_agent(self):
        self.task()
        discovery.sync(self.context)
        task = pr.read_tasks_jsonl(self.context.tasks_file)[0]
        self.workflow("five", 5, "custom")
        app = ui.TasksUI(self.context)
        async with app.run_test() as pilot:
            results = []
            screen = ui.EditTaskScreen(task, self.context)
            app.push_screen(screen, results.append)
            await pilot.pause()
            screen.query_one("#workflow", Input).value = "five"
            await pilot.pause()
            self.assertEqual(len(screen.step_configs), 5)
            self.assertEqual(screen.query_one("#agents", Input).value, ", ".join(["custom"] * 5))
            screen._save()
            await pilot.pause()
            self.assertEqual(results[0]["workflow"], "five")
            self.assertEqual(list(results[0]["agents"].values()), ["custom"] * 5)

    async def test_tui_execution_uses_selected_context_and_shared_locks(self):
        self.task()
        discovery.sync(self.context)
        task_id = pr.read_tasks_jsonl(self.context.tasks_file)[0]["id"]
        app = ui.TasksUI(self.context)
        async with app.run_test() as pilot:
            app._start_task_run(task_id)
            for _ in range(50):
                await pilot.pause(0.05)
                if not app.runner_active:
                    break
            self.assertFalse(app.runner_active)
            row = pr.read_tasks_jsonl(self.context.tasks_file)[0]
            self.assertEqual(row["status"], "done")
            self.assertEqual(len(row["history"]), 1)
            self.assertTrue(row["history"][0]["ok"])
            self.assertEqual(row["history"][0]["attempt_id"], row["attempt_id"])
            self.assertIsNone(app.run_lock)
