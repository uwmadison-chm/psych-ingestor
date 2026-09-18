# Definitions

The words Pig uses, and what each one means here. Where a term is used loosely elsewhere in
the docs, this page wins.

Pig borrows its vocabulary from BIDS, because most people reading this have already met it
there: a **participant** comes in for a **session**, does a **task**, possibly more than
once — each time is a **run** — and a run records **events**.

## Task

One thing a participant does: a Stroop task, a delay-discounting game, whatever.

A task has a definition in configuration, a directory its runs are stored under, and a
state — open to new data or closed.

A single Pig deployment runs many tasks, for many projects, across a lab.

## Participant

A person in a study, identified by whatever your task's configuration says identifies them
— usually something like `participant_id=10351`.

## Session

A point in the study when a participant does things: `baseline`, `3mo`, `followup`. This is
the psychology meaning of the word, and the same one BIDS uses. A study that sees each
participant once may not need sessions at all.

A session is a value that arrives in the participant's link, like
`?participant_id=10351&session=baseline`. It is not something Pig creates or tracks a
lifecycle for — that's a run.

## Run

One participant doing one task, one time: "participant 10351 played the Balloons game."

A run is what your task starts when a participant arrives, and what it finalizes when they
finish — or what Pig closes for it, once it's been open as long as the task allows. It's
identified by a **run ID** that Pig generates and returns — not by the link parameters,
though the parameters are what determine which run is meant.

A run is the only thing in Pig with a lifecycle. Everything else is either configuration or
stored data.

## Run ID

The identifier Pig generates when a run starts, and the only thing a task needs in order to
send events to it. A random UUID, stored as text.

Two properties matter, and both come from the same fact — a participant can read their own
run ID out of their browser's network tab:

- **Unguessable.** If run IDs were sequential, a bored participant could count to someone
  else's run and post events into it. Generate them randomly (`uuid4`), never from a
  timestamp or a counter (`uuid1` leaks both, and encodes a MAC address besides).
- **Unique across the whole deployment**, not just within a task, enforced by the database.
  That's what makes it safe to treat a run ID as a complete answer to "which run is this,"
  and it's why a run ID used with the wrong task code is a `404` rather than a lookup that
  quietly succeeds.

