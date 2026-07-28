"""Editing a task definition under a running service."""

import os
from pathlib import Path

from fastapi.testclient import TestClient

from psych_ingestor import config as config_module
from psych_ingestor.config import ConfigSource

BASELINE = {"participant_id": "10351", "session": "baseline"}


def rewrite(path: Path, old: str, new: str) -> None:
    """Edit the configuration file the way a person would, and make sure the change is
    visible to a modification-time check."""
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))
    stat = path.stat()
    # Tests run faster than the clock ticks on some filesystems.
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


def test_closing_a_task_takes_effect_without_a_restart(
    client: TestClient, config_path: Path
):
    assert client.post("/task/stroop/run", json=BASELINE).status_code == 201

    rewrite(
        config_path,
        'path = "{participant_id}/{session}_{run_number}.jsonl"',
        'path = "{participant_id}/{session}_{run_number}.jsonl"\nopen = false',
    )

    refused = client.post("/task/stroop/run", json=BASELINE)
    assert refused.status_code == 409


def test_a_new_task_appears_without_a_restart(client: TestClient, config_path: Path):
    assert client.post("/task/dd_game/run", json=BASELINE).status_code == 404

    with open(config_path, "a") as handle:
        handle.write(
            "\n[task.dd_game]\n"
            'parameters = ["participant_id"]\n'
            'run_key = ["participant_id"]\n'
            'path = "{participant_id}/dd_{run_number}.jsonl"\n'
        )

    started = client.post("/task/dd_game/run", json={"participant_id": "10351"})
    assert started.status_code == 201


def test_a_broken_file_keeps_the_last_good_one_serving(
    client: TestClient, config_path: Path
):
    """A typo in a text editor must not stop data collection for someone mid-task."""
    run_id = client.post("/task/stroop/run", json=BASELINE).json()["run_id"]

    rewrite(config_path, "[task.stroop]", "[task.stroop")  # a real typo

    # Everything keeps working on the configuration that last loaded.
    sent = client.post(f"/task/stroop/run/{run_id}", json={"1": {"data": {"t": 1}}})
    assert sent.status_code == 201
    assert client.post("/task/stroop/run", json=BASELINE).status_code == 201

    # ...and the health check is where you find out.
    health = client.get("/health")
    assert health.status_code == 503
    assert health.json()["checks"]["configuration_loads"] is False
    assert "isn't valid TOML" in health.json()["configuration_problem"]


def test_fixing_the_file_recovers(client: TestClient, config_path: Path):
    rewrite(config_path, "[task.stroop]", "[task.stroop")
    assert client.get("/health").status_code == 503

    rewrite(config_path, "[task.stroop", "[task.stroop]")
    assert client.get("/health").status_code == 200
    assert client.get("/health").json()["checks"]["configuration_loads"] is True


def test_a_deleted_file_keeps_the_last_good_one_serving(
    client: TestClient, config_path: Path
):
    config_path.unlink()
    assert client.post("/task/stroop/run", json=BASELINE).status_code == 201
    assert client.get("/health").status_code == 503


def test_an_unchanged_file_is_not_re_read(config_path: Path, monkeypatch):
    """One stat per request, not one parse per request."""
    reads = []
    real = config_module.load_config

    def counted(path):
        reads.append(path)
        return real(path)

    monkeypatch.setattr(config_module, "load_config", counted)

    source = ConfigSource(config_path)
    for _ in range(5):
        source.current()
    assert len(reads) == 1

    rewrite(config_path, "open = false", "open = true")
    source.current()
    assert len(reads) == 2
