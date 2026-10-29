"""The lock that keeps two of the same job from working at once.

`pig sweep`, `pigsty admit`, and `pigsty organize` each hold one while they work, so a
second one started by an overlapping timer, or by hand, stops instead of doing the same
work alongside the first. They all take it the same way, here:

    try:
        with hold_lock(path):
            ...the work...
    except LockHeld:
        ...say so, and do nothing...

It's a POSIX lock (`lockf`), rather than `flock`, because that's the kind an SMB or NFS
mount passes on to the server, where a job on another machine would see it. The
operating system lets go of it when the process ends, however it ends, so a crashed job
leaves nothing to clean up. The file itself stays, empty; it's never deleted, since
deleting it while another process is waiting to open it would let two jobs each lock a
different file of the same name.

A POSIX lock belongs to the process, not to the open file, which has two consequences
the operating system won't warn about. A second lock on the same file from the same
process succeeds rather than failing. And closing any open copy of the file in that
process lets go of the lock, even a copy that was only opened to take it again. So this
keeps track of what this process holds, and refuses a second hold before it gets as far
as opening the file.
"""

from __future__ import annotations

import errno
import fcntl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class LockHeld(Exception):
    """Something else holds the lock: another process, or this one, already."""


# Lock files this process holds, by resolved path, and a lock to guard the set itself.
_held: set[Path] = set()
_held_guard = threading.Lock()


@contextmanager
def hold_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on `path` for the length of the `with` block.

    Makes the file if it isn't there, and never changes what's in it. Raises
    `LockHeld`, before the block runs, if anything else holds the lock. Doesn't wait.
    """
    key = path.resolve()
    with _held_guard:
        if key in _held:
            raise LockHeld(f"this process already holds the lock on {path}")
        _held.add(key)
    try:
        with open(path, "a") as lock:
            try:
                fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                # These two mean someone else has it. Anything else, like a share
                # that doesn't do locks at all, is a real problem, and shouldn't be
                # reported as a job that's merely busy.
                if e.errno in (errno.EACCES, errno.EAGAIN):
                    raise LockHeld(f"another process holds the lock on {path}") from e
                raise
            yield
            # Closing the file lets go of the lock.
    finally:
        with _held_guard:
            _held.discard(key)
