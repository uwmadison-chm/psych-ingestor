"""Reading request bodies, and checking events, before anything touches the database.

Everything here turns what a task sent into checked values or a refusal saying what was
wrong with it. None of it needs the database, a lock, or the disk, so it's what the
property tests in `fuzz/` aim most of their generated requests at.
"""

from __future__ import annotations

from typing import Any

from .. import formats, keys
from . import media
from .config import TaskDefinition
from .runs import RequestProblem, Run

MAX_EVENT_ID_LENGTH = 256

# What an encrypting run says to plaintext. The usual cause is a browser that loaded the
# task's settings before it encrypted, and kept them while it was offline.
NOT_ENCRYPTED = (
    "This run is encrypted, and this event isn't. Pig only takes encrypted events for "
    "it. The page probably has an old copy of this task's settings; reloading it fetches "
    "the current ones, and a new run will encrypt."
)


class BadEvent(Exception):
    """One event Pig won't store, and why. Never worth retrying: the same event will be
    refused the same way."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ------------------------------------------------------------------ events


def check_event(task: TaskDefinition, run: Run, event_id: str, data: Any) -> str:
    """Check one event against every rule that doesn't need the database, and return
    the hash that tells a retry from a collision. Raises `BadEvent` if it can't be stored.

    Every event comes through here, whether it arrived in a batch of events or started a
    media item, so the rules for an event are written once.
    """
    if len(event_id) > MAX_EVENT_ID_LENGTH:
        raise BadEvent(f"Event IDs can be at most {MAX_EVENT_ID_LENGTH} characters.")
    if run.encrypted_to and not keys.is_encrypted_text(data):
        raise BadEvent(NOT_ENCRYPTED)

    # The size limit and the hash both cover what the task sent, not what Pig adds. See
    # `formats.TASK_KEYS`.
    hashed = formats.hashed_text({"event_id": event_id, "data": data})
    if len(hashed.encode("utf-8")) > task.max_event_size:
        raise BadEvent(
            f"This event is bigger than this task allows ({task.max_event_size} bytes)."
        )
    return formats.content_hash(hashed)


def event_data(event: Any) -> Any:
    """The `data` of one event in a batch, `{"data": {...}}`, or `BadEvent`.

    Per event rather than for the whole request, because one malformed event in a batch
    is refused on its own and the rest are still stored.
    """
    if not isinstance(event, dict):
        raise BadEvent("An event has to be a JSON object with a 'data' field.")
    if "data" not in event:
        raise BadEvent("This event has no 'data' field.")

    # Anything outside `data` would be dropped on the floor, and a task that put a
    # timestamp there would never know. Refusing is the only way it finds out.
    unexpected = sorted(key for key in event if key != "data")
    if unexpected:
        raise BadEvent(
            f"This event has {unexpected} outside 'data'. Pig stores only what's "
            "inside 'data', so put everything you want to keep in there."
        )
    return event["data"]


# ---------------------------------------------------------- request bodies


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


def as_event_dict(submitted: Any) -> dict[str, Any]:
    """The events in a request, by event ID."""
    if not isinstance(submitted, dict):
        raise RequestProblem(
            422,
            "Expected the events as a JSON object whose keys are your event IDs, like "
            '{"events": {"1": {"data": {...}}}}.',
        )
    return {str(event_id): event for event_id, event in submitted.items()}


def as_media_start(submitted: Any) -> tuple[str, Any]:
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


def as_declared_parts(submitted: Any) -> int:
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
    # Refused here, before anything counts up to it: checking which of a billion
    # parts are missing would take minutes, under the database's write lock.
    if parts > media.MAX_PART:
        raise RequestProblem(
            422,
            f"'parts' is {parts}, and a media item can have at most {media.MAX_PART} "
            f"parts. {expected}",
        )
    return parts


def as_purge_hashes(submitted: Any) -> dict[str, str]:
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
