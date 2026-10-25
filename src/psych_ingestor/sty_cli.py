"""The secure side's command line, `pigsty`.

`pig` runs on the web server and reads pig.toml. `pigsty` runs where the data ends up and
reads pigsty.toml, and never pig.toml or the database: by the time it runs, neither may
exist on this machine. Two commands rather than one keeps that rule visible in every
command you type. See issue #33.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Annotated, Any

import cyclopts

from . import admit as admit_module
from . import db, keys, sty
from . import organize as organize_module
from . import purge as purge_module
from .config import ConfigurationError

app = cyclopts.App(
    name="pigsty",
    help="Psych Ingestor's secure side: where collected data ends up.",
)

StyConfigPath = Annotated[
    Path,
    cyclopts.Parameter(
        name=["--config", "-c"],
        help="The secure side's configuration file. Defaults to ./local/pigsty.toml.",
    ),
]


def _load(path: Path | None) -> sty.StyConfig:
    chosen = path or Path(os.environ.get("PIGSTY_CONFIG", sty.DEFAULT_STY_CONFIG))
    if not chosen.exists():
        print(f"There's no configuration file at {chosen}.", file=sys.stderr)
        if path is None:
            print(
                "\nTo set up a local one:\n"
                "    mkdir local\n"
                "    cp pigsty.example.toml local/pigsty.toml",
                file=sys.stderr,
            )
        raise SystemExit(1)
    try:
        return sty.load_sty_config(chosen)
    except ConfigurationError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error


# How long pigsty check waits for the web server. It's only asking for settings.
CHECK_TIMEOUT_SECONDS = 10


def _private_keys(loaded: sty.StyConfig) -> list[keys.PrivateKey]:
    """Every private key in private_keys_dir, or none if there isn't one. Exits saying
    what's wrong with a key that can't be used."""
    if loaded.private_keys_dir is None:
        return []
    try:
        return keys.read_private_keys(loaded.private_keys_dir)
    except ConfigurationError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error


@app.command
def check(*, config: StyConfigPath | None = None) -> None:
    """Check pigsty.toml, and say what each task is set up to do.

    Also checks every private key in private_keys_dir. With pig_url set, asks the web
    server which public key each task encrypts to, and warns about one that no private
    key here matches: data encrypted to it couldn't be decrypted. The web server's
    pig.toml is checked by `pig check`.
    """
    loaded = _load(config)
    private_keys = _private_keys(loaded)
    print(f"Configuration looks good: {len(loaded.task)} task(s) listed.")
    if loaded.pig_url:
        print(f"Purges runs from {loaded.pig_url}.")
    else:
        print("No pig_url, so pigsty purge won't run.")
    if loaded.private_keys_dir is None:
        print("No private_keys_dir, so encrypted runs can't be admitted.")
    else:
        print(
            f"Decrypts with {len(private_keys)} private key(s) in "
            f"{loaded.private_keys_dir}: "
            + (", ".join(key.name for key in private_keys) or "none yet")
        )
    for code, task in sorted(loaded.task.items()):
        print(f"\n{code}")
        print(f"  datastore: {task.datastore_base}")
        print(f"  organized: {task.organized_root}")
        print(f"  pattern:   {task.pattern}")
    if loaded.pig_url:
        _check_published_keys(loaded.pig_url, sorted(loaded.task), private_keys)


def _check_published_keys(
    pig_url: str, codes: list[str], private_keys: list[keys.PrivateKey]
) -> None:
    """Warn about any task the web server says encrypts to a public key that no private
    key here matches. Only ever a warning: an unreachable server isn't this file's
    problem, and the check is about the web server's configuration, not this one."""
    have = {key.public_key: key.name for key in private_keys}
    print()
    for code in codes:
        try:
            settings = _task_settings(pig_url, code)
        except purge_module.Unreachable as error:
            print(f"Warning: didn't check which keys the web server uses: {error}")
            return
        if settings is None:
            print(f"Warning: the web server at {pig_url} has no task {code!r}.")
            continue
        encryption = settings.get("encryption")
        if not encryption:
            print(f"{code}: the web server doesn't encrypt it.")
        elif encryption.get("public_key") in have:
            print(
                f"{code}: encrypted to {have[encryption['public_key']]}, which is here."
            )
        else:
            print(
                f"Warning: {code}: the web server encrypts it to a public key that no "
                "private key here matches. Runs encrypted to it couldn't be decrypted. "
                "Check public_keys_file and encrypt_to in pig.toml."
            )


