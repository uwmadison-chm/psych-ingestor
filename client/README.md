# The Psych Ingestor JavaScript client

**Status: early.** Starting runs, events, finalizing, offline runs, and resuming across
pages all work. Media and encryption aren't here yet. Names marked *provisional* below may
change. See [issue #11](https://github.com/uwmadison-chm/psych-ingestor/issues/11).

This is a small library for tasks that run in a web browser. It talks to Pig for you:
it starts a run, keeps every event safe on the participant's device until Pig confirms it
has it, sends events as soon as it can, and finalizes the run when you say so. If the
network drops partway through, nothing is lost; the events wait, and go when the network
comes back.

You can do all of this yourself with `fetch`, as [docs/api.md](../docs/api.md) shows. The
client is for when you'd rather not write the retrying and the bookkeeping.

## Getting the files

You need three files, kept together in the same directory on the site that hosts your
task:

- `pig.script.js`, for a plain `<script>` tag. It gives you a global called `pig`.
- `pig.js`, the same thing as an ES module, for `import` or a bundler.
- `pig-worker.js`, which both of the others start. It does the actual sending, in the
  background, so your task's own timing isn't disturbed.

Download them from the [latest release](https://github.com/uwmadison-chm/psych-ingestor/releases),
or build them yourself with `npm install` and `npm run build` in this directory, which puts
them in `dist/`.

The client is written in TypeScript, but these files are plain JavaScript; you don't need
TypeScript to use them. If your task is written in TypeScript, the build also puts type
declarations in `dist/types/`.

Host them yourself, next to your task, rather than linking to them from somewhere else.
Browsers only start a worker from the same site as the page.

## Recording a run

```html
<script src="pig.script.js"></script>
<script>
  async function main() {
    const run = await pig.start("https://pig.yourlab.edu", "stroop", {
      participant_id: "10351",
      session: "baseline",
    });

    // ... for each trial:
    run.add({ type: "trial", word: "GREEN", ink: "red", rt: 843 });

    await run.finalize();
    await run.sent();
    // Everything is on the server. Tell the participant they're done.
  }
  main();
</script>
```

Or, as a module:

```javascript
import * as pig from "./pig.js";
const run = await pig.start("https://pig.yourlab.edu", "stroop", parameters);
```

**`pig.start(server, task, parameters)`** checks the parameters against what your task
needs and starts a run. `parameters` is every name and value you want the run started
with, all strings. Pig records any it doesn't need. If Pig can be reached, the run is
started on the server before `start()` returns, so a closed task or a bad parameter fails
right there, where you can show the participant a message.

**`pig.startForURL(server, task, url)`** does the same with the parameters in a URL's
query string, every one of them. Most often, that's the address of the page the
participant followed a link to:

```javascript
const run = await pig.startForURL("https://pig.yourlab.edu", "stroop", window.location);
```

`url` can also be a `URL` or a string.

**`run.add(data)`** queues an event. `data` is any plain object that can be turned into
JSON. It returns straight away, with a promise. If you `await` the promise, you'll wait
until the event is stored on the device, which takes a few milliseconds. If you don't, your
task carries on at once. Either is fine; choose by whether your task is timing something.

**`run.finalize()`** says the run is finished. It's sent after every event you added
before it.

**`run.sent()`** waits until everything for this run has reached the server. Use it before
telling the participant they can close the tab. Offline, it waits until the device is back
online.

You don't number your events. The client gives each one an event ID, counting from 1.

## What the client adds to your data

Every event gets a `_client` field inside its data, with two times taken when you called
`add()`:

```json
{
  "type": "trial",
  "rt": 843,
  "_client": { "wall_time": "2026-10-02T14:03:11.482-05:00", "performance_now": 18234.5 }
}
```

`wall_time` is the participant's clock, with its UTC offset. `performance_now` is
`performance.now()`: milliseconds since the page loaded, from a clock that doesn't jump
when the computer's time changes. Use it for timing within a page. Don't name a field of
your own `_client`; `add()` will refuse it. *Provisional names.*

Every run also starts with an event the client sends itself, event `0`, describing the
browser: its user agent, languages, time zone, screen and window size, and the client's
version. Nothing in it identifies the participant. *Provisional.*

## When the network isn't there

A run can start without a network, as long as this device has started the same task
online at least once before. (The client needs the task's settings, and it saves them
each time it can reach Pig.) Offline, `start()` gives you a run whose `runId` is `null`;
the server run starts when the device is back online, and everything queued goes up then.

The client tries again with growing waits, up to a minute apart, and tries at once when
the browser says it's back online or the participant switches away from the tab.

Two limits worth knowing, both from browsers rather than from Pig:

- Events only send while a page using the client is open on the same site. Nothing sends
  in the background after the tab closes. The next time the participant opens any page
  that uses the client on the same site, it sends what was left.
- Safari deletes a site's stored data after seven days without a visit, unless the site
  is installed to the home screen. Events still queued then are lost.

## Tasks that span pages

A run stays open when the participant moves to another page, but the new page has to pick
it up. Keep the run's `id` somewhere that survives the page change, and pass it to
`pig.resume()`:

```javascript
// First page
const run = await pig.start(server, "sret", parameters);
sessionStorage.setItem("pigRun", run.id);

// Next page
const run = await pig.resume(sessionStorage.getItem("pigRun"));
```

A page that loads without calling `resume()` doesn't continue the old run. Calling
`start()` begins a new one.

## Tasks with no natural end

Some tasks, like a game a participant plays for as long as they like, never get to call
`finalize()`: the participant just closes the tab. Pig closes those runs itself once
they've been open as long as the task allows (`expires_after` in its configuration).

If you'd rather they were finalized, ask for it when you start the run:

```javascript
const run = await pig.start(server, "dd_game", parameters, { finalizeWhenAbandoned: true });
```

Then, the next time any page using the client opens on the same site, it sends whatever
the run had left and finalizes it. A run counts as abandoned once no page has it open and
it hasn't been touched for 30 seconds. If Pig had already closed the run by then, the
client leaves it closed.

There's no reliable way to finalize at the moment someone leaves. Browsers, phones
especially, often close a page without warning it. That's why this waits for the next
visit.

## Showing progress

```javascript
const { events, bytes, failed } = await run.pending();      // this run
const everything = await pig.pending();                     // every run on this device
const forTask = await pig.pending({ task: "stroop" });      // one task
```

`events` and `bytes` are what's still waiting to be sent. To update a progress display as
things are sent, listen for `progress`:

```javascript
pig.events.addEventListener("progress", (e) => show(e.detail.pending));
```

## Checking the browser first

```javascript
if (!(await pig.supported())) {
  // Ask the participant to use another browser.
}
```

`pig.supported()` says whether this browser has what the client needs, and logs what's
missing if it doesn't. The client needs a browser from about 2022 on: Safari 15.4, Chrome
93, Firefox 96, or later. The page has to be served over `https`. Some browsers turn off
storage in a private window; `supported()` checks for that too.

## When something goes wrong

Mistakes you can fix in your task come back from the call that made them: `start()` with a
closed task or a bad parameter, `add()` with an event that's too big or isn't an object. Each is
a `PigError` with a `code` saying which kind, and a `message` for people.

Problems that turn up later, while sending, are reported as `error` events, on the run and
on `pig.events`:

```javascript
run.addEventListener("error", (e) => console.warn(e.detail.code, e.detail.message));
```

If Pig refuses an event for good (because it's too big, or reuses an ID), the client keeps
it on the device rather than dropping it, counts it in `failed`, and carries on with the
rest. Call `pig.discardFailed()` to throw those away. *Provisional.*

If a run expires on the server while events are still waiting (a device offline for a
long time, say), the client starts a new run for the same participant and sends them
there. That run's first event names the run it continues. *Provisional.*

## Debugging

The client logs what it does to the browser's console, starting each line with
`[psych-ingestor]`. Routine things are logged with `console.debug`, which most browsers
hide unless you ask for "verbose" messages; problems are warnings and errors.

`pig.debugLog()` returns the client's recent log lines, for when a participant reports a
problem after the fact.

## A single file, if you need one

If you can't host `pig-worker.js` next to the other file, `pig.connect({ workerUrl })` takes
any URL for it, including one made with `URL.createObjectURL()` from the worker's code. A
strict Content Security Policy may block that. This isn't the usual way.

## Developing the client

```
npm install
npm run check        # type-check
npm test             # the queue and sender, in Node, against a pretend Pig
npm run build        # dist/
npm run e2e          # a real browser against a real `pig serve`
```

Node runs the TypeScript sources directly, without compiling them, so the code sticks to
TypeScript that only adds types: no enums, no namespaces. It needs Node 22.18 or later.
The browser tests start Pig with `uv run pig`; set `PIG_COMMAND` to run it some other way.
`src/core.ts` has the interesting part, and explains itself at the top.
