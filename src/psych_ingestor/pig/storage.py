"""What only the web server writes: events as they arrive, media parts, and the manifest.

The formats themselves, the directory layout, the event line, and the manifest's shape,
are in `formats.py`, because the secure side reads them too. The durable-write primitives
are in `files.py`. Nothing here reads `pig.toml` or the database.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import files, formats
from .runs import Run

# What a file is called while it's being written. Never left behind on purpose, and
# the sweep removes any that were: see `remove_partial_files`.
PARTIAL_SUFFIX = ".partial"
# How much of a media part PartWriter keeps: enough to tell an encrypted one.
START_LENGTH = 64


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

    An append a crash interrupted can leave the file ending partway through a line. The
    next line would be joined onto it, and a JSON reader would lose both, including one
    Pig said was stored. So if the file doesn't end with a newline, this writes one
    first: the cut-short line stays, on its own, and is the only line lost. See #53.
    """
    # Opened for reading as well, to look at the last byte. Writes still go to the end.
    with open(path, "a+b") as handle:
        size = os.fstat(handle.fileno()).st_size
        cut_short = size > 0 and os.pread(handle.fileno(), 1, size - 1) != b"\n"
        text = ("\n" if cut_short else "") + "".join(line + "\n" for line in lines)
        handle.write(text.encode("utf-8"))
        files.flush_and_fsync(handle)
    if size == 0:
        # The first lines, so most likely a new file. Its name needs fsyncing too, or a
        # crash can take the file, and the events Pig just said were stored, with it.
        files.fsync_directory(path.parent)


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
        # The part's first few bytes, so an encrypted run can check they start an age
        # file without reading the part back.
        self.start = b""
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
        if len(self.start) < START_LENGTH:
            self.start += chunk[: START_LENGTH - len(self.start)]
        self._hash.update(chunk)
        self._handle.write(chunk)

    def finish(self) -> str:
        """The body is all here. Make it durable and return its hash."""
        files.flush_and_fsync(self._handle)
        self._close()
        return self._hash.hexdigest()

    def keep(self) -> None:
        """Put the finished scratch file in the part's real place."""
        self._close()
        self.scratch.replace(self.final)
        files.fsync_directory(self.final.parent)

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
        files.flush_and_fsync(handle)
    files.fsync_directory(path.parent)


def touch(path: Path) -> None:
    """Open a file for writing and close it, then set its modification time to now.

    Both, so that whatever is watching sees it: a systemd path unit or inotify sees the
    close, and something that compares modification times sees the time. Nothing is
    written, and nothing is fsynced, because nothing here is data.
    """
    with open(path, "a", encoding="utf-8"):
        pass
    os.utime(path)


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
        "type": formats.MANIFEST_TYPE,
        "manifest_version": formats.MANIFEST_VERSION,
        "run_id": run.run_id,
        "task_code": run.task_code,
        "run_number": run.run_number,
        # The ordered parameter names, because "this is run 2" means nothing offline
        # unless you know 2 of what.
        "run_key_parameters": run.run_key_names,
        # Which of `parameters` were held to the safe-value rule, so organize can tell a
        # value it may put in a path from one it mustn't, and which ones were signed.
        "path_parameters": run.path_parameter_names,
        "signed_parameters": run.signed_parameter_names,
        "disposition": run.disposition.value,
        "started_at": run.started_at.isoformat(),
        "closed_at": run.closed_at.isoformat(),
        "done_at": done_at.isoformat(),
        "parameters": run.parameters,
        "extra_parameters": run.extra_parameters,
        "media": media,
        # The public key's name, if the browser encrypted the run's events and media
        # parts. The files below are then the ciphertext, and pigsty admit decrypts
        # them. Null for a run that isn't encrypted.
        "encrypted_to": run.encrypted_to,
        # The name of the key that signed `signed_parameters`, which Pig checked when
        # the run started. Null for a run whose task didn't check signatures.
        "signed_by": run.signed_by,
        "files": formats.describe_files(directory),
    }


def write_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    """Write the manifest so that it's either completely there or not there at all."""
    final = directory / formats.MANIFEST_FILE
    scratch = directory / (formats.MANIFEST_FILE + ".partial")
    with open(scratch, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        files.flush_and_fsync(handle)
    scratch.replace(final)
    files.fsync_directory(directory)
