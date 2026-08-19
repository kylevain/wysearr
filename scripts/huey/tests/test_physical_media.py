import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


HUEY_DIR = Path(__file__).resolve().parents[1]
if str(HUEY_DIR) not in sys.path:
    sys.path.insert(0, str(HUEY_DIR))

from database import RequestStore
from physical_media import (
    PhysicalMediaError,
    PhysicalMediaIntake,
    PhysicalRadarrClient,
    PhysicalSonarrClient,
    load_delivery_manifest,
)
from huey import reconcile_notifications


class FakeRadarr:
    def __init__(self):
        self.imported = None
        self.command = "started"
        self.start_calls = []
        self.resolve_calls = []

    def resolve_movie(self, identity):
        self.resolve_calls.append(dict(identity))
        return {
            "id": 44,
            "title": identity["title"],
            "year": identity["year"],
            "imdbId": identity.get("imdb_id"),
            "tmdbId": identity.get("tmdb_id") or 11970,
        }

    def ensure_movie(self, candidate):
        return {**candidate, "id": 44, "hasFile": False}

    def validate_physical_identity(self, identity, candidate, evidence=None):
        self.last_identity_evidence = dict(evidence or {})

    def start_manual_import(self, **values):
        self.start_calls.append(values)
        return 81

    def command_state(self, command_id):
        self.last_command_id = command_id
        return self.command

    def imported_file(self, movie_id):
        self.last_movie_id = movie_id
        return self.imported


class PreviewRadarr(PhysicalRadarrClient):
    def __init__(self, preview):
        self.preview = preview
        self.calls = []

    def _api(self, endpoint):
        return endpoint

    def _request(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs))
        if method == "GET" and endpoint == "manualimport":
            return self.preview
        if method == "POST" and endpoint == "command":
            return {"id": 81}
        raise AssertionError(f"unexpected request: {method} {endpoint}")


class ExistingMovieRadarr(PhysicalRadarrClient):
    def __init__(self, movies):
        self.movies = movies
        self.calls = []

    def _api(self, endpoint):
        return endpoint

    def _request(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs))
        if method == "GET" and endpoint == "movie":
            return self.movies
        raise AssertionError(f"unexpected request: {method} {endpoint}")


class CollisionRadarr(PhysicalRadarrClient):
    def __init__(self):
        self.ensure_calls = []
        self.start_calls = []

    def resolve_movie(self, identity):
        return {
            "id": 2014,
            "title": "Into the Woods",
            "year": 2014,
            "imdbId": "tt2180411",
            "tmdbId": 224141,
            "runtime": 125,
        }

    def lookup(self, title):
        self.lookup_title = title
        return [
            {
                "id": 2014,
                "title": "Into the Woods",
                "year": 2014,
                "imdbId": "tt2180411",
                "tmdbId": 224141,
                "runtime": 125,
            },
            {
                "id": 1991,
                "title": "Into the Woods",
                "year": 1991,
                "imdbId": "tt0099851",
                "tmdbId": 23378,
                "runtime": 153,
            },
        ]

    def ensure_movie(self, candidate):
        self.ensure_calls.append(candidate)
        return {**candidate, "id": 2014, "hasFile": False}

    def start_manual_import(self, **values):
        self.start_calls.append(values)
        return 81


class FakeSonarr:
    def __init__(self):
        self.imported = False
        self.command = "started"
        self.start_calls = []

    def resolve_series(self, identity):
        return {"id": 55, "title": identity["title"], "tvdbId": 1234}

    def ensure_series(self, candidate):
        return {**candidate, "id": 55, "statistics": {"episodeFileCount": 0}}

    def episodes_for_season(self, series_id, season):
        return [
            {"id": 101, "episodeNumber": 1, "episodeFileId": 1001 if self.imported else 0, "episodeFile": {"path": "/media/tv/Upload/Season 01/Upload - S01E01.mkv"}},
            {"id": 102, "episodeNumber": 2, "episodeFileId": 1002 if self.imported else 0, "episodeFile": {"path": "/media/tv/Upload/Season 01/Upload - S01E02.mkv"}},
            {"id": 103, "episodeNumber": 3, "episodeFileId": 0},
        ]

    def start_manual_import(self, **values):
        self.start_calls.append(values)
        return 91

    def command_state(self, command_id):
        self.last_command_id = command_id
        return self.command

    def imported_episode_paths(self, series_id, season, episodes):
        if not self.imported:
            return None
        return [
            f"/media/tv/Upload/Season 01/Upload - S01E{episode:02d}.mkv"
            for episode in episodes
        ]


