"""The work that happens on a schedule rather than on request.

Filing finished datasets and expiring runs that have been open too long. Run from the
CLI, by a systemd timer in production or by hand on a laptop. Safe to run twice at once,
and safe to run when there's nothing to do.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import db, storage
from .runs import Pig


@dataclass
class SweepReport:
    """What one sweep did, so the CLI can print it and a person can watch it work."""

    filed: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


def sweep(pig: Pig) -> SweepReport:
    report = SweepReport()
    expire_runs(pig, report)
    file_finished_runs(pig, report)
    return report


def file_finished_runs(pig: Pig, report: SweepReport) -> SweepReport:
    """Sort each waiting dataset, move it where the task says, and mark the run done.

    A run whose filing fails stays where it is and gets reported, rather than quietly
    becoming `complete`. There's no retry beyond the next sweep.
    """
    waiting = pig.connection.execute(
        "SELECT * FROM runs WHERE filed_at IS NULL AND status IN ('finalizing', 'expired') "
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
            else pig.config.expired_root
        )
        parameters = json.loads(run["parameters"])
        destination = root / task.dataset_path(parameters, run["run_number"])
        source = storage.in_progress_path(pig.config.in_progress_root, run["run_id"])

        try:
            storage.file_dataset(source, destination)
        except OSError as error:
            report.failed[run["run_id"]] = str(error)
            continue

        finished = "complete" if run["status"] == "finalizing" else "expired"
        pig.connection.execute(
            "UPDATE runs SET status = ?, filed_at = ?, dataset_path = ? WHERE run_id = ?",
            (finished, db.now(), str(destination), run["run_id"]),
        )
        report.filed.append(run["run_id"])

    return report


def expire_runs(pig: Pig, report: SweepReport) -> SweepReport:
    """Close runs that have been open longer than their task allows.

    The limit counts from when the run started, not from its last event, so a run can't
    stay open forever just because something keeps arriving. A task whose runs have no
    natural end — a game people play as long as they like — sets a long `expires_after`
    and starts a new run when Pig says the old one has expired.

    Only `in_progress` runs expire. A run sitting in `finalizing` is waiting on us, not
    on the participant. Nothing is deleted: the run is marked and its dataset is filed
    with the other expired ones.
    """
    now = datetime.now(UTC)
    runs = pig.connection.execute(
        "SELECT run_id, task_code, started_at FROM runs WHERE status = 'in_progress'"
    ).fetchall()

    for run in runs:
        task = pig.config.task.get(run["task_code"])
        if task is None:
            continue
        started = datetime.fromisoformat(run["started_at"])
        if now - started < timedelta(seconds=task.expires_after):
            continue
        pig.connection.execute(
            "UPDATE runs SET status = 'expired' WHERE run_id = ? AND status = 'in_progress'",
            (run["run_id"],),
        )
        report.expired.append(run["run_id"])

    return report
