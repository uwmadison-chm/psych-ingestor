// A task that checks signatures, from the built client in a real browser: a link signed
// by `pigsty sign` starts a run, and the same link with a value changed doesn't. The
// browser only passes the parameters along, so this is where `pigsty sign` and
// `pig serve` are checked against each other through a real URL.

import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import { PIG_DIR } from "../playwright.config.js";

const PIG = "http://127.0.0.1:8765";
const repo = fileURLToPath(new URL("../..", import.meta.url));

function signedLink(...values) {
  const command = (process.env.PIGSTY_COMMAND ?? "uv run pigsty").split(" ");
  const args = ["sign", "signed", "--key", "e2e-signing", "--url", "http://127.0.0.1:8766/script.html"];
  const done = spawnSync(command[0], [...command.slice(1), ...args, ...values, "--config", join(PIG_DIR, "pigsty.toml")], {
    cwd: repo,
    encoding: "utf-8",
  });
  expect(done.status, done.stderr).toBe(0);
  return done.stdout.trim();
}

test("a signed link starts a run, and an edited one doesn't", async ({ page }) => {
  const link = signedLink("participant_id=10351", "modules=questions:1,2,3|reels:03,22");
  await page.goto(link);
  const started = await page.evaluate(async (server) => {
    const run = await pig.startForURL(server, "signed", window.location);
    await run.finalize();
    await run.sent();
    return run.runNumber;
  }, PIG);
  expect(started).toBe(1);

  await page.goto(link.replace("reels%3A03%2C22", "reels%3A01"));
  const refused = await page.evaluate(async (server) => {
    try {
      await pig.startForURL(server, "signed", window.location);
      return null;
    } catch (error) {
      return error.code;
    }
  }, PIG);
  expect(refused).toBe("bad-signature");
});
