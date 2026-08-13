# Architecture

## Host and storage boundary

`wysearr` is the acquisition and automation node. qBittorrent payloads live on
its local SSD under `/home/wyseadmin/homelab/state/torrents`. Permanent media
lives on the Pi-SSD/DAS CIFS share mounted at `/mnt/media`.

qBittorrent intentionally has no DAS mount. ARR services, BookBot, and the
feature-gated Shelfarr evaluator can read local downloads and write their DAS
destinations. This keeps active torrent I/O and disposable payloads off
permanent storage.

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

## Discord and direct-media flow

```text
Discord request channel
        v
Huey: parse -> deduplicate -> persist -> deterministic handler
        |                |                         |
        | movies/tv      | ebooks/audiobooks       | comics/ROMs/sheet music
        v                v                         v
Sonarr or Radarr   Shelfarr API             Prowlarr + qBittorrent
        |          direct/SAB/qBittorrent           |
        |                |                       BookBot
        |                v                          |
        +--------> confirmed DAS import <-----------+
                         |
                Huey lifecycle router
```

The Shelfarr branch exists only while `SHELFARR_ENABLED=true`. Shelfarr owns
book finalization directly; BookBot does not process its isolated `shelfarr`
download category. When the flag is false, ebook/audiobook handlers return to
the preserved Prowlarr/qBittorrent/BookBot branch. Movies and TV never depend on
Shelfarr.

Huey stores request and event state in `state/huey/huey.db`. Discord
`message_id` plus durable delivery aliases make gateway redelivery idempotent.
A transactionally reserved target key coalesces distinct Discord messages only
when media type, movie/TV kind, case/space-normalized title, and normalized
author match an active or completed request exactly. Punctuation, accents, year,
edition, platform, and format remain significant; failed and `needs_selection`
requests remain retryable.
Historical active/completed requests receive the same key during migration but
are never replayed or silently merged. Titles are never silently guessed. When
Shelfarr owns an ebook/audiobook request and metadata has two or three close,
safe matches, Huey reserves the exact target in `awaiting_selection` and replies
with at most three numbered candidates. Only the original requester may reply
directly to that persisted Huey prompt, in the same request channel, with one
strict whole-number choice. The default confirmation lifetime is 15 minutes
(`HUEY_SELECTION_TTL_SECONDS=900`); expiry releases the target and stages one
request-status rejection. Legacy parse failures, no-result/low-confidence
matches, and every non-Shelfarr `needs_selection` result remain terminal and are
not resumable.

Candidate confirmation selects metadata, not an acquisition release. Huey
persists a bounded Shelfarr search snapshot, freshly searches and verifies the
same fingerprint after confirmation, then creates the Shelfarr request with the
original Huey request ID and `huey:<id>` correlation. It does not call
Shelfarr's `/grab` endpoint. No accepted/download lifecycle event is emitted
until that request creation succeeds. Movies/TV and every direct-media handler
remain on their existing paths.

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

BookBot-owned qBittorrent jobs carry a `huey-<request-id>` tag so BookBot can reconcile
the terminal import with the original request. Huey records delivery per logical
event and destination. Discord itself does not provide an atomic exactly-once
send transaction, so a route is marked delivered only after its send succeeds.

The six configured intake channels are `#movies-tv`, `#ebooks`, `#audiobooks`,
`#manga-comics`, `#roms`, and `#sheet-music`. Huey replies to the originating
message only for the request acknowledgement. It routes later events by purpose:

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

An ARR terminal completion currently means Sonarr or Radarr reports imported
media on the DAS; Huey does not yet trigger or confirm a Plex scan, so the
matching Plex library must be scanned manually until that integration is
authorized. A Shelfarr book completion proves Shelfarr's final DAS publication;
a BookBot completion proves its validated atomic copy. None of those boundaries
proves that a downstream playback or catalog application has indexed the item.

BookBot-owned direct acquisition accepts only payloads whose BitTorrent v1 identity can be
derived and cross-checked from the magnet or exact torrent metadata. Pure v2
and hybrid payloads are declined instead of trusting unverified indexer
metadata; ARR-managed acquisition is unaffected by this direct-media boundary.

BookBot accepts only allow-listed file types, rejects symlinks and unsafe paths,
normalizes names, copies through a temporary file with an atomic final rename,
and records completed imports in its SQLite ledger. Existing conflicting files
are preserved in `/media/duplicates/<type>` rather than overwritten.

Direct destinations are:

| Request type | DAS destination |
| --- | --- |
| ebooks | `/media/ebooks/Books` (Shelfarr during evaluation; otherwise BookBot) |
| audiobooks | `/media/audiobooks` (Shelfarr during evaluation; otherwise BookBot) |
| manga/comics | `/media/ebooks/Comics` |
| roms | `/media/roms` |
| sheet-music | `/media/sheetmusic` |

## Lifecycle and safety

Every ARR/BookBot-owned qBittorrent media category has a base and `-imported`
category. The isolated `shelfarr` category is owned and finalized by Shelfarr
and deliberately has no BookBot `-imported` peer. Payloads
remain in the base category when acquisition or import fails. Only a successful
ARR/BookBot import moves a job to `-imported`; BookBot may delete that job and
its local payload after the configured 14-day retention interval. This makes a
failed import fail safe and preserves seeding after a successful import.

Shelfarr uses a private local nested staging mount for Project Gutenberg ebook
downloads before publishing to the CIFS DAS. Direct LibriVox audiobooks are
disabled because Shelfarr requires atomic same-filesystem directory publication.
The enabled-Usenet source preference is direct, then Usenet, then torrent;
when disabled it is direct, then torrent and Shelfarr's SAB client is off.
Usenet is fail-closed behind `WYSEARR_USENET_ENABLED`: the repository manages one
TLS-verified SABnzbd provider and one Generic Newznab indexer. Prowlarr uses
separate `shelfarr` and `wysearr-arr` tags so the book-only Newznab cannot sync
to Sonarr, Radarr, Lidarr, or Whisparr while existing torrent indexers remain
available to those applications and to Shelfarr fallback. No NNTP provider or
book Newznab credentials were available for the initial or 2026-08-12 Usenet
preflight, so this branch remains disabled and unproven end to end.

All containers use bind-mounted persistence, `restart: unless-stopped`, bounded
JSON logs, and Docker healthchecks. Official images and Python build bases are
pinned by digest. Huey and BookBot run as UID/GID 1000 rather than root.

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

`scripts/backup.py` makes an integrity-checked copy of each SQLite database and
writes a hash manifest. It also captures a bounded, file-stable snapshot of
qBittorrent's `BT_backup` resume metadata. That resume snapshot is only
cross-file exact when qBittorrent is stopped.

Checkpoints deliberately exclude `state/torrents` payloads and all `/mnt/media`
library content. Git plus a compatible checkpoint can recover the service
control plane and request history; the DAS library, CIFS credential, local Git
history, and any irreplaceable active payloads require independent protection.
A downgraded image must never be started against newer persisted state: restore
the matching database generation, with its owning service stopped, before
starting the older image.
