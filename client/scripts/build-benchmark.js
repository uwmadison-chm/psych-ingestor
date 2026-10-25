// Builds benchmarks/dist/encryption.html: the benchmark page with its worker inside it, so
// it's one file to put on any https server and open on a phone.
//
// The worker is bundled from benchmarks/encryption-worker.ts, which uses the client's own
// encryption code, so the page times exactly what a task would run.

import { mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { build } from "vite";

const root = fileURLToPath(new URL("..", import.meta.url));
const PLACEHOLDER = '<script type="text/plain" id="worker-source"></script>';

const built = await build({
  root,
  configFile: false,
  // A dependency's comment makes Rollup warn while it bundles, harmlessly.
  logLevel: "error",
  build: {
    write: false,
    minify: true,
    target: "es2022",
    lib: {
      entry: "benchmarks/encryption-worker.ts",
      formats: ["iife"],
      name: "benchmark",
      fileName: () => "worker.js",
    },
  },
});
const worker = built[0].output[0].code;
if (worker.includes("</script")) throw new Error("The worker can't be put inside a <script> as it is.");

const page = await readFile(`${root}benchmarks/encryption.html`, "utf8");
if (!page.includes(PLACEHOLDER)) throw new Error(`benchmarks/encryption.html has no ${PLACEHOLDER}`);
await mkdir(`${root}benchmarks/dist`, { recursive: true });
await writeFile(
  `${root}benchmarks/dist/encryption.html`,
  page.replace(PLACEHOLDER, () => `<script type="text/plain" id="worker-source">${worker}</script>`),
);
console.log("Built benchmarks/dist/encryption.html");
