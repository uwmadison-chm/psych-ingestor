"""Starting runs, storing events, finalizing. Everything the web service actually does.

Kept free of FastAPI so it reads as ordinary Python, and so the CLI can use the same code
the service does. The runs table and its rules live in `runs.py`; this is the layer that
turns a request into a call on them.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import db, media, runs, storage
from .config import SAFE_VALUE_EXPLANATION, Config, TaskDefinition, is_safe_value
from .media import MediaItem
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


@dataclass
class Reply:
    """A status code and a body, for the media requests. Their bodies differ in shape
    from one request to the next, so there's nothing to derive."""

    status_code: int
    body: dict[str, Any]


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
        run_id = str(uuid.uuid4())

        # The directory first, then the row. A directory with no row is harmless litter;
        # a row with no directory is a run the sweep will have to report. The directory
        # exists from the start so that "is it there" has one meaning by sweep time,
        # whether or not the run ever sent an event.
        storage.run_directory(self.config.in_progress_root, task.code, run_id).mkdir(
            parents=True, exist_ok=True
        )

        run_number = runs.insert(
            self.connection,
            run_id=run_id,
            task_code=task.code,
            run_key_hash=runs.hash_run_key([parameters[name] for name in task.run_key]),
            run_key_names=list(task.run_key),
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
                    f"{value!r} can't be used for {name!r}, because it will become part "
                    f"of a file name. Allowed: {SAFE_VALUE_EXPLANATION}.",
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
        events_file = (
            storage.run_directory(self.config.in_progress_root, task_code, run_id)
            / storage.EVENTS_FILE
        )

        errors: dict[str, dict[str, Any]] = {}
        wrote_something = False
        for event_id, event in _as_event_dict(submitted).items():
            problem, written = self._store_one(
                task, run_id, events_file, event_id, event
            )
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
        events_file: Path,
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

        # Anything outside `data` would be dropped on the floor, and a task that put a
        # timestamp there would never know. Refusing is the only way it finds out.
        unexpected = sorted(key for key in event if key != "data")
        if unexpected:
            return _problem(
                f"This event has {unexpected} outside 'data'. Pig stores only what's "
                "inside 'data', so put everything you want to keep in there.",
                False,
            ), False

        stored_at = db.now()
        line_object = storage.event_line(event_id, event["data"], stored_at)
        # The size limit and the hash both cover what the task sent, not what Pig added.
        hashed = storage.hashed_text(line_object)

        if len(hashed.encode("utf-8")) > task.max_event_size:
            return _problem(
                f"This event is bigger than this task allows ({task.max_event_size} bytes).",
                False,
            ), False

        digest = storage.content_hash(hashed)
        known = runs.receipt_hash(self.connection, run_id, event_id)
        if known is not None:
            if known == digest:
                return None, False  # A retry. We already have it.
            return _collision(event_id), False

        # The file first, then the database. A process that dies between them leaves a
        # line the index doesn't know about; the retry that follows writes it again, and
        # the repeated line stays. The other order would tell a task its event was
        # stored when it wasn't. See docs/design_assumptions.md.
        storage.append_line(events_file, storage.canonical(line_object))
        if not runs.record_receipt(
            self.connection, run_id, event_id, digest, stored_at
        ):
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
        described: dict[str, Any] = {
            "status": run.api_status,
            "stored": self._stored_ids(run_id),
        }
        if self.task(task_code).media:
            described["media"] = [
                media.summary(self.connection, item)
                for item in media.for_run(self.connection, run_id)
            ]
        return described

    # ---------------------------------------------------------------- media
    #
    # A media item is an event with bytes attached. Starting one stores an ordinary
    # event, under every rule an event has, and gives it a media ID that names the
    # directory its parts go in. The parts follow one per request, and the task finishes
    # the item by saying how many it sent. See docs/api.md.

    def start_media(self, task_code: str, run_id: str, submitted: Any) -> Reply:
        task = self._media_task(task_code)
        run = self._run_for_task(task_code, run_id)
        event_id, data = _as_media_start(submitted)

        if not run.accepting_data:
            refused = StoreResult(
                RUN_CLOSED,
                run.api_status,
                self._stored_ids(run_id),
                {event_id: _problem(_run_closed(run.disposition), can_retry=True)},
            )
            return Reply(refused.status_code, refused.body())

        if len(event_id) > MAX_EVENT_ID_LENGTH:
            return self._start_refused(
                run,
                event_id,
                f"Event IDs can be at most {MAX_EVENT_ID_LENGTH} characters.",
            )

        stored_at = db.now()
        # The hash and the size limit cover what the task sent, and the media ID isn't
        # part of that, so both are settled before there is a media ID.
        hashed = storage.hashed_text(storage.event_line(event_id, data, stored_at))
        if len(hashed.encode("utf-8")) > task.max_event_size:
            return self._start_refused(
                run,
                event_id,
                f"This event is bigger than this task allows ({task.max_event_size} "
                "bytes).",
            )
        digest = storage.content_hash(hashed)

        run_dir = storage.run_directory(self.config.in_progress_root, task_code, run_id)
        events_file = run_dir / storage.EVENTS_FILE

        # One transaction from the receipt check to the row, because the media ID is
        # handed out inside it and has to be written on the event's line before the row
        # that records it exists. The file is still written before the database: a
        # crash after the append leaves a line the index doesn't know about, and the
        # retry hands out the same ID again and writes the same line again.
        with db.write_transaction(self.connection):
            known = runs.receipt_hash(self.connection, run_id, event_id)
            if known is not None:
                item = media.by_event_id(self.connection, run_id, event_id)
                if item is None:
                    return self._start_refused(
                        run,
                        event_id,
                        f"This run already has an ordinary event with the ID "
                        f"{event_id!r}, sent without media. Pig kept it. Use a "
                        "different event ID for the media item.",
                    )
                if known == digest:
                    return Reply(200, self._started(task, item))  # A retry.
                return self._start_refused(
                    run, event_id, _collision(event_id)["message"]
                )

            media_id = media.next_media_id(self.connection, run_id)
            if media_id > media.MAX_MEDIA_ID:
                return self._start_refused(
                    run,
                    event_id,
                    f"This run already has {media.MAX_MEDIA_ID} media items, which is "
                    "as many as media IDs go.",
                )
            line = storage.event_line(event_id, data, stored_at, media_id)
            storage.media_directory(run_dir, media_id).mkdir(
                parents=True, exist_ok=True
            )
            storage.append_line(events_file, storage.canonical(line))
            media.insert(self.connection, run_id, media_id, event_id)
            if not runs.record_receipt(
                self.connection, run_id, event_id, digest, stored_at
            ):
                # Can't happen under the write lock we hold; if it does, the
                # transaction rolls back and the appended line stays as a harmless
                # repeat.
                raise RuntimeError(
                    f"a receipt for {event_id!r} appeared under the write lock"
                )
            item = media.get(self.connection, run_id, media_id)
        assert item is not None
        return Reply(201, self._started(task, item))

    def _started(self, task: TaskDefinition, item: MediaItem) -> dict[str, Any]:
        return {"media_id": item.media_id, "max_part_size": task.max_part_size}

    def _start_refused(self, run: Run, event_id: str, message: str) -> Reply:
        refused = StoreResult(
            422,
            run.api_status,
            self._stored_ids(run.run_id),
            {event_id: _problem(message, can_retry=False)},
        )
        return Reply(refused.status_code, refused.body())

    async def store_part(
        self,
        task_code: str,
        run_id: str,
        media_id: int,
        part: int,
        chunks: AsyncIterator[bytes],
        content_length: int | None = None,
    ) -> Reply:
        """Store one part of a media item as its bytes arrive.

        Async because the body is streamed to disk rather than read whole: a part can
        be megabytes, and holding one per request in memory is the thing to avoid. It's
        the one async thing in the service, and the writes inside it block the event
        loop for as long as a disk write takes, which at Pig's traffic is fine.

        Everything that can be refused without reading the body is refused first, so a
        closed run or a finished item doesn't cost a megabyte to find out about.
        """
        task, run, item = self._media_item(task_code, run_id, media_id)
        key = str(part)

        if not run.accepting_data:
            return self._media_refused(
                RUN_CLOSED, run, item, key, _run_closed(run.disposition), can_retry=True
            )
        if item.finished:
            return self._media_refused(
                RUN_CLOSED, run, item, key, _item_finished(item), can_retry=False
            )
        if part < 1 or part > media.MAX_PART:
            return self._media_refused(
                422,
                run,
                item,
                key,
                f"Part numbers count from 1 and go up to {media.MAX_PART}.",
                can_retry=False,
            )
        if content_length is not None and content_length > task.max_part_size:
            return self._media_refused(
                413, run, item, key, _part_too_large(task), can_retry=False
            )

        run_dir = storage.run_directory(self.config.in_progress_root, task_code, run_id)
        writer = storage.PartWriter(
            storage.part_path(run_dir, media_id, part), task.max_part_size
        )
        try:
            async for chunk in chunks:
                writer.write(chunk)
            digest = writer.finish()
        except storage.PartTooLarge:
            writer.discard()
            return self._media_refused(
                413, run, item, key, _part_too_large(task), can_retry=False
            )
        except BaseException:
            writer.discard()
            raise

        # The check and the rename happen under one write lock. Unlike an appended
        # event, a part that lands replaces whatever had its name, so two uploads of
        # the same part number can't be allowed to both get past the check: the loser
        # would leave its bytes on disk under the winner's receipt.
        now = db.now()
        with db.write_transaction(self.connection):
            run = self._run_for_task(task_code, run_id)
            current = media.get(self.connection, run_id, media_id)
            assert current is not None
            if not run.accepting_data:
                writer.discard()
                return self._media_refused(
                    RUN_CLOSED,
                    run,
                    current,
                    key,
                    _run_closed(run.disposition),
                    can_retry=True,
                )
            if current.finished:
                writer.discard()
                return self._media_refused(
                    RUN_CLOSED,
                    run,
                    current,
                    key,
                    _item_finished(current),
                    can_retry=False,
                )
            known = media.part_hash(self.connection, run_id, media_id, part)
            if known == digest:
                writer.discard()
                return Reply(200, self._media_body(run, current))  # A retry.
            if known is not None:
                writer.discard()
                return self._media_refused(
                    422,
                    run,
                    current,
                    key,
                    f"This media item already has a different part {part}. Pig kept "
                    "the one it had. Two parts were given the same number, which is a "
                    "bug in the task rather than a network problem.",
                    can_retry=False,
                )
            writer.keep()
            media.record_part(
                self.connection, run_id, media_id, part, writer.size, digest, now
            )
        return Reply(201, self._media_body(run, current))

    def finish_media(
        self, task_code: str, run_id: str, media_id: int, submitted: Any
    ) -> Reply:
        """The task says how many parts it sent. Pig checks it holds exactly those."""
        _, run, item = self._media_item(task_code, run_id, media_id)
        declared = _as_declared_parts(submitted)

        if not run.accepting_data:
            return self._media_refused(
                RUN_CLOSED,
                run,
                item,
                "media",
                "This run has closed, so nothing in it can be finished now. Every part "
                "Pig received is saved, and the run's manifest will say the item "
                "wasn't finished.",
                can_retry=False,
            )

        # Under the write lock so no part lands between the check and the mark.
        with db.write_transaction(self.connection):
            current = media.get(self.connection, run_id, media_id)
            assert current is not None
            if current.finished:
                if current.declared_parts == declared:
                    return Reply(200, self._media_body(run, current))  # A retry.
                return self._media_refused(
                    422,
                    run,
                    current,
                    "media",
                    f"This media item was already finished with "
                    f"{current.declared_parts} part(s), and that can't change.",
                    can_retry=False,
                )

            stored = media.stored_parts(self.connection, run_id, media_id)
            held = set(stored)
            missing = [
                number for number in range(1, declared + 1) if number not in held
            ]
            extra = [number for number in stored if number > declared]
            if extra:
                return self._media_refused(
                    422,
                    run,
                    current,
                    "media",
                    f"You said this media item has {declared} part(s), but Pig holds "
                    f"part(s) {_list(extra)} beyond that. Pig kept them. Check the "
                    "count your task sends.",
                    can_retry=False,
                )
            if missing:
                return self._media_refused(
                    422,
                    run,
                    current,
                    "media",
                    f"You said this media item has {declared} part(s), but Pig doesn't "
                    f"have part(s) {_list(missing)}. Send them, then finish again.",
                    can_retry=True,
                )
            media.mark_finished(self.connection, current, declared, db.now())
            finished = media.get(self.connection, run_id, media_id)
        assert finished is not None
        return Reply(200, self._media_body(run, finished))

    def _media_task(self, task_code: str) -> TaskDefinition:
        task = self.task(task_code)
        if not task.media:
            raise RequestProblem(
                404,
                f"The task {task_code!r} isn't set up to take media. Set `media = "
                "true` in its configuration if it should be.",
            )
        return task

    def _media_item(
        self, task_code: str, run_id: str, media_id: int
    ) -> tuple[TaskDefinition, Run, MediaItem]:
        task = self._media_task(task_code)
        run = self._run_for_task(task_code, run_id)
        item = media.get(self.connection, run_id, media_id)
        if item is None:
            raise RequestProblem(
                404, "There's no media item with that ID for this run."
            )
        return task, run, item

    def _media_body(self, run: Run, item: MediaItem) -> dict[str, Any]:
        return {
            "status": run.api_status,
            "media": media.summary(self.connection, item),
        }

    def _media_refused(
        self,
        status_code: int,
        run: Run,
        item: MediaItem,
        key: str,
        message: str,
        can_retry: bool,
    ) -> Reply:
        body = self._media_body(run, item)
        body["errors"] = {key: _problem(message, can_retry)}
        return Reply(status_code, body)

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


def _item_finished(item: MediaItem) -> str:
    return (
        f"This media item was finished with {item.declared_parts} part(s), so it isn't "
        "taking more. If there's more to record, start a new media item."
    )


def _part_too_large(task: TaskDefinition) -> str:
    return (
        f"This part is bigger than this task allows ({task.max_part_size} bytes). "
        "Send smaller parts."
    )


def _list(numbers: list[int]) -> str:
    """'17' or '17, 18, 19' or '17, 18, 19 and 4 more', for an error message."""
    shown = ", ".join(str(number) for number in numbers[:5])
    if len(numbers) > 5:
        shown += f" and {len(numbers) - 5} more"
    return shown


def _as_media_start(submitted: Any) -> tuple[str, Any]:
    """The event that starts a media item: `{"event_id": ..., "data": {...}}`."""
    expected = 'Expected {"event_id": "...", "data": {...}}.'
    if not isinstance(submitted, dict):
        raise RequestProblem(422, expected)
    if "event_id" not in submitted:
        raise RequestProblem(422, f"This media item has no 'event_id'. {expected}")
    if "data" not in submitted:
        raise RequestProblem(422, f"This media item has no 'data'. {expected}")
    unexpected = sorted(key for key in submitted if key not in ("event_id", "data"))
    if unexpected:
        raise RequestProblem(
            422,
            f"This media item has {unexpected} outside 'data'. Pig stores only what's "
            "inside 'data', so put everything you want to keep in there.",
        )
    event_id = submitted["event_id"]
    if isinstance(event_id, int) and not isinstance(event_id, bool):
        event_id = str(event_id)  # A counter, as JavaScript would send it.
    if not isinstance(event_id, str):
        raise RequestProblem(422, "'event_id' has to be text.")
    return event_id, submitted["data"]


def _as_declared_parts(submitted: Any) -> int:
    """The body of a finish request: `{"parts": 37}` and nothing else."""
    expected = 'Expected {"parts": N}, the number of parts you sent, counting from 1.'
    if not isinstance(submitted, dict) or "parts" not in submitted:
        raise RequestProblem(422, expected)
    unexpected = sorted(key for key in submitted if key != "parts")
    if unexpected:
        raise RequestProblem(422, f"Unexpected {unexpected} in the request. {expected}")
    parts = submitted["parts"]
    if isinstance(parts, bool) or not isinstance(parts, int) or parts < 1:
        raise RequestProblem(
            422, f"'parts' has to be a whole number, 1 or more. {expected}"
        )
    return parts


def _as_event_dict(submitted: Any) -> dict[str, Any]:
    if not isinstance(submitted, dict):
        raise RequestProblem(
            422,
            'Expected a JSON object whose keys are your event IDs, like {"1": '
            '{"data": {...}}}.',
        )
    return {str(event_id): event for event_id, event in submitted.items()}
