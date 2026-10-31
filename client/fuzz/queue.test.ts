// A model of one run's queue, checked against the client with fast-check.
//
// fast-check plays out sequences of what a task and the world can do to a run: add
// events, start media items and add their bytes, finish them, finalize; go offline and
// come back; answer with a 500, or drop the connection, or do the work and lose the
// reply; expire the server run partway through. Then it lets the client send everything
// and checks what reached the pretend Pig:
//
// - Every event the task added arrived, in the order it was added, with its data. One
//   may arrive twice, but never two different ways.
// - Every media part arrived, under the number it was given, with its bytes.
// - Each server run after the first says which one it continues.
// - A finalized run ends finalized (or expired, if time ran out first), and the device
//   forgets it.
// - Nothing is left queued, nothing was refused, and the page heard of no errors.
//
// `npm run fuzz` runs it; CI doesn't. For a longer look: FUZZ_RUNS=2000 npm run fuzz

import assert from "node:assert/strict";
import { test } from "node:test";

import fc from "fast-check";

import type { Core } from "../src/core.ts";
import { eventually, FakePig, makeCore, STAMP, startRun } from "../test/helpers.ts";

const RUNS = Number(process.env.FUZZ_RUNS ?? 300);
const PART_SIZE = 8;
const MAX_EVENT_SIZE = 1024;

interface ModelItem {
  /** The parts the client should cut what was added into, in order. */
  parts: Uint8Array[];
  finishQueued: boolean;
}

interface Model {
  /** Events and media starts, in the order the task added them. */
  added: { id: string; data: Record<string, unknown> }[];
  /** Media items by event ID. */
  media: Map<string, ModelItem>;
  finalized: boolean;
}

interface Real {
  core: Core;
  pig: FakePig;
  run: string;
  /** How many of the next requests the pretend Pig does the work of, and whose
   * replies then go missing. */
  lostReplies: { count: number };
}

type Command = fc.AsyncCommand<Model, Real>;

// ---------------------------------------------------------------- what a task does

class Add implements Command {
  data: Record<string, unknown>;
  constructor(data: Record<string, unknown>) {
    this.data = data;
  }
  check = () => true;
  async run(model: Model, real: Real) {
    if (model.finalized) return await refusedAsFinished(real.core.add(real.run, this.data, STAMP));
    if (Object.hasOwn(this.data, "_client")) {
      await assert.rejects(real.core.add(real.run, this.data, STAMP), { code: "bad-event" });
      return;
    }
    const size = byteLength(JSON.stringify({ data: { ...this.data, _client: STAMP } }));
    if (size > MAX_EVENT_SIZE) {
      await assert.rejects(real.core.add(real.run, this.data, STAMP), { code: "too-big" });
      return;
    }
    const id = await real.core.add(real.run, this.data, STAMP);
    assert.equal(id, String(model.added.length + 1));
    model.added.push({ id, data: this.data });
  }
  toString = () => `add(${JSON.stringify(this.data)})`;
}

class StartMedia implements Command {
  prompt: number;
  constructor(prompt: number) {
    this.prompt = prompt;
  }
  check = () => true;
  async run(model: Model, real: Real) {
    const data = { prompt: this.prompt };
    if (model.finalized) return await refusedAsFinished(real.core.startMedia(real.run, data, STAMP));
    const id = await real.core.startMedia(real.run, data, STAMP);
    assert.equal(id, String(model.added.length + 1));
    model.added.push({ id, data });
    model.media.set(id, { parts: [], finishQueued: false });
  }
  toString = () => `startMedia({prompt: ${this.prompt}})`;
}

