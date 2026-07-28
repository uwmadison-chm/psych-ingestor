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

If you edit `local/pig.toml`, run `pig check` and then restart the server — it reads the
file once, when it starts.

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

## See where the data went

While a run is in progress its data is in `local/data/in_progress/`, named for the run ID.
It moves where your configuration says when you file it:

```
uv run pig sweep
```

That's the scheduled half of Pig — filing finished datasets and giving up on runs nobody
came back to. In production a systemd timer runs it every few minutes; on a laptop, run it
by hand when you want to watch a run reach `complete`. **Until you run it, finalized runs
sit in `finalizing` and their files stay in `local/data/in_progress/`.** That's normal,
not a failure.

Then:

```
uv run pig runs                 # every run, most recent first
uv run pig health               # what GET /health reports
cat local/data/complete/stroop/10351/baseline_run-0001.jsonl
```

## Things worth trying to break

These all behave a particular way on purpose, and each one is a decision that could be
wrong:

- **Send the same batch twice.** The second one is a `200` and writes nothing.
- **Send the same event ID with different content.** Refused with `can_retry: false`. Pig
  keeps what it had.
- **Start the same participant and session twice.** Two runs, `run-0001` and `run-0002`,
  and nothing is overwritten.
- **Use a capital letter in `session`.** The run remembers what you typed; the directory is
  lowercase.
- **Put a space or a dot in `participant_id`.** The run is refused, because that value
  becomes a directory name.
- **Post events after finalizing.** `409`, and the events aren't stored.
- **Add a parameter the task doesn't know about.** Ignored, and recorded on the run.

## What isn't here yet

The service is the four endpoints in [api.md](api.md) plus `/health`. Not built: parameter
signing, participant rosters, `max_runs`, settings returned at run start, copying datasets
offsite, and per-task allowed origins (every task currently allows any origin). See
[configuration.md](configuration.md) for what's marked *built* and what isn't.

## The other commands

| Command | What it does |
| --- | --- |
| `pig check` | Validate the configuration and print what each task will do. |
| `pig serve` | Run the web service. |
| `pig sweep` | File finished datasets; give up on runs nobody came back to. |
| `pig runs` | List runs. `--task` and `--status` narrow it. |
| `pig health` | The health report, as JSON. |
| `pig finalize RUN_ID` | Finalize a run by hand — for one that was abandoned but turned out fine. |

Any of them take `--config` if your file isn't `./local/pig.toml`.
