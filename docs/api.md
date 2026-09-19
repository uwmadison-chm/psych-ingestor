# The Psych Ingestor API

**Status: early. Everything on this page works, and some of it will change.** There's a
server you can point a task at — see [trying_it.md](trying_it.md) to run one. What's still
moving is listed at the bottom of this page; if you're depending on one of those, ask
before you build on it.

This page is for people writing tasks — in jsPsych, PsychoPy, plain JavaScript, or a
standalone app. It describes the requests your task makes to Psych Ingestor (Pig) and what
comes back.

You need three things to use Pig: start a run, send events as they happen, and say when
you're done.

A **run** is one participant doing your task one time. It's what Pig gives you an ID for and
what holds your data. (Pig borrows its words from BIDS: a participant comes in for a
*session*, does a *task*, and each time they do it is a *run*. See
[definitions.md](definitions.md).)

## What you'll need before you start

- **The address of your lab's Pig server.** Something like `https://pig.yourlab.edu`. Every
  example below uses that; replace it with yours.
- **Your task code.** A short name for your task, set when someone adds it to Pig's
  configuration — `stroop`, `balloons`, `dd_game`. It appears in every URL your task calls,
  after `/task/`.
- **The list of link parameters Pig expects for your task**, like `participant_id` and
  `session`. This is also set in configuration, and Pig will reject a run that doesn't
  match.

Those parameters will eventually become directory and file names, so their values are
restricted: letters, digits, underscore, and dash, up to 64 characters, and no dash at the
start. Anything else — a space, a dot, an accented letter, an empty value — and Pig
refuses to start the run rather than guessing at what you meant. Worth knowing when you're
deciding what to put in participant links.

Case matters. `?session=Baseline` and `?session=baseline` are two different sessions, and
Pig keeps exactly what your link said. If your links come from more than one place, make
sure they agree on spelling.

## The steps in a run

1. A participant follows a link to your task. The link carries parameters that identify
   them — something like `?participant_id=10351&session=baseline`. Which parameters Pig
   expects is set per task in your configuration.
2. Your task tells Pig a run is starting, and Pig replies with a run ID.
3. As the participant works, your task sends events. Each event is one JSON object — a
   trial, a response, an image presentation, whatever you record. You can send them one at
   a time or in batches. You assign each event an **event ID** that's unique within the
   run.
4. Pig replies with the event IDs it has stored, so you know which ones you don't need to
   send again.
5. When the participant finishes, your task tells Pig to finalize the run. That marks the
   data as whole; Pig finishes up on its own from there.

