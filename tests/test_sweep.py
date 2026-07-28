"""Filing datasets and reaping runs nobody came back to."""

from datetime import UTC, datetime, timedelta

import pytest

from psych_ingestor import storage, sweep
from psych_ingestor.runs import Pig

BASELINE = {"participant_id": "PPT-1003", "session": "Baseline"}


def event(trial: int, at: str) -> dict:
    return {"timestamp": at, "data": {"trial": trial}}


def test_filing_sorts_moves_and_completes(pig: Pig):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events(
        "stroop",
        run_id,
        {
            "2": event(2, "2026-07-26T18:25:47-05:00"),
            "1": event(1, "2026-07-26T18:25:43-05:00"),
        },
    )
    pig.finalize_run("stroop", run_id)

    report = sweep.sweep(pig)
    assert report.filed == [run_id]

    # Values are lowercased on their way into the path; the original is kept on the run.
    filed = pig.config.complete_root / "stroop/ppt-1003/baseline_run-0001.jsonl"
    assert filed.exists()
    assert [line["event_id"] for line in storage.read_lines(filed)] == ["1", "2"]
    assert not storage.in_progress_path(pig.config.in_progress_root, run_id).exists()

    run = pig.connection.execute(
        "SELECT * FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert run["status"] == "complete"
    assert run["filed_at"] is not None


def test_filing_drops_lines_that_repeat_exactly(pig: Pig):
    """The crash window leaves duplicate lines. The file heals when it's filed."""
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1, "2026-07-26T18:25:43-05:00")})

    # A line that reached the file but never the database, as a crash would leave it.
    path = storage.in_progress_path(pig.config.in_progress_root, run_id)
    storage.append_line(path, storage.canonical(storage.read_lines(path)[0]))
    assert len(storage.read_lines(path)) == 2

    pig.finalize_run("stroop", run_id)
    sweep.sweep(pig)

    filed = pig.config.complete_root / "stroop/ppt-1003/baseline_run-0001.jsonl"
    assert len(storage.read_lines(filed)) == 1


def test_filing_keeps_two_lines_that_share_an_id_but_differ(pig: Pig):
    """Both events are in the file. Nothing was lost and nothing was silently chosen."""
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1, "2026-07-26T18:25:43-05:00")})

    path = storage.in_progress_path(pig.config.in_progress_root, run_id)
    storage.append_line(
        path, storage.canonical({"event_id": "1", "data": {"trial": 99}})
    )

    pig.finalize_run("stroop", run_id)
    sweep.sweep(pig)

    filed = pig.config.complete_root / "stroop/ppt-1003/baseline_run-0001.jsonl"
    assert len(storage.read_lines(filed)) == 2
    assert storage.duplicate_event_ids(filed) == ["1"]


def test_a_stale_run_is_abandoned_and_filed_separately(pig: Pig):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1, "2026-07-26T18:25:43-05:00")})

    long_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    pig.connection.execute(
        "UPDATE runs SET started_at = ? WHERE run_id = ?", (long_ago, run_id)
    )
    pig.connection.execute("UPDATE events SET stored_at = ?", (long_ago,))

    report = sweep.sweep(pig)
    assert report.abandoned == [run_id]

    assert (
        pig.config.abandoned_root / "stroop/ppt-1003/baseline_run-0001.jsonl"
    ).exists()
    status = pig.connection.execute(
        "SELECT status FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()["status"]
    assert status == "abandoned"


def test_a_recent_run_is_left_alone(pig: Pig):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    report = sweep.sweep(pig)
    assert report.abandoned == []
    status = pig.connection.execute(
        "SELECT status FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()["status"]
    assert status == "in_progress"


def test_finalizing_an_abandoned_run_by_hand_recovers_its_data(pig: Pig):
    """A run that turned out fine, filed with the abandoned ones, and rescued."""
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1, "2026-07-26T18:25:43-05:00")})
    pig.connection.execute(
        "UPDATE runs SET status = 'abandoned' WHERE run_id = ?", (run_id,)
    )
    sweep.sweep(pig)
    assert (
        pig.config.abandoned_root / "stroop/ppt-1003/baseline_run-0001.jsonl"
    ).exists()

    sweep.reopen_for_finalizing(pig, run_id)
    sweep.sweep(pig)

    filed = pig.config.complete_root / "stroop/ppt-1003/baseline_run-0001.jsonl"
    assert len(storage.read_lines(filed)) == 1
    assert not (
        pig.config.abandoned_root / "stroop/ppt-1003/baseline_run-0001.jsonl"
    ).exists()
    status = pig.connection.execute(
        "SELECT status FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()["status"]
    assert status == "complete"


def test_a_complete_run_cannot_be_finalized_again(pig: Pig):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.finalize_run("stroop", run_id)
    sweep.sweep(pig)
    with pytest.raises(ValueError):
        sweep.reopen_for_finalizing(pig, run_id)


def test_finalizing_an_unknown_run_by_hand_says_so(pig: Pig):
    with pytest.raises(ValueError):
        sweep.reopen_for_finalizing(pig, "not-a-run-id")


def test_sweeping_twice_is_safe(pig: Pig):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1, "2026-07-26T18:25:43-05:00")})
    pig.finalize_run("stroop", run_id)

    sweep.sweep(pig)
    second = sweep.sweep(pig)
    assert second.filed == []
    assert second.failed == {}


def test_sweeping_with_nothing_to_do_is_fine(pig: Pig):
    report = sweep.sweep(pig)
    assert report.filed == []
    assert report.abandoned == []
    assert report.failed == {}
