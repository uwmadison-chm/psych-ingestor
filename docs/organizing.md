# Organizing finished runs

Pig stores each finished run in a directory named for its run ID: `done/stroop/9f3c1a7e-…/`. That's the record, and it's deliberately not something a person can browse. On the machine where the data ends up, three commands take it from there:

- `pigsty admit` takes runs as they arrive from the web server and moves each one into place once it's whole.
- `pigsty purge` asks the web server to delete each run that's been admitted, so the data doesn't stay on the public-facing machine.
- `pigsty organize` copies each admitted run to a path built from its parameters:

```
/secure/mystudy/intermediate_data/pig/stroop/baseline/10351_0001/
    pig-run.json
    events.jsonl
    media/prompt3_audio/000001.part     # if the task sent media
                        000002.part
```

Neither runs on the web server. Neither reads `pig.toml` or Pig's database, so neither has to be there. Everything they need is in each run.

The same machine also signs links, with `pigsty sign`, for tasks that only start runs from signed ones. See [Signing links](#signing-links).

## pigsty.toml

Where the data ends up gets its own configuration file, `pigsty.toml`, separate from the web server's `pig.toml`. Keep the two on different machines: `pigsty.toml` is for the side that will eventually hold private keys, and nothing about it should be on a public-facing server.

At the top, `incoming_dir` says where runs arrive from the web server. It has the same shape as the web server's `done/`: a directory for each task, and a directory for each run inside that. Then each task gets an entry saying where its runs are kept and where its tree goes:

```toml
# pigsty.toml
incoming_dir = "/secure/pig/incoming"

[task.stroop]
datastore_dir = "/some/mount/mystudy/primary_data/pig/stroop"
organized = "/some/mount/mystudy/intermediate_data/pig/stroop/{session}/{participant_id}_{run_number}"
```

A task with no entry is never admitted or organized, even if its runs are in `incoming_dir`. Two tasks can't share a `datastore_dir`, and neither can share where they're organized; see [Patterns](#patterns). No `datastore_dir` can be inside `incoming_dir`, or the other way around, because the copy into `incoming_dir` deletes whatever the bucket doesn't have.

The two sides have their own commands as well as their own files. `pig` runs on the web server and reads `pig.toml`; `pigsty` runs where the data ends up and reads `pigsty.toml`, never `pig.toml` or the database. `pigsty check` checks the file, the way `pig check` checks `pig.toml`, and lists the directories it names that aren't there yet. `pigsty check --fix` makes them: `private_keys_dir`, readable only by you, `incoming_dir`, and each task's `runs/` and the directory its tree goes in. It never makes a key, and never changes the permissions of anything already there. Paths are relative to the file, as they are in `pig.toml`. Every `pigsty` command reads `local/pigsty.toml`, or the file named by `--config` or `PIGSTY_CONFIG` (see [Environment variables](configuration.md#environment-variables)).

`run_as` works as it does in `pig.toml` (see [configuration.md](configuration.md#running-as-one-user)): when it's set, every `pigsty` command refuses to run as any other user, so `admit`, `organize`, and `keys new` never make files that belong to someone else.

```toml
# pigsty.toml
run_as = "pigsty"
```

## Trying it on a laptop

`pigsty.example.toml` in the repository is a starting point. Copy it to `local/pigsty.toml`, where its paths are under `local/`. On one machine there's no bucket, so its `incoming_dir` is your local Pig's `done/`:

```toml
# local/pigsty.toml
incoming_dir = "./data/done"
```

Then admit and organize your finished stroop runs:

```
pigsty check --fix
pigsty admit
pigsty organize
```

The tree is in `local/organized/stroop/`. Add `--dry-run` to `pigsty organize` to see where each run would go without copying anything.

With `pig serve` running, `pigsty purge` then deletes those runs from `local/data/done/stroop/`, which is also `incoming_dir`, so they're gone from both at once. The same setting works for a real deployment that keeps Pig and pigsty on one machine, though that puts the private keys on the machine that faces the internet.

## Copying runs from the bucket

The web server copies its `done/` to a bucket (see [deployment.md](deployment.md#copying-finished-runs-to-the-bucket)), so the bucket has the shape `incoming_dir` wants. Copy it down with `rclone sync`, only the tasks in `pigsty.toml`, since a bucket can hold other studies' tasks too:

```
rclone sync pigbucket:chm-wilbur-01/ /secure/pig/incoming/ \
  --checksum --immutable --fast-list \
  --include "/teddy/**" --include "/teddy_followup/**"
```

`contrib/rclone/` has `pigsty-fetch`, which reads `incoming_dir` and the task list from `pigsty.toml` and runs exactly that, so neither has to be typed. See its README.

- Use `sync`, not `copy`. Purged runs leave `incoming_dir` because they've left the bucket, and `copy` would keep them forever. With `--include`, sync only deletes inside the tasks it was given.
- `--checksum` compares files by hash. On S3 the hash comes with the listing, so that's faster than comparing sizes and times, as well as stricter.
- `--immutable` stops rather than overwrite a file that's changed. A finished run's files never change.
- The destination is `incoming_dir` and nothing else. Sync deletes whatever the bucket doesn't have, so pointed at a `datastore_dir` it would delete admitted runs. That's the one way this pipeline can lose the secure side's copy, and the reason to let `pigsty-fetch` build the command.

Run the copy, `pigsty admit`, and `pigsty purge` one after another, from one scheduled job, never at the same time. rclone downloads each file under a temporary name, and admit refuses a run with a file its manifest doesn't list. In systemd, a `oneshot` service with an `ExecStart=` for each does it, and stops at the first one that fails. Every 15 minutes to an hour is plenty: nothing is waiting except the web server, to be rid of the data.

## Admitting downloaded runs

On a real secure server, runs arrive by a copy Pig doesn't run: `rclone sync` from the bucket the web server uploads to, or something like it, into `incoming_dir`. That copy isn't all-at-once. A run can be half there for a while, and its manifest can arrive before the files it lists. `pigsty admit` is what stands between that and everything else: it moves a run into place only once it's whole.

`pigsty admit` reads each task's runs from `{incoming_dir}/{task_code}/`, and puts each one in `runs/`, inside the task's `datastore_dir`, once every file its manifest lists is there, with the right size and hash, and nothing else is. A task with nothing in `incoming_dir` yet isn't a problem; `incoming_dir` itself not being there is.

A run in `runs/` holds its files exactly as the web server stored them, decrypted if the task encrypts (see [Encrypted runs](#encrypted-runs)), and `pig-run.json`, which says what the run is: task, run number, parameters, media. It's the manifest without the file hashes. The manifest itself stays in `incoming_dir`, because it describes the files as they traveled.

Running `pigsty admit` again is safe, and it's how new runs get admitted. A run already in `runs/` is left alone. A run still downloading is left for next time and isn't an error. A run that's all there but doesn't match its manifest is reported, nothing of it is admitted, and `pigsty admit` exits with an error so a scheduled job notices. Nothing in `incoming_dir` is ever changed: it's a copy of the bucket, and runs leave it when they leave the bucket.

A run with a symbolic link anywhere in it is refused too. Pig never makes one, and one could point anywhere on the secure machine.

Only one `pigsty admit` works on a task at a time.

## Purging admitted runs from the web server

Once a run is admitted, the web server doesn't need its copy, and shouldn't keep it: it's the machine on the public internet. `pigsty purge` asks it to delete each one. It needs the web server's address, once, at the top of `pigsty.toml`:

```toml
# pigsty.toml
pig_url = "https://pig.example.edu"
```

For each run that's in both `runs/` and `incoming_dir`, `pigsty purge` hashes the files in `incoming_dir` and sends the hashes to Pig, which deletes its copy only if every one matches its own. It hashes `incoming_dir` because that's a copy of the bucket, so its files are exactly the web server's. A run still downloading doesn't match, and isn't purged. Neither is a directory in `runs/` without the `pig-run.json` admit writes, naming that run and task: being in `runs/` only counts if admit put it there, not if it was copied in by hand or half-restored from a backup. How the hashes work is in [api.md](api.md#purging-a-finished-run).

Then the run leaves on its own. The web server's next copy to the bucket deletes it there, and this side's next copy from the bucket deletes it from `incoming_dir`. Its copy in `runs/` stays. So a run is in `incoming_dir` from when it arrives until it's purged and both copies have run, and a run that stays in `incoming_dir` long after it was admitted means something in that chain isn't working.

Running `pigsty purge` again is safe. A purged run gets a note, `runs/{run_id}/.state/purged`, saying when, and isn't hashed again. Organize doesn't copy `.state/` into the tree. A run Pig refuses to purge is reported with Pig's reason, the rest carry on, and `pigsty purge` exits with an error so a scheduled job notices.

Run it right after `pigsty admit`, from the same scheduled job. Between the two, a run's data is on both machines.

## Encrypted runs

A task with `encrypt_to` in `pig.toml` encrypts everything in the participant's browser, and `pigsty admit` decrypts it on the way into `runs/`. So `runs/`, the organized tree, and anything that reads them see the data as the task recorded it, and nothing after admit needs to know about encryption.

The private keys that decrypt runs live in one directory, named at the top of `pigsty.toml`:

```toml
# pigsty.toml
private_keys_dir = "/etc/pigsty/private_keys"
```

Each key is a file in it, and its name is its path without `.key`: `lucida/2026a.key` is the key `lucida/2026a`. You don't need to write these files by hand. To set up a key:

```
pigsty keys new lucida/2026a
pigsty keys export > public_keys.txt
```

The first makes `lucida/2026a.key`, readable only by you. The second writes every key's public half to `public_keys.txt`. Copy that file to the web server, point `public_keys_file` at it, in `pig.toml` or in the study's own file of tasks (see [configuration.md](configuration.md#one-file-per-study)), and set `encrypt_to = "lucida/2026a"` for each task that should use the key. See [configuration.md](configuration.md#encryption). A public key isn't secret; the private key never leaves this machine, except to be backed up.

Back the key file up somewhere an administrator can reach, like a shared 1Password vault. Anything encrypted to it that hasn't been admitted yet can't be read without it. [security.md](security.md#keeping-the-private-key) says more.

`pigsty check` refuses a key file anyone but its owner can read, and so does every command that reads keys. Some filesystems, like some network shares, report permissions they don't enforce, so every file looks readable by everyone. There, set `ignore_permissions = true` in `pigsty.toml`, or give any `pigsty` command `--ignore-permissions`, and it reads the key files anyway, with a warning. It skips only that check: symbolic links, `run_as`, and everything `admit` and `organize` check are checked as usual. Keeping the keys private is then up to the share's own access controls.

With `pig_url` set, `pigsty check` also asks the web server which public key each task encrypts to, and warns if no private key here matches. A task encrypting to a key nobody has would collect data no one can ever read, so run `pigsty check` after changing keys on either side.

To change keys, make a new one, export again, copy the file over, and change `encrypt_to`. Leave the old key file where it is. `pigsty admit` tries every key in the directory, so runs encrypted to the old key, from a browser that saved its settings a while ago, still decrypt.

### When a run won't decrypt

A run that no key in `private_keys_dir` opens isn't admitted. It's reported, and `pigsty admit` exits with an error, like a run that doesn't match its manifest. Because it's not in `runs/`, `pigsty purge` leaves the web server's copy alone, so nothing is lost while someone works out which key it needs.

### Keeping the ciphertext

`pigsty admit --copy-ciphertext-to /secure/pig/ciphertext` also keeps each encrypted run as it arrived, with its manifest, in `/secure/pig/ciphertext/{task_code}/{run_id}/`. That copy is the one the manifest's hashes describe, so it's what to keep if you want to be able to show later that a run wasn't changed after it left the web server. It's small: events and media parts are hardly bigger encrypted.

### Decrypting by hand

The key files are ordinary age key files, so the stock [`age`](https://age-encryption.org) command reads them, and you never need Pig to get data back. In a run as it came from the web server, each event's `data` is base64 of an age file, and each media part is an age file:

```
jq -r .data events.jsonl | while read -r data; do
  echo "$data" | base64 -d | age -d -i lucida/2026a.key; echo
done
age -d -i lucida/2026a.key media/00001/000001.part > 000001.webm-part
```

That's also the yearly check that the backed-up key works: decrypt one real run with the backup, on a machine that isn't the secure server.

## Signing links

A task with `signed_by` in `pig.toml` only starts a run from a link whose parameters were signed with one of its signing keys. The signing keys live in `private_keys_dir`, beside the encryption keys, and their public halves go to the web server in the same exported file:

```
pigsty keys new --signing lucida/signing
pigsty keys export > public_keys.txt
```

Copy the file to the web server and add `signed_by = ["lucida/signing"]` to the task, with its `signed_parameters`. See [configuration.md](configuration.md#parameter-signing).

`pigsty sign` needs to know which parameters to sign, and never reads `pig.toml`, so the task's entry in `pigsty.toml` lists them too, the same list:

```toml
# pigsty.toml
[task.teddy]
datastore_dir = "/secure/lucida/pig/teddy"
organized = "/secure/lucida/intermediate_data/pig/teddy/{id}/{event}_{run_number}"
signed_parameters = ["id", "event", "modules"]
```

If the two lists differ, links signed here don't start runs there, so the first test link shows it. With `pig_url` set, `pigsty check` also compares them with what the web server says, and warns if they differ.

To sign one link's parameters, give their values. It prints the signature to add to the link, as `sig`:

```
pigsty sign teddy --key lucida/signing id=10235 event=base 'modules=questions:1,2,3|reels:03,22'
```

With `--url`, it prints the whole link, ready to paste into a browser, which is the easy way to test a task that checks signatures:

```
pigsty sign teddy --key lucida/signing --url https://teddy.example.edu/ id=10235 event=base modules=reels:01
```

To sign a whole list of participants, give it a CSV file with a column named for each signed parameter. It writes the file again with a `sig` column added, keeping every other column as it was:

```
pigsty sign teddy --key lucida/signing --csv roster.csv > signed.csv
```

For REDCap, make `sig` a text field, import `signed.csv`, and build the link by piping: `…?id=[record_id]&event=base&modules=…&sig=[sig]`. In a longitudinal project, the field is on each event, so each session gets its own signature. REDCap can't make the signature itself, which is why it's made here, on the machine with the key.

If the task reads the signature from the link under another name, give that name with `--name`, and it's used for the column and for the link `--url` prints.

Signatures don't expire, and a signing key can't be used to decrypt anything. Back the key file up like any other: losing it means signing every outstanding link again with a new key. Anyone who has it can make links that work, so it never leaves this machine except to be backed up.

## Patterns

A task's `organized` is a directory, then a pattern. In the example above, the directory is `/some/mount/mystudy/intermediate_data/pig/stroop` and the pattern is `{session}/{participant_id}_{run_number}`.

The directory is everything up to the last `/` before the first placeholder. It's the task's own: no other task can be organized into it, or into a directory inside it, or one around it. That's where `pigsty organize` keeps its record of what it has placed, and it's what makes two tasks' runs unable to land on each other.

The pattern is filled in from each run. Its placeholders, in braces, are:

- `{task_code}` and `{run_number}`, which every run has. The run number is written with four digits, `0001`.
- Any parameter the task lists under `path_parameters` in `pig.toml`, like `{participant_id}`.

Every pattern has to include `{run_number}`. Without it, a participant's second run would land on top of the first.

Only path parameters can be used. Extra parameters that arrive in a link, like `utm_source`, are recorded but never checked, so they can't become part of a path. Nor can a signed parameter that isn't a path parameter, like Teddy's `modules`: it's text, which can be anything. To use one, add it to the task's `path_parameters`, and Pig will hold it to the rule for values in paths from then on. Which parameters were path parameters is recorded in each run's `pig-run.json`, so organize knows without `pig.toml`.

Every part of the path the pattern makes, its fixed parts included, has to be a [safe value](configuration.md#safe-values): letters, digits, underscore, and dash. So `run-{run_number}` is fine, and `v1.2/{run_number}` isn't. The task's directory can be anything.

A placeholder is a name and nothing else. `{session.upper}`, `{run_number:03}`, and any other brace are refused.

## Running it again

Running `pigsty organize` again is safe, and it's how new runs get added. A run that's already in place is left alone. Nothing in `runs/` is ever changed or deleted, and nothing already in the tree is overwritten.

Each task's directory has a record, `.placed.jsonl`, with a line for each run placed in it. A run in the record isn't read again, which is what keeps a second organize quick on a network share with tens of thousands of runs. A run in the tree but not the record, after a crash say, is recognized by the `pig-run.json` in its directory, and recorded.

Because the record is trusted, a run removed from the tree by hand isn't put back. `pigsty organize --full` reads every run, checks each against the tree, puts back what's missing, and writes the record again. If the record is ever damaged, `pigsty organize` stops and says to run that.

To rebuild a tree from scratch, for a new pattern say, delete the task's directory, record included, and run `pigsty organize` again.

Only one `pigsty organize` works on a task at a time.

## When a run isn't placed

`pigsty organize` reports each run it couldn't place, carries on with the rest, and exits with an error at the end, so a scheduled job notices. A run isn't placed when:

- **Two runs would land in the same place.** Neither is placed, unless one is already in the tree, in which case it stays and the new one isn't placed. This happens when a pattern leaves out something that tells runs apart, or when two values differ only in capitals: `PPT-1003` and `ppt-1003` are two participants to Pig, but one directory on a Mac.
- **Something else is already at its path** in the tree.
- **The pattern names something the run doesn't have**, or a parameter that isn't a path parameter, or a value isn't safe in a path. Values are checked again here, whatever the web server checked, because a web server that's been broken into can write anything.
- **The run has a symbolic link in it.**
- **The run has no `pig-run.json`.** Everything in `runs/` should have been put there by `pigsty admit`.

## What organize doesn't do

It copies data exactly as Pig stored it. It doesn't join media parts into playable files, add file extensions, or drop repeated lines from `events.jsonl`. Those depend on what a task sends, so they belong to a script that understands that task's data.

It doesn't check files against hashes, either. `pigsty admit` did that, and nothing is in `runs/` unless it matched.

Expired runs are placed like any other. Whether a run was finalized by its task or expired is in its `pig-run.json`, as `disposition`.
