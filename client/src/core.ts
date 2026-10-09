// Everything the client does with the queue and the server. Runs inside the web worker.
//
// It's written against small interfaces rather than browser globals: a Store, an http
// object, a Web Locks object, and a function for telling the page about things. That's
// so the tests can run all of it in Node with a fake server, and so none of it depends
// on running in a worker in particular.
//
// The shape, briefly. Each run has one ordered queue of ops in IndexedDB: start the
// server run, events, finalize. One sender per run works through that queue from the
// front, batching consecutive events into one request. Strict order means a finalize
// can't overtake the events it closes, and being offline is just a sender that can't get
// past the first op yet.
//
// Exactly one page sends for any run, enforced with a Web Lock named for the run. The
// page that started or resumed a run holds its lock for as long as the page is open. A
// run nobody holds is an orphan: its page closed, or navigated away. Any page using the
// client picks up orphans and sends what they have left, and, if the task asked for it,
// finalizes the ones that were abandoned. See `sweep()`.

import type { Http, Outcome } from "./http.ts";
import { PigError, wallTime } from "./shared.ts";
import type { Store } from "./store.ts";
import type {
  ClientInfo,
  LogLevel,
  EventOp,
  NewOp,
  Notice,
  Op,
  Pending,
  RunRecord,
  RunSummary,
  Stamp,
  TaskParameters,
  TaskSettings,
} from "./types.ts";
import { CLIENT_VERSION } from "./version.ts";

export { PigError };

// The server's rule for parameter values, from config.py. Checked here as well so an
// offline run with a bad link fails at the start, not after the participant has done
// the whole task.
const SAFE_VALUE = /^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$/;

export const DEFAULTS = {
  // Waiting between tries after a failure: doubling from the first, up to the cap, with
  // up to half of each wait taken off at random so a lab's worth of devices coming back
  // online don't all retry in step.
  firstRetryMs: 1_000,
  maxRetryMs: 60_000,
  // How much goes in one request. The server limits each event; the web server in front
  // of it limits whole requests, so batches stay modest.
  maxBatchEvents: 100,
  maxBatchBytes: 256 * 1024,
  // A run whose page has gone, and that hasn't been touched for this long, counts as
  // abandoned. Long enough that a task moving from one page to the next has resumed it
  // well before then.
  abandonedAfterMs: 30_000,
  // How often the page holding a run says it's still there, and how often a page looks
  // for orphaned runs.
  heartbeatMs: 10_000,
  sweepMs: 30_000,
  // How long resume() waits for another page to let go of a run.
  resumeWaitMs: 5_000,
  // How long start() waits for the server before carrying on as if offline. Someone is
  // looking at the screen while it waits.
  startWaitMs: 5_000,
};

export type Timing = typeof DEFAULTS;

/** The part of Web Locks the client uses. */
export interface Locks {
  request(name: string, options: LockOptions, callback: (lock: Lock | null) => unknown): Promise<unknown>;
}

/** What a sender's attempt at the op at the front of the queue came to. */
type Attempt = "done" | "later" | "refused" | "too-big";

export class Core {
  store: Store;
  http: Http;
  locks: Locks;
  /** Tells the page about progress and errors. */
  notify: (message: Notice) => void;
  log: (level: LogLevel, ...parts: unknown[]) => void;
  options: Timing;
  /** Runs this page holds the lock for: run ID → function that lets go of it. */
  held = new Map<string, () => void>();
  /** Runs with a sender working on them. */
  senders = new Map<string, Sender>();
  /** Runs this page is sending for only because their own page is gone. */
  orphans = new Set<string>();
  /** Callers waiting for a run's queue to empty: run ID → [resolve] */
  sentWaiters = new Map<string, (() => void)[]>();
  timers: ReturnType<typeof setInterval>[] = [];
  closed = false;

