"""Finishing a purge, on the web server.

`pigsty purge` sends the purge hashes of its copy of a run (see `formats.py`), and the
service checks them against its own and moves the run out of `done/` into `purging/`.
This deletes it from there: from the moment it's moved, nothing will copy it anywhere,
and the next purge request or the next sweep, whichever comes first, finishes the job if
this one is interrupted.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .. import files
from . import db, runs


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
    files.fsync_directory(directory.parent)


def _ignore_missing(function: Any, path: str, error: BaseException) -> None:
    # Another purge, or a sweep, deleting the same directory at the same time.
    if not isinstance(error, FileNotFoundError):
        raise error
