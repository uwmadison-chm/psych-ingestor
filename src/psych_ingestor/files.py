"""Putting files on disk so they stay there, and the lock that keeps two jobs apart.

Both sides use these. "If the server says an event is stored, it's on disk" depends on
them, and so does every step on the secure side that moves a run into place. A file or a
directory isn't safely on disk until it, and the directory holding it, have been fsynced;
everything here does both.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import IO, Any

# ------------------------------------------------------------------- making


def make_directories(directory: Path) -> None:
    """Make a directory, and any missing above it, so that they survive a crash.

    A new directory is only an entry in its parent until the parent is fsynced, so each
    one made here has its parent fsynced. Without that, a run directory Pig has already
    written events into could be gone after a power cut, events and all. See #54.
    """
    missing = []
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for made in reversed(missing):
        made.mkdir(exist_ok=True)
        fsync_directory(made.parent)


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
    make_directories(destination.parent)
    source.rename(destination)
    fsync_directory(destination.parent)
    if source.parent != destination.parent:
        fsync_directory(source.parent)


# ------------------------------------------------------------ building a directory
#
# How the secure side puts a run anywhere: build it in a scratch directory beside where
# it goes, fsync everything in it, and rename it into place. So a run is either all
# there or not there at all, even after a crash.


def fresh_scratch(destination: Path, suffix: str) -> Path:
    """An empty scratch directory beside `destination`, named for it, to build a run in
    before `rename_into_place` moves it there. The directories above it are made first,
    durably, by `make_directories`.

    Its name starts with a dot, so it can't be mistaken for a run, and ends with
    `suffix`, so one left by an interrupted command is recognizably ours, and is
    deleted here.
    """
    make_directories(destination.parent)
    scratch = destination.with_name(f".{destination.name}{suffix}")
    try:
        shutil.rmtree(scratch)
    except FileNotFoundError:
        pass
    scratch.mkdir()
    return scratch


def make_parent(target: Path, made: set[Path]) -> None:
    """Make a file's directory if it isn't made yet, and remember each directory between
    it and the scratch directory, for `rename_into_place` to fsync. `made` starts out
    holding the scratch directory."""
    if target.parent not in made:
        target.parent.mkdir(parents=True, exist_ok=True)
        directory = target.parent
        while directory not in made:
            made.add(directory)
            directory = directory.parent


def copy_file(source: Path, target: Path, made: set[Path]) -> str:
    """Copy one file into a scratch directory, fsync it, and return its sha256. `made`
    is as for `make_parent`, so each directory is made once rather than once per file."""
    make_parent(target, made)
    digest = hashlib.sha256()
    with open(source, "rb") as reading, open(target, "wb") as writing:
        while chunk := reading.read(1024 * 1024):
            digest.update(chunk)
            writing.write(chunk)
        flush_and_fsync(writing)
    return digest.hexdigest()


def rename_into_place(scratch: Path, destination: Path, made: set[Path]) -> None:
    """Fsync every directory made in the scratch directory, so the entries in each are
    on disk, then move it to `destination` with `move_directory`."""
    for directory in made:
        fsync_directory(directory)
    move_directory(scratch, destination)


# ------------------------------------------------------------------- listing


class SymbolicLink(Exception):
    """A symbolic link in, or as, a run directory. Pig never makes one, and on the
    secure side one could point anywhere on the machine, so nothing follows one."""


def directories_in(directory: Path) -> list[str]:
    """The names of the directories directly inside this one, sorted, leaving out
    dot-names.

    A symbolic link is listed too, whatever it points to, so that whatever handles it
    finds it with `files_in` and says so, rather than passing it by without a word.

    `os.scandir` says whether each entry is a directory without a separate question per
    entry, which matters on a network share. See issue #42.
    """
    with os.scandir(directory) as entries:
        return sorted(
            entry.name
            for entry in entries
            if not entry.name.startswith(".")
            and (entry.is_symlink() or entry.is_dir(follow_symlinks=False))
        )


def files_in(run_directory: Path) -> list[PurePosixPath]:
    """Every file in a run directory, sorted, relative to it. Raises SymbolicLink if
    anything in it, the directory included, is a symbolic link.

    Listed with `os.scandir`, which says whether each entry is a link, a file, or a
    directory without a separate question per entry.
    """
    if run_directory.is_symlink():
        raise SymbolicLink("it's a symbolic link, not a directory")
    found = []
    waiting = [run_directory]
    while waiting:
        directory = waiting.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                relative = PurePosixPath(path.relative_to(run_directory).as_posix())
                if entry.is_symlink():
                    raise SymbolicLink(f"{relative} is a symbolic link")
                if entry.is_dir(follow_symlinks=False):
                    waiting.append(path)
                else:
                    found.append(relative)
    return sorted(found)


# ------------------------------------------------------------------- fsync


def flush_and_fsync(handle: IO[Any]) -> None:
    """Flush what's been written to an open file and fsync it, so it's on disk."""
    handle.flush()
    os.fsync(handle.fileno())


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


# ------------------------------------------------------------------- locking
#
# The lock that keeps two of the same job from working at once.
#
# `pig sweep`, `pigsty admit`, and `pigsty organize` each hold one while they work, so a
# second one started by an overlapping timer, or by hand, stops instead of doing the
# same work alongside the first. They all take it the same way, here:
#
#     try:
#         with files.hold_lock(path):
#             ...the work...
#     except files.LockHeld:
#         ...say so, and do nothing...
#
# It's a POSIX lock (`lockf`), rather than `flock`, because that's the kind an SMB or
# NFS mount passes on to the server, where a job on another machine would see it. The
# operating system lets go of it when the process ends, however it ends, so a crashed
# job leaves nothing to clean up. The file itself stays, empty; it's never deleted,
# since deleting it while another process is waiting to open it would let two jobs each
# lock a different file of the same name.
#
# A POSIX lock belongs to the process, not to the open file, which has two consequences
# the operating system won't warn about. A second lock on the same file from the same
# process succeeds rather than failing. And closing any open copy of the file in that
# process lets go of the lock, even a copy that was only opened to take it again. So
# this keeps track of what this process holds, and refuses a second hold before it gets
# as far as opening the file.


class LockHeld(Exception):
    """Something else holds the lock: another process, or this one, already."""


# Lock files this process holds, by resolved path, and a lock to guard the set itself.
_held_locks: set[Path] = set()
_held_locks_guard = threading.Lock()


@contextmanager
def hold_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on `path` for the length of the `with` block.

    Makes the file if it isn't there, and never changes what's in it. Raises
    `LockHeld`, before the block runs, if anything else holds the lock. Doesn't wait.
    """
    key = path.resolve()
    with _held_locks_guard:
        if key in _held_locks:
            raise LockHeld(f"this process already holds the lock on {path}")
        _held_locks.add(key)
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
        with _held_locks_guard:
            _held_locks.discard(key)
