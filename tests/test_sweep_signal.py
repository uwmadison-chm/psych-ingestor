"""Touching a file when a run is finalized, so a sweep can start without waiting for the
timer. A hint, never part of storing anything. See issue #93."""

import logging
import os
from pathlib import Path

import pytest

from psych_ingestor import db
from psych_ingestor.cli import app as pig_cli
from psych_ingestor.config import ConfigurationError, load_config
from psych_ingestor.service import Pig

CONFIG = """
data_root = "./data"
database = "./pig.db"

[task.stroop]
path_parameters = ["participant_id", "session"]
run_key_parameters = ["participant_id", "session"]
"""

BASELINE = {"participant_id": "10351", "session": "baseline"}


def signalling_pig(tmp_path: Path, touch: str = "./run/sweep-due") -> Pig:
    path = tmp_path / "pig.toml"
    path.write_text(f'touch_after_finalizing = "{touch}"\n{CONFIG}')
    config = load_config(path)
    config.in_progress_root.mkdir(parents=True, exist_ok=True)
    return Pig(config, db.connect(config.database))


def test_the_path_is_relative_to_the_config_file(tmp_path):
    pig = signalling_pig(tmp_path)
    assert pig.config.touch_after_finalizing == (tmp_path / "run" / "sweep-due")


def test_unset_means_nothing_is_touched(pig: Pig):
    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    assert pig.finalize_run("stroop", run_id).status_code == 200
    assert pig.config.touch_after_finalizing is None


def test_finalizing_touches_the_file(tmp_path):
    pig = signalling_pig(tmp_path)
    signal = tmp_path / "run" / "sweep-due"
    signal.parent.mkdir()
    run_id = pig.start_run("stroop", BASELINE)["run_id"]

    assert not signal.exists()
    assert pig.finalize_run("stroop", run_id).status_code == 200
    assert signal.exists()
    assert signal.read_text() == ""


def test_touching_again_moves_the_modification_time(tmp_path):
    """Something comparing modification times has to see each finalize, not only the
    first one."""
    pig = signalling_pig(tmp_path)
    signal = tmp_path / "run" / "sweep-due"
    signal.parent.mkdir()
    signal.touch()
    os.utime(signal, (0, 0))

    run_id = pig.start_run("stroop", BASELINE)["run_id"]
    pig.finalize_run("stroop", run_id)

    assert signal.stat().st_mtime > 0


def test_a_touch_that_fails_doesnt_fail_the_finalize(tmp_path, caplog):
    """The events are on disk once the run is `finalizing`. The touch only hurries the
    sweep along, so its failing is logged and nothing else."""
    pig = signalling_pig(tmp_path)  # run/ was never made
    run_id = pig.start_run("stroop", BASELINE)["run_id"]

    with caplog.at_level(logging.WARNING):
        result = pig.finalize_run("stroop", run_id)

    assert result.status_code == 200
    assert result.status == "finalizing"
    assert "sweep-due" in caplog.text


def test_an_included_file_cant_set_it(tmp_path):
    (tmp_path / "studies").mkdir()
    (tmp_path / "studies" / "lucida.toml").write_text(
        'touch_after_finalizing = "./sweep-due"\n'
    )
    path = tmp_path / "pig.toml"
    path.write_text(f'include = ["studies"]\n{CONFIG}')

    with pytest.raises(ConfigurationError) as raised:
        load_config(path)
    assert "touch_after_finalizing, and run_as belong in the main pig.toml" in str(
        raised.value
    )


# ---------------------------------------------------------------------- pig check


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "pig.toml"
    path.write_text(f'touch_after_finalizing = "./run/sweep-due"\n{CONFIG}')
    return path


def test_pig_check_says_who_can_write_it(tmp_path, capsys):
    path = write_config(tmp_path)
    (tmp_path / "run").mkdir()

    pig_cli(["check", "--config", str(path)], result_action="return_value")

    out = capsys.readouterr().out
    assert f"touches:   {tmp_path / 'run' / 'sweep-due'}" in out
    assert "can write it" in out


def test_pig_check_lists_its_directory_when_it_isnt_there(tmp_path, capsys):
    path = write_config(tmp_path)

    pig_cli(["check", "--config", str(path)], result_action="return_value")

    out = capsys.readouterr().out
    assert f"Not there yet:\n  {tmp_path / 'data'}\n  {tmp_path / 'run'}\n" in out
    assert not (tmp_path / "run").exists()


def test_pig_check_fix_makes_its_directory(tmp_path, capsys):
    path = write_config(tmp_path)

    pig_cli(["check", "--config", str(path), "--fix"], result_action="return_value")

    assert (tmp_path / "run").is_dir()
    assert not (tmp_path / "run" / "sweep-due").exists()
    assert "can write it" in capsys.readouterr().out


def test_pig_check_refuses_a_file_it_cant_write(tmp_path, capsys, monkeypatch):
    """Faked, because tests may run as root, who can write anything."""
    path = write_config(tmp_path)
    (tmp_path / "run").mkdir()
    monkeypatch.setattr(os, "access", lambda *_: False)

    with pytest.raises(SystemExit) as exited:
        pig_cli(["check", "--config", str(path)], result_action="return_value")

    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert f"can't write {tmp_path / 'run' / 'sweep-due'}" in err
    assert "sudo -u" in err
