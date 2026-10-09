// The client's queue and sender, run in Node against a pretend Pig.

import assert from "node:assert/strict";
import { afterEach, describe, test } from "node:test";

import { eventually, FakeLocks, FakePig, makeCore, PARAMETERS, SERVER, STAMP, startRun } from "./helpers.ts";

type Made = Awaited<ReturnType<typeof makeCore>>;
const cores: Made[] = [];
async function setUp(options: Parameters<typeof makeCore>[0]): Promise<Made> {
  const made = await makeCore(options);
  cores.push(made);
  return made;
}
afterEach(async () => {
  for (const { core, store } of cores.splice(0)) {
    await core.close();
    store.close();
  }
});

describe("a run while online", () => {
  test("starts the server run before start() returns", async () => {
    const pig = new FakePig();
    const { core } = await setUp({ pig });
    const run = await startRun(core);
    assert.equal(run.runId, "run-1");
    assert.equal(run.runNumber, 1);
    assert.deepEqual(pig.run("run-1").parameters, PARAMETERS);
  });

  test("sends the client's first event, then the task's, then finalizes", async () => {
    const pig = new FakePig();
    const { core, store } = await setUp({ pig });
    const run = await startRun(core);

    assert.equal(await core.add(run.id, { type: "trial", rt: 843 }, STAMP), "1");
    assert.equal(await core.add(run.id, { type: "trial", rt: 612 }, STAMP), "2");
    await core.finalize(run.id);
    await core.sent(run.id);

    const stored = pig.stored("run-1");
    assert.deepEqual(Object.keys(stored), ["0", "1", "2"]);
    assert.equal(stored["0"]._client.event, "run_start");
    assert.equal(stored["0"]._client.user_agent, "test");
    assert.deepEqual(stored["1"], { type: "trial", rt: 843, _client: STAMP });
    assert.equal(pig.run("run-1").status, "finalizing");
    await eventually(async () => (await store.run(run.id)) === undefined, { what: "the finished run to be forgotten" });
  });

  test("refuses to start when the task is closed", async () => {
    const pig = new FakePig();
    pig.tasks.stroop.open = false;
    const { core } = await setUp({ pig });
    await assert.rejects(startRun(core), { code: "task-closed" });
  });

  test("refuses to start an unknown task", async () => {
    const { core } = await setUp({ pig: new FakePig() });
    await assert.rejects(startRun(core, { task: "nope" }), { code: "task-unknown" });
  });

  test("refuses a link that's missing a parameter, before asking the server", async () => {
    const pig = new FakePig();
    const { core } = await setUp({ pig });
    await assert.rejects(startRun(core, { parameters: { participant_id: "10351" } }), (error: any) => {
      assert.equal(error.code, "parameters");
      assert.match(error.message, /session is missing/);
      return true;
    });
    assert.equal(pig.sent("start").length, 0);
  });

  test("refuses a parameter value the server wouldn't take", async () => {
    const { core } = await setUp({ pig: new FakePig() });
    await assert.rejects(startRun(core, { parameters: { participant_id: "10 351", session: "baseline" } }), {
      code: "parameters",
    });
  });

  test("passes extra link parameters through", async () => {
    const pig = new FakePig();
    const { core } = await setUp({ pig });
    await startRun(core, { parameters: { ...PARAMETERS, utm_source: "email" } });
    assert.equal(pig.run("run-1").parameters.utm_source, "email");
  });
});

describe("adding events", () => {
  test("refuses a field called _client", async () => {
    const { core } = await setUp({ pig: new FakePig() });
    const run = await startRun(core);
    await assert.rejects(core.add(run.id, { _client: 1 }, STAMP), { code: "bad-event" });
  });

  test("refuses something that isn't a plain object", async () => {
    const { core } = await setUp({ pig: new FakePig() });
    const run = await startRun(core);
    await assert.rejects(core.add(run.id, ["a", "list"], STAMP), { code: "bad-event" });
    await assert.rejects(core.add(run.id, "words", STAMP), { code: "bad-event" });
  });

  test("refuses an event bigger than the task allows, when it's added", async () => {
    const { core } = await setUp({ pig: new FakePig() });
    const run = await startRun(core);
    await assert.rejects(core.add(run.id, { text: "x".repeat(2000) }, STAMP), { code: "too-big" });
  });

  test("refuses events after finalize", async () => {
    const { core } = await setUp({ pig: new FakePig() });
    const run = await startRun(core);
    await core.finalize(run.id);
    await assert.rejects(core.add(run.id, { type: "late" }, STAMP), { code: "finished" });
  });

  test("refuses events for a run this page doesn't hold", async () => {
    const { core } = await setUp({ pig: new FakePig() });
    await assert.rejects(core.add("someone-elses", { type: "trial" }, STAMP), { code: "not-held" });
  });
});

