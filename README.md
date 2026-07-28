# Psych Ingestor
A simple web server for ingesting, routing, and saving data from behavioral tasks in the psych research world

**Status: early.** There's a working service you can run and point a task at -- see
`docs/trying_it.md`. It does the four things in `docs/api.md` and not much else; the
deferred features below are still deferred. If you're writing a task against it, read
`docs/api.md` -- it ends with a list of what's still moving.

## Overview
Psych Ingestor (Pig) is set up to handle data streams from a variety of online studies -- in principle, all of your lab's tasks can share this service. You'll set up a file containing a set of task definitions, and your tasks will send HTTPS requests to it to signal start and end of tasks, as well as send data. Data is sent as a series of JSON lines (one JSON object per recorded event) and stored with minimal processing.

Your task itself will generally be statically-hostable; it will normally consist of HTML, javascript, and CSS. There's nothing saying you _can't_ have a separate server-side component to this, but you don't need one. Similarly, if you're building your tasks as standalone apps, that works too -- a task can collect a whole session offline and send everything at once when the device next has a network.

Technically, Pig consists of a few components:

- A Python / FastAPI service that handles data collection and storage
- A file, in version control, containing your task definitions
- A SQLite database of runtime state -- runs in progress, participants seen, which events have been stored
- Somewhere on the server to keep the data files, one per run
- A command-line program that does everything happening on a schedule rather than on request, plus checking your configuration

Tasks are the top level: there's no notion of a study or a project that owns them. A lab
running one project with six tasks configures six tasks.

## The words

Pig uses BIDS' vocabulary, because most people here have already met it:

- A **participant** takes part in a study
- They come in for a **session** -- `baseline`, `3mo`, whatever your study calls its timepoints
- At each session they do a **task** -- a Stroop task, a delay-discounting game
- Each time they do that task is a **run**, and a run records **events**

A run is the thing Pig tracks. It starts when a participant arrives, collects events while
they work, and finishes when the task says so. If the same participant starts the same task
twice, that's two runs, and Pig keeps both -- nothing overwrites anything.

See `docs/definitions.md` if you need the precise version.

## How a task talks to Pig

Four URLs, and you only need the first three. All of them take and return JSON.

| URL | What it does |
| --- | --- |
| `POST /task/{task_code}/run` | Start a run. Send the parameters from the participant's link; get back a run ID. |
| `POST /task/{task_code}/run/{run_id}` | Send events. One JSON object per event, keyed by an event ID you make up. |
| `POST /task/{task_code}/run/{run_id}/finalize` | Say you're done. Pig files the data away. |
| `GET /task/{task_code}/run/{run_id}` | Check on a run -- its status and which events Pig has. |

The event ID is the important part. You assign one to every event, unique within the run,
and Pig stores each ID exactly once. That means a failed request is always safe to send
again: retry the whole batch and Pig will keep what it's missing and ignore what it already
has. It also means a flaky connection can't quietly put a duplicate trial in your data
file.

When you send events, Pig replies with everything it's stored for the run so far, so you
always know where you stand. If the participant closes the tab before you finalize, the
events you already sent are still saved.

See `docs/api.md` for the full detail, including a complete worked example.

## Task configuration

Each task gets an entry in a configuration file, kept in version control. The entry
answers things like:

- What parameters Pig expects from the link to define a run (`participant_id`? plus a `session` for repeated visits?)
- Where data should be stored, and how the files should be named
- What (if any) settings will Pig send back to the task when a run starts?
- How long to wait for a run to conclude, and what to do if it's left open
- Is the task accepting more data, or is it currently closed?

See `docs/configuration.md`.

## Command-line programs

The web service only does web service things -- it takes requests, checks them, and writes
data. Everything that happens on a schedule instead of on request lives in a command-line
program you run from cron or a systemd timer: filing finished datasets where they belong,
copying them offsite, reaping runs that were never finalized.

There's also a `check` command for configuration, because a typo in a task definition
should surface before a participant hits the task, not during.

The commands are `pig check`, `pig serve`, `pig sweep`, `pig runs`, `pig health`, and
`pig finalize`. See `docs/trying_it.md`.

## Documentation

| File | What's in it |
| --- | --- |
| `docs/trying_it.md` | Getting a server running on your own machine. Start here if you want to poke at it. |
| `docs/api.md` | The requests your task makes. Start here if you're writing a task. |
| `docs/definitions.md` | What Pig means by task, session, run, dataset, event, participant. |
| `docs/configuration.md` | Task definitions, storage paths, the CLI. |
| `docs/security.md` | What Pig defends against and what it doesn't. |
| `docs/deployment.md` | Running the service. |
| `docs/design_assumptions.md` | Why Pig is shaped the way it is. Read before arguing about a design decision. |
| `docs/documentation_style.md` | How to write Pig's user-facing prose. |

## Deferred features

These _may_ prove important, but they're not what we're building first.

### Parameter signing

If you want to stop people from editing their own session parameters, we can generate per-study (or per-participant or per-session) keys, and either distribute a signing key to clients, or distribute signed values to them (eg, we put a participant_id signature in REDCap and pass that to tasks). When starting a run we can check that the value has been correctly signed.

This is worth doing eventually and it's a real complication for testing -- you can't type a URL by hand any more -- so it'll be optional and set per task.

### Limiting the number of runs

Right now, a participant who starts the same task five times gets five runs, all kept. A
task might reasonably want to say "one and done," or "at most three" -- a `max_runs`
setting. That needs a defined answer for what the participant past the limit gets told,
since turning someone away mid-study is a real event and the task has to say something
useful to them.

### Saving binary data

Audio, images, video could be generated during a study. Right now you could bodge them into the `data` field for an event, base64 encoded, but that will only work for very small content and is generally silly.

### Pre-defining the participants / runs for a study

Maybe you want a bit more security than just "hey anyone can gin up a json request and barf data into the study" and that's a pretty reasonable thing to want to limit. So we can add a CLI that lets study managers / scripts add participants and/or sessions to a database of people who are allowed to do a task.

When that happens, a roster will be a named thing that tasks point at, rather than a list written into each task's definition -- so the six tasks in one project can share one roster by naming it.
