# Architecture

## Host and storage boundary

`wysearr` is the acquisition and automation node. qBittorrent payloads live on
its local SSD under `/home/wyseadmin/homelab/state/torrents`. Permanent media
lives on the Pi-SSD/DAS CIFS share mounted at `/mnt/media`.

qBittorrent intentionally has no DAS mount. ARR services and BookBot can read
local downloads and write their DAS destinations; retained Shelfarr can do so
only for ebooks. LazyLibrarian and ABBA have neither a download-payload mount
nor a DAS mount: they can submit only their exact ebook or audiobook handoff
through qBittorrent's private API. This keeps active torrent I/O and disposable
payloads off permanent storage. BookBot finalizes LazyLibrarian ebooks and ABBA
audiobooks; Shelfarr retains its separate ebook finalizer for fallback jobs.

## Managed media flow

```text
Sonarr / Radarr / Lidarr / Whisparr
        | search (indexers synced by Prowlarr)
        v
qBittorrent base category on local SSD
        | completed download
        v
ARR copies/imports into /media/<library>
        | marks torrent <category>-imported
        v
BookBot removes torrent and local payload after 14 days
```

ARR destinations are `/media/tv`, `/media/movies`, `/media/music`, and
`/media/spicy`. Bazarr connects to Sonarr and Radarr, applies the configured
English language profile, and searches enabled subtitle providers when the
profile is not already satisfied. Matching embedded English tracks count as
available subtitles, so an external `.srt` is neither required nor evidence of
success by itself. External matches remain content- and provider-dependent.
Bazarr's native Discord notifier remains disabled, and Bazarr does not emit
routine subtitle activity.

## Physical DVD/Blu-ray intake

BatFire remains the only optical-drive/ARM host and is not a DAS client. ARM
keeps MakeMKV's original audio and subtitle tracks and publishes its one
`MAINFEATURE` MKV over the existing OpenSSH transport:

```text
BatFire ARM completed MKV
  -> rsync once as feature.mkv.partial
  -> WyseARR /home/wyseadmin/homelab/state/physical-media/incoming/arm-<sha-prefix>
  -> atomic feature.mkv rename
  -> manifest.json published last
  -> Huey validates regular-file containment, one MKV, EBML header, size,
     SHA-256, and exact title/year/provider identity
  -> Radarr ManualImport (move) from /downloads/physical-media/incoming/...
  -> /media/movies/<Radarr naming> on the DAS
  -> Huey verifies the final readable, non-empty, same-size file
  -> existing library_imported -> #recent-additions route
```

The receiving host path is
`/home/wyseadmin/homelab/state/physical-media/incoming`; Radarr sees the same
bytes at `/downloads/physical-media/incoming`, and Huey sees them at
`/physical-media/incoming`. There is no BatFire -> WyseARR -> second WyseARR
staging copy and no direct write into `/mnt/media`. Radarr alone chooses final
movie naming and moves the validated source into `/media/movies`.

`trusted_library_events` owns physical-disc state independently of `requests`.
Its durable identity is `physical-disc` plus the full MKV SHA-256. The shared
`notification_deliveries` outbox accepts exactly one owner: `request_id` or
`trusted_event_id`. Partial unique indexes deduplicate event/route delivery for
both owner types. A replay therefore neither creates a Huey request nor starts a
second import or Discord message.

Missing/ambiguous identity, a rejected/failed Radarr import, a final-file proof
failure, or a crash across the uncertain Radarr POST boundary moves the event to
`manual_review`/`failed` and stages the existing `import_failed` route to
`#import-errors`. A transient Radarr outage leaves the state retryable. Plex
continues to observe the existing DAS movie library; this path adds no Plex
mount, library, or notification mechanism.

## Discord and direct-media flow