If step 5 never happens — the participant closes the tab, the laptop dies — the data you
already sent is still saved. Pig closes the run on its own after a while (24 hours unless
whoever configured your task chose otherwise) and keeps what it received. See
[When a run expires](#when-a-run-expires), especially if your task has no natural end.

A run can also happen entirely offline. In a phone app or a PWA, a participant might do the
whole task with no network at all, storing events locally; when the device gets a
connection, the app starts a run and sends everything at once. Pig doesn't care how much
time passed.

## A complete example

Starting a run for participant 10351 at their baseline visit, sending two events, and
finishing:

```javascript
const PIG = "https://pig.yourlab.edu";
const TASK = "stroop";

// 1. Start the run.
const startResponse = await fetch(`${PIG}/task/${TASK}/run`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    participant_id: "10351",
    session: "baseline"
  })
});
const { run_id } = await startResponse.json();

// 2. Send some events. The keys are your event IDs.
await fetch(`${PIG}/task/${TASK}/run/${run_id}`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    "1": {
      data: { type: "task_start", time: "2026-07-26T18:25:43.511-05:00" }
    },
    "2": {
      data: { type: "trial", time: "2026-07-26T18:25:47.204-05:00",
              word: "GREEN", ink: "red", rt: 843, correct: true }
    }
  })
});

// 3. Tell Pig you're done.
await fetch(`${PIG}/task/${TASK}/run/${run_id}/finalize`, { method: "POST" });
```

## Starting a run

### `POST /task/{task_code}/run`

Send the link parameters as JSON:

```json
{
  "participant_id": "10351",
  "session": "baseline"
}
```

Pig checks them against what your task's configuration expects, and refuses the run if they
don't match.

You get back the run ID to use in every later request, and the run number — which time this
is for this participant and session:

```json
{
  "run_id": "9f1c4a72-3e58-4b0d-8a16-2d7e5c93f4b1",
  "run_number": 2
}
```

Use `run_id` in every later request. It's a long random string, and it's the only thing Pig
needs to find your run — the two numbers are for humans.

`run_number` tells you which time this is for this participant and session. Starting the
task again with the same link always gives you a new run with a new number, so a
participant who reloads partway through never overwrites their earlier data.

## Sending events

### `POST /task/{task_code}/run/{run_id}`

The body is a JSON object whose keys are your event IDs:

```json
{
  "1": { "data": { "type": "task_start", "time": "2026-07-26T18:25:43.511-05:00" } },
  "2": { "data": { "type": "instructions", "time": "2026-07-26T18:25:43.511-05:00" } },
  "3": { "data": { "type": "get_ready", "time": "2026-07-26T18:26:03.29-05:00" } }
}
```

**Event IDs** are yours to make up, and must be unique within the run. A counter starting
at 1 is fine. So is a UUID. Pig uses the ID to make sure it stores each event exactly once,
so if a request fails — a dropped connection, or a bug that sends the same batch twice —
it's always safe to send it again.

Don't put participant information in an event ID. They aren't guaranteed to be private. Use
something made up.

**`data`** is yours. Pig does not look inside it and stores it unchanged. Everything you
want to keep goes in there — a timestamp, a trial number, whatever your task records —
and `data` is the only field an event has. If an event has anything else next to `data`,
Pig refuses that event and says so, because otherwise it would have to drop the extra
field silently and you'd never know. (Earlier versions of Pig took a `timestamp` next to
`data`. If your task still sends one, move it inside.)

Pig stores events in the order they arrive and never reorders them. If you want your data
in time order, record a timestamp in `data` and sort on it when you analyze.

The reply tells you what's stored:

```json
{
  "status": "in_progress",
  "stored": ["1", "2", "3"],
  "errors": {
    "4": {
      "message": "What went wrong with this event",
      "can_retry": true
    }
  }
}
```

`stored` lists every event ID Pig holds for this run, including ones from earlier requests —
so it's the full picture, not just what this request added.

The status code tells you what happened:

| Code | Means |
| --- | --- |
| `201 Created` | Everything is stored, and this request is what stored at least one of them. |
| `200 OK` | Everything is stored, and Pig already had all of it. This is what a successful retry looks like. |
| `422 Unprocessable Entity` | At least one event wasn't stored. Read `errors`. |

You get the same JSON either way, so you can always read `stored` and `errors` to decide
what to resend. `can_retry` tells you whether the event itself is fine. If it's `false`,
something about the event is wrong and resending won't help. If it's `true`, the event is
fine but this run wouldn't take it — usually because the run has closed, in which case the
message says to send it to a new one.

### Sending the same event twice

Resending is safe, and it's the expected response to a failed request. What Pig does depends
on whether the event is really the same one:

- **Same event ID, same content** — Pig already has it. Nothing is written, the ID appears
  in `stored`, and you get `200 OK`. This is a retry, and it works exactly as if the first
  request had succeeded.
- **Same event ID, different content** — Pig refuses the event and puts it in `errors` with
  `can_retry: false`. It keeps what it already had; the new version is not written.

The second case means two different events were given the same ID, which is a bug in the
task rather than a network problem. Sending it again won't help, and Pig won't guess at
which version you meant. The error says what Pig already had for that ID so you can work out
where the counter went wrong.

