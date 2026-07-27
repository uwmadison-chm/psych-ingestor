# Design assumptions

The constraints that shape what gets built. When a decision is contested, this is what to
argue from.

These are currently duplicated in CLAUDE.md. This file should become the canonical copy,
with CLAUDE.md linking here.

## The documentation and API are for researchers

The people writing tasks against this service use PsychoPy and jsPsych. They are not
seasoned programmers, and the public-facing documentation must not read as though they
are. See [documentation_style.md](documentation_style.md).

This applies to the API itself, not only the prose about it. An endpoint that needs three
requests and a retry loop to use correctly is a documentation problem no amount of good
writing will fix.

## Write straighforward code

Humans need to read this code and understand what's happening. Use the clearest code that
gives a working project, with "make this code read as cleraly as possible." Prefer obviousness over cleverness, even if it results in slightly longer or slower code.

Abstractions are best used when they increase comprehensibility, especially for humans.

## Use straightforward configuration

Use toml instead json or yaml for human-edited configuration. For sizes and durations, use "human-friendly" values (either the actual `humanfriendly` library, or just some quick parse functions that do the same things). `30m` is easier to understand than a bare `1800`; `1M` is easier than `1048576`

## Silently losing data is not acceptable.

- If the server says an event is stored, that must be true — the response comes after the
  write is durable, not before.
- If a dataset is marked complete, it contains every event the client sent.
- When the safe behavior and the convenient behavior conflict, take the safe one. Nothing here is performance-sensitive-enough to be worth a lost run.
- Process reliability is a plus but not essential -- we should expect to be managed by something like `systemd` in production so if we hit an OOM error or something very weird and potentially transient, letting the server process die and be restarted is acceptable.

Failures should contain information needed to correc the problem. Refusing a request the server can't honor is always better than accepting it and hoping.

## Checking and errors are better than silent fixes

In general, doing subtle, clever things to try and correct misconfigurations is probably the wrong path to take. Then we have weird behavior to explain; also, this is tempting fate with regards to security bugs. If something has a value it shouldn't, throw a clear error rather than trying to fix it and continue.

There is one deliberate exception, and it's worth knowing why it earned its place:
**lowercasing values on their way into a filename.** It survives this rule because the
error it prevents is one the researcher can't see and can't recover from — case-variant
directory names silently merge on macOS and split on Linux, so the same dataset is whole on
one machine and halved on another. Refusing the run instead would just move a common,
harmless typo into a participant's face mid-study. The original value is preserved
everywhere except the path, so nothing is actually lost. See
[configuration.md](configuration.md).

That's the shape a future exception would have to match: silent corruption on one side, an
ordinary human typo on the other, and no information destroyed. Anything less, throw the
error.

## Link parameters can be used in filenames; be careful

We may want a data directory structure like:

`{base_dir}/{task_code}/{participant_id}/{session}_run-0001.jsonl`

... where `task_code` is supplied in the config file, `run-0001` is assigned by Pig, and `participant_id` and `session` are supplied by the participant. It's critical that before we try to create files or directories, we check that the parameters are safe. As above: raise errors rather than trying to sanitize.

The rule for what counts as safe is in [configuration.md](configuration.md), and it's
deliberately narrow: letters, digits, underscore, and a non-leading dash. Narrow enough
that `..` can't be expressed, so traversal isn't a thing we have to be clever about.

## Data is minimally-processed JSONL

One JSON object per recorded event. Other than handling duplicate events, the ingestor does not interpret event data.

Deduplication is by a client-generated event ID, which is what makes retries safe. This
matters more than it sounds: a task on a flaky connection will retry, and duplicate trials
in a data file are hard to detect after the fact. 

The format is readable by humans, appendable one line at a time, and survives a
truncated write with the loss of at most the last line.

A repeated event ID is two different situations wearing one face, and Pig tells them apart
by content rather than guessing.

When Pig stores an event it serializes it in a normalized way and records a hash of that
serialization alongside the run ID and event ID:

```python
json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
```

