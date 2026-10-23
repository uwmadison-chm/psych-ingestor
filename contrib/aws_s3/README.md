# Pig and S3

Helpers for running Pig with an S3 bucket between the web server and the secure side. They're separate from Pig on purpose: Pig never talks to S3 itself, and nothing here is needed to run it.

```
cd contrib/aws_s3
uv sync
```

## pig-expire-versions

For an S3 service that doesn't implement lifecycle rules, like the campus IBM Cleversafe.

With versioning on, a deleted or overwritten object isn't gone: its old version stays in the bucket and can be brought back. That's the undo window. On AWS, a lifecycle rule ends it after a set number of days. Without one, old versions stay forever, including those of runs that have been purged. This does the lifecycle rule's job:

```
uv run pig-expire-versions --profile chm-pig-cleanup \
  --endpoint-url https://s3.drive.wisc.edu \
  --bucket chm-wilbur-01 --prefix "" --keep-days 30
```

That prints what it would delete. Add `--delete` to delete it.

- A version is deleted once it has been deleted or replaced for longer than `--keep-days`, counted from when that happened, the way AWS counts it.
- A delete marker is removed once it's older than `--keep-days` and nothing it hid is left.
- An object's current version is never touched.

Run it on a schedule from the secure side, with a key of its own that can delete versions. The web server's key shouldn't be able to: a web server that had been broken into could otherwise end the undo window itself.
