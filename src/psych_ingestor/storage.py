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
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from .runs import Run

EVENTS_FILE = "events.jsonl"
MANIFEST_FILE = "manifest.json"
MEDIA_DIRECTORY = "media"
PART_SUFFIX = ".part"
# Where secure-side commands record what they've done to a run. See `describe_files`.
STATE_DIRECTORY = ".state"
# What a file is called while it's being written. Never left behind on purpose, and
# the sweep removes any that were: see `remove_partial_files`.
PARTIAL_SUFFIX = ".partial"

# Every manifest carries these two, so a reader can tell what it's looking at and which
# shape to expect. Bump the version when the manifest's shape changes.
MANIFEST_TYPE = "pig_run_manifest"
MANIFEST_VERSION = 2


def run_directory(root: Path, task_code: str, run_id: str) -> Path:
    """Where a run lives under one of the two trees."""
    return root / task_code / run_id


def media_directory(run_dir: Path, media_id: int) -> Path:
    """Where one media item's parts live: `media/00001/` inside the run directory."""
    return run_dir / MEDIA_DIRECTORY / f"{media_id:05d}"


def part_path(run_dir: Path, media_id: int, part: int) -> Path:
    """One part: `media/00001/000003.part`. Zero-padded so a sorted listing is in part
    order, and `.part` rather than a media extension because a part isn't a playable
    file and Pig doesn't parse content types."""
    return media_directory(run_dir, media_id) / f"{part:06d}{PART_SUFFIX}"


# ------------------------------------------------------------------- stored lines
#
# A stored line has three keys, split by who wrote them:
#
#     {"event_id": "12", "data": {...}, "metadata": {"stored_at": "..."}}
#
# `event_id` and `data` are the task's, stored unchanged. `metadata` is Pig's: facts Pig
# generated about the event, never anything the client sent, and never hashed. The hash
# that tells a retry from a collision covers `event_id` and `data` only, and so does the
# size limit a task is held to — otherwise both would drift whenever Pig added a field.

# What the task sent, and therefore what the hash and the size limit cover.
TASK_KEYS = ("event_id", "data")


