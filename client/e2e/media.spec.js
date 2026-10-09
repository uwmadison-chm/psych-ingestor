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

test("record() sends what a MediaRecorder records and finishes when it stops", async ({ page, request }) => {
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
    recorder.start(200);
    const recording = await run.startMedia({ content_type: recorder.mimeType });
    recording.record(recorder);
    await new Promise((resolve) => setTimeout(resolve, 1200));
    recorder.stop();
    clearInterval(draw);
    await new Promise((resolve) => recorder.addEventListener("stop", resolve));
    await run.finalize();
    await run.sent();
    return run.runId;
  }, PIG);

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
      recorder.start(200);
      const item = await run.startMedia({ content_type: recorder.mimeType, clip });
      item.record(recorder);
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
