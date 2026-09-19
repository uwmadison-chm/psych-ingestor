# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## The developer

The main developer is Nate Vack. In these documents, "I" normally refers to him, and "we" or "you" refers to him and anyone else (LLMs included!) doing development in this project.

## Status

Early. Much of `docs/` is draft, and open questions are marked as such in place. Don't treat a draft as a decision, and don't quietly resolve an open question — raise it.

There is now a working first implementation (the run, event, and media endpoints in `docs/api.md`, `/health`, TOML config, SQLite, `pig check|serve|sweep|runs|health`), built deliberately as something to get feedback on rather than as a settled design. It had to answer some open questions to exist; those answers are marked *provisional* where the question appears in `docs/`. A provisional answer is not a decision either — changing one should be cheap, and if it isn't, that's worth saying.

This guidance applies generally -- when I've asked for something that conflicts with past decisions, or past decisions seem inappropriate for work you're doing, stop and ask rather than tying to brute-force your way through things, or guess what I mean. Sometimes I forget stuff. Sometimes I change my mind and forget to write it down.

## What this project is

Psych Ingestor (Pig): an HTTP API for ingesting, routing, and storing data from online
behavioral-research tasks. Its parts:

- A Python / FastAPI service handling data collection and storage
- A file containing task definitions
- A SQLite database of runtime state — runs, participants, event bookkeeping
- A collection of JSONL files holding the data for each task's runs
- A CLI for the database and for validating configuration

## Documentation map

Read what the task at hand calls for rather than all of it.

| Read | When |
| --- | --- |
| [docs/design_assumptions.md](docs/design_assumptions.md) | Before any design or implementation decision. These constraints are what to argue from when a choice is contested. |
| [docs/definitions.md](docs/definitions.md) | Any time you use the words task, session, run, dataset, event, or participant. Pig follows BIDS: a participant comes in for a *session*, does a *task*, and each time they do it is a *run*. "Session" never means the HTTP thing. |
| [docs/api.md](docs/api.md) | Working on endpoints, request/response shapes, or the client side of a task. |
| [docs/configuration.md](docs/configuration.md) | Working on task definitions, the config file, storage paths, or the CLI. |
| [docs/security.md](docs/security.md) | Working on CORS, signing, or anything about what Pig does and doesn't defend against. |
| [docs/deployment.md](docs/deployment.md) | Working on running the service — systemd, the health check. |
| [docs/documentation_style.md](docs/documentation_style.md) | Writing any user-facing prose — docs, error messages, CLI help. Read `README.md` alongside it. |
| [docs/discussions/](docs/discussions/) | Only when you need to know *why* a past decision went the way it did. Superseded material — never current, and never a source for what Pig does today. |

`README.md` is maintained deliberately as an example of well-written prose for this
project. Well, what Nate considers well-written, anyhow. Treat it as the style reference, and don't rewrite it without being asked.

## The load-bearing constraints

Summarized from `docs/design_assumptions.md`; that file is canonical.

- **Reliability over convenience.** If the server says an event is stored, it's on disk. If
  a dataset is complete, it holds every event the task sent. Refuse a request you can't
  honor rather than accepting it hopefully.
- **Clarity over cleverness.** Researchers will read this code. Prefer a longer obvious
  function to a shorter clever one, and no abstraction to an abstraction with one caller.
- **Write for researchers, not programmers.** This applies to the API's shape as much as
  to its documentation.
- **Few dependencies.** No server-based database, no external queue. Someone should be able
  to run Pig on a laptop in an afternoon.

## Settled, and not to be re-opened casually

- **No studies.** Tasks are the top level — of configuration, of URLs, of storage. Two
  rules keep that reversible, and both are load-bearing: task codes are unique across the
  whole deployment, and anything shared between tasks (a roster, a session list) is a named
  object a task references rather than something inlined in its definition. Breaking either
  one is what would force a study into the URL space later.
- **All background work is in the CLI.** The web service handles requests and nothing else:
  no threads, no schedulers, no work outliving a request. Finishing closed runs, copying
  them offsite, and expiring runs that have been open too long are all scheduled CLI
  commands.
- **Extra link parameters are ignored**, not an error. Recorded on the run, never used to
  identify or route anything.
- **Repeat runs are always allowed** and always get their own dataset. `max_runs` may be
  added someday but does not yet exist.
- **A run the task never finalizes is a normal outcome**, not a failure. Every task sets
  `expires_after`, a fixed limit from run start; the next sweep marks such a run `expired`
  and finishes it. `complete` means the task vouched for the data, `expired` means it holds
  what arrived. Nothing reopens either, from the API or the CLI. Any closed run answers
  `409`, whichever way it closed. See issue #12.
- **Runs are stored by run ID, with a manifest.** `in_progress/{task}/{run_id}/` while Pig
  is working on it, `done/{task}/{run_id}/` afterwards, moved by one rename. The manifest
  is written once, by the sweep, and never changed: the database is the record in flight,
  the manifest afterwards. The server never rewrites `events.jsonl`. A readable tree is
  `pig organize`'s job (#9), on another machine, and organize never reads `pig.toml` or
  the database. Purge deletes files, never run rows — rows are the run-number counter.
  See issue #3.
- **Parameter values are kept exactly as they arrive.** No lowercasing anywhere.
  `PPT-1003` and `ppt-1003` are two participants.
- **Closing a task refuses new runs only.** Runs in progress keep going and expire on
  their own schedule.
- **A media item is an event with bytes attached.** Starting one stores an ordinary
  event with `media_id` in its `metadata`; parts are `PUT` by number into
  `media/{media_id}/`; `finish` takes a count and is strict. Pig defines no media fields,
  never joins parts, and caps per part only. Off per task unless `media = true`. See
  issue #4.

## Open scoping questions

Live, and worth flagging rather than assuming past:

- **Run finalization** — whether it reports an event count. Deferred deliberately.
  *Provisionally: no count.*
- **Settings returned at run start** — condition assignment and similar. Planned, unshaped.
  Not built.
