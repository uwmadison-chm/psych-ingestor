"""Finishing closed runs and expiring runs that have been open too long."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from psych_ingestor import runs, storage, sweep
from psych_ingestor.service import Pig

BASELINE = {"participant_id": "PPT-1003", "session": "Baseline"}


def event(trial: int) -> dict:
    return {
        "data": {"trial": trial, "timestamp": f"2026-07-26T18:25:{trial:02d}-05:00"}
    }


def in_progress(pig: Pig, run_id: str) -> Path:
    return storage.run_directory(pig.config.in_progress_root, "stroop", run_id)


def done(pig: Pig, run_id: str) -> Path:
    return storage.run_directory(pig.config.done_root, "stroop", run_id)


def test_finishing_writes_a_manifest_and_moves_the_directory(pig: Pig):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"2": event(2), "1": event(1)})
    pig.finalize_run("stroop", run_id)

    report = sweep.sweep(pig.config, pig.connection)
    assert report.finished == [run_id]
    assert report.failed == {}
    assert report.refused == {}

    directory = done(pig, run_id)
    assert directory.is_dir()
    assert not in_progress(pig, run_id).exists()
    assert sorted(p.name for p in directory.iterdir()) == [
        "events.jsonl",
        "manifest.json",
    ]

    run = runs.get(pig.connection, run_id)
    assert run is not None
    assert run.api_status == "complete"
    assert run.phase is runs.Phase.DONE
    assert run.disposition is runs.Disposition.FINALIZED
    assert run.done_at is not None


def test_the_manifest_describes_the_run_without_pig(pig: Pig):
    run_id = pig.start_run("stroop", {**BASELINE, "utm_source": "email"})["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1)})
    pig.finalize_run("stroop", run_id)
    sweep.sweep(pig.config, pig.connection)

    manifest = storage.read_manifest(done(pig, run_id))
    assert manifest["type"] == "pig_run_manifest"
    assert manifest["manifest_version"] == 2
    assert manifest["run_id"] == run_id
    assert manifest["task_code"] == "stroop"
    assert manifest["run_number"] == 1
    assert manifest["run_key"] == ["participant_id", "session"]
    assert manifest["disposition"] == "finalized"
    # Parameters exactly as the link sent them. Nothing lowercases anything.
    assert manifest["parameters"] == BASELINE
    assert manifest["extra_parameters"] == {"utm_source": "email"}
    assert manifest["media"] == []
    for stamp in ("started_at", "closed_at", "done_at"):
        assert datetime.fromisoformat(manifest[stamp]).tzinfo is not None
    assert manifest["started_at"] <= manifest["closed_at"] <= manifest["done_at"]

    # The files entry is what lets a copy elsewhere be checked by hash.
    events_file = done(pig, run_id) / "events.jsonl"
    assert manifest["files"] == [
        {
            "path": "events.jsonl",
            "bytes": events_file.stat().st_size,
            "sha256": storage.content_hash(events_file.read_text()),
        }
    ]
    assert "count" not in json.dumps(manifest)


def test_events_are_left_exactly_as_written(pig: Pig):
    """The server neither sorts nor deduplicates. `events.jsonl` in `done/` is the append
    log, byte for byte, which is what makes the manifest's hash a hash of what happened."""
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"2": event(2), "1": event(1)})

    # A line that reached the file but never the database, as a crash would leave it.
    events_file = in_progress(pig, run_id) / "events.jsonl"
    before = events_file.read_text()
    storage.append_line(
        events_file, storage.canonical(storage.read_lines(events_file)[0])
    )
    assert len(storage.read_lines(events_file)) == 3

    pig.finalize_run("stroop", run_id)
    sweep.sweep(pig.config, pig.connection)

    after = (done(pig, run_id) / "events.jsonl").read_text()
    assert after.startswith(before)
    assert [
        line["event_id"]
        for line in storage.read_lines(done(pig, run_id) / "events.jsonl")
    ] == ["2", "1", "2"]


