# WyseARR

WyseARR is the production media-acquisition node on `wysearr`. It runs the ARR
applications, qBittorrent, subtitle automation, Huey's Discord request intake,
Shelfarr's feature-gated book evaluator, and BookBot's preserved non-ARR
importer. Downloads stay on the local SSD; finished
library files are copied to the Pi-SSD/DAS mounted at `/mnt/media`.

## Request media

Post one request per message in the matching Discord request channel:

| Channel | Request syntax | Workflow |
| --- | --- | --- |
| `#movies-tv` | `movie: The Thing 1982` or `tv: Slow Horses 2022` | Radarr or Sonarr |
| `#ebooks` | `The Left Hand of Darkness by Ursula K. Le Guin` | Shelfarr while evaluation is enabled; otherwise preserved BookBot path |
| `#audiobooks` | `Piranesi by Susanna Clarke` | Shelfarr while evaluation is enabled; otherwise preserved BookBot path |
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
integration is authorized, scan the matching Plex library manually. For an
evaluation ebook/audiobook, `complete` means Shelfarr published the item to its
configured DAS path. For BookBot-owned direct media, it means BookBot safely
copied the validated payload. Neither state confirms that Kavita,
Audiobookshelf, or RomM has indexed it.

`#automation-admin` remains outside the lifecycle route set. Bazarr does not post
routine subtitle activity to Discord; any future Bazarr runtime health event must
enter Huey's shared routing path and may use only `#system-health`.

Music is managed through Lidarr's Web UI. Whisparr is likewise operated through
its Web UI because neither has a configured Discord request channel.

## Operate the stack

Run the idempotent production deployment from any working directory:

```bash
/home/wyseadmin/homelab/deploy.sh
```

The deployment validates the DAS mount, creates the complete path layout, makes
a SQLite-safe runtime checkpoint, pulls pinned images, builds Huey and BookBot,
repairs known compatible migrations, bootstraps service integrations, waits for
health, and runs the production validator.

qBittorrent is fail-closed behind PIA through Gluetun; its LAN address and every
internal `http://qbittorrent:8080` integration remain unchanged. The architecture,
kill-switch proof, port-forward behavior, and recovery procedure are in
[docs/qbittorrent-vpn.md](docs/qbittorrent-vpn.md).

Common commands:

```bash
cd /home/wyseadmin/homelab
docker compose ps
docker compose logs --tail=100 huey bookbot
python3 scripts/validate.py
python3 scripts/validate_qbittorrent_vpn.py
python3 scripts/backup.py
```

Web UIs are available on the trusted home LAN at `http://192.168.4.86` on the
ports documented in [docs/services.md](docs/services.md). Do not publish these
ports to the Internet. Runtime credentials and databases are deliberately kept
out of Git in `.env`, `config/`, and `state/`.

See [docs/architecture.md](docs/architecture.md) for the data flow and
[docs/recovery.md](docs/recovery.md) for rollback and restoration. The controlled
Shelfarr outcome and feature-flag rollback are in
[docs/shelfarr-evaluation.md](docs/shelfarr-evaluation.md).
