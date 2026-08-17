# WyseARR

WyseARR is the production media-acquisition node on `wysearr`. It runs the ARR
applications, qBittorrent, subtitle automation, Huey's Discord request intake,
LazyLibrarian's private ebook catalog/acquisition service, the serial Shelfarr
ebook fallback, the private ABBA audiobook adapter, and BookBot's non-ARR
importer. Downloads stay on the local SSD; finished
library files are copied to the Pi-SSD/DAS mounted at `/mnt/media`.

## Request media

Post one request per message in the matching Discord request channel:

| Channel | Request syntax | Workflow |
| --- | --- | --- |
| `#movies-tv` | `movie: The Thing 1982` or `tv: Slow Horses 2022` | Radarr or Sonarr |
| `#ebooks` | `The Left Hand of Darkness by Ursula K. Le Guin` | Huey's serial `lazylibrarian,shelfarr` cascade; the acquisition backend is invisible to the requester |
| `#audiobooks` | `Piranesi by Susanna Clarke` | ABBA is the sole acquisition owner when `ABBA_ENABLED=true`, followed by qBittorrent and BookBot; `false` restores the preserved direct Prowlarr/qBittorrent/BookBot route for new requests |
| `#manga-comics` | `Saga Volume 1` | Prowlarr, qBittorrent, BookBot |
| `#roms` | `Game Title platform` | Prowlarr, qBittorrent, BookBot |
| `#sheet-music` | `Work Title composer instrument` | Prowlarr, qBittorrent, BookBot |

Huey acknowledges a request by replying to its original Discord message with a
request number, and it persists every state change. No unthreaded acknowledgement
is added to an intake channel. If an exact, safe match cannot be selected, Huey
asks for a more specific title instead of guessing. When close direct-media
matches have useful metadata, that response shows at most three sanitized release
titles with safe format/size distinctions; it never includes download URLs,
torrent identities, credentials, or provider IDs.

When Huey shows numbered choices, use Discord's **Reply** action on that exact
Huey message and verify the composer shows that it is replying to Huey before
sending one listed number. A standalone `1`, `2`, or `3` has no Discord message
reference, so Huey rejects it without selecting or acquiring anything.
For ebooks, that one selected work identity is reused by the entire cascade;
Huey never asks the user to select a work separately for each backend.
All requester-facing lifecycle messages stay backend-neutral, including
selection, queue, progress, uncertainty, completion, and failure. A safe ebook
miss continues as “still searching,” and Huey reports one useful not-found
result only after every configured backend is exhausted. Service names remain
available only in operator logs and `#system-health` diagnostics.

Separate messages for the same exact active or completed target reuse its
canonical request number. The identity normalizes case and whitespace but keeps
punctuation, accents, media kind, author, year, edition, platform, and format
distinct; failed or selection-needed requests remain retryable. Huey also checks
existing ARR state and exact qBittorrent hashes before starting work, and fails
closed when an existing torrent belongs to a different media category.

Huey routes Discord events by purpose:

| Event | Destination |
| --- | --- |
| Request acknowledgement | Reply to the original request message only |
| Accepted, rejected, completed, or failed request | `#request-status` |
| Queued acquisition, active download, or download lifecycle event | `#download-queue` |
| Newly imported DAS library item | `#recent-additions` |
| Import failure or manual intervention required | `#import-errors` |
| Service or runtime health issue | `#system-health` |

A request completion and a new library addition are distinct events with distinct
messages; Huey does not mirror one lifecycle message into several channels. Huey
is the sole Discord notification producer. Native Discord connections in Radarr,
Sonarr, Lidarr, and Bazarr remain disabled so they cannot bypass this routing or
create duplicates.

For movies and TV, `complete` currently means Sonarr or Radarr confirms that it
imported a media file to the DAS. Huey does not yet trigger or confirm a Plex
scan, so this does not claim that the title is visible in Plex; until the Plex
integration is authorized, scan the matching Plex library manually. For a
LazyLibrarian ebook, `complete` means BookBot safely copied the exact correlated
qBittorrent payload; for a Shelfarr fallback ebook, it means Shelfarr published
the item through its isolated finalizer to the configured DAS path. For an ABBA-acquired
audiobook or other BookBot-owned direct media, it likewise means BookBot safely
copied the validated payload. A fresh, exactly correlated
ABBA audiobook uses the sanitized Huey request title as its one-level library
folder and, when the source has no OPF/NFO, receives a generated `metadata.opf`.
Neither state confirms that Kavita, Audiobookshelf, or RomM has indexed it.

