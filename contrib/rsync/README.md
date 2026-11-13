# Pig and rsync

On the secure side, `pigsty admit` reads runs from `incoming_dir` in `pigsty.toml`, which has the same shape as the web server's `done/`. Without a bucket in between, the secure side can copy them straight from `done/` over SSH. `pigsty-pull` does that with one `rsync`, limited to the tasks `pigsty.toml` lists, so a web server that holds other studies' tasks too only gives you yours.

It needs Python 3.12 or later and rsync, and nothing else. Use rsync 3.4.0 or later, or your distribution's build with its fixes: it's talking to the machine most likely to be broken into. It's one file, so you can download it and run it, with `main` replaced by a tag or commit to pin a version:

```
curl -fsSLo /usr/local/bin/pigsty-pull \
  https://raw.githubusercontent.com/uwmadison-chm/psych-ingestor/main/contrib/rsync/src/pig_rsync/pull.py
chmod +x /usr/local/bin/pigsty-pull
```

Or install it with uv, which puts `pigsty-pull` on your PATH:

```
uv tool install "git+https://github.com/uwmadison-chm/psych-ingestor#subdirectory=contrib/rsync"
```

Then:

```
pigsty-pull --from pig-pull: --config /etc/pigsty/circe/pigsty.toml \
  --ssh-config /etc/pigsty/circe/ssh/config --keep-purged /secure/circe/pig/purged
```

`--from` is where the web server's `done/` is, as rsync names it: an SSH host, a colon, and a path. With the web server's key limited to `done/` by `rrsync`, as [pulling_over_ssh.md](../../docs/pulling_over_ssh.md) sets it up, the path is relative to `done/`, so `pig-pull:` is all of it. It prints the rsync command, then runs it:

```
rsync --recursive --times --delete --ignore-existing --backup --backup-dir=/secure/circe/pig/purged/2026-11-12 \
  --include=/teddy/ --include=/teddy_followup/ '--exclude=/*' -e 'ssh -F /etc/pigsty/circe/ssh/config' \
  pig-pull:/ /secure/circe/pig/incoming/
```

Anything after `--` goes to rsync as it is. `-- --dry-run --itemize-changes` shows what would be copied and deleted without doing either.

- `--delete` takes runs out of `incoming_dir` once they've left `done/`, which is how purged runs leave it. It only deletes inside the tasks you listed.
- `--keep-purged` moves those runs into a directory for the day instead of deleting them, as the undo for a purge that shouldn't have happened. The directory has to be there already, on the same filesystem as `incoming_dir` and not inside it. Something else has to delete what's been there long enough; see [pulling_over_ssh.md](../../docs/pulling_over_ssh.md#undoing-a-purge).
- `--ignore-existing` never copies a file again once it's in `incoming_dir`. A finished run's files never change, so if the web server's copy differs, `pigsty purge` finds that its hashes don't match and says so.
- There's no `--archive`, so rsync skips symbolic links. Pig never makes one, and admit refuses a run with one.
- It refuses an `incoming_dir` that isn't there, rather than letting rsync make one, and one that's a symbolic link.

Run `pigsty admit` once it's finished, not at the same time: rsync writes each file under a temporary name, and admit refuses a run with a file its manifest doesn't list. [pulling_over_ssh.md](../../docs/pulling_over_ssh.md) has the rest: the restricted keys on the web server, and the systemd units that start a pull whenever a run arrives.
