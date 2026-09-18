"""The run lifecycle, and the constraints that keep a run row consistent.

The rules here used to be expressions scattered across `sweep.py`, `health.py` and the
request path, reachable only by inserting a row and editing it. Now they're on `Run`, and
a `Run` needs no database — so this file is about the rules themselves.
"""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from psych_ingestor import db, runs
from psych_ingestor.runs import Disposition, Phase, Run

STARTED = datetime(2026, 7, 26, 18, 0, tzinfo=UTC)


def make_run(**overrides) -> Run:
    """A run with nothing behind it. No configuration, no database, no connection."""
    fields: dict = {
        "run_id": "9f3c1a7e",
        "task_code": "stroop",
        "run_key_hash": runs.hash_run_key(["10351", "baseline"]),
        "run_number": 1,
        "parameters": {"participant_id": "10351", "session": "baseline"},
        "extra_parameters": {},
        "run_key_names": ["participant_id", "session"],
        "phase": Phase.COLLECTING,
        "started_at": STARTED,
    }
    return Run(**{**fields, **overrides})


# --------------------------------------------------------------- accepting data


def test_only_a_collecting_run_accepts_data():
    assert make_run(phase=Phase.COLLECTING).accepting_data
    for phase in (Phase.CLOSED, Phase.DONE):
        run = make_run(
            phase=phase,
            disposition=Disposition.FINALIZED,
            closed_at=STARTED,
            done_at=STARTED if phase is Phase.DONE else None,
        )
        assert not run.accepting_data


# ------------------------------------------------------------------- expiring


def test_a_run_is_past_its_limit_counting_from_when_it_started():
    run = make_run()
    assert not run.is_past_its_limit(3600, now=STARTED + timedelta(minutes=59))
    assert run.is_past_its_limit(3600, now=STARTED + timedelta(hours=1))
    assert run.is_past_its_limit(3600, now=STARTED + timedelta(days=3))


def test_a_closed_run_is_never_past_its_limit():
    """A run waiting for the sweep is waiting on Pig, not on the participant."""
    run = make_run(
        phase=Phase.CLOSED, disposition=Disposition.FINALIZED, closed_at=STARTED
    )
    assert not run.is_past_its_limit(1, now=STARTED + timedelta(days=365))


# ------------------------------------------------------- the reported status


@pytest.mark.parametrize(
    "phase, disposition, expected",
    [
        (Phase.COLLECTING, None, "in_progress"),
        (Phase.CLOSED, Disposition.FINALIZED, "finalizing"),
        (Phase.DONE, Disposition.FINALIZED, "complete"),
        (Phase.CLOSED, Disposition.EXPIRED, "expired"),
        (Phase.DONE, Disposition.EXPIRED, "expired"),
    ],
)
def test_every_stored_state_maps_to_a_status_the_api_documents(
    phase, disposition, expected
):
    assert runs.api_status(phase, disposition) == expected
    assert expected in runs.API_STATUSES


def test_expired_says_nothing_about_whether_the_run_has_been_finished():
    """The asymmetry in the API vocabulary, pinned so a change to it is deliberate.

    A finalized run reports `finalizing` before the sweep and `complete` after. An
    expired one reports `expired` either way; `pig runs` shows the phase for whoever
    needs the difference. See issue #16.
    """
    waiting = runs.api_status(Phase.CLOSED, Disposition.EXPIRED)
    finished = runs.api_status(Phase.DONE, Disposition.EXPIRED)
    assert waiting == finished == "expired"

    assert runs.api_status(Phase.CLOSED, Disposition.FINALIZED) != runs.api_status(
        Phase.DONE, Disposition.FINALIZED
    )


# ------------------------------------------------------------- reading a row


def test_a_row_round_trips_through_the_model(pig):
    run_id = pig.start_run("stroop", {"participant_id": "PPT-1003", "session": "Base"})[
        "run_id"
    ]
    run = runs.get(pig.connection, run_id)
    assert run is not None
    assert run.phase is Phase.COLLECTING
    assert run.disposition is None
    assert run.parameters == {"participant_id": "PPT-1003", "session": "Base"}
    assert run.run_key_names == ["participant_id", "session"]
    assert run.run_key_hash == runs.hash_run_key(["PPT-1003", "Base"])
    assert run.started_at.tzinfo is not None
    assert run.closed_at is None
    assert run.done_at is None


def test_the_run_key_is_stored_only_as_a_hash(pig):
    """The values are one join away, so this isn't protection. It's just that a column
    only ever compared has no reason to be readable."""
    pig.start_run("stroop", {"participant_id": "PPT-1003", "session": "Base"})
    row = pig.connection.execute("SELECT * FROM runs").fetchone()
    assert "PPT-1003" not in " ".join(str(value) for value in tuple(row))
    assert "parameters" not in row


def test_an_unknown_run_is_none(pig):
    assert runs.get(pig.connection, "not-a-run-id") is None


