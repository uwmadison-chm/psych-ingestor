"""Purging a finished run from the web server, once the secure side has a copy."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from psych_ingestor import db, purge, runs, storage, sweep
from psych_ingestor.service import Pig

BASELINE = {"participant_id": "PPT-1003", "session": "Baseline"}


def finished_run(pig: Pig, parameters: dict = BASELINE) -> str:
    run_id = pig.start_run("stroop", parameters)["run_id"]
    pig.store_events("stroop", run_id, {"1": {"data": {"trial": 1}}})
    pig.finalize_run("stroop", run_id)
    sweep.sweep(pig.config, pig.connection)
    return run_id


def done(pig: Pig, run_id: str) -> Path:
    return storage.run_directory(pig.config.done_root, "stroop", run_id)


def purging(pig: Pig, run_id: str) -> Path:
    return storage.run_directory(pig.config.purging_root, "stroop", run_id)


def secure_copy(pig: Pig, run_id: str, tmp_path: Path) -> Path:
    """What the secure side would have: a copy of the run as it was in done/."""
    copy = tmp_path / "incoming" / run_id
    shutil.copytree(done(pig, run_id), copy)
    return copy


def ask(client: TestClient, run_id: str, hashes: dict, task: str = "stroop"):
    return client.post(
        f"/task/{task}/run/{run_id}/purge", json={"purge_hashes": hashes}
    )


# ------------------------------------------------------------------- the hashes


def test_a_purge_hash_is_the_sha256_of_the_prefix_and_the_bytes(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"event_id":"1"}\n')

    expected = hashlib.sha256(b'pig-purge\n{"event_id":"1"}\n').hexdigest()
    assert purge.purge_hash(path) == expected
    # Not the hash the manifest lists, which is the whole point.
    assert expected != hashlib.sha256(path.read_bytes()).hexdigest()


def test_every_file_is_hashed_manifest_included(pig: Pig):
    run_id = finished_run(pig)

    assert sorted(purge.purge_hashes(done(pig, run_id))) == [
        "events.jsonl",
        "manifest.json",
    ]


def test_hashing_refuses_a_symbolic_link(tmp_path: Path):
    run = tmp_path / "run"
    run.mkdir()
    (tmp_path / "secret").write_text("key")
    (run / "events.jsonl").symlink_to(tmp_path / "secret")

    with pytest.raises(purge.PurgeProblem):
        purge.purge_hashes(run)


# ------------------------------------------------------------------- purging


def test_matching_hashes_purge_the_run(pig: Pig, client: TestClient, tmp_path: Path):
    run_id = finished_run(pig)
    copy = secure_copy(pig, run_id, tmp_path)

    reply = ask(client, run_id, purge.purge_hashes(copy))

    assert reply.status_code == 200
    assert reply.json() == {"status": "purged"}
    assert not done(pig, run_id).exists()
    assert not purging(pig, run_id).exists()


def test_purge_forgets_the_identifiers_and_keeps_the_counter(
    pig: Pig, client: TestClient, tmp_path: Path
):
    run_id = finished_run(pig)
    ask(client, run_id, purge.purge_hashes(secure_copy(pig, run_id, tmp_path)))

    run = runs.get(pig.connection, run_id)
    assert run is not None
    assert run.parameters is None
    assert run.extra_parameters is None
    assert run.api_status == "complete"
    assert run.run_number == 1

    # The same participant and session coming back get the next number, not 1 again.
    again = pig.start_run("stroop", BASELINE)
    assert again["run_number"] == 2


def test_the_identifiers_arent_left_in_the_database_file(
    pig: Pig, client: TestClient, tmp_path: Path
):
    # Another run beside it, so the deleted row shares a page with one that stays.
    finished_run(pig, {"participant_id": "PPT-2000", "session": "Baseline"})
    run_id = finished_run(pig, {**BASELINE, "utm_source": "find-me-later"})
    # Into the database file, as it would be for any run older than a few minutes.
    db.checkpoint(pig.connection)
    assert b"find-me-later" in pig.config.database.read_bytes()

    ask(client, run_id, purge.purge_hashes(secure_copy(pig, run_id, tmp_path)))
    pig.connection.close()

    database = pig.config.database
    files = [database, database.with_name(database.name + "-wal")]
    for path in files:
        if path.exists():
            assert b"find-me-later" not in path.read_bytes()


def test_purging_twice_is_fine(pig: Pig, client: TestClient, tmp_path: Path):
    run_id = finished_run(pig)
    hashes = purge.purge_hashes(secure_copy(pig, run_id, tmp_path))
    ask(client, run_id, hashes)

    reply = ask(client, run_id, hashes)

    assert reply.status_code == 200
    assert reply.json() == {"status": "purged"}


def test_a_copy_missing_a_file_purges_nothing(
    pig: Pig, client: TestClient, tmp_path: Path
):
    run_id = finished_run(pig)
    copy = secure_copy(pig, run_id, tmp_path)
    (copy / "events.jsonl").unlink()

    reply = ask(client, run_id, purge.purge_hashes(copy))

    assert reply.status_code == 409
    assert "events.jsonl" in reply.json()["message"]
    assert done(pig, run_id).is_dir()
    assert runs.get(pig.connection, run_id).parameters == BASELINE


def test_a_copy_that_differs_purges_nothing(
    pig: Pig, client: TestClient, tmp_path: Path
):
    run_id = finished_run(pig)
    copy = secure_copy(pig, run_id, tmp_path)
    with open(copy / "events.jsonl", "a") as events:
        events.write('{"event_id":"2"}\n')

    reply = ask(client, run_id, purge.purge_hashes(copy))

    assert reply.status_code == 409
    assert "events.jsonl" in reply.json()["message"]
    assert done(pig, run_id).is_dir()


def test_a_copy_with_an_extra_file_purges_nothing(
    pig: Pig, client: TestClient, tmp_path: Path
):
    run_id = finished_run(pig)
    copy = secure_copy(pig, run_id, tmp_path)
    (copy / "notes.txt").write_text("mine")

    reply = ask(client, run_id, purge.purge_hashes(copy))

    assert reply.status_code == 409
    assert "notes.txt" in reply.json()["message"]
    assert done(pig, run_id).is_dir()


def test_the_manifests_hashes_purge_nothing(pig: Pig, client: TestClient):
    """Someone holding only the manifest can't purge a run they've never seen."""
    run_id = finished_run(pig)
    manifest_path = done(pig, run_id) / storage.MANIFEST_FILE
    manifest = storage.read_manifest(done(pig, run_id))
    hashes = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    hashes["manifest.json"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    reply = ask(client, run_id, hashes)

    assert reply.status_code == 409
    assert done(pig, run_id).is_dir()


def test_a_run_not_finished_yet_cant_be_purged(pig: Pig, client: TestClient):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.finalize_run("stroop", run_id)

    reply = ask(client, run_id, {"events.jsonl": "0" * 64})

    assert reply.status_code == 409
    assert "isn't finished" in reply.json()["message"]


def test_an_unknown_run_is_a_404(client: TestClient):
    reply = ask(client, "9f3c1a7e-0000-0000-0000-000000000000", {"x": "y"})
    assert reply.status_code == 404


def test_a_run_asked_for_under_another_task_is_a_404(
    pig: Pig, client: TestClient, tmp_path: Path
):
    run_id = finished_run(pig)
    hashes = purge.purge_hashes(secure_copy(pig, run_id, tmp_path))

    assert ask(client, run_id, hashes, task="balloons").status_code == 404
    assert done(pig, run_id).is_dir()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"purge_hashes": {}},
        {"purge_hashes": ["events.jsonl"]},
        {"purge_hashes": {"a": 1}},
        [],
    ],
)
def test_a_malformed_request_is_a_422(pig: Pig, client: TestClient, body):
    run_id = finished_run(pig)

    reply = client.post(f"/task/stroop/run/{run_id}/purge", json=body)

    assert reply.status_code == 422
    assert done(pig, run_id).is_dir()


def test_a_task_taken_out_of_the_configuration_can_still_be_purged(
    pig: Pig, client: TestClient, config_path: Path, tmp_path: Path
):
    run_id = finished_run(pig)
    hashes = purge.purge_hashes(secure_copy(pig, run_id, tmp_path))
    text = config_path.read_text()
    config_path.write_text(text.replace("[task.stroop]", "[task.stroop_gone]"))

    reply = ask(client, run_id, hashes)

    assert reply.status_code == 200
    assert not done(pig, run_id).exists()


def test_a_run_missing_from_done_that_pig_didnt_purge_is_reported(
    pig: Pig, client: TestClient, tmp_path: Path
):
    run_id = finished_run(pig)
    hashes = purge.purge_hashes(secure_copy(pig, run_id, tmp_path))
    shutil.rmtree(done(pig, run_id))

    reply = ask(client, run_id, hashes)

    assert reply.status_code == 409
    assert "didn't purge it" in reply.json()["message"]
    assert runs.get(pig.connection, run_id).parameters == BASELINE


# ------------------------------------------------------------ interrupted purges


def interrupted_purge(pig: Pig) -> str:
    """A run Pig moved into purging/ and then stopped before deleting."""
    run_id = finished_run(pig)
    storage.move_directory(done(pig, run_id), purging(pig, run_id))
    return run_id


def test_a_retried_request_finishes_an_interrupted_purge(
    pig: Pig, client: TestClient, tmp_path: Path
):
    run_id = finished_run(pig)
    hashes = purge.purge_hashes(secure_copy(pig, run_id, tmp_path))
    storage.move_directory(done(pig, run_id), purging(pig, run_id))

    reply = ask(client, run_id, hashes)

    assert reply.status_code == 200
    assert not purging(pig, run_id).exists()
    assert runs.get(pig.connection, run_id).parameters is None


def test_the_sweep_finishes_an_interrupted_purge(pig: Pig):
    run_id = interrupted_purge(pig)

    report = sweep.sweep(pig.config, pig.connection)

    assert report.purged == [run_id]
    assert not purging(pig, run_id).exists()
    assert runs.get(pig.connection, run_id).parameters is None


def test_a_sweep_with_nothing_to_purge_reports_nothing(pig: Pig):
    finished_run(pig)
    assert sweep.sweep(pig.config, pig.connection).purged == []


# ------------------------------------------------------------------- pig runs


def test_pig_runs_lists_a_purged_run(
    pig: Pig, client: TestClient, config_path: Path, tmp_path: Path, capsys
):
    from psych_ingestor import cli

    run_id = finished_run(pig)
    ask(client, run_id, purge.purge_hashes(secure_copy(pig, run_id, tmp_path)))

    cli.runs(config=config_path)

    line = capsys.readouterr().out.strip()
    assert run_id in line
    assert "(purged)" in line
    assert "PPT-1003" not in line


def test_the_manifest_isnt_needed_by_the_server_to_compare(
    pig: Pig, client: TestClient, tmp_path: Path
):
    """The server hashes its own files; a manifest edited after sweep changes the
    manifest's hash, and that's caught like any other file."""
    run_id = finished_run(pig)
    copy = secure_copy(pig, run_id, tmp_path)
    manifest = json.loads((copy / "manifest.json").read_text())
    manifest["run_number"] = 7
    (copy / "manifest.json").write_text(json.dumps(manifest))

    reply = ask(client, run_id, purge.purge_hashes(copy))

    assert reply.status_code == 409
    assert "manifest.json" in reply.json()["message"]
