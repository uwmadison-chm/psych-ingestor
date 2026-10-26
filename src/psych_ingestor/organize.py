"""Build a readable tree from a task's admitted runs.

`pigsty admit` keeps each task's runs in `{datastore_base}/runs/`, one directory per run,
named for its run ID. That's the record, and it's deliberately not readable:
`runs/9f3c1a7e-…/` tells a person nothing. This copies each run to a path built from its
`pig-run.json`, like `stroop/baseline/10351_0002/`, under the task's organized root.

Two rules the rest of this rests on. It never reads `pig.toml` or the database: by the
time it runs, neither may exist on this machine, so everything comes from `pig-run.json`.
And it never changes `runs/` or deletes anything from it. The tree is regenerable; to
rebuild one, delete it and run this again.

Each task's tree keeps a record of its own, `.placed.jsonl` in its organized root, a
line for each run placed in it. A task can have tens of thousands of runs, on a network
share where every file read is a round trip, so a second organize reads that file and
the listing of `runs/`, and reads the `pig-run.json` of new runs only. With `full`, it
ignores the record, checks every run against the tree, and writes the record again. See
issue #42.

It never changes the data either. Events and media parts are copied byte for byte, and
the only thing renamed is each media item's directory, from Pig's number to the event ID
the task gave it. They aren't checked against hashes: admit did that, and nothing in
`runs/` is there unless it matched. See issue #9.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Literal

from . import storage
from .admit import RUN_FILE, RUN_FILE_TYPE, RUN_FILE_VERSION, RUNS_DIRECTORY
from .config import SAFE_VALUE_EXPLANATION, is_safe_value
from .sty import BUILT_IN_NAMES, PLACEHOLDER

# What a run is copied into before it's renamed into place, so a tree never holds half
# a run. Starts with a dot so it can't collide with a path a pattern produces.
SCRATCH_SUFFIX = ".organizing"

# The tree's record of the runs placed in it, in the task's organized root. Starts with
# a dot for the same reason.
RECORD_FILE = ".placed.jsonl"

# Held while an organize works on a task's tree, so two at once, from scheduled jobs
# that overlap say, can't both copy the same run.
LOCK_FILE = ".organize.lock"

# How many runs are read and copied at once. On a network share the time goes to
# waiting for the server, not to work, so a few at once is several times faster. Each
# run is handled start to finish by one worker, and everything about it is reported
# and recorded by the main thread, in run ID order.
WORKERS = 8


class RunProblem(Exception):
    """Something about one run that stops it from being placed."""


class RecordProblem(Exception):
    """The tree's record of placed runs can't be read."""


@dataclass
class Recorded:
    """A run the tree's record says is already placed. Not read from `runs/`."""

    run_id: str
    # Relative to the organized root.
    destination: PurePosixPath


@dataclass
class Placement:
    """One run, and where in the tree it goes."""

    run_id: str
    source: Path
    # Relative to the organized root.
    destination: PurePosixPath
    # `pig-run.json` as it is in `runs/`, copied into the tree as it is.
    run_file_bytes: bytes
    # Pig's media directory names (`00001`) to the event IDs they become.
    media_names: dict[str, str]


@dataclass
class Outcome:
    """What happened to one run, from the worker that handled it."""

    status: Literal["placed", "already there", "not placed"]
    # Why it wasn't placed.
    problem: str = ""


