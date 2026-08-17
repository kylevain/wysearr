"""Durable, fail-closed physical DVD/Blu-ray intake for Radarr."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    from .clients import RadarrClient, ServiceError
    from .database import RequestStore
    from .notifications import physical_media_notification
except ImportError:  # pragma: no cover - direct container entrypoint
    from clients import RadarrClient, ServiceError
    from database import RequestStore
    from notifications import physical_media_notification


_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_IMDB = re.compile(r"\Att[0-9]{7,10}\Z")
_SAFE_TITLE = re.compile(r"\A[^\x00-\x1f/\\]{1,160}\Z")
_TERMINAL_STATES = frozenset({"completed", "manual_review", "failed"})
_ACTIVE_STATES = (
    "received",
    "validated",
    "identity_resolved",
    "import_submitting",
    "importing",
)


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
    if not isinstance(raw, Mapping) or raw.get("version") != 1:
        raise PhysicalMediaError("delivery manifest version is unsupported")

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
        digest = hashlib.sha256()
        stream.seek(0)
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
    }


class PhysicalRadarrClient(RadarrClient):
    """Radarr operations used by the trusted physical-media state machine."""

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
            and str(candidate.get("title") or "").casefold() == str(identity["title"]).casefold()
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


class PhysicalMediaIntake:
    """Advance trusted deliveries one durable, replay-safe transition at a time."""

    def __init__(
        self,
        store: RequestStore,
        radarr: PhysicalRadarrClient,
        intake_root: str | Path,
        *,
        radarr_intake_root: str = "/downloads/physical-media/incoming",
        media_root: str | Path = "/media",
        min_size_bytes: int = 50 * 1024 * 1024,
    ):
        self.store = store
        self.radarr = radarr
        self.intake_root = Path(intake_root)
        self.radarr_intake_root = Path(radarr_intake_root)
        self.media_root = Path(media_root)
        self.min_size_bytes = int(min_size_bytes)

    def _radarr_source_path(self, host_path: str) -> str:
        relative = Path(host_path).relative_to(self.intake_root)
        return str(self.radarr_intake_root / relative)

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
        return str(path)

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
            )
            if created:
                self.store.transition_trusted_library_event(int(event["id"]), "validated")
                discovered += 1
        return discovered

    def reconcile(self) -> int:
        changed = self.discover()
        for event in self.store.trusted_library_events(states=_ACTIVE_STATES):
            try:
                final_path = self._final_file(event)
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
                if state in {"received", "validated"}:
                    candidate = self.radarr.resolve_movie(event)
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
                    self.store.transition_trusted_library_event(
                        int(event["id"]), "import_submitting"
                    )
                    command_id = self.radarr.start_manual_import(
                        movie_id=int(event["radarr_movie_id"]),
                        source_path=self._radarr_source_path(str(event["source_path"])),
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
                    command_state = self.radarr.command_state(int(event["radarr_command_id"]))
                    if command_state in {"failed", "aborted", "cancelled"}:
                        self._attention(event, "Radarr reported that physical-media import failed.", failed=True)
                        changed += 1
                    elif command_state == "completed":
                        self._attention(
                            event,
                            "Radarr completed the import command but no readable final DAS file was found.",
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
