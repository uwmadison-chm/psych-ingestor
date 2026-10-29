"""Starting runs, storing events, finalizing. Everything the web service actually does.

Kept free of FastAPI so it reads as ordinary Python, and so the CLI can use the same code
the service does. The runs table and its rules live in `runs.py`; this is the layer that
turns a request into a call on them.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from . import db, keys, media, purge, runs, signing, storage
from .config import (
    SAFE_VALUE_EXPLANATION,
    TEXT_VALUE_EXPLANATION,
    Config,
    TaskDefinition,
    is_safe_value,
    is_text_value,
)
from .media import MediaItem
from .runs import Disposition, Phase, RequestProblem, Run

MAX_EVENT_ID_LENGTH = 256

log = logging.getLogger(__name__)

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

    def describe_task(self, task_code: str) -> dict[str, Any]:
        """What a task needs to know about itself before it starts a run.

        A closed task gets the same answer with `open` false. Closing refuses new runs
        only, and the runs already going still need the rest. Nothing here is secret:
        it's all visible from how the other requests behave.
        """
        task = self.task(task_code)
        return {
            "task_code": task.code,
            "open": task.open,
            "parameters": task.required_parameters,
            # The ones held to the safe-value rule. The rest are text.
            "path_parameters": task.path_parameters,
            "expires_after_sec": task.expires_after,
            "max_event_size_bytes": task.max_event_size,
            # A feature a task can have off is `null` when it's off and an object of its
            # settings when it's on.
            "media": (
                {"max_part_size_bytes": task.max_part_size} if task.media else None
            ),
            # The key, but not its name: the browser has no use for the name, and
            # nothing it sends says which key it used. See docs/api.md.
            "encryption": (
                {"public_key": task.public_key} if task.public_key else None
            ),
            # Which parameters the signature covers, so pigsty check can compare them
            # with pigsty.toml's, and a task can tell a link without one. Not which keys:
            # nothing that asks has a use for them.
            "parameter_signing": (
                {"signed_parameters": task.signed_parameters}
                if task.checks_signatures
                else None
            ),
        }

    # ------------------------------------------------------------- starting

    def start_run(
        self, task_code: str, submitted: dict[str, Any], signature: Any = None
    ) -> dict[str, Any]:
        """Start a run with the parameters `submitted`. `signature` only matters to a
        task that checks signatures; any other ignores it."""
        task = self.task(task_code)
        if not task.open:
            raise RequestProblem(
                409, f"The task {task_code!r} isn't accepting new data right now."
            )

        parameters, extra = self._check_parameters(task, submitted)
        signed_by = None
        if task.checks_signatures:
            # The signature isn't kept. It's no use without the parameters it covers,
            # and the key's name says what it proved.
            signed_by = _check_signature(task, parameters, signature)
        run_id = str(uuid.uuid4())

        # The directory first, then the row. A directory with no row is harmless litter;
        # a row with no directory is a run the sweep will have to report. The directory
        # exists from the start so that "is it there" has one meaning by sweep time,
        # whether or not the run ever sent an event.
        storage.make_directories(
            storage.run_directory(self.config.in_progress_root, task.code, run_id)
        )

        run_number = runs.insert(
            self.connection,
            run_id=run_id,
            task_code=task.code,
            run_key_hash=runs.hash_run_key(
                [parameters[name] for name in task.run_key_parameters]
            ),
            run_key_names=list(task.run_key_parameters),
            path_parameter_names=list(task.path_parameters),
            signed_parameter_names=list(task.signed_parameters),
            parameters=parameters,
            extra_parameters=extra,
            started_at=db.now(),
            encrypted_to=task.encrypt_to,
            signed_by=signed_by,
        )
        return {"run_id": run_id, "run_number": run_number}

    def _check_parameters(
        self, task: TaskDefinition, submitted: dict[str, Any]
    ) -> tuple[dict[str, str], dict[str, Any]]:
        """Split what arrived into the parameters this task uses and everything else.

        Path parameters have to be safe values, since `pigsty organize` may put them in
        a path. A parameter that's only signed is text, kept exactly as it arrives, and
        only has to be short and free of control characters.

        Extra parameters are recorded and otherwise ignored — a `utm_source` or a
        leftover `debug=1` never stops a run from starting.
        """
        if not isinstance(submitted, dict):
            raise RequestProblem(
                422,
                "Expected the run parameters as a JSON object, like "
                '{"parameters": {"participant_id": "10351"}}.',
            )

        parameters: dict[str, str] = {}
        for name in task.required_parameters:
            if name not in submitted:
                raise RequestProblem(
                    422,
                    f"This task needs {name!r}, and it wasn't in the request. It expects "
                    f"{task.required_parameters}.",
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
            if name in task.path_parameters and not is_safe_value(value):
                raise RequestProblem(
                    422,
                    f"{value!r} can't be used for {name!r}, because it will become part "
                    f"of a file name. Allowed: {SAFE_VALUE_EXPLANATION}.",
                )
            if not is_text_value(value):
                raise RequestProblem(
                    422,
                    f"The value for {name!r} can't be used. Allowed: "
                    f"{TEXT_VALUE_EXPLANATION}.",
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
        # An unknown task or run is a 404 before anything else.
        run = self._run_for_task(task_code, run_id)
        task = self.task(task_code)
        events = _as_event_dict(submitted)

        # Everything that can be checked without the database, before taking its lock.
        errors: dict[str, dict[str, Any]] = {}
        checked: dict[str, tuple[Any, str]] = {}
        for event_id, event in events.items():
            problem, digest = _check_event(task, run, event_id, event)
            if problem:
                errors[event_id] = problem
            else:
                checked[event_id] = (event["data"], digest)

        events_file = (
            storage.run_directory(self.config.in_progress_root, task_code, run_id)
            / storage.EVENTS_FILE
        )

        # From reading the run's phase to recording the receipts, under the database's
        # write lock. Nothing can close the run in between, so the sweep can't move its
        # directory into done/ while we're about to append to it. Without the lock, a
        # task could be told an event was stored that the finished run doesn't have.
        # See #52.
        with db.write_transaction(self.connection):
            run = self._run_for_task(task_code, run_id)
            if not run.accepting_data:
                # Nothing is wrong with the events; it's the run that's closed. Sending
                # them to a new run will work, so they're retryable, just not here.
                refused = {
                    event_id: _problem(_run_closed(run.disposition), can_retry=True)
                    for event_id in events
                }
                return StoreResult(
                    RUN_CLOSED, run.api_status, self._stored_ids(run_id), refused
                )

            stored_at = db.now()
            new: dict[str, tuple[dict[str, Any], str]] = {}
            for event_id, (data, digest) in checked.items():
                known = runs.receipt_hash(self.connection, run_id, event_id)
                if known is None:
                    line = storage.event_line(event_id, data, stored_at)
                    new[event_id] = (line, digest)
                elif known != digest:
                    errors[event_id] = _collision(event_id)
                # Otherwise it's a retry, and we already have it.

            # The file first, then the database. A process that dies between them
            # leaves lines the index doesn't know about; the retry that follows writes
            # them again, and the repeated lines stay. The other order would tell a
            # task its event was stored when it wasn't. See docs/design_assumptions.md.
            if new:
                storage.append_lines(
                    events_file, [storage.canonical(line) for line, _ in new.values()]
                )
            for event_id, (_, digest) in new.items():
                if not runs.record_receipt(
                    self.connection, run_id, event_id, digest, stored_at
                ):
                    # Can't happen under the write lock we hold; if it does, the
                    # transaction rolls back and the appended lines stay as repeats.
                    raise RuntimeError(
                        f"a receipt for {event_id!r} appeared under the write lock"
                    )

        stored = self._stored_ids(run_id)
        if errors:
            return StoreResult(422, "in_progress", stored, errors)
        return StoreResult(201 if new else 200, "in_progress", stored)

    # ------------------------------------------------------------- finishing

    def finalize_run(self, task_code: str, run_id: str) -> StoreResult:
        run = self._run_for_task(task_code, run_id)

        if run.accepting_data:
            if runs.mark_closed(self.connection, run, Disposition.FINALIZED, db.now()):
                self._signal_sweep()
                return StoreResult(200, "finalizing", self._stored_ids(run_id))
            # The sweep expired it between our read and our update. Answer for the run
            # as it is now, not as we found it.
            run = self._run_for_task(task_code, run_id)

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

    def _signal_sweep(self) -> None:
        """Touch `touch_after_finalizing`, if it's set, so a sweep can start now.

        This is a hint, not part of storing anything. The run is already `finalizing` and
        its events are on disk, so a touch that fails is logged and the task still hears
        that its run was finalized. The scheduled sweep finishes the run either way.
        """
        path = self.config.touch_after_finalizing
        if path is None:
            return
        try:
            storage.touch(path)
        except OSError as error:
            log.warning("Couldn't touch %s to start a sweep: %s", path, error)

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

        if len(event_id) > MAX_EVENT_ID_LENGTH:
            return self._start_refused(
                run,
                event_id,
                f"Event IDs can be at most {MAX_EVENT_ID_LENGTH} characters.",
            )
        if run.encrypted_to and not keys.is_encrypted_text(data):
            return self._start_refused(run, event_id, _NOT_ENCRYPTED)

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

        # One transaction from the phase check to the row, because the media ID is
        # handed out inside it and has to be written on the event's line before the row
        # that records it exists, and because nothing may close the run in between (see
        # `store_events`). The file is still written before the database: a crash after
        # the append leaves a line the index doesn't know about, and the retry hands
        # out the same ID again and writes the same line again.
        with db.write_transaction(self.connection):
            run = self._run_for_task(task_code, run_id)
            if not run.accepting_data:
                refused = StoreResult(
                    RUN_CLOSED,
                    run.api_status,
                    self._stored_ids(run_id),
                    {event_id: _problem(_run_closed(run.disposition), can_retry=True)},
                )
                return Reply(refused.status_code, refused.body())

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
            storage.make_directories(storage.media_directory(run_dir, media_id))
            storage.append_lines(events_file, [storage.canonical(line)])
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
            storage.part_path(run_dir, media_id, part),
            storage.scratch_path(self.config.uploading_root, run_id, media_id, part),
            task.max_part_size,
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
        if run.encrypted_to and not keys.is_encrypted_bytes(writer.start):
            writer.discard()
            return self._media_refused(
                422, run, item, key, _PART_NOT_ENCRYPTED, can_retry=False
            )

        # The check and the rename happen under one write lock, as an event's check
        # and append do. A part that lands replaces whatever had its name, so two
        # uploads of the same part number can't be allowed to both get past the check:
        # the loser would leave its bytes on disk under the winner's receipt.
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
        self._media_item(task_code, run_id, media_id)
        declared = _as_declared_parts(submitted)

        # Under the write lock so no part lands, and the run doesn't close, between the
        # checks and the mark.
        with db.write_transaction(self.connection):
            run = self._run_for_task(task_code, run_id)
            current = media.get(self.connection, run_id, media_id)
            assert current is not None
            if not run.accepting_data:
                return self._media_refused(
                    RUN_CLOSED,
                    run,
                    current,
                    "media",
                    "This run has closed, so nothing in it can be finished now. Every "
                    "part Pig received is saved, and the run's manifest will say the "
                    "item wasn't finished.",
                    can_retry=False,
                )
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

    # -------------------------------------------------------------- purging
    #
    # The secure side asks for this once it has a run, so the copy here can go. It sends
    # a purge hash of every file in its copy, and the run is deleted only if they all
    # match this one. See purge.py, and docs/api.md for why there's no key.

    def purge_run(self, task_code: str, run_id: str, submitted: Any) -> dict[str, Any]:
        sent = _as_purge_hashes(submitted)
        # Not `_run_for_task`: a task taken out of pig.toml still has runs in `done/`,
        # and they still need purging.
        run = runs.get(self.connection, run_id)
        if run is None or run.task_code != task_code:
            raise RequestProblem(404, "There's no run with that ID for this task.")
        if run.phase is not Phase.DONE:
            raise RequestProblem(
                409,
                "This run isn't finished yet, so there's nothing to purge. A run can be "
                "purged once the sweep has finished it and it's in done/.",
            )

        done = storage.run_directory(self.config.done_root, task_code, run_id)
        purging = storage.run_directory(self.config.purging_root, task_code, run_id)
        if not done.exists():
            if purging.exists() or run.parameters is None:
                # Purged already, or purging was interrupted after the run left done/.
                # Either way, the copy that could be uploaded is gone. A retried request
                # is what this looks like, so it isn't an error.
                purge.finish(self.connection, run_id, purging)
                return {"status": "purged"}
            raise RequestProblem(
                409,
                f"Pig's copy of this run isn't in {self.config.done_root}, and Pig didn't "
                "purge it. Nothing was changed. Someone needs to look at the web server.",
            )

        try:
            ours = purge.purge_hashes(done)
        except purge.PurgeProblem as problem:
            raise RequestProblem(
                409, f"Pig can't check its copy of this run: {problem}"
            ) from None
        found = purge.differences(sent, ours)
        if found:
            raise RequestProblem(
                409,
                "Your copy of this run doesn't match Pig's, so nothing was purged. "
                + " ".join(found),
            )

        # Out of done/ in one rename, so the copy to the bucket sees the whole run or
        # none of it, never half a deleted one.
        try:
            storage.move_directory(done, purging)
        except OSError:
            if done.exists():
                raise
            # Another purge of the same run moved it first. Finishing is safe to repeat.
        purge.finish(self.connection, run_id, purging)
        return {"status": "purged"}

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


# What an encrypting run says to plaintext. The usual cause is a browser that loaded the
# task's settings before it encrypted, and kept them while it was offline.
_NOT_ENCRYPTED = (
    "This run is encrypted, and this event isn't. Pig only takes encrypted events for "
    "it. The page probably has an old copy of this task's settings; reloading it fetches "
    "the current ones, and a new run will encrypt."
)
_PART_NOT_ENCRYPTED = (
    "This run is encrypted, and this part isn't. Pig only takes encrypted parts for it."
)


def _check_event(
    task: TaskDefinition, run: Run, event_id: str, event: Any
) -> tuple[dict[str, Any] | None, str]:
    """Check one event against everything that doesn't need the database. Returns a
    problem, or None and the hash of what the task sent."""
    if len(event_id) > MAX_EVENT_ID_LENGTH:
        return _problem(
            f"Event IDs can be at most {MAX_EVENT_ID_LENGTH} characters.", False
        ), ""
    if not isinstance(event, dict):
        return _problem(
            "An event has to be a JSON object with a 'data' field.", False
        ), ""
    if "data" not in event:
        return _problem("This event has no 'data' field.", False), ""

    # Anything outside `data` would be dropped on the floor, and a task that put a
    # timestamp there would never know. Refusing is the only way it finds out.
    unexpected = sorted(key for key in event if key != "data")
    if unexpected:
        return _problem(
            f"This event has {unexpected} outside 'data'. Pig stores only what's "
            "inside 'data', so put everything you want to keep in there.",
            False,
        ), ""
    if run.encrypted_to and not keys.is_encrypted_text(event["data"]):
        return _problem(_NOT_ENCRYPTED, False), ""

    # The size limit and the hash both cover what the task sent, not what Pig adds.
    hashed = storage.hashed_text({"event_id": event_id, "data": event["data"]})
    if len(hashed.encode("utf-8")) > task.max_event_size:
        return _problem(
            f"This event is bigger than this task allows ({task.max_event_size} bytes).",
            False,
        ), ""
    return None, storage.content_hash(hashed)


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


def _as_purge_hashes(submitted: Any) -> dict[str, str]:
    """The `purge_hashes` of a purge request, or a 422 saying what's wrong with it."""
    hashes = submitted.get("purge_hashes") if isinstance(submitted, dict) else None
    if (
        not isinstance(hashes, dict)
        or not hashes
        or not all(
            isinstance(path, str) and isinstance(value, str)
            for path, value in hashes.items()
        )
    ):
        raise RequestProblem(
            422,
            'A purge request is {"purge_hashes": {path: hash, ...}}, with the purge '
            "hash of every file in the run. See docs/api.md.",
        )
    return hashes


