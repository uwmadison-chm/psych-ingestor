"""The command line: everything that isn't a request.

Checking configuration, running the service, and the scheduled work — finishing closed
runs and expiring runs that have been open too long. Also the secure side's commands,
which read pigsty.toml and never pig.toml or the database: `pig organize`, for now.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Annotated

import cyclopts

from . import db, health, sty
from . import organize as organize_module
from . import runs as runs_module
from . import sweep as sweep_module
from .config import (
    DEFAULT_CONFIG,
    Config,
    ConfigurationError,
    describe_duration,
    describe_size,
    load_config,
)
from .runs import API_STATUSES, Phase

# The help lists commands by where they run. The web side's read pig.toml and the
# database; the secure side's read pigsty.toml and never either. `check` reads both
# kinds of file, so it's listed in both groups.
WEB_SIDE = cyclopts.Group(
    "Web side",
    help="On the web server. These read pig.toml and the database.",
    sort_key=1,
)
SECURE_SIDE = cyclopts.Group(
    "Secure side",
    help="Where the data ends up. These read pigsty.toml, and never pig.toml or the "
    "database.",
    sort_key=2,
)

app = cyclopts.App(
    name="pig",
    help="Psych Ingestor: collect data from online behavioral tasks.",
    # Only --help and --version are left in the default group. Listed last.
    group_commands=cyclopts.Group("Commands", sort_key=3),
)

ConfigPath = Annotated[
    Path,
    cyclopts.Parameter(
        name=["--config", "-c"],
        help="The task definitions file. Defaults to ./local/pig.toml.",
    ),
]


StyConfigPath = Annotated[
    Path,
    cyclopts.Parameter(
        name=["--config", "-c"],
        help="The secure side's configuration file. Defaults to ./local/pigsty.toml.",
    ),
]


def _default_config_path() -> Path:
    """Where a local deployment keeps its configuration.

    Everything a working copy accumulates — the file, the database, the data — lives
    under `local/`, which is the one thing version control ignores.
    """
    return Path(os.environ.get("PIG_CONFIG", DEFAULT_CONFIG))


def _load(path: Path | None) -> Config:
    chosen = path or _default_config_path()
    if not chosen.exists():
        print(f"There's no configuration file at {chosen}.", file=sys.stderr)
        if path is None:
            print(
                "\nTo set up a local one:\n"
                "    mkdir local\n"
                "    cp pig.example.toml local/pig.toml",
                file=sys.stderr,
            )
        raise SystemExit(1)
    try:
        return load_config(chosen)
    except ConfigurationError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error


def _open(path: Path | None) -> tuple[Config, sqlite3.Connection]:
    """The configuration and a connection to its database, for one CLI command."""
    config = _load(path)
    try:
        return config, db.connect(config.database)
    except db.DatabaseProblem as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error


def _load_sty(path: Path | None) -> sty.StyConfig:
    chosen = path or Path(os.environ.get("PIGSTY_CONFIG", sty.DEFAULT_STY_CONFIG))
    if not chosen.exists():
        print(f"There's no configuration file at {chosen}.", file=sys.stderr)
        raise SystemExit(1)
    try:
        return sty.load_sty_config(chosen)
    except ConfigurationError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error


@app.command(group=(WEB_SIDE, SECURE_SIDE))
def check(*, config: ConfigPath | None = None) -> None:
    """Check a configuration file, and say what each task is set up to do.

    Takes either pig.toml or pigsty.toml, and tells them apart by the `kind` each one
    declares.
    """
    chosen = config or _default_config_path()
    try:
        kind = sty.read_kind(chosen) if chosen.exists() else None
    except ConfigurationError:
        kind = None  # _load says what's wrong with it.
    if kind == sty.SECURE_KIND:
        _check_sty(chosen)
        return

    loaded = _load(config)
    print(f"Configuration looks good: {len(loaded.task)} task(s).")
    print(f"  data root: {loaded.data_root}")
    print(f"  database:  {loaded.database}")
    for code, task in sorted(loaded.task.items()):
        state = "open" if task.open else "closed"
        print(f"\n{code} ({state})")
        print(f"  expects:     {', '.join(task.parameters)}")
        print(f"  run key:     {', '.join(task.run_key)}")
        print(
            f"  runs expire: {describe_duration(task.expires_after)} after they start"
        )
        if task.media:
            print(
                f"  media:       yes, parts up to {describe_size(task.max_part_size)}"
            )
    largest = max(
        (task.max_part_size for task in loaded.task.values() if task.media), default=0
    )
    if largest:
        print(
            f"\nAt least one task takes media, so the web server in front of Pig has to "
            f"allow request bodies of {describe_size(largest)} or more. See "
            "docs/deployment.md."
        )


def _check_sty(path: Path) -> None:
    loaded = _load_sty(path)
    print(f"Secure-side configuration looks good: {len(loaded.task)} task(s) listed.")
    print(f"  store:     {loaded.store}")
    print(f"  organized: {loaded.organized}")
    for code, task in sorted(loaded.task.items()):
        print(f"\n{code}")
        print(f"  pattern: {task.pattern or '(the default)'}")
    print(
        "\nA task not listed here is organized with the default pattern: its task "
        "code, its run-key values, then its run number."
    )


@app.command(group=SECURE_SIDE)
def organize(
    store: Path | None = None,
    output: Path | None = None,
    /,
    *,
    config: StyConfigPath | None = None,
    dry_run: bool = False,
    full: bool = False,
) -> None:
    """Copy finished runs into a readable tree, named for their parameters.

    On the secure server, pigsty.toml says where the runs are, where the tree goes, and
    what each task's paths look like. To organize a copied store without one, give the
    store and the output directory instead, and every task gets the default pattern.

    Safe to run again: a run already in the tree is left alone. Nothing in the store is
    ever changed. With --dry-run, prints where each run would go and copies nothing.

    The tree keeps a record of the runs placed in it, so a second organize reads only
    new runs. With --full, every run is checked against the tree, and the record is
    written again.
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
        loaded = _load_sty(config)
        store, output = loaded.store, loaded.organized
        patterns = {
            code: task.pattern
            for code, task in loaded.task.items()
            if task.pattern is not None
        }

    report = organize_module.organize(
        store, output, patterns, dry_run=dry_run, full=full
    )

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


