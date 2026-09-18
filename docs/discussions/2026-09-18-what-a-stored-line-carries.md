# What a stored line carries

*Superseded by [issue #3](https://github.com/uwmadison-chm/psych-ingestor/issues/3),
revised 2026-09-18. The conclusions are in that issue's "What a stored line carries"
section; this is the argument behind them.*

Worked out reading #3 one more time before implementing it (talked through with Claude).
The issue as it stood put `stored_at` on the stored line next to `timestamp`, and added a
sort key and a dedupe rule to keep both working. All three of those turned out to be
things to remove rather than things to specify. What follows is why, including the routes
that were considered and dropped, since the issue keeps only the answers.

---

## The starting complaint

Two fields on a stored line are outside `data`, and neither has a good reason to be there.

**`timestamp`** is the client's clock. The only thing the server does with it is sort on
it at sweep time. That's a weak reason to lift a field out of the task's own data and give
it a place in Pig's vocabulary — an optional field in the API, a type check in
`service.py`, a paragraph in `api.md`, and a share of the content hash.

**`stored_at`** is the server's clock, and it's a quasi-identifier. Not health
information, but the kind of thing that ends up on a list when someone asks what's
identifying about a dataset. There's an end-to-end-encrypted version of Pig planned, and
under it `data` is a blob the server can't read — so anything the server writes onto the
line sits outside the encryption.

## The shape

```json
{"event_id":"12","data":{…},"metadata":{"stored_at":"…"}}
```

`timestamp` moves into `data`. `stored_at` moves into `metadata`. The division is by who
wrote the bytes: `data` is the task's, `metadata` is Pig's.

**`metadata` needs an edge, or it becomes a junk drawer.** It's a name that refuses
nothing on its own. The rule: *facts Pig generated about the event, never anything the
client sent, and never hashed.* That makes the next question answerable instead of a
matter of taste — a per-event key Pig generated could go there; one the task handed over
could not.

**`event_id` stays at the top level, and that's not an inconsistency to fix.** It's
client-supplied and it isn't in `data`, because it's the one thing Pig reads. Under an
encrypted task, `data` is opaque and the ingest-time duplicate check still has to work.
The rule isn't "client bytes in `data`" — it's *Pig reads exactly one thing the task
sends, and that one thing stays where Pig can see it.*

## What the nesting does and doesn't buy

Rejected framing, worth naming because it's the obvious one: *putting `stored_at` in
`metadata` keeps it from escaping the encryption.* It doesn't. The server generates it, so
it is outside any client-side encryption wherever it sits on the line. Nesting hides
nothing.

What it does buy is a subtree instead of a field list. [#8](https://github.com/uwmadison-chm/psych-ingestor/issues/8)
nulls one named thing rather than enumerating server fields, and a scrubber written
downstream doesn't need to know what Pig added last release. That's a real benefit and a
small one; the honest version is worth stating rather than the grander one.

The stronger argument runs the other way. Under end-to-end encryption `data` is opaque,
which only works if nothing the server needs lives inside it. That's precisely why
`timestamp` can leave the top level — nothing reads it — and why `event_id` can't.

## Pig stops having a timestamp field

Considered and rejected: keep accepting a top-level `timestamp` and have Pig fold it into
`data` at ingest. That would mean Pig editing `data`, which contradicts the one promise
`api.md` makes about it — "`data` is yours. Pig does not look inside it and stores it
unchanged."

So the submitted event becomes `{"data": {…}}` and nothing else. A task that wants a
timestamp records it in `data` alongside everything else it records, which is where the
rest of its clock readings already live. The type check and its error message go with the
field.

The retry warning in `api.md` — build each event once, don't rebuild it and re-read the
clock — stays, and carries more weight than before. The clock is inside the hashed payload
either way; only the field name changes.

## The server stops ordering

`storage.sort_key` sorts on `(timestamp, event_id)` as strings, so a task that sends no
timestamps gets `"1"`, `"10"`, `"2"`. An earlier comment on #3 proposed
`(timestamp, stored_at, event_id)` to fix that.

The better fix is to stop. An ordering assembled from a client clock the server cannot
check is not more trustworthy than the order things arrived in, and arrival order is
something Pig actually knows. A consumer that wants chronological order sorts on whatever
the task recorded, which it understands better than Pig does; if that's worth automating
it's a sort key for `pig organize`, later.

`api.md`'s advice that counting event IDs lets you skip timestamps deletes rather than
gets corrected.

## The server stops deduplicating

`file_dataset` drops lines that repeat byte-for-byte. #3 proposed narrowing that to the
hashed subset, since `stored_at` on the line would make a crash-window retry no longer
byte-identical.

Better: don't deduplicate at all. Sorting was already going, and dedupe was the rest of
what sweep did to the file. Without both, nothing rewrites `events.jsonl` — sweep writes
the manifest, fsyncs and renames — and the file in `done/` is the append log exactly as
the server wrote it. That's what makes the manifest's `sha256` a hash of what happened
rather than of a tidied-up rendering of it.

**Where duplicate lines come from.** Two paths, both from the file-then-database write
order, which is not changing:

- The documented crash window. The append succeeds, the process dies before
  `record_receipt`, the retry finds no receipt and appends the line again.
- A race nothing documents. Two identical requests in flight at once both pass the
  `receipt_hash` check and both append; the loser's `INSERT` fails on the unique
  constraint, it re-reads, finds the same digest, and returns no error. Two identical
  lines, nobody crashed.

**A repro that doesn't work**, recorded so nobody builds a test on it: putting the same
event twice in one request. The body is an object keyed by event ID, so two copies are two
duplicate JSON keys and the parser keeps one. Reproducing this means preventing the
receipt write — kill between the append and `record_receipt`, or send two identical
requests concurrently.

**What it costs.** The manifest says no event count because it's `wc -l`. With duplicates
left in place, `wc -l` is an upper bound until organize has run. Acceptable: a line count
on an unorganized file wasn't an interesting number anyway, and the file is on the web
server, which is not where anyone should be counting trials.

**Where it moves.** `pig organize` rewrites the file already, so dropping exact duplicates
there is free, and it needs nothing but the run directory — no database, no `pig.toml` —
so #9's rule holds. Whether organize reports what it dropped is #9's question.

## A claim in design_assumptions.md that was already too strong

`design_assumptions.md` says that by the time a dataset reaches finalize, every line
sharing an event ID is guaranteed byte-identical, because the request that would have
introduced a differing one was refused — and that this is what makes the dedupe safe.

The guarantee doesn't hold in the crash window, which is the only place duplicates come
from. There's no receipt there to compare against, so a client that rebuilds the event and
re-reads its clock lands a differing line with the same ID. `storage.py` knows this: its
docstring says lines sharing an ID but differing in content are both kept. The code was
right and the assumption was overstated.

It stops mattering, because the paragraph existed only to justify the dedupe. The
write-time hash check stands on its own: it refuses two genuinely different events sharing
an ID at ingest, which is all it was ever for. What replaces the section's conclusion is
that the crash window leaves duplicate lines and Pig leaves them there — the file holds at
least one copy of every event the server accepted, not exactly one.

## Loose end

`max_event_size` is measured against the canonical line, which would now include
`metadata`. That makes the limit a task is held to drift whenever a server-generated field
is added. Measuring it against the hashed subset instead keeps the limit about what the
task sent. Small, but it's a behavior change either way, so it's in the issue rather than
left to whoever writes the code.
