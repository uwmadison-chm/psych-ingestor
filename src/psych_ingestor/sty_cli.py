"""The secure side's command line, `pigsty`.

`pig` runs on the web server and reads pig.toml. `pigsty` runs where the data ends up and
reads pigsty.toml, and never pig.toml or the database: by the time it runs, neither may
exist on this machine. Two commands rather than one keeps that rule visible in every
command you type. See issue #33.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Annotated, Any, NoReturn

import cyclopts

from . import admit as admit_module
from . import db, keys, signing, sty
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
    """Every private key for decrypting in private_keys_dir, or none if there isn't
    one. Exits saying what's wrong with a key that can't be used."""
    return [key for key in _every_key(loaded) if isinstance(key, keys.PrivateKey)]


def _signing_keys(loaded: sty.StyConfig) -> list[keys.SigningKey]:
    """Every signing key in private_keys_dir, or none if there isn't one."""
    return [key for key in _every_key(loaded) if isinstance(key, keys.SigningKey)]


def _every_key(loaded: sty.StyConfig) -> list[keys.PrivateKey | keys.SigningKey]:
    if loaded.private_keys_dir is None:
        return []
    try:
        return keys.read_all_keys(loaded.private_keys_dir)
    except ConfigurationError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error


@app.command
def check(*, config: StyConfigPath | None = None) -> None:
    """Check pigsty.toml, and say what each task is set up to do.

    Also checks every private key in private_keys_dir. With pig_url set, asks the web
    server which public key each task encrypts to, and warns about one that no private
    key here matches: data encrypted to it couldn't be decrypted. And it warns about a
    task whose signed_parameters differ from the web server's, since links signed here
    wouldn't start runs there. The web server's pig.toml is checked by `pig check`.
    """
    loaded = _load(config)
    private_keys = _private_keys(loaded)
    signing_keys = _signing_keys(loaded)
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
        if signing_keys:
            print(
                f"Signs with {len(signing_keys)} signing key(s): "
                + ", ".join(key.name for key in signing_keys)
            )
    for code, task in sorted(loaded.task.items()):
        print(f"\n{code}")
        print(f"  datastore: {task.datastore_base}")
        print(f"  organized: {task.organized_root}")
        print(f"  pattern:   {task.pattern}")
        if task.signed_parameters:
            print(f"  signs:     {', '.join(task.signed_parameters)}")
    if loaded.pig_url:
        _check_web_server(loaded, private_keys)


def _check_web_server(
    loaded: sty.StyConfig, private_keys: list[keys.PrivateKey]
) -> None:
    """Warn about any task the web server says encrypts to a public key that no private
    key here matches, or signs other parameters than pigsty.toml says. Only ever a
    warning: an unreachable server isn't this file's problem, and the check is mostly
    about the web server's configuration, not this one."""
    pig_url = loaded.pig_url or ""
    have = {key.public_key: key.name for key in private_keys}
    print()
    for code, task in sorted(loaded.task.items()):
        try:
            settings = _task_settings(pig_url, code)
        except purge_module.Unreachable as error:
            print(f"Warning: didn't check the web server's settings: {error}")
            return
        if settings is None:
            print(f"Warning: the web server at {pig_url} has no task {code!r}.")
            continue
        _check_signed_parameters(code, task, settings)
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


