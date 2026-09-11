"""The SQLite database of runtime state: runs, and receipts for events already stored.

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

    -- Two columns rather than one status, because they answer different questions and
    -- outlive each other. `phase` is how far along Pig's own bookkeeping is, and stops
    -- mattering once a run is done. `disposition` is why the run stopped accepting data,
    -- which is the fact that still matters to whoever reads the data later.
    phase            TEXT NOT NULL CHECK (phase IN ('collecting', 'closed', 'done')),
    disposition      TEXT          CHECK (disposition IN ('finalized', 'expired')),

    started_at       TEXT NOT NULL,
    closed_at        TEXT,
    filed_at         TEXT,
    dataset_path     TEXT,

    UNIQUE (task_code, run_key, run_number),

    -- The timestamps annotate transitions; they never decide which phase a run is in.
    -- These say so, so the database refuses a row where the two disagree instead of
    -- leaving it to be noticed later.
    CHECK ((phase = 'collecting') = (disposition IS NULL)),
    CHECK ((phase = 'collecting') = (closed_at IS NULL)),
    CHECK ((phase = 'done') = (filed_at IS NOT NULL AND dataset_path IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS runs_by_phase ON runs (phase, task_code);

-- One row per event Pig has durably written: proof it's on disk, and the hash that tells
-- a retry apart from two different events sharing an ID. It holds no event data.
--
-- The uniqueness constraint lives here rather than in a check the application does
-- before writing: two retries of the same request can be in flight at once, and a
-- check-then-write between them writes the line twice.
CREATE TABLE IF NOT EXISTS event_receipts (
    run_id       TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    stored_at    TEXT NOT NULL,
    PRIMARY KEY (run_id, event_id)
);
"""


def now() -> datetime:
    """The current time. Aware, and in UTC, like everything Pig stores."""
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    """How a time is written to the database: ISO-8601, always carrying its offset."""
    return moment.isoformat()


def parse_time(stored: str) -> datetime:
    """Read a stored time back as an aware UTC datetime.

    A value with no offset is read as UTC rather than left naive. Everything Pig writes
    carries one, but a row edited by hand might not, and one naive datetime in the runs
    table would make the sweep's arithmetic raise `TypeError` — taking down the whole
    sweep rather than skipping the run it came from.
    """
    moment = datetime.fromisoformat(stored)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


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