def _check_signature(
    task: TaskDefinition, parameters: dict[str, str], signature: Any
) -> str:
    """The name of the key that signed this run's parameters, or a 403 saying why
    they don't check out.

    403 rather than 422, because nothing about the request is malformed: it's that Pig
    won't start a run from this link. The message is for whoever made the link, and the
    participant is told to use the one they were sent.
    """
    values = {name: parameters[name] for name in task.signed_parameters}
    try:
        return signing.verify(signature, task.code, values, task.signing_keys)
    except signing.BadSignature as bad:
        raise RequestProblem(
            403,
            f"This link can't start a run of {task.code!r}, because {bad} Use the link "
            "you were sent, exactly as it is.",
        ) from None


def read_start_request(body: Any) -> tuple[Any, Any]:
    """The parameters and the signature in a request to start a run.

    Each request body is an object with named fields, rather than the parameters or the
    events themselves, so there's room for something new beside them without taking a
    name a task might want.
    """
    _check_fields(
        body,
        required="parameters",
        allowed={"parameters", "signature"},
        example='{"parameters": {"participant_id": "10351"}}',
    )
    return body["parameters"], body.get("signature")


def read_events_request(body: Any) -> Any:
    """The events in a request that sends them. See `read_start_request`."""
    _check_fields(
        body,
        required="events",
        allowed={"events"},
        example='{"events": {"1": {"data": {...}}}}',
    )
    return body["events"]


def _check_fields(body: Any, required: str, allowed: set[str], example: str) -> None:
    if not isinstance(body, dict) or required not in body:
        raise RequestProblem(
            422, f"Expected a JSON object with {required!r} in it, like {example}."
        )
    unexpected = sorted(str(name) for name in set(body) - allowed)
    if unexpected:
        # Refused rather than ignored: a parameter or an event put here by mistake would
        # otherwise be dropped without anyone being told.
        raise RequestProblem(
            422,
            f"This request can only have {' and '.join(map(repr, sorted(allowed)))} "
            f"in it, and it also has {', '.join(map(repr, unexpected))}. Did that "
            f"belong inside {required!r}?",
        )


def _as_event_dict(submitted: Any) -> dict[str, Any]:
    if not isinstance(submitted, dict):
        raise RequestProblem(
            422,
            "Expected the events as a JSON object whose keys are your event IDs, like "
            '{"events": {"1": {"data": {...}}}}.',
        )
    return {str(event_id): event for event_id, event in submitted.items()}