@dataclass
class OrganizeReport:
    """What one organize did for one task, or would do with `dry_run`.

    `errors` are runs that weren't placed, keyed by their directory in `runs/`, and
    anything that stopped the task being organized at all.
    """

    placed: list[Placement] = field(default_factory=list)
    # Run IDs. Most come from the record, without their pig-run.json being read.
    already_there: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def organize(
    task_code: str,
    datastore: Path,
    root: Path,
    pattern: str,
    dry_run: bool = False,
    full: bool = False,
) -> OrganizeReport:
    """Place every run in `datastore/runs/` under `root`, by `pattern`.

    Safe to run again over the same tree: a run already in place is left alone. A run
    in the tree's record isn't read at all, unless `full`.
    """
    if dry_run:
        return _organize(task_code, datastore, root, pattern, dry_run, full)
    root.mkdir(parents=True, exist_ok=True)
    with open(root / LOCK_FILE, "a") as lock:
        # A POSIX lock, rather than flock, because that's the kind an SMB or NFS mount
        # passes on to the server, where another machine's organize would see it.
        try:
            fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            report = OrganizeReport()
            report.errors[str(root)] = (
                "another pigsty organize is working on this tree. Nothing was placed. "
                "Run this again once it's finished."
            )
            return report
        return _organize(task_code, datastore, root, pattern, dry_run, full)


def _organize(
    task_code: str,
    datastore: Path,
    root: Path,
    pattern: str,
    dry_run: bool,
    full: bool,
) -> OrganizeReport:
    report = OrganizeReport()
    runs = datastore / RUNS_DIRECTORY
    try:
        run_ids = _directories_in(runs)
    except FileNotFoundError:
        report.errors[str(runs)] = (
            "There's no runs directory here. pigsty admit makes it, and puts this "
            "task's runs in it."
        )
        return report

    if full:
        recorded: dict[str, Recorded] = {}
    else:
        try:
            recorded = read_record(root / RECORD_FILE)
        except RecordProblem as problem:
            report.errors[str(root / RECORD_FILE)] = str(problem)
            return report

    sources = [runs / run_id for run_id in run_ids if run_id not in recorded]
    planned = _plan(task_code, sources, pattern, recorded, report)
    with ThreadPoolExecutor(WORKERS) as pool:
        outcomes = pool.map(
            lambda placement: _organize_one(placement, root, dry_run), planned
        )
        record: BinaryIO | None = None
        try:
            for placement, outcome in zip(planned, outcomes, strict=True):
                if outcome.status == "not placed":
                    report.errors[str(placement.source)] = outcome.problem
                    continue
                if outcome.status == "placed":
                    report.placed.append(placement)
                else:
                    report.already_there.append(placement.run_id)
                if dry_run:
                    continue
                if record is None:
                    record = _open_record(root, full)
                _append_record(record, placement)
        finally:
            if record is not None:
                record.close()
    if full and not dry_run:
        _replace_record(root, wrote=record is not None)
    report.already_there.extend(recorded)
    return report


def _organize_one(placement: Placement, root: Path, dry_run: bool) -> Outcome:
    """Place one run. Run by the workers, so it reports nothing itself."""
    destination = root / placement.destination
    try:
        if _already_placed(destination, placement):
            return Outcome("already there")
        if not dry_run:
            _copy_run(placement, root, destination)
    except RunProblem as problem:
        return Outcome("not placed", str(problem))
    except OSError as error:
        return Outcome("not placed", f"couldn't copy it: {error}")
    return Outcome("placed")


def _directories_in(directory: Path) -> list[str]:
    """The names of the directories directly inside this one, leaving out dot-names.

    `os.scandir` says whether each entry is a directory without a separate question per
    entry, which matters on a network share. See issue #42.
    """
    with os.scandir(directory) as entries:
        return sorted(
            entry.name
            for entry in entries
            if not entry.name.startswith(".") and entry.is_dir()
        )


# ---------------------------------------------------------------------- the record