```text
Discord request channel
        v
Huey: parse -> deduplicate -> persist -> deterministic handler
        |               |                                |
        | movies/tv     | ebooks                         | audiobooks / direct media
        v               v                                v
Sonarr or Radarr   LazyLibrarian primary             ABBA or Prowlarr
                   | accepted       | safe pre-mutation   |
                   v                | miss/failure         v
            qBit category ebooks    v                  BookBot
                   |          Shelfarr secondary          |
                BookBot             |                     |
                   |         qBit category shelfarr       |
                   |                |                     |
                   |        Shelfarr finalizer            |
                   |                |                     |
                   +-------- confirmed DAS import --------+
                                    |
                        Huey lifecycle router
```

`EBOOK_ACQUISITION_BACKENDS=lazylibrarian,shelfarr` is the authoritative and
only validated production order. Huey tries the backends serially for one
durable ebook request; Shelfarr is never tried first or in parallel. After
resolving one work identity, Huey uses deterministic, read-only Prowlarr
searches restricted to ebook category `7020` before allowing the
LazyLibrarian attempt to mutate its wanted list. A supplied author is a hard
identity and release gate: all normalized author tokens must occur in the
metadata candidate's author and in the release title, in addition to the
normal title score and ebook-format/media checks. Only a metadata miss, zero
plausible `7020` ebook releases, an administratively disabled primary, or
another service failure still proven to be before the durable mutation marker
may advance the same request and selected identity to Shelfarr. Any plausible
result keeps LazyLibrarian primary. Once Huey writes that marker, a timeout,
transport failure, raw API `OK` plus missing history, or any other uncertain
outcome must reconcile or quarantine and never authorizes fallback. The
deprecated `EBOOK_ACQUISITION_OWNER=lazylibrarian` remains only as a
compatibility primary assertion and must match the first backend. Validation
rejects blank, unknown, noncanonical, duplicate, reversed, disabled, or
uncredentialed production backends. At runtime an administratively false
feature flag is a pre-mutation unavailable attempt and may advance to the next
configured backend; deployment/validation still reports that degraded policy
as not ready.

LazyLibrarian owns work discovery and provider acquisition on the primary path,
Prowlarr supplies ebook-only Torznab providers, qBittorrent uses category
`ebooks`, and BookBot alone imports that payload. Shelfarr is the secondary
backend: it uses its isolated `shelfarr` category and its own proven finalizer;
BookBot never finalizes a Shelfarr job. The backends never acquire in parallel.
The old direct ebook handler remains only as a non-production compatibility
route when `EBOOK_ACQUISITION_BACKENDS` is absent and the legacy owner explicitly
selects `direct`; it is not a cascade member.
`ABBA_ENABLED=true` independently makes the private ABBA service the sole
discovery/submission owner for new audiobook requests, while BookBot remains
the audiobook importer and retention owner. An enabled ABBA failure never
falls through to direct acquisition. After in-flight work is drained, setting
the flag to `false` restores the preserved Prowlarr/qBittorrent/BookBot route
for new audiobooks, independent of `SHELFARR_ENABLED`. The former Shelfarr
audiobook route is available only by a deliberate full code-and-state rollback,
not by feature-flag fallback. Movies and TV never depend on either feature.

Huey has no Compose startup dependency on optional ABBA, LazyLibrarian,
Shelfarr, or SABnzbd. Deployment health-gates each optional service enabled by
the active policy before reopening intake, while a later optional outage cannot
prevent unrelated request channels from starting or operating. An ebook outage
may advance only under the safe serial pre-mutation rules above; an enabled but
unavailable ABBA service does not silently become the direct audiobook route.

Huey stores request and event state in `state/huey/huey.db`. Discord
`message_id` plus durable delivery aliases make gateway redelivery idempotent.
A transactionally reserved target key coalesces distinct Discord messages only
when media type, movie/TV kind, case/space-normalized title, and normalized
author match an active or completed request exactly. Punctuation, accents, year,
edition, platform, and format remain significant; failed and `needs_selection`
requests remain retryable.

