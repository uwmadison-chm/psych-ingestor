"""The secure side's command line, `pigsty`.

`pig` runs on the web server and reads pig.toml. `pigsty` runs where the data ends up and
reads pigsty.toml, and never pig.toml or the database: by the time it runs, neither may
exist on this machine. Two commands rather than one keeps that rule visible in every
command you type. See issue #33.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

import cyclopts

from . import admit as admit_module
from . import organize as organize_module
from . import purge as purge_module
from . import sty
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


@app.command
def check(*, config: StyConfigPath | None = None) -> None:
    """Check pigsty.toml, and say what each task is set up to do.

    The web server's pig.toml is checked by `pig check`.
    """
    loaded = _load(config)
    print(f"Configuration looks good: {len(loaded.task)} task(s) listed.")
    if loaded.pig_url:
        print(f"Purges runs from {loaded.pig_url}.")
    else:
        print("No pig_url, so pigsty purge won't run.")
    for code, task in sorted(loaded.task.items()):
        print(f"\n{code}")
        print(f"  datastore: {task.datastore_base}")
        print(f"  organized: {task.organized_root}")
        print(f"  pattern:   {task.pattern}")


@app.command
def admit(*, config: StyConfigPath | None = None) -> None:
    """Check downloaded runs against their manifests, and move the ones that are whole
    into place.

    For each task in pigsty.toml, looks at the runs in its datastore's incoming/
    directory that aren't in runs/ yet. A run whose files all match its
    manifest is copied into runs/. A run still downloading is left for next time. A run
    that's all there but doesn't match is reported, and the command exits with an error.
    Nothing in incoming/ is ever changed.
    """
    loaded = _load(config)
    problems = False
    for code, task in sorted(loaded.task.items()):
        report = admit_module.admit(code, task.datastore_base)
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


def main() -> None:
    app()
