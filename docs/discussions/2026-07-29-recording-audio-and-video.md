# Recording audio and video from tasks: issue #4 as it stood before the rewrite

*Superseded by [issue #4](https://github.com/uwmadison-chm/psych-ingestor/issues/4),
rewritten 2026-09-19. This is the text of that issue beforehand, with Nate's answers to
its open questions left inline where he wrote them.*

Kept because the rewrite cut most of it. The proxy and streaming section, the table of what
a missing part costs, and the argument against pre-signed URLs survive in shorter form; the
rest is either folded into the new body or dropped. The reasoning for what changed is in
[2026-09-19-media-as-events.md](2026-09-19-media-as-events.md).

Two things to know reading it. The text predates
[#3](https://github.com/uwmadison-chm/psych-ingestor/issues/3) landing, so it speaks of
the run-directory layout as an assumption rather than a fact. And it proposes a
client-supplied `metadata` field on a media start request, which
[#24](https://github.com/uwmadison-chm/psych-ingestor/pull/24) later made impossible by
giving `metadata` on a stored line the opposite meaning: facts Pig generated, never
anything the client sent. That collision is what started the rewrite.

The comment proposing an upload-test endpoint is still on the issue, where it's dated.

---

Two tasks coming up need this: one records video from the selfie camera for the length of the
task, another records short audio answers to prompts. Right now there's nowhere for that data
to go.

This is a proposal, not a decision. The open questions at the bottom are real.

The storage layout here assumes #3.

## Why not base64 in an event

The obvious thing — encode the media and put it in `data` — is bad in a way that's worth
being explicit about, because it's what someone will try first:

- It breaks the "readable JSONL, appendable one line at a time" property most of the
  reliability story rests on. A 4 MB single-line event makes the dataset unopenable in a text
  editor.
- 33% overhead on data that's already the biggest thing we store.
- It puts unrecoverable bytes behind `max_event_size`, a limit designed for a trial record.
- The whole blob goes through the canonical-JSON hash path on every arrival.

So media is its own thing, with its own endpoints.

## The proposal

A recording is stateful, so it follows the shape a task already knows from runs: **post to
start it, send parts, say when you're done.** Start run / events / finalize, one level down.

### Starting a recording

```
POST /task/{task_code}/run/{run_id}/media
```

```json
{
  "event_id": "trial12_video",
  "content_type": "video/webm;codecs=\"vp8,opus\"",
  "filename": "trial12_video.webm",
  "metadata": { "camera": "user", "prompt": 12 }
}
```

Pig replies with a media ID, which is a number counting from 1 within the run:

```json
{ "media_id": 1 }
```

It doesn't need to be unguessable the way a run ID does — you already need the run ID to get
here — so a readable counter is better than a UUID, and it's the same number the recording's
directory is named for on disk.

Every field except `event_id` is optional. pig interprets none of them:

- **`event_id`** — the event that started this recording, which is how you know what the
  recording is *of*. A recording starts because something happened in the task, and that's an
  event. Pig doesn't verify it: the event need not have been accepted yet, and need not ever
  arrive. It's a note about provenance, not a foreign key. (This is metadata rather than an
  identifier, which is what lets event IDs keep their exemption from the safe-value rule —
  they never touch the filesystem.)
- **`content_type`** — stored verbatim, never parsed, never validated, never used to pick a
  file extension. See below.
- **`filename`** — what the task suggests the assembled file be called. Pig never assembles
  anything, so this is data.
- **`metadata`** — anything else the task wants to record about the recording. Same contract
  as `data` on an event: Pig stores it and does not look inside. This is where "and such"
  lives, so there's no list of blessed fields to maintain and argue about.

### Sending parts

```
PUT /task/{task_code}/run/{run_id}/media/{media_id}/{part}
```

The body is the raw bytes. Nothing else — no wrapper, no encoding, no multipart. `part` is an
integer counting from 1, required even when there's only one.

The URL is the part's identity, so idempotency needs no thought: retrying means sending to the
same URL again. Same part, same bytes is a retry. Same part, different bytes is refused. That
is the identical rule events already follow, so there's one concept rather than two.

### Finishing

```
POST /task/{task_code}/run/{run_id}/media/{media_id}/finish
```

```json
{ "parts": 37 }
```

The count is the one moment the task says what it *intended* to send. Without it, "skipped
part 17" and "stopped recording at part 16" are indistinguishable to anyone reading the data
later, and Pig doesn't infer intent anywhere else.

### The client

```javascript
// Start the recorder first: mimeType isn't reliably populated until after start().
recorder.start(5000);

const { media_id } = await fetch(`${PIG}/task/${TASK}/run/${runId}/media`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    event_id: "trial12_video",
    content_type: recorder.mimeType,
    filename: "trial12_video.webm"
  })
}).then(r => r.json());

let part = 1;
recorder.ondataavailable = (e) => queue.push({ blob: e.data, part: part++ });
recorder.onstop = () => queue.finish(part - 1);
```

**A task needs an upload queue, and this isn't optional.** Two reasons, and they compound:
`recorder.mimeType` isn't reliably populated until after `recorder.start()`, so the recorder
is already running — and possibly already firing `ondataavailable` — while the start request
is in flight. And every part has to be held until its own request succeeds, because a retry
must resend the same bytes.

That's a real piece of code, maybe fifteen lines, and it's the same fifteen lines in every
task. Asking each researcher to get it right is the "not seasoned programmers" problem in
miniature, so **we should ship a small helper alongside the example tasks**, not just document
the pattern.

### Pig does not care what the bytes are

No allowlist of media types, no refusing an unfamiliar codec, no deriving a file extension, no
checking that parts agree with each other. Pig doesn't interpret event `data`; there's no
reason it should interpret a media type.

*Doesn't care* is not *doesn't record*, though. The declared `content_type` is stored verbatim
because it's what tells someone later how to read a damaged recording — see below. It's an
opaque string, held the same way `data` is held.

### Responses

```json
{
  "status": "in_progress",
  "media": {
    "media_id": 1,
    "stored": [1, 2, 3, 4, 5],
    "finished": false
  }
}
```

`stored` is the same idea as `stored` on events, and means the same thing: every part Pig holds
for this recording, not just the one this request carried. A task compares it against what it
sent and resends anything absent.

Pig deliberately does **not** report parts as *missing*. To call a part missing, Pig would have
to decide what was expected — inferring intent from the highest part number it has seen and
assuming parts are contiguous. It has no idea how many parts a task means to send, any more
than it knows how many events. What's on disk is a fact; what was intended is the task's
business. This is also why the response doesn't need to be a summary: 360 parts is about 1.6 KB
of JSON, returned on a request that carried a megabyte.

### One part per request

The body is raw bytes, so there's no batching — one part, one request. That's the right
granularity rather than a limitation. Batching exists for events because a few hundred bytes
per event makes request overhead dominate; a media part is around a megabyte arriving every few
seconds, so the overhead is already noise.

It's also more reliable in the case that looks most like it wants batching: an offline task
that stores a whole run locally and uploads when it gets a connection. 360 independently
retryable requests beat a 20 MB batch that fails as a unit.

| Code | Means |
| --- | --- |
| `201 Created` | Stored, and this request is what stored it. |
| `200 OK` | Pig already had this part, byte-identical. A successful retry. |
| `422 Unprocessable Entity` | Same part number, different bytes. `can_retry: false`. |
| `404 Not Found` | No such run, no such recording, or the run belongs to a different task. |
| `409 Conflict` | The run isn't accepting data any more. Same rule as events. |
| `413 Payload Too Large` | Over `max_part_size`, or over the run's media cap. |

Pig does not police ordering or contiguity on arrival. Part 38 landing before part 12 is
normal HTTP behaviour, not an error.

`GET /task/{task_code}/run/{run_id}` grows a `media` section with the same summary per
recording. No separate endpoint for checking on media.

## Pig never concatenates the parts

Parts are stored as parts, forever. Pig does not assemble them, at finalize or ever.

The reason is that assembling means deciding what to do about a gap, and Pig is not qualified
to make that call (see below). Writing out a file that looks whole and isn't is exactly the
failure this project exists to avoid. Keeping the parts as parts also keeps `sweep` cheap —
no large rewrite, no transient doubling of disk — and makes every part immutable the moment
it's written, which is what an offsite copy wants.

The assembly step downstream is `cat` in order, for both WebM and fragmented MP4. The manifest
records the parts in order with a hash each, so whoever assembles can check their work.

## What a missing part actually costs

Worth writing down because it drives what we tell task authors, not what we build. It depends
on both the container and the codec, and the two do different jobs.

**The container decides whether a demuxer can find its footing again.**

| Container | Init lives in | Resync |
| --- | --- | --- |
| WebM / Matroska (Chrome, Firefox) | part 1 (EBML header + Tracks) | Clusters start with a known ID and an absolute timecode. Scannable, timeline stays correct. |
| Fragmented MP4 (Safari) | part 1 (`ftyp` + `moov`) | `moof`/`mdat` pairs with explicit lengths, a sequence number, and absolute decode time. |
| Ogg (Firefox, audio) | part 1 (header pages) | Capture pattern and granule position per page. The most forgiving of the three. |

MediaRecorder's MP4 is always *fragmented* — a normal MP4 with `moov` at the end would be
unrecoverable from a partial write. So "it's an mp4" doesn't mean it behaves like the mp4s on
your disk.

**Losing part 1 is fatal in all three**, and that doesn't vary. No header means no codec
information, and the file isn't recoverable-with-artifacts, it's unidentifiable.

**The codec decides how much decoded output is garbage after the resync**, and our two tasks
differ sharply here:

- **Video** (VP8, VP9, H.264) needs a keyframe to restart. Damage extends past the missing
  bytes by up to a full GOP, and the keyframe interval is the browser's business.
- **Audio** (Opus, AAC) recovers almost immediately — Opus frames are ~20 ms and effectively
  self-contained.

So the audio task's worst case is "a few seconds are missing." The video task's is "a few
seconds are missing and a few more are unwatchable." Both are recoverable-with-a-hole; neither
kills the rest of the recording.

Because the severity is genuinely tool- and alignment-dependent, **Pig should not render a
verdict.** The manifest records which parts are present and how many the task said there would
be, and a hole is evident from those two facts without Pig having to characterise it. Whoever
holds the data decides whether recovery is worth attempting. That's the same instinct as
keeping both lines when two events share an ID.

**To do before writing the docs:** record 60 seconds with a 5-second timeslice on Chrome
(WebM), Safari (fMP4), and Firefox (audio); drop a middle part; try the concatenation in a
browser, in VLC, and through an `ffmpeg -c copy` remux. An afternoon, and it tells us what the
documentation should actually promise instead of what we think it should.

We should also write example tasks for both the continuous-video and discrete-audio shapes,
including the retry pattern. This is the part most likely to be got wrong, and prose won't fix
it on its own.

## Storage

Assumes the run-directory layout in #3. Within a run's directory, each recording gets a
directory named for its media ID:

```
media/00001/000001.part
            000002.part
```

Parts carry no extension beyond `.part`, because Pig doesn't parse the content type and a part
isn't a playable file anyway.

The manifest carries, per recording: the media ID, the event ID, the declared content type, the
suggested filename, the task's `metadata` verbatim, the parts in order with size and sha256
each, and whether the recording was finished and with what declared count.

During the run, parts live beside the in-progress dataset under the same structure.

## Configuration

Two new per-task settings, neither optional:

- `max_part_size` — per part.
- `max_media_bytes` — per run.

Media is the first thing in Pig that can fill a volume. Fifty participants × 30 minutes of 720p
is tens of gigabytes, which makes free-space monitoring in `/health` real work rather than a
nicety.

What the values should be depends partly on what happens in front of Pig — see below.

## Where the bytes actually go

Worth working out before picking numbers, because the two web servers people will put in front
of Pig behave oppositely by default.

**nginx buffers the whole request body before Pig sees any of it.**
`proxy_request_buffering` is on by default: nginx reads the entire request, keeps up to
`client_body_buffer_size` (8–16 KB) in memory, and spills the rest to a temp file under
`client_body_temp_path`, then replays it to the upstream. So a 5 MB part lands on nginx's disk
first. Not a memory problem, but temp space scales with concurrency × part size, and Pig
doesn't see the request until the upload has fully arrived.

The default that will bite everyone: **`client_max_body_size` is 1 MB**, so out of the box
nginx rejects any real media part with a 413 that Pig never hears about.

**Caddy streams by default.** `reverse_proxy` passes the body through as it arrives, with
buffering opt-in via `request_buffers`. Caddy holds almost nothing and Pig sees bytes
immediately, but a slow client then occupies a connection to Pig for the whole upload instead
of being absorbed by the proxy. That's affordable because uvicorn is async — it would not be
behind a thread-per-request server. (Worth checking against current Caddy docs; their buffering
defaults have moved between versions.)

**The memory question is mostly ours, though.** `await request.body()` in Starlette reads the
entire body into memory as one `bytes` object, so a 50 MB part is 50 MB of RSS per concurrent
request no matter how well the proxy behaved. `request.stream()` gives an async iterator
instead.

So: **stream each part to disk, never materialise it in memory.** Write to `.partial`, fsync,
rename — the same pattern `file_dataset` already uses.

And **the size cap has to be enforced during the stream**, counting bytes as they arrive.
`Content-Length` can be absent under chunked encoding and can't be trusted when it's present,
so it's useful only as an early reject.

### What that means for the numbers

Four things push toward modest parts, and only one pushes back:

- nginx temp disk scales with concurrency × part size.
- A failed retry re-sends the whole part, so a 50 MB part costs 50 MB again on a bad
  connection.
- Bigger parts mean longer before Pig sees the request at all, which interacts with proxy
  timeouts.
- Against: smaller parts mean more requests and more fsyncs — real, but weak at our traffic.

A 5-second timeslice at typical 720p webcam bitrates produces roughly 1–2 MB per part, so a
`max_part_size` default around 8 MB gives generous headroom without making any of the above
hurt. The audio case isn't close to it — a 30-second Opus answer is tens of kilobytes.

Two follow-ons for deployment:

- `client_max_body_size` (or Caddy's `request_body max_size`) has to be at least
  `max_part_size`, and this belongs in `deployment.md` with a config snippet. `pig check` could
  reasonably print the value a proxy needs to allow, since nobody will remember.
- The proxy's temp directory becomes a thing worth watching alongside the data root.

## Why not pre-signed URLs

The standard web answer to large uploads is for the server to mint a pre-signed URL and have
the browser upload straight to object storage. Worth saying why not, since an experienced web
developer will suggest it:

- It would make Pig maximally concerned with the mechanics of talking to S3 — bucket
  credentials in the configuration, provider-specific signing, CORS on the bucket, provider
  errors in the request path. Pig's position is that it supplies what a transfer needs and
  otherwise stays out of it.
- It breaks the durability promise. Pig couldn't say "if the server says it's stored, it's on
  disk" — it would be relaying an unverified claim about someone else's storage.
- It breaks the laptop-in-an-afternoon test. You'd need a bucket before you could try Pig.

## Security and #2

An unauthenticated endpoint accepting multi-megabyte binaries is a different proposition from
one accepting ~1 KB of trial data. It doesn't change the permissive-by-default stance in
`security.md`, but it probably makes per-task byte caps required rather than advisory, and it's
a second argument for the task-scoped authorization key #2 already gestures at.

It also changes the shape of #2. Purge as drafted there is one run key plus one sha256, because
a run is one file. A run with media is a set of files, so the confirmation has to cover the set
— either a hash per file, or the manifest's hash with the manifest covering the rest. The
second is tidier and falls out of the manifest already existing.

## Open questions

- **A single-shot form for one-part recordings.** The audio task is roughly 40 prompts, each
  producing one blob, which is three requests apiece under the shape above. A way to say "here
  is the whole recording, metadata and all, in one call" is clearly worth having. The trouble is
  finding a URL shape that isn't sly: distinguishing "start a recording" from "here is a whole
  recording" by whether the body is JSON or binary is exactly the cleverness we avoid, and
  metadata doesn't fit comfortably in query parameters once `metadata` is a free-form object.
  Worth noting that if we ship the client helper, the three-call cost is invisible to the task
  author anyway, which may make this unnecessary. Undecided, and I'd rather decide it with an
  example task in front of us. (Nate's suggestion: Handle this with on the client end for now.)
- **Whether `filename` is validated at ingest.** Pig never uses it, so there's an argument for
  storing it verbatim and letting whoever assembles the file worry about it. But "checking and
  errors are better than silent fixes" argues for refusing an unsafe one at the boundary, where
  the task author sees the error while developing rather than an export tool hitting it two
  years later. Leaning towards validating — safe value plus an extension. (Nate's answer: No. Filename isn't even required. This gets validated by the consumer. Honestly,  this, along with everything other than `event_id`, probably belong in `metadata`.)
- **Does finalize care about unfinished recordings?** Probably not — a truncated recording is a
  fact to record accurately, not a failure to refuse. (Nate: Agreed.)
- **Whether a run's media caps are enforced per part or cumulatively**, and what a task is told
  when it hits one mid-recording. Refusing a part is refusing data we can't get back. (Nate: Per-part, this is really "the server can't handle this part")
- **Per-part timestamps.** They don't give AV sync (that comes from an ordinary event logging
  "recording started, task clock = 12345"), so they'd be diagnostic only. Worth the field? (Nate: No.)
- **Resumability across a page reload.** Out of scope for a first version, but the shape should
  not preclude it — a task that reloads can query `GET .../run/{run_id}` and see what Pig holds. (Nate: If you hold on to your run and event ids and such in local storage, I think you can resume today.)
- **Part size guidance.** Smaller timeslice means less lost when a tab dies, more requests.
  Something around 5 seconds is probably the right default recommendation, but that belongs in
  the example tasks. (Nate: Also in a client library.)
