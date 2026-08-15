# Deployed services

Host: `wysearr` (`192.168.4.86`) on Debian 13. Docker Compose project: `wysearr`.

| Service | LAN URL | Role | Persistent data |
| --- | --- | --- | --- |
| Gluetun 3.41.3 | no UI; control API is namespace-local | PIA OpenVPN namespace, kill switch, and transient forwarded-port lease | private `gluetun-data` volume |
| qBittorrent 5.1.4 | `http://192.168.4.86:8080`, published only by Gluetun | Torrent client sharing Gluetun's network namespace | `config/qbittorrent`, `state/torrents` |
| qBittorrent port-forward helper | no UI | Reconciles qBittorrent's listening preference to Gluetun's current PIA lease | reads private `gluetun-data` volume |
| Prowlarr | `http://192.168.4.86:9696` | Indexers and ARR synchronization | `config/prowlarr` |
| LazyLibrarian 02af0464-ls331 | host loopback `http://127.0.0.1:5299` | Ebook metadata/search and qBittorrent submission | `config/lazylibrarian` |
| SABnzbd 5.0.4 | host loopback `http://127.0.0.1:8085` | Evaluation Usenet client | `config/sabnzbd`, `state/torrents/incomplete/usenet`, `state/torrents/usenet` |
| Shelfarr 2026.08.09.1 | host loopback `http://127.0.0.1:5056` | Secondary ebook acquisition and isolated finalization | `config/shelfarr` |
| ABBA | Compose-only `http://abba:5078` | Feature-gated AudioBookBay search and qBittorrent submission | `config/abba/abba.db` |
| Sonarr | `http://192.168.4.86:8989` | TV acquisition/import | `config/sonarr` |
| Radarr | `http://192.168.4.86:7878` | Movie acquisition/import | `config/radarr` |
| Lidarr | `http://192.168.4.86:8686` | Music acquisition/import | `config/lidarr` |
| Bazarr | `http://192.168.4.86:6767` | Sonarr/Radarr subtitles | `config/bazarr` |
| Whisparr | `http://192.168.4.86:6969` | Adult-library acquisition/import | `config/whisparr` |
| Huey | no HTTP UI | Discord intake and request state | `state/huey` |
| BookBot | no HTTP UI | Direct-media import and retention, including ABBA audiobooks | `config/bookbot` |

The Usenet layer is enabled only by `WYSEARR_USENET_ENABLED=true` with private
NNTP and Newznab credentials in `.env`. Repository bootstrap code configures and
connection-tests the managed SABnzbd server and Prowlarr Generic Newznab; no
service configuration file needs a manual edit. The managed book indexer is
isolated from every ARR application by the `shelfarr`/`wysearr-arr` tag
boundary. With the flag false, the managed NNTP provider and Shelfarr's SABnzbd
client are disabled, and Shelfarr omits Usenet from its acquisition order.

Existing UI ports are intended only for the trusted home LAN. LazyLibrarian,
Shelfarr, and SABnzbd are more restricted: their host ports bind only to
`127.0.0.1`, and none is a family request UI. ABBA publishes no host port and is
reachable only from the Compose network. qBittorrent has no fixed or
host-published peer port: Gluetun owns PIA's transient forwarded-port lease and
VPN firewall opening, while `qbittorrent-port-forward` reconciles qBittorrent's
listening preference to that lease through the Web API. Inter-service calls use
Compose DNS names; healthchecks use container-local loopback. Evaluation setup,
ownership, and rollback are documented in
[`shelfarr-evaluation.md`](shelfarr-evaluation.md).

The Pi-SSD/DAS source is `//192.168.4.46/Media`, mounted at `/mnt/media` through
`/etc/fstab`. Container UID/GID is 1000. qBittorrent, ARR category, API-key,
indexer, Bazarr, and provider configuration is converged by `scripts/bootstrap.py`
during every deployment. When the Shelfarr feature flag is enabled,
`scripts/bootstrap_shelfarr.py` additionally converges its scoped API identity,
clients, source policy, ebook output path, and notification boundary. Shelfarr
has no audiobook library mount. When LazyLibrarian is enabled,
`scripts/bootstrap_lazylibrarian.py` prepares its private mode-0600 API/config,
then validates the pinned API version, exact ebook-only settings, qBittorrent
handoff, and Prowlarr application/providers without adding or searching a book.

