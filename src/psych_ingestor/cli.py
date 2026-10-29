"""The command line: everything that isn't a request.

Checking configuration, running the service, and the scheduled work — finishing closed
runs and expiring runs that have been open too long. These run on the web server and read
pig.toml. The secure side's commands are a separate command, `pigsty`; see sty_cli.py.
"""

from __future__ import annotations

import json
import os
import pwd
import sqlite3
import sys
from pathlib import Path
from typing import Annotated

import cyclopts

from . import db, health
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

app = cyclopts.App(
    name="pig",
    help="Psych Ingestor: collect data from online behavioral tasks.",
)

ConfigPath = Annotated[
    Path,
    cyclopts.Parameter(
        name=["--config", "-c"],
        help="The task definitions file. Defaults to ./local/pig.toml.",
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


@app.command
def check(*, config: ConfigPath | None = None, fix: bool = False) -> None:
    """Check pig.toml, and say what each task is set up to do.

    Also lists what Pig needs that isn't there yet: data_root, the database, the
    directory it goes in, and the directory touch_after_finalizing goes in. The service
    makes them itself when it first needs them; with --fix, this makes them now, so
    they're there to set owners and permissions on. It never changes anything that's
    already there.

    If touch_after_finalizing is set, checks that whoever runs this can write it. Run it
    as the service's user to check what the service can do: sudo -u pig pig check.

    The secure side's pigsty.toml is checked by `pigsty check`.
    """
    loaded = _load(config)
    directories = [loaded.data_root, loaded.database.parent]
    signal = loaded.touch_after_finalizing
    if signal is not None:
        directories.append(signal.parent)
    missing = [
        directory for directory in dict.fromkeys(directories) if not directory.is_dir()
    ]
    if not loaded.database.exists():
        missing.append(loaded.database)
    if fix:
        for path in missing:
            try:
                if path == loaded.database:
                    # The same call the service makes, so it's the same database.
                    db.connect(path).close()
                else:
                    path.mkdir(parents=True, exist_ok=True)
            except (OSError, sqlite3.Error, db.DatabaseProblem) as error:
                print(f"Couldn't make {path}: {error}", file=sys.stderr)
                raise SystemExit(1) from error
            print(f"Made {path}")
        missing = []
    print(f"Configuration looks good: {len(loaded.task)} task(s).")
    print(f"  data root: {loaded.data_root}")
    print(f"  database:  {loaded.database}")
    for directory in loaded.include:
        print(f"  includes:  {directory}")
    # Checked as whoever is running this, because that's the only answer the operating
    # system gives without guessing. The user is named so the answer isn't mistaken for
    # the service's.
    user = pwd.getpwuid(os.getuid()).pw_name
    unwritable = False
    if signal is not None:
        print(f"  touches:   {signal}, after each finalize")
        if signal.parent.is_dir():
            if signal.exists():
                unwritable = not os.access(signal, os.W_OK)
            else:
                unwritable = not os.access(signal.parent, os.W_OK | os.X_OK)
            if not unwritable:
                print(f"             {user} can write it")
    for code, task in sorted(loaded.task.items()):
        state = "open" if task.open else "closed"
        print(f"\n{code} ({state})")
        if loaded.include:
            print(f"  defined in:  {task.source}")
        print(f"  expects:     {', '.join(task.required_parameters)}")
        text = [
            name
            for name in task.required_parameters
            if name not in task.path_parameters
        ]
        if text:
            print(f"  as text:     {', '.join(text)} (not usable in a path)")
        print(f"  run key:     {', '.join(task.run_key_parameters)}")
        print(
            f"  runs expire: {describe_duration(task.expires_after)} after they start"
        )
        if task.media:
            print(
                f"  media:       yes, parts up to {describe_size(task.max_part_size)}"
            )
        if task.encrypt_to:
            print(f"  encrypts to: {task.encrypt_to}")
        if task.checks_signatures:
            print(f"  signed:      {', '.join(task.signed_parameters)}")
            print(f"  signed by:   {', '.join(task.signed_by)}")
    largest = max(
        (task.max_part_size for task in loaded.task.values() if task.media), default=0
    )
    if largest:
        print(
            f"\nAt least one task takes media, so the web server in front of Pig has to "
            f"allow request bodies of {describe_size(largest)} or more. See "
            "docs/deployment.md."
        )
    if missing:
        print("\nNot there yet:")
        for path in missing:
            print(f"  {path}")
        print("Pig makes them when it needs them, or pig check --fix makes them now.")
    if unwritable:
        print(
            f"\n{user} can't write {signal}, so finalizing a run won't start a sweep. "
            "Runs still finish on the sweep's timer. If the service runs as someone "
            "else, check as that user: sudo -u <user> pig check.",
            file=sys.stderr,
        )
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


@app.command
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


@app.command
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
    if report.purged:
        counts.append(f"finished purging {len(report.purged)}")
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


@app.command
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
        if run.parameters is None:
            described = "(purged)"
        else:
            described = " ".join(
                f"{name}={value}" for name, value in run.parameters.items()
            )
        print(
            f"{run.run_id}  {run.task_code:<{task_width}}  run-{run.run_number:04d}  "
            f"{run.api_status:<{status_width}}  {run.phase:<{phase_width}}  "
            f"{counts[run.run_id]:>{count_width}} events  {described}"
        )


@app.command
def migrate(*, config: ConfigPath | None = None) -> None:
    """Bring a database made by an older version of Pig up to date.

    Stop pig serve first. Pig refuses to use a database from an older version until
    this has run, so nothing is changed without someone deciding to.
    """
    loaded = _load(config)
    try:
        before = db.migrate(loaded.database)
    except db.DatabaseProblem as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
    if before == db.SCHEMA_VERSION:
        print(f"{loaded.database} is already at schema version {before}.")
    else:
        print(
            f"Migrated {loaded.database} from schema version {before} "
            f"to {db.SCHEMA_VERSION}."
        )


@app.command(name="health")
def health_command(*, config: ConfigPath | None = None) -> None:
    """Print the same report as `GET /health`, and exit 1 when it would answer 503."""
    loaded, connection = _open(config)
    report = health.report(loaded, connection)
    print(json.dumps(report, indent=2))
    # The same answer /health gives with its status code, for a scheduled job to notice.
    if not report["ok"]:
        raise SystemExit(1)


def main() -> None:
    app()
