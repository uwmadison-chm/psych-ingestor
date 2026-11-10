"""`pigsty purge`: ask the web server to delete its copy of each run this side has.

Once a run is safely on the secure side, its copy on the web server should go, so that
human-subjects data isn't left sitting on the public-facing machine. For each run this
side has admitted, this sends the web server the purge hash of every file in its copy
(see `formats.py`), and the web server deletes its own copy only if every hash matches.
See issue #2.

It hashes the copy in `incoming/`, never `runs/`: `incoming/` is a mirror of the bucket,
so its bytes are the web server's bytes, and the files in `runs/` have been decrypted.
A run that's in `incoming/` but not `runs/` isn't admitted, so it isn't purged. One
that's in `runs/` but not `incoming/` has already left the bucket, so there's nothing to
ask about.

A run that's been purged gets `runs/{run_id}/.state/purged`, so the next pass doesn't
hash it again while the syncs catch up and drop it from `incoming/`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import files, formats
from ..common import os_error_reason

# Where the secure side records what it's done to a run. Organize never copies it.
STATE_DIRECTORY = formats.STATE_DIRECTORY
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


def purge_runs(
    task_code: str, incoming: Path, datastore: Path, send: Sender
) -> PurgeReport:
    """Ask Pig to purge every run in `datastore/runs/` that's still in `incoming_dir`, this
    task's directory in `incoming_dir`."""
    report = PurgeReport()
    admitted = datastore / formats.RUNS_DIRECTORY
    if not incoming.is_dir() or not admitted.is_dir():
        return report

    for run_id in sorted(
        set(files.directories_in(incoming)) & set(files.directories_in(admitted))
    ):
        if (admitted / run_id).is_symlink():
            report.problems[str(admitted / run_id)] = (
                "it's a symbolic link, not a directory. pigsty admit never makes one, "
                "so this run wasn't purged."
            )
            continue
        state = admitted / run_id / STATE_DIRECTORY
        if (state / PURGED_FILE).exists():
            report.already_purged += 1
            continue
        not_admitted = _not_admitted(task_code, admitted / run_id)
        if not_admitted:
            report.problems[str(admitted / run_id)] = (
                f"{not_admitted} pigsty admit writes it into every run it admits, so "
                "this run wasn't purged."
            )
            continue
        try:
            hashes = formats.purge_hashes(incoming / run_id)
        except formats.PurgeProblem as problem:
            report.problems[str(incoming / run_id)] = f"couldn't hash it: {problem}"
            continue
        except OSError as error:
            report.problems[str(incoming / run_id)] = (
                f"couldn't hash it: {os_error_reason(error)}."
            )
            continue
        path = f"/v1/task/{task_code}/run/{urllib.parse.quote(run_id, safe='')}/purge"
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


def _not_admitted(task_code: str, directory: Path) -> str:
    """Why a directory in `runs/` doesn't look like a run admit put there, or nothing
    if it does.

    A directory with the run's name isn't enough. One copied in by hand, or left by a
    partial restore, would have it, and purging on its word would leave the secure side
    with whatever that directory holds. Admit writes `pig-run.json` before renaming the
    run into `runs/`, so every run it admitted has one, naming the run and its task.
    """
    try:
        described = json.loads(
            (directory / formats.RUN_FILE).read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return f"There's no {formats.RUN_FILE} in it."
    except (OSError, ValueError) as error:
        reason = os_error_reason(error) if isinstance(error, OSError) else error
        return f"Its {formats.RUN_FILE} can't be read ({reason})."
    if (
        not isinstance(described, dict)
        or described.get("type") != formats.RUN_FILE_TYPE
        or described.get("run_id") != directory.name
        or described.get("task_code") != task_code
    ):
        return (
            f"Its {formats.RUN_FILE} doesn't describe run {directory.name} of task "
            f"{task_code}."
        )
    return ""


def _record_purged(state: Path) -> None:
    """Write `.state/purged`, saying when, and make sure it's on disk."""
    state.mkdir(exist_ok=True)
    with open(state / PURGED_FILE, "w", encoding="utf-8") as written:
        json.dump({"purged_at": datetime.now(UTC).isoformat()}, written)
        written.write("\n")
        files.flush_and_fsync(written)
    files.fsync_directory(state)
    files.fsync_directory(state.parent)


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
            if isinstance(reason, OSError):
                reason = os_error_reason(reason)
            raise Unreachable(f"couldn't reach Pig at {pig_url}: {reason}.") from error

    return send


def _json_or_empty(raw: bytes) -> dict[str, Any]:
    # A reply from the web server in front of Pig, a 502 say, isn't JSON.
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
