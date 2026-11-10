"""What `pig` and `pigsty` both need to read their configuration files and run as the
right user: reading TOML, saying what went wrong, and checking `run_as`.
"""

from __future__ import annotations

import os
import pwd
import tomllib
from pathlib import Path
from typing import Any


class ConfigurationError(Exception):
    """The configuration file is wrong in a way we can describe."""


def os_error_reason(error: OSError, path: Path | None = None) -> str:
    """What went wrong, like "permission denied (/srv/stroop/runs)", without the
    "[Errno 13]" Python adds. The path is left out if it's `path`, which the message
    around it already names."""
    if not error.strerror:
        return str(error)
    reason = error.strerror.lower()
    if error.filename is not None and str(error.filename) != str(path):
        reason += f" ({error.filename})"
    return reason


def check_run_as(path: Path, run_as: str | None) -> None:
    """Raise ConfigurationError if run_as names a user this machine doesn't have."""
    if run_as is None:
        return
    try:
        pwd.getpwnam(run_as)
    except KeyError:
        raise ConfigurationError(
            f"{path} has a problem: run_as is {run_as!r}, and there's no user by that "
            "name on this machine."
        ) from None


def user_name(uid: int) -> str:
    """The name of the user with this ID, or the ID itself if it has no name."""
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"user {uid}"


def wrong_user(path: Path, run_as: str | None, command: str) -> str | None:
    """Why this process isn't allowed to run, or None if it is.

    Compared by user ID, not by name, because two names can share an ID. The effective
    ID, because that's who owns what the command makes.
    """
    if run_as is None:
        return None
    if os.geteuid() == pwd.getpwnam(run_as).pw_uid:
        return None
    running = user_name(os.geteuid())
    return (
        f"{path} says to run {command} as {run_as}, and this is {running}. Anything "
        f"it made would belong to {running}, and {run_as} might not be able to use it. "
        f"Run it as {run_as}: sudo -u {run_as} {command} ..."
    )


def read_toml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_bytes()
    except OSError as error:
        raise ConfigurationError(
            f"Can't read {path}: {os_error_reason(error, path)}."
        ) from error

    try:
        return tomllib.loads(text.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigurationError(f"{path} isn't valid TOML: {error}") from error
