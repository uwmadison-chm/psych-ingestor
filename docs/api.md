# The Psych Ingestor API

**Status: draft, and not built yet.** This describes what PI will do, so you can write a
task against it. You can't test against it today. What's still moving is listed at the
bottom of this page — if you're depending on one of those, ask before you build on it.

This page is for people writing tasks — in jsPsych, PsychoPy, plain JavaScript, or a
standalone app. It describes the requests your task makes to Psych Ingestor (PI) and what
comes back.

You need three things to use PI: start a run, send events as they happen, and say when
you're done.

A **run** is one participant doing your task one time. It's what PI gives you an ID for and
what holds your data. (PI borrows its words from BIDS: a participant comes in for a
*session*, does a *task*, and each time they do it is a *run*. See
[definitions.md](definitions.md).)

## What you'll need before you start

- **The address of your lab's PI server.** Something like `https://pi.yourlab.edu`. Every
  example below uses that; replace it with yours.
- **Your task code.** A short name for your task, set when someone adds it to PI's
  configuration — `stroop`, `balloons`, `dd_game`. It appears in every URL your task calls,
  after `/task/`.
- **The list of link parameters PI expects for your task**, like `participant_id` and
  `session`. This is also set in configuration, and PI will reject a run that doesn't
  match.

Some of those parameters become directory and file names, so their values are restricted:
letters, digits, underscore, and dash, up to 64 characters, and no dash at the start.
Anything else — a space, a dot, an accented letter, an empty value — and PI refuses to
start the run rather than guessing at what you meant. Worth knowing when you're deciding
what to put in participant links.

Case doesn't matter. `?session=Baseline` and `?session=baseline` land in the same place, so
you don't have to be careful about it. PI remembers what your link actually said and
reports it back; it only lowercases the folder name.

## The steps in a run

1. A participant follows a link to your task. The link carries parameters that identify
   them — something like `?participant_id=10351&session=baseline`. Which parameters PI
   expects is set per task in your configuration.
2. Your task tells PI a run is starting, and PI replies with a run ID.
3. As the participant works, your task sends events. Each event is one JSON object — a
   trial, a response, an image presentation, whatever you record. You can send them one at
   a time or in batches. You assign each event an **event ID** that's unique within the
   run.
4. PI replies with the event IDs it has stored, so you know which ones you don't need to
   send again.
5. When the participant finishes, your task tells PI to finalize the run. That marks the
   data as whole and hands it to PI to file with the completed data.

If step 5 never happens — the participant closes the tab, the laptop dies — the data you
already sent is still saved.

A run can also happen entirely offline. In a phone app or a PWA, a participant might do the
whole task with no network at all, storing events locally; when the device gets a
connection, the app starts a run and sends everything at once. PI doesn't care how much
time passed.

## A complete example

Starting a run for participant 10351 at their baseline visit, sending two events, and
finishing:

