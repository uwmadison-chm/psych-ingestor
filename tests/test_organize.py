"""`pigsty organize`: a readable tree built from each task's admitted runs, and the
`pigsty.toml` that tells it where things go."""

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from psych_ingestor import sweep
from psych_ingestor.admit import RUN_FILE, admit
from psych_ingestor.config import ConfigurationError, load_config
from psych_ingestor.organize import LOCK_FILE, RECORD_FILE, organize
from psych_ingestor.service import Pig
from psych_ingestor.sty import (
    PatternError,
    check_pattern,
    load_sty_config,
    split_organized,
)
from psych_ingestor.sty_cli import app

BASELINE = {"participant_id": "PPT-1003", "session": "Baseline"}
PATTERN = "{session}/{participant_id}_{run_number}"
PLACED = Path("Baseline/PPT-1003_0001")


@pytest.fixture
def datastore(tmp_path: Path) -> Path:
    return tmp_path / "stroop-data"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return tmp_path / "organized" / "stroop"


def admitted_run(pig: Pig, datastore: Path, parameters: dict = BASELINE) -> str:
    """Finish a stroop run on the web side, copy it into the datastore, and admit it."""
    run_id = pig.start_run("stroop", parameters)["run_id"]
    pig.store_events("stroop", run_id, {"1": {"data": {"trial": 1}}})
    pig.finalize_run("stroop", run_id)
    return arrive(pig, datastore, run_id)


def arrive(pig: Pig, datastore: Path, run_id: str, task: str = "stroop") -> str:
    sweep.sweep(pig.config, pig.connection)
    shutil.copytree(
        pig.config.done_root / task / run_id, datastore / "incoming" / run_id
    )
    assert admit(task, datastore).admitted == [run_id]
    return run_id


def run_file(directory: Path) -> dict:
    return json.loads((directory / RUN_FILE).read_text())


