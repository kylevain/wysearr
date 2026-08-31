# Working with Kyle

## Instruction format — required

- **One thing at a time.** One command per block. Never append a verification
  step or a "while you're there" extra. If something else matters, say so in
  prose after answering what was asked.
- **Label every code block with its target machine**, e.g. `**ON WYSEARR**`.
  Ten machines, constant SSH — an unlabeled block gets run on the wrong host.
  The label is a heading, not part of the command.
- **Commands must paste with zero modification.** No placeholders, no
  "change X to Y", no nano/vim/sed editing steps. Write files with heredocs.
  Make blocks idempotent.
- **Never write "navigate to" or "find that option."** GUI steps need exact
  element names, in order.
- **Keep it short.** Lead with the command. Explain only what is non-obvious
  or a trap.
- **Document as you go**, not at the end. Sessions get interrupted; a wrap-up
  that never gets written is the real failure mode.
- **Do not write closing summaries.** End on the next action or a question.
- **When Kyle raises a concern, change approach** — do not restate the same
  approach in new words.

## Decide rather than ask

Kyle is not reviewing your reasoning step by step. When a choice is
reversible, make it, state what you chose in one line, and continue. Only
stop when the choice is irreversible — destroying data, touching production,
spending money — or when you cannot proceed without information only he has.
Sort orders, colours, thresholds and filenames are not worth asking about.

**But own the choice.** Deciding without asking means checking the decision
against the real data, not picking a default and moving on. A sort order that
buries the useful rows, a threshold that never fires, a colour scheme that is
unreadable at night — those are yours to catch, not his to report back to
you. If there is a number involved, compute it. If there is a distribution,
run the decision against it.

## Handoff format

When Kyle needs to relay findings to someone else, end the response with a
short block under a `--- HANDOFF ---` line: what changed, what you found,
what decision is needed. Plain prose, no code, no tool output.

## Verify, don't assume

Several tools in this lab accept a value silently and do nothing. Config is
layered: defaults in source, overridden by systemd `Environment=` lines,
overridden by EnvironmentFile. Reading source shows the value that LOSES.
Check `systemctl --user cat <unit>` for what actually applies.

Agent reports of success have repeatedly not survived a grep. Verify state
directly rather than trusting a prior session's summary — including this
file's.

## This repository

This is a CLONE for reading and editing. **Production is `~/homelab` on
wysearr** (192.168.4.86). Changes reach production via push, then pull on
wysearr. Do not assume edits here are live.

wysearr is a 2011 AMD G-T48E and cannot run Claude Code (no SSE4.2/AVX —
SIGILL). That is why this clone exists on batfire.

## Permission boundaries

**This repo is a disposable clone on batfire. Nothing here reaches production;
worst case is a re-clone. Production is `~/homelab` on wysearr, reachable only
by ssh and only with Kyle's approval. Work freely in the clone — the gate is
on anything that leaves it.**

Encoded in `.claude/settings.local.json` (gitignored — recreate it on a new
machine). Note the permission system appends its own narrow rules to that file
when a prompt is approved, which can overwrite hand-written entries; check it
still holds the broad rules if prompts reappear.

Pre-approved, no prompt: `python`/`python3` (including heredocs), `pytest`,
`unittest`, `cd`, `mkdir`, `cp`, `mv`, `sed`, all read-only inspection, and
`git add`/`commit`/`checkout`/`stash`/`status`/`diff`/`log`/`show`. Edits and
writes anywhere under `/home/batadmin/wysearr`.

Always ask first — and these are the only ones that matter:
`ssh`, `scp`, `rsync`, `git push`, `sudo`, `docker`, `systemctl`.

Also ask before restarting or stopping Huey. It is live; a restart drops the
Discord gateway and runs whatever SQLite migrations are pending in
`initialize()`.

Run tests freely and report real output. Never claim a suite passed without
running it.

## Known data issues