  constructor({
    store,
    http,
    locks,
    notify = () => {},
    log = () => {},
    options = {},
  }: {
    store: Store;
    http: Http;
    locks: Locks;
    notify?: (message: Notice) => void;
    log?: (level: LogLevel, ...parts: unknown[]) => void;
    options?: Partial<Timing>;
  }) {
    this.store = store;
    this.http = http;
    this.locks = locks;
    this.notify = notify;
    this.log = log;
    this.options = { ...DEFAULTS, ...options };
  }

  /** Start the background work: looking for orphaned runs, now and every so often. */
  begin(): Promise<void> {
    this.#every(this.options.sweepMs, () => this.sweep());
    this.#every(this.options.heartbeatMs, () => this.#heartbeat());
    return this.sweep();
  }

  /** Stop everything and let go of every run. Used by the tests. */
  async close(): Promise<void> {
    this.closed = true;
    for (const timer of this.timers) clearInterval(timer);
    for (const sender of this.senders.values()) sender.wake();
    for (const release of this.held.values()) release();
    this.held.clear();
  }

  // ------------------------------------------------------------ starting a run

  /**
   * Start a run. When the server can be reached, the server run is started before this
   * returns, so a closed task or a bad link fails here, where the task can tell the
   * participant. Offline, the run starts locally and the server run starts later.
   *
   * `clientInfo` is what the page knows about itself, for the first event; `stamp` is
   * when start() was called.
   */
  async start({
    server,
    task,
    parameters,
    finalizeWhenAbandoned = false,
    clientInfo,
    stamp,
  }: {
    server: string;
    task: string;
    parameters: TaskParameters;
    finalizeWhenAbandoned?: boolean;
    clientInfo: ClientInfo;
    stamp: Stamp;
  }): Promise<RunSummary> {
    const settings = await this.#settings(server, task);
    if (!settings.open) {
      throw new PigError("task-closed", `The task ${JSON.stringify(task)} isn't accepting new data right now.`);
    }
    checkParameters(settings, parameters);

    const id = randomId();
    const now = Date.now();
    await this.store.saveRun({
      id,
      server,
      task,
      parameters,
      settings,
      client_info: clientInfo,
      server_run_id: null,
      run_number: null,
      server_started_at: null,
      // Event 0 is the client's own first event; the task's events count from 1.
      next_event_id: 1,
      finalize_when_abandoned: finalizeWhenAbandoned,
      finalize_queued: false,
      closed: false,
      failed: null,
      created_at: now,
      last_active: now,
    });
    await this.#hold(id);
    await this.store.queue(id, () => ({ kind: "start" }));
    await this.store.queue(id, () => firstEvent(clientInfo, stamp, null));
    this.log("info", `Started collecting data for task ${task}, run ${id}.`);

    // Try to start the server run now, so a refusal reaches the task.
    const run = await this.#stored(id);
    const [startOp] = await this.store.ops(id, 1);
    const outcome = await this.#sendStart(run, startOp, this.options.startWaitMs);
    if (outcome === "refused") {
      const failed = (await this.#stored(id)).failed ?? { code: "refused", message: "The server refused the run." };
      await this.store.deleteRun(id);
      this.#letGo(id);
      throw new PigError(failed.code, failed.message);
    }
    if (outcome === "later") this.log("info", `Couldn't reach ${server}; run ${id} will start on the server once it can.`);
    this.#ensureSender(id);
    return this.summary(await this.#stored(id));
  }

  /**
   * Pick up a run this page or an earlier one started. Waits a few seconds for another
   * page to let go of it, which is what happens while a task moves between pages.
   */
  async resume(id: string): Promise<RunSummary> {
    if (this.held.has(id)) return this.summary(await this.#stored(id));
    const run = await this.store.run(id);
    if (run === undefined) {
      throw new PigError("not-found", `There's no run ${id} on this device. It may have finished already.`);
    }
    if (run.finalize_queued || run.closed) {
      throw new PigError("finished", `Run ${id} has been finalized, so it can't be resumed. Start a new run.`);
    }
    const got = await this.#hold(id, { waitMs: this.options.resumeWaitMs });
    if (!got) {
      throw new PigError("busy", `This task is already open in another tab or window.`);
    }
    // Another page may have finalized it while we waited.
    const current = await this.store.run(id);
    if (current === undefined || current.finalize_queued || current.closed) {
      this.#letGo(id);
      throw new PigError("finished", `Run ${id} has been finalized, so it can't be resumed. Start a new run.`);
    }
    await this.store.updateRun(id, (r) => {
      r.last_active = Date.now();
    });
    this.log("info", `Resumed run ${id}.`);
    this.#ensureSender(id);
    return this.summary(current);
  }

  // ------------------------------------------------------------------ events

  /**
   * Queue an event. Resolves with its event ID once it's stored in IndexedDB. `stamp` is
   * taken on the page when add() was called.
   */
  async add(id: string, data: unknown, stamp: Stamp): Promise<string> {
    const run = await this.#heldRun(id);
    if (run.finalize_queued) {
      throw new PigError("finished", `Run ${id} has been finalized, so it can't take more events.`);
    }
    if (!isPlainObject(data)) {
      throw new PigError("bad-event", "An event has to be a plain object, like { type: \"trial\", rt: 843 }.");
    }
    if (Object.hasOwn(data, "_client")) {
      throw new PigError("bad-event", "_client is filled in by the client. Use another name for your field.");
    }
    const json = JSON.stringify({ data: { ...data, _client: stamp } });
    const size = byteLength(json);
    const limit = run.settings.max_event_size_bytes;
    if (limit && size > limit) {
      throw new PigError("too-big", `This event is ${size} bytes, and this task allows ${limit}.`);
    }
    const op = await this.store.queue(id, (eventId) => ({ kind: "event", event_id: eventId!, json, bytes: size }), {
      withEventId: true,
    });
    const eventId = op.kind === "event" ? op.event_id : "";
    this.log("debug", `Queued event ${eventId} for run ${id}.`);
    this.#wake(id);
    return eventId;
  }

  /** Queue the finalize. It's sent after every event queued before it. */
  async finalize(id: string): Promise<void> {
    const run = await this.#heldRun(id);
    if (run.finalize_queued) return;
    await this.store.updateRun(id, (r) => {
      r.finalize_queued = true;
    });
    await this.store.queue(id, () => ({ kind: "finalize" }));
    this.log("info", `Queued finalize for run ${id}.`);
    this.#wake(id);
  }

  /** Resolves once nothing is queued for this run. Never rejects; it just waits. */
  async sent(id: string): Promise<void> {
    const done = new Promise<void>((resolve) => {
      const waiting = this.sentWaiters.get(id) ?? [];
      waiting.push(resolve);
      this.sentWaiters.set(id, waiting);
    });
    // Checked after registering, so an emptying that happens in between isn't missed.
    if ((await this.store.ops(id, 1)).length === 0) this.#settleSent(id);
    await done;
  }

  // --------------------------------------------------------- what's queued

  /**
   * How much is waiting to be sent: for one run, for one task, or for everything on
   * this device. `failed` counts events the server refused for good; they're kept, but
   * never sent. `runs` counts runs with anything left to send.
   */
  async pending({ run, task }: { run?: string; task?: string } = {}): Promise<Pending> {
    const runs = await this.store.allRuns();
    const runTask = new Map(runs.map((r) => [r.id, r.task]));
    const matches = (op: Op) => (run === undefined || op.run === run) && (task === undefined || runTask.get(op.run) === task);
    const counts = { events: 0, bytes: 0, failed: 0, runs: 0 };
    const withWork = new Set();
    for (const op of await this.store.allOps()) {
      if (!matches(op)) continue;
      withWork.add(op.run);
      if (op.kind === "event") {
        counts.events += 1;
        counts.bytes += op.bytes;
      }
    }
    for (const op of await this.store.allFailed()) {
      if (matches(op)) counts.failed += 1;
    }
    counts.runs = withWork.size;
    return counts;
  }

  /** Throw away every event the server refused for good. */
  async discardFailed(): Promise<void> {
    await this.store.discardFailed();
    this.log("info", "Discarded failed events.");
  }

  /** Something changed that might let a waiting sender succeed: try again now. */
  nudge(): void {
    for (const sender of this.senders.values()) sender.wake();
  }

  // ---------------------------------------------------------------- orphans

  /**
   * Look for runs no page is holding, and finish what they left behind: send anything
   * still queued, and finalize the ones that asked for it once they're abandoned.
   * Also forgets runs with nothing left to send whose server run has certainly expired.
   */
  async sweep(): Promise<void> {
    if (this.closed) return;
    const runs = await this.store.allRuns();
    const allOps = await this.store.allOps();
    const queued = new Set(allOps.map((op) => op.run));

    for (const run of runs) {
      if (this.held.has(run.id) || this.senders.has(run.id)) continue;
      const wantsFinalize = run.finalize_when_abandoned && !run.finalize_queued && !run.closed;
      const expired =
        run.server_started_at !== null &&
        Date.now() - run.server_started_at > run.settings.expires_after_sec * 1000;
      if (!queued.has(run.id) && !wantsFinalize && !expired) continue;
      if (!queued.has(run.id) && wantsFinalize && Date.now() - run.last_active < this.options.abandonedAfterMs) {
        continue; // Not abandoned yet. Maybe the next page is about to resume it.
      }

      if (!(await this.#hold(run.id))) continue; // Another page has it.
      const current = await this.store.run(run.id);
      if (current === undefined) {
        this.#letGo(run.id);
        continue;
      }
      const hasOps = (await this.store.ops(run.id, 1)).length > 0;
      if (!hasOps && expired && !(current.finalize_when_abandoned && !current.finalize_queued)) {
        this.log("info", `Forgetting run ${run.id}: nothing left to send, and its server run has expired.`);
        await this.store.deleteRun(run.id);
        this.#letGo(run.id);
        continue;
      }
      if (
        current.finalize_when_abandoned &&
        !current.finalize_queued &&
        !current.closed &&
        Date.now() - current.last_active >= this.options.abandonedAfterMs
      ) {
        this.log("info", `Run ${run.id} was abandoned; it will be finalized once its queue is sent.`);
        await this.store.updateRun(run.id, (r) => {
          r.finalize_queued = true;
        });
        await this.store.queue(run.id, () => ({ kind: "finalize" }));
      }
      this.log("info", `Sending what run ${run.id} left behind.`);
      this.#ensureSender(run.id, { orphan: true });
    }
  }

  // ---------------------------------------------------------------- sending

  #ensureSender(id: string, { orphan = false } = {}): void {
    if (this.senders.has(id) || this.closed) return;
    const sender = new Sender();
    this.senders.set(id, sender);
    if (orphan) this.orphans.add(id);
    this.#sendLoop(id, sender, orphan)
      .catch((error) => {
        this.log("error", `The sender for run ${id} stopped:`, error);
        this.notify({ type: "error", run: id, code: "internal", message: String(error?.message ?? error) });
      })
      .finally(() => {
        this.senders.delete(id);
        if (orphan) {
          this.orphans.delete(id);
          this.#letGo(id);
        }
      });
  }

  /** There's new work for this run's sender. Doesn't cut short a wait after a failure. */
  #wake(id: string): void {
    this.senders.get(id)?.newWork();
  }

  /**
   * Work through one run's queue, front to back, for as long as there's anything in it.
   * An orphaned run's sender stops when the queue is empty; the page's own runs' senders
   * wait for more.
   */
  async #sendLoop(id: string, sender: Sender, orphan: boolean): Promise<void> {
    let failures = 0;
    let oneAtATime = false;

    while (!this.closed) {
      const run = await this.store.run(id);
      if (run === undefined) return;
      const ops = await this.store.ops(id, this.options.maxBatchEvents);

      if (ops.length === 0) {
        this.#settleSent(id);
        if (run.closed) {
          this.log("info", `Run ${id} is finished and fully sent.`);
          await this.store.deleteRun(id);
          this.#letGo(id);
          this.notify({ type: "run", run: { ...this.summary(run), state: "done" } });
          return;
        }
        if (orphan) return;
        await sender.idle(); // until add() or finalize() has something for us
        continue;
      }

      if (run.failed) {
        // The server refused to start this run, so nothing queued for it can go
        // anywhere. Its data stays on the device; see pending() and discardFailed().
        return;
      }

      const head = ops[0];
      let outcome: Attempt;
      if (head.kind === "start") {
        outcome = await this.#sendStart(run, head);
      } else if (head.kind === "finalize") {
        outcome = await this.#sendFinalize(run, head);
      } else {
        const batch = takeBatch(ops, oneAtATime ? 1 : this.options.maxBatchEvents, this.options.maxBatchBytes);
        outcome = await this.#sendEvents(run, batch);
        if (outcome === "too-big" && batch.length > 1) {
          oneAtATime = true; // Try them singly, to find the one that's too big.
          continue;
        }
        if (outcome === "too-big") {
          await this.store.failOps([{ seq: batch[0].seq, reason: "The server said this event is too big." }]);
          this.#reportFailed(run, [batch[0].event_id], "The server said this event is too big.");
          outcome = "done";
        }
        if (outcome === "done") oneAtATime = false;
      }

      if (outcome === "later") {
        failures += 1;
        const wait = retryWait(failures, this.options);
        this.log("debug", `Run ${id}: trying again in ${Math.round(wait)} ms.`);
        await sender.wait(wait);
      } else if (outcome === "refused") {
        return;
      } else {
        failures = 0;
        await this.#progress(run);
      }
    }
  }

  async #sendStart(run: RunRecord, op: Op, waitMs?: number): Promise<"done" | "later" | "refused"> {
    const reply = await this.http.startRun(run.server, run.task, run.parameters, waitMs);
    if (reply.ok) {
      await this.store.updateRun(run.id, (r) => {
        r.server_run_id = reply.body.run_id;
        r.run_number = reply.body.run_number;
        r.server_started_at = Date.now();
      });
      await this.store.deleteOps([op.seq]);
      this.log("info", `Run ${run.id} is server run ${reply.body.run_id} (run number ${reply.body.run_number}).`);
      this.notify({ type: "run", run: this.summary(await this.#stored(run.id)) });
      return "done";
    }
    if (reply.retry) {
      this.log("debug", `Couldn't start run ${run.id} on the server yet: ${reply.message}`);
      return "later";
    }
    const code = reply.status === 404 ? "task-unknown" : reply.status === 409 ? "task-closed" : "refused";
    await this.store.updateRun(run.id, (r) => {
      r.failed = { code, message: reply.message };
    });
    this.log("error", `The server refused to start run ${run.id}: ${reply.message}`);
    this.notify({ type: "error", run: run.id, code, message: reply.message });
    return "refused";
  }

  async #sendFinalize(run: RunRecord, op: Op): Promise<"done" | "later" | "refused"> {
    const reply = await this.http.finalize(run.server, run.task, run.server_run_id!);
    if (!reply.ok && reply.retry) return "later";
    if (reply.ok || reply.status === 409) {
      // 409 means the server run had already closed: expired, most likely. Either way
      // there's nothing left to do. Everything it received is saved.
      if (!reply.ok) this.log("info", `Run ${run.id} had already closed on the server (${reply.body?.status}).`);
      await this.store.deleteOps([op.seq]);
      await this.store.updateRun(run.id, (r) => {
        r.closed = true;
      });
      return "done";
    }
    return this.#runRefused(run, reply);
  }

  async #sendEvents(run: RunRecord, batch: EventOp[]): Promise<Attempt> {
    const body = `{${batch.map((op) => `${JSON.stringify(op.event_id)}:${op.json}`).join(",")}}`;
    const reply = await this.http.sendEvents(run.server, run.task, run.server_run_id!, body);
    if (!reply.ok && reply.retry) return "later";
    if (reply.status === 413) return "too-big";
    if (!reply.ok && reply.status !== 422 && reply.status !== 409) return this.#runRefused(run, reply);

    const stored = new Set<string>(reply.body?.stored ?? []);
    const errors: Record<string, { message: string; can_retry: boolean }> = reply.body?.errors ?? {};
    // `stored` lists every ID the server holds, so an event it refused as a collision
    // is in there too, under the other version. Only ours counts as sent if there's no
    // error for it.
    const sentNow = batch.filter((op) => stored.has(op.event_id) && !errors[op.event_id]);
    await this.store.deleteOps(sentNow.map((op) => op.seq));
    this.log("debug", `Run ${run.id}: the server has ${sentNow.length} of ${batch.length} events just sent.`);

    if (reply.status === 409) return this.#serverRunClosed(run, reply);

    const refused = batch.filter((op) => errors[op.event_id]?.can_retry === false);
    if (refused.length > 0) {
      await this.store.failOps(refused.map((op) => ({ seq: op.seq, reason: errors[op.event_id].message })));
      this.#reportFailed(run, refused.map((op) => op.event_id), errors[refused[0].event_id].message);
    }
    // Events refused with can_retry: true stay queued and go again.
    const retryable = batch.some((op) => errors[op.event_id]?.can_retry === true);
    return retryable ? "later" : "done";
  }

  /**
   * The server run closed under us. If it expired, start a new server run and send the
   * rest there, with a first event naming the run it continues. If it was finalized,
   * nothing more belongs in it: throw away what's left.
   */
  async #serverRunClosed(run: RunRecord, reply: Outcome): Promise<"done"> {
    const status = reply.body?.status;
    if (status === "expired") {
      this.log("info", `Server run ${run.server_run_id} expired; starting a new one for run ${run.id}.`);
      // The old first event, if it never got sent, is replaced by the new one.
      const leftover = await this.store.ops(run.id);
      const oldFirst = leftover.filter((op) => op.kind === "event" && op.event_id === "0");
      await this.store.deleteOps(oldFirst.map((op) => op.seq));
      const stamp: Stamp = { wall_time: wallTime(), performance_now: null };
      await this.store.prepend(run.id, [{ kind: "start" }, firstEvent(run.client_info, stamp, run.server_run_id)]);
      await this.store.updateRun(run.id, (r) => {
        r.server_run_id = null;
        r.run_number = null;
        r.server_started_at = null;
      });
      this.notify({ type: "run", run: this.summary(await this.#stored(run.id)) });
      return "done";
    }

    const left = (await this.store.ops(run.id)).filter((op) => op.kind === "event");
    this.log("warn", `Server run ${run.server_run_id} was already finalized; discarding ${left.length} events.`);
    const rest = (await this.store.ops(run.id)).filter((op) => op.kind !== "start");
    await this.store.deleteOps(rest.map((op) => op.seq));
    await this.store.updateRun(run.id, (r) => {
      r.closed = true;
      r.finalize_queued = true;
    });
    if (left.length > 0) {
      this.notify({
        type: "error",
        run: run.id,
        code: "discarded",
        message: `The server had already finalized this run, so ${left.length} events were thrown away.`,
      });
    }
    return "done";
  }

  /** The server doesn't know this run, or refused a request about it outright. */
  async #runRefused(run: RunRecord, reply: Outcome & { ok: false }): Promise<"refused"> {
    const code = reply.status === 404 ? "run-unknown" : "refused";
    await this.store.updateRun(run.id, (r) => {
      r.failed = { code, message: reply.message };
    });
    this.log("error", `The server refused a request for run ${run.id}: ${reply.message}`);
    this.notify({ type: "error", run: run.id, code, message: reply.message });
    return "refused";
  }

  #reportFailed(run: RunRecord, eventIds: string[], message: string): void {
    this.log("error", `The server refused events ${eventIds.join(", ")} of run ${run.id} for good: ${message}`);
    this.notify({ type: "error", run: run.id, code: "event-refused", events: eventIds, message });
  }

  async #progress(run: RunRecord): Promise<void> {
    this.notify({ type: "progress", run: run.id, pending: await this.pending({ run: run.id }) });
  }

  #settleSent(id: string): void {
    const waiting = this.sentWaiters.get(id);
    if (!waiting) return;
    this.sentWaiters.delete(id);
    for (const resolve of waiting) resolve();
  }

