# Security

What Pig defends against, what it doesn't, and why. This page is for whoever runs the
service, not for people writing tasks.

## The threat model is smaller than you'd think

Pig receives behavioral data from online studies. Nobody steals an identity, commits fraud,
or advertises effectively by sending a research server unsolicited trial data. We are not a
plausible target for clickjacking, and there is nothing here worth a targeted attack.

The realistic problems are mundane: a participant editing their own ID or condition in a
link, a broken task sending garbage, and somebody discovering an open URL and filling a
directory with junk.

## Cross-origin requests

Tasks are static pages hosted anywhere — a lab web server, a university host, Pavlovia,
someone's laptop during testing. They will essentially never share an origin with Pig, so
browsers will send cross-origin requests and preflights for everything a task does.

Cross-origin headers give us very little security in practice. They constrain browsers, and
the things we actually care about aren't done from a browser — if someone wants to send us
junk, `curl` is right there. So the default is permissive: wildcard origins, no framing
restrictions.

One specific case is worth naming: Chrome's Private Network Access check. When a task page
loaded over https tries to reach a Pig running on `localhost` — the ordinary shape of local
development — Chrome sends an extra preflight header asking the server to opt in, and blocks
the request if nothing answers it. Pig answers it, for the same reason as everything else
here: refusing would only get in a developer's way, not stop anyone determined to send junk.

Where a specific task needs something tighter, it's set per task in that task's definition.
See [configuration.md](configuration.md).

The one exception is HSTS, which matters and should be handled by the web server in front
of Pig, not by the application.

## What actually limits unsolicited data

Not CORS headers and not a content security policy. The real mitigations, in rough order of
how much they buy you:

