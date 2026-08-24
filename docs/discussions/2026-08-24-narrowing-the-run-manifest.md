# Narrowing the run manifest

*Superseded by [issue #3](https://github.com/uwmadison-chm/psych-ingestor/issues/3),
rewritten 2026-08-24. Originally a comment on that issue.*

Written while reconsidering the first draft of #3, which justified run-ID directories
mainly as a defence against a task's `path` being edited mid-run. This is where that
framing got dropped and the manifest's contents got narrowed to what the export machine
actually needs. The conclusions are in the issue; the argument is here.

---

Follow-up after sitting with this for a bit (talked it through with Claude) — a few things worth writing down before picking this back up Wednesday.

**The framing above overweights config mutation.** Editing a task's `path` while a run of that task is in flight is real but rare, and on reflection I don't think it's actually the reason to do this. What actually justifies it:

- **#4 needs it structurally.** Media makes a run a set of files, not one file. Run-ID directories aren't a nice-to-have once that lands, they're required.
- **#2 wants a clean unit.** An immutable, hashable, deletable directory per run is what purge wants to operate on. (To be fair, a single file works for this today too — it's #4 that forces multi-file-per-run, and once that's true, only a directory gives purge one thing to hash/verify/delete.)
- **The naming pattern stops being permanent**, independent of the above. Being able to fix a bad `path` pattern later without a migration is worth something on its own.

**Run parameters are already saved, for what it's worth.** `runs.parameters` / `runs.extra_parameters` in SQLite, original case preserved — I went and checked, nothing's lost today. The real gap is narrower than "parameters aren't saved": they only live in the *database*, not in the filed data. Copy `complete/` offsite without `pig.db` and the original-case value is gone; the lowercased directory name is all that's left.

**The thing that actually settles the manifest question: export runs on a different machine, without the database.** Per the "resolution" section above, `pig export` runs wherever the data ends up — the secure machine after the #2 transfer, or a laptop — never the web server. So a manifest isn't there to survive config drift during a run; it's there because *the machine building the readable tree has no database to ask.* Everything export needs has to be sitting in the run directory itself, because by the time export runs, `pig.db` may not exist anymore — that's the point of purge.

That narrows what a manifest actually needs to carry: run identity (`run_id`, `run_number`, `task_code`), `status`, timestamps, and the parameters (real + extra, original case). It does **not** need a snapshot of the task's full config (`run_key`, `open`, `max_event_size`, `abandon_after`) — that was scaffolding for the mutation scenario above, which doesn't need defending against.

**Tentative scope for a first pass:** run-ID directories + a narrow manifest (identity/status/timestamps/parameters only), landing storage-of-record and nothing else. `pig export` — patterns, configurability, exactly where and how it runs — stays a separate follow-up; it needs its own design pass now that we know it's meant to run detached from the web server and its database entirely.

Picking this back up Wednesday.

