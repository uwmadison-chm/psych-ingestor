"""The SQLite database of runtime state: runs, and which events have been stored.

Small on purpose. The data lives in files; this is the bookkeeping that makes retries safe
and lets the CLI find work to do.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    task_code        TEXT NOT NULL,
    run_key          TEXT NOT NULL,
    run_number       INTEGER NOT NULL,
    parameters       TEXT NOT NULL,
    extra_parameters TEXT NOT NULL,
    status           TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    finalized_at     TEXT,
    filed_at         TEXT,
    dataset_path     TEXT,
    UNIQUE (task_code, run_key, run_number)
);

CREATE INDEX IF NOT EXISTS runs_by_status ON runs (status, task_code);

-- The uniqueness constraint lives here rather than in a check the application does
-- before writing: two retries of the same request can be in flight at once, and a
-- check-then-write between them writes the line twice.
CREATE TABLE IF NOT EXISTS events (
    run_id       TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    stored_at    TEXT NOT NULL,
    PRIMARY KEY (run_id, event_id)
);
"""


def now() -> str:
    """The current time, as the ISO-8601 string we store everywhere."""
    return datetime.now(UTC).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    """Open the database, creating it if it isn't there yet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.executescript(SCHEMA)
    return connection
