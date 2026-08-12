# Deployment lessons and upgrade checklist

## Compatibility is state plus image

Whisparr's live database contained a quality definition from an incompatible
image generation even though the container itself was running. A useful upgrade
test must exercise API endpoints that deserialize persisted data, not only
`/ping`. The production repair is narrow, creates and integrity-checks its
rollback database before live mutation, refuses to modify a populated library
automatically, and checks both quality definition and profile APIs afterward.

qBittorrent remains at 5.1.4 because newer login behavior had failed integration
testing with this ARR set. All service images are pinned by digest. An upgrade
means intentionally changing the digest, backing up, recreating, and rerunning
the entire production validator.

## Running does not mean configured

Container uptime previously hid zero Prowlarr indexers, transient qBittorrent
credentials, a failing Lidarr client, disabled Bazarr integrations, and idle Huey
and BookBot placeholders. Production acceptance now verifies APIs, synced
indexers, download clients, category paths, library roots, subtitle settings,
SQLite schema/integrity, and custom-service readiness.

## Configuration is reproducible but private

Bootstrap logic belongs in Git; credentials and service databases do not. The
deployment reads/generates private `.env` values, converges APIs and category
configuration idempotently, and takes SQLite-safe local checkpoints plus bounded
qBittorrent resume-metadata snapshots. Active payloads and DAS library media are
outside that checkpoint boundary. A Git clone without a runtime checkpoint
cannot reconstruct external credentials or request history, and a checkpoint on
the same disk is not an independent backup.

## Storage failures must fail closed

The host may start Docker before a network share is mounted. Deployment
therefore verifies that `/mnt/media` is the actual writable mount. Imports copy
atomically and only mark a qBittorrent job imported after success. Retention
deletes only imported-category jobs; it never treats age alone as proof of a
safe import.

## Upgrade checklist

1. Commit the current known-good code. For a stateful change, stop the affected
   owner and run `python3 scripts/backup.py` so its database/resume generation is
   exact; verify the checkpoint before proceeding.
2. Read the image's migration/release notes and change one compatibility boundary
   at a time.
3. Run unit tests, `docker compose config --quiet`, and `git diff --check`.
4. Rebuild/recreate the affected container with its persistent configuration.
5. Exercise service APIs, database integrity, download-client tests, indexer
   sync, mount writes, healthchecks, and `python3 scripts/validate.py`.
6. Inspect logs for authentication, migration, permissions, and repeated retry
   errors before committing the upgrade.
7. Keep the matching post-validation deployment checkpoint. If rollback later
   requires an older image, restore its compatible pre-upgrade state while the
   owning service is stopped; never let the old image open the newer database.