def read_record(path: Path) -> dict[str, Recorded]:
    """The runs a tree's record says are placed, by run ID.

    A tree with no record yet has placed nothing as far as it knows; runs already in it
    are found by their pig-run.json and recorded. A last line with no newline was being
    written when an organize stopped, and is left out: its run is found the same way.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as error:
        raise RecordProblem(_damaged(path, error)) from error
    recorded = {}
    # Everything up to the last newline. Whatever follows it is an unfinished line.
    for number, line in enumerate(text.split("\n")[:-1], start=1):
        try:
            entry = json.loads(line)
            run = Recorded(
                run_id=str(entry["run_id"]),
                destination=PurePosixPath(entry["destination"]),
            )
        except (ValueError, KeyError, TypeError) as error:
            raise RecordProblem(_damaged(path, f"line {number}: {error}")) from error
        recorded[run.run_id] = run
    return recorded


def _damaged(path: Path, why: object) -> str:
    return (
        f"can't read {path}, the record of runs placed in this tree ({why}). Nothing "
        "was placed. To check every run against the tree and write the record again, "
        "run pigsty organize --full."
    )


def _record_path(root: Path, full: bool) -> Path:
    """Where the record is, or with `full`, where its new one is written."""
    return root / (f"{RECORD_FILE}.partial" if full else RECORD_FILE)


def _open_record(root: Path, full: bool) -> BinaryIO:
    """The record, open to add lines to; with `full`, a new one beside it.

    The caller closes it.
    """
    path = _record_path(root, full)
    if full:
        record = open(path, "wb")  # noqa: SIM115 -- closed by the caller
        storage.fsync_directory(root)
        return record
    record = open(path, "a+b")  # noqa: SIM115 -- closed by the caller
    storage.fsync_directory(root)
    # A last line an earlier organize didn't finish is cut off, so this one's first
    # line starts on a line of its own. Usually only the last byte is read.
    record.seek(0, os.SEEK_END)
    if record.tell() > 0:
        record.seek(-1, os.SEEK_END)
        if record.read(1) != b"\n":
            record.seek(0)
            record.truncate(record.read().rfind(b"\n") + 1)
    return record


def _append_record(record: BinaryIO, placement: Placement) -> None:
    """Record one run as placed. Only after its directory is in place, so a record
    never names a run the tree doesn't have.

    Fsynced, so a line, once written, stays. A line lost to a crash before that only
    means the next organize finds that run by its pig-run.json, and records it then.
    """
    line = {
        "run_id": placement.run_id,
        "destination": placement.destination.as_posix(),
    }
    record.write((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
    record.flush()
    os.fsync(record.fileno())


def _replace_record(root: Path, wrote: bool) -> None:
    """After `full`, put the new record in place of the old one, or with no runs in
    the tree, remove the old one.

    The new record was written beside the old one and replaces it only now, so an
    interrupted --full leaves the old record rather than half of a new one.
    """
    if wrote:
        _record_path(root, full=True).replace(_record_path(root, full=False))
    else:
        _record_path(root, full=False).unlink(missing_ok=True)
    storage.fsync_directory(root)


# --------------------------------------------------------------------- planning


def _plan(
    task_code: str,
    sources: list[Path],
    pattern: str,
    recorded: dict[str, Recorded],
    report: OrganizeReport,
) -> list[Placement]:
    """Where each of these runs goes, with any two that would land together refused
    rather than placed."""
    planned: list[Placement] = []
    with ThreadPoolExecutor(WORKERS) as pool:
        places = pool.map(
            lambda source: _try_place(task_code, source, pattern), sources
        )
        for source, place in zip(sources, places, strict=True):
            if isinstance(place, Placement):
                planned.append(place)
            else:
                report.errors[str(source)] = place

    # Two runs in one place is always an error, and neither is placed: skipping the
    # second would make a run silently missing from the tree, and placing whichever
    # came first would make which one depend on directory order. One run inside
    # another's directory counts too. Compared without case, since `Baseline` and
    # `baseline` are one directory on a Mac.
    # Runs already placed are in the comparison too, so a new run can't land on one.
    # Only the new run is refused: the placed one is in the tree, and stays there.
    by_path: dict[tuple[str, ...], Placement | Recorded] = {}
    by_ancestor: dict[tuple[str, ...], Placement | Recorded] = {}
    colliding: dict[str, str] = {}
    for placement in [*recorded.values(), *planned]:
        key = tuple(part.casefold() for part in placement.destination.parts)
        ancestors = [key[:length] for length in range(1, len(key))]
        others = [by_path.get(key), by_ancestor.get(key)]
        others += [by_path.get(ancestor) for ancestor in ancestors]
        for other in others:
            if other is not None:
                colliding[placement.run_id] = _collision(placement, other)
                colliding[other.run_id] = _collision(other, placement)
        by_path.setdefault(key, placement)
        for ancestor in ancestors:
            by_ancestor.setdefault(ancestor, placement)

    kept = []
    for placement in planned:
        if placement.run_id in colliding:
            report.errors[str(placement.source)] = colliding[placement.run_id]
        else:
            kept.append(placement)
    return kept


def _try_place(task_code: str, source: Path, pattern: str) -> Placement | str:
    """Where one run goes, or why it can't be placed. Run by the workers."""
    try:
        return _place(task_code, source, pattern)
    except RunProblem as problem:
        return str(problem)
    except (KeyError, TypeError, ValueError) as error:
        # A pig-run.json admit wrote always has these. One that doesn't has been edited.
        return (
            f"{RUN_FILE} is missing something Pig always writes, or has an unexpected "
            f"value in it ({error!r})."
        )