def _task_settings(pig_url: str, code: str) -> dict[str, Any] | None:
    """What `GET /task/{code}` answers, or None for a task the web server doesn't
    have."""
    url = f"{pig_url.rstrip('/')}/task/{code}"
    try:
        with urllib.request.urlopen(url, timeout=CHECK_TIMEOUT_SECONDS) as reply:
            return json.loads(reply.read())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise purge_module.Unreachable(
            f"the web server at {pig_url} answered {error.code}."
        ) from error
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        reason = getattr(error, "reason", error)
        raise purge_module.Unreachable(
            f"couldn't reach Pig at {pig_url}: {reason}."
        ) from error


@app.command
def admit(
    *, config: StyConfigPath | None = None, copy_ciphertext_to: Path | None = None
) -> None:
    """Check downloaded runs against their manifests, and move the ones that are whole
    into place.

    For each task in pigsty.toml, looks at the runs in its datastore's incoming/
    directory that aren't in runs/ yet. A run whose files all match its
    manifest is copied into runs/, and decrypted on the way if it's encrypted. A run
    still downloading is left for next time. A run that's all there but doesn't match,
    or that no private key opens, is reported, and the command exits with an error.
    Nothing in incoming/ is ever changed.

    Parameters
    ----------
    copy_ciphertext_to
        Also keep each encrypted run as it arrived, with its manifest, in
        {this}/{task_code}/{run_id}/. It's the only copy the manifest's hashes describe.
    """
    loaded = _load(config)
    private_keys = _private_keys(loaded)
    problems = False
    for code, task in sorted(loaded.task.items()):
        report = admit_module.admit(
            code,
            task.datastore_base,
            private_keys,
            copy_ciphertext_to.resolve() / code if copy_ciphertext_to else None,
        )
        counts = [
            f"admitted {len(report.admitted)} run(s)",
            f"{report.already_admitted} already admitted",
        ]
        if report.arriving:
            counts.append(f"{len(report.arriving)} still arriving")
        if report.problems:
            counts.append(f"{len(report.problems)} not admitted")
        print(f"{code}: " + ", ".join(counts) + ".")
        for where, why in report.problems.items():
            print(f"  not admitted: {where}: {why}", file=sys.stderr)
        problems = problems or bool(report.problems)
    if problems:
        raise SystemExit(1)


@app.command
def organize(
    *,
    config: StyConfigPath | None = None,
    dry_run: bool = False,
    full: bool = False,
) -> None:
    """Copy each task's admitted runs into a readable tree, named for their parameters.

    For each task in pigsty.toml, places every run in its datastore's runs/ directory
    at the path its organized setting describes.

    Safe to run again: a run already in the tree is left alone, and isn't even read,
    because each tree keeps a record of the runs placed in it. Nothing in runs/ is ever
    changed. With --dry-run, prints where each run would go and copies nothing. With
    --full, checks every run against the tree rather than trusting the record, and
    writes the record again.
    """
    loaded = _load(config)
    problems = False
    verb = "would place" if dry_run else "placed"
    for code, task in sorted(loaded.task.items()):
        root = task.organized_root
        report = organize_module.organize(
            code, task.datastore_base, root, task.pattern, dry_run=dry_run, full=full
        )
        counts = [
            f"{verb} {len(report.placed)} run(s)",
            f"{len(report.already_there)} already there",
        ]
        if report.errors:
            counts.append(f"{len(report.errors)} not placed")
        print(f"{code}: " + ", ".join(counts) + ".")
        if dry_run:
            for placement in sorted(report.placed, key=lambda p: p.destination):
                print(f"  {root / placement.destination}  {placement.run_id}")
        for where, why in report.errors.items():
            print(f"  not placed: {where}: {why}", file=sys.stderr)
        problems = problems or bool(report.errors)
    if problems:
        raise SystemExit(1)