Each ebook request has one row in `ebook_cascades`: immutable `policy_json`,
`current_ordinal`, shared work identity/fingerprint, request-wide
`mutation_backend`, and the proven `final_backend`/`finalizer`. Ordered rows in
`ebook_backend_attempts` retain each backend's status, local identity, external
correlation, mutation timestamps, and outcome. `ebook_backend_reservations`
uniquely binds backend-local primary identities and aliases across active or
successful requests and active or fulfilled unavailable-retry owners. The active
identity and resume indexes make restart recovery select the current attempt
without repeating an earlier mutation.

When a canonically resolved ebook exhausts the configured cascade in safe
pre-mutation misses, and at least one exact release probe conclusively reports
that no usable release exists, the same transaction that records the normal
one-time failure also creates one `unavailable_retries` row. Operational errors,
ambiguous or stale metadata, uncertain submission state, downloader failures,
and import failures do not create a new retry owner. The row retains the
original Huey request and Discord correlation, the sanitized selected-work
snapshot, its provider-independent identity key, and retry/final-import state in
`state/huey/huey.db`; no second queue service or database exists.

The deterministic eligibility dates are 7, 37, 67, 97, 127, 157, and 187 days
after the original unavailability result, assuming each preceding retry ends in
another safe miss. Huey's existing background reconciliation loop claims due
rows. The seventh failed retry expires the row, so the bounded lifetime is about
six months. A retry reuses the saved work identity and the normal
`lazylibrarian,shelfarr` cascade. It does not ask for metadata again and will not
substitute a different work when the saved identity can no longer be resolved.
Claiming a retry resets transient attempt timing, status, and external correlation
only; persisted backend identities and every provider alias remain reserved for
that logical request. The request can therefore revalidate and reuse its own
provider ID, while changed title/year metadata cannot claim the same ID as a new
owner. Reservations persist through `fulfilled` final proof and are released only
when the retry expires.

The retry states are `queued`, `retrying`, `awaiting_import`, `blocked`,
`fulfilled`, and `expired`. Search-only misses return to `queued`; a proven
handoff or mutation-uncertain correlation moves to `awaiting_import`. A
definitive failure after a mutation or downloader handoff becomes `blocked`,
retains its backend reservation and ownership, and is never acquired again
automatically. The only allowed later edge is proof-only completion for that
same persisted handoff: BookBot's exact Huey tag/hash, ledger-validated DAS copy
from the exact `ebooks` processing category into the Books library for the
LazyLibrarian path, or Shelfarr's exact retained remote request reporting
completed final DAS publication. A drifted `manga-comics` or other processing
category cannot complete an ebook owner. The atomic final-proof trigger repairs the
failed attempt/cascade and moves the row to `fulfilled`; all other blocked
observations remain unchanged and silent. Blocked Shelfarr proof polling uses
the durable `last_proof_check_at` cursor and atomically advances each claimed
least-recently-checked batch before remote reads, so a leading set of unfinished
owners cannot starve later proof across reconciliation cycles or restarts.
Indexer results, submission
acceptance, downloader acceptance, and completed payload bytes are all
nonterminal.

The active retry identity index includes `blocked`, so a live request, a due
retry, and another Discord delivery cannot race into duplicate acquisition. A
new live request for the same canonical work resolves to the existing owner and
may make a still-queued pre-mutation retry immediately due; it cannot bypass an
active, awaiting-import, or blocked owner. Background attempts stage no
acknowledgement, selection, queue, download, miss, error, or expiry notification.
After verified final import, Huey's existing unique outbox stages each logical
request-completion and recent-addition event once. This policy currently covers
ebooks only; audiobook, movie/TV, comics, ROM, and sheet-music failure behavior
is unchanged.

