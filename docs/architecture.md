# Architecture

## Host and storage boundary

`wysearr` is the acquisition and automation node. qBittorrent payloads live on
its local SSD under `/home/wyseadmin/homelab/state/torrents`. Permanent media
lives on the Pi-SSD/DAS CIFS share mounted at `/mnt/media`.

qBittorrent intentionally has no DAS mount. ARR services and BookBot can read
local downloads and write the DAS. This keeps active torrent I/O and disposable
payloads off permanent storage.

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
English language profile, and searches enabled subtitle providers.

## Discord and direct-media flow

```text
Discord request channel
        v
Huey: parse -> deduplicate -> persist -> deterministic handler
        |                         |
        | movies/tv               | ebooks/audiobooks/comics/ROMs/sheet music
        v                         v
Sonarr or Radarr          Prowlarr search + conservative match
        |                         v
        | import probe    qBittorrent base category
        |                         v
        |                BookBot validates and atomically copies
        |                         v
        +--------------> confirmed DAS import
                                  v
                         Huey terminal notification
```

Huey stores request and event state in `state/huey/huey.db`. Discord
`message_id` is unique, so gateway redelivery cannot create a second download.
Titles are never silently guessed: ambiguous searches end in `needs_selection`.
Direct qBittorrent jobs carry a `huey-<request-id>` tag so BookBot can reconcile
the terminal import with the original request. Terminal delivery is idempotently
recorded after at least one configured Discord route succeeds; Discord itself
does not provide an atomic exactly-once send transaction.

Direct acquisition accepts only payloads whose BitTorrent v1 identity can be
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
| ebooks | `/media/ebooks/Books` |
| audiobooks | `/media/audiobooks` |
| manga/comics | `/media/ebooks/Comics` |
| roms | `/media/roms` |
| sheet-music | `/media/sheetmusic` |

## Lifecycle and safety

Every qBittorrent media category has a base and `-imported` category. Payloads
remain in the base category when acquisition or import fails. Only a successful
ARR/BookBot import moves a job to `-imported`; BookBot may delete that job and
its local payload after the configured 14-day retention interval. This makes a
failed import fail safe and preserves seeding after a successful import.

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
