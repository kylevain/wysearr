# Louie

Louie is the read-only status joiner for the `wysearr` Docker Compose project. It combines Huey's SQLite request/event store with physical-disc library events and caches the result in its own SQLite database.

## Deployment

Louie runs as an additive container in `/home/wyseadmin/homelab`, on `wysearr_default`. It uses Compose DNS names. qBittorrent is addressed through `gluetun:8080`: qBittorrent has `network_mode: service:gluetun` and no network identity of its own.

The `homelab-dewey-trusted-self-intake` and `homelab-dewey-abba-trusted-self-intake` directories are branch checkouts, not the Dewey worker and not active deployments. The `dewey` name is a branch false hit. Dewey remains in Pilot on optiframe; this service does not dispatch to Pilot.

## Data model

Louie unions Huey's `requests` and `trusted_library_events`. Discord rows use `origin=discord`; physical-disc rows use `origin=physical_disc` and IDs such as `physical-17`. Physical events are not converted into fake request rows. `complete` and `completed` normalize to `completed`. `needs_selection` with Huey's parser error becomes `unparsed`; other `needs_selection` rows become `ambiguous`.

Every row has `content_class=general` by default. API responses exclude other classes unless `content_class=all` is explicitly requested. The filter is enforced in the query layer, not only in the dashboard.

## Upstreams and adapters

v1 has read-only adapters for Radarr v3, Sonarr v3, Lidarr v1, qBittorrent v2, SABnzbd, and Shelfarr `/api/v1`. qBittorrent and SABnzbd are separate download-client sources; a usenet job will never appear in qBittorrent. ABBA is deferred pending its undocumented contract. LazyLibrarian is deferred pending confirmation of its real command vocabulary. Whisparr is out of scope because no working key is configured. Bazarr, Bitbot, and BookBot are not request backends.

## Read-only rule

Louie may issue GET requests only to every upstream. It must never add, delete, rename, retry, trigger searches, pause torrents, or modify settings. Shelfarr POST routes are forbidden. Any future retry action belongs in Huey.

## API

- `GET /api/requests`
- `GET /api/requests/{id}` with history
- `GET /api/health`
- `/` is the static status dashboard

`scripts/louie-status` is the operator helper. Deployment installs the
`scripts/motd/90-louie-status` entry into `/etc/update-motd.d` when permitted;
this unprivileged build session could not install the root-owned host entry.

The poller refreshes the source projection on an interval and retains status transitions in Louie's SQLite database so upstream disappearance does not erase history. Ebook rows whose service is `prowlarr` or NULL are marked `routing_state=superseded`; default API results and metrics exclude them. Pass `routing_state=all` to inspect fossils.

Louie binds its HTTP server before starting the first poll. Polling runs in a
background thread, so a cold instance serves an empty dataset immediately.
`/api/health` reports `ever_completed`, `last_successful_poll`,
`poll_age_seconds`, and cached per-upstream results; an upstream is not called
from the health request itself.

Shelfarr `external_source` values are classified without coercion. `huey:N` is
`huey_sequence` only when N is an existing Huey request ID; a numeric value
outside that sequence is recorded as `huey_external` and is not joined, while
other formats are `unrecognized` and are rejected. The counts are retained in
the `shelfarr_correlations` table.

Enrichment joins ARR library, queue, wanted/missing, and history records by the
persisted backend ID. qBittorrent hashes are matched case-insensitively to ARR
`downloadId`; SABnzbd queue and history identifiers remain separate because
usenet jobs do not appear in qBittorrent. `LOUIE_STALLED_AFTER_SECONDS` is
intentionally unset until observed grab-to-import timing can establish a real
threshold; stalled derivation is disabled while it is unset.

Huey is opened through SQLite URI `mode=ro`, independently of filesystem mount
permissions. Huey currently uses rollback-journal mode; a hot journal left by a
Huey crash can prevent Louie from opening `huey.db` until Huey recovers it.
