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

## Request Architecture

Discord is the human request interface.

Media-specific Discord channels define request intent. The request listener does not infer media type from message content.

Examples:

- movies/tv → movie and television requests
- ebooks → ebook requests
- audiobooks → audiobook requests
- manga/comics → comic requests
- roms → ROM requests
- sheet music → sheet music requests
- future channels → future media handlers

The selected channel determines which deterministic handler processes the request.

## Request State

SQLite is an approved local service-state pathway.

Request state may be stored locally for:

- request tracking
- approval state
- processing history
- recovery
- auditability

Databases remain service-owned and are not centralized by default.

## Pilot Integration

Pilot acts as an intent and request generation layer.

Pilot may create structured requests, but does not directly control acquisition services.

Flow:

Pilot → structured request → Discord/request pipeline → deterministic handlers → ARR/library services
