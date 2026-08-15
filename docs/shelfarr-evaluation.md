# Shelfarr ebook production evaluation

This records the reversible ebook acquisition evaluation and its retained role
as the secondary production backend. Discord and Huey remain the only
family-facing request interface. `SHELFARR_ENABLED=true` keeps Shelfarr healthy
and available behind the LazyLibrarian primary. Huey may dispatch it only after
a metadata miss, a bounded read-only Prowlarr `7020` preflight finds no
plausible ebook torrent, an administratively disabled primary, or another
primary failure still proven to be before the durable mutation marker; it never
races LazyLibrarian. After that marker, a timeout, transport failure, or
LazyLibrarian raw `OK` with no exact history is uncertainty, not a fallback
signal. Shelfarr no longer receives audiobook requests and has no audiobook
library mount. Audiobook ownership is documented in
[architecture.md](architecture.md): ABBA performs
discovery/submission while `ABBA_ENABLED=true`, and BookBot performs import and
retention; disabling ABBA restores the direct Prowlarr/qBittorrent/BookBot path.

## Deployed boundary

```text
Discord -> Huey -> LazyLibrarian primary
                       |
             safe pre-mutation miss only
                       |
                       v
                  Shelfarr API
                       |
             +---------+---------+
             |         |         |
           direct    SABnzbd   qBittorrent
             |         |         |
             +---------+---------+
                       |
                 Shelfarr ebook import
                       |
             /mnt/media/ebooks/Books
                       |
                     Kavita
```

Shelfarr is pinned to `2026.08.09.1` and its multi-platform OCI digest. SABnzbd
is pinned to stable `5.0.4` and its multi-platform OCI digest. Both administrative
ports bind to host loopback only. They are not reachable from the LAN and must
not be presented as request interfaces.

| Service | Compose URL | Operator-only host URL | Persistent state |
| --- | --- | --- | --- |
| Shelfarr | `http://shelfarr` | `http://127.0.0.1:5056` | `config/shelfarr` |
| SABnzbd | `http://sabnzbd:8080` | `http://127.0.0.1:8085` | `config/sabnzbd` |

Use an SSH tunnel for administration from another computer:

```bash
ssh -L 5056:127.0.0.1:5056 -L 8085:127.0.0.1:8085 wyseadmin@192.168.4.86
```

The complete Shelfarr `/rails/storage` tree is persistent and included in
WyseARR checkpoints. This includes all four SQLite databases, generated secret
and encryption keys, queue state, and Active Storage. Copying only
`production.sqlite3` is not a valid backup.

## Cascade and ownership controls

- `EBOOK_ACQUISITION_BACKENDS=lazylibrarian,shelfarr` is authoritative.
  `EBOOK_ACQUISITION_OWNER=lazylibrarian` is only the matching compatibility
  primary assertion. Never infer active request ownership merely from Shelfarr
  being enabled or running.
- Shelfarr outputs ebooks at `/ebooks`, mounted from
  `/mnt/media/ebooks/Books`.
- Shelfarr has no `/audiobooks` mount. A legacy `audiobook_output_path` value in
  its database is inert and is not an ownership grant.
- Project Gutenberg ebook downloads stage privately at
  `state/shelfarr-staging/ebooks`, mounted over `/ebooks/.shelfarr-staging`,
  before Shelfarr publishes them to the DAS. This is required because CIFS
  does not preserve Shelfarr's mandatory mode-0700 private staging.
- The download clients share only the acquisition-specific paths they need
  under a consistent `/downloads` namespace. qBittorrent owns the host download
  root, Shelfarr sees `/downloads/shelfarr` and `/downloads/usenet`, and SABnzbd
  sees `/downloads/incomplete/usenet` and `/downloads/usenet`.
- Shelfarr qBittorrent jobs must use category `shelfarr`, whose save path is
  `/downloads/shelfarr`.
- SABnzbd jobs must use a dedicated `shelfarr` category. Set its complete path
  below `/downloads/usenet` and temporary path below
  `/downloads/incomplete/usenet`.
- Do not configure Shelfarr to use qBittorrent categories `ebooks` or
  `audiobooks`; those belong to BookBot import ownership. ABBA may submit only
  the `audiobooks` category through qBittorrent's API.
- Do not enable Shelfarr's native Discord integration. Huey is the only Discord
  notification producer.
- Do not configure a BookBot handoff for Shelfarr jobs. Shelfarr is the
  exclusive finalizer for every fallback job it accepts.
- Before enabling Shelfarr, the BookBot ebook categories (`ebooks` and
  `ebooks-imported`) must contain zero torrents. The bootstrap intentionally
  ignores `audiobooks` and `audiobooks-imported`; those remain BookBot-owned and
  must not be paused by an ebook ownership change.