def files_under(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


# ------------------------------------------------------------------ placing runs


def test_runs_are_placed_by_the_pattern(pig: Pig, datastore: Path, tree: Path):
    first = admitted_run(pig, datastore)
    second = admitted_run(pig, datastore)
    runs = datastore / "runs"
    runs_before = files_under(runs)

    report = organize("stroop", datastore, tree, PATTERN)

    assert report.errors == {}
    assert sorted(p.run_id for p in report.placed) == sorted([first, second])
    one = tree / PLACED
    assert sorted(files_under(one)) == ["events.jsonl", RUN_FILE]
    assert run_file(one)["run_id"] == first
    assert (one / "events.jsonl").read_bytes() == (
        runs / first / "events.jsonl"
    ).read_bytes()
    assert run_file(one.with_name("PPT-1003_0002"))["run_id"] == second
    # runs/ is never touched.
    assert files_under(runs) == runs_before


def test_organizing_again_leaves_placed_runs_alone(
    pig: Pig, datastore: Path, tree: Path
):
    admitted_run(pig, datastore)
    organize("stroop", datastore, tree, PATTERN)
    admitted_run(pig, datastore, {**BASELINE, "session": "Followup"})

    report = organize("stroop", datastore, tree, PATTERN)

    assert report.errors == {}
    assert len(report.already_there) == 1
    assert [p.destination.as_posix() for p in report.placed] == [
        "Followup/PPT-1003_0001"
    ]


def test_an_expired_run_is_placed_like_any_other(pig: Pig, datastore: Path, tree: Path):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    long_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    pig.connection.execute(
        "UPDATE runs SET started_at = ? WHERE run_id = ?", (long_ago, run_id)
    )
    arrive(pig, datastore, run_id)

    report = organize("stroop", datastore, tree, PATTERN)

    assert report.errors == {}
    assert run_file(tree / PLACED)["disposition"] == "expired"


def test_dry_run_writes_nothing(pig: Pig, datastore: Path, tree: Path):
    admitted_run(pig, datastore)

    report = organize("stroop", datastore, tree, PATTERN, dry_run=True)

    assert len(report.placed) == 1
    assert not tree.exists()


def test_a_datastore_with_no_runs_directory_is_a_problem(datastore: Path, tree: Path):
    report = organize("stroop", datastore, tree, PATTERN)

    assert "pigsty admit" in only_error(report)


# ------------------------------------------------------------------- media


def test_media_directories_are_named_for_their_event_ids(
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
    # One the task never finished is copied too: organize doesn't judge the data.
    client.post(base, json={"event_id": "prompt4_audio", "data": {}})
    client.put(f"{base}/2/1", content=b"only")
    client.post(f"/v1/task/interview/run/{run_id}/finalize")
    datastore, tree = tmp_path / "interview-data", tmp_path / "interview"
    arrive(pig, datastore, run_id, task="interview")

    report = organize("interview", datastore, tree, "{participant_id}/{run_number}")

    assert report.errors == {}
    assert files_under(tree / "10351" / "0001" / "media") == {
        "prompt3_audio/000001.part": b"first",
        "prompt3_audio/000002.part": b"second",
        "prompt4_audio/000001.part": b"only",
    }


def test_a_media_event_id_that_isnt_a_safe_name_stops_the_run(
    pig: Pig, datastore: Path, tree: Path
):
    run_id = admitted_run(pig, datastore)
    edit_run_file(
        datastore,
        run_id,
        lambda r: r.update(media=[{"media_id": 1, "event_id": "a/b"}]),
    )

    report = organize("stroop", datastore, tree, PATTERN)

    assert report.placed == []
    assert "'a/b'" in only_error(report)


# --------------------------------------------------------------- refusing


def test_two_runs_in_one_place_are_both_refused(pig: Pig, datastore: Path, tree: Path):
    admitted_run(pig, datastore)
    admitted_run(pig, datastore, {**BASELINE, "session": "Followup"})
    # Leaves out session, so both are PPT-1003's first run.
    pattern = "{participant_id}/run-{run_number}"

    report = organize("stroop", datastore, tree, pattern)

    assert report.placed == []
    assert len(report.errors) == 2
    assert all("Neither is placed" in why for why in report.errors.values())
    # Only the lock, made before anything was planned.
    assert [p.name for p in tree.iterdir()] == [LOCK_FILE]


def test_values_differing_only_in_case_collide(pig: Pig, datastore: Path, tree: Path):
    admitted_run(pig, datastore)
    admitted_run(pig, datastore, {**BASELINE, "participant_id": "ppt-1003"})

    report = organize("stroop", datastore, tree, PATTERN)

    assert report.placed == []
    assert len(report.errors) == 2


def test_extra_parameters_cant_be_used_in_a_path(pig: Pig, datastore: Path, tree: Path):
    admitted_run(pig, datastore, {**BASELINE, "utm_source": "email"})

    report = organize("stroop", datastore, tree, "{utm_source}/run-{run_number}")

    assert report.placed == []
    assert "extra parameter" in only_error(report)


def test_a_hand_edited_unsafe_value_is_refused(pig: Pig, datastore: Path, tree: Path):
    run_id = admitted_run(pig, datastore)
    edit_run_file(datastore, run_id, lambda r: r["parameters"].update(session=".."))

    report = organize("stroop", datastore, tree, PATTERN)

    assert report.placed == []
    assert "'..'" in only_error(report)


def test_a_value_with_a_slash_is_refused(pig: Pig, datastore: Path, tree: Path):
    # Each part of a/b is safe, so only checking the value itself catches this.
    run_id = admitted_run(pig, datastore)
    edit_run_file(datastore, run_id, lambda r: r["parameters"].update(session="a/b"))

    report = organize("stroop", datastore, tree, PATTERN)

    assert report.placed == []
    assert "'a/b'" in only_error(report)


def test_a_symlink_in_a_run_is_refused(pig: Pig, datastore: Path, tree: Path):
    run_id = admitted_run(pig, datastore)
    secret = datastore / "secret"
    secret.write_text("private")
    (datastore / "runs" / run_id / "extra").symlink_to(secret)

    report = organize("stroop", datastore, tree, PATTERN)

    assert "symbolic link" in only_error(report)
    # Not even a scratch directory.
    assert [p.name for p in tree.iterdir()] == [LOCK_FILE]


def test_a_symlinked_run_is_refused(pig: Pig, datastore: Path, tree: Path):
    run_id = admitted_run(pig, datastore)
    moved = datastore / "moved"
    (datastore / "runs" / run_id).rename(moved)
    (datastore / "runs" / run_id).symlink_to(moved)

    report = organize("stroop", datastore, tree, PATTERN)

    assert "symbolic link" in only_error(report)
    assert report.placed == []


def test_whats_recorded_about_a_run_isnt_placed_with_it(
    pig: Pig, datastore: Path, tree: Path
):
    run_id = admitted_run(pig, datastore)
    state = datastore / "runs" / run_id / ".state"
    state.mkdir()
    (state / "purged").write_text("{}\n")

    organize("stroop", datastore, tree, PATTERN)

    assert ".state" not in {p.name for p in (tree / PLACED).iterdir()}


def test_a_run_without_pig_run_json_is_refused(pig: Pig, datastore: Path, tree: Path):
    run_id = admitted_run(pig, datastore)
    (datastore / "runs" / run_id / RUN_FILE).unlink()

    assert "pigsty admit" in only_error(organize("stroop", datastore, tree, PATTERN))


def test_a_run_of_another_task_is_refused(pig: Pig, datastore: Path, tree: Path):
    admitted_run(pig, datastore)

    report = organize("balloons", datastore, tree, PATTERN)

    assert "task stroop" in only_error(report)


def test_something_else_already_at_the_destination_is_left_alone(
    pig: Pig, datastore: Path, tree: Path
):
    admitted_run(pig, datastore)
    occupied = tree / PLACED
    occupied.mkdir(parents=True)
    (occupied / "notes.txt").write_text("mine")

    report = organize("stroop", datastore, tree, PATTERN)

    assert "already there" in only_error(report)
    assert files_under(occupied) == {"notes.txt": b"mine"}


def test_state_isnt_copied(pig: Pig, datastore: Path, tree: Path):
    run_id = admitted_run(pig, datastore)
    state = datastore / "runs" / run_id / ".state"
    state.mkdir()
    (state / "purged").write_text("")

    organize("stroop", datastore, tree, PATTERN)

    assert not (tree / PLACED / ".state").exists()


# --------------------------------------------------------------- the record


def test_each_placed_run_is_recorded(pig: Pig, datastore: Path, tree: Path):
    run_id = admitted_run(pig, datastore)

    organize("stroop", datastore, tree, PATTERN)

    lines = (tree / RECORD_FILE).read_text().splitlines()
    assert [json.loads(line) for line in lines] == [
        {"run_id": run_id, "destination": PLACED.as_posix()}
    ]


def test_a_recorded_run_isnt_read_again(pig: Pig, datastore: Path, tree: Path):
    run_id = admitted_run(pig, datastore)
    organize("stroop", datastore, tree, PATTERN)
    # Without the record, this run would now be refused for having no pig-run.json.
    (datastore / "runs" / run_id / RUN_FILE).unlink()

    report = organize("stroop", datastore, tree, PATTERN)

    assert report.errors == {}
    assert report.already_there == [run_id]


def test_a_run_in_the_tree_but_not_the_record_is_recorded(
    pig: Pig, datastore: Path, tree: Path
):
    run_id = admitted_run(pig, datastore)
    organize("stroop", datastore, tree, PATTERN)
    (tree / RECORD_FILE).unlink()

    report = organize("stroop", datastore, tree, PATTERN)

    assert report.already_there == [run_id]
    assert run_id in (tree / RECORD_FILE).read_text()


def test_a_new_run_cant_land_on_a_recorded_one(pig: Pig, datastore: Path, tree: Path):
    first = admitted_run(pig, datastore)
    pattern = "{participant_id}/run-{run_number}"
    organize("stroop", datastore, tree, pattern)
    admitted_run(pig, datastore, {**BASELINE, "session": "Followup"})

    report = organize("stroop", datastore, tree, pattern)

    assert "Neither is placed" in only_error(report)
    assert run_file(tree / "PPT-1003" / "run-0001")["run_id"] == first


def test_a_new_run_cant_land_inside_a_recorded_one(
    pig: Pig, datastore: Path, tree: Path
):
    # Within one pattern every run is the same depth, so this takes a pattern that
    # changed after a run was placed.
    first = admitted_run(pig, datastore)
    organize("stroop", datastore, tree, "{session}/{run_number}")
    admitted_run(pig, datastore, {**BASELINE, "participant_id": "PPT-2000"})

    report = organize(
        "stroop", datastore, tree, "{session}/{run_number}/{participant_id}"
    )

    assert "Neither is placed" in only_error(report)
    assert run_file(tree / "Baseline" / "0001")["run_id"] == first


def test_an_unfinished_last_line_is_cut_off(pig: Pig, datastore: Path, tree: Path):
    admitted_run(pig, datastore)
    organize("stroop", datastore, tree, PATTERN)
    with open(tree / RECORD_FILE, "a") as record:
        record.write('{"run_id": "half')
    admitted_run(pig, datastore, {**BASELINE, "session": "Followup"})

    report = organize("stroop", datastore, tree, PATTERN)

    assert report.errors == {}
    lines = (tree / RECORD_FILE).read_text().splitlines()
    assert [json.loads(line)["destination"] for line in lines] == [
        PLACED.as_posix(),
        "Followup/PPT-1003_0001",
    ]


def test_a_damaged_record_stops_organize_until_full(
    pig: Pig, datastore: Path, tree: Path
):
    admitted_run(pig, datastore)
    organize("stroop", datastore, tree, PATTERN)
    (tree / RECORD_FILE).write_text("not json\n")
    admitted_run(pig, datastore, {**BASELINE, "session": "Followup"})

    report = organize("stroop", datastore, tree, PATTERN)

    assert "--full" in only_error(report)
    assert report.placed == []

    report = organize("stroop", datastore, tree, PATTERN, full=True)

    assert report.errors == {}
    assert len(report.already_there) == 1
    assert len(report.placed) == 1
    assert len((tree / RECORD_FILE).read_text().splitlines()) == 2


def test_a_run_removed_from_the_tree_comes_back_only_with_full(
    pig: Pig, datastore: Path, tree: Path
):
    admitted_run(pig, datastore)
    organize("stroop", datastore, tree, PATTERN)
    shutil.rmtree(tree / PLACED)

    assert organize("stroop", datastore, tree, PATTERN).placed == []
    assert not (tree / PLACED).exists()

    assert len(organize("stroop", datastore, tree, PATTERN, full=True).placed) == 1
    assert (tree / PLACED / RUN_FILE).exists()


def test_full_with_no_runs_left_removes_the_record(
    pig: Pig, datastore: Path, tree: Path
):
    run_id = admitted_run(pig, datastore)
    organize("stroop", datastore, tree, PATTERN)
    shutil.rmtree(tree / PLACED)
    shutil.rmtree(datastore / "runs" / run_id)

    organize("stroop", datastore, tree, PATTERN, full=True)

    assert not (tree / RECORD_FILE).exists()


def test_two_organizes_cant_work_on_one_tree_at_once(
    pig: Pig, datastore: Path, tree: Path, locked_elsewhere
):
    admitted_run(pig, datastore)
    tree.mkdir(parents=True)
    with locked_elsewhere(tree / LOCK_FILE):
        report = organize("stroop", datastore, tree, PATTERN)

    assert "another pigsty organize" in only_error(report)
    assert report.placed == []
    assert len(organize("stroop", datastore, tree, PATTERN).placed) == 1


def test_a_dry_run_records_nothing(pig: Pig, datastore: Path, tree: Path):
    admitted_run(pig, datastore)
    organize("stroop", datastore, tree, PATTERN)
    before = (tree / RECORD_FILE).read_bytes()
    admitted_run(pig, datastore, {**BASELINE, "session": "Followup"})

    organize("stroop", datastore, tree, PATTERN, dry_run=True)
    organize("stroop", datastore, tree, PATTERN, dry_run=True, full=True)

    assert (tree / RECORD_FILE).read_bytes() == before


# ------------------------------------------------------------------- patterns


@pytest.mark.parametrize(
    "pattern",
    [
        "{task_code}/{participant_id}",  # no run number
        "{task_code}/{x.y}/run-{run_number}",
        "{task_code}/{x[0]}/run-{run_number}",
        "{task_code}/run-{run_number:04}",
        "{task_code}/run-{run_number!r}",
        "{task_code}/{{literal}}/run-{run_number}",
        "{task_code}/run-{run_number}}",
        "/abs/{task_code}/run-{run_number}",
    ],
)
def test_unusable_patterns_are_refused(pattern: str):
    with pytest.raises(PatternError):
        check_pattern(pattern)


def test_a_literal_part_must_be_a_safe_value_too(pig: Pig, datastore: Path, tree: Path):
    admitted_run(pig, datastore)

    report = organize("stroop", datastore, tree, "v1.2/run-{run_number}")

    assert "'v1.2'" in only_error(report)


def test_organized_splits_at_the_first_placeholders_directory():
    assert split_organized("/secure/my.study/stroop/{session}/{run_number}") == (
        "/secure/my.study/stroop",
        "{session}/{run_number}",
    )
    assert split_organized("./out/s-{session}/{run_number}") == (
        "./out",
        "s-{session}/{run_number}",
    )


@pytest.mark.parametrize(
    "organized",
    [
        "{session}/{run_number}",  # no root
        "/{session}/{run_number}",  # the root would be /
        "/secure/stroop",  # no placeholders
        "/secure/stroop/{session}",  # no run number
        "/sec}ure/{session}/{run_number}",
    ],
)
def test_unusable_organized_settings_are_refused(organized: str):
    with pytest.raises(PatternError):
        split_organized(organized)


# ------------------------------------------------------------- pigsty.toml


STY = """
[task.stroop]
datastore_base = "./stroop-data"
organized = "./organized/stroop/{session}/{participant_id}_{run_number}"
"""


def test_pigsty_paths_are_relative_to_the_file(tmp_path: Path):
    path = tmp_path / "pigsty.toml"
    path.write_text(STY)

    stroop = load_sty_config(path).task["stroop"]

    assert stroop.datastore_base == tmp_path / "stroop-data"
    assert stroop.organized_root == tmp_path / "organized" / "stroop"
    assert stroop.pattern == "{session}/{participant_id}_{run_number}"


def test_pigsty_with_a_bad_pattern_is_refused(tmp_path: Path):
    path = tmp_path / "pigsty.toml"
    path.write_text(STY.replace("_{run_number}", ""))

    with pytest.raises(ConfigurationError, match="run_number"):
        load_sty_config(path)


@pytest.mark.parametrize(
    "balloons",
    [
        "./organized/stroop/{participant_id}/{run_number}",  # the same root
        "./organized/stroop/balloons/{participant_id}/{run_number}",  # inside it
        "./organized/{participant_id}/{run_number}",  # around it
    ],
)
def test_two_tasks_cant_share_an_organized_root(tmp_path: Path, balloons: str):
    path = tmp_path / "pigsty.toml"
    path.write_text(
        STY + '\n[task.balloons]\ndatastore_base = "./balloons-data"\n'
        f'organized = "{balloons}"\n'
    )

    with pytest.raises(ConfigurationError, match="directory of its own"):
        load_sty_config(path)


def test_two_tasks_can_have_neighboring_organized_roots(tmp_path: Path):
    path = tmp_path / "pigsty.toml"
    path.write_text(
        STY + '\n[task.balloons]\ndatastore_base = "./balloons-data"\n'
        'organized = "./organized/stroop-2/{participant_id}/{run_number}"\n'
    )

    assert len(load_sty_config(path).task) == 2


def test_pig_says_when_its_handed_pigsty(tmp_path: Path):
    path = tmp_path / "pig.toml"
    path.write_text(STY)

    with pytest.raises(ConfigurationError, match="looks like pigsty.toml"):
        load_config(path)


def test_pigsty_says_when_its_handed_pig_toml(config_path: Path):
    with pytest.raises(ConfigurationError, match="looks like pig.toml"):
        load_sty_config(config_path)


# ------------------------------------------------------------------- the CLI


def test_the_cli_organizes_each_task(pig: Pig, tmp_path: Path, capsys):
    admitted_run(pig, tmp_path / "stroop-data")
    sty = tmp_path / "pigsty.toml"
    sty.write_text(STY)

    app(["organize", "--config", str(sty)], result_action="return_value")

    assert "stroop: placed 1 run(s)" in capsys.readouterr().out
    assert (tmp_path / "organized" / "stroop" / PLACED / RUN_FILE).is_file()


def test_the_cli_dry_run_says_where_runs_would_go(pig: Pig, tmp_path: Path, capsys):
    admitted_run(pig, tmp_path / "stroop-data")
    sty = tmp_path / "pigsty.toml"
    sty.write_text(STY)

    app(["organize", "--config", str(sty), "--dry-run"], result_action="return_value")

    out = capsys.readouterr().out
    assert "stroop: would place 1 run(s)" in out
    assert str(tmp_path / "organized" / "stroop" / PLACED) in out
    assert not (tmp_path / "organized").exists()


def test_the_cli_exits_nonzero_when_a_run_isnt_placed(pig: Pig, tmp_path: Path):
    admitted_run(pig, tmp_path / "stroop-data")
    admitted_run(
        pig, tmp_path / "stroop-data", {**BASELINE, "participant_id": "ppt-1003"}
    )
    sty = tmp_path / "pigsty.toml"
    sty.write_text(STY)

    with pytest.raises(SystemExit) as exited:
        app(["organize", "--config", str(sty)], result_action="return_value")
    assert exited.value.code == 1


def test_pigsty_checks_pigsty_toml(tmp_path: Path, capsys):
    sty = tmp_path / "pigsty.toml"
    sty.write_text(STY)

    app(["check", "--config", str(sty)], result_action="return_value")

    out = capsys.readouterr().out
    assert "Configuration looks good: 1 task(s)" in out
    assert "{session}/{participant_id}_{run_number}" in out


def test_the_example_pigsty_loads():
    example = Path(__file__).parent.parent / "pigsty.example.toml"

    loaded = load_sty_config(example)

    assert "stroop" in loaded.task


# ------------------------------------------------------------------- helpers


def edit_run_file(datastore: Path, run_id: str, change) -> None:
    path = datastore / "runs" / run_id / RUN_FILE
    described = json.loads(path.read_text())
    change(described)
    path.write_text(json.dumps(described))


def only_error(report) -> str:
    assert len(report.errors) == 1, report.errors
    return next(iter(report.errors.values()))
