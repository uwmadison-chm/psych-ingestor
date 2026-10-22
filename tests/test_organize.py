"""`pig organize`: a readable tree built from finished run directories, and the
`pigsty.toml` that tells it where things go."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from psych_ingestor import storage, sweep
from psych_ingestor.cli import app
from psych_ingestor.config import ConfigurationError, load_config
from psych_ingestor.organize import organize
from psych_ingestor.service import Pig
from psych_ingestor.sty import PatternError, check_pattern, load_sty_config

BASELINE = {"participant_id": "PPT-1003", "session": "Baseline"}


def finished_run(pig: Pig, parameters: dict, task: str = "stroop") -> str:
    """Start a run, send an event, finalize it, and sweep it into done/."""
    run_id = pig.start_run(task, parameters)["run_id"]
    pig.store_events(task, run_id, {"1": {"data": {"trial": 1}}})
    pig.finalize_run(task, run_id)
    assert sweep.sweep(pig.config, pig.connection).finished == [run_id]
    return run_id


def files_under(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


# ------------------------------------------------------------------ placing runs


def test_the_default_pattern_is_task_run_key_then_run_number(pig: Pig, tmp_path: Path):
    first = finished_run(pig, BASELINE)
    second = finished_run(pig, BASELINE)
    store, tree = pig.config.done_root, tmp_path / "organized"
    store_before = files_under(store)

    report = organize(store, tree, {})

    assert report.errors == {}
    assert report.warnings == []
    assert sorted(p.run_id for p in report.placed) == sorted([first, second])
    one = tree / "stroop" / "PPT-1003" / "Baseline" / "run-0001"
    assert storage.read_manifest(one)["run_id"] == first
    assert (one / "events.jsonl").read_bytes() == (
        store / "stroop" / first / "events.jsonl"
    ).read_bytes()
    assert storage.read_manifest(one.with_name("run-0002"))["run_id"] == second
    # The store is never touched.
    assert files_under(store) == store_before


def test_organizing_again_leaves_placed_runs_alone(pig: Pig, tmp_path: Path):
    finished_run(pig, BASELINE)
    tree = tmp_path / "organized"
    organize(pig.config.done_root, tree, {})
    finished_run(pig, {**BASELINE, "session": "Followup"})

    report = organize(pig.config.done_root, tree, {})

    assert report.errors == {}
    assert len(report.already_there) == 1
    assert [p.destination.as_posix() for p in report.placed] == [
        "stroop/PPT-1003/Followup/run-0001"
    ]


def test_an_expired_run_is_placed_like_any_other(pig: Pig, tmp_path: Path):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    long_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    pig.connection.execute(
        "UPDATE runs SET started_at = ? WHERE run_id = ?", (long_ago, run_id)
    )
    sweep.sweep(pig.config, pig.connection)

    report = organize(pig.config.done_root, tmp_path / "organized", {})

    assert report.errors == {}
    placed = tmp_path / "organized" / "stroop" / "PPT-1003" / "Baseline" / "run-0001"
    assert storage.read_manifest(placed)["disposition"] == "expired"


def test_a_pattern_can_group_tasks_under_a_literal_prefix(pig: Pig, tmp_path: Path):
    finished_run(pig, BASELINE)
    pattern = "lucida/{task_code}/{participant_id}/{session}/run-{run_number}"

    report = organize(pig.config.done_root, tmp_path / "tree", {"stroop": pattern})

    assert [p.destination.as_posix() for p in report.placed] == [
        "lucida/stroop/PPT-1003/Baseline/run-0001"
    ]


def test_dry_run_writes_nothing(pig: Pig, tmp_path: Path):
    finished_run(pig, BASELINE)

    report = organize(pig.config.done_root, tmp_path / "tree", {}, dry_run=True)

    assert len(report.placed) == 1
    assert not (tmp_path / "tree").exists()


# ------------------------------------------------------------------- media


def test_media_directories_are_named_for_their_event_ids(
    client: TestClient, pig: Pig, tmp_path: Path
):
    run_id = client.post(
        "/task/interview/run", json={"participant_id": "10351"}
    ).json()["run_id"]
    base = f"/task/interview/run/{run_id}/media"
    client.post(base, json={"event_id": "prompt3_audio", "data": {}})
    client.put(f"{base}/1/1", content=b"first")
    client.put(f"{base}/1/2", content=b"second")
    client.post(f"{base}/1/finish", json={"parts": 2})
    # One the task never finished is copied too: organize doesn't judge the data.
    client.post(base, json={"event_id": "prompt4_audio", "data": {}})
    client.put(f"{base}/2/1", content=b"only")
    client.post(f"/task/interview/run/{run_id}/finalize")
    sweep.sweep(pig.config, pig.connection)

    report = organize(pig.config.done_root, tmp_path / "tree", {})

    assert report.errors == {}
    assert report.warnings == []
    placed = tmp_path / "tree" / "interview" / "10351" / "run-0001"
    assert files_under(placed / "media") == {
        "prompt3_audio/000001.part": b"first",
        "prompt3_audio/000002.part": b"second",
        "prompt4_audio/000001.part": b"only",
    }


def test_a_media_event_id_that_isnt_a_safe_name_stops_the_run(pig: Pig, tmp_path: Path):
    run_id = finished_run(pig, BASELINE)
    edit_manifest(
        pig, run_id, lambda m: m.update(media=[{"media_id": 1, "event_id": "a/b"}])
    )

    report = organize(pig.config.done_root, tmp_path / "tree", {})

    assert report.placed == []
    assert "'a/b'" in only_error(report)


# --------------------------------------------------------------- refusing


def test_two_runs_in_one_place_are_both_refused(pig: Pig, tmp_path: Path):
    finished_run(pig, BASELINE)
    finished_run(pig, {**BASELINE, "session": "Followup"})
    # Leaves out session, so both are PPT-1003's first run.
    pattern = "{task_code}/{participant_id}/run-{run_number}"

    report = organize(pig.config.done_root, tmp_path / "tree", {"stroop": pattern})

    assert report.placed == []
    assert len(report.errors) == 2
    assert all("Neither is placed" in why for why in report.errors.values())
    assert not (tmp_path / "tree").exists()


def test_values_differing_only_in_case_collide(pig: Pig, tmp_path: Path):
    finished_run(pig, BASELINE)
    finished_run(pig, {**BASELINE, "participant_id": "ppt-1003"})

    report = organize(pig.config.done_root, tmp_path / "tree", {})

    assert report.placed == []
    assert len(report.errors) == 2


def test_a_run_inside_another_runs_directory_collides(pig: Pig, tmp_path: Path):
    finished_run(pig, BASELINE)
    finished_run(pig, {"participant_id": "10351"}, task="interview")
    patterns = {
        "stroop": "x/run-{run_number}",
        "interview": "x/run-{run_number}/inner/run-{run_number}",
    }

    report = organize(pig.config.done_root, tmp_path / "tree", patterns)

    assert report.placed == []
    assert len(report.errors) == 2


def test_extra_parameters_cant_be_used_in_a_path(pig: Pig, tmp_path: Path):
    finished_run(pig, {**BASELINE, "utm_source": "email"})
    pattern = "{task_code}/{utm_source}/run-{run_number}"

    report = organize(pig.config.done_root, tmp_path / "tree", {"stroop": pattern})

    assert report.placed == []
    assert "extra parameter" in only_error(report)


def test_a_hand_edited_unsafe_value_is_refused(pig: Pig, tmp_path: Path):
    run_id = finished_run(pig, BASELINE)
    edit_manifest(pig, run_id, lambda m: m["parameters"].update(session=".."))

    report = organize(pig.config.done_root, tmp_path / "tree", {})

    assert report.placed == []
    assert "'..'" in only_error(report)


def test_a_run_without_a_manifest_is_refused(pig: Pig, tmp_path: Path):
    unfinished = pig.config.done_root / "stroop" / "not-finished"
    unfinished.mkdir(parents=True)
    (unfinished / "events.jsonl").write_text("")

    report = organize(pig.config.done_root, tmp_path / "tree", {})

    assert "isn't finished" in only_error(report)


def test_something_else_already_at_the_destination_is_left_alone(
    pig: Pig, tmp_path: Path
):
    finished_run(pig, BASELINE)
    occupied = tmp_path / "tree" / "stroop" / "PPT-1003" / "Baseline" / "run-0001"
    occupied.mkdir(parents=True)
    (occupied / "notes.txt").write_text("mine")

    report = organize(pig.config.done_root, tmp_path / "tree", {})

    assert "already there" in only_error(report)
    assert files_under(occupied) == {"notes.txt": b"mine"}


# --------------------------------------------------------------- verifying


def test_a_file_that_doesnt_match_its_manifest_is_copied_with_a_warning(
    pig: Pig, tmp_path: Path
):
    run_id = finished_run(pig, BASELINE)
    (pig.config.done_root / "stroop" / run_id / "events.jsonl").write_text("changed\n")

    report = organize(pig.config.done_root, tmp_path / "tree", {})

    assert len(report.placed) == 1
    assert len(report.warnings) == 1
    assert "doesn't match the manifest" in report.warnings[0]


def test_files_the_manifest_doesnt_list_are_warned_about_and_not_copied(
    pig: Pig, tmp_path: Path
):
    run_id = finished_run(pig, BASELINE)
    run_directory = pig.config.done_root / "stroop" / run_id
    (run_directory / "stray.txt").write_text("?")

    report = organize(pig.config.done_root, tmp_path / "tree", {})

    assert len(report.warnings) == 1
    assert "stray.txt" in report.warnings[0]
    placed = tmp_path / "tree" / "stroop" / "PPT-1003" / "Baseline" / "run-0001"
    assert not (placed / "stray.txt").exists()


def test_state_is_neither_verified_nor_copied(pig: Pig, tmp_path: Path):
    run_id = finished_run(pig, BASELINE)
    state = pig.config.done_root / "stroop" / run_id / ".state"
    state.mkdir()
    (state / "purged").write_text("")

    report = organize(pig.config.done_root, tmp_path / "tree", {})

    assert report.warnings == []
    placed = tmp_path / "tree" / "stroop" / "PPT-1003" / "Baseline" / "run-0001"
    assert not (placed / ".state").exists()


def test_the_manifest_never_lists_state(tmp_path: Path):
    (tmp_path / ".state").mkdir()
    (tmp_path / ".state" / "purged").write_text("")
    (tmp_path / "events.jsonl").write_text("")

    assert [f["path"] for f in storage.describe_files(tmp_path)] == ["events.jsonl"]


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


def test_a_literal_part_must_be_a_safe_value_too(pig: Pig, tmp_path: Path):
    finished_run(pig, BASELINE)

    report = organize(
        pig.config.done_root, tmp_path / "tree", {"stroop": "v1.2/run-{run_number}"}
    )

    assert "'v1.2'" in only_error(report)


# ------------------------------------------------------------- pigsty.toml


STY = """
kind = "secure"
store = "./store"
organized = "./organized"

