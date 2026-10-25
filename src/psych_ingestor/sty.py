"""The secure side's configuration, `pigsty.toml`.

The web server reads `pig.toml`; the machine where the data ends up reads `pigsty.toml`,
and no command reads both. That's what lets `pig.toml` sit on a public-facing machine:
nothing the secure side needs ever has to be in it. See issue #33.

This holds what `pigsty admit`, `pigsty organize`, `pigsty purge`, `pigsty sign` and
`pigsty keys` need: for each task, where its runs are kept, where its readable tree goes,
and which of its parameters links are signed for; the address of the web server to purge
runs from; and the directory of private keys that encrypted runs are decrypted with and
links are signed with. The keys themselves are never in this file, so it can be kept in
version control like `pig.toml`.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import (
    RESERVED_PARAMETER_PREFIX,
    SAFE_VALUE_EXPLANATION,
    ConfigurationError,
    is_safe_value,
)

DEFAULT_STY_CONFIG = "local/pigsty.toml"


# ------------------------------------------------------------------- patterns
#
# A task's `organized` is a path with `{name}` placeholders:
#
#     /secure/mystudy/intermediate_data/pig/stroop/{session}/{participant_id}_{run_number}
#
# Everything before the first placeholder's directory is the task's organized root, a
# directory that's there whatever run is being placed. The rest is its pattern, which
# each run fills in.
#
# Deliberately not Python's `str.format`, which would let a pattern reach into objects
# (`{x.__class__}`) and accept format specs nobody needs. A placeholder is a name in
# braces and nothing else; any other brace is an error.

PLACEHOLDER = re.compile(r"\{([^{}]*)\}")

# Placeholders every run can fill, whatever its task. Anything else names a parameter.
BUILT_IN_NAMES = ("task_code", "run_number")


class PatternError(ValueError):
    """A pattern that can't be used, whatever run it's given."""


def placeholder_names(pattern: str) -> list[str]:
    """The names a pattern uses, in order, or PatternError saying what's wrong with it."""
    names = []
    for match in PLACEHOLDER.finditer(pattern):
        name = match.group(1)
        if not name.isidentifier():
            raise PatternError(
                f"{{{name}}} in {pattern!r} isn't a placeholder Pig understands. A "
                "placeholder is a name in braces, like {participant_id}, with nothing "
                "else inside."
            )
        names.append(name)
    leftover = PLACEHOLDER.sub("", pattern)
    if "{" in leftover or "}" in leftover:
        raise PatternError(
            f"{pattern!r} has a brace that isn't part of a placeholder. Placeholders "
            "look like {participant_id}; braces can't appear anywhere else."
        )
    return names


def check_pattern(pattern: str) -> None:
    """Everything that can be checked about a pattern without a run to fill it from."""
    names = placeholder_names(pattern)
    if "run_number" not in names:
        raise PatternError(
            f"{pattern!r} doesn't include {{run_number}}. Without it, two runs of the "
            "same participant and session would be put in the same place."
        )
    if pattern.startswith("/"):
        raise PatternError(
            f"{pattern!r} starts with '/'. A pattern is a path inside the organized "
            "directory, so it can't be absolute."
        )


def split_organized(organized: str) -> tuple[str, str]:
    """A task's `organized` split into its root and its pattern, or PatternError.

    The root is everything up to the last `/` before the first placeholder, so
    `/secure/stroop/{session}/run-{run_number}` is the root `/secure/stroop` and the
    pattern `{session}/run-{run_number}`.
    """
    first = organized.find("{")
    slash = organized.rfind("/", 0, first) if first != -1 else -1
    if slash <= 0:
        raise PatternError(
            f"{organized!r} needs a directory before its first placeholder, like "
            "/secure/mystudy/stroop/{participant_id}/run-{run_number}. That directory "
            "is where this task's tree goes, and where pigsty keeps its record of what "
            "it has placed there."
        )
    root, pattern = organized[:slash], organized[slash + 1 :]
    if "}" in root:
        raise PatternError(
            f"{organized!r} has a brace that isn't part of a placeholder. Placeholders "
            "look like {participant_id}; braces can't appear anywhere else."
        )
    check_pattern(pattern)
    return root, pattern


# --------------------------------------------------------------- the file