Huey remains the only book request interface. The production policy is
`EBOOK_ACQUISITION_BACKENDS=lazylibrarian,shelfarr`: Huey tries the enabled,
credentialed services serially, never in parallel. `EBOOK_ACQUISITION_OWNER`
is a deprecated compatibility assertion and, when set, must equal the first
backend. A missing explicit backend policy can still select one legacy owner,
including the old direct route, but that is not the validated production
cascade. Both production service flags remain literal `true`. Huey can skip a
configured backend whose flag is administratively false and continue safely,
but deploy and production validation intentionally mark that state degraded.
`ABBA_ENABLED` independently governs audiobooks. With ABBA enabled, ABBA is the
sole discovery/submission owner for new audiobook requests: Huey talks only to
`http://abba:5078`, ABBA reuses the existing qBittorrent credentials, and every
grab is constrained to category `audiobooks` and save path
`/downloads/audiobooks`. BookBot imports the completed payload into
`/mnt/media/audiobooks` and changes the category to `audiobooks-imported`.
An ABBA error or outage while the flag is true never falls through to another
acquirer. After in-flight work is drained, disabling ABBA restores the direct
Prowlarr/qBittorrent/BookBot route regardless of `SHELFARR_ENABLED`; the former
Shelfarr audiobook route requires a full compatible code-and-state rollback.

Huey does not have a Compose startup dependency on optional ABBA,
LazyLibrarian, Shelfarr, or SABnzbd. Deployment health-gates each service that
the active policy enables before reopening intake, but a later optional-service
outage cannot prevent Huey from starting or handling unrelated channels. Ebook
outages follow the serial pre-mutation fallback rules below; an ABBA outage does
not silently switch an enabled ABBA request to the direct audiobook route.

On the primary attempt, Huey sends the title to LazyLibrarian `findBook`, applies its
confidence/ambiguity matcher, and—when the request supplied an author—requires
every normalized author token in the metadata candidate before persisting the
exact OpenLibrary work ID and freshly revalidating any numbered Reply selection.
Before `addBook`, Huey runs bounded read-only Prowlarr searches for the resolved
title/author variants in exact category `7020`. Any plausible torrent preserves
LazyLibrarian as the first acquirer; zero acceptable results or a probe failure
is still pre-mutation and may advance safely. Prowlarr remains provider
substrate, not a third acquisition backend. When the request supplies an
author, every normalized author token must also be present in the release
title; a title score alone cannot authorize acquisition. The preflight accepts
BookBot's EPUB, MOBI, AZW3, and PDF formats (or an otherwise plausible
formatless ebook result) and rejects explicit audiobook, comic, magazine, and
unsupported-only formats. Huey then adds/verifies the book,
queues and searches only `type=eBook`, and
requires an active LazyLibrarian history row to yield one live qBittorrent hash
at `/downloads/ebooks` in `ebooks` or an already transitioned
`ebooks-imported` state. The latter remains nonterminal until BookBot rechecks
its own ledger and destination. LazyLibrarian's raw search `OK` response is
never treated as a grab. BookBot imports that job to
`/mnt/media/ebooks/Books`, changes it to `ebooks-imported`, and completes the
Huey request by exact hash correlation. LazyLibrarian has no download or DAS
mount and never postprocesses the payload.

A metadata miss, a zero-result `7020` preflight, or another pre-mutation
availability failure advances the same request and selected work identity to
Shelfarr. LazyLibrarian's synchronous `searchBook` catches internal errors, so
raw `OK` with no exact active history/hash is always quarantined after the
mutation marker and never treated as a miss. Shelfarr uses only its isolated
qBittorrent `shelfarr` path (plus its independently gated direct/Usenet sources)
and publishes through its own finalizer; BookBot never processes that job. Once
either backend may have submitted an acquisition, Huey reconciles or
quarantines uncertainty instead of dispatching the other backend.