## Automated production convergence

The Compose fallback for `SHELFARR_ENABLED` is `false`, while the production
`.env` policy requires literal `true`. When enabled, `deploy.sh` starts
Shelfarr and SABnzbd, runs `scripts/bootstrap_shelfarr.py`, validates the
result, and then recreates Huey. New requests follow the ordered backend policy;
Shelfarr remains idle unless the same durable request safely advances past
LazyLibrarian. The bootstrap is idempotent and performs these
operations without printing secrets:

- configures SABnzbd's isolated incomplete, complete, and `shelfarr` category
  paths;
- when `WYSEARR_USENET_ENABLED=true`, connection-tests and converges the one
  WyseARR-managed NNTP server and Generic Newznab indexer without exposing
  credentials;
- creates the private Shelfarr operator account and a dedicated non-admin Huey
  API user with exactly `search:read`, `requests:read`, and `requests:write`;
- configures Prowlarr, the isolated qBittorrent and SABnzbd clients, output
  paths, copy import, and English ebook matching. When Usenet is enabled, the source
  order is direct -> Usenet -> torrent; otherwise Shelfarr disables its SAB
  client and uses direct -> torrent;
- enables only Project Gutenberg direct acquisition; and
- disables LibriVox, credentialed direct sources, Discord, generic webhooks,
  and Telegram.

## Discord metadata confirmation

Shelfarr metadata selection remains inside Discord; its UI is not exposed as a
family request interface. For a newly submitted ebook, Huey still
automatically uses one unambiguous high-confidence work. If two or three safe
metadata works fall inside the configured ambiguity band, Huey instead:

1. persists at most three bounded candidate snapshots and reserves the exact
   request target as `awaiting_selection`;
2. replies to the original request with a numbered candidate list;
3. accepts only a strict positive integer sent as a direct reply to that exact
   Huey prompt by the original Discord user in the original request channel;
4. atomically claims the first valid Discord reply and ignores gateway
   redelivery without a second dispatch;
5. searches Shelfarr metadata again and requires the persisted candidate
   fingerprint to match exactly; and
6. creates the Shelfarr request using the original Huey request ID and
   `huey:<id>` correlation, then starts the normal request-status/download
   lifecycle routing.

The default reply lifetime is 15 minutes. Set
`HUEY_SELECTION_TTL_SECONDS` to a literal integer from 1 through 86400 seconds
to change it. Expiration releases the target for a new request and durably
routes one clarification/rejection to request-status. A malformed,
out-of-range, wrong-user, or wrong-channel reply is corrective only: it cannot
dispatch acquisition or emit lifecycle events. If Discord cannot return and
persist the prompt message ID, Huey releases the target without acquisition.

The Shelfarr fallback reuses the ebook's one authoritative selection.
LazyLibrarian uses the same strict Discord Reply binding and fresh work-level
fingerprint check and persists an exact LazyLibrarian/OpenLibrary BookID before
dispatch. A safe miss advances that identity without a second prompt. ABBA
audiobook confirmation uses its separate search/grab/status contract. Existing
`needs_selection` rows from parse failures, no metadata results, low-confidence
matches, and non-Shelfarr handlers cannot be resumed. Candidate confirmation is
metadata-work selection; Huey does not call Shelfarr's `/grab` endpoint, which
selects a downloadable acquisition result only after a Shelfarr request exists.
Request-channel `read_message_history` permission is already required by Huey's
readiness check and is sufficient for Discord reply references; no slash-command
or application-command permission is introduced.

## Deployment procedure

Use the repository deployment as the only enable procedure. It quiesces Huey,
BookBot, LazyLibrarian, ABBA, and both evaluation services, checkpoints state,
quarantines any persisted NNTP
server before SAB can start, handles first-run INI creation, converges Prowlarr
and Shelfarr, validates while intake is closed, and restores Huey only after all
checks pass:

```bash
./deploy.sh
```

If deployment fails after replacing runtime state, it deliberately leaves Huey,
BookBot, LazyLibrarian, ABBA, Shelfarr, and SABnzbd stopped. Inspect the reported drain or configuration
error, repair it through the repository/private `.env`, and rerun `./deploy.sh`;
do not start Huey manually into a partially converged ownership state.

## Usenet capability gate

Usenet is separately fail-closed behind `WYSEARR_USENET_ENABLED`. Service
configuration is generated by the repository bootstrap; do not hand-edit
SABnzbd or Prowlarr YAML/configuration files. Provider and indexer secrets live
only in the ignored, mode-0600 `.env` file. The required private inputs are:

| Setting | Purpose |
| --- | --- |
| `USENET_SERVER_HOST` | NNTP provider hostname |
| `USENET_SERVER_PORT` | NNTP port; defaults to TLS port `563` |
| `USENET_SERVER_SSL` | must be literal `true`; credentials require TLS |
| `USENET_SERVER_USERNAME` | NNTP account username |
| `USENET_SERVER_PASSWORD` | NNTP account password |
| `USENET_SERVER_CONNECTIONS` | provider-approved connection limit |
| `USENET_SERVER_RETENTION` | optional provider retention days; defaults to `0` |
| `NEWZNAB_INDEXER_NAME` | fixed managed identity; blank or exactly `WyseARR Books` |
| `NEWZNAB_BASE_URL` | book-capable Newznab service URL, without embedded credentials |
| `NEWZNAB_API_PATH` | Newznab API path; defaults to `/api` |
| `NEWZNAB_API_KEY` | Newznab service API key |

Set the feature flag to `true` only when every required value is available.
The bootstrap connection-tests the NNTP provider before enabling the managed
`WyseARR Primary` SABnzbd server and live-tests the Generic Newznab resource in
Prowlarr. The retained managed-indexer contract advertises audiobook category
`3030` for full rollback compatibility and ebook category `7000` or `7020`, but
current Shelfarr intake uses only the ebook capability; reachability alone is
insufficient.
Validation repeats both live tests and category checks. A partial or unreachable
configuration fails deployment with Huey intake closed.

Prowlarr tags enforce the ownership boundary. Existing non-Shelfarr indexers
and the Sonarr/Radarr/Lidarr/Whisparr applications share the additive
`wysearr-arr` tag, preserving their pre-pilot sync set. The managed book
Newznab has `shelfarr` but not `wysearr-arr`, so it cannot sync into movie/TV
or other ARR workflows. Shelfarr intentionally keeps an empty Prowlarr search
tag filter so it can see both the book Newznab and existing torrent fallback
indexers. Existing unrelated tags are preserved.

With `WYSEARR_USENET_ENABLED=true`, Shelfarr's acquisition preference is:

1. direct sources;
2. Usenet through SABnzbd;
3. torrent through qBittorrent.

This is a preference across eligible candidates, not a promise that every
failure will advance to the next transport. With the flag false, the order is
direct then torrent and the SAB client is disabled; torrent support is retained
in both modes.

As of the 2026-08-12 preflight, no NNTP account or book-capable Newznab
endpoint/API key exists in production. `WYSEARR_USENET_ENABLED` therefore
remains false, and none of the six remaining requests has been retried. The
SABnzbd adapter is healthy, but the NNTP-to-SAB-to-Shelfarr-import path remains
unproven until private credentials are supplied and all readiness checks pass.

The pinned Shelfarr API exposes only the newest 100 requests and has no
work-ID lookup endpoint. Huey reuses exact completed/active works and recovers
lost submissions within that window; an older duplicate is still rejected by
Shelfarr rather than reacquired, but Huey cannot turn that rejection into an
"already available" response. Treat 100 requests as the pilot scale ceiling
until Shelfarr exposes a filtered lookup API.

## Historical success-rate record

Record one row per previously unsuccessful request. The acquisition comparison counts
a request as successful only after a readable, nonempty file of the expected
type is present in its final DAS path. Catalog visibility is a separate result:
until Kavita or Audiobookshelf confirms the item, do not describe it as
family-visible. A search result or backend completion flag by itself is not
success. The audiobook row below records the superseded 2026-08-12 Shelfarr
experiment; it is not a current routing instruction.

| Huey request | Title | Format | Shelfarr result | Source | Download | Final DAS path | Library visible | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| #9 | Tourist Season | ebook | search hits; no safe auto-selection | none | not started | none | unverified | Automatic attempt closed cleanly. |
| #10 | Work in Progress | ebook | ambiguous metadata | none | not started | none | unverified | Author/title did not identify one work confidently. |
| #11 | Harry Potter and the Order of the Phoenix | ebook | search hits; no safe auto-selection | none | not started | none | unverified | Automatic attempt closed cleanly. |
| #12 | The Boxcar Children | ebook | found and imported | direct (Project Gutenberg) | success | `/mnt/media/ebooks/Books/Gertrude Chandler Warner/The Boxcar Children/42796.epub` | unverified | 675,960-byte EPUB; final corrected attempt succeeded. |
| #13 | Island of the Blue Dolphins | ebook | search hits; no safe auto-selection | none | not started | none | unverified | Automatic attempt closed cleanly. |
| #18 | Tourist Season | audiobook | search hits; no safe auto-selection | none | not started | none | unverified | No LibriVox path; Usenet unavailable. |
| #19 | Down in the Dirt | ebook | metadata not found confidently | none | not started | none | unverified | No Shelfarr request was created. |