[task.stroop]
pattern = "lucida/{task_code}/{participant_id}/{session}/run-{run_number}"
"""


def test_pigsty_paths_are_relative_to_the_file(tmp_path: Path):
    path = tmp_path / "pigsty.toml"
    path.write_text(STY)

    loaded = load_sty_config(path)

    assert loaded.store == tmp_path / "store"
    assert loaded.pattern_for("stroop") is not None
    assert loaded.pattern_for("balloons") is None


def test_pigsty_without_kind_is_refused(tmp_path: Path):
    path = tmp_path / "pigsty.toml"
    path.write_text(STY.replace('kind = "secure"', ""))

    with pytest.raises(ConfigurationError, match='kind = "secure"'):
        load_sty_config(path)


def test_pigsty_with_a_bad_pattern_is_refused(tmp_path: Path):
    path = tmp_path / "pigsty.toml"
    path.write_text(STY.replace("/run-{run_number}", ""))

    with pytest.raises(ConfigurationError, match="run_number"):
        load_sty_config(path)


def test_the_web_side_refuses_pigsty(tmp_path: Path):
    path = tmp_path / "pig.toml"
    path.write_text(STY)

    with pytest.raises(ConfigurationError, match="pigsty.toml"):
        load_config(path)


def test_pig_toml_may_say_it_is_the_web_side(config_path: Path):
    config_path.write_text('kind = "web"\n' + config_path.read_text())

    assert load_config(config_path).kind == "web"


# ------------------------------------------------------------------- the CLI


def test_the_cli_organizes_from_pigsty(pig: Pig, tmp_path: Path, capsys):
    finished_run(pig, BASELINE)
    sty = tmp_path / "pigsty.toml"
    sty.write_text(STY.replace("./store", str(pig.config.done_root)))

    app(["organize", "--config", str(sty)], result_action="return_value")

    assert "Placed 1 run(s)" in capsys.readouterr().out
    assert (tmp_path / "organized" / "lucida" / "stroop" / "PPT-1003").is_dir()


def test_the_cli_takes_a_store_and_output_without_pigsty(
    pig: Pig, tmp_path: Path, capsys
):
    finished_run(pig, BASELINE)

    app(
        ["organize", str(pig.config.done_root), str(tmp_path / "out")],
        result_action="return_value",
    )

    assert (tmp_path / "out" / "stroop" / "PPT-1003" / "Baseline").is_dir()


def test_the_cli_exits_nonzero_when_a_run_isnt_placed(pig: Pig, tmp_path: Path):
    finished_run(pig, BASELINE)
    finished_run(pig, {**BASELINE, "participant_id": "ppt-1003"})

    with pytest.raises(SystemExit) as exited:
        app(
            ["organize", str(pig.config.done_root), str(tmp_path / "out")],
            result_action="return_value",
        )
    assert exited.value.code == 1


def test_check_tells_the_two_files_apart(tmp_path: Path, capsys):
    sty = tmp_path / "pigsty.toml"
    sty.write_text(STY)

    app(["check", "--config", str(sty)], result_action="return_value")

    assert "Secure-side configuration looks good" in capsys.readouterr().out


# ------------------------------------------------------------------- helpers


def edit_manifest(pig: Pig, run_id: str, change) -> None:
    directory = pig.config.done_root / "stroop" / run_id
    manifest = storage.read_manifest(directory)
    change(manifest)
    (directory / "manifest.json").write_text(json.dumps(manifest))


def only_error(report) -> str:
    assert len(report.errors) == 1, report.errors
    return next(iter(report.errors.values()))
