// Builds bundled/ with Vite, into bundled/dist/, for the "bundled by Vite" test. Runs
// before the static server starts; see playwright.config.js. Needs the client built.

import { fileURLToPath } from "node:url";
import { build } from "vite";

await build({
  root: fileURLToPath(new URL("./bundled/", import.meta.url)),
  configFile: false,
  logLevel: "warn",
  base: "./", // it's served from /bundled/, not the root
  build: { outDir: "dist", emptyOutDir: true, minify: false },
});
