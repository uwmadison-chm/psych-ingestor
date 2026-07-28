# Configuring tasks

**Status: early. The fields marked *built* below are what the service reads today; the
rest are still proposals.** A first implementation had to pick answers for some of the open
questions on this page, and those picks are marked *provisional* where they appear. They're
easy to change — nobody has data depending on them yet.

Each task your lab runs gets an entry in your configuration. The entry answers a handful of
questions: how participants are identified, where their data goes, and when the task stops
accepting data.

One Pig deployment serves all of your lab's tasks, so this file grows as you add tasks.

## Where configuration lives

Two places, split by whether a human writes it or the service does:

- **A file, in version control** — task definitions. The things you'd want to review, diff,
  and roll back: expected link parameters, storage paths and naming, allowed origins,
  signing, whether the task is open.
- **The database** — what accumulates at runtime: runs, participants discovered as they
  arrive, event bookkeeping. SQLite, in WAL mode.

The file is the authority for anything it defines. Nothing in the service edits it, and the
database never holds a second opinion about a task definition.

Consequences worth keeping in mind while building:

- Task definitions can change under a running service. *Built: the service notices the
  file changing and re-reads it, and a definition change doesn't disturb runs already in
  progress. See below.*
- The CLI's job is the database — rosters, reaping abandoned runs, inspecting what's stored — plus
  validating the file. It is not an editor for task definitions; that's what a text editor
  and a pull request are for.
- A task that appears in the database but no longer in the file is a real situation (someone
  deleted an entry). Its existing data must remain readable.

## What a task entry looks like today

The file the service actually reads. Everything here is built; everything in the next
section that isn't mentioned here isn't.

```toml
# local/pig.toml
data_root = "./data"
database = "./pig.db"

[task.stroop]
parameters = ["participant_id", "session"]
run_key = ["participant_id", "session"]
path = "{participant_id}/{session}_{run_number}.jsonl"
open = true
max_event_size = "1M"      # optional, defaults to 1M
abandon_after = "24h"      # optional, defaults to 24h
```

`data_root` and `database` are relative to the configuration file, so a checkout can move
without editing anything. A working copy keeps the whole lot — file, database, and data —
under `local/`, which is the one directory version control ignores; the commands default
to `./local/pig.toml` and `--config` or `PIG_CONFIG` overrides that. A real deployment
puts the file wherever its configuration belongs and points `data_root` at real storage.

Under the data root, Pig keeps three directories: `in_progress/` for runs still collecting, `complete/` for datasets of runs that finalized, and
`abandoned/` for the rest. A task's `path` places its file within `complete/{task_code}/`
or `abandoned/{task_code}/`.

`{run_number}` renders as `run-0001`.

**The service re-reads this file whenever it changes.** Edit a definition and the next
request uses it — no restart, no signal. It checks the file's modification time on each
request and only re-reads when that changed, so the cost is one `stat` per request.

Two things follow, and both are deliberate:

- **A file that won't load doesn't take the service down.** The last configuration that
  did load keeps serving, `GET /health` reports the problem and answers `503`, and the
  next good save picks up from there. A typo in a text editor must not stop data
  collection for participants who are mid-task — that trade is the whole reason this
  isn't "refuse everything until it parses."
- **`data_root` and `database` are not safe to change under a running service.** They're
  re-read like everything else, so the change takes effect immediately, and datasets for
  runs already in progress are under the *old* root where nothing will look for them.
  Change those two while the service is stopped.

Runs already in progress are unaffected by a definition change: a run's storage path is
computed when it's filed, and the parameters it was started with are on the run.

`pig check` validates the file and prints where each task's data will land. Worth running
after an edit, since the service won't complain to you directly — it just keeps serving the
last good version and reports the problem on `/health`.

## What a task entry covers

### Identifying the run

Which parameters Pig expects from the participant's link, and which of them make up the
**run key** — the values that decide whether two runs are repeats of the same thing. A
study that sees each participant once might use only `participant_id`; a study with
repeated visits needs `session` as well, so `["participant_id", "session"]`.

Parameters Pig doesn't know about are ignored. They don't identify the run, they aren't
required, and their presence is never an error — a link carrying a `utm_source` or a
leftover `debug=1` still starts a run normally.

Pig records them anyway, on the run, because they cost almost nothing to keep and
occasionally explain something months later. They're never used to identify or route
anything, and they never appear in a path.

