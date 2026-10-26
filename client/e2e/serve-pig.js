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

// The secure side, for the encrypted task and the signed one: a private key of each
// kind, and the public keys file the web side reads. encryption.spec.js admits runs, and
// signing.spec.js signs links, with the same pigsty.toml.
writeFileSync(
  join(dir, "pigsty.toml"),
  `private_keys_dir = "./private_keys"

[task.sealed]
datastore_base = "./sealed"
organized = "./organized/sealed/{participant_id}_{run_number}"

[task.signed]
datastore_base = "./signed"
organized = "./organized/signed/{participant_id}_{run_number}"
signed_parameters = ["participant_id", "modules"]
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
// Admit goes through every task in pigsty.toml, and the signed task's runs never arrive.
mkdirSync(join(dir, "signed", "incoming"), { recursive: true });
pigsty("keys", "new", "e2e");
pigsty("keys", "new", "--signing", "e2e-signing");
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

# Only starts runs from signed links. modules is signed text, as Teddy's is.
[task.signed]
path_parameters = ["participant_id"]
run_key_parameters = ["participant_id"]
signed_parameters = ["participant_id", "modules"]
signed_by = ["e2e-signing"]

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
