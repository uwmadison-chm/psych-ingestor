"""The SQLite database of runtime state: runs, and receipts for events already stored.

Small on purpose. The data lives in files; this is the bookkeeping that makes retries safe
and lets the CLI find work to do. It's the record for a run in flight, and once the sweep
has finished a run, the manifest in its directory is the record instead. The two never
overlap, so they can't disagree.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

# Bumped whenever the tables below change shape. There's no migration: a database from a
# different version is refused with a message saying so, which beats a confusing "no such
# column" from the first query that touches it.
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    task_code        TEXT NOT NULL,

    -- A hash of the run key's values, not the values themselves. It's only ever compared —
    -- for the next run number and for the uniqueness constraint below — and nothing parses
    -- it apart, so storing it readable would just make this table one more place
    -- identifiers accumulate. The hash isn't protection: the values are one join away, in
    -- run_identifiers.
    run_key_hash     TEXT NOT NULL,
    run_number       INTEGER NOT NULL,

    -- Two columns rather than one status, because they answer different questions and
    -- outlive each other. `phase` is how far along Pig's own bookkeeping is, and stops
    -- mattering once a run is done. `disposition` is why the run stopped accepting data,
    -- which is the fact that still matters to whoever reads the data later.
    phase            TEXT NOT NULL CHECK (phase IN ('collecting', 'closed', 'done')),
    disposition      TEXT          CHECK (disposition IN ('finalized', 'expired')),

    started_at       TEXT NOT NULL,
    closed_at        TEXT,
    done_at          TEXT,

    UNIQUE (task_code, run_key_hash, run_number),

    -- The timestamps annotate transitions; they never decide which phase a run is in.
    -- These say so, so the database refuses a row where the two disagree instead of
    -- leaving it to be noticed later.
    CHECK ((phase = 'collecting') = (disposition IS NULL)),
    CHECK ((phase = 'collecting') = (closed_at IS NULL)),
    CHECK ((phase = 'done') = (done_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS runs_by_phase ON runs (phase, task_code);

-- Everything a participant's link supplied, and nothing else. Kept apart from `runs` so
-- that direct identifiers live in exactly one table — a structural fact rather than a
-- remembered one. (`runs` still holds timestamps, which are quasi-identifiers; this is
-- "direct identifiers here", not "that table is anonymous".)
--
-- `run_key_names` is the ordered list of parameter names that made up the run key when
-- the run started. It's captured here because the sweep builds the manifest from this
-- database alone, and the task definition can change — or vanish — between run start and
-- sweep.
CREATE TABLE IF NOT EXISTS run_identifiers (
    run_id           TEXT PRIMARY KEY REFERENCES runs (run_id),
    parameters       TEXT NOT NULL,
    extra_parameters TEXT NOT NULL,
    run_key_names    TEXT NOT NULL
);

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


class DatabaseProblem(Exception):
    """The database file can't be used, and we can say why."""


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
    """Open the database, creating it if it isn't there yet.

    Raises DatabaseProblem for a database some other version of Pig made.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")

    _check_version(connection, path)
    connection.executescript(SCHEMA)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return connection


def _check_version(connection: sqlite3.Connection, path: Path) -> None:
    """Refuse a database whose tables aren't the ones this Pig expects.

    SQLite keeps a small integer in the file header for exactly this. A brand-new file
    reads 0 and has no tables; a file from before the version was recorded reads 0 and
    does have tables, which is how the two are told apart.
    """
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    has_tables = connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
    ).fetchone()[0]
    if version == 0 and not has_tables:
        return  # Brand new. The schema is about to be created.
    connection.close()
    raise DatabaseProblem(
        f"{path} was made by a different version of Pig (its schema version is "
        f"{version}; this Pig uses {SCHEMA_VERSION}), and there's no migration between "
        "them. If nothing in it matters, delete it and Pig will make a new one."
    )
