# WyseARR operator model

This is the short operator view. The authoritative component and data-flow
description is [architecture.md](architecture.md).

## Normal use

The six production request channels are `#movies-tv`, `#ebooks`, `#audiobooks`,
`#manga-comics`, `#roms`, and `#sheet-music`. Only the combined movie/TV
channel needs a type prefix:

```text
movie: The Third Man
tv: Severance
```

Ebook and audiobook requests may add an author with `by`. Other direct-media
requests may include an edition, platform, instrument, or format in the title
when needed to disambiguate a release.

Huey reuses the existing request number when a new Discord message has the same
exact active or completed media target. Case and whitespace are normalized, but
punctuation, accents, author, movie/TV kind, year, edition, platform, and format
remain distinct. Failed or selection-needed requests can be retried with a new
message. If direct-media results are genuinely tied, Huey may show up to three
sanitized release-title and format/size distinctions; refine the request with
one of those terms. It never posts torrent links, hashes, credentials, or raw
provider identifiers.

For existing ARR items, Huey reports imported media immediately, leaves an
already monitored item alone without starting a duplicate search, and monitors
then searches an existing unmonitored item. For BookBot-owned direct media, an exact torrent
already in the expected category is correlated rather than added again. An
imported-category record is not called complete until BookBot verifies its safe
import ledger; another category requires administrator review.

Huey immediately records the message and acknowledges it as a reply to the
original Discord message only. The user does not operate qBittorrent or move
files. Accepted, rejected, completed, and failed request states go to
`#request-status`; queued acquisitions and download lifecycle updates go to
`#download-queue`. ARR API failures remain queued for a later reconciliation
pass rather than being guessed terminal.

When an ARR, Shelfarr, or BookBot confirms a new DAS import, Huey publishes the new item to
`#recent-additions` and the completed request state to `#request-status` as two
distinct events. An import failure sends the failed request state to
`#request-status` and the operator-facing failure or manual-action notice to
`#import-errors`. Runtime and service health issues alone may use
`#system-health`. Lifecycle events are not copied back into request channels.
Requester-facing acknowledgement, selection, queue, progress, uncertainty,
completion, rejection, and failure text never identifies LazyLibrarian,
Shelfarr, ABBA, Prowlarr, qBittorrent, or BookBot. Those implementation names
remain available in operator logs and `#system-health` diagnostics only.

For movies and TV, completion currently proves that Sonarr or Radarr reports an
imported media file on the DAS. No Plex scan is yet requested or accepted as
part of that state, and visibility in Plex is not established. Scan the matching
Plex library manually until that integration is authorized. On the primary
ebook path, completion proves BookBot's validated exact-hash import of the
LazyLibrarian-correlated qBittorrent payload. On a Shelfarr fallback, completion
instead proves Shelfarr's isolated final DAS publication; Shelfarr jobs never
enter BookBot. For an ABBA-acquired audiobook or other BookBot-owned direct
media, completion proves BookBot's validated atomic copy. None of these states
proves that the downstream library application has indexed the item.
LazyLibrarian's `OK` or history state alone is never a completion boundary.

Huey is the sole Discord notification producer. Native Discord integrations in
Radarr, Sonarr, Lidarr, and Bazarr remain disabled so lifecycle events cannot
bypass Huey's routing or be emitted twice. `#automation-admin` is not part of the
lifecycle route set.

Music and adult-library requests currently use their Lidarr and Whisparr Web
UIs because no Discord channels are assigned to those media types.

Bazarr manages subtitles for Sonarr and Radarr with the default English profile.
A matching embedded English subtitle satisfies that profile, so the absence of
an external `.srt` file is not a failure. External subtitle availability is
content- and provider-dependent. Bazarr does not post routine subtitle events to
Discord; only a routed runtime or service health issue belongs in
`#system-health`.

## Data placement

- Active and retained torrent data: local SSD under `state/torrents`.
- TV, movies, music, adult media: ARR imports into `/mnt/media/<library>`.
- Ebooks: `/mnt/media/ebooks/Books` for Kavita.
- Manga/comics: `/mnt/media/ebooks/Comics` for Kavita.
- Audiobooks: `/mnt/media/audiobooks` for Audiobookshelf.
- ROMs: `/mnt/media/roms` for RomM.
- Sheet music: `/mnt/media/sheetmusic`.

Successful imports retain the original torrent locally for 14 days and then
remove both the qBittorrent job and local payload. Failed or incomplete imports
are never eligible for automatic payload deletion.

## Administrator use

`./deploy.sh` is the single redeploy/reconciliation command. It does not require
manual YAML editing or repeated Web UI configuration. `python3 scripts/validate.py`
is the non-destructive production acceptance check; it performs live service
connection tests but does not mutate managed configuration or media. See
[recovery.md](recovery.md) before restoring databases or rolling back code.