**Request #126 needs manual cleanup.** It is `audiobooks`, `raw_request` and
`title` both `"m4b"`, status `queued`, service `abba`, and its
`external_title` is `An Abundance Of Katherines - M4B - Clean Source - John
Green`. A bare reply of `"m4b"` was parsed as a new request, ABBA matched a
filename containing `M4B`, and it queued a book nobody asked for. **The book
is being kept.**

Two fields still need correcting:
- `title`, so Louie stops displaying the row as `"m4b"` (Louie shows
  `title or raw_request`, and it is read-only, so the fix must be in Huey's
  table).
- `target_key`, currently `v1:["audiobooks","","m4b",""]`. While that value
  stands and the row is `queued`, `create_request` collapses every future
  `m4b`/`mp3`/`epub` reply in `#audiobooks` onto #126.

The underlying bug is fixed — `request_target_key` now returns `None` for text
that cannot identify a work, and such a request never reaches an acquisition
service. **#126 predates the fix and is not corrected retroactively.** The fix
is also what makes clearing its key durable: `_backfill_target_keys` re-keys
`NULL` rows in `queued`/`complete`/`completed` at every start, and only skips
#126 because the guard now returns `None` for `"m4b"`.

## Current state

Louie is built, deployed and healthy — a read-only status aggregator over
Huey's request store, physical-disc events, and live Radarr/Sonarr/Lidarr/
qBittorrent/SABnzbd/Shelfarr state. See `LOUIE.md`. It is GET-only against
every upstream; that constraint is not negotiable.

Next work is in **Huey**, not Louie:
- 75 requests sit in `ambiguous` (~30% of live requests) awaiting
  clarification. Nothing retries them; they are inert.
- Replying in Discord appears to attribute the reply to the FIRST open
  ambiguous row rather than the request actually replied to. Unconfirmed,
  needs investigation before building on it.
- A numbered picker is the intended fix — post candidates, accept a numeric
  reply. Applies to every channel. Shipped for movies-tv, ebooks, and
  audiobooks.
- 66 movies-tv rows in `needs_selection` predate the picker and are inert.
  `scripts/huey/redrive_selection.py report` surveys them read-only against
  live Radarr/Sonarr; it must be run on wysearr and reviewed before anything
  posts.
- Backfill batch one prompted #5, #16 and #25 successfully, then crashed on
  #26 with a `target_key` UNIQUE violation. See *Duplicate rows for one exact
  target*. Batch two must run `collapse` first, and has not been run yet.

A candidate prompt used to be one-shot per request row: `expires_at` passing,
or startup recovery failing an unbound prompt, left a terminal
`candidate_confirmations` row that refused every later confirmation. The row
is now reset instead. One row per request stays a schema invariant —
`request_id` and `shelfarr_correlation` are both `UNIQUE` — so a second prompt
reuses the row rather than adding one.

## Rejecting a picker: the token is `n`, and it could not be `0`

A picker had no way to say "none of these". Request #113, `Tv: Brooklyn 99`,
was offered Brooklyn DA (2013), Brooklyn 11223 (2012) and Brooklyn South
(1997); any number acquires the wrong show, and the only escape was to let the
prompt expire in silence.

**`0` was not available.** `_SELECTION_ORDINAL` is `^[1-9][0-9]*$`, so
`_selection_ordinal` already returns `0` for *every* unreadable reply —
`"0"`, `"banana"`, `""`, `"0001"`, `" 1"`, overlong digits. Spending `0` on
rejection would turn every typo into a silent release unless the sentinel were
changed to `-1` and every call site re-audited, including the guard that stops
a bare token becoming a standalone request. `n`/`no`/`none` occupy a disjoint
token space and leave the ordinal contract alone. The same guard now covers
bare rejection tokens too: in the book channels `no` parses as a title and
reserves a real target key.