`#automation-admin` remains outside the lifecycle route set. Bazarr does not post
routine subtitle activity to Discord; any future Bazarr runtime health event must
enter Huey's shared routing path and may use only `#system-health`.

Music is managed through Lidarr's Web UI. Whisparr is likewise operated through
its Web UI because neither has a configured Discord request channel.

## Operate the stack

### Physical DVD/Blu-ray delivery

WyseARR receives ARM output at
`/home/wyseadmin/homelab/state/physical-media/incoming`. Install
`scripts/deliver_physical_media_from_batfire.sh` on BatFire and invoke it only
after ARM reports a completed MakeMKV main-feature rip:

```bash
sudo -u arm /opt/arm/scripts/deliver_physical_media_from_batfire.sh \
  '/home/arm/media/completed/JOB/feature.mkv' 'Movie Title' 2024 tt1234567
```

Use ARM/OMDb's exact title, year, and IMDb ID when present. Omitting or guessing
identity does not import: Huey quarantines it to `#import-errors`. Configure an
SSH key for BatFire's `arm` user to `wyseadmin@192.168.4.86`; the exact network
target is
`wyseadmin@192.168.4.86:/home/wyseadmin/homelab/state/physical-media/incoming/arm-<sha-prefix>`.
The helper resumes an interrupted rsync, renames the MKV atomically, and sends
the tiny manifest last. It does not mount the DAS and does not transcode.

ARM installations differ in how completed-job extensions are registered. On
BatFire, attach the command above to its already-installed successful-job hook;
do not use a generic directory watcher because it cannot prove ARM job success.
The BatFire host was not reachable by name from WyseARR during implementation,
so the exact hook filename remains a live-host configuration step.

Run the idempotent production deployment from any working directory:

```bash
/home/wyseadmin/homelab/deploy.sh
```

The deployment validates the DAS mount, creates the complete path layout, makes
a SQLite-safe runtime checkpoint, pulls pinned images, builds ABBA, Huey, and BookBot,
repairs known compatible migrations, bootstraps service integrations, waits for
health, and runs the production validator.

qBittorrent is fail-closed behind PIA through Gluetun; its LAN address and every
internal `http://qbittorrent:8080` integration remain unchanged. The architecture,
kill-switch proof, port-forward behavior, and recovery procedure are in
[docs/qbittorrent-vpn.md](docs/qbittorrent-vpn.md).
Acquisition-only deployments preserve a correctly attached qBittorrent/Gluetun
runtime and its torrent inventory; they do not move qBittorrent back onto the
ordinary Compose network or replace its persistent config/download mounts.

Production acceptance uses controlled acquisition state-machine tests plus live,
non-acquiring checks against the genuine service transports. This exercises
selection, fallback, mutation, restart, correlation, and completion boundaries
without creating unwanted downloads. The acceptance commands and explicitly
historical 2026-08-14 rollout record are in
[docs/services.md](docs/services.md#production-acceptance).

Common commands:

```bash
cd /home/wyseadmin/homelab
docker compose ps
docker compose logs --tail=100 huey abba bookbot
python3 scripts/validate.py
python3 scripts/validate_qbittorrent_vpn.py
python3 scripts/backup.py
```

Web UIs are available on the trusted home LAN at `http://192.168.4.86` on the
ports documented in [docs/services.md](docs/services.md). Do not publish these
ports to the Internet. Runtime credentials and databases are deliberately kept
out of Git in `.env`, `config/`, and `state/`.

See [docs/architecture.md](docs/architecture.md) for the data flow and
[docs/recovery.md](docs/recovery.md) for ebook-cascade/ABBA rollback and state
restoration. Shelfarr's current serial-fallback role and explicitly historical
pilot outcome are in [docs/shelfarr-evaluation.md](docs/shelfarr-evaluation.md).
