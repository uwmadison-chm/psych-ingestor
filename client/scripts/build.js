// Builds the three files a task needs, into dist/:
//
//   pig.js           an ES module, for `import * as pig from "./pig.js"` or a bundler
//   pig.script.js    the same, for a plain <script> tag; it defines a global `pig`
//   pig-worker.js    the web worker both of them start. Keep it next to them.
//
// Not minified: they're small, and people debugging a task should be able to read them.

import { fileURLToPath } from "node:url";
import { build } from "vite";

const root = fileURLToPath(new URL("..", import.meta.url));

const common = {
  root,
  configFile: false,
  logLevel: "warn",
  build: { outDir: "dist", emptyOutDir: false, minify: false, sourcemap: true, target: "es2022" },
};

async function bundle(entry, fileName, format, name) {
  await build({
    ...common,
    build: {
      ...common.build,
      lib: { entry, formats: [format], name, fileName: () => fileName },
    },
  });
}

await bundle("src/index.js", "pig.js", "es");
await bundle("src/index.js", "pig.script.js", "iife", "pig");
await bundle("src/worker.js", "pig-worker.js", "iife", "pigWorker");
console.log("Built dist/pig.js, dist/pig.script.js, dist/pig-worker.js");
