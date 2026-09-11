"""The work that happens on a schedule rather than on request.

Filing finished datasets and expiring runs that have been open too long. Run from the
CLI, by a systemd timer in production or by hand on a laptop. Safe to run twice at once,
and safe to run when there's nothing to do.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import db, runs, storage
from .config import Config
from .runs import Disposition


@dataclass
class SweepReport:
    """What one sweep did, so the CLI can print it and a person can watch it work."""

    filed: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


def sweep(config: Config, connection: sqlite3.Connection) -> SweepReport:
    report = SweepReport()
    expire_runs(config, connection, report)
    file_finished_runs(config, connection, report)
    return report


def file_finished_runs(
    config: Config, connection: sqlite3.Connection, report: SweepReport
) -> SweepReport:
    """Sort each waiting dataset, move it where the task says, and mark the run done.

    A run whose filing fails stays where it is and gets reported, rather than quietly
    becoming `done`. There's no retry beyond the next sweep.
    """
    for run in runs.awaiting_sweep(connection):
        task = config.task.get(run.task_code)
        if task is None:
            # Someone deleted the task's entry. Its data stays readable where it is.
            report.failed[run.run_id] = (
                f"task {run.task_code!r} is no longer in the configuration"
            )
            continue

        # Which tree a dataset lands in follows from why the run ended, not from how far
        # along Pig is with it.
        root = (
            config.complete_root
            if run.disposition is Disposition.FINALIZED
            else config.expired_root
        )
        destination = root / task.dataset_path(run.parameters, run.run_number)
        source = storage.in_progress_path(config.in_progress_root, run.run_id)

        try:
            storage.file_dataset(source, destination)
        except OSError as error:
            report.failed[run.run_id] = str(error)
            continue

        # Only the sweep that actually marked it reports it, so two sweeps running at
        # once don't both claim the same run.
        if runs.mark_done(connection, run, destination, db.now()):
            report.filed.append(run.run_id)

    return report


def expire_runs(
    config: Config, connection: sqlite3.Connection, report: SweepReport
) -> SweepReport:
    """Close runs that have been open longer than their task allows.

    The limit counts from when the run started, not from its last event, so a run can't
    stay open forever just because something keeps arriving. A task whose runs have no
    natural end — a game people play as long as they like — sets a long `expires_after`
    and starts a new run when Pig says the old one has expired.

    Only runs still collecting expire. A run already closed is waiting on us, not on the
    participant. Nothing is deleted: the run is marked and its dataset is filed with the
    other expired ones.
    """
    now = db.now()
    for run in runs.collecting(connection):
        task = config.task.get(run.task_code)
        if task is None:
            continue
        if not run.is_past_its_limit(task.expires_after, now):
            continue
        if runs.mark_closed(connection, run, Disposition.EXPIRED, db.now()):
            report.expired.append(run.run_id)

    return report
