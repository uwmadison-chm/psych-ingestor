import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from psych_ingestor import db
from psych_ingestor.app import create_app
from psych_ingestor.config import load_config
from psych_ingestor.service import Pig

CONFIG = """
data_root = "./data"
database = "./pig.db"

[task.stroop]
path_parameters = ["participant_id", "session"]
run_key_parameters = ["participant_id", "session"]
expires_after = "24h"

[task.balloons]
path_parameters = ["participant_id"]
run_key_parameters = ["participant_id"]
open = false

[task.interview]
path_parameters = ["participant_id"]
run_key_parameters = ["participant_id"]
media = true
max_part_size = "1k"
"""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "pig.toml"
    path.write_text(CONFIG)
    return path


@pytest.fixture
def pig(config_path: Path) -> Pig:
    config = load_config(config_path)
    config.in_progress_root.mkdir(parents=True, exist_ok=True)
    return Pig(config, db.connect(config.database))


@pytest.fixture
def client(config_path: Path) -> TestClient:
    return TestClient(create_app(config_path))


@contextmanager
def _locked_by_another_process(path: Path) -> Iterator[None]:
    # A POSIX lock is held per process, so the other holder has to be another one. It
    # takes the lock with plain `lockf` rather than Pig's helper, the way a job running
    # some other version of Pig, or on another machine, would.
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, sys; lock = open(sys.argv[1], 'a'); "
                "fcntl.lockf(lock, fcntl.LOCK_EX); print('locked', flush=True); "
                "sys.stdin.read()"
            ),
            str(path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == "locked\n"
        yield
    finally:
        holder.communicate("")


@pytest.fixture
def locked_elsewhere() -> Callable[[Path], AbstractContextManager[None]]:
    """`with locked_elsewhere(path):` holds a lock on `path` from another process."""
    return _locked_by_another_process
