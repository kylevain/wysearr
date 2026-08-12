# Decisions

## Recorded decisions

- 2026-08-09: Rebuild on Debian 13 and keep the service identity
  `wyseadmin@wysearr`.
- 2026-08-11: Discord channels define media intent. Huey remains a deterministic
  intake/routing service; conversational systems may submit requests through the
  same pipeline but do not control acquisition services directly.
- 2026-08-11: SQLite is the local, service-owned state mechanism. Live SQLite
  files are backed up with SQLite's online backup API, not copied blindly.
- 2026-08-11: The combined movies/TV channel requires an explicit `movie:` or
  `tv:` prefix. Channel identity alone cannot choose between Radarr and Sonarr.
- 2026-08-11: Readarr is intentionally unsupported. It was never deployed in
  this repository, and no compatible production image/configuration was found.
  Ebook acquisition is Huey -> Prowlarr -> qBittorrent -> BookBot -> Kavita's
  `/media/ebooks/Books` library.
- 2026-08-11: Media without a suitable ARR manager uses conservative direct
  Prowlarr matching. Ambiguous or low-confidence results require a more specific
  request; automation must not choose arbitrarily.
- 2026-08-11: Direct-media acquisition requires a payload-derived BitTorrent v1
  identity. Pure v2 and hybrid payloads are declined until end-to-end v2
  correlation is implemented; indexer-supplied hashes are never trusted as the
  submitted job identity.
- 2026-08-11: Successful imports are marked by changing the qBittorrent category
  to `<type>-imported`. Only those jobs are eligible for deletion after 14 days.
  Acquisition/import failures retain the torrent and payload for diagnosis.
- 2026-08-11: qBittorrent downloads remain on the local SSD. Permanent library
  content is copied to the DAS; qBittorrent never receives a `/media` mount.
- 2026-08-11: Image digests are pinned. Upgrades are deliberate changes followed
  by API, database, health, and recreation validation.
- 2026-08-11: The service UIs are for the trusted home LAN only. They must not be
  port-forwarded or otherwise published to the Internet.

Huey, Dewey, and Louie follow the established Duck-nephew naming scheme. Huey is
Discord intake; Dewey is the conversational library interface; Louie remains
reserved for future library automation.