describe("offline", () => {
  test("starts a run from saved settings and sends everything once back online", async () => {
    const pig = new FakePig();
    const { core } = await setUp({ pig });
    await startRun(core); // online once, so the settings are saved

    pig.offline = true;
    const run = await startRun(core);
    assert.equal(run.runId, null);
    for (let i = 0; i < 5; i += 1) await core.add(run.id, { trial: i }, STAMP);
    await core.finalize(run.id);

    pig.offline = false;
    core.nudge();
    await core.sent(run.id);

    assert.deepEqual(Object.keys(pig.stored("run-2")), ["0", "1", "2", "3", "4", "5"]);
    assert.equal(pig.run("run-2").status, "finalizing");
  });

  test("batches what built up into one request", async () => {
    const pig = new FakePig();
    const { core } = await setUp({ pig });
    await startRun(core);
    pig.offline = true;
    const run = await startRun(core);
    for (let i = 0; i < 30; i += 1) await core.add(run.id, { trial: i }, STAMP);

    pig.offline = false;
    pig.requests = [];
    core.nudge();
    await core.sent(run.id);
    const batches = pig.sent("events").filter((r) => r.path.includes("run-2"));
    assert.equal(batches.length, 1);
    assert.equal(Object.keys(JSON.parse(batches[0].body!)).length, 31);
  });

  test("can't start a task this device has never seen", async () => {
    const pig = new FakePig();
    pig.offline = true;
    const { core } = await setUp({ pig });
    await assert.rejects(startRun(core), { code: "offline" });
  });

  test("a retry sends exactly the bytes the first try did", async () => {
    const pig = new FakePig();
    const { core } = await setUp({ pig });
    const run = await startRun(core);
    await core.sent(run.id);
    pig.upcoming.push("network");
    await core.add(run.id, { trial: 1 }, STAMP);
    await core.sent(run.id);
    const [first, second] = pig.sent("events").slice(-2);
    assert.equal(first.body, second.body);
    assert.deepEqual(Object.keys(pig.stored("run-1")), ["0", "1"]);
  });

  test("keeps trying through server errors", async () => {
    const pig = new FakePig();
    const { core } = await setUp({ pig });
    const run = await startRun(core);
    pig.upcoming.push({ status: 503, body: {} }, { status: 502, body: {} }, "network");
    await core.add(run.id, { trial: 1 }, STAMP);
    await core.sent(run.id);
    assert.deepEqual(Object.keys(pig.stored("run-1")), ["0", "1"]);
  });
});

describe("refusals", () => {
  test("an event refused for good is kept aside, reported, and doesn't block the rest", async () => {
    const pig = new FakePig();
    const { core, notes } = await setUp({ pig });
    const run = await startRun(core);
    await core.sent(run.id);
    // Make the server already hold a different event 1, so ours collides.
    pig.run("run-1").events.set("1", JSON.stringify({ something: "else" }));

    await core.add(run.id, { trial: 1 }, STAMP);
    await core.add(run.id, { trial: 2 }, STAMP);
    await core.sent(run.id);

    assert.deepEqual(JSON.parse(pig.run("run-1").events.get("2")!).trial, 2);
    const refusal = notes.find((n) => n.type === "error" && n.code === "event-refused");
    assert.deepEqual(refusal?.type === "error" && refusal.events, ["1"]);
    assert.deepEqual(await core.pending({ run: run.id }), { events: 0, bytes: 0, failed: 1, runs: 0 });

    await core.discardFailed();
    assert.equal((await core.pending()).failed, 0);
  });

  test("a batch the web server says is too big is split up to find the culprit", async () => {
    const pig = new FakePig();
    const { core } = await setUp({ pig });
    await startRun(core);
    pig.offline = true;
    const run = await startRun(core);
    for (let i = 0; i < 3; i += 1) await core.add(run.id, { trial: i }, STAMP);
    pig.offline = false;
    // Any request carrying trial 0 is too big for the web server in front of Pig.
    pig.intercept = (method, path, body) =>
      body?.includes('"trial":0') ? new Response("", { status: 413 }) : undefined;
    core.nudge();
    await core.sent(run.id);
    assert.deepEqual(Object.keys(pig.stored("run-2")), ["0", "2", "3"]);
    assert.equal((await core.pending()).failed, 1);
  });
});

describe("closed server runs", () => {
  test("an expired run's leftovers go to a new server run that names the old one", async () => {
    const pig = new FakePig();
    const { core, notes } = await setUp({ pig });
    const run = await startRun(core);
    await core.add(run.id, { trial: 1 }, STAMP);
    await core.sent(run.id);

    pig.expire("run-1");
    await core.add(run.id, { trial: 2 }, STAMP);
    await core.sent(run.id);

    const continued = pig.stored("run-2");
    assert.equal(continued["0"]._client.continues_run, "run-1");
    assert.equal(continued["2"].trial, 2);
    assert.deepEqual(pig.run("run-2").parameters, PARAMETERS);
    assert.ok(notes.some((n) => n.type === "run" && n.run.runId === "run-2"));
  });

  test("finalizing an expired run just stops", async () => {
    const pig = new FakePig();
    const { core, store } = await setUp({ pig });
    const run = await startRun(core);
    await core.sent(run.id);
    pig.expire("run-1");
    await core.finalize(run.id);
    await core.sent(run.id);
    assert.equal(pig.runs.size, 1);
    await eventually(async () => (await store.run(run.id)) === undefined, { what: "the run to be forgotten" });
  });

  test("events for a run someone else finalized are discarded, and that's reported", async () => {
    const pig = new FakePig();
    const { core, notes } = await setUp({ pig });
    const run = await startRun(core);
    await core.sent(run.id);
    pig.run("run-1").status = "finalizing";
    await core.add(run.id, { trial: 1 }, STAMP);
    await core.sent(run.id);
    assert.equal(pig.runs.size, 1);
    assert.ok(notes.some((n) => n.type === "error" && n.code === "discarded"));
  });
});

