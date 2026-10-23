"""Build a readable tree from a store of finished run directories.

The store is what the web server's `done/` holds, copied to wherever the data ends up:
one directory per run, named for its run ID, each with a manifest. That's the record,
and it's deliberately not readable — `stroop/9f3c1a7e-…/` tells a person nothing. This
copies each run to a path built from its manifest, like `stroop/10351/baseline/run-0002/`.

Two rules the rest of this rests on. It never reads `pig.toml` or the database: by the
time it runs, neither may exist on this machine, so everything comes from the manifests.
And it never changes the store or deletes anything from it. The tree is regenerable; to
rebuild one, delete it and run this again.

The tree keeps one file of its own, `.placed.jsonl`, a line for each run placed in it.
A store can hold tens of thousands of runs, on a network share where every file read is
a round trip, so a second organize reads that one file and the store's listing, and
reads the manifests of new runs only. With `full`, it ignores the record, checks every
run against the tree, and writes the record again. See issue #42.

It never changes the data either. Events and media parts are copied byte for byte, and
the only thing renamed is each media item's directory, from Pig's number to the event ID
the task gave it. See issue #9.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Literal

from . import storage
from .config import SAFE_VALUE_EXPLANATION, is_safe_value
from .sty import BUILT_IN_NAMES, PLACEHOLDER, PatternError, check_pattern
from .sty import default_pattern as make_default_pattern

# What a run is copied into before it's renamed into place, so a tree never holds half
# a run. Starts with a dot so it can't collide with a path a pattern produces.
SCRATCH_SUFFIX = ".organizing"

# The tree's record of the runs placed in it. Starts with a dot for the same reason.
RECORD_FILE = ".placed.jsonl"

# How many runs are read and copied at once. On a network share the time goes to
# waiting for the server, not to work, so a few at once is several times faster. Each
# run is handled start to finish by one worker, and everything about it is reported
# and recorded by the main thread, in store order.
WORKERS = 8


class RunProblem(Exception):
    """Something about one run that stops it from being placed."""


class RecordProblem(Exception):
    """The tree's record of placed runs can't be read."""


@dataclass
class Recorded:
    """A run the tree's record says is already placed. Not read from the store."""

    run_id: str
    task_code: str
    # Relative to the organized directory.
    destination: PurePosixPath


@dataclass
class Placement:
    """One run, and where in the tree it goes."""

    run_id: str
    source: Path
    # Relative to the organized directory.
    destination: PurePosixPath
    manifest: dict[str, Any]
    # The manifest file as it is in the store, copied into the tree as it is.
    manifest_bytes: bytes
    # Pig's media directory names (`00001`) to the event IDs they become.
    media_names: dict[str, str]


@dataclass
class Outcome:
    """What happened to one run, from the worker that handled it."""

    status: Literal["placed", "already there", "not placed"]
    warnings: list[str]
    # Why it wasn't placed.
    problem: str = ""


@dataclass
class OrganizeReport:
    """What one organize did, or would do with `dry_run`.

    `errors` are runs that weren't placed, keyed by their directory in the store.
    `warnings` are runs that were placed but whose files didn't all match the manifest:
    a file missing, a hash that differs, a file the manifest doesn't list. Either one is
    something a person needs to look at.
    """

    placed: list[Placement] = field(default_factory=list)
    # Run IDs. Most come from the record, without their manifests being read.
    already_there: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def organize(
    store: Path,
    output: Path,
    patterns: dict[str, str],
    dry_run: bool = False,
    full: bool = False,
) -> OrganizeReport:
    """Place every run in `store` under `output`.

    `patterns` maps a task code to its pattern; a task without one gets the default.
    Safe to run again over the same tree: a run already in place is left alone. A run
    in the tree's record isn't read at all, unless `full`.
    """
    report = OrganizeReport()
    record_path = output / RECORD_FILE
    if full:
        recorded: dict[tuple[str, str], Recorded] = {}
    else:
        try:
            recorded = read_record(record_path)
        except RecordProblem as problem:
            report.errors[str(record_path)] = str(problem)
            return report

    planned = _plan(store, patterns, recorded, report)
    with ThreadPoolExecutor(WORKERS) as pool:
        outcomes = pool.map(
            lambda placement: _organize_one(placement, output, dry_run), planned
        )
        # Opened when there's a first line for it, so an organize that places
        # nothing leaves no trace.
        record: BinaryIO | None = None
        try:
            for placement, outcome in zip(planned, outcomes, strict=True):
                report.warnings.extend(outcome.warnings)
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
                    record = _open_record(record_path, full)
                _append_record(record, placement)
        finally:
            if record is not None:
                record.close()
    if full and not dry_run:
        # The new record was written beside the old one and replaces it now, so an
        # interrupted --full leaves the old record rather than half a new one.
        if record is not None:
            _partial_record(record_path).replace(record_path)
        else:
            record_path.unlink(missing_ok=True)
    report.already_there.extend(entry.run_id for entry in recorded.values())
    return report