The historical book pilot and Shelfarr warm-rollback boundary are documented
in [shelfarr-evaluation.md](shelfarr-evaluation.md). LazyLibrarian, Shelfarr,
and ABBA are never family request UIs; Discord and Huey remain the only request
interface. `EBOOK_ACQUISITION_BACKENDS=lazylibrarian,shelfarr` is the exact
production order; both service flags and credentials are required. The retained
`EBOOK_ACQUISITION_OWNER=lazylibrarian` value only asserts the first backend.
Huey advances serially on a clean pre-mutation miss/failure and never races the
services or falls back after an uncertain submission. LazyLibrarian uses
Prowlarr ebook-only providers, qBittorrent category `ebooks`, and BookBot import
to `/mnt/media/ebooks/Books`. Shelfarr fallback uses its isolated `shelfarr`
category and its own finalizer, never BookBot. `ABBA_ENABLED=true` makes ABBA the
sole audiobook discovery/submission owner, always followed by qBittorrent and
BookBot; an outage in that mode does not fall through to direct acquisition.
Disabling ABBA after draining in-flight work restores the direct
Prowlarr/qBittorrent/BookBot route regardless of `SHELFARR_ENABLED`, without
changing movie/TV handling. Former Shelfarr audiobook ownership requires a full
compatible code-and-state rollback.

Huey and ABBA independently reserve each opaque audiobook candidate and resolved
v1 hash. Same-candidate or same-hash duplicates may resolve only to one root
canonical request; a candidate that resolves to a different hash is quarantined.
Restart recovery preserves that owner and follows only exact, nonchained aliases.
BookBot accepts only lowercase `huey-<positive-decimal-request-id>` tokens from
qBittorrent's comma-delimited tag field, and multiple Huey tags must all resolve
with the exact hash to that same canonical ABBA audiobook owner.

BookBot completion is not proof that Audiobookshelf indexed the item. The
application-side acceptance boundary is an authenticated Audiobookshelf API
result from the configured audiobook library, with that library's application-
side folder confirmed to map to the same DAS directory WyseARR mounts as
`/mnt/media/audiobooks`. For a fresh correlated ABBA import, require the exact
single-component relative path created from the sanitized trusted request title,
the expected application title, and the request author when one was supplied.
This matters because production Audiobookshelf gives `folderStructure` first
metadata precedence. WyseARR does not store the required token or library ID. An
operator can verify the complete boundary with a session-only token and explicit
values using the documented [Audiobookshelf API](https://api.audiobookshelf.org/):

```bash
ABS_URL=http://192.168.4.46:13378
ABS_LIBRARY_ID='<audiobook-library-id>'
ABS_LIBRARY_ROOT='<Audiobookshelf-side root, without trailing slash>'
EXPECTED_REL_PATH='<exact sanitized one-level folder name>'
EXPECTED_TITLE='<exact title Audiobookshelf must expose>'
EXPECTED_AUTHOR='<exact request author, or blank if none was supplied>'
read -rsp 'Audiobookshelf API token: ' ABS_API_TOKEN; echo
curl -fsS -H "Authorization: Bearer ${ABS_API_TOKEN}" \
  "${ABS_URL}/api/libraries/${ABS_LIBRARY_ID}" | \
  jq -e --arg root "$ABS_LIBRARY_ROOT" '
    .mediaType == "book" and
    ([.folders[]? | select(.fullPath == $root)] | length == 1)
  '
curl -fsSG -H "Authorization: Bearer ${ABS_API_TOKEN}" \
  --data-urlencode "q=${EXPECTED_TITLE}" \
  "${ABS_URL}/api/libraries/${ABS_LIBRARY_ID}/search" | \
  jq -e \
    --arg library "$ABS_LIBRARY_ID" \
    --arg root "$ABS_LIBRARY_ROOT" \
    --arg rel "$EXPECTED_REL_PATH" \
    --arg title "$EXPECTED_TITLE" \
    --arg author "$EXPECTED_AUTHOR" '
      [
        .book[]?.libraryItem
        | select(
            ($rel | contains("/") | not) and
            .libraryId == $library and
            .mediaType == "book" and
            .isMissing == false and
            .isInvalid == false and
            .relPath == $rel and
            .path == ($root + "/" + $rel) and
            .media.metadata.title == $title and
            (
              $author == "" or
              .media.metadata.authorName == $author or
              ([.media.metadata.authors[]? | .name] | index($author)) != null
            )
          )
      ]
      | if length == 1 then
          .[0] | {
            id,
            path,
            relPath,
            title: .media.metadata.title,
            authorName: .media.metadata.authorName,
            authors: .media.metadata.authors
          }
        else
          error("expected exactly one healthy item with the exact path and metadata")
        end
    '
unset ABS_API_TOKEN
```

`ABS_LIBRARY_ROOT` is the path visible inside Audiobookshelf, not necessarily the
WyseARR host path. Confirm out of band that this one library folder maps to
`/mnt/media/audiobooks`. The first `jq -e` requires that exact root. The second
requires exactly one healthy book at the exact one-level relative path, with the
exact application title and, when specified, author. A source OPF/NFO is
authoritative input that BookBot preserves, so validate its intended application
metadata deliberately rather than expecting a generated `metadata.opf`. Do not
put the token in `.env`, documentation, shell history, or validator output unless
a separately reviewed least-privilege integration is added.

No secrets are committed. `.env`, service configuration, request databases, and
private runtime checkpoints remain local and ignored by Git.
