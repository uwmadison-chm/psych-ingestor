// Runs a real Pig for the browser tests, with a fresh configuration and data directory
// each time, in PIG_DIR, where the tests can look at what Pig wrote. PIG_COMMAND says
// how to run it; the default suits a uv checkout.

import { spawn } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

const port = process.env.PIG_PORT ?? "8765";
const dir = process.env.PIG_DIR;
rmSync(dir, { recursive: true, force: true });
mkdirSync(dir, { recursive: true });
const config = join(dir, "pig.toml");
writeFileSync(
  config,
  `data_root = "./data"
database = "./pig.db"

[task.stroop]
parameters = ["participant_id", "session"]
run_key = ["participant_id", "session"]

# Small parts, so a modest recording is cut into several.
[task.voice]
parameters = ["participant_id"]
run_key = ["participant_id"]
media = true
max_part_size = "64K"

[task.closed]
parameters = ["participant_id"]
run_key = ["participant_id"]
open = false
`,
);

const command = (process.env.PIG_COMMAND ?? "uv run pig").split(" ");
const repo = fileURLToPath(new URL("../..", import.meta.url));
const pig = spawn(command[0], [...command.slice(1), "serve", "--port", port, "--config", config], {
  cwd: repo,
  stdio: "inherit",
});
pig.on("exit", (code) => process.exit(code ?? 1));
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => pig.kill(signal));
