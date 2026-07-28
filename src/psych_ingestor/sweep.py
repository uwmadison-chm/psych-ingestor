"""The work that happens on a schedule rather than on request.

Filing finished datasets and reaping runs nobody came back to. Run from the CLI, by a
systemd timer in production or by hand on a laptop. Safe to run twice at once, and safe
to run when there's nothing to do.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import db, storage
from .runs import Pig


@dataclass
class SweepReport:
    """What one sweep did, so the CLI can print it and a person can watch it work."""

    filed: list[str] = field(default_factory=list)
    abandoned: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


def sweep(pig: Pig) -> SweepReport:
    report = SweepReport()
    reap_abandoned_runs(pig, report)
    file_finished_runs(pig, report)
    return report


def file_finished_runs(pig: Pig, report: SweepReport) -> SweepReport:
    """Sort each waiting dataset, move it where the task says, and mark the run done.

    A run whose filing fails stays where it is and gets reported, rather than quietly
    becoming `complete`. There's no retry beyond the next sweep.
    """
    waiting = pig.connection.execute(
        "SELECT * FROM runs WHERE filed_at IS NULL AND status IN ('finalizing', 'abandoned') "
        "ORDER BY finalized_at"
    ).fetchall()

    for run in waiting:
        task = pig.config.task.get(run["task_code"])
        if task is None:
            # Someone deleted the task's entry. Its data stays readable where it is.
            report.failed[run["run_id"]] = (
                f"task {run['task_code']!r} is no longer in the configuration"
            )
            continue

        root = (
            pig.config.complete_root
            if run["status"] == "finalizing"
            else pig.config.abandoned_root
        )
        parameters = json.loads(run["parameters"])
        destination = root / task.dataset_path(parameters, run["run_number"])
        source = storage.in_progress_path(pig.config.in_progress_root, run["run_id"])

        try:
            storage.file_dataset(source, destination)
        except OSError as error:
            report.failed[run["run_id"]] = str(error)
            continue

        finished = "complete" if run["status"] == "finalizing" else "abandoned"
        pig.connection.execute(
            "UPDATE runs SET status = ?, filed_at = ?, dataset_path = ? WHERE run_id = ?",
            (finished, db.now(), str(destination), run["run_id"]),
        )
        report.filed.append(run["run_id"])

    return report


def reopen_for_finalizing(pig: Pig, run_id: str) -> None:
    """Finalize a run by hand — one that was abandoned but turned out fine.

    If its dataset has already been filed with the abandoned ones, bring the file back so
    the next sweep has something to file. Raises ValueError, saying why, if it can't.
    """
    run = pig.connection.execute(
        "SELECT * FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if run is None:
        raise ValueError(f"There's no run with the ID {run_id!r}.")
    if run["status"] not in ("in_progress", "abandoned"):
        raise ValueError(
            f"That run is {run['status']}, so there's nothing to finalize."
        )

    in_progress = storage.in_progress_path(pig.config.in_progress_root, run_id)
    if run["filed_at"] and not in_progress.exists():
        filed = Path(run["dataset_path"] or "")
        if not filed.exists():
            raise ValueError(
                f"That run's dataset isn't where the database says it is ({filed}), so "
                "finalizing it would file an empty one. Find the file first."
            )
        in_progress.parent.mkdir(parents=True, exist_ok=True)
        filed.replace(in_progress)

    pig.connection.execute(
        "UPDATE runs SET status = 'finalizing', finalized_at = COALESCE(finalized_at, ?), "
        "filed_at = NULL, dataset_path = NULL WHERE run_id = ?",
        (db.now(), run_id),
    )


def reap_abandoned_runs(pig: Pig, report: SweepReport) -> SweepReport:
    """Give up on runs nobody has sent anything to in a while.

    Only `in_progress` runs are ever abandoned. A run sitting in `finalizing` is waiting
    on us, not on the participant. Nothing is deleted: the run is marked and its dataset
    is filed with the other abandoned ones, so a run that turned out fine can still be
    finalized by hand.
    """
    now = datetime.now(UTC)
    runs = pig.connection.execute(
        "SELECT runs.run_id, runs.task_code, runs.started_at, "
        "  (SELECT MAX(stored_at) FROM events WHERE events.run_id = runs.run_id) AS last_event "
        "FROM runs WHERE status = 'in_progress'"
    ).fetchall()

    for run in runs:
        task = pig.config.task.get(run["task_code"])
        if task is None:
            continue
        last_heard_from = datetime.fromisoformat(run["last_event"] or run["started_at"])
        if now - last_heard_from < timedelta(seconds=task.abandon_after):
            continue
        pig.connection.execute(
            "UPDATE runs SET status = 'abandoned' WHERE run_id = ? AND status = 'in_progress'",
            (run["run_id"],),
        )
        report.abandoned.append(run["run_id"])

    return report