One thing to watch for when you write a retry: **build each event once and resend that same
object.** If your retry code rebuilds the event and re-reads the clock for a timestamp in
`data`, the content changes and Pig will treat it as a collision — a spurious error for
what was really just a retry. Keep the event you failed to send, and send that.

### When it doesn't work

**The run isn't accepting data any more** — you finalized it, or it expired. You get
`409 Conflict` with the usual JSON: `status` says which, and every event Pig wouldn't take
is in `errors` with a message saying what to do. Nothing is wrong with the events; start a
new run and send them there. See [When a run expires](#when-a-run-expires).

**The run doesn't exist** — `404 Not Found`, with the usual JSON. Don't expect anything in
`stored`. You'll also get a `404` if the run ID is real but belongs to a different task
than the one in the URL, which usually means a copy-paste between two tasks.

**Your request was too big.** There's a size limit per event (set in your task's
configuration, 1M by default), and the web server in front of Pig has its own limit for a
whole request. Too large gets you `413 Payload Too Large` or a `500`-series error, and Pig
can't control the message in that case. If you're batching, batch modestly.

## Finishing a run

### `POST /task/{task_code}/run/{run_id}/finalize`

Closes the run to further data.

```json
{
  "status": "finalizing",
  "stored": ["1", "2", "3"]
}
```

**Your task is done at this point.** `finalizing` means every event you sent is on disk and
no more will be accepted — tell the participant they're finished and close the tab. Pig's
remaining work (describing the run and moving it to finished storage) happens without
you, and the run becomes `complete` when it's done. Don't wait for it; nothing your task
can do would change the outcome.

## When a run expires

A run doesn't stay open forever. Each task has a time limit, counted from when the run
started — 24 hours unless whoever configured your task set something else — and once a run
has been open that long, Pig closes it and keeps the data it received. The run's status
becomes `expired`.

For most tasks this never comes up: the participant finishes in an hour and you finalize.
It matters when your task has no natural end — a game people play for as long as they like,
say. For a task like that, expiring is the normal way a run ends. Nothing about it is an
error, and nothing you sent is lost.

What you see is the reply to your next request:

```json
{
  "status": "expired",
  "stored": ["1", "2", "3"],
  "errors": {
    "4": {
      "message": "This run has expired, so it isn't taking events. Start a new run for this participant and send these events to it.",
      "can_retry": true
    }
  }
}
```

with status code `409 Conflict`. `stored` is everything Pig kept, and `errors` lists what it
didn't take. Do what the message says: start a new run with the same link parameters, and
send the refused events to the new run ID. Pig gives the new run the next run number, so
nothing is overwritten and the two runs sit side by side in your data.

If your task expects this to happen, hold on to each event until you've seen its ID in
`stored`, so you have it to resend. That's the same thing you'd do for a failed request.
Finalizing an expired run gets the same `409`; there's nothing to finalize, and everything
it received is already saved.

## Checking on a run

### `GET /task/{task_code}/run/{run_id}`

```json
{
  "status": "in_progress",
  "stored": ["1", "2", "3"]
}
```

`status` is one of:

| Status | Means |
| --- | --- |
| `in_progress` | The run is still taking events. |
| `finalizing` | It isn't. Pig is finishing up. |
| `complete` | Pig is finished with it. |
| `expired` | The run was open as long as the task allows, and Pig closed it. |

If the run ID doesn't exist, you get `404 Not Found`.

If your task is set up to take media, the reply also has a `media` list, one entry per
media item in the run, in the same shape the media requests below reply with.

## Sending audio, video, and other files

*Only for tasks whose configuration says `media = true`. Any other task gets `404` from
these requests, with a message saying so.*

Events are for trial data, a few hundred bytes at a time. A recording is megabytes, and
putting it in an event as text would make your data file unreadable and hit the size
limit. So Pig takes audio, video, images, and any other file as a **media item**, in
three steps that mirror a run: start it, send it in parts, say when you're done.

