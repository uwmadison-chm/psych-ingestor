"""What Pig writes to disk, and where.

Every run is a directory named for its run ID, under `in_progress/{task_code}/` while Pig
is still working on it and under `done/{task_code}/` once the sweep has finished with it.
Inside are the run's events, one JSON object per line in `events.jsonl`, and — once the run
is done — a manifest describing the run. Nothing here reads `pig.toml` or the database:
a run directory has to make sense on its own, on a machine that has neither.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import IO, Any

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
# How much of a media part PartWriter keeps: enough to tell an encrypted one.
START_LENGTH = 64

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
        flush_and_fsync(handle)
    if size == 0:
        # The first lines, so most likely a new file. Its name needs fsyncing too, or a
        # crash can take the file, and the events Pig just said were stored, with it.
        fsync_directory(path.parent)


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
        flush_and_fsync(self._handle)
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


def create_empty_file(path: Path) -> None:
    """An empty file, durably. For a run that never sent an event."""
    with open(path, "x", encoding="utf-8") as handle:
        flush_and_fsync(handle)
    fsync_directory(path.parent)


def touch(path: Path) -> None:
    """Open a file for writing and close it, then set its modification time to now.

    Both, so that whatever is watching sees it: a systemd path unit or inotify sees the
    close, and something that compares modification times sees the time. Nothing is
    written, and nothing is fsynced, because nothing here is data.
    """
    with open(path, "a", encoding="utf-8"):
        pass
    os.utime(path)


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
        flush_and_fsync(handle)
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
#         with storage.hold_lock(path):
#             ...the work...
#     except storage.LockHeld:
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
