"""Filing datasets and expiring runs that have been open too long."""

from datetime import UTC, datetime, timedelta

import pytest

from psych_ingestor import runs, storage, sweep
from psych_ingestor.service import Pig

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

    report = sweep.sweep(pig.config, pig.connection)
    assert report.filed == [run_id]

    # Values are lowercased on their way into the path; the original is kept on the run.
    filed = pig.config.complete_root / "stroop/ppt-1003/baseline_run-0001.jsonl"
    assert filed.exists()
    assert [line["event_id"] for line in storage.read_lines(filed)] == ["1", "2"]
    assert not storage.in_progress_path(pig.config.in_progress_root, run_id).exists()

    run = runs.get(pig.connection, run_id)
    assert run is not None
    assert run.api_status == "complete"
    assert run.phase is runs.Phase.DONE
    assert run.disposition is runs.Disposition.FINALIZED
    assert run.filed_at is not None


def test_filing_drops_lines_that_repeat_exactly(pig: Pig):
    """The crash window leaves duplicate lines. The file heals when it's filed."""
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1, "2026-07-26T18:25:43-05:00")})

    # A line that reached the file but never the database, as a crash would leave it.
    path = storage.in_progress_path(pig.config.in_progress_root, run_id)
    storage.append_line(path, storage.canonical(storage.read_lines(path)[0]))
    assert len(storage.read_lines(path)) == 2

    pig.finalize_run("stroop", run_id)
    sweep.sweep(pig.config, pig.connection)

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
    sweep.sweep(pig.config, pig.connection)

    filed = pig.config.complete_root / "stroop/ppt-1003/baseline_run-0001.jsonl"
    assert len(storage.read_lines(filed)) == 2
    assert storage.duplicate_event_ids(filed) == ["1"]


def test_a_run_open_too_long_expires_and_is_filed_separately(pig: Pig):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1, "2026-07-26T18:25:43-05:00")})

    long_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    pig.connection.execute(
        "UPDATE runs SET started_at = ? WHERE run_id = ?", (long_ago, run_id)
    )

    report = sweep.sweep(pig.config, pig.connection)
    assert report.expired == [run_id]

    assert (
        pig.config.expired_root / "stroop/ppt-1003/baseline_run-0001.jsonl"
    ).exists()
    run = runs.get(pig.connection, run_id)
    assert run is not None
    assert run.api_status == "expired"
    assert run.disposition is runs.Disposition.EXPIRED


def test_a_run_still_receiving_events_expires_anyway(pig: Pig):
    """The limit counts from the start, so something that keeps sending can't hold a
    run open forever."""
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    long_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    pig.connection.execute(
        "UPDATE runs SET started_at = ? WHERE run_id = ?", (long_ago, run_id)
    )
    # An event that arrived just now.
    pig.store_events("stroop", run_id, {"1": event(1, "2026-07-26T18:25:43-05:00")})

    report = sweep.sweep(pig.config, pig.connection)
    assert report.expired == [run_id]


def test_a_recent_run_is_left_alone(pig: Pig):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    report = sweep.sweep(pig.config, pig.connection)
    assert report.expired == []
    run = runs.get(pig.connection, run_id)
    assert run is not None
    assert run.accepting_data


def test_sweeping_twice_is_safe(pig: Pig):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1, "2026-07-26T18:25:43-05:00")})
    pig.finalize_run("stroop", run_id)

    sweep.sweep(pig.config, pig.connection)
    second = sweep.sweep(pig.config, pig.connection)
    assert second.filed == []
    assert second.failed == {}


def test_sweeping_with_nothing_to_do_is_fine(pig: Pig):
    report = sweep.sweep(pig.config, pig.connection)
    assert report.filed == []
    assert report.expired == []
    assert report.failed == {}


def test_a_run_that_sent_no_events_still_gets_an_empty_dataset(pig: Pig):
    """A uniform layout is worth more than saving a zero-byte file: "is the dataset
    there" shouldn't be a question with two answers."""
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.finalize_run("stroop", run_id)

    report = sweep.sweep(pig.config, pig.connection)
    assert report.filed == [run_id]
    assert report.failed == {}

    filed = pig.config.complete_root / "stroop/ppt-1003/baseline_run-0001.jsonl"
    assert filed.exists()
    assert storage.read_lines(filed) == []


def test_filing_refuses_to_replace_a_dataset_with_an_empty_one(tmp_path):
    """A missing source reads as no events. That's right for a run that sent none and
    catastrophic for one another sweep already filed, and the two look the same from
    inside `file_dataset` — so it refuses rather than guessing. See issue #18."""
    source, destination = tmp_path / "run.jsonl", tmp_path / "filed.jsonl"
    storage.append_line(source, storage.canonical({"event_id": "1", "data": {}}))
    assert storage.file_dataset(source, destination) == 1

    with pytest.raises(OSError):
        storage.file_dataset(source, destination)

    assert len(storage.read_lines(destination)) == 1


def test_filing_an_empty_dataset_twice_is_still_fine(tmp_path):
    """The other half of that guard: it refuses nothing a real run needs."""
    destination = tmp_path / "filed.jsonl"
    assert storage.file_dataset(tmp_path / "never-existed.jsonl", destination) == 0
    assert storage.file_dataset(tmp_path / "never-existed.jsonl", destination) == 0
    assert destination.exists()


def test_a_sweep_reports_a_dataset_it_cannot_account_for(pig: Pig):
    """What a raced or half-finished sweep leaves: the dataset is filed, but the database
    still thinks it isn't. The run is reported rather than quietly emptied."""
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1, "2026-07-26T18:25:43-05:00")})
    pig.finalize_run("stroop", run_id)
    sweep.sweep(pig.config, pig.connection)

    filed = pig.config.complete_root / "stroop/ppt-1003/baseline_run-0001.jsonl"
    assert len(storage.read_lines(filed)) == 1

    # Put the run back to waiting, as a sweep that died before its last write would.
    pig.connection.execute(
        "UPDATE runs SET phase = 'closed', filed_at = NULL, dataset_path = NULL "
        "WHERE run_id = ?",
        (run_id,),
    )

    report = sweep.sweep(pig.config, pig.connection)
    assert report.filed == []
    assert run_id in report.failed
    # And the dataset that was already there is untouched.
    assert len(storage.read_lines(filed)) == 1


def test_a_run_whose_data_vanished_is_reported_not_emptied(pig: Pig):
    """The case the destination guard can't see: a run with events whose in-progress file
    is gone before it was ever filed. Nothing exists at the destination to refuse, so the
    receipts are what say this run should have had data. See issue #18."""
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1, "2026-07-26T18:25:43-05:00")})
    pig.finalize_run("stroop", run_id)

    # Whatever lost it — a disk problem, someone tidying up by hand.
    storage.in_progress_path(pig.config.in_progress_root, run_id).unlink()

    report = sweep.sweep(pig.config, pig.connection)
    assert report.filed == []
    assert run_id in report.failed
    assert "1 event(s)" in report.failed[run_id]

    # No empty dataset was written, and the run is still waiting rather than called done.
    assert not (
        pig.config.complete_root / "stroop/ppt-1003/baseline_run-0001.jsonl"
    ).exists()
    run = runs.get(pig.connection, run_id)
    assert run is not None
    assert run.phase is runs.Phase.CLOSED
