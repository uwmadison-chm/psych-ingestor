"""Starting runs, storing events, finalizing. Everything the web service actually does.

Kept free of FastAPI so it reads as ordinary Python, and so the CLI can use the same
code the service does.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import db, storage
from .config import SAFE_VALUE_EXPLANATION, Config, TaskDefinition, is_safe_value

MAX_RUN_NUMBER = 9999
MAX_EVENT_ID_LENGTH = 256

# Runs whose status isn't one of these aren't taking events. The code tells the task
# whether waiting could help: 409 means the run finished, 423 means we gave up on it.
NOT_ACCEPTING_CODES = {
    "finalizing": 409,
    "complete": 409,
    "abandoned": 423,
}


class RequestProblem(Exception):
    """Something about the request means we can't do it. Carries what to tell the task."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass
class StoreResult:
    """What came back from a request that sent events."""

    status_code: int
    status: str
    stored: list[str]
    errors: dict[str, dict[str, Any]] = field(default_factory=dict)

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"status": self.status, "stored": self.stored}
        if self.errors:
            body["errors"] = self.errors
        return body


def _problem(message: str, can_retry: bool) -> dict[str, Any]:
    return {"message": message, "can_retry": can_retry}


class Pig:
    """The service's work, over one configuration and one database connection."""

    def __init__(self, config: Config, connection: sqlite3.Connection):
        self.config = config
        self.connection = connection

    # ------------------------------------------------------------------ tasks

    def task(self, task_code: str) -> TaskDefinition:
        definition = self.config.task.get(task_code)
        if definition is None:
            raise RequestProblem(404, f"There's no task called {task_code!r}.")
        return definition

    # ------------------------------------------------------------- starting

    def start_run(self, task_code: str, submitted: dict[str, Any]) -> dict[str, Any]:
        task = self.task(task_code)
        if not task.open:
            raise RequestProblem(
                409, f"The task {task_code!r} isn't accepting new runs right now."
            )

        parameters, extra = self._check_parameters(task, submitted)
        run_key = "\x1f".join(parameters[name].lower() for name in task.run_key)
        run_id = str(uuid.uuid4())
        run_number = self._insert_run(task, run_id, run_key, parameters, extra)
        return {"run_id": run_id, "run_number": run_number}

    def _check_parameters(
        self, task: TaskDefinition, submitted: dict[str, Any]
    ) -> tuple[dict[str, str], dict[str, Any]]:
        """Split what arrived into the parameters this task uses and everything else.

        Extra parameters are recorded and otherwise ignored — a `utm_source` or a
        leftover `debug=1` never stops a run from starting.
        """
        if not isinstance(submitted, dict):
            raise RequestProblem(
                422,
                "Expected a JSON object of link parameters, like "
                '{"participant_id": "10351"}.',
            )

        parameters: dict[str, str] = {}
        for name in task.parameters:
            if name not in submitted:
                raise RequestProblem(
                    422,
                    f"This task needs {name!r}, and it wasn't in the request. It expects "
                    f"{task.parameters}.",
                )
            value = submitted[name]
            # A JSON number is what you get from `participant_id: 10351` in JavaScript,
            # which is common enough to accept. Nothing about it is ambiguous.
            if isinstance(value, int) and not isinstance(value, bool):
                value = str(value)
            if not isinstance(value, str):
                raise RequestProblem(
                    422,
                    f"The value for {name!r} has to be text; got {type(value).__name__}.",
                )
            if not is_safe_value(value):
                raise RequestProblem(
                    422,
                    f"{value!r} can't be used for {name!r}, because it becomes part of a "
                    f"file name. Allowed: {SAFE_VALUE_EXPLANATION}.",
                )
            parameters[name] = value

        extra = {
            name: value for name, value in submitted.items() if name not in parameters
        }
        return parameters, extra

    def _insert_run(
        self,
        task: TaskDefinition,
        run_id: str,
        run_key: str,
        parameters: dict[str, str],
        extra: dict[str, Any],
    ) -> int:
        """Give the run the next number for its key, and write it down.

        Two participants starting at the same instant can pick the same number; the
        unique constraint catches that and we try again.
        """
        for _ in range(10):
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.connection.execute(
                    "SELECT MAX(run_number) AS highest FROM runs "
                    "WHERE task_code = ? AND run_key = ?",
                    (task.code, run_key),
                ).fetchone()
                run_number = (row["highest"] or 0) + 1
                if run_number > MAX_RUN_NUMBER:
                    raise RequestProblem(
                        409,
                        f"This participant already has {MAX_RUN_NUMBER} runs of "
                        f"{task.code!r}, which is as many as run numbers go.",
                    )
                self.connection.execute(
                    "INSERT INTO runs (run_id, task_code, run_key, run_number, "
                    "parameters, extra_parameters, status, started_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'in_progress', ?)",
                    (
                        run_id,
                        task.code,
                        run_key,
                        run_number,
                        json.dumps(parameters),
                        json.dumps(extra),
                        db.now(),
                    ),
                )
                self.connection.execute("COMMIT")
                return run_number
            except sqlite3.IntegrityError:
                self.connection.execute("ROLLBACK")
                continue
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
        raise RequestProblem(
            503, "Couldn't get a run number for this participant. Try again."
        )

    # --------------------------------------------------------------- events

    def store_events(
        self, task_code: str, run_id: str, submitted: dict[str, Any]
    ) -> StoreResult:
        run = self._run_for_task(task_code, run_id)

        if run["status"] != "in_progress":
            code = NOT_ACCEPTING_CODES[run["status"]]
            refused = {
                str(event_id): _problem(
                    f"This run is {run['status']}, so it isn't taking events.",
                    can_retry=False,
                )
                for event_id in _as_event_dict(submitted)
            }
            return StoreResult(code, run["status"], self._stored_ids(run_id), refused)

        task = self.task(task_code)
        dataset = storage.in_progress_path(self.config.in_progress_root, run_id)

        errors: dict[str, dict[str, Any]] = {}
        wrote_something = False
        for event_id, event in _as_event_dict(submitted).items():
            problem, written = self._store_one(task, run_id, dataset, event_id, event)
            if problem:
                errors[event_id] = problem
            wrote_something = wrote_something or written

        stored = self._stored_ids(run_id)
        if errors:
            return StoreResult(422, "in_progress", stored, errors)
        return StoreResult(201 if wrote_something else 200, "in_progress", stored)

    def _store_one(
        self,
        task: TaskDefinition,
        run_id: str,
        dataset: Path,
        event_id: str,
        event: Any,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Store one event. Returns (problem or None, whether we wrote a line)."""
        if len(event_id) > MAX_EVENT_ID_LENGTH:
            return _problem(
                f"Event IDs can be at most {MAX_EVENT_ID_LENGTH} characters.", False
            ), False
        if not isinstance(event, dict):
            return _problem(
                "An event has to be a JSON object with a 'data' field.", False
            ), False
        if "data" not in event:
            return _problem("This event has no 'data' field.", False), False

        timestamp = event.get("timestamp")
        if timestamp is not None and not isinstance(timestamp, str):
            return _problem(
                "'timestamp' has to be text, like '2026-07-26T18:25:43.511-05:00'.",
                False,
            ), False

        line_object: dict[str, Any] = {"event_id": event_id, "data": event["data"]}
        if timestamp is not None:
            line_object["timestamp"] = timestamp
        line = storage.canonical(line_object)

        if len(line.encode("utf-8")) > task.max_event_size:
            return _problem(
                f"This event is bigger than this task allows ({task.max_event_size} bytes).",
                False,
            ), False

        digest = storage.content_hash(line)
        known = self.connection.execute(
            "SELECT content_hash FROM events WHERE run_id = ? AND event_id = ?",
            (run_id, event_id),
        ).fetchone()
        if known is not None:
            if known["content_hash"] == digest:
                return None, False  # A retry. We already have it.
            return _collision(event_id), False

        # The file first, then the database. A process that dies between them leaves a
        # line the index doesn't know about, which finalize cleans up. The other order
        # would tell a task its event was stored when it wasn't.
        storage.append_line(dataset, line)
        try:
            self.connection.execute(
                "INSERT INTO events (run_id, event_id, content_hash, stored_at) "
                "VALUES (?, ?, ?, ?)",
                (run_id, event_id, digest, db.now()),
            )
        except sqlite3.IntegrityError:
            # Another request stored this ID between our check and our insert.
            existing = self.connection.execute(
                "SELECT content_hash FROM events WHERE run_id = ? AND event_id = ?",
                (run_id, event_id),
            ).fetchone()
            if existing is None or existing["content_hash"] != digest:
                return _collision(event_id), True
        return None, True

    # ------------------------------------------------------------- finishing

    def finalize_run(self, task_code: str, run_id: str) -> StoreResult:
        run = self._run_for_task(task_code, run_id)
        status = run["status"]

        if status == "in_progress":
            self.connection.execute(
                "UPDATE runs SET status = 'finalizing', finalized_at = ? "
                "WHERE run_id = ? AND status = 'in_progress'",
                (db.now(), run_id),
            )
            return StoreResult(200, "finalizing", self._stored_ids(run_id))

        if status in ("finalizing", "complete"):
            # Finalizing twice is what a retried request looks like. Say what's true.
            return StoreResult(200, status, self._stored_ids(run_id))

        return StoreResult(
            NOT_ACCEPTING_CODES[status],
            status,
            self._stored_ids(run_id),
            {
                "run": _problem(
                    "This run was abandoned, so it can't be finalized by the task. "
                    "Someone with access to the server can finalize it by hand.",
                    can_retry=False,
                )
            },
        )

    def describe_run(self, task_code: str, run_id: str) -> dict[str, Any]:
        run = self._run_for_task(task_code, run_id)
        return {"status": run["status"], "stored": self._stored_ids(run_id)}

    # --------------------------------------------------------------- lookups

    def _run_for_task(self, task_code: str, run_id: str) -> sqlite3.Row:
        self.task(task_code)  # An unknown task is a 404 before anything else.
        row = self.connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None or row["task_code"] != task_code:
            # A real run ID used with the wrong task code is a 404 too, which is usually
            # a copy-paste between two tasks.
            raise RequestProblem(404, "There's no run with that ID for this task.")
        return row

    def _stored_ids(self, run_id: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT event_id FROM events WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
        return [row["event_id"] for row in rows]


def _collision(event_id: str) -> dict[str, Any]:
    return _problem(
        f"This run already has a different event with the ID {event_id!r}. Pig kept the "
        "one it had. Two events were given the same ID, which is a bug in the task "
        "rather than a network problem — sending it again won't help.",
        can_retry=False,
    )


def _as_event_dict(submitted: Any) -> dict[str, Any]:
    if not isinstance(submitted, dict):
        raise RequestProblem(
            422,
            'Expected a JSON object whose keys are your event IDs, like {"1": '
            '{"data": {...}}}.',
        )
    return {str(event_id): event for event_id, event in submitted.items()}
