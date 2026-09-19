"""Media items: an event with bytes attached. The HTTP surface as docs/api.md describes
it, plus what lands on disk and in the manifest."""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from psych_ingestor import media, storage, sweep
from psych_ingestor.service import Pig

PARTICIPANT = {"participant_id": "10351"}
START = {
    "event_id": "prompt3_audio",
    "data": {"content_type": "audio/webm;codecs=opus", "prompt": 3},
}


def start_run(client: TestClient) -> str:
    return client.post("/task/interview/run", json=PARTICIPANT).json()["run_id"]


def start_media(client: TestClient, run_id: str) -> int:
    started = client.post(f"/task/interview/run/{run_id}/media", json=START)
    assert started.status_code == 201
    return started.json()["media_id"]


def put_part(client: TestClient, run_id: str, media_id: int, part: int, body: bytes):
    return client.put(
        f"/task/interview/run/{run_id}/media/{media_id}/{part}", content=body
    )


def finish(client: TestClient, run_id: str, media_id: int, parts: int):
    return client.post(
        f"/task/interview/run/{run_id}/media/{media_id}/finish", json={"parts": parts}
    )


# ---------------------------------------------------------------------- the whole flow


def test_the_worked_example(client: TestClient):
    run_id = start_run(client)

    started = client.post(f"/task/interview/run/{run_id}/media", json=START)
    assert started.status_code == 201
    assert started.json() == {"media_id": 1, "max_part_size": 1024}

    first = put_part(client, run_id, 1, 1, b"first part")
    assert first.status_code == 201
    assert first.json() == {
        "status": "in_progress",
        "media": {
            "media_id": 1,
            "event_id": "prompt3_audio",
            "stored": [1],
            "finished": False,
        },
    }
    assert put_part(client, run_id, 1, 2, b"second part").status_code == 201

    finished = finish(client, run_id, 1, 2)
    assert finished.status_code == 200
    assert finished.json()["media"] == {
        "media_id": 1,
        "event_id": "prompt3_audio",
        "stored": [1, 2],
        "finished": True,
        "parts": 2,
    }

    # The run knows about it, both as an event and as a media item.
    checked = client.get(f"/task/interview/run/{run_id}").json()
    assert checked["stored"] == ["prompt3_audio"]
    assert checked["media"] == [finished.json()["media"]]


def test_starting_a_media_item_stores_an_ordinary_event(pig: Pig, client: TestClient):
    run_id = start_run(client)
    start_media(client, run_id)

    events_file = (
        storage.run_directory(pig.config.in_progress_root, "interview", run_id)
        / storage.EVENTS_FILE
    )
    [line] = storage.read_lines(events_file)
    assert line["event_id"] == "prompt3_audio"
    assert line["data"] == START["data"]
    assert line["metadata"]["media_id"] == 1
    assert "stored_at" in line["metadata"]


def test_media_ids_count_from_one_within_the_run(client: TestClient):
    run_id = start_run(client)
    assert start_media(client, run_id) == 1
    second = client.post(
        f"/task/interview/run/{run_id}/media",
        json={"event_id": "prompt4_audio", "data": {}},
    )
    assert second.json()["media_id"] == 2

    other_run = start_run(client)
    assert start_media(client, other_run) == 1


def test_parts_land_in_the_run_directory(pig: Pig, client: TestClient):
    run_id = start_run(client)
    start_media(client, run_id)
    put_part(client, run_id, 1, 3, b"three")

    run_dir = storage.run_directory(pig.config.in_progress_root, "interview", run_id)
    part = run_dir / "media" / "00001" / "000003.part"
    assert part.read_bytes() == b"three"
    assert list((run_dir / "media" / "00001").iterdir()) == [part]


# -------------------------------------------------------------------------- retries


def test_retrying_a_start_gives_the_same_media_id(client: TestClient):
    run_id = start_run(client)
    assert start_media(client, run_id) == 1
    again = client.post(f"/task/interview/run/{run_id}/media", json=START)
    assert again.status_code == 200
    assert again.json() == {"media_id": 1, "max_part_size": 1024}


