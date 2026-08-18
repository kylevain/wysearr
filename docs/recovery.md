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
SQLite-backup-API copies, the complete bounded Shelfarr storage tree, bounded
qBittorrent `BT_backup` resume metadata, private Shelfarr evaluation reports,
ABBA's `config/abba/abba.db` correlation ledger, LazyLibrarian's private
configuration and SQLite state, and a SHA-256 manifest. Verification
checks every manifest hash, rejects extra files and links, and runs
`PRAGMA integrity_check` on every copied database. The manifest records both
the Git HEAD and whether the source worktree was dirty; a dirty checkpoint is
state evidence, not an exact code-generation rollback point. Diagnostic
`logs.db` databases are deliberately excluded so a checkpoint cannot preserve
credentials exposed by an upstream log-redaction defect.

A release checkpoint is authoritative for its code generation only when it is
created after the release commit from a clean worktree, its manifest verifies,
`git_head` equals that exact commit, and `git_dirty` is literal `false`. A
`post-deploy-*` checkpoint created while implementation changes were still
uncommitted remains valuable runtime-state evidence, but its name does not make
it a clean rollback generation. After committing and completing the exact
committed-state regression, stop the six book state writers/intake services,
create and verify a new uniquely named checkpoint, then restart only the
services enabled by the committed policy. Record that path and commit together
in the handoff.

Normal deployment stops Huey, BookBot, ABBA, LazyLibrarian, Shelfarr, and
SABnzbd around both checkpoints, so book intake, acquisition, and import form
one stopped-owner generation. A standalone `backup.py` call does not stop
services; for an exact book-acquisition generation, stop those six services
first and restart only those appropriate to the current flags/owner.

The consistency boundary matters:

- Each SQLite copy is transactionally consistent, but the collection is not an
  atomic snapshot across all services.
- ABBA recovery requires the transactionally copied `config/abba/abba.db` from
  the same checkpoint as Huey's request state. Restore it with ABBA and Huey
  stopped; never combine a restored main file with live `-wal` or `-shm` files.
  Both ledgers independently reserve canonical candidate and hash ownership, so
  mixing generations can manufacture a missing owner, unsafe alias, or apparent
  replay opportunity. Preserve the exact root owner/alias graph; never repair it
  by retagging or resubmitting.
- Shelfarr recovery requires its four transactional `.sqlite3` copies plus the
  remaining `/rails/storage` generation, including encryption keys, queue state,
  and Active Storage. Restore those from one checkpoint while Shelfarr is
  stopped; never mix generations.
- LazyLibrarian recovery requires `config/lazylibrarian/config.ini`, its
  transactionally copied SQLite database, and Huey's request database from the
  same checkpoint. Restore with Huey, LazyLibrarian, and BookBot stopped; do not
  replay an ambiguous dispatch marker or combine a restored main database with
  live WAL/SHM sidecars.
- qBittorrent metadata is limited to 20,000 files, 32 MiB per file, and 512 MiB
  total. Every file must be regular and remain unchanged while copied.
- A running qBittorrent checkpoint is not atomic across `.torrent` and
  `.fastresume` files. Stop qBittorrent before `backup.py` when an exact resume
  generation is required for a stateful upgrade.
- `state/torrents/**` payloads and `/mnt/media/**` library media are never copied.
  `state/shelfarr-staging/**` direct-download payloads are also excluded. Resume
  metadata and application state cannot recreate missing payload bytes. An exact
  rollback of an active acquisition therefore requires a separately protected
  payload generation; the normal deploy checkpoint is a coherent control-state
  generation, not a media backup.
- Service `logs.db` diagnostic databases are never copied. They are neither
  request history nor an acquisition replay source, and upstream log-redaction
  defects can make them credential-bearing.

These checkpoints are on the same host and are rollback aids, not independent
disaster-recovery backups.

## Credential-generation boundaries

