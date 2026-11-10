"""`pigsty admit`: downloaded runs checked against their manifests and moved into
`runs/`, and nothing that's half downloaded or wrong."""

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from psych_ingestor import files, formats
from psych_ingestor.formats import RUN_FILE
from psych_ingestor.pig import sweep
from psych_ingestor.pig.config import ConfigurationError
from psych_ingestor.pig.service import Pig
from psych_ingestor.pigsty.admit import LOCK_FILE, admit
from psych_ingestor.pigsty.cli import app
from psych_ingestor.pigsty.config import load_pigsty_config

BASELINE = {"participant_id": "PPT-1003", "session": "Baseline"}


def downloaded_run(pig: Pig, root: Path, parameters: dict = BASELINE) -> Path:
    """Finish a stroop run on the web side and copy it into `{root}/incoming/stroop/`,
    as the copy from the bucket would."""
    run_id = pig.start_run("stroop", parameters)["run_id"]
    pig.store_events("stroop", run_id, {"1": {"data": {"trial": 1}}})
    pig.finalize_run("stroop", run_id)
    sweep.sweep(pig.config, pig.connection)
    incoming = root / "incoming" / "stroop" / run_id
    shutil.copytree(pig.config.done_root / "stroop" / run_id, incoming)
    return incoming


def files_under(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


# ------------------------------------------------------------------- admitting


def test_a_whole_run_is_admitted(pig: Pig, tmp_path: Path):
    incoming = downloaded_run(pig, tmp_path)
    before = files_under(incoming)

    report = admit("stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore")

    assert report.admitted == [incoming.name]
    assert report.problems == {}
    admitted = tmp_path / "datastore" / "runs" / incoming.name
    assert (admitted / "events.jsonl").read_bytes() == before["events.jsonl"]
    # Nothing in incoming/ changes.
    assert files_under(incoming) == before


def test_an_admitted_run_has_pig_run_json_and_no_manifest(pig: Pig, tmp_path: Path):
    incoming = downloaded_run(pig, tmp_path)

    admit("stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore")

    admitted = tmp_path / "datastore" / "runs" / incoming.name
    assert sorted(files_under(admitted)) == ["events.jsonl", RUN_FILE]
    described = json.loads((admitted / RUN_FILE).read_text())
    manifest = formats.read_manifest(incoming)
    assert described["type"] == "pig_run"
    assert described["run_id"] == incoming.name
    assert described["parameters"] == BASELINE
    assert described["run_number"] == manifest["run_number"]
    assert "files" not in described


def test_an_admitted_run_isnt_admitted_again(pig: Pig, tmp_path: Path):
    downloaded_run(pig, tmp_path)
    admit("stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore")

    report = admit("stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore")

    assert report.admitted == []
    assert report.already_admitted == 1


def test_a_media_run_is_admitted_with_its_parts(
    client: TestClient, pig: Pig, tmp_path: Path
):
    run_id = client.post(
        "/v1/task/interview/run", json={"parameters": {"participant_id": "10351"}}
    ).json()["run_id"]
    base = f"/v1/task/interview/run/{run_id}/media"
    client.post(base, json={"event_id": "prompt3_audio", "data": {}})
    client.put(f"{base}/1/1", content=b"first")
    client.put(f"{base}/1/2", content=b"second")
    client.post(f"{base}/1/finish", json={"parts": 2})
    client.post(f"/v1/task/interview/run/{run_id}/finalize")
    sweep.sweep(pig.config, pig.connection)
    shutil.copytree(
        pig.config.done_root / "interview" / run_id,
        tmp_path / "incoming" / "interview" / run_id,
    )

    report = admit(
        "interview", tmp_path / "incoming" / "interview", tmp_path / "datastore"
    )

    assert report.admitted == [run_id]
    admitted = files_under(tmp_path / "datastore" / "runs" / run_id)
    assert admitted["media/00001/000001.part"] == b"first"
    assert admitted["media/00001/000002.part"] == b"second"


# ------------------------------------------------------- still arriving


def test_a_run_without_its_manifest_yet_is_still_arriving(pig: Pig, tmp_path: Path):
    incoming = downloaded_run(pig, tmp_path)
    (incoming / "manifest.json").unlink()

    report = admit("stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore")

    assert report.arriving == [incoming.name]
    assert report.problems == {}
    assert list((tmp_path / "datastore" / "runs").iterdir()) == []


def test_a_run_missing_a_file_is_still_arriving(pig: Pig, tmp_path: Path):
    incoming = downloaded_run(pig, tmp_path)
    (incoming / "events.jsonl").unlink()

    report = admit("stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore")

    assert report.arriving == [incoming.name]


def test_a_run_with_a_short_file_is_still_arriving(pig: Pig, tmp_path: Path):
    incoming = downloaded_run(pig, tmp_path)
    events = incoming / "events.jsonl"
    events.write_bytes(events.read_bytes()[:5])

    report = admit("stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore")

    assert report.arriving == [incoming.name]


def test_a_run_still_arriving_is_admitted_once_its_here(pig: Pig, tmp_path: Path):
    incoming = downloaded_run(pig, tmp_path)
    manifest = (incoming / "manifest.json").read_bytes()
    (incoming / "manifest.json").unlink()
    admit("stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore")
    (incoming / "manifest.json").write_bytes(manifest)

    assert admit(
        "stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore"
    ).admitted == [incoming.name]


# ------------------------------------------------------------- problems


def test_a_file_with_the_wrong_hash_isnt_admitted(pig: Pig, tmp_path: Path):
    incoming = downloaded_run(pig, tmp_path)
    events = incoming / "events.jsonl"
    original = events.read_bytes()
    events.write_bytes(original[:-2] + b"X\n")

    report = admit("stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore")

    assert report.admitted == []
    assert "doesn't match the manifest" in only_problem(report)
    # No admitted run, and no scratch directory left behind.
    assert list((tmp_path / "datastore" / "runs").iterdir()) == []


def test_a_file_too_long_isnt_admitted(pig: Pig, tmp_path: Path):
    incoming = downloaded_run(pig, tmp_path)
    with open(incoming / "events.jsonl", "ab") as events:
        events.write(b"more\n")

    assert "bytes" in only_problem(
        admit("stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore")
    )


def test_a_file_the_manifest_doesnt_list_isnt_admitted(pig: Pig, tmp_path: Path):
    incoming = downloaded_run(pig, tmp_path)
    (incoming / "stray.txt").write_text("?")

    assert "stray.txt" in only_problem(
        admit("stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore")
    )


@pytest.mark.parametrize("path", ["../escaped", "/etc/passwd", "media//x", "./x"])
def test_a_manifest_path_outside_the_run_isnt_admitted(
    pig: Pig, tmp_path: Path, path: str
):
    incoming = downloaded_run(pig, tmp_path)
    manifest = formats.read_manifest(incoming)
    manifest["files"].append({"path": path, "bytes": 0, "sha256": "0" * 64})
    (incoming / "manifest.json").write_text(json.dumps(manifest))

    assert "isn't a path inside the run" in only_problem(
        admit("stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore")
    )
    assert not (tmp_path / "escaped").exists()


def test_a_symlinked_file_isnt_admitted(pig: Pig, tmp_path: Path):
    incoming = downloaded_run(pig, tmp_path)
    secret = tmp_path / "secret"
    secret.write_bytes((incoming / "events.jsonl").read_bytes())
    (incoming / "events.jsonl").unlink()
    (incoming / "events.jsonl").symlink_to(secret)

    assert "symbolic link" in only_problem(
        admit("stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore")
    )
    assert list((tmp_path / "datastore" / "runs").iterdir()) == []


def test_a_symlinked_directory_isnt_admitted(pig: Pig, tmp_path: Path):
    incoming = downloaded_run(pig, tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (incoming / "media").symlink_to(elsewhere)

    assert "media is a symbolic link" in only_problem(
        admit("stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore")
    )


def test_a_symlinked_run_isnt_admitted(pig: Pig, tmp_path: Path):
    incoming = downloaded_run(pig, tmp_path)
    moved = tmp_path / "moved"
    incoming.rename(moved)
    incoming.symlink_to(moved)

    assert "symbolic link" in only_problem(
        admit("stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore")
    )


def test_the_first_admit_makes_runs_durably(
    pig: Pig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`runs/` is new on the first admit, and only an entry in the datastore until the
    datastore is fsynced. Without that, a crash could take every run admitted with it."""
    downloaded_run(pig, tmp_path)
    synced: list[Path] = []
    real = files.fsync_directory

    def record(directory: Path) -> None:
        synced.append(Path(directory))
        real(directory)

    monkeypatch.setattr(files, "fsync_directory", record)

    admit("stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore")

    assert tmp_path / "datastore" in synced
    assert tmp_path / "datastore" / "runs" in synced


def test_a_run_in_the_wrong_tasks_incoming_isnt_admitted(pig: Pig, tmp_path: Path):
    incoming = downloaded_run(pig, tmp_path)
    (tmp_path / "incoming" / "balloons").mkdir()
    incoming.rename(tmp_path / "incoming" / "balloons" / incoming.name)

    assert "task stroop" in only_problem(
        admit("balloons", tmp_path / "incoming" / "balloons", tmp_path / "datastore")
    )


def test_a_task_with_nothing_in_incoming_yet_isnt_a_problem(tmp_path: Path):
    report = admit("stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore")

    assert report.admitted == []
    assert report.problems == {}


def test_two_admits_cant_work_on_one_datastore_at_once(
    pig: Pig, tmp_path: Path, locked_elsewhere
):
    downloaded_run(pig, tmp_path)
    (tmp_path / "datastore").mkdir()
    with locked_elsewhere(tmp_path / "datastore" / LOCK_FILE):
        report = admit(
            "stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore"
        )

    assert "another pigsty admit" in only_problem(report)
    assert (
        len(
            admit(
                "stroop", tmp_path / "incoming" / "stroop", tmp_path / "datastore"
            ).admitted
        )
        == 1
    )


# ------------------------------------------------------------ pigsty.toml


def test_datastore_dir_is_relative_to_the_file(tmp_path: Path):
    path = tmp_path / "pigsty.toml"
    path.write_text(TOP + sty_toml("./stroop-data"))

    loaded = load_pigsty_config(path)

    assert loaded.task["stroop"].datastore_dir == tmp_path / "stroop-data"


def test_pigsty_toml_needs_incoming(tmp_path: Path):
    path = tmp_path / "pigsty.toml"
    path.write_text(sty_toml("./stroop-data"))

    with pytest.raises(ConfigurationError, match="needs incoming_dir"):
        load_pigsty_config(path)


def test_incoming_is_relative_to_the_file(tmp_path: Path):
    path = tmp_path / "pigsty.toml"
    path.write_text(TOP + sty_toml("./stroop-data"))

    assert load_pigsty_config(path).incoming_dir == tmp_path / "incoming"


@pytest.mark.parametrize(
    ("incoming", "datastore"),
    [("./data", "./data"), ("./data", "./data/stroop"), ("./data/incoming", "./data")],
)
def test_incoming_and_a_datastore_cant_be_inside_each_other(
    tmp_path: Path, incoming: str, datastore: str
):
    path = tmp_path / "pigsty.toml"
    path.write_text(f'incoming_dir = "{incoming}"\n' + sty_toml(datastore))

    with pytest.raises(ConfigurationError, match="One is inside the other"):
        load_pigsty_config(path)


def test_two_tasks_cant_share_a_datastore(tmp_path: Path):
    path = tmp_path / "pigsty.toml"
    path.write_text(
        TOP + sty_toml("./shared") + "\n" + sty_toml("./shared", task="balloons")
    )

    with pytest.raises(ConfigurationError, match="same datastore_dir"):
        load_pigsty_config(path)


# ------------------------------------------------------------------ the CLI


def test_the_cli_admits_each_tasks_runs(pig: Pig, tmp_path: Path, capsys):
    datastore = tmp_path / "stroop-data"
    downloaded_run(pig, tmp_path)
    path = tmp_path / "pigsty.toml"
    path.write_text(TOP + sty_toml(str(datastore)))

    app(["admit", "--config", str(path)], result_action="return_value")

    assert "stroop: admitted 1 run(s)" in capsys.readouterr().out


def test_the_cli_exits_nonzero_when_a_run_isnt_admitted(pig: Pig, tmp_path: Path):
    datastore = tmp_path / "stroop-data"
    incoming = downloaded_run(pig, tmp_path)
    (incoming / "stray.txt").write_text("?")
    path = tmp_path / "pigsty.toml"
    path.write_text(TOP + sty_toml(str(datastore)))

    with pytest.raises(SystemExit) as exited:
        app(["admit", "--config", str(path)], result_action="return_value")
    assert exited.value.code == 1


def test_the_cli_refuses_an_incoming_that_isnt_there(tmp_path: Path, capsys):
    """A task's own directory in incoming can be missing, but incoming itself missing
    means the copy has never run or incoming is set wrong."""
    path = tmp_path / "pigsty.toml"
    path.write_text(TOP + sty_toml("./stroop-data"))

    with pytest.raises(SystemExit) as exited:
        app(["admit", "--config", str(path)], result_action="return_value")

    assert exited.value.code == 1
    assert "pigsty check --fix makes it" in capsys.readouterr().err


# ------------------------------------------------------------------ helpers


# The settings above the tasks, which have to come first in the file.
TOP = 'incoming_dir = "./incoming"\n'


def sty_toml(datastore: str, task: str = "stroop") -> str:
    return (
        f'[task.{task}]\ndatastore_dir = "{datastore}"\n'
        f'organized = "./organized/{task}/{{participant_id}}/{{run_number}}"\n'
    )


def only_problem(report) -> str:
    assert len(report.problems) == 1, report.problems
    return next(iter(report.problems.values()))
