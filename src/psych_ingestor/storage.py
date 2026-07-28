"""Writing datasets to disk.

A dataset is a JSONL file: one JSON object per event. While a run is in progress it lives
under `in_progress/` named for the run ID; when the run is filed it's sorted and moved to
the path the task's configuration asks for.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def canonical(event_line: dict[str, Any]) -> str:
    """The exact text of one line in a dataset.

    Everything that compares two events compares this, so the hash covers the whole
    stored event and not just its `data`.
    """
    return json.dumps(
        event_line, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def content_hash(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def in_progress_path(in_progress_root: Path, run_id: str) -> Path:
    return in_progress_root / f"{run_id}.jsonl"


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


def read_lines(path: Path) -> list[dict[str, Any]]:
    """Read a dataset. A truncated last line is dropped, which is the one thing an
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


def sort_key(event_line: dict[str, Any]) -> tuple[str, str]:
    """Sort on timestamp, then event ID. Events without a timestamp sort first, which
    keeps them next to each other rather than scattered."""
    return (str(event_line.get("timestamp") or ""), str(event_line.get("event_id", "")))


def file_dataset(source: Path, destination: Path) -> int:
    """Sort a finished dataset, drop lines that repeat exactly, and move it into place.

    Returns the number of events in the filed dataset. Lines that share an event ID but
    differ in content are both kept: that's the crash-window case, and picking a winner
    would be the data loss this whole mechanism exists to prevent.
    """
    events = read_lines(source)
    seen: set[str] = set()
    kept: list[str] = []
    for event_line in sorted(events, key=sort_key):
        line = canonical(event_line)
        if line in seen:
            continue
        seen.add(line)
        kept.append(line)

    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch = destination.with_suffix(destination.suffix + ".partial")
    with open(scratch, "w", encoding="utf-8") as handle:
        for line in kept:
            handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    scratch.replace(destination)

    if source.exists():
        source.unlink()
    return len(kept)


def duplicate_event_ids(path: Path) -> list[str]:
    """Event IDs appearing more than once in a filed dataset. Rare, and not data loss,
    but an analyst who assumes IDs are unique needs to know before they start counting."""
    counts: dict[str, int] = {}
    for event_line in read_lines(path):
        event_id = str(event_line.get("event_id", ""))
        counts[event_id] = counts.get(event_id, 0) + 1
    return sorted(event_id for event_id, count in counts.items() if count > 1)