def _check_signed_parameters(
    code: str, task: sty.StyTask, settings: dict[str, Any]
) -> None:
    """Warn if the web server signs other parameters for a task than pigsty.toml says.
    Links signed here would then never start a run there."""
    signing_settings = settings.get("parameter_signing") or {}
    theirs = signing_settings.get("signed_parameters", [])
    if sorted(theirs) == sorted(task.signed_parameters):
        return
    if not theirs:
        print(
            f"Warning: {code}: pigsty.toml signs {', '.join(task.signed_parameters)}, "
            "and the web server doesn't check signatures for this task."
        )
    elif not task.signed_parameters:
        print(
            f"Warning: {code}: the web server checks signatures over "
            f"{', '.join(theirs)}, and pigsty.toml has no signed_parameters for it, "
            "so pigsty sign can't sign links for it."
        )
    else:
        print(
            f"Warning: {code}: the web server checks signatures over "
            f"{', '.join(theirs)}, and pigsty.toml signs "
            f"{', '.join(task.signed_parameters)}. Links signed here won't start runs "
            "there until the two lists are the same."
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


@app.command
def sign(
    task: str,
    *values: str,
    key: str,
    csv_file: Annotated[Path | None, cyclopts.Parameter(name="--csv")] = None,
    column: str = signing.SIGNATURE_PARAMETER,
    url: str | None = None,
    config: StyConfigPath | None = None,
) -> None:
    """Sign a link's parameters, so a task that checks signatures will start a run
    from it.

    Signs exactly the parameters the task's signed_parameters in pigsty.toml lists,
    and prints the _sig value that goes in the link alongside them:

        pigsty sign stroop --key lucida/signing participant_id=10351 session=baseline

    With --csv, signs every row of a CSV file instead, and writes it to standard output
    with a _sig column added. The file needs a column named for each signed
    parameter. Other columns are kept as they are, so a REDCap export can be signed and
    imported again:

        pigsty sign stroop --key lucida/signing --csv roster.csv > signed.csv

    Signatures don't expire. To stop a key's links working, take it out of the task's
    signed_by in pig.toml.

    Parameters
    ----------
    task
        The task code, as pigsty.toml and pig.toml name it.
    values
        Each signed parameter as name=value. Not used with --csv.
    key
        The signing key to sign with, by name, from private_keys_dir.
    csv_file
        A CSV file to sign every row of.
    column
        With --csv, what to call the column the signatures go in. REDCap field names
        have to start with a letter, so for REDCap, name it for the field, like sig,
        and pipe it into the link as _sig=[sig].
    url
        Print a whole link instead: this address, with the parameters and _sig added.
        Not used with --csv.
    """
    loaded = _load(config)
    signed_parameters = _signed_parameters(loaded, task)
    signing_key = _signing_key(loaded, key)
    if csv_file is not None:
        if values or url:
            _fail("With --csv, the values come from the file. Leave out the others.")
        _sign_csv(csv_file, column, task, signed_parameters, signing_key)
        return

    given = _parse_values(list(values))
    missing = [name for name in signed_parameters if name not in given]
    unsigned = [name for name in given if name not in signed_parameters]
    if missing:
        _fail(
            f"{task} signs {', '.join(signed_parameters)}. Give a value for "
            f"{', '.join(missing)} too, like {missing[0]}=10351."
        )
    if unsigned:
        _fail(
            f"{task} doesn't sign {', '.join(unsigned)}; it signs "
            f"{', '.join(signed_parameters)}. Put anything else in the link unsigned."
        )
    signature = signing.sign(signing_key, task, given)
    if url is None:
        print(signature)
        return
    query = urllib.parse.urlencode(
        {**given, signing.SIGNATURE_PARAMETER: signature}, quote_via=urllib.parse.quote
    )
    print(f"{url}{'&' if '?' in url else '?'}{query}")


def _signed_parameters(loaded: sty.StyConfig, code: str) -> list[str]:
    task = loaded.task.get(code)
    if task is None:
        _fail(f"pigsty.toml has no task {code!r}.")
    if not task.signed_parameters:
        _fail(
            f"pigsty.toml doesn't say which of {code}'s parameters to sign. Add "
            "signed_parameters to it, the same list as its signed_parameters in "
            "pig.toml."
        )
    return task.signed_parameters


def _signing_key(loaded: sty.StyConfig, name: str) -> keys.SigningKey:
    signing_keys = _signing_keys(loaded)
    for signing_key in signing_keys:
        if signing_key.name == name:
            return signing_key
    known = ", ".join(signing_key.name for signing_key in signing_keys)
    _fail(
        f"There's no signing key called {name} in private_keys_dir. "
        + (
            f"There is: {known}."
            if known
            else "Make one with: pigsty keys new --signing NAME"
        )
    )


def _parse_values(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        name, equals, text = value.partition("=")
        if not equals or not name:
            _fail(f"{value!r} should be a name and a value, like participant_id=10351.")
        if name in parsed:
            _fail(f"{name} is given twice.")
        parsed[name] = text
    return parsed


def _sign_csv(
    path: Path,
    column: str,
    task: str,
    signed_parameters: list[str],
    signing_key: keys.SigningKey,
) -> None:
    try:
        # utf-8-sig, because Excel starts a "CSV UTF-8" file with a byte-order mark.
        with open(path, newline="", encoding="utf-8-sig") as reading:
            rows = list(csv.DictReader(reading))
            columns = list(rows[0].keys()) if rows else []
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        _fail(f"Can't read {path}: {error}")
    if not rows:
        _fail(f"{path} has no rows to sign.")
    missing = [name for name in signed_parameters if name not in columns]
    if missing:
        _fail(
            f"{path} has no column for {', '.join(missing)}. {task} signs "
            f"{', '.join(signed_parameters)}, and needs a column named for each."
        )
    if column in columns:
        _fail(
            f"{path} already has a {column} column. Take it out to sign the file again."
        )
    # Every row is checked before anything is written, so a bad one never leaves half a
    # file behind. csv.DictReader fills a short row with None, and puts a long row's
    # extra values under None.
    for number, row in enumerate(rows, start=1):
        if None in row or None in row.values():
            _fail(
                f"Row {number} of {path}, after the header, doesn't have the same "
                "number of columns as the header."
            )
    writer = csv.DictWriter(sys.stdout, fieldnames=[*columns, column])
    writer.writeheader()
    for row in rows:
        values = {name: row[name] for name in signed_parameters}
        writer.writerow(
            {
                **row,
                column: signing.sign(signing_key, task, values),
            }
        )


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


keys_app = cyclopts.App(
    name="keys",
    help="Make the private keys that decrypt runs and sign links, and export their "
    "public keys for the web server.",
)
app.command(keys_app)


@keys_app.command
def new(
    name: str, *, signing: bool = False, config: StyConfigPath | None = None
) -> None:
    """Make a new private key in private_keys_dir.

    The key is written to {private_keys_dir}/{name}.key, readable only by you. A name
    can have directories in it, like lucida/2026a. Afterwards, export the public keys,
    copy the file to the web server, and set encrypt_to for the tasks that should use
    this key, or signed_by for a signing key.

    Back the key file up somewhere an administrator can reach, like a 1Password vault.
    Anything encrypted to it that hasn't been admitted yet can't be read without it.
    A signing key can always be replaced by a new one, but every link signed with it
    stops working when it's taken out of signed_by.

    Parameters
    ----------
    name
        What to call the key, like lucida/2026a.
    signing
        Make a key for signing links, for pigsty sign, rather than one for
        decrypting.
    """
    loaded = _load(config)
    if loaded.private_keys_dir is None:
        print(
            "pigsty.toml has no private_keys_dir to put the key in. Add one, like "
            'private_keys_dir = "/etc/pigsty/private_keys".',
            file=sys.stderr,
        )
        raise SystemExit(1)
    make = keys.new_signing_key if signing else keys.new_private_key
    try:
        made = make(loaded.private_keys_dir, name, db.now())
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
    try:
        every_key = keys.read_all_keys(loaded.private_keys_dir)
    except ConfigurationError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
    print(keys.public_keys_text(every_key, loaded.private_keys_dir, db.now()), end="")


def main() -> None:
    app()
