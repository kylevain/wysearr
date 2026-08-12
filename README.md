# WyseARR

WyseARR is the production media-acquisition node on `wysearr`. It runs the ARR
applications, qBittorrent, subtitle automation, Huey's Discord request intake,
and BookBot's non-ARR importer. Downloads stay on the local SSD; finished
library files are copied to the Pi-SSD/DAS mounted at `/mnt/media`.

## Request media

Post one request per message in the matching Discord request channel:

| Channel | Request syntax | Workflow |
| --- | --- | --- |
| movies/tv | `movie: The Thing 1982` or `tv: Slow Horses 2022` | Radarr or Sonarr |
| ebooks | `The Left Hand of Darkness by Ursula K. Le Guin` | Prowlarr, qBittorrent, BookBot |
| audiobooks | `Piranesi by Susanna Clarke` | Prowlarr, qBittorrent, BookBot |
| manga/comics | `Saga Volume 1` | Prowlarr, qBittorrent, BookBot |
| roms | `Game Title platform` | Prowlarr, qBittorrent, BookBot |
| sheet-music | `Work Title composer instrument` | Prowlarr, qBittorrent, BookBot |

Huey replies with a request number and persists every state change. If an exact,
safe match cannot be selected, it asks for a more specific title instead of
guessing. A terminal import success or failure is delivered best-effort to the
original request and the request-status channel; one successful route marks the
notification delivered so transient failure cannot create an endless retry loop.

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

Common commands:

```bash
cd /home/wyseadmin/homelab
docker compose ps
docker compose logs --tail=100 huey bookbot
python3 scripts/validate.py
python3 scripts/backup.py
```

Web UIs are available on the trusted home LAN at `http://192.168.4.86` on the
ports documented in [docs/services.md](docs/services.md). Do not publish these
ports to the Internet. Runtime credentials and databases are deliberately kept
out of Git in `.env`, `config/`, and `state/`.

See [docs/architecture.md](docs/architecture.md) for the data flow and
[docs/recovery.md](docs/recovery.md) for rollback and restoration.
