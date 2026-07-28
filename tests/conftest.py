from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from psych_ingestor import db
from psych_ingestor.app import create_app
from psych_ingestor.config import load_config
from psych_ingestor.runs import Pig

CONFIG = """
data_root = "./data"
database = "./pig.db"

[task.stroop]
parameters = ["participant_id", "session"]
run_key = ["participant_id", "session"]
path = "{participant_id}/{session}_{run_number}.jsonl"
abandon_after = "24h"

[task.balloons]
parameters = ["participant_id"]
run_key = ["participant_id"]
path = "{participant_id}/balloons_{run_number}.jsonl"
open = false
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