Shelfarr's deployed request-list endpoint exposes only its newest 100 rows and
does not implement a real offset. Huey therefore attaches an exact correlation
found in that bounded page, proves absence only when fewer than 100 rows are
returned, and quarantines a full-page miss. It never posts a possible duplicate
beyond that inspection horizon.

The Prowlarr application sync category is exactly `[7020]`. Eligibility requires
an enabled torrent indexer that explicitly advertises `7020` and has no retained
failure status; non-`7020`, disabled, failed, and Usenet indexers do not receive
the `lazylibrarian-ebooks` tag. In the historical 2026-08-14 convergence, the
resulting providers were LimeTorrents and The Pirate Bay. LazyLibrarian must
expose exactly the corresponding Torznab providers with `BOOKCAT=7020`,
`DLTYPES=E`, and `MANUAL=1`. Its provider API may retain capability-derived `AUDIOCAT`,
`MAGCAT`, and `COMICCAT` fields, but they are dormant metadata: the pinned
dispatcher rejects every non-ebook download type before category lookup.
Prowlarr's scheduled full sync can refresh capabilities but cannot reset
`DLTYPES` or `MANUAL`; bootstrap and validation reassert the exact contract.

LazyLibrarian's operator-facing settings are:

| Setting | Purpose |
| --- | --- |
| `EBOOK_ACQUISITION_BACKENDS` | Ordered Huey policy; production is exactly `lazylibrarian,shelfarr`. |
| `EBOOK_ACQUISITION_OWNER` | Deprecated compatibility/primary assertion; production is `lazylibrarian`. |
| `LAZYLIBRARIAN_ENABLED` | Literal deployment/readiness flag; production requires `true`. |
| `SHELFARR_ENABLED` | Literal deployment/readiness flag; production requires `true`. |
| `LAZYLIBRARIAN_ADMIN_PORT` | Loopback-only host management port; production default is `5299`. |
| `LAZYLIBRARIAN_URL` | Huey's Compose endpoint; production is `http://lazylibrarian:5299`. |
| `LAZYLIBRARIAN_API_KEY` | Generated 32-character private API key; never log or document its value. |
| `LAZYLIBRARIAN_TIMEOUT_SECONDS` | Bounded Huey API timeout. |
| `LAZYLIBRARIAN_SEARCH_LIMIT` | Maximum catalog candidates considered before safe matching. |
| `LAZYLIBRARIAN_METADATA_SOURCE` | Required persisted source; production is `OpenLibrary`. |
| `HUEY_LAZYLIBRARIAN_MINIMUM_CONFIDENCE` | Minimum automatic/interactive work-match score. |
| `HUEY_LAZYLIBRARIAN_RUNNER_UP_GAP` | Required uniqueness gap for automatic selection. |

Do not expose LazyLibrarian's `/api`: its provider listings include provider
credentials, and the same key authorizes configuration and process commands.
Keep logging at the converged normal level, never log API URLs, and never use
`searchBook`, `queueBook`, or provider listing as a health probe.

An ambiguous ebook or ABBA audiobook search can produce a persisted
two-or-three-option Discord prompt. The same Discord user must use Discord's
Reply action on that exact Huey prompt in the same channel, verify the composer
shows a reply to Huey, and send one integer within 15 minutes by default. A bare
number posted without that Discord message reference is rejected without a
selection. For an ebook, that one selection is authoritative for every cascade
attempt; backend transitions do not create more prompts. Huey freshly
revalidates the selected result before request/grab. The
ABBA readiness endpoint verifies its writable SQLite ledger, qBittorrent auth,
category, and save path without contacting AudioBookBay.

ABBA's operator-facing `.env` settings are:

| Setting | Purpose |
| --- | --- |
| `ABBA_ENABLED` | Literal `true` selects ABBA; literal `false` selects the direct audiobook rollback route. |
| `ABBA_URL` | Huey's Compose-only endpoint; production must be `http://abba:5078`. |
| `ABBA_ABB_HOSTNAME` | AudioBookBay hostname passed to the adapter as `ABB_HOSTNAME`. |
| `ABBA_PAGE_LIMIT` | Search page bound; production is `1`. |
| `ABBA_TIMEOUT_SECONDS` | Huey's ABBA request timeout. |
| `ABBA_SEARCH_LIMIT` | Maximum structured results Huey considers before applying its safe match policy. |
| `HUEY_ABBA_MINIMUM_CONFIDENCE` | Minimum score for automatic or interactive audiobook selection. |
| `HUEY_ABBA_RUNNER_UP_GAP` | Required uniqueness gap for automatic selection. |
| `ABBA_SEARCH_CACHE_SECONDS` | Local sanitized search-cache lifetime. |
| `ABBA_SEARCH_MIN_INTERVAL_SECONDS` | Minimum upstream search interval. |
| `ABBA_RESULT_TTL_SECONDS` | Maximum lifetime of a candidate reference before fresh search is required. |
| `ABBA_HTTP_TIMEOUT_SECONDS` | Adapter timeout for AudioBookBay/qBittorrent HTTP operations. |

Compose maps the existing `QBITTORRENT_USERNAME` and `QBITTORRENT_PASSWORD`
directly to ABBA's `DL_USERNAME` and `DL_PASSWORD`; there is no second secret.
The fixed internal contract is `DOWNLOAD_CLIENT=qbittorrent`,
`DL_SCHEME=http`, `DL_HOST=qbittorrent`, `DL_PORT=8080`,
`DL_CATEGORY=audiobooks`, `SAVE_PATH_BASE=/downloads/audiobooks`,
`ABBA_DB_PATH=/config/abba.db`, `ABBA_MAX_RESULTS=10`, and
`PORT=5078`. Do not override those values in production.

The normal lifecycle is received -> searching -> optional numbered selection ->
queued -> downloading -> processing/importing -> complete, with a durable failed
state at any boundary. ABBA reports only structured search/grab/qBittorrent
state; Huey owns correlation and all Discord messages, and BookBot alone proves
the final import. Every requester-facing selection, queue, progress,
uncertainty, completion, and failure message is backend-neutral. Service names
are confined to operator logs and `#system-health` diagnostics.

ABBA and Huey independently enforce durable candidate and hash ownership in
their respective SQLite ledgers. The first request to reserve an opaque ABBA
candidate and its resolved lowercase v1 hash is canonical. A repeated candidate,
or a different candidate resolving to that exact hash, can only become an inert
alias of the same root owner: Discord message/reply lookups follow the owner,
stale duplicate lifecycle delivery is removed, and no second grab is issued. A
single candidate resolving to a different hash is quarantined as an identity
conflict, not aliased. Initialization and restart reconciliation reject missing,
self-referential, chained, cyclic, or mismatched aliases, preserve ownership for
post-mutation failed/uncertain work, and reuse only the exact persisted candidate
and hash. Failures proven to predate mutation remain releasable/retryable.

For a new ABBA-owned audiobook, BookBot recognizes Huey correlation only as an
exact lowercase `huey-<positive-decimal-request-id>` token in qBittorrent's
comma-delimited tag field. It does not accept uppercase, `huey:`, whitespace-
delimited, partial, zero, negative, or out-of-SQLite-range lookalikes. A normal
job has one such tag; if canonical coalescing leaves more than one, every tag
must resolve to the same root ABBA audiobook owner and every saved hash must
equal the completed qBittorrent v1 hash. Unknown, mixed-media, chained, or
conflicting bindings reject metadata use, import, and lifecycle mutation before
copy. A trusted match publishes one sanitized folder at
`/mnt/media/audiobooks/<request-title>` and stages an
XML-escaped `metadata.opf` containing the title, optional author, and
`urn:btih:<hash>` identifier as part of the atomic import. Existing source OPF or
NFO files are copied unchanged and suppress generation; they are never
overwritten. Uncorrelated/direct audiobooks and every other BookBot category keep
their prior source-derived naming and metadata behavior.