def test_a_start_with_the_same_id_and_different_data_is_refused(client: TestClient):
    run_id = start_run(client)
    start_media(client, run_id)
    changed = client.post(
        f"/task/interview/run/{run_id}/media",
        json={"event_id": "prompt3_audio", "data": {"prompt": 4}},
    )
    assert changed.status_code == 422
    assert changed.json()["errors"]["prompt3_audio"]["can_retry"] is False
    assert changed.json()["stored"] == ["prompt3_audio"]


def test_an_event_id_already_used_by_a_plain_event_is_refused(client: TestClient):
    run_id = start_run(client)
    client.post(
        f"/task/interview/run/{run_id}",
        json={"prompt3_audio": {"data": START["data"]}},
    )
    refused = client.post(f"/task/interview/run/{run_id}/media", json=START)
    assert refused.status_code == 422
    problem = refused.json()["errors"]["prompt3_audio"]
    assert problem["can_retry"] is False
    assert "ordinary event" in problem["message"]


def test_retrying_a_part_is_a_200_and_writes_nothing(pig: Pig, client: TestClient):
    run_id = start_run(client)
    start_media(client, run_id)
    assert put_part(client, run_id, 1, 1, b"same bytes").status_code == 201
    again = put_part(client, run_id, 1, 1, b"same bytes")
    assert again.status_code == 200
    assert again.json()["media"]["stored"] == [1]


def test_a_part_with_the_same_number_and_different_bytes_is_refused(
    pig: Pig, client: TestClient
):
    run_id = start_run(client)
    start_media(client, run_id)
    put_part(client, run_id, 1, 1, b"the real part")
    refused = put_part(client, run_id, 1, 1, b"something else")

    assert refused.status_code == 422
    assert refused.json()["errors"]["1"]["can_retry"] is False
    assert refused.json()["media"]["stored"] == [1]
    run_dir = storage.run_directory(pig.config.in_progress_root, "interview", run_id)
    assert storage.part_path(run_dir, 1, 1).read_bytes() == b"the real part"
    # And no scratch file was left behind.
    assert list(run_dir.rglob("*.partial")) == []


def test_parts_may_arrive_in_any_order(client: TestClient):
    run_id = start_run(client)
    start_media(client, run_id)
    assert put_part(client, run_id, 1, 3, b"c").status_code == 201
    assert put_part(client, run_id, 1, 1, b"a").status_code == 201
    replied = put_part(client, run_id, 1, 2, b"b")
    assert replied.json()["media"]["stored"] == [1, 2, 3]


# ------------------------------------------------------------------------- finishing


def test_finishing_with_a_part_missing_is_refused_and_says_which(client: TestClient):
    run_id = start_run(client)
    start_media(client, run_id)
    put_part(client, run_id, 1, 1, b"a")
    put_part(client, run_id, 1, 3, b"c")

    refused = finish(client, run_id, 1, 3)
    assert refused.status_code == 422
    problem = refused.json()["errors"]["media"]
    assert problem["can_retry"] is True
    assert "part(s) 2" in problem["message"]
    assert refused.json()["media"]["finished"] is False
    assert refused.json()["media"]["stored"] == [1, 3]

    # Send the missing one and try again.
    put_part(client, run_id, 1, 2, b"b")
    assert finish(client, run_id, 1, 3).status_code == 200


def test_finishing_with_fewer_parts_than_pig_holds_is_refused(client: TestClient):
    run_id = start_run(client)
    start_media(client, run_id)
    put_part(client, run_id, 1, 1, b"a")
    put_part(client, run_id, 1, 2, b"b")
    refused = finish(client, run_id, 1, 1)
    assert refused.status_code == 422
    problem = refused.json()["errors"]["media"]
    assert problem["can_retry"] is False
    assert "part(s) 2 beyond" in problem["message"]