`reject_candidate_selection` carries a claim's full authorization — same
requester, same channel, reply ID recorded — because it is an answer, not a
delivery failure. The row returns to `needs_selection` immediately, the prompt
goes terminal so a better-informed request can prompt again, and a late
rejection after a claim is a no-op rather than an undo.

**A rejection is recorded as `rejected`, not `invalid`.** That needed a rebuild
of `candidate_confirmation_replies`, whose CHECK could not be altered in place
(`_migrate_candidate_reply_outcomes`, guarded on the stored table SQL so it is
idempotent). Folding it into `invalid` would make the audit trail say the
requester typed nonsense when they answered correctly.

**The `selection_rejected` event is what stops the loop.** A rejected row goes
back to `needs_selection`, which is exactly what the backfill re-drives — so
without a durable record, batch two offers the identical three wrong shows
again. `redrive_selection` classifies a row whose *latest* selection event is a
rejection as `rejected_by_requester` and never prompts it. Latest, not any:
a fresh prompt afterwards makes the row re-drivable once more. Confirmation
status alone cannot carry this, because a rejection and an undeliverable prompt
both land on `failed` and only the latter should be re-driven.

A rejected row does **not** block a retry. `needs_selection` sits outside the
active-target index, so retyping the identical string creates a new row with
the identical `target_key` and proceeds normally; retyping with better detail
is a different target anyway. If the retyped row is acquired, the sweep aliases
the rejected row onto it.

## Duplicate rows for one exact target

`requests_active_target_uq` is UNIQUE on `target_key` across the *active*
statuses only. `needs_selection` and `failed` are outside it, deliberately, so
those rows stay retryable. The cost: two rows naming one exact target coexist
happily while both sit stuck, and collide the instant the second is driven out.
Three `movie: coda 2021` rows crashed backfill batch one that way, and once the
first was queued the other two were stranded **permanently** — nothing retries a
`needs_selection` row on its own.

The constraint is now handled where it bites, not by the caller:

- **Entering any active status**, a row whose target another active or complete
  request already owns is aliased onto that owner rather than raising —
  `_coalesce_target_duplicate`, the ABBA coalescer generalised, sharing
  `_repoint_aliased_request` with it so the two cannot drift.
- **Reaching `queued`/`complete`/`completed`**, the owner sweeps every
  `needs_selection` row naming the same target onto itself.

The two fire at different boundaries on purpose. `processing` is not a
commitment — `fail_candidate_prompt` releases a prompt that could not be posted
straight back to `needs_selection` — so sweeping there would alias siblings onto
a row that then goes back to being stuck. Only `failed` siblings are never
swept: a failure may be unrelated to the target and stays legitimately
retryable.

