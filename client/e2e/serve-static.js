// Serves the built client and the test pages, on a different port from Pig, so every
// request the client makes is cross-origin, as it is for a real task.

import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const port = Number(process.env.STATIC_PORT ?? 8766);
const roots = {
  "/dist/": fileURLToPath(new URL("../dist/", import.meta.url)),
  "/": fileURLToPath(new URL("./pages/", import.meta.url)),
};
const types = { ".html": "text/html", ".js": "text/javascript", ".map": "application/json" };

createServer((request, response) => {
  const path = decodeURIComponent(new URL(request.url, "http://x").pathname);
  const prefix = Object.keys(roots).find((p) => path.startsWith(p));
  const file = normalize(join(roots[prefix], path.slice(prefix.length)));
  if (!file.startsWith(roots[prefix]) || !existsSync(file) || !statSync(file).isFile()) {
    response.writeHead(404).end();
    return;
  }
  response.writeHead(200, { "Content-Type": types[extname(file)] ?? "application/octet-stream" });
  createReadStream(file).pipe(response);
}).listen(port, "127.0.0.1");
