#!/usr/bin/env python3
"""Put the runs in a `pigsty organize` tree in order for analysis.

    pig-prepare ORGANIZED PREPARED

ORGANIZED can be a whole tree, any directory in one, or a single run's directory
(one with a pig-run.json). For each run, it writes to
PREPARED/{task_code}/{one directory per run key value}/, like
PREPARED/stroop/10351/baseline/, with _0002 and so on after the last value for a
participant's second run:

- events.csv: one row per event, with the run's fields and link parameters on
  every row, and nested fields flattened into dotted columns
- one file per media item, its parts joined, and remuxed with ffmpeg if it's audio
  or video so that it plays and seeks

It also keeps PREPARED/runs.csv, one row per prepared run, and PREPARED/events.csv,
every run's rows in one file.

A media item's file is named by the `name` in its event's data, if the task sent
one, and by its event ID otherwise. Its extension comes from `content_type`.

It never changes ORGANIZED. A run is prepared once: a run listed in runs.csv is
skipped next time (unless --force), and PREPARED/events.csv is rebuilt only when a
run was added.

This is a starting point. Copy it into your study and change what you need; the
parts most likely to change are near the top.
"""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

# The file `pigsty admit` writes into each run, and organize copies into the tree. It's
# how this finds a run, and everything it knows about the run comes from it.
RUN_FILE = "pig-run.json"
RUN_FILE_TYPE = "pig_run"
RUN_FILE_VERSIONS = (1, 2)

# Columns that come first, in this order, whether or not a run has them. Every other
# field follows in the order it was first seen.
RUN_COLUMNS = ["task_code", "run_number", "run_id", "disposition"]
EVENT_COLUMNS = [
    "event_id",
    "stored_at",
    "media_file",
    "media_finished",
    "media_duration_s",
    "_client.wall_time",
    "_client.performance_now",
]

# What a link parameter's column is called when an event field already has its name
# (a link with ?name=..., say).
PARAMETER_PREFIX = "param."

# Top-level fields your task's events can have that might also be link parameters. A
# parameter with one of these names gets the prefix, so its column doesn't collide with
# the event field's. It's a fixed list, rather than the fields seen so far, so that a
# parameter's column doesn't change name as more runs are prepared. If a run's
# parameter does collide, this script says so, and the fix is to add its name here.
EVENT_FIELDS = {
    "event_id",
    "stored_at",
    "media_file",
    "media_finished",
    "media_duration_s",
    "_client",
    "data",
    "name",
    "content_type",
}

# File extensions for the content types browsers record in. Anything else goes to
# Python's mimetypes, then .bin.
EXTENSIONS = {
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/mp4": ".mp4",
    # Not .weba: ffmpeg doesn't know it, and picks the file's format by its extension.
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
}


def media_name(event_id, data):
    """A media item's file name, without the extension.

    The task names it with `name` in the event's data, like `prompt3` or `drawing`. A
    `name` that ends with the extension its content type gets (`drawing.png`) has it
    taken off, so the file isn't `drawing.png.png`. Anything other than letters, digits,
    `-` and `_` becomes `_`. Without a `name`, it's `media_` and the event ID.

    This is the place to change if your task doesn't send `name`, and its files should
    be named from other fields: `f"{data['block']}_{data['trial']}"`, say.
    """
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, str) or not name.strip():
        return f"media_{safe(event_id)}"
    suffix = extension(data.get("content_type"))
    if name.lower().endswith(suffix) and len(name) > len(suffix):
        name = name[: -len(suffix)]
    return safe(name)


def safe(text):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text)


def content_base(content_type):
    """`audio/webm;codecs=opus` is `audio/webm`."""
    if not isinstance(content_type, str):
        return ""
    return content_type.split(";")[0].strip().lower()


def extension(content_type):
    base = content_base(content_type)
    return EXTENSIONS.get(base) or mimetypes.guess_extension(base) or ".bin"


def is_recording(content_type):
    """Audio and video sent in parts need remuxing to play and seek. Anything else, like
    an image, is complete once its parts are joined."""
    return content_base(content_type).split("/")[0] in ("audio", "video")


class Problems:
    """Everything that went wrong, reported as it happens and counted at the end. A note
    is something worth knowing that nobody needs to fix, like a recording cut short by
    a closed tab; notes don't count."""

    def __init__(self):
        self.count = 0

    def __call__(self, where, message):
        self.count += 1
        print(f"{where}: {message}", file=sys.stderr)

    def note(self, where, message):
        print(f"{where}: {message}", file=sys.stderr)


# ---------------------------------------------------------------- reading runs


