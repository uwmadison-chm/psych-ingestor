"""Task definitions, read from a TOML file.

The file is the authority for everything it defines. Nothing in the service writes to it,
and the database never holds a second opinion about a task definition.
"""

from __future__ import annotations

import re
import string
import threading
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# A safe value: letters, digits, underscore, and a non-leading dash, 1-64 characters.
# Narrow enough that "." and ".." can't be expressed, so traversal isn't something we
# have to defend against. Pig itself never puts a parameter value in a path any more —
# run directories are named for the run ID — but `pigsty organize` will, on another machine,
# and checking at run start is what makes that safe. See docs/configuration.md.
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


def describe_size(size: int) -> str:
    """Say '8M' rather than '8388608', for anything a person reads."""
    for unit, name in ((1024**3, "G"), (1024**2, "M"), (1024, "k")):
        if size >= unit and size % unit == 0:
            return f"{size // unit}{name}"
    return f"{size} bytes"


class TaskDefinition(BaseModel):
    """One task's entry in the configuration file."""

    model_config = ConfigDict(extra="forbid")

    parameters: list[str]
    run_key: list[str]
    open: bool = True
    max_event_size: int = Field(default=1024 * 1024)
    # How long a run may stay open, counted from when it started. After this, the next
    # sweep marks it expired and files what arrived. See docs/configuration.md.
    expires_after: int = Field(default=24 * 3600)
    # Whether this task may send audio, video, and other bytes as media items. Off unless
    # asked for: an open URL taking multi-megabyte bodies is a different exposure from
    # one taking trial data, and most tasks record nothing. See docs/configuration.md.
    media: bool = False
    # The most one media part may be. The web server in front of Pig has to allow request
    # bodies at least this big, or parts are refused before Pig sees them.
    max_part_size: int = Field(default=8 * 1024 * 1024)
    # The name of the public key, from `public_keys_file`, that this task's events and
    # media are encrypted to in the participant's browser. None means the task doesn't
    # encrypt. See docs/configuration.md and issue #10.
    encrypt_to: str | None = None

    # Filled in by load_config, since a task doesn't know its own code, or what its key
    # name stands for.
    code: str = ""
    public_key: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _renamed_settings(cls, raw: Any) -> Any:
        # `extra="forbid"` would refuse this anyway, but "extra inputs are not permitted"
        # doesn't tell anyone what to type instead.
        if isinstance(raw, dict) and "abandon_after" in raw:
            raise ValueError(
                "abandon_after is now called expires_after. It means the same thing: how "
                "long a run may stay open, counted from when it started."
            )
        if isinstance(raw, dict) and "path" in raw:
            raise ValueError(
                "path is no longer a task setting; remove it. See docs/configuration.md."
            )
        return raw

    @field_validator("max_event_size", "max_part_size", mode="before")
    @classmethod
    def _size(cls, value: str | int) -> int:
        return parse_size(value)

    @field_validator("expires_after", mode="before")
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
        return self


class Config(BaseModel):
    """The whole configuration file."""

    model_config = ConfigDict(extra="forbid")

    data_root: Path
    database: Path
    # The public keys tasks can encrypt to, as `pigsty keys export` writes them on the
    # secure side. Only needed if a task sets `encrypt_to`.
    public_keys_file: Path | None = None
    task: dict[str, TaskDefinition] = Field(default_factory=dict)

    # Two trees under the data root, and a run directory moves from one to the other
    # exactly once, by a rename. A purged run makes one more rename, out of `done/` and
    # into `purging/`, where it's deleted. A media part is written under `uploading/`
    # and renamed into its run. All four are on one filesystem or those renames aren't
    # atomic; see docs/deployment.md.

    @property
    def in_progress_root(self) -> Path:
        """Runs still collecting, and closed runs the sweep hasn't finished yet."""
        return self.data_root / "in_progress"

    @property
    def done_root(self) -> Path:
        """Finished runs, whichever way they closed. Nothing here ever changes again.
        A run leaves only when it's purged, all at once."""
        return self.data_root / "done"

    @property
    def purging_root(self) -> Path:
        """Purged runs on their way to being deleted. Empty except for a moment during a
        purge, or after one was interrupted, until the next sweep clears it."""
        return self.data_root / "purging"

    @property
    def uploading_root(self) -> Path:
        """Media parts while they arrive, before they're renamed into their run. Kept
        out of the run's directory so the sweep can never mistake one for part of the
        run. See `storage.PartWriter`."""
        return self.data_root / "uploading"

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
        # Pydantic refuses pigsty.toml anyway, but by listing keys it doesn't know.
        hint = ""
        tasks = raw.get("task") if isinstance(raw, dict) else None
        if isinstance(tasks, dict) and any(
            isinstance(task, dict) and {"datastore_base", "organized"} & task.keys()
            for task in tasks.values()
        ):
            hint = (
                "\nThis looks like pigsty.toml, the secure side's configuration, which "
                "pigsty reads. pig reads the web server's pig.toml."
            )
        raise ConfigurationError(f"{path} has a problem:\n{error}{hint}") from error

    # Paths in the file are relative to the file, so a checkout can move without editing.
    base = path.parent.resolve()
    config.data_root = (base / config.data_root).resolve()
    config.database = (base / config.database).resolve()
    _resolve_public_keys(path, config)
    return config


