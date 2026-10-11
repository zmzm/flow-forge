"""Tests for watch-task.py rendering against a synthetic opencode DB."""

import contextlib
import fcntl
import importlib.util
import io
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ImportError:
    from tests import _bootstrap  # noqa: F401

_spec = importlib.util.spec_from_file_location(
    "watch_task", Path(__file__).resolve().parent.parent / "watch-task.py"
)
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)


def _make_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, title TEXT,"
        " agent TEXT, model TEXT, time_created INTEGER, time_updated INTEGER, time_archived INTEGER)"
    )
    db.execute(
        "CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,"
        " time_updated INTEGER, data TEXT)"
    )
    return db


def _add_session(db, sid, parent_id=None, agent="execution-plan", model=None,
                 created=1000000, updated=1000000, title="t1: task.md"):
    db.execute(
        "INSERT INTO session (id, parent_id, title, agent, model, time_created, time_updated, time_archived)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        (sid, parent_id, title, agent, model, created, updated),
    )


def _add_part(db, sid, ts, payload):
    db.execute(
        "INSERT INTO part (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (f"{sid}-{ts}", sid, ts, ts, json.dumps(payload)),
    )


class ModelLabelTests(unittest.TestCase):
    def test_json_model_label(self):
        raw = json.dumps({"providerID": "zai-coding-plan", "id": "glm-5.3"})
        self.assertEqual(wt._model_label(raw), "zai-coding-plan/glm-5.3")

    def test_missing_and_plain(self):
        self.assertEqual(wt._model_label(None), "-")
        self.assertEqual(wt._model_label("glm-4.6"), "glm-4.6")


class RenderReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flowforge-watch-"))
        self.db_path = self.tmp / "opencode.db"
        self.db = _make_db(self.db_path)
        now = int(time.time() * 1000)
        self.now = now
        _add_session(
            self.db, "ses-parent0001", agent="execution-plan",
            model=json.dumps({"providerID": "zai-coding-plan", "id": "glm-5.3"}),
            created=now - 600_000, updated=now - 2_000, title="t1: task.md",
        )
        _add_session(
            self.db, "ses-child00001", parent_id="ses-parent0001", agent="general",
            model=json.dumps({"providerID": "zai-coding-plan", "id": "glm-4.7"}),
            created=now - 300_000, updated=now - 1_000, title="",
        )
        _add_part(self.db, "ses-parent0001", now - 5_000, {"type": "tool", "tool": "read", "state": {"status": "completed"}})
        _add_part(self.db, "ses-child00001", now - 1_000, {"type": "text", "text": "Chunk B in progress\nsecond line"})
        self.db.commit()
        self.tasks = [
            {
                "id": "t1",
                "status": "running",
                "session_id": "ses-parent0001",
                "last_run_log": str(self.tmp / "run.log"),
            }
        ]
        (self.tmp / "run.log").write_text("x", encoding="utf-8")

    def test_report_includes_task_session_and_subagent(self):
        report = wt.render_report(tasks=self.tasks, db=self.db, window_minutes=15, parts=3)
        self.assertIn("t1", report)
        self.assertIn(wt._short("ses-parent0001"), report)
        self.assertIn("agent=execution-plan", report)
        self.assertIn("zai-coding-plan/glm-5.3", report)
        self.assertIn("tool read [completed]", report)
        self.assertIn("ses-child00001", report)
        self.assertIn("Chunk B in progress", report)

    def test_unrelated_active_session_listed_separately(self):
        _add_session(self.db, "ses-other0001", agent="build", created=self.now - 60_000, updated=self.now - 3_000)
        self.db.commit()
        report = wt.render_report(tasks=self.tasks, db=self.db, window_minutes=15, parts=1)
        self.assertIn("Other active sessions", report)
        self.assertIn("ses-other0001", report)

    def test_stale_session_outside_window_hidden(self):
        _add_session(self.db, "ses-stale0001", agent="build", created=self.now - 3_600_000, updated=self.now - 3_600_000)
        self.db.commit()
        report = wt.render_report(tasks=self.tasks, db=self.db, window_minutes=15, parts=1)
        self.assertNotIn("ses-stale0001", report)

    def test_missing_session_in_db(self):
        self.tasks[0]["id"] = "zz9"
        self.tasks[0]["session_id"] = "ses-unknown1"
        report = wt.render_report(tasks=self.tasks, db=self.db, window_minutes=15, parts=1)
        self.assertIn("not found in db", report)

    def test_stale_recorded_session_falls_back_to_active_title_match(self):
        self.tasks[0]["session_id"] = "ses-gone00001"
        self.db.execute(
            "UPDATE session SET time_updated = ? WHERE id IN ('ses-parent0001', 'ses-child00001')",
            (self.now - 3_600_000,),
        )
        _add_session(
            self.db, "ses-active0001", agent="execution-plan",
            created=self.now - 60_000, updated=self.now - 1_000, title="t1: task.md",
        )
        self.db.commit()
        report = wt.render_report(tasks=self.tasks, db=self.db, window_minutes=15, parts=1)
        self.assertIn("ses-active0001", report)
        self.assertNotIn("ses-parent0001", report)
        self.assertNotIn("Other active sessions", report)

    def test_subagents_not_duplicated_in_other_section(self):
        report = wt.render_report(tasks=self.tasks, db=self.db, window_minutes=15, parts=1)
        self.assertEqual(report.count("ses-child00001"), 1)

    def test_no_db_reports_unavailable(self):
        report = wt.render_report(tasks=self.tasks, db=None, window_minutes=15, parts=1)
        self.assertIn("session: unknown", report)
        self.assertIn("active sessions unavailable", report)

    def test_no_running_tasks(self):
        report = wt.render_report(tasks=[{"id": "t9", "status": "todo"}], db=self.db, window_minutes=15, parts=1)
        self.assertIn("Running tasks (0):", report)
        self.assertIn("none", report)


class BuildStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flowforge-json-"))
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self._orig = {
            name: getattr(wt.pr, name)
            for name in ("TASKS_FILE", "STEPS_FILE", "PROJECT_DIR", "RUNS_DIR")
        }
        wt.pr.PROJECT_DIR = self.project
        wt.pr.RUNS_DIR = self.runs
        (self.project / "task-a.md").write_text("# First task title\n\nbody\n", encoding="utf-8")
        (self.project / "task-b.md").write_text("no heading here\n", encoding="utf-8")
        self.tasks_file = self.tmp / "tasks.jsonl"
        self.tasks_file.write_text(
            "\n".join(
                json.dumps(row)
                for row in [
                    {"id": "t-a", "task_file": "task-a.md", "status": "todo"},
                    {"id": "t-b", "task_file": "task-b.md", "status": "todo"},
                    {"id": "t-c", "task_file": "task-c.md", "status": "done"},
                    {"id": "t-d", "task_file": "task-a.md", "status": "failed", "session_id": "ses-d"},
                ]
            ) + "\n",
            encoding="utf-8",
        )
        self.steps_file = self.tmp / "steps.json"
        self.steps_file.write_text(
            json.dumps(
                {
                    "steps": [
                        {"id": "concept", "label": "Business plan", "agent": "concept-plan"},
                        {"id": "execution", "label": "Execution", "agent": "execution-plan"},
                    ]
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(wt.pr, name, value)

    def hold_lock(self):
        handle = (self.runs / ".flowforge.lock").open("w")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle

    def test_schema_queue_and_titles(self):
        status = wt.build_status(tasks_file=self.tasks_file)
        self.assertEqual(status["schema_version"], 1)
        self.assertFalse(status["lock_held"])
        self.assertIsNone(status["running"])
        self.assertEqual(status["queue_total"], 2)
        by_id = {row["id"]: row for row in status["tasks"]}
        self.assertEqual(by_id["t-a"]["title"], "First task title")
        self.assertIsNone(by_id["t-b"]["title"])
        self.assertEqual(by_id["t-c"]["status"], "done")

    def test_lock_held_reported(self):
        handle = self.hold_lock()
        try:
            status = wt.build_status(tasks_file=self.tasks_file)
        finally:
            handle.close()
        self.assertTrue(status["lock_held"])

    def test_stale_running_when_lock_free(self):
        wt.pr.write_tasks_jsonl(
            self.tasks_file,
            [{"id": "t-a", "task_file": "task-a.md", "status": "running", "session_id": "ses-a"}],
        )
        status = wt.build_status(tasks_file=self.tasks_file)
        self.assertIsNone(status["running"])
        self.assertEqual(status["stale_running_task_id"], "t-a")

    def test_running_stage_duration_and_session(self):
        log = self.runs / "20260918_084849_t-a.log"
        log.write_text(
            "$ opencode run --format json --agent concept-plan --title 't-a: task-a.md' do\n"
            '{"type":"text"}\n'
            "$ opencode run --format json --agent execution-plan --title 't-a: task-a.md' do\n",
            encoding="utf-8",
        )
        wt.pr.write_tasks_jsonl(
            self.tasks_file,
            [{"id": "t-a", "task_file": "task-a.md", "status": "running", "session_id": "ses-a"}],
        )
        handle = self.hold_lock()
        try:
            now_ms = int(datetime.now().timestamp() * 1000)
            status = wt.build_status(tasks_file=self.tasks_file, now_ms=now_ms)
        finally:
            handle.close()
        running = status["running"]
        self.assertEqual(running["id"], "t-a")
        self.assertEqual(running["title"], "First task title")
        self.assertEqual(running["session_id"], "ses-a")
        self.assertEqual(running["stage"], "Execution")
        self.assertEqual(running["log_path"], str(log))
        expected = datetime.strptime("20260918_084849", "%Y%m%d_%H%M%S").timestamp()
        self.assertAlmostEqual(running["duration_sec"], now_ms / 1000 - expected, delta=1.0)

    def test_last_failed_quota_and_error(self):
        log = self.runs / "20260918_090918_t-d.log"
        log.write_text(
            '{"type":"error","error":{"data":{"message":"Usage limit reached for 5 hour.'
            ' Your limit will reset at 2026-09-18 19:00:48"}}}\n'
            "Pipeline error: Step 'Execution' failed: APIError: quota\n",
            encoding="utf-8",
        )
        status = wt.build_status(tasks_file=self.tasks_file)
        failed = status["last_failed"]
        self.assertEqual(failed["id"], "t-d")
        self.assertEqual(failed["title"], "First task title")
        self.assertEqual(failed["quota_reset_at"], "2026-09-18 19:00:48")
        self.assertIn("Step 'Execution' failed", failed["error"])

    def test_last_failed_without_log(self):
        status = wt.build_status(tasks_file=self.tasks_file)
        self.assertEqual(status["last_failed"]["id"], "t-d")
        self.assertNotIn("quota_reset_at", status["last_failed"])
        self.assertNotIn("error", status["last_failed"])

    def test_missing_tasks_file(self):
        status = wt.build_status(tasks_file=self.tmp / "nope.jsonl")
        self.assertEqual(status["tasks"], [])
        self.assertEqual(status["queue_total"], 0)
        self.assertFalse(status["lock_held"])

    def test_main_json_flag_prints_status(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = wt.main(["--json", "--tasks-file", str(self.tasks_file)])
        self.assertEqual(code, 0)
        status = json.loads(buffer.getvalue())
        self.assertEqual(status["schema_version"], 1)
        self.assertEqual(status["queue_total"], 2)


if __name__ == "__main__":
    unittest.main()
