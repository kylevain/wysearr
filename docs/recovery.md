# Recovery and rollback

## Checkpoint boundary

Create and verify a private checkpoint before maintenance:

```bash
cd /home/wyseadmin/homelab
python3 scripts/backup.py
python3 scripts/backup.py --verify backups/YYYYMMDD-HHMMSS
git status --short
```

Each deployment creates a matching `pre-deploy-<id>` checkpoint before changing
containers and a `post-deploy-<id>` checkpoint only after production validation
succeeds. A checkpoint contains a Git HEAD, selected configuration, `.env`,
SQLite-backup-API copies, bounded qBittorrent `BT_backup` resume metadata, and a
SHA-256 manifest. Verification checks every manifest hash, rejects extra files
and links, and runs `PRAGMA integrity_check` on every copied database.

The consistency boundary matters:

- Each SQLite copy is transactionally consistent, but the collection is not an
  atomic snapshot across all services.
- qBittorrent metadata is limited to 20,000 files, 32 MiB per file, and 512 MiB
  total. Every file must be regular and remain unchanged while copied.
- A running qBittorrent checkpoint is not atomic across `.torrent` and
  `.fastresume` files. Stop qBittorrent before `backup.py` when an exact resume
  generation is required for a stateful upgrade.
- `state/torrents/**` payloads and `/mnt/media/**` library media are never copied.
  Resume metadata cannot recreate missing payload bytes.

These checkpoints are on the same host and are rollback aids, not independent
disaster-recovery backups.

## Code-only rollback

A rollback is code-only only when the reverted change did not change an image
digest, persisted database schema, service-generated configuration, bootstrap
API state, persisted data semantics, or credential format. Inspect the diff and
checkpoint manifest before classifying it this way.

```bash
git log --oneline --decorate -10
git show BAD_COMMIT
git revert BAD_COMMIT
python3 -m unittest -q scripts.tests.test_bootstrap scripts.tests.test_infra
./deploy.sh
```

Pinned digests make an unchanged image selection repeatable. If an image or
persisted-state compatibility boundary changed, do not run `deploy.sh` as a
code-only rollback; use the stateful procedure below.

## Stateful or image rollback

Never start a downgraded image against a database that a newer image has opened
or migrated. Roll code, image, and owning state back as one unit:

1. Select the pre-upgrade checkpoint that matches the target code/image and run
   `python3 scripts/backup.py --verify CHECKPOINT_PATH`.
2. While the current image is still selected, stop the service that owns the
   state: `docker compose stop SERVICE`. Confirm it is stopped with
   `docker compose ps SERVICE`.
3. With that owner still stopped, create a new, uniquely named checkpoint of the
   current generation. This is the recovery path back to the newer state if the
   rollback fails.
4. Revert the code/image selection, but do not start or deploy the downgraded
   service yet.
5. Move the current database main file and any same-generation `-wal` and `-shm`
   sidecars into a new private `backups/displaced-<timestamp>/...` directory.
   Do not overwrite or delete them.
6. Copy only the checkpoint's main `.db` to its repository-relative location,
   mode `0600`, and restore its expected UID/GID. Checkpoints intentionally do
   not contain WAL/SHM sidecars; never combine a restored main database with
   sidecars from another generation.
7. Restore other service-owned configuration only when it is required by that
   version, using files from the same verified checkpoint. Keep the owner
   stopped throughout every state move and copy.
8. Only after compatible state is in place, start that service with
   `docker compose up -d SERVICE`. Check its migration log and API before
   running the full validator and `./deploy.sh`.

For example, a Radarr restore moves all of
`config/radarr/radarr.db`, `radarr.db-wal`, and `radarr.db-shm` that exist into
the private displaced directory before installing the checkpoint's
`config/radarr/radarr.db`. Apply the same rule to the actual main database name
for Sonarr, Lidarr, Prowlarr, Whisparr, Huey, or BookBot. Do not assume every
service uses the same filename.

For qBittorrent, stop `qbittorrent`, move the entire current
`config/qbittorrent/qBittorrent/BT_backup` directory aside, recreate it, and
restore only the verified checkpoint's files. Restore matching qBittorrent
configuration and `.env` credentials when required. Leave `state/torrents`
untouched: metadata is useful only while its referenced payload paths still
exist. Prefer a checkpoint made with qBittorrent stopped for this operation.

If only `.env` was lost and no credential rotation occurred after the selected
checkpoint, verify and restore that file, set mode `0600`, then run `./deploy.sh`.
Bootstrap is idempotent, but it deliberately converges live API configuration;
that is a state change, not a substitute for restoring an incompatible database.

## Rebuild the host

1. Install Debian, Docker Engine, and the Compose plugin.
2. Restore this Git repository and a verified private runtime checkpoint.
3. Restore the CIFS credential outside the repository and the `/etc/fstab`
   `/mnt/media` automount entry.
4. Restore the DAS library from its independent backup. Restore local torrent
   payloads separately if active jobs must resume.
5. Confirm `mountpoint /mnt/media` and DAS read/write access.
6. Restore compatible service state while each owner is stopped, then run
   `./deploy.sh`.

There is no Git remote configured. Protect the local Git history, checkpoint
directories, DAS library, CIFS credential, and any irreplaceable active payloads
with independent backups.

## Failure locations

- Container state: `docker compose ps`
- Service logs: `docker compose logs --tail=200 SERVICE`
- Aggregate checks: `python3 scripts/validate.py`
- Huey request/events DB: `state/huey/huey.db`
- BookBot ledger/logs: `config/bookbot/` and `docker compose logs bookbot`
- ARR service logs/DBs: `config/<service>/logs` and `config/<service>/*.db`
- Deployment checkpoints: matching `pre-deploy-<id>` and `post-deploy-<id>`

Never delete a base-category qBittorrent payload merely to clear an error. Base
categories indicate acquisition/import has not reached a confirmed safe state.
