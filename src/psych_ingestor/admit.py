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

What lands in `runs/{run_id}/` is the run's files and `pig-run.json`: what the run is
(task, run number, parameters, media), taken from the manifest without the file hashes.
The manifest itself stays in `incoming/`. It describes the files as they traveled.

A run that isn't encrypted is copied byte for byte. An encrypted one (its manifest names
the public key in `encrypted_to`) is decrypted on its way in, so `runs/`, and everything
after it, only ever holds plaintext: each event's `data` in `events.jsonl` becomes what
the task recorded, and each media part becomes its bytes. A run that no private key
opens isn't admitted. Purge only touches admitted runs, so the web server keeps its copy
until someone sorts it out. See issue #10.

The ciphertext is only what the manifest's hashes describe, so with
`copy_ciphertext_to`, admit also keeps a copy of it there, with the manifest, as
`{copy_ciphertext_to}/{run_id}/`.

Nothing in `incoming/` is changed or deleted. It's a mirror of the bucket, and it empties
itself as runs are purged from the web server and drop out of the bucket.
"""

from __future__ import annotations

import base64
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from . import storage
from .keys import CantDecrypt, Keyring, PrivateKey, is_encrypted_text

INCOMING_DIRECTORY = "incoming"
RUNS_DIRECTORY = "runs"

# What a run is, without its file hashes. Written by admit into `runs/{run_id}/`, and
# copied by organize into the readable tree.
RUN_FILE = "pig-run.json"
RUN_FILE_TYPE = "pig_run"
RUN_FILE_VERSION = 2

# The manifest's fields that describe the run rather than its files.
RUN_FIELDS = (
    "run_id",
    "task_code",
    "run_number",
    "run_key_parameters",
    "path_parameters",
    "signed_parameters",
    "disposition",
    "started_at",
    "closed_at",
    "done_at",
    "parameters",
    "extra_parameters",
    "media",
    "signed_by",
)

# What a run is copied into before it's renamed into `runs/`. Starts with a dot, so a
# listing of `runs/` never mistakes it for an admitted run.
SCRATCH_SUFFIX = ".admitting"
# Where an encrypted run's checked ciphertext is copied, before it's decrypted.
CIPHERTEXT_SUFFIX = ".ciphertext"

# Manifests admit reads. Version 2 is from before encryption, and its runs never are.
# Versions 2 and 3 are from before signing, and before parameters were split into path
# parameters and signed ones; see `_from_before_signing`.
MANIFEST_VERSIONS = (2, 3, storage.MANIFEST_VERSION)

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


def admit(
    task_code: str,
    datastore: Path,
    private_keys: list[PrivateKey] | None = None,
    copy_ciphertext_to: Path | None = None,
) -> AdmitReport:
    """Admit every run in `datastore/incoming/` that isn't in `datastore/runs/` yet.

    `private_keys` are the keys an encrypted run is decrypted with. With
    `copy_ciphertext_to`, each encrypted run's ciphertext is kept there too.
    """
    report = AdmitReport()
    incoming = datastore / INCOMING_DIRECTORY
    runs = datastore / RUNS_DIRECTORY
    if not incoming.is_dir():
        report.problems[str(incoming)] = (
            "There's no incoming directory here. It's where the copy from the bucket "
            "is expected to put this task's runs."
        )
        return report
    storage.make_directories(runs)

    try:
        with storage.hold_lock(datastore / LOCK_FILE):
            admitted = set(storage.directories_in(runs))
            new = []
            for run_id in storage.directories_in(incoming):
                if run_id in admitted:
                    report.already_admitted += 1
                else:
                    new.append(run_id)

            with ThreadPoolExecutor(WORKERS) as pool:
                outcomes = pool.map(
                    lambda run_id: _admit_one(
                        task_code,
                        incoming / run_id,
                        runs,
                        private_keys or [],
                        copy_ciphertext_to,
                    ),
                    new,
                )
                for run_id, outcome in zip(new, outcomes, strict=True):
                    if outcome.status == "admitted":
                        report.admitted.append(run_id)
                    elif outcome.status == "arriving":
                        report.arriving.append(run_id)
                    else:
                        report.problems[str(incoming / run_id)] = outcome.why
    except storage.LockHeld:
        report.problems[str(datastore)] = (
            "another pigsty admit is working on this task. Nothing was admitted. "
            "Run this again once it's finished."
        )
    return report


def _admit_one(
    task_code: str,
    source: Path,
    runs: Path,
    private_keys: list[PrivateKey],
    copy_ciphertext_to: Path | None,
) -> Outcome:
    """Check one run and move it into `runs/`. Run by the workers."""
    try:
        manifest = _read_manifest(task_code, source)
        _check_listing(source, manifest)
        if manifest.get("encrypted_to") is None:
            _copy_into_place(source, runs / source.name, manifest)
        else:
            _decrypt_into_place(
                source, runs / source.name, manifest, private_keys, copy_ciphertext_to
            )
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
    if manifest.get("manifest_version") not in MANIFEST_VERSIONS:
        raise RunProblem(
            f"manifest.json is version {manifest.get('manifest_version')!r}, and this "
            f"version of Pig reads versions {MANIFEST_VERSIONS[0]} to "
            f"{MANIFEST_VERSIONS[-1]}."
        )
    if manifest["run_id"] != source.name or manifest["task_code"] != task_code:
        raise RunProblem(
            f"the manifest says this is run {manifest['run_id']} of task "
            f"{manifest['task_code']}, but it's in task {task_code}'s incoming "
            f"directory as {source.name}. One of them has been moved or edited."
        )
    if manifest["manifest_version"] < 4:
        return _from_before_signing(manifest)
    return manifest


def _from_before_signing(manifest: dict[str, Any]) -> dict[str, Any]:
    """An older manifest in today's shape. Before signing, the run key was `run_key`,
    every parameter a task declared was a path parameter, and nothing was signed."""
    return {
        **manifest,
        "run_key_parameters": manifest["run_key"],
        "path_parameters": list(manifest["parameters"]),
        "signed_parameters": [],
        "signed_by": None,
    }


def _check_listing(source: Path, manifest: dict[str, Any]) -> None:
    """That every file the manifest lists is here at its full size, and nothing else is.

    A file that's missing or short is still downloading. A file that's too long, or
    that the manifest doesn't list, isn't something a download in progress leaves
    behind.
    """
    listed = {_listed_path(entry["path"]): entry for entry in manifest["files"]}
    # First, so that nothing below follows a symbolic link anywhere in the run.
    try:
        present = storage.files_in(source)
    except storage.SymbolicLink as link:
        raise RunProblem(
            f"{link}. Pig never makes one, so nothing was admitted."
        ) from None
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


def _copy_into_place(source: Path, destination: Path, manifest: dict[str, Any]) -> None:
    """Copy a run's files into a scratch directory, checking each hash as it's copied,
    then write `pig-run.json` and rename the scratch directory into place.

    Every file and directory is fsynced before the rename, and `runs/` after, so a run
    in `runs/` is on disk.
    """
    scratch = storage.fresh_scratch(destination, SCRATCH_SUFFIX)
    try:
        made = _copy_checked(source, scratch, manifest)
        _write_run_file(scratch, manifest, decrypted_with=[])
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    storage.rename_into_place(scratch, destination, made)


def _decrypt_into_place(
    source: Path,
    destination: Path,
    manifest: dict[str, Any],
    private_keys: list[PrivateKey],
    copy_ciphertext_to: Path | None,
) -> None:
    """Admit an encrypted run: copy its ciphertext aside, checking each hash, then
    decrypt that copy into a scratch directory and rename it into place.

    Decrypting from the checked copy, rather than from `incoming/`, means what's
    decrypted is exactly what was checked, even if the copy from the bucket changes a
    file in between. With `copy_ciphertext_to`, the checked copy is kept there, with the
    manifest; otherwise it's deleted once the run is admitted.
    """
    if not private_keys:
        raise RunProblem(
            f"it's encrypted, to {manifest['encrypted_to']}, and pigsty.toml has no "
            "private_keys_dir to decrypt it with. Nothing was admitted."
        )
    kept = copy_ciphertext_to / destination.name if copy_ciphertext_to else None
    ciphertext = storage.fresh_scratch(kept or destination, CIPHERTEXT_SUFFIX)
    scratch = None
    try:
        ciphertext_made = _copy_checked(source, ciphertext, manifest)
        scratch = storage.fresh_scratch(destination, SCRATCH_SUFFIX)
        made = {scratch}
        keyring = Keyring(private_keys)
        for entry in manifest["files"]:
            relative = _listed_path(entry["path"])
            storage.make_parent(scratch / relative, made)
            _decrypt_file(ciphertext / relative, scratch / relative, relative, keyring)
        _write_run_file(scratch, manifest, decrypted_with=sorted(keyring.used))

        if kept is not None:
            storage.copy_file(
                source / storage.MANIFEST_FILE,
                ciphertext / storage.MANIFEST_FILE,
                ciphertext_made,
            )
            if kept.exists():
                # Kept by an admit that was interrupted before the run itself was
                # renamed into place. The same run ID is the same ciphertext.
                shutil.rmtree(ciphertext)
            else:
                storage.rename_into_place(ciphertext, kept, ciphertext_made)
    except BaseException:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(ciphertext, ignore_errors=True)
        raise
    storage.rename_into_place(scratch, destination, made)
    shutil.rmtree(ciphertext, ignore_errors=True)


def _decrypt_file(
    source: Path, target: Path, relative: PurePosixPath, keyring: Keyring
) -> None:
    """Decrypt one of a run's files: `events.jsonl` an event at a time, a media part
    whole. The plaintext is fsynced before this returns."""
    if relative == PurePosixPath(storage.EVENTS_FILE):
        with (
            open(source, encoding="utf-8") as reading,
            open(target, "w", encoding="utf-8") as writing,
        ):
            writing.writelines(_decrypt_line(line, keyring) for line in reading)
            storage.flush_and_fsync(writing)
        return
    if relative.parts[0] == storage.MEDIA_DIRECTORY and relative.suffix == (
        storage.PART_SUFFIX
    ):
        try:
            plaintext = keyring.decrypt(source.read_bytes())
        except CantDecrypt:
            raise RunProblem(
                f"no private key in private_keys_dir opens {relative}, or it's been "
                "damaged. Nothing was admitted, so the web server's copy stays put."
            ) from None
        with open(target, "wb") as writing:
            writing.write(plaintext)
            storage.flush_and_fsync(writing)
        return
    raise RunProblem(
        f"{relative} isn't a file Pig writes, so there's no telling how to decrypt it. "
        "Nothing was admitted."
    )


def _decrypt_line(line: str, keyring: Keyring) -> str:
    """One line of `events.jsonl`, with its `data` decrypted.

    A line that isn't JSON is one an interrupted append cut short (see
    `storage.read_lines`). It's kept as it is, the way an unencrypted run keeps it,
    and whatever reads the file skips it.
    """
    try:
        stored = json.loads(line)
    except json.JSONDecodeError:
        return line if line.endswith("\n") else line + "\n"
    event_id = stored.get("event_id")
    if not is_encrypted_text(stored.get("data")):
        raise RunProblem(
            f"event {event_id!r} isn't encrypted, and the run is. Pig refuses those, so "
            "this file has been changed since. Nothing was admitted."
        )
    try:
        plaintext = keyring.decrypt(base64.b64decode(stored["data"]))
    except CantDecrypt:
        raise RunProblem(
            f"no private key in private_keys_dir opens event {event_id!r}, or it's "
            "been damaged. Nothing was admitted, so the web server's copy stays put."
        ) from None
    try:
        data = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RunProblem(
            f"event {event_id!r} decrypts to something that isn't JSON. The browser "
            "encrypted something other than the event. Nothing was admitted."
        ) from None
    return storage.canonical({**stored, "data": data}) + "\n"


def _copy_checked(source: Path, scratch: Path, manifest: dict[str, Any]) -> set[Path]:
    """Copy every file the manifest lists into `scratch`, checking each hash as it's
    copied. Returns the directories made, for `storage.rename_into_place` to fsync."""
    made = {scratch}
    for entry in manifest["files"]:
        relative = _listed_path(entry["path"])
        digest = storage.copy_file(source / relative, scratch / relative, made)
        if digest != entry["sha256"]:
            raise RunProblem(
                f"{relative} doesn't match the manifest (sha256 {digest}; the "
                f"manifest says {entry['sha256']}). It's the right size, so this "
                "isn't a download in progress. Nothing was admitted."
            )
    return made


def _write_run_file(
    scratch: Path, manifest: dict[str, Any], decrypted_with: list[str]
) -> None:
    described = {
        "type": RUN_FILE_TYPE,
        "run_file_version": RUN_FILE_VERSION,
        **{name: manifest[name] for name in RUN_FIELDS},
        # Which public key the task encrypted to, and which private keys opened it.
        # Normally one and the same key; they differ only if the browser had an old copy
        # of the task's settings. Null and empty for a run that isn't encrypted.
        "encrypted_to": manifest.get("encrypted_to"),
        "decrypted_with": decrypted_with,
    }
    with open(scratch / RUN_FILE, "w", encoding="utf-8") as written:
        json.dump(described, written, indent=2, ensure_ascii=False)
        written.write("\n")
        storage.flush_and_fsync(written)
