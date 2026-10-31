"""What the property tests share: a fresh Pig for each example, and a way to read its
answers the way `app.py` would send them.

Hypothesis runs each test many times with different inputs, and an input it shrinks has
to fail the same way every time it's tried. So every example gets its own Pig, in its own
directory, rather than one shared across them.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import shutil
import signal
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse

from psych_ingestor import db, keys
from psych_ingestor.config import load_config
from psych_ingestor.runs import RequestProblem
from psych_ingestor.service import Pig, Reply, StoreResult

SIGNING_KEY = "fuzz/signing"
AGE_KEY = "fuzz/age"

# One task for each kind of check Pig makes, so a generated request meets all of them.
CONFIG = """
data_root = "./data"
database = "./pig.db"
public_keys_file = "{public_keys}"

[task.stroop]
path_parameters = ["participant_id", "session"]
run_key_parameters = ["participant_id", "session"]

[task.interview]
path_parameters = ["participant_id"]
run_key_parameters = ["participant_id"]
media = true
max_part_size = "1k"
max_event_size = "4k"

[task.teddy]
path_parameters = ["id"]
run_key_parameters = ["id"]
signed_parameters = ["id", "modules"]
signed_by = ["{signing_key}"]

[task.sealed]
path_parameters = ["participant_id"]
run_key_parameters = ["participant_id"]
encrypt_to = "{age_key}"
media = true

[task.balloons]
path_parameters = ["participant_id"]
run_key_parameters = ["participant_id"]
open = false
"""

TASK_CODES = ["stroop", "interview", "teddy", "sealed", "balloons"]

# Parameters that start a run of each task, for tests that need one going.
STARTS = {
    "stroop": {"participant_id": "PPT-1003", "session": "baseline"},
    "interview": {"participant_id": "PPT-1003"},
    "sealed": {"participant_id": "PPT-1003"},
}


@cache
def key_directory() -> Path:
    """Keys for the tasks that sign and encrypt. Made once per test process, since
    making a key writes files, and every example can share them."""
    directory = Path(tempfile.mkdtemp(prefix="pig-fuzz-keys-"))
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    now = datetime(2026, 10, 31, tzinfo=UTC)
    private = directory / "private_keys"
    keys.new_signing_key(private, SIGNING_KEY, now)
    keys.new_private_key(private, AGE_KEY, now)
    (directory / "public_keys.txt").write_text(
        keys.public_keys_text(keys.read_all_keys(private), private, now)
    )
    return directory


def signing_key() -> keys.SigningKey:
    return keys.read_signing_keys(key_directory() / "private_keys")[0]


@contextmanager
def fresh_pig(config: str = CONFIG) -> Iterator[Pig]:
    """A Pig over a configuration, database, and data directory of its own. `config`
    may name the keys above, as `CONFIG` does."""
    with tempfile.TemporaryDirectory(prefix="pig-fuzz-") as temporary:
        path = Path(temporary) / "pig.toml"
        path.write_text(
            config.format(
                public_keys=key_directory() / "public_keys.txt",
                signing_key=SIGNING_KEY,
                age_key=AGE_KEY,
            )
        )
        config = load_config(path)
        config.in_progress_root.mkdir(parents=True)
        connection = db.connect(config.database)
        try:
            yield Pig(config, connection)
        finally:
            connection.close()


def answer(request: Callable[[], Any]) -> tuple[int, dict[str, Any]]:
    """Make one request of the service, and return the status and body `app.py` would
    send for it. Anything the service raises other than a RequestProblem is what the
    web service would answer with a 500, so it's left to fail the test."""
    try:
        result = request()
    except RequestProblem as problem:
        return problem.status_code, {"message": problem.message}
    if isinstance(result, StoreResult):
        return result.status_code, result.body()
    if isinstance(result, Reply):
        return result.status_code, result.body
    return 200, result


def send_part(
    pig: Pig,
    task_code: str,
    run_id: str,
    media_id: int,
    part: int,
    chunks: list[bytes],
    content_length: int | None = None,
) -> Reply:
    """Send a media part the way `app.py` does, as chunks of a streamed body."""

    async def stream() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    return asyncio.run(
        pig.store_part(task_code, run_id, media_id, part, stream(), content_length)
    )


def check_answer(status: int, body: dict[str, Any]) -> None:
    """A success, or a refusal Pig wrote on purpose, and either way a body that can be
    sent.

    A refusal Pig wrote says what's wrong, and any `errors` in it each have a
    `message` and say whether trying again can help.
    """
    assert 200 <= status < 300 or 400 <= status < 500, f"answered {status}: {body}"
    # What app.py hands each body to. It refuses what JSON can't carry, like NaN, and
    # what UTF-8 can't, like half of a UTF-16 pair, and either would be a 500.
    JSONResponse(status_code=status, content=body)
    if status >= 400:
        # A `message`, or the run's `status`, which is what a task reads from a 409.
        assert isinstance(body.get("message") or body.get("status"), str), body
        for problem in body.get("errors", {}).values():
            assert isinstance(problem["message"], str), body
            assert isinstance(problem["can_retry"], bool), body


@contextmanager
def time_limit(seconds: float) -> Iterator[None]:
    """Raise TimeoutError in whatever's running if it takes longer than this. For a
    test whose failure is taking forever, so it fails rather than hangs."""

    def stop(signal_number: int, frame: Any) -> None:
        raise TimeoutError(f"took longer than {seconds} seconds")

    previous = signal.signal(signal.SIGALRM, stop)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def strict_json(text: str) -> Any:
    """Parse JSON as `jq` and `JSON.parse` do: no NaN, no Infinity."""

    def refuse(constant: str) -> None:
        raise ValueError(f"{constant} isn't JSON")

    return json.loads(text, parse_constant=refuse)


def check_stored_lines(pig: Pig) -> None:
    """Every line Pig has written to any events file is JSON that any reader can read.

    The lines Pig writes are read by researchers' tools, not just by Pig, and a line
    `jq` refuses is a trial lost as surely as one never written.
    """
    for events in pig.config.data_root.rglob("events.jsonl"):
        # Split on newlines only. `str.splitlines` also splits on characters JSON
        # can hold as they are, like U+2028, and Pig writes those unescaped.
        for line in events.read_bytes().decode("utf-8").split("\n"):
            if line:
                strict_json(line)
