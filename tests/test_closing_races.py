"""A run closing while a request about it is under way. See #52.

The sweep runs in another process, on its own connection, and can expire and finish a run
at any moment. Each test here makes it do that at the worst moment for one request: after
the request has read the run, before it writes.
"""

import pytest
from fastapi.testclient import TestClient

from psych_ingestor import db, runs, service, storage, sweep
from psych_ingestor.runs import Disposition
from psych_ingestor.service import Pig

PARTICIPANT = {"participant_id": "10351", "session": "baseline"}

# Kept before any test replaces it, for the sweep in `expire_and_finish` to use.
mark_closed = runs.mark_closed


def expire_and_finish(pig: Pig, run_id: str) -> None:
    """What a sweep does to a run that's past its limit, on a connection of its own."""
    connection = db.connect(pig.config.database)
    run = runs.get(connection, run_id)
    assert run is not None
    mark_closed(connection, run, Disposition.EXPIRED, db.now())
    sweep.finish_closed_runs(pig.config, connection, sweep.SweepReport())
    connection.close()


def done(pig: Pig, task: str, run_id: str):
    return storage.run_directory(pig.config.done_root, task, run_id)


def test_an_event_arriving_as_the_run_closes_is_refused_not_lost(pig: Pig, monkeypatch):
    run_id = pig.start_run("stroop", PARTICIPANT)["run_id"]
    pig.store_events("stroop", run_id, {"1": {"data": {"trial": 1}}})

    real = service._check_event

    def sweep_first(*args):
        expire_and_finish(pig, run_id)
        return real(*args)

    monkeypatch.setattr(service, "_check_event", sweep_first)
    result = pig.store_events("stroop", run_id, {"2": {"data": {"trial": 2}}})

    assert result.status_code == 409
    assert result.status == "expired"
    assert result.stored == ["1"]
    assert result.errors["2"]["can_retry"] is True
    # Nothing was written anywhere, least of all a fresh directory under in_progress/.
    events = (done(pig, "stroop", run_id) / "events.jsonl").read_text()
    assert '"trial":2' not in events
    in_progress = storage.run_directory(pig.config.in_progress_root, "stroop", run_id)
    assert not in_progress.exists()


def test_appending_to_a_run_that_has_moved_is_an_error(pig: Pig):
    run_id = pig.start_run("stroop", PARTICIPANT)["run_id"]
    pig.finalize_run("stroop", run_id)
    sweep.sweep(pig.config, pig.connection)

    events = (
        storage.run_directory(pig.config.in_progress_root, "stroop", run_id)
        / "events.jsonl"
    )
    with pytest.raises(FileNotFoundError):
        storage.append_lines(events, ["{}"])
    assert not events.parent.exists()


def test_finalizing_as_the_run_expires_says_it_expired(pig: Pig, monkeypatch):
    run_id = pig.start_run("stroop", PARTICIPANT)["run_id"]

    def sweep_first(connection, run, disposition, when):
        expire_and_finish(pig, run_id)
        return mark_closed(connection, run, disposition, when)

    monkeypatch.setattr(runs, "mark_closed", sweep_first)
    result = pig.finalize_run("stroop", run_id)

    assert result.status_code == 409
    assert result.status == "expired"


def test_a_media_start_arriving_as_the_run_closes_is_refused(
    pig: Pig, client: TestClient, monkeypatch
):
    run_id = client.post(
        "/task/interview/run", json={"participant_id": "10351"}
    ).json()["run_id"]

    real = service._as_media_start

    def sweep_first(submitted):
        expire_and_finish(pig, run_id)
        return real(submitted)

    monkeypatch.setattr(service, "_as_media_start", sweep_first)
    started = client.post(
        f"/task/interview/run/{run_id}/media",
        json={"event_id": "audio", "data": {"content_type": "audio/webm"}},
    )

    assert started.status_code == 409
    assert started.json()["status"] == "expired"
    assert (done(pig, "interview", run_id) / "events.jsonl").read_text() == ""
    assert not storage.run_directory(
        pig.config.in_progress_root, "interview", run_id
    ).exists()


def test_parts_are_written_outside_the_run_and_the_sweep_clears_them(
    pig: Pig, client: TestClient
):
    run_id = client.post(
        "/task/interview/run", json={"participant_id": "10351"}
    ).json()["run_id"]
    client.post(
        f"/task/interview/run/{run_id}/media",
        json={"event_id": "audio", "data": {"content_type": "audio/webm"}},
    )
    assert client.put(
        f"/task/interview/run/{run_id}/media/1/1", content=b"a"
    ).is_success

    # What an upload cut off partway leaves behind, for this run and another.
    uploading = pig.config.uploading_root
    stray = storage.scratch_path(uploading, run_id, 1, 2)
    stray.write_bytes(b"half a part")
    someone_else = storage.scratch_path(uploading, "another-run", 1, 1)
    someone_else.write_bytes(b"still arriving")

    client.post(f"/task/interview/run/{run_id}/finalize")
    report = sweep.sweep(pig.config, pig.connection)

    assert report.finished == [run_id]
    assert not stray.exists()
    assert someone_else.exists()
    paths = [
        entry["path"]
        for entry in storage.read_manifest(done(pig, "interview", run_id))["files"]
    ]
    assert paths == ["events.jsonl", "media/00001/000001.part"]
