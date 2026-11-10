"""What Pig writes to disk, and how to read it back: the formats both sides share.

Every run is a directory named for its run ID, under `in_progress/{task_code}/` while Pig
is still working on it and under `done/{task_code}/` once the sweep has finished with it.
Inside are the run's events, one JSON object per line in `events.jsonl`, and — once the run
is done — a manifest describing the run. On the secure side, `pigsty admit` copies each
run into `runs/{run_id}/` with a `pig-run.json` saying what it is.

Nothing here reads a configuration file or the database: a run directory has to make
sense on its own, on a machine that has neither. Writing these files durably is
`files.py`'s job, and what only the web server writes is in `pig/storage.py`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .files import SymbolicLink, files_in

EVENTS_FILE = "events.jsonl"
MANIFEST_FILE = "manifest.json"
MEDIA_DIRECTORY = "media"
PART_SUFFIX = ".part"
# Where secure-side commands record what they've done to a run. See `describe_files`.
STATE_DIRECTORY = ".state"

# Every manifest carries these two, so a reader can tell what it's looking at and which
# shape to expect. Bump the version when the manifest's shape changes.
MANIFEST_TYPE = "pig_run_manifest"
MANIFEST_VERSION = 4


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


# ------------------------------------------------------------------- reading


def read_lines(path: Path) -> list[dict[str, Any]]:
    """Read an events file. A line that isn't JSON is dropped: it's a line an interrupted
    append cut short, which is the one thing a crash can leave behind. `append_lines`
    makes sure the next line starts after it rather than on the end of it."""
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
#
# Written once, by `pig sweep` when it finishes a run (see `pig/storage.py`), and
# never changed. Everything after that, on either side, reads it.


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


def read_manifest(directory: Path) -> dict[str, Any]:
    return json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8"))


# ------------------------------------------------------------------- pig-run.json

# Where `pigsty admit` puts the runs it admits, in a task's datastore.
RUNS_DIRECTORY = "runs"

# What a run is, without its file hashes. Written by admit into `runs/{run_id}/`, and
# copied by organize into the readable tree.
RUN_FILE = "pig-run.json"
RUN_FILE_TYPE = "pig_run"
RUN_FILE_VERSION = 2

# The manifest's fields that describe the run rather than its files, and so the fields
# of `pig-run.json`.
RUN_FIELDS = (
    "run_id",
    "task_code",
    "run_number",
    "run_key_parameters",
    "path_parameters",
    "signed_parameters",
    "disposition",
    "started_at",
    "closed_at",
    "done_at",
    "parameters",
    "extra_parameters",
    "media",
    "signed_by",
)


# ------------------------------------------------------------------ purge hashes
#
# How the secure side shows the web server it has a run's bytes. Once a run is safely on
# the secure side, its copy on the web server should go, so that human-subjects data
# isn't left sitting on the public-facing machine. `pigsty purge` asks for that by
# sending a hash of every file in its copy of the run, and the web server deletes its
# copy only if every hash matches its own. See issue #2.
#
# The hashes aren't the plain sha256 the manifest lists. Each is the sha256 of
# `pig-purge:` followed by the file's bytes. The manifest travels with the data and can
# be copied anywhere, and if purge took the manifest's hashes, anyone holding a manifest
# could purge a run without ever having seen its data. The prefix makes a hash that can
# only be computed from the bytes themselves, so producing one means having the file.
#
# Neither side stores them. Both compute them when a purge happens, from the files they
# have: the web server from `done/`, the secure side from its copy of the bucket.

# Plain text with nothing to escape, so a purge hash can be checked by hand:
#     { printf 'pig-purge:'; cat events.jsonl; } | sha256sum
PURGE_HASH_PREFIX = b"pig-purge:"


class PurgeProblem(Exception):
    """A run directory that can't be hashed for purging, and why."""


def purge_hash(path: Path) -> str:
    """One file's purge hash: the sha256 of the prefix, then the file's bytes."""
    digest = hashlib.sha256(PURGE_HASH_PREFIX)
    with open(path, "rb") as reading:
        while chunk := reading.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def purge_hashes(run_directory: Path) -> dict[str, str]:
    """The purge hash of every file in a run directory, `manifest.json` included, by
    path relative to the directory.

    Raises PurgeProblem if anything in it, the directory included, is a symbolic link.
    Pig never makes one, and on the secure side one could point anywhere on the machine.
    """
    try:
        files = files_in(run_directory)
    except SymbolicLink as link:
        raise PurgeProblem(f"{link}.") from None
    hashes = {
        relative.as_posix(): purge_hash(run_directory / relative) for relative in files
    }
    return dict(sorted(hashes.items()))


def differences(theirs: dict[str, str], ours: dict[str, str]) -> list[str]:
    """How two sets of purge hashes differ, as sentences, or nothing if they match.

    `theirs` is what the secure side sent and `ours` is what the web server computed, so
    the sentences are written from the web server's side.
    """
    missing = sorted(ours.keys() - theirs.keys())
    extra = sorted(theirs.keys() - ours.keys())
    different = sorted(
        path for path in ours.keys() & theirs.keys() if ours[path] != theirs[path]
    )
    found = []
    if missing:
        found.append(f"Your copy doesn't have {_some(missing)}.")
    if extra:
        found.append(f"Pig's copy doesn't have {_some(extra)}.")
    if different:
        found.append(f"These don't match Pig's copy: {_some(different)}.")
    return found


def _some(paths: list[str]) -> str:
    """'a' or 'a, b, c' or 'a, b, c, d, e and 4 more', for a message."""
    shown = ", ".join(paths[:5])
    if len(paths) > 5:
        shown += f" and {len(paths) - 5} more"
    return shown