**A media item is an event with bytes attached.** You start one by sending an ordinary
event, with an event ID and `data` like any other, and Pig gives it a **media ID**. The
event is stored in your data file with everything else you record, so whoever reads the
data later finds the recording in sequence with the trial it belongs to. The bytes go
next to the data file, in a directory named for the media ID.

### Starting a media item

#### `POST /task/{task_code}/run/{run_id}/media`

```json
{
  "event_id": "prompt3_audio",
  "data": { "content_type": "audio/webm;codecs=opus", "prompt": 3 }
}
```

The same rules as any event: `event_id` is yours and unique within the run, `data` is
yours and Pig doesn't look inside it, and the same size limit applies. Pig defines no
fields for media. The content type, a filename you'd like the assembled recording to
have, which trial it belongs to: all of that goes in `data`, if you want it. Do record
the content type, though. It's what tells someone how to play a recording later, and
Pig has no other way of knowing.

You get back the media ID and the largest part this task will take:

```json
{ "media_id": 1, "max_part_size": 8388608 }
```

Media IDs count from 1 within the run. Sending the same start again is safe: same event
ID, same `data` gets `200 OK` and the same media ID back. Same event ID with different
`data` is refused, exactly as an event would be, with the ID in `errors` and
`can_retry: false`.

### Sending the parts

#### `PUT /task/{task_code}/run/{run_id}/media/{media_id}/{part}`

The body is the bytes, and nothing else: no JSON around them, no base64, no form
encoding. Send whatever your recorder hands you, as it hands it to you. `part` is a
number counting from 1, and it's required even if there's only one part. Parts can
arrive in any order.

```javascript
await fetch(`${PIG}/task/${TASK}/run/${runId}/media/${mediaId}/${part}`, {
  method: "PUT",
  body: blob
});
```

The part number is what makes a retry safe. Send the same part with the same bytes again
and you get `200 OK`; nothing is written twice. Send the same part number with different
bytes and Pig refuses it and keeps what it had, because two parts were given the same
number and that's a bug in the task. So hold on to each blob until you've seen its part
number in `stored`, and resend that same blob, never a rebuilt one.

Every reply tells you where the item stands:

```json
{
  "status": "in_progress",
  "media": {
    "media_id": 1,
    "event_id": "prompt3_audio",
    "stored": [1, 2, 3, 4, 5],
    "finished": false
  }
}
```

`stored` is every part Pig holds for this item, not just the one you sent, so compare it
against what you've sent and resend anything absent. Pig doesn't say what's *missing*,
because until you finish the item it has no idea how many parts you mean to send.

| Code | Means |
| --- | --- |
| `201 Created` | Stored, and this request is what stored it. |
| `200 OK` | Pig already had this part, byte for byte. A successful retry. |
| `422 Unprocessable Entity` | Same part number, different bytes. Read `errors`; `can_retry` is `false`. |
| `413 Payload Too Large` | The part is bigger than this task allows. Send smaller parts. |
| `409 Conflict` | The run has closed, or you already finished this item. |
| `404 Not Found` | No such run, no such media item, or the run belongs to another task. |

Pig writes each part to disk as it arrives and doesn't hold it in memory, so the size
limit is about your deployment, not Pig. Whoever runs your Pig sets `max_part_size`,
8 MB unless they chose otherwise, and the web server in front of Pig has to allow bodies
at least that big. A part a few seconds long is well under that at normal recording
settings.

### Finishing a media item

#### `POST /task/{task_code}/run/{run_id}/media/{media_id}/finish`

```json
{ "parts": 37 }
```

Tell Pig how many parts you sent. Pig checks that it holds exactly parts 1 through 37,
and if it does, the item is finished and takes no more parts:

```json
{
  "status": "in_progress",
  "media": { "media_id": 1, "event_id": "prompt3_audio", "stored": [1, 2, 3], "finished": true, "parts": 3 }
}
```