def _collision(placement: Placement | Recorded, other: Placement | Recorded) -> str:
    return (
        f"run {placement.run_id} would go to {placement.destination}, which is the "
        f"same place as run {other.run_id} ({other.destination}), or inside it. "
        "Neither is placed; if one is already in the tree, it stays as it is. The "
        "pattern needs something that tells the two apart."
    )


def _place(task_code: str, source: Path, pattern: str) -> Placement:
    """Read one run's pig-run.json and work out where it goes."""
    run_file_bytes = _read_run_file_bytes(source)
    run = _parse_run_file(run_file_bytes)
    if run["run_id"] != source.name or run["task_code"] != task_code:
        raise RunProblem(
            f"{RUN_FILE} says this is run {run['run_id']} of task {run['task_code']}, "
            f"but it's in task {task_code}'s runs directory as {source.name}. One of "
            "them has been moved or edited."
        )
    return Placement(
        run_id=source.name,
        source=source,
        destination=render(pattern, run),
        run_file_bytes=run_file_bytes,
        media_names=_media_names(run),
    )


def _read_run_file_bytes(source: Path) -> bytes:
    try:
        return (source / RUN_FILE).read_bytes()
    except FileNotFoundError:
        raise RunProblem(
            f"there's no {RUN_FILE}. Everything in runs/ should have been put there by "
            "pigsty admit, which writes one in every run."
        ) from None
    except OSError as error:
        raise RunProblem(f"can't read {RUN_FILE}: {error}") from error


def _parse_run_file(run_file_bytes: bytes) -> dict[str, Any]:
    try:
        run = json.loads(run_file_bytes.decode("utf-8"))
    except ValueError as error:
        raise RunProblem(f"can't read {RUN_FILE}: {error}") from error
    if not isinstance(run, dict) or run.get("type") != RUN_FILE_TYPE:
        raise RunProblem(f"{RUN_FILE} isn't a Pig run file.")
    version = run.get("run_file_version")
    if version not in (1, RUN_FILE_VERSION):
        raise RunProblem(
            f"{RUN_FILE} is version {version!r}, and this version of Pig reads "
            f"versions 1 and {RUN_FILE_VERSION}."
        )
    if version == 1:
        # Admitted before parameters were split apart for signing, when every
        # parameter a task declared was a path parameter.
        run["path_parameters"] = list(run["parameters"])
    return run