  // -------------------------------------------------------------- settings

  /**
   * The task's settings: fresh from the server when it can be reached, otherwise the
   * copy saved the last time it could. A task's first run on a device has to be online.
   */
  async #settings(server: string, task: string): Promise<TaskSettings> {
    const reply = await this.http.taskSettings(server, task, this.options.startWaitMs);
    if (reply.ok) {
      await this.store.saveSettings(server, task, reply.body);
      return reply.body;
    }
    if (reply.status === 404) {
      throw new PigError("task-unknown", `The server at ${server} has no task called ${JSON.stringify(task)}.`);
    }
    if (!reply.retry) throw new PigError("refused", reply.message);
    const saved = await this.store.settings(server, task);
    if (saved === undefined) {
      throw new PigError(
        "offline",
        `Couldn't reach ${server}, and this device has never done task ${JSON.stringify(task)} before, so it doesn't know the task's settings. The first time a device does a task, it has to be online.`,
      );
    }
    this.log("info", `Couldn't reach ${server}; using the saved settings for ${task}.`);
    return saved;
  }

  // ----------------------------------------------------------------- locks

  /**
   * Take the run's lock and keep it until #letGo. With `waitMs`, waits that long for
   * another page to let go; without it, gives up at once if the lock is taken.
   * Resolves with whether we got it.
   */
  #hold(id: string, { waitMs }: { waitMs?: number } = {}): Promise<boolean> {
    if (this.held.has(id)) return Promise.resolve(true);
    return new Promise((resolve) => {
      let options: LockOptions = { ifAvailable: true };
      if (waitMs !== undefined) {
        const giveUp = new AbortController();
        setTimeout(() => giveUp.abort(), waitMs);
        options = { signal: giveUp.signal };
      }
      this.locks
        .request(lockName(id), options, (lock) => {
          if (!lock) {
            resolve(false);
            return undefined;
          }
          return new Promise<void>((release) => {
            this.held.set(id, release);
            resolve(true);
          });
        })
        .catch(() => resolve(false)); // Timed out waiting.
    });
  }

  #letGo(id: string): void {
    this.held.get(id)?.();
    this.held.delete(id);
  }

  async #heldRun(id: string): Promise<RunRecord> {
    if (!this.held.has(id)) {
      throw new PigError("not-held", `Run ${id} isn't open on this page. Use resume() to pick it up.`);
    }
    const run = await this.store.run(id);
    if (run === undefined) throw new PigError("not-found", `There's no run ${id} on this device.`);
    return run;
  }

  /** Say the page holding these runs is still here, so they don't count as abandoned. */
  async #heartbeat(): Promise<void> {
    for (const id of this.held.keys()) {
      if (this.orphans.has(id)) continue;
      if (!(await this.store.run(id))?.closed) {
        await this.store.updateRun(id, (r) => {
          r.last_active = Date.now();
        });
      }
    }
  }

  /** A run's record, which the caller knows is there. */
  async #stored(id: string): Promise<RunRecord> {
    const run = await this.store.run(id);
    if (run === undefined) throw new PigError("not-found", `There's no run ${id} on this device.`);
    return run;
  }

  #every(ms: number, work: () => Promise<void>): void {
    const timer = setInterval(() => {
      work().catch((error) => this.log("error", "Background work failed:", error));
    }, ms);
    this.timers.push(timer);
  }

  /** What the page gets to know about a run. */
  summary(run: RunRecord): RunSummary {
    return {
      id: run.id,
      task: run.task,
      runId: run.server_run_id,
      runNumber: run.run_number,
      state: run.failed ? "failed" : run.closed ? "closed" : run.finalize_queued ? "finalizing" : "open",
      failed: run.failed,
    };
  }
}