class PreviewSonarr(PhysicalSonarrClient):
    def __init__(self, preview):
        self.preview = preview
        self.calls = []

    def _api(self, endpoint):
        return endpoint

    def _request(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs))
        if method == "GET" and endpoint == "manualimport":
            return self.preview
        if method == "POST" and endpoint == "command":
            return {"id": 91}
        raise AssertionError(f"unexpected request: {method} {endpoint}")


class PhysicalMediaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.intake_root = self.root / "physical" / "incoming"
        self.media_root = self.root / "media"
        self.intake_root.mkdir(parents=True)
        self.media_root.mkdir()
        self.store = RequestStore(self.root / "huey.db")
        self.store.initialize()
        self.radarr = FakeRadarr()
        self.intake = PhysicalMediaIntake(
            self.store,
            self.radarr,
            self.intake_root,
            sonarr=FakeSonarr(),
            media_root=self.media_root,
            min_size_bytes=4,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def delivery(self, *, title="Into the Woods", year=2014, valid=True, extra=None):
        folder = self.intake_root / "arm-test"
        folder.mkdir(exist_ok=True)
        media = folder / "feature.mkv"
        payload = b"\x1aE\xdf\xa3" + b"physical-media-test"
        media.write_bytes(payload)
        manifest = {
            "version": 1,
            "source": "arm",
            "file": media.name,
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "title": title,
            "year": year,
            "imdb_id": "tt2180411",
        }
        if extra:
            manifest.update(extra)
        if not valid:
            manifest.pop("year")
        path = folder / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path, manifest

    def tv_delivery(self):
        folder = self.intake_root / "arm-tv"
        folder.mkdir(exist_ok=True)
        files = []
        for episode in (1, 2):
            media = folder / f"title{episode:02d}.mkv"
            payload = b"\x1aE\xdf\xa3" + f"episode-{episode}".encode()
            media.write_bytes(payload)
            files.append({
                "file": media.name,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "duration_seconds": 1500,
                "track_number": episode,
                "season": 1,
                "episode": episode,
                "episode_title": f"Episode {episode}",
                "kind": "episode",
            })
        manifest = {
            "version": 2,
            "source": "arm",
            "media_type": "tv",
            "series_title": "Upload",
            "season": 1,
            "disc_label": "UPLOAD_S1_D1",
            "dvd_crc64": "0123456789abcdef",
            "files": files,
        }
        path = folder / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path, manifest

    def event(self):
        events = self.store.trusted_library_events()
        self.assertEqual(len(events), 1)
        return events[0]

    def test_manifest_validates_identity_mkv_size_and_fingerprint(self):
        path, manifest = self.delivery(extra={
            "disc_label": "INTO_THE_WOODS",
            "dvd_crc64": "889f7ee9e88191f7",
            "duration_seconds": 9048,
            "arm_job_id": 7,
            "arm_title": "Into-the-Woods",
            "arm_year": 2014,
            "arm_imdb_id": "tt2180411",
        })
        parsed = load_delivery_manifest(path, min_size_bytes=4)
        self.assertEqual(parsed["title"], "Into the Woods")
        self.assertEqual(parsed["year"], 2014)
        self.assertEqual(parsed["fingerprint"], manifest["sha256"])
        self.assertEqual(parsed["disc_label"], "INTO_THE_WOODS")
        self.assertEqual(parsed["dvd_crc64"], "889f7ee9e88191f7")
        self.assertEqual(parsed["duration_seconds"], 9048)
        (path.parent / "feature.mkv").write_bytes(b"not-an-mkv")
        with self.assertRaises(PhysicalMediaError):
            load_delivery_manifest(path, min_size_bytes=4)

    def test_trusted_event_persists_without_a_fake_request(self):
        self.delivery()
        self.assertEqual(self.intake.discover(), 1)
        event = self.event()
        self.assertEqual(event["state"], "validated")
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM requests").fetchone()[0], 0)

    def test_identity_and_radarr_import_are_correlated_to_exact_file(self):
        _path, manifest = self.delivery()
        self.intake.reconcile()  # discover + resolve
        self.intake.reconcile()  # reserve POST boundary + invoke
        event = self.event()
        self.assertEqual(event["state"], "importing")
        self.assertEqual(event["radarr_movie_id"], 44)
        self.assertEqual(event["radarr_command_id"], 81)
        self.assertEqual(len(self.radarr.start_calls), 1)
        call = self.radarr.start_calls[0]
        self.assertEqual(call["fingerprint"], manifest["sha256"])
        self.assertEqual(
            call["source_path"],
            "/downloads/physical-media/incoming/arm-test/Into the Woods (2014).mkv",
        )
        self.assertFalse((self.intake_root / "arm-test" / "feature.mkv").exists())
        self.assertTrue((self.intake_root / "arm-test" / "Into the Woods (2014).mkv").is_file())
        rewritten = json.loads((self.intake_root / "arm-test" / "manifest.json").read_text())
        self.assertEqual(rewritten["file"], "Into the Woods (2014).mkv")

    def test_generic_physical_filename_is_made_deterministic_before_preview(self):
        _path, manifest = self.delivery(title="Greedy", year=1994)
        self.intake.reconcile()
        self.intake.reconcile()
        event = self.event()
        self.assertEqual(event["state"], "importing")
        self.assertEqual(
            self.radarr.start_calls[0]["source_path"],
            "/downloads/physical-media/incoming/arm-test/Greedy (1994).mkv",
        )
        deterministic = self.intake_root / "arm-test" / "Greedy (1994).mkv"
        self.assertEqual(hashlib.sha256(deterministic.read_bytes()).hexdigest(), manifest["sha256"])

    def test_same_title_runtime_collision_fails_closed_before_radarr_mutation(self):
        self.delivery(extra={
            "disc_label": "INTO_THE_WOODS",
            "dvd_crc64": "889f7ee9e88191f7",
            "duration_seconds": 9048,
            "arm_job_id": 7,
            "arm_title": "Into-the-Woods",
            "arm_year": 2014,
            "arm_imdb_id": "tt2180411",
        })
        self.intake.radarr = CollisionRadarr()

        self.intake.reconcile()

        event = self.event()
        self.assertEqual(event["state"], "manual_review")
        self.assertIn("Physical-disc identity collision", event["error"])
        self.assertIn("main feature 151 min", event["error"])
        self.assertIn("Into the Woods (1991)", event["error"])
        self.assertEqual(self.intake.radarr.ensure_calls, [])
        self.assertEqual(self.intake.radarr.start_calls, [])
        deliveries = self.store.pending_notification_deliveries()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["route"], "import-errors")

    def test_manual_import_uses_exact_nested_movie_preview_correlation(self):
        source_path = "/downloads/physical-media/incoming/arm-test/movie.mkv"
        radarr = PreviewRadarr([
            {
                "path": source_path,
                "movieId": None,
                "movie": {"id": 44},
                "quality": {"quality": {"id": 8}},
                "languages": [{"id": 1, "name": "English"}],
                "rejections": [],
            }
        ])
        command_id = radarr.start_manual_import(
            movie_id=44,
            source_path=source_path,
            fingerprint="a" * 64,
        )
        self.assertEqual(command_id, 81)
        preview_call, import_call = radarr.calls
        self.assertEqual(preview_call[0:2], ("GET", "manualimport"))
        self.assertEqual(
            preview_call[2]["params"],
            {
                "folder": "/downloads/physical-media/incoming/arm-test",
                "filterExistingFiles": "true",
            },
        )
        self.assertEqual(import_call[0:2], ("POST", "command"))
        self.assertEqual(import_call[2]["json"]["name"], "ManualImport")
        self.assertEqual(import_call[2]["json"]["importMode"], "move")
        self.assertEqual(import_call[2]["json"]["files"][0]["movieId"], 44)
        self.assertEqual(import_call[2]["json"]["files"][0]["path"], source_path)

    def test_manual_import_rejects_mismatched_nested_movie_without_post(self):
        source_path = "/downloads/physical-media/incoming/arm-test/movie.mkv"
        radarr = PreviewRadarr([
            {
                "path": source_path,
                "movieId": None,
                "movie": {"id": 45},
                "rejections": [],
            }
        ])
        with self.assertRaises(PhysicalMediaError):
            radarr.start_manual_import(
                movie_id=44,
                source_path=source_path,
                fingerprint="a" * 64,
            )
        self.assertEqual(len(radarr.calls), 1)

    def test_existing_radarr_movie_is_reused_by_exact_durable_identity(self):
        existing = {
            "id": 38,
            "title": "The Secret of My Success",
            "year": 1987,
            "imdbId": "tt0093936",
            "tmdbId": 10021,
            "hasFile": False,
        }
        radarr = ExistingMovieRadarr([existing])
        movie = radarr.ensure_movie({
            "title": "The Secret of My Success",
            "year": 1987,
            "imdbId": "tt0093936",
            "tmdbId": 10021,
        })
        self.assertEqual(movie, existing)
        self.assertEqual([(call[0], call[1]) for call in radarr.calls], [("GET", "movie")])

    def test_existing_radarr_movie_requires_all_available_ids_to_match(self):
        radarr = ExistingMovieRadarr([
            {
                "id": 38,
                "title": "The Secret of My Success",
                "year": 1987,
                "imdbId": "tt0093936",
                "tmdbId": 99999,
            }
        ])
        with self.assertRaises(AssertionError):
            radarr.ensure_movie({
                "title": "The Secret of My Success",
                "year": 1987,
                "imdbId": "tt0093936",
                "tmdbId": 10021,
            })
        self.assertEqual(len(radarr.calls), 2)

    def test_success_requires_readable_nonempty_das_file_and_notifies_once(self):
        self.delivery()
        self.intake.reconcile()
        self.intake.reconcile()
        final = self.media_root / "movies" / "Into the Woods (2014)" / "movie.mkv"
        final.parent.mkdir(parents=True)
        source = self.intake_root / "arm-test" / "Into the Woods (2014).mkv"
        final.write_bytes(source.read_bytes())
        self.radarr.imported = {"path": "/media/movies/Into the Woods (2014)/movie.mkv"}
        self.intake.reconcile()
        self.intake.reconcile()
        event = self.event()
        self.assertEqual(event["state"], "completed")
        deliveries = self.store.pending_notification_deliveries()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["event_key"], "library_imported")
        self.assertEqual(deliveries[0]["route"], "recent-additions")
        self.assertIsNone(deliveries[0]["request_id"])
        self.assertEqual(deliveries[0]["trusted_event_id"], event["id"])

    def test_replay_suppresses_duplicate_import_and_notification(self):
        self.delivery()
        self.intake.reconcile()
        self.intake.reconcile()
        final = self.media_root / "movies" / "movie.mkv"
        final.parent.mkdir(parents=True)
        final.write_bytes((self.intake_root / "arm-test" / "Into the Woods (2014).mkv").read_bytes())
        self.radarr.imported = {"path": "/media/movies/movie.mkv"}
        self.intake.reconcile()
        for _ in range(4):
            self.intake.reconcile()
        self.assertEqual(len(self.store.trusted_library_events()), 1)
        self.assertEqual(len(self.radarr.start_calls), 1)
        self.assertEqual(len(self.store.pending_notification_deliveries()), 1)

    def test_radarr_move_is_finalized_without_invalid_manifest_quarantine(self):
        manifest_path, _manifest = self.delivery()
        self.intake.reconcile()
        self.intake.reconcile()
        source = self.intake_root / "arm-test" / "Into the Woods (2014).mkv"
        final = self.media_root / "movies" / "Into the Woods (2014)" / "movie.mkv"
        final.parent.mkdir(parents=True)
        source.rename(final)
        self.radarr.imported = {"path": "/media/movies/Into the Woods (2014)/movie.mkv"}

        self.intake.reconcile()

        events = self.store.trusted_library_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["state"], "completed")
        self.assertFalse(manifest_path.exists())
        self.assertFalse(manifest_path.parent.exists())
        deliveries = self.store.pending_notification_deliveries()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["event_key"], "library_imported")

    def test_ambiguous_metadata_routes_once_to_import_errors(self):
        self.delivery(valid=False)
        self.intake.reconcile()
        event = self.event()
        self.assertEqual(event["state"], "manual_review")
        deliveries = self.store.pending_notification_deliveries()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["route"], "import-errors")
        self.assertEqual(deliveries[0]["event_key"], "import_failed")
        self.intake.reconcile()
        self.assertEqual(len(self.store.pending_notification_deliveries()), 1)

    def test_tv_disc_with_explicit_episode_mapping_imports_through_sonarr(self):
        self.tv_delivery()
        sonarr = self.intake.sonarr
        self.intake.reconcile()
        event = self.event()
        self.assertEqual(event["state"], "identity_resolved")
        self.assertEqual(event["media_type"], "tv")
        self.assertEqual(event["sonarr_series_id"], 55)

        self.intake.reconcile()
        event = self.event()
        self.assertEqual(event["state"], "importing")
        self.assertEqual(event["sonarr_command_id"], 91)
        self.assertEqual(len(sonarr.start_calls), 1)
        call = sonarr.start_calls[0]
        self.assertEqual(call["source_dir"], "/downloads/physical-media/incoming/arm-tv")
        self.assertEqual(
            [Path(item["sonarr_path"]).name for item in call["files"]],
            ["Upload - S01E01 - Episode 1.mkv", "Upload - S01E02 - Episode 2.mkv"],
        )

        sonarr.command = "completed"
        sonarr.imported = True
        self.intake.reconcile()
        event = self.event()
        self.assertEqual(event["state"], "completed")
        self.assertIn("S01E01", event["final_path"])
        deliveries = self.store.pending_notification_deliveries()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["route"], "recent-additions")

    def test_tv_episode_mapping_rejects_unknown_episode_without_sonarr_post(self):
        path, manifest = self.tv_delivery()
        manifest["files"][1]["episode"] = 99
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.intake.reconcile()
        event = self.event()
        self.assertEqual(event["state"], "manual_review")
        self.assertIn("episodes Sonarr does not know", event["error"])
        self.assertEqual(self.intake.sonarr.start_calls, [])

    def test_grouped_ambiguous_video_preserves_artifacts_for_review(self):
        folder = self.intake_root / "arm-ambiguous"
        folder.mkdir(exist_ok=True)
        media = folder / "title01.mkv"
        payload = b"\x1aE\xdf\xa3" + b"ambiguous"
        media.write_bytes(payload)
        manifest = {
            "version": 2,
            "source": "arm",
            "media_type": "ambiguous",
            "title": "Mystery Disc",
            "files": [{
                "file": media.name,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "duration_seconds": 1200,
            }],
        }
        (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.intake.reconcile()
        event = self.event()
        self.assertEqual(event["state"], "manual_review")
        self.assertEqual(event["media_type"], "ambiguous")
        self.assertTrue(media.exists())

    def test_yearless_unidentified_arm_delivery_becomes_ambiguous_video_review(self):
        path, manifest = self.delivery(title="not identified", year=2014)
        manifest.pop("year")
        manifest.pop("imdb_id")
        manifest.update({
            "disc_label": "not identified",
            "dvd_crc64": "07e812f33f894c6b",
            "duration_seconds": 7722,
            "arm_job_id": 9,
            "arm_title": "not identified",
        })
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.intake.reconcile()
        event = self.event()
        self.assertEqual(event["state"], "manual_review")
        self.assertEqual(event["media_type"], "ambiguous")
        self.assertEqual(event["source_fingerprint"], manifest["sha256"])
        self.assertIn("Physical video is preserved", event["error"])
        self.assertTrue((path.parent / "feature.mkv").exists())

    def test_sonarr_manual_import_uses_exact_episode_preview_paths(self):
        source = "/downloads/physical-media/incoming/arm-tv/Upload - S01E01.mkv"
        sonarr = PreviewSonarr([
            {
                "path": source,
                "seriesId": 55,
                "episodeIds": [101],
                "quality": {"quality": {"id": 8}},
                "languages": [{"id": 1, "name": "English"}],
                "rejections": [],
            }
        ])
        command_id = sonarr.start_manual_import(
            series_id=55,
            source_dir="/downloads/physical-media/incoming/arm-tv",
            files=[{"sonarr_path": source}],
            fingerprint="b" * 64,
        )
        self.assertEqual(command_id, 91)
        self.assertEqual(sonarr.calls[1][2]["json"]["files"][0]["episodeIds"], [101])

    def test_sonarr_manual_import_rejects_preview_without_episode(self):
        source = "/downloads/physical-media/incoming/arm-tv/Upload - S01E01.mkv"
        sonarr = PreviewSonarr([{"path": source, "seriesId": 55, "episodeIds": [], "rejections": []}])
        with self.assertRaises(PhysicalMediaError):
            sonarr.start_manual_import(
                series_id=55,
                source_dir="/downloads/physical-media/incoming/arm-tv",
                files=[{"sonarr_path": source}],
                fingerprint="b" * 64,
            )
        self.assertEqual(len(sonarr.calls), 1)

    def test_radarr_failure_routes_to_import_errors(self):
        self.delivery()
        self.intake.reconcile()
        self.intake.reconcile()
        self.radarr.command = "failed"
        self.intake.reconcile()
        self.assertEqual(self.event()["state"], "failed")
        self.assertEqual(self.store.pending_notification_deliveries()[0]["route"], "import-errors")

    def test_restart_at_uncertain_post_boundary_fails_closed_without_repost(self):
        self.delivery()
        self.intake.reconcile()
        event = self.event()
        self.store.transition_trusted_library_event(event["id"], "import_submitting")
        restarted = PhysicalMediaIntake(
            RequestStore(self.root / "huey.db"),
            self.radarr,
            self.intake_root,
            media_root=self.media_root,
            min_size_bytes=4,
        )
        restarted.reconcile()
        self.assertEqual(self.event()["state"], "manual_review")
        self.assertEqual(self.radarr.start_calls, [])
        self.assertEqual(self.store.pending_notification_deliveries()[0]["route"], "import-errors")

    def test_legacy_request_outbox_migrates_without_loss(self):
        legacy = self.root / "legacy.db"
        RequestStore(legacy).initialize()
        with sqlite3.connect(legacy) as connection:
            connection.executescript(
                """
                DROP INDEX notification_deliveries_pending_idx;
                DROP INDEX notification_deliveries_request_uq;
                DROP INDEX notification_deliveries_trusted_event_uq;
                DROP TABLE notification_deliveries;
                CREATE TABLE notification_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER NOT NULL,
                    event_key TEXT NOT NULL,
                    route TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    delivered_at TEXT,
                    FOREIGN KEY(request_id) REFERENCES requests(id) ON DELETE CASCADE,
                    UNIQUE(request_id, event_key, route)
                );
                CREATE INDEX notification_deliveries_pending_idx
                    ON notification_deliveries(delivered_at, id);
                """
            )
        migrated = RequestStore(legacy)
        migrated.initialize()
        with migrated.connect() as connection:
            columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(notification_deliveries)")}
        self.assertIn("trusted_event_id", columns)
        self.assertEqual(columns["request_id"]["notnull"], 0)


class TrustedNotificationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_huey_delivery_sends_trusted_addition_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RequestStore(Path(directory) / "huey.db")
            store.initialize()
            event, _created = store.register_trusted_library_event(
                source_fingerprint="a" * 64,
                source_path="/physical-media/incoming/test/feature.mkv",
                size_bytes=100,
                title="Arrival",
                year=2016,
            )
            store.enqueue_trusted_notification(
                event["id"],
                "library_imported",
                "recent-additions",
                "physical addition",
            )

            class Channel:
                def __init__(self):
                    self.sent = []

                async def send(self, message):
                    self.sent.append(message)

            channel = Channel()

            class Client:
                def get_channel(self, channel_id):
                    return channel if channel_id == 22 else None

                async def fetch_channel(self, channel_id):
                    return None

            config = type(
                "Config",
                (),
                {"lifecycle_channels": {"recent-additions": "22"}},
            )()
            client = Client()

            async def direct_call(function, *args, **kwargs):
                return function(*args, **kwargs)

            with patch("huey.asyncio.to_thread", new=direct_call):
                await reconcile_notifications(client, config, store)
                await reconcile_notifications(client, config, store)
            self.assertEqual(channel.sent, ["physical addition"])
            self.assertEqual(store.pending_notification_deliveries(), [])


if __name__ == "__main__":
    unittest.main()
