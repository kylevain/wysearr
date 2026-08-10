# Architecture

Clean Debian 13 rebuild. Planned services: Docker, ARR applications,
qBittorrent, Discord listener, processing scripts.

## WyseARR Service Stack

Planned Docker services:

- qBittorrent
- Sonarr
- Radarr
- Lidarr
- Readarr
- Prowlarr
- Processing scripts
- Discord integration

Data flow:

Request
→ ARR service
→ qBittorrent
→ Download complete
→ Processing
→ Pi-SSD library storage
→ Discord notification
