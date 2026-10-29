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

Each task gets an entry saying where its runs are kept and where its tree goes:

```toml
# pigsty.toml
[task.stroop]
datastore_base = "/some/mount/mystudy/primary_data/pig/stroop"
organized = "/some/mount/mystudy/intermediate_data/pig/stroop/{session}/{participant_id}_{run_number}"
```

A task with no entry is never admitted or organized. Two tasks can't share a `datastore_base`, and neither can share where they're organized; see [Patterns](#patterns).

The two sides have their own commands as well as their own files. `pig` runs on the web server and reads `pig.toml`; `pigsty` runs where the data ends up and reads `pigsty.toml`, never `pig.toml` or the database. `pigsty check` checks the file, the way `pig check` checks `pig.toml`, and lists the directories it names that aren't there yet. `pigsty check --fix` makes them: `private_keys_dir`, readable only by you, and each task's `incoming/`, `runs/`, and the directory its tree goes in. It never makes a key, and never changes the permissions of anything already there. Paths are relative to the file, as they are in `pig.toml`. Every `pigsty` command reads `local/pigsty.toml`, or the file named by `--config` or `PIGSTY_CONFIG`.

## Trying it on a laptop

`pigsty.example.toml` in the repository is a starting point. Copy it to `local/pigsty.toml`, where its paths are under `local/`. Then copy your local Pig's finished stroop runs to where the bucket's copy would put them, and admit and organize them:

```
pigsty check --fix
cp -R local/data/done/stroop/* local/stroop-data/incoming/
pigsty admit
pigsty organize
```

The tree is in `local/organized/stroop/`. Add `--dry-run` to `pigsty organize` to see where each run would go without copying anything.

With `pig serve` running, `pigsty purge` then deletes those runs from `local/data/done/stroop/`. There's no bucket on a laptop, so nothing takes them out of `incoming/`; delete them by hand.

## Admitting downloaded runs

On a real secure server, runs arrive by a copy Pig doesn't run: `rclone sync` from the bucket the web server uploads to, or something like it. That copy isn't all-at-once. A run can be half there for a while, and its manifest can arrive before the files it lists. `pigsty admit` is what stands between that and everything else: it moves a run into place only once it's whole.

Inside each task's `datastore_base`:

- `incoming/` is where the copy puts the task's runs, one directory per run ID, as they are in the bucket. Point `rclone sync` at it.
- `runs/` is where `pigsty admit` puts each run once every file its manifest lists is there, with the right size and hash, and nothing else is.

A run in `runs/` holds its files exactly as the web server stored them, decrypted if the task encrypts (see [Encrypted runs](#encrypted-runs)), and `pig-run.json`, which says what the run is: task, run number, parameters, media. It's the manifest without the file hashes. The manifest itself stays in `incoming/`, because it describes the files as they traveled.

Running `pigsty admit` again is safe, and it's how new runs get admitted. A run already in `runs/` is left alone. A run still downloading is left for next time and isn't an error. A run that's all there but doesn't match its manifest is reported, nothing of it is admitted, and `pigsty admit` exits with an error so a scheduled job notices. Nothing in `incoming/` is ever changed: it's a copy of the bucket, and runs leave it when they leave the bucket.

A run with a symbolic link anywhere in it is refused too. Pig never makes one, and one could point anywhere on the secure machine.

Only one `pigsty admit` works on a task at a time.

## Purging admitted runs from the web server

Once a run is admitted, the web server doesn't need its copy, and shouldn't keep it: it's the machine on the public internet. `pigsty purge` asks it to delete each one. It needs the web server's address, once, at the top of `pigsty.toml`:

```toml
# pigsty.toml
pig_url = "https://pig.example.edu"
```

For each run that's in both `runs/` and `incoming/`, `pigsty purge` hashes the files in `incoming/` and sends the hashes to Pig, which deletes its copy only if every one matches its own. It hashes `incoming/` because that's a copy of the bucket, so its files are exactly the web server's. A run still downloading doesn't match, and isn't purged. How the hashes work is in [api.md](api.md#purging-a-finished-run).

Then the run leaves on its own. The web server's next copy to the bucket deletes it there, and this side's next copy from the bucket deletes it from `incoming/`. Its copy in `runs/` stays. So a run is in `incoming/` from when it arrives until it's purged and both copies have run, and a run that stays in `incoming/` long after it was admitted means something in that chain isn't working.

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

`pigsty check` refuses a key file anyone but its owner can read. With `pig_url` set, it also asks the web server which public key each task encrypts to, and warns if no private key here matches. A task encrypting to a key nobody has would collect data no one can ever read, so run `pigsty check` after changing keys on either side.

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
datastore_base = "/secure/lucida/pig/teddy"
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