An arriving event whose ID is already known is compared by hash. Matching hash means this
is a retry: Pig already has the event, writes nothing, and reports it as stored. A differing
hash means two different events were given the same ID -- a broken counter, or two code
paths that both think they own ID 5. Pig keeps what it has, refuses the new one, and says
so, because that's the case where accepting silently would lose a trial and quietly corrupt
a dataset.

This is what lets both rules hold at once: retries stay free, and a task that's actually
broken finds out.

Two things this depends on:

- **The uniqueness constraint lives in the database**, on `(run_id, event_id)`, not in a
  check the application does before writing. Two retries of the same request can be in
  flight at once, and a check-then-write between them writes the line twice.
- **The hash covers what gets written**, so it's the whole stored event and not just the
  `data` field. A client that re-reads the clock when it retries will change the timestamp
  and trip the collision check; that's a client bug, but it's a predictable one and the
  docs warn about it.

### The write path, and why finalize also deduplicates

An event is durable once it's in the file and known-stored once it's in the database. Those
are two writes, so the order matters:

**The file first, then the database.** A process that dies between them leaves a line the
index doesn't know about, which is recoverable. The other order loses data outright: the
database would claim an event is stored, the client would be told so, and the file wouldn't
have it. "If the server says an event is stored, it's on disk" decides this, and it decides
it in only one direction.

So the crash window leaves duplicate lines, never missing ones. The retry that follows a
crash finds no database record, writes the line a second time, and the file now has it
twice.

**Finalize drops lines that repeat exactly**, as part of the sort it already does. No
startup recovery, no reconciliation pass -- the file heals when it's filed.

That dedup is only safe because of the write-time hash check, and this is the part worth
not losing: by the time a dataset reaches finalize, every line sharing an event ID is
guaranteed to be byte-identical, because the request that would have introduced a differing
one was refused. Deduplicating without that check would mean silently discarding one of two
genuinely different trials -- exactly the data loss this whole mechanism exists to prevent.
The two halves hold each other up.

The stored hash earns its place for the same reason. It's how Pig answers "is this the same
event I already have?" without opening the data file, which is the only alternative. Thirty-
two bytes in the index beats scanning a JSONL on every arriving event.

**Consequence worth remembering:** after a crash, the file can hold an event the database
never recorded. If finalize ever reports an event count, it has to come from the file, not
from the database, or the two will disagree in exactly the case someone is most likely to
be investigating.

### The case this doesn't catch

A dataset can still end up with two lines sharing an event ID and holding different data.
It takes a crash in the window above *and* a later reuse of that same ID with different
content: the first version reached the file but never the database, so the write-time check
had nothing to compare against and the second version was accepted.

This is rare -- it needs a broken task and a dead process, together -- and it is annoying
rather than harmful. Both events are in the file. Nothing was lost, and nothing was
silently chosen. An analyst who finds two lines with ID 5 can see exactly what happened and
decide what to do about it, which is a far better position than not knowing a trial went
missing.

Finalize must not resolve this on its own. Keep both lines, don't pick a winner, and make
the dataset's duplicate IDs visible -- in the health check and wherever the CLI describes a
run -- because an analyst who assumes IDs are unique will otherwise get a quietly wrong
answer. The run is still `complete`: it holds every event the task sent, which is what that
word promises.

The general shape here is deliberate. Every failure path Pig has bends toward *duplicated
and visible* rather than *missing and silent*. Duplicates are recoverable by someone
holding the data; a missing trial is not recoverable by anyone.

## The web service only does web service things

It accepts requests, checks them, appends events to files, and updates the database. That's
all. It runs no background threads, no schedulers, no work that outlives a request.

Everything else lives in the CLI, run on a schedule: filing finished datasets, copying them
wherever they belong, reaping abandoned runs, retrying whatever failed last time. All the
work that is slow, or depends on something outside the machine, or needs to happen at a
time nobody requested.

The reason is debuggability. A researcher who wants to know why data hasn't reached S3 can
run the command and watch it, which is not a thing you can do with a background task inside
a web process. It also means the service can be restarted at any moment without interrupting
anything, and that the two halves can fail independently: an `rclone` push that hangs takes
down nothing that a participant depends on.

