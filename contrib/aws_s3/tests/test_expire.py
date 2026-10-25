"""Which versions `pig-expire-versions` deletes. The bucket itself isn't involved: this is
the decision, given a listing."""

from datetime import UTC, datetime, timedelta

from pig_aws_s3.expire import Entry, to_delete

NOW = datetime(2026, 10, 23, tzinfo=UTC)


def days_ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def version(key, version_id, age, latest=False, marker=False) -> Entry:
    return Entry(key, version_id, days_ago(age), latest, marker)


def doomed(entries, keep_days=30) -> list[str]:
    return [e.version_id for e in to_delete(entries, NOW, keep_days)]


def test_the_current_version_is_never_deleted():
    assert doomed([version("a", "v1", 400, latest=True)]) == []


def test_a_version_replaced_long_ago_is_deleted():
    entries = [version("a", "v2", 40, latest=True), version("a", "v1", 400)]

    assert doomed(entries) == ["v1"]


def test_age_counts_from_when_it_was_replaced_not_written():
    # Written 400 days ago, but only replaced 10 days ago: still in the undo window.
    entries = [version("a", "v2", 10, latest=True), version("a", "v1", 400)]

    assert doomed(entries) == []


def test_only_versions_replaced_long_enough_ago_go():
    entries = [
        version("a", "v3", 10, latest=True),
        version("a", "v2", 40),  # replaced 10 days ago
        version("a", "v1", 400),  # replaced 40 days ago
    ]

    assert doomed(entries) == ["v1"]


def test_a_deleted_object_inside_the_window_is_kept():
    entries = [version("a", "m1", 5, latest=True, marker=True), version("a", "v1", 400)]

    assert doomed(entries) == []


def test_a_deleted_object_past_the_window_goes_with_its_marker():
    entries = [
        version("a", "m1", 40, latest=True, marker=True),
        version("a", "v1", 400),
    ]

    assert doomed(entries) == ["v1", "m1"]


def test_a_marker_stays_while_something_behind_it_does():
    entries = [
        version("a", "m1", 40, latest=True, marker=True),
        version("a", "v2", 50),
        version("a", "v1", 400),
    ]

    # v2 was deleted 40 days ago and goes; v1 was replaced 50 days ago and goes too.
    assert doomed(entries) == ["v2", "v1", "m1"]


def test_the_current_entry_comes_first_even_with_a_tied_timestamp():
    same = days_ago(40)
    entries = [
        Entry("a", "v1", same, is_latest=False, is_delete_marker=False),
        Entry("a", "m1", same, is_latest=True, is_delete_marker=True),
    ]

    assert doomed(entries) == ["v1", "m1"]


def test_a_key_with_no_current_entry_is_left_alone():
    assert doomed([version("a", "v1", 400)]) == []


def test_keys_are_decided_separately():
    entries = [
        version("a", "v2", 40, latest=True),
        version("a", "v1", 400),
        version("b", "w1", 400, latest=True),
    ]

    assert doomed(entries) == ["v1"]
