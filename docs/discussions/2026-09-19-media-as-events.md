# Media as events

*Superseded by [issue #4](https://github.com/uwmadison-chm/psych-ingestor/issues/4),
rewritten 2026-09-19. Worked out reading the original proposal against the code after
[#24](https://github.com/uwmadison-chm/psych-ingestor/pull/24) landed (talked through with
Claude). The conclusions are in the issue; the argument, and the routes considered and
dropped, are here.*

The original text is archived in
[2026-07-29-recording-audio-and-video.md](2026-07-29-recording-audio-and-video.md).

---

## The starting complaint

The original proposal has a media start request carry `event_id`, `content_type`,
`filename`, and a free-form `metadata` object the task fills in. Between that proposal and
now, #24 defined `metadata` on a stored line with an edge: *facts Pig generated about the
event, never anything the client sent, and never hashed.* A client-supplied `metadata` on a
media request contradicts that by name, and the docs would have to explain two opposite
meanings of one word.

Nate's answer to the filename question already pointed at the fix: nothing but `event_id`
is required, Pig reads none of it, and it all belongs in one free-form object the task
owns. That object already has a name in Pig. It's `data`.

## A media item is an event with bytes attached

So the start body is `{"event_id": ..., "data": {...}}`, which is exactly an event. Rather
than a second thing that happens to look like an event, make it one: starting a media
item stores an ordinary line in `events.jsonl`, under the same ID rule, the same hash, the
same `max_event_size`. The one thing Pig adds is the media ID, and it goes where Pig's
facts go:

```json
{"event_id":"trial12_video","data":{"content_type":"video/webm","trial":12},"metadata":{"stored_at":"…","media_id":1}}
```

The discussion behind #24 said a per-event key Pig generated could live in `metadata`.
This is that case, and it's the first one.

What it buys, in order of weight:

**Retrying the start request becomes safe.** As drafted, a start had no identity. A
request whose response was lost and then retried made a second media item with a new
ID, and the client could never learn which one it had been uploading to. The orphan
wasn't data loss, but it was an empty, never-finished item in every manifest it happened
in. And it's the one request in the flow that's in flight while the recorder is already
running, which is when a timeout is likeliest. With the event ID as the identity, a
retry finds the receipt, compares the hash, and returns the same `media_id`. Same ID
and content, `200`; same ID and different content, refused. The rule events already
follow, applied to one more thing.

**Pig defines no media fields.** `content_type`, a suggested filename, which trial the
item belongs to: all `data`, all conventions of the client helper, none of it read or
typed or documented by Pig. The declared content type still matters to whoever tries to
read a damaged file later, which is why the helper should always record it. But that's
advice to task authors, not a rule the server enforces.

**`event_id` stops being an unverified pointer.** The original had it name *the event
that started this recording*, unverified, possibly never arriving: a note about
provenance. Now it's the item's own ID. A task that wants to say "this is the video for
trial 12" puts that in `data`, as it would put a trial number anywhere else.

**A reader of `events.jsonl` sees media in sequence with everything else**, and the line
says where the bytes are. Without this they cross-reference the manifest.

**The per-recording manifest block shrinks.** The original carried content type,
filename, the task's metadata, and every part's hash. Content type and the rest are in
the event line now, and the part hashes are already in `files`, so what's left is the
media ID, the event ID, and whether the item was finished.

One cost: the events `stored` list gains media IDs. That's correct, since the task did
send that event, but `api.md` has to say so.

## Finish takes a count, and it's strict

The original had `finish` carry `{"parts": N}` as a statement of intent, recorded and
not checked, and left open what happens to a part arriving after finish. Two readings
were on the table:

- **Accept stragglers.** A retry of part 17 after finish is correct client behaviour,
  and refusing it is refusing data we can't get back.
- **Drop the count** and have finish mean only "no more parts."

Nate's position, and the one taken: if finish carries a count, Pig should not `200` a
finish whose count doesn't match what it holds, and a finished item should take no more
parts. So finish checks that Pig holds exactly parts 1 through N. If it does, the item
is finished and closed. If it doesn't, finish is refused with `stored` in the reply, and
the task resends what's missing and tries again.

That gives `finished` the meaning `complete` has for a run: the task vouched for the
data and Pig verified it holds all of it. An item never finished is the media
counterpart of an expired run. It holds what arrived, and the manifest says it was never
finished. Finalizing the run doesn't refuse it.

Why a count here when run finalize, provisionally, has none: parts are a byte stream cut
up in order, so "1 through N" is a complete statement of what was meant. Event IDs
aren't contiguous by any rule, so a count says much less about events. And the count is
what catches the one failure a contiguity check can't: the last part, the one most
likely to still be in flight when the task decides it's done.

The stragglers argument loses because the client helper drains its queue before sending
finish, so a part after a successful finish is a helper bug, not a network condition.
And a part after a *refused* finish is exactly what the refusal asked for.

## Purge confirms files, not the manifest

The comment on #2 leaned toward confirming a purge with one hash of the manifest, since
the manifest carries the file hashes. That proves the caller has the manifest, not the
files: if the transfer copied the manifest and dropped a part, a manifest hash passes.
Nate's instinct was per-file hashes, and it's right. `pig purge` hashes every file in its
local copy and sends the set; the server compares against the `files` block of its
stored manifest, without re-reading anything, and requires an exact match with nothing
missing and nothing extra.

Two things follow for #4:

- **`describe_files` already covers media.** It walks the whole run directory, so every
  part lands in `files` with size and sha256 with no media-specific code. That's why the
  per-item manifest block must not repeat part hashes: two places is one place too many.
- **The sweep has to remove stray `.partial` files** before writing the manifest. A
  crashed upload leaves one; `rglob` would hash it into `files`; purge would then
  require the secure side to hold a file Pig never said it stored. Deleting it is right
  because Pig never acknowledged it.

## What changed under the issue

Things true of the code as of #24 and #25 that the original didn't have to deal with:

- **The service is synchronous.** Every endpoint is a plain `def` run in a threadpool.
  Streaming a body to disk needs an `async def` handler and `request.stream()`. The
  writes and the final fsync briefly block the event loop. Fine at this scale, and worth
  a comment so nobody "fixes" it.
- **CORS allows GET, POST and OPTIONS.** PUT has to be added or the preflight fails
  before Pig hears anything.
- **The write order flips.** Events are file then database, and a crash between them
  leaves a duplicate line. A part is streamed to `.partial` while hashing, compared
  against any receipt, renamed into place, then recorded. A crash leaves an unreceipted
  file that the retry replaces with identical bytes. Nothing duplicated.
- **#8's plan needs a media analogue.** Media rows and part receipts are live-run
  mechanisms, deletable at sweep after the manifest is written. The sweep's
  receipts-but-no-file guard extends to parts.
- **`max_media_bytes` goes**, per Nate's answer that the cap is per part and means "the
  server can't handle this part." That leaves no per-run byte bound at all: a runaway
  task fills the disk until `expires_after` closes the run. Accepted, and it makes free
  space in `/health` required work rather than a nicety.

## Considered and dropped

- **Media as opt-in per task.** Raised because an unauthenticated PUT taking 8 MB bodies
  is a different exposure from 1 KB events, and most tasks record nothing. Not decided
  here; it's an open question on the issue.
- **"Recording" as the word.** Images aren't recordings. The issue says *media* and *media
  item*.
- **The upload-test endpoint** in the comment on #4 is good and separable. It waits.
- **A single-shot form** for one-part items: the helper hides the three requests, so
  handled on the client.