def render(pattern: str, run: dict[str, Any]) -> PurePosixPath:
    """Fill a pattern from a run's pig-run.json, or raise RunProblem saying why it
    can't be.

    Parameters come from `path_parameters` only: those are the ones Pig checked against
    the safe-value rule when the run started. The task's other parameters are text,
    and `extra_parameters` were recorded as they arrived and never checked, so neither
    can become part of a path.
    """
    parameters = run["parameters"]
    path_parameters = run["path_parameters"]
    values: dict[str, str] = {
        "task_code": run["task_code"],
        "run_number": f"{run['run_number']:04d}",
    }

    def fill(match: Any) -> str:
        name = match.group(1)
        if name in BUILT_IN_NAMES:
            return values[name]
        if name in parameters and name not in path_parameters:
            raise RunProblem(
                f"the pattern uses {{{name}}}, which isn't one of this task's "
                "path_parameters. It's text, which can be anything, so it can't be "
                "used in a path. Add it to the task's path_parameters in pig.toml if it "
                "should be, and it will be held to the rule for values in paths."
            )
        if name in parameters:
            # Each value on its own, not just the path it makes: a value of `a/b`
            # would otherwise pass as two safe parts and add a directory.
            value = str(parameters[name])
            if not is_safe_value(value):
                raise RunProblem(
                    f"{name} is {value!r}, which can't be part of a path: "
                    f"{SAFE_VALUE_EXPLANATION}."
                )
            return value
        if name in run.get("extra_parameters", {}):
            raise RunProblem(
                f"the pattern uses {{{name}}}, which this task doesn't declare. It "
                "arrived in the link as an extra parameter, and those are never "
                "checked, so they can't be used in a path. Add it to the task's "
                "path_parameters in pig.toml if it should be."
            )
        raise RunProblem(
            f"the pattern uses {{{name}}}, and this run has no parameter by that "
            f"name. It has: {', '.join(parameters) or 'none'}."
        )

    rendered = PLACEHOLDER.sub(fill, pattern)
    # Checked again here, whatever was checked at ingest, because a hand-edited file
    # will happen, and because a web server that's been broken into can write
    # anything. This also covers the literal parts of the pattern, and an empty
    # part from `a//b`.
    for part in rendered.split("/"):
        if not is_safe_value(part):
            raise RunProblem(
                f"{part!r} in {rendered!r} can't be part of a path: "
                f"{SAFE_VALUE_EXPLANATION}."
            )
    return PurePosixPath(rendered)


def _media_names(run: dict[str, Any]) -> dict[str, str]:
    """Pig's media directory names to the event IDs they become in the tree.

    Event IDs are whatever the task chose, up to 256 characters of anything, which is
    why Pig numbers media directories in the first place. One that isn't a safe value,
    or two that differ only in case, stops the run rather than being renamed.
    """
    names: dict[str, str] = {}
    seen: dict[str, str] = {}
    for item in run.get("media", []):
        event_id = str(item["event_id"])
        if not is_safe_value(event_id):
            raise RunProblem(
                f"media event ID {event_id!r} can't be a directory name: "
                f"{SAFE_VALUE_EXPLANATION}."
            )
        if event_id.casefold() in seen:
            raise RunProblem(
                f"media event IDs {seen[event_id.casefold()]!r} and {event_id!r} "
                "differ only in case, so they'd be one directory on a Mac."
            )
        seen[event_id.casefold()] = event_id
        directory = storage.media_directory(Path(), int(item["media_id"])).name
        names[directory] = event_id
    return names


# --------------------------------------------------------------------- copying


def _already_placed(destination: Path, placement: Placement) -> bool:
    """Whether this run is already where it belongs. RunProblem if something else is.

    Reads the run file first and asks whether the destination exists only if that
    fails, since on a --full organize nearly every run is already there, and every
    question is a round trip on a network share.
    """
    try:
        there = json.loads((destination / RUN_FILE).read_text(encoding="utf-8"))
        there = there["run_id"]
    except FileNotFoundError:
        if not os.path.lexists(destination):
            return False
        there = None
    except (OSError, ValueError, KeyError, TypeError):
        there = None
    if there == placement.run_id:
        return True
    described = f"run {there}" if there else "something that isn't a placed run"
    raise RunProblem(
        f"run {placement.run_id} goes to {placement.destination}, and {described} is "
        "already there. Nothing was changed."
    )