Historical active/completed requests receive the same key during migration but
are never replayed or silently merged. Titles are never silently guessed. When
an ebook request or ABBA audiobook request has two or three close safe metadata
matches, Huey reserves the exact target in
`awaiting_selection` and replies with at most three numbered candidates. Only
the original requester may use Discord's Reply action on that persisted Huey
prompt, in the same request channel, with one strict whole-number choice. The
resulting Discord message must reference the exact persisted prompt ID; a nearby
standalone number is deliberately rejected. The default confirmation lifetime is 15 minutes
(`HUEY_SELECTION_TTL_SECONDS=900`); expiry releases the target and stages one
request-status rejection. Legacy parse failures, no-result/low-confidence
matches, and every non-book `needs_selection` result remain terminal and are
not resumable.

For ebooks, candidate confirmation selects one catalog work rather than an
acquisition release. Huey applies the persisted source, work identifier,
title/author metadata, and fingerprint to every backend attempt; it never asks
the user to resolve the same identity twice. For LazyLibrarian it persists the
exact `BookID`, adds/verifies that work, queues an ebook-only search, and treats the API's raw
`OK` only as command acceptance—not as a successful grab. It then binds the
exact active `getHistory` row to qBittorrent's live hash, save path
`/downloads/ebooks`, and the `ebooks` handoff (or an already BookBot-transitioned
`ebooks-imported` job). An imported-category race remains nonterminal until
BookBot revalidates its ledger/destination and reports completion. For
audiobooks, Huey uses ABBA's bounded JSON
search result, freshly verifies the selected result before calling `/api/grab`,
persists correlation, and then follows `/api/status/<hash>` through the exact
qBittorrent job. ABBA's `/health` checks only local database and qBittorrent
readiness; health validation never searches AudioBookBay. No accepted/download
lifecycle event is emitted until the selected backend has accepted the request.

The private ABBA contract is `POST /api/search`, `POST /api/grab`, and
`GET /api/status/<40-character-hash>` (with correlation lookup available at
`GET /api/status?correlation_id=...`). Huey sends a title plus optional author,
keeps the returned opaque candidate ID/fingerprint bound to its durable Discord
confirmation, and never consumes ABBA's HTML UI. ABBA persists only sanitized
candidate/correlation state and never returns magnet links or credentials to
Discord.

ABBA and Huey enforce acquisition ownership independently in their own SQLite
ledgers. Each durably reserves both the opaque ABBA candidate ID and the resolved
lowercase v1 hash, so loss, delay, or restart on one side cannot authorize a
second submission on the other. A second request for the same candidate, or a
different candidate that resolves to the same hash, becomes an inert alias of
the one canonical request: its original Discord message and any claimed Reply
selection resolve to that owner, pending duplicate lifecycle delivery is
discarded, and no second grab is submitted. One candidate resolving to a
different hash is not a safe alias and is quarantined as an identity conflict.
Self-aliases, missing owners, alias chains/cycles, and mismatched candidate/hash
owners fail closed.

Restart migration elects the same deterministic canonical owner in both
ledgers and preserves post-mutation failed/uncertain ownership. A failure proven
to precede mutation may release its adapter reservation and remain retryable;
it is never backfilled into a permanent hash owner merely because an old row
contains a resolved hash. Recovery uses only the exact persisted candidate and
hash. It may attach to a proven canonical owner, but it never replays a
different candidate or turns a candidate/hash conflict into an alias.

An existing ARR entity is read before mutation. Imported media returns an
already-imported result; a monitored item starts no duplicate search; an
unmonitored item is monitored and searched. Direct acquisition derives the
payload's exact v1 hash and asks qBittorrent whether that hash already exists
before adding it. An expected base/imported category is correlated to the
request without a second add; an imported category still requires BookBot's
safe ledger verification before completion. A different or empty category
fails closed for administrator review.

When ambiguity metadata is useful, Huey renders no more than three candidates
from the actual close-score band. Only a sanitized release title and derived
format/size hints are shown; provider IDs, URLs, hashes, and credentials are
never included. Poor or low-confidence metadata retains the generic refinement
response.

