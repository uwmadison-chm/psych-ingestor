// Builds the three files a task needs, into dist/:
//
//   pig.js           an ES module, for `import * as pig from "./pig.js"` or a bundler
//   pig.script.js    the same, for a plain <script> tag; it defines a global `pig`
//   pig-worker.js    the web worker both of them start. Keep it next to them.
//
// Type declarations for TypeScript users go in dist/types; package.json runs tsc for those.
//
// Not minified: they're small, and people debugging a task should be able to read them.

import { fileURLToPath } from "node:url";
import { build, createLogger } from "vite";

const root = fileURLToPath(new URL("..", import.meta.url));

// pig.js finds the worker with `new URL("./pig-worker.js", import.meta.url)`, which is
// there for the bundlers of the tasks that use it, not for this build. Vite warns that it
// can't find the file next to the source; that's expected, so don't print it.
const logger = createLogger("warn");
const warn = logger.warn;
logger.warn = (message, options) => {
  if (!message.includes("doesn't exist at build time")) warn(message, options);
};
logger.warnOnce = logger.warn;

const common = {
  root,
  configFile: false,
  customLogger: logger,
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

await bundle("src/index.ts", "pig.js", "es");
await bundle("src/index.ts", "pig.script.js", "iife", "pig");
await bundle("src/worker.ts", "pig-worker.js", "iife", "pigWorker");
console.log("Built dist/pig.js, dist/pig.script.js, dist/pig-worker.js");
