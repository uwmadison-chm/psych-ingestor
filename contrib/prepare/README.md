# Preparing organized runs for analysis

`pigsty organize` copies each run exactly as Pig stored it: events as JSON lines, media in numbered parts. `pig-prepare` takes it from there. For each run it writes a CSV of its events, and a file for each media item that you can open: a recording you can play, an image you can look at.

It's a starting point, not a finished tool. What a task's events mean, which fields matter, and what its files should be called are up to the task, so copy `src/pig_prepare/prepare.py` into your study and change what you need. The parts most likely to change are near the top.

It needs Python 3.12 or later and nothing else, except ffmpeg and ffprobe to make recordings playable. Without them, it still joins each recording's parts, and says it couldn't remux them.

```
python3 prepare.py /secure/circe/intermediate_data/pig/stroop /secure/circe/prepared
```

Or install it with uv, which puts `pig-prepare` on your PATH:

```
uv tool install "git+https://github.com/uwmadison-chm/psych-ingestor#subdirectory=contrib/prepare"
```

The first argument can be a whole organized tree, any directory in one, or one run's directory. The second is where it writes:

```
prepared/
    runs.csv                   one row per prepared run
    events.csv                 every run's events in one file
    stroop/10351/baseline/     task, then each value in the task's run key
        events.csv
        prompt3.webm
        drawing.png
    stroop/10351/baseline_0002/   that participant's second run of that session
```

`runs.csv` is the place to start an analysis. Each row has the task code, the run key, `run_number`, `path`, `run_id`, `disposition` (`finalized`, or `expired` if Pig closed the run before the task finished it), when the run started and closed, how many events and media items it has, how many media items the task never finished, how many problems preparing it reported, and when it was prepared.

Each `events.csv` has a row per event, in event ID order, with a repeated event (a retry Pig stored twice) dropped. Every row carries the run's task code, run number, run ID, disposition, and link parameters, so rows from different runs can be put together. An event field that's an object of plain values becomes dotted columns: `{"stimulus": {"word": "owl"}}` is `stimulus.word`. Anything more deeply nested stays whole, as JSON, in one cell.

## What media files are called

A media item's file is named by the `name` in its event's data, and its extension comes from `content_type`. A task that starts an item with

```javascript
run.startMedia({ content_type: "image/png", name: "drawing" });
```

gets `drawing.png`. A `name` that already ends with the extension, like `drawing.png`, isn't given it twice. Anything other than letters, digits, `-` and `_` becomes `_`, so a name can't reach outside its run's directory.

Without a `name`, the file is named by the event ID: `media_12.png`. Pig's client numbers events, so that tells you nothing about what's in it. Send a `name`.

Two items in a run with the same name, like a prompt the participant recorded again, are `prompt3.webm` and `prompt3_take2.webm`, in the order they were made.

If your task already sends fields that say what a recording is, change `media_name()` to build the name from them, like `f"{data['block']}_{data['trial']}"`.

Each recording's `media_file`, `media_finished`, and `media_duration_s` are on its event's row.

## Running it again

Run it after each `pigsty organize`, as runs come in. A run is prepared once: a run listed in `runs.csv` is skipped next time, without reading its events or touching its media. Finding runs lists directories but never goes inside a run's, so the media parts aren't scanned. The combined `events.csv` is rebuilt from the runs' own CSVs only when there's a new run; at thousands of runs it's the slow part, and `--no-combined` skips it. `--force` prepares every run again.

It never changes the organized tree.

## Problems

Everything that goes wrong is reported as it happens, the run carries on where it can, and the script exits with an error at the end, so a scheduled job notices:

- **A media item's parts don't match what Pig recorded.** It joins what's there.
- **ffmpeg can't remux a recording.** The joined parts are kept as `prompt3.joined.webm`, for a person to look at.
- **An event is stored twice with different data.** The first is kept.
- **A link parameter has the same name as an event field.** Add its name to `EVENT_FIELDS`, and its column becomes `param.` and the name.
- **Two runs would land in the same directory.** The second isn't prepared. Pig's run numbers should make that impossible.

A recording the task never finished, because the participant closed the tab, say, is a note rather than a problem: it's joined, and ends where the data does. A recording split across two runs, because a run expired partway through it, is prepared as two files, one in each run. Joining those is up to you.
