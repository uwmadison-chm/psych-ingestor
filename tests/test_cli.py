"""The command lines' own shape, apart from what each command does."""

import pytest

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
