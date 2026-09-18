"""Runs: what the database knows about one, and everything that reads or changes it.

A `Run` is a plain value — what the runs table said at the moment it was read. Nothing on
it talks to the database. The functions below do, and every one takes the connection it
should use. That's deliberate: the service opens a connection per request and closes it
when the request ends (see `app.py`), so a run object outliving its connection is normal,
and a run that could quietly reach for a closed one would be a bug waiting to happen.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from . import db

MAX_RUN_NUMBER = 9999


class Phase(StrEnum):
    """How far along Pig's own bookkeeping is for a run.

    `collecting` is the only phase that takes events. A run leaves it either because the
    task finalized it or because it stayed open longer than its task allows — its
    `Disposition` says which. `done` means the sweep has finished the run — written its
    manifest and moved its directory to `done/` — and Pig has no work left for it.
    """

    COLLECTING = "collecting"
    CLOSED = "closed"
    DONE = "done"


class Disposition(StrEnum):
    """Why a run stopped accepting data. There isn't one while it still is.

    This is the fact that outlives the run. `finalized` carries the task's word that it
    sent everything it had; `expired` means the dataset holds whatever arrived before time
    ran out. Finishing the run doesn't change it, which is why it's separate from the
    phase, and why it's the one the manifest carries.
    """

    FINALIZED = "finalized"
    EXPIRED = "expired"


# The status words Pig reports to tasks, prints in the CLI, and documents in `api.md`.
# Derived from phase and disposition by `Run.api_status` and nowhere else.
API_STATUSES = ("in_progress", "finalizing", "complete", "expired")


def api_status(phase: Phase, disposition: Disposition | None) -> str:
    """The one place phase and disposition become the status a task sees.

    The four-word vocabulary predates the split and is a documented contract, so it's
    derived rather than stored. Note the asymmetry it carries: `finalizing` and `complete`
    distinguish a finalized run waiting for the sweep from one the sweep has finished,
    and `expired` covers both of those for an expired run. That's deliberate — a task
    that didn't finalize a run has nothing to do differently either way — and `pig runs`
    shows the phase for the operator who does care.
    """
    if phase is Phase.COLLECTING:
        return "in_progress"
    if disposition is Disposition.EXPIRED:
        return "expired"
    return "complete" if phase is Phase.DONE else "finalizing"


class RequestProblem(Exception):
    """Something about the request means we can't do it. Carries what to tell the task.

    It carries an HTTP status code because Pig's refusals map one-to-one onto responses,
    and a translation layer between the two would have exactly one caller.
    """

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True, slots=True)
class Run:
    """One run as the database knows it: its `runs` row joined to its identifiers."""

    run_id: str
    task_code: str
    run_key_hash: str
    run_number: int
    parameters: dict[str, str]
    extra_parameters: dict[str, Any]
    run_key_names: list[str]
    phase: Phase
    started_at: datetime
    disposition: Disposition | None = None
    closed_at: datetime | None = None
    done_at: datetime | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Run:
        return cls(
            run_id=row["run_id"],
            task_code=row["task_code"],
            run_key_hash=row["run_key_hash"],
            run_number=row["run_number"],
            parameters=json.loads(row["parameters"]),
            extra_parameters=json.loads(row["extra_parameters"]),
            run_key_names=json.loads(row["run_key_names"]),
            phase=Phase(row["phase"]),
            started_at=db.parse_time(row["started_at"]),
            disposition=(
                Disposition(row["disposition"]) if row["disposition"] else None
            ),
            closed_at=_maybe_time(row["closed_at"]),
            done_at=_maybe_time(row["done_at"]),
        )

    @property
    def accepting_data(self) -> bool:
        """Whether this run still takes events."""
        return self.phase is Phase.COLLECTING

    @property
    def api_status(self) -> str:
        """What a task is told this run's status is."""
        return api_status(self.phase, self.disposition)

    def is_past_its_limit(self, expires_after: int, now: datetime) -> bool:
        """Whether this run has been open longer than its task allows.

        Counted from when the run started, not from its last event, so nothing can hold a
        run open forever by continuing to send.
        """
        return self.accepting_data and now - self.started_at >= timedelta(
            seconds=expires_after
        )


def _maybe_time(stored: str | None) -> datetime | None:
    return db.parse_time(stored) if stored else None


def hash_run_key(values: list[str]) -> str:
    """The stored form of a run key: a hash of its values, in run-key order.

    Compared exactly, so the values have to match exactly: `PPT-1003` and `ppt-1003` are
    two different participants. Pig used to lowercase here, and stopped — that was a
    silent edit to a participant's identity, justified by a filename problem this layer
    no longer has. See docs/definitions.md.
    """
    joined = "\x1f".join(values)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------ reading runs

