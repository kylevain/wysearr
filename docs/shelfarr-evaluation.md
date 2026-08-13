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
- creates the private Shelfarr operator account and a dedicated non-admin Huey
  API user with exactly `search:read`, `requests:read`, and `requests:write`;
- configures Prowlarr, the isolated qBittorrent and SABnzbd clients, output
  paths, copy import, English matching, and direct -> Usenet -> torrent
  preference;
- enables only Project Gutenberg direct acquisition; and
- disables LibriVox, credentialed direct sources, Discord, generic webhooks,
  and Telegram.

For an initial disabled installation, stop the old BookBot-owning Huey and both
evaluation services before the drain gate. Start SABnzbd once to create its INI,
stop it again, disable API parameter logging on disk, then start and converge the
evaluation services before recreating Huey:

```bash
docker compose stop huey shelfarr sabnzbd
python3 scripts/bootstrap_shelfarr.py --check-drain-only
docker compose up -d --wait --wait-timeout 300 sabnzbd
docker compose stop sabnzbd
python3 scripts/bootstrap_shelfarr.py --prepare-sab-config
docker compose up -d --wait --wait-timeout 300 sabnzbd shelfarr
python3 scripts/bootstrap_shelfarr.py --enable
docker compose up -d --build --no-deps --force-recreate huey
python3 scripts/validate.py
```

If the bootstrap command fails, `SHELFARR_ENABLED` remains false. Do not switch
ownership: run `docker compose stop shelfarr sabnzbd` followed by
`docker compose start huey`, inspect the reported drain or configuration error,
and retry only after it is resolved. This restores the previous Huey/BookBot
request path without leaving a Shelfarr worker running.

A Usenet provider and at least one working Usenet indexer are still required
before Usenet can improve availability. The 2026-08-12 evaluation had neither;
SABnzbd and its Shelfarr adapter were healthy, but Usenet was unavailable.
Consequently the SABnzbd download-complete-to-Shelfarr-import handoff has not
been proven end to end. Fallback also requires another eligible release and is
not guaranteed for every failure classification.

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

## Rollback

The rollback does not delete Shelfarr state or acquired library files:

1. Stop Discord intake explicitly, then inspect Shelfarr and wait for every
   request/download to become terminal. Do not switch ownership mid-import:

   ```bash
   docker compose stop huey
   ```

2. Set `SHELFARR_ENABLED=false` in `.env`.
3. Stop the evaluation services before restarting BookBot-owned intake:

   ```bash
   docker compose stop shelfarr sabnzbd
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

With `SHELFARR_ENABLED=false`, later normal deployments do not start or
reconfigure Shelfarr/SABnzbd, so the rollback remains durable.
