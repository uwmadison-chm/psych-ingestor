# Configuring tasks

**Status: early. The fields marked *built* below are what the service reads today; the
rest are still proposals.** A first implementation had to pick answers for some of the open
questions on this page, and those picks are marked *provisional* where they appear. They're
easy to change — nobody has data depending on them yet.

Each task your lab runs gets an entry in your configuration. The entry answers a handful of
questions: how participants are identified, how long a run may stay open, and whether the
task is accepting data.

One Pig deployment serves all of your lab's tasks, so this file grows as you add tasks.

## Where configuration lives

Two places, split by whether a human writes it or the service does:

- **A file, in version control** — task definitions. The things you'd want to review, diff,
  and roll back: expected link parameters, the run key, allowed origins, signing, whether
  the task is open.
- **The database** — what accumulates at runtime: runs, participants discovered as they
  arrive, event bookkeeping. SQLite, in WAL mode.

The file is the authority for anything it defines. Nothing in the service edits it, and the
database never holds a second opinion about a task definition.

Consequences worth keeping in mind while building:

- Task definitions can change under a running service. *Built: the service notices the
  file changing and re-reads it, and a definition change doesn't disturb runs already in
  progress. See below.*
- The CLI's job is the database — rosters, expiring runs that have been open too long, inspecting what's stored — plus
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
open = true
max_event_size = "1M"      # optional, defaults to 1M
expires_after = "24h"      # optional, defaults to 24h
```

`data_root` and `database` are relative to the configuration file, so a checkout can move
without editing anything. A working copy keeps the whole lot — file, database, and data —
under `local/`, which is the one directory version control ignores; the commands default
to `./local/pig.toml` and `--config` or `PIG_CONFIG` overrides that. A real deployment
puts the file wherever its configuration belongs and points `data_root` at real storage.

Under the data root, Pig keeps two directories, and every run is a directory named for
its run ID under one of them:

```
data/
  in_progress/{task_code}/{run_id}/    collecting, or closed and waiting for the sweep
  done/{task_code}/{run_id}/           finished; nothing here ever changes again