class StyTask(BaseModel):
    """One task's entry in `pigsty.toml`."""

    model_config = ConfigDict(extra="forbid")

    # Where this task's runs are kept on the secure side: `incoming/`, where the copy
    # from the bucket puts them, and `runs/`, where `pigsty admit` puts them once they
    # check out.
    datastore_base: Path
    # Where `pigsty organize` puts each of this task's runs: a fixed directory, then a
    # pattern. See `split_organized`.
    organized: str
    # The parameters `pigsty sign` signs for this task. The same list as the task's
    # `signed_parameters` in pig.toml, which this machine never reads: if the two
    # differ, links signed here don't start runs there. See issue #15.
    signed_parameters: list[str] = Field(default_factory=list)

    @property
    def organized_root(self) -> Path:
        return Path(split_organized(self.organized)[0])

    @property
    def pattern(self) -> str:
        return split_organized(self.organized)[1]


class StyConfig(BaseModel):
    """The whole of `pigsty.toml`."""

    model_config = ConfigDict(extra="forbid")

    # The web server's address, like https://pig.example.edu, for `pigsty purge`. Not
    # needed by anything else.
    pig_url: str | None = None
    # The private keys encrypted runs are decrypted with, one `.key` file each. See
    # `keys.py`. Not needed until a task encrypts.
    private_keys_dir: Path | None = None
    task: dict[str, StyTask] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> StyConfig:
        if self.pig_url is not None and not self.pig_url.startswith(
            ("https://", "http://")
        ):
            raise ValueError(
                f"pig_url {self.pig_url!r} isn't a web address. It's where Pig's web "
                "server is, like https://pig.example.edu."
            )
        for code, task in self.task.items():
            if code != code.lower() or not is_safe_value(code):
                raise ValueError(
                    f"task code {code!r} isn't one Pig could have used. Task codes are "
                    f"lowercase {SAFE_VALUE_EXPLANATION}."
                )
            try:
                split_organized(task.organized)
            except PatternError as error:
                raise ValueError(f"task {code!r}: {error}") from None
            for name in task.signed_parameters:
                if not name.isidentifier() or name.startswith(
                    RESERVED_PARAMETER_PREFIX
                ):
                    raise ValueError(
                        f"task {code!r}: {name!r} in signed_parameters isn't a name "
                        "pig.toml could have for a parameter."
                    )
            if len(set(task.signed_parameters)) != len(task.signed_parameters):
                raise ValueError(
                    f"task {code!r}: signed_parameters lists a parameter twice."
                )
        return self


def load_sty_config(path: Path) -> StyConfig:
    """Read and check `pigsty.toml`, or raise ConfigurationError saying why."""
    raw = _read_toml(path)
    try:
        config = StyConfig.model_validate(raw)
    except Exception as error:
        # Pydantic refuses pig.toml anyway, but by listing keys it doesn't know.
        hint = ""
        if {"data_root", "database"} & raw.keys():
            hint = (
                "\nThis looks like pig.toml, the web server's configuration, which pig "
                "reads. pigsty reads the secure side's pigsty.toml."
            )
        raise ConfigurationError(f"{path} has a problem:\n{error}{hint}") from error

    # Paths in the file are relative to the file, as they are in pig.toml.
    base = path.parent.resolve()
    if config.private_keys_dir is not None:
        config.private_keys_dir = (base / config.private_keys_dir).resolve()
    for task in config.task.values():
        task.datastore_base = (base / task.datastore_base).resolve()
        root, pattern = split_organized(task.organized)
        task.organized = f"{(base / root).resolve()}/{pattern}"

    # One task per datastore, so admit can tell from where a run is which task it
    # belongs to. And one task per organized root, with no root inside another, so each
    # task's tree, and pigsty's record of it, is that task's alone.
    codes = sorted(config.task)
    for number, code in enumerate(codes):
        for other in codes[number + 1 :]:
            mine, theirs = config.task[code], config.task[other]
            if mine.datastore_base == theirs.datastore_base:
                raise ConfigurationError(
                    f"{path} has a problem: tasks {code!r} and {other!r} have the same "
                    f"datastore_base, {mine.datastore_base}. Each task needs its own."
                )
            roots = sorted([mine.organized_root, theirs.organized_root])
            if roots[0] == roots[1] or roots[0] in roots[1].parents:
                raise ConfigurationError(
                    f"{path} has a problem: tasks {code!r} and {other!r} are organized "
                    f"into {mine.organized_root} and {theirs.organized_root}. Each task "
                    "needs a directory of its own, not inside another task's, before "
                    "its first placeholder."
                )
    return config


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_bytes()
    except OSError as error:
        raise ConfigurationError(f"Can't read {path}: {error}") from error
    try:
        return tomllib.loads(text.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigurationError(f"{path} isn't valid TOML: {error}") from error
