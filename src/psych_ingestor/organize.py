"""Build a readable tree from a store of finished run directories.

The store is what the web server's `done/` holds, copied to wherever the data ends up:
one directory per run, named for its run ID, each with a manifest. That's the record,
and it's deliberately not readable — `stroop/9f3c1a7e-…/` tells a person nothing. This
copies each run to a path built from its manifest, like `stroop/10351/baseline/run-0002/`.

Two rules the rest of this rests on. It never reads `pig.toml` or the database: by the
time it runs, neither may exist on this machine, so everything comes from the manifests.
And it never changes the store or deletes anything from it. The tree is regenerable; to
rebuild one, delete it and run this again.

It never changes the data either. Events and media parts are copied byte for byte, and
the only thing renamed is each media item's directory, from Pig's number to the event ID
the task gave it. See issue #9.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import storage
from .config import SAFE_VALUE_EXPLANATION, is_safe_value
from .sty import BUILT_IN_NAMES, PLACEHOLDER, PatternError, check_pattern
from .sty import default_pattern as make_default_pattern

# What a run is copied into before it's renamed into place, so a tree never holds half
# a run. Starts with a dot so it can't collide with a path a pattern produces.
SCRATCH_SUFFIX = ".organizing"


class RunProblem(Exception):
    """Something about one run that stops it from being placed."""


@dataclass
class Placement:
    """One run, and where in the tree it goes."""

    run_id: str
    source: Path
    # Relative to the organized directory.
    destination: PurePosixPath
    manifest: dict[str, Any]
    # Pig's media directory names (`00001`) to the event IDs they become.
    media_names: dict[str, str]


@dataclass
class OrganizeReport:
    """What one organize did, or would do with `dry_run`.

    `errors` are runs that weren't placed, keyed by their directory in the store.
    `warnings` are runs that were placed but whose files didn't all match the manifest:
    a file missing, a hash that differs, a file the manifest doesn't list. Either one is
    something a person needs to look at.
    """

    placed: list[Placement] = field(default_factory=list)
    already_there: list[Placement] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def organize(
    store: Path,
    output: Path,
    patterns: dict[str, str],
    dry_run: bool = False,
) -> OrganizeReport:
    """Place every run in `store` under `output`.

    `patterns` maps a task code to its pattern; a task without one gets the default.
    Safe to run again over the same tree: a run already in place is left alone.
    """
    report = OrganizeReport()
    for placement in _plan(store, patterns, report):
        destination = output / placement.destination
        already = _already_placed(destination, placement, report)
        if already:
            report.already_there.append(placement)
            continue
        if already is None:
            continue
        if not dry_run:
            try:
                _copy_run(placement, destination, report)
            except OSError as error:
                report.errors[str(placement.source)] = f"couldn't copy it: {error}"
                continue
        report.placed.append(placement)
    return report


# --------------------------------------------------------------------- planning


def _plan(
    store: Path, patterns: dict[str, str], report: OrganizeReport
) -> list[Placement]:
    """Where every run in the store goes, with any two that would land together
    refused rather than placed."""
    planned: list[Placement] = []
    for source in _run_directories(store, report):
        try:
            planned.append(_place(source, patterns))
        except RunProblem as problem:
            report.errors[str(source)] = str(problem)
        except (KeyError, TypeError, ValueError) as error:
            # A manifest Pig wrote always has these. One that doesn't has been edited.
            report.errors[str(source)] = (
                f"manifest.json is missing something Pig always writes, or has an "
                f"unexpected value in it ({error!r})."
            )

    # Two runs in one place is always an error, and neither is placed: skipping the
    # second would make a run silently missing from the tree, and placing whichever
    # came first would make which one depend on directory order. One run inside
    # another's directory counts too. Compared without case, since `Baseline` and
    # `baseline` are one directory on a Mac.
    by_path: dict[tuple[str, ...], Placement] = {}
    by_ancestor: dict[tuple[str, ...], Placement] = {}
    colliding: dict[str, str] = {}
    for placement in planned:
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


def _collision(placement: Placement, other: Placement) -> str:
    return (
        f"run {placement.run_id} would go to {placement.destination}, which is the "
        f"same place as run {other.run_id} ({other.destination}), or inside it. "
        "Neither is placed; if one is already in the tree, it stays as it is. The "
        "pattern needs something that tells the two apart."
    )


def _run_directories(store: Path, report: OrganizeReport) -> list[Path]:
    """Every `{task_code}/{run_id}/` directory in the store."""
    if not store.is_dir():
        report.errors[str(store)] = "There's no directory here to organize."
        return []
    found = []
    for task_directory in sorted(store.iterdir()):
        if task_directory.name.startswith(".") or not task_directory.is_dir():
            continue
        for run_directory in sorted(task_directory.iterdir()):
            if run_directory.name.startswith(".") or not run_directory.is_dir():
                continue
            found.append(run_directory)
    return found


def _place(source: Path, patterns: dict[str, str]) -> Placement:
    """Read one run's manifest and work out where it goes."""
    manifest = _read_manifest(source)
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
        media_names=_media_names(manifest),
    )