def test_two_lines_that_share_an_id_but_differ_are_both_kept(pig: Pig):
    """Nothing was lost and nothing was silently chosen; the analyst gets to know."""
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1)})

    events_file = in_progress(pig, run_id) / "events.jsonl"
    storage.append_line(
        events_file,
        storage.canonical({"event_id": "1", "data": {"trial": 99}, "metadata": {}}),
    )

    pig.finalize_run("stroop", run_id)
    sweep.sweep(pig.config, pig.connection)

    finished = done(pig, run_id) / "events.jsonl"
    assert len(storage.read_lines(finished)) == 2
    assert storage.duplicate_event_ids(finished) == ["1"]


def test_a_run_open_too_long_expires_and_is_finished_in_the_same_sweep(pig: Pig):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1)})

    long_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    pig.connection.execute(
        "UPDATE runs SET started_at = ? WHERE run_id = ?", (long_ago, run_id)
    )

    report = sweep.sweep(pig.config, pig.connection)
    assert report.expired == [run_id]
    assert report.finished == [run_id]

    # Expired runs go to the same tree as finalized ones; the manifest says which.
    assert storage.read_manifest(done(pig, run_id))["disposition"] == "expired"
    run = runs.get(pig.connection, run_id)
    assert run is not None
    assert run.api_status == "expired"
    assert run.disposition is runs.Disposition.EXPIRED
    assert run.phase is runs.Phase.DONE


def test_a_run_still_receiving_events_expires_anyway(pig: Pig):
    """The limit counts from the start, so something that keeps sending can't hold a
    run open forever."""
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    long_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    pig.connection.execute(
        "UPDATE runs SET started_at = ? WHERE run_id = ?", (long_ago, run_id)
    )
    # An event that arrived just now.
    pig.store_events("stroop", run_id, {"1": event(1)})

    report = sweep.sweep(pig.config, pig.connection)
    assert report.expired == [run_id]


def test_a_recent_run_is_left_alone(pig: Pig):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    report = sweep.sweep(pig.config, pig.connection)
    assert report.expired == []
    run = runs.get(pig.connection, run_id)
    assert run is not None
    assert run.accepting_data
    assert in_progress(pig, run_id).is_dir()


def test_sweeping_twice_is_safe(pig: Pig):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1)})
    pig.finalize_run("stroop", run_id)

    sweep.sweep(pig.config, pig.connection)
    second = sweep.sweep(pig.config, pig.connection)
    assert second.finished == []
    assert second.failed == {}


def test_sweeping_with_nothing_to_do_is_fine(pig: Pig):
    report = sweep.sweep(pig.config, pig.connection)
    assert report.finished == []
    assert report.expired == []
    assert report.failed == {}
    assert report.refused == {}


def test_a_task_deleted_from_the_configuration_is_still_finished(pig: Pig):
    """The manifest comes from the database and the disk, never from `pig.toml`, so a
    task whose entry is gone finishes like any other."""
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1)})
    pig.finalize_run("stroop", run_id)
    del pig.config.task["stroop"]

    report = sweep.sweep(pig.config, pig.connection)
    assert report.finished == [run_id]
    assert storage.read_manifest(done(pig, run_id))["run_key"] == [
        "participant_id",
        "session",
    ]


def test_a_run_that_sent_no_events_still_gets_an_empty_events_file(pig: Pig):
    """A uniform layout is worth more than saving a zero-byte file: "is the dataset
    there" shouldn't be a question with two answers."""
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.finalize_run("stroop", run_id)

    report = sweep.sweep(pig.config, pig.connection)
    assert report.finished == [run_id]
    assert report.failed == {}
    assert report.refused == {}

    events_file = done(pig, run_id) / "events.jsonl"
    assert events_file.exists()
    assert events_file.stat().st_size == 0
    assert storage.read_manifest(done(pig, run_id))["files"][0]["bytes"] == 0


# ------------------------------------------------------- the sweep's own crash window


def put_back_to_closed(pig: Pig, run_id: str) -> None:
    """The state a sweep that died between the rename and the row update leaves. No
    code path produces it, so it's written by hand."""
    pig.connection.execute(
        "UPDATE runs SET phase = 'closed', done_at = NULL WHERE run_id = ?", (run_id,)
    )


