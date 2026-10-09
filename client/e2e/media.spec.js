// Media items from the built client, in a real browser, against a real `pig serve`.
// These read the part files Pig wrote, to check they join back together with `cat`.

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "@playwright/test";
import { PIG_DIR } from "../playwright.config.js";

const PIG = "http://127.0.0.1:8765";

/** What the server says about a run of the voice task. */
async function serverRun(request, runId) {
  const reply = await request.get(`${PIG}/task/voice/run/${runId}`);
  expect(reply.ok()).toBeTruthy();
  return reply.json();
}

/** A media item's part files, in name order, joined: what `cat media/00001/*.part` gives. */
function catParts(runId, mediaId) {
  const dir = join(PIG_DIR, "data", "in_progress", "voice", runId, "media", String(mediaId).padStart(5, "0"));
  const names = readdirSync(dir).filter((name) => name.endsWith(".part")).sort();
  return { names, bytes: Buffer.concat(names.map((name) => readFileSync(join(dir, name)))) };
}

test("a blob bigger than a part is cut up, sent, and joins back together", async ({ page, request }) => {
  await page.goto("/script.html");
  const runId = await page.evaluate(async (server) => {
    const run = await pig.start(server, "voice", { participant_id: "10351" });
    const recording = await run.startMedia({ content_type: "application/octet-stream" });
    // 200 KB of a repeating pattern, in parts of at most 64 KB.
    const bytes = new Uint8Array(200 * 1024).map((_, i) => i % 251);
    window.parts = await recording.add(new Blob([bytes]));
    await recording.finish();
    await run.sent();
    return run.runId;
  }, PIG);

  expect(await page.evaluate(() => window.parts)).toEqual([1, 2, 3, 4]);
  const [item] = (await serverRun(request, runId)).media;
  expect(item).toMatchObject({ event_id: "1", stored: [1, 2, 3, 4], finished: true, parts: 4 });

  const { names, bytes } = catParts(runId, item.media_id);
  expect(names).toEqual(["000001.part", "000002.part", "000003.part", "000004.part"]);
  expect(bytes.length).toBe(200 * 1024);
  expect(bytes.every((byte, i) => byte === i % 251)).toBe(true);
});

test("record() starts a MediaRecorder, sends what it records, and finishes when it stops", async ({ page, request }) => {
  await page.goto("/script.html");
  const runId = await page.evaluate(async (server) => {
    // Something to record without a camera: a canvas, redrawn.
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d");
    let frame = 0;
    const draw = setInterval(() => {
      context.fillStyle = `hsl(${(frame += 10) % 360} 80% 50%)`;
      context.fillRect(0, 0, canvas.width, canvas.height);
    }, 20);
    const recorder = new MediaRecorder(canvas.captureStream(30));

    const run = await pig.start(server, "voice", { participant_id: "10351" });
    const before = performance.now();
    await run.record(recorder, { prompt: 3 }, { timeslice: 200 });
    const after = performance.now();
    await new Promise((resolve) => setTimeout(resolve, 1200));
    // The recorder's own stop(), then waiting for it, works as well as recording.stop().
    recorder.stop();
    clearInterval(draw);
    await new Promise((resolve) => recorder.addEventListener("stop", resolve));
    await run.finalize();
    await run.sent();
    window.timing = { before, after, mimeType: recorder.mimeType };
    return run.runId;
  }, PIG);

  // The item's event is stamped with the recorder's start, on the page's clock.
  const { before, after, mimeType } = await page.evaluate(() => window.timing);
  const event = readFileSync(join(PIG_DIR, "data", "in_progress", "voice", runId, "events.jsonl"), "utf8")
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line))
    .find((line) => line.event_id === "1");
  expect(event.data.prompt).toBe(3);
  expect(event.data.content_type).toBe(mimeType);
  expect(event.data._client.performance_now).toBeGreaterThanOrEqual(before);
  expect(event.data._client.performance_now).toBeLessThanOrEqual(after);

  const held = await serverRun(request, runId);
  const [item] = held.media;
  expect(item.finished).toBe(true);
  expect(item.parts).toBeGreaterThan(1);
  expect(item.stored).toEqual(Array.from({ length: item.parts }, (_, i) => i + 1));
  // A WebM file starts with the EBML header.
  expect(catParts(runId, item.media_id).bytes.subarray(0, 4)).toEqual(Buffer.from([0x1a, 0x45, 0xdf, 0xa3]));
});

