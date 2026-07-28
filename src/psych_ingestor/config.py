"""Task definitions, read from a TOML file.

The file is the authority for everything it defines. Nothing in the service writes to it,
and the database never holds a second opinion about a task definition.
"""

from __future__ import annotations

import re
import string
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# A safe value: letters, digits, underscore, and a non-leading dash, 1-64 characters.
# Narrow enough that "." and ".." can't be expressed, so traversal isn't something we
# have to defend against. See docs/configuration.md.
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$")

SAFE_VALUE_EXPLANATION = (
    "letters, digits, underscore, and dash (not first), 1 to 64 characters"
)


# A working copy keeps its configuration, database, and data under `local/`, which is the
# one path version control ignores. Everything in the file is relative to the file, so
# moving the whole directory somewhere else needs no edits.
DEFAULT_CONFIG = "local/pig.toml"


class ConfigurationError(Exception):
    """The configuration file is wrong in a way we can describe."""


def is_safe_value(value: str) -> bool:
    return bool(SAFE_VALUE.match(value))


def parse_size(value: str | int) -> int:
    """Turn '1M' into 1048576. Bare numbers are bytes."""
    return _parse_with_units(
        value,
        units={"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3},
        what="size",
        examples="'1M', '500k', '2048'",
    )


def parse_duration(value: str | int) -> int:
    """Turn '30m' into 1800. Bare numbers are seconds."""
    return _parse_with_units(
        value,
        units={"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800},
        what="duration",
        examples="'30m', '24h', '7d'",
    )


def _parse_with_units(
    value: str | int, units: dict[str, int], what: str, examples: str
) -> int:
    if isinstance(value, int):
        number, suffix = value, ""
    else:
        text = value.strip()
        digits = text.rstrip(string.ascii_letters)
        suffix = text[len(digits) :].lower()
        try:
            number = int(digits)
        except ValueError:
            raise ValueError(
                f"{value!r} isn't a {what} I understand. Try something like {examples}."
            ) from None
    if suffix not in units:
        known = ", ".join(sorted(u for u in units if u))
        raise ValueError(
            f"{value!r} uses an unknown {what} unit {suffix!r}. Known units: {known}."
        )
    if number < 0:
        raise ValueError(f"{value!r} is a negative {what}.")
    return number * units[suffix]


def describe_duration(seconds: int) -> str:
    """Say '24 hours' rather than '86400', for anything a person reads."""
    for size, name in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size and seconds % size == 0:
            count = seconds // size
            return f"{count} {name}{'s' if count != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def placeholders_in(pattern: str) -> set[str]:
    """The {names} in a storage pattern."""
    return {
        name for _, name, _, _ in string.Formatter().parse(pattern) if name is not None
    }


class TaskDefinition(BaseModel):
    """One task's entry in the configuration file."""

    model_config = ConfigDict(extra="forbid")

    parameters: list[str]
    run_key: list[str]
    path: str
    open: bool = True
    max_event_size: int = Field(default=1024 * 1024)
    abandon_after: int = Field(default=24 * 3600)

    # Filled in by load_config, since a task doesn't know its own code.
    code: str = ""

    @field_validator("max_event_size", mode="before")
    @classmethod
    def _size(cls, value: str | int) -> int:
        return parse_size(value)

    @field_validator("abandon_after", mode="before")
    @classmethod
    def _duration(cls, value: str | int) -> int:
        return parse_duration(value)

    @model_validator(mode="after")
    def _check_coherence(self) -> TaskDefinition:
        for name in self.parameters:
            if not name.isidentifier():
                raise ValueError(
                    f"parameter name {name!r} isn't usable; parameter names should look "
                    "like participant_id"
                )

        missing = [name for name in self.run_key if name not in self.parameters]
        if missing:
            raise ValueError(
                f"run_key names {missing} that aren't in parameters. Every part of the "
                "run key has to be something the link supplies."
            )
        if not self.run_key:
            raise ValueError(
                "run_key can't be empty; a run has to be a repeat of something"
            )

        self._check_path()
        return self

    def _check_path(self) -> None:
        placeholders = placeholders_in(self.path)
        available = set(self.parameters) | {"run_number"}
        unknown = sorted(placeholders - available)
        if unknown:
            raise ValueError(
                f"path uses {unknown}, which this task doesn't have. Available: "
                f"{sorted(available)}."
            )
        if "run_number" not in placeholders:
            raise ValueError(
                "path has no {run_number}, so the second run of a participant would "
                "overwrite the first."
            )
        if Path(self.path).is_absolute():
            raise ValueError("path has to be relative to the data root")
        if ".." in Path(self.path).parts:
            raise ValueError("path can't contain '..'")

    def dataset_path(self, parameters: dict[str, str], run_number: int) -> Path:
        """Where this run's dataset belongs, relative to the completed-data directory.

        Values are lowercased on their way into a path, and only here — the original
        spelling stays on the run. See docs/configuration.md.
        """
        values: dict[str, str] = {
            name: parameters[name].lower() for name in self.parameters
        }
        values["run_number"] = f"run-{run_number:04d}"
        return Path(self.code) / self.path.format(**values)


class Config(BaseModel):
    """The whole configuration file."""

    model_config = ConfigDict(extra="forbid")

    data_root: Path
    database: Path
    task: dict[str, TaskDefinition] = Field(default_factory=dict)

    @property
    def in_progress_root(self) -> Path:
        return self.data_root / "in_progress"

    @property
    def complete_root(self) -> Path:
        return self.data_root / "complete"

    @property
    def abandoned_root(self) -> Path:
        return self.data_root / "abandoned"

    @model_validator(mode="after")
    def _check_task_codes(self) -> Config:
        for code, task in self.task.items():
            if code != code.lower():
                raise ValueError(
                    f"task code {code!r} has capital letters; task codes are lowercase"
                )
            if not is_safe_value(code):
                raise ValueError(
                    f"task code {code!r} isn't usable as a directory name: "
                    f"{SAFE_VALUE_EXPLANATION}"
                )
            task.code = code
        return self


def load_config(path: Path) -> Config:
    """Read and check a configuration file, or raise ConfigurationError saying why."""
    try:
        text = path.read_bytes()
    except OSError as error:
        raise ConfigurationError(f"Can't read {path}: {error}") from error

    try:
        raw = tomllib.loads(text.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigurationError(f"{path} isn't valid TOML: {error}") from error

    try:
        config = Config.model_validate(raw)
    except Exception as error:
        raise ConfigurationError(f"{path} has a problem:\n{error}") from error

    # Paths in the file are relative to the file, so a checkout can move without editing.
    base = path.parent.resolve()
    config.data_root = (base / config.data_root).resolve()
    config.database = (base / config.database).resolve()
    return config
