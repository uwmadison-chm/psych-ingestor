import { tmpdir } from "node:os";
import { join } from "node:path";
import { defineConfig } from "@playwright/test";

const PIG_PORT = 8765;
// Where the browser tests' Pig keeps its configuration and data, so they can read its files.
export const PIG_DIR = join(tmpdir(), "pig-client-e2e");
const STATIC_PORT = 8766;

export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  workers: 1, // one Pig, one browser profile: tests share IndexedDB per origin
  use: { baseURL: `http://127.0.0.1:${STATIC_PORT}` },
  webServer: [
    {
      command: "node e2e/serve-pig.js",
      url: `http://127.0.0.1:${PIG_PORT}/health`,
      env: { PIG_PORT: String(PIG_PORT), PIG_DIR },
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: "node e2e/serve-static.js",
      url: `http://127.0.0.1:${STATIC_PORT}/script.html`,
      env: { STATIC_PORT: String(STATIC_PORT) },
      reuseExistingServer: false,
    },
  ],
});
