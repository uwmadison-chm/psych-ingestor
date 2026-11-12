# Pulling finished runs over SSH

**Status: a sketch.** Nobody has run this in production yet. Everything below was tried on one test machine with a real Pig, rsync, and Docker, except the systemd units: they pass `systemd-analyze verify`, but haven't run under systemd.

The secure side can copy finished runs straight from the web server, with no bucket in between. It keeps an SSH connection open to the web server, hears about each run as soon as the sweep moves it into `done/`, and copies it with rsync. Pig and pigsty don't change. `incoming_dir` has the same shape either way, and `pigsty admit` and `pigsty purge` work as they always have. All of this is a few files on each machine, outside Pig.

Compared with [copying through a bucket](deployment.md#copying-finished-runs-to-the-bucket), the web server holds no keys for moving data, and a run reaches the secure side seconds after it's finished instead of at the next timer. What you give up is a copy somewhere else. Until the secure side pulls a run, the web server's disk is the only place it exists. Once it's purged, the only undo is the copy this setup keeps on the secure side for 30 days; see [Undoing a purge](#undoing-a-purge).

## How it works

1. A service on the secure server, `pigsty-watch`, runs `ssh` to the web server. Its key can only run `inotifywait`, a standard Linux tool that watches a directory. It prints a line once it's watching `done/`, and another each time a run moves in or out.
2. Each burst of lines starts `pigsty-sync.service`. The watcher waits for five quiet seconds first, so a sweep that finishes many runs starts one sync.
3. The sync runs `pigsty-pull`, which is `rsync` from `done/` into `incoming_dir`, with a second key that can only read `done/`. Then `pigsty admit` and `pigsty purge` run, each in a container of its own.
4. Purge moves runs out of `done/`, which prints more lines and starts one more sync. That sync's pull takes the purged runs out of `incoming_dir` and sets them aside.
5. If the connection drops, systemd starts the watcher again 30 seconds later. Its first line starts a sync, which catches up on whatever happened in between.
6. A timer starts a sync after 15 minutes without one, in case the watcher isn't working.

The watcher never reads what the web server sends. A line only means something changed, and every pull copies whatever is new, so a missed line costs time and nothing else.

Every connection goes from the secure server to the web server. The web server never connects to anything.

The examples use these paths and names. Change them to yours.

| Path or name | What it is |
| --- | --- |
| `pig.example.edu` | The web server |
| `/var/lib/pig/data/done/` | Pig's `done/`, on the web server |
| `/etc/pigsty/pigsty.toml` | `pigsty.toml`, on the secure server |
| `/etc/pigsty/private_keys/` | `private_keys_dir` |
| `/etc/pigsty/ssh/` | The secure server's SSH keys and settings for this |
| `/secure/pig/incoming/` | `incoming_dir` |
| `/secure/pig/purged/` | Where purged runs are set aside. It has to be on the same filesystem as `incoming_dir` |
| `/secure/mystudy/primary_data/pig/` | The directory holding each task's `datastore_dir` |
| `stroop`, `balloons` | The tasks in `pigsty.toml` |
| `pigsty-local` | A Docker image with pigsty installed |

## On the web server

The secure side logs in as an account of its own, `pigpull`, which can read `done/` and can't write anything of Pig's. Install `inotify-tools` (it's in EPEL on RHEL) and make sure `rrsync` is there. `rrsync` comes with rsync, but distributions put it in different places or leave it out; Ubuntu 24.04 has it at `/usr/bin/rrsync`.

```
useradd --system --create-home --home-dir /var/lib/pigpull --shell /bin/sh --groups pig pigpull
```

It needs a real shell, because sshd runs each key's command with it, but it has no password, so nobody can log in as it except with the keys below.

Then make `/var/lib/pigpull/.ssh/authorized_keys`, owned by `pigpull` and readable only by it (`chmod 600`), with one line for each of the two keys from [SSH keys and settings](#ssh-keys-and-settings):

```
restrict,command="inotifywait --monitor --recursive --event create,moved_to,moved_from /var/lib/pig/data/done/ 2>&1" ssh-ed25519 AAAA… pigsty watch
restrict,command="rrsync -ro /var/lib/pig/data/done/" ssh-ed25519 AAAA… pigsty pull
```

- `command=` runs that and only that, whatever the secure side asks for.
- `restrict` turns off everything else SSH can do: forwarding ports, a terminal, and the rest.
- `rrsync -ro` limits rsync to `done/`, and lets it send files but not receive them.
- `2>&1` is there because `inotifywait` says it's watching on its error output, and that line is what starts the first sync.

On the test machine, the pull key refused to receive a file, refused a path with `..` in it, and refused `cat /etc/passwd`. The secure side's `--delete` still works, because deleting happens on the receiving end.

`pigpull` reads Pig's files through the `pig` group, so they have to be readable by the group. Pig doesn't set permissions itself, and with the usual umask of 022, its files are readable by the group and everyone else. To limit that to the group, set `UMask=0027` in the units that run `pig serve` and `pig sweep`.

Let SSH in from the secure server's address only.

## On the secure server

### SSH keys and settings

Make the two keys, with no passphrase, since nobody is there to type one:

```
mkdir -p /etc/pigsty/ssh
ssh-keygen -t ed25519 -N '' -C 'pigsty watch' -f /etc/pigsty/ssh/watch_key
ssh-keygen -t ed25519 -N '' -C 'pigsty pull' -f /etc/pigsty/ssh/pull_key
ssh-keyscan pig.example.edu > /etc/pigsty/ssh/known_hosts
chown -R pigsty: /etc/pigsty/ssh
chmod 700 /etc/pigsty/ssh
```

Put the `.pub` halves in the web server's `authorized_keys`, above. `ssh-keyscan` records the web server's host key, so later connections can check they've reached the right machine. Compare what it recorded with the key on the web server (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` there) before trusting it.

`/etc/pigsty/ssh/config` gives each key a name:

```
Host pig-watch
    IdentityFile /etc/pigsty/ssh/watch_key
Host pig-pull
    IdentityFile /etc/pigsty/ssh/pull_key
Host pig-watch pig-pull
    HostName pig.example.edu
    User pigpull
    IdentitiesOnly yes
    UserKnownHostsFile /etc/pigsty/ssh/known_hosts
    StrictHostKeyChecking yes
    BatchMode yes
    RequestTTY no
    ServerAliveInterval 60
    ServerAliveCountMax 3
```

`ServerAliveInterval` notices a web server that vanished without closing the connection: `ssh` gives up after about three minutes without an answer.

### Watching for new runs

`/usr/local/bin/pigsty-watch`:

```bash
#!/bin/bash
# Start pigsty-sync.service whenever the web server's done/ changes.
#
# Each line from the web server means "something changed". What it says is never read:
# every pull copies everything new, so a line only says when. The first line comes once
# the watch is set up, so every connection starts with a pull, which catches up on
# whatever changed while it was down.
set -u
ssh -F /etc/pigsty/ssh/config pig-watch | while read -r _; do
    # A sweep can move many runs in a row. Wait for 5 quiet seconds, so they're one sync.
    while read -r -t 5 _; do :; done
    systemctl start pigsty-sync.service || true
done
# The connection dropped. Exit, and systemd starts this again.
exit 1
```

`systemctl start` waits for the sync to finish, and lines that arrive meanwhile start one more sync once it has.

`/etc/systemd/system/pigsty-watch.service`. `StartLimitIntervalSec=0` means systemd keeps restarting it however long the web server is away:

```ini
[Unit]
Description=Start pigsty-sync whenever Pig's done/ changes
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
User=pigsty
ExecStart=/usr/local/bin/pigsty-watch
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

### Pulling

`/usr/local/bin/pigsty-pull`. List every task in `pigsty.toml` with an `--include`:

```sh
#!/bin/sh
# Copy finished runs from the web server's done/ into incoming_dir.
#
# --delete takes purged runs out of incoming_dir, and --backup moves them into a
# directory for today under /secure/pig/purged/ instead of deleting them.
exec rsync --recursive --times --delete --ignore-existing \
    --backup --backup-dir="/secure/pig/purged/$(date +%F)" \
    --include=/stroop/ --include=/balloons/ --exclude='/*' \
    -e 'ssh -F /etc/pigsty/ssh/config' \
    pig-pull:/ /secure/pig/incoming/
```

- `--include` for each task, then `--exclude='/*'`, copies only this study's tasks from a web server that may serve other studies too. `--delete` only deletes inside those tasks.
- `--ignore-existing` never rewrites a file that's already in `incoming_dir`. A finished run's files never change, so one that differs on the web server isn't copied, and `pigsty purge` then reports that its hashes don't match instead of purging it.
- There's no `--archive`, so rsync skips symbolic links. Pig never makes one, and admit refuses a run that has one.
- rsync writes each file under a temporary name and renames it when it's finished. A pull that's stopped partway leaves a temporary file, and the next pull's `--delete` removes it.
- A task missing from the `--include` list never arrives, and its runs stay on the web server, never purged. Add a task here when you add it to `pigsty.toml`.

`/etc/systemd/system/pigsty-pull.service`. The pull is the step that talks to the machine most likely to be broken into, so its unit lets it write `incoming_dir` and the directory for purged runs, and nothing else, and hides the private keys and the datastores from it:

```ini
[Unit]
Description=Copy finished runs from Pig's done/ into incoming_dir

[Service]
Type=oneshot
User=pigsty
ExecStart=/usr/local/bin/pigsty-pull
TimeoutStartSec=1h
ProtectSystem=strict
ReadWritePaths=/secure/pig/incoming /secure/pig/purged
InaccessiblePaths=/etc/pigsty/private_keys /secure/mystudy
PrivateTmp=yes
NoNewPrivileges=yes
```

With `ProtectSystem=strict`, a mistyped destination fails instead of deleting whatever is there.

### Admitting and purging in containers

`/usr/local/bin/pigsty-sync` runs admit, then purge, each in a container of its own. [Running pigsty in a container](#running-pigsty-in-a-container) says why each flag is there.

```bash
#!/bin/bash
# Admit new runs, then purge them from the web server, each in a container of its own.
#
# Each step is a separate `docker run --rm`, so purge never sees anything admit wrote
# inside its own container. If a mount is wrong, nothing is purged, and the web server
# keeps its copy.
set -euo pipefail

# Admit has the private keys, and no network.
docker run --rm --read-only --tmpfs /tmp --network none --user pigsty \
    --env PIGSTY_CONFIG=/etc/pigsty/pigsty.toml \
    --mount type=bind,source=/etc/pigsty/pigsty.toml,target=/etc/pigsty/pigsty.toml,readonly \
    --mount type=bind,source=/etc/pigsty/private_keys,target=/etc/pigsty/private_keys,readonly \
    --mount type=bind,source=/secure/pig/incoming,target=/secure/pig/incoming,readonly \
    --mount type=bind,source=/secure/mystudy/primary_data/pig,target=/secure/mystudy/primary_data/pig \
    pigsty-local pigsty admit

# Purge has the network, and no keys: it only hashes what's in incoming_dir.
docker run --rm --read-only --tmpfs /tmp --user pigsty \
    --env PIGSTY_CONFIG=/etc/pigsty/pigsty.toml \
    --mount type=bind,source=/etc/pigsty/pigsty.toml,target=/etc/pigsty/pigsty.toml,readonly \
    --mount type=bind,source=/secure/pig/incoming,target=/secure/pig/incoming,readonly \
    --mount type=bind,source=/secure/mystudy/primary_data/pig,target=/secure/mystudy/primary_data/pig \
    pigsty-local pigsty purge
```

`/etc/systemd/system/pigsty-sync.service`. It runs as root because `docker` needs it; the containers themselves run as `pigsty`. With `Requires=` and `After=`, starting it runs the pull first, and a pull that fails stops it there:

```ini
[Unit]
Description=Pull, admit, and purge Pig's finished runs
Requires=pigsty-pull.service
After=pigsty-pull.service docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/pigsty-sync
TimeoutStartSec=1h
```

Run `pigsty organize` however you do now. It only reads `runs/`, so it doesn't need to be part of this.

### The timer

`/etc/systemd/system/pigsty-sync.timer`. `OnUnitInactiveSec=` counts from the end of the last sync, whatever started it, so the timer only starts one after 15 quiet minutes:

```ini
[Unit]
Description=Sync with Pig's web server even when nothing announced a change

[Timer]
OnBootSec=5min
OnUnitInactiveSec=15min

[Install]
WantedBy=timers.target
```

### Letting the watcher start a sync

The watcher runs as `pigsty`, and only root can start a system service unless polkit allows it. `/etc/polkit-1/rules.d/50-pigsty-sync.rules` allows that one service, and nothing else:

```js
// Let pigsty-watch start a sync, and nothing else.
polkit.addRule(function (action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        action.lookup("unit") == "pigsty-sync.service" &&
        action.lookup("verb") == "start" &&
        subject.user == "pigsty") {
        return polkit.Result.YES;
    }
});
```

### Undoing a purge

With a bucket, the bucket's old versions are the undo for a purge that shouldn't have happened. Here, it's `/secure/pig/purged/`: each day's pull moves the runs that left `done/` into a directory named for the day, like `/secure/pig/purged/2026-11-12/stroop/{run_id}/`, with the same bytes and manifest the web server had.

That directory has the same shape as `incoming_dir`, so `pigsty admit` can admit from it. To put back a run that's missing from `runs/`, copy `pigsty.toml` with `incoming_dir` changed to that day's directory, and run admit with the copy:

```toml
# /etc/pigsty/restore.toml: pigsty.toml, except for this line
incoming_dir = "/secure/pig/purged/2026-11-12"
```

```
docker run --rm --read-only --tmpfs /tmp --network none --user pigsty \
    --env PIGSTY_CONFIG=/etc/pigsty/restore.toml \
    --mount type=bind,source=/etc/pigsty/restore.toml,target=/etc/pigsty/restore.toml,readonly \
    --mount type=bind,source=/etc/pigsty/private_keys,target=/etc/pigsty/private_keys,readonly \
    --mount type=bind,source=/secure/pig/purged,target=/secure/pig/purged,readonly \
    --mount type=bind,source=/secure/mystudy/primary_data/pig,target=/secure/mystudy/primary_data/pig \
    pigsty-local pigsty admit
```

Runs already in `runs/` are left alone. Don't run purge with that file: those runs are already gone from the web server.

This covers mistakes on the secure side, like a run admitted to the wrong place or deleted from `runs/`. It doesn't cover losing the secure server's storage, which a bucket on another system did. Back the datastores up for that.

Deleting what's been set aside for more than 30 days, `/etc/systemd/system/pigsty-clear-purged.service`:

```ini
[Unit]
Description=Delete purged runs set aside more than 30 days ago

[Service]
Type=oneshot
User=pigsty
ExecStart=/usr/bin/find /secure/pig/purged -mindepth 1 -maxdepth 1 -type d -mtime +30 -exec rm -r {} +
```

And `/etc/systemd/system/pigsty-clear-purged.timer`:

```ini
[Unit]
Description=Delete old purged runs once a day

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

### Turning it on

```
install -d -o pigsty -g pigsty /secure/pig/purged
systemctl daemon-reload
systemctl enable --now pigsty-watch.service pigsty-sync.timer pigsty-clear-purged.timer
```

`journalctl -u pigsty-watch -u pigsty-pull -u pigsty-sync` shows what each did. A failed admit or purge shows up as a failed `pigsty-sync.service`, as it would from a timer.

## Running pigsty in a container

The danger with a container is a mount that's missing or mistyped. Admit then makes `datastore_dir` inside the container's own filesystem and admits runs there, purge finds them and deletes the web server's copy, and the container's filesystem goes away when it exits. On the test machine, with every guard below turned off and admit and purge in one container, that's what happened: the run was gone from both machines, and only `/secure/pig/purged/` still had it.

So `pigsty-sync` has several guards, and any one of them stopped it in the test:

- **Admit and purge each get a container of their own.** Purge only purges a run it finds in `runs/`, and a separate container can't see anything admit wrote inside its own. With a mistyped mount, admit admitted into its container, purge found nothing to purge, and the web server kept the run.
- **`--read-only`** makes the container's own filesystem read-only, so writing anywhere that isn't a mount fails. Admit stopped with "read-only file system." `--tmpfs /tmp` gives Python the scratch space it needs.
- **`--user pigsty`.** Docker makes any directory it needs for a mount as root, so `pigsty` can't make a missing `datastore_dir` beside it. Admit stopped with "permission denied." With `run_as = "pigsty"` in `pigsty.toml`, pigsty also refuses to run as anyone else. The image needs a `pigsty` user with the same user ID as on the host, so files on the share belong to the same account either way.
- **`--mount type=bind`** refuses a source that isn't there: "bind source path does not exist." `-v /secure/…:/secure/…` makes the missing directory on the host instead, and carries on. In Compose, the short form, `- /secure/…:/secure/…`, makes it too; the long form, with `type: bind`, doesn't unless you set `create_host_path`.
- **Mounting deep in the tree.** If the share isn't mounted on the host, everything below its mount point is missing, so a source like `/secure/mystudy/primary_data/pig` doesn't exist and the container doesn't start. That only works if nothing has made those directories on the host's own disk under the empty mount point. Check once, with the share unmounted.

Two more things to check about the image:

- **No `VOLUME` for any of these paths.** A `VOLUME` line in a Dockerfile declares a mount point, and if nothing is mounted there, Docker makes an anonymous volume for it: writable even with `--read-only`, and deleted by `--rm` when the container exits. A base image can declare one too, and there's no way to undo it in your own Dockerfile. `docker image inspect --format '{{json .Config.Volumes}}' pigsty-local` should print `null`.
- **Run pigsty from the image's virtual environment, not with `uv run`,** which wants to write its cache.

## When something goes wrong

Most problems cost time, not data, because every pull copies whatever is new. The last two rows are the ones that change what can be lost.

| What goes wrong | What happens | What it costs |
| --- | --- | --- |
| The connection drops, or the web server restarts | `ssh` exits within about 3 minutes. The watcher restarts 30 seconds later and keeps retrying, and the first line after it reconnects starts a sync | Runs wait until it's back |
| A line never arrives: the kernel's queue of changes overflows, or a run lands in a new task's directory before its watch is set up | The next line, or the timer, starts a sync that copies it anyway | Up to 15 minutes |
| A run lands while a sync the timer started is running | `systemctl start` waits for that sync instead of starting another | Up to 15 minutes, or until the next change |
| `inotifywait` can't watch every directory | It needs one watch per directory under `done/`, and some systems stop at 8,192. The watcher retries every 30 seconds, and the timer keeps syncing | The timer's 15-minute schedule |
| A pull, admit, or purge fails | `pigsty-sync.service` fails, and the watcher keeps watching | Same as on a timer |
| The secure server is down | Finished runs stay in `done/`, on the web server's disk and nowhere else | If that disk fails meanwhile, they're lost |
| A purge turns out to have been wrong | The run is in `/secure/pig/purged/` for 30 days. See [Undoing a purge](#undoing-a-purge) | After 30 days, only a backup of the datastore has it |

## What this changes about security

The web server no longer holds a key that can write to or delete from a bucket, so breaking into it gets no credentials for anything else.

On the other hand, the secure side now talks rsync directly to the machine most likely to be broken into, not to a bucket. rsync 3.4.0 fixed [six bugs](https://openwall.com/lists/oss-security/2025/01/14/3) in January 2025, among them one that let a malicious server write files outside the destination of a pull. Use rsync 3.4.0 or later on the secure side, or a distribution's build with those fixes. The limits on `pigsty-pull.service` are there for bugs nobody has found yet: the pull can write only `incoming_dir` and the directory for purged runs, and can't see the private keys or the datastores.

The watcher's `ssh` talks to the web server too. It never acts on anything the web server sends, beyond starting a sync.

The secure side's pull key can read every task in `done/`, not just this study's, the same as a bucket key that can read the whole bucket. For tasks that encrypt, that's ciphertext.
