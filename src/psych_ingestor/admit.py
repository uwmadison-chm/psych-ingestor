"""Admit downloaded runs: check each one against its manifest and move it into `runs/`.

Runs reach the secure side by a copy Pig doesn't run, `rclone sync` from the bucket or
something like it, into each task's `incoming/`. That copy isn't atomic. A run can be
half there for a while, and its manifest can arrive before the files it lists. So nothing
on the secure side reads `incoming/`, except this, and purge, which needs the bytes the
web server has.

A run is admitted once every file its manifest lists is there with the right size and
hash, and nothing else is. Admitting copies it into a scratch directory beside `runs/`
and renames that into place, the way `pig sweep` moves a run into `done/`. So a run in
`runs/` is always whole, and every later step can trust that it is.

What lands in `runs/{run_id}/` is the run's files, byte for byte, and `pig-run.json`: what
the run is (task, run number, parameters, media), taken from the manifest without the
file hashes. The manifest itself stays in `incoming/`. It describes the files as they
traveled, and once decryption (#10) happens here, those aren't the files in `runs/`.

Nothing in `incoming/` is changed or deleted. It's a mirror of the bucket, and it empties
itself as runs are purged from the web server and drop out of the bucket.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from . import storage

INCOMING_DIRECTORY = "incoming"
RUNS_DIRECTORY = "runs"

# What a run is, without its file hashes. Written by admit into `runs/{run_id}/`, and
# copied by organize into the readable tree.
RUN_FILE = "pig-run.json"
RUN_FILE_TYPE = "pig_run"
RUN_FILE_VERSION = 1

# The manifest's fields that describe the run rather than its files.
RUN_FIELDS = (
    "run_id",
    "task_code",
    "run_number",
    "run_key",
    "disposition",
    "started_at",
    "closed_at",
    "done_at",
    "parameters",
    "extra_parameters",
    "media",
)

# What a run is copied into before it's renamed into `runs/`. Starts with a dot, so a
# listing of `runs/` never mistakes it for an admitted run.
SCRATCH_SUFFIX = ".admitting"

# Held while an admit works on a task's datastore, so two at once can't both admit the
# same run.
LOCK_FILE = ".admit.lock"

# How many runs are checked and copied at once. On a network share the time goes to
# waiting for the server, so a few at once is several times faster. Each run is handled
# start to finish by one worker, and reported by the main thread, in order.
WORKERS = 8


class StillArriving(Exception):
    """A run that isn't all here yet. Not a problem: the next admit tries again."""


class RunProblem(Exception):
    """A run that's all here but wrong. Someone needs to look at it."""


@dataclass
class AdmitReport:
    """What one admit did for one task.

    `arriving` are runs not all downloaded yet, which the next admit will try again.
    `problems` are runs that won't ever be admitted as they are, by their directory in
    `incoming/`.
    """

    admitted: list[str] = field(default_factory=list)
    already_admitted: int = 0
    arriving: list[str] = field(default_factory=list)
    problems: dict[str, str] = field(default_factory=dict)


@dataclass
class Outcome:
    """What happened to one run, from the worker that handled it."""

    status: Literal["admitted", "arriving", "problem"]
    why: str = ""


def admit(task_code: str, datastore: Path) -> AdmitReport:
    """Admit every run in `datastore/incoming/` that isn't in `datastore/runs/` yet."""
    report = AdmitReport()
    incoming = datastore / INCOMING_DIRECTORY
    runs = datastore / RUNS_DIRECTORY
    if not incoming.is_dir():
        report.problems[str(incoming)] = (
            "There's no incoming directory here. It's where the copy from the bucket "
            "is expected to put this task's runs."
        )
        return report
    runs.mkdir(parents=True, exist_ok=True)

    with open(datastore / LOCK_FILE, "a") as lock:
        # A POSIX lock, rather than flock, because that's the kind an SMB or NFS mount
        # passes on to the server, where another machine's admit would see it.
        try:
            fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            report.problems[str(datastore)] = (
                "another pigsty admit is working on this task. Nothing was admitted. "
                "Run this again once it's finished."
            )
            return report

        admitted = set(_directories_in(runs))
        new = []
        for run_id in _directories_in(incoming):
            if run_id in admitted:
                report.already_admitted += 1
            else:
                new.append(run_id)

        with ThreadPoolExecutor(WORKERS) as pool:
            outcomes = pool.map(
                lambda run_id: _admit_one(task_code, incoming / run_id, runs), new
            )
            for run_id, outcome in zip(new, outcomes, strict=True):
                if outcome.status == "admitted":
                    report.admitted.append(run_id)
                elif outcome.status == "arriving":
                    report.arriving.append(run_id)
                else:
                    report.problems[str(incoming / run_id)] = outcome.why
    return report


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


def _admit_one(task_code: str, source: Path, runs: Path) -> Outcome:
    """Check one run and move it into `runs/`. Run by the workers."""
    try:
        manifest = _read_manifest(task_code, source)
        _check_listing(source, manifest)
        _copy_into_place(source, runs / source.name, manifest)
    except StillArriving as arriving:
        return Outcome("arriving", str(arriving))
    except RunProblem as problem:
        return Outcome("problem", str(problem))
    except (KeyError, TypeError, ValueError) as error:
        # A manifest Pig wrote always has these. One that doesn't has been edited.
        return Outcome(
            "problem",
            "manifest.json is missing something Pig always writes, or has an "
            f"unexpected value in it ({error!r}).",
        )
    except OSError as error:
        return Outcome("problem", f"couldn't admit it: {error}")
    return Outcome("admitted")


