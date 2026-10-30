"""The lock that sweep, admit, and organize each hold while they work."""

import errno
import fcntl
import subprocess
import sys
from pathlib import Path

import pytest

from psych_ingestor import storage
from psych_ingestor.storage import LockHeld, hold_lock


def another_process_can_lock(path: Path) -> bool:
    """Whether another process could take the lock on `path` right now. A POSIX lock is
    held per process, so this process can't find out by trying itself."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, sys; lock = open(sys.argv[1], 'a')\n"
                "try:\n"
                "    fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "except OSError:\n"
                "    print('held')\n"
                "else:\n"
                "    print('free')\n"
            ),
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout == "free\n"


def test_holding_the_lock_keeps_other_processes_out(tmp_path: Path):
    path = tmp_path / "job.lock"
    with hold_lock(path):
        assert not another_process_can_lock(path)
    assert another_process_can_lock(path)


def test_the_lock_file_is_made_and_left_behind(tmp_path: Path):
    path = tmp_path / "job.lock"
    with hold_lock(path):
        assert path.is_file()
    assert path.read_bytes() == b""


def test_whats_in_an_existing_lock_file_is_left_alone(tmp_path: Path):
    path = tmp_path / "job.lock"
    path.write_text("not ours\n")
    with hold_lock(path):
        pass
    assert path.read_text() == "not ours\n"


def test_a_lock_another_process_holds_raises_before_the_block_runs(
    tmp_path: Path, locked_elsewhere
):
    path = tmp_path / "job.lock"
    ran = False
    with locked_elsewhere(path), pytest.raises(LockHeld), hold_lock(path):
        ran = True
    assert not ran


def test_the_lock_can_be_taken_once_the_other_process_lets_go(
    tmp_path: Path, locked_elsewhere
):
    path = tmp_path / "job.lock"
    with locked_elsewhere(path):
        pass
    with hold_lock(path):
        assert not another_process_can_lock(path)


def test_the_lock_is_let_go_when_the_block_raises(tmp_path: Path):
    path = tmp_path / "job.lock"

    class Oops(Exception):
        pass

    with pytest.raises(Oops), hold_lock(path):
        raise Oops
    assert another_process_can_lock(path)
    with hold_lock(path):
        pass


def test_an_error_inside_the_block_isnt_mistaken_for_a_held_lock(tmp_path: Path):
    path = tmp_path / "job.lock"
    with pytest.raises(OSError) as raised, hold_lock(path):
        raise OSError(errno.EAGAIN, "from the work, not the lock")
    assert not isinstance(raised.value, LockHeld)


def test_a_second_hold_in_the_same_process_is_refused(tmp_path: Path):
    """POSIX would let this process lock the file twice, and the inner `with` closing
    its copy of the file would quietly let go of the outer one's lock too."""
    path = tmp_path / "job.lock"
    with hold_lock(path):
        with pytest.raises(LockHeld), hold_lock(path):
            pass
        # The refused hold didn't cost the outer one its lock.
        assert not another_process_can_lock(path)
    assert another_process_can_lock(path)
    with hold_lock(path):
        pass


def test_the_same_file_named_another_way_is_the_same_lock(tmp_path: Path):
    path = tmp_path / "job.lock"
    (tmp_path / "sub").mkdir()
    (tmp_path / "link.lock").symlink_to(path)
    with hold_lock(path):
        with pytest.raises(LockHeld), hold_lock(tmp_path / "sub" / ".." / "job.lock"):
            pass
        with pytest.raises(LockHeld), hold_lock(tmp_path / "link.lock"):
            pass
        assert not another_process_can_lock(path)


def test_different_files_are_different_locks(tmp_path: Path):
    with hold_lock(tmp_path / "sweep.lock"):
        with hold_lock(tmp_path / "admit.lock"):
            assert not another_process_can_lock(tmp_path / "admit.lock")
        assert another_process_can_lock(tmp_path / "admit.lock")
        assert not another_process_can_lock(tmp_path / "sweep.lock")


def test_a_share_that_cant_lock_is_an_error_not_a_busy_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Reporting this as "another job is running" would have every run say so, forever,
    and nobody would look."""

    def no_locks_here(file, cmd):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "lockf", no_locks_here)
    path = tmp_path / "job.lock"
    with pytest.raises(OSError) as raised, hold_lock(path):
        pass
    assert raised.value.errno == errno.ENOLCK
    assert not isinstance(raised.value, LockHeld)

    # And the failed attempt doesn't leave this process thinking it holds the lock.
    monkeypatch.undo()
    assert storage._held_locks == set()
    with hold_lock(path):
        pass
