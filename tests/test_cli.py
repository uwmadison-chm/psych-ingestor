"""The command lines' own shape, apart from what each command does."""

from psych_ingestor.cli import app as pig
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
