"""The health check: is anything wrong right now, and where."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import db, runs
from .config import Config

# How long a run can wait for the sweep before we call it stuck. Finishing a run normally
# takes as long as the next sweep, so this is generous.
STUCK_AFTER = timedelta(hours=1)


def report(
    config: Config,
    connection: sqlite3.Connection,
    configuration_problem: str | None = None,
) -> dict[str, Any]:
    """Is anything wrong right now, and where.

    `configuration_problem` is set when the file on disk stopped loading. The service
    keeps running on the last configuration that did load, so this is the only place
    that failure becomes visible.
    """
    checks = {
        "configuration_loads": configuration_problem is None,
        "database_writable": _is_writable(config.database.parent),
        "data_root_writable": _is_writable(config.data_root),
    }
    tasks = {
        code: _task_report(config, connection, code) for code in sorted(config.task)
    }
    stuck = sum(task["stuck_finalizing"] for task in tasks.values())

    report: dict[str, Any] = {
        "ok": all(checks.values()) and stuck == 0,
        "checks": checks,
        "tasks": tasks,
    }
    if configuration_problem is not None:
        report["configuration_problem"] = configuration_problem
        report["note"] = (
            "The configuration file on disk won't load. The service is still running on "
            "the last one that did. Fix the file and the next request picks it up."
        )
    return report


def _task_report(
    config: Config, connection: sqlite3.Connection, task_code: str
) -> dict[str, Any]:
    task = config.task[task_code]
    last_event = runs.last_stored_at(connection, task_code)
    return {
        "open": task.open,
        "runs": runs.counts_by_api_status(connection, task_code),
        "stuck_finalizing": runs.count_stuck(
            connection, task_code, db.now() - STUCK_AFTER
        ),
        "awaiting_sweep": runs.count_awaiting_sweep(connection, task_code),
        "last_event_at": db.stamp(last_event) if last_event else None,
    }


def _is_writable(directory: Path) -> bool:
    probe = directory / ".pig-write-check"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("")
        probe.unlink()
        return True
    except OSError:
        return False