def read_run_file(run_dir, problem):
    """The run's pig-run.json, or None if it can't be used."""
    try:
        run = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        problem(run_dir, f"couldn't read {RUN_FILE} ({e}); skipped")
        return None
    if not isinstance(run, dict) or run.get("type") != RUN_FILE_TYPE:
        problem(run_dir, f"{RUN_FILE} isn't a Pig run file; skipped")
        return None
    if run.get("run_file_version") not in RUN_FILE_VERSIONS:
        problem(
            run_dir,
            f"{RUN_FILE} is version {run.get('run_file_version')!r}, and this script "
            f"reads versions {', '.join(map(str, RUN_FILE_VERSIONS))}; skipped",
        )
        return None
    return run


def read_events(run_dir, problem):
    """A run's events, in event ID order with repeats dropped, each as (event ID, data,
    row), where row is the event's data flattened into columns."""
    events = {}
    with open(run_dir / "events.jsonl", encoding="utf-8") as f:
        for number, line in enumerate(f, 1):
            if not line.strip():
                continue
            stored = json.loads(line)
            event_id = str(stored["event_id"])
            if event_id in events:
                # A retry can store the same event twice. They should match.
                if events[event_id][0] != stored["data"]:
                    problem(
                        run_dir,
                        f"event {event_id} is stored twice with different data "
                        f"(line {number}); kept the first",
                    )
                continue
            stored_at = (stored.get("metadata") or {}).get("stored_at")
            events[event_id] = (stored["data"], stored_at)

    rows = []
    for event_id in sorted(events, key=event_order):
        data, stored_at = events[event_id]
        row = {"event_id": event_id, "stored_at": stored_at}
        try:
            flatten(data, row)
        except ValueError as e:
            problem(run_dir, f"event {event_id}: {e}")
        rows.append((event_id, data, row))
    return rows


def event_order(event_id):
    # Pig's client numbers events 0, 1, 2, ... in the order they were made. A task that
    # names its own event IDs gets them in alphabetical order, after any numbers.
    return (0, int(event_id), "") if event_id.isdigit() else (1, 0, event_id)


def flatten(data, row):
    """An event's fields as columns. A field that's an object of plain values becomes
    dotted columns: {"stimulus": {"word": ...}} is stimulus.word. A list, or an object
    with anything nested in it, stays whole in one cell, as JSON, so a big nested field
    doesn't turn into dozens of sparse columns. Pig's client's `_client` is the
    exception: its plain values are always columns, since every event's timing is in
    it. Data that isn't an object at all goes in one column, `data`."""
    if not isinstance(data, dict):
        put(row, "data", data)
        return
    for key, value in data.items():
        if isinstance(value, dict) and value and (key == "_client" or all_plain(value)):
            for inner, item in value.items():
                put(row, f"{key}.{inner}", item)
        else:
            put(row, key, value)


def all_plain(value):
    return not any(isinstance(v, (dict, list)) for v in value.values())


def put(row, name, value):
    if name in row:
        raise ValueError(f"two fields flatten to {name}; kept the first")
    row[name] = cell(value)