```javascript
const PI = "https://pi.yourlab.edu";
const TASK = "stroop";

// 1. Start the run.
const startResponse = await fetch(`${PI}/task/${TASK}/run`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    participant_id: "10351",
    session: "baseline"
  })
});
const { run_id } = await startResponse.json();

// 2. Send some events. The keys are your event IDs.
await fetch(`${PI}/task/${TASK}/run/${run_id}`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    "1": {
      timestamp: "2026-07-26T18:25:43.511-05:00",
      data: { type: "task_start", ts: 0 }
    },
    "2": {
      timestamp: "2026-07-26T18:25:47.204-05:00",
      data: { type: "trial", word: "GREEN", ink: "red", rt: 843, correct: true }
    }
  })
});

// 3. Tell PI you're done.
await fetch(`${PI}/task/${TASK}/run/${run_id}/finalize`, { method: "POST" });
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

PI checks them against what your task's configuration expects, and refuses the run if they
don't match.

You get back the run ID to use in every later request, and the run number — which time this
is for this participant and session:

```json
{
  "run_id": "9f1c4a72-3e58-4b0d-8a16-2d7e5c93f4b1",
  "run_number": 2
}
```

Use `run_id` in every later request. It's a long random string, and it's the only thing PI
needs to find your run — the two numbers are for humans.

`run_number` tells you which time this is for this participant and session. Starting the
task again with the same link always gives you a new run with a new number, so a
participant who reloads partway through never overwrites their earlier data.

## Sending events

### `POST /task/{task_code}/run/{run_id}`

The body is a JSON object whose keys are your event IDs:

```json
{
  "1": {
    "timestamp": "2026-07-26T18:25:43.511-05:00",
    "data": { "type": "task_start", "ts": 0 }
  },
  "2": {
    "timestamp": "2026-07-26T18:25:43.511-05:00",
    "data": { "type": "instructions", "ts": 0 }
  },
  "3": {
    "timestamp": "2026-07-26T18:26:03.29-05:00",
    "data": { "type": "get_ready", "ts": 19779 }
  }
}
```

**Event IDs** are yours to make up, and must be unique within the run. A counter starting
at 1 is fine. So is a UUID. PI uses the ID to make sure it stores each event exactly once,
so if a request fails — a dropped connection, or a bug that sends the same batch twice —
it's always safe to send it again.

Don't put participant information in an event ID. They aren't guaranteed to be private. Use
something made up.

**`timestamp`** is optional. PI uses it to sort the dataset when it files the completed
data, sorting on timestamp and then event ID — so if your event IDs count up, you can skip
timestamps entirely.

**`data`** is yours. PI does not look inside it and stores it unchanged.

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

`stored` lists every event ID PI holds for this run, including ones from earlier requests —
so it's the full picture, not just what this request added.

The status code tells you what happened:

| Code | Means |
| --- | --- |
| `201 Created` | Everything is stored, and this request is what stored at least one of them. |
| `200 OK` | Everything is stored, and PI already had all of it. This is what a successful retry looks like. |
| `422 Unprocessable Entity` | At least one event wasn't stored. Read `errors`. |

You get the same JSON either way, so you can always read `stored` and `errors` to decide
what to resend. `can_retry` tells you whether sending it again could work; if it's `false`,
something about the event itself is wrong and resending won't help.

### Sending the same event twice

Resending is safe, and it's the expected response to a failed request. What PI does depends
on whether the event is really the same one:

- **Same event ID, same content** — PI already has it. Nothing is written, the ID appears
  in `stored`, and you get `200 OK`. This is a retry, and it works exactly as if the first
  request had succeeded.
- **Same event ID, different content** — PI refuses the event and puts it in `errors` with
  `can_retry: false`. It keeps what it already had; the new version is not written.

The second case means two different events were given the same ID, which is a bug in the
task rather than a network problem. Sending it again won't help, and PI won't guess at
which version you meant. The error says what PI already had for that ID so you can work out
where the counter went wrong.

One thing to watch for when you write a retry: **build each event once and resend that same
object.** If your retry code rebuilds the event and re-reads the clock for `timestamp`, the
content changes and PI will treat it as a collision — a spurious error for what was really
just a retry. Keep the event you failed to send, and send that.

### When it doesn't work

**The run isn't accepting data any more** — you finalized it, or it was abandoned. You get
`409 Conflict` or `423 Locked`, with the usual JSON; `status` says which state it's in, and
everything PI wouldn't take is in `errors`.

**The run doesn't exist** — `404 Not Found`, with the usual JSON. Don't expect anything in
`stored`. You'll also get a `404` if the run ID is real but belongs to a different task
than the one in the URL, which usually means a copy-paste between two tasks.

**Your request was too big.** There's a size limit per event (set in your task's
configuration, 1M by default), and the web server in front of PI has its own limit for a
whole request. Too large gets you `413 Payload Too Large` or a `500`-series error, and PI
can't control the message in that case. If you're batching, batch modestly.

## Finishing a run

### `POST /task/{task_code}/run/{run_id}/finalize`

Closes the run to further data and hands the dataset to PI to file.

```json
{
  "status": "finalizing",
  "stored": ["1", "2", "3"]
}
```

**Your task is done at this point.** `finalizing` means every event you sent is on disk and
no more will be accepted — tell the participant they're finished and close the tab. PI's
remaining work (sorting the dataset, moving it to completed storage, copying it elsewhere)
happens without you, and the run becomes `complete` when it's done. Don't wait for it;
nothing your task can do would change the outcome.

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
| `finalizing` | It isn't. PI is filing the dataset. |
| `complete` | PI is finished with it. |
| `abandoned` | The run was never finalized, and PI stopped waiting. |

If the run ID doesn't exist, you get `404 Not Found`.

## Cross-origin requests

Your task will almost never be hosted on the same server as PI, so every request it makes
is a cross-origin one. PI's default is to allow them from anywhere, so this should just
work. If your lab has restricted a task to specific sites, that's set in the task's
configuration. See [security.md](security.md).

## Things that will change

Nothing here should stop you writing a task, but these will grow:

- **`finalize` may come to report the number of events it stored**, so your task can check
  it against its own count before telling the participant they're done. Whether a finalized
  run can be reopened is also unsettled. Neither changes what you write today.
- **A task may eventually cap how many times a participant can run it** (`max_runs`).
  Today there's no limit, and every start gets its own run. If your task depends on being
  able to restart, that keeps working; if you'd like it capped, that's coming.
- **PI will send settings back when a run starts** — condition assignment, a stimulus set,
  a block order — so you can change a task's behavior without redeploying it. Planned; the
  shape isn't settled.
- **The error for a repeated event ID will tell you about the version PI already has.** It
  will tell you something; the exact shape isn't settled.

Two things you can rely on that you might expect to be in flux:

- **Extra link parameters are fine.** PI ignores parameters it doesn't know about — a
  `utm_source` or a leftover `debug=1` won't stop a run from starting. Only the parameters
  your task's configuration names are used to identify the run.
- **PI has no notion of a study.** Tasks are the top level. Your task code is the whole
  address; there's no project or study to nest it under.