# Every read joins the two tables, so a `Run` always has its parameters.
_SELECT = (
    "SELECT runs.*, run_identifiers.parameters, run_identifiers.extra_parameters, "
    "run_identifiers.run_key_names FROM runs "
    "JOIN run_identifiers ON run_identifiers.run_id = runs.run_id"
)


def get(connection: sqlite3.Connection, run_id: str) -> Run | None:
    """One run by ID, or None if there's no run with that ID."""
    row = connection.execute(f"{_SELECT} WHERE runs.run_id = ?", (run_id,)).fetchone()
    return Run.from_row(row) if row else None


def awaiting_sweep(connection: sqlite3.Connection) -> list[Run]:
    """Every run the next sweep has to finish, oldest first.

    This is the whole definition, in one place: a run that has stopped accepting data and
    isn't done yet is exactly a run in `closed`.
    """
    rows = connection.execute(
        f"{_SELECT} WHERE phase = ? ORDER BY closed_at", (Phase.CLOSED,)
    ).fetchall()
    return [Run.from_row(row) for row in rows]


def collecting(connection: sqlite3.Connection) -> list[Run]:
    """Every run still taking events."""
    rows = connection.execute(
        f"{_SELECT} WHERE phase = ?", (Phase.COLLECTING,)
    ).fetchall()
    return [Run.from_row(row) for row in rows]


def recent_first(
    connection: sqlite3.Connection, task_code: str | None = None
) -> list[Run]:
    """Every run, most recently started first. For the CLI."""
    query = _SELECT
    values: list[Any] = []
    if task_code:
        query += " WHERE task_code = ?"
        values.append(task_code)
    query += " ORDER BY started_at DESC"
    return [Run.from_row(row) for row in connection.execute(query, values)]


# ------------------------------------------------------------------------ writing runs


def insert(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    task_code: str,
    run_key_hash: str,
    run_key_names: list[str],
    parameters: dict[str, str],
    extra_parameters: dict[str, Any],
    started_at: datetime,
) -> int:
    """Give the run the next number for its key, and write it down.

    Two participants starting at the same instant can pick the same number; the unique
    constraint catches that and we try again.

    The number comes from every row for this key, whatever its phase. Run rows outlive
    their data as counters: delete one and the next run of that key is numbered 1 again,
    with nothing to say so. That's why purging a run means deleting its files, never
    its row.
    """
    for _ in range(10):
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT MAX(run_number) AS highest FROM runs "
                "WHERE task_code = ? AND run_key_hash = ?",
                (task_code, run_key_hash),
            ).fetchone()
            run_number = (row["highest"] or 0) + 1
            if run_number > MAX_RUN_NUMBER:
                raise RequestProblem(
                    409,
                    f"This participant already has {MAX_RUN_NUMBER} runs of "
                    f"{task_code!r}, which is as many as run numbers go.",
                )
            connection.execute(
                "INSERT INTO runs (run_id, task_code, run_key_hash, run_number, phase, "
                "started_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    task_code,
                    run_key_hash,
                    run_number,
                    Phase.COLLECTING,
                    db.stamp(started_at),
                ),
            )
            connection.execute(
                "INSERT INTO run_identifiers (run_id, parameters, extra_parameters, "
                "run_key_names) VALUES (?, ?, ?, ?)",
                (
                    run_id,
                    json.dumps(parameters),
                    json.dumps(extra_parameters),
                    json.dumps(run_key_names),
                ),
            )
            connection.execute("COMMIT")
            return run_number
        except sqlite3.IntegrityError:
            connection.execute("ROLLBACK")
            continue
        except Exception:
            connection.execute("ROLLBACK")
            raise
    raise RequestProblem(
        503, "Couldn't get a run number for this participant. Try again."
    )


def mark_closed(
    connection: sqlite3.Connection,
    run: Run,
    disposition: Disposition,
    when: datetime,
) -> bool:
    """Stop a run accepting data, recording why.

    Both ways a run can close come through here — the task finalizing it and the sweep
    expiring it — because they differ only in the disposition. Returns whether this call
    was the one that closed it, so a caller racing another process can tell.
    """
    cursor = connection.execute(
        "UPDATE runs SET phase = ?, disposition = ?, closed_at = ? "
        "WHERE run_id = ? AND phase = ?",
        (Phase.CLOSED, disposition, db.stamp(when), run.run_id, Phase.COLLECTING),
    )
    return cursor.rowcount == 1


