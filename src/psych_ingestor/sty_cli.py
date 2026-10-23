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
    print(f"  store:     {loaded.store}")
    print(f"  organized: {loaded.organized}")
    for code, task in sorted(loaded.task.items()):
        print(f"\n{code}")
        print(f"  datastore: {task.datastore_base or '(none; not admitted)'}")
        print(f"  pattern:   {task.pattern or '(the default)'}")
    print(
        "\nA task not listed here is organized with the default pattern: its task "
        "code, its run-key values, then its run number."
    )


@app.command
def admit(*, config: StyConfigPath | None = None) -> None:
    """Check downloaded runs against their manifests, and move the ones that are whole
    into place.

    For each task with a datastore_base in pigsty.toml, looks at the runs in its
    incoming/ directory that aren't in runs/ yet. A run whose files all match its
    manifest is copied into runs/. A run still downloading is left for next time. A run
    that's all there but doesn't match is reported, and the command exits with an error.
    Nothing in incoming/ is ever changed.
    """
    loaded = _load(config)
    problems = False
    for code, task in sorted(loaded.task.items()):
        if task.datastore_base is None:
            continue
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
    store: Path | None = None,
    output: Path | None = None,
    /,
    *,
    config: StyConfigPath | None = None,
    dry_run: bool = False,
) -> None:
    """Copy finished runs into a readable tree, named for their parameters.

    pigsty.toml says where the runs are, where the tree goes, and what each task's paths
    look like. To organize a copied store without one, give the store and the output
    directory instead, and every task gets the default pattern.

    Safe to run again: a run already in the tree is left alone. Nothing in the store is
    ever changed. With --dry-run, prints where each run would go and copies nothing.
    """
    if (store is None) != (output is None):
        print("Give both a store and an output directory, or neither.", file=sys.stderr)
        raise SystemExit(1)
    if store is not None and output is not None:
        if config is not None:
            print(
                "Give either a configuration file or a store and output directory, "
                "not both. To try out a pattern, put it in a small pigsty.toml along "
                "with the store and output directory.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        patterns: dict[str, str] = {}
    else:
        loaded = _load(config)
        store, output = loaded.store, loaded.organized
        patterns = {
            code: task.pattern
            for code, task in loaded.task.items()
            if task.pattern is not None
        }

    report = organize_module.organize(store, output, patterns, dry_run=dry_run)

    verb = "Would place" if dry_run else "Placed"
    counts = [
        f"{verb} {len(report.placed)} run(s)",
        f"{len(report.already_there)} already there",
    ]
    if report.errors:
        counts.append(f"{len(report.errors)} not placed")
    if report.warnings:
        counts.append(f"{len(report.warnings)} warning(s)")
    print(", ".join(counts) + ".")
    if dry_run:
        for placement in sorted(report.placed, key=lambda p: p.destination):
            print(f"  {placement.destination}  {placement.run_id}")

    for where, why in report.errors.items():
        print(f"  not placed: {where}: {why}", file=sys.stderr)
    for warning in report.warnings:
        print(f"  warning: {warning}", file=sys.stderr)
    # A warning is a file that doesn't match its manifest, which someone has to look
    # at even though the run was placed, so it fails a scheduled job too.
    if report.errors or report.warnings:
        raise SystemExit(1)


def main() -> None:
    app()