If a part is missing, you get `422` and the message says which. Send the missing parts,
then finish again. If Pig holds parts *beyond* your count, that's also `422`, with
`can_retry: false`: the count your task sent is wrong, and Pig kept the parts.

The count is what makes a finished item mean something. `finished: true` says the task
vouched for the recording and Pig holds all of it, the same way `complete` says that for
a run. An item you never finish is still kept, every part of it, and marked as never
finished. Finalizing the run doesn't mind an unfinished item; a recording cut off when
the participant closed the tab is a fact to record, not an error.

Finishing twice with the same count is `200 OK`. Finishing with a different count is
refused.

### Putting it together

Your task needs a small upload queue, and this isn't optional. Two things force it. A
browser's `MediaRecorder` starts handing you blobs as soon as it's running, possibly
before Pig has answered your start request, so blobs have to wait for the media ID. And
every blob has to be held until Pig confirms its part, because a retry must send the
same bytes.

The shape of it:

1. Start the recorder, with a timeslice so it hands you a blob every few seconds.
   Around five seconds is a good default: little is lost if the tab dies, and the parts
   stay small. Read `recorder.mimeType` *after* starting; browsers don't reliably fill
   it in until then.
2. Send the start request. Blobs that arrive meanwhile wait in the queue.
3. Number each blob as it arrives, from 1, in order. Send them one at a time, and don't
   drop a blob until its number is in `stored`. On a failed request, send the same blob
   again.
4. When the recorder stops, wait for the queue to empty, then finish with the number of
   parts you sent.

A blob doesn't have to be one part. The recording is a stream of bytes and where you
cut it doesn't matter as long as the order is kept, so a blob bigger than
`max_part_size` can be sliced with `Blob.slice()` and sent as several consecutive parts.

A helper that does all of this, so each task doesn't repeat it, is planned but not
written yet.

**Pig never joins the parts back together.** They stay as parts, each hashed in the
run's manifest, and whoever works with the data later joins them: for the WebM and
fragmented MP4 that browsers record, that's concatenating the files in order. Pig
doesn't join them because joining means deciding what to do about a gap, and a file
that looks whole and isn't is exactly the kind of quiet mistake Pig exists to avoid.

Pig doesn't look at the bytes at all. No list of allowed types, no checking that parts
belong together, no file extension chosen for you. What you send is what's stored.

## Cross-origin requests

Your task will almost never be hosted on the same server as Pig, so every request it makes
is a cross-origin one. Pig's default is to allow them from anywhere, including the `PUT`
that media parts use, so this should just work. If your lab has restricted a task to specific sites, that's set in the task's
configuration. See [security.md](security.md).

## Things that will change

Nothing here should stop you writing a task, but these will grow:

- **`finalize` may come to report the number of events it stored**, so your task can check
  it against its own count before telling the participant they're done. It doesn't change
  what you write today.
- **A task may eventually cap how many times a participant can run it** (`max_runs`).
  Today there's no limit, and every start gets its own run. If your task depends on being
  able to restart, that keeps working; if you'd like it capped, that's coming.
- **Pig will send settings back when a run starts** — condition assignment, a stimulus set,
  a block order — so you can change a task's behavior without redeploying it. Planned; the
  shape isn't settled.
- **The error for a repeated event ID will tell you about the version Pig already has.** It
  will tell you something; the exact shape isn't settled.

Three things you can rely on that you might expect to be in flux:

- **A closed run stays closed.** Once a run is finalized or expired, nothing reopens it.
  If there's more to record, that's a new run with a new run number.
- **Extra link parameters are fine.** Pig ignores parameters it doesn't know about — a
  `utm_source` or a leftover `debug=1` won't stop a run from starting. Only the parameters
  your task's configuration names are used to identify the run.
- **Pig has no notion of a study.** Tasks are the top level. Your task code is the whole
  address; there's no project or study to nest it under.
