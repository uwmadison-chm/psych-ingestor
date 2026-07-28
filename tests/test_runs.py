"""The parts that carry the reliability promise: dedup, collisions, and the write order."""

import json

import pytest

from psych_ingestor import storage
from psych_ingestor.runs import Pig, RequestProblem

BASELINE = {"participant_id": "10351", "session": "baseline"}


def event(payload: dict) -> dict:
    return {"timestamp": "2026-07-26T18:25:43.511-05:00", "data": payload}


def start(pig: Pig, **overrides) -> str:
    parameters = {**BASELINE, **overrides}
    return pig.start_run("stroop", parameters)["run_id"]


def dataset_lines(pig: Pig, run_id: str) -> list[dict]:
    path = storage.in_progress_path(pig.config.in_progress_root, run_id)
    return storage.read_lines(path)


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


def test_case_variants_are_the_same_run_key(pig: Pig):
    pig.start_run("stroop", BASELINE)
    shouted = pig.start_run("stroop", {**BASELINE, "session": "BASELINE"})
    assert shouted["run_number"] == 2


def test_extra_parameters_are_recorded_and_ignored(pig: Pig):
    run_id = pig.start_run("stroop", {**BASELINE, "utm_source": "email"})["run_id"]
    row = pig.connection.execute(
        "SELECT parameters, extra_parameters FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert json.loads(row["parameters"]) == BASELINE
    assert json.loads(row["extra_parameters"]) == {"utm_source": "email"}


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
    assert dataset_lines(pig, run_id) == [
        {
            "event_id": "1",
            "data": {"trial": 1},
            "timestamp": "2026-07-26T18:25:43.511-05:00",
        }
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


def test_a_timestamp_is_optional(pig: Pig):
    run_id = start(pig)
    result = pig.store_events("stroop", run_id, {"1": {"data": {"trial": 1}}})
    assert result.status_code == 201
    assert dataset_lines(pig, run_id) == [{"event_id": "1", "data": {"trial": 1}}]


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
    assert refused.errors["2"]["can_retry"] is False
    assert refused.stored == ["1"]


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