class AddMedia implements Command {
  which: number;
  bytes: Uint8Array;
  constructor(which: number, bytes: Uint8Array) {
    this.which = which;
    this.bytes = bytes;
  }
  check = (model: Readonly<Model>) => model.media.size > 0;
  async run(model: Model, real: Real) {
    const [id, item] = [...model.media][this.which % model.media.size];
    const adding = real.core.addMedia(real.run, id, new Blob([this.bytes as BlobPart]));
    if (model.finalized || item.finishQueued) return await refusedAsFinished(adding);
    const expected: Uint8Array[] = [];
    for (let offset = 0; offset < this.bytes.length; offset += PART_SIZE) {
      expected.push(this.bytes.slice(offset, offset + PART_SIZE));
    }
    const first = item.parts.length + 1;
    assert.deepEqual(
      await adding,
      expected.map((_, i) => first + i),
    );
    item.parts.push(...expected);
  }
  toString = () => `addMedia(item ${this.which}, ${this.bytes.length} bytes)`;
}

class FinishMedia implements Command {
  which: number;
  constructor(which: number) {
    this.which = which;
  }
  check = (model: Readonly<Model>) => model.media.size > 0;
  async run(model: Model, real: Real) {
    const [id, item] = [...model.media][this.which % model.media.size];
    const finishing = real.core.finishMedia(real.run, id);
    if (item.finishQueued) return await again(finishing, model);
    if (model.finalized) return await refusedAsFinished(finishing);
    await finishing;
    item.finishQueued = true;
  }
  toString = () => `finishMedia(item ${this.which})`;
}

class Finalize implements Command {
  check = () => true;
  async run(model: Model, real: Real) {
    if (model.finalized) return await again(real.core.finalize(real.run), model);
    await real.core.finalize(real.run);
    model.finalized = true;
  }
  toString = () => "finalize()";
}

// ------------------------------------------------------------- what the world does

class Offline implements Command {
  offline: boolean;
  constructor(offline: boolean) {
    this.offline = offline;
  }
  check = () => true;
  async run(_model: Model, real: Real) {
    real.pig.offline = this.offline;
    if (!this.offline) real.core.nudge();
  }
  toString = () => (this.offline ? "goOffline()" : "comeBack()");
}

type TroubleKind = 500 | 502 | 503 | "network" | "lost reply";

/** The next few requests, whatever they are, go wrong this way. */
class Trouble implements Command {
  kind: TroubleKind;
  times: number;
  constructor(kind: TroubleKind, times: number) {
    this.kind = kind;
    this.times = times;
  }
  check = () => true;
  async run(_model: Model, real: Real) {
    for (let i = 0; i < this.times; i++) {
      if (this.kind === "lost reply") real.lostReplies.count += 1;
      else if (this.kind === "network") real.pig.upcoming.push("network");
      else real.pig.upcoming.push({ status: this.kind, body: {} });
    }
  }
  toString = () => `trouble(${JSON.stringify(this.kind)}, ${this.times})`;
}

class Expire implements Command {
  check = () => true;
  async run(_model: Model, real: Real) {
    // The newest server run, if it's still taking data. Only an open run expires.
    const newest = [...real.pig.runs.entries()].at(-1);
    if (newest && newest[1].status === "in_progress") real.pig.expire(newest[0]);
  }
  toString = () => "expireTheServerRun()";
}

class Pause implements Command {
  ms: number;
  constructor(ms: number) {
    this.ms = ms;
  }
  check = () => true;
  async run() {
    await new Promise((resolve) => setTimeout(resolve, this.ms));
  }
  toString = () => `pause(${this.ms})`;
}

const commands = [
  fc.dictionary(fc.string({ maxLength: 8 }), fc.jsonValue({ maxDepth: 2 }), { maxKeys: 4 }).map((data) => new Add(data)),
  fc.nat(9).map((prompt) => new StartMedia(prompt)),
  fc.tuple(fc.nat(), fc.uint8Array({ maxLength: 3 * PART_SIZE })).map(([which, bytes]) => new AddMedia(which, bytes)),
  fc.nat().map((which) => new FinishMedia(which)),
  fc.constant(new Finalize()),
  fc.boolean().map((offline) => new Offline(offline)),
  fc
    .tuple(fc.constantFrom<TroubleKind>(500, 502, 503, "network", "lost reply"), fc.integer({ min: 1, max: 3 }))
    .map(([kind, times]) => new Trouble(kind, times)),
  fc.constant(new Expire()),
  fc.integer({ min: 0, max: 30 }).map((ms) => new Pause(ms)),
];

