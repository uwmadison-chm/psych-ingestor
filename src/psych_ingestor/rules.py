"""The rules values are held to, wherever they're written or checked.

What a safe value is, which a parameter has to be before `pigsty organize` can put it in
a path; what a text value is; what a task code is; and how sizes and durations are
written. Both sides check against these, so each is written once, here.
"""

from __future__ import annotations

import re
import string
import unicodedata

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


# A task code names a directory on both sides, and is part of every URL, so it's a safe
# value. Lowercase too, so two tasks can't differ only by case on a filesystem that
# ignores it. pig.toml and pigsty.toml are both held to this.
TASK_CODE_EXPLANATION = f"lowercase {SAFE_VALUE_EXPLANATION}"


def is_safe_value(value: str) -> bool:
    return bool(SAFE_VALUE.match(value))


def is_task_code(code: str) -> bool:
    return code == code.lower() and is_safe_value(code)


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
