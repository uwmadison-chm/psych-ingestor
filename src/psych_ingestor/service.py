"""Starting runs, storing events, finalizing. Everything the web service actually does.

Kept free of FastAPI so it reads as ordinary Python, and so the CLI can use the same code
the service does. The runs table and its rules live in `runs.py`; this is the layer that
turns a request into a call on them.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import db, runs, storage
from .config import SAFE_VALUE_EXPLANATION, Config, TaskDefinition, is_safe_value
from .runs import Disposition, RequestProblem, Run

MAX_EVENT_ID_LENGTH = 256

# What a task gets for any run that isn't taking events, whichever way it closed. The
# task's next move is the same in every case — start a new run — so one code is enough,
# and the body's `status` says which kind of closed it is.
RUN_CLOSED = 409


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
        run_number = runs.insert(
            self.connection,
            run_id=run_id,
            task_code=task.code,
            run_key=run_key,
            parameters=parameters,
            extra_parameters=extra,
            started_at=db.now(),
        )
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

    # --------------------------------------------------------------- events

    def store_events(
        self, task_code: str, run_id: str, submitted: dict[str, Any]
    ) -> StoreResult:
        run = self._run_for_task(task_code, run_id)

        if not run.accepting_data:
            # Nothing is wrong with the events; it's the run that's closed. Sending them
            # to a new run will work, so they're retryable — just not here.
            refused = {
                str(event_id): _problem(_run_closed(run.disposition), can_retry=True)
                for event_id in _as_event_dict(submitted)
            }
            return StoreResult(
                RUN_CLOSED, run.api_status, self._stored_ids(run_id), refused
            )

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
        known = runs.receipt_hash(self.connection, run_id, event_id)
        if known is not None:
            if known == digest:
                return None, False  # A retry. We already have it.
            return _collision(event_id), False

        # The file first, then the database. A process that dies between them leaves a
        # line the index doesn't know about, which finalize cleans up. The other order
        # would tell a task its event was stored when it wasn't.
        storage.append_line(dataset, line)
        if not runs.record_receipt(self.connection, run_id, event_id, digest, db.now()):
            # Another request stored this ID between our check and our write.
            existing = runs.receipt_hash(self.connection, run_id, event_id)
            if existing is None or existing != digest:
                return _collision(event_id), True
        return None, True

    # ------------------------------------------------------------- finishing

    def finalize_run(self, task_code: str, run_id: str) -> StoreResult:
        run = self._run_for_task(task_code, run_id)

        if run.accepting_data:
            runs.mark_closed(self.connection, run, Disposition.FINALIZED, db.now())
            return StoreResult(200, "finalizing", self._stored_ids(run_id))

        if run.disposition is Disposition.FINALIZED:
            # Finalizing twice is what a retried request looks like. Say what's true.
            return StoreResult(200, run.api_status, self._stored_ids(run_id))

        return StoreResult(
            RUN_CLOSED,
            run.api_status,
            self._stored_ids(run_id),
            {
                "run": _problem(
                    "This run has expired, so there's nothing to finalize. Everything it "
                    "received is saved. If the participant is still working, start a new "
                    "run.",
                    can_retry=False,
                )
            },
        )

    def describe_run(self, task_code: str, run_id: str) -> dict[str, Any]:
        run = self._run_for_task(task_code, run_id)
        return {"status": run.api_status, "stored": self._stored_ids(run_id)}

    # --------------------------------------------------------------- lookups

    def _run_for_task(self, task_code: str, run_id: str) -> Run:
        self.task(task_code)  # An unknown task is a 404 before anything else.
        run = runs.get(self.connection, run_id)
        if run is None or run.task_code != task_code:
            # A real run ID used with the wrong task code is a 404 too, which is usually
            # a copy-paste between two tasks.
            raise RequestProblem(404, "There's no run with that ID for this task.")
        return run

    def _stored_ids(self, run_id: str) -> list[str]:
        return runs.stored_event_ids(self.connection, run_id)


def _run_closed(disposition: Disposition | None) -> str:
    """Why a closed run refused events, and what the task should do instead."""
    if disposition is Disposition.EXPIRED:
        return (
            "This run has expired, so it isn't taking events. Start a new run for this "
            "participant and send these events to it."
        )
    return (
        "This run was finalized, so it isn't taking events. If there's more to record, "
        "start a new run for this participant and send these events to it."
    )


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
