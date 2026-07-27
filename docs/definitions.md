# Definitions

The words Pig uses, and what each one means here. Where a term is used loosely elsewhere in
the docs, this page wins.

Pig borrows its vocabulary from BIDS, because most people reading this have already met it
there: a **participant** comes in for a **session**, does a **task**, possibly more than
once — each time is a **run** — and a run records **events**.

## Task

One thing a participant does: a Stroop task, a delay-discounting game, whatever.

A task has a definition in configuration, a place its data goes, and a state — open to new
data or closed.

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
finish. It's identified by a **run ID** that Pig generates and returns — not by the link
parameters, though the parameters are what determine which run is meant.

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

Run IDs don't appear in filenames — that's the run number's job.

## Run key

The parameters whose values decide whether two runs are repeats of the same thing. For
example, `["participant_id", "session"]` means Pig tracks participants and sessions
together, so participant 10351 arriving at `baseline` twice produces two runs of the same
key.

Which parameters make up the key is set per task. See
[configuration.md](configuration.md).

## Run number

Which time this is, for a given run key: `run-0001` is the first time participant 10351
started the Balloons game at `baseline`, `run-0002` the second.

Run numbers count up from 1 and are never reused. Starting a task again with the same link
always produces a new run with a new number, so Pig never has to overwrite or merge
anything.

This is the number that goes in filenames, zero-padded to four digits so that a directory
listing sorts the way a person expects. Four digits caps a run key at 9999 runs, which is
far more than any real participant will produce; Pig refuses rather than rolling over.

A task cannot currently refuse a repeat run. Every start gets a run, and Pig keeps them all.
A `max_runs` setting is planned; see [configuration.md](configuration.md).

## Run status

Every run is in exactly one of four states. This is the only status vocabulary Pig uses —
the API reports these words, the CLI prints them, and a dataset takes its state from the
run that produced it.

| Status | Means | Accepting events? |
| --- | --- | --- |
| `in_progress` | The run has started and the task is sending events. | Yes |
| `finalizing` | The task said it was done. Pig has the events and is filing them. | No |
| `complete` | Pig is finished with the run. The dataset is whole and where it belongs. | No |
| `abandoned` | The run was never finalized, and Pig has waited long enough to give up. | No |

The normal path is `in_progress` → `finalizing` → `complete`, and most runs pass through
`finalizing` too fast to notice. It is a real state anyway: filing a dataset can mean
sorting it, moving it to completed storage, and copying it somewhere else entirely (an
`rclone` push to S3, say). If that destination is slow or down, runs can sit in
`finalizing` for a long time, and some will need a human.

`abandoned` is reachable only from `in_progress` — it's what happens to a run whose task
never came back. A run that got stuck in `finalizing` is not abandoned; it's a run Pig still
owes work to, and it stays `finalizing` until that work succeeds.

A run that fails to be filed stays in `finalizing` indefinitely, on purpose. There's no
retry logic and no failure state yet; the health check is how you find out. Deferred, not
forgotten.

Abandoned runs will eventually get collected and filed too — their datasets are worth
keeping, but they must not be mixed in with the data of runs that finished properly. A
reader pointed at completed data should get only datasets known to hold everything.

Whether a dataset has been copied to its final home is tracked separately from the run's
status, as a `filed_at` timestamp that's empty until the copy succeeds. It's a different
fact from how the run ended, and keeping the two apart means "every run that finished
properly" stays a single check. Tasks never see it. See
[configuration.md](configuration.md).

## Dataset

The stored events for one run — in practice, one `.jsonl` file on disk, at a path like
`{task_code}/{participant_id}/{session}_run-0002.jsonl`. The values in that path are
lowercased; see [configuration.md](configuration.md).

One run, one dataset. The dataset of a `complete` run is guaranteed to hold every event the
task sent before finalizing.

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

Pig stores each event ID exactly once, which is what makes it safe to resend events after a
failed request. This is the mechanism the reliability guarantee rests on.

If the same ID arrives twice with the same content, that's a retry and Pig ignores it. If it
arrives twice with *different* content, that's a task with a broken counter, and Pig refuses
the second one rather than picking a winner. See
[design_assumptions.md](design_assumptions.md).

## Task definition

The configuration entry for one task: expected link parameters, where data goes and what
it's named, whether signing is on, allowed origins, whether the task is open. Lives in a
version-controlled file. See [configuration.md](configuration.md).

## Data root

The directory Pig stores data under. Every task's storage path is relative to it, and a path
that would escape it is refused.

## Parameter signing

An optional per-task feature: link parameters carry a signature proving the link came from
you, so a participant can't edit their own ID or condition assignment. Off by default,
because it makes testing considerably harder.

## Open / closed

Whether a task accepts new data. Closing a task is how data collection ends without taking
the service down. (Whether closing also stops runs already in progress is an open question
— see [configuration.md](configuration.md).)

---

## What Pig doesn't have a word for

### Study

Pig has no concept of a study. Tasks are the top level: they're what configuration defines,
what URLs address, and what data directories are named for. A lab running one project with
six tasks has six independent task definitions, and nothing in Pig ties them together.

This is a deliberate simplification rather than an oversight. A study layer would give
shared participant rosters an obvious home and would match how labs talk, but it's a second
level of configuration, URL structure, and on-disk layout to design and maintain, and
nothing Pig does today needs it. Tasks that belong to the same project can say so in their
task codes and storage paths.

Adding one later stays cheap as long as two rules hold: task codes are unique across the
whole deployment, and things shared between tasks are named and referenced rather than
written out inside each task. Together those keep a study out of the URL — it becomes
something you look up from a task code, not something you route through — so the layer can
be added without touching deployed tasks or moving data. See
[design_assumptions.md](design_assumptions.md).
