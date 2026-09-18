"""The command line: everything that isn't a request.

Checking configuration, running the service, and the scheduled work — finishing closed
runs and expiring runs that have been open too long.
"""

from __future__ import annotations

import json
import os
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
def check(*, config: ConfigPath | None = None) -> None:
    """Check the configuration file, and say what each task is set up to do."""
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
    print(f"Expired {len(report.expired)} run(s), finished {len(report.finished)}.")
    for run_id, why in report.failed.items():
        print(f"  couldn't finish {run_id}: {why}", file=sys.stderr)
    if report.failed:
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
        described = " ".join(
            f"{name}={value}" for name, value in run.parameters.items()
        )
        print(
            f"{run.run_id}  {run.task_code:<{task_width}}  run-{run.run_number:04d}  "
            f"{run.api_status:<{status_width}}  {run.phase:<{phase_width}}  "
            f"{counts[run.run_id]:>{count_width}} events  {described}"
        )


@app.command(name="health")
def health_command(*, config: ConfigPath | None = None) -> None:
    """Print the same report as `GET /health`."""
    loaded, connection = _open(config)
    print(json.dumps(health.report(loaded, connection), indent=2))


def main() -> None:
    app()