The current boundary is 2026-08-15. qBittorrent and Prowlarr were independently
rotated after diagnostic exposure. At the start of this maintenance boundary,
no checkpoint contained both current credential generations. Treat credential
material in every checkpoint created before the completed post-rotation
deployment as stale, including `.env`, qBittorrent's saved WebUI hash, all saved
consumer passwords/API keys, and encrypted Shelfarr download-client state.
Verified manifests and transactionally copied databases remain useful state
evidence, but stale credentials must never be restored or tested as live values.

Only a new, secret-safe post-rotation checkpoint may become
credential-authoritative, and only after its manifest is verified, every
credential consumer is converged from the current private `.env`, stopped owners
form a coherent generation, and fresh non-secret authentication checks pass.
Until such a checkpoint is created and verified, preserve current live
credentials separately whenever recovering older state. Record its exact path
and clean Git HEAD in the maintenance handoff; never infer authority merely from
a `post-deploy-` name.

### Historical 2026-08-14 record

The checkpoint names and counts below preserve the 2026-08-14 maintenance
record. They describe credentials that were current then, not credentials that
are current after the 2026-08-15 boundary.

All checkpoint generations made before the 2026-08-14 Prowlarr API-key rotation
were classified as **credential-stale**, including
`pre-deploy-20260814-062557-1743396`,
`post-deploy-20260814-062557-1743396`,
`pre-lazylibrarian-20260814-165528`, and
`pre-prowlarr-rotation-20260814-181301`. Their verified manifests and
transactional state copies remained valid rollback evidence, but their `.env`
and Prowlarr-consumer credentials were not valid restoration inputs. Recovery at
that boundary required preserving the then-current rotated secret, reconverging
Prowlarr, Huey, Shelfarr, and LazyLibrarian, and running the production validator
before reopening intake. The temporary Audiobookshelf validation token was
removed from the affected deployment checkpoint snapshots; this did not make
their older Prowlarr key current.

The completed 2026-08-14 qBittorrent WebUI credential rotation created a
separate credential-generation boundary. The checkpoint
`backups/post-lazylibrarian-20260814-183034` and every earlier checkpoint are
**qBittorrent-credential-stale** for that historical generation. At the time of
the rotation this included all existing checkpoint generations, whether or not
a particular generation was current for the independently rotated Prowlarr key.

`backups/post-qbittorrent-rotation-20260814-092347` was the first authoritative
checkpoint for both then-current rotated credential generations with every book
consumer coherently quiesced. Its 97-file manifest was verified immediately after
creation. qBittorrent intentionally remained running to protect 23 incomplete
downloads, so this checkpoint was authoritative for credentials and book-control
state at that time. It is credential-stale after the 2026-08-15 rotations and
is not an atomic qBittorrent resume generation; the running-client limitation
above still applies.

The ebook cascade produced two later verified 124-file control-state checkpoints.
`backups/pre-ebook-cascade-20260814-212743` was created with Huey and BookBot
stopped, no active ebook request or selection prompt, and all ebook torrent
lanes empty; it retains the legacy singleton-owner environment and pre-cascade
Huey schema for bounded rollback. `backups/post-ebook-cascade-20260814-213213`
contains the activated ordered policy and migrated cascade ledger after live
validation. The post checkpoint was taken with services running, so each
SQLite copy is transactionally valid but the collection is not an exact
cross-service generation. qBittorrent remained running for both checkpoints,
and neither is an atomic resume generation.

The current 2026-08-15 boundary does not invalidate a checkpoint's verified
manifest, transactional databases, or qBittorrent resume metadata. It means the
saved qBittorrent WebUI hash, Prowlarr key, and every saved consumer copy must
not be restored as current credentials. Credential-bearing copies include
`.env`, qBittorrent's configuration, LazyLibrarian's configuration, Shelfarr's
encrypted download-client record, and the qBittorrent/Prowlarr client
definitions in application databases. ABBA, BookBot, and Huey consume `.env`
values when their containers are created.

