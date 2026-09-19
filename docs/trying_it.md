# Trying Pig out

**Status: early.** This is a first implementation, built so there's something real to point
a task at. It collects and stores data correctly, and it's expected to change — especially
where the shape of it turns out to be annoying to use. That feedback is the point of it
existing this early, so if something here is awkward, say so.

Getting a server running takes about five minutes and needs nothing but Python.

## Start it

```
uv sync
mkdir local
cp pig.example.toml local/pig.toml
uv run pig check          # says what your tasks are set up to do
uv run pig serve          # http://127.0.0.1:8000
```

`pig serve` runs in the foreground and logs every request. Stop it with Ctrl-C.

Everything your copy accumulates lives under `local/` — the configuration file, the
database, and the data. It's the one directory git ignores, so nothing you collect while
testing can end up in a commit, and deleting `local/` gets you a clean slate.

**You can edit `local/pig.toml` while the server is running.** It notices the file
changing and the next request uses it — open or close a task, add a new one, change how
long its runs stay open, no restart needed. If you save something that doesn't parse, the server
keeps running on the last version that worked and `pig health` tells you what's wrong.

The exception is `data_root` and `database`: change those with the server stopped, or
runs already in progress end up with their data somewhere nothing will look for it.

## Send it something

The example configuration has a `stroop` task expecting `participant_id` and `session`.
From another terminal:

```
curl -X POST localhost:8000/task/stroop/run \
  -H 'Content-Type: application/json' \
  -d '{"participant_id": "10351", "session": "baseline"}'
```

That gives you a run ID. Send events to it, keyed by IDs you make up:

```
curl -X POST localhost:8000/task/stroop/run/YOUR-RUN-ID \
  -H 'Content-Type: application/json' \
  -d '{"1": {"data": {"type": "task_start"}}}'
```

...and when you're done:

```
curl -X POST localhost:8000/task/stroop/run/YOUR-RUN-ID/finalize
```

[api.md](api.md) has the full detail and a JavaScript example you can paste into a task.

The example configuration also has an `interview` task that takes media. Start a run of
it, then send a recording as an event plus its bytes:

```
curl -X POST localhost:8000/task/interview/run/YOUR-RUN-ID/media \
  -H 'Content-Type: application/json' \
  -d '{"event_id": "prompt1_audio", "data": {"content_type": "audio/webm"}}'
curl -X PUT localhost:8000/task/interview/run/YOUR-RUN-ID/media/1/1 \
  --data-binary @some-file.webm
curl -X POST localhost:8000/task/interview/run/YOUR-RUN-ID/media/1/finish \
  -H 'Content-Type: application/json' -d '{"parts": 1}'
```

## See where the data went

Every run is a directory named for its run ID. While a run is in progress it's under
`local/data/in_progress/stroop/`; it moves to `local/data/done/stroop/` when you finish
it:

```
uv run pig sweep
```

That's the scheduled half of Pig — finishing closed runs and expiring runs that have been
open too long. In production a systemd timer runs it every few minutes; on a laptop, run
it by hand when you want to watch a run reach `complete`. **Until you run it, finalized
runs sit in `finalizing` and their directories stay in `local/data/in_progress/`.**
That's normal, not a failure.

Then:

```
uv run pig runs                 # every run, most recent first
uv run pig health               # what GET /health reports
ls local/data/done/stroop/YOUR-RUN-ID/
cat local/data/done/stroop/YOUR-RUN-ID/manifest.json
cat local/data/done/stroop/YOUR-RUN-ID/events.jsonl
ls local/data/done/interview/YOUR-RUN-ID/media/00001/     # the parts, if you sent any
```

The manifest says what the run is — who, which session, which run number, when — and the
events file is what your task sent, one line per event, in the order it arrived. A
readable tree named for participants and sessions is what `pig organize` will build from
these; it isn't written yet.

## Things worth trying to break

These all behave a particular way on purpose, and each one is a decision that could be
wrong:

- **Send the same batch twice.** The second one is a `200` and writes nothing.
- **Send the same event ID with different content.** Refused with `can_retry: false`. Pig
  keeps what it had.
- **Start the same participant and session twice.** Two runs, `run-0001` and `run-0002`,
  and nothing is overwritten.
- **Use a capital letter in `session`.** `Baseline` and `baseline` are two different
  sessions, each starting at `run-0001`. Pig keeps exactly what you typed.
- **Put a space or a dot in `participant_id`.** The run is refused, because that value
  will become a directory name.
- **Put a `timestamp` next to `data` in an event.** Refused, with a message saying to put
  it inside `data`. Pig stores only what's in `data`, and it won't drop anything silently.
- **Post events after finalizing.** `409`, the events aren't stored, and the message tells
  you to start a new run.
- **Add a parameter the task doesn't know about.** Ignored, and recorded on the run.
- **Send a media part to `stroop`.** `404`: that task isn't set up for media, and the
  message says what to set.
- **Finish a media item with the wrong count.** Refused, and the message says which
  parts are missing, or that Pig holds more than you said.
- **Send a part after finishing.** `409`, and the reply lists what Pig holds.

## What isn't here yet

The service is the requests in [api.md](api.md) plus `/health`. Not built: parameter
signing, participant rosters, `max_runs`, settings returned at run start, copying runs
offsite, `pig organize`, a JavaScript helper for media uploads, and per-task allowed
origins (every task currently allows any origin). See
[configuration.md](configuration.md) for what's marked *built* and what isn't.

## The other commands

| Command | What it does |
| --- | --- |
| `pig check` | Validate the configuration and print what each task will do. |
| `pig serve` | Run the web service. |
| `pig sweep` | Finish closed runs; expire runs that have been open too long. |
| `pig runs` | List runs. `--task` and `--status` narrow it. |
| `pig health` | The health report, as JSON. |

Any of them take `--config` if your file isn't `./local/pig.toml`.
