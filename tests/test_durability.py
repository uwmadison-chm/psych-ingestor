"""What Pig creates on disk has its name fsynced, not just its contents. See #54.

A new file or directory is an entry in its parent, and the entry isn't durable until the
parent is fsynced. These can't crash a machine, so they check that the fsyncs happen.
"""

from pathlib import Path

import pytest

from psych_ingestor import storage
from psych_ingestor.service import Pig


@pytest.fixture
def fsynced(monkeypatch) -> list[Path]:
    synced: list[Path] = []
    real = storage.fsync_directory

    def record(directory: Path) -> None:
        synced.append(Path(directory))
        real(directory)

    monkeypatch.setattr(storage, "fsync_directory", record)
    return synced


def test_a_new_run_directory_and_its_parents_are_fsynced(pig: Pig, fsynced):
    run_id = pig.start_run("stroop", {"participant_id": "1", "session": "a"})["run_id"]
    run_dir = storage.run_directory(pig.config.in_progress_root, "stroop", run_id)
    # in_progress/stroop/ was new too, so in_progress/ holds a new entry as well.
    assert run_dir.parent in fsynced
    assert pig.config.in_progress_root in fsynced


def test_the_first_event_fsyncs_the_new_events_file_name(pig: Pig, fsynced):
    run_id = pig.start_run("stroop", {"participant_id": "1", "session": "a"})["run_id"]
    run_dir = storage.run_directory(pig.config.in_progress_root, "stroop", run_id)
    fsynced.clear()
    pig.store_events("stroop", run_id, {"1": {"data": {}}})
    assert fsynced == [run_dir]

    # Later appends add to a file that's already there, and need no more.
    fsynced.clear()
    pig.store_events("stroop", run_id, {"2": {"data": {}}})
    assert fsynced == []


def test_a_new_media_directory_is_fsynced(pig: Pig, fsynced):
    run_id = pig.start_run("interview", {"participant_id": "1"})["run_id"]
    run_dir = storage.run_directory(pig.config.in_progress_root, "interview", run_id)
    fsynced.clear()
    pig.start_media("interview", run_id, {"event_id": "a", "data": {}})
    assert run_dir in fsynced  # for media/
    assert run_dir / "media" in fsynced  # for media/00001/
