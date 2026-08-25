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
