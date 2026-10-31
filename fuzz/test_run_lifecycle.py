"""A model of runs' lives, checked against Pig one step at a time.

Hypothesis plays out sequences of everything that can happen to runs: starting them,
sending events (new, retried, and colliding), media items and their parts, finalizing,
time passing, the sweep, purging, and the process dying between writing a file and
recording its receipt. After every step it checks what Pig must never get wrong:

- Every receipt has its line or its part on disk. "Stored" means stored.
- No run is in both trees, and where a run's directory is matches its phase.
- A finished run's manifest matches its files, and nothing in `done/` ever changes.
- The event IDs a run lists as stored only grow.
- Nothing is answered with a 500.

That's "duplicated and visible, never missing and silent", checked by machine. See
docs/design_assumptions.md.

Each rule also checks Pig's answer against a model of what it should be: a retry is
stored once, a different event under a known ID is refused, a closed run answers 409.
The model is kept as simple as it can be, so a disagreement is easy to read.
"""

from __future__ import annotations

import hashlib
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    invariant,
    multiple,
    rule,
)
from support import answer, check_answer, fresh_pig, send_part, strict_json

from psych_ingestor import db, media, purge, runs, storage, sweep
from psych_ingestor.runs import Phase

CONFIG = """
data_root = "./data"
database = "./pig.db"

[task.stroop]
path_parameters = ["participant_id"]
run_key_parameters = ["participant_id"]
expires_after = "1h"

[task.interview]
path_parameters = ["participant_id"]
run_key_parameters = ["participant_id"]
expires_after = "1h"
media = true
max_part_size = "1k"
"""

EXPIRES_AFTER = timedelta(hours=1)
MAX_PART_SIZE = 1024

# Few enough of each that the same ones come up again: a retry, or a collision.
event_ids = st.sampled_from(["0", "1", "2", "3"])
event_data = st.sampled_from([{"rt": 843}, {"rt": 612}, {"said": "naïve "}, []])
part_numbers = st.integers(min_value=1, max_value=3)
part_bytes = st.sampled_from(
    [b"a", b"bb", b"x" * MAX_PART_SIZE, b"x" * (MAX_PART_SIZE + 1)]
)


class Crash(Exception):
    """The process died. Raised in place of recording a receipt."""


def crash(*args: Any) -> None:
    raise Crash


@dataclass
class ModelRun:
    """What the model expects of one run."""

    task_code: str
    started_at: datetime
    closed: bool = False
    expired: bool = False
    done: bool = False
    purged: bool = False
    # Event ID → data, for each event Pig said it stored.
    stored: dict[str, Any] = field(default_factory=dict)
    # Event ID → media ID, for the events that started media items.
    media: dict[str, int] = field(default_factory=dict)


@dataclass
class ModelMedia:
    """What the model expects of one media item."""

    run_id: str
    media_id: int
    # Part number → bytes, for each part Pig said it stored.
    parts: dict[int, bytes] = field(default_factory=dict)
    finished_with: int | None = None