test("one recorder, stopped and started again, makes one media item per clip", async ({ page, request }) => {
  await page.goto("/script.html");
  const runId = await page.evaluate(async (server) => {
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d");
    const draw = setInterval(() => context.fillRect(0, 0, 10, 10), 20);
    const recorder = new MediaRecorder(canvas.captureStream(30));
    const run = await pig.start(server, "voice", { participant_id: "10351" });

    for (const clip of ["first", "second"]) {
      await run.record(recorder, { clip }, { timeslice: 200 });
      await new Promise((resolve) => setTimeout(resolve, 600));
      const stopped = new Promise((resolve) => recorder.addEventListener("stop", resolve, { once: true }));
      recorder.stop();
      await stopped;
    }
    clearInterval(draw);
    await run.sent();
    return run.runId;
  }, PIG);

  const { media } = await serverRun(request, runId);
  expect(media).toHaveLength(2);
  for (const item of media) {
    expect(item.finished).toBe(true);
    expect(catParts(runId, item.media_id).bytes.subarray(0, 4)).toEqual(Buffer.from([0x1a, 0x45, 0xdf, 0xa3]));
  }
});

test("recording.stop() waits for the last blob, so finalizing straight after loses nothing", async ({ page, request }) => {
  await page.goto("/script.html");
  const { runId, errors } = await page.evaluate(async (server) => {
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d");
    const draw = setInterval(() => context.fillRect(0, 0, 10, 10), 20);
    const recorder = new MediaRecorder(canvas.captureStream(30));
    const run = await pig.start(server, "voice", { participant_id: "10351" });
    const errors = [];
    run.addEventListener("error", (e) => errors.push(e.detail.code));

    const recording = await run.record(recorder, {}, { timeslice: 200 });
    await new Promise((resolve) => setTimeout(resolve, 600));
    await recording.stop();
    clearInterval(draw);
    await run.finalize();
    await run.sent();
    return { runId: run.runId, errors };
  }, PIG);
  expect(errors).toEqual([]);
  const [item] = (await serverRun(request, runId)).media;
  expect(item.finished).toBe(true);
});

test("finalize() straight after the recorder's own stop() still waits for its last blob", async ({ page, request }) => {
  await page.goto("/script.html");
  const { runId, errors } = await page.evaluate(async (server) => {
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d");
    const draw = setInterval(() => context.fillRect(0, 0, 10, 10), 20);
    const recorder = new MediaRecorder(canvas.captureStream(30));
    const run = await pig.start(server, "voice", { participant_id: "10351" });
    const errors = [];
    run.addEventListener("error", (e) => errors.push(e.detail.code));

    await run.record(recorder, {}, { timeslice: 60000 });
    await new Promise((resolve) => setTimeout(resolve, 600));
    recorder.stop();
    await run.finalize(); // before the recorder has handed over its last blob
    clearInterval(draw);
    await run.sent();
    return { runId: run.runId, errors };
  }, PIG);
  expect(errors).toEqual([]);
  const [item] = (await serverRun(request, runId)).media;
  expect(item.finished).toBe(true);
  expect(item.parts).toBeGreaterThan(0);
});