describe("pages", () => {
  test("a run started on one page can be resumed on the next", async () => {
    const pig = new FakePig();
    const locks = new FakeLocks();
    const first = await setUp({ pig, locks });
    const run = await startRun(first.core);
    await first.core.add(run.id, { page: 1 }, STAMP);
    await first.core.close(); // the page goes away

    const second = await setUp({ pig, locks, factory: first.factory });
    const resumed = await second.core.resume(run.id);
    assert.equal(resumed.runId, "run-1");
    assert.equal(await second.core.add(run.id, { page: 2 }, STAMP), "2");
    await second.core.finalize(run.id);
    await second.core.sent(run.id);
    assert.deepEqual(Object.keys(pig.stored("run-1")), ["0", "1", "2"]);
  });

  test("a run open on another page can't be resumed", async () => {
    const pig = new FakePig();
    const locks = new FakeLocks();
    const first = await setUp({ pig, locks });
    const run = await startRun(first.core);
    const second = await setUp({ pig, locks, factory: first.factory, options: { resumeWaitMs: 50 } });
    await assert.rejects(second.core.resume(run.id), { code: "busy" });
  });

  test("resuming a finished run says so", async () => {
    const pig = new FakePig();
    const { core } = await setUp({ pig });
    await assert.rejects(core.resume("no-such-run"), { code: "not-found" });
  });

  test("a later page sends what an earlier one left, without finalizing it", async () => {
    const pig = new FakePig();
    const locks = new FakeLocks();
    const first = await setUp({ pig, locks });
    await startRun(first.core);
    pig.offline = true;
    const run = await startRun(first.core);
    await first.core.add(run.id, { trial: 1 }, STAMP);
    await first.core.close();

    pig.offline = false;
    const later = await setUp({ pig, locks, factory: first.factory });
    await later.core.sweep();
    await eventually(() => pig.runs.get("run-2")?.events.size === 2, { what: "the leftovers to be sent" });
    assert.equal(pig.run("run-2").status, "in_progress");
  });

  test("an abandoned run that asked for it is finalized by a later page", async () => {
    const pig = new FakePig();
    const locks = new FakeLocks();
    const first = await setUp({ pig, locks });
    const run = await startRun(first.core, { finalizeWhenAbandoned: true });
    await first.core.add(run.id, { trial: 1 }, STAMP);
    await first.core.sent(run.id);
    await first.core.close();

    const later = await setUp({ pig, locks, factory: first.factory });
    await later.core.sweep(); // too soon: it might be about to be resumed
    assert.equal(pig.run("run-1").status, "in_progress");

    await new Promise((r) => setTimeout(r, 120));
    await later.core.sweep();
    await eventually(() => pig.run("run-1").status === "finalizing", { what: "the abandoned run to be finalized" });
  });

  test("a run whose page is still open isn't treated as abandoned", async () => {
    const pig = new FakePig();
    const locks = new FakeLocks();
    const first = await setUp({ pig, locks });
    first.core.begin();
    await startRun(first.core, { finalizeWhenAbandoned: true });

    const other = await setUp({ pig, locks, factory: first.factory });
    await new Promise((r) => setTimeout(r, 150));
    await other.core.sweep();
    await new Promise((r) => setTimeout(r, 30));
    assert.equal(pig.run("run-1").status, "in_progress");
  });
});

describe("counting", () => {
  test("counts what's waiting per run, per task, and overall", async () => {
    const pig = new FakePig();
    pig.tasks.sret = { ...pig.tasks.stroop };
    const { core } = await setUp({ pig });
    await core.sent((await startRun(core)).id);
    await core.sent((await startRun(core, { task: "sret" })).id);
    pig.offline = true;
    const a = await startRun(core);
    const b = await startRun(core, { task: "sret" });
    await core.add(a.id, { trial: 1 }, STAMP);
    await core.add(b.id, { trial: 1 }, STAMP);
    await core.add(b.id, { trial: 2 }, STAMP);

    // Each run also has its unsent first event.
    assert.equal((await core.pending({ run: a.id })).events, 2);
    assert.equal((await core.pending({ task: "sret" })).events, 3);
    const all = await core.pending();
    assert.equal(all.events, 5);
    assert.equal(all.runs, 2);
    assert.ok(all.bytes > 0);
  });
});

test("the server address may end with a slash", async () => {
  const pig = new FakePig();
  const { core } = await setUp({ pig });
  await startRun(core, { server: `${SERVER}/` });
  assert.equal(pig.requests[0].path, "/task/stroop");
});
