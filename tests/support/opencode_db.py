"""Synthetic OpenCode SQLite schema and event builders."""

import json
import sqlite3
from pathlib import Path


def make_db(path: Path) -> sqlite3.Connection:
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


def add_session(
    db,
    sid,
    parent_id=None,
    agent="execution-plan",
    model=None,
    created=1000000,
    updated=1000000,
    title="t1: task.md",
):
    db.execute(
        "INSERT INTO session (id, parent_id, title, agent, model, time_created, time_updated, time_archived)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        (sid, parent_id, title, agent, model, created, updated),
    )


def add_part(db, sid, ts, payload):
    db.execute(
        "INSERT INTO part (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (f"{sid}-{ts}", sid, ts, ts, json.dumps(payload)),
    )