def test_finishing_twice_with_the_same_count_is_a_200(client: TestClient):
    run_id = start_run(client)
    start_media(client, run_id)
    put_part(client, run_id, 1, 1, b"a")
    assert finish(client, run_id, 1, 1).status_code == 200
    assert finish(client, run_id, 1, 1).status_code == 200


def test_finishing_again_with_a_different_count_is_refused(client: TestClient):
    run_id = start_run(client)
    start_media(client, run_id)
    put_part(client, run_id, 1, 1, b"a")
    finish(client, run_id, 1, 1)
    refused = finish(client, run_id, 1, 2)
    assert refused.status_code == 422
    assert refused.json()["errors"]["media"]["can_retry"] is False
    assert refused.json()["media"]["parts"] == 1


def test_a_finished_item_takes_no_more_parts(client: TestClient):
    run_id = start_run(client)
    start_media(client, run_id)
    put_part(client, run_id, 1, 1, b"a")
    finish(client, run_id, 1, 1)

    late = put_part(client, run_id, 1, 2, b"b")
    assert late.status_code == 409
    assert late.json()["errors"]["2"]["can_retry"] is False
    assert late.json()["media"]["stored"] == [1]
    # Even a retry of a part it already has: same answer as a closed run gives a
    # retried event, and `stored` says the part is there.
    retried = put_part(client, run_id, 1, 1, b"a")
    assert retried.status_code == 409
    assert retried.json()["media"]["stored"] == [1]


def test_a_finish_body_has_to_be_a_count(client: TestClient):
    run_id = start_run(client)
    start_media(client, run_id)
    for body in ({}, {"parts": 0}, {"parts": "3"}, {"parts": True}, {"count": 3}):
        refused = client.post(f"/task/interview/run/{run_id}/media/1/finish", json=body)
        assert refused.status_code == 422, body
        assert "parts" in refused.json()["message"]


# ------------------------------------------------------------------------- refusals


def test_a_task_without_media_says_so(client: TestClient):
    run_id = client.post(
        "/task/stroop/run", json={"participant_id": "10351", "session": "baseline"}
    ).json()["run_id"]
    refused = client.post(f"/task/stroop/run/{run_id}/media", json=START)
    assert refused.status_code == 404
    assert "media = true" in refused.json()["message"]
    assert (
        client.put(f"/task/stroop/run/{run_id}/media/1/1", content=b"x").status_code
        == 404
    )
    # And a run that doesn't take media doesn't mention it.
    assert "media" not in client.get(f"/task/stroop/run/{run_id}").json()


def test_an_unknown_media_item_is_a_404(client: TestClient):
    run_id = start_run(client)
    assert put_part(client, run_id, 7, 1, b"x").status_code == 404
    assert finish(client, run_id, 7, 1).status_code == 404


def test_a_part_bigger_than_the_task_allows_is_refused_by_content_length(
    pig: Pig, client: TestClient
):
    run_id = start_run(client)
    start_media(client, run_id)
    refused = put_part(client, run_id, 1, 1, b"x" * 1025)
    assert refused.status_code == 413
    problem = refused.json()["errors"]["1"]
    assert problem["can_retry"] is False
    assert "1024 bytes" in problem["message"]
    assert refused.json()["media"]["stored"] == []
    run_dir = storage.run_directory(pig.config.in_progress_root, "interview", run_id)
    assert list((run_dir / "media" / "00001").iterdir()) == []


def test_a_part_bigger_than_the_task_allows_is_refused_while_streaming(
    pig: Pig, client: TestClient
):
    """No Content-Length, so the only way to know is to count. The limit is enforced
    as the bytes arrive, and nothing is left on disk."""
    run_id = start_run(client)
    start_media(client, run_id)

    def chunks():
        for _ in range(3):
            yield b"x" * 500

    refused = client.put(f"/task/interview/run/{run_id}/media/1/1", content=chunks())
    assert refused.status_code == 413
    assert refused.json()["errors"]["1"]["can_retry"] is False
    run_dir = storage.run_directory(pig.config.in_progress_root, "interview", run_id)
    assert list((run_dir / "media" / "00001").iterdir()) == []


