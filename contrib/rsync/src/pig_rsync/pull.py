#!/usr/bin/env python3
"""Copy the tasks in `pigsty.toml` from the web server's `done/` into `incoming_dir`,
with `rsync` over SSH.

The web server's `done/` holds `{task}/{run_id}/…`, and `incoming_dir` in `pigsty.toml`
has the same shape. The web server can hold other studies' tasks too. This runs one
`rsync`, from `done/` to `incoming_dir`, with an `--include` for each task `pigsty.toml`
lists and an `--exclude` for everything else, so only those are copied.

It deletes too, on purpose. Once a run is purged, it leaves the web server's `done/`, and
`--delete` takes it out of `incoming_dir`. Deletes only happen inside the tasks that are
included, so another task's directory in `incoming_dir`, if one is there, is left alone.
With `--keep-purged`, a run that's deleted is moved into a directory for the day
instead, as the undo for a purge that shouldn't have happened.

`--delete` removes whatever is at the destination and not on the web server, so the
destination has to be right. That's why this reads it from `pigsty.toml` instead of
having anyone type it, and refuses an `incoming_dir` that's a symbolic link: on a machine
that also runs Pig, it could be Pig's own `done/`, and there's nothing to copy then.

Run `pigsty admit` after this finishes, not alongside it. rsync writes each file under a
temporary name and renames it once it's whole, and admit refuses a run with a file its
manifest doesn't list.

See docs/pulling_over_ssh.md for the rest: the restricted key on the web server, and the
service that starts this whenever a run arrives.
"""

from __future__ import annotations

import argparse
import datetime
import os
import pwd
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

DEFAULT_CONFIG = "local/pigsty.toml"

# --recursive and --times, not --archive: no symbolic links, devices, owners or
# permissions. Pig never makes a symbolic link, and admit refuses a run that has one.
# --delete: a purged run leaves done/, so it leaves incoming_dir too.
# --ignore-existing: a finished run's files never change, so one already here is never
# copied again. If the web server's copy differs, purge finds the hashes don't match and
# says so, instead of this quietly replacing what admit checked.
RSYNC_FLAGS = ["--recursive", "--times", "--delete", "--ignore-existing"]


class Refused(Exception):
    """Something about the configuration means this won't run rsync at all."""


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
                f"files rsync makes would belong to {me!r}. Run this as {run_as!r}."
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
    """Raise Refused if rsync shouldn't copy into `incoming_dir`."""
    if incoming.is_symlink():
        raise Refused(
            f"incoming_dir, {incoming}, is a symbolic link. rsync --delete would delete "
            "and write wherever it points, so nothing was copied. If it's Pig's done/ on "
            "this machine, there's nothing to pull."
        )
    if not incoming.is_dir():
        raise Refused(
            f"incoming_dir, {incoming}, isn't there. `pigsty check --fix` makes it. "
            "Nothing was copied, so a mistyped path can't become a new directory full "
            "of runs."
        )


def check_keep_purged(keep_purged: Path, incoming: Path) -> None:
    """Raise Refused if purged runs shouldn't be moved into `keep_purged`."""
    if keep_purged.is_symlink() or not keep_purged.is_dir():
        raise Refused(
            f"--keep-purged is {keep_purged}, and there's no directory there. Make it, on "
            "the same filesystem as incoming_dir. Nothing was copied, so no purged run "
            "was deleted instead of kept."
        )
    kept, arriving = keep_purged.resolve(), incoming.resolve()
    if kept.is_relative_to(arriving) or arriving.is_relative_to(kept):
        raise Refused(
            f"--keep-purged, {keep_purged}, and incoming_dir, {incoming}, are one inside "
            "the other. incoming_dir holds only what's on the web server, so purged runs "
            "need a directory of their own. Nothing was copied."
        )


def command(
    source: str,
    incoming: Path,
    tasks: list[str],
    rsync: str,
    ssh_config: Path | None,
    keep_purged: Path | None,
    today: datetime.date,
    extra: list[str],
) -> list[str]:
    args = [rsync, *RSYNC_FLAGS]
    if keep_purged is not None:
        args += ["--backup", f"--backup-dir={keep_purged / today.isoformat()}"]
    args += [f"--include=/{task}/" for task in tasks]
    args += ["--exclude=/*"]
    if ssh_config is not None:
        args += ["-e", f"ssh -F {ssh_config}"]
    return args + extra + [f"{source.rstrip('/')}/", f"{incoming}/"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pigsty-pull",
        description=(
            "Copy the runs of the tasks in pigsty.toml from the web server's done/ into "
            "incoming_dir, with rsync over SSH. Run pigsty admit after it."
        ),
        epilog=(
            "Anything after -- goes to rsync as it is, like "
            "`pigsty-pull --from pig-pull: -- --dry-run --itemize-changes`."
        ),
    )
    parser.add_argument(
        "--from",
        dest="source",
        required=True,
        metavar="HOST:PATH",
        help=(
            "Where the web server's done/ is, as rsync names it: an SSH host, a colon, "
            "and the path. With rrsync limiting the key to done/ on the web server, the "
            "path is relative to done/, so `pig-pull:` is all of it."
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
        "--ssh-config",
        type=Path,
        help="An SSH configuration file for ssh to read instead of your own.",
    )
    parser.add_argument(
        "--keep-purged",
        type=Path,
        metavar="DIRECTORY",
        help=(
            "Instead of deleting a run that's left the web server, move it into a "
            "directory for today in this one, like DIRECTORY/2026-11-12/stroop/{run_id}/. "
            "It has to be there already, on the same filesystem as incoming_dir."
        ),
    )
    parser.add_argument(
        "--rsync",
        default="rsync",
        help="The rsync to run. Defaults to the one on PATH.",
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
        if args.keep_purged is not None:
            check_keep_purged(args.keep_purged, incoming)
    except Refused as refusal:
        print(refusal, file=sys.stderr)
        return 1

    to_run = command(
        args.source,
        incoming,
        tasks,
        args.rsync,
        args.ssh_config,
        args.keep_purged,
        datetime.date.today(),
        extra,
    )
    print(shlex.join(to_run), flush=True)
    try:
        finished = subprocess.run(to_run, check=False)
    except FileNotFoundError:
        print(f"There's no {args.rsync} to run. Is rsync installed?", file=sys.stderr)
        return 1
    if finished.returncode != 0:
        print(
            f"rsync stopped with exit status {finished.returncode}. Don't run pigsty "
            "admit until a pull finishes.",
            file=sys.stderr,
        )
    return finished.returncode


if __name__ == "__main__":
    sys.exit(main())
