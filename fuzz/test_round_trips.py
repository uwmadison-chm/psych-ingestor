"""Things that go in one way and come out another, and must come back as they went in,
or be refused: sizes and durations, bech32, signatures, and the paths the secure side
builds from what the web server sent it."""

from __future__ import annotations

import base64
import json
import os.path
import tempfile
from pathlib import Path, PurePosixPath

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from support import signing_key

from psych_ingestor import bech32, signing
from psych_ingestor.admit import RUN_FILE, RUN_FILE_TYPE, RUN_FILE_VERSION, _listed_path
from psych_ingestor.admit import RunProblem as AdmitProblem
from psych_ingestor.config import (
    describe_duration,
    describe_size,
    is_safe_value,
    parse_duration,
    parse_size,
)
from psych_ingestor.organize import (
    OrganizeReport,
    Recorded,
    RunProblem,
    _plan,
    render,
)

# ------------------------------------------------------------- sizes and durations

SIZE_UNITS = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


@given(
    number=st.integers(min_value=0, max_value=10**6),
    unit=st.sampled_from(sorted(SIZE_UNITS)),
    capital=st.booleans(),
    padding=st.sampled_from(["", " ", "\t"]),
)
def test_a_size_in_any_unit(number: int, unit: str, capital: bool, padding: str):
    written = f"{padding}{number}{unit.upper() if capital else unit}{padding}"
    assert parse_size(written) == number * SIZE_UNITS[unit]


@given(
    number=st.integers(min_value=0, max_value=10**6),
    unit=st.sampled_from(sorted(DURATION_UNITS)),
    capital=st.booleans(),
)
def test_a_duration_in_any_unit(number: int, unit: str, capital: bool):
    written = f"{number}{unit.upper() if capital else unit}"
    assert parse_duration(written) == number * DURATION_UNITS[unit]


@given(st.integers(min_value=0, max_value=2**50))
def test_describing_a_size_loses_nothing(size: int):
    """`describe_size` is for people, so it says `8M` or `1000 bytes`. Either one,
    read back, is the size it described."""
    described = describe_size(size)
    assert parse_size(described.replace(" bytes", "b")) == size


@given(st.integers(min_value=0, max_value=2**40))
def test_describing_a_duration_loses_nothing(seconds: int):
    """`describe_duration` says `24 hours` or `90 seconds`. Read back, it's the
    duration it described."""
    count, name = describe_duration(seconds).split(" ")
    unit = {"day": "d", "hour": "h", "minute": "m", "second": "s"}[name.rstrip("s")]
    assert parse_duration(f"{count}{unit}") == seconds


@given(st.text())
def test_any_text_is_a_size_or_a_readable_refusal(text: str):
    """Whatever's in pig.toml, it's a size or it's refused with a message, and never
    a negative one."""
    for parse in (parse_size, parse_duration):
        try:
            assert parse(text) >= 0
        except ValueError as refused:
            assert str(refused)


# ------------------------------------------------------------------------- bech32

prefixes = st.text("abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=10)
# Short enough that the whole string stays inside the length bech32's checksum is
# designed for, 90 characters.
key_bytes = st.binary(max_size=40)


@given(prefix=prefixes, data=key_bytes)
def test_bech32_round_trip(prefix: str, data: bytes):
    encoded = bech32.encode(prefix, data)
    assert bech32.decode(encoded) == (prefix, data)
    # age writes private keys in capitals.
    assert bech32.decode(encoded.upper()) == (prefix, data)


@given(
    prefix=prefixes, data=key_bytes, where=st.integers(min_value=0), to=st.characters()
)
def test_bech32_refuses_any_one_character_changed(
    prefix: str, data: bytes, where: int, to: str
):
    """The checksum is the point: a key mistyped while it was copied fails to decode,
    rather than becoming a different key."""
    encoded = bech32.encode(prefix, data)
    where %= len(encoded)
    assume(encoded[where] != to)
    changed = encoded[:where] + to + encoded[where + 1 :]
    with pytest.raises(ValueError):
        bech32.decode(changed)


@given(st.text())
def test_bech32_decodes_any_text_or_says_why_not(text: str):
    try:
        prefix, data = bech32.decode(text)
    except ValueError as refused:
        assert str(refused)
    else:
        assert bech32.encode(prefix, data) == text.lower()


# --------------------------------------------------------------------- signatures

parameter_values = st.dictionaries(
    st.sampled_from(["id", "event", "modules"]), st.text(max_size=40), max_size=3
)


@given(task_code=st.sampled_from(["teddy", "stroop"]), values=parameter_values)
def test_a_signature_checks_out(task_code: str, values: dict[str, str]):
    key = signing_key()
    signature = signing.sign(key, task_code, values)
    accepted = {key.name: key.public_key}
    assert signing.verify(signature, task_code, values, accepted) == key.name


@given(values=parameter_values, where=st.integers(min_value=0), to=st.characters())
def test_a_signature_with_one_character_changed_fails(
    values: dict[str, str], where: int, to: str
):
    """A signature is copied around inside a link, so this is the change it's most
    likely to suffer.

    One exception, which isn't a weakness. The signature is 64 bytes in 86 characters of
    base64, which is room for 516 bits, so the last character carries four bits nobody
    reads. Change only those, and the signature is the same 64 bytes, and checks out,
    as it should: it proves exactly what it did before.
    """
    key = signing_key()
    accepted = {key.name: key.public_key}
    signature = signing.sign(key, "teddy", values)
    where %= len(signature)
    assume(signature[where] != to)
    changed = signature[:where] + to + signature[where + 1 :]
    try:
        signing.verify(changed, "teddy", values, accepted)
    except signing.BadSignature:
        return
    assert where == len(signature) - 1, f"{changed!r} checked out"
    assert decoded(changed) == decoded(signature)


@given(
    values=parameter_values.filter(bool),
    name=st.sampled_from(["id", "event", "modules"]),
    to=st.text(max_size=40),
)
def test_a_signature_over_other_values_fails(
    values: dict[str, str], name: str, to: str
):
    assume(values.get(name) != to)
    key = signing_key()
    signature = signing.sign(key, "teddy", values)
    with pytest.raises(signing.BadSignature):
        signing.verify(
            signature, "teddy", {**values, name: to}, {key.name: key.public_key}
        )
    with pytest.raises(signing.BadSignature):
        signing.verify(signature, "stroop", values, {key.name: key.public_key})


def decoded(signature: str) -> bytes:
    encoded = signature.rpartition(".")[2]
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))