def _read_manifest(task_code: str, source: Path) -> dict[str, Any]:
    try:
        text = (source / storage.MANIFEST_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        # The web side uploads the manifest last, but a download can still bring it in
        # before the rest, or not yet at all.
        raise StillArriving("no manifest.json yet") from None
    try:
        manifest = json.loads(text)
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
    if manifest["run_id"] != source.name or manifest["task_code"] != task_code:
        raise RunProblem(
            f"the manifest says this is run {manifest['run_id']} of task "
            f"{manifest['task_code']}, but it's in task {task_code}'s incoming "
            f"directory as {source.name}. One of them has been moved or edited."
        )
    return manifest


def _check_listing(source: Path, manifest: dict[str, Any]) -> None:
    """That every file the manifest lists is here at its full size, and nothing else is.

    A file that's missing or short is still downloading. A file that's too long, or
    that the manifest doesn't list, isn't something a download in progress leaves
    behind.
    """
    listed = {_listed_path(entry["path"]): entry for entry in manifest["files"]}
    # First, so that nothing below follows a symbolic link anywhere in the run.
    present = _files_in(source)
    for relative, entry in listed.items():
        try:
            size = (source / relative).stat().st_size
        except FileNotFoundError:
            raise StillArriving(f"{relative} isn't here yet") from None
        if size < entry["bytes"]:
            raise StillArriving(f"{relative} is still arriving")
        if size > entry["bytes"]:
            raise RunProblem(
                f"{relative} is {size} bytes, and the manifest says "
                f"{entry['bytes']}. Nothing was admitted."
            )
    for relative in present:
        if relative != PurePosixPath(storage.MANIFEST_FILE) and relative not in listed:
            raise RunProblem(
                f"{relative} is here but not in the manifest. Nothing was admitted."
            )


def _listed_path(path: str) -> PurePosixPath:
    """A path from the manifest's `files`, refused if it could reach outside the run.

    The manifest came from the web server, by way of the bucket, and the web server is
    the machine most likely to be compromised. A path like `../../somewhere` would have
    admit write outside the run directory.
    """
    relative = PurePosixPath(path)
    if (
        not path
        or relative.is_absolute()
        or any(part in ("", ".", "..") for part in path.split("/"))
        or "\\" in path
    ):
        raise RunProblem(
            f"the manifest lists {path!r}, which isn't a path inside the run. "
            "Nothing was admitted."
        )
    return relative


def _files_in(run_directory: Path) -> list[PurePosixPath]:
    """Every file in a run directory, relative to it, or RunProblem if anything in it,
    the directory included, is a symbolic link.

    Pig never writes a symbolic link, and one in a downloaded run could point anywhere
    on this machine, so admit would copy whatever it pointed at into `runs/`. Listed
    with `os.scandir`, which says whether each entry is a link, a file, or a directory
    without a separate question per entry.
    """
    if run_directory.is_symlink():
        raise RunProblem(
            "it's a symbolic link, not a directory. Pig never makes one, so nothing "
            "was admitted."
        )
    found = []
    waiting = [run_directory]
    while waiting:
        directory = waiting.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                relative = PurePosixPath(path.relative_to(run_directory).as_posix())
                if entry.is_symlink():
                    raise RunProblem(
                        f"{relative} is a symbolic link. Pig never makes one, so "
                        "nothing was admitted."
                    )
                if entry.is_dir(follow_symlinks=False):
                    waiting.append(path)
                else:
                    found.append(relative)
    return sorted(found)


def _copy_into_place(source: Path, destination: Path, manifest: dict[str, Any]) -> None:
    """Copy a run's files into a scratch directory, checking each hash as it's copied,
    then write `pig-run.json` and rename the scratch directory into place.

    Every file and directory is fsynced before the rename, and `runs/` after, so a run
    in `runs/` is on disk.
    """
    scratch = destination.with_name(f".{destination.name}{SCRATCH_SUFFIX}")
    try:
        # Left by an admit that was interrupted. Ours by its name, never incoming's.
        shutil.rmtree(scratch)
    except FileNotFoundError:
        pass
    scratch.mkdir()
    made = {scratch}
    try:
        for entry in manifest["files"]:
            relative = _listed_path(entry["path"])
            digest = _copy_and_hash(source / relative, scratch / relative, made)
            if digest != entry["sha256"]:
                raise RunProblem(
                    f"{relative} doesn't match the manifest (sha256 {digest}; the "
                    f"manifest says {entry['sha256']}). It's the right size, so this "
                    "isn't a download in progress. Nothing was admitted."
                )

        described = {
            "type": RUN_FILE_TYPE,
            "run_file_version": RUN_FILE_VERSION,
            **{name: manifest[name] for name in RUN_FIELDS},
        }
        with open(scratch / RUN_FILE, "w", encoding="utf-8") as written:
            json.dump(described, written, indent=2, ensure_ascii=False)
            written.write("\n")
            written.flush()
            os.fsync(written.fileno())
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise

    for directory in made:
        storage.fsync_directory(directory)
    scratch.rename(destination)
    storage.fsync_directory(destination.parent)


def _copy_and_hash(source: Path, target: Path, made: set[Path]) -> str:
    """Copy one file and return its sha256. `made` is the directories already created,
    so each is created once rather than once per file."""
    if target.parent not in made:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Each directory between this one and the scratch directory, for its entries.
        directory = target.parent
        while directory not in made:
            made.add(directory)
            directory = directory.parent
    digest = hashlib.sha256()
    with open(source, "rb") as reading, open(target, "wb") as writing:
        while chunk := reading.read(1024 * 1024):
            digest.update(chunk)
            writing.write(chunk)
        writing.flush()
        os.fsync(writing.fileno())
    return digest.hexdigest()