Direct and ABBA BookBot-owned qBittorrent jobs use exact lowercase
`huey-<positive-decimal-request-id>` correlation tags in qBittorrent's
comma-delimited tag field. A Huey token is valid only as a complete comma-bounded
token; uppercase, `huey:`, whitespace-delimited, partial, zero, negative, and
out-of-SQLite-range lookalikes are not accepted. Other ordinary qBittorrent tags
may coexist. If more than one valid Huey tag is present on an ABBA job, BookBot
continues only when every tag and stored hash resolves to the same root canonical
ABBA audiobook owner; an unknown, mixed-media, chained, or conflicting owner
fails closed before copy or lifecycle mutation.
LazyLibrarian-created ebook jobs instead bind their exact qBittorrent hash into
Huey's durable external ID. Only after that database ownership succeeds does Huey
attach the same exact `huey-<request-id>` tag; reconciliation restores the tag
only after exact hash/category/save-path validation. BookBot requires both the
tag and case-insensitive persisted hash before mapping the terminal import. Huey
records delivery per logical
event and destination. Discord itself does not provide an atomic exactly-once
send transaction, so a route is marked delivered only after its send succeeds.

The six configured intake channels are `#movies-tv`, `#ebooks`, `#audiobooks`,
`#manga-comics`, `#roms`, and `#sheet-music`. Huey replies to the originating
message only for the request acknowledgement. It routes later events by purpose:

Human-authored messages in those channels remain normal intake. Dewey may use
the same `DISCORD_BOT_TOKEN` through Discord's create-message REST endpoint,
but Huey accepts a bot-authored message only when Discord reports Huey's own
live bot user ID, no webhook ID, and an exact string message nonce matching
`dewey:v1:[A-Za-z0-9_-]{16}`. Every other bot and every webhook remains ignored.
The nonce is transport metadata, never part of request content. Dewey must set
`enforce_nonce=true` and reuse the same stable 16-character URL-safe suffix for
every retry of one logical submission. Discord's returned message ID remains
Huey's durable delivery key. Ordinary Huey sends and replies use numeric nonces,
so they cannot recurse into request intake. The shared bot token must never be
committed or logged; the nonce must never be placed in request content or shown
to users.

| Event class | Discord destination |
| --- | --- |
| Accepted, rejected, completed, or failed request | `#request-status` |
| Queued acquisition, active download, or download lifecycle | `#download-queue` |
| Newly imported DAS library item | `#recent-additions` |
| Import failure or manual intervention required | `#import-errors` |
| Service or runtime health issue | `#system-health` |

A completed request and a newly imported item are separate logical events, not
copies of one notification. `#automation-admin` is not a lifecycle route. Huey is
the sole Discord producer; native Discord delivery in Radarr, Sonarr, Lidarr, and
Bazarr stays disabled to prevent bypasses and duplicates. Lidarr music and
Whisparr adult-media requests remain Web-UI-only.

Every requester-facing acknowledgement, candidate prompt, accepted/queued/
download state, uncertainty notice, completion, rejection, and failure uses
backend-neutral language. LazyLibrarian, Shelfarr, ABBA, Prowlarr, qBittorrent,
and BookBot names are reserved for operator logs and service/runtime health
diagnostics; a backend transition never exposes a second user-visible request
or prompt.

An ARR terminal completion currently means Sonarr or Radarr reports imported
media on the DAS; Huey does not yet trigger or confirm a Plex scan, so the
matching Plex library must be scanned manually until that integration is
authorized. A LazyLibrarian ebook and an ABBA audiobook do not complete until
BookBot proves the validated atomic copy; a Shelfarr fallback completion proves
Shelfarr's final DAS publication. None of those boundaries
prove that a downstream playback or catalog application has indexed the item.

