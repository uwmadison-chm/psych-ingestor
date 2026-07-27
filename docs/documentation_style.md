# Writing for Psych Ingestor

How to write Pig's public-facing documentation — the API reference, configuration docs,
error messages, and CLI help.

Our readers run studies. They write enough code to make a task work, and they are reading
this because something needs to be collecting data, not because they find HTTP interesting.
Write for a competent person outside your field.

This guide is for prose aimed at users. Internal documents like
[design_assumptions.md](design_assumptions.md) can assume a reader who's working on Pig
itself.

## README.md is the reference

The project maintainer will keep `README.md` in a state they consider well written. When this
guide and the README disagree about tone, phrasing, or how much to explain, **follow the
README** — and say so, because the guide is then the thing that needs updating.

Read it before writing any user-facing prose. It's the shortest description available of
what good looks like here, and it's maintained deliberately rather than incidentally.

## Say what the reader does

Describe actions the reader takes, not properties the system has.

> No: The endpoint accepts a JSONL payload.
>
> Yes: Send your events, one JSON object per line.

> No: Deduplication is performed on the client-supplied event identifier.
>
> Yes: Pig uses your event ID to make sure it never stores the same event twice — so if a
> request fails, it's always safe to send it again.

The second version of each is longer. That's fine. Length is cheap; a reader who has to
guess is not.

## Words to avoid

Not because they're wrong — because they cost the reader a lookup:

| Instead of | Write |
| --- | --- |
| idempotent | safe to send more than once |
| payload | what you send / the events you send |
| endpoint | (name the actual request, or "URL") |
| serialize | turn into JSON |
| persist | save / store |
| validate | check |
| authenticate | check that the link came from you |
| instantiate | create / start |
| schema | the fields Pig expects |
| atomic | either it all saves or none of it does |

Some jargon has no plain equivalent worth inventing — JSON, HTTPS, CORS. Use those, and
give a one-line explanation the first time on a page.

## Lead with the common case

Start with what almost everyone does. Put the unusual case after it, marked as unusual.

A reader configuring their first task needs the three fields everyone sets, not a complete
field reference. The complete reference goes further down the page, where the person who
needs it will look.

## Explain why, briefly, when the reason is not obvious

A reader who understands why signing exists will configure it correctly. A reader who
doesn't will copy an example and hope.

One sentence is usually enough: "Signing lets you verify a participant came from the link
you sent, so they can't change their own participant ID by editing the URL."

Don't do this for everything. Reasons attached to obvious things are noise.

## Show real examples

Examples should be things a reader could actually run, with plausible values —
`participant_id=10351`, not `<PARTICIPANT_ID>`. If a value is a placeholder, make it
obvious which part they replace.

Prefer a complete small example to a fragment. Fragments make readers guess at the context,
and they guess wrong.

## Write errors the reader can act on

Error text is documentation, read at the worst possible moment. Every error should say what
happened and what to do.

> No: Invalid link parameters.
>
> Yes: This task expects a `participant_id` in the link, and the link didn't have one.
> Check the link you sent participants, or the `parameters` setting for this task.

Name the specific thing that was wrong. "Invalid configuration" sends someone hunting
through a file; "the `data_path` for task `stroop` points outside the data directory" does
not.

## Formatting

- Sentence case for headings.
- Short paragraphs. Two or three sentences.
- Lists for things a reader chooses among or does in order; prose for anything with
  reasoning in it. A list of five bullets that each need a "because" should be prose.
- Backticks for anything typed literally: field names, values, commands, URLs.
- Link to the other page rather than restating it, so the two can't drift apart.

## Things not to do

- Don't apologize for the software or hedge with "simply," "just," or "of course." If it
  were simple the reader wouldn't be reading.
- Don't document intentions as though they're features. If it isn't built, mark it.
- Don't assume a terminal. Some readers will be on Windows, and some will do everything
  from a GUI editor.
- Don't assume prior Pig knowledge on any given page. Readers may arrive by search, in the middle.
