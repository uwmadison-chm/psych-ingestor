# Deployment Guidelines

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

Can be deferred slightly, but not indefinitely — it's where a stuck run becomes visible.

For the service generally:

- Are there errors in the configuration?
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