# ----------------------------------------------------------- paths on the secure side
#
# The secure side builds paths out of what came from the web server, and the web server
# is the machine most likely to be broken into. So nothing it sends can name a path
# outside where it belongs.


@given(st.text())
def test_a_manifest_cant_name_a_path_outside_its_run(path: str):
    try:
        relative = _listed_path(path)
    except AdmitProblem:
        return
    root = "/datastore/runs/some-run"
    joined = os.path.normpath(os.path.join(root, relative))
    assert joined.startswith(root + "/"), f"{path!r} leads to {joined}"
    assert relative.as_posix() == path, f"{path!r} was read as {relative}"


placeholders = st.sampled_from(
    [
        "{participant_id}",
        "{session}",
        "{modules}",
        "{task_code}",
        "{run_number}",
        "{utm_source}",
        "{nope}",
    ]
)
patterns = st.lists(
    placeholders | st.sampled_from(["/", "_", "-", ".", ".."]) | st.text(max_size=3),
    min_size=1,
    max_size=6,
).map("".join)


@given(
    pattern=patterns,
    participant_id=st.text(max_size=8),
    session=st.text(max_size=8),
    modules=st.text(max_size=8),
    task_code=st.text(max_size=8),
    run_number=st.integers(),
    utm_source=st.text(max_size=8),
)
def test_organize_only_builds_safe_paths(
    pattern, participant_id, session, modules, task_code, run_number, utm_source
):
    """Whatever the pattern, and whatever the run file says, a path organize builds is
    made of safe values only. Anything else is refused."""
    run = {
        "task_code": task_code,
        "run_number": run_number,
        "parameters": {
            "participant_id": participant_id,
            "session": session,
            "modules": modules,
        },
        "path_parameters": ["participant_id", "session"],
        "extra_parameters": {"utm_source": utm_source},
    }
    try:
        path = render(pattern, run)
    except RunProblem:
        return
    assert not path.is_absolute()
    assert path.parts and all(is_safe_value(part) for part in path.parts), path


# Few enough values, differing only in case, that runs keep landing on each other.
crowded_values = st.sampled_from(["a", "A", "b", "B", "ab", "aB"])


@given(
    runs=st.lists(st.tuples(crowded_values, crowded_values), min_size=1, max_size=8),
    already_placed=st.lists(
        st.lists(crowded_values, min_size=1, max_size=3), max_size=4
    ),
    pattern=st.sampled_from(
        [
            "{participant_id}",
            "{participant_id}/{session}",
            "{session}/{participant_id}",
            "{participant_id}_{session}",
        ]
    ),
)
def test_organize_never_puts_two_runs_in_one_place(
    runs: list[tuple[str, str]], already_placed: list[list[str]], pattern: str
):
    """Two runs at one path, or one inside another's directory, compared without case
    because a Mac's disk doesn't tell `Baseline` from `baseline`. Every run is either
    placed somewhere no other run is, or refused, and none is left out silently."""
    with tempfile.TemporaryDirectory() as temporary:
        sources = []
        for number, (participant_id, session) in enumerate(runs):
            source = Path(temporary) / f"run-{number}"
            source.mkdir()
            (source / RUN_FILE).write_text(
                json.dumps(
                    {
                        "type": RUN_FILE_TYPE,
                        "run_file_version": RUN_FILE_VERSION,
                        "run_id": source.name,
                        "task_code": "stroop",
                        "run_number": 1,
                        "parameters": {
                            "participant_id": participant_id,
                            "session": session,
                        },
                        "path_parameters": ["participant_id", "session"],
                        "extra_parameters": {},
                        "media": [],
                    }
                )
            )
            sources.append(source)
        recorded = {
            f"placed-{number}": Recorded(f"placed-{number}", PurePosixPath(*parts))
            for number, parts in enumerate(already_placed)
        }
        report = OrganizeReport()
        kept = _plan("stroop", sources, pattern, recorded, report)

    assert {placement.run_id for placement in kept} | {
        Path(refused).name for refused in report.errors
    } == {source.name for source in sources}

    def folded(destination: PurePosixPath) -> tuple[str, ...]:
        return tuple(part.casefold() for part in destination.parts)

    everything = [*recorded.values(), *kept]
    for placement in kept:
        mine = folded(placement.destination)
        for other in everything:
            if other is placement:
                continue
            theirs = folded(other.destination)
            shorter = min(len(mine), len(theirs))
            assert mine[:shorter] != theirs[:shorter], (placement, other)
