"""What Pig writes to disk, and where.

Every run is a directory named for its run ID, under `in_progress/{task_code}/` while Pig
is still working on it and under `done/{task_code}/` once the sweep has finished with it.
Inside are the run's events, one JSON object per line in `events.jsonl`, and — once the run
is done — a manifest describing the run. Nothing here reads `pig.toml` or the database:
a run directory has to make sense on its own, on a machine that has neither.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .runs import Run

EVENTS_FILE = "events.jsonl"
MANIFEST_FILE = "manifest.json"

# Every manifest carries these two, so a reader can tell what it's looking at and which
# shape to expect. Bump the version when the manifest's shape changes.
MANIFEST_TYPE = "pig_run_manifest"
MANIFEST_VERSION = 1


def run_directory(root: Path, task_code: str, run_id: str) -> Path:
    """Where a run lives under one of the two trees."""
    return root / task_code / run_id


# ------------------------------------------------------------------- stored lines
#
# A stored line has three keys, split by who wrote them:
#
#     {"event_id": "12", "data": {...}, "metadata": {"stored_at": "..."}}
#
# `event_id` and `data` are the task's, stored unchanged. `metadata` is Pig's: facts Pig
# generated about the event, never anything the client sent, and never hashed. The hash
# that tells a retry from a collision covers the line minus `metadata`, and so does the
# size limit a task is held to — otherwise both would drift whenever Pig added a field.


def canonical(line_object: dict[str, Any]) -> str:
    """The exact text of one line in `events.jsonl`."""
    return json.dumps(
        line_object, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def event_line(event_id: str, data: Any, stored_at: datetime) -> dict[str, Any]:
    """The object one stored event becomes."""
    return {
        "event_id": event_id,
        "data": data,
        "metadata": {"stored_at": stored_at.isoformat()},
    }


def hashed_text(line_object: dict[str, Any]) -> str:
    """The part of a line the task is answerable for: everything but `metadata`.

    Structural rather than a field list, so it stays right as `metadata` grows.
    """
    return canonical({k: v for k, v in line_object.items() if k != "metadata"})


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -------------------------------------------------------------------- writing


def append_line(path: Path, line: str) -> None:
    """Append one line and don't return until it's really on disk.

    "If the server says an event is stored, it's on disk" is the whole reason for the
    fsync. It costs a few milliseconds per request, which we have.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def create_empty_file(path: Path) -> None:
    """An empty file, durably. For a run that never sent an event."""
    with open(path, "x", encoding="utf-8") as handle:
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def read_lines(path: Path) -> list[dict[str, Any]]:
    """Read an events file. A truncated last line is dropped, which is the one thing an
    interrupted append can leave behind."""
    if not path.exists():
        return []
    events = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            events.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return events


def duplicate_event_ids(path: Path) -> list[str]:
    """Event IDs appearing more than once in an events file.

    Pig never rewrites `events.jsonl`, so a line the crash window repeated stays
    repeated. Exact repeats are harmless and `pig organize` will drop them; two lines
    sharing an ID but differing in content are the case an analyst needs to know about
    before they start counting. This reports both, because telling them apart is the
    analyst's call.
    """
    counts: dict[str, int] = {}
    for line_object in read_lines(path):
        event_id = str(line_object.get("event_id", ""))
        counts[event_id] = counts.get(event_id, 0) + 1
    return sorted(event_id for event_id, count in counts.items() if count > 1)


# ------------------------------------------------------------------ the manifest


def manifest_for(run: Run, directory: Path, done_at: datetime) -> dict[str, Any]:
    """Everything needed to understand a finished run without asking Pig, and nothing
    else.

    No event count: `events.jsonl` may hold a repeated line, so a count would be an
    upper bound at best. No configuration snapshot, nothing secret.
    """
    if run.disposition is None or run.closed_at is None:
        raise ValueError(
            f"run {run.run_id} is still collecting; it has no manifest yet"
        )
    return {
        "type": MANIFEST_TYPE,
        "manifest_version": MANIFEST_VERSION,
        "run_id": run.run_id,
        "task_code": run.task_code,
        "run_number": run.run_number,
        # The ordered parameter names, because "this is run 2" means nothing offline
        # unless you know 2 of what.
        "run_key": run.run_key_names,
        "disposition": run.disposition.value,
        "started_at": run.started_at.isoformat(),
        "closed_at": run.closed_at.isoformat(),
        "done_at": done_at.isoformat(),
        "parameters": run.parameters,
        "extra_parameters": run.extra_parameters,
        "files": describe_files(directory),
    }


def describe_files(directory: Path) -> list[dict[str, Any]]:
    """Every file in a run directory but the manifest, with its size and hash.

    This is what makes the directory verifiable standing alone: a copy elsewhere can be
    confirmed by hash, with no reference back to this machine.
    """
    described = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        relative = path.relative_to(directory).as_posix()
        if relative == MANIFEST_FILE:
            continue
        described.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return described


def write_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    """Write the manifest so that it's either completely there or not there at all."""
    final = directory / MANIFEST_FILE
    scratch = directory / (MANIFEST_FILE + ".partial")
    with open(scratch, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    scratch.replace(final)
    _fsync_directory(directory)


def read_manifest(directory: Path) -> dict[str, Any]:
    return json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8"))


# ------------------------------------------------------------------- moving


def move_directory(source: Path, destination: Path) -> None:
    """Move a finished run directory into place, all at once.

    A rename within one filesystem is atomic, so the directory is never half-there.
    Refuses if something is already at the destination: a rename onto an existing empty
    directory would quietly succeed, and onto a non-empty one would fail with a message
    about the wrong thing.
    """
    if destination.exists():
        raise OSError(f"{destination} already exists; not moving {source} over it")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
    _fsync_directory(destination.parent)
    _fsync_directory(source.parent)


def _fsync_directory(directory: Path) -> None:
    """Make a rename or a new file durable: the directory entry needs its own fsync."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