When restoring state from any existing checkpoint, keep qBittorrent and
Prowlarr consumers stopped and preserve both current rotated credentials. Do not
restore an old WebUI password/hash or API key, and do not start a consumer from
the checkpoint's saved credential. After restoring only the required state,
force-persist and retest the qBittorrent clients in Sonarr, Radarr, Lidarr, and
Whisparr; reconverge every Prowlarr consumer plus LazyLibrarian and Shelfarr; and
recreate ABBA, BookBot, and Huey from the current private `.env` before reopening
intake. A masked ARR password or a successful test through a surviving SID is
not persistence proof. Finally run the production validator and verify the exact
live torrent inventory independently without printing credentials or raw URLs.

## Ebook cascade recovery and rollback

The validated production policy is exactly
`EBOOK_ACQUISITION_BACKENDS=lazylibrarian,shelfarr`, with
`EBOOK_ACQUISITION_OWNER=lazylibrarian` retained as a matching compatibility
assertion. A LazyLibrarian metadata miss, a bounded read-only Prowlarr `7020`
preflight with no plausible ebook torrent, or another pre-mutation outage may
advance to Shelfarr, so ordinary backend unavailability does not require a
configuration change or user resubmission. Raw LazyLibrarian `OK` with missing
history after its mutation marker is never an advance signal. Huey likewise
skips a configured backend whose
feature flag is administratively false, although deploy/validation correctly
reports that state as degraded. Never alter policy while an ebook request is
being dispatched, downloaded, imported, reconciled, or quarantined:

1. Stop new Discord intake with `docker compose stop huey`.
2. Inspect Huey's `ebook_cascades`, `ebook_backend_attempts`, and
   `ebook_backend_reservations` state through approved read-only diagnostics,
   plus qBittorrent categories
   `ebooks`, `ebooks-imported`, and `shelfarr`. Let every proven acquisition
   reach its backend-specific finalizer. Do not re-submit, retag, or move a
   payload between categories to manufacture a drain.
3. Preserve uncertain post-submission attempts for reconciliation. Never skip
   from an uncertain LazyLibrarian mutation to Shelfarr merely because the
   first API call timed out.
4. For an exceptional degraded Shelfarr-only recovery, set
   `EBOOK_ACQUISITION_BACKENDS=shelfarr` and
   `EBOOK_ACQUISITION_OWNER=shelfarr` only after the primary path is fully
   drained. This application policy is deterministic but intentionally fails
   the production exact-order validator until the normal pair is restored; do
   not label that degraded runtime PASS.
5. The historical direct ebook route is available only when
   `EBOOK_ACQUISITION_BACKENDS` is absent and the legacy owner is explicitly
   `direct`. It is not a production cascade member and also cannot satisfy the
   production validator.
6. Return to the exact two-backend policy with both service flags enabled and
   credentials converged, then run `./deploy.sh`. Confirm the validator reports
   the exact order and both backends available before reopening intake.

LazyLibrarian has no idempotency/request-ID API. Once Huey durably crosses its
dispatch boundary, any ambiguous timeout or crash is quarantined for manual
correlation: do not retry `addBook`, `queueBook`, or `searchBook`. Compare the
stored exact BookID with LazyLibrarian `getAllBooks`/active ebook history and
the live qBittorrent hash/category/path. Bind the one proven hash or close the
request explicitly; never infer success from raw `OK`. BookBot remains the
completion authority for LazyLibrarian jobs; Shelfarr remains the separate
completion authority for its isolated fallback jobs.

Shelfarr correlation recovery is deliberately bounded to the newest 100 API
request rows because the deployed endpoint ignores offsets. An exact match can
be attached; fewer than 100 rows with no match proves absence; a full page with
no match remains quarantined. Do not bypass that horizon with a blind retry.

## Unavailable ebook retry recovery

The silent retry backlog is part of `state/huey/huey.db`, not a separate queue
service. Its schedule, selected canonical metadata, Discord correlation,
ownership, and final-import state therefore move with the same transactionally
copied Huey database as `requests`, `ebook_cascades`, and the notification
outbox. Restore all of those tables from one checkpoint; never copy an
`unavailable_retries` row into another database generation or reconstruct it
from logs.