def mark_done(connection: sqlite3.Connection, run: Run, when: datetime) -> bool:
    """Record that this run's directory is in `done/` and Pig is finished with it.

    Called after the directory has actually moved, never before — see the ordering note
    in `sweep.py`. The disposition is left alone: finishing doesn't change why a run
    ended. Nothing records where the directory is, because that follows from the run:
    `done/{task_code}/{run_id}/` under whatever the data root is today.
    """
    cursor = connection.execute(
        "UPDATE runs SET phase = ?, done_at = ? WHERE run_id = ? AND phase = ?",
        (Phase.DONE, db.stamp(when), run.run_id, Phase.CLOSED),
    )
    return cursor.rowcount == 1


# ------------------------------------------------------------------------- counting
# These count runs rather than producing them, so they aren't properties of one.


def counts_by_api_status(
    connection: sqlite3.Connection, task_code: str
) -> dict[str, int]:
    """How many runs of this task are in each status the API reports."""
    counts = {status: 0 for status in API_STATUSES}
    for row in connection.execute(
        "SELECT phase, disposition, COUNT(*) AS count FROM runs WHERE task_code = ? "
        "GROUP BY phase, disposition",
        (task_code,),
    ):
        disposition = Disposition(row["disposition"]) if row["disposition"] else None
        counts[api_status(Phase(row["phase"]), disposition)] += row["count"]
    return counts


def count_awaiting_sweep(connection: sqlite3.Connection, task_code: str) -> int:
    """How many of this task's runs the next sweep has to finish."""
    return connection.execute(
        "SELECT COUNT(*) AS count FROM runs WHERE task_code = ? AND phase = ?",
        (task_code, Phase.CLOSED),
    ).fetchone()["count"]


def count_stuck(
    connection: sqlite3.Connection, task_code: str, closed_before: datetime
) -> int:
    """Finalized runs that have been waiting for the sweep for too long.

    Only finalized ones, which is what the health check has always counted: a task that
    finalized a run is waiting on Pig. An expired run waiting just as long is the same
    problem and isn't counted here.
    """
    return connection.execute(
        "SELECT COUNT(*) AS count FROM runs WHERE task_code = ? AND phase = ? "
        "AND disposition = ? AND closed_at < ?",
        (task_code, Phase.CLOSED, Disposition.FINALIZED, db.stamp(closed_before)),
    ).fetchone()["count"]


# --------------------------------------------------------------------- event receipts
# What Pig has durably written for a run. No event data lives here; see `db.SCHEMA`.


def receipt_hash(
    connection: sqlite3.Connection, run_id: str, event_id: str
) -> str | None:
    """The content hash Pig recorded for this event, or None if it has no receipt."""
    row = connection.execute(
        "SELECT content_hash FROM event_receipts WHERE run_id = ? AND event_id = ?",
        (run_id, event_id),
    ).fetchone()
    return row["content_hash"] if row else None


def record_receipt(
    connection: sqlite3.Connection,
    run_id: str,
    event_id: str,
    content_hash: str,
    when: datetime,
) -> bool:
    """Write down that this event is stored.

    False means another request recorded the same event ID between our check and this
    write, which is the caller's cue to look at what it recorded.
    """
    try:
        connection.execute(
            "INSERT INTO event_receipts (run_id, event_id, content_hash, stored_at) "
            "VALUES (?, ?, ?, ?)",
            (run_id, event_id, content_hash, db.stamp(when)),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def stored_event_ids(connection: sqlite3.Connection, run_id: str) -> list[str]:
    """Every event ID Pig has stored for this run, in the order they arrived."""
    rows = connection.execute(
        "SELECT event_id FROM event_receipts WHERE run_id = ? ORDER BY rowid", (run_id,)
    ).fetchall()
    return [row["event_id"] for row in rows]


def count_stored_events(connection: sqlite3.Connection, run_id: str) -> int:
    return connection.execute(
        "SELECT COUNT(*) AS count FROM event_receipts WHERE run_id = ?", (run_id,)
    ).fetchone()["count"]


def last_stored_at(connection: sqlite3.Connection, task_code: str) -> datetime | None:
    """When Pig last stored an event for this task, across all its runs."""
    last = connection.execute(
        "SELECT MAX(event_receipts.stored_at) AS last FROM event_receipts "
        "JOIN runs ON runs.run_id = event_receipts.run_id WHERE runs.task_code = ?",
        (task_code,),
    ).fetchone()["last"]
    return db.parse_time(last) if last else None
