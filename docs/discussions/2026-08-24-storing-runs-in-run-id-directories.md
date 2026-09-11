# Storing runs in run-ID directories: issue #3 as it stood before the rewrite

*Superseded by [issue #3](https://github.com/uwmadison-chm/psych-ingestor/issues/3),
rewritten 2026-09-11. This is the text of that issue beforehand.*

Kept because the rewrite is mostly a cut. The argument for what the manifest carries, the
inventory of documentation the change contradicts, and the parts that turned out to belong to
[#9](https://github.com/uwmadison-chm/psych-ingestor/issues/9) are shorter or gone in the
current body. The conclusions are in the issue; the working-out is here.

Two things to know reading it. The text predates
[#12](https://github.com/uwmadison-chm/psych-ingestor/issues/12) and
[#17](https://github.com/uwmadison-chm/psych-ingestor/issues/17), so it says `abandoned`
where Pig now says `expired`, and it sketches a single `status` column where the runs table
now has `phase` and `disposition`. And the two comments correcting it are still on the issue,
where they're dated; the rewritten body folds them in.

---

`sweep` currently renames a finished dataset into a path built from the run's parameters —
`complete/stroop/10351/baseline_run-0002.jsonl`. This proposes that a run instead be a
**directory named for its run ID**, holding everything about the run, with the organized
tree produced separately by the CLI.

This contradicts things `configuration.md` and `definitions.md` currently describe as built.
That's fine for where the project is, but it should be an argued change rather than a quiet
one.

## Why

Three reasons, none of them about configuration changing mid-run:

- **#4 needs it structurally.** Media makes a run a set of files rather than one file. Once
  that lands, run-ID directories aren't a nice-to-have, they're required.
- **#2 wants a clean unit.** An immutable, hashable, deletable directory per run is what
  purge needs to operate on.
- **The naming pattern stops being permanent.** Getting a layout wrong today is
  irreversible, because the inputs to the rename survive only in the database. Being able
  to fix it later without a migration is worth something on its own.

There's a smaller version of the third already live: the original-case parameter value is
kept only in the database. Copy the completed data offsite without `pig.db` and `PPT-1003`
is gone — you have `ppt-1003/` as a directory name and no record of what the participant's
link actually said.

## What a run directory holds

```
done/{task_code}/{run_id}/
    manifest.json
    events.jsonl
    media/00001/000001.part      # once #4 lands
```

`manifest.json` carries everything needed to understand the run without asking Pig:

```json
{
  "type": "pig_run_manifest",
  "manifest_version": 1,
  "run_id": "9f3c1a7e-6b2d-4f80-9c11-2a5e8d40b7c3",
  "task_code": "stroop",
  "run_number": 2,
  "run_key": ["participant_id", "session"],
  "status": "complete",
  "started_at": "2026-08-24T15:03:11.482913+00:00",
  "finalized_at": "2026-08-24T15:41:02.117640+00:00",
  "done_at": "2026-08-24T16:00:00.004221+00:00",
  "parameters": { "participant_id": "PPT-1003", "session": "Baseline" },
  "extra_parameters": { "utm_source": "email" },
  "files": [
    { "path": "events.jsonl", "bytes": 48213, "sha256": "3b1f…" }
  ]
}
```

Notes on the fields:

- **`run_key`** is the ordered parameter *names*, not values. Without it `run_number` is
  uninterpretable offline — "this is run 2" means nothing unless you know 2 of what.
- **`parameters`** holds the declared parameters in their original case. **`extra_parameters`**
  holds everything else the link carried. They stay separate: declared parameters are
  safe-value checked, extra ones are arbitrary JSON, and only the first kind may appear in a
  layout pattern.
- **`status`** is `complete` or `abandoned`. `finalizing` never appears — by the time a
  manifest is written the status has resolved. `finalized_at` is null for an abandoned run,
  which never finalized.
- **`files`** is what makes the directory verifiable standing alone. #2 confirms a transfer
  by hash; with a run as a set of files, the manifest is the thing that covers the set, and
  #4 already assumes this shape for media parts.
- No event count. It's `wc -l`, and leaving it out keeps the manifest purely structural.
- No configuration snapshot, and nothing secret. See "`path` leaves configuration" below for
  the one piece of config this was ever going to need.

A run that never sent an event still gets an `events.jsonl`, empty. A uniform layout is
worth more than saving a zero-byte file, and it means "is the dataset there" stops being a
question with two answers.

## Events carry their arrival time

A stored line is `{"event_id": …, "data": …}` plus `timestamp` if the task sent one. The
server's receipt time exists only in the `events` table, which means it's the one fact that
would be destroyed by deleting those rows (#8). It's also more load-bearing than it looks:
`storage.sort_key` orders on `(timestamp, event_id)` as strings, so a task that sends no
timestamps gets a dataset ordered lexicographically by event ID — `"1"`, `"10"`, `"2"` for a
plain counter.

So **the line gains `stored_at`**, written at ingest:

```json
{"event_id":"12","data":{…},"timestamp":"…","stored_at":"2026-08-24T15:03:11.482913+00:00"}
```

Two things have to move with it.

**The content hash covers what the client supplied, not the whole line.** Hashing `stored_at`
would make every retry a fresh receipt time, a different digest, and a refused event. Hash
`event_id`, `data`, and `timestamp`; leave `stored_at` out. This restates the invariant rather
than weakening it — the rule exists to catch two genuinely different client submissions sharing
an ID, and a server-generated field can't differ between two different trials. The "the hash
covers what gets written" paragraph in `design_assumptions.md` needs rewording to say so.

**`file_dataset` dedupes on the same subset.** It currently drops lines that repeat *exactly*.
In the crash window — line written, process dies before the `INSERT`, client retries — the
second line carries a different `stored_at`, so the two are no longer byte-identical and the
duplicate survives filing. Compare on the hashed subset and keep the first occurrence. That's
safe for exactly the reason it's safe today: the write-time check guarantees same-ID lines
differ in nothing the client sent.

`sort_key` then falls back to `stored_at` instead of lexicographic event ID.

Worth noting what this buys beyond ordering: a line the database never recorded — the crash
window — still gets a `stored_at`, because it's written at ingest rather than reconstructed
later. That's precisely the case someone would be investigating.

`api.md` gains a note, since task authors will see the field in their data.

## When the manifest is written

**Once, at sweep time, atomically, never mutated.**

The rule that follows: **the database is the record for a run in flight; the manifest is the
record once the run is done.** No overlap, so the two can't disagree.

The cost is that an in-progress run directory is opaque — a UUID-named directory with no
manifest. That's acceptable because nothing off the web server ever looks at one: #2's
transfer and purge both operate on finished data, and the only reader of `in_progress/` is
someone standing on the machine that has the database.

It also keeps the web service out of it entirely, which is the "web service only does web
service things" rule holding without an exception. Under #4 it means per-part hashes are
computed once, by sweep, which doubles as a verification pass over exactly the bytes that
are about to be transferred.

## Storage layout

Two top-level directories under the data root instead of three:

```
data/
  in_progress/     runs still collecting, and runs waiting to be swept
  done/            runs sweep has finished with; nothing here ever changes again
```

Both are task-scoped: `in_progress/{task_code}/{run_id}/` and `done/{task_code}/{run_id}/`.
One path function serves both trees, and the move between them is structurally identical on
each side.

`complete/` and `abandoned/` merge into `done/`. The directory can't be named for a status,
because it holds both, and it can't be `filed/` because filing is the wrong verb — see
"Retire 'filing'" below.

**Sweep assembles the run directory in place under `in_progress/`** — sorts `events.jsonl`,
writes the manifest, fsyncs — **and only then renames it into `done/`.** A directory rename
within a filesystem is atomic, so a run appears in `done/` whole and self-describing or not
at all. An `rclone` copy can never catch one half-built. This requires `in_progress/` and
`done/` to be on the same filesystem, or the rename degrades to a copy and loses that
property; that's a `deployment.md` note.

Because the directory name is the run ID and never changes, an offsite copy needs no rename
logic ever and re-running it is a no-op.

Merging `complete/` and `abandoned/` helps #2: `rclone copy data/done` gets *everything* off
the public-facing machine, including abandoned runs, which are still human-subjects data.
Today you could copy only `complete/` and leave them behind. The complete-vs-abandoned
distinction moves to the secure side, where `pig organize` makes it.

## `path` leaves configuration

Nothing in the request path reads `path` any more, so keeping it in `pig.toml` would mean
the config file describing something the service never does. It goes away entirely. Layout
becomes `pig organize`'s business and nobody else's.

Where the layout actually lives is the invocation of `pig organize`, on the machine that
runs it — a script or a `justfile` next to the other post-transfer steps. That's a better
home than a file on the web server that the web server ignores.

This makes a rule possible, and it's worth stating as a rule rather than a default:
**`pig organize` never reads `pig.toml` and never opens the database.** If something can't
be done from a run directory alone, it isn't organize's job. That's what forces the manifest
to be genuinely complete.

For that to be pleasant, organize needs a sensible default layout computable from a manifest
alone. `run_key` in the manifest gives it one — the run-key values in declared order, then
the run number:

```
done/stroop/9f3c1a7e-…/   →   stroop/10351/baseline/run-0002/
```

Run-key values plus run number is exactly the minimal collision-free tree, since
`UNIQUE (task_code, run_key, run_number)` is what the database already enforces. So the
common case needs no pattern typed at all.

## Database changes

The manifest is generated at sweep time, so it has to be reconstructible from the run row
and the files on disk — with no reference to the config file, which can be edited or have
the task's entry deleted between run start and sweep.

That needs one thing captured that isn't today: the run key's parameter *names*. They exist
only in the task definition. The existing `run_key` column holds the joined lowercased
*values* (`"10351\x1fbaseline"`), and the names can't be derived from `parameters` either,
since the run key is a subset and nothing on the row says which subset.

Three changes, and two of them are here for a reason that belongs to a different issue.

**Split identifying columns into their own table.**

```
runs               run_id, task_code, run_key_hash, run_number,
                   status, started_at, finalized_at, done_at
run_identifiers    run_id, parameters, extra_parameters, run_key_names
```

Everything a participant's link supplied lives in `run_identifiers` and nowhere else. That's
the kind of rule this project prefers to make structural rather than remembered — the same
instinct as excluding `..` by construction, or putting the uniqueness constraint in the
database instead of in application code. It also means "did the identifiers actually leave?"
is a question you answer by looking at one table.

Not overselling it: `started_at` and `done_at` stay in `runs`, and they're quasi-identifiers.
The split is "direct identifiers here," not "this table is anonymous."

**Store the run key as a hash.** It's only ever compared, never read — it exists for the
`MAX(run_number)` lookup and the uniqueness constraint, and nothing parses it apart. Storing
it readable makes it a place identifiers accumulate for no benefit.

A plain hash, not an HMAC. The threat worth designing against is the public-facing machine
being compromised, and a key would live in `pig.toml` on that same machine — so it buys
nothing against the case that motivates it, while costing a secret to generate, protect,
back up, and rotate, plus a new way to silently break run numbering if it's ever lost or
changed. Be honest in the docs about what the hash is for: not protection, but keeping the
one column that can never be cleared from being readable.

**Drop `dataset_path`.** It's derivable — `done/{task_code}/{run_id}/` — and an absolute
path stored in a database is wrong the moment `data_root` moves.

No migration. There's no data anyone depends on yet, and that's exactly why the first two
changes belong here rather than in the issue that motivates them: doing them later means
rewriting real rows.

### What the run row is for after a run is done

Nothing needs the database to find, read, or interpret a finished run's data — that's what
the manifest is for. But the row itself has to survive, for one reason: **run numbering.**
`_insert_run` does `SELECT MAX(run_number) FROM runs WHERE task_code = ? AND run_key = ?`
with no status filter, so a participant returning for a third run is numbered correctly only
because rows describing their first two still exist.

Which gives #2 a rule it needs: **purge deletes files, never run rows.** Dropping a row would
make `MAX(run_number)` return null, the next run of that key would be numbered 1 again, and
the organized tree would grow a second `run-0001/` for the same participant under a different
run ID. Nothing would error — the uniqueness constraint is satisfied, because the old row is
gone. It would just quietly be wrong.

The row survives as a counter, not as a description. That distinction is what the table split
and the hashed key are preparing for.

## What this issue is not

`pig organize` — pattern syntax, whether it copies or hardlinks, what it does about
re-running over an existing tree, how it handles abandoned runs — is #9. It
needs its own design pass now that we know it runs detached from the web server and its
database.

The seam: **a question belongs here if its answer changes what bytes land in a run
directory.** Everything else waits.

One thing has to be carried across that seam deliberately rather than falling through it.
`definitions.md` promises that a reader pointed at completed data gets only datasets known
to hold everything. Today the `complete/` vs `abandoned/` split enforces that at the layout
level. With `done/` holding both, the promise moves into `pig organize` — complete-only by
default, abandoned runs on request or into a separate tree.

## Docs this contradicts

- `definitions.md`: "Run IDs don't appear in filenames — that's the run number's job."
  Directly reversed.
- `definitions.md`: the **Dataset** entry describes one run as one `.jsonl` file at a
  parameter-derived path.
- `definitions.md`: the **Run status** table, and the `filed_at` paragraph, which goes away
  with the verb.
- `configuration.md`: `path` disappears. The open questions under "Where data goes and what
  it's called" resolve rather than persist — but see below, they don't all simply vanish.
- `configuration.md`: the three-directory description under the data root.
- `configuration.md`: "Validating configuration" — `pig check` stops printing where a task's
  data lands, since it's now invariant (`done/{task_code}/{run_id}/`).
- `design_assumptions.md`: "Link parameters can be used in filenames; be careful."
- `design_assumptions.md`: "The hash covers what gets written" — now covers what the *client*
  wrote. See "Events carry their arrival time" above.
- `api.md`: stored events gain a `stored_at` field.
- `design_assumptions.md`: storage paths being per-task is currently part of how a task
  "lives under a project directory" without Pig knowing about studies. That flexibility moves
  to organize. Minor, but it's load-bearing in the no-studies argument, so it should move
  deliberately.
- `docs/trying_it.md`: the walkthrough cats
  `local/data/complete/stroop/10351/baseline_run-0001.jsonl`.
- `pig.example.toml`: loses its `path` lines.

### Retire "filing" as a verb

`filed_at` is the wrong name for what the code actually sets, which is the moment sweep
finished with a run — not the moment the data reached its final home, which is what
`definitions.md` currently defines the word to mean. Rather than fix the definition, retire
the verb: there should be one vocabulary, and the directory is `done/`.

- The column and the manifest field become `done_at`.
- Prose says sweep **finishes** a run, not files it. Roughly two dozen verb-sense uses
  across `configuration.md`, `definitions.md`, `design_assumptions.md`, `deployment.md`,
  `api.md`, `trying_it.md`, and `README.md`. "File" as a noun — a `.jsonl` file — is
  untouched.
- `file_finished_runs` and `file_dataset` get renamed to match.

Whether data reached its final home stops being something Pig tracks. The transfer is
external (an `rclone` push), so #2 is where that fact belongs, if anywhere.

### The lowercasing exception relocates rather than shrinking

Worth calling out separately, because it's easy to get wrong. `design_assumptions.md` and
`configuration.md` justify lowercasing entirely on the macOS-splits-on-Linux filename
argument. That argument moves to `pig organize`, where it still holds.

But lowercasing also decides the run key (`runs.py:80`), and therefore whether `PPT-1003`
and `ppt-1003` are the same participant for run-numbering purposes. That has nothing to do
with filenames and survives this change untouched. It needs its own written rationale —
about participant identity, not filesystem portability — rather than inheriting one that no
longer applies at that layer.

### Two open questions that move rather than close

"Must the pattern include `{run_number}`" and "which patterns can collide" don't close by
construction. They're true of storage now, but organize still renders a pattern into a tree
and two runs of one key can still collide there. What changes is *when you find out*: today
it's a `pig check` failure, afterwards it's an organize-time error on a different machine.
Given "checking and errors are better than silent fixes," that's an argument for organize
validating its pattern and printing its plan before writing anything. Belongs in the
organize issue, but it shouldn't be recorded here as resolved.

## Settled

- Run-ID directories, with a manifest, as the storage of record.
- The manifest is narrow: identity, status, timestamps, parameters, files. No config
  snapshot, nothing secret.
- Written once by sweep, never mutated. Database is authoritative in flight, manifest
  afterwards.
- `in_progress/` and `done/`, top-level, task-scoped, same filesystem, atomic rename between
  them.
- `manifest_version` on every manifest, plus a `type` marker so a stray file is
  identifiable.
- A run with no events gets an empty `events.jsonl`.
- Stored lines gain `stored_at`; the content hash covers client-supplied fields only, and
  `file_dataset` dedupes on that same subset.
- `path` leaves configuration; `run_key` stays a task setting and enters the manifest.
- "Filing" retires as a verb; `filed_at` becomes `done_at`.
- `runs` / `run_identifiers` split; run key stored as a plain hash; `dataset_path` dropped.
- Purge deletes files, never run rows.
- No migration.

## Related

- **#2** gets the rule above, and is where the database's identifying columns actually get
  cleared.
- **#8 — making the database forget**: deleting event rows once dedup is over, nulling the
  timestamps the manifest now carries, and the health and CLI changes that follow. Enabled by
  this issue rather than part of it — the database can only forget because the manifest
  remembers.
- **#9 — `pig organize`**, per "What this issue is not" above.