Aliased requesters are told (`request_accepted`, "already tracked as request
#N"), unless they are the owner's requester. A distinct person who asked months
ago and hears nothing cannot tell the fix from the bug it replaces.

**The key is exact typed text, not the film.** `request_target_key` is
`[media_type, kind, title, author]` with only case and whitespace normalised,
and the parser does not extract years, so `coda 2021` and `coda` are different
targets, as are `passengers` and `passenger`. Only byte-identical requests
merge. Duplicate-looking rows are not necessarily duplicate targets.

`scripts/huey/redrive_selection.py` prompts at most one row per target per
batch and never prompts a row whose target is already owned. Its `collapse`
subcommand repairs rows stranded before this existed — report-first, writing
only with `--apply`. It deliberately leaves clusters with *no* owner alone:
nothing has been acquired, so there is no winner to pick, and the sweep settles
them when a real owner commits. That work is not an automatic migration in
`initialize()` on purpose — restarts run unattended, and this backlog is read
before it is written.

**A third `failed` -> active site is knowingly unguarded**: the Shelfarr
completion reconciliation (`database.py`, `SET status = 'completed' ... WHERE
id = ? AND status = 'failed'`). Coalescing there would discard a row that has a
verified artifact, and clearing its `target_key` instead is the plausible fix
but has no evidence behind it. It has never been observed to collide.

## A requested year orders the field, it never empties it

`rank_arr_candidates` used to drop every candidate whose release year differed
from the one the requester typed. TMDb disagrees with consumer sources often
enough that this made an accurate year worse than no year: `movie: cashback
2006` returned nothing while `movie: cashback` returned a three-option picker
containing the film. It also broke any title whose own name contains digits —
`1917`, `2012`, `blade runner 2049` all bailed with zero candidates.

The year is now scored, not filtered. Each candidate is scored against the
title with the year token left in and taken out, and the better fit wins, so a
candidate that matches the full form is treated as having the digits in its
name rather than being filtered against a release year no film has. A year
mismatch demotes by 0.02 per year, capped at 0.12. `select_arr_candidate`
still drops conflicting candidates before its gates, so no wrong-year film can
auto-match and nothing that auto-matched before stops.

**Known design gap: year agreement counts for nothing.** The scoring is
one-sided — a year that disagrees is penalised, a year that *agrees* earns
nothing. So a corroborating year cannot lift a weak title: `The Wildlife 1984`
matches `The Wild Life` (1984) on the year exactly, still scores 0.596, and
bails below both the 0.62 confirmation floor and the automatic gate. The
requester supplied evidence that the match is right and nothing counted it.

Fixing it means scoring agreement as evidence, which raises scores and
therefore **moves auto-accept outcomes** — titles that currently require a
prompt would start being taken automatically. That is a different class of
risk from a picker change, which by construction can only offer more, never
acquire more. It needs its own evaluation against the backlog and must not be
folded into a picker change.

A single candidate can be offered as a confirmation — "Did you mean X (year)?
Reply 1 to confirm" — but only above `ARR_AUTO_MATCH_MIN_SIMILARITY` (0.62),
the bar the automatic gate already demands of a title. The point of the higher
bar is that a lone option is only worth asking about when the identity is
strong and something *else* blocked it, such as a disputed year. If the
identity itself is weak, the prompt becomes a backdoor to accepting what the
gate refused. Only the ARR picker emits one option; Shelfarr, LazyLibrarian
and ABBA keep their own two-option rule.

## A trailing year no longer defeats author parsing

Request #288, `Kaiju: Battlefield Surgeon by Matt Dinniman 2019`, did not split
its author at all. `_looks_like_author` required every word of the candidate to
contain a letter, so `2019` rejected the whole candidate and the entire string
— `by`, author and year — became a five-word title. The requester supplied the
most information of three attempts and got the worst parse for it: title score
0.859 against the real release where the same request without the year scores
1.000.

A trailing four-digit year is now split off before the candidate is judged, and
returned as a `year` hint. **Deliberately narrow**: only `1\d{3}` or `20\d{2}`,
only at the end. `Blink 182` is still rejected — that is a name containing a
number, not a year — and `Stand by Me 1986` is still a title, because pronoun
rejection runs on the year-stripped remainder. The `year` key is present only
when one was actually split off, so no existing caller's shape changes.

**This changes `target_key` for such requests.** `by Matt Dinniman 2019` now
keys on title `kaiju: battlefield surgeon` + author `matt dinniman` rather than
one long title. A row acquired under the old parse will not coalesce with a new
request typed the same way; the result is a duplicate request, not lost data,
and it only affects requests carrying a trailing year after an author.

## Known limitation: the parser keeps the author inside the title

`parse_request` only splits an author off when the text contains ` by `.
`leaders eat last by Simon sinek` parses as title + author;
`Leaders eat Last simon sinek` parses as a five-word *title*. Everything
downstream then scores against a title that contains an author, so any release
whose name does not also repeat the author is penalised — the correct book
scores 0.401 where the same book scores 0.484 when the author is parsed out.

## One bad listing must not fail the whole ABBA search

Requests #289 and #290 ("Dream it. Do it.") came back `failed`, not
`needs_selection`. `AbbaClient.search` validated every listing inside one
comprehension, and `_search_candidate` raised `ServiceError` whenever
`sanitize_display_text` returned `None` for a post title. That pattern matches
`www.`, `https?://`, `magnet:` and `discord.gg/` — and AudioBookBay post titles
routinely carry a site tag. **One tagged listing among ten failed the entire
request and discarded the nine good releases beside it.**

The codebase already disagreed with itself here: `_selection_proposal` hits the
same sanitiser on the same data and skips the release rather than dying. That
policy just never ran, because the fatal raise was three functions upstream.

Now:

- A bracketed group *containing a link* is stripped and the listing kept —
  `[www.audiobookbay.se] Dream It Do It - Marty Sklar` is a real result, and
  losing it is a worse outcome than losing the tag. Only bracketed groups with
  a link are removed; `[M4B]`, `(Unabridged)`, `[2019]` are identity and stay.
- If the text still cannot be rendered, **the listing** is dropped, not the
  response. The unit of exclusion is the listing even when the unrenderable
  field is optional: blanking a narrator would keep a listing whose metadata is
  not what it should be.
- Every drop is logged with the candidate id and the reason, plus a
  kept/dropped tally, because a search that quietly loses candidates reads as
  "the book is not on ABB".

`sanitize_display_text` is unchanged and still decides what is safe. Stripping
only removes a tag *before* asking it; a stripped title is put through the
sanitiser exactly as before, so a title that is only a URL is still dropped.

## A failed audiobook result must carry its own cause

`_generic_audiobook_result` rewrites the requester-facing sentence, and its
`else` branch — every `failed` result — replaced the message with "An
administrator can review the saved request safely". That rewrite ran *before*
`_persist_handler_result`, so the `error` column got the same generic text: the
row promised a reviewable artifact and carried nothing to review. The only
surviving record was a log line.

The cause now travels as state, the way the decline reasons already did:
`external_status` is `acquisition_service_error` or `internal_error`, and the
specific text reaches the `error` column via `_diagnostic`. `error` is never
sent to Discord for ebooks or audiobooks — `notifications.py` returns a fixed
sentence for those media types before it reads the field — so a backend's own
words belong there and stay out of the channel.

`_persist_handler_result` prefers `_diagnostic` **only when the message has
actually lost it**. Radarr's failure message already contains its own reason,
and preferring the fragment would have trimmed a fuller sentence to a shorter
one.

## What AudioBookBay listings actually look like

Verified against `/api/search` on 2026-08-29, not modelled. Post titles are
bare `Title - Author` with no subtitle — `Leaders Eat Last - Simon Sinek` —
which is why a title-only request lands in the *ambiguous* band (>=0.82) and
never in `low_confidence`. The post block carries `Format:` and `Bitrate:` but
no `Author:`, `Narrator:`, `Year:`, or `Edition:` line, so every one of those
fields comes back null. Do not model ABB results as subtitled catalogue
titles; that assumption produced a fix that passed its tests and failed in
Discord.

Two listings for one book can therefore be identical in every field Huey can
see. `AbbaClient._indistinguishable_band` settles that case by taking the
top-ranked listing and reporting the count, rather than offering a choice
nobody has information to make. It is deliberately restricted to `ambiguous`:
in `low_confidence` the work itself is unproven, and auto-picking there is the
wrong-work risk the confidence gate exists to prevent.

`AbbaClient.PICKER_MIN_TITLE_SCORE` is 0.40 rather than ARR's 0.45 to
accommodate exactly that, and `PICKER_MIN_TITLE_RECALL` exists because the same
unparsed author lets an unrelated book by that author score 0.57. **Both
numbers are compensating for the parser, not for anything about ABBA.** Fix the
parser — recognise a trailing name without `by` — and the floor should go back
to ARR's value rather than drifting further down each time a phrasing misses.
