"""Purge hashes: how the secure side shows the web server it has a run's bytes.

Once a run is safely on the secure side, its copy on the web server should go, so that
human-subjects data isn't left sitting on the public-facing machine. `pigsty purge` asks
for that by sending a hash of every file in its copy of the run, and the web server deletes
its copy only if every hash matches its own. See issue #2.

The hashes aren't the plain sha256 the manifest lists. Each is the sha256 of `pig-purge:`
followed by the file's bytes. The manifest travels with the data and can be
copied anywhere, and if purge took the manifest's hashes, anyone holding a manifest could
purge a run without ever having seen its data. The prefix makes a hash that can only be
computed from the bytes themselves, so producing one means having the file.

Neither side stores them. Both compute them when a purge happens, from the files they
have: the web server from `done/`, the secure side from its copy of the bucket.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import admit, db, runs, storage

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


# ------------------------------------------------------------- the secure side's
#
# `pigsty purge` asks the web server to purge each run this side has admitted. It hashes
# the copy in `incoming/`, never `runs/`: `incoming/` is a mirror of the bucket, so its
# bytes are the web server's bytes, and once decryption (#10) happens in admit, the
# files in `runs/` won't be. A run that's in `incoming/` but not `runs/` isn't admitted,
# so it isn't purged. One that's in `runs/` but not `incoming/` has already left the
# bucket, so there's nothing to ask about.
#
# A run that's been purged gets `runs/{run_id}/.state/purged`, so the next pass doesn't
# hash it again while the syncs catch up and drop it from `incoming/`.

# Where the secure side records what it's done to a run. Organize never copies it.
STATE_DIRECTORY = storage.STATE_DIRECTORY
PURGED_FILE = "purged"

# The web server hashes its copy of the run before it answers, which for a run with a
# lot of media takes a while.
TIMEOUT_SECONDS = 600

# Sends one purge request: the path after Pig's address, and the body. Returns the
# reply's status code and its body. Given to `purge_runs` so tests can stand in for
# the web server.
Sender = Callable[[str, dict[str, Any]], tuple[int, dict[str, Any]]]


class Unreachable(Exception):
    """Pig didn't answer at all. Every other run would go the same way, so stop."""


@dataclass
class PurgeReport:
    """What one `pigsty purge` did for one task.

    `problems` are runs Pig refused to purge, or that couldn't be asked about, by their
    directory in `incoming/`. Each one means the run is still on the web server.
    """

    purged: list[str] = field(default_factory=list)
    already_purged: int = 0
    problems: dict[str, str] = field(default_factory=dict)


def purge_runs(task_code: str, datastore: Path, send: Sender) -> PurgeReport:
    """Ask Pig to purge every run in `datastore/runs/` that's still in `incoming/`."""
    report = PurgeReport()
    incoming = datastore / admit.INCOMING_DIRECTORY
    admitted = datastore / admit.RUNS_DIRECTORY
    if not incoming.is_dir() or not admitted.is_dir():
        return report

    for run_id in sorted(
        set(_directories_in(incoming)) & set(_directories_in(admitted))
    ):
        state = admitted / run_id / STATE_DIRECTORY
        if (state / PURGED_FILE).exists():
            report.already_purged += 1
            continue
        try:
            hashes = purge_hashes(incoming / run_id)
        except (PurgeProblem, OSError) as problem:
            report.problems[str(incoming / run_id)] = f"couldn't hash it: {problem}"
            continue
        path = f"/task/{task_code}/run/{urllib.parse.quote(run_id, safe='')}/purge"
        try:
            status, body = send(path, {"purge_hashes": hashes})
        except Unreachable as error:
            report.problems[str(incoming / run_id)] = (
                f"{error} Nothing more was purged for this task."
            )
            return report
        if status != 200:
            message = body.get("message") or f"Pig answered {status}."
            report.problems[str(incoming / run_id)] = f"not purged: {message}"
            continue
        _record_purged(state)
        report.purged.append(run_id)
    return report


def _directories_in(directory: Path) -> list[str]:
    """The names of the directories directly inside this one, leaving out dot-names."""
    with os.scandir(directory) as entries:
        return [
            entry.name
            for entry in entries
            if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False)
        ]


def _record_purged(state: Path) -> None:
    """Write `.state/purged`, saying when, and make sure it's on disk."""
    state.mkdir(exist_ok=True)
    with open(state / PURGED_FILE, "w", encoding="utf-8") as written:
        json.dump({"purged_at": db.now().isoformat()}, written)
        written.write("\n")
        written.flush()
        os.fsync(written.fileno())
    storage.fsync_directory(state)
    storage.fsync_directory(state.parent)


def http_sender(pig_url: str) -> Sender:
    """A Sender that makes real requests to the Pig at `pig_url`.

    The standard library's `urllib`, so the secure side needs nothing installed for it.
    """

    def send(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        request = urllib.request.Request(
            pig_url.rstrip("/") + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as reply:
                return reply.status, _json_or_empty(reply.read())
        except urllib.error.HTTPError as error:
            return error.code, _json_or_empty(error.read())
        except (urllib.error.URLError, TimeoutError) as error:
            reason = getattr(error, "reason", error)
            raise Unreachable(f"couldn't reach Pig at {pig_url}: {reason}.") from error

    return send


def _json_or_empty(raw: bytes) -> dict[str, Any]:
    # A reply from the web server in front of Pig, a 502 say, isn't JSON.
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
