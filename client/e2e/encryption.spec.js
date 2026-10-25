// An encrypted task, from the built client in a real browser, through a real `pig serve`
// and `pig sweep`, to `pigsty admit` decrypting it. The browser encrypts with typage and
// the secure side decrypts with pyrage, so this is also where the two are checked
// against each other.

import { spawnSync } from "node:child_process";
import { cpSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import { PIG_DIR } from "../playwright.config.js";

const PIG = "http://127.0.0.1:8765";
const repo = fileURLToPath(new URL("../..", import.meta.url));

function run(variable, fallback, ...args) {
  const command = (process.env[variable] ?? fallback).split(" ");
  const done = spawnSync(command[0], [...command.slice(1), ...args], { cwd: repo, encoding: "utf-8" });
  expect(done.status, done.stderr).toBe(0);
}

test("an encrypted run is unreadable on the web server and decrypted by admit", async ({ page, request }) => {
  const settings = await (await request.get(`${PIG}/task/sealed`)).json();
  expect(settings.encryption.public_key).toMatch(/^age1/);

  await page.goto("/script.html");
  const runId = await page.evaluate(async (server) => {
    const run = await pig.start(server, "sealed", { participant_id: "10351" });
    await run.add({ trial: 1, rt: 523 });
    const recording = await run.startMedia({ content_type: "application/octet-stream" });
    await recording.add(new Blob([new Uint8Array(150 * 1024).map((_, i) => i % 251)]));
    await recording.finish();
    await run.finalize();
    await run.sent();
    return run.runId;
  }, PIG);

  // On the web server, nothing the task recorded is readable.
  const events = readFileSync(join(PIG_DIR, "data", "in_progress", "sealed", runId, "events.jsonl"), "utf-8");
  expect(events).not.toContain("523");
  expect(events).not.toContain("run_start");

  run("PIG_COMMAND", "uv run pig", "sweep", "--config", join(PIG_DIR, "pig.toml"));
  cpSync(join(PIG_DIR, "data", "done", "sealed", runId), join(PIG_DIR, "sealed", "incoming", runId), { recursive: true });
  run("PIGSTY_COMMAND", "uv run pigsty", "admit", "--config", join(PIG_DIR, "pigsty.toml"));

  const admitted = join(PIG_DIR, "sealed", "runs", runId);
  const lines = readFileSync(join(admitted, "events.jsonl"), "utf-8").trim().split("\n").map(JSON.parse);
  expect(lines.map((line) => line.data._client?.event ?? line.data.trial ?? line.data.content_type)).toEqual([
    "run_start",
    1,
    "application/octet-stream",
  ]);
  const media = join(admitted, "media", "00001");
  const parts = readdirSync(media).sort();
  expect(parts.length).toBeGreaterThan(2);
  const joined = Buffer.concat(parts.map((name) => readFileSync(join(media, name))));
  expect(joined).toEqual(Buffer.from(new Uint8Array(150 * 1024).map((_, i) => i % 251)));
});
