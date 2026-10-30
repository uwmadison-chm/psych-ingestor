"""The secure side's shared ways of listing runs and building them in place: what
admit, organize, and purge all use, so they all behave the same way."""

from pathlib import Path, PurePosixPath

import pytest

from psych_ingestor import storage

# ------------------------------------------------------------------- listing


def test_directories_are_listed_sorted_without_dot_names(tmp_path: Path):
    for name in ["b", "a", ".hidden", ".a.organizing"]:
        (tmp_path / name).mkdir()
    (tmp_path / "c.txt").write_text("not a directory")

    assert storage.directories_in(tmp_path) == ["a", "b"]


def test_a_symlink_is_listed_whatever_it_points_at(tmp_path: Path):
    """So whatever handles it finds it, and says so, rather than passing it by."""
    (tmp_path / "real").mkdir()
    (tmp_path / "file").write_text("x")
    listed = tmp_path / "listed"
    listed.mkdir()
    (listed / "to-directory").symlink_to(tmp_path / "real")
    (listed / "to-file").symlink_to(tmp_path / "file")
    (listed / "dangling").symlink_to(tmp_path / "nothing")

    assert storage.directories_in(listed) == ["dangling", "to-directory", "to-file"]


def test_files_are_listed_relative_and_sorted(tmp_path: Path):
    (tmp_path / "media" / "00001").mkdir(parents=True)
    (tmp_path / "media" / "00001" / "00001.part").write_bytes(b"x")
    (tmp_path / "events.jsonl").write_text("{}\n")
    (tmp_path / "manifest.json").write_text("{}\n")

    assert storage.files_in(tmp_path) == [
        PurePosixPath("events.jsonl"),
        PurePosixPath("manifest.json"),
        PurePosixPath("media/00001/00001.part"),
    ]


def test_an_empty_directory_has_no_files(tmp_path: Path):
    (tmp_path / "media").mkdir()
    assert storage.files_in(tmp_path) == []


def test_a_symlinked_file_is_refused(tmp_path: Path):
    run = tmp_path / "run"
    run.mkdir()
    (tmp_path / "secret").write_text("x")
    (run / "events.jsonl").symlink_to(tmp_path / "secret")

    with pytest.raises(storage.SymbolicLink, match="events.jsonl is a symbolic link"):
        storage.files_in(run)


def test_a_symlinked_directory_inside_is_refused(tmp_path: Path):
    run = tmp_path / "run"
    (run / "media").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (run / "media" / "00001").symlink_to(tmp_path / "elsewhere")

    with pytest.raises(storage.SymbolicLink, match="media/00001 is a symbolic link"):
        storage.files_in(run)


def test_a_symlinked_run_directory_is_refused(tmp_path: Path):
    (tmp_path / "real").mkdir()
    (tmp_path / "run").symlink_to(tmp_path / "real")

    with pytest.raises(storage.SymbolicLink, match="not a directory"):
        storage.files_in(tmp_path / "run")


# --------------------------------------------------------- building a directory


def test_a_run_is_built_in_scratch_and_renamed_into_place(tmp_path: Path):
    source = tmp_path / "source.part"
    source.write_bytes(b"hello")
    destination = tmp_path / "tree" / "Baseline" / "PPT-1003_0001"

    scratch = storage.fresh_scratch(destination, ".building")
    assert scratch == tmp_path / "tree" / "Baseline" / ".PPT-1003_0001.building"
    made = {scratch}
    digest = storage.copy_file(source, scratch / "media" / "x" / "1.part", made)
    storage.rename_into_place(scratch, destination, made)

    assert digest == storage.content_hash("hello")
    assert (destination / "media" / "x" / "1.part").read_bytes() == b"hello"
    assert not scratch.exists()


def test_a_scratch_directory_left_behind_is_replaced(tmp_path: Path):
    destination = tmp_path / "run"
    left = storage.fresh_scratch(destination, ".building")
    (left / "half-written").write_text("x")

    scratch = storage.fresh_scratch(destination, ".building")

    assert scratch == left
    assert list(scratch.iterdir()) == []


def test_nothing_is_renamed_over_something_already_there(tmp_path: Path):
    destination = tmp_path / "run"
    destination.mkdir()
    scratch = storage.fresh_scratch(destination, ".building")

    with pytest.raises(OSError, match="already exists"):
        storage.rename_into_place(scratch, destination, {scratch})
    assert scratch.is_dir()


@pytest.fixture
def fsynced(monkeypatch) -> list[Path]:
    synced: list[Path] = []
    real = storage.fsync_directory

    def record(directory: Path) -> None:
        synced.append(Path(directory))
        real(directory)

    monkeypatch.setattr(storage, "fsync_directory", record)
    return synced


def test_every_new_directory_is_fsynced(tmp_path: Path, fsynced):
    """Each directory made, inside the run and above it, holds a new entry that's only
    durable once the directory it's in is fsynced."""
    source = tmp_path / "source"
    source.write_bytes(b"x")
    destination = tmp_path / "tree" / "Baseline" / "run"

    scratch = storage.fresh_scratch(destination, ".building")
    made = {scratch}
    storage.copy_file(source, scratch / "media" / "a" / "1.part", made)
    storage.rename_into_place(scratch, destination, made)

    # tree/ and Baseline/, made on the way.
    assert tmp_path in fsynced
    assert tmp_path / "tree" in fsynced
    # Everything inside the run, before it was renamed.
    assert scratch in fsynced
    assert scratch / "media" in fsynced
    assert scratch / "media" / "a" in fsynced
    # And the rename itself.
    assert fsynced[-1] == tmp_path / "tree" / "Baseline"