Normal restart handling is state-specific:

- `queued` retains its exact `next_retry_at`; downtime does not reset the
  seven-attempt ceiling.
- Search-safe `retrying` work resumes through the existing ebook cascade and
  saved work identity. It is not re-parsed and does not ask Discord for another
  metadata selection.
- `awaiting_import` retains a proven backend handoff or mutation-uncertain
  correlation and returns to the normal LazyLibrarian/BookBot or Shelfarr
  finalizer/correlation reconciliation. A request already marked complete is
  repaired to `fulfilled` during startup.
- A definitive post-mutation failure becomes `blocked`. It remains the unique
  terminal operator-review owner and is never converted into another automatic
  acquisition attempt. Its backend reservation stays held. Read-only
  reconciliation may still accept exact final proof from the already-correlated
  BookBot hash/tag or Shelfarr remote request and atomically repair it to
  `fulfilled`; it cannot search, submit, cancel, or select another work. Blocked
  Shelfarr checks rotate by the durable least-recently-checked cursor, which is
  claimed atomically and survives restart instead of repeatedly polling only
  the first bounded batch.
- `fulfilled` and `expired` are terminal. Restart does not reacquire or renotify
  either state.

All retry reconciliation remains silent until an exact final-library proof
makes the original request complete. Do not infer success from a search hit,
backend acceptance, qBittorrent state, or completed payload. The only success
boundaries are BookBot's exact-hash ledger-validated DAS copy for a
LazyLibrarian acquisition and Shelfarr's correlated completed DAS publication.
Once an item is `blocked`, acquisition never resumes automatically. BookBot may
finish only the exact retained LazyLibrarian hash with its exact Huey tag after
the `ebooks` CategorySpec validates a Books-library destination. A torrent
drifted into `manga-comics` or another category is not ebook final proof. Huey
may poll only the exact retained Shelfarr request ID for `completed`; every other
state remains blocked and silent. Investigate its retained correlation without
resubmitting it.

