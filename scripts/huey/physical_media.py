"""Durable, fail-closed physical DVD/Blu-ray intake for Radarr and Sonarr."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    from .clients import RadarrClient, ServiceError, SonarrClient
    from .database import RequestStore
    from .notifications import physical_media_notification
except ImportError:  # pragma: no cover - direct container entrypoint
    from clients import RadarrClient, ServiceError, SonarrClient
    from database import RequestStore
    from notifications import physical_media_notification


_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_IMDB = re.compile(r"\Att[0-9]{7,10}\Z")
_CRC64 = re.compile(r"\A[0-9a-f]{16}\Z")
_SAFE_TITLE = re.compile(r"\A[^\x00-\x1f/\\]{1,160}\Z")
_SAFE_FILENAME_CHAR = re.compile(r"[^A-Za-z0-9 ._()'&!+-]+")
_TERMINAL_STATES = frozenset({"completed", "manual_review", "failed"})
_ACTIVE_STATES = (
    "received",
    "validated",
    "identity_resolved",
    "import_submitting",
    "importing",
)
_MEDIA_TYPES = frozenset({"movie", "tv", "nonstandard", "ambiguous"})


class PhysicalMediaError(ValueError):
    """A delivery is unsafe or ambiguous and needs operator review."""


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise PhysicalMediaError(f"{label} is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise PhysicalMediaError(f"{label} is missing or invalid") from error
    if not minimum <= parsed <= maximum:
        raise PhysicalMediaError(f"{label} is outside the accepted range")
    return parsed


def _clean_title(value: object) -> str:
    title = " ".join(str(value or "").split())
    if not _SAFE_TITLE.fullmatch(title) or any(marker in title for marker in ("@", "`", "<", ">")):
        raise PhysicalMediaError("movie title is missing or unsafe")
    return title


def _title_key(value: object) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", str(value or "").casefold()).split())


def _optional_safe_text(value: object, *, label: str, limit: int = 160) -> str | None:
    if value in (None, ""):
        return None
    text = " ".join(str(value or "").split())
    if (
        not text
        or len(text) > limit
        or any(marker in text for marker in ("\x00", "/", "\\", "@", "`", "<", ">"))
    ):
        raise PhysicalMediaError(f"{label} is unsafe")
    return text


def _optional_integer(
    value: object, *, label: str, minimum: int, maximum: int
) -> int | None:
    if value in (None, ""):
        return None
    return _integer(value, label, minimum, maximum)


def _deterministic_mkv_name(title: object, year: object) -> str:
    clean = _clean_title(title)
    parsed_year = _integer(year, "movie year", 1878, 2200)
    safe_title = _SAFE_FILENAME_CHAR.sub(" ", clean)
    safe_title = re.sub(r"\s+", " ", safe_title).strip(" .")
    if not safe_title:
        raise PhysicalMediaError("movie title is unsafe for import filename")
    return f"{safe_title} ({parsed_year}).mkv"


def _safe_file_basename(value: object) -> str:
    name = str(value or "")
    if (
        not name
        or Path(name).name != name
        or Path(name).suffix.casefold() != ".mkv"
        or any(marker in name for marker in ("\x00", "/", "\\"))
    ):
        raise PhysicalMediaError("manifest must name MKV basenames")
    return name


def _safe_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if not 2 <= len(encoded) <= 65536:
        raise PhysicalMediaError("physical-media evidence is too large")
    return json.loads(encoded)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_delivery_manifest(
    manifest_path: str | Path, *, min_size_bytes: int = 50 * 1024 * 1024
) -> dict[str, Any]:
    """Validate metadata, containment, MKV framing, size, and full SHA-256."""

    path = Path(manifest_path)
    if path.name != "manifest.json" or path.is_symlink() or not path.is_file():
        raise PhysicalMediaError("delivery manifest is not a regular manifest.json")
    if path.stat().st_size > 64 * 1024:
        raise PhysicalMediaError("delivery manifest is too large")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PhysicalMediaError("delivery manifest is not valid UTF-8 JSON") from error
    if not isinstance(raw, Mapping) or raw.get("version") not in {1, 2}:
        raise PhysicalMediaError("delivery manifest version is unsupported")
    media_type = str(raw.get("media_type") or "movie").casefold()
    if media_type not in _MEDIA_TYPES:
        raise PhysicalMediaError("physical-media type is unsupported")

    if media_type != "movie":
        return load_grouped_delivery_manifest(path, raw, min_size_bytes=min_size_bytes)

    omdb = raw.get("omdb") if isinstance(raw.get("omdb"), Mapping) else {}
    title = _clean_title(raw.get("title") or omdb.get("Title"))
    year_text = str(raw.get("year") or omdb.get("Year") or "")
    year_match = re.match(r"\A([0-9]{4})", year_text)
    if not year_match:
        raise PhysicalMediaError("movie year is missing or ambiguous")
    year = _integer(year_match.group(1), "movie year", 1878, 2200)
    imdb_id = str(raw.get("imdb_id") or omdb.get("imdbID") or "").strip() or None
    if imdb_id is not None and not _IMDB.fullmatch(imdb_id):
        raise PhysicalMediaError("IMDb identity is invalid")
    tmdb_id = raw.get("tmdb_id")
    if tmdb_id not in (None, ""):
        tmdb_id = _integer(tmdb_id, "TMDb identity", 1, 2_147_483_647)
    else:
        tmdb_id = None
    duration_seconds = _optional_integer(
        raw.get("duration_seconds") or raw.get("main_feature_seconds"),
        label="main-feature duration",
        minimum=60,
        maximum=24 * 60 * 60,
    )
    dvd_crc64 = str(raw.get("dvd_crc64") or "").casefold() or None
    if dvd_crc64 is not None and not _CRC64.fullmatch(dvd_crc64):
        raise PhysicalMediaError("DVD CRC64 fingerprint is invalid")
    arm_job_id = _optional_integer(
        raw.get("arm_job_id"), label="ARM job id", minimum=1, maximum=10**12
    )
    disc_label = _optional_safe_text(raw.get("disc_label"), label="disc label", limit=120)
    arm_title = _optional_safe_text(raw.get("arm_title"), label="ARM title", limit=160)
    arm_year = _optional_integer(
        raw.get("arm_year"), label="ARM year", minimum=1878, maximum=2200
    )
    arm_imdb_id = str(raw.get("arm_imdb_id") or "").strip() or None
    if arm_imdb_id is not None and not _IMDB.fullmatch(arm_imdb_id):
        raise PhysicalMediaError("ARM IMDb identity is invalid")

    file_name = str(raw.get("file") or "")
    if (
        not file_name
        or Path(file_name).name != file_name
        or Path(file_name).suffix.casefold() != ".mkv"
    ):
        raise PhysicalMediaError("manifest must name one MKV basename")
    media_path = path.parent / file_name
    if media_path.is_symlink() or not media_path.is_file():
        raise PhysicalMediaError("manifest MKV is missing or is not a regular file")
    mkv_files = [item for item in path.parent.iterdir() if item.is_file() and item.suffix.casefold() == ".mkv"]
    if len(mkv_files) != 1 or mkv_files[0].name != file_name:
        raise PhysicalMediaError("delivery must contain exactly one main-feature MKV")

    expected_size = _integer(raw.get("size_bytes"), "MKV size", min_size_bytes, 10**13)
    actual_size = media_path.stat().st_size
    if actual_size != expected_size:
        raise PhysicalMediaError("MKV size does not match the completed delivery manifest")
    with media_path.open("rb") as stream:
        if stream.read(4) != b"\x1aE\xdf\xa3":
            raise PhysicalMediaError("delivered file does not have an MKV/EBML header")
        stream.seek(0)
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    fingerprint = str(raw.get("sha256") or "").casefold()
    if not _SHA256.fullmatch(fingerprint) or digest.hexdigest() != fingerprint:
        raise PhysicalMediaError("MKV SHA-256 does not match the completed delivery manifest")
    return {
        "title": title,
        "year": year,
        "imdb_id": imdb_id,
        "tmdb_id": tmdb_id,
        "fingerprint": fingerprint,
        "size_bytes": actual_size,
        "host_path": str(media_path),
        "manifest_path": str(path),
        "duration_seconds": duration_seconds,
        "dvd_crc64": dvd_crc64,
        "arm_job_id": arm_job_id,
        "disc_label": disc_label,
        "arm_title": arm_title,
        "arm_year": arm_year,
        "arm_imdb_id": arm_imdb_id,
        "media_type": "movie",
        "group_key": fingerprint,
        "metadata": {},
    }


def load_grouped_delivery_manifest(
    path: Path,
    raw: Mapping[str, Any],
    *,
    min_size_bytes: int,
) -> dict[str, Any]:
    """Validate grouped physical-video deliveries without assuming one movie."""

    media_type = str(raw.get("media_type") or "").casefold()
    files = raw.get("files")
    if not isinstance(files, list) or not files:
        raise PhysicalMediaError("grouped physical-media manifest requires files")
    title = _clean_title(raw.get("series_title") or raw.get("title") or "Physical Video")
    year = _optional_integer(raw.get("year"), label="video year", minimum=1878, maximum=2200)
    disc_label = _optional_safe_text(raw.get("disc_label"), label="disc label", limit=120)
    dvd_crc64 = str(raw.get("dvd_crc64") or "").casefold() or None
    if dvd_crc64 is not None and not _CRC64.fullmatch(dvd_crc64):
        raise PhysicalMediaError("DVD CRC64 fingerprint is invalid")
    arm_job_id = _optional_integer(
        raw.get("arm_job_id"), label="ARM job id", minimum=1, maximum=10**12
    )
    total_size = 0
    group_digest = hashlib.sha256()
    normalized_files: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    seen_episodes: set[int] = set()
    for index, item in enumerate(files, start=1):
        if not isinstance(item, Mapping):
            raise PhysicalMediaError("grouped physical-media files must be objects")
        file_name = _safe_file_basename(item.get("file"))
        if file_name in seen_names:
            raise PhysicalMediaError("grouped physical-media file names must be unique")
        seen_names.add(file_name)
        media_path = path.parent / file_name
        if media_path.is_symlink() or not media_path.is_file():
            raise PhysicalMediaError("grouped MKV is missing or is not a regular file")
        expected_size = _integer(
            item.get("size_bytes"), "MKV size", min_size_bytes, 10**13
        )
        actual_size = media_path.stat().st_size
        if actual_size != expected_size:
            raise PhysicalMediaError("grouped MKV size does not match the manifest")
        with media_path.open("rb") as stream:
            if stream.read(4) != b"\x1aE\xdf\xa3":
                raise PhysicalMediaError("grouped file does not have an MKV/EBML header")
        fingerprint = str(item.get("sha256") or "").casefold()
        if not _SHA256.fullmatch(fingerprint) or _sha256_file(media_path) != fingerprint:
            raise PhysicalMediaError("grouped MKV SHA-256 does not match the manifest")
        duration_seconds = _optional_integer(
            item.get("duration_seconds"),
            label="title duration",
            minimum=60,
            maximum=24 * 60 * 60,
        )
        normalized: dict[str, Any] = {
            "file": file_name,
            "sha256": fingerprint,
            "size_bytes": actual_size,
            "duration_seconds": duration_seconds,
            "source_path": str(media_path),
            "track_number": _optional_integer(
                item.get("track_number"), label="track number", minimum=1, maximum=9999
            ) or index,
            "kind": str(item.get("kind") or "episode").casefold(),
        }
        if media_type == "tv":
            episode_number = _integer(
                item.get("episode"), "episode number", minimum=1, maximum=2000
            )
            if episode_number in seen_episodes:
                raise PhysicalMediaError("episode mapping contains duplicate episode numbers")
            seen_episodes.add(episode_number)
            normalized["episode"] = episode_number
            normalized["season"] = _integer(
                item.get("season") or raw.get("season"),
                "season number",
                minimum=0,
                maximum=1000,
            )
            normalized["episode_title"] = _optional_safe_text(
                item.get("episode_title"), label="episode title", limit=200
            )
            if normalized["kind"] != "episode":
                raise PhysicalMediaError("TV automation accepts only explicit episode files")
        total_size += actual_size
        group_digest.update(fingerprint.encode("ascii"))
        group_digest.update(b"\0")
        normalized_files.append(normalized)

    if media_type == "tv":
        season = _integer(raw.get("season"), "season number", minimum=0, maximum=1000)
        if any(int(item["season"]) != season for item in normalized_files):
            raise PhysicalMediaError("episode mapping crosses seasons")
        series_title = _clean_title(raw.get("series_title") or raw.get("title"))
        metadata = _safe_metadata({
            "series_title": series_title,
            "season": season,
            "files": normalized_files,
            "disc_label": disc_label,
            "dvd_crc64": dvd_crc64,
            "arm_job_id": arm_job_id,
        })
        return {
            "title": series_title,
            "year": year,
            "imdb_id": None,
            "tmdb_id": None,
            "fingerprint": group_digest.hexdigest(),
            "size_bytes": total_size,
            "host_path": str(path.parent),
            "manifest_path": str(path),
            "disc_label": disc_label,
            "dvd_crc64": dvd_crc64,
            "arm_job_id": arm_job_id,
            "media_type": "tv",
            "group_key": f"tv:{_title_key(series_title)}:s{season:04d}:{dvd_crc64 or group_digest.hexdigest()[:16]}",
            "metadata": metadata,
        }

    metadata = _safe_metadata({
        "files": normalized_files,
        "disc_label": disc_label,
        "dvd_crc64": dvd_crc64,
        "arm_job_id": arm_job_id,
        "classification_reason": raw.get("classification_reason"),
    })
    return {
        "title": title,
        "year": year,
        "imdb_id": None,
        "tmdb_id": None,
        "fingerprint": group_digest.hexdigest(),
        "size_bytes": total_size,
        "host_path": str(path.parent),
        "manifest_path": str(path),
        "disc_label": disc_label,
        "dvd_crc64": dvd_crc64,
        "arm_job_id": arm_job_id,
        "media_type": media_type,
        "group_key": f"{media_type}:{group_digest.hexdigest()}",
        "metadata": metadata,
    }


class PhysicalRadarrClient(RadarrClient):
    """Radarr operations used by the trusted physical-media state machine."""

    @staticmethod
    def _movie_key(movie: Mapping[str, Any]) -> tuple[int, str]:
        tmdb_id = int(movie.get("tmdbId") or movie.get("tmdb_id") or 0)
        imdb_id = str(movie.get("imdbId") or movie.get("imdb_id") or "")
        return tmdb_id, imdb_id

    @staticmethod
    def _runtime_minutes(movie: Mapping[str, Any]) -> int | None:
        try:
            runtime = int(movie.get("runtime") or 0)
        except (TypeError, ValueError):
            return None
        return runtime if 1 <= runtime <= 1000 else None

    def _same_title_candidates(self, title: object) -> list[Mapping[str, Any]]:
        title_key = _title_key(title)
        if not title_key:
            return []
        seen: set[tuple[int, str]] = set()
        candidates: list[Mapping[str, Any]] = []
        for candidate in self.lookup(str(title)):
            if not isinstance(candidate, Mapping):
                continue
            if _title_key(candidate.get("title")) != title_key:
                continue
            key = self._movie_key(candidate)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
        return candidates

    def validate_physical_identity(
        self,
        identity: Mapping[str, Any],
        candidate: Mapping[str, Any],
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        """Require disc-level corroboration before importing same-title collisions."""

        evidence = evidence or {}
        title = str(candidate.get("title") or identity.get("title") or "")
        same_title = self._same_title_candidates(title)
        if len(same_title) <= 1:
            return

        duration_seconds = evidence.get("duration_seconds")
        try:
            disc_minutes = int(round(int(duration_seconds) / 60)) if duration_seconds else None
        except (TypeError, ValueError):
            disc_minutes = None
        if disc_minutes is None:
            raise PhysicalMediaError(
                "Physical-disc identity collision: multiple same-title Radarr candidates exist "
                "and the delivery has no main-feature duration corroboration."
            )

        candidate_key = self._movie_key(candidate)
        candidate_runtime = self._runtime_minutes(candidate)
        manual_resolution = evidence.get("manual_resolution")
        if (
            isinstance(manual_resolution, Mapping)
            and manual_resolution.get("type") == "movie"
            and (identity.get("tmdb_id") or identity.get("imdb_id"))
            and (
                not identity.get("tmdb_id")
                or int(candidate.get("tmdbId") or 0) == int(identity["tmdb_id"])
            )
            and (
                not identity.get("imdb_id")
                or str(candidate.get("imdbId") or "") == identity["imdb_id"]
            )
        ):
            return

        def runtime_delta(movie: Mapping[str, Any]) -> int | None:
            runtime = self._runtime_minutes(movie)
            return abs(runtime - disc_minutes) if runtime is not None else None

        candidate_delta = runtime_delta(candidate)
        matching_alternates = [
            item
            for item in same_title
            if self._movie_key(item) != candidate_key
            and (delta := runtime_delta(item)) is not None
            and delta <= 15
        ]
        if candidate_delta is not None and candidate_delta <= 15 and not matching_alternates:
            return

        alternate_detail = "; ".join(
            f"{item.get('title')} ({item.get('year')}) "
            f"IMDb {item.get('imdbId') or 'unknown'} TMDb {item.get('tmdbId') or 'unknown'} "
            f"runtime {self._runtime_minutes(item) or 'unknown'} min"
            for item in matching_alternates[:3]
        ) or "no alternate with matching runtime"
        disc_detail = (
            f"disc label {evidence.get('disc_label') or 'unknown'}, "
            f"DVD CRC64 {evidence.get('dvd_crc64') or 'unknown'}, "
            f"main feature {disc_minutes} min"
        )
        candidate_detail = (
            f"{candidate.get('title')} ({candidate.get('year')}) "
            f"IMDb {candidate.get('imdbId') or identity.get('imdb_id') or 'unknown'} "
            f"TMDb {candidate.get('tmdbId') or identity.get('tmdb_id') or 'unknown'} "
            f"runtime {candidate_runtime or 'unknown'} min"
        )
        raise PhysicalMediaError(
            "Physical-disc identity collision: "
            f"{disc_detail}; selected candidate {candidate_detail}; "
            f"same-title alternate evidence: {alternate_detail}. Manual review required."
        )

    def resolve_movie(self, identity: Mapping[str, Any]) -> Mapping[str, Any]:
        if identity.get("tmdb_id"):
            candidate = self._request(
                "GET", self._api("movie/lookup/tmdb"), params={"tmdbId": identity["tmdb_id"]}
            )
            candidates = [candidate] if isinstance(candidate, Mapping) else []
        elif identity.get("imdb_id"):
            candidate = self._request(
                "GET", self._api("movie/lookup/imdb"), params={"imdbId": identity["imdb_id"]}
            )
            candidates = [candidate] if isinstance(candidate, Mapping) else []
        else:
            candidates = list(self.lookup(f"{identity['title']} {identity['year']}"))
        exact = [
            candidate
            for candidate in candidates
            if isinstance(candidate, Mapping)
            and _title_key(candidate.get("title")) == _title_key(identity["title"])
            and int(candidate.get("year") or 0) == int(identity["year"])
            and (
                not identity.get("tmdb_id")
                or int(candidate.get("tmdbId") or 0) == int(identity["tmdb_id"])
            )
            and (
                not identity.get("imdb_id")
                or str(candidate.get("imdbId") or "") == identity["imdb_id"]
            )
        ]
        if len(exact) != 1:
            raise PhysicalMediaError("Radarr did not return one exact title/year identity")
        return exact[0]

    def ensure_movie(self, candidate: Mapping[str, Any]) -> Mapping[str, Any]:
        if candidate.get("id"):
            return self._get_entity(int(candidate["id"]))
        movies = self._request("GET", self._api("movie"))
        if not isinstance(movies, list):
            raise ServiceError("radarr returned an invalid movie library")
        tmdb_id = int(candidate.get("tmdbId") or 0)
        imdb_id = str(candidate.get("imdbId") or "")
        existing = [
            movie
            for movie in movies
            if isinstance(movie, Mapping)
            and bool(tmdb_id or imdb_id)
            and (not tmdb_id or int(movie.get("tmdbId") or 0) == tmdb_id)
            and (not imdb_id or str(movie.get("imdbId") or "") == imdb_id)
            and str(movie.get("title") or "").casefold()
            == str(candidate.get("title") or "").casefold()
            and int(movie.get("year") or 0) == int(candidate.get("year") or 0)
        ]
        if len(existing) == 1:
            return existing[0]
        if len(existing) > 1:
            raise PhysicalMediaError("Radarr has multiple matching movie library entries")
        payload = self._payload(
            candidate, self._discover_root_folder(), self._discover_quality_profile()
        )
        payload["monitored"] = False
        payload["addOptions"] = {"searchForMovie": False}
        added = self._request("POST", self._api("movie"), json=payload)
        if not isinstance(added, Mapping) or not added.get("id"):
            raise ServiceError("radarr did not confirm the physical movie identity")
        return added

    def start_manual_import(
        self, *, movie_id: int, source_path: str, fingerprint: str
    ) -> int:
        candidates = self._request(
            "GET",
            self._api("manualimport"),
            params={
                "folder": str(Path(source_path).parent),
                "filterExistingFiles": "true",
            },
        )
        if not isinstance(candidates, list):
            raise ServiceError("radarr returned an invalid manual-import preview")
        exact = [
            item
            for item in candidates
            if isinstance(item, Mapping)
            and item.get("path") == source_path
            and int(
                item.get("movieId")
                or (
                    item.get("movie", {}).get("id")
                    if isinstance(item.get("movie"), Mapping)
                    else 0
                )
                or 0
            )
            == int(movie_id)
        ]
        if len(exact) != 1 or exact[0].get("rejections"):
            raise PhysicalMediaError("Radarr manual-import preview rejected or could not correlate the MKV")
        preview = exact[0]
        file_payload = {
            key: preview.get(key)
            for key in (
                "path", "movieId", "quality", "languages", "releaseGroup",
                "customFormats", "customFormatScore", "indexerFlags",
            )
            if preview.get(key) is not None
        }
        file_payload["movieId"] = int(movie_id)
        file_payload["downloadId"] = f"physical-disc:{fingerprint}"
        command = self._request(
            "POST",
            self._api("command"),
            json={"name": "ManualImport", "files": [file_payload], "importMode": "move"},
        )
        if not isinstance(command, Mapping) or not command.get("id"):
            raise ServiceError("radarr did not confirm the manual-import command")
        return int(command["id"])

    def command_state(self, command_id: int) -> str:
        command = self._request("GET", self._api(f"command/{int(command_id)}"))
        if not isinstance(command, Mapping):
            raise ServiceError("radarr returned an invalid command state")
        return str(command.get("status") or "").casefold()

    def imported_file(self, movie_id: int) -> Mapping[str, Any] | None:
        movie = self._get_entity(int(movie_id))
        if movie.get("hasFile") is not True:
            return None
        files = self._request("GET", self._api("moviefile"), params={"movieId": int(movie_id)})
        if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], Mapping):
            raise PhysicalMediaError("Radarr reports media but not one final movie file")
        return files[0]


class PhysicalSonarrClient(SonarrClient):
    """Sonarr operations used by the trusted physical-media state machine."""

    def resolve_series(self, identity: Mapping[str, Any]) -> Mapping[str, Any]:
        title = str(identity.get("title") or "")
        candidates = self.lookup(title)
        exact = [
            candidate
            for candidate in candidates
            if isinstance(candidate, Mapping)
            and _title_key(candidate.get("title")) == _title_key(title)
        ]
        if len(exact) != 1:
            raise PhysicalMediaError("Sonarr did not return one exact series identity")
        return exact[0]

    def ensure_series(self, candidate: Mapping[str, Any]) -> Mapping[str, Any]:
        if candidate.get("id"):
            return self._get_entity(int(candidate["id"]))
        series = self._request("GET", self._api("series"))
        if not isinstance(series, list):
            raise ServiceError("sonarr returned an invalid series library")
        tvdb_id = int(candidate.get("tvdbId") or candidate.get("tvdb_id") or 0)
        title_slug = str(candidate.get("titleSlug") or "")
        existing = [
            item
            for item in series
            if isinstance(item, Mapping)
            and (
                (tvdb_id and int(item.get("tvdbId") or 0) == tvdb_id)
                or (title_slug and str(item.get("titleSlug") or "") == title_slug)
            )
            and _title_key(item.get("title")) == _title_key(candidate.get("title"))
        ]
        if len(existing) == 1:
            return existing[0]
        if len(existing) > 1:
            raise PhysicalMediaError("Sonarr has multiple matching series library entries")
        payload = self._payload(
            candidate, self._discover_root_folder(), self._discover_quality_profile()
        )
        payload["monitored"] = False
        payload["seasonFolder"] = True
        payload["addOptions"] = {"searchForMissingEpisodes": False}
        added = self._request("POST", self._api("series"), json=payload)
        if not isinstance(added, Mapping) or not added.get("id"):
            raise ServiceError("sonarr did not confirm the physical series identity")
        return added

    def episodes_for_season(self, series_id: int, season: int) -> list[Mapping[str, Any]]:
        episodes = self._request(
            "GET",
            self._api("episode"),
            params={"seriesId": int(series_id), "seasonNumber": int(season)},
        )
        if not isinstance(episodes, list):
            raise ServiceError("sonarr returned invalid episode metadata")
        return [item for item in episodes if isinstance(item, Mapping)]

    def start_manual_import(
        self,
        *,
        series_id: int,
        source_dir: str,
        files: list[Mapping[str, Any]],
        fingerprint: str,
    ) -> int:
        previews = self._request(
            "GET",
            self._api("manualimport"),
            params={
                "folder": source_dir,
                "filterExistingFiles": "true",
                "seriesId": int(series_id),
            },
        )
        if not isinstance(previews, list):
            raise ServiceError("sonarr returned an invalid manual-import preview")
        by_path = {
            str(item.get("path")): item
            for item in previews
            if isinstance(item, Mapping) and item.get("path")
        }
        payload_files: list[dict[str, Any]] = []
        for file_item in files:
            path = str(file_item["sonarr_path"])
            preview = by_path.get(path)
            if not preview or preview.get("rejections"):
                raise PhysicalMediaError("Sonarr manual-import preview rejected or could not correlate an episode MKV")
            episode_ids = preview.get("episodeIds")
            if not isinstance(episode_ids, list) or len(episode_ids) != 1:
                raise PhysicalMediaError("Sonarr manual-import preview did not resolve one episode")
            file_payload = {
                key: preview.get(key)
                for key in (
                    "path", "seriesId", "episodeIds", "quality", "languages",
                    "releaseGroup", "customFormats", "customFormatScore",
                    "indexerFlags",
                )
                if preview.get(key) is not None
            }
            file_payload["seriesId"] = int(series_id)
            file_payload["downloadId"] = f"physical-disc:{fingerprint}"
            payload_files.append(file_payload)
        command = self._request(
            "POST",
            self._api("command"),
            json={"name": "ManualImport", "files": payload_files, "importMode": "move"},
        )
        if not isinstance(command, Mapping) or not command.get("id"):
            raise ServiceError("sonarr did not confirm the manual-import command")
        return int(command["id"])

    def command_state(self, command_id: int) -> str:
        command = self._request("GET", self._api(f"command/{int(command_id)}"))
        if not isinstance(command, Mapping):
            raise ServiceError("sonarr returned an invalid command state")
        return str(command.get("status") or "").casefold()

    def imported_episode_paths(
        self, series_id: int, season: int, episodes: list[int]
    ) -> list[str] | None:
        expected = set(int(item) for item in episodes)
        records = self.episodes_for_season(series_id, season)
        paths: list[str] = []
        for record in records:
            number = int(record.get("episodeNumber") or 0)
            if number not in expected:
                continue
            file_id = int(record.get("episodeFileId") or 0)
            if file_id <= 0:
                return None
            episode_file = record.get("episodeFile")
            path = (
                episode_file.get("path")
                if isinstance(episode_file, Mapping)
                else record.get("path")
            )
            paths.append(str(path or f"episodeFileId:{file_id}"))
        return paths if len(paths) == len(expected) else None


class PhysicalMediaIntake:
    """Advance trusted deliveries one durable, replay-safe transition at a time."""

    def __init__(
        self,
        store: RequestStore,
        radarr: PhysicalRadarrClient,
        intake_root: str | Path,
        *,
        sonarr: PhysicalSonarrClient | None = None,
        radarr_intake_root: str = "/downloads/physical-media/incoming",
        sonarr_intake_root: str = "/downloads/physical-media/incoming",
        media_root: str | Path = "/media",
        min_size_bytes: int = 50 * 1024 * 1024,
        verify_final_hash: bool = True,
    ):
        self.store = store
        self.radarr = radarr
        self.sonarr = sonarr
        self.intake_root = Path(intake_root)
        self.radarr_intake_root = Path(radarr_intake_root)
        self.sonarr_intake_root = Path(sonarr_intake_root)
        self.media_root = Path(media_root)
        self.min_size_bytes = int(min_size_bytes)
        self.verify_final_hash = bool(verify_final_hash)

    def _radarr_source_path(self, host_path: str) -> str:
        relative = Path(host_path).relative_to(self.intake_root)
        return str(self.radarr_intake_root / relative)

    def _sonarr_source_path(self, host_path: str) -> str:
        relative = Path(host_path).relative_to(self.intake_root)
        return str(self.sonarr_intake_root / relative)

    @staticmethod
    def _manifest_source(manifest_path: Path) -> tuple[str, str] | None:
        """Read only the fields needed to recognize an already-owned delivery."""

        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, Mapping):
            return None
        fingerprint = str(raw.get("sha256") or "").casefold()
        file_name = str(raw.get("file") or "")
        if (
            not _SHA256.fullmatch(fingerprint)
            or not file_name
            or Path(file_name).name != file_name
            or Path(file_name).suffix.casefold() != ".mkv"
        ):
            return None
        return fingerprint, str(manifest_path.parent / file_name)

    def _is_owned_moved_delivery(
        self, manifest_path: Path, events: Mapping[str, Mapping[str, Any]]
    ) -> bool:
        manifest_source = self._manifest_source(manifest_path)
        if manifest_source is None:
            return False
        fingerprint, source_path = manifest_source
        event = events.get(fingerprint)
        return bool(
            event
            and event.get("state") in {"importing", "completed"}
            and event.get("source_path") == source_path
            and not Path(source_path).exists()
        )

    def _cleanup_owned_delivery(self, event: Mapping[str, Any]) -> bool:
        """Remove only the empty, manifest-only staging directory after import."""

        source_path = Path(str(event["source_path"]))
        try:
            source_path.relative_to(self.intake_root)
        except ValueError:
            return False
        directory = source_path.parent
        if directory.parent != self.intake_root or source_path.exists():
            return False
        manifest_path = directory / "manifest.json"
        manifest_source = self._manifest_source(manifest_path)
        if manifest_source != (str(event["source_fingerprint"]), str(source_path)):
            return False
        try:
            if {item.name for item in directory.iterdir()} != {"manifest.json"}:
                return False
            manifest_path.unlink()
            directory.rmdir()
        except OSError:
            return False
        return True

    def _verify_staged_source(self, path: Path, event: Mapping[str, Any]) -> None:
        if path.is_symlink() or not path.is_file():
            raise PhysicalMediaError("trusted physical-media MKV is missing")
        if path.stat().st_size != int(event["size_bytes"]):
            raise PhysicalMediaError("trusted physical-media MKV size changed before import")
        if _sha256_file(path) != str(event["source_fingerprint"]):
            raise PhysicalMediaError("trusted physical-media MKV checksum changed before import")

    def _verify_staged_source_metadata(
        self, path: Path, event: Mapping[str, Any]
    ) -> None:
        if path.is_symlink() or not path.is_file():
            raise PhysicalMediaError("trusted physical-media MKV is missing")
        if path.stat().st_size != int(event["size_bytes"]):
            raise PhysicalMediaError("trusted physical-media MKV size changed before import")
        manifest_source = self._manifest_source(path.parent / "manifest.json")
        if manifest_source != (str(event["source_fingerprint"]), str(path)):
            raise PhysicalMediaError("trusted physical-media manifest no longer matches the staged MKV")

    def _rewrite_manifest_file(self, manifest_path: Path, file_name: str) -> None:
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PhysicalMediaError("delivery manifest is not valid UTF-8 JSON") from error
        if not isinstance(raw, dict):
            raise PhysicalMediaError("delivery manifest version is unsupported")
        raw["file"] = file_name
        temporary = manifest_path.with_name(f"{manifest_path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(raw, sort_keys=True, separators=(",", ": ")) + "\n",
                encoding="utf-8",
            )
            temporary.replace(manifest_path)
        except OSError as error:
            raise PhysicalMediaError("failed to update deterministic import manifest") from error

    def _identity_evidence(self, event: Mapping[str, Any]) -> dict[str, Any]:
        source_path = Path(str(event["source_path"]))
        manifest_path = (
            source_path / "manifest.json"
            if str(event.get("media_type") or "movie") != "movie"
            else source_path.parent / "manifest.json"
        )
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, Mapping):
            return {}
        evidence: dict[str, Any] = {}
        for key in (
            "duration_seconds",
            "main_feature_seconds",
            "dvd_crc64",
            "arm_job_id",
            "disc_label",
            "arm_title",
            "arm_year",
            "arm_imdb_id",
        ):
            if raw.get(key) not in (None, ""):
                evidence[key] = raw[key]
        if isinstance(raw.get("manual_resolution"), Mapping):
            evidence["manual_resolution"] = dict(raw["manual_resolution"])
        if "duration_seconds" not in evidence and "main_feature_seconds" in evidence:
            evidence["duration_seconds"] = evidence["main_feature_seconds"]
        return evidence

    @staticmethod
    def _event_metadata(event: Mapping[str, Any]) -> dict[str, Any]:
        try:
            value = json.loads(str(event.get("metadata_json") or "{}"))
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def _prepare_import_source(self, event: Mapping[str, Any]) -> Path:
        source_path = Path(str(event["source_path"]))
        try:
            source_path.relative_to(self.intake_root)
        except ValueError as error:
            raise PhysicalMediaError(
                "delivery resolves outside the physical-media intake"
            ) from error
        if source_path.parent.is_symlink() or source_path.parent.parent != self.intake_root:
            raise PhysicalMediaError("delivery directory cannot be a symlink")

        desired = source_path.with_name(
            _deterministic_mkv_name(event.get("title"), event.get("year"))
        )
        source = source_path
        if not source.exists():
            matches = [
                candidate
                for candidate in source_path.parent.glob("*.mkv")
                if candidate.is_file()
                and not candidate.is_symlink()
                and candidate.stat().st_size == int(event["size_bytes"])
                and _sha256_file(candidate) == str(event["source_fingerprint"])
            ]
            if len(matches) != 1:
                raise PhysicalMediaError("trusted physical-media MKV is missing")
            source = matches[0]

        self._verify_staged_source_metadata(source, event)
        if source != desired:
            if desired.exists() and desired != source:
                self._verify_staged_source(desired, event)
                source.unlink()
            else:
                source.rename(desired)
            source = desired
        self._rewrite_manifest_file(source.parent / "manifest.json", source.name)
        self.store.update_trusted_library_event_source_path(int(event["id"]), str(source))
        return source

    def _rewrite_grouped_manifest_files(
        self, manifest_path: Path, replacements: Mapping[str, str]
    ) -> None:
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PhysicalMediaError("delivery manifest is not valid UTF-8 JSON") from error
        if not isinstance(raw, dict) or not isinstance(raw.get("files"), list):
            raise PhysicalMediaError("grouped delivery manifest is invalid")
        for item in raw["files"]:
            if isinstance(item, dict) and item.get("file") in replacements:
                item["file"] = replacements[str(item["file"])]
        temporary = manifest_path.with_name(f"{manifest_path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(raw, sort_keys=True, separators=(",", ": ")) + "\n",
                encoding="utf-8",
            )
            temporary.replace(manifest_path)
        except OSError as error:
            raise PhysicalMediaError("failed to update deterministic import manifest") from error

    def _prepare_tv_sources(self, event: Mapping[str, Any]) -> tuple[Path, list[dict[str, Any]]]:
        if self.sonarr is None:
            raise PhysicalMediaError("TV physical-media intake requires Sonarr configuration")
        source_dir = Path(str(event["source_path"]))
        try:
            source_dir.relative_to(self.intake_root)
        except ValueError as error:
            raise PhysicalMediaError("delivery resolves outside the physical-media intake") from error
        if source_dir.is_symlink() or source_dir.parent != self.intake_root:
            raise PhysicalMediaError("TV delivery directory cannot be a symlink")
        metadata = self._event_metadata(event)
        files = metadata.get("files")
        if not isinstance(files, list) or not files:
            raise PhysicalMediaError("TV delivery is missing episode mapping evidence")
        series_title = _clean_title(metadata.get("series_title") or event.get("title"))
        season = _integer(metadata.get("season"), "season number", 0, 1000)
        replacements: dict[str, str] = {}
        prepared: list[dict[str, Any]] = []
        for item in sorted(files, key=lambda value: int(value.get("episode") or 0)):
            if not isinstance(item, Mapping):
                raise PhysicalMediaError("TV episode mapping is invalid")
            episode = _integer(item.get("episode"), "episode number", 1, 2000)
            episode_title = item.get("episode_title")
            suffix = f" - {_SAFE_FILENAME_CHAR.sub(' ', str(episode_title)).strip(' .')}" if episode_title else ""
            desired_name = f"{_SAFE_FILENAME_CHAR.sub(' ', series_title).strip(' .')} - S{season:02d}E{episode:02d}{suffix}.mkv"
            current_name = _safe_file_basename(item.get("file"))
            current_path = source_dir / current_name
            desired_path = source_dir / desired_name
            expected_size = int(item["size_bytes"])
            fingerprint = str(item["sha256"])
            source = current_path if current_path.exists() else desired_path
            if source.is_symlink() or not source.is_file():
                raise PhysicalMediaError("trusted TV episode MKV is missing")
            if source.stat().st_size != expected_size or _sha256_file(source) != fingerprint:
                raise PhysicalMediaError("trusted TV episode MKV changed before import")
            if source != desired_path:
                if desired_path.exists():
                    if desired_path.stat().st_size != expected_size or _sha256_file(desired_path) != fingerprint:
                        raise PhysicalMediaError("deterministic TV filename collides with different media")
                    source.unlink()
                else:
                    source.rename(desired_path)
            replacements[current_name] = desired_name
            prepared.append({
                **dict(item),
                "source_path": str(desired_path),
                "sonarr_path": self._sonarr_source_path(str(desired_path)),
            })
        if replacements:
            self._rewrite_grouped_manifest_files(source_dir / "manifest.json", replacements)
        return source_dir, prepared

    def _validate_tv_episode_mapping(self, event: Mapping[str, Any], series_id: int) -> None:
        if self.sonarr is None:
            raise PhysicalMediaError("TV physical-media intake requires Sonarr configuration")
        metadata = self._event_metadata(event)
        files = metadata.get("files")
        season = _integer(metadata.get("season"), "season number", 0, 1000)
        expected = self.sonarr.episodes_for_season(series_id, season)
        by_number = {
            int(item.get("episodeNumber") or 0): item
            for item in expected
            if int(item.get("episodeNumber") or 0) > 0
        }
        if not isinstance(files, list):
            raise PhysicalMediaError("TV delivery is missing episode mapping evidence")
        missing = [
            int(item.get("episode") or 0)
            for item in files
            if int(item.get("episode") or 0) not in by_number
        ]
        if missing:
            raise PhysicalMediaError("TV episode mapping references episodes Sonarr does not know")

    def _final_file(self, event: Mapping[str, Any]) -> str | None:
        movie_id = event.get("radarr_movie_id")
        if not movie_id:
            return None
        item = self.radarr.imported_file(int(movie_id))
        if item is None:
            return None
        path = Path(str(item.get("path") or ""))
        if not path.is_absolute() or path.parts[:2] != ("/", "media"):
            raise PhysicalMediaError("Radarr final file is outside /media")
        local = self.media_root.joinpath(*path.parts[2:])
        if local.is_symlink() or not local.is_file() or not os.access(local, os.R_OK):
            raise PhysicalMediaError("Radarr final file is not a readable regular DAS file")
        if local.stat().st_size != int(event["size_bytes"]):
            raise PhysicalMediaError("Radarr final DAS file size differs from the validated MKV")
        if self.verify_final_hash and _sha256_file(local) != str(event["source_fingerprint"]):
            raise PhysicalMediaError("Radarr final DAS file checksum differs from the validated MKV")
        return str(path)

    def _final_tv_files(self, event: Mapping[str, Any]) -> str | None:
        if self.sonarr is None or not event.get("sonarr_series_id"):
            return None
        metadata = self._event_metadata(event)
        files = metadata.get("files")
        if not isinstance(files, list) or not files:
            return None
        season = _integer(metadata.get("season"), "season number", 0, 1000)
        episodes = [_integer(item.get("episode"), "episode number", 1, 2000) for item in files if isinstance(item, Mapping)]
        paths = self.sonarr.imported_episode_paths(
            int(event["sonarr_series_id"]), season, episodes
        )
        if not paths:
            return None
        return json.dumps(paths, sort_keys=True, separators=(",", ":"))

    def _attention(self, event: Mapping[str, Any], detail: str, *, failed: bool = False) -> None:
        state = "failed" if failed else "manual_review"
        self.store.transition_trusted_library_event(int(event["id"]), state, error=detail)
        plan = physical_media_notification({**dict(event), "error": detail}, success=False)
        self.store.enqueue_trusted_notification(
            int(event["id"]), plan.event_key, plan.route, plan.message
        )

    def discover(self) -> int:
        discovered = 0
        owned_events = {
            str(event["source_fingerprint"]): event
            for event in self.store.trusted_library_events(limit=1000)
        }
        for manifest_path in sorted(self.intake_root.glob("*/manifest.json")):
            if self._is_owned_moved_delivery(manifest_path, owned_events):
                continue
            manifest_source = self._manifest_source(manifest_path)
            if manifest_source is not None:
                fingerprint, source_path = manifest_source
                event = owned_events.get(fingerprint)
                if event and event.get("source_path") == source_path:
                    continue
            try:
                try:
                    manifest_path.resolve().relative_to(self.intake_root.resolve())
                except ValueError as error:
                    raise PhysicalMediaError(
                        "delivery resolves outside the physical-media intake"
                    ) from error
                if manifest_path.parent.is_symlink():
                    raise PhysicalMediaError("delivery directory cannot be a symlink")
                identity = load_delivery_manifest(
                    manifest_path, min_size_bytes=self.min_size_bytes
                )
            except PhysicalMediaError as error:
                try:
                    manifest_bytes = manifest_path.read_bytes()
                    quarantine_fingerprint = hashlib.sha256(
                        b"invalid-manifest\0" + manifest_bytes
                    ).hexdigest()
                    event, created = self.store.register_trusted_library_event(
                        source_fingerprint=quarantine_fingerprint,
                        source_path=str(manifest_path),
                        size_bytes=max(1, len(manifest_bytes)),
                    )
                    if created:
                        self._attention(event, str(error))
                        discovered += 1
                except OSError:
                    pass
                continue
            event, created = self.store.register_trusted_library_event(
                source_fingerprint=identity["fingerprint"],
                source_path=identity["host_path"],
                size_bytes=identity["size_bytes"],
                title=identity["title"],
                year=identity["year"],
                imdb_id=identity["imdb_id"],
                tmdb_id=identity["tmdb_id"],
                media_type=identity["media_type"],
                group_key=identity["group_key"],
                metadata=identity["metadata"],
            )
            if created:
                self.store.transition_trusted_library_event(int(event["id"]), "validated")
                discovered += 1
        return discovered

    def reconcile(self) -> int:
        changed = self.discover()
        for event in self.store.trusted_library_events(states=_ACTIVE_STATES):
            try:
                media_type = str(event.get("media_type") or "movie")
                final_path = (
                    self._final_tv_files(event)
                    if media_type == "tv"
                    else self._final_file(event)
                    if media_type == "movie"
                    else None
                )
                if final_path:
                    self.store.transition_trusted_library_event(
                        int(event["id"]), "completed", final_path=final_path
                    )
                    plan = physical_media_notification(
                        {**dict(event), "final_path": final_path}, success=True
                    )
                    self.store.enqueue_trusted_notification(
                        int(event["id"]), plan.event_key, plan.route, plan.message
                    )
                    self._cleanup_owned_delivery(event)
                    changed += 1
                    continue

                state = str(event["state"])
                if media_type in {"ambiguous", "nonstandard"}:
                    self._attention(
                        event,
                        "Physical video is preserved and requires manual classification before library placement.",
                    )
                    changed += 1
                    continue
                if state in {"received", "validated"}:
                    if media_type == "tv":
                        if self.sonarr is None:
                            raise PhysicalMediaError("TV physical-media intake requires Sonarr configuration")
                        candidate = self.sonarr.resolve_series(event)
                        series = self.sonarr.ensure_series(candidate)
                        self._validate_tv_episode_mapping(event, int(series["id"]))
                        self.store.transition_trusted_library_event(
                            int(event["id"]),
                            "identity_resolved",
                            title=str(candidate.get("title") or event["title"]),
                            year=int(candidate.get("year") or event.get("year") or 0) or None,
                            sonarr_series_id=int(series["id"]),
                        )
                    else:
                        candidate = self.radarr.resolve_movie(event)
                        self.radarr.validate_physical_identity(
                            event, candidate, self._identity_evidence(event)
                        )
                        movie = self.radarr.ensure_movie(candidate)
                        if movie.get("hasFile") is True:
                            raise PhysicalMediaError(
                                "Radarr already has a different file for this movie"
                            )
                        self.store.transition_trusted_library_event(
                            int(event["id"]),
                            "identity_resolved",
                            title=str(candidate.get("title") or event["title"]),
                            year=int(candidate.get("year") or event["year"]),
                            imdb_id=str(candidate.get("imdbId") or event.get("imdb_id") or "") or None,
                            tmdb_id=int(candidate.get("tmdbId") or event.get("tmdb_id") or 0) or None,
                            radarr_movie_id=int(movie["id"]),
                        )
                    changed += 1
                    continue
                if state == "identity_resolved":
                    if media_type == "tv":
                        if self.sonarr is None:
                            raise PhysicalMediaError("TV physical-media intake requires Sonarr configuration")
                        source_dir, files = self._prepare_tv_sources(event)
                        self.store.transition_trusted_library_event(
                            int(event["id"]), "import_submitting"
                        )
                        command_id = self.sonarr.start_manual_import(
                            series_id=int(event["sonarr_series_id"]),
                            source_dir=self._sonarr_source_path(str(source_dir)),
                            files=files,
                            fingerprint=str(event["source_fingerprint"]),
                        )
                        self.store.transition_trusted_library_event(
                            int(event["id"]), "importing", sonarr_command_id=command_id
                        )
                        changed += 1
                        continue
                    source_path = self._prepare_import_source(event)
                    self.store.transition_trusted_library_event(
                        int(event["id"]), "import_submitting"
                    )
                    command_id = self.radarr.start_manual_import(
                        movie_id=int(event["radarr_movie_id"]),
                        source_path=self._radarr_source_path(str(source_path)),
                        fingerprint=str(event["source_fingerprint"]),
                    )
                    self.store.transition_trusted_library_event(
                        int(event["id"]), "importing", radarr_command_id=command_id
                    )
                    changed += 1
                    continue
                if state == "import_submitting":
                    self._attention(
                        event,
                        "Huey restarted across the Radarr import POST boundary; inspect Radarr before retrying.",
                    )
                    changed += 1
                    continue
                if state == "importing":
                    command_state = (
                        self.sonarr.command_state(int(event["sonarr_command_id"]))
                        if media_type == "tv"
                        else self.radarr.command_state(int(event["radarr_command_id"]))
                    )
                    if command_state in {"failed", "aborted", "cancelled"}:
                        owner = "Sonarr" if media_type == "tv" else "Radarr"
                        self._attention(event, f"{owner} reported that physical-media import failed.", failed=True)
                        changed += 1
                    elif command_state == "completed":
                        final_path = (
                            self._final_tv_files(event)
                            if media_type == "tv"
                            else self._final_file(event)
                        )
                        if final_path:
                            self.store.transition_trusted_library_event(
                                int(event["id"]), "completed", final_path=final_path
                            )
                            plan = physical_media_notification(
                                {**dict(event), "final_path": final_path}, success=True
                            )
                            self.store.enqueue_trusted_notification(
                                int(event["id"]), plan.event_key, plan.route, plan.message
                            )
                            changed += 1
                            continue
                        owner = "Sonarr" if media_type == "tv" else "Radarr"
                        self._attention(
                            event,
                            f"{owner} completed the import command but no readable final DAS file was found.",
                            failed=True,
                        )
                        changed += 1
            except PhysicalMediaError as error:
                self._attention(event, str(error))
                changed += 1
            except ServiceError:
                # Transient API failures remain active and are retried without
                # staging a false import failure or repeating a POST.
                continue
        return changed