// ------------------------------------------------------------------------ the test

test("everything a task adds reaches Pig, whatever happens on the way", async () => {
  await fc.assert(
    fc.asyncProperty(fc.commands(commands, { maxCommands: 30 }), async (sequence) => {
      const pig = new FakePig();
      pig.tasks.stroop.max_part_size = PART_SIZE;
      pig.tasks.stroop.max_event_size_bytes = MAX_EVENT_SIZE;
      const lostReplies = { count: 0 };
      const answer = pig.fetch;
      pig.fetch = async (url, init) => {
        const response = await answer(url, init);
        if (lostReplies.count > 0) {
          lostReplies.count -= 1;
          throw new TypeError("Failed to fetch");
        }
        return response;
      };

      const { core, store, notes } = await makeCore({ pig });
      try {
        const run = (await startRun(core)).id;
        const model: Model = { added: [], media: new Map(), finalized: false };
        await fc.asyncModelRun(() => ({ model, real: { core, pig, run, lostReplies } }), sequence);

        // Back online, with nothing in the way, and everything sent.
        pig.offline = false;
        pig.upcoming = [];
        lostReplies.count = 0;
        core.nudge();
        await within(5_000, core.sent(run), "sent() to resolve");

        checkWhatArrived(pig, model);
        assert.deepEqual(await core.pending({ run }), { events: 0, bytes: 0, failed: 0, runs: 0 });
        assert.deepEqual(
          notes.filter((note) => note.type === "error"),
          [],
        );
        if (model.finalized) {
          await eventually(async () => (await store.run(run)) === undefined, { what: "the finished run to be forgotten" });
        }
      } finally {
        await core.close();
        store.close();
      }
    }),
    { numRuns: RUNS, includeErrorInReport: true },
  );
});

// Half of a UTF-16 pair is valid in a JavaScript string, and JSON.stringify writes it as
// `\ud83d`, which Pig can't store, so it answers 500, which the client retries forever,
// holding up everything behind it (#106). add() should refuse such an event. It doesn't
// yet, so this checks that it still doesn't: when #106 is fixed, this fails, and the
// outer `assert.rejects` comes off.
const withHalfAPair = fc
  .tuple(fc.string({ maxLength: 4 }), fc.integer({ min: 0xd800, max: 0xdfff }), fc.string({ maxLength: 4 }))
  .map(([before, half, after]) => before + String.fromCharCode(half) + after);

test("add() refuses an event with half a UTF-16 pair in it (fails until #106 is fixed)", async () => {
  const { core, store } = await makeCore({ pig: new FakePig() });
  try {
    const run = (await startRun(core)).id;
    await assert.rejects(
      fc.assert(
        fc.asyncProperty(
          fc.oneof(
            withHalfAPair.map((text) => ({ said: text })),
            withHalfAPair.map((text) => ({ [text]: 1 })),
            withHalfAPair.map((text) => ({ trials: [{ said: text }] })),
          ),
          async (data) => {
            await assert.rejects(core.add(run, data, STAMP), { code: "bad-event" });
          },
        ),
      ),
      "add() refuses half a UTF-16 pair now, so #106 is fixed. Take the outer assert.rejects off.",
    );
  } finally {
    await core.close();
    store.close();
  }
});

