# WyseARR operator model

This is the short operator view. The authoritative component and data-flow
description is [architecture.md](architecture.md).

## Normal use

Users request movies, TV, ebooks, audiobooks, manga/comics, ROMs, and sheet
music in the corresponding Discord channels. Only the combined movies/TV
channel needs a type prefix:

```text
movie: The Third Man
tv: Severance
```

Ebook and audiobook requests may add an author with `by`. Other direct-media
requests may include an edition, platform, instrument, or format in the title
when needed to disambiguate a release.

Huey immediately records the message and reports whether it was queued, needs a
more specific request, or failed. The user does not operate qBittorrent or move
files. When an ARR or BookBot confirms an import, or BookBot rejects one, Huey
posts one terminal notification by the available original-message or
request-status route. ARR API failures remain queued for a later reconciliation
pass rather than being guessed terminal.

Music and adult-library requests currently use their Lidarr and Whisparr Web
UIs because no Discord channels are assigned to those media types.

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
