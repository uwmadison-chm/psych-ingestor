# A JavaScript client library: issue #11 as it stood before the rewrite

*Superseded by [issue #11](https://github.com/uwmadison-chm/psych-ingestor/issues/11),
rewritten October 2026. This is the text of that issue beforehand, followed by the
questions raised while turning it into a plan, with Nate's answers.*

Kept because the rewrite keeps the conclusions and drops the asking. In particular, the
reasoning behind dropping leftovers for finalized runs, and behind finalize-on-leave
being a beacon that abandons the queue, is only here.

---

Okay. We are ready for a proper javascript client. The basic outline of this lil guy:

- Making "the normal use case" work as easily as possible for task developers is a top priority.
- We should not expect that task devs will know or use a front-end build system (though they should be able to) so shipping a `<script>`-able build is good. pig developers can be expected to use a build system (vite, I am guessing.)
- At some future point, we may want to look into direct integration with jsPsych, psychopy, and other similar things. However, those would be separate from (likely used by) the main client.
- We should build this to accommodate encryption (see #10), parameter signing (see #15), and offline PWA mode. We will use these in the first deployment. It is _good_ if, as much as possible, the task author does not need to think about these things more than necessary.
- The normalest "I am recording data" flow is (I think) "get needed task parameters, start run, add events, finish"
- Task authors should be able to either hand off the "add event" thing so it returns _immediately_ (as near as possibly) or after it's durably (internally) added to the queue. Some tasks rely on sub-frame timing so leaving the main thread free is a high priority.
- To this end, we should probably use web workers for event processing, and indexeddb for queue storage.
- If we're encrypting data, we encrypt before adding to the queue, even if that means the window for data loss is longer. Again, task authors can decide if they want to wait for database commit.
- We should support per-task configuration, copied to a run, and then per-run queues.
- Task authors should be able to display a progress bar showing queue progress.
- The queue's contents are opaque to users. There's no looking back to see what was in previous events. Tasks need to track state themselves.
- We should default to including "things you would reasonably want to know" in run and event data -- things like user agent and timestamp
- We should also probably manage event id and media part id ourselves
- Maybe we should let users choose field names for those things (timestamp_field=<something>) and override those things by supplying their own values (lord knows what weird things people might want to do) -- or maybe we just say "timestamp will come over in a _timestamp field and that's just what it is. I could be convinced either way.
- Inspections that _are_ probably supported are things like "how many events are in my run's queue" and "how many events are in every run's queue for this task" and "how many total events do we have queued up across everything" along with size in bytes for those things -- tasks may very reasonably want to "send everything in all queues"
- For media events, the server reports a maximum size -- this client might report a "recommended maximum size" that's, say, 80% of the server's absolute max, to allow for things like encryption overhead. (Or maybe the client can just... take data and deal with making it fit?)
- When someone has offline-recorded data and gets connected while the code is running, we should try to send data.
- We probably want some reasonable backoff
- We don't consider data sent and cleared from the queue until the server confirms receipt
- I don't know that there would be enough benefit to 

We will likely want to add a GET endpoint for tasks -- that'll return things like "is the task still open" and sizes and expiration duration and (once implemented) settings for encryption and signing and requisite keys for encrypting / signing. I think the client would store these settings locally and if offline, fall back to the local settings.

The first consumer of this will be https://github.com/uwmadison-chm/teddy-static, a behavioral task called the self-referential encoding task (SRET), and some brain games. Teddy and the SRET have well-defined ends, the brain games, less so -- people may just close their tab. (We could try to add that "one last request as you're closing" thing on Chrome.) 

So the brain games will often rely on either the server collecting expired runs, or subsequent runs cleaning up.

---

## Questions raised, and answers

*Paraphrased questions. Nate's answers are in his words, lightly trimmed.*

**One local run can become several server runs** — offline, there's no `run_id` at
start, and an expiring run means leftovers go somewhere new. Roll over automatically?
What about a queued finalize that hits an expired run?

> Yup. run_id will be synthetic in offline mode and would start a run and send a bunch of
> stuff all at once.
>
> I think that _really_ spending too much time thinking about what happens if people go
> offline and wait a long time after starting a run... it'll drive us crazy. When things
> like that happen, starting new runs is kind of weird. Throwing away data is kind of
> weird. Participants can't do anything about this. I guess if we could start a new run,
> starting with an event like "previous run {run_id} expired; starting a new one" message
> would be okay. This is really not super likely to happen in the things where tracking
> runs precisely is going to be essential.

**Start the server run eagerly or lazily?**

> Agreed; we should start as eagerly as we can. We'll probably, honestly, start runs with a
> metadata event anyhow.

**Signatures with `exp` strand offline runs; and Pig never sees the participant's URL.**

> I think `exp` on signatures is a mistake actually; I should fix the issue. And specify
> that we're signing _a parameter or set of parameters_ not a URL. (We'd put signatures
> for parameters _into_ URLs)

**Safari: no Background Sync, and IndexedDB cleared after seven days unvisited.**

> Yup, there are data loss paths. We'll do our best to send data as early as we can. If
> data's going stale by seven days of non-use in a non-PWA app I think losing it is fine.
> We can warn task devs about this but... it's like Jack Handy's keys in the lava. They're
> gone, man.

**Workers can't be loaded cross-origin from a CDN; a Blob-URL worker can be blocked by
CSP.**

> I don't ever plan to host this on a CDN. [...] I would expect people to host the script
> on their own origin, either by downloading a prebuilt release or by using frontend
> tooling.

And later: document the Blob-URL approach for people who want it, but it isn't the main
event.

**Several tabs flushing the same queue.**

> Yes we should prevent race conditions _and also_ if someone is doing multiple behavioral
> tasks at the same time in the same browser, cats and dogs are living in the same house
> already. So: yes to locks, so long as there is no condition where "ope we stopped sending
> data because locks are hard"

**#10 returns the encryption key at run start, but offline clients encrypt before any
run.**

> It's okay if the key _also_ comes back in the run-start response, I think -- there just
> needs to be a way to get it without starting a run as well.

**Stale cached settings: encryption turned on while a client thinks it's off.**

> If the server expects encryption and we didn't do that because it previously told us it
> didn't want that then the client will fail at sending the events, that's okay

**Encrypting media parts one at a time means they can't be concatenated before
decryption.**

> We should adjust api.md a bit, but the idea remains the same; really before anyone can
> do anything with any data, it needs to be decrypted

**Part sizes:** age's overhead is deterministic, so the client can slice to fit exactly
rather than report a recommended maximum. Agreed.

**Client-added fields: fixed names, configurable names, or something else?**

> Another thing we could do would be to add a `_client` object inside `data` and add
> whatever we want there; we would not allow tasks to set stuff in side of (or provide)
> `_client`

What goes in it:

> Client version can go in per-run metadata. I don't think the local run ID is useful.
> Really, client wallclock with UTC is good, performance.now is good, I can't think of a
> ton else. We might want to be able to do some performance logging (eg, "how long did it
> take to post the message to the worker / encrypt / enqueue / send to the server") but
> that doesn't need to go in the data I think

**Run-level info (user agent and the like) as an automatic first event:** agreed.

**Does a local run survive a page reload?**

> It should be possible (and fairly straightforward, hopefully!) for a task author to keep
> enough state to persist across page reloads with the queue behaving "sanely" on reload
> or navigation -- a task may involve nav.

A resume call taking a local run ID the author keeps was "roughly what I had in mind."

**A last-gasp send on close is capped at 64 KB, and encryption is async.**

> What I would _really_ like, I think, is for task authors (see: brain games) to be able to
> send `finalize` when someone leaves the page. Yes, that might orphan some events, and we
> should warn authors that can happen. But it's cleaner than just leaving the runs open for
> yonks

And then, on what happens to the orphans:

> What I had in mind was that task authors could opt-in to finalize-on-leave mode. If it's
> on, the client would abandon the queue at that point, and the main thread would probably
> use navigator.sendBeacon() to finalize the run. The client should not try and restart
> sessions for _explicitly_ closed runs, and discard the remaining data. (Honestly,
> dropping data for all closed runs is also okay, as long as that is well-documented. Make
> your expiry time long if you want to allow data for a long time.)

**Cloning event data to the worker costs main-thread time.**

> I think cloning event data is okay. I'm not 100% certain about media buffers? I think
> probably that's all right too

(`Blob`s aren't copied when posted to a worker, and `ArrayBuffer`s can be transferred, so
media costs nothing extra.)

**Where the client lives.**

> As long as getting something up in npm wouldn't be too hard from this repo, I guess I'd
> keep them together for now

**#11 is in the launch milestone; #10 and #15 are in "extras."**

> We need #11 to even test this, and we _can_ launch that way. Getting #10 and #15 out
> before we go live would be _real nice_ but it is not _required_

**The truncated bullet** ("I don't know that there would be enough benefit to"):

> No idea what was in my brain when I wrote the truncated text
