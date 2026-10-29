"""Task definitions, read from a TOML file.

The file is the authority for everything it defines. Nothing in the service writes to it,
and the database never holds a second opinion about a task definition.
"""

from __future__ import annotations

import re
import string
import threading
import tomllib
import unicodedata
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

# A parameter that's signed but isn't a path parameter, like Teddy's `modules`, is text:
# kept exactly as it arrives, but never part of a path, so it isn't held to the
# safe-value rule. The cap keeps it from being anything but a parameter. Provisional,
# like allowing characters outside ASCII; see issue #44.
MAX_TEXT_VALUE_LENGTH = 2048

TEXT_VALUE_EXPLANATION = (
    f"any text up to {MAX_TEXT_VALUE_LENGTH} characters, without control characters "
    "like tabs or line breaks"
)


# A working copy keeps its configuration, database, and data under `local/`, which is the
# one path version control ignores. Everything in the file is relative to the file, so
# moving the whole directory somewhere else needs no edits.
DEFAULT_CONFIG = "local/pig.toml"


class ConfigurationError(Exception):
    """The configuration file is wrong in a way we can describe."""


def is_safe_value(value: str) -> bool:
    return bool(SAFE_VALUE.match(value))


def is_text_value(value: str) -> bool:
    """Whether a value is usable as a text parameter: not too long, and nothing in it
    that can't be written to a file as it is."""
    return len(value) <= MAX_TEXT_VALUE_LENGTH and not any(
        # Cc is control characters; Cs is half of a UTF-16 pair, which JSON can carry
        # but UTF-8 can't write.
        unicodedata.category(character) in ("Cc", "Cs")
        for character in value
    )


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

    # Parameters Pig holds to the safe-value rule, so `pigsty organize` can use them in
    # a path. See docs/configuration.md.
    path_parameters: list[str]
    # The path parameters whose values decide whether two runs are repeats of each other.
    run_key_parameters: list[str]
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
    # The parameters a run's signature covers, and the names of the signing keys, from
    # `public_keys_file`, it may be signed with. Both empty means the task doesn't check
    # signatures. See docs/configuration.md and issue #15.
    signed_parameters: list[str] = Field(default_factory=list)
    signed_by: list[str] = Field(default_factory=list)

    # Filled in by load_config, since a task doesn't know its own code, which file it's
    # in, or what its key names stand for.
    code: str = ""
    source: Path | None = None
    public_key: str | None = None
    signing_keys: dict[str, str] = Field(default_factory=dict)

    @property
    def required_parameters(self) -> list[str]:
        """Every parameter a run needs: a parameter is required if it's in any list."""
        return self.path_parameters + [
            name for name in self.signed_parameters if name not in self.path_parameters
        ]

    @property
    def checks_signatures(self) -> bool:
        return bool(self.signed_by)

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
        if isinstance(raw, dict) and {"parameters", "run_key"} & raw.keys():
            raise ValueError(
                "parameters and run_key are now called path_parameters and "
                "run_key_parameters. They mean the same things: the parameters a run "
                "needs, which can be used in a path, and which of them identify the run."
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
        for setting in ("path_parameters", "signed_parameters", "signed_by"):
            listed = getattr(self, setting)
            repeated = sorted({name for name in listed if listed.count(name) > 1})
            if repeated:
                raise ValueError(
                    f"{setting} lists {', '.join(repeated)} more than once"
                )
        for name in self.required_parameters:
            if not name.isidentifier():
                raise ValueError(
                    f"parameter name {name!r} isn't usable; parameter names should look "
                    "like participant_id"
                )

        missing = [
            name for name in self.run_key_parameters if name not in self.path_parameters
        ]
        if missing:
            raise ValueError(
                f"run_key_parameters names {missing} that aren't in path_parameters. "
                "pigsty organize builds a run's path from its run key, so every part of "
                "it has to be usable in a path."
            )
        if not self.run_key_parameters:
            raise ValueError(
                "run_key_parameters can't be empty; a run has to be a repeat of "
                "something"
            )

        if self.signed_parameters and not self.signed_by:
            raise ValueError(
                "signed_parameters needs signed_by too: the name of the signing key, "
                "from public_keys_file, that links for this task are signed with."
            )
        if self.signed_by and not self.signed_parameters:
            raise ValueError(
                "signed_by needs signed_parameters too: the parameters the signature "
                "covers."
            )
        unsigned = [
            name
            for name in self.run_key_parameters
            if name not in self.signed_parameters
        ]
        if self.signed_parameters and unsigned:
            # Otherwise a participant could change their own ID while everything else
            # in the link stayed locked.
            raise ValueError(
                f"signed_parameters leaves out {', '.join(unsigned)}, from "
                "run_key_parameters. A task that checks signatures has to sign every "
                "part of the run key, or a participant could change who they are."
            )
        return self


class Config(BaseModel):
    """The whole configuration file."""

    model_config = ConfigDict(extra="forbid")

    data_root: Path
    database: Path
    # The public keys tasks can encrypt to and check signatures with, as `pigsty keys
    # export` writes them on the secure side. Only needed if a task sets `encrypt_to` or
    # `signed_by`.
    public_keys_file: Path | None = None
    # Directories of more task definitions, one file or a few per study, so each study's
    # tasks can live in that study's own repository. See docs/configuration.md and
    # issue #36.
    include: list[Path] = Field(default_factory=list)
    # A file the service touches each time a run is finalized, so something watching it —
    # a systemd path unit, say — can start a sweep without waiting for the timer. Only a
    # hint: the timer still runs, and a touch that fails doesn't fail the finalize.
    touch_after_finalizing: Path | None = None
    task: dict[str, TaskDefinition] = Field(default_factory=dict)

    # Filled in by load_config: every public keys file it read, from this file and the
    # included ones, so the service notices a new copy of any of them.
    key_files: list[Path] = Field(default_factory=list)

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
        _check_task_codes(self.task)
        return self


class TaskFile(BaseModel):
    """One file in an `include` directory: task definitions, and the public keys file
    those tasks use. Everything else is the main file's to say."""

    model_config = ConfigDict(extra="forbid")

    public_keys_file: Path | None = None
    task: dict[str, TaskDefinition] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_task_codes(self) -> TaskFile:
        _check_task_codes(self.task)
        return self


def _check_task_codes(tasks: dict[str, TaskDefinition]) -> None:
    for code, task in tasks.items():
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


def task_files(directory: Path) -> list[Path]:
    """The files in an `include` directory that hold tasks, in the order they're read:
    every `.toml` file directly inside it, except hidden ones. Not the directories
    inside it, so what's live is what `ls` shows."""
    return sorted(
        entry
        for entry in directory.iterdir()
        if entry.name.endswith(".toml") and not entry.name.startswith(".")
    )


def load_config(path: Path) -> Config:
    """Read and check a configuration file, and the files it includes, or raise
    ConfigurationError saying why."""
    raw = _read_toml(path)
    try:
        config = Config.model_validate(raw)
    except Exception as error:
        raise ConfigurationError(
            f"{path} has a problem:\n{error}{_pigsty_hint(raw)}"
        ) from error

    # Paths in the file are relative to the file, so a checkout can move without editing.
    base = path.parent.resolve()
    config.data_root = (base / config.data_root).resolve()
    config.database = (base / config.database).resolve()
    config.include = [(base / directory).resolve() for directory in config.include]
    if config.touch_after_finalizing is not None:
        config.touch_after_finalizing = (base / config.touch_after_finalizing).resolve()
    for task in config.task.values():
        task.source = path.resolve()
    config.public_keys_file = _resolve_public_keys(
        path, base, config.public_keys_file, config.task
    )
    config.key_files = [config.public_keys_file] if config.public_keys_file else []

    for file in _included_files(path, config.include):
        included = _load_task_file(file)
        twice = sorted(config.task.keys() & included.task.keys())
        if twice:
            code = twice[0]
            raise ConfigurationError(
                f"Task {code!r} is defined twice: in {config.task[code].source} and "
                f"in {file}. A task code can only be used once, across every file."
            )
        config.task.update(included.task)
        if included.public_keys_file:
            config.key_files.append(included.public_keys_file)
    return config


def _included_files(path: Path, include: list[Path]) -> list[Path]:
    """Every file the include directories hold tasks in, directory by directory."""
    files = []
    for directory in include:
        if include.count(directory) > 1:
            raise ConfigurationError(
                f"{path} has a problem: {directory} is in include more than once."
            )
        try:
            files.extend(task_files(directory))
        except OSError as error:
            raise ConfigurationError(
                f"{path} has a problem: it includes {directory}, and that can't be "
                f"read: {error}"
            ) from error
    return files


def _load_task_file(path: Path) -> TaskFile:
    """Read and check one included file, and find the keys its tasks use."""
    raw = _read_toml(path)
    try:
        included = TaskFile.model_validate(raw)
    except Exception as error:
        hint = _pigsty_hint(raw)
        if {"data_root", "database", "include", "touch_after_finalizing"} & raw.keys():
            hint += (
                "\nIt's in an include directory, so it can only hold tasks and a "
                "public_keys_file. data_root, database, include, and "
                "touch_after_finalizing belong in the main pig.toml."
            )
        raise ConfigurationError(f"{path} has a problem:\n{error}{hint}") from error

    for task in included.task.values():
        task.source = path
    # Relative to where the file really is, so a study's file can be linked in from
    # its repository, the way nginx's sites-enabled links to sites-available, and
    # still find the keys file next to it.
    included.public_keys_file = _resolve_public_keys(
        path, path.resolve().parent, included.public_keys_file, included.task
    )
    return included


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_bytes()
    except OSError as error:
        raise ConfigurationError(f"Can't read {path}: {error}") from error

    try:
        return tomllib.loads(text.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigurationError(f"{path} isn't valid TOML: {error}") from error


def _pigsty_hint(raw: dict[str, Any]) -> str:
    """Pydantic refuses pigsty.toml anyway, but by listing keys it doesn't know."""
    tasks = raw.get("task")
    if isinstance(tasks, dict) and any(
        isinstance(task, dict) and {"datastore_base", "organized"} & task.keys()
        for task in tasks.values()
    ):
        return (
            "\nThis looks like pigsty.toml, the secure side's configuration, which "
            "pigsty reads. pig reads the web server's pig.toml."
        )
    return ""


def _resolve_public_keys(
    path: Path,
    base: Path,
    public_keys_file: Path | None,
    tasks: dict[str, TaskDefinition],
) -> Path | None:
    """Read the public keys file a file names, and give each of that file's tasks that
    encrypts its public key, and each that checks signatures its signing keys. A task
    only ever uses the keys named in its own file, so one study's task can't encrypt to
    another study's key by mistake. `public_keys_file` is relative to `base`. Returns
    where the keys file is."""
    # Imported here, because keys.py uses this module's safe-value rule.
    from .keys import is_signing_public_key, read_public_keys

    using = sorted(
        code
        for code, task in tasks.items()
        if task.encrypt_to or task.checks_signatures
    )
    if public_keys_file is None:
        if using:
            raise ConfigurationError(
                f"{path} has a problem: {', '.join(using)} set encrypt_to or "
                "signed_by, but there's no public_keys_file in this file to find the "
                "keys in. A task uses the public keys file named in its own file. Make "
                "one on the secure side with pigsty keys export, copy it here, and "
                "point public_keys_file at it."
            )
        return None
    public_keys_file = (base / public_keys_file).resolve()
    public_keys = read_public_keys(public_keys_file)

    def find(code: str, name: str, setting: str, signing: bool) -> str:
        if name not in public_keys:
            known = ", ".join(sorted(public_keys)) or "none"
            raise ConfigurationError(
                f"{path} has a problem: task {code!r} has {name!r} in {setting}, and "
                f"{public_keys_file} has no key by that name. It has: {known}. "
                "If the key is new, export the public keys again on the secure side "
                "and copy the file here."
            )
        if is_signing_public_key(public_keys[name]) != signing:
            wanted = "a signing key" if signing else "an encryption key"
            found = "an encryption key" if signing else "a signing key"
            raise ConfigurationError(
                f"{path} has a problem: task {code!r} has {name!r} in {setting}, and "
                f"that's {found}. {setting} needs {wanted}."
            )
        return public_keys[name]

    for code in using:
        task = tasks[code]
        if task.encrypt_to:
            task.public_key = find(code, task.encrypt_to, "encrypt_to", signing=False)
        task.signing_keys = {
            name: find(code, name, "signed_by", signing=True) for name in task.signed_by
        }
    return public_keys_file


class ConfigSource:
    """The configuration file, and the files it includes, re-read whenever one of them
        changes on disk.

        The service asks for the current configuration on every request, so editing a task
        definition takes effect without a restart or a signal. Reading the file every time
        would be wasteful, so this remembers what it read and checks the file's modification
        time — one `stat` per file per request, and a listing of each include directory, which
    is nothing next to the work of the request.

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
        self._fingerprint: tuple[object, ...] | None = None
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

    def _read_fingerprint(self) -> tuple[object, ...] | None:
        """Enough of each file's identity to notice an edit: when, how big, which file.
        That's the main file, every file in its include directories, and the public keys
        files they name, so adding a study's file, or copying a new keys file over, takes
        effect the way editing pig.toml does."""
        try:
            stat = self.path.stat()
        except OSError as error:
            # The file went away. Keep serving what we have and say so.
            self.problem = f"Can't read {self.path}: {error}"
            return None
        watched: list[Path] = []
        if self._config is not None:
            for directory in self._config.include:
                try:
                    watched.extend(task_files(directory))
                except OSError:
                    # Reloading will say what's wrong with it.
                    watched.append(directory)
            watched.extend(self._config.key_files)
        return (
            (stat.st_mtime_ns, stat.st_size, stat.st_ino),
            tuple((str(path), _identity(path)) for path in watched),
        )

    def _reload(self, fingerprint: tuple[object, ...]) -> None:
        try:
            self._config = load_config(self.path)
        except ConfigurationError as error:
            self.problem = str(error)
            return
        # Only remember the fingerprint of a file that actually loaded, so a broken one
        # is retried on the next request rather than being treated as read. When this
        # load read a file the last one didn't, the fingerprint didn't cover it, so the
        # next request loads once more. That's cheaper than the alternative, which can
        # miss an edit made while this one was loading.
        self._fingerprint = fingerprint
        self.problem = None


def _identity(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        # Reloading will say what's wrong with it.
        return None
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)