def _copy_run(placement: Placement, root: Path, destination: Path) -> None:
    """Copy one run's files into place.

    Everything goes into a scratch directory beside the destination, which is renamed
    into place at the end, so a run in the tree is always a whole run. Every file and
    directory is fsynced before the rename, and the directories above it after, so a
    run the record names is on disk.
    """
    # Before anything is made, so a run with a symbolic link leaves nothing behind.
    files = _files_in(placement.source)
    scratch = destination.with_name(f".{destination.name}{SCRATCH_SUFFIX}")
    try:
        # Left by an organize that was interrupted. Ours by its name, never runs/'s.
        shutil.rmtree(scratch)
    except FileNotFoundError:
        pass
    scratch.mkdir(parents=True)
    made = {scratch}

    for relative in files:
        if relative == PurePosixPath(RUN_FILE):
            continue
        target = scratch / _renamed(relative, placement.media_names)
        _copy(placement.source / relative, target, made)

    # pig-run.json goes in last, and it's what a later organize looks for to know this
    # run is already placed. It's the bytes read while planning, so it isn't read twice.
    with open(scratch / RUN_FILE, "wb") as run_file:
        run_file.write(placement.run_file_bytes)
        run_file.flush()
        os.fsync(run_file.fileno())
    # Each directory made, and each between it and the scratch directory, for the
    # entries in it.
    to_sync = {scratch}
    for directory in made:
        while directory != scratch:
            to_sync.add(directory)
            directory = directory.parent
    for directory in to_sync:
        storage.fsync_directory(directory)
    scratch.rename(destination)
    # The rename, and any directory made above the run on the way to it.
    for directory in destination.parents:
        storage.fsync_directory(directory)
        if directory == root:
            break


def _files_in(run_directory: Path) -> list[PurePosixPath]:
    """Every file in a run directory, relative to it, leaving out `.state/`, or
    RunProblem if anything in it is a symbolic link.

    `pigsty admit` refuses symbolic links, so there shouldn't be any, but one could
    point anywhere on this machine. Listed with `os.scandir`, which says whether each
    entry is a link, a file, or a directory without a separate question per entry.
    """
    found = []
    waiting = [run_directory]
    while waiting:
        directory = waiting.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                relative = PurePosixPath(path.relative_to(run_directory).as_posix())
                if relative == PurePosixPath(storage.STATE_DIRECTORY):
                    continue
                if entry.is_symlink():
                    raise RunProblem(
                        f"{relative} is a symbolic link. Pig never makes one, so the "
                        "run wasn't placed."
                    )
                if entry.is_dir(follow_symlinks=False):
                    waiting.append(path)
                else:
                    found.append(relative)
    return sorted(found)


def _renamed(relative: PurePosixPath, media_names: dict[str, str]) -> PurePosixPath:
    """A file's path in the tree: the same as in `runs/`, except that a media item's
    directory is named for its event ID."""
    parts = relative.parts
    is_media = len(parts) >= 3 and parts[0] == storage.MEDIA_DIRECTORY
    if is_media and parts[1] in media_names:
        return PurePosixPath(parts[0], media_names[parts[1]], *parts[2:])
    return relative


def _copy(source: Path, target: Path, made: set[Path]) -> None:
    """Copy one file and fsync it. `made` is the directories this run has already
    created, so each is created once rather than once per file."""
    if target.parent not in made:
        target.parent.mkdir(parents=True, exist_ok=True)
        made.add(target.parent)
    with open(source, "rb") as reading, open(target, "wb") as writing:
        shutil.copyfileobj(reading, writing, 1024 * 1024)
        writing.flush()
        os.fsync(writing.fileno())