```

Nothing in the task entry says where its data lands, because nothing about that is a
choice: it's `done/{task_code}/`, with one directory per run holding `events.jsonl` and a
`manifest.json` describing the run. A readable layout — one directory per participant,
files named for session and run number — is built later by `pig organize`, from the
manifests, on whatever machine the data ends up on. See [definitions.md](definitions.md)
for what a run directory holds and issue #9 for `pig organize`.

Both directories have to be on one filesystem. The sweep moves a finished run from one to
the other with a rename, which is what guarantees a run is never half-there in `done/`,
and a rename only works that way within a filesystem. See [deployment.md](deployment.md).

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

Runs already in progress are unaffected by a definition change: the parameters a run was
started with, and which of them made up its run key, are recorded on the run itself. The
sweep finishes a run from that record alone, so a task whose entry has been deleted still
gets its runs finished.

`pig check` validates the file and prints what each task is set up to do. Worth running
after an edit, since the service won't complain to you directly — it just keeps serving the
last good version and reports the problem on `/health`.

## What a task entry covers

### Identifying the run

Which parameters Pig expects from the participant's link, and which of them make up the
**run key** — the values that decide whether two runs are repeats of the same thing. A
study that sees each participant once might use only `participant_id`; a study with
repeated visits needs `session` as well, so `["participant_id", "session"]`.

The two lists are separate on purpose, even though most tasks will set them the same. A
task can require a parameter that doesn't identify the run — a `redcap_record` alongside
`participant_id`, or a `condition` the task needs handed back to it. If every required
parameter were part of the key, the same participant arriving with a different value for
one of them would start over at `run-0001` — the same person appearing twice, as two first
runs. Keeping the lists apart means "required" and "identifies the run" stay different
claims, and only the second one is the strong one.

Run key values are compared exactly, capital letters included: `Baseline` and `baseline`
are two sessions. Pig doesn't edit a participant's identity to make two spellings match,
so if a link template is producing both, that's the thing to fix. See
[definitions.md](definitions.md).

Parameters Pig doesn't know about are ignored. They don't identify the run, they aren't
required, and their presence is never an error — a link carrying a `utm_source` or a
leftover `debug=1` still starts a run normally.

Pig records them anyway, on the run and in its manifest, because they cost almost nothing
to keep and occasionally explain something months later. They're never used to identify
or route anything.

**Open question:** do parameter values get checked for shape beyond the [safe
value](#safe-values) rule — digits only, a required prefix — or is any safe value accepted?
*Provisional: any safe value is accepted. There's no way to ask for more.*

### Where data goes

Not a setting. Every run is stored at `done/{task_code}/{run_id}/` under the data root,
and there's nothing to configure about that. Earlier versions of Pig had a `path`
pattern here; a configuration that still has one is refused with a message saying so.

The readable tree that pattern used to describe — `{participant_id}/{session}_run-0001.jsonl`
and the like — becomes `pig organize`'s business, on the machine where the data is
analyzed rather than the one collecting it. The open questions that came with the pattern
(must it include the run number, which patterns can collide) go with it; they're recorded
in issue #9.

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

### How long a run may stay open

`expires_after` is how long a run may stay open, counted from when it started. Once that
much time has passed, the next sweep marks the run `expired` and files whatever arrived.
The default is 24 hours, which is plenty for a task a participant sits down and finishes.

The limit counts from the start, not from the last event, so nothing can hold a run open
forever by continuing to send. A task whose runs have no natural end — a game people play
for as long as they like — sets a long limit, handles the expiry by starting a new run, or
both. [api.md](api.md) says what the task sees when a run expires.

Expiring never deletes anything. The run is marked, and the same sweep finishes it like
any other closed run. A run can't be reopened afterwards, from the API or the CLI; if the
participant is still working, the task starts a new run and gets the next run number.

Only `in_progress` runs expire. A run sitting in `finalizing` is waiting on Pig, not on
the participant — see [definitions.md](definitions.md).

Expiring is done by the CLI, on a schedule, not by the service. See
[deployment.md](deployment.md).

### Finishing a run

What Pig does with a run once it has closed, whether the task finalized it or it expired:
write a manifest into the run's directory, and move the directory from `in_progress/` to
`done/`. The run is `finalizing` while it waits for this and `complete` afterwards (or
`expired` throughout; see [definitions.md](definitions.md)).

*Built. `pig sweep` does it.* Nothing in it depends on anything outside the machine, so
the ways it can fail are the ordinary ones — a full disk, a permissions mistake — and the
sweep reports each run it couldn't finish and tries again next time. There's no retry
beyond that yet, and a run that can't be finished stays where it is, visible, rather than
quietly becoming `done`.

The sweep counts those runs in two groups, because they don't ask the same thing of you.
A run it *failed* to finish hit one of those ordinary problems; fix the disk and the next
sweep finishes it, along with every other run the same problem stopped. A run it *refused*
to finish is one whose files don't match what Pig recorded — the run is in two places at
once, or its events file is gone while the database says events were stored. Pig won't
write over that or make up an empty directory to replace it, so it leaves the run alone
and names it. Nothing but a person looking at that run will clear it.

None of this happens in the web service. Finalizing a run marks it `finalizing` and returns
— that's the whole of the service's involvement. A scheduled CLI command does the rest:
finds closed runs, writes each one's manifest, moves its directory, and marks the run
`done`.

Copying finished runs somewhere else — an `rclone` push to S3 or similar — is not built,
and when it is, it won't be Pig tracking whether the copy happened: `done/` is a directory
of immutable, hashable run directories, which is what a copy tool wants. See issue #2.

Every run waits for the next sweep before it reaches `complete`. That's the cost of keeping
the service simple, and it's affordable because the durability promise lands at
`finalizing`, not at `complete` — nobody is waiting on the sweep except whoever wants to
read the finished run. See [deployment.md](deployment.md) for scheduling it.

### Open or closed

Whether the task currently accepts new data. Closing a task is how data collection ends
without taking the service down.

Closing refuses new runs only. Runs already in progress keep sending events and can
finalize normally — they belong to participants who are mid-task, usually not the ones
whose data collection is ending — and they expire on their own schedule like any other.

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
it's the directory the task's runs live in under `done/`.

Task codes live under `/task/` rather than at the root of the URL space, so they can never
collide with the service's own routes. A task code of `health` is just
`/task/health/run`, and `GET /health` is unaffected. Nothing needs a list of reserved
names.

A task code must be a safe value, below, and lowercase. It becomes a directory name on
every machine the data reaches, and macOS and Linux disagree about whether `Stroop/` and
`stroop/` are the same directory; requiring one spelling sidesteps that. Link parameter
values aren't held to this, because they don't become directory names on the collecting
machine — see below.

Task codes are unique across the whole deployment, not per project — there is exactly one
`stroop`. Two projects that both want that name use `sleep_stroop` and `mem_stroop`. This
is what keeps a future study layer out of the URL space; see
[design_assumptions.md](design_assumptions.md).

## Safe values

Some values end up as directory and file names: the task code now, and the link parameters
later, when `pig organize` builds a readable tree from them. Those have to be restricted,
because a filesystem will accept things that later turn out to be a problem — and the
check has to happen when the run starts, on the collecting machine, because by the time
`pig organize` runs there's nobody to refuse the value to.

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

### Case is kept

Values may contain either case, and Pig keeps exactly what arrived. `?session=Baseline`
and `?session=baseline` are two different sessions, and a participant who arrives as
`PPT-1003` once and `ppt-1003` once is, as far as Pig can tell, two participants. Pig
never changes a value to make two spellings match — see
[design_assumptions.md](design_assumptions.md) on why refusing beats fixing.

Case variation in links is common, though — links get retyped, copied between REDCap
instances, and edited by hand — so it's worth knowing about early. A health check that
reports parameter values differing only in case is planned; see
[deployment.md](deployment.md).

One consequence for the readable tree `pig organize` builds later: two spellings of a
value are one directory on macOS and two on Linux. That's `pig organize`'s problem to
solve, on the machine where the tree is built; see issue #9.

## Validating configuration

*Built.* `pig check` reads the file, reports every problem it can describe, and prints each
task: whether it's open, what parameters it expects, which of them make up the run key,
and how long its runs may stay open. It exits non-zero on a bad file, so a deployment can
gate a restart on it.
