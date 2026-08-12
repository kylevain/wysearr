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
then searches an existing unmonitored item. For direct media, an exact torrent
already in the expected category is correlated rather than added again. An
imported-category record is not called complete until BookBot verifies its safe
import ledger; another category requires administrator review.

Huey immediately records the message and acknowledges it as a reply to the
original Discord message only. The user does not operate qBittorrent or move
files. Accepted, rejected, completed, and failed request states go to
`#request-status`; queued acquisitions and download lifecycle updates go to
`#download-queue`. ARR API failures remain queued for a later reconciliation
pass rather than being guessed terminal.

When an ARR or BookBot confirms a new DAS import, Huey publishes the new item to
`#recent-additions` and the completed request state to `#request-status` as two
distinct events. An import failure sends the failed request state to
`#request-status` and the operator-facing failure or manual-action notice to
`#import-errors`. Runtime and service health issues alone may use
`#system-health`. Lifecycle events are not copied back into request channels.

For movies and TV, completion currently proves that Sonarr or Radarr reports an
imported media file on the DAS. No Plex scan is yet requested or accepted as
part of that state, and visibility in Plex is not established. Scan the matching
Plex library manually until that integration is authorized. For direct media,
completion proves BookBot's validated, atomic copy to the configured DAS path;
it does not prove that the downstream library application has indexed the item.

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

No secrets are committed. `.env`, service configuration, request databases, and
private runtime checkpoints remain local and ignored by Git.