def _read_manifest(source: Path) -> dict[str, Any]:
    if not (source / storage.MANIFEST_FILE).exists():
        raise RunProblem(
            "there's no manifest.json, so this run isn't finished. The store should "
            "only hold runs from the web server's done/ directory, never in_progress/."
        )
    try:
        manifest = storage.read_manifest(source)
    except (OSError, ValueError) as error:
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


def _already_placed(
    destination: Path, placement: Placement, report: OrganizeReport
) -> bool | None:
    """Whether this run is already where it belongs: True if so, False if nothing's
    there, None if something else is (recorded as an error)."""
    if not destination.exists():
        return False
    manifest_path = destination / storage.MANIFEST_FILE
    try:
        there = json.loads(manifest_path.read_text(encoding="utf-8"))["run_id"]
    except (OSError, ValueError, KeyError, TypeError):
        there = None
    if there == placement.run_id:
        return True
    described = f"run {there}" if there else "something that isn't a placed run"
    report.errors[str(placement.source)] = (
        f"run {placement.run_id} goes to {placement.destination}, and {described} is "
        "already there. Nothing was changed."
    )
    return None


def _copy_run(placement: Placement, destination: Path, report: OrganizeReport) -> None:
    """Copy one run's files into place, checking each against the manifest.

    Everything goes into a scratch directory beside the destination, which is renamed
    into place at the end, so a run in the tree is always a whole run.
    """
    scratch = destination.with_name(f".{destination.name}{SCRATCH_SUFFIX}")
    if scratch.exists():
        # Left by an organize that was interrupted. Ours by its name, never the store's.
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)

    listed = set()
    for entry in placement.manifest["files"]:
        relative = PurePosixPath(entry["path"])
        listed.add(relative)
        source = placement.source / relative
        if not source.is_file():
            report.warnings.append(
                f"run {placement.run_id}: {relative} is in the manifest but missing "
                "from the store."
            )
            continue
        target = scratch / _renamed(relative, placement.media_names)
        size, digest = _copy_and_hash(source, target)
        if size != entry["bytes"] or digest != entry["sha256"]:
            report.warnings.append(
                f"run {placement.run_id}: {relative} doesn't match the manifest "
                f"({size} bytes, sha256 {digest}; the manifest says {entry['bytes']} "
                f"bytes, sha256 {entry['sha256']}). It was copied as it is."
            )

    for path in sorted(placement.source.rglob("*")):
        relative = PurePosixPath(path.relative_to(placement.source).as_posix())
        if not path.is_file() or relative.parts[0] == storage.STATE_DIRECTORY:
            continue
        if relative != PurePosixPath(storage.MANIFEST_FILE) and relative not in listed:
            report.warnings.append(
                f"run {placement.run_id}: {relative} is in the store but not in the "
                "manifest. It wasn't copied."
            )

    # The manifest goes in last, and it's what a later organize looks for to know this
    # run is already placed.
    shutil.copyfile(
        placement.source / storage.MANIFEST_FILE, scratch / storage.MANIFEST_FILE
    )
    scratch.rename(destination)


def _renamed(relative: PurePosixPath, media_names: dict[str, str]) -> PurePosixPath:
    """A file's path in the tree: the same as in the store, except that a media
    item's directory is named for its event ID."""
    parts = relative.parts
    is_media = len(parts) >= 3 and parts[0] == storage.MEDIA_DIRECTORY
    if is_media and parts[1] in media_names:
        return PurePosixPath(parts[0], media_names[parts[1]], *parts[2:])
    return relative


def _copy_and_hash(source: Path, target: Path) -> tuple[int, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with open(source, "rb") as reading, open(target, "wb") as writing:
        while chunk := reading.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
            writing.write(chunk)
    return size, digest.hexdigest()