function checkWhatArrived(pig: FakePig, model: Model): void {
  // A server run with no events is one whose start went through and whose reply was
  // lost, so the client started another. Nothing was sent to it. See #109.
  const serverRuns = [...pig.runs.entries()].filter(([, run]) => run.events.size > 0);

  // Each starts with the client's own first event, and each after the first says which
  // earlier run it continues. So can the first: a run can expire before its first event
  // goes, and then nothing reaches it.
  const order = [...pig.runs.keys()];
  serverRuns.forEach(([id, run], i) => {
    const [first] = run.events.keys();
    assert.equal(first, "0", `server run ${id} doesn't start with event 0`);
    const continues = pig.stored(id)["0"]._client.continues_run;
    if (i > 0) assert.ok(continues, `server run ${id} doesn't say which run it continues`);
    if (continues) {
      const earlier = order.indexOf(continues) >= 0 && order.indexOf(continues) < order.indexOf(id);
      assert.ok(earlier, `server run ${id} says it continues ${continues}`);
    }
  });

  // The task's events, in order of first arrival, are what it added. An event that
  // arrives again (a media item started again in the next server run, say) arrives the
  // same way each time.
  const arrived = new Map<string, unknown>();
  for (const [id] of serverRuns) {
    for (const [eventId, data] of Object.entries(pig.stored(id))) {
      if (eventId === "0") continue;
      if (arrived.has(eventId)) assert.deepEqual(data, arrived.get(eventId), `event ${eventId} arrived two ways`);
      else arrived.set(eventId, data);
    }
  }
  assert.deepEqual(
    [...arrived.keys()],
    model.added.map((event) => event.id),
  );
  for (const { id, data } of model.added) {
    assert.deepEqual(arrived.get(id), JSON.parse(JSON.stringify({ ...data, _client: STAMP })));
  }

  // Every part of every media item, under its number, with its bytes, in whichever
  // server run it went to.
  for (const [eventId, item] of model.media) {
    const parts = new Map<number, Uint8Array>();
    const holders = new Set<string>();
    for (const [id] of serverRuns) {
      for (const [number, bytes] of pig.media(id, eventId)?.bytes ?? []) {
        if (parts.has(number)) assert.deepEqual(bytes, parts.get(number), `part ${number} arrived two ways`);
        parts.set(number, bytes);
        holders.add(id);
      }
    }
    assert.deepEqual(
      [...parts.keys()].sort((a, b) => a - b),
      item.parts.map((_, i) => i + 1),
      `media item ${eventId} is missing parts`,
    );
    item.parts.forEach((bytes, i) => assert.deepEqual(parts.get(i + 1), bytes));
    // A finished item is finished where one server run holds all of it, unless that run
    // expired, perhaps before the finish got there. An item split between two runs
    // can't be finished in either.
    const [holder] = holders;
    if (item.finishQueued && holders.size === 1 && pig.run(holder).status !== "expired") {
      assert.equal(pig.media(holder, eventId)?.finished, true, `media item ${eventId} wasn't finished`);
    }
    if (holders.size > 1) {
      for (const id of holders) assert.equal(pig.media(id, eventId)?.finished, false);
    }
  }

  const last = serverRuns.at(-1)?.[1];
  if (model.finalized) assert.ok(last && ["finalizing", "expired"].includes(last.status), `ended ${last?.status}`);
}

// Once a finalized run is sent, the device forgets it, and a call about it is refused as
// a run that isn't there, or one this page doesn't hold, depending on how far the
// forgetting has got.
const FORGOTTEN = ["not-found", "not-held"];

/** A call made after finalize(), which is refused: as finished, or as forgotten. */
async function refusedAsFinished(call: Promise<unknown>): Promise<void> {
  await assert.rejects(call, (error: { code?: string }) => {
    assert.ok(error.code === "finished" || FORGOTTEN.includes(error.code ?? ""), `refused with ${error.code}`);
    return true;
  });
}

/** A finish made a second time, which does nothing, or is refused once the run is
 * forgotten. */
async function again(call: Promise<unknown>, model: Model): Promise<void> {
  try {
    await call;
  } catch (error) {
    assert.ok(model.finalized && FORGOTTEN.includes((error as { code?: string }).code ?? ""), String(error));
  }
}

function byteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}

/** `promise`, or a failure saying what didn't happen, if it takes longer than `ms`. */
function within<T>(ms: number, promise: Promise<T>, what: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`Timed out waiting for ${what}.`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}
