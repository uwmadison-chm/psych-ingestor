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

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .config import (
    SAFE_VALUE_EXPLANATION,
    ConfigurationError,
    TaskDefinition,
    check_run_as,
    is_safe_value,
    read_toml,
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
    # The user every pigsty command has to run as, so nothing it makes is owned by
    # someone else. Unset means anyone. See issue #95.
    run_as: str | None = None
    # Read private key files even if their permissions say other people can, for a
    # filesystem that reports permissions it doesn't enforce. `--ignore-permissions`
    # does the same for one command.
    ignore_permissions: bool = False
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
                if not name.isidentifier():
                    raise ValueError(
                        f"task {code!r}: {name!r} in signed_parameters isn't a name "
                        "pig.toml could have for a parameter."
                    )
            if len(set(task.signed_parameters)) != len(task.signed_parameters):
                raise ValueError(
                    f"task {code!r}: signed_parameters lists a parameter twice."
                )
        return self


# ------------------------------------------------------- saying what's wrong
#
# Pydantic's own messages name Python types, quote dictionaries back at you, and link to
# its website. These say which table a problem is in and what to write instead.

# What each setting is for, and what one looks like.
SETTINGS = {
    "pig_url": ("the web server's address", '"https://pig.example.edu"'),
    "private_keys_dir": ("the directory of private keys", '"./private_keys"'),
    "run_as": ("the user every pigsty command has to run as", '"pigsty"'),
    "ignore_permissions": (
        "whether to read private key files other people can read",
        "true",
    ),
    "datastore_base": ("the directory this task's runs are kept in", '"./stroop-data"'),
    "organized": (
        "where pigsty organize puts each run",
        '"./organized/stroop/{session}/{participant_id}_{run_number}"',
    ),
    "signed_parameters": (
        "the parameters pigsty sign signs",
        '["participant_id", "session"]',
    ),
}

# A task's settings in pig.toml, so a task copied from there can be called that. The
# fields load_config fills in itself aren't settings anyone writes.
PIG_TASK_SETTINGS = set(TaskDefinition.model_fields) - {
    "code",
    "source",
    "public_key",
    "signing_keys",
}


def describe_problems(error: ValidationError, raw: dict[str, Any]) -> list[str]:
    """Everything wrong with pigsty.toml, one sentence each, top-level problems first
    and then each task's."""
    by_table: dict[str | None, list[str]] = {}
    pig_settings: dict[str, list[str]] = {}
    unknown_settings: dict[str | None, list[str]] = {}

    for problem in error.errors():
        location = [str(part) for part in problem["loc"]]
        if problem["type"] == "value_error":
            # From StyConfig._check, which already says it plainly.
            by_table.setdefault(None, []).append(str(problem["ctx"]["error"]))
            continue
        if location == ["task"]:
            by_table.setdefault(None, []).append(
                "task should be a table for each task, like [task.stroop], with that "
                "task's settings under it."
            )
            continue
        if location[0] == "task" and len(location) == 2:
            code = location[1]
            by_table.setdefault(code, []).append(
                f"task.{code} should be a table, like {_table(code)}, with the task's "
                "settings under it."
            )
            continue

        if location[0] == "task":
            code, setting = location[1], location[2]
            value = raw["task"][code].get(setting)
            prefix = f"In {_table(code)}, "
        else:
            code, setting = None, location[0]
            value = raw.get(setting)
            prefix = ""

        if problem["type"] == "extra_forbidden":
            if code is not None and setting in PIG_TASK_SETTINGS:
                pig_settings.setdefault(code, []).append(setting)
            else:
                unknown_settings.setdefault(code, []).append(setting)
        elif problem["type"] == "missing":
            purpose, example = SETTINGS[setting]
            where = _table(code) if code is not None else "pigsty.toml"
            by_table.setdefault(code, []).append(
                f"{where} needs {setting}, {purpose}, like {setting} = {example}."
            )
        elif setting in SETTINGS:
            _, example = SETTINGS[setting]
            by_table.setdefault(code, []).append(
                f"{prefix}{setting} is {_as_toml(value)}. It should look like "
                f"{setting} = {example}."
            )
        else:
            by_table.setdefault(code, []).append(f"{prefix}{setting}: {problem['msg']}")

    for code, names in pig_settings.items():
        by_table.setdefault(code, []).append(
            f"In {_table(code)}, {_listed(names)} "
            + ("is a pig.toml setting" if len(names) == 1 else "are pig.toml settings")
            + ", for the web server. pigsty.toml doesn't use "
            + ("it." if len(names) == 1 else "them.")
        )
    for code, names in unknown_settings.items():
        where = f"In {_table(code)}, " if code is not None else ""
        by_table.setdefault(code, []).append(
            f"{where}{_listed(names)} "
            + ("isn't a setting" if len(names) == 1 else "aren't settings")
            + " pigsty knows."
        )
    for code in pig_settings.keys() | unknown_settings.keys():
        if code is None:
            by_table[code].append(
                "pigsty.toml can have pig_url, private_keys_dir, run_as, "
                "ignore_permissions, and a [task.…] table for each task. See "
                "pigsty.example.toml."
            )
        else:
            by_table[code].append(
                f"A task in pigsty.toml can have {_listed(list(StyTask.model_fields))}."
                " See pigsty.example.toml."
            )

    problems = []
    for code in sorted(by_table, key=lambda code: (code is not None, code or "")):
        for line in by_table[code]:
            if line not in problems:
                problems.append(line)
    return problems


def _table(code: str) -> str:
    """A task's table header, as it'd be written in pigsty.toml."""
    if re.fullmatch(r"[A-Za-z0-9_-]+", code):
        return f"[task.{code}]"
    return f"[task.{json.dumps(code)}]"


def _as_toml(value: Any) -> str:
    """A value about the way it was written in pigsty.toml. JSON is close enough."""
    return json.dumps(value, default=str)


def _listed(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def load_sty_config(path: Path) -> StyConfig:
    """Read and check `pigsty.toml`, or raise ConfigurationError saying why."""
    raw = read_toml(path)
    try:
        config = StyConfig.model_validate(raw)
    except ValidationError as error:
        problems = "\n".join(
            f"  {problem}" for problem in describe_problems(error, raw)
        )
        # Refused anyway, but saying so is quicker than reading what's wrong with it.
        hint = ""
        if {"data_root", "database"} & raw.keys():
            hint = (
                "\nThis looks like pig.toml, the web server's configuration, which pig "
                "reads. pigsty reads the secure side's pigsty.toml."
            )
        raise ConfigurationError(f"{path} has a problem:\n{problems}{hint}") from error

    check_run_as(path, config.run_as)

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