# The first file descriptor systemd passes to a service it started from a socket unit.
SD_LISTEN_FDS_START = 3


def _activated_fd() -> int | None:
    """The listening socket systemd handed us, if it handed us one.

    A systemd socket unit binds and listens on the socket itself and then starts the
    service with that socket already open, saying so in the environment. So a
    socket-activated Pig never opens an address of its own — it's given one. Gunicorn
    does this too, which is why a gunicorn unit file mentions no address anywhere.

    `LISTEN_PID` is part of the protocol because these variables are inherited by child
    processes, and only the process systemd named should believe them.
    """
    if os.environ.get("LISTEN_PID") != str(os.getpid()):
        return None
    count = os.environ.get("LISTEN_FDS", "0")
    if not count.isdigit() or int(count) < 1:
        return None
    return SD_LISTEN_FDS_START


@app.command(group=WEB_SIDE)
def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    config: ConfigPath | None = None,
) -> None:
    """Run the web service.

    Listens on `--host` and `--port`, unless systemd started us from a socket unit, in
    which case it uses the socket systemd already opened and neither one applies. See
    docs/deployment.md.
    """
    import uvicorn

    path = config or _default_config_path()
    _load(path)  # Fail here, with a readable message, rather than inside uvicorn.
    os.environ["PIG_CONFIG"] = str(path)

    given = _activated_fd()
    if given is None:
        uvicorn.run("psych_ingestor.app:app", host=host, port=port, factory=True)
    else:
        uvicorn.run("psych_ingestor.app:app", fd=given, factory=True)


@app.command(group=WEB_SIDE)
def sweep(*, config: ConfigPath | None = None) -> None:
    """Finish closed runs and expire runs that have been open too long.

    This is the scheduled half of Pig. Until it runs, finalized runs sit in `finalizing`
    and their directories stay under `in_progress/`.
    """
    loaded, connection = _open(config)
    report = sweep_module.sweep(loaded, connection)
    # Refusals and failures are counted separately because they ask for different
    # things: a failure is usually one problem behind several runs, and the next sweep
    # finishes them once it's fixed. A refusal is that run's data to account for, and
    # no sweep resolves it. Neither count is printed when it's zero, so a sweep with
    # nothing wrong still reads as one line.
    counts = [
        f"Expired {len(report.expired)} run(s)",
        f"finished {len(report.finished)}",
    ]
    if report.refused:
        counts.append(f"refused {len(report.refused)}")
    if report.failed:
        counts.append(f"failed {len(report.failed)}")
    print(", ".join(counts) + ".")

    for run_id, why in report.refused.items():
        print(f"  refused {run_id}: {why}", file=sys.stderr)
    for run_id, why in report.failed.items():
        print(f"  couldn't finish {run_id}: {why}", file=sys.stderr)
    if report.refused or report.failed:
        raise SystemExit(1)


@app.command(group=WEB_SIDE)
def runs(
    *,
    task: str | None = None,
    status: str | None = None,
    config: ConfigPath | None = None,
) -> None:
    """List runs, most recent first.

    Shows both the status a task sees and Pig's own phase, because the status alone
    doesn't say whether an expired run has been finished yet.
    """
    if status is not None and status not in API_STATUSES:
        print(
            f"{status!r} isn't a run status. Pig uses: {', '.join(API_STATUSES)}.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # The configuration is loaded and checked even though listing runs doesn't read it:
    # a command that silently works against a broken config file would be worse.
    _, connection = _open(config)
    listed = [
        run
        for run in runs_module.recent_first(connection, task_code=task)
        if status is None or run.api_status == status
    ]

    # Column widths: wide enough for the widest value each column can hold.
    task_width = max((len(run.task_code) for run in listed), default=0)
    status_width = max(len(word) for word in API_STATUSES)
    phase_width = max(len(phase) for phase in Phase)
    counts = {
        run.run_id: runs_module.count_stored_events(connection, run.run_id)
        for run in listed
    }
    count_width = max((len(str(count)) for count in counts.values()), default=1)

    for run in listed:
        described = " ".join(
            f"{name}={value}" for name, value in run.parameters.items()
        )
        print(
            f"{run.run_id}  {run.task_code:<{task_width}}  run-{run.run_number:04d}  "
            f"{run.api_status:<{status_width}}  {run.phase:<{phase_width}}  "
            f"{counts[run.run_id]:>{count_width}} events  {described}"
        )


@app.command(name="health", group=WEB_SIDE)
def health_command(*, config: ConfigPath | None = None) -> None:
    """Print the same report as `GET /health`."""
    loaded, connection = _open(config)
    print(json.dumps(health.report(loaded, connection), indent=2))


def main() -> None:
    app()
