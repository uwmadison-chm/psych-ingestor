# Organizing finished runs

Pig stores each finished run in a directory named for its run ID: `done/stroop/9f3c1a7e-…/`. That's the record, and it's deliberately not something a person can browse. On the machine where the data ends up, two commands turn those into a tree named for what each run is:

- `pigsty admit` takes runs as they arrive from the web server and moves each one into place once it's whole.
- `pigsty organize` copies each admitted run to a path built from its parameters:

```
/secure/mystudy/intermediate_data/pig/stroop/baseline/10351_0001/
    pig-run.json
    events.jsonl
    media/prompt3_audio/000001.part     # if the task sent media
                        000002.part
```

Neither runs on the web server. Neither reads `pig.toml` or Pig's database, so neither has to be there. Everything they need is in each run.

## pigsty.toml

Where the data ends up gets its own configuration file, `pigsty.toml`, separate from the web server's `pig.toml`. Keep the two on different machines: `pigsty.toml` is for the side that will eventually hold private keys, and nothing about it should be on a public-facing server.

Each task gets an entry saying where its runs are kept and where its tree goes:

```toml
# pigsty.toml
[task.stroop]
datastore_base = "/some/mount/mystudy/primary_data/pig/stroop"
organized = "/some/mount/mystudy/intermediate_data/pig/stroop/{session}/{participant_id}_{run_number}"
```

A task with no entry is never admitted or organized. Two tasks can't share a `datastore_base`, and neither can share where they're organized; see [Patterns](#patterns).

The two sides have their own commands as well as their own files. `pig` runs on the web server and reads `pig.toml`; `pigsty` runs where the data ends up and reads `pigsty.toml`, never `pig.toml` or the database. `pigsty check` checks the file, the way `pig check` checks `pig.toml`. Paths are relative to the file, as they are in `pig.toml`. Every `pigsty` command reads `local/pigsty.toml`, or the file named by `--config` or `PIGSTY_CONFIG`.

## Trying it on a laptop

`pigsty.example.toml` in the repository is a starting point. Copy it to `local/pigsty.toml`, where its paths are under `local/`. Then copy your local Pig's finished stroop runs to where the bucket's copy would put them, and admit and organize them:

```
mkdir -p local/stroop-data/incoming
cp -R local/data/done/stroop/* local/stroop-data/incoming/
pigsty admit
pigsty organize
```

The tree is in `local/organized/stroop/`. Add `--dry-run` to `pigsty organize` to see where each run would go without copying anything.

## Admitting downloaded runs

On a real secure server, runs arrive by a copy Pig doesn't run: `rclone sync` from the bucket the web server uploads to, or something like it. That copy isn't all-at-once. A run can be half there for a while, and its manifest can arrive before the files it lists. `pigsty admit` is what stands between that and everything else: it moves a run into place only once it's whole.

Inside each task's `datastore_base`:

- `incoming/` is where the copy puts the task's runs, one directory per run ID, as they are in the bucket. Point `rclone sync` at it.
- `runs/` is where `pigsty admit` puts each run once every file its manifest lists is there, with the right size and hash, and nothing else is.

A run in `runs/` holds its files exactly as the web server stored them, and `pig-run.json`, which says what the run is: task, run number, parameters, media. It's the manifest without the file hashes. The manifest itself stays in `incoming/`, because it describes the files as they traveled.

Running `pigsty admit` again is safe, and it's how new runs get admitted. A run already in `runs/` is left alone. A run still downloading is left for next time and isn't an error. A run that's all there but doesn't match its manifest is reported, nothing of it is admitted, and `pigsty admit` exits with an error so a scheduled job notices. Nothing in `incoming/` is ever changed: it's a copy of the bucket, and runs leave it when they leave the bucket.

Only one `pigsty admit` works on a task at a time.

## Patterns

A task's `organized` is a directory, then a pattern. In the example above, the directory is `/some/mount/mystudy/intermediate_data/pig/stroop` and the pattern is `{session}/{participant_id}_{run_number}`.

The directory is everything up to the last `/` before the first placeholder. It's the task's own: no other task can be organized into it, or into a directory inside it, or one around it. That's where `pigsty organize` keeps its record of what it has placed, and it's what makes two tasks' runs unable to land on each other.

The pattern is filled in from each run. Its placeholders, in braces, are:

- `{task_code}` and `{run_number}`, which every run has. The run number is written with four digits, `0001`.
- Any parameter the task lists under `parameters` in `pig.toml`, like `{participant_id}`.

Every pattern has to include `{run_number}`. Without it, a participant's second run would land on top of the first.

Only parameters the task declares can be used. Extra parameters that arrive in a link, like `utm_source`, are recorded but never checked, so they can't become part of a path. To use one, add it to the task's `parameters`.

Every part of the path the pattern makes, its fixed parts included, has to be a [safe value](configuration.md#safe-values): letters, digits, underscore, and dash. So `run-{run_number}` is fine, and `v1.2/{run_number}` isn't. The task's directory can be anything.

A placeholder is a name and nothing else. `{session.upper}`, `{run_number:03}`, and any other brace are refused.

## Running it again

Running `pigsty organize` again is safe, and it's how new runs get added. A run that's already in place is left alone. Nothing in `runs/` is ever changed or deleted, and nothing already in the tree is overwritten.

Each task's directory has a record, `.placed.jsonl`, with a line for each run placed in it. A run in the record isn't read again, which is what keeps a second organize quick on a network share with tens of thousands of runs. A run in the tree but not the record, after a crash say, is recognized by the `pig-run.json` in its directory, and recorded.

Because the record is trusted, a run removed from the tree by hand isn't put back. `pigsty organize --full` reads every run, checks each against the tree, puts back what's missing, and writes the record again. If the record is ever damaged, `pigsty organize` stops and says to run that.

To rebuild a tree from scratch, for a new pattern say, delete the task's directory, record included, and run `pigsty organize` again.

Only one `pigsty organize` works on a task at a time.

## When a run isn't placed

`pigsty organize` reports each run it couldn't place, carries on with the rest, and exits with an error at the end, so a scheduled job notices. A run isn't placed when:

- **Two runs would land in the same place.** Neither is placed, unless one is already in the tree, in which case it stays and the new one isn't placed. This happens when a pattern leaves out something that tells runs apart, or when two values differ only in capitals: `PPT-1003` and `ppt-1003` are two participants to Pig, but one directory on a Mac.
- **Something else is already at its path** in the tree.
- **The pattern names something the run doesn't have**, or a value isn't safe in a path.
- **The run has no `pig-run.json`.** Everything in `runs/` should have been put there by `pigsty admit`.

## What organize doesn't do

It copies data exactly as Pig stored it. It doesn't join media parts into playable files, add file extensions, or drop repeated lines from `events.jsonl`. Those depend on what a task sends, so they belong to a script that understands that task's data.

It doesn't check files against hashes, either. `pigsty admit` did that, and nothing is in `runs/` unless it matched.

Expired runs are placed like any other. Whether a run was finalized by its task or expired is in its `pig-run.json`, as `disposition`.
