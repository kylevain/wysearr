# Shelfarr production evaluation

This is a reversible ebook/audiobook acquisition evaluation. Discord and Huey
remain the only family-facing request interface. Shelfarr owns final ebook and
audiobook placement while the evaluation flag is enabled; BookBot remains
deployed and unchanged so the previous path can be restored without rebuilding
it.

## Deployed boundary

```text
Discord -> Huey -> Shelfarr API
                       |
             +---------+---------+
             |         |         |
           direct    SABnzbd   qBittorrent
             |         |         |
             +---------+---------+
                       |
                 Shelfarr import
                       |
          +------------+-------------+
          |                          |
 /mnt/media/ebooks/Books   /mnt/media/audiobooks
          |                          |
        Kavita                Audiobookshelf
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

## Ownership controls

- Shelfarr outputs ebooks at `/ebooks`, mounted from
  `/mnt/media/ebooks/Books`.
- Shelfarr outputs audiobooks at `/audiobooks`, mounted from
  `/mnt/media/audiobooks`.
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
  `audiobooks`; those belong to the preserved BookBot path.
- Do not enable Shelfarr's native Discord integration. Huey is the only Discord
  notification producer.
- Do not configure a BookBot handoff. Shelfarr is the exclusive ebook and
  audiobook finalizer while `SHELFARR_ENABLED=true`.
- Before enabling Shelfarr, all BookBot book categories (`ebooks`,
  `ebooks-imported`, `audiobooks`, and `audiobooks-imported`) must contain zero
  torrents. The bootstrap enforces this drain gate so Shelfarr cannot adopt the
  same infohash while BookBot still owns or retains it.

## Automated production convergence

`SHELFARR_ENABLED` defaults to `false`. When it is `true`, `deploy.sh` starts
Shelfarr and SABnzbd, runs `scripts/bootstrap_shelfarr.py`, validates the
result, and then recreates Huey. The bootstrap is idempotent and performs these
operations without printing secrets:

- configures SABnzbd's isolated incomplete, complete, and `shelfarr` category
  paths;
- when `WYSEARR_USENET_ENABLED=true`, connection-tests and converges the one
  WyseARR-managed NNTP server and Generic Newznab indexer without exposing
  credentials;
- creates the private Shelfarr operator account and a dedicated non-admin Huey
  API user with exactly `search:read`, `requests:read`, and `requests:write`;
- configures Prowlarr, the isolated qBittorrent and SABnzbd clients, output
  paths, copy import, and English matching. When Usenet is enabled, the source
  order is direct -> Usenet -> torrent; otherwise Shelfarr disables its SAB
  client and uses direct -> torrent;
- enables only Project Gutenberg direct acquisition; and
- disables LibriVox, credentialed direct sources, Discord, generic webhooks,
  and Telegram.

## Discord metadata confirmation

Shelfarr metadata selection remains inside Discord; its UI is not exposed as a
family request interface. For a newly submitted ebook or audiobook, Huey still
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

This flow is enabled only for Shelfarr-owned ebooks/audiobooks. Existing
`needs_selection` rows from parse failures, no metadata results, low-confidence
matches, and non-Shelfarr handlers cannot be resumed. Candidate confirmation is
metadata-work selection; Huey does not call Shelfarr's `/grab` endpoint, which
selects a downloadable acquisition result only after a Shelfarr request exists.
Request-channel `read_message_history` permission is already required by Huey's
readiness check and is sufficient for Discord reply references; no slash-command
or application-command permission is introduced.

## Deployment procedure

Use the repository deployment as the only enable procedure. It quiesces Huey
and both evaluation services, checkpoints state, quarantines any persisted NNTP
server before SAB can start, handles first-run INI creation, converges Prowlarr
and Shelfarr, validates while intake is closed, and restores Huey only after all
checks pass:

```bash
./deploy.sh
```

If deployment fails after replacing runtime state, it deliberately leaves Huey,
Shelfarr, and SABnzbd stopped. Inspect the reported drain or configuration
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
Prowlarr. The persisted indexer must advertise audiobook category `3030` and
ebook category `7000` or `7020`; reachability alone is insufficient.
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

## Success-rate record

Record one row per previously unsuccessful request. The acquisition comparison counts
a request as successful only after a readable, nonempty file of the expected
type is present in its final DAS path. Catalog visibility is a separate result:
until Kavita or Audiobookshelf confirms the item, do not describe it as
family-visible. A search result or Shelfarr completion flag by itself is not
success.

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

The pilot therefore demonstrates a material acquisition/finalization
improvement at the DAS boundary for one previously unsuccessful ebook. It does not
yet prove improved family-visible catalog availability. Keep Shelfarr as a
controlled pilot, retain BookBot for rollback, and do not claim catalog
visibility until read-only library credentials are added.

Compare successful final DAS outcomes against those requests' recorded
qBittorrent-only failures. Track catalog visibility separately; do not infer it
from source count, search hits, queue acceptance, or Shelfarr completion.

The private JSON evaluation report records metadata resolution and candidate
count, candidate counts by `direct`/`usenet`/`torrent`/`unknown`, the selected
source, acquisition outcome, import outcome, and final DAS verification. These
fields are measurement only and do not weaken Huey's matcher or select a
release.

The six-request Usenet retry cohort is #9, #10, #11, #13, #18, and #19. Run it
only after the validator proves an enabled NNTP provider and a live managed
Newznab indexer. Do not count a retry as successful unless Shelfarr completes
the import and a readable, nonempty expected-format artifact exists in the
confined final DAS path.

Huey now fails closed into its interactive Discord confirmation state only when
Shelfarr returns two or three safe work-level metadata candidates. The selected
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

## Rollback

The rollback does not delete Shelfarr state or acquired library files:

1. Stop Discord intake explicitly, then inspect Shelfarr and wait for every
   request/download to become terminal. Do not switch ownership mid-import:

   ```bash
   docker compose stop huey
   ```

2. Set `WYSEARR_USENET_ENABLED=false` and `SHELFARR_ENABLED=false` in `.env`.
3. Stop the evaluation services before restarting BookBot-owned intake:

   ```bash
   docker compose stop shelfarr sabnzbd
   python3 scripts/bootstrap_shelfarr.py --prepare-sab-config
   ```

4. Recreate Huey without starting dependencies:

   ```bash
   docker compose up -d --no-deps --force-recreate huey
   ```

5. Confirm new ebook/audiobook requests use the preserved qBittorrent/BookBot
   route and that BookBot is healthy before accepting requests again.
6. Revert the evaluation git commit only after the runtime owner has been
   switched back. Run the normal deployment validation afterward.

`config/shelfarr`, `config/sabnzbd`, and downloaded files are intentionally
left in place. Remove them only through a separately reviewed cleanup after a
verified checkpoint; deleting them is not part of rollback.

With `SHELFARR_ENABLED=false`, later normal deployments first disable the exact
WyseARR-managed NNTP server in the private INI while SABnzbd is stopped. They
then briefly start SABnzbd to verify that disabled state and stop it again.
They do not start or reconfigure Shelfarr. This makes rollback durable without
letting a persisted queue resume during the ownership transition.