def test_a_stored_time_without_an_offset_is_read_as_utc():
    """One naive datetime would make the sweep's arithmetic raise rather than skip a run."""
    naive = db.parse_time("2026-07-26T18:00:00")
    assert naive.tzinfo is not None
    assert naive == STARTED
    # And anything Pig wrote itself round-trips unchanged.
    assert db.parse_time(db.stamp(STARTED)) == STARTED


# ------------------------------------------------------------------ writing


def test_closing_a_run_twice_only_counts_once(pig):
    """Two sweeps racing each other must not both report the same run expired."""
    run_id = pig.start_run("stroop", {"participant_id": "1", "session": "a"})["run_id"]
    run = runs.get(pig.connection, run_id)
    assert run is not None

    assert runs.mark_closed(pig.connection, run, Disposition.EXPIRED, db.now())
    assert not runs.mark_closed(pig.connection, run, Disposition.EXPIRED, db.now())


def test_a_run_cannot_be_marked_done_before_it_is_closed(pig):
    run_id = pig.start_run("stroop", {"participant_id": "1", "session": "a"})["run_id"]
    run = runs.get(pig.connection, run_id)
    assert run is not None
    assert not runs.mark_done(pig.connection, run, db.now())


def test_counts_are_reported_in_the_api_vocabulary(pig):
    first = pig.start_run("stroop", {"participant_id": "1", "session": "a"})["run_id"]
    pig.start_run("stroop", {"participant_id": "2", "session": "a"})
    pig.finalize_run("stroop", first)

    counts = runs.counts_by_api_status(pig.connection, "stroop")
    assert counts == {
        "in_progress": 1,
        "finalizing": 1,
        "complete": 0,
        "expired": 0,
    }


# --------------------------------------------------- what the database refuses
# The model can't produce these. Something editing rows by hand could, which is why the
# constraints are in the schema rather than in a check the application remembers to do.


def insert_raw(connection: sqlite3.Connection, **columns) -> None:
    row = {
        "run_id": "r",
        "task_code": "stroop",
        "run_key_hash": "k",
        "run_number": 1,
        "phase": "collecting",
        "disposition": None,
        "started_at": "2026-07-26T18:00:00+00:00",
        "closed_at": None,
        "done_at": None,
        **columns,
    }
    placeholders = ", ".join("?" for _ in row)
    connection.execute(
        f"INSERT INTO runs ({', '.join(row)}) VALUES ({placeholders})",
        tuple(row.values()),
    )


@pytest.mark.parametrize(
    "label, columns",
    [
        ("a collecting run with a disposition", {"disposition": "expired"}),
        (
            "a closed run with no disposition",
            {"phase": "closed", "closed_at": "2026-07-26T19:00:00+00:00"},
        ),
        (
            "a collecting run that has been closed",
            {"closed_at": "2026-07-26T19:00:00+00:00"},
        ),
        (
            "a done run with no done_at",
            {
                "phase": "done",
                "disposition": "finalized",
                "closed_at": "2026-07-26T19:00:00+00:00",
            },
        ),
        (
            "a closed run with a done_at",
            {
                "phase": "closed",
                "disposition": "finalized",
                "closed_at": "2026-07-26T19:00:00+00:00",
                "done_at": "2026-07-26T20:00:00+00:00",
            },
        ),
        ("a phase that doesn't exist", {"phase": "filed", "disposition": "expired"}),
        (
            "a disposition that doesn't exist",
            {
                "phase": "closed",
                "disposition": "abandoned",
                "closed_at": "2026-07-26T19:00:00+00:00",
            },
        ),
    ],
)
def test_the_database_refuses_an_inconsistent_run(pig, label, columns):
    with pytest.raises(sqlite3.IntegrityError):
        insert_raw(pig.connection, **columns)


def test_the_legal_states_are_all_storable(pig):
    """The other half of the constraints: they refuse nothing a real run needs."""
    legal = [
        {"phase": "collecting"},
        {"phase": "closed", "disposition": "finalized", "closed_at": "T"},
        {"phase": "closed", "disposition": "expired", "closed_at": "T"},
        {"phase": "done", "disposition": "finalized", "closed_at": "T", "done_at": "T"},
        {"phase": "done", "disposition": "expired", "closed_at": "T", "done_at": "T"},
    ]
    for number, columns in enumerate(legal, start=1):
        insert_raw(pig.connection, run_id=f"r{number}", run_number=number, **columns)


# ------------------------------------------------------------ the database file


def test_a_database_from_another_version_of_pig_is_refused(tmp_path):
    """No migration, so the failure has to be a readable one rather than "no such
    column" from the first query."""
    path = tmp_path / "pig.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE runs (run_id TEXT, dataset_path TEXT)")
    old.commit()
    old.close()

    with pytest.raises(db.DatabaseProblem) as raised:
        db.connect(path)
    assert "delete it" in str(raised.value)


def test_a_fresh_database_records_its_version(tmp_path):
    connection = db.connect(tmp_path / "pig.db")
    assert connection.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    # And opening it again is fine.
    db.connect(tmp_path / "pig.db").close()