def test_a_part_exactly_at_the_limit_is_fine(client: TestClient):
    run_id = start_run(client)
    start_media(client, run_id)
    assert put_part(client, run_id, 1, 1, b"x" * 1024).status_code == 201


def test_part_zero_is_refused(client: TestClient):
    run_id = start_run(client)
    start_media(client, run_id)
    refused = put_part(client, run_id, 1, 0, b"x")
    assert refused.status_code == 422
    assert refused.json()["errors"]["0"]["can_retry"] is False


def test_a_closed_run_refuses_all_three(client: TestClient):
    run_id = start_run(client)
    start_media(client, run_id)
    put_part(client, run_id, 1, 1, b"a")
    client.post(f"/task/interview/run/{run_id}/finalize")

    started = client.post(
        f"/task/interview/run/{run_id}/media",
        json={"event_id": "prompt4_audio", "data": {}},
    )
    assert started.status_code == 409
    assert started.json()["status"] == "finalizing"
    assert started.json()["errors"]["prompt4_audio"]["can_retry"] is True

    part = put_part(client, run_id, 1, 2, b"b")
    assert part.status_code == 409
    assert part.json()["status"] == "finalizing"
    assert part.json()["errors"]["2"]["can_retry"] is True

    finished = finish(client, run_id, 1, 1)
    assert finished.status_code == 409
    assert finished.json()["errors"]["media"]["can_retry"] is False


def test_a_start_body_is_checked_like_an_event(client: TestClient):
    run_id = start_run(client)
    url = f"/task/interview/run/{run_id}/media"
    assert client.post(url, json=["nope"]).status_code == 422
    assert client.post(url, json={"data": {}}).status_code == 422
    assert client.post(url, json={"event_id": "x"}).status_code == 422
    outside = client.post(
        url, json={"event_id": "x", "data": {}, "content_type": "video/webm"}
    )
    assert outside.status_code == 422
    assert "'data'" in outside.json()["message"]
    # A numeric event ID is what JavaScript sends for a counter; it's kept as text.
    numeric = client.post(url, json={"event_id": 12, "data": {}})
    assert numeric.status_code == 201
    assert client.get(f"/task/interview/run/{run_id}").json()["stored"] == ["12"]


def test_a_browser_can_preflight_a_part_upload(client: TestClient):
    preflight = client.options(
        "/task/interview/run/some-run/media/1/1",
        headers={
            "Origin": "https://tasks.example.edu",
            "Access-Control-Request-Method": "PUT",
        },
    )
    assert preflight.status_code == 200
    assert "PUT" in preflight.headers["access-control-allow-methods"]


# ---------------------------------------------------------------------------- sweep


def done(pig: Pig, run_id: str) -> Path:
    return storage.run_directory(pig.config.done_root, "interview", run_id)


def test_the_manifest_carries_the_media_and_hashes_every_part(
    pig: Pig, client: TestClient
):
    run_id = start_run(client)
    start_media(client, run_id)
    put_part(client, run_id, 1, 1, b"first")
    put_part(client, run_id, 1, 2, b"second")
    finish(client, run_id, 1, 2)
    # A second item the task never finished.
    client.post(
        f"/task/interview/run/{run_id}/media",
        json={"event_id": "prompt4_audio", "data": {}},
    )
    put_part(client, run_id, 2, 1, b"only")
    client.post(f"/task/interview/run/{run_id}/finalize")

    report = sweep.sweep(pig.config, pig.connection)
    assert report.finished == [run_id]
    assert report.refused == {}

    manifest = storage.read_manifest(done(pig, run_id))
    assert manifest["media"] == [
        {"media_id": 1, "event_id": "prompt3_audio", "finished": True, "parts": 2},
        {"media_id": 2, "event_id": "prompt4_audio", "finished": False},
    ]
    described = {entry["path"]: entry for entry in manifest["files"]}
    assert set(described) == {
        "events.jsonl",
        "media/00001/000001.part",
        "media/00001/000002.part",
        "media/00002/000001.part",
    }
    assert described["media/00001/000002.part"] == {
        "path": "media/00001/000002.part",
        "bytes": 6,
        "sha256": hashlib.sha256(b"second").hexdigest(),
    }


