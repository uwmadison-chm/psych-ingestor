"""The work that happens on a schedule rather than on request.

Finishing closed runs and expiring runs that have been open too long. Run from the CLI,
by a systemd timer in production or by hand on a laptop. Safe to run twice at once, and
safe to run when there's nothing to do.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import db, runs, storage
from .config import Config
from .runs import Disposition


@dataclass
class SweepReport:
    """What one sweep did, so the CLI can print it and a person can watch it work.

    Runs that didn't finish are split by what they need from whoever reads the report.
    A `failed` run hit an `OSError`: the disk was full, the destination wasn't mounted.
    A dozen of those are usually one problem, and fixing it lets the next sweep finish
    all of them. A `refused` run is one where what's on disk doesn't match what the
    database says, and every move Pig could make would bury that rather than record it.
    A dozen of those are a dozen separate investigations, and no sweep clears them on
    its own.
    """

    finished: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)


def sweep(config: Config, connection: sqlite3.Connection) -> SweepReport:
    report = SweepReport()
    expire_runs(config, connection, report)
    finish_closed_runs(config, connection, report)
    return report


def finish_closed_runs(
    config: Config, connection: sqlite3.Connection, report: SweepReport
) -> SweepReport:
    """Write each closed run's manifest, move its directory to `done/`, mark it done.

    In that order, on purpose. The manifest is written and fsynced while the directory
    is still under `in_progress/`, so the rename into `done/` is the one moment the run
    appears there, whole. The row is updated last, after the directory has actually
    moved, so the database never claims a run is done before it is.

    This needs nothing from `pig.toml`: the manifest comes from the run's row and the
    files on disk, so a task whose entry was deleted still gets finished.

    A run that can't be finished stays where it is and gets reported, rather than
    quietly becoming `done`. There's no retry beyond the next sweep.
    """
    for run in runs.awaiting_sweep(connection):
        source = storage.run_directory(
            config.in_progress_root, run.task_code, run.run_id
        )
        destination = storage.run_directory(config.done_root, run.task_code, run.run_id)
        now = db.now()

        # The sweep's own crash window: it died after the rename and before the row
        # update. The directory is in `done/`, whole, with its manifest. Finish the
        # bookkeeping and touch nothing. (The row's `done_at` will be a little later
        # than the manifest's; that's the honest record of what happened.)
        if destination.exists() and not source.exists():
            if runs.mark_done(connection, run, now):
                report.finished.append(run.run_id)
            continue

        if destination.exists():
            report.refused[run.run_id] = (
                f"There's a directory for this run in both {config.in_progress_root} "
                f"and {config.done_root}. Pig can't have done that, so it isn't touching "
                "either. Someone needs to look."
            )
            continue

        # Neither tree has the run. The service creates the directory when the run
        # starts, so this means something removed it, and fabricating an empty one here
        # would turn that into a run that looks like it sent nothing.
        if not source.exists():
            report.refused[run.run_id] = (
                f"{source} isn't there, and the run hasn't been finished either. Not "
                "making an empty directory in its place. The run stays where it is."
            )
            continue

        # An events file that's missing is normal for a run that never sent an event.
        # But the receipts say whether that's what happened: Pig writes the line before
        # recording the receipt, so a receipt means the line was on disk. Receipts with
        # no file means the data is gone, and an empty file in its place would make that
        # permanent and silent. See issue #18.
        events = source / storage.EVENTS_FILE
        stored = runs.count_stored_events(connection, run.run_id)
        if not events.exists() and stored > 0:
            report.refused[run.run_id] = (
                f"Pig recorded {stored} event(s) for this run, but {events} isn't "
                "there. Not writing an empty file over it. The run stays where it is."
            )
            continue

        try:
            if not events.exists():
                storage.create_empty_file(events)
            storage.write_manifest(source, storage.manifest_for(run, source, now))
            storage.move_directory(source, destination)
        except OSError as error:
            if destination.exists() and not source.exists():
                # Another sweep finished this run between our check and our rename.
                # It's theirs to report.
                continue
            if destination.exists():
                # A run in both trees again, reached by a race rather than by the check
                # at the top: something appeared in `done/` while we were working. Same
                # situation as that check describes, so the same refusal.
                report.refused[run.run_id] = str(error)
                continue
            report.failed[run.run_id] = str(error)
            continue

        # Only the sweep that actually marked it reports it, so two sweeps running at
        # once don't both claim the same run.
        if runs.mark_done(connection, run, now):
            report.finished.append(run.run_id)

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
    participant. Nothing is deleted: the run is marked, and the same sweep finishes it
    like any other closed run.
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
