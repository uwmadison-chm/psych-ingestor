"""Task definitions spread over include directories, a file or a few per study, each
with its own public keys. See issue #36."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from psych_ingestor import keys
from psych_ingestor.config import ConfigSource, ConfigurationError, load_config

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

MAIN = """
data_root = "./data"
database = "./pig.db"
include = ["studies"]

[task.stroop]
path_parameters = ["participant_id"]
run_key_parameters = ["participant_id"]
"""

LUCIDA = """
public_keys_file = "public_keys.txt"

[task.teddy]
path_parameters = ["id", "event"]
run_key_parameters = ["id", "event"]
encrypt_to = "lucida/2026a"
"""


def export_keys(tmp_path: Path, name: str, into: Path) -> None:
    """What a study's own pigsty does: make a key, export its public half, and the file
    gets copied to the web server."""
    secure = tmp_path / "secure" / name.split("/")[0]
    keys.new_private_key(secure, name, NOW)
    into.write_text(keys.public_keys_text(keys.read_all_keys(secure), secure, NOW))


@pytest.fixture
def main(tmp_path: Path) -> Path:
    path = tmp_path / "pig.toml"
    path.write_text(MAIN)
    studies = tmp_path / "studies"
    (studies / "lucida").mkdir(parents=True)
    (studies / "lucida.toml").write_text(LUCIDA)
    export_keys(tmp_path, "lucida/2026a", studies / "public_keys.txt")
    return path


def touch(path: Path) -> None:
    """Make sure a change is visible to a modification-time check."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


def test_included_tasks_are_tasks(main: Path, tmp_path: Path):
    config = load_config(main)
    assert sorted(config.task) == ["stroop", "teddy"]
    assert config.task["stroop"].source == main
    assert config.task["teddy"].source == tmp_path / "studies" / "lucida.toml"
    assert config.task["teddy"].code == "teddy"


def test_an_included_files_keys_are_relative_to_it(main: Path, tmp_path: Path):
    config = load_config(main)
    assert config.task["teddy"].public_key
    assert config.key_files == [(tmp_path / "studies" / "public_keys.txt").resolve()]


def test_only_toml_files_directly_inside_are_read(main: Path, tmp_path: Path):
    studies = tmp_path / "studies"
    broken = "this isn't [ toml"
    (studies / ".lucida.toml.swp").write_text(broken)
    (studies / ".hidden.toml").write_text(broken)
    (studies / "notes.txt").write_text(broken)
    (studies / "lucida" / "nested.toml").write_text(broken)
    assert sorted(load_config(main).task) == ["stroop", "teddy"]


def test_a_task_defined_twice_names_both_files(main: Path, tmp_path: Path):
    (tmp_path / "studies" / "other.toml").write_text(
        '[task.stroop]\npath_parameters = ["p"]\nrun_key_parameters = ["p"]\n'
    )
    with pytest.raises(ConfigurationError) as raised:
        load_config(main)
    message = str(raised.value)
    assert "'stroop' is defined twice" in message
    assert str(main) in message
    assert "other.toml" in message


def test_a_task_uses_only_its_own_files_keys(main: Path, tmp_path: Path):
    """The main file has keys too, but they don't reach an included file's tasks."""
    (tmp_path / "public_keys.txt").write_text(
        (tmp_path / "studies" / "public_keys.txt").read_text()
    )
    main.write_text(
        MAIN.replace("include", 'public_keys_file = "public_keys.txt"\ninclude')
    )
    lucida = tmp_path / "studies" / "lucida.toml"
    lucida.write_text(LUCIDA.replace('public_keys_file = "public_keys.txt"\n', ""))
    with pytest.raises(ConfigurationError) as raised:
        load_config(main)
    assert str(lucida) in str(raised.value)
    assert "no public_keys_file in this file" in str(raised.value)


def test_one_studys_task_cant_encrypt_to_anothers_key(main: Path, tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    export_keys(tmp_path, "other/2026a", other / "public_keys.txt")
    main.write_text(MAIN.replace('["studies"]', '["studies", "other"]'))
    (other / "dd.toml").write_text(
        'public_keys_file = "public_keys.txt"\n'
        "[task.dd]\n"
        'path_parameters = ["p"]\n'
        'run_key_parameters = ["p"]\n'
        'encrypt_to = "lucida/2026a"\n'
    )
    with pytest.raises(ConfigurationError) as raised:
        load_config(main)
    assert "has no key by that name" in str(raised.value)


def test_an_included_file_cant_say_where_data_goes(main: Path, tmp_path: Path):
    (tmp_path / "studies" / "lucida.toml").write_text(
        'data_root = "/elsewhere"\n' + LUCIDA
    )
    with pytest.raises(ConfigurationError) as raised:
        load_config(main)
    assert "belong in the main pig.toml" in str(raised.value)


def test_a_missing_include_directory_is_refused(main: Path):
    main.write_text(MAIN.replace('["studies"]', '["studies", "nowhere"]'))
    with pytest.raises(ConfigurationError) as raised:
        load_config(main)
    assert "nowhere" in str(raised.value)


def test_a_directory_listed_twice_is_refused(main: Path):
    main.write_text(MAIN.replace('["studies"]', '["studies", "./studies"]'))
    with pytest.raises(ConfigurationError) as raised:
        load_config(main)
    assert "more than once" in str(raised.value)


# ------------------------------------------------------------------ under a service


def test_a_new_studys_file_appears_without_a_restart(main: Path, tmp_path: Path):
    source = ConfigSource(main)
    assert "dd" not in source.current().task
    (tmp_path / "studies" / "dd.toml").write_text(
        '[task.dd]\npath_parameters = ["p"]\nrun_key_parameters = ["p"]\n'
    )
    assert "dd" in source.current().task


def test_removing_a_studys_file_removes_its_tasks(main: Path, tmp_path: Path):
    source = ConfigSource(main)
    assert "teddy" in source.current().task
    (tmp_path / "studies" / "lucida.toml").unlink()
    assert "teddy" not in source.current().task


def test_editing_an_included_file_takes_effect(main: Path, tmp_path: Path):
    source = ConfigSource(main)
    assert source.current().task["teddy"].open
    lucida = tmp_path / "studies" / "lucida.toml"
    lucida.write_text(LUCIDA + "open = false\n")
    touch(lucida)
    assert not source.current().task["teddy"].open


def test_a_broken_included_file_keeps_the_last_good_one_serving(
    main: Path, tmp_path: Path
):
    source = ConfigSource(main)
    source.current()
    lucida = tmp_path / "studies" / "lucida.toml"
    lucida.write_text(LUCIDA.replace("[task.teddy]", "[task.teddy"))
    touch(lucida)
    assert "teddy" in source.current().task
    assert source.problem and "lucida.toml" in source.problem


def test_a_new_copy_of_an_included_keys_file_takes_effect(main: Path, tmp_path: Path):
    source = ConfigSource(main)
    before = source.current().task["teddy"].public_key
    secure = tmp_path / "secure" / "lucida"
    for key in secure.rglob("*.key"):
        key.unlink()
    export_keys(tmp_path, "lucida/2026a", tmp_path / "studies" / "public_keys.txt")
    touch(tmp_path / "studies" / "public_keys.txt")
    after = source.current().task["teddy"].public_key
    assert after and after != before
