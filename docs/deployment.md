# Deployment Guidelines

**Status: the two things below both exist (`pig serve` and `pig sweep`); the systemd units
to run them don't. To try Pig on a laptop, see [trying_it.md](trying_it.md).**

Systemd stuff here

## Two things to run

**The web service**, under systemd, handling requests. It does nothing on its own schedule.

**A scheduled CLI command**, on a systemd timer, doing everything else: filing finished
datasets into completed storage, copying them wherever each task says they go, reaping runs
that were never finalized, and retrying whatever didn't work last time.

Both are required. A deployment that runs only the service collects data correctly and
never files any of it — runs pile up in `finalizing`, which is not data loss but is not
finished either.

How often to run it is a judgment call. Every few minutes is plenty; the only thing waiting
on it is somebody who wants to read a finished dataset. It must be safe to run twice at
once, or to run while nothing needs doing, because a timer will do both.

Trying Pig out on a laptop, you can skip the timer and run the command by hand when you want
to see a run reach `complete`.

## The health check

### `GET /health`

Status information about the service, as JSON, for monitoring and for answering "is
anything wrong right now." People writing tasks don't use this; see [api.md](api.md) for
what they need.

Partly built. What it reports today: whether the database and data root are writable, and
for each task whether it's open, how many runs are in each status, how many are waiting to
be filed, how many have been `finalizing` too long, and when data last arrived. It answers
`503` rather than `200` when something is wrong, so a monitor can watch the status code.
`pig health` prints the same report.

The rest of this list isn't built yet — duplicate event IDs and case-variant parameters in
particular, which are the two that need someone to go looking at the data.

For the service generally:

- Are there errors in the configuration? *Built. The service re-reads its configuration
  file when it changes, keeps running on the last one that loaded if a save is broken,
  and reports that here — which makes this the only place a bad save surfaces.*
- Is everything basically working — is the database there, can we write to it, can we read
  and write in the data directories?

For each task:

- Is the task open or closed?
- How many runs are in each status right now?
- Runs that have been `finalizing` longer than they should be, and why. A dataset that
  can't be copied to its final home stays `finalizing` indefinitely and there's no retry
  yet, so this is the only place it surfaces.
- Runs whose datasets haven't been filed yet — no `filed_at` timestamp.
- When did data last arrive?
- Datasets holding more than one event with the same ID. Rare, and not data loss, but an
  analyst who assumes IDs are unique needs to know before they start counting. See
  [design_assumptions.md](design_assumptions.md).
- Link parameters that have arrived in more than one capitalization. They all store to the
  same place, so nothing is broken, but it usually means two versions of a participant link
  are in circulation — which is worth knowing before someone asks why a spreadsheet has
  both `PPT-1003` and `ppt-1003` in it.