The cost is that a run reaches `complete` only when the sweep next runs, and that every
deployment needs the command scheduled. Both are acceptable, because the promise Pig makes to
a task is fulfilled at `finalizing` — the events are on disk by then. `complete` is
bookkeeping, and bookkeeping can wait for a cron job.

## One service, many tasks

A single deployment can serve all tasks for all studies running at a lab or Center. Tasks are described by definitions the service loads.

Changes made for one task must not disturb others, and nothing about a task
should require a redeploy or a restart if it can be avoided.

## Two rules that keep the study-shaped door open

Pig has no concept of a study, and doesn't need one. But the reasons to want one -- a
participant roster shared across a project's tasks, an agreed list of timepoints -- are
real, and they'll come up again. Two constraints make adding a study layer later a purely
additive change rather than a migration. Both are free today.

**Task codes are unique across the whole deployment, permanently.** This is what keeps
studies out of the URL. `/task/stroop/run` is unambiguous as long as there's exactly one
`stroop`, and a study can then be something you look up *from* a task rather than something
you route through. The moment task codes are scoped per study -- two projects each with
their own `stroop` -- the study has to appear in every URL, every deployed task has to
change, and existing data has to move. Two projects that want the same name use
`sleep_stroop` and `mem_stroop`. That's the whole cost.

**Things shared between tasks are named and referenced, never inlined.** A roster belongs
in its own named object that a task points at:

```toml
[task.stroop]
roster = "sleepstudy_participants"
```

...not spelled out inside the task's own definition. Several tasks sharing a roster is then
just several tasks naming the same one, which is most of what a study would have given us.
Inlining is the version that hurts: adding studies later would mean restructuring the
configuration file and every task in it.

Storage needs no rule. Paths are already per-task, so a task that wants to live under a
project directory simply says so, and Pig doesn't have to know why.

## Tasks are static clients, hosted anywhere

A task is normally HTML/JS/CSS with no server-side component, talking to Pig over HTTPS.
Never assume it shares a domain with Pig. Every request a task makes is cross-origin, so
anything depending on same-origin cookies is out, and Pig's cross-origin headers have to be
permissive by default or ordinary tasks break. That's a deliberate position, not laziness —
see [security.md](security.md). Some tasks may be standalone apps rather than web pages.

## Traffic is light; data is large but human-manageable

A few participants at a time, peaking at a few requests per second. Hundreds or thousands
of rows per run; thousands or tens of thousands of runs per task.

This is a small amount of work for a modern machine, and the design should spend that
headroom on reliability and clarity rather than throughput. It also means directories of
files is are perfectly good store, and a researcher can read their data with standard tools
and no help from us.

## Runs have a lifecycle

A run is one participant doing one task one time — "participant 10351 doing Stroop at
baseline." Tasks signal start and end. Per-task configuration governs what happens to a
dataset that's never finalized, and whether repeat runs are allowed at all. Tasks can be
open or closed to new data.

The run is the only thing in Pig with a lifecycle; see [definitions.md](definitions.md) for
how it relates to participants, sessions, and datasets, and for the four states it moves
through.

Finalizing a run is real work — sorting the dataset, moving it, possibly copying it off the
machine — and that work can fail for reasons outside Pig. So it happens after the task has
been told its data is safe, never as a condition of saying so, and a run whose filing
failed stays visible rather than being called done.

The interesting cases are all the abnormal ones — the abandoned run, the restarted
task, the duplicate submission. They will happen in the real world, so they can't be afterthoughts.

## Simple to try and deploy, with few dependencies

This must be friendly to run on minimal hardware. No server-based database — SQLite in WAL
mode is genuinely the right tool here — and no external queueing system unless something
forces it.

The test: someone should be able to test Pig running on their laptop in an hour (or less) without becoming a sysadmin. Every added dependency is measured against that.

For deployment, we can assume some sysadmin competency. Following modern systemd conventions and being friendly in that envirionment is the general goal.