test("finalize() stops a recording that's still going, and keeps its last blob", async ({ page, request }) => {
  await page.goto("/script.html");
  const { runId, errors, state } = await page.evaluate(async (server) => {
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d");
    const draw = setInterval(() => context.fillRect(0, 0, 10, 10), 20);
    const recorder = new MediaRecorder(canvas.captureStream(30));
    const run = await pig.start(server, "voice", { participant_id: "10351" });
    const errors = [];
    run.addEventListener("error", (e) => errors.push(e.detail.code));
    const sizes = [];
    recorder.addEventListener("dataavailable", (e) => sizes.push(e.data.size));

    // A long timeslice, so the only blob is the one the recorder hands over as it stops.
    run.record(recorder, {}, { timeslice: 60000 }); // not awaited
    await new Promise((resolve) => setTimeout(resolve, 600));
    await run.finalize();
    clearInterval(draw);
    await run.sent();
    window.sizes = sizes;
    return { runId: run.runId, errors, state: recorder.state };
  }, PIG);
  expect(state).toBe("inactive");
  expect(errors).toEqual([]);
  const [item] = (await serverRun(request, runId)).media;
  expect(item.finished).toBe(true);
  expect(item.parts).toBeGreaterThan(0);
  expect(catParts(runId, item.media_id).bytes.subarray(0, 4)).toEqual(Buffer.from([0x1a, 0x45, 0xdf, 0xa3]));
});

test("stop() is only for recordings", async ({ page }) => {
  await page.goto("/script.html");
  const code = await page.evaluate(async (server) => {
    const run = await pig.start(server, "voice", { participant_id: "10351" });
    const image = await run.startMedia({ content_type: "image/png" });
    try {
      await image.stop();
    } catch (error) {
      return error.code;
    }
  }, PIG);
  expect(code).toBe("bad-call");
});

test("progress counts down while a part uploads", async ({ page }) => {
  await page.goto("/script.html");
  const seen = await page.evaluate(async (server) => {
    const run = await pig.start(server, "voice", { participant_id: "10351" });
    await run.sent();
    const bytes = [];
    run.addEventListener("progress", (e) => bytes.push(e.detail.pending.bytes));
    const recording = await run.startMedia({ content_type: "application/octet-stream" });
    await recording.add(new Blob([new Uint8Array(60 * 1024)]));
    await recording.finish();
    await run.sent();
    return bytes;
  }, PIG);
  expect(seen.length).toBeGreaterThan(0);
  expect(seen.at(-1)).toBe(0);
  for (let i = 1; i < seen.length; i += 1) expect(seen[i]).toBeLessThanOrEqual(seen[i - 1]);
});

test("record() finishes a clip stopped before its item was even stored", async ({ page, request }) => {
  await page.goto("/script.html");
  const runId = await page.evaluate(async (server) => {
    const canvas = document.createElement("canvas");
    canvas.getContext("2d").fillRect(0, 0, 10, 10);
    const recorder = new MediaRecorder(canvas.captureStream(30));
    const run = await pig.start(server, "voice", { participant_id: "10351" });
    recorder.addEventListener("start", () => recorder.stop(), { once: true });
    await run.record(recorder);
    await run.sent();
    return run.runId;
  }, PIG);
  const [item] = (await serverRun(request, runId)).media;
  expect(item.finished).toBe(true);
});

test("record() won't take a recorder that's already running", async ({ page }) => {
  await page.goto("/script.html");
  const code = await page.evaluate(async (server) => {
    const canvas = document.createElement("canvas");
    canvas.getContext("2d");
    const recorder = new MediaRecorder(canvas.captureStream(30));
    const run = await pig.start(server, "voice", { participant_id: "10351" });
    recorder.start();
    try {
      await run.record(recorder);
    } catch (error) {
      return error.code;
    } finally {
      recorder.stop();
    }
  }, PIG);
  expect(code).toBe("bad-call");
});

test("a task that doesn't take media says so at startMedia()", async ({ page }) => {
  await page.goto("/script.html");
  const code = await page.evaluate(async (server) => {
    const run = await pig.start(server, "stroop", { participant_id: "10351", session: "baseline" });
    try {
      await run.startMedia({ content_type: "audio/webm" });
    } catch (error) {
      return error.code;
    }
  }, PIG);
  expect(code).toBe("media-off");
});
