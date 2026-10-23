# Organizing finished runs

Pig stores each finished run in a directory named for its run ID: `done/stroop/9f3c1a7e-…/`. That's the record, and it's deliberately not something a person can browse. `pig organize` copies runs into a tree named for what they are:

```
organized/stroop/10351/baseline/run-0001/
    manifest.json
    events.jsonl
    media/prompt3_audio/000001.part     # if the task sent media
                        000002.part
```

Run it on the machine where the data ends up, not the web server. It never reads `pig.toml` or Pig's database, so neither has to be there. Everything it needs is in each run's `manifest.json`.

## Trying it on a copy

Point it at a directory of finished runs and say where the tree should go:

```
pig organize local/data/done organized
```

Each task's runs are placed by the default pattern: the task code, then the run-key values in the order the task lists them, then the run number. A task with `run_key = ["participant_id", "session"]` gets `stroop/10351/baseline/run-0001/`.

Add `--dry-run` to see where each run would go without copying anything.

## On the secure server: pigsty.toml

Where the data ends up gets its own configuration file, `pigsty.toml`, separate from the web server's `pig.toml`. Keep the two on different machines: `pigsty.toml` is for the side that will eventually hold private keys, and nothing about it should be on a public-facing server.

```toml
# pigsty.toml
kind = "secure"

store = "/secure/pig/store"          # finished run directories, as copied from done/
organized = "/secure/pig/organized"  # where pig organize builds the tree

[task.stroop]
pattern = "lucida/{task_code}/{participant_id}/{session}/run-{run_number}"
```

`kind = "secure"` is required. It's how Pig tells the two files apart: the web server's commands refuse a file that says it, and `pig organize` refuses a file that doesn't. `pig check --config pigsty.toml` checks the file. Paths are relative to the file, as they are in `pig.toml`.

`pigsty.example.toml` in the repository is a starting point: copy it to `local/pigsty.toml`, where its paths point at your local Pig's `done/` directory.

With that in place, `pig organize` needs no arguments. It reads `local/pigsty.toml`, or the file named by `--config` or `PIGSTY_CONFIG`. A task with no entry uses the default pattern.

## Patterns

A pattern is a path inside the organized directory, with placeholders in braces:

- `{task_code}` and `{run_number}`, which every run has. The run number is written with four digits, `0001`.
- Any parameter the task lists under `parameters` in `pig.toml`, like `{participant_id}`.

Every pattern has to include `{run_number}`. Without it, a participant's second run would land on top of the first.

Only parameters the task declares can be used. Extra parameters that arrive in a link, like `utm_source`, are recorded but never checked, so they can't become part of a path. To use one, add it to the task's `parameters`.

Every part of the finished path, the fixed parts of the pattern included, has to be a [safe value](configuration.md#safe-values): letters, digits, underscore, and dash. So `lucida/` is fine, and `lucida.v2/` isn't.

A placeholder is a name and nothing else. `{session.upper}`, `{run_number:03}`, and any other brace are refused.

## Running it again

Running `pig organize` again over the same tree is safe, and it's how new runs get added. A run that's already in place is left alone. Nothing in the store is ever changed or deleted, and nothing already in the tree is overwritten.

The tree keeps a list of the runs placed in it, one file per task, in `.placed/` at its top: `.placed/stroop.jsonl`. A second `pig organize` reads those lists and the names of the runs in the store, and opens only the runs it hasn't placed yet. A store can hold tens of thousands of runs on a network share, where opening each one takes a while, so this is what keeps a scheduled organize quick.

A run is added to its list only once it's on disk in the tree, so a list never names a run the tree doesn't have, even after a crash.

Only one `pig organize` works on a tree at a time. Another one started meanwhile, by a scheduled job that overlaps the last, says so and stops without placing anything.

The lists are only ever added to as runs are placed, so they're trusted as they stand. A run deleted from the tree by hand isn't put back, and a run moved or edited there isn't noticed. `pig organize --full` checks every run in the store against the tree, as if the lists weren't there: it places any run that's missing, and writes the lists again from what it finds. It's also what to run if `pig organize` says a list can't be read.

To rebuild a tree from scratch, for a new pattern say, delete it and run `pig organize` again. The lists go with it.

## When a run isn't placed

`pig organize` reports each run it couldn't place, carries on with the rest, and exits with an error at the end, so a scheduled job notices. A run isn't placed when:

- **Two runs would land in the same place.** Neither is placed. This happens when a pattern leaves out something that tells runs apart, or when two values differ only in capitals: `PPT-1003` and `ppt-1003` are two participants to Pig, but one directory on a Mac.
- **Something else is already at its path** in the tree.
- **The pattern names something the run doesn't have**, or a value isn't safe in a path.
- **The run has no `manifest.json`.** It isn't finished. The store should only hold runs from the web server's `done/`, never `in_progress/`.

## Checking files against the manifest

Every file is checked against the size and hash in the run's manifest as it's copied. A file that doesn't match, a file the manifest lists that's missing, and a file the manifest doesn't list are each reported as a warning. The run is still placed, with files that don't match copied as they are, and `pig organize` exits with an error so someone looks.

The one exception is a `.state/` directory inside a run directory. That's where commands on the secure side will record what they've done to a run, such as purging it from the web server. It's never in the manifest, never checked, and never copied.

## What organize doesn't do

It copies data exactly as Pig stored it. It doesn't join media parts into playable files, add file extensions, or drop repeated lines from `events.jsonl`. Those depend on what a task sends, so they belong to a script that understands that task's data.

Expired runs are placed like any other. Whether a run was finalized by its task or expired is in its manifest, as `disposition`.
