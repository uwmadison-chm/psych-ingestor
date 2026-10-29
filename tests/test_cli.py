"""The command lines' own shape, apart from what each command does."""

import os
import pwd
import stat

import pytest

from psych_ingestor import db
from psych_ingestor.cli import app as pig
from psych_ingestor.cli import health_command
from psych_ingestor.sty_cli import app as pigsty


def listed_commands(help_text: str) -> set[str]:
    """The command names in the help: the first word on each line of a panel."""
    words = (line.strip("│ ").split(" ", 1)[0] for line in help_text.splitlines())
    return {word for word in words if word.isalpha()}


def test_pig_has_the_web_sides_commands(capsys):
    pig(["--help"], result_action="return_value")
    listed = listed_commands(capsys.readouterr().out)

    assert {"check", "health", "runs", "serve", "sweep"} <= listed
    assert "organize" not in listed


def test_pigsty_has_the_secure_sides_commands(capsys):
    pigsty(["--help"], result_action="return_value")
    listed = listed_commands(capsys.readouterr().out)

    assert {"check", "organize"} <= listed
    assert "serve" not in listed


def test_pig_health_exits_like_the_health_check_answers(config_path, pig, capsys):
    """0 when GET /health would answer 200, 1 when it would answer 503."""
    health_command(config=config_path)
    assert '"ok": true' in capsys.readouterr().out

    # A finalized run the sweep hasn't finished in hours is something to look at.
    run_id = pig.start_run("stroop", {"participant_id": "1", "session": "a"})["run_id"]
    pig.finalize_run("stroop", run_id)
    pig.connection.execute(
        "UPDATE runs SET closed_at = '2000-01-01T00:00:00+00:00' WHERE run_id = ?",
        (run_id,),
    )
    with pytest.raises(SystemExit) as exited:
        health_command(config=config_path)
    assert exited.value.code == 1
    assert '"ok": false' in capsys.readouterr().out


# ------------------------------------------------------------------- check --fix

STY = """
private_keys_dir = "./private_keys"

[task.stroop]
datastore_base = "./mount/stroop-data"
organized = "./mount/organized/stroop/{participant_id}_{run_number}"
"""


def test_pig_check_lists_directories_that_arent_there(config_path, capsys):
    pig(["check", "--config", str(config_path)], result_action="return_value")

    out = capsys.readouterr().out
    assert (
        f"Not there yet:\n  {config_path.parent / 'data'}\n"
        f"  {config_path.parent / 'pig.db'}\n"
    ) in out
    assert not (config_path.parent / "data").exists()
    assert not (config_path.parent / "pig.db").exists()


def test_pig_check_fix_makes_the_data_directory_and_the_database(config_path, capsys):
    pig(["check", "--config", str(config_path), "--fix"], result_action="return_value")

    out = capsys.readouterr().out
    assert f"Made {config_path.parent / 'data'}" in out
    assert f"Made {config_path.parent / 'pig.db'}" in out
    assert "Not there yet" not in out
    assert (config_path.parent / "data").is_dir()
    connection = db.connect(config_path.parent / "pig.db")
    assert connection.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_pigsty_check_refuses_a_missing_private_keys_dir_and_says_how_to_fix_it(
    tmp_path, capsys
):
    sty = tmp_path / "pigsty.toml"
    sty.write_text(STY)

    with pytest.raises(SystemExit) as exited:
        pigsty(["check", "--config", str(sty)], result_action="return_value")

    assert exited.value.code == 1
    assert "pigsty check --fix makes it" in capsys.readouterr().err
    assert not (tmp_path / "private_keys").exists()


def test_pigsty_check_lists_directories_that_arent_there(tmp_path, capsys):
    sty = tmp_path / "pigsty.toml"
    sty.write_text(STY)
    (tmp_path / "private_keys").mkdir(mode=0o700)

    pigsty(["check", "--config", str(sty)], result_action="return_value")

    out = capsys.readouterr().out
    assert f"  {tmp_path / 'mount' / 'stroop-data' / 'incoming'}\n" in out
    assert f"  {tmp_path / 'mount' / 'stroop-data' / 'runs'}\n" in out
    assert f"  {tmp_path / 'mount' / 'organized' / 'stroop'}\n" in out
    assert not (tmp_path / "mount").exists()


def test_pigsty_check_fix_makes_every_directory_it_needs(tmp_path, capsys):
    sty = tmp_path / "pigsty.toml"
    sty.write_text(STY)

    pigsty(["check", "--config", str(sty), "--fix"], result_action="return_value")

    out = capsys.readouterr().out
    assert "Not there yet" not in out
    assert "none yet" in out
    for made in [
        "private_keys",
        "mount/stroop-data/incoming",
        "mount/stroop-data/runs",
        "mount/organized/stroop",
    ]:
        assert (tmp_path / made).is_dir()
        assert f"Made {tmp_path / made}" in out
    assert stat.S_IMODE((tmp_path / "private_keys").stat().st_mode) == 0o700
    assert list((tmp_path / "private_keys").iterdir()) == []


