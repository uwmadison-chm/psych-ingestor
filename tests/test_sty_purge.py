"""`pigsty purge`: asking the web server to delete runs this side has admitted."""

import json
import shutil
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from psych_ingestor import purge, runs, storage, sweep
from psych_ingestor.admit import admit
from psych_ingestor.service import Pig
from psych_ingestor.sty import load_sty_config
from psych_ingestor.sty_cli import app

BASELINE = {"participant_id": "PPT-1003", "session": "Baseline"}


def through(client: TestClient) -> purge.Sender:
    """A Sender that hands each request to the test web server."""

    def send(path: str, body: dict) -> tuple[int, dict]:
        reply = client.post(path, json=body)
        return reply.status_code, reply.json()

    return send


def downloaded_run(pig: Pig, root: Path, parameters: dict = BASELINE) -> str:
    """Finish a stroop run on the web side and copy it into `{root}/incoming/stroop/`,
    as the copy from the bucket would."""
    run_id = pig.start_run("stroop", parameters)["run_id"]
    pig.store_events("stroop", run_id, {"1": {"data": {"trial": 1}}})
    pig.finalize_run("stroop", run_id)
    sweep.sweep(pig.config, pig.connection)
    shutil.copytree(done(pig, run_id), root / "incoming" / "stroop" / run_id)
    return run_id


def admitted_run(pig: Pig, root: Path, parameters: dict = BASELINE) -> str:
    """Download a run into `{root}/incoming/` and admit it into `{root}/stroop-data/`."""
    run_id = downloaded_run(pig, root, parameters)
    report = admit("stroop", root / "incoming" / "stroop", root / "stroop-data")
    assert report.admitted == [run_id]
    return run_id


def done(pig: Pig, run_id: str) -> Path:
    return storage.run_directory(pig.config.done_root, "stroop", run_id)


def purged_file(root: Path, run_id: str) -> Path:
    return root / "stroop-data" / "runs" / run_id / ".state" / "purged"


# ------------------------------------------------------------------- purging


def test_an_admitted_run_is_purged(pig: Pig, client: TestClient, tmp_path: Path):
    run_id = admitted_run(pig, tmp_path)

    report = purge.purge_runs(
        "stroop",
        tmp_path / "incoming" / "stroop",
        tmp_path / "stroop-data",
        through(client),
    )

    assert report.purged == [run_id]
    assert report.problems == {}
    assert not done(pig, run_id).exists()
    assert runs.get(pig.connection, run_id).parameters is None
    assert "purged_at" in json.loads(purged_file(tmp_path, run_id).read_text())


def test_a_run_not_admitted_isnt_purged(pig: Pig, client: TestClient, tmp_path: Path):
    run_id = downloaded_run(pig, tmp_path)
    (tmp_path / "stroop-data" / "runs").mkdir(parents=True)

    report = purge.purge_runs(
        "stroop",
        tmp_path / "incoming" / "stroop",
        tmp_path / "stroop-data",
        through(client),
    )

    assert report.purged == []
    assert done(pig, run_id).is_dir()


def test_a_directory_in_runs_without_a_run_file_isnt_purged(
    pig: Pig, client: TestClient, tmp_path: Path
):
    """Only admit's word counts. A directory copied into runs/ by hand doesn't."""
    run_id = downloaded_run(pig, tmp_path)
    shutil.copytree(
        tmp_path / "incoming" / "stroop" / run_id,
        tmp_path / "stroop-data" / "runs" / run_id,
    )

    report = purge.purge_runs(
        "stroop",
        tmp_path / "incoming" / "stroop",
        tmp_path / "stroop-data",
        through(client),
    )

    assert report.purged == []
    assert (
        "pig-run.json"
        in report.problems[str(tmp_path / "stroop-data" / "runs" / run_id)]
    )
    assert done(pig, run_id).is_dir()
    assert not purged_file(tmp_path, run_id).exists()


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": "someone-else"},
        {"task_code": "flanker"},
        {"type": "something_else"},
    ],
)
def test_a_run_file_for_another_run_isnt_purged(
    pig: Pig, client: TestClient, tmp_path: Path, change: dict
):
    run_id = admitted_run(pig, tmp_path)
    run_file = tmp_path / "stroop-data" / "runs" / run_id / "pig-run.json"
    run_file.write_text(json.dumps({**json.loads(run_file.read_text()), **change}))

    report = purge.purge_runs(
        "stroop",
        tmp_path / "incoming" / "stroop",
        tmp_path / "stroop-data",
        through(client),
    )

    assert report.purged == []
    assert (
        "doesn't describe"
        in report.problems[str(tmp_path / "stroop-data" / "runs" / run_id)]
    )
    assert done(pig, run_id).is_dir()