The run ID is also the name of the run's directory on disk. It never changes, and it's
the same on every machine a copy of the data reaches, which is what makes copying data
offsite a matter of copying directories. See [Dataset](#dataset).

## Run key

The parameters whose values decide whether two runs are repeats of the same thing. For
example, `["participant_id", "session"]` means Pig tracks participants and sessions
together, so participant 10351 arriving at `baseline` twice produces two runs of the same
key.

Which parameters make up the key is set per task. See
[configuration.md](configuration.md).

**Values are compared exactly, case included.** `?session=Baseline` and
`?session=baseline` are two different sessions, the same way `10351` and `10352` are two
different participants. Pig never edits a value on its way to becoming part of a
participant's identity: a link that arrives with a different spelling is a different link,
and the place to fix that is the link template, not the data. (An earlier version of Pig
lowercased values here, for a filename reason that no longer applies — parameter values
don't become directory names any more. The behaviour went with the reason.)

In the database, the run key is stored as a hash of its values rather than the values
themselves. It's only ever compared, so it doesn't need to be readable, and a column that
isn't readable is one fewer place identifiers accumulate. This is tidiness, not
protection: the values are on the run, one join away.

## Run number

Which time this is, for a given run key: `run-0001` is the first time participant 10351
started the Balloons game at `baseline`, `run-0002` the second.

Run numbers count up from 1 and are never reused. Starting a task again with the same link
always produces a new run with a new number, so Pig never has to overwrite or merge
anything.

This is the number `pig organize` will use in filenames, zero-padded to four digits so
that a directory listing sorts the way a person expects. Four digits caps a run key at
9999 runs, which is far more than any real participant will produce; Pig refuses rather
than rolling over.

A task cannot currently refuse a repeat run. Every start gets a run, and Pig keeps them all.
A `max_runs` setting is planned; see [configuration.md](configuration.md).

## Run status

Pig stores two facts about where a run stands, and reports a single word derived from them.

**Phase** is how far along Pig's own bookkeeping is.

| Phase | Means | Accepting events? |
| --- | --- | --- |
| `collecting` | The run has started and the task is sending events. | Yes |
| `closed` | The run has stopped taking events. It's waiting for the sweep to finish it. | No |
| `done` | The sweep has finished the run: written its manifest and moved it to `done/`. Pig has no work left for it. | No |

**Disposition** is why the run stopped accepting data. A run still collecting doesn't have
one yet.

| Disposition | Means |
| --- | --- |
| `finalized` | The task said it was done, and vouches for having sent everything it had. |
| `expired` | The run stayed open as long as its task allows, and Pig closed it. |

The normal path is `collecting` → `closed` → `done` with a `finalized` disposition, and most
runs pass through `closed` too fast to notice. It is a real state anyway: finishing a run
means writing its manifest and moving its directory, and either can fail — a full disk, a
permissions mistake — in which case the run sits in `closed` until someone looks. The
sweep reports every run it couldn't finish, and tries again next time.

A run can only expire while it is collecting. Every task sets how long a run may stay open
(`expires_after`, counted from when the run started), and a run still collecting past that is
closed by the next sweep. A run already closed never expires; it's a run Pig still owes work
to, and it stays closed until that work succeeds — indefinitely, on purpose. There's no retry
logic and no failure state yet; the health check is how you find out. Deferred, not forgotten.

Keeping the two apart is deliberate. The phase stops mattering once a run is done, while the
disposition is the fact that still matters to whoever reads the data months later — which
is why the disposition is what a run's manifest records, and the phase never appears in
one. The database enforces the relationship between them with `CHECK` constraints, so a
row whose phase and disposition disagree can't be stored at all.

### What the API reports

The API, the CLI, and anything reading a dataset's state use one word, derived from the pair:

| Phase | Disposition | Reported status |
| --- | --- | --- |
| `collecting` | — | `in_progress` |
| `closed` | `finalized` | `finalizing` |
| `done` | `finalized` | `complete` |
| `closed` | `expired` | `expired` |
| `done` | `expired` | `expired` |

This is the only status vocabulary a task ever sees. Note the asymmetry in the last three
rows: `finalizing` and `complete` say whether the sweep has finished a finalized run yet,
and `expired` covers both for an expired one. That's deliberate. A task that didn't
finalize a run has nothing to do differently either way, so the distinction is for Pig's
operator, and `pig runs` shows the phase next to the status for exactly that reader.

Expiring is a normal way for a run to end, not a failure. A task with no natural finish — a
game people play for as long as they like — may never finalize a run at all; it sets a long
`expires_after` and starts a new run when Pig tells it the old one has expired. The only
difference between `complete` and `expired` is who vouched for the data: a `complete` run
carries the task's word that it sent everything it had before it stopped, and an `expired`
run holds everything that arrived. Both kinds are stored the same way, in the same place,
and each run's manifest says which kind it is; a reader who wants only vouched-for data
gets that from `pig organize`, which is where the distinction is applied.

Neither kind of run can be reopened. If the participant is still working after a run closes,
the task starts a new one, which gets the next run number.

Timestamps record when each transition happened — `started_at`, `closed_at`, and `done_at` —
and nothing reads them to decide what state a run is in. That's the phase's job. Tasks never
see them. See [configuration.md](configuration.md).

## Dataset

The stored data for one run: a directory named for the run ID, holding the run's events
and, once the sweep has finished the run, a manifest describing it.

```
done/stroop/9f3c1a7e-6b2d-4f80-9c11-2a5e8d40b7c3/
    manifest.json
    events.jsonl
```

`events.jsonl` is the events, one JSON object per line, in the order they arrived. Pig
never rewrites it: no sorting, no tidying. It can hold the same line twice — the retry
after a crash at the wrong moment writes it again — so a line count is an upper bound on
the number of events until `pig organize` has dropped the repeats. A run that never sent an
event still has an empty `events.jsonl`, so "is the dataset there" always has one answer.

`manifest.json` says what the run is without reference to Pig: the task code, the run
number and which parameters it counts, the parameters the link carried, whether the run
was finalized or expired, when it started, closed and was finished, and the size and hash
of every other file in the directory. It's written once, when the sweep finishes the run,
and never changed. Until then the database is the record for the run; afterwards the
manifest is. The two never overlap, so they can't disagree.

One run, one dataset. The dataset of a `complete` run is guaranteed to hold every event the
task sent before finalizing. The dataset of an `expired` run holds every event that reached
Pig before the run closed; the task may have had more it never got to send.

Nothing in the path says who the participant was. The readable tree — one directory per
participant, files named for the session and run number — is something `pig organize`
builds from the manifests, on whatever machine the data ends up on. See issue #9.

## Event

One JSON object — a trial, a response, a marker, whatever the task records. Events are the
unit Pig stores and counts, and it doesn't interpret their contents.

This is the BIDS sense of the word, not the REDCap one. A REDCap event is closer to what Pig
calls a session.

## Event ID

A string the task assigns to each event, unique within the run. A counter starting at 1 is
fine. A UUID is fine. As long as it's unique (and not absurdly long), it's fine.

Event IDs are not held to the [safe value](configuration.md) rule, because they never
become part of a filename — they live inside the dataset, as JSON keys. They only need to
be unique and bounded in length.

Pig accepts each event ID exactly once, which is what makes it safe to resend events after
a failed request. This is the mechanism the reliability guarantee rests on. (The file can
still hold an accepted event twice; see [Dataset](#dataset).)

If the same ID arrives twice with the same content, that's a retry and Pig ignores it. If it
arrives twice with *different* content, that's a task with a broken counter, and Pig refuses
the second one rather than picking a winner. See
[design_assumptions.md](design_assumptions.md).

## Task definition

The configuration entry for one task: expected link parameters, which of them make up the
run key, how long a run may stay open, whether signing is on, allowed origins, whether the
task is open. Lives in a version-controlled file. See [configuration.md](configuration.md).

## Data root

The directory Pig stores data under. It holds two trees, `in_progress/` and `done/`, each
with one directory per task and one directory per run inside that.

## Parameter signing

An optional per-task feature: link parameters carry a signature proving the link came from
you, so a participant can't edit their own ID or condition assignment. Off by default,
because it makes testing considerably harder.

## Open / closed

Whether a task accepts new runs. Closing a task is how data collection ends without taking
the service down. Runs already in progress keep going: they belong to participants who are
mid-task, and they finalize or expire on their own.

---

## What Pig doesn't have a word for

### Study

Pig has no concept of a study. Tasks are the top level: they're what configuration defines,
what URLs address, and what the directories under `done/` are named for. A lab running one
project with six tasks has six independent task definitions, and nothing in Pig ties them
together.

This is a deliberate simplification rather than an oversight. A study layer would give
shared participant rosters an obvious home and would match how labs talk, but it's a second
level of configuration, URL structure, and on-disk layout to design and maintain, and
nothing Pig does today needs it. Tasks that belong to the same project can say so in their
task codes.

Adding one later stays cheap as long as two rules hold: task codes are unique across the
whole deployment, and things shared between tasks are named and referenced rather than
written out inside each task. Together those keep a study out of the URL — it becomes
something you look up from a task code, not something you route through — so the layer can
be added without touching deployed tasks or moving data. See
[design_assumptions.md](design_assumptions.md).