def _organize_one(placement: Placement, output: Path, dry_run: bool) -> Outcome:
    """Place one run. Run by the workers, so it reports nothing itself."""
    destination = output / placement.destination
    warnings: list[str] = []
    try:
        if _already_placed(destination, placement):
            return Outcome("already there", warnings)
        if not dry_run:
            _copy_run(placement, destination, warnings)
    except RunProblem as problem:
        return Outcome("not placed", warnings, str(problem))
    except OSError as error:
        return Outcome("not placed", warnings, f"couldn't copy it: {error}")
    return Outcome("placed", warnings)


# ---------------------------------------------------------------------- the record


def read_record(path: Path) -> dict[tuple[str, str], Recorded]:
    """The runs a tree's record says are placed, by task code and run ID.

    A tree with no record yet has placed nothing as far as the record knows; runs
    already in it are found by their manifests and recorded. A last line with no
    newline was being written when an organize stopped, and is left out: its run is
    found the same way.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as error:
        raise RecordProblem(_damaged(path, error)) from error
    lines = text.split("\n")
    # Everything up to the last newline. Whatever follows it is an unfinished line.
    recorded = {}
    for number, line in enumerate(lines[:-1], start=1):
        try:
            entry = json.loads(line)
            run = Recorded(
                run_id=str(entry["run_id"]),
                task_code=str(entry["task_code"]),
                destination=PurePosixPath(entry["destination"]),
            )
        except (ValueError, KeyError, TypeError) as error:
            raise RecordProblem(_damaged(path, f"line {number}: {error}")) from error
        recorded[(run.task_code, run.run_id)] = run
    return recorded


def _damaged(path: Path, why: object) -> str:
    return (
        f"can't read {path}, the record of runs placed in this tree ({why}). Nothing "
        "was placed. To check every run against the tree and write the record again, "
        "run pig organize --full."
    )


def _partial_record(path: Path) -> Path:
    return path.with_name(path.name + ".partial")


def _open_record(path: Path, full: bool) -> BinaryIO:
    """The record, open to add lines to; with `full`, a new one beside it.

    The caller closes it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if full:
        return open(_partial_record(path), "wb")
    record = open(path, "a+b")  # noqa: SIM115
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
    """Record one run as placed. Only after its directory is in place, so the record
    never names a run the tree doesn't have.

    Not fsynced: a line lost to a crash only means the next organize finds that run by
    its manifest, and records it then.
    """
    line = {
        "run_id": placement.run_id,
        "task_code": placement.manifest["task_code"],
        "destination": placement.destination.as_posix(),
    }
    record.write((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
    record.flush()


# --------------------------------------------------------------------- planning


def _plan(
    store: Path,
    patterns: dict[str, str],
    recorded: dict[tuple[str, str], Recorded],
    report: OrganizeReport,
) -> list[Placement]:
    """Where every run in the store that isn't recorded goes, with any two that would
    land together refused rather than placed."""
    sources = [
        source
        for source in _run_directories(store, report)
        if (source.parent.name, source.name) not in recorded
    ]
    planned: list[Placement] = []
    with ThreadPoolExecutor(WORKERS) as pool:
        places = pool.map(lambda source: _try_place(source, patterns), sources)
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


def _try_place(source: Path, patterns: dict[str, str]) -> Placement | str:
    """Where one run goes, or why it can't be placed. Run by the workers."""
    try:
        return _place(source, patterns)
    except RunProblem as problem:
        return str(problem)
    except (KeyError, TypeError, ValueError) as error:
        # A manifest Pig wrote always has these. One that doesn't has been edited.
        return (
            f"manifest.json is missing something Pig always writes, or has an "
            f"unexpected value in it ({error!r})."
        )


def _collision(placement: Placement | Recorded, other: Placement | Recorded) -> str:
    return (
        f"run {placement.run_id} would go to {placement.destination}, which is the "
        f"same place as run {other.run_id} ({other.destination}), or inside it. "
        "Neither is placed; if one is already in the tree, it stays as it is. The "
        "pattern needs something that tells the two apart."
    )


def _run_directories(store: Path, report: OrganizeReport) -> list[Path]:
    """Every `{task_code}/{run_id}/` directory in the store.

    Listed with `os.scandir`, which says whether each entry is a directory without
    asking the filesystem about each one separately. On a network share, every
    separate question is a round trip, and the store may hold tens of thousands of
    runs. See issue #42.
    """
    if not store.is_dir():
        report.errors[str(store)] = "There's no directory here to organize."
        return []
    found = []
    for task_directory in _directories_in(store):
        found.extend(_directories_in(task_directory))
    return found


def _directories_in(directory: Path) -> list[Path]:
    """The directories directly inside this one, by name, leaving out dot-names."""
    with os.scandir(directory) as entries:
        names = [
            entry.name
            for entry in entries
            if not entry.name.startswith(".") and entry.is_dir()
        ]
    return [directory / name for name in sorted(names)]


def _place(source: Path, patterns: dict[str, str]) -> Placement:
    """Read one run's manifest and work out where it goes."""
    manifest_bytes = _read_manifest_bytes(source)
    manifest = _parse_manifest(manifest_bytes)
    run_id = manifest["run_id"]
    task_code = manifest["task_code"]
    if source.name != run_id or source.parent.name != task_code:
        raise RunProblem(
            f"the manifest says this is run {run_id} of task {task_code}, but it's "
            f"stored at {source.parent.name}/{source.name}. One of them has been "
            "moved or edited."
        )

    pattern = patterns.get(task_code) or make_default_pattern(
        task_code, manifest["run_key"]
    )
    try:
        check_pattern(pattern)
    except PatternError as error:
        raise RunProblem(str(error)) from None

    return Placement(
        run_id=run_id,
        source=source,
        destination=render(pattern, manifest),
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        media_names=_media_names(manifest),
    )


def _read_manifest_bytes(source: Path) -> bytes:
    try:
        return (source / storage.MANIFEST_FILE).read_bytes()
    except FileNotFoundError:
        raise RunProblem(
            "there's no manifest.json, so this run isn't finished. The store should "
            "only hold runs from the web server's done/ directory, never in_progress/."
        ) from None
    except OSError as error:
        raise RunProblem(f"can't read manifest.json: {error}") from error


def _parse_manifest(manifest_bytes: bytes) -> dict[str, Any]:
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except ValueError as error:
        raise RunProblem(f"can't read manifest.json: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("type") != (
        storage.MANIFEST_TYPE
    ):
        raise RunProblem("manifest.json isn't a Pig run manifest.")
    if manifest.get("manifest_version") != storage.MANIFEST_VERSION:
        raise RunProblem(
            f"manifest.json is version {manifest.get('manifest_version')!r}, and this "
            f"version of Pig reads version {storage.MANIFEST_VERSION}."
        )
    return manifest


def render(pattern: str, manifest: dict[str, Any]) -> PurePosixPath:
    """Fill a pattern from a manifest, or raise RunProblem saying why it can't be.

    Parameters come from the manifest's `parameters` only: those are the ones the task
    declared, and Pig checked every one against the safe-value rule when the run
    started. `extra_parameters` were recorded as they arrived and never checked, so
    they can't become part of a path.
    """
    parameters = manifest["parameters"]
    values: dict[str, str] = {
        "task_code": manifest["task_code"],
        "run_number": f"{manifest['run_number']:04d}",
    }

    def fill(match: Any) -> str:
        name = match.group(1)
        if name in BUILT_IN_NAMES:
            return values[name]
        if name in parameters:
            return str(parameters[name])
        if name in manifest.get("extra_parameters", {}):
            raise RunProblem(
                f"the pattern uses {{{name}}}, which this task doesn't declare. It "
                "arrived in the link as an extra parameter, and those are never "
                "checked, so they can't be used in a path. Add it to the task's "
                "parameters in pig.toml if it should be."
            )
        raise RunProblem(
            f"the pattern uses {{{name}}}, and this run has no parameter by that "
            f"name. It has: {', '.join(parameters) or 'none'}."
        )

    rendered = PLACEHOLDER.sub(fill, pattern)
    # Checked again here, whatever was checked at ingest, because a hand-edited
    # manifest will happen. This also covers the literal parts of the pattern, and
    # an empty part from `a//b`.
    for part in rendered.split("/"):
        if not is_safe_value(part):
            raise RunProblem(
                f"{part!r} in {rendered!r} can't be part of a path: "
                f"{SAFE_VALUE_EXPLANATION}."
            )
    return PurePosixPath(rendered)


def _media_names(manifest: dict[str, Any]) -> dict[str, str]:
    """Pig's media directory names to the event IDs they become in the tree.

    Event IDs are whatever the task chose, up to 256 characters of anything, which is
    why Pig numbers media directories in the first place. One that isn't a safe value,
    or two that differ only in case, stops the run rather than being renamed.
    """
    names: dict[str, str] = {}
    seen: dict[str, str] = {}
    for item in manifest.get("media", []):
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

    Reads the manifest first and asks whether the destination exists only if that
    fails, since on a second organize nearly every run is already there, and every
    question is a round trip on a network share.
    """
    manifest_path = destination / storage.MANIFEST_FILE
    try:
        there = json.loads(manifest_path.read_text(encoding="utf-8"))["run_id"]
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


def _copy_run(placement: Placement, destination: Path, warnings: list[str]) -> None:
    """Copy one run's files into place, checking each against the manifest, and adding
    to `warnings` each file that doesn't match.

    Everything goes into a scratch directory beside the destination, which is renamed
    into place at the end, so a run in the tree is always a whole run.
    """
    scratch = destination.with_name(f".{destination.name}{SCRATCH_SUFFIX}")
    try:
        # Left by an organize that was interrupted. Ours by its name, never the store's.
        shutil.rmtree(scratch)
    except FileNotFoundError:
        pass
    scratch.mkdir(parents=True)
    made = {scratch}

    listed = set()
    for entry in placement.manifest["files"]:
        relative = PurePosixPath(entry["path"])
        listed.add(relative)
        target = scratch / _renamed(relative, placement.media_names)
        copied = _copy_and_hash(placement.source / relative, target, made)
        if copied is None:
            warnings.append(
                f"run {placement.run_id}: {relative} is in the manifest but missing "
                "from the store."
            )
            continue
        size, digest = copied
        if size != entry["bytes"] or digest != entry["sha256"]:
            warnings.append(
                f"run {placement.run_id}: {relative} doesn't match the manifest "
                f"({size} bytes, sha256 {digest}; the manifest says {entry['bytes']} "
                f"bytes, sha256 {entry['sha256']}). It was copied as it is."
            )

    for relative in _files_in(placement.source):
        if relative != PurePosixPath(storage.MANIFEST_FILE) and relative not in listed:
            warnings.append(
                f"run {placement.run_id}: {relative} is in the store but not in the "
                "manifest. It wasn't copied."
            )

    # The manifest goes in last, and it's what a later organize looks for to know this
    # run is already placed. It's the bytes read from the store while planning, so the
    # store's copy isn't read twice.
    (scratch / storage.MANIFEST_FILE).write_bytes(placement.manifest_bytes)
    scratch.rename(destination)


def _files_in(run_directory: Path) -> list[PurePosixPath]:
    """Every file in a run directory, relative to it, leaving out `.state/`.

    `os.walk` lists each directory once, rather than asking about every file in it.
    """
    found = []
    for directory, subdirectories, files in os.walk(run_directory):
        here = Path(directory)
        if here == run_directory and storage.STATE_DIRECTORY in subdirectories:
            subdirectories.remove(storage.STATE_DIRECTORY)
        for name in files:
            found.append(
                PurePosixPath((here / name).relative_to(run_directory).as_posix())
            )
    return sorted(found)


def _renamed(relative: PurePosixPath, media_names: dict[str, str]) -> PurePosixPath:
    """A file's path in the tree: the same as in the store, except that a media
    item's directory is named for its event ID."""
    parts = relative.parts
    is_media = len(parts) >= 3 and parts[0] == storage.MEDIA_DIRECTORY
    if is_media and parts[1] in media_names:
        return PurePosixPath(parts[0], media_names[parts[1]], *parts[2:])
    return relative


def _copy_and_hash(
    source: Path, target: Path, made: set[Path]
) -> tuple[int, str] | None:
    """Copy one file, returning its size and hash, or None if the source isn't there.

    `made` is the directories this run has already created, so each is created once
    rather than once per file.
    """
    # Opened outside `with` so that only a missing source means "missing", never a
    # problem writing the target.
    try:
        reading = open(source, "rb")  # noqa: SIM115
    except (FileNotFoundError, IsADirectoryError):
        return None
    digest = hashlib.sha256()
    size = 0
    with reading:
        if target.parent not in made:
            target.parent.mkdir(parents=True, exist_ok=True)
            made.add(target.parent)
        with open(target, "wb") as writing:
            while chunk := reading.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                writing.write(chunk)
    return size, digest.hexdigest()
