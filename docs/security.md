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

- **Parameter signing.** Link parameters carry a signature proving the link came from you,
  so a participant can't change their own participant ID or condition assignment. Off by
  default, per task, because it makes testing considerably harder — you can't type a test
  URL by hand.
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
and nothing beyond it, and link parameters that become path components must be checked
before they're used, never sanitized into something safe-looking. See
[design_assumptions.md](design_assumptions.md).

## The secure side doesn't trust the web server

The web server is the machine on the public internet, so it's the one most likely to be broken into. Nothing on the secure side relies on it having behaved. Whatever it wrote, including manifests, parameters, event data and media, is treated as possibly hostile:

- `pigsty admit` checks every file against the run's manifest. It refuses a manifest path that could reach outside the run, and it refuses a run with a symbolic link anywhere in it.
- `pigsty organize` checks every value it puts in a path, whatever the web server checked when the run started. It refuses symbolic links too.

The web server's checks are still worth having, but they're there for task authors: a value that can't become a path is refused when the run starts, while someone can still fix the task, rather than months later when the data is organized.

Encryption (#10) won't change this. It will show that a run wasn't altered after the web server encrypted it, but not that the web server was honest when it did.

The checks keep hostile data from landing anywhere but its own run directory. They don't make the data safe to open. Media in particular can be built to attack whatever decodes it, so run anything that processes collected data in a container or on an account that can't reach much else.
