from pathlib import Path

import pytest

from psych_ingestor.config import (
    ConfigurationError,
    is_safe_value,
    load_config,
    parse_duration,
    parse_size,
)

VALID_CONFIG = """
data_root = "./data"
database = "./pig.db"

[task.stroop]
parameters = ["participant_id", "session"]
run_key = ["participant_id", "session"]
path = "{participant_id}/{session}_{run_number}.jsonl"

[task.balloons]
parameters = ["participant_id"]
run_key = ["participant_id"]
path = "{participant_id}/balloons_{run_number}.jsonl"
"""


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "pig.toml"
    path.write_text(text)
    return path


# --- is_safe_value ---


@pytest.mark.parametrize(
    "value",
    ["a", "abc123", "ABC", "under_score", "trailing-dash-", "a-b-c", "10351", "x" * 64],
)
def test_is_safe_value_accepts(value: str) -> None:
    assert is_safe_value(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "x" * 65,
        "-leading",
        "has.dot",
        "has space",
        "has/slash",
        "café",
        "..",
    ],
)
def test_is_safe_value_rejects(value: str) -> None:
    assert not is_safe_value(value)


# --- parse_size ---


def test_parse_size_units() -> None:
    assert parse_size("1M") == 1048576
    assert parse_size("500k") == 512000


def test_parse_size_bare_int() -> None:
    assert parse_size(2048) == 2048


def test_parse_size_bare_numeric_string() -> None:
    assert parse_size("2048") == 2048


def test_parse_size_unknown_unit_raises() -> None:
    with pytest.raises(ValueError):
        parse_size("1X")


def test_parse_size_garbage_raises() -> None:
    with pytest.raises(ValueError):
        parse_size("banana")


# --- parse_duration ---


def test_parse_duration_units() -> None:
    assert parse_duration("30m") == 1800
    assert parse_duration("24h") == 86400
    assert parse_duration("7d") == 604800


def test_parse_duration_bare_int() -> None:
    assert parse_duration(90) == 90


def test_parse_duration_unknown_unit_raises() -> None:
    with pytest.raises(ValueError):
        parse_duration("30x")


# --- TaskDefinition.dataset_path ---


def test_dataset_path_lowercases_parameter_values(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, VALID_CONFIG))
    task = config.task["stroop"]

    path = task.dataset_path({"participant_id": "P01", "session": "Baseline"}, 1)

    assert path == Path("stroop/p01/baseline_run-0001.jsonl")


def test_dataset_path_zero_pads_run_number(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, VALID_CONFIG))
    task = config.task["balloons"]

    path = task.dataset_path({"participant_id": "p01"}, 12)

    assert path == Path("balloons/p01/balloons_run-0012.jsonl")


def test_dataset_path_prefixes_task_code(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, VALID_CONFIG))
    task = config.task["stroop"]

    path = task.dataset_path({"participant_id": "p01", "session": "1"}, 1)

    assert path.parts[0] == "stroop"


# --- load_config error cases ---


def test_load_config_rejects_invalid_toml(tmp_path: Path) -> None:
    path = write_config(tmp_path, "this is not [ valid toml")
    with pytest.raises(ConfigurationError):
        load_config(path)


def test_load_config_rejects_uppercase_task_code(tmp_path: Path) -> None:
    config_text = """
data_root = "./data"
database = "./pig.db"

[task.Stroop]
parameters = ["participant_id"]
run_key = ["participant_id"]
path = "{participant_id}/{run_number}.jsonl"
"""
    with pytest.raises(ConfigurationError):
        load_config(write_config(tmp_path, config_text))


def test_load_config_rejects_task_code_with_dot(tmp_path: Path) -> None:
    config_text = """
data_root = "./data"
database = "./pig.db"

[task."stroop.v2"]
parameters = ["participant_id"]
run_key = ["participant_id"]
path = "{participant_id}/{run_number}.jsonl"
"""
    with pytest.raises(ConfigurationError):
        load_config(write_config(tmp_path, config_text))


def test_load_config_rejects_run_key_not_in_parameters(tmp_path: Path) -> None:
    config_text = """
data_root = "./data"
database = "./pig.db"

[task.stroop]
parameters = ["participant_id"]
run_key = ["participant_id", "session"]
path = "{participant_id}/{run_number}.jsonl"
"""
    with pytest.raises(ConfigurationError):
        load_config(write_config(tmp_path, config_text))


def test_load_config_rejects_unknown_path_placeholder(tmp_path: Path) -> None:
    config_text = """
data_root = "./data"
database = "./pig.db"

[task.stroop]
parameters = ["participant_id"]
run_key = ["participant_id"]
path = "{participant_id}/{condition}_{run_number}.jsonl"
"""
    with pytest.raises(ConfigurationError):
        load_config(write_config(tmp_path, config_text))


def test_load_config_rejects_path_without_run_number(tmp_path: Path) -> None:
    config_text = """
data_root = "./data"
database = "./pig.db"

[task.stroop]
parameters = ["participant_id"]
run_key = ["participant_id"]
path = "{participant_id}/data.jsonl"
"""
    with pytest.raises(ConfigurationError):
        load_config(write_config(tmp_path, config_text))


def test_load_config_rejects_absolute_path(tmp_path: Path) -> None:
    config_text = """
data_root = "./data"
database = "./pig.db"

[task.stroop]
parameters = ["participant_id"]
run_key = ["participant_id"]
path = "/etc/{participant_id}/{run_number}.jsonl"
"""
    with pytest.raises(ConfigurationError):
        load_config(write_config(tmp_path, config_text))


def test_load_config_rejects_path_with_dotdot(tmp_path: Path) -> None:
    config_text = """
data_root = "./data"
database = "./pig.db"

[task.stroop]
parameters = ["participant_id"]
run_key = ["participant_id"]
path = "../{participant_id}/{run_number}.jsonl"
"""
    with pytest.raises(ConfigurationError):
        load_config(write_config(tmp_path, config_text))


# --- load_config path resolution ---


def test_load_config_resolves_paths_relative_to_config_file(tmp_path: Path) -> None:
    subdir = tmp_path / "somewhere"
    subdir.mkdir()
    config_path = subdir / "pig.toml"
    config_path.write_text(VALID_CONFIG)

    config = load_config(config_path)

    assert config.data_root == (subdir / "data").resolve()
    assert config.database == (subdir / "pig.db").resolve()


# --- load_config happy path ---


def test_load_config_valid_config_with_two_tasks(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, VALID_CONFIG))

    assert set(config.task) == {"stroop", "balloons"}
    assert config.task["stroop"].code == "stroop"
    assert config.task["balloons"].code == "balloons"
