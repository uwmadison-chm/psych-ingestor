"""The secure side's configuration, `pigsty.toml`.

The web server reads `pig.toml`; the machine where the data ends up reads `pigsty.toml`,
and no command reads both. That's what lets `pig.toml` sit on a public-facing machine:
nothing the secure side needs ever has to be in it. See issue #33.

For now this holds only what `pig organize` needs: where the store is, where the
organized tree goes, and each task's pattern. Keys, fetching, and purging come later.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import SAFE_VALUE_EXPLANATION, ConfigurationError, is_safe_value

DEFAULT_STY_CONFIG = "local/pigsty.toml"

# Each configuration file says which side it's for, so a command handed the wrong one
# can say so plainly instead of complaining about the keys it doesn't recognize.
WEB_KIND = "web"
SECURE_KIND = "secure"


# ------------------------------------------------------------------- patterns
#
# A pattern is a path with `{name}` placeholders:
#
#     lucida/{task_code}/{participant_id}/{session}/run-{run_number}
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


def default_pattern(task_code: str, run_key: list[str]) -> str:
    """The task code, the run-key values in the order the task declared them, then
    the run number: the smallest tree in which no two runs can land in one place."""
    return "/".join(["{task_code}", *(f"{{{name}}}" for name in run_key)]) + (
        "/run-{run_number}"
    )


# --------------------------------------------------------------- the file


class StyTask(BaseModel):
    """One task's entry in `pigsty.toml`."""

    model_config = ConfigDict(extra="forbid")

    # Where `pig organize` puts this task's runs. Without one, the default pattern.
    pattern: str | None = None


class StyConfig(BaseModel):
    """The whole of `pigsty.toml`."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["secure"]
    # Finished run directories as they arrived from the web server: the durable copy.
    store: Path
    # Where `pig organize` builds the readable tree. Regenerable from `store`.
    organized: Path
    task: dict[str, StyTask] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> StyConfig:
        for code, task in self.task.items():
            if code != code.lower() or not is_safe_value(code):
                raise ValueError(
                    f"task code {code!r} isn't one Pig could have used. Task codes are "
                    f"lowercase {SAFE_VALUE_EXPLANATION}."
                )
            if task.pattern is not None:
                try:
                    check_pattern(task.pattern)
                except PatternError as error:
                    raise ValueError(f"task {code!r}: {error}") from None
        return self

    def pattern_for(self, task_code: str) -> str | None:
        """This task's pattern, or None if it uses the default."""
        task = self.task.get(task_code)
        return task.pattern if task else None


def read_kind(path: Path) -> str | None:
    """Which side a configuration file says it's for, without checking anything else.

    For `pig check`, which takes either file and has to know which rules to apply.
    A file with no `kind` is a `pig.toml` written before the key existed.
    """
    raw = _read_toml(path)
    kind = raw.get("kind")
    return kind if isinstance(kind, str) else None


def load_sty_config(path: Path) -> StyConfig:
    """Read and check `pigsty.toml`, or raise ConfigurationError saying why."""
    raw = _read_toml(path)
    if raw.get("kind") != SECURE_KIND:
        raise ConfigurationError(
            f"{path} isn't a secure-side configuration: it doesn't say "
            f'kind = "{SECURE_KIND}". Commands that run where the data ends up, like '
            "`pig organize`, read pigsty.toml, never the web server's pig.toml."
        )
    try:
        config = StyConfig.model_validate(raw)
    except Exception as error:
        raise ConfigurationError(f"{path} has a problem:\n{error}") from error

    # Paths in the file are relative to the file, as they are in pig.toml.
    base = path.parent.resolve()
    config.store = (base / config.store).resolve()
    config.organized = (base / config.organized).resolve()
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
