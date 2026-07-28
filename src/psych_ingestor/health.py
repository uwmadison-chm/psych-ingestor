"""The health check: is anything wrong right now, and where."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .runs import Pig

# How long a run can sit in `finalizing` before we call it stuck. Filing normally takes
# as long as the next sweep, so this is generous.
STUCK_AFTER = timedelta(hours=1)

STATUSES = ("in_progress", "finalizing", "complete", "abandoned")


def report(pig: Pig, configuration_problem: str | None = None) -> dict[str, Any]:
    """Is anything wrong right now, and where.

    `configuration_problem` is set when the file on disk stopped loading. The service
    keeps running on the last configuration that did load, so this is the only place
    that failure becomes visible.
    """
    checks = {
        "configuration_loads": configuration_problem is None,
        "database_writable": _is_writable(pig.config.database.parent),
        "data_root_writable": _is_writable(pig.config.data_root),
    }
    tasks = {code: _task_report(pig, code) for code in sorted(pig.config.task)}
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


def _task_report(pig: Pig, task_code: str) -> dict[str, Any]:
    task = pig.config.task[task_code]
    counts = {status: 0 for status in STATUSES}
    for row in pig.connection.execute(
        "SELECT status, COUNT(*) AS count FROM runs WHERE task_code = ? GROUP BY status",
        (task_code,),
    ):
        counts[row["status"]] = row["count"]

    cutoff = (datetime.now(UTC) - STUCK_AFTER).isoformat()
    stuck = pig.connection.execute(
        "SELECT COUNT(*) AS count FROM runs WHERE task_code = ? AND status = 'finalizing' "
        "AND finalized_at < ?",
        (task_code, cutoff),
    ).fetchone()["count"]

    unfiled = pig.connection.execute(
        "SELECT COUNT(*) AS count FROM runs WHERE task_code = ? AND filed_at IS NULL "
        "AND status IN ('finalizing', 'abandoned')",
        (task_code,),
    ).fetchone()["count"]

    last_event = pig.connection.execute(
        "SELECT MAX(events.stored_at) AS last FROM events "
        "JOIN runs ON runs.run_id = events.run_id WHERE runs.task_code = ?",
        (task_code,),
    ).fetchone()["last"]

    return {
        "open": task.open,
        "runs": counts,
        "stuck_finalizing": stuck,
        "waiting_to_be_filed": unfiled,
        "last_event_at": last_event,
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