def test_a_run_already_in_done_gets_its_bookkeeping_finished_and_nothing_else(
    pig: Pig,
):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1)})
    pig.finalize_run("stroop", run_id)
    sweep.sweep(pig.config, pig.connection)
    manifest_before = (done(pig, run_id) / "manifest.json").read_bytes()

    put_back_to_closed(pig, run_id)

    report = sweep.sweep(pig.config, pig.connection)
    assert report.finished == [run_id]
    assert report.failed == {}
    assert report.refused == {}
    # The directory in `done/` wasn't touched: same manifest, byte for byte.
    assert (done(pig, run_id) / "manifest.json").read_bytes() == manifest_before
    run = runs.get(pig.connection, run_id)
    assert run is not None
    assert run.phase is runs.Phase.DONE


def test_a_run_in_neither_tree_is_reported_and_nothing_is_made_up(pig: Pig):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.finalize_run("stroop", run_id)
    in_progress(pig, run_id).rmdir()  # Someone tidying up by hand.

    report = sweep.sweep(pig.config, pig.connection)
    assert report.finished == []
    assert run_id in report.refused
    assert report.failed == {}
    assert not done(pig, run_id).exists()
    assert not in_progress(pig, run_id).exists()
    run = runs.get(pig.connection, run_id)
    assert run is not None
    assert run.phase is runs.Phase.CLOSED


def test_a_run_in_both_trees_is_reported_and_neither_is_touched(pig: Pig):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1)})
    pig.finalize_run("stroop", run_id)
    sweep.sweep(pig.config, pig.connection)

    put_back_to_closed(pig, run_id)
    stray = in_progress(pig, run_id)
    stray.mkdir(parents=True)
    (stray / "events.jsonl").write_text("not the real data\n")

    report = sweep.sweep(pig.config, pig.connection)
    assert run_id in report.refused
    assert report.failed == {}
    assert (stray / "events.jsonl").read_text() == "not the real data\n"
    assert len(storage.read_lines(done(pig, run_id) / "events.jsonl")) == 1


def test_a_destination_that_appears_mid_sweep_is_refused_not_called_a_failure(
    pig: Pig, monkeypatch
):
    """The check for a run in both trees happens before the manifest is written, so
    another sweep can put a directory in `done/` after we've looked and before we
    rename. `move_directory` raises `OSError` to refuse that, and the same `except`
    catches a full disk, so the sweep looks at the trees again to tell them apart."""
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1)})
    pig.finalize_run("stroop", run_id)

    real_write_manifest = storage.write_manifest

    def write_manifest_then_lose_the_race(directory: Path, manifest: dict) -> None:
        real_write_manifest(directory, manifest)
        done(pig, run_id).mkdir(parents=True)

    monkeypatch.setattr(storage, "write_manifest", write_manifest_then_lose_the_race)

    report = sweep.sweep(pig.config, pig.connection)
    assert run_id in report.refused
    assert report.failed == {}
    assert report.finished == []

    # Both directories are still there, untouched, for someone to sort out.
    assert in_progress(pig, run_id).exists()
    assert done(pig, run_id).exists()


def test_a_run_whose_events_vanished_is_reported_not_emptied(pig: Pig):
    """The directory is there but the events file isn't, and the receipts say it should
    be. Nothing exists in `done/` to refuse over, so the receipts are what say this run
    should have had data. See issue #18."""
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.store_events("stroop", run_id, {"1": event(1)})
    pig.finalize_run("stroop", run_id)

    (in_progress(pig, run_id) / "events.jsonl").unlink()

    report = sweep.sweep(pig.config, pig.connection)
    assert report.finished == []
    assert run_id in report.refused
    assert report.failed == {}
    assert "1 event(s)" in report.refused[run_id]

    # No empty file was written, and the run is still waiting rather than called done.
    assert not (in_progress(pig, run_id) / "events.jsonl").exists()
    assert not done(pig, run_id).exists()
    run = runs.get(pig.connection, run_id)
    assert run is not None
    assert run.phase is runs.Phase.CLOSED
