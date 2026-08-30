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
  reply. Applies to every channel.

## Known limitation: the parser keeps the author inside the title

`parse_request` only splits an author off when the text contains ` by `.
`leaders eat last by Simon sinek` parses as title + author;
`Leaders eat Last simon sinek` parses as a five-word *title*. Everything
downstream then scores against a title that contains an author, so any release
whose name does not also repeat the author is penalised — the correct book
scores 0.401 where the same book scores 0.484 when the author is parsed out.

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
