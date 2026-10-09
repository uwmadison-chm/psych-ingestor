import { defineConfig } from "@playwright/test";

const PIG_PORT = 8765;
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
      env: { PIG_PORT: String(PIG_PORT) },
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
