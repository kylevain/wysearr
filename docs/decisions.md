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
  The original Huey -> Prowlarr -> qBittorrent -> BookBot book path remains a
  non-production compatibility rollback; the later ebook decision below defines
  the validated production cascade.
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
- 2026-08-12: Huey is the sole Discord notification producer. It replies to the
  original request only for acknowledgement, then routes request state to
  `#request-status`, acquisition/download lifecycle to `#download-queue`, new DAS
  imports to `#recent-additions`, import failures/manual action to
  `#import-errors`, and service/runtime health only to `#system-health`. Native
  Discord delivery in Radarr, Sonarr, Lidarr, and Bazarr remains disabled to
  prevent bypasses and duplicates.
- 2026-08-12: A request completion and a newly imported library item are distinct
  events, not mirrored copies. Completion proves import to the DAS library path;
  it does not prove Plex or another catalog application has indexed the item.
- 2026-08-12: The initial feature-gated Shelfarr 2026.08.09.1 evaluation owned
  ebook and audiobook discovery, acquisition, and final DAS placement while
  `SHELFARR_ENABLED=true`; it did not hand off to BookBot. The initial failed
  cohort improved from 0/7 to 1/7 final DAS placements through Project
  Gutenberg. This is a historical result; the production audiobook route below
  supersedes Shelfarr's audiobook ownership.
- 2026-08-13: Shelfarr remains feature-gated for ebooks. With
  `ABBA_ENABLED=true`, the private ABBA adapter owns AudioBookBay search and
  qBittorrent submission for audiobooks, constrained to category `audiobooks`
  and `/downloads/audiobooks`; BookBot remains the only audiobook importer and
  retention owner. ABBA publishes no host port and its readiness probe never
  searches AudioBookBay. After in-flight work is drained, setting
  `ABBA_ENABLED=false` restores the direct Prowlarr/qBittorrent/BookBot route
  regardless of `SHELFARR_ENABLED`. Former Shelfarr audiobook ownership requires
  a deliberate full compatible code-and-state rollback.
- 2026-08-13: An audiobook request is complete only after BookBot validates and
  atomically publishes it to `/mnt/media/audiobooks`. This does not establish
  Audiobookshelf catalog visibility. Authenticated application-side verification
  requires a least-privilege API token, audiobook library ID, and confirmed root
  mapping; none is stored in the repository.
- 2026-08-13: Production Audiobookshelf uses `folderStructure`-first metadata
  precedence. A normal fresh ABBA import therefore binds one Huey request tag to
  the exact torrent hash, uses the sanitized trusted request title as the single
  folder below `/mnt/media/audiobooks`, and generates an XML-escaped
  `metadata.opf` only when the source provides no OPF/NFO. The optional request
  author may be staged in that OPF. Source metadata is preserved, non-ABBA imports
  remain unchanged, and already imported items are never automatically renamed or
  retrofitted.
- 2026-08-13: ABBA derives from canonical upstream AudioBookBay Automated at
  commit `b8e252c2cee5ed745aeeaa0574efd31a05973e8e`, with its OCI image pinned by
  manifest digest `sha256:be58c8a0c2ef4ec4c1a1cc6714791b5b72c8bf62a24774ee8c784257c87a2678`.
  WyseARR retains a local API/security overlay and its tests. Upgrades require
  reviewing the upstream diff and adapter contract, pinning a new commit and
  digest together, and rerunning adapter/infrastructure tests; `latest` is never
  a production input.
- 2026-08-14: `EBOOK_ACQUISITION_BACKENDS=lazylibrarian,shelfarr` is the
  authoritative ordered production policy. Huey tries these backends serially
  for one request and one selected work identity. LazyLibrarian availability is
  gated by a deterministic, read-only Prowlarr `7020` search before `addBook`;
  a metadata miss, zero plausible ebook releases, or another failure proven to
  be pre-mutation may advance. A supplied author is a hard metadata and release
  gate, so all normalized author tokens must appear in the candidate's author
  and release title before the score can authorize LazyLibrarian mutation.
  LazyLibrarian raw `OK` with no history is not a
  miss because the pinned implementation swallows internal search exceptions;
  submission uncertainty must reconcile or quarantine and cannot race the next
  backend.
  `EBOOK_ACQUISITION_OWNER=lazylibrarian` remains only as a deprecated matching
  primary assertion. A missing backend setting preserves the historical
  singleton-owner/direct compatibility route, but `direct` is not a production
  cascade member.