**Open question:** do parameter values get checked for shape beyond the [safe
value](#safe-values) rule — digits only, a required prefix — or is any safe value accepted?
*Provisional: any safe value is accepted. There's no way to ask for more.*

### Where data goes and what it's called

A directory for the task, and a naming pattern for the files within it built from the link
parameters and the run number — something like `{participant_id}/{session}_run-{run_number}.jsonl`.

**Open questions**
- What's the full set of values available to the pattern? Link parameters, a timestamp, the
  run ID, the run number? *Provisional: link parameters and `{run_number}`, nothing else.
  A pattern naming anything the task doesn't have is refused by `pig check`.*
- Must the pattern include the run number? Leaving it out means the second run of a key
  would overwrite the first, which the reliability rule forbids — so either Pig requires it
  or it refuses patterns that can collide. *Provisional: required, and its absence is a
  configuration error. Requiring it is the check that's obviously right; refusing only the
  patterns that can collide needs an argument about which those are.*
- Are paths relative to a configured data root, with escaping from it refused? They should
  be. *Built: they are. An absolute path or one containing `..` is a configuration error,
  and the [safe value](#safe-values) rule keeps `..` out of the parameters themselves.*

### Repeat runs

A participant who restarts partway through, or a task deliberately run twice, produces a
second run under the same run key. Run numbers keep the two datasets apart, so nothing is
overwritten and nothing needs merging.

Today a task can't refuse a repeat run. A participant who starts a task fifty times
produces fifty runs and Pig keeps all of them.

A `max_runs` setting is planned, capping how many runs a task will accept for one run key.
When it arrives it needs an answer for what the run past the limit gets told — refusing a
participant mid-study is a real event, and the task has to be able to say something useful
to them.

### Settings sent back to the task

*Not built.* Optional values Pig returns when a run starts: condition assignment, a stimulus set, a
block order. Lets you change a task's behavior without redeploying it.

**Open question:** is this a static blob per task, or can it vary per participant — a
condition assignment stored on the participant record? The latter is much more useful and
much more to build.

### Abandoned runs

How long Pig waits before considering an unfinished run dead, and what happens then.

Whatever happens, it can't be "delete the data." Marking the run abandoned and leaving the
file in place is the safe default; a run that turned out fine can be finalized by hand.

Only `in_progress` runs are ever abandoned. A run sitting in `finalizing` is waiting on Pig,
not on the participant — see [definitions.md](definitions.md).

The reaping is done by the CLI, on a schedule, not by the service. See
[deployment.md](deployment.md).

### Filing a completed dataset

What Pig does with a dataset once the task finalizes the run: sort it, move it to completed
storage, and possibly copy it somewhere else — an `rclone` push to S3 or similar. The run
is `finalizing` while this happens and `complete` when it's done.

This is the one place Pig depends on something outside itself, so it's the one place that
can be slow or broken for reasons no researcher can do anything about. It has to be
retryable, and a run that can't be filed has to stay visible rather than quietly becoming
`complete`.

*Sorting, moving, and the `complete` / `abandoned` split are built; copying anywhere else
is not. `pig sweep` does the filing.*

The copy-elsewhere step is configured **per task**. Different studies have different
archives, different retention rules, and different people paying for storage, so this can't
be a single deployment-wide setting.

For now there is no retry: a run whose filing fails stays `finalizing` until someone looks.
Retry policy is deferred.

None of this happens in the web service. Finalizing a run marks it `finalizing` and returns
— that's the whole of the service's involvement. A scheduled CLI command does the rest:
finds runs waiting to be filed, sorts each dataset, moves it to completed storage, copies it
wherever the task says, sets `filed_at`, and marks the run `complete`.

Abandoned runs get filed the same way, to a separate destination from complete ones, so
that nothing reading completed data ever picks up a partial dataset.

This means every run waits for the next sweep before it reaches `complete`, even on a
deployment that copies nothing anywhere. That's the cost of keeping the service simple, and
it's affordable because the durability promise lands at `finalizing`, not at `complete` —
nobody is waiting on the sweep except whoever wants to read the finished file. See
[deployment.md](deployment.md) for scheduling it.

### Open or closed

Whether the task currently accepts new data. Closing a task is how data collection ends
without taking the service down.

**Open question:** does closing a task also refuse new events for runs already in
progress? Refusing them loses data from participants who are mid-task, which argues for
letting existing runs finish. *Provisional: closing refuses new runs only. Runs already in
progress keep sending events and can finalize normally.*

### Parameter signing

*Not built.* Off by default and set per task. When on, Pig verifies that a participant's link parameters
were signed by you, so participants can't edit their own ID or condition.

It complicates testing considerably — you can't just type a URL by hand — so it needs an
easy way to generate a valid signed link, presumably a CLI command.

**Open questions**
- What signing scheme, and where does the key live?
- Do signed links expire?

### Allowed origins

*Not built — every task allows any origin.* Which sites may make requests to Pig for this
task. Permissive by default; see
[security.md](security.md) for why.

## The task code

The short name for the task — `stroop`, `balloons`, `dd_game`. It's the key of the task's
configuration entry, it appears in every URL the task calls (`POST /task/stroop/run`), and
it's normally the top-level directory the task's data lives in.

Task codes live under `/task/` rather than at the root of the URL space, so they can never
collide with the service's own routes. A task code of `health` is just
`/task/health/run`, and `GET /health` is unaffected. Nothing needs a list of reserved
names.

A task code must be a safe value, below, and lowercase. See [safe values](#safe-values) for
why the lowercase part is stricter than the general rule.

Task codes are unique across the whole deployment, not per project — there is exactly one
`stroop`. Two projects that both want that name use `sleep_stroop` and `mem_stroop`. This
is what keeps a future study layer out of the URL space; see
[design_assumptions.md](design_assumptions.md).

## Safe values

Some values end up as directory and file names: the task code, and any link parameter used
in a storage pattern. Those have to be restricted, because a filesystem will accept things
that later turn out to be a problem.

A safe value contains only:

- letters `A`–`Z` and `a`–`z`
- digits `0`–`9`
- underscore `_`
- dash `-`, but not as the first character

and is between 1 and 64 characters long.

Nothing else. No dots, no spaces, no other punctuation, no characters outside ASCII. Pig
checks values against this rule when a run starts and refuses the run if one doesn't match
— it never edits a value to make it fit. A participant who arrives with an unusable ID is a
problem someone needs to know about, not one to paper over.

Each restriction is carrying weight:

- **No dots** means `.` and `..` can't appear at all, so directory traversal stops being
  something to defend against. It's excluded by construction rather than by a check
  somebody might forget.
- **No leading dash** keeps a value from being read as an option by whatever command
  touches the file later. `rclone copy -rf/...` is a bad afternoon, and Pig is designed
  expecting an `rclone`-shaped thing to run over this data.
- **No characters outside ASCII** is about normalization, not fear of other alphabets. `é`
  has two valid encodings; macOS rewrites filenames to one of them on write and Linux
  doesn't, so the same participant ID can produce two different files on two machines, and
  a lookup that works on the researcher's laptop fails on the server.
- **A length cap** keeps a long value from blowing past the filesystem's limit on a path
  component deep inside a write, where the error is confusing and the run is already
  underway.
- **At least one character** because an empty value silently collapses a path component,
  turning `10351//run-0001.jsonl` into something in the wrong directory. `?participant_id=`
  with nothing after it is easy to generate by accident.

Digits are fine at the start; participant IDs are frequently all digits.

### Case is normalized on the way to disk

Values may contain either case, but **Pig lowercases them when it builds a path**.
`?session=Baseline`, `?session=baseline`, and `?session=BaseLine` all write to
`baseline/`.

This is the one place Pig changes a value instead of refusing it, and it's deliberate. Case
variation in link parameters is common — links get retyped, copied between REDCap
instances, and edited by hand — and the failure it causes is bad in a specific way: two
spellings of `baseline` are one directory on macOS and two on Linux, so a dataset that
looks complete on the server splits in half when someone copies it to a laptop to analyze.
That's data loss that presents as a naming quirk, and it's worth a small violation of "no
silent fixes" to make impossible.

**The original value is kept.** Lowercasing applies to the path and nothing else. What the
participant's link actually said is stored in the database exactly as it arrived, and
that's what the CLI and the API report. A study that uses `PPT-1003` everywhere else
doesn't lose that; it just gets `ppt-1003/` as a directory name.

Two consequences worth being explicit about:

- `PPT-1003` and `ppt-1003` become the same participant as far as storage is concerned.
  That's the intended behavior, and the reason it's safe is that the alternative — treating
  them as two participants — silently breaks on half the machines that will touch the data.
- Because a value can differ from the directory it's stored in, Pig can notice when one task
  has received IDs that differ only by case. That's a strong sign of a broken link
  template, and it's the sort of thing the health check should surface.

Lowercasing is unambiguous here only because [safe values](#safe-values) are ASCII. Case
conversion outside ASCII is genuinely treacherous — `ß` uppercases to `SS`, and Turkish `İ`
lowercases to a character that isn't `i` — so the two rules depend on each other.

Task codes are required to be lowercase outright, since we control those and there's no
reason to accept a spelling we're only going to convert.

## Validating configuration

*Built.* `pig check` reads the file, reports every problem it can describe, and prints each
task: whether it's open, what parameters it expects, and the path a dataset will land at.
It exits non-zero on a bad file, so a deployment can gate a restart on it.