def test_an_unreadable_run_file_isnt_purged(
    pig: Pig, client: TestClient, tmp_path: Path
):
    run_id = admitted_run(pig, tmp_path)
    (tmp_path / "stroop-data" / "runs" / run_id / "pig-run.json").write_text(
        "{not json"
    )

    report = purge.purge_runs(
        "stroop",
        tmp_path / "incoming" / "stroop",
        tmp_path / "stroop-data",
        through(client),
    )

    assert report.purged == []
    assert (
        "can't be read"
        in report.problems[str(tmp_path / "stroop-data" / "runs" / run_id)]
    )
    assert done(pig, run_id).is_dir()


def test_a_purged_run_isnt_asked_about_again(
    pig: Pig, client: TestClient, tmp_path: Path
):
    admitted_run(pig, tmp_path)
    purge.purge_runs(
        "stroop",
        tmp_path / "incoming" / "stroop",
        tmp_path / "stroop-data",
        through(client),
    )

    def refuse(path: str, body: dict) -> tuple[int, dict]:
        raise AssertionError("asked again")

    report = purge.purge_runs(
        "stroop", tmp_path / "incoming" / "stroop", tmp_path / "stroop-data", refuse
    )

    assert report.already_purged == 1
    assert report.purged == []


def test_the_incoming_copy_is_what_gets_hashed(
    pig: Pig, client: TestClient, tmp_path: Path
):
    """If incoming/ doesn't match the web server, nothing is purged, whatever runs/ has."""
    run_id = admitted_run(pig, tmp_path)
    (tmp_path / "incoming" / "stroop" / run_id / "events.jsonl").write_text(
        "not the same\n"
    )

    report = purge.purge_runs(
        "stroop",
        tmp_path / "incoming" / "stroop",
        tmp_path / "stroop-data",
        through(client),
    )

    assert report.purged == []
    assert (
        "events.jsonl"
        in report.problems[str(tmp_path / "incoming" / "stroop" / run_id)]
    )
    assert done(pig, run_id).is_dir()
    assert not purged_file(tmp_path, run_id).exists()


def test_a_partial_incoming_copy_purges_nothing(
    pig: Pig, client: TestClient, tmp_path: Path
):
    run_id = admitted_run(pig, tmp_path)
    (tmp_path / "incoming" / "stroop" / run_id / "events.jsonl").unlink()

    report = purge.purge_runs(
        "stroop",
        tmp_path / "incoming" / "stroop",
        tmp_path / "stroop-data",
        through(client),
    )

    assert list(report.problems) == [str(tmp_path / "incoming" / "stroop" / run_id)]
    assert done(pig, run_id).is_dir()


def test_a_run_that_left_incoming_is_left_alone(
    pig: Pig, client: TestClient, tmp_path: Path
):
    run_id = admitted_run(pig, tmp_path)
    shutil.rmtree(tmp_path / "incoming" / "stroop" / run_id)

    report = purge.purge_runs(
        "stroop",
        tmp_path / "incoming" / "stroop",
        tmp_path / "stroop-data",
        through(client),
    )

    assert report == purge.PurgeReport()


def test_one_refusal_doesnt_stop_the_rest(pig: Pig, client: TestClient, tmp_path: Path):
    bad = admitted_run(pig, tmp_path)
    good = admitted_run(pig, tmp_path, {"participant_id": "PPT-2000", "session": "B"})
    (tmp_path / "incoming" / "stroop" / bad / "events.jsonl").write_text("changed\n")

    report = purge.purge_runs(
        "stroop",
        tmp_path / "incoming" / "stroop",
        tmp_path / "stroop-data",
        through(client),
    )

    assert report.purged == [good]
    assert list(report.problems) == [str(tmp_path / "incoming" / "stroop" / bad)]


def test_a_symbolic_link_in_incoming_is_refused(
    pig: Pig, client: TestClient, tmp_path: Path
):
    run_id = admitted_run(pig, tmp_path)
    (tmp_path / "secret").write_text("key")
    (tmp_path / "incoming" / "stroop" / run_id / "extra").symlink_to(
        tmp_path / "secret"
    )

    report = purge.purge_runs(
        "stroop",
        tmp_path / "incoming" / "stroop",
        tmp_path / "stroop-data",
        through(client),
    )

    assert (
        "symbolic link"
        in report.problems[str(tmp_path / "incoming" / "stroop" / run_id)]
    )
    assert done(pig, run_id).is_dir()


