"""Starting runs, storing events, finalizing. Everything the web service actually does.

Kept free of FastAPI so it reads as ordinary Python, and so the CLI can use the same code
the service does. The runs table and its rules live in `runs.py`; this is the layer that
turns a request into a call on them.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .. import files, formats, keys, signing
from ..rules import (
    SAFE_VALUE_EXPLANATION,
    TEXT_VALUE_EXPLANATION,
    is_safe_value,
    is_text_value,
)
from . import db, media, purge, requests, runs, storage
from .config import Config, TaskDefinition
from .media import MediaItem
from .requests import BadEvent
from .runs import Disposition, Phase, RequestProblem, Run

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


class RunClosed(Exception):
    """The run stopped taking data. Raised by `Pig._collecting`, and answered by each
    request in its own shape, since what a task needs to hear differs between them."""

    def __init__(self, run: Run):
        super().__init__(run.run_id)
        self.run = run


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
        files.make_directories(
            formats.run_directory(self.config.in_progress_root, task.code, run_id)
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

    # --------------------------------------------------------------- storing
    #
    # Every request that adds to a run (events, a media start, a part, a media finish)
    # does its writing inside `_collecting`, and nowhere else. That's what makes "stored
    # means on disk" true.

    @contextmanager
    def _collecting(self, task_code: str, run_id: str) -> Iterator[Run]:
        """Hold the database's write lock for a block that adds to a run, and give it the
        run as it is under the lock. Raises `RunClosed` if the run isn't taking data.

        From reading the run's phase to recording the receipts, nothing else can write
        to the database, so nothing can close the run in between, and the sweep can't
        move its directory into done/ while we're about to write to it. Without the
        lock, a task could be told something was stored that the finished run doesn't
        have. See #52. Whatever the request read about the run before this is a guess;
        the run this yields is the one that counts.

        Inside the block, the file goes first and the database second. A process that
        dies between them leaves something on disk the database doesn't know about; the
        retry that follows writes it again, and the repeat stays. The other order would
        tell a task its data was stored when it wasn't. Duplicated and visible, never
        missing and silent. See docs/design_assumptions.md.

        Raising anything inside the block rolls the database back.
        """
        with db.write_transaction(self.connection):
            run = self._run_for_task(task_code, run_id)
            if not run.accepting_data:
                raise RunClosed(run)
            yield run

    def _record_receipt(
        self, run_id: str, event_id: str, digest: str, stored_at: datetime
    ) -> None:
        """Record that an event is on disk. Only inside `_collecting`, after the write."""
        if not runs.record_receipt(
            self.connection, run_id, event_id, digest, stored_at
        ):
            # Can't happen under the write lock we hold; if it does, the transaction
            # rolls back and the line already written stays as a harmless repeat.
            raise RuntimeError(
                f"a receipt for {event_id!r} appeared under the write lock"
            )

    # --------------------------------------------------------------- events

    def store_events(
        self, task_code: str, run_id: str, submitted: dict[str, Any]
    ) -> StoreResult:
        # An unknown task or run is a 404 before anything else.
        run = self._run_for_task(task_code, run_id)
        task = self.task(task_code)
        events = requests.as_event_dict(submitted)

        # Everything that can be checked without the database, before taking its lock.
        errors: dict[str, dict[str, Any]] = {}
        checked: dict[str, tuple[Any, str]] = {}
        for event_id, event in events.items():
            try:
                data = requests.event_data(event)
                checked[event_id] = (
                    data,
                    requests.check_event(task, run, event_id, data),
                )
            except BadEvent as bad:
                errors[event_id] = _problem(bad.message, can_retry=False)

        events_file = (
            formats.run_directory(self.config.in_progress_root, task_code, run_id)
            / formats.EVENTS_FILE
        )

        try:
            with self._collecting(task_code, run_id):
                stored_at = db.now()
                new: dict[str, tuple[dict[str, Any], str]] = {}
                for event_id, (data, digest) in checked.items():
                    known = runs.receipt_hash(self.connection, run_id, event_id)
                    if known is None:
                        line = formats.event_line(event_id, data, stored_at)
                        new[event_id] = (line, digest)
                    elif known != digest:
                        errors[event_id] = _collision(event_id)
                    # Otherwise it's a retry, and we already have it.

                # The file first, then the database. See `_collecting`.
                if new:
                    storage.append_lines(
                        events_file,
                        [formats.canonical(line) for line, _ in new.values()],
                    )
                for event_id, (_, digest) in new.items():
                    self._record_receipt(run_id, event_id, digest, stored_at)
        except RunClosed as closed:
            # Nothing is wrong with the events; it's the run that's closed. Sending
            # them to a new run will work, so they're retryable, just not here.
            refused = {
                event_id: _problem(_run_closed(closed.run.disposition), can_retry=True)
                for event_id in events
            }
            return StoreResult(
                RUN_CLOSED, closed.run.api_status, self._stored_ids(run_id), refused
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
        event_id, data = requests.as_media_start(submitted)
        try:
            digest = requests.check_event(task, run, event_id, data)
        except BadEvent as bad:
            return self._start_refused(run, event_id, bad.message)

        run_dir = formats.run_directory(self.config.in_progress_root, task_code, run_id)
        events_file = run_dir / formats.EVENTS_FILE

        # The media ID is handed out under the lock, and written on the event's line
        # before the row that records it exists. A crash after the append leaves a line
        # the database doesn't know about, and the retry hands out the same ID again and
        # writes the same line again.
        try:
            with self._collecting(task_code, run_id) as run:
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
                        f"This run already has {media.MAX_MEDIA_ID} media items, which "
                        "is as many as media IDs go.",
                    )
                stored_at = db.now()
                line = formats.event_line(event_id, data, stored_at, media_id)
                files.make_directories(formats.media_directory(run_dir, media_id))
                storage.append_lines(events_file, [formats.canonical(line)])
                media.insert(self.connection, run_id, media_id, event_id)
                self._record_receipt(run_id, event_id, digest, stored_at)
                item = media.get(self.connection, run_id, media_id)
        except RunClosed as closed:
            refused = StoreResult(
                RUN_CLOSED,
                closed.run.api_status,
                self._stored_ids(run_id),
                {
                    event_id: _problem(
                        _run_closed(closed.run.disposition), can_retry=True
                    )
                },
            )
            return Reply(refused.status_code, refused.body())
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

        # Saves reading the body for a run or an item that's already closed. The checks
        # that count are the ones under the lock, below.
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

        run_dir = formats.run_directory(self.config.in_progress_root, task_code, run_id)
        writer = storage.PartWriter(
            formats.part_path(run_dir, media_id, part),
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
        try:
            with self._collecting(task_code, run_id) as run:
                current = self._current_item(run_id, media_id)
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
                        f"This media item already has a different part {part}. Pig "
                        "kept the one it had. Two parts were given the same number, "
                        "which is a bug in the task rather than a network problem.",
                        can_retry=False,
                    )
                writer.keep()
                media.record_part(
                    self.connection, run_id, media_id, part, writer.size, digest, now
                )
        except RunClosed as closed:
            writer.discard()
            return self._media_refused(
                RUN_CLOSED,
                closed.run,
                self._current_item(run_id, media_id),
                key,
                _run_closed(closed.run.disposition),
                can_retry=True,
            )
        return Reply(201, self._media_body(run, current))

    def finish_media(
        self, task_code: str, run_id: str, media_id: int, submitted: Any
    ) -> Reply:
        """The task says how many parts it sent. Pig checks it holds exactly those."""
        self._media_item(task_code, run_id, media_id)
        declared = requests.as_declared_parts(submitted)

        # Under the write lock so no part lands, and the run doesn't close, between the
        # checks and the mark. Nothing is written to disk.
        try:
            with self._collecting(task_code, run_id) as run:
                current = self._current_item(run_id, media_id)
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
                        f"You said this media item has {declared} part(s), but Pig "
                        f"holds part(s) {_list(extra)} beyond that. Pig kept them. "
                        "Check the count your task sends.",
                        can_retry=False,
                    )
                if missing:
                    return self._media_refused(
                        422,
                        run,
                        current,
                        "media",
                        f"You said this media item has {declared} part(s), but Pig "
                        f"doesn't have part(s) {_list(missing)}. Send them, then finish "
                        "again.",
                        can_retry=True,
                    )
                media.mark_finished(self.connection, current, declared, db.now())
                finished = self._current_item(run_id, media_id)
        except RunClosed as closed:
            return self._media_refused(
                RUN_CLOSED,
                closed.run,
                self._current_item(run_id, media_id),
                "media",
                "This run has closed, so nothing in it can be finished now. Every "
                "part Pig received is saved, and the run's manifest will say the "
                "item wasn't finished.",
                can_retry=False,
            )
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
        # The media ID is from the URL, which FastAPI reads as a whole number of any
        # size. One Pig would never hand out has no item, and asking the database
        # about one too big for its integers raises rather than finding nothing.
        item = None
        if 1 <= media_id <= media.MAX_MEDIA_ID:
            item = media.get(self.connection, run_id, media_id)
        if item is None:
            raise RequestProblem(
                404, "There's no media item with that ID for this run."
            )
        return task, run, item

    def _current_item(self, run_id: str, media_id: int) -> MediaItem:
        """A media item `_media_item` has already found, as the database has it now."""
        item = media.get(self.connection, run_id, media_id)
        assert item is not None
        return item

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
    # match this one. See formats.py, and docs/api.md for why there's no key.

    def purge_run(self, task_code: str, run_id: str, submitted: Any) -> dict[str, Any]:
        sent = requests.as_purge_hashes(submitted)
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

        done = formats.run_directory(self.config.done_root, task_code, run_id)
        purging = formats.run_directory(self.config.purging_root, task_code, run_id)
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
            ours = formats.purge_hashes(done)
        except formats.PurgeProblem as problem:
            raise RequestProblem(
                409, f"Pig can't check its copy of this run: {problem}"
            ) from None
        found = formats.differences(sent, ours)
        if found:
            raise RequestProblem(
                409,
                "Your copy of this run doesn't match Pig's, so nothing was purged. "
                + " ".join(found),
            )

        # Out of done/ in one rename, so the copy to the bucket sees the whole run or
        # none of it, never half a deleted one.
        try:
            files.move_directory(done, purging)
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


_PART_NOT_ENCRYPTED = (
    "This run is encrypted, and this part isn't. Pig only takes encrypted parts for it."
)


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