Use the bounded queue listing and guarded `retry_admin.py force` command in [the
service runbook](services.md#silent-unavailable-ebook-retries). The force
operation is valid only for `queued`, pre-mutation ownership. Never force
`retrying`, `awaiting_import`, or `blocked`, edit retry timestamps with raw SQL,
clear the active-identity index, or delete a row to make a live request bypass
ownership. An unresolved saved identity is a silent failed attempt, not
authority to select a nearby edition.

For restoration or rollback, stop Huey and BookBot along with both ebook
acquisition services, restore the Huey database without live WAL/SHM sidecars,
then start the schema-owning Huey generation before allowing import completion
writes. A version that predates `unavailable_retries` does not understand this
ownership. Treat removal of the table, its partial unique index, or its terminal
triggers as a stateful schema rollback and use a matching pre-feature checkpoint;
do not run downgraded code against the migrated database.

## ABBA feature rollback

`ABBA_ENABLED` owns only new audiobook acquisition. Shelfarr remains ebook-only,
and BookBot remains the audiobook importer in both modes. Do not switch owners
while an ABBA-correlated audiobook is being submitted, downloaded, or imported:

1. Stop new Discord intake with `docker compose stop huey`.
2. Inspect ABBA status and the qBittorrent `audiobooks` category. Allow every
   accepted job to reach BookBot's imported ledger, or leave the flag unchanged
   and resolve the job. Do not re-submit or recategorize it to force a drain.
3. Set `ABBA_ENABLED=false` in `.env`. Leave `config/abba` and existing payloads
   in place; rollback does not delete history or media.
4. Run `./deploy.sh`. Deployment checkpoints the stopped ABBA ledger, leaves
   ABBA stopped, starts Huey without starting the disabled dependency, and
   validates the preserved direct audiobook owner and category paths.
5. Confirm `docker compose ps -a abba` is stopped, BookBot is healthy, and a new
   audiobook request follows Prowlarr/qBittorrent/BookBot regardless of
   `SHELFARR_ENABLED`.

While `ABBA_ENABLED=true`, ABBA remains the sole discovery/submission owner:
service failure or outage is not authorization to invoke the direct route. The
direct route applies only to new requests after the explicit drained flag
transition above. Existing canonical candidate/hash owners and their inert
aliases remain bound to their original mode through completion or review.

To re-enable, drain any direct audiobook submission already in flight, set the
flag to literal `true`, and run `./deploy.sh`. Never call ABBA's grab endpoint as
a health test; `GET /health` is the non-acquiring readiness check.

Restoring former Shelfarr audiobook ownership is not a feature-flag rollback.
It requires a deliberate compatible code-and-state rollback, including the old
mount/ownership model, and must follow the stateful rollback procedure below.

## Older audiobook metadata repair

BookBot applies trusted Huey title/author metadata only while performing a fresh,
exactly correlated ABBA import. Normal reconciliation deliberately does not
rename a retained library directory, add or replace an OPF/NFO, or rewrite its
ledger destination. Do not replay a torrent, edit Huey's hash/tag correlation, or
rename a DAS folder merely to invoke the new behavior.

For an older item whose Audiobookshelf title was derived incorrectly under the
production `folderStructure`-first precedence, first use the authenticated
application-side check in
[the operator model](wysearr-architecture.md#administrator-use) to bind one exact
Audiobookshelf item ID to one exact library root/path. Preserve a backup of that
item and its existing OPF/NFO before any repair. A catalog-only correction belongs
in Audiobookshelf's item editor or a separately reviewed authenticated update for
that exact item ID; verify it again after a scan/restart. Never use `matchall`, a
fuzzy title match, or a bulk metadata overwrite as repair.

A persistent filesystem rename or sidecar migration is a stateful maintenance
operation because BookBot's ledger, the retained qBittorrent job, the DAS path,
and Audiobookshelf may all refer to the old directory. Stop BookBot and new
audiobook intake, checkpoint Huey/BookBot/qBittorrent state, preserve source
sidecars byte-for-byte, and use a separately reviewed per-item migration plan.
The current runbook does not authorize an in-place rename or OPF/NFO overwrite.
Resume only after the exact authenticated path/title/optional-author validation
passes and the BookBot ledger still names the actual destination.

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
   Do not overwrite or delete them. For Shelfarr, move the complete current
   `config/shelfarr` tree as one generation.
6. Copy the checkpoint's transactionally copied main `.db`, `.sqlite`, or
   `.sqlite3` file to its repository-relative location, mode `0600`, and restore
   its expected UID/GID. Checkpoints intentionally do not contain WAL/SHM
   sidecars; never combine a restored main database with sidecars from another
   generation. For Shelfarr, restore the entire checkpointed
   `config/shelfarr` tree—including all four `.sqlite3` databases, keys, queue
   state, and Active Storage—before setting the directory to mode `0700`.
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

For ABBA, the owning service is `abba` and the main database is
`config/abba/abba.db`. Stop both `huey` and `abba`, displace any main/WAL/SHM
generation together, restore the verified main file as UID/GID 1000 with mode
`0600`, and keep `config/abba` owner-only. Restart ABBA and require its exact
`/health` checks (`database`, `qbittorrent`, `category`, and `save_path`) to be
`ok` before restarting Huey.

For LazyLibrarian, stop `huey`, `lazylibrarian`, and `bookbot`; restore
`config/lazylibrarian` and Huey's database from the same verified generation.
Keep the directory mode 0700 and private files mode 0600, start LazyLibrarian
alone, run `scripts/bootstrap_lazylibrarian.py`, and verify its pinned API,
Prowlarr providers, and qBittorrent handoff before starting BookBot and Huey.
Never start a downgraded LazyLibrarian against a database opened by the newer
image, and never recover an uncertain Huey dispatch by issuing another search.

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
- Service logs: `docker compose logs --tail=200 SERVICE` except LazyLibrarian
  and Whisparr, whose Docker stdout logging is intentionally disabled
- Aggregate checks: `python3 scripts/validate.py`
- Huey request/events DB: `state/huey/huey.db`
- ABBA correlation DB/logs: `config/abba/abba.db` and `docker compose logs abba`
- LazyLibrarian state/logs: `config/lazylibrarian` and its private
  `config/lazylibrarian/Logs` tree; use secret-safe count-only diagnostics, not
  Docker logs
- BookBot ledger/logs: `config/bookbot/` and `docker compose logs bookbot`
- ARR service logs/DBs: `config/<service>/logs` and `config/<service>/*.db`
- Deployment checkpoints: matching `pre-deploy-<id>` and `post-deploy-<id>`
- Physical intake: `state/physical-media/incoming` and the
  `trusted_library_events` rows in `state/huey/huey.db`

Do not print raw ARR logs while investigating a failed Prowlarr consumer test.
Some Servarr releases preserve the request as an escaped URL that bypasses
normal query-value redaction. Scan for the current secret by count and file path
only, clear any affected logs through the application's supported operation,
and rescan without displaying matching lines.

Never delete a base-category qBittorrent payload merely to clear an error. Base
categories indicate acquisition/import has not reached a confirmed safe state.

## Physical-media recovery

The intake directory is payload state and is deliberately not copied into Git.
Do not delete an MKV in `state/physical-media/incoming` while its trusted event
is active. Movie deliveries contain one validated MKV and route to Radarr. TV
deliveries use manifest version 2 with `media_type: tv` and a `files` array;
Huey imports them through Sonarr only when the series, season, and every
episode number are explicit and Sonarr knows those episodes. Grouped ambiguous
or nonstandard physical-video deliveries stay preserved under
`state/physical-media/incoming` and receive one `#import-errors` review notice.

Restarting Huey safely resumes `validated`, `identity_resolved`, and
`importing` rows. It first checks Radarr/Sonarr and the final DAS state, so a
completed move is recovered without another POST.

`import_submitting` means Huey may have crossed the non-idempotent ARR POST
boundary before persisting the command ID. Startup fails that row closed to
`manual_review` and emits one `#import-errors` event. Inspect Radarr or Sonarr
history, the entity, the source path, and `/mnt/media` before changing that
state; never replay the delivery blindly. `manual_review` and `failed` are
terminal until an operator deliberately corrects metadata or imports the file.

Re-sending the identical MKV and manifest is safe: its full SHA-256 resolves to
the existing event, and the trusted-event outbox unique index prevents another
Discord delivery. If the manifest is invalid, it receives a deterministic
quarantine identity and one import-error notification; correct delivery creates
a new file-derived identity without overwriting the audit row.

Before enabling the worker, require all of the following:

1. `/mnt/media` is mounted read/write and the normal deploy write probe passes.
2. Radarr reports `/media/movies` accessible and Sonarr reports the TV root
   accessible.
3. BatFire has delivered either one deterministic movie MKV plus `manifest.json`
   or a grouped manifest-last delivery.
4. Movie manifests identify one movie; TV manifests identify one series, one
   season, and explicit episode numbers.

Then set `PHYSICAL_MEDIA_ENABLED=true` and recreate Huey, Radarr, and Sonarr
when their mount specifications changed. If no safe artifact exists, leave the
flag false; read-only API, schema, and mount validation are the supported
stopping boundary.

BatFire owns the optical-drive trigger outside Docker. Rebuild the host trigger
from the tracked files in `scripts/batfire/`: install
`99-arm-physical-media.rules` to `/etc/udev/rules.d/`, install
`arm-disc-trigger@.service` to `/etc/systemd/system/`, install
`arm-disc-trigger` to `/usr/local/sbin/`, reload udev/systemd, and keep
container-local ARM udev rules disabled so only the host starts jobs.
