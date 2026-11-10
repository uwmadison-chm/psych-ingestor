"""Media items: what the database knows about one, and everything that reads or changes it.

A media item is an event with bytes attached. The event is an ordinary line in
`events.jsonl` with an ordinary receipt; what's here is the rest — which media ID the
event was given, which parts have arrived, and whether the task said it was done sending
them. Same rules as `runs.py`: a `MediaItem` is a plain value read from a row, and every
function takes the connection it should use.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import db

# Media IDs and part numbers are zero-padded in file names so that sorting the names
# sorts the numbers. These are as far as the padding goes.
MAX_MEDIA_ID = 99_999
MAX_PART = 999_999


@dataclass(frozen=True, slots=True)
class MediaItem:
    """One media item as the database knows it."""

    run_id: str
    media_id: int
    event_id: str
    declared_parts: int | None = None
    finished_at: datetime | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> MediaItem:
        return cls(
            run_id=row["run_id"],
            media_id=row["media_id"],
            event_id=row["event_id"],
            declared_parts=row["declared_parts"],
            finished_at=(
                db.parse_time(row["finished_at"]) if row["finished_at"] else None
            ),
        )

    @property
    def finished(self) -> bool:
        """Whether the task has said it sent every part, and Pig agreed."""
        return self.finished_at is not None


# ------------------------------------------------------------------------ reading items


def get(connection: sqlite3.Connection, run_id: str, media_id: int) -> MediaItem | None:
    row = connection.execute(
        "SELECT * FROM media WHERE run_id = ? AND media_id = ?", (run_id, media_id)
    ).fetchone()
    return MediaItem.from_row(row) if row else None


def by_event_id(
    connection: sqlite3.Connection, run_id: str, event_id: str
) -> MediaItem | None:
    """The media item this event started, if it started one."""
    row = connection.execute(
        "SELECT * FROM media WHERE run_id = ? AND event_id = ?", (run_id, event_id)
    ).fetchone()
    return MediaItem.from_row(row) if row else None


def for_run(connection: sqlite3.Connection, run_id: str) -> list[MediaItem]:
    """Every media item in a run, in media ID order."""
    rows = connection.execute(
        "SELECT * FROM media WHERE run_id = ? ORDER BY media_id", (run_id,)
    ).fetchall()
    return [MediaItem.from_row(row) for row in rows]


def summary(connection: sqlite3.Connection, item: MediaItem) -> dict[str, Any]:
    """What a task is told about one media item, in every media response."""
    described: dict[str, Any] = {
        "media_id": item.media_id,
        "event_id": item.event_id,
        "stored": stored_parts(connection, item.run_id, item.media_id),
        "finished": item.finished,
    }
    if item.finished:
        described["parts"] = item.declared_parts
    return described


def describe_for_manifest(
    connection: sqlite3.Connection, run_id: str
) -> list[dict[str, Any]]:
    """The manifest's `media` block: which event each item is, and whether it was
    finished. The parts themselves are in the manifest's `files`, with their hashes, so
    they aren't repeated here."""
    described = []
    for item in for_run(connection, run_id):
        entry: dict[str, Any] = {
            "media_id": item.media_id,
            "event_id": item.event_id,
            "finished": item.finished,
        }
        if item.finished:
            entry["parts"] = item.declared_parts
        described.append(entry)
    return described


# ------------------------------------------------------------------------ writing items
# The callers hold a write transaction (see `db.write_transaction`) across the check and
# the write, so none of these has to handle a race of its own.


def next_media_id(connection: sqlite3.Connection, run_id: str) -> int:
    """The next media ID for this run: one more than the highest so far, from 1."""
    row = connection.execute(
        "SELECT MAX(media_id) AS highest FROM media WHERE run_id = ?", (run_id,)
    ).fetchone()
    return (row["highest"] or 0) + 1


def insert(
    connection: sqlite3.Connection, run_id: str, media_id: int, event_id: str
) -> None:
    connection.execute(
        "INSERT INTO media (run_id, media_id, event_id) VALUES (?, ?, ?)",
        (run_id, media_id, event_id),
    )


def mark_finished(
    connection: sqlite3.Connection,
    item: MediaItem,
    declared_parts: int,
    when: datetime,
) -> bool:
    """Record that the task said it sent this many parts and Pig holds them all.

    Returns whether this call was the one that finished it.
    """
    cursor = connection.execute(
        "UPDATE media SET declared_parts = ?, finished_at = ? "
        "WHERE run_id = ? AND media_id = ? AND finished_at IS NULL",
        (declared_parts, db.stamp(when), item.run_id, item.media_id),
    )
    return cursor.rowcount == 1


# ------------------------------------------------------------------------------- parts


def part_hash(
    connection: sqlite3.Connection, run_id: str, media_id: int, part: int
) -> str | None:
    """The hash Pig recorded for this part, or None if it has no receipt."""
    row = connection.execute(
        "SELECT sha256 FROM media_parts WHERE run_id = ? AND media_id = ? AND part = ?",
        (run_id, media_id, part),
    ).fetchone()
    return row["sha256"] if row else None


def record_part(
    connection: sqlite3.Connection,
    run_id: str,
    media_id: int,
    part: int,
    size: int,
    sha256: str,
    when: datetime,
) -> None:
    """Write down that this part is on disk."""
    connection.execute(
        "INSERT INTO media_parts (run_id, media_id, part, bytes, sha256, stored_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, media_id, part, size, sha256, db.stamp(when)),
    )


def stored_parts(
    connection: sqlite3.Connection, run_id: str, media_id: int
) -> list[int]:
    """Every part number Pig holds for this item, lowest first."""
    rows = connection.execute(
        "SELECT part FROM media_parts WHERE run_id = ? AND media_id = ? ORDER BY part",
        (run_id, media_id),
    ).fetchall()
    return [row["part"] for row in rows]


def parts_for_run(connection: sqlite3.Connection, run_id: str) -> list[tuple[int, int]]:
    """Every (media_id, part) Pig has recorded for a run. For the sweep's guard."""
    rows = connection.execute(
        "SELECT media_id, part FROM media_parts WHERE run_id = ? "
        "ORDER BY media_id, part",
        (run_id,),
    ).fetchall()
    return [(row["media_id"], row["part"]) for row in rows]


def last_stored_at(connection: sqlite3.Connection, task_code: str) -> datetime | None:
    """When Pig last stored a media part for this task, across all its runs."""
    last = connection.execute(
        "SELECT MAX(media_parts.stored_at) AS last FROM media_parts "
        "JOIN runs ON runs.run_id = media_parts.run_id WHERE runs.task_code = ?",
        (task_code,),
    ).fetchone()["last"]
    return db.parse_time(last) if last else None