This filesystem contract is intentional because the production Audiobookshelf
library evaluates `folderStructure` before later metadata sources. The directory
therefore carries the trusted title, while OPF can provide the optional author.
BookBot does not rename or add sidecars to an already imported item during normal
reconciliation. See the authenticated acceptance check in
[the operator model](wysearr-architecture.md#administrator-use) and the bounded
legacy repair policy in [recovery](recovery.md#older-audiobook-metadata-repair).

Useful health commands:

```bash
docker compose ps
python3 scripts/validate.py
docker compose logs --tail=100 huey abba bookbot
```

LazyLibrarian deliberately has no Docker stdout log stream because its early
configuration loader can emit downloader settings before upstream redaction is
active. Whisparr's Docker stdout retention is also disabled because its deployed
console target can retain a failed Prowlarr URL whose query value was not
redacted. Inspect their private, access-controlled application logs only through
a secret-safe diagnostic; do not add either service to `docker compose logs`
commands.

Some Servarr releases can persist a failed Prowlarr request as an escaped URL
whose query value bypasses their normal display redaction. Repeated live indexer
tests can therefore create credential-bearing ARR log entries. Secret audits of
those files must report counts and paths only—never matching lines or raw URLs.
If a count is nonzero, use the affected application's supported log-clear
operation and, where necessary, recreate only that consumer before rescanning;
do not print a candidate match to diagnose it.

`python3 scripts/validate.py` checks that ABBA is healthy, private, hardened,
using the exact category/save path, reachable from Huey, and backed by a valid
SQLite ledger. ABBA's `GET /health` checks `database`, `qbittorrent`, `category`,
and `save_path`; it never searches AudioBookBay. Never use `/api/search` or
`/api/grab` as a health probe. Diagnose search failures separately from a green
readiness boundary, and diagnose post-download/import failures in BookBot rather
than resubmitting the grab.

LazyLibrarian validation likewise checks its private mount/port, pinned API
version and capabilities, ebook-only configuration, Huey routing, Prowlarr
application/provider boundary, and exact qBittorrent handoff without searching
or acquiring a title. Separate policy checks require the exact ordered pair,
reject unknown/blank/duplicate/reversed entries and owner mismatches, require
both feature flags and credentials, and compare Huey's running environment to
the same private configuration without printing secret values.

LazyLibrarian completion establishes only a validated BookBot copy on the DAS,
not Kavita catalog visibility. WyseARR currently has no Kavita API credential,
library ID, or confirmed application-side root mapping, so verify that final
boundary manually until a least-privilege read-only check is configured.

Audiobook completion establishes a validated BookBot copy on the DAS, not
Audiobookshelf catalog visibility. API credentials, audiobook library ID, and
root mapping have not been added to WyseARR, so verify Audiobookshelf manually
with the authenticated command in
[the operator model](wysearr-architecture.md#administrator-use) until a
least-privilege catalog check is configured.

## Production acceptance

The authoritative ebook policy is
`EBOOK_ACQUISITION_BACKENDS=lazylibrarian,shelfarr` with compatibility assertion
`EBOOK_ACQUISITION_OWNER=lazylibrarian`. LazyLibrarian is primary; Shelfarr is
the serial fallback and remains enabled, healthy, and idle until Huey records a
safe pre-mutation reason to advance. The primary path is:

```text
#ebooks request -> Huey -> LazyLibrarian -> Prowlarr 7020 provider
  -> qBittorrent category ebooks at /downloads/ebooks
  -> BookBot -> /mnt/media/ebooks/Books -> Kavita
```

The fallback path is `Huey -> Shelfarr -> qBittorrent category shelfarr ->
Shelfarr finalizer -> /mnt/media/ebooks/Books -> Kavita`. It never feeds
BookBot. It may begin only after a metadata miss, zero plausible primary `7020`
releases, an administratively disabled primary, or another failure proven to be
before the durable mutation marker. After that marker, timeouts, raw `OK`
without exact history, and every uncertain outcome reconcile or quarantine and
never fall through. Requester-facing messages remain backend-neutral, and Huey
reports a single final not-found response only after both legitimate backends
miss.

Acquisition acceptance combines deterministic, controlled state-machine tests
with genuine, non-acquiring transport validation. The controlled cases cover
metadata selection and ambiguity, backend order and availability, safe
pre-mutation fallback, timeouts and uncertain mutation, deduplication, replay,
correlation, lifecycle routing, restart recovery, and partial outages. The live
checks use the deployed LazyLibrarian, ABBA, Shelfarr, qBittorrent, Huey, and
BookBot transports and persisted configuration, but do not submit a title,
search AudioBookBay, create a Shelfarr request, add a LazyLibrarian book, or add
a torrent. This is the production acceptance boundary; an unwanted real-media
acquisition is not required.

Run the four unit suites from the repository root. The ABBA suite runs inside
the production-derived image with networking disabled; do not encode a mutable
test count into the acceptance contract:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_*.py'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts python3 -m unittest discover -s scripts/huey/tests -p 'test_*.py'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts/processing python3 -m unittest discover -s scripts/processing/tests -p 'test_*.py'
docker compose build abba
docker run --rm --network none --read-only --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD:/workspace:ro" -w /workspace wysearr-abba python -m unittest discover -s scripts/abba/tests -p 'test_*.py'
```

Then run `docker compose config --quiet`, `python3 scripts/validate.py`, and
`python3 scripts/validate_qbittorrent_vpn.py` against the deployed stack. The
kill-switch acceptance in [qbittorrent-vpn.md](qbittorrent-vpn.md#live-kill-switch-acceptance)
is a deliberate maintenance test and remains separate from routine health.

The production validator still tests every enabled public indexer and reports
secret-free live/failure aggregates. When that enabled inventory is nonempty,
individual upstream search-provider failures are nonblocking warnings. An empty
enabled inventory is blocking, as are local credentials, schemas, download
clients, the independently required live Prowlarr torrent protocol, and the
exact nonempty LazyLibrarian provider contract.

An acquisition release also verifies that qBittorrent still shares Gluetun's
network namespace, Gluetun alone publishes the LAN WebUI, the PIA tunnel and
dynamic forwarded port remain valid, and the aggregate torrent inventory is
unchanged by the deployment. A correctly attached qBittorrent/Gluetun runtime
is preserved rather than recreated for application-only changes.

These checks prove service and acquisition boundaries, not downstream catalog
visibility. WyseARR still lacks the least-privilege Kavita and Audiobookshelf
credentials and confirmed application-side mappings needed for an automated
read-only catalog assertion, so those two visibility checks remain honest manual
limitations rather than hidden release gates.

### Historical 2026-08-14 bounded rollout

The remainder of this subsection records the 2026-08-14 promotion only. Its
container identities, inventories, counts, and credential generations are
historical evidence, not a description of the current live generation.

That live promotion deliberately did not run the full `deploy.sh` transaction:
qBittorrent had unrelated incomplete movie/TV transfers, so it was neither
restarted nor recreated. LazyLibrarian, Huey, and BookBot used scoped service
operations, and qBittorrent identity/start time and its non-ebook workload were
checked afterward. This is a record of the bounded live rollout, not permission
to bypass the normal deployment transaction for future ownership changes.

For the ebook-cascade activation, only Huey was recreated from the tested
image; the unchanged BookBot container was stopped for the pre-checkpoint and
then started. qBittorrent retained container ID
`4524fc7997c9dbd950f42bc1d437130f5aeeef6f2d99db1308321a11b77b666e`, start
time `2026-08-14T06:27:22.090641702Z`, and restart count zero. Its exact
unfiltered pre/post inventory was the same 57 hashes with 37 incomplete jobs;
the `ebooks`, `ebooks-imported`, and `shelfarr` lanes remained empty. All 13
Compose services were healthy afterward. Production validation passed 121/124
checks; the three failures were exhaustive availability tests for already
configured external Prowlarr/Lidarr/Whisparr indexers, not cascade, credential,
configuration, downloader, or import-path failures.

### Historical 2026-08-14 qBittorrent credential rotation

On 2026-08-14, qBittorrent 5.1.4's WebUI credential was rotated in place through
its supported Web API. The qBittorrent container and process were neither
restarted nor recreated. Its container identity and start time remained
unchanged, and the exact unfiltered pre/post inventory remained the same 36
torrent hashes, including the same 23 incomplete jobs. No torrent was added,
removed, or substituted as part of the rotation.

Every credential consumer was then converged to that generation:

- Sonarr, Radarr, Lidarr, and Whisparr each had their one qBittorrent download
  client force-saved and the persisted definition retested. This explicit write
  was required because a masked password plus a surviving old qBittorrent SID
  can make the current-definition test pass without proving the stored password.
- LazyLibrarian's qBittorrent configuration and Shelfarr's encrypted download
  client record were updated and connection-tested.
- ABBA, BookBot, and Huey were recreated with the new private `.env` generation;
  a plain container restart would not have refreshed their environment.

Fresh authentication and consumer checks passed after convergence, and the
temporary authentication-failure guard was restored to its normal setting.
At that time, the verified post-rotation rollback point was
`backups/post-qbittorrent-rotation-20260814-092347` (97 files). All book-control
services were paused together for that checkpoint while qBittorrent and its
active transfers remained continuously running. The independently rotated
2026-08-15 credentials supersede this checkpoint for credential recovery; see
[recovery](recovery.md#credential-generation-boundaries).

## ABBA provenance and upgrades

ABBA's canonical upstream is [AudioBookBay Automated](https://github.com/JamesRy96/audiobookbay-automated).
Production is pinned to upstream commit
`b8e252c2cee5ed745aeeaa0574efd31a05973e8e` and image
`ghcr.io/jamesry96/audiobookbay-automated@sha256:be58c8a0c2ef4ec4c1a1cc6714791b5b72c8bf62a24774ee8c784257c87a2678`.
The WyseARR API/security overlay lives in `docker/abba/Dockerfile`,
`docker/abba/app.py`, and `docker/abba/wsgi.py`; its tests live under
`scripts/abba/tests/`.

The live AudioBookBay search contract currently requires a same-origin `POST`
to `/` with the `s` form field; a `GET /?s=...` redirect drops the query. Result
references are accepted only as exact same-host `/abss/` paths (current) or
`/audio-books/` paths (legacy). The adapter versions its persisted search cache
when this contract changes. Upgrade validation must exercise a real search and
fresh detail-page resolution, including exact title/format identity, rather than
relying on container health or cached candidates.

Never use `latest`. For an upgrade, review the upstream commit diff and its
application/helpers for API, parser, downloader, and security changes; test the
local overlay against the candidate; then update the pinned manifest digest and
documented commit together. Preserve and run the adapter tests plus the full
infrastructure suite before deployment. An upstream image is not a replacement
for the local overlay or its private API contract.

## LazyLibrarian upgrades

Production uses LinuxServer build `02af0464-ls331` at manifest digest
`sha256:f2fd332fb4c5918571f8babd4d52fbcb9ca514be254ba101a47c275cd57eb33f`.
The integration depends on API commands whose response shapes vary by command,
so an upgrade requires reviewing the upstream API implementation, changing the
digest deliberately, and testing the exact candidate/add/verify/queue/search/
history contract against an isolated instance. Then run the bootstrap tests,
full Huey/infrastructure suites, a stopped-owner checkpoint, non-acquiring live
bootstrap, controlled acquisition state-machine cases, genuine non-acquiring
transport validation, and restart recovery. Never infer compatibility from the
moving upstream default branch or a healthy Web UI.
