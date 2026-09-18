"""The parts that carry the reliability promise: dedup, collisions, and the write order."""

import pytest

from psych_ingestor import db, runs, storage
from psych_ingestor.runs import Disposition, RequestProblem
from psych_ingestor.service import Pig

BASELINE = {"participant_id": "10351", "session": "baseline"}


def event(payload: dict) -> dict:
    return {"data": payload}


def start(pig: Pig, **overrides) -> str:
    parameters = {**BASELINE, **overrides}
    return pig.start_run("stroop", parameters)["run_id"]


def expire(pig: Pig, run_id: str) -> None:
    """Close a run the way the sweep does, rather than by hand: the database refuses a
    row whose phase and disposition disagree."""
    run = runs.get(pig.connection, run_id)
    assert run is not None
    runs.mark_closed(pig.connection, run, Disposition.EXPIRED, db.now())


def dataset_lines(pig: Pig, run_id: str) -> list[dict]:
    directory = storage.run_directory(pig.config.in_progress_root, "stroop", run_id)
    return storage.read_lines(directory / storage.EVENTS_FILE)


def without_metadata(lines: list[dict]) -> list[dict]:
    return [{k: v for k, v in line.items() if k != "metadata"} for line in lines]


def test_run_numbers_count_up_for_the_same_key(pig: Pig):
    first = pig.start_run("stroop", BASELINE)
    second = pig.start_run("stroop", BASELINE)
    assert first["run_number"] == 1
    assert second["run_number"] == 2
    assert first["run_id"] != second["run_id"]


def test_a_different_session_is_a_different_key(pig: Pig):
    pig.start_run("stroop", BASELINE)
    followup = pig.start_run("stroop", {**BASELINE, "session": "3mo"})
    assert followup["run_number"] == 1


def test_case_variants_are_different_run_keys(pig: Pig):
    """Pig doesn't edit a participant's identity. `baseline` and `BASELINE` are two
    sessions, the same way `10351` and `10352` are two participants."""
    pig.start_run("stroop", BASELINE)
    shouted = pig.start_run("stroop", {**BASELINE, "session": "BASELINE"})
    assert shouted["run_number"] == 1


def test_starting_a_run_creates_its_directory(pig: Pig):
    """So that by sweep time "is the directory there" means one thing, whether or not
    the run ever sent an event."""
    run_id = start(pig)
    directory = storage.run_directory(pig.config.in_progress_root, "stroop", run_id)
    assert directory.is_dir()
    assert list(directory.iterdir()) == []


def test_extra_parameters_are_recorded_and_ignored(pig: Pig):
    run_id = pig.start_run("stroop", {**BASELINE, "utm_source": "email"})["run_id"]
    run = runs.get(pig.connection, run_id)
    assert run is not None
    assert run.parameters == BASELINE
    assert run.extra_parameters == {"utm_source": "email"}


def test_an_unusable_parameter_refuses_the_run(pig: Pig):
    with pytest.raises(RequestProblem) as raised:
        pig.start_run("stroop", {**BASELINE, "participant_id": "../etc"})
    assert raised.value.status_code == 422


def test_a_missing_parameter_refuses_the_run(pig: Pig):
    with pytest.raises(RequestProblem) as raised:
        pig.start_run("stroop", {"participant_id": "10351"})
    assert raised.value.status_code == 422


def test_a_closed_task_takes_no_new_runs(pig: Pig):
    with pytest.raises(RequestProblem) as raised:
        pig.start_run("balloons", {"participant_id": "10351"})
    assert raised.value.status_code == 409


def test_storing_events_reports_everything_stored_so_far(pig: Pig):
    run_id = start(pig)
    first = pig.store_events("stroop", run_id, {"1": event({"trial": 1})})
    second = pig.store_events("stroop", run_id, {"2": event({"trial": 2})})
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.stored == ["1", "2"]


def test_resending_the_same_event_is_a_retry(pig: Pig):
    run_id = start(pig)
    batch = {"1": event({"trial": 1}), "2": event({"trial": 2})}
    pig.store_events("stroop", run_id, batch)
    again = pig.store_events("stroop", run_id, batch)

    assert again.status_code == 200
    assert again.errors == {}
    assert len(dataset_lines(pig, run_id)) == 2


def test_a_partly_new_batch_is_a_201(pig: Pig):
    run_id = start(pig)
    pig.store_events("stroop", run_id, {"1": event({"trial": 1})})
    mixed = pig.store_events(
        "stroop", run_id, {"1": event({"trial": 1}), "2": event({"trial": 2})}
    )
    assert mixed.status_code == 201
    assert mixed.stored == ["1", "2"]


def test_the_same_id_with_different_content_is_refused(pig: Pig):
    run_id = start(pig)
    pig.store_events("stroop", run_id, {"1": event({"trial": 1})})
    collision = pig.store_events("stroop", run_id, {"1": event({"trial": 99})})

    assert collision.status_code == 422
    assert collision.errors["1"]["can_retry"] is False
    # Pig keeps what it had.
    assert without_metadata(dataset_lines(pig, run_id)) == [
        {"event_id": "1", "data": {"trial": 1}}
    ]


