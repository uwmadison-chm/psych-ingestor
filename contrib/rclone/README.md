# Pig and rclone

On the secure side, `pigsty admit` reads runs from `incoming_dir` in `pigsty.toml`, which has the same shape as the web server's `done/`. Something has to copy them there from the bucket the web server uploads to. `pigsty-fetch` does that with one `rclone sync`, limited to the tasks `pigsty.toml` lists, so a bucket that holds other studies' tasks too only gives you yours.

It needs Python 3.12 or later and rclone, and nothing else. It's one file, so you can download it and run it, with `main` replaced by a tag or commit to pin a version:

```
curl -fsSLo /usr/local/bin/pigsty-fetch \
  https://raw.githubusercontent.com/uwmadison-chm/psych-ingestor/main/contrib/rclone/src/pig_rclone/fetch.py
chmod +x /usr/local/bin/pigsty-fetch
```

Or install it with uv, which puts `pigsty-fetch` on your PATH:

```
uv tool install "git+https://github.com/uwmadison-chm/psych-ingestor#subdirectory=contrib/rclone"
```

Then:

```
pigsty-fetch --from pigbucket:chm-wilbur-01 \
  --config /etc/pigsty/pigsty.toml --rclone-config /etc/pigsty/rclone.conf
```

`--from` is the bucket as rclone names it: a remote in rclone's configuration, then the bucket. It prints the rclone command, then runs it:

```
rclone sync pigbucket:chm-wilbur-01/ /secure/lucida/incoming/ --checksum --immutable \
  --fast-list --include /teddy/** --include /teddy_followup/** --config /etc/pigsty/rclone.conf
```

Anything after `--` goes to rclone as it is. `-- --dry-run` shows what would be copied and deleted without doing either.

- It's `sync`, so runs leave `incoming_dir` once they've left the bucket, which is how purged runs leave it. With `--include`, it only deletes inside the tasks you listed.
- `--checksum` compares files by hash. On S3 the hash comes with the listing, so that's faster than comparing sizes and times as well as stricter.
- `--immutable` stops rather than overwrite a file that's changed. A finished run's files never change, so one that has is something to look at.
- It refuses an `incoming_dir` that isn't there, rather than letting rclone make one, and one that's a symbolic link.

Run `pigsty admit` once it's finished, not at the same time: rclone downloads each file under a temporary name, and admit refuses a run with a file its manifest doesn't list. In one systemd unit:

```ini
[Service]
Type=oneshot
User=pigsty
Environment=PIGSTY_CONFIG=/etc/pigsty/pigsty.toml
ExecStart=/usr/local/bin/pigsty-fetch --from pigbucket:chm-wilbur-01 --rclone-config /etc/pigsty/rclone.conf
ExecStart=/opt/pig/.venv/bin/pigsty admit
ExecStart=/opt/pig/.venv/bin/pigsty purge
```

Each step runs only if the one before it worked. See [deployment.md](../../docs/deployment.md) for the rclone configuration and the keys each side needs.

With Pig and pigsty on one machine, there's no bucket and nothing to fetch: set `incoming_dir` to Pig's `done/`.