def test_pigsty_check_fix_leaves_what_is_there_alone(tmp_path, capsys):
    """A private_keys_dir others can read isn't tightened, and a key others can read is
    still refused: what to do about a key that may have been read is a person's call."""
    sty = tmp_path / "pigsty.toml"
    sty.write_text(STY)
    keys_dir = tmp_path / "private_keys"
    keys_dir.mkdir(mode=0o755)
    keys_dir.chmod(0o755)
    key = keys_dir / "lucida.key"
    key.write_text("not really a key\n")
    key.chmod(0o644)

    with pytest.raises(SystemExit):
        pigsty(["check", "--config", str(sty), "--fix"], result_action="return_value")

    assert "chmod 600" in capsys.readouterr().err
    assert stat.S_IMODE(keys_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE(key.stat().st_mode) == 0o644


# ------------------------------------------------------------------- run_as

ME = pwd.getpwuid(os.geteuid()).pw_name


def run_as(config_path, name=ME):
    config_path.write_text(f'run_as = "{name}"\n' + config_path.read_text())


@pytest.fixture
def someone_else(monkeypatch):
    """Pretend to be a user other than the one run_as names: the next ID up."""
    real = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real + 1)


@pytest.mark.parametrize(
    "command",
    [["check"], ["check", "--fix"], ["sweep"], ["runs"], ["migrate"], ["health"]],
)
def test_pig_refuses_every_command_as_the_wrong_user(
    config_path, someone_else, capsys, command
):
    """Reading commands too: opening the database in WAL mode can make files."""
    run_as(config_path)

    with pytest.raises(SystemExit) as exited:
        pig([*command, "--config", str(config_path)], result_action="return_value")

    assert exited.value.code == 1
    assert f"run pig as {ME}" in capsys.readouterr().err
    assert not (config_path.parent / "data").exists()
    assert not (config_path.parent / "pig.db").exists()


def test_pig_serve_refuses_the_wrong_user_before_listening(
    config_path, someone_else, capsys
):
    run_as(config_path)

    with pytest.raises(SystemExit):
        pig(["serve", "--config", str(config_path)], result_action="return_value")

    assert f"sudo -u {ME} pig" in capsys.readouterr().err


def test_pig_runs_as_the_user_run_as_names(config_path, capsys):
    run_as(config_path)

    pig(["check", "--config", str(config_path)], result_action="return_value")

    assert f"  run by:    {ME}\n" in capsys.readouterr().out


def test_pig_check_says_who_ran_it_without_run_as(config_path, capsys):
    pig(["check", "--config", str(config_path)], result_action="return_value")

    assert f"  run by:    {ME}\n" in capsys.readouterr().out


def test_run_as_a_user_that_doesnt_exist_is_a_configuration_error(config_path, capsys):
    run_as(config_path, "no-such-user-here")

    with pytest.raises(SystemExit):
        pig(["check", "--config", str(config_path)], result_action="return_value")

    assert "no user by that name" in capsys.readouterr().err


def test_run_as_belongs_in_the_main_file(config_path, capsys):
    studies = config_path.parent / "studies"
    studies.mkdir()
    (studies / "study.toml").write_text(f'run_as = "{ME}"\n')
    config_path.write_text('include = ["studies"]\n' + config_path.read_text())

    with pytest.raises(SystemExit):
        pig(["check", "--config", str(config_path)], result_action="return_value")

    assert "run_as belong in the main pig.toml" in capsys.readouterr().err


def test_pigsty_refuses_the_wrong_user_and_makes_nothing(
    tmp_path, someone_else, capsys
):
    sty = tmp_path / "pigsty.toml"
    sty.write_text(f'run_as = "{ME}"\n' + STY)

    with pytest.raises(SystemExit) as exited:
        pigsty(["check", "--config", str(sty), "--fix"], result_action="return_value")

    assert exited.value.code == 1
    assert f"sudo -u {ME} pigsty" in capsys.readouterr().err
    assert not (tmp_path / "private_keys").exists()
    assert not (tmp_path / "mount").exists()


def test_pigsty_check_says_who_ran_it(tmp_path, capsys):
    sty = tmp_path / "pigsty.toml"
    sty.write_text(f'run_as = "{ME}"\n' + STY)

    pigsty(["check", "--config", str(sty), "--fix"], result_action="return_value")

    assert f"Run by {ME}.\n" in capsys.readouterr().out
