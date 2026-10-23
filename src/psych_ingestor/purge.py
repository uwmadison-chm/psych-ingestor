"""Purge hashes: how the secure side shows the web server it has a run's bytes.

Once a run is safely on the secure side, its copy on the web server should go, so that
human-subjects data isn't left sitting on the public-facing machine. `pigsty purge` asks
for that by sending a hash of every file in its copy of the run, and the web server deletes
its copy only if every hash matches its own. See issue #2.

The hashes aren't the plain sha256 the manifest lists. Each is the sha256 of `pig-purge`
and a newline, followed by the file's bytes. The manifest travels with the data and can be
copied anywhere, and if purge took the manifest's hashes, anyone holding a manifest could
purge a run without ever having seen its data. The prefix makes a hash that can only be
computed from the bytes themselves, so producing one means having the file.

Neither side stores them. Both compute them when a purge happens, from the files they
have: the web server from `done/`, the secure side from its copy of the bucket.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from . import db, runs, storage

PURGE_HASH_PREFIX = b"pig-purge\n"


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
    if run_directory.is_symlink():
        raise PurgeProblem(f"{run_directory} is a symbolic link, not a directory.")
    hashes = {}
    waiting = [run_directory]
    while waiting:
        directory = waiting.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                relative = path.relative_to(run_directory).as_posix()
                if entry.is_symlink():
                    raise PurgeProblem(f"{relative} is a symbolic link.")
                if entry.is_dir(follow_symlinks=False):
                    waiting.append(path)
                else:
                    hashes[relative] = purge_hash(path)
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


# ------------------------------------------------------------- the web server's side


def finish(connection: sqlite3.Connection, run_id: str, directory: Path) -> None:
    """Finish purging a run that's already been moved out of `done/` into `directory`,
    under `purging/`.

    Its identifiers go first, then its files. Either order leaves the run out of `done/`
    from the moment it's moved, which is what matters to the copy to the bucket. This
    order means a run whose files are still in `purging/` can always be finished by the
    next purge request or the next sweep, whichever comes first, and both are safe to
    run at once.
    """
    runs.forget_identifiers(connection, run_id)
    db.checkpoint(connection)
    shutil.rmtree(directory, onexc=_ignore_missing)
    storage.fsync_directory(directory.parent)


def _ignore_missing(function: Any, path: str, error: BaseException) -> None:
    # Another purge, or a sweep, deleting the same directory at the same time.
    if not isinstance(error, FileNotFoundError):
        raise error
