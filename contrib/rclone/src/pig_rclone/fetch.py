#!/usr/bin/env python3
"""Copy the tasks in `pigsty.toml` from the bucket into `incoming_dir`, with `rclone sync`.

The web server copies its `done/` to a bucket, so the bucket holds `{task}/{run_id}/…`,
and `incoming_dir` in `pigsty.toml` has the same shape. A bucket can hold other tasks too,
other studies' among them. This runs one `rclone sync`, from the bucket to `incoming_dir`,
with an `--include` for each task `pigsty.toml` lists, so only those are copied.

It's `sync`, not `copy`, on purpose. Once a run is purged, the web server's next copy
drops it from the bucket, and this drops it from `incoming_dir`. With `--include`, sync only
deletes what the includes match, so another task's directory in `incoming_dir`, if one is
there, is left alone.

`sync` deletes whatever is at the destination and not in the bucket, so the destination
has to be right. That's why this reads it from `pigsty.toml` instead of having anyone
type it, and refuses an `incoming_dir` that's a symbolic link: on a machine that also runs
Pig, it could be Pig's own `done/`, and there's nothing to copy then.

Run `pigsty admit` after this finishes, not alongside it. rclone downloads each file
under a temporary `.partial` name, and admit refuses a run with a file its manifest
doesn't list.
"""

from __future__ import annotations

import argparse
import os
import pwd
import subprocess
import sys
import tomllib
from pathlib import Path

DEFAULT_CONFIG = "local/pigsty.toml"

# --checksum: compare files by hash, not by size and time. On S3 the hash comes with the
# listing, so this is quicker as well as stricter.
# --immutable: a run's files never change once it's finished, so one that differs from
# the bucket's is a problem to stop at, not an update to copy. Deletes still happen.
# --fast-list: fewer requests to list the bucket, for a little more memory.
RCLONE_FLAGS = ["--checksum", "--immutable", "--fast-list"]


class Refused(Exception):
    """Something about the configuration means this won't run rclone at all."""


def read_config(config_path: Path) -> tuple[Path, list[str]]:
    """`incoming_dir`, resolved the way pigsty resolves it, and the task codes, sorted."""
    try:
        with config_path.open("rb") as file:
            config = tomllib.load(file)
    except FileNotFoundError:
        raise Refused(f"There's no {config_path}.") from None
    except tomllib.TOMLDecodeError as error:
        raise Refused(f"{config_path} isn't valid TOML: {error}") from None

    run_as = config.get("run_as")
    if run_as is not None:
        me = pwd.getpwuid(os.geteuid()).pw_name
        if me != run_as:
            raise Refused(
                f"{config_path} sets run_as to {run_as!r}, and this is {me!r}. The "
                f"files rclone makes would belong to {me!r}. Run this as {run_as!r}."
            )

    if "incoming_dir" not in config:
        raise Refused(
            f"{config_path} has no incoming_dir, so there's nowhere to copy runs to."
        )
    tasks = sorted(config.get("task", {}))
    if not tasks:
        raise Refused(f"{config_path} has no tasks, so there's nothing to copy.")

    # Paths in pigsty.toml are relative to the file, as pigsty reads them.
    incoming = config_path.parent.resolve() / config["incoming_dir"]
    return incoming, tasks


def check_destination(incoming: Path) -> None:
    """Raise Refused if rclone shouldn't sync into `incoming_dir`."""
    if incoming.is_symlink():
        raise Refused(
            f"incoming_dir, {incoming}, is a symbolic link. rclone sync would delete and "
            "write wherever it points, so nothing was copied. If it's Pig's done/ on "
            "this machine, there's no bucket to copy from."
        )
    if not incoming.is_dir():
        raise Refused(
            f"incoming_dir, {incoming}, isn't there. `pigsty check --fix` makes it. "
            "Nothing was copied, so a mistyped path can't become a new directory full "
            "of runs."
        )


def command(
    source: str,
    incoming: Path,
    tasks: list[str],
    rclone: str,
    rclone_config: Path | None,
    extra: list[str],
) -> list[str]:
    args = [rclone, "sync", f"{source.rstrip('/')}/", f"{incoming}/", *RCLONE_FLAGS]
    for task in tasks:
        args += ["--include", f"/{task}/**"]
    if rclone_config is not None:
        args += ["--config", str(rclone_config)]
    return args + extra


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pigsty-fetch",
        description=(
            "Copy the runs of the tasks in pigsty.toml from the bucket into incoming_dir, "
            "with rclone sync. Run pigsty admit after it."
        ),
        epilog=(
            "Anything after -- goes to rclone as it is, like "
            "`pigsty-fetch --from pigbucket:chm-wilbur-01 -- --dry-run`."
        ),
    )
    parser.add_argument(
        "--from",
        dest="source",
        required=True,
        metavar="REMOTE:BUCKET",
        help=(
            "Where the web server copies done/ to, as rclone names it: a remote in "
            "rclone's configuration, then the bucket, and a path inside it if the web "
            "server copies to one."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=(
            "The secure side's configuration file. Defaults to PIGSTY_CONFIG, or "
            f"./{DEFAULT_CONFIG}, as pigsty does."
        ),
    )
    parser.add_argument(
        "--rclone-config",
        type=Path,
        help="rclone's own configuration file, where the remote and its key are.",
    )
    parser.add_argument(
        "--rclone",
        default="rclone",
        help="The rclone to run. Defaults to the one on PATH.",
    )
    argv = sys.argv[1:] if argv is None else argv
    extra: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, extra = argv[:split], argv[split + 1 :]
    args = parser.parse_args(argv)

    config_path = args.config or Path(os.environ.get("PIGSTY_CONFIG", DEFAULT_CONFIG))
    try:
        incoming, tasks = read_config(config_path)
        check_destination(incoming)
    except Refused as refusal:
        print(refusal, file=sys.stderr)
        return 1

    to_run = command(
        args.source, incoming, tasks, args.rclone, args.rclone_config, extra
    )
    print(" ".join(to_run), flush=True)
    try:
        finished = subprocess.run(to_run, check=False)
    except FileNotFoundError:
        print(f"There's no {args.rclone} to run. Is rclone installed?", file=sys.stderr)
        return 1
    if finished.returncode != 0:
        print(
            f"rclone stopped with exit status {finished.returncode}. Don't run pigsty "
            "admit until a sync finishes.",
            file=sys.stderr,
        )
    return finished.returncode


if __name__ == "__main__":
    sys.exit(main())