def canonical(line_object: dict[str, Any]) -> str:
    """The exact text of one line in `events.jsonl`."""
    return json.dumps(
        line_object, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def event_line(
    event_id: str, data: Any, stored_at: datetime, media_id: int | None = None
) -> dict[str, Any]:
    """The object one stored event becomes.

    `media_id` is set when the event is a media item: it says which directory under
    `media/` holds the event's parts. It's a fact Pig generated about the event, which is
    what `metadata` is for.
    """
    metadata: dict[str, Any] = {"stored_at": stored_at.isoformat()}
    if media_id is not None:
        metadata["media_id"] = media_id
    return {"event_id": event_id, "data": data, "metadata": metadata}


def hashed_text(line_object: dict[str, Any]) -> str:
    """The part of a line the task is answerable for: `event_id` and `data`."""
    return canonical({key: line_object[key] for key in TASK_KEYS})


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -------------------------------------------------------------------- writing


def append_lines(path: Path, lines: list[str]) -> None:
    """Append lines to an existing file, and don't return until they're really on disk.

    "If the server says an event is stored, it's on disk" is the whole reason for the
    fsync. It costs a few milliseconds per request, which we have. One write and one
    fsync for a whole batch, so the caller holds the database's write lock for one disk
    flush, not one per event.

    The file's directory has to be there already. A run's directory is made when the run
    starts and moved into `done/` when it's finished, so a missing one means the run has
    moved on, and making a fresh one here would put the lines where nothing will look.
    """
    with open(path, "ab") as handle:
        handle.write("".join(line + "\n" for line in lines).encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


class PartTooLarge(Exception):
    """More bytes arrived for a part than the task allows."""


class PartWriter:
    """Write one media part to disk as it arrives, without holding it in memory.

    Bytes go straight from each chunk to a scratch file, hashed and counted on the way through, and the size limit is enforced as they
    arrive rather than trusted from a header. When the whole body is in, `finish`
    fsyncs it and hands back the size and hash; whoever called us then decides whether
    to `keep` it (rename into place) or `discard` it (a retry Pig already has, or a
    part that differs from one it already has). Nothing is in the part's real name
    until `keep`, so a crash mid-write leaves a scratch file and never a part that
    looks whole and isn't.

    The scratch file is outside the run directory, under `uploading/`, named for the run
    (see `scratch_path`). A run can close and be finished by the sweep while a part is
    still arriving, and a scratch file inside the run's directory could then be hashed
    into its manifest, or carried into `done/`, half written. See #52.
    """

    def __init__(self, final: Path, scratch: Path, limit: int):
        self.final = final
        self.limit = limit
        self.scratch = scratch
        scratch.parent.mkdir(parents=True, exist_ok=True)
        self.size = 0
        self._hash = hashlib.sha256()
        # Held open across several calls, so a context manager can't scope it.
        self._handle = open(self.scratch, "wb")  # noqa: SIM115
        self._open = True

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > self.limit:
            raise PartTooLarge(
                f"This part is bigger than this task allows ({self.limit} bytes)."
            )
        self._hash.update(chunk)
        self._handle.write(chunk)

    def finish(self) -> str:
        """The body is all here. Make it durable and return its hash."""
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._close()
        return self._hash.hexdigest()

    def keep(self) -> None:
        """Put the finished scratch file in the part's real place."""
        self._close()
        self.scratch.replace(self.final)
        fsync_directory(self.final.parent)

    def discard(self) -> None:
        """Throw the scratch file away. Safe to call at any point, more than once."""
        self._close()
        try:
            self.scratch.unlink()
        except FileNotFoundError:
            pass

    def _close(self) -> None:
        if self._open:
            self._handle.close()
            self._open = False


def scratch_path(uploading_root: Path, run_id: str, media_id: int, part: int) -> Path:
    """Where a part is written while it arrives, like
    `uploading/{run_id}.00001.000003.{random}.partial`.

    Named for the run, so the sweep can find a finished run's scratch files and delete
    them. The random part keeps two uploads of the same part at once from writing over
    each other.
    """
    return uploading_root / (
        f"{run_id}.{media_id:05d}.{part:06d}.{secrets.token_hex(4)}{PARTIAL_SUFFIX}"
    )


def remove_partial_files(directory: Path) -> None:
    """Delete scratch files an interrupted write left in a run directory.

    Pig never said any of these were stored, and the manifest is about to hash every
    file in the directory, so leaving one would make a copy elsewhere responsible for
    bytes nobody acknowledged. (Parts are written under `uploading/` now, but a run
    started by an older Pig may still have one here.)
    """
    for path in directory.rglob(f"*{PARTIAL_SUFFIX}"):
        path.unlink()


def remove_upload_scratch(uploading_root: Path, run_id: str) -> None:
    """Delete a run's scratch parts from `uploading/`, once the run is closed.

    An upload still going for this run will find its scratch file gone and its run
    closed, and refuse the part, which is what it would have done anyway.
    """
    for path in uploading_root.glob(f"{run_id}.*{PARTIAL_SUFFIX}"):
        path.unlink(missing_ok=True)


def create_empty_file(path: Path) -> None:
    """An empty file, durably. For a run that never sent an event."""
    with open(path, "x", encoding="utf-8") as handle:
        os.fsync(handle.fileno())
    fsync_directory(path.parent)


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
    repeated, and so does `pigsty organize`, which copies the file byte for byte. Exact
    repeats are harmless and whatever reads the data drops them; two lines sharing an
    ID but differing in content are the case an analyst needs to know about before they
    start counting. This reports both, because telling them apart is the
    analyst's call.
    """
    counts: dict[str, int] = {}
    for line_object in read_lines(path):
        event_id = str(line_object.get("event_id", ""))
        counts[event_id] = counts.get(event_id, 0) + 1
    return sorted(event_id for event_id, count in counts.items() if count > 1)


# ------------------------------------------------------------------ the manifest


def manifest_for(
    run: Run,
    directory: Path,
    done_at: datetime,
    media: list[dict[str, Any]],
) -> dict[str, Any]:
    """Everything needed to understand a finished run without asking Pig, and nothing
    else.

    `media` is one entry per media item — which event it is and whether the task
    finished it — from `media.describe_for_manifest`. The parts themselves are in
    `files` like everything else, so their hashes live in one place.

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
        "media": media,
        "files": describe_files(directory),
    }


def describe_files(directory: Path) -> list[dict[str, Any]]:
    """Every file in a run directory but the manifest, with its size and hash.

    This is what makes the directory verifiable standing alone: a copy elsewhere can be
    confirmed by hash, with no reference back to this machine.

    `.state/` is left out too. It's where the secure side records what it's done to a
    run, the one part of a run directory that changes after the manifest is written,
    so the manifest must never vouch for it. The web side never writes one.
    """
    described = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        relative = path.relative_to(directory).as_posix()
        if relative == MANIFEST_FILE or relative.startswith(STATE_DIRECTORY + "/"):
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
    fsync_directory(directory)


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
    fsync_directory(destination.parent)
    fsync_directory(source.parent)


def fsync_directory(directory: Path) -> None:
    """Make a rename or a new file durable: the directory entry needs its own fsync.

    Opened read-only because that's the only way to open a directory, and fsync doesn't
    care: it flushes the object the descriptor refers to, whatever mode it was opened
    with. This is the standard idiom on Linux and macOS.
    """
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
