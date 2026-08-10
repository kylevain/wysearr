# WyseARR Architecture v1

Host: wysearr (192.168.4.86)

## Storage
- Temporary torrent data: WyseARR local SSD.
- Permanent media library: `/mnt/media` from Pi-SSD/DAS.
- Torrent paths: `watch/`, `active/`, `complete/`, `processed-torrents/`.

## Lifecycle
1. qBittorrent receives torrent.
2. Payload downloads to local SSD.
3. Completed payload stays local for seeding.
4. Import/processing copies cleaned library content to DAS.
5. Original payload remains untouched while seeding.
6. Seeding policy target: 14 days.
7. Cleanup removes torrent job, local payload, `.torrent`, and related metadata after policy is satisfied.

## Services
qBittorrent, Prowlarr, Sonarr, Radarr, Lidarr, Readarr, Whisparr, Bazarr, BookBot processing, Discord integration.

## Secrets
No secrets are committed. Copy `.env.example` to `.env` locally.