def test_the_sweep_removes_scratch_files_an_upload_left_behind(
    pig: Pig, client: TestClient
):
    run_id = start_run(client)
    start_media(client, run_id)
    put_part(client, run_id, 1, 1, b"a")
    run_dir = storage.run_directory(pig.config.in_progress_root, "interview", run_id)
    stray = run_dir / "media" / "00001" / "000002.part.deadbeef.partial"
    stray.write_bytes(b"half of a part")
    client.post(f"/task/interview/run/{run_id}/finalize")

    report = sweep.sweep(pig.config, pig.connection)
    assert report.finished == [run_id]
    assert list(done(pig, run_id).rglob("*.partial")) == []
    paths = [
        entry["path"] for entry in storage.read_manifest(done(pig, run_id))["files"]
    ]
    assert paths == ["events.jsonl", "media/00001/000001.part"]


def test_a_run_whose_part_vanished_is_reported_not_finished(
    pig: Pig, client: TestClient
):
    run_id = start_run(client)
    start_media(client, run_id)
    put_part(client, run_id, 1, 1, b"a")
    put_part(client, run_id, 1, 2, b"b")
    client.post(f"/task/interview/run/{run_id}/finalize")
    run_dir = storage.run_directory(pig.config.in_progress_root, "interview", run_id)
    storage.part_path(run_dir, 1, 2).unlink()

    report = sweep.sweep(pig.config, pig.connection)
    assert report.finished == []
    assert run_id in report.refused
    assert "media/00001/000002.part" in report.refused[run_id]
    assert not done(pig, run_id).exists()


def test_an_expired_run_keeps_its_unfinished_media(pig: Pig, client: TestClient):
    run_id = start_run(client)
    start_media(client, run_id)
    put_part(client, run_id, 1, 1, b"a")
    long_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    pig.connection.execute(
        "UPDATE runs SET started_at = ? WHERE run_id = ?", (long_ago, run_id)
    )

    report = sweep.sweep(pig.config, pig.connection)
    assert report.expired == [run_id]
    manifest = storage.read_manifest(done(pig, run_id))
    assert manifest["disposition"] == "expired"
    assert manifest["media"] == [
        {"media_id": 1, "event_id": "prompt3_audio", "finished": False}
    ]
    assert (done(pig, run_id) / "media" / "00001" / "000001.part").read_bytes() == b"a"


# ---------------------------------------------------------------------------- health


def test_health_counts_a_part_as_data_arriving(client: TestClient):
    before = client.get("/health").json()["tasks"]["interview"]
    assert before["media"] is True
    assert before["last_event_at"] is None

    run_id = start_run(client)
    start_media(client, run_id)
    # Erase the event's receipt time so only the part can account for activity.
    after_start = client.get("/health").json()["tasks"]["interview"]["last_event_at"]
    assert after_start is not None
    put_part(client, run_id, 1, 1, b"a")
    after_part = client.get("/health").json()["tasks"]["interview"]["last_event_at"]
    assert after_part >= after_start

    report = client.get("/health").json()
    assert isinstance(report["data_root_free_bytes"], int)
    assert report["tasks"]["stroop"]["media"] is False


def test_media_summary_lists_parts_in_order(pig: Pig, client: TestClient):
    run_id = start_run(client)
    start_media(client, run_id)
    put_part(client, run_id, 1, 10, b"j")
    put_part(client, run_id, 1, 9, b"i")
    item = media.get(pig.connection, run_id, 1)
    assert item is not None
    assert media.summary(pig.connection, item)["stored"] == [9, 10]