- 2026-08-14: LazyLibrarian owns primary catalog/provider acquisition only;
  Prowlarr supplies ebook-only Torznab providers, qBittorrent receives category
  `ebooks` at `/downloads/ebooks`, and BookBot is its importer/retention owner.
  LazyLibrarian receives no payload or DAS mount and its postprocessor/schedulers
  are disabled. Shelfarr is the enabled secondary backend and retains its
  isolated `shelfarr` download path and its own finalizer. BookBot never
  finalizes a Shelfarr fallback job.
- 2026-08-14: LazyLibrarian has no idempotency key. Huey persists and atomically
  reserves the exact metadata-source BookID before the first mutation, then
  accepts only one active ebook history identity that resolves to the exact
  live qBittorrent hash/category/save path. Raw search `OK` is not acquisition
  proof. A timeout or restart after dispatch is quarantined for one manual
  correlation notification and never automatically replays add/queue/search.
- 2026-08-14: LazyLibrarian production is pinned to LinuxServer build
  `02af0464-ls331` at manifest digest
  `sha256:f2fd332fb4c5918571f8babd4d52fbcb9ca514be254ba101a47c275cd57eb33f`.
  The API and provider response shapes are version-sensitive; upgrades require
  isolated contract/state-machine testing plus genuine non-acquiring transport
  validation and restart recovery, not only health checks.
- 2026-08-14: The LazyLibrarian provider boundary is category `7020`, not broad
  Books category `7000`. Only enabled torrent indexers that explicitly advertise
  `7020` and have no retained failure status receive the managed Prowlarr tag.
  Their one-for-one LazyLibrarian Torznab providers require `BOOKCAT=7020`,
  `DLTYPES=E`, and `MANUAL=1`. Capability-refresh values in `AUDIOCAT`,
  `MAGCAT`, and `COMICCAT` are accepted only as canonical dormant metadata;
  the pinned dispatcher rejects those media types before category selection.
- 2026-08-14: qBittorrent 5.1.4's WebUI credential was rotated through the live
  supported API without restarting or recreating qBittorrent. The exact
  unfiltered inventory was unchanged at 36 torrents, including 23 incomplete,
  and all ARR, LazyLibrarian, Shelfarr, ABBA, BookBot, and Huey credential
  consumers were explicitly converged. Future rotations must force-persist
  masked ARR client credentials instead of trusting a successful test that may
  be using a surviving SID, and must treat every earlier checkpoint as stale for
  the qBittorrent credential while retaining its independently valid state.
- 2026-08-15: ABBA and Huey independently reserve the opaque audiobook
  candidate ID and resolved v1 hash in their own durable ledgers. The first
  canonical owner receives the acquisition; same-candidate or same-hash
  duplicates become inert request/delivery aliases only when their identities
  converge on that owner. A candidate resolving to a different hash, a missing
  or chained owner, or any mixed identity is quarantined rather than submitted.
  Restart migration and reconciliation preserve post-mutation ownership, never
  replay a different candidate, and keep a proven pre-mutation failure
  retryable.
- 2026-08-15: BookBot accepts Huey correlation only as exact lowercase
  `huey-<positive-decimal-request-id>` tokens in qBittorrent's comma-delimited
  tag field. Multiple Huey tags are safe only when every tag and the exact hash
  resolve to the same root canonical ABBA audiobook request; malformed,
  unknown, mixed-media, chained, or conflicting bindings fail closed. All
  requester-facing selection and lifecycle text is backend-neutral. Backend
  names belong only in operator logs and service/runtime health diagnostics.
- 2026-08-15: Runtime checkpoint manifests record `git_head` and `git_dirty`.
  Dirty checkpoints remain state evidence but are not exact code-generation
  rollback points. The release checkpoint is created and verified only after
  commit from a clean worktree, with that commit recorded as `git_head` and
  `git_dirty=false`; torrent/library/staging payloads and service `logs.db`
  diagnostic databases remain outside the checkpoint boundary.
- 2026-08-15: Exhaustive tests of a nonempty enabled indexer inventory report
  individual upstream failures as nonblocking warnings. An empty inventory and
  the independent required live torrent protocol, credential, category, and
  LazyLibrarian provider-contract checks remain blocking release gates.

Huey, Dewey, and Louie follow the established Duck-nephew naming scheme. Huey is
Discord intake; Dewey is the conversational library interface; Louie remains
reserved for future library automation.
