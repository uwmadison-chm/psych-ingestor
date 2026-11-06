"""What `pigsty-fetch` asks rclone to do, given a pigsty.toml. rclone itself isn't
involved: a stand-in records the command it was given."""

import json
import stat
import sys
from pathlib import Path

import pytest

from pig_rclone.fetch import main

PIGSTY = """
incoming_dir = "./incoming"

[task.stroop]
datastore_base = "./stroop-data"
organized = "./organized/stroop/{run_number}"

[task.balloons]
datastore_base = "./balloons-data"
organized = "./organized/balloons/{run_number}"
"""


@pytest.fixture
def fake_rclone(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in for rclone that writes the arguments it got to a file, and exits with
    the status in `status`, if there is one."""
    record = tmp_path / "rclone-args.json"
    script = tmp_path / "rclone"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(record)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"status = pathlib.Path({str(tmp_path / 'status')!r})\n"
        "sys.exit(int(status.read_text()) if status.exists() else 0)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, record


def write_config(tmp_path: Path, text: str = PIGSTY) -> Path:
    path = tmp_path / "pigsty.toml"
    path.write_text(text)
    return path


def run(tmp_path: Path, rclone: Path, *more: str) -> int:
    config = tmp_path / "pigsty.toml"
    return main(
        ["--from", "pigbucket:chm-wilbur-01", "--config", str(config)]
        + ["--rclone", str(rclone), *more]
    )


def test_one_sync_into_incoming_with_an_include_per_task(tmp_path, fake_rclone):
    rclone, record = fake_rclone
    write_config(tmp_path)
    (tmp_path / "incoming").mkdir()

    assert run(tmp_path, rclone) == 0

    args = json.loads(record.read_text())
    assert args[:3] == ["sync", "pigbucket:chm-wilbur-01/", f"{tmp_path / 'incoming'}/"]
    assert "--checksum" in args and "--immutable" in args
    includes = [args[i + 1] for i, arg in enumerate(args) if arg == "--include"]
    assert includes == ["/balloons/**", "/stroop/**"]


def test_rclone_config_and_anything_after_dashes_reach_rclone(tmp_path, fake_rclone):
    rclone, record = fake_rclone
    write_config(tmp_path)
    (tmp_path / "incoming").mkdir()

    run(
        tmp_path,
        rclone,
        "--rclone-config",
        "/etc/pigsty/rclone.conf",
        "--",
        "--dry-run",
    )

    args = json.loads(record.read_text())
    assert args[args.index("--config") + 1] == "/etc/pigsty/rclone.conf"
    assert args[-1] == "--dry-run"


def test_a_missing_incoming_runs_nothing(tmp_path, fake_rclone, capsys):
    rclone, record = fake_rclone
    write_config(tmp_path)

    assert run(tmp_path, rclone) == 1
    assert not record.exists()
    assert "pigsty check --fix" in capsys.readouterr().err


def test_a_symlinked_incoming_runs_nothing(tmp_path, fake_rclone, capsys):
    rclone, record = fake_rclone
    write_config(tmp_path)
    (tmp_path / "done").mkdir()
    (tmp_path / "incoming").symlink_to(tmp_path / "done")

    assert run(tmp_path, rclone) == 1
    assert not record.exists()
    assert "symbolic link" in capsys.readouterr().err


def test_no_incoming_setting_runs_nothing(tmp_path, fake_rclone, capsys):
    rclone, record = fake_rclone
    write_config(tmp_path, PIGSTY.replace('incoming_dir = "./incoming"', ""))

    assert run(tmp_path, rclone) == 1
    assert not record.exists()
    assert "no incoming_dir" in capsys.readouterr().err


def test_rclones_failure_is_this_commands_failure(tmp_path, fake_rclone):
    rclone, _ = fake_rclone
    write_config(tmp_path)
    (tmp_path / "incoming").mkdir()
    (tmp_path / "status").write_text("3")

    assert run(tmp_path, rclone) == 3