def cell(value):
    if value is None:
        return ""
    if isinstance(value, (bool, list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


# ---------------------------------------------------------------- media


def join_media(run_dir, out_dir, run, rows, force, problem):
    """Joins each media item's parts into one file, remuxed if it's a recording, and
    notes the file on the item's row. In the tree, an item's parts are in
    media/{event_id}/."""
    by_id = {event_id: (data, row) for event_id, data, row in rows}
    used = set()
    for item in sorted(
        run.get("media", []), key=lambda m: event_order(str(m["event_id"]))
    ):
        event_id = str(item["event_id"])
        where = f"{run_dir} media {event_id}"
        if event_id not in by_id:
            problem(where, "no event for this media item; skipped")
            continue
        data, row = by_id[event_id]
        row["media_finished"] = cell(item["finished"])

        parts = sorted(
            (run_dir / "media" / event_id).glob("*.part"), key=lambda p: int(p.stem)
        )
        if not parts:
            # A task that started an item and sent nothing. Its event is still a row.
            problem.note(where, "no parts")
            continue
        numbers = [int(p.stem) for p in parts]
        if numbers != list(range(1, len(parts) + 1)) or (
            item["finished"] and len(parts) != item.get("parts")
        ):
            problem(
                where,
                f"has parts {numbers}, but Pig lists {item.get('parts')}; joined what's there",
            )

        # Two items with one name in the same run (a prompt recorded again, say) get
        # _take2, _take3, in the order they were made. Compared without case, because on
        # a Mac Prompt3 and prompt3 are one file.
        content_type = data.get("content_type") if isinstance(data, dict) else None
        base = name = media_name(event_id, data)
        take = 1
        while name.casefold() in used:
            take += 1
            name = f"{base}_take{take}"
        used.add(name.casefold())
        target = out_dir / (name + extension(content_type))
        row["media_file"] = target.name

        if force or not target.exists():
            if not item["finished"]:
                problem.note(
                    where,
                    "the task never finished this item, so it may end early; joined what arrived",
                )
            if is_recording(content_type):
                remux(parts, target, where, problem, item["finished"])
            else:
                join(parts, target)
        if target.exists() and is_recording(content_type):
            row["media_duration_s"] = duration(target)


def join(parts, target):
    """The parts, in order, in one file, which appears only once it's whole."""
    scratch = target.with_name(target.stem + ".partial" + target.suffix)
    with open(scratch, "wb") as out:
        for part in parts:
            with open(part, "rb") as f:
                shutil.copyfileobj(f, out)
    scratch.replace(target)


def remux(parts, target, where, problem, finished):
    """Joins the parts in order, then copies the streams into a fresh file with ffmpeg. A
    recording sent in parts has no duration or seek index until it's remuxed. If ffmpeg
    can't read the joined file, or isn't installed, the joined file is kept, named
    .joined, for a person to look at."""
    joined = target.with_name(target.stem + ".joined" + target.suffix)
    join(parts, joined)
    if shutil.which("ffmpeg") is None:
        problem(
            where,
            f"ffmpeg isn't installed, or isn't on the PATH; the joined parts are in {joined.name}",
        )
        return
    scratch = target.with_name(target.stem + ".partial" + target.suffix)
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-i",
            joined,
            "-c",
            "copy",
            scratch,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        scratch.unlink(missing_ok=True)
        problem(
            where,
            f"ffmpeg couldn't remux it; the joined parts are in {joined.name}\n{result.stderr.strip()}",
        )
        return
    if result.stderr.strip():
        # An unfinished recording is expected to end mid-frame.
        report = problem if finished else problem.note
        report(where, f"ffmpeg reported problems remuxing it\n{result.stderr.strip()}")
    scratch.replace(target)
    joined.unlink()


def duration(path):
    if shutil.which("ffprobe") is None:
        return ""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return round(float(result.stdout.strip()), 3)
    except ValueError:
        return ""


# ---------------------------------------------------------------- finding runs


def find_runs(organized):
    """Every run directory under ORGANIZED, in order, or ORGANIZED itself if it's a run.
    A run's directory is the one with a pig-run.json, and nothing below it is a run, so
    the walk never goes inside one: it lists directories, not every media part."""
    runs = []
    for directory, subdirectories, files in os.walk(organized):
        if RUN_FILE in files:
            runs.append(Path(directory))
            subdirectories.clear()
        else:
            subdirectories.sort()
    return sorted(runs)


def run_path(run):
    """Where a run goes in PREPARED: its task code, then a directory per run key value,
    like stroop/10351/baseline. A participant's second run of that session is
    stroop/10351/baseline_0002."""
    names = run.get("run_key_parameters") or []
    if not names:
        raise ValueError(
            "it has no run_key_parameters, so there's nothing to name its directory by"
        )
    values = [run.get("parameters", {}).get(n) for n in names]
    for name, value in zip(names, values):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError(f"its {name}, {value!r}, can't be a directory name")
    if run["run_number"] > 1:
        values[-1] = f"{values[-1]}_{run['run_number']:04d}"
    return Path(run["task_code"], *values)


# ---------------------------------------------------------------- the index

# PREPARED/runs.csv lists every run that's been prepared, one row each. It's how the
# script knows a run is done, and it's the place to start an analysis: filter on
# disposition, or on problems, then follow path.
INDEX_FILE = "runs.csv"


def read_index(path):
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {row["run_id"]: row for row in csv.DictReader(f)}


def index_row(run, path, rows, problems):
    media = run.get("media", [])
    key = {column_for(n): run["parameters"].get(n) for n in run["run_key_parameters"]}
    return {
        "task_code": run["task_code"],
        **key,
        "run_number": run["run_number"],
        "path": path.as_posix(),
        "run_id": run["run_id"],
        "disposition": run["disposition"],
        "started_at": run.get("started_at"),
        "closed_at": run.get("closed_at"),
        "events": len(rows),
        "media": len(media),
        "unfinished_media": sum(1 for m in media if not m["finished"]),
        "problems": problems,
        "prepared_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def write_index(path, index):
    columns = []
    for row in index.values():
        columns += [c for c in row if c not in columns]

    def order(row):
        # By task code and the run key's values (the columns before run_number), then
        # run number.
        key = list(row.values())[: list(row).index("run_number")]
        return [str(v) for v in key], int(row["run_number"])

    write_csv(path, columns, sorted(index.values(), key=order))


# ---------------------------------------------------------------- writing CSVs


def parameters(run):
    """The run's link parameters, run key first, then the rest the task declares, then
    any extras the link carried, each under its column name."""
    declared = run.get("parameters", {})
    extra = run.get("extra_parameters", {}) or {}
    names = list(run.get("run_key_parameters", []))
    names += [n for n in declared if n not in names]
    names += [n for n in extra if n not in names]
    values = {**extra, **declared}
    return {column_for(n): values.get(n) for n in names}


def column_for(parameter):
    if parameter in EVENT_FIELDS or parameter in RUN_COLUMNS:
        return PARAMETER_PREFIX + parameter
    return parameter


def write_run_csv(path, run, rows, problem):
    """The run's events.csv. It's written last, once its media files are, and appears
    all at once, since its being there is what says the run is done."""
    fields = {c: run.get(c) for c in RUN_COLUMNS}
    params = parameters(run)
    seen = {c for _, _, row in rows for c in row}
    for column in params:
        if column in seen:
            problem(
                path.parent,
                f"the link parameter {column} is also an event field; add it to "
                f"EVENT_FIELDS so it gets a {PARAMETER_PREFIX} prefix",
            )
    fields.update(params)
    columns = list(fields) + EVENT_COLUMNS
    for _, _, row in rows:
        columns += [c for c in row if c not in columns]
    write_csv(path, columns, ({**fields, **row} for _, _, row in rows))


def write_combined(path, prepared, run_dirs):
    """Every run's events.csv in one file, read back from the runs' own CSVs so nothing
    has to be held in memory. Its columns are every run's columns: run fields, then
    every parameter, then every event field, so a column one run doesn't have is empty
    in its rows."""
    csvs = [d / "events.csv" for d in run_dirs if (d / "events.csv").exists()]
    params, fields = [], list(EVENT_COLUMNS)
    for run_csv in csvs:
        with open(run_csv, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        split = header.index("event_id") if "event_id" in header else len(header)
        params += [c for c in header[len(RUN_COLUMNS) : split] if c not in params]
        fields += [c for c in header[split:] if c not in fields]

    def rows():
        for run_csv in csvs:
            here = run_csv.parent.relative_to(prepared).as_posix()
            with open(run_csv, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("media_file"):
                        row["media_file"] = f"{here}/{row['media_file']}"
                    yield row

    write_csv(path, RUN_COLUMNS + params + fields, rows())
    return len(csvs)


def write_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + ".partial")
    with open(scratch, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    scratch.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "organized",
        type=Path,
        help="a pigsty organize tree, any directory in one, or one run's directory",
    )
    parser.add_argument(
        "prepared", type=Path, help="where to write the CSVs and media files"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="prepare every run again, even ones already in PREPARED",
    )
    parser.add_argument(
        "--no-combined", action="store_true", help="don't rebuild PREPARED/events.csv"
    )
    args = parser.parse_args(argv)

    organized = args.organized.resolve()
    prepared = args.prepared.resolve()
    if (
        prepared == organized
        or organized in prepared.parents
        or prepared in organized.parents
    ):
        sys.exit("PREPARED has to be outside ORGANIZED, and the other way round.")

    problem = Problems()
    index_path = prepared / INDEX_FILE
    index = read_index(index_path)
    taken = {row["path"]: run_id for run_id, row in index.items()}
    run_dirs = find_runs(organized)
    done = 0
    try:
        for run_dir in run_dirs:
            run = read_run_file(run_dir, problem)
            if run is None:
                continue
            run_id = run["run_id"]
            try:
                path = run_path(run)
            except ValueError as e:
                problem(run_dir, f"{e}; skipped")
                continue
            if taken.get(path.as_posix(), run_id) != run_id:
                problem(
                    run_dir,
                    f"run {run_id} would go in {path}, which already holds run {taken[path.as_posix()]}; skipped",
                )
                continue
            out_dir = prepared / path
            if not args.force and run_id in index and (out_dir / "events.csv").exists():
                continue

            before = problem.count
            rows = read_events(run_dir, problem)
            out_dir.mkdir(parents=True, exist_ok=True)
            join_media(run_dir, out_dir, run, rows, args.force, problem)
            write_run_csv(out_dir / "events.csv", run, rows, problem)
            index[run_id] = index_row(run, path, rows, problem.count - before)
            taken[path.as_posix()] = run_id
            done += 1
    finally:
        # Whatever got done is recorded, even if something stopped the script.
        if done:
            write_index(index_path, index)

    print(f"Prepared {done} new run(s); {len(index)} in {index_path}.")
    # Rebuilt only when it's missing or a run was added, since at thousands of runs it's
    # the slowest part of a run with nothing new.
    combined = prepared / "events.csv"
    if not args.no_combined and (done or not combined.exists()):
        run_dirs = [
            prepared / row["path"]
            for row in sorted(index.values(), key=lambda r: r["path"])
        ]
        count = write_combined(combined, prepared, run_dirs)
        print(f"Wrote {combined}, with {count} run(s).")
    if problem.count:
        sys.exit(f"{problem.count} problem(s); see above.")
    return 0


if __name__ == "__main__":
    main()
