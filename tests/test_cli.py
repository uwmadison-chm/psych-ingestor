"""The command line's own shape, apart from what each command does."""

from psych_ingestor.cli import app


def listed_commands(panel: str) -> set[str]:
    """The command names in one panel of the help: the first word on each line."""
    words = (line.strip("│ ").split(" ", 1)[0] for line in panel.splitlines())
    return {word for word in words if word.isalpha()}


def test_help_groups_commands_by_side(capsys):
    app(["--help"], result_action="return_value")
    out = capsys.readouterr().out

    web, secure, rest = (
        out.index("Web side"),
        out.index("Secure side"),
        out.index("Commands"),
    )
    assert web < secure < rest
    # check reads either kind of file, so it's listed on both sides.
    assert {"check", "health", "runs", "serve", "sweep"} <= listed_commands(
        out[web:secure]
    )
    assert "organize" not in listed_commands(out[web:secure])
    assert {"check", "organize"} <= listed_commands(out[secure:rest])
    assert "serve" not in listed_commands(out[secure:rest])