def test_a_symlinked_run_in_incoming_is_reported(
    pig: Pig, client: TestClient, tmp_path: Path
):
    run_id = admitted_run(pig, tmp_path)
    incoming = tmp_path / "incoming" / "stroop" / run_id
    incoming.rename(tmp_path / "moved")
    incoming.symlink_to(tmp_path / "moved")

    report = purge.purge_runs(
        "stroop",
        tmp_path / "incoming" / "stroop",
        tmp_path / "stroop-data",
        through(client),
    )

    assert "symbolic link" in report.problems[str(incoming)]
    assert report.purged == []
    assert done(pig, run_id).is_dir()


def test_a_symlinked_admitted_run_is_reported(
    pig: Pig, client: TestClient, tmp_path: Path
):
    run_id = admitted_run(pig, tmp_path)
    admitted = tmp_path / "stroop-data" / "runs" / run_id
    admitted.rename(tmp_path / "moved")
    admitted.symlink_to(tmp_path / "moved")

    report = purge.purge_runs(
        "stroop",
        tmp_path / "incoming" / "stroop",
        tmp_path / "stroop-data",
        through(client),
    )

    assert "symbolic link" in report.problems[str(admitted)]
    assert report.purged == []
    assert done(pig, run_id).is_dir()
    assert not (tmp_path / "moved" / ".state").exists()


def test_an_unreachable_web_server_stops_the_task(pig: Pig, tmp_path: Path):
    first = admitted_run(pig, tmp_path)
    admitted_run(pig, tmp_path, {"participant_id": "PPT-2000", "session": "B"})

    report = purge.purge_runs(
        "stroop",
        tmp_path / "incoming" / "stroop",
        tmp_path / "stroop-data",
        purge.http_sender(closed_port()),
    )

    assert report.purged == []
    assert len(report.problems) == 1
    assert "couldn't reach Pig" in next(iter(report.problems.values()))
    assert done(pig, first).is_dir()


def test_on_one_machine_incoming_can_be_pigs_own_done(
    pig: Pig, client: TestClient, tmp_path: Path
):
    """With Pig and pigsty on one machine there's no bucket, and incoming is `done/`:
    nothing is copied, and a purged run leaves incoming the moment Pig deletes it."""
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": {"data": {"trial": 1}}})
    pig.finalize_run("stroop", run_id)
    sweep.sweep(pig.config, pig.connection)
    incoming = pig.config.done_root / "stroop"
    datastore = tmp_path / "stroop-data"

    assert admit("stroop", incoming, datastore).admitted == [run_id]
    report = purge.purge_runs("stroop", incoming, datastore, through(client))

    assert report.purged == [run_id]
    assert not (incoming / run_id).exists()
    assert (datastore / "runs" / run_id / "events.jsonl").exists()
    again = purge.purge_runs("stroop", incoming, datastore, through(client))
    assert again.purged == [] and again.problems == {}


def closed_port() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


# ------------------------------------------------------------------ the config


def test_pig_url_has_to_be_a_web_address(tmp_path: Path):
    path = tmp_path / "pigsty.toml"
    path.write_text('pig_url = "pig.example.edu"\n' + sty_task(tmp_path))

    with pytest.raises(Exception, match="isn't a web address"):
        load_sty_config(path)


# ------------------------------------------------------------------ the CLI


def test_the_cli_needs_a_pig_url(tmp_path: Path, capsys):
    path = tmp_path / "pigsty.toml"
    path.write_text(sty_task(tmp_path))

    with pytest.raises(SystemExit) as exited:
        app(["purge", "--config", str(path)], result_action="return_value")

    assert exited.value.code == 1
    assert "no pig_url" in capsys.readouterr().err


def test_the_cli_exits_nonzero_when_pig_cant_be_reached(pig: Pig, tmp_path: Path):
    admitted_run(pig, tmp_path)
    path = tmp_path / "pigsty.toml"
    path.write_text(f'pig_url = "{closed_port()}"\n' + sty_task(tmp_path))

    with pytest.raises(SystemExit) as exited:
        app(["purge", "--config", str(path)], result_action="return_value")
    assert exited.value.code == 1


# ------------------------------------------------------------------ helpers


def sty_task(tmp_path: Path) -> str:
    return (
        f'incoming_dir = "{tmp_path / "incoming"}"\n'
        "[task.stroop]\n"
        f'datastore_base = "{tmp_path / "stroop-data"}"\n'
        f'organized = "{tmp_path / "organized"}/{{participant_id}}_{{run_number}}"\n'
    )
