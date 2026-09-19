from pathlib import Path

import pytest

from psych_ingestor.config import (
    ConfigurationError,
    describe_size,
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

[task.balloons]
parameters = ["participant_id"]
run_key = ["participant_id"]
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
"""
    with pytest.raises(ConfigurationError):
        load_config(write_config(tmp_path, config_text))


def test_load_config_says_path_is_gone(tmp_path: Path) -> None:
    """A configuration from before issue #3 says where data lands. It doesn't any more,
    and the error should say so rather than "extra inputs are not permitted"."""
    config_text = """
data_root = "./data"
database = "./pig.db"

[task.stroop]
parameters = ["participant_id"]
run_key = ["participant_id"]
path = "{participant_id}/{run_number}.jsonl"
"""
    with pytest.raises(ConfigurationError) as raised:
        load_config(write_config(tmp_path, config_text))
    assert "no longer a task setting" in str(raised.value)


def test_load_config_says_what_abandon_after_is_called_now(tmp_path: Path) -> None:
    config_text = """
data_root = "./data"
database = "./pig.db"

[task.stroop]
parameters = ["participant_id"]
run_key = ["participant_id"]
abandon_after = "24h"
"""
    with pytest.raises(ConfigurationError) as raised:
        load_config(write_config(tmp_path, config_text))
    assert "expires_after" in str(raised.value)


def test_expires_after_reads_a_duration(tmp_path: Path) -> None:
    config_text = """
data_root = "./data"
database = "./pig.db"

[task.stroop]
parameters = ["participant_id"]
run_key = ["participant_id"]
expires_after = "7d"
"""
    config = load_config(write_config(tmp_path, config_text))
    assert config.task["stroop"].expires_after == 7 * 86400


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


# --- media ---


def test_media_is_off_unless_asked_for(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, VALID_CONFIG))
    assert config.task["stroop"].media is False
    assert config.task["stroop"].max_part_size == 8 * 1024 * 1024


def test_max_part_size_reads_a_size(tmp_path: Path) -> None:
    config_text = """
data_root = "./data"
database = "./pig.db"

[task.interview]
parameters = ["participant_id"]
run_key = ["participant_id"]
media = true
max_part_size = "2M"
"""
    config = load_config(write_config(tmp_path, config_text))
    assert config.task["interview"].media is True
    assert config.task["interview"].max_part_size == 2 * 1024 * 1024


def test_describe_size() -> None:
    assert describe_size(8 * 1024 * 1024) == "8M"
    assert describe_size(1024) == "1k"
    assert describe_size(1500) == "1500 bytes"