### 2026-08-12 outcome

- Previous qBittorrent-only final DAS placement success: **0/7**.
- Shelfarr final DAS placement success: **1/7** (14.3%).
- Successful added source: **Project Gutenberg direct download**.
- Usenet contribution: **not evaluated**; no NNTP provider or Usenet indexer
  credentials were available.
- Kavita/Audiobookshelf catalog visibility: **unverified**. The final paths are
  correct and Audiobookshelf's public health endpoint is ready, but WyseARR has
  no Kavita endpoint/auth key or library IDs and no Audiobookshelf API key.

The historical pilot demonstrates one improved DAS finalization among seven
previously unsuccessful requests. It does not establish a general success rate,
current cascade acceptance, or family-visible catalog availability. Shelfarr is
now the enabled serial production fallback after a safe LazyLibrarian
pre-mutation miss; it retains its isolated finalizer, while BookBot remains the
importer for LazyLibrarian/direct jobs. Do not claim catalog visibility until
read-only library credentials are added.

Compare successful final DAS outcomes against those requests' recorded
qBittorrent-only failures. Track catalog visibility separately; do not infer it
from source count, search hits, queue acceptance, or Shelfarr completion.

The private JSON evaluation report records metadata resolution and candidate
count, candidate counts by `direct`/`usenet`/`torrent`/`unknown`, the selected
source, acquisition outcome, import outcome, and final DAS verification. These
fields are measurement only and do not weaken Huey's matcher or select a
release.

The historical Shelfarr Usenet retry cohort contains ebooks #9, #10, #11, #13,
and #19. Run it only after the validator proves an enabled NNTP provider and a live
managed Newznab indexer. Historical audiobook #18 belongs to the ABBA/BookBot
route instead. Do not count an ebook retry as successful unless Shelfarr
completes the import and a readable, nonempty EPUB exists in the confined final
DAS path.

For this ebook path, Huey fails closed into its interactive Discord confirmation
state only when Shelfarr returns two or three safe work-level metadata candidates. The selected
work is freshly fingerprint-verified against the original Huey request before
request creation. Shelfarr also exposes release-level selection APIs, but those
APIs do not independently enforce the requested author or format and are not
used by this flow. Do not auto-grab ambiguous or mismatched acquisition
releases.

Catalog visibility remains a separate read-only check. Ebook validation needs
the actual Kavita URL, a least-privilege Kavita auth key, and ebook library ID
and root mapping. Audiobook validation needs an Audiobookshelf API key,
audiobook library ID, and root mapping. Those values are not currently
configured; without them, report catalog state as `unverified`, never visible.
The authenticated, token-without-persistence verification command is documented
in [wysearr-architecture.md](wysearr-architecture.md).

## Rollback

Ordinary LazyLibrarian unavailability already falls through to Shelfarr before
mutation, so it does not require an ownership switch. For an exceptional
Shelfarr-only degraded mode, first stop Discord intake and fully drain or
reconcile every ebook attempt and the `ebooks`, `ebooks-imported`, and
`shelfarr` categories. Then set `EBOOK_ACQUISITION_BACKENDS=shelfarr` and the
compatibility owner to `shelfarr`. This deterministic singleton mode is not the
validated production policy and must remain visibly degraded until the exact
`lazylibrarian,shelfarr` order is restored. Never move an uncertain or accepted
LazyLibrarian request into Shelfarr to manufacture a fallback.

Restoring production requires both services enabled and credentialed, the
compatibility owner set to `lazylibrarian`, and `./deploy.sh` passing its exact
order and availability gates. Confirm Shelfarr jobs remain on its isolated
`shelfarr` category/finalizer and never enter BookBot's `ebooks` intake.

`config/shelfarr`, `config/sabnzbd`, and downloaded files are intentionally
left in place. Remove them only through a separately reviewed cleanup after a
verified checkpoint; deleting them is not part of rollback.

The preserved direct ebook route is available only as a non-production legacy
singleton when `EBOOK_ACQUISITION_BACKENDS` is absent and
`EBOOK_ACQUISITION_OWNER=direct`; it is not a cascade member and cannot satisfy
the production validator.

With `SHELFARR_ENABLED=false`, later normal deployments first disable the exact
WyseARR-managed NNTP server in the private INI while SABnzbd is stopped. They
then briefly start SABnzbd to verify that disabled state and stop it again.
They do not start or reconfigure Shelfarr. This makes rollback durable without
letting a persisted queue resume during the ownership transition.
