// Runs a real Pig for the browser tests, with a fresh configuration and data directory
// each time, in PIG_DIR, where the tests can look at what Pig wrote. PIG_COMMAND says
// how to run it; the default suits a uv checkout.

import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

const port = process.env.PIG_PORT ?? "8765";
const dir = process.env.PIG_DIR;
rmSync(dir, { recursive: true, force: true });
mkdirSync(dir, { recursive: true });
const repo = fileURLToPath(new URL("../..", import.meta.url));
const pigstyCommand = (process.env.PIGSTY_COMMAND ?? "uv run pigsty").split(" ");

// The secure side, for the encrypted task: a private key, and the public keys file the
// web side reads. encryption.spec.js admits runs with the same pigsty.toml.
writeFileSync(
  join(dir, "pigsty.toml"),
  `private_keys_dir = "./private_keys"

[task.sealed]
datastore_base = "./sealed"
organized = "./organized/sealed/{participant_id}_{run_number}"
`,
);
function pigsty(...args) {
  const done = spawnSync(pigstyCommand[0], [...pigstyCommand.slice(1), ...args, "--config", join(dir, "pigsty.toml")], {
    cwd: repo,
    encoding: "utf-8",
  });
  if (done.status !== 0) throw new Error(`pigsty ${args.join(" ")} failed: ${done.stderr}`);
  return done.stdout;
}
pigsty("keys", "new", "e2e");
writeFileSync(join(dir, "public_keys.txt"), pigsty("keys", "export"));

const config = join(dir, "pig.toml");
writeFileSync(
  config,
  `data_root = "./data"
database = "./pig.db"
public_keys_file = "./public_keys.txt"

[task.stroop]
path_parameters = ["participant_id", "session"]
run_key_parameters = ["participant_id", "session"]

# Small parts, so a modest recording is cut into several.
[task.voice]
path_parameters = ["participant_id"]
run_key_parameters = ["participant_id"]
media = true
max_part_size = "64K"

# Encrypted in the browser. Small parts, so the recording is cut into several.
[task.sealed]
path_parameters = ["participant_id"]
run_key_parameters = ["participant_id"]
media = true
max_part_size = "64K"
encrypt_to = "e2e"

[task.closed]
path_parameters = ["participant_id"]
run_key_parameters = ["participant_id"]
open = false
`,
);

const command = (process.env.PIG_COMMAND ?? "uv run pig").split(" ");
const pig = spawn(command[0], [...command.slice(1), "serve", "--port", port, "--config", config], {
  cwd: repo,
  stdio: "inherit",
});
pig.on("exit", (code) => process.exit(code ?? 1));
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => pig.kill(signal));