- **Parameter signing.** Run parameters carry a signature proving the link came from you,
  so a participant can't change their own participant ID or condition assignment. Off by
  default, per task, because it makes testing considerably harder — you can't type a test
  URL by hand. See [Parameter signing](#parameter-signing) below.
- **Closing a task.** A task that isn't collecting data doesn't accept any.
- **Knowing your participants in advance.** Checking arrivals against a roster, so a run
  can only start for someone you expect. Not built; see `README.md`.

Media is the one place the exposure grows. A task with `media = true` has a URL that
accepts uploads of up to `max_part_size` from anyone who has a run ID, and nothing caps
how much a run sends in total before it expires. That's why media is off for every task
unless its configuration turns it on, and why free space under the data root is in the
health report. Beyond that, the same reasoning applies: the realistic problem is a
broken task filling a disk, and the fix is closing the task.

Run IDs are also load-bearing here, in a small way. A participant can see their own run ID
in their browser's network tab, so a guessable one would let them post events into someone
else's run. They're random UUIDs for that reason — see [definitions.md](definitions.md).

## Data on disk

Datasets are plain files. Pig's protection of them is filesystem permissions and nothing
else, which is the right amount for data that a researcher needs to read with ordinary
tools.

Two consequences worth stating: the account running Pig needs write access to the data root
and nothing beyond it, and path parameters, which become path components, must be checked
before they're used, never sanitized into something safe-looking. See
[design_assumptions.md](design_assumptions.md).

## Parameter signing

A task with `signed_by` only starts a run from a link whose signed parameters were signed with one of its keys. The signature is Ed25519. The private key is on the secure side, and the web server has only the public key, which can check a signature but not make one. So a web server that's been broken into still can't make links that work, and an attacker who has the web server's configuration has nothing to sign with.

What a signature covers is the task code, the key's name, and the name and value of each signed parameter. Not the link: Pig never sees the address a participant followed, only the parameters a task sends when it starts a run. A link signed for one task can't start another, and a value changed after signing fails.

What it doesn't do:

- **A link that leaks works for whoever has it.** Signing proves you made the link, not who's using it. Anyone with a participant's link can start runs as that participant, as many as they like. Checking arrivals against a roster is the other half of that, and isn't built.
- **Signatures don't expire.** A participant offline for a whole run would otherwise have their data stuck on their device. The way to stop links working is to take their key out of the task's `signed_by`, which stops all of that key's links at once.
- **Unsigned parameters stay editable.** Anything not in `signed_parameters`, like a `nextURL`, is the participant's to change. Put anything that matters in the signed list.
- **It's checked only when a run starts.** After that, the run ID identifies the run, as it does for every task, and it's as secret as the participant keeps it.
- **The parameters aren't secret.** They're in the link, and the task reads them in plain text. Signing is about whether they were changed, not who can read them.

## Purging

Purging deletes a finished run from the web server once the secure side has a copy. There's no key on the request, and that's deliberate. The secure side proves it has the run by sending a hash of every file, computed from the files themselves (see [api.md](api.md#purging-a-finished-run)). Anyone who can do that already has all the data purging would delete, so a secret would protect nothing.

So purge isn't access control. It's a check that keeps a broken pipeline from deleting data: a partial download, a sync pointed at the wrong place. Don't add a key to "harden" it. If you want to limit who can reach it, do that in the web server in front of Pig, by address.

What purge can't check is whether the secure side's copy is durable. The web server can't see the secure side, so the bucket's own retention, versioning say, is the only undo for a purge that shouldn't have happened. Pig can't check that either. See [deployment.md](deployment.md).

A purged run's row stays in Pig's database, as a counter. Purge deletes the run's parameters and extra parameters, and SQLite is set to overwrite deleted rows rather than leave them readable in the file. The row still says that someone did the task, when, and how many runs they'd done, keyed by a hash of the run key. A short participant ID can be recovered from that hash by trying every value, so a purged database still isn't anonymous.

## The secure side doesn't trust the web server

The web server is the machine on the public internet, so it's the one most likely to be broken into. Nothing on the secure side relies on it having behaved. Whatever it wrote, including manifests, parameters, event data and media, is treated as possibly hostile:

- `pigsty admit` checks every file against the run's manifest. It refuses a manifest path that could reach outside the run, and it refuses a run with a symbolic link anywhere in it.
- `pigsty organize` checks every value it puts in a path, whatever the web server checked when the run started. It refuses symbolic links too.

The web server's checks are still worth having, but they're there for task authors: a value that can't become a path is refused when the run starts, while someone can still fix the task, rather than months later when the data is organized.

Encryption doesn't change this. The browser encrypts, but anyone with the public key can make data that decrypts correctly, including the web server. So encryption keeps the web server from reading a run, not from replacing it with one of its own.

The checks keep hostile data from landing anywhere but its own run directory. They don't make the data safe to open. Media in particular can be built to attack whatever decodes it, so run anything that processes collected data in a container or on an account that can't reach much else.

## End-to-end encryption

A task with `encrypt_to` set encrypts everything it records in the participant's browser, to a public key whose private half is only on the secure side. The web server and the bucket then carry data they can't read. If the web server is broken into, what's there to take is event IDs, run parameters, times, sizes and ciphertext. See [configuration.md](configuration.md#encryption) for turning it on and [organizing.md](organizing.md#encrypted-runs) for the keys.

It also means a participant's lost or stolen device holds nothing readable. The JavaScript client encrypts each event before it's saved on the device.

What isn't encrypted, and is worth knowing about when someone asks what's identifying:

- **Run parameters.** `participant_id` and the rest are what Pig uses to number runs, so it has to read them. They're in the database until the run is purged and in the run's manifest. Opaque participant codes, with the mapping kept somewhere else, are the answer to that, not encryption.
- **Extra parameters**, recorded on the run as they arrived.
- **Times.** When each event arrived, in the run's data file, and when the run started and closed, in the database.
- **Sizes**, of events and media parts, and file modification times.

The encryption is [age](https://age-encryption.org): X25519 and ChaCha20-Poly1305, with no settings to get wrong. Neither is a NIST-approved algorithm, and no browser library is a FIPS-validated module, so this isn't FIPS 140 encryption. No choice available to a task in a browser would be.

### Keeping the private key

Losing the private key loses whatever was encrypted to it and hasn't been admitted yet. Admitted runs are already decrypted, so if `pigsty admit` runs often, what's at risk is small. Still, back up each key file somewhere an administrator can reach without its usual keeper, like a shared 1Password vault. UW–Madison's Storage and Encryption Standard asks for exactly that: a backup copy of the key that's available without the normal keyholder.

Once a year, decrypt a real run with the backed-up copy on a machine that isn't the secure server, to show the backup works. The key files are ordinary age key files, so the stock `age` command can do it without Pig; see [organizing.md](organizing.md#decrypting-by-hand).

A key is never replaced and needn't ever be deleted. To change keys, make a new one and point tasks at it. Old ones stay in `private_keys_dir`, so a run encrypted with an old one, from a browser that saved its settings a while ago, still decrypts.

