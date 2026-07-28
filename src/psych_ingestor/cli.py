"""The command line: everything that isn't a request.

Checking configuration, running the service, and the scheduled work — filing finished
datasets and reaping runs that were never finalized.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Annotated

import cyclopts

from . import db, health
from . import sweep as sweep_module
from .config import (
    DEFAULT_CONFIG,
    Config,
    ConfigurationError,
    describe_duration,
    load_config,
)
from .runs import Pig

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


def _open(config: Config) -> Pig:
    return Pig(config, db.connect(config.database))


@app.command
def check(*, config: ConfigPath | None = None) -> None:
    """Check the configuration file, and say what each task is set up to do."""
    loaded = _load(config)
    print(f"Configuration looks good: {len(loaded.task)} task(s).")
    print(f"  data root: {loaded.data_root}")
    print(f"  database:  {loaded.database}")
    for code, task in sorted(loaded.task.items()):
        state = "open" if task.open else "closed"
        example = task.dataset_path(
            {name: f"<{name}>" for name in task.parameters}, run_number=1
        )
        print(f"\n{code} ({state})")
        print(f"  expects:     {', '.join(task.parameters)}")
        print(f"  run key:     {', '.join(task.run_key)}")
        print(f"  data lands:  {loaded.complete_root / example}")
        print(
            "  gives up on an unfinished run after "
            f"{describe_duration(task.abandon_after)}"
        )


@app.command
def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    config: ConfigPath | None = None,
) -> None:
    """Run the web service."""
    import uvicorn

    path = config or _default_config_path()
    _load(path)  # Fail here, with a readable message, rather than inside uvicorn.
    os.environ["PIG_CONFIG"] = str(path)
    uvicorn.run("psych_ingestor.app:app", host=host, port=port, factory=True)


@app.command
def sweep(*, config: ConfigPath | None = None) -> None:
    """File finished datasets and give up on runs nobody came back to.

    This is the scheduled half of Pig. Until it runs, finalized runs sit in `finalizing`
    and their data stays in the in-progress directory.
    """
    pig = _open(_load(config))
    report = sweep_module.sweep(pig)
    print(f"Abandoned {len(report.abandoned)} run(s), filed {len(report.filed)}.")
    for run_id, why in report.failed.items():
        print(f"  couldn't file {run_id}: {why}", file=sys.stderr)
    if report.failed:
        raise SystemExit(1)


@app.command
def runs(
    *,
    task: str | None = None,
    status: str | None = None,
    config: ConfigPath | None = None,
) -> None:
    """List runs, most recent first."""
    pig = _open(_load(config))
    query = "SELECT * FROM runs"
    conditions, values = [], []
    if task:
        conditions.append("task_code = ?")
        values.append(task)
    if status:
        conditions.append("status = ?")
        values.append(status)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY started_at DESC"

    for run in pig.connection.execute(query, values):
        parameters = json.loads(run["parameters"])
        described = " ".join(f"{name}={value}" for name, value in parameters.items())
        count = pig.connection.execute(
            "SELECT COUNT(*) AS count FROM events WHERE run_id = ?", (run["run_id"],)
        ).fetchone()["count"]
        print(
            f"{run['run_id']}  {run['task_code']:<12} run-{run['run_number']:04d}  "
            f"{run['status']:<12} {count:>5} events  {described}"
        )


@app.command
def finalize(run_id: str, *, config: ConfigPath | None = None) -> None:
    """Finalize a run by hand — for one that was abandoned but turned out fine."""
    pig = _open(_load(config))
    try:
        sweep_module.reopen_for_finalizing(pig, run_id)
    except ValueError as why:
        print(why, file=sys.stderr)
        raise SystemExit(1) from why
    print(f"{run_id} is finalizing. Run `pig sweep` to file its data.")


@app.command(name="health")
def health_command(*, config: ConfigPath | None = None) -> None:
    """Print the same report as `GET /health`."""
    pig = _open(_load(config))
    print(json.dumps(health.report(pig), indent=2))


def main() -> None:
    app()