/**
 * How a run's sender waits. Two kinds of waiting: idle, with nothing to send, until
 * there's new work; and after a failure, for a while before trying again. New work ends
 * an idle wait but not a failure's, or a server that's down would get a request for every
 * event added. wake() ends either, for when there's reason to think the network is back.
 */
class Sender {
  #resolve: (() => void) | null = null;
  #idle = false;
  #missed = false;

  idle(): Promise<void> {
    return this.#sleep(undefined, true);
  }

  wait(ms: number): Promise<void> {
    return this.#sleep(ms, false);
  }

  newWork(): void {
    if (this.#resolve === null) this.#missed = true; // Busy: look again before idling.
    else if (this.#idle) this.wake();
  }

  wake(): void {
    const resolve = this.#resolve;
    this.#resolve = null;
    resolve?.();
  }

  #sleep(ms: number | undefined, idle: boolean): Promise<void> {
    if (idle && this.#missed) {
      this.#missed = false;
      return Promise.resolve();
    }
    this.#missed = false;
    this.#idle = idle;
    return new Promise<void>((resolve) => {
      const timer = ms === undefined ? undefined : setTimeout(() => this.wake(), ms);
      this.#resolve = () => {
        clearTimeout(timer);
        resolve();
      };
    });
  }
}

// ------------------------------------------------------------------ helpers