def test_one_bad_event_does_not_stop_the_others(pig: Pig):
    run_id = start(pig)
    result = pig.store_events(
        "stroop", run_id, {"1": event({"trial": 1}), "2": {"no_data_field": True}}
    )
    assert result.status_code == 422
    assert result.stored == ["1"]
    assert "2" in result.errors


def test_an_event_over_the_size_limit_is_refused(pig: Pig):
    run_id = start(pig)
    pig.config.task["stroop"].max_event_size = 100
    result = pig.store_events("stroop", run_id, {"1": event({"blob": "x" * 500})})
    assert result.status_code == 422
    assert result.errors["1"]["can_retry"] is False


def test_a_stored_line_carries_when_pig_stored_it(pig: Pig):
    """Three keys: `event_id` and `data` are the task's, `metadata` is Pig's."""
    run_id = start(pig)
    pig.store_events("stroop", run_id, {"1": {"data": {"trial": 1}}})
    (line,) = dataset_lines(pig, run_id)
    assert set(line) == {"event_id", "data", "metadata"}
    assert set(line["metadata"]) == {"stored_at"}
    stored_at = line["metadata"]["stored_at"]
    assert stored_at.endswith("+00:00")
    # And it's the same instant the receipt records.
    receipt = pig.connection.execute(
        "SELECT stored_at FROM event_receipts WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert receipt["stored_at"] == stored_at


def test_a_field_outside_data_is_refused(pig: Pig):
    run_id = start(pig)
    result = pig.store_events(
        "stroop", run_id, {"1": {"data": {"trial": 1}, "timestamp": "2026-07-26"}}
    )
    assert result.status_code == 422
    assert result.errors["1"]["can_retry"] is False
    assert "timestamp" in result.errors["1"]["message"]
    assert dataset_lines(pig, run_id) == []


def test_the_size_limit_and_the_hash_ignore_what_pig_added(pig: Pig):
    """Both cover the line minus `metadata`, so neither drifts as Pig adds fields."""
    run_id = start(pig)
    line = storage.event_line("1", {"trial": 1}, db.now())
    hashed = storage.hashed_text(line)
    assert "metadata" not in hashed
    assert "stored_at" not in hashed

    pig.config.task["stroop"].max_event_size = len(hashed.encode("utf-8"))
    result = pig.store_events("stroop", run_id, {"1": {"data": {"trial": 1}}})
    assert result.status_code == 201
    assert runs.receipt_hash(pig.connection, run_id, "1") == storage.content_hash(
        hashed
    )


def test_a_run_id_from_another_task_is_a_404(pig: Pig):
    run_id = start(pig)
    with pytest.raises(RequestProblem) as raised:
        pig.describe_run("balloons", run_id)
    assert raised.value.status_code == 404


def test_an_unknown_run_is_a_404(pig: Pig):
    with pytest.raises(RequestProblem) as raised:
        pig.describe_run("stroop", "not-a-run-id")
    assert raised.value.status_code == 404


def test_finalizing_closes_the_run_to_events(pig: Pig):
    run_id = start(pig)
    pig.store_events("stroop", run_id, {"1": event({"trial": 1})})

    finalized = pig.finalize_run("stroop", run_id)
    assert finalized.status == "finalizing"
    assert finalized.stored == ["1"]

    refused = pig.store_events("stroop", run_id, {"2": event({"trial": 2})})
    assert refused.status_code == 409
    assert refused.stored == ["1"]
    # Nothing is wrong with the event, so it's fine to send it — to a new run.
    assert refused.errors["2"]["can_retry"] is True
    assert "start a new run" in refused.errors["2"]["message"]


def test_an_expired_run_tells_the_task_to_start_a_new_one(pig: Pig):
    run_id = start(pig)
    pig.store_events("stroop", run_id, {"1": event({"trial": 1})})
    expire(pig, run_id)

    refused = pig.store_events("stroop", run_id, {"2": event({"trial": 2})})
    assert refused.status_code == 409
    assert refused.status == "expired"
    assert refused.stored == ["1"]
    assert refused.errors["2"]["can_retry"] is True
    assert "expired" in refused.errors["2"]["message"]
    assert "Start a new run" in refused.errors["2"]["message"]


def test_an_expired_run_cannot_be_finalized(pig: Pig):
    run_id = start(pig)
    expire(pig, run_id)
    result = pig.finalize_run("stroop", run_id)
    assert result.status_code == 409
    assert result.status == "expired"
    assert result.errors["run"]["can_retry"] is False


def test_finalizing_twice_is_not_an_error(pig: Pig):
    run_id = start(pig)
    pig.finalize_run("stroop", run_id)
    again = pig.finalize_run("stroop", run_id)
    assert again.status_code == 200
    assert again.status == "finalizing"


def test_events_are_on_disk_before_the_response(pig: Pig):
    """The whole promise: if the reply says stored, the line is in the file."""
    run_id = start(pig)
    result = pig.store_events("stroop", run_id, {"1": event({"trial": 1})})
    assert result.stored == ["1"]
    assert len(dataset_lines(pig, run_id)) == 1
