# Deployed services

Host: `wysearr` (`192.168.4.86`) on Debian 13. Docker Compose project: `wysearr`.

| Service | LAN URL | Role | Persistent data |
| --- | --- | --- | --- |
| qBittorrent 5.1.4 | `http://192.168.4.86:8080` | Local torrent client | `config/qbittorrent`, `state/torrents` |
| Prowlarr | `http://192.168.4.86:9696` | Indexers and ARR synchronization | `config/prowlarr` |
| SABnzbd 5.0.4 | host loopback `http://127.0.0.1:8085` | Evaluation Usenet client | `config/sabnzbd`, `state/torrents/incomplete/usenet`, `state/torrents/usenet` |
| Shelfarr 2026.08.09.1 | host loopback `http://127.0.0.1:5056` | Huey book acquisition and finalization | `config/shelfarr` |
| Sonarr | `http://192.168.4.86:8989` | TV acquisition/import | `config/sonarr` |
| Radarr | `http://192.168.4.86:7878` | Movie acquisition/import | `config/radarr` |
| Lidarr | `http://192.168.4.86:8686` | Music acquisition/import | `config/lidarr` |
| Bazarr | `http://192.168.4.86:6767` | Sonarr/Radarr subtitles | `config/bazarr` |
| Whisparr | `http://192.168.4.86:6969` | Adult-library acquisition/import | `config/whisparr` |
| Huey | no HTTP UI | Discord intake and request state | `state/huey` |
| BookBot | no HTTP UI | Direct-media import and retention | `config/bookbot` |

Existing UI ports are intended only for the trusted home LAN. Shelfarr and
SABnzbd are more restricted: their host ports bind only to `127.0.0.1`, and
Shelfarr is not a family request UI. qBittorrent's peer port is TCP/UDP 6881.
Inter-service calls use Compose DNS names; healthchecks use container-local
loopback. Evaluation setup, ownership, and rollback are documented in
[`shelfarr-evaluation.md`](shelfarr-evaluation.md).

The Pi-SSD/DAS source is `//192.168.4.46/Media`, mounted at `/mnt/media` through
`/etc/fstab`. Container UID/GID is 1000. qBittorrent, ARR category, API-key,
indexer, Bazarr, and provider configuration is converged by `scripts/bootstrap.py`
during every deployment. When the Shelfarr feature flag is enabled,
`scripts/bootstrap_shelfarr.py` additionally converges its scoped API identity,
clients, source policy, output paths, and notification boundary.

Useful health commands:

```bash
docker compose ps
python3 scripts/validate.py
docker compose logs --tail=100 <service>
```