/** The op for a run's first event: who the client is, and which run this one continues. */
function firstEvent(clientInfo: ClientInfo, stamp: Stamp, continues: string | null): NewOp {
  const _client: Record<string, unknown> = { ...stamp, ...clientInfo, version: CLIENT_VERSION, event: "run_start" };
  if (continues) _client.continues_run = continues;
  const json = JSON.stringify({ data: { _client } });
  return { kind: "event", event_id: "0", json, bytes: byteLength(json) };
}

function checkParameters(settings: TaskSettings, parameters: TaskParameters): void {
  const missing = settings.parameters.filter((name) => !(name in parameters));
  if (missing.length > 0) {
    throw new PigError(
      "parameters",
      `This task needs ${settings.parameters.join(", ")} to start, and ${missing.join(", ")} ${missing.length === 1 ? "is" : "are"} missing. Check the link.`,
    );
  }
  for (const name of settings.parameters) {
    const value = parameters[name];
    if (typeof value !== "string" || !SAFE_VALUE.test(value)) {
      throw new PigError(
        "parameters",
        `${name}=${JSON.stringify(value)} isn't allowed. Values may use letters, digits, underscore, and dash (not first), 1 to 64 characters. Check the link.`,
      );
    }
  }
}

/** Consecutive event ops from the front of the queue, within the batch limits. */
function takeBatch(ops: Op[], maxEvents: number, maxBytes: number): EventOp[] {
  const batch: EventOp[] = [];
  let bytes = 0;
  for (const op of ops) {
    if (op.kind !== "event" || batch.length >= maxEvents) break;
    if (batch.length > 0 && bytes + op.bytes > maxBytes) break;
    batch.push(op);
    bytes += op.bytes;
  }
  return batch;
}

function retryWait(failures: number, { firstRetryMs, maxRetryMs }: Timing): number {
  const full = Math.min(maxRetryMs, firstRetryMs * 2 ** (failures - 1));
  return full * (0.5 + Math.random() / 2);
}

function lockName(id: string): string {
  return `psych-ingestor:run:${id}`;
}

function randomId(): string {
  return crypto.randomUUID();
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== "object") return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

function byteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}
