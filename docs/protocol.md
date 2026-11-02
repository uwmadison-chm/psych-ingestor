# The Pig protocol

What any Pig server promises and what a client can count on: how a run moves through its states, what's guaranteed about storing, and what's allowed to happen when requests arrive at once. The shapes of the requests and replies are in [api.yaml](api.yaml); [api.md](api.md) explains them to people writing tasks. This page is for people writing or changing a server or a client, or the tests for either.

**Status: draft.** Written on 1 November 2026, against `main` at `8434b17`, as the first step toward the TypeScript server planned in #124. Where today's server falls short of a rule here, or an open issue will change one, it says so. This page, `api.yaml`, `api.md` and the code should agree. When they don't, one of them is wrong, and which one is a question to raise, not to settle quietly.

## Words

[definitions.md](definitions.md) has task, run, run key, run number, event, event ID and media item. These are used precisely here:

- **Server:** anything that answers Pig's API. Today that's `pig serve`.
- **Client:** anything that makes requests: a task, the JavaScript client, `pigsty`.
- **Acknowledge:** a server acknowledges an event when it lists the event's ID in a reply's `stored`, a part when it lists the part's number in `media.stored`, a media start when it replies with the media ID, and a finalize when it replies `200`.
- **Durable:** still there after the server process dies or the machine loses power. On a local disk, that means written and fsynced, along with the directory that holds it. On object storage, it means the store said the write succeeded.
- **Close:** the moment a run stops taking data, because the task finalized it or because it expired.
- **Finished run:** a closed run as the secure side receives it. See [What a finished run holds](#what-a-finished-run-holds).
- **Content:** what makes two events with the same ID the same event or a collision. See [Content and size](#content-and-size).

## A run's states

A client sees four statuses. The server keeps two facts, a phase and a disposition, and derives the status from them; [definitions.md](definitions.md#run-status) has that table.

```
start ──▶ in_progress ──finalize──▶ finalizing ──server finishes the run──▶ complete
               │
               └──deadline passes, server closes it──▶ expired
```

- A run starts `in_progress`. It leaves by finalize, to `finalizing`, or by expiry, to `expired`. Exactly one of the two happens, once, and whichever comes first wins.
- `finalizing` becomes `complete` when the server has finished the run. Today that's when the sweep has written its manifest and moved it into `done/`. A client treats the two alike: both mean the task is done, and nothing the client does changes which one it sees.
- `expired` stays `expired`. An expired run gets finished too, but its status doesn't change when it does.
- Nothing goes back. No request and no command reopens a run, however it closed (#12).
- Closing a task (`open = false`) refuses new runs only. Runs already in progress carry on and expire on their own schedule.

### Expiry

Every run has a deadline: when it started, plus its task's `expires_after`.

- A server may close a run as expired at any moment after its deadline, and never before.
- Until it does, the run is `in_progress` and takes data like any other run. A client can't tell that the deadline has passed and doesn't need to: its next request after the close gets `409` with status `expired`, and an expired run is a normal ending, not an error.
- Today the next `pig sweep` closes it, so a run can go on taking events for up to one sweep interval past its deadline. A server that closes a run at the first request after its deadline meets this rule too.
- Today the sweep reads `expires_after` from the configuration as it is then, not as it was when the run started, so editing it changes the deadline of runs in progress (#56). Runs of a task taken out of the configuration never expire (#55).

## The guarantees

Numbered so that tests and issues can refer to them. Each says how today's server meets it, and which tests check it.

### 1. Acknowledged means durable

Before a reply acknowledges anything (an event, a part, a media start, a close), that thing is durable. If the server dies before it replies, the client gets no reply and sends again, which guarantee 2 makes safe.

Today an event's line is appended to `events.jsonl` and fsynced before its receipt is written to SQLite, which runs with `synchronous = FULL`. A part is written to a scratch file outside the run, fsynced, and renamed into the run under the database's write lock before its receipt is written. [design_assumptions.md](design_assumptions.md#the-write-path-and-what-it-leaves-in-the-file) explains the order: data first, then the receipt, so a crash between them leaves a repeat, never a gap.

Checked by `tests/test_durability.py`, `tests/test_runs.py::test_events_are_on_disk_before_the_response`, and the invariant "every receipt has its line or its part on disk" in `fuzz/test_run_lifecycle.py`.

### 2. One content per event ID

For each event ID in a run, at most one content is ever acknowledged. After that, an event with the same ID and the same content is a retry: Pig writes nothing and reports it as stored. An event with the same ID and different content is a collision: refused with `422` and `can_retry: false`, and the acknowledged version never changes.

A media start is an event. Its event ID is one of the run's event IDs, so a media start reusing an ordinary event's ID is refused with `422`, and the same start sent again is a retry that gets the same media ID back.

Today the receipt check and the write happen under SQLite's write lock.

Checked by `tests/test_runs.py`, `tests/test_media.py`, and `fuzz/test_run_lifecycle.py`.

### 3. Every acknowledged event is in the finished run

At least once. The finished run may also hold lines that were never acknowledged, and repeats:

- After a crash between writing an event's line and recording its receipt, the line is there without a receipt. If the task sends the event again, it's written again, and the run holds it twice.
- If the task never sends it again, the run holds a line that was never acknowledged.
- If the task sends a different event under that ID instead, the run holds two different lines with one ID. That needs a crash and a broken task together. [design_assumptions.md](design_assumptions.md#the-case-this-doesnt-catch) has the reasoning.

A server never removes, reorders or chooses among these lines; whoever reads the data does. Every failure bends toward duplicated and visible, never missing and silent.

This covers the race with the close (#52): an event sent as the run closes is either acknowledged and in the finished run, or refused with `409`.

Checked by `tests/test_closing_races.py` and `fuzz/test_run_lifecycle.py`.

### 4. Order

Across requests, the finished run's lines are in the order the server committed them, and `stored` lists event IDs in that order. Within one request, and between requests that arrive at once, the order isn't specified. Today's server keeps a request's order, but a JavaScript server reading the request with `JSON.parse` would put IDs that look like array indexes (`"1"`, `"2"`, `"10"`) first, in numeric order.

The order of lines isn't a record of when things happened, since a task that was offline sends its events long after it recorded them. Whoever analyzes the data sorts on a timestamp the task records in `data`. Pig never orders events by anything in `data`.

### 5. Run numbers

For each task and run key, run numbers start at 1, go up by one with every start, and are never reused. That includes starts whose reply never reached the client, and runs that have since been purged. A start that would make run 10,000 is refused with `409`.

Today the next number and the new row are one SQLite transaction, and a purged run's row stays as the counter.

Checked by `tests/test_runs.py`.

### 6. A run closes once

A run closes exactly once, as finalized or expired. After the close, nothing more is acknowledged: events, media starts, parts and finishes all get `409`. Finalizing a finalized run gets `200` and the status as it is now. Finalizing an expired run gets `409`, with the refusal in `errors` under `run`.

Checked by `tests/test_closing_races.py`, `tests/test_runs.py`, `tests/test_media.py::test_a_closed_run_refuses_all_three`, and `fuzz/test_run_lifecycle.py`.

### 7. Media

- Media IDs in a run start at 1 and go up by one with every new media item, to 99,999.
- A part number, from 1 to 999,999, is stored at most once per item. The same bytes again get `200` and nothing is written. Different bytes get `422` with `can_retry: false`, and the stored bytes never change.
- Finishing with a count of N finishes the item if Pig holds exactly parts 1 through N. Otherwise it's refused with `422`: `can_retry: true` if parts are missing, `false` if Pig holds parts beyond N. A finished item takes no more parts (`409`, `can_retry: false`). Finishing again with N gets `200`; with another count, `422`.
- A finished item holds exactly parts 1 through N in the finished run. An item nobody finished keeps every part it got, and the manifest says it wasn't finished. Neither stops the run being finalized.
- An acknowledged part is in the finished run (guarantees 1 and 3 hold for parts as for events). A part that races the close or the finish is either acknowledged and in the finished run, or refused.

Checked by `tests/test_media.py`, `tests/test_closing_races.py`, and `fuzz/test_run_lifecycle.py`.

### 8. Run IDs

A run ID is random (a version 4 UUID), unique across the deployment, and the only thing a later request needs to find the run. A real run ID used with another task's code gets `404`, the same as one that doesn't exist. Run IDs aren't secret, since participants can read theirs in the browser, but they can't be guessed.

Checked by `tests/test_runs.py::test_a_run_id_from_another_task_is_a_404`.

### 9. Purge deletes only what the secure side holds

The server deletes a run only if it's finished and the secure side sent a purge hash for every one of its files, all matching, with none missing and none extra. Purging a purged run gets `200`. The run's number stays taken (guarantee 5).

Checked by `tests/test_purge.py` and `tests/test_sty_purge.py`.

### 10. Every refusal is the server's own

Whatever a request holds, the reply is a `2xx` or a `4xx` that the server wrote and that says what's wrong (#106). A `5xx` means "try again", so a server that answered one for something a task sent would be asked again forever. The only `5xx` a server sends on purpose is `503` when other starts at the same moment kept it from handing out a run number.

Today some refusals come back in FastAPI's shape rather than Pig's: malformed JSON, a media ID or part number that isn't a whole number, an unknown URL, a wrong method (#64).

Checked by `fuzz/test_any_request.py`.

### 11. Starting a run is safe to retry

**Not built** (#109). Today every start makes a new run. A start sent again after a lost reply leaves an empty run that expires later, and the participant's data lands in the next run number.

## What a client can count on, and what it owes

Every request except a start (guarantee 11) is safe to send again. After no reply, or a `5xx`, send the same request again later.

| Reply | Means | What the client does |
| --- | --- | --- |
| `200`, `201` | Stored | Stop holding whatever `stored` lists |
| `409` | The run has closed, or the media item is finished | `can_retry: true`: start a new run and send it there. `false`: don't send it again |
| `422` | Something was refused, and the rest of the request may have been stored | Read `stored` and `errors`. `can_retry: false`: drop it and report it |
| `403` | The link's signature doesn't check out | Don't send again. The participant needs the link they were sent |
| `404` | Wrong task, run or item, or the task doesn't take media | Don't send again |
| `413` | Too big | Send smaller pieces |
| `5xx`, or none | Unknown | Send the same request again later |

A client owes the server three things:

- **Build each event once, and send that same event every time.** An event rebuilt for a retry, with the clock read again, is different content under the same ID, which is a collision. The same goes for parts: resend the same bytes.
- **Encrypt once, when the event is recorded**, for an encrypted task. Encrypting the same data twice gives different bytes.
- **Hold each event and part until its ID or number is in `stored`.**

## What can happen at once

The races a server has to get right, and the only outcomes allowed. Some have tests already, in `tests/test_closing_races.py`. Each is meant to become a named test of its own, and running them with real concurrency is #122.

| At the same time | Allowed outcomes |
| --- | --- |
| Two requests with the same new event, same content | One writes it. Both acknowledge it. The run holds it once, unless a crash intervenes (guarantee 3) |
| Two requests with the same new event ID, different content | One is acknowledged, the other refused as a collision. Which one wins isn't specified |
| An event, and the close | The event is acknowledged and in the finished run, or refused with `409` (#52) |
| Finalize, and expiry | One close. Finalize gets `200` and `finalizing` if it won, `409` and `expired` if expiry did |
| Two finalizes | One close. Both get `200` |
| Two starts with the same run key | Two runs, with consecutive run numbers. Which gets which isn't specified |
| Two media starts with different event IDs | Two items, with consecutive media IDs |
| A media start, and the close | As for an event |
| Two uploads of the same part | One is stored. The other gets `200` if its bytes are the same and `422` if not. The stored bytes are always the ones whose hash was recorded |
| A part, and the finish | The part is acknowledged before the finish counts, and is counted (or makes the finish refuse it as beyond N). Or the part is refused with `409`. Never acknowledged and then left out of 1 through N |
| A part, and the close | The part is acknowledged and in the finished run, or refused with `409` |
| Two purges of one run | Both get `200` |
| The server dies, at any moment | After a restart, guarantees 1 to 10 still hold. Work that wasn't acknowledged may be lost or done twice |

A client whose request got no reply can't know whether it happened. Sending it again is how it finds out.

## Content and size

An event's content is its `event_id` and its `data`. What Pig adds, in `metadata`, is never part of it.

Two events are the same when their event IDs are the same text and their `data` is the same JSON value. JSON has one kind of number, so numbers are compared as numbers: `1`, `1.0` and `1e0` are the same. Objects are the same when they have the same keys with the same values, in any order. Pig stores `data` as that value, not as the text that arrived, so the stored line can space, order and write numbers differently from the request.

A server tells whether two events are the same by comparing a canonical form: text that comes out identical exactly when the values are the same. It keeps the sha256 of that text as the event's receipt. An event's size, for `max_event_size_bytes`, is the canonical form's length in bytes, so the limit doesn't depend on how a client spaced its JSON. A media start is measured the same way.

Today's server uses this canonical form, the JSON of `{"data": ..., "event_id": ...}` with object keys sorted at every level, no whitespace, and characters outside ASCII written as themselves, in UTF-8:

```python
json.dumps({"event_id": event_id, "data": data}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
```

It falls short of the rule: Python writes `1` and `1.0` differently, so a retry that changed one into the other would be refused as a collision. Clients resend what they sent, so it's unlikely to come up. RFC 8785 (JCS) is a canonical form that meets the rule. It writes numbers the way JavaScript does, so a JavaScript server gets it almost for free, and Python needs a number formatter of its own. Changing the form changes the receipts, so runs open across the change would need both.

Numbers are compared as IEEE 754 doubles, which is what JavaScript holds and what I-JSON (RFC 7493) recommends. Integers beyond 2^53 can't all be held exactly that way, so two that differ only past that point compare as the same.

Two parts are the same when their bytes are. The server keeps each part's sha256.

Event IDs are at most 256 characters. Path parameter values match `^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$`. Text parameter values are at most 2,048 characters, with no control characters and no unpaired surrogates (Unicode categories Cc and Cs). Characters here are code points, which isn't how JavaScript counts them (#118).

## The storage contract

**Not built.** This is for the TypeScript server planned in #124. Today's server uses SQLite and the files in the run directory, and doesn't go through this.

That server's core reads and writes storage through five calls and relies on nothing else about the store, so the same core runs on a filesystem, on S3, on R2, and on an adversarial in-memory store in the tests. Every store has to give these guarantees:

| Call | Guarantee |
| --- | --- |
| get(key): its bytes and version, or absent | Sees every write that completed before the call began |
| create(key, bytes): created, or already there | Either it all saves or none of it does. Of several creates of one key at once, exactly one succeeds |
| replace(key, bytes, version): replaced, or changed since | Either it all saves or none of it does, against every other write to that key. Succeeds only if the key still has that version |
| list(prefix): keys | Sees every create that completed before the call began |
| head(key): its version, or absent | As get, without the bytes |

A create or replace that succeeded is durable. A call whose outcome is unknown, after a timeout or a lost reply, may or may not have happened, and the core has to be right either way. Nothing on the request path deletes.

On a filesystem, create opens the file with `O_EXCL`, writes, and fsyncs the file and its directory. Replace takes an in-memory lock for the key, compares versions, writes a temporary file, fsyncs it, renames it over the old one and fsyncs the directory. Only one server process may use a store at a time, which a lock file taken at startup enforces. On object storage, create is a PUT with `If-None-Match: *`, replace is a PUT with `If-Match` and the ETag, and `412` means already there or changed since. A store that doesn't support both headers can't meet the contract, and the server checks at deploy. #124 has the design that sits on top: a ledger per run, changed only by replace.

## What a finished run holds

What a run looks like on the secure side, once `pigsty admit` has checked it and moved it into `runs/{run_id}/`, is the contract with whoever reads the data. Today the server produces it nearly as it is, and admit checks it against the manifest and decrypts it. Another server can hand pigsty something else to assemble it from, as long as what admit leaves is the same. What `pigsty organize` builds from it is in [organizing.md](organizing.md).

```
runs/{run_id}/
  events.jsonl
  pig-run.json
  media/00001/000001.part
  media/00001/000002.part
```

**`events.jsonl`** has one event per line, as compact JSON with sorted keys, each line ending in a newline:

```json
{"data":{"rt":843,"type":"trial"},"event_id":"2","metadata":{"stored_at":"2026-08-24T15:03:11.482913+00:00"}}
```

`metadata` holds facts Pig generated about the event: `stored_at`, an ISO 8601 time with its UTC offset, and for a media start, `media_id`. A line cut short by a crash can be in the file, on a line of its own (#53). Repeats and unacknowledged lines can be too (guarantee 3). For an encrypted run, admit has replaced each event's `data` with what it decrypts to.

**`media/`** holds each item's parts as Pig received them, named `{media_id}/{part}.part`, zero-padded to five and six digits so that a sorted listing is in order. Pig never joins them. For an encrypted run, admit has decrypted each one.

**`pig-run.json`** says what the run is: `type` (`pig_run`), `run_file_version` (2), `run_id`, `task_code`, `run_number`, `run_key_parameters`, `path_parameters`, `signed_parameters`, `disposition` (`finalized` or `expired`), `started_at`, `closed_at`, `done_at`, `parameters`, `extra_parameters`, `media` (each item's media ID, event ID, whether it was finished, and its count if it was) and `signed_by`. It comes from the manifest, without the file hashes.

The manifest (`manifest.json`, version 4) travels with the run from the server to the secure side, and stays in `incoming/`, since its hashes describe the files as they traveled, ciphertext included. It has the fields above, `encrypted_to`, and every file's path, size and sha256. It's written once, after the close, and never changed. It has no event count, because `events.jsonl` can hold repeats.

## Open questions

Each of these changes something above. None is settled here.

- **A partly stored batch** answers `422` today. #113 leans toward a `2xx` (probably `200`, maybe `201` when something was created), and a `4xx` only when nothing in the request was stored.
- **`stored` lists every event ID in the run**, so a client sending one event at a time pays for the whole list each time. #113 would list only the IDs from the request.
- **Every non-2xx body gets a `message`** (#64), and the media start reply's `max_part_size` becomes `max_part_size_bytes`, to match the task's settings.
- **Strict JSON** (#107). Clients must send UTF-8; that's decided. `NaN`, the infinities and duplicate keys are to be refused rather than stored.
- **The canonical form.** Whether today's server moves to RFC 8785 (JCS), so that `1` and `1.0` are the same event there too and both servers hash alike, and how runs open across the change are handled. See [Content and size](#content-and-size).
- **Integers beyond 2^53.** Whether to refuse them along with #107's other refusals, as I-JSON advises, rather than compare them as doubles.
- **The deadline fixed at start** (#56), and runs of a removed task (#55).
- **Refusing unknown fields in the purge request** (#114), so a future `dry_run` can't purge for real on an older server.
- **Media event IDs** that `pigsty organize` can't use as directory names (#57).
- **Whether finalize reports an event count.** Deferred deliberately; provisionally, no.
