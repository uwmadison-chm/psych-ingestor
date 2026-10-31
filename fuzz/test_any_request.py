"""Any request, to any endpoint, gets a success or a refusal Pig wrote, never a 500.

Every client reads a 5xx as "try again", so a 500 for something a task sent is a request
the task sends again forever, holding up everything queued behind it (#106). Pig answers
whatever a request holds with a 2xx or a 4xx that says what's wrong.

These call the service the way `app.py` does, without HTTP, so each example is quick.
The JSON a body can hold is everything `json.loads` returns, which is more than JSON:
NaN, the infinities, and text with half of a UTF-16 pair in it.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from hypothesis import Phase, given, settings
from hypothesis import strategies as st
from support import (
    STARTS,
    TASK_CODES,
    answer,
    check_answer,
    check_stored_lines,
    fresh_pig,
    send_part,
    signing_key,
    time_limit,
)

from psych_ingestor import media, service, signing, sweep

# ----------------------------------------------------------------- what a body holds

# Text as `json.loads` hands it over. `\ud83d` is valid JSON, and decodes to half of a
# UTF-16 pair, which UTF-8 can't encode. Hypothesis leaves those out of text unless
# asked, so they're asked for here, often enough to turn up. Joined from a list rather
# than made with `st.text`, which drops them even when its alphabet has them.
any_character = st.characters() | st.characters(categories=["Cs"])
any_text = st.lists(any_character, max_size=20).map("".join)

# NaN and the infinities are what `json.loads` makes of `NaN`, `Infinity`, and `1e400`.
any_float = st.floats(allow_nan=True, allow_infinity=True)


def json_values(text: st.SearchStrategy[str], floats: st.SearchStrategy[float]):
    """Anything `json.loads` could return, built from this text and these floats."""
    scalars = st.none() | st.booleans() | st.integers() | floats | text
    return st.recursive(
        scalars,
        lambda inner: (
            st.lists(inner, max_size=4) | st.dictionaries(text, inner, max_size=4)
        ),
        max_leaves=12,
    )


# Inputs Pig doesn't handle yet. The tests below leave them out, so that they can find
# anything else, and each has a test of its own at the end, marked as expected to fail.
# When one's fixed, its test passes, which fails the run (that's what `strict` does):
# then take the mark off, and put the input back in here.
#
# - Half of a UTF-16 pair is a 500 (#106). Then `text` can be `any_text`.
# - NaN and the infinities are stored, as lines strict JSON readers refuse (#107). Then
#   `floats` can be `any_float`.
# - A media ID beyond SQLite's integers, which go to 2**63 - 1, is a 500. Then
#   `media_ids` can be `st.integers()`.
# - A finish that claims more parts than a media item can have takes as long as the
#   number is big, holding the database's write lock all the while. Then
#   `part_counts` can be `st.integers()`.
text = st.text(max_size=20)
floats = st.floats(allow_nan=False, allow_infinity=False)
any_json = json_values(text, floats)
media_ids = st.integers(min_value=-(2**63), max_value=2**63 - 1)
part_counts = st.integers(max_value=2 * media.MAX_PART)

# What a request with the right outline holds, so most examples get past the first
# check and on to the ones after it.
parameter_names = st.sampled_from(["participant_id", "session", "id", "modules"]) | text
parameters = st.dictionaries(parameter_names, any_json | text, max_size=5)
event_ids = st.integers() | text
events = st.dictionaries(
    text, any_json | st.fixed_dictionaries({"data": any_json}), max_size=5
)


def start_bodies(json, parameters):
    return json | st.fixed_dictionaries(
        {"parameters": parameters}, optional={"signature": json}
    )


def event_bodies(json, events):
    return json | st.fixed_dictionaries({"events": json | events})


def media_start_bodies(json, event_ids):
    return json | st.fixed_dictionaries({"event_id": event_ids | json, "data": json})


# ------------------------------------------------------------------------- the tests

task_codes = st.sampled_from(TASK_CODES) | text


@given(task_code=task_codes, body=start_bodies(any_json, parameters))
def test_starting_a_run(task_code: str, body: Any):
    with fresh_pig() as pig:
        check_answer(*answer(lambda: start(pig, task_code, body)))


@given(task_code=task_codes, body=start_bodies(any_json, parameters))
def test_starting_a_run_with_a_good_signature(task_code: str, body: Any):
    """The same, but signed the way `pigsty sign` would, so the parameters get past the
    signature and on to the checks after it."""
    if isinstance(body, dict) and isinstance(body.get("parameters"), dict):
        values = {
            name: value
            for name, value in body["parameters"].items()
            if name in ("id", "modules") and isinstance(value, str)
        }
        body["signature"] = signing.sign(signing_key(), "teddy", values)
    with fresh_pig() as pig:
        check_answer(*answer(lambda: start(pig, task_code, body)))


@given(task_code=st.sampled_from(sorted(STARTS)), body=event_bodies(any_json, events))
def test_sending_events(task_code: str, body: Any):
    with fresh_pig() as pig:
        run_id = started(pig, task_code)
        check_answer(*answer(lambda: send_events(pig, task_code, run_id, body)))
        check_stored_lines(pig)


@given(
    task_code=st.sampled_from(sorted(STARTS)),
    body=media_start_bodies(any_json, event_ids),
)
def test_starting_a_media_item(task_code: str, body: Any):
    with fresh_pig() as pig:
        run_id = started(pig, task_code)
        check_answer(*answer(lambda: pig.start_media(task_code, run_id, body)))
        check_stored_lines(pig)


@given(
    media_id=media_ids,
    part=st.integers(),
    chunks=st.lists(st.binary(max_size=600), max_size=4),
    content_length=st.none() | st.integers(min_value=0),
)
def test_sending_a_part(
    media_id: int, part: int, chunks: list[bytes], content_length: int | None
):
    with fresh_pig() as pig:
        run_id = started(pig, "interview")
        pig.start_media("interview", run_id, {"event_id": "1", "data": {}})
        check_answer(
            *answer(
                lambda: send_part(
                    pig, "interview", run_id, media_id, part, chunks, content_length
                )
            )
        )


@given(
    media_id=media_ids,
    body=any_json | st.fixed_dictionaries({"parts": part_counts | any_json}),
)
def test_finishing_a_media_item(media_id: int, body: Any):
    with fresh_pig() as pig:
        run_id = started(pig, "interview")
        pig.start_media("interview", run_id, {"event_id": "1", "data": {}})
        check_answer(
            *answer(lambda: pig.finish_media("interview", run_id, media_id, body))
        )


@given(
    body=any_json
    | st.fixed_dictionaries(
        {"purge_hashes": any_json | st.dictionaries(text, text, max_size=4)}
    )
)
def test_purging(body: Any):
    with fresh_pig() as pig:
        run_id = started(pig, "stroop")
        pig.finalize_run("stroop", run_id)
        sweep.sweep(pig.config, pig.connection)
        check_answer(*answer(lambda: pig.purge_run("stroop", run_id, body)))


@given(task_code=task_codes, run_id=text, real_run=st.booleans())
def test_any_address(task_code: str, run_id: str, real_run: bool):
    """Task codes and run IDs come from the URL, and can be any text but a slash. (Half
    of a UTF-16 pair can't arrive this way: a server decodes a URL's bytes as UTF-8,
    and bytes that aren't become a replacement character.)"""
    with fresh_pig() as pig:
        if real_run:
            run_id = started(pig, "stroop")
        for request in (
            lambda: pig.describe_task(task_code),
            lambda: pig.describe_run(task_code, run_id),
            lambda: pig.finalize_run(task_code, run_id),
            lambda: send_events(pig, task_code, run_id, {"events": {}}),
        ):
            check_answer(*answer(request))


# ------------------------------------------------------- inputs with open issues
#
# See the note above `text`. Each of these passes once its input is handled. They make
# sure every example has the input in it, and they don't shrink what they find: the
# failure is known, and all they need to show is that it's still there.

known = settings(phases=[Phase.explicit, Phase.reuse, Phase.generate])

# Text with half of a UTF-16 pair somewhere in it.
with_half_a_pair = st.tuples(text, st.characters(categories=["Cs"]), text).map("".join)


@pytest.mark.xfail(
    strict=True, raises=UnicodeEncodeError, reason="#106: half a UTF-16 pair is a 500"
)
@known
@given(half=with_half_a_pair, other=text, in_the_event_id=st.booleans())
def test_half_a_utf16_pair_is_refused(half: str, other: str, in_the_event_id: bool):
    event_id, data = (half, other) if in_the_event_id else (other, {"said": half})
    with fresh_pig() as pig:
        run_id = started(pig, "stroop")
        body = {"events": {event_id: {"data": data}}}
        check_answer(*answer(lambda: send_events(pig, "stroop", run_id, body)))


@pytest.mark.xfail(
    strict=True, raises=ValueError, reason="#107: NaN is stored, and readers refuse it"
)
@known
@given(
    data=st.dictionaries(text, any_json, max_size=3),
    name=text,
    value=st.sampled_from([math.nan, math.inf, -math.inf]),
)
def test_nan_and_infinity_are_refused(data: dict[str, Any], name: str, value: float):
    with fresh_pig() as pig:
        run_id = started(pig, "stroop")
        body = {"events": {"1": {"data": {**data, name: value}}}}
        check_answer(*answer(lambda: send_events(pig, "stroop", run_id, body)))
        check_stored_lines(pig)


@pytest.mark.xfail(
    strict=True, raises=OverflowError, reason="a media ID too big for SQLite is a 500"
)
@known
@given(media_id=st.integers(min_value=2**63) | st.integers(max_value=-(2**63) - 1))
def test_any_media_id(media_id: int):
    """A media ID comes from the URL, and FastAPI reads it as a whole number of any
    size."""
    with fresh_pig() as pig:
        run_id = started(pig, "interview")
        check_answer(
            *answer(lambda: pig.finish_media("interview", run_id, media_id, {}))
        )
        check_answer(
            *answer(
                lambda: send_part(pig, "interview", run_id, media_id, 1, [b"x"], None)
            )
        )


@pytest.mark.xfail(
    strict=True, raises=TimeoutError, reason="a huge part count holds the write lock"
)
def test_a_huge_part_count_is_refused_at_once():
    """No media item can have more than `MAX_PART` parts, so a finish that says it has
    can be refused straight away. Today Pig lists every part it's missing first, all
    under the database's write lock, which for a trillion parts is hours and more
    memory than the machine has, and no other request can store anything meanwhile.

    One example rather than many: each takes as long as the time limit to fail."""
    with fresh_pig() as pig:
        run_id = started(pig, "interview")
        pig.start_media("interview", run_id, {"event_id": "1", "data": {}})
        body = {"parts": 10**12}
        with time_limit(seconds=1):
            check_answer(
                *answer(lambda: pig.finish_media("interview", run_id, 1, body))
            )


# --------------------------------------------------------------------------- helpers
#
# Each does what the matching endpoint in app.py does with a request.


def start(pig, task_code: str, body: Any) -> dict[str, Any]:
    parameters, signature = service.read_start_request(body)
    return pig.start_run(task_code, parameters, signature)


def send_events(pig, task_code: str, run_id: str, body: Any):
    return pig.store_events(task_code, run_id, service.read_events_request(body))


def started(pig, task_code: str) -> str:
    return pig.start_run(task_code, STARTS[task_code])["run_id"]
