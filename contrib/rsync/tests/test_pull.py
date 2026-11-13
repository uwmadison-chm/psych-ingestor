"""What `pigsty-pull` asks rsync to do, given a pigsty.toml. rsync itself isn't
involved: a stand-in records the command it was given."""

import datetime
import json
import stat
import sys
from pathlib import Path

import pytest

from pig_rsync.pull import main

PIGSTY = """
incoming_dir = "./incoming"

[task.stroop]
datastore_dir = "./stroop-data"
organized = "./organized/stroop/{run_number}"

[task.balloons]
datastore_dir = "./balloons-data"
organized = "./organized/balloons/{run_number}"
"""


@pytest.fixture
def fake_rsync(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in for rsync that writes the arguments it got to a file, and exits with
    the status in `status`, if there is one."""
    record = tmp_path / "rsync-args.json"
    script = tmp_path / "rsync"
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


def run(tmp_path: Path, rsync: Path, *more: str) -> int:
    config = tmp_path / "pigsty.toml"
    return main(
        ["--from", "pig-pull:", "--config", str(config)]
        + ["--rsync", str(rsync), *more]
    )


def test_one_pull_into_incoming_with_an_include_per_task(tmp_path, fake_rsync):
    rsync, record = fake_rsync
    write_config(tmp_path)
    (tmp_path / "incoming").mkdir()

    assert run(tmp_path, rsync) == 0

    args = json.loads(record.read_text())
    assert args[-2:] == ["pig-pull:/", f"{tmp_path / 'incoming'}/"]
    assert "--delete" in args and "--ignore-existing" in args
    assert "--archive" not in args
    filters = [arg for arg in args if arg.startswith(("--include=", "--exclude="))]
    assert filters == ["--include=/balloons/", "--include=/stroop/", "--exclude=/*"]
    assert "--backup" not in args


def test_keep_purged_moves_deleted_runs_into_a_directory_for_today(
    tmp_path, fake_rsync
):
    rsync, record = fake_rsync
    write_config(tmp_path)
    (tmp_path / "incoming").mkdir()
    (tmp_path / "purged").mkdir()

    before = datetime.date.today()
    assert run(tmp_path, rsync, "--keep-purged", str(tmp_path / "purged")) == 0
    after = datetime.date.today()

    args = json.loads(record.read_text())
    assert "--backup" in args
    backup_dirs = {
        f"--backup-dir={tmp_path / 'purged' / day.isoformat()}"
        for day in (before, after)
    }
    assert backup_dirs & set(args)


def test_ssh_config_and_anything_after_dashes_reach_rsync(tmp_path, fake_rsync):
    rsync, record = fake_rsync
    write_config(tmp_path)
    (tmp_path / "incoming").mkdir()

    run(
        tmp_path,
        rsync,
        "--ssh-config",
        "/etc/pigsty/circe/ssh/config",
        "--",
        "--dry-run",
    )

    args = json.loads(record.read_text())
    assert args[args.index("-e") + 1] == "ssh -F /etc/pigsty/circe/ssh/config"
    # Before the source and destination, where rsync expects its options.
    assert args.index("--dry-run") < args.index("pig-pull:/")


def test_a_missing_incoming_runs_nothing(tmp_path, fake_rsync, capsys):
    rsync, record = fake_rsync
    write_config(tmp_path)

    assert run(tmp_path, rsync) == 1
    assert not record.exists()
    assert "pigsty check --fix" in capsys.readouterr().err


def test_a_symlinked_incoming_runs_nothing(tmp_path, fake_rsync, capsys):
    rsync, record = fake_rsync
    write_config(tmp_path)
    (tmp_path / "done").mkdir()
    (tmp_path / "incoming").symlink_to(tmp_path / "done")

    assert run(tmp_path, rsync) == 1
    assert not record.exists()
    assert "symbolic link" in capsys.readouterr().err


def test_a_missing_keep_purged_runs_nothing(tmp_path, fake_rsync, capsys):
    rsync, record = fake_rsync
    write_config(tmp_path)
    (tmp_path / "incoming").mkdir()

    assert run(tmp_path, rsync, "--keep-purged", str(tmp_path / "purged")) == 1
    assert not record.exists()
    assert "no directory there" in capsys.readouterr().err


def test_keep_purged_inside_incoming_runs_nothing(tmp_path, fake_rsync, capsys):
    rsync, record = fake_rsync
    write_config(tmp_path)
    (tmp_path / "incoming" / "purged").mkdir(parents=True)

    kept = str(tmp_path / "incoming" / "purged")
    assert run(tmp_path, rsync, "--keep-purged", kept) == 1
    assert not record.exists()
    assert "one inside the other" in capsys.readouterr().err


def test_no_incoming_setting_runs_nothing(tmp_path, fake_rsync, capsys):
    rsync, record = fake_rsync
    write_config(tmp_path, PIGSTY.replace('incoming_dir = "./incoming"', ""))

    assert run(tmp_path, rsync) == 1
    assert not record.exists()
    assert "no incoming_dir" in capsys.readouterr().err


def test_rsyncs_failure_is_this_commands_failure(tmp_path, fake_rsync):
    rsync, _ = fake_rsync
    write_config(tmp_path)
    (tmp_path / "incoming").mkdir()
    (tmp_path / "status").write_text("23")

    assert run(tmp_path, rsync) == 23