BookBot-owned direct acquisition accepts only payloads whose BitTorrent v1 identity can be
derived and cross-checked from the magnet or exact torrent metadata. Pure v2
and hybrid payloads are declined instead of trusting unverified indexer
metadata; ARR-managed acquisition is unaffected by this direct-media boundary.

BookBot accepts only allow-listed file types, rejects symlinks and unsafe paths,
normalizes names, copies through a temporary file with an atomic final rename,
and records completed imports in its SQLite ledger. Existing conflicting files
are preserved in `/media/duplicates/<type>` rather than overwritten.

For a fresh ABBA audiobook import, BookBot trusts Huey's request metadata only
when every exact Huey tag resolves to one root ABBA audiobook owner whose stored
hash exactly matches the job's v1 hash. A normal job has one tag; multiple tags
are accepted only for safe aliases that converge on that same owner. No valid
Huey correlation leaves the existing source-derived direct-import behavior
unchanged, while malformed, unknown, mixed, chained, or conflicting correlation
fails closed before copying. A trusted match makes the sanitized request title
the single directory component directly below `/media/audiobooks`, regardless
of the release payload's name. Slashes, traversal-like text, reserved
characters, whitespace, and length remain subject to BookBot's normal component
sanitizer.

During that same atomic import, BookBot stages an XML-escaped `metadata.opf` with
the trusted title, BitTorrent identifier, and optional request author. It never
overwrites metadata supplied by the release: if any source `.opf` or `.nfo`
exists, that sidecar is copied byte-for-byte and no generated OPF is added. The
production Audiobookshelf library gives `folderStructure` first metadata
precedence, so the trusted one-level folder is the authoritative title boundary;
the OPF supplies compatible structured metadata without weakening it. Direct
fallback audiobooks and every non-audiobook category retain their existing
source-derived layout and sidecar behavior. Reconciliation of an already imported
item never renames its directory or retrofits metadata.

Direct destinations are:

| Request type | DAS destination |
| --- | --- |
| ebooks | `/media/ebooks/Books` (BookBot for the LazyLibrarian primary path; Shelfarr's own finalizer for fallback) |
| audiobooks | `/media/audiobooks/<sanitized-request-title>` for a newly correlated ABBA import; otherwise BookBot's source-derived one-level folder |
| manga/comics | `/media/ebooks/Comics` |
| roms | `/media/roms` |
| sheet-music | `/media/sheetmusic` |

## Lifecycle and safety

Every ARR/BookBot-owned qBittorrent media category has a base and `-imported`
category. LazyLibrarian may submit only category `ebooks` with save path
`/downloads/ebooks`; ABBA may submit only category `audiobooks` with save path
`/downloads/audiobooks`. BookBot changes a successfully imported job to the
matching `-imported` category, which shares that save path. The isolated `shelfarr`
category is ebook-only, owned and finalized by Shelfarr, and deliberately has no
BookBot `-imported` peer. Payloads
remain in the base category when acquisition or import fails. Only a successful
ARR/BookBot import moves a job to `-imported`; BookBot may delete that job and
its local payload after the configured 14-day retention interval. This makes a
failed import fail safe and preserves seeding after a successful import.

Shelfarr uses a private local nested staging mount for Project Gutenberg ebook
downloads before publishing to the CIFS DAS. Its old audiobook output setting
may remain in historical state, but the container has no `/audiobooks` mount and
Huey never sends it new audiobook requests.
The enabled-Usenet source preference is direct, then Usenet, then torrent;
when disabled it is direct, then torrent and Shelfarr's SAB client is off.
Usenet is fail-closed behind `WYSEARR_USENET_ENABLED`: the repository manages one
TLS-verified SABnzbd provider and one Generic Newznab indexer. Prowlarr uses
separate `shelfarr` and `wysearr-arr` tags so the book-only Newznab cannot sync
to Sonarr, Radarr, Lidarr, or Whisparr while existing torrent indexers remain
available to those applications and to Shelfarr fallback. No NNTP provider or
book Newznab credentials were available for the initial or 2026-08-12 Usenet
preflight, so this branch remains disabled and unproven end to end.

LazyLibrarian is pinned to one LinuxServer manifest digest, binds its management
port to host loopback only, and persists only `config/lazylibrarian`. Its
postprocessor, search-on-add, scheduled searches, native final-library paths,
non-qBittorrent downloaders, built-in providers, telemetry, and automatic
updates are disabled. Prowlarr tags only enabled torrent indexers whose
advertised capabilities explicitly include ebook category `7020` and whose
retained indexer status has no failure; its LazyLibrarian application full-syncs
the one-element category list `[7020]`. LazyLibrarian receives exactly those
tagged indexers as Torznab providers. Each active provider is converged to
`BOOKCAT=7020`, `DLTYPES=E`, and `MANUAL=1`.

The pinned LazyLibrarian provider API refreshes capabilities while applying a
change and may repopulate canonical `AUDIOCAT`, `MAGCAT`, or `COMICCAT` metadata.
Those fields are deliberately dormant: with `DLTYPES=E`, the pinned search
dispatcher rejects audiobook, magazine, and comic work before consulting their
categories. `MANUAL=1` prevents LazyLibrarian's unrelated background provider
refreshes. Prowlarr's scheduled six-hour full sync may refresh capability
metadata, but cannot change `DLTYPES` or `MANUAL`; convergence and validation
require `BOOKCAT` to return to exactly `7020`. Usenet, built-in, failed,
non-`7020`, and rival-provider lanes remain disabled or untagged.

All containers use bind-mounted persistence, `restart: unless-stopped`, bounded
JSON logs, and Docker healthchecks. Official images and Python build bases are
pinned by digest. Huey, BookBot, and ABBA run as UID/GID 1000 rather than root.
ABBA has a read-only root filesystem, drops every Linux capability, applies
`no-new-privileges`, uses a bounded `/tmp` tmpfs, persists only
`config/abba/abba.db`, and publishes no host port.

The CIFS share is mounted by the host with `_netdev`, `nofail`, and systemd
automount semantics. Deployment refuses to start unless `/mnt/media` is the
actual writable mount, avoiding writes into an unmounted local directory.

## Persistence and recovery boundary

Git stores code, Compose, documentation, and repeatable bootstrap logic. Secrets
and live service state are not committed:

- `.env` contains local credentials and API keys.
- `config/<service>` contains service databases/configuration.
- `state/huey` contains request state.
- `backups/<timestamp>` and matching deployment checkpoint pairs contain private
  runtime rollback metadata.

`scripts/backup.py` makes an integrity-checked copy of each SQLite database,
including ABBA's correlation ledger and LazyLibrarian's database discovered
under their private config trees, and
writes a hash manifest. It also captures a bounded, file-stable snapshot of
qBittorrent's `BT_backup` resume metadata. That resume snapshot is only
cross-file exact when qBittorrent is stopped.

The manifest records the exact Git HEAD and `git_dirty` provenance. A checkpoint
with `git_dirty=true` is useful state evidence but cannot identify an exact code
generation. The release rollback point must be created after the acquisition
commit from a clean worktree, verified, and show that commit as `git_head` with
`git_dirty=false`; a dirty `post-deploy-*` name does not satisfy this rule.

Checkpoints deliberately exclude `state/torrents/**` payloads,
`state/shelfarr-staging/**` direct-download staging, all `/mnt/media/**` library
content, and service `logs.db` diagnostic databases. Git plus a compatible
checkpoint can recover the service control plane and request history; the DAS
library, CIFS credential, local Git history, and any irreplaceable active
payloads require independent protection.
A downgraded image must never be started against newer persisted state: restore
the matching database generation, with its owning service stopped, before
starting the older image.
