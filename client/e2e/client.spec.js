// The built client in a real browser, against a real `pig serve`.

import { expect, test } from "@playwright/test";

const PIG = "http://127.0.0.1:8765";
const LINK = "?participant_id=10351&session=baseline";

/** What the server holds for a run. */
async function serverRun(request, runId) {
  const reply = await request.get(`${PIG}/task/stroop/run/${runId}`);
  expect(reply.ok()).toBeTruthy();
  return reply.json();
}

test("a whole run from a <script> tag", async ({ page, request }) => {
  await page.goto(`/script.html${LINK}`);
  const runId = await page.evaluate(async (server) => {
    const run = await pig.start({ server, task: "stroop" });
    run.add({ type: "trial", rt: 843 }); // not awaited: the return-at-once way
    await run.add({ type: "trial", rt: 612 });
    await run.finalize();
    await run.sent();
    return run.runId;
  }, PIG);

  const held = await serverRun(request, runId);
  expect(held.status).not.toBe("in_progress");
  expect(held.stored).toEqual(["0", "1", "2"]);
});

test("a whole run from an ES module", async ({ page, request }) => {
  await page.goto(`/module.html${LINK}`);
  await page.waitForFunction(() => window.pig);
  const runId = await page.evaluate(async (server) => {
    const run = await pig.start({ server, task: "stroop" });
    await run.add({ type: "trial" });
    await run.finalize();
    await run.sent();
    return run.runId;
  }, PIG);
  expect((await serverRun(request, runId)).stored).toEqual(["0", "1"]);
});

test("a closed task fails at start()", async ({ page }) => {
  await page.goto(`/script.html?participant_id=10351`);
  const code = await page.evaluate(async (server) => {
    try {
      await pig.start({ server, task: "closed" });
    } catch (error) {
      return error.code;
    }
  }, PIG);
  expect(code).toBe("task-closed");
});

test("a run recorded offline reaches the server once the browser is back online", async ({ page, context, request }) => {
  await page.goto(`/script.html${LINK}`);
  // Online once, so the task's settings are saved on this device.
  await page.evaluate(async (server) => {
    const run = await pig.start({ server, task: "stroop" });
    await run.sent();
  }, PIG);

  await context.setOffline(true);
  const local = await page.evaluate(async (server) => {
    window.offlineRun = await pig.start({ server, task: "stroop" });
    for (let i = 1; i <= 3; i += 1) await window.offlineRun.add({ trial: i });
    await window.offlineRun.finalize();
    return { runId: window.offlineRun.runId, pending: await pig.pending() };
  }, PIG);
  expect(local.runId).toBeNull();
  expect(local.pending.events).toBe(4);

  await context.setOffline(false);
  const runId = await page.evaluate(async () => {
    await window.offlineRun.sent();
    return window.offlineRun.runId;
  });
  expect(runId).not.toBeNull();
  expect((await serverRun(request, runId)).stored).toEqual(["0", "1", "2", "3"]);
});

test("a run carries on across a page change", async ({ page, request }) => {
  await page.goto(`/script.html${LINK}`);
  await page.evaluate(async (server) => {
    const run = await pig.start({ server, task: "stroop" });
    await run.add({ page: 1 });
    sessionStorage.setItem("run", run.id);
  }, PIG);

  await page.goto(`/script.html${LINK}`);
  const runId = await page.evaluate(async () => {
    const run = await pig.resume(sessionStorage.getItem("run"));
    await run.add({ page: 2 });
    await run.finalize();
    await run.sent();
    return run.runId;
  });
  expect((await serverRun(request, runId)).stored).toEqual(["0", "1", "2"]);
});

test("an abandoned run is finalized when a later page loads the client", async ({ context, request }) => {
  const timing = { abandonedAfterMs: 500, sweepMs: 200, heartbeatMs: 100 };
  const first = await context.newPage();
  await first.goto(`/script.html${LINK}`);
  const runId = await first.evaluate(
    async ({ server, timing }) => {
      const run = await pig.start({ server, task: "stroop", finalizeWhenAbandoned: true, timing });
      await run.add({ trial: 1 });
      await run.sent();
      return run.runId;
    },
    { server: PIG, timing },
  );
  await first.close(); // the participant closes the tab

  const later = await context.newPage();
  await later.goto(`/script.html${LINK}`);
  await later.evaluate((timing) => pig.connect({ timing }), timing);
  await expect.poll(async () => (await serverRun(request, runId)).status, { timeout: 10_000 }).not.toBe("in_progress");
  expect((await serverRun(request, runId)).stored).toEqual(["0", "1"]);
});

test("progress and the debug log are there to read", async ({ page }) => {
  await page.goto(`/script.html${LINK}`);
  const result = await page.evaluate(async (server) => {
    const seen = [];
    pig.events.addEventListener("progress", (e) => seen.push(e.detail.pending));
    const run = await pig.start({ server, task: "stroop" });
    await run.add({ trial: 1 });
    await run.sent();
    return { seen: seen.length, log: pig.debugLog().length, pending: await run.pending() };
  }, PIG);
  expect(result.seen).toBeGreaterThan(0);
  expect(result.log).toBeGreaterThan(0);
  expect(result.pending).toEqual({ events: 0, bytes: 0, failed: 0, runs: 0 });
});