def _resolve_public_keys(path: Path, config: Config) -> None:
    """Read the public keys file, and give each task that encrypts its public key."""
    # Imported here, because keys.py uses this module's safe-value rule.
    from .keys import read_public_keys

    encrypting = sorted(code for code, task in config.task.items() if task.encrypt_to)
    if config.public_keys_file is None:
        if encrypting:
            raise ConfigurationError(
                f"{path} has a problem: {', '.join(encrypting)} set encrypt_to, but "
                "there's no public_keys_file to find the key in. Make one on the secure "
                "side with pigsty keys export, copy it here, and point public_keys_file "
                "at it."
            )
        return
    config.public_keys_file = (
        path.parent.resolve() / config.public_keys_file
    ).resolve()
    public_keys = read_public_keys(config.public_keys_file)
    for code in encrypting:
        task = config.task[code]
        if task.encrypt_to not in public_keys:
            known = ", ".join(sorted(public_keys)) or "none"
            raise ConfigurationError(
                f"{path} has a problem: task {code!r} encrypts to {task.encrypt_to!r}, "
                f"and {config.public_keys_file} has no key by that name. It has: "
                f"{known}. If the key is new, export the public keys again on the "
                "secure side and copy the file here."
            )
        task.public_key = public_keys[task.encrypt_to]


class ConfigSource:
    """The configuration file, re-read whenever it changes on disk.

    The service asks for the current configuration on every request, so editing a task
    definition takes effect without a restart or a signal. Reading the file every time
    would be wasteful, so this remembers what it read and checks the file's modification
    time — one `stat` per request, which is nothing next to the work of the request.

    **A file that won't load doesn't take the service down.** The last configuration that
    did load keeps serving, the problem is reported by the health check, and the next
    good save picks up from there. Refusing every request instead would mean a typo in a
    text editor stops data collection for participants who are mid-task, and losing their
    data is far worse than running for a few minutes on a definition someone is editing.
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._config: Config | None = None
        self._fingerprint: tuple[tuple[int, int, int] | None, ...] | None = None
        self.problem: str | None = None

    def current(self) -> Config:
        """The configuration as of the file's current state on disk.

        Raises ConfigurationError only if the file has never loaded — a service that has
        never had a valid configuration should fail loudly at startup.
        """
        with self._lock:
            fingerprint = self._read_fingerprint()
            if fingerprint is not None and fingerprint != self._fingerprint:
                self._reload(fingerprint)
            if self._config is None:
                raise ConfigurationError(self.problem or f"Can't read {self.path}.")
            return self._config

    def _read_fingerprint(self) -> tuple[tuple[int, int, int] | None, ...] | None:
        """Enough of the file's identity to notice an edit: when, how big, which file.
        And the same for the public keys file it names, so copying a new one over takes
        effect the way editing pig.toml does."""
        try:
            stat = self.path.stat()
        except OSError as error:
            # The file went away. Keep serving what we have and say so.
            self.problem = f"Can't read {self.path}: {error}"
            return None
        keys = None
        if self._config is not None and self._config.public_keys_file is not None:
            try:
                keys_stat = self._config.public_keys_file.stat()
                keys = (keys_stat.st_mtime_ns, keys_stat.st_size, keys_stat.st_ino)
            except OSError:
                # Reloading will say what's wrong with it.
                pass
        return ((stat.st_mtime_ns, stat.st_size, stat.st_ino), keys)

    def _reload(self, fingerprint: tuple[tuple[int, int, int] | None, ...]) -> None:
        try:
            self._config = load_config(self.path)
        except ConfigurationError as error:
            self.problem = str(error)
            return
        # Only remember the fingerprint of a file that actually loaded, so a broken one
        # is retried on the next request rather than being treated as read. When this
        # load named a public keys file the last one didn't, the fingerprint didn't
        # cover it, so the next request loads once more. That's cheaper than the
        # alternative, which can miss an edit made while this one was loading.
        self._fingerprint = fingerprint
        self.problem = None