@app.command
def purge(*, config: StyConfigPath | None = None) -> None:
    """Ask the web server to delete each run that's been admitted here.

    For each task in pigsty.toml, looks at the runs that are in both its datastore's
    runs/ and incoming/ directories. Hashes each one's files in incoming/, which are a
    copy of the web server's, and sends the hashes to the Pig at pig_url, which deletes
    its copy only if they all match. A run Pig refuses is reported, and the command
    exits with an error. Once Pig has deleted a run, the copies to and from the bucket
    take it out of incoming/.
    """
    loaded = _load(config)
    if not loaded.pig_url:
        print(
            "pigsty.toml has no pig_url, so there's nowhere to purge runs from. Add the "
            'web server\'s address, like pig_url = "https://pig.example.edu".',
            file=sys.stderr,
        )
        raise SystemExit(1)
    send = purge_module.http_sender(loaded.pig_url)
    problems = False
    for code, task in sorted(loaded.task.items()):
        report = purge_module.purge_runs(code, task.datastore_base, send)
        print(
            f"{code}: purged {len(report.purged)} run(s), "
            f"{report.already_purged} purged before."
            + (f" {len(report.problems)} not purged." if report.problems else "")
        )
        for where, why in report.problems.items():
            print(f"  {where}: {why}", file=sys.stderr)
        problems = problems or bool(report.problems)
    if problems:
        raise SystemExit(1)


keys_app = cyclopts.App(
    name="keys",
    help="Make the private keys encrypted runs are decrypted with, and export their "
    "public keys for the web server.",
)
app.command(keys_app)


@keys_app.command
def new(name: str, *, config: StyConfigPath | None = None) -> None:
    """Make a new private key in private_keys_dir.

    The key is written to {private_keys_dir}/{name}.key, readable only by you. A name
    can have directories in it, like lucida/2026a. Afterwards, export the public keys,
    copy the file to the web server, and set encrypt_to for the tasks that should use
    this key.

    Back the key file up somewhere an administrator can reach, like a 1Password vault.
    Anything encrypted to it that hasn't been admitted yet can't be read without it.

    Parameters
    ----------
    name
        What to call the key, like lucida/2026a.
    """
    loaded = _load(config)
    if loaded.private_keys_dir is None:
        print(
            "pigsty.toml has no private_keys_dir to put the key in. Add one, like "
            'private_keys_dir = "/etc/pigsty/private_keys".',
            file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        made = keys.new_private_key(loaded.private_keys_dir, name, db.now())
    except ValueError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
    except FileExistsError:
        print(
            f"There's already a key called {name}. Pick another name; a key is never "
            "replaced, because data encrypted to it would be unreadable.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(f"Made {made.path}")
    print(f"Its public key is {made.public_key}")
    print("\nNext, export the public keys for the web server: pigsty keys export")


@keys_app.command
def export(*, config: StyConfigPath | None = None) -> None:
    """Print the public keys file for the web server.

    One line per private key in private_keys_dir: its name and its public key. Save it,
    copy it to the web server, and point public_keys_file in pig.toml at it:

        pigsty keys export > public_keys.txt
    """
    loaded = _load(config)
    if loaded.private_keys_dir is None:
        print(
            "pigsty.toml has no private_keys_dir, so there are no keys to export.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(
        keys.public_keys_text(_private_keys(loaded), loaded.private_keys_dir, db.now()),
        end="",
    )


def main() -> None:
    app()
