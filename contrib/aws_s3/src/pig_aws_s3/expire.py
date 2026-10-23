"""Delete old versions from a versioned bucket, for S3 services without lifecycle rules.

With versioning on, nothing in a bucket is really gone when it's deleted or overwritten:
the old version stays, and can be brought back. That's the undo window. On AWS, a
lifecycle rule (`NoncurrentVersionExpiration`) ends it after a set number of days. Some
S3-compatible services, the campus IBM Cleversafe among them, don't implement lifecycle
rules, so without this old versions are kept forever, purged runs included.

A version is deleted once it has been noncurrent, replaced or deleted, for longer than
`--keep-days`. That's counted from when the version after it was written, as AWS counts
it. A delete marker is removed once it's older than `--keep-days` and every version it
hid is gone. The current version of an object is never touched.

Nothing is deleted without `--delete`. Without it, this prints what it would delete.

Run it from somewhere other than the web server, with a key the web server doesn't have.
The web server's key shouldn't be able to delete versions: if it could, a web server that
had been broken into could end the undo window itself.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import boto3
from botocore.config import Config


@dataclass(frozen=True)
class Entry:
    """One version of one object, or a delete marker, as the bucket lists it."""

    key: str
    version_id: str
    last_modified: datetime
    is_latest: bool
    is_delete_marker: bool


def to_delete(entries: list[Entry], now: datetime, keep_days: int) -> list[Entry]:
    """The entries that have been noncurrent for longer than `keep_days`, plus delete
    markers with nothing left behind them. Never an object's current version."""
    cutoff = now - timedelta(days=keep_days)
    by_key: dict[str, list[Entry]] = {}
    for entry in entries:
        by_key.setdefault(entry.key, []).append(entry)

    doomed = []
    for key in sorted(by_key):
        # Newest first, with the current one at the front whatever the timestamps say:
        # they're to the second, and a delete marker can share one with a version.
        versions = sorted(
            by_key[key],
            key=lambda entry: (entry.is_latest, entry.last_modified),
            reverse=True,
        )
        if not versions[0].is_latest:
            # Every key has a current entry. One without has been listed partway through
            # a change, so it's left for the next run.
            continue
        older = []
        for newer, version in pairwise(versions):
            # A version became noncurrent when the one after it was written.
            if newer.last_modified < cutoff:
                older.append(version)
        doomed.extend(older)

        latest = versions[0]
        everything_behind_it_goes = len(older) == len(versions) - 1
        if (
            latest.is_delete_marker
            and latest.last_modified < cutoff
            and everything_behind_it_goes
        ):
            doomed.append(latest)
    return doomed


def list_entries(client: Any, bucket: str, prefix: str) -> list[Entry]:
    """Every version and delete marker under `prefix`."""
    entries = []
    pages = client.get_paginator("list_object_versions").paginate(
        Bucket=bucket, Prefix=prefix
    )
    for page in pages:
        for item in page.get("Versions", []):
            entries.append(_entry(item, is_delete_marker=False))
        for item in page.get("DeleteMarkers", []):
            entries.append(_entry(item, is_delete_marker=True))
    return entries


def _entry(item: dict[str, Any], is_delete_marker: bool) -> Entry:
    return Entry(
        key=item["Key"],
        version_id=item["VersionId"],
        last_modified=item["LastModified"],
        is_latest=item["IsLatest"],
        is_delete_marker=is_delete_marker,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="pig-expire-versions",
        description=(
            "Delete versions that have been noncurrent for longer than --keep-days, "
            "for S3 services without lifecycle rules. Prints what it would delete "
            "unless given --delete."
        ),
    )
    parser.add_argument("--bucket", required=True)
    parser.add_argument(
        "--prefix",
        required=True,
        help='Only look under this prefix. Give "" for the whole bucket.',
    )
    parser.add_argument(
        "--keep-days",
        required=True,
        type=int,
        help="How long a deleted or replaced version is kept: the undo window.",
    )
    parser.add_argument("--profile", help="AWS CLI profile with the credentials.")
    parser.add_argument("--endpoint-url", help="For an S3 service other than AWS.")
    parser.add_argument(
        "--delete", action="store_true", help="Delete. Without this, nothing is."
    )
    args = parser.parse_args(argv)
    if args.keep_days < 1:
        parser.error("--keep-days has to be at least 1.")

    session = boto3.Session(profile_name=args.profile)
    client = session.client(
        "s3",
        endpoint_url=args.endpoint_url,
        # Only the checksums S3 requires. Newer boto3 adds its own to every request,
        # and S3-compatible services that don't know them refuse the request.
        config=Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )

    doomed = to_delete(
        list_entries(client, args.bucket, args.prefix),
        now=datetime.now(UTC),
        keep_days=args.keep_days,
    )
    failed = 0
    for entry in doomed:
        what = "delete marker" if entry.is_delete_marker else "version"
        described = (
            f"{entry.key} ({what} {entry.version_id}, "
            f"{entry.last_modified.isoformat()})"
        )
        if not args.delete:
            print(f"would delete {described}")
            continue
        try:
            # One at a time, rather than DeleteObjects, which requires a checksum
            # header that some S3-compatible services don't take.
            client.delete_object(
                Bucket=args.bucket, Key=entry.key, VersionId=entry.version_id
            )
        except Exception as error:  # noqa: BLE001 -- reported, then the rest go on
            failed += 1
            print(f"couldn't delete {described}: {error}", file=sys.stderr)
            continue
        print(f"deleted {described}")

    if args.delete:
        print(f"Deleted {len(doomed) - failed} of {len(doomed)}.")
    else:
        print(f"Would delete {len(doomed)}. Give --delete to delete them.")
    if failed:
        raise SystemExit(1)