class RunLives(RuleBasedStateMachine):
    started = Bundle("started")
    media_items = Bundle("media_items")

    def __init__(self) -> None:
        super().__init__()
        self.now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
        self.cleanup = ExitStack()
        self.pig = self.cleanup.enter_context(fresh_pig(CONFIG))
        # Pig asks db.now() for the time, everywhere. This is the model's clock.
        real_now = db.now
        db.now = lambda: self.now
        self.cleanup.callback(setattr, db, "now", real_now)

        self.runs: dict[str, ModelRun] = {}
        self.items: dict[tuple[str, int], ModelMedia] = {}
        # What each run last listed as stored.
        self.listed: dict[str, list[str]] = {}
        # Each finished run's files, with their hashes, as first seen in done/.
        self.finished_files: dict[str, dict[str, str]] = {}

    def teardown(self) -> None:
        self.cleanup.close()

    def ask(self, request) -> tuple[int, dict[str, Any]]:
        status, body = answer(request)
        check_answer(status, body)
        return status, body

    # ------------------------------------------------------------------ rules

    @rule(
        target=started,
        task_code=st.sampled_from(["stroop", "interview"]),
        participant=st.sampled_from(["PPT-1003", "PPT-1004"]),
    )
    def start(self, task_code: str, participant: str):
        status, body = self.ask(
            lambda: self.pig.start_run(task_code, {"participant_id": participant})
        )
        assert status == 200, body
        self.runs[body["run_id"]] = ModelRun(task_code, started_at=self.now)
        return body["run_id"]

    @rule(
        run_id=started,
        events=st.dictionaries(event_ids, event_data, min_size=1, max_size=3),
    )
    def send_events(self, run_id: str, events: dict[str, Any]):
        run = self.runs[run_id]
        sent = {event_id: {"data": data} for event_id, data in events.items()}
        status, body = self.ask(
            lambda: self.pig.store_events(run.task_code, run_id, sent)
        )

        if run.closed:
            assert status == 409, body
            # Nothing wrong with the events, so they can go to a new run.
            assert all(problem["can_retry"] for problem in body["errors"].values())
            return

        new = False
        for event_id, data in events.items():
            if event_id not in run.stored:
                run.stored[event_id] = data
                new = True
            if run.stored[event_id] == data:
                assert event_id not in body.get("errors", {}), body
            else:
                assert body["errors"][event_id]["can_retry"] is False, body
        if body.get("errors"):
            assert status == 422, body
        else:
            assert status == (201 if new else 200), body
        assert set(body["stored"]) == set(run.stored), body

    @rule(
        run_id=started,
        events=st.dictionaries(event_ids, event_data, min_size=1, max_size=3),
    )
    def die_while_sending_events(self, run_id: str, events: dict[str, Any]):
        """The process dies after the events' lines are written, before their receipts
        are. Nothing answers; the task will try again, or won't."""
        run = self.runs[run_id]
        sent = {event_id: {"data": data} for event_id, data in events.items()}
        real = runs.record_receipt
        runs.record_receipt = crash
        try:
            self.pig.store_events(run.task_code, run_id, sent)
        except Crash:
            pass
        finally:
            runs.record_receipt = real

    @rule(target=media_items, run_id=started, event_id=event_ids, data=event_data)
    def start_media(self, run_id: str, event_id: str, data: Any):
        run = self.runs[run_id]
        status, body = self.ask(
            lambda: self.pig.start_media(
                run.task_code, run_id, {"event_id": event_id, "data": data}
            )
        )
        if run.task_code != "interview":
            assert status == 404, body
        elif run.closed:
            assert status == 409, body
        elif event_id not in run.stored:
            assert status == 201, body
            run.stored[event_id] = data
            run.media[event_id] = body["media_id"]
            self.items[(run_id, body["media_id"])] = ModelMedia(
                run_id, body["media_id"]
            )
            return (run_id, body["media_id"])
        elif event_id in run.media and run.stored[event_id] == data:
            # A retry: the same item, which is in the bundle already.
            assert status == 200, body
            assert body["media_id"] == run.media[event_id], body
        else:
            # An ordinary event already has the ID, or a different media item does.
            assert status == 422, body
        return multiple()

    @rule(item=media_items, part=part_numbers, content=part_bytes)
    def send_a_part(self, item: tuple[str, int], part: int, content: bytes):
        run_id, media_id = item
        run, model = self.runs[run_id], self.items[item]
        status, body = self.ask(
            lambda: send_part(
                self.pig, run.task_code, run_id, media_id, part, [content]
            )
        )
        if run.closed:
            assert status == 409, body
            return
        if model.finished_with is not None:
            assert status == 409, body
            return
        if len(content) > MAX_PART_SIZE:
            assert status == 413, body
            return
        if part not in model.parts:
            model.parts[part] = content
            assert status == 201, body
        elif model.parts[part] == content:
            assert status == 200, body
        else:
            assert status == 422, body
        assert body["media"]["stored"] == sorted(model.parts), body

    @rule(item=media_items, part=part_numbers, content=part_bytes)
    def die_while_sending_a_part(
        self, item: tuple[str, int], part: int, content: bytes
    ):
        """The process dies after the part is renamed into place, before its receipt is
        recorded."""
        run_id, media_id = item
        run = self.runs[run_id]
        real = media.record_part
        media.record_part = crash
        try:
            send_part(self.pig, run.task_code, run_id, media_id, part, [content])
        except Crash:
            pass
        finally:
            media.record_part = real

    @rule(item=media_items, parts=st.integers(min_value=1, max_value=4))
    def finish_media(self, item: tuple[str, int], parts: int):
        run_id, media_id = item
        run, model = self.runs[run_id], self.items[item]
        status, body = self.ask(
            lambda: self.pig.finish_media(
                run.task_code, run_id, media_id, {"parts": parts}
            )
        )
        if run.closed:
            assert status == 409, body
        elif model.finished_with is not None:
            assert status == (200 if parts == model.finished_with else 422), body
        elif any(number > parts for number in model.parts):
            assert status == 422 and not body["errors"]["media"]["can_retry"], body
        elif any(number not in model.parts for number in range(1, parts + 1)):
            assert status == 422 and body["errors"]["media"]["can_retry"], body
        else:
            assert status == 200, body
            model.finished_with = parts

    @rule(run_id=started)
    def finalize(self, run_id: str):
        run = self.runs[run_id]
        status, body = self.ask(lambda: self.pig.finalize_run(run.task_code, run_id))
        if not run.closed:
            run.closed = True
            assert (status, body["status"]) == (200, "finalizing"), body
        elif run.expired:
            assert (status, body["status"]) == (409, "expired"), body
        else:
            assert status == 200, body
            assert body["status"] == ("complete" if run.done else "finalizing"), body

    @rule(minutes=st.integers(min_value=1, max_value=90))
    def time_passes(self, minutes: int):
        self.now += timedelta(minutes=minutes)

    @rule()
    def sweep(self):
        report = sweep.sweep(self.pig.config, self.pig.connection)
        assert not (report.busy or report.failed or report.refused), report

        past_limit = {
            run_id
            for run_id, run in self.runs.items()
            if not run.closed and self.now - run.started_at >= EXPIRES_AFTER
        }
        assert set(report.expired) == past_limit, report
        for run_id in past_limit:
            self.runs[run_id].closed = True
            self.runs[run_id].expired = True

        waiting = {
            run_id for run_id, run in self.runs.items() if run.closed and not run.done
        }
        assert set(report.finished) == waiting, report
        for run_id in waiting:
            self.runs[run_id].done = True

    @rule(run_id=started, tamper=st.booleans())
    def purge(self, run_id: str, tamper: bool):
        """`pigsty purge`, with the purge hashes of the run's files as the secure side
        would compute them from its copy, or with one of them wrong."""
        run = self.runs[run_id]
        directory = self.directory(run_id, done=True)
        if run.done and not run.purged:
            hashes = purge.purge_hashes(directory)
        else:
            hashes = {storage.EVENTS_FILE: "0" * 64}
        if tamper:
            hashes[storage.EVENTS_FILE] = "f" * 64
        status, body = self.ask(
            lambda: self.pig.purge_run(run.task_code, run_id, {"purge_hashes": hashes})
        )
        if not run.done:
            assert status == 409, body
        elif run.purged:
            # A retry, which a purge interrupted after the move looks like too.
            assert status == 200, body
        elif tamper:
            assert status == 409, body
        else:
            assert status == 200, body
            run.purged = True

    # ------------------------------------------------------------- invariants

    @invariant()
    def every_receipt_has_its_line(self):
        for row in self.pig.connection.execute(
            "SELECT run_id, event_id, content_hash FROM event_receipts"
        ):
            if self.runs[row["run_id"]].purged:
                continue
            lines = self.lines(row["run_id"])
            assert (row["event_id"], row["content_hash"]) in lines, (
                f"run {row['run_id']} has a receipt for event {row['event_id']!r} "
                "and no line for it"
            )

    @invariant()
    def every_part_receipt_has_its_part(self):
        for row in self.pig.connection.execute(
            "SELECT run_id, media_id, part, sha256 FROM media_parts"
        ):
            if self.runs[row["run_id"]].purged:
                continue
            path = storage.part_path(
                self.directory(row["run_id"]), row["media_id"], row["part"]
            )
            assert path.exists(), f"{path} has a receipt and isn't there"
            assert sha256(path) == row["sha256"], f"{path} isn't what its receipt says"

    @invariant()
    def each_run_is_where_its_phase_says(self):
        for run_id, run in self.runs.items():
            in_progress = self.directory(run_id, done=False).exists()
            done = self.directory(run_id, done=True).exists()
            assert not (in_progress and done), f"run {run_id} is in both trees"
            phase = runs.get(self.pig.connection, run_id).phase
            if not run.closed:
                assert phase is Phase.COLLECTING and in_progress
            elif not run.done:
                assert phase is Phase.CLOSED and in_progress
            elif not run.purged:
                assert phase is Phase.DONE and done
            else:
                assert phase is Phase.DONE and not in_progress and not done

    @invariant()
    def finished_runs_match_their_manifests_and_never_change(self):
        for run_id, run in self.runs.items():
            if not run.done or run.purged:
                continue
            directory = self.directory(run_id, done=True)
            files = storage.describe_files(directory)
            assert storage.read_manifest(directory)["files"] == files
            hashes = {
                path.relative_to(directory).as_posix(): sha256(path)
                for path in directory.rglob("*")
                if path.is_file()
            }
            assert self.finished_files.setdefault(run_id, hashes) == hashes

    @invariant()
    def stored_event_ids_only_grow(self):
        for run_id, run in self.runs.items():
            stored = self.pig.describe_run(run.task_code, run_id)["stored"]
            before = self.listed.get(run_id, [])
            assert stored[: len(before)] == before, (before, stored)
            assert set(stored) == set(run.stored), (stored, run.stored)
            self.listed[run_id] = stored

    # ---------------------------------------------------------------- helpers

    def directory(self, run_id: str, done: bool | None = None) -> Path:
        """Where a run's directory is, or should be. Unless `done` says which tree,
        whichever one the model has it in."""
        run = self.runs[run_id]
        if done is None:
            done = run.done
        root = self.pig.config.done_root if done else self.pig.config.in_progress_root
        return storage.run_directory(root, run.task_code, run_id)

    def lines(self, run_id: str) -> set[tuple[str, str]]:
        """Each line in a run's events file, as its event ID and the hash Pig records
        for it."""
        events = self.directory(run_id) / storage.EVENTS_FILE
        if not events.exists():
            return set()
        found = set()
        for text in events.read_text(encoding="utf-8").split("\n"):
            if text:
                line = strict_json(text)
                hashed = storage.content_hash(storage.hashed_text(line))
                found.add((line["event_id"], hashed))
        return found


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


test_run_lives = RunLives.TestCase
