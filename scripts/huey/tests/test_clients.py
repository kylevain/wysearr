import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

from clients import (
    LidarrClient,
    MAX_TORRENT_BYTES,
    ProwlarrClient,
    QBittorrentClient,
    RadarrClient,
    ServiceError,
    SonarrClient,
)


class FakeResponse:
    def __init__(
        self,
        status=200,
        json_value=None,
        text="Ok.",
        content=b"",
        headers=None,
        chunks=None,
    ):
        self.status_code = status
        self._json = json_value
        self.text = text
        self.content = content
        self.headers = dict(headers or {})
        self.chunks = list(chunks) if chunks is not None else [content]
        self.iterated = False
        self.closed = False

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json

    def iter_content(self, chunk_size):
        self.iterated = True
        yield from self.chunks

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        if not self.responses:
            raise AssertionError(f"Unexpected HTTP request: {method} {url}")
        return self.responses.pop(0)


class ArrClientTests(unittest.TestCase):
    def test_radarr_lookup_add_monitor_and_search(self):
        selected = {"title": "Arrival", "year": 2016, "tmdbId": 329865, "id": 0}
        session = FakeSession(
            [
                FakeResponse(json_value=[{"title": "The Arrival", "tmdbId": 1}, selected]),
                FakeResponse(json_value=[{"id": 2, "path": "/movies"}]),
                FakeResponse(json_value=[{"id": 4, "name": "HD"}]),
                FakeResponse(json_value={**selected, "id": 44}),
                FakeResponse(json_value={"id": 9, "status": "queued"}),
            ]
        )
        client = RadarrClient("http://radarr:7878", "api-secret", session=session)
        response = client.submit("Arrival")

        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["external_id"], "44")
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET", "GET", "POST", "POST"])
        lookup = session.calls[0]
        self.assertTrue(lookup[1].endswith("/api/v3/movie/lookup"))
        self.assertEqual(lookup[2]["params"], {"term": "Arrival"})
        self.assertEqual(lookup[2]["headers"]["X-Api-Key"], "api-secret")

        add_payload = session.calls[3][2]["json"]
        self.assertTrue(add_payload["monitored"])
        self.assertEqual(add_payload["rootFolderPath"], "/movies")
        self.assertEqual(add_payload["qualityProfileId"], 4)
        command = session.calls[4][2]["json"]
        self.assertEqual(command, {"name": "MoviesSearch", "movieIds": [44]})

    def test_existing_monitored_arr_item_does_not_start_duplicate_search(self):
        session = FakeSession(
            [
                FakeResponse(json_value=[{"title": "Arrival", "tmdbId": 329865, "id": 44}]),
                FakeResponse(
                    json_value={
                        "title": "Arrival",
                        "id": 44,
                        "monitored": True,
                        "hasFile": False,
                    }
                ),
            ]
        )
        response = RadarrClient("http://radarr:7878", "key", session=session).submit("Arrival")
        self.assertEqual(response["status"], "queued")
        self.assertIn("already monitored", response["message"])
        self.assertIn("no duplicate search", response["message"])
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET"])

    def test_existing_imported_arr_item_returns_completed_without_mutation(self):
        session = FakeSession(
            [
                FakeResponse(json_value=[{"title": "Arrival", "tmdbId": 329865, "id": 44}]),
                FakeResponse(
                    json_value={
                        "title": "Arrival",
                        "id": 44,
                        "monitored": True,
                        "hasFile": True,
                    }
                ),
            ]
        )
        response = RadarrClient("http://radarr:7878", "key", session=session).submit("Arrival")
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["external_id"], "44")
        self.assertIn("already has imported media", response["message"])
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET"])

    def test_existing_unmonitored_arr_item_is_monitored_then_searched(self):
        existing = {
            "title": "Arrival",
            "tmdbId": 329865,
            "id": 44,
            "monitored": False,
            "hasFile": False,
        }
        session = FakeSession(
            [
                FakeResponse(json_value=[existing]),
                FakeResponse(json_value=existing),
                FakeResponse(json_value={**existing, "monitored": True}),
                FakeResponse(json_value={"id": 9}),
            ]
        )
        response = RadarrClient("http://radarr:7878", "key", session=session).submit("Arrival")
        self.assertEqual(response["status"], "queued")
        self.assertIn("enabled monitoring and started a search", response["message"])
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET", "PUT", "POST"])
        self.assertTrue(session.calls[2][2]["json"]["monitored"])

    def test_no_safe_arr_match_returns_needs_selection_without_add(self):
        session = FakeSession([FakeResponse(json_value=[{"title": "Unrelated", "tmdbId": 1}])])
        response = RadarrClient("http://radarr:7878", "key", session=session).submit("Arrival")
        self.assertEqual(response["status"], "needs_selection")
        self.assertEqual(len(session.calls), 1)

    def test_request_failure_is_sanitized(self):
        secret_title = "private title"
        session = FakeSession(error=requests.ConnectionError(f"URL query={secret_title}&apikey=secret"))
        client = RadarrClient("http://radarr:7878", "api-secret", session=session)
        with self.assertRaises(ServiceError) as context:
            client.lookup(secret_title)
        self.assertNotIn(secret_title, str(context.exception))
        self.assertNotIn("api-secret", str(context.exception))

    def test_sonarr_uses_series_search_command(self):
        selected = {"title": "Severance", "tvdbId": 371980, "id": 0}
        session = FakeSession(
            [
                FakeResponse(json_value=[selected]),
                FakeResponse(json_value=[{"id": 1, "path": "/tv"}]),
                FakeResponse(json_value=[{"id": 2, "name": "HD"}]),
                FakeResponse(json_value={**selected, "id": 33}),
                FakeResponse(json_value={"id": 5}),
            ]
        )
        response = SonarrClient("http://sonarr:8989", "key", session=session).submit(
            "Severance"
        )
        self.assertEqual(response["service"], "sonarr")
        self.assertEqual(
            session.calls[-1][2]["json"], {"name": "SeriesSearch", "seriesId": 33}
        )

    def test_lidarr_discovers_metadata_profile_and_searches_artist(self):
        selected = {
            "artistName": "Massive Attack",
            "foreignArtistId": "artist-guid",
            "id": 0,
        }
        session = FakeSession(
            [
                FakeResponse(json_value=[selected]),
                FakeResponse(json_value=[{"id": 1, "path": "/music"}]),
                FakeResponse(json_value=[{"id": 2, "name": "Lossless"}]),
                FakeResponse(json_value=[{"id": 3, "name": "Standard"}]),
                FakeResponse(json_value={**selected, "id": 55}),
                FakeResponse(json_value={"id": 6}),
            ]
        )
        response = LidarrClient("http://lidarr:8686", "key", session=session).submit(
            "Massive Attack"
        )
        self.assertEqual(response["service"], "lidarr")
        self.assertEqual(session.calls[4][2]["json"]["metadataProfileId"], 3)
        self.assertEqual(
            session.calls[-1][2]["json"], {"name": "ArtistSearch", "artistId": 55}
        )

    def test_radarr_completion_requires_explicit_has_file(self):
        for entity, expected in (
            ({"id": 44, "hasFile": False}, False),
            ({"id": 44, "hasFile": True}, True),
            ({"id": 44, "movieFile": {"id": 8}}, False),
        ):
            with self.subTest(entity=entity):
                session = FakeSession([FakeResponse(json_value=entity)])
                client = RadarrClient("http://radarr:7878", "key", session=session)
                self.assertIs(client.has_imported_media("44"), expected)
                self.assertTrue(session.calls[0][1].endswith("/api/v3/movie/44"))
                self.assertEqual(session.calls[0][0], "GET")

    def test_sonarr_completion_uses_episode_count_or_size(self):
        for statistics, expected in (
            ({"episodeFileCount": 0, "sizeOnDisk": 0}, False),
            ({"episodeFileCount": 1, "sizeOnDisk": 0}, True),
            ({"episodeFileCount": 0, "sizeOnDisk": 2048}, True),
            ({"episodeFileCount": "1", "sizeOnDisk": "2048"}, False),
        ):
            with self.subTest(statistics=statistics):
                session = FakeSession(
                    [FakeResponse(json_value={"id": 33, "statistics": statistics})]
                )
                client = SonarrClient("http://sonarr:8989", "key", session=session)
                self.assertIs(client.has_imported_media(33), expected)
                self.assertTrue(session.calls[0][1].endswith("/api/v3/series/33"))

    def test_lidarr_completion_uses_track_count_or_size(self):
        for statistics, expected in (
            ({"trackFileCount": 0, "sizeOnDisk": 0}, False),
            ({"trackFileCount": 7, "sizeOnDisk": 0}, True),
            ({"trackFileCount": 0, "sizeOnDisk": 4096}, True),
        ):
            with self.subTest(statistics=statistics):
                session = FakeSession(
                    [FakeResponse(json_value={"id": 55, "statistics": statistics})]
                )
                client = LidarrClient("http://lidarr:8686", "key", session=session)
                self.assertIs(client.has_imported_media(55), expected)
                self.assertTrue(session.calls[0][1].endswith("/api/v1/artist/55"))

    def test_completion_probe_rejects_invalid_entity_and_response(self):
        client = RadarrClient(
            "http://radarr:7878",
            "key",
            session=FakeSession([FakeResponse(json_value=[])]),
        )
        with self.assertRaisesRegex(ServiceError, "invalid entity ID"):
            client.has_imported_media("not-an-id")
        with self.assertRaisesRegex(ServiceError, "invalid entity response"):
            client.has_imported_media(44)


class ProwlarrClientTests(unittest.TestCase):
    def test_search_uses_required_categories_and_header_key(self):
        session = FakeSession([FakeResponse(json_value=[])])
        client = ProwlarrClient("http://prowlarr:9696", "key", session=session)
        self.assertEqual(client.search("Dune", (7020, 7000)), [])
        call = session.calls[0]
        self.assertTrue(call[1].endswith("/api/v1/search"))
        self.assertEqual(call[2]["params"]["categories"], [7020, 7000])
        self.assertEqual(call[2]["headers"]["X-Api-Key"], "key")

    def test_search_preserves_a_configured_base_path(self):
        session = FakeSession([FakeResponse(json_value=[])])
        client = ProwlarrClient(
            "https://services.invalid/prowlarr", "key", session=session
        )
        client.search("Dune", (7020, 7000))
        self.assertEqual(
            session.calls[0][1],
            "https://services.invalid/prowlarr/api/v1/search",
        )

    def test_search_retries_timeout_with_extended_read_budget(self):
        class TimeoutThenSuccessSession(FakeSession):
            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                if len(self.calls) == 1:
                    raise requests.ReadTimeout("slow indexer query=private&apikey=secret")
                return FakeResponse(json_value=[])

        session = TimeoutThenSuccessSession()
        client = ProwlarrClient(
            "http://prowlarr:9696",
            "key",
            session=session,
            search_retry_delay=1,
        )
        with patch("clients.time.sleep") as sleep:
            self.assertEqual(client.search("Tourist Season", (3030, 3000)), [])

        self.assertEqual(len(session.calls), 2)
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET"])
        self.assertTrue(
            all(call[1].endswith("/api/v1/search") for call in session.calls)
        )
        self.assertTrue(
            all(call[2]["timeout"] == (5, 90) for call in session.calls)
        )
        self.assertTrue(
            all(call[2]["params"]["categories"] == [3030, 3000] for call in session.calls)
        )
        self.assertTrue(
            all(call[2]["headers"]["X-Api-Key"] == "key" for call in session.calls)
        )
        sleep.assert_called_once_with(1)

    def test_exhausted_search_timeouts_are_sanitized(self):
        class AlwaysTimeoutSession(FakeSession):
            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                raise requests.ReadTimeout("query=private-title&apikey=secret")

        session = AlwaysTimeoutSession()
        client = ProwlarrClient(
            "http://prowlarr:9696",
            "key",
            session=session,
            search_retry_delay=0,
        )
        with self.assertRaisesRegex(ServiceError, "timed out") as caught:
            client.search("private-title", (3030, 3000))
        self.assertEqual(len(session.calls), 2)
        self.assertNotIn("private-title", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))

    def test_search_authentication_failure_is_not_retried(self):
        session = FakeSession([FakeResponse(status=401, json_value={})])
        client = ProwlarrClient(
            "http://prowlarr:9696",
            "key",
            session=session,
            search_retry_delay=0,
        )
        with self.assertRaisesRegex(ServiceError, "HTTP 401"):
            client.search("Dune", (3030, 3000))
        self.assertEqual(len(session.calls), 1)

    def test_download_returns_bytes(self):
        response = FakeResponse(content=b"torrent bytes")
        session = FakeSession([response])
        client = ProwlarrClient("http://prowlarr:9696", "key", session=session)
        self.assertEqual(client.download_torrent("/api/v1/download/1"), b"torrent bytes")
        self.assertEqual(session.calls[0][2]["headers"]["X-Api-Key"], "key")
        self.assertTrue(session.calls[0][2]["stream"])
        self.assertEqual(session.calls[0][2]["timeout"], 30)
        self.assertTrue(response.closed)

    def test_api_key_is_not_forwarded_to_external_download_host(self):
        session = FakeSession([FakeResponse(content=b"torrent bytes")])
        client = ProwlarrClient("http://prowlarr:9696", "key", session=session)
        client.download_torrent("https://downloads.invalid/release.torrent")
        self.assertNotIn("X-Api-Key", session.calls[0][2]["headers"])

    def test_cross_origin_redirect_strips_api_key_and_disables_automatic_redirects(self):
        session = FakeSession(
            [
                FakeResponse(
                    status=302,
                    headers={"Location": "https://downloads.invalid/release.torrent"},
                ),
                FakeResponse(content=b"torrent bytes"),
            ]
        )
        client = ProwlarrClient("http://prowlarr:9696", "key", session=session)
        self.assertEqual(
            client.download_torrent("/api/v1/download/1"), b"torrent bytes"
        )
        self.assertEqual(session.calls[0][2]["headers"]["X-Api-Key"], "key")
        self.assertNotIn("X-Api-Key", session.calls[1][2]["headers"])
        self.assertFalse(session.calls[0][2]["allow_redirects"])
        self.assertFalse(session.calls[1][2]["allow_redirects"])

    def test_declared_oversized_torrent_is_rejected_without_reading_body(self):
        response = FakeResponse(
            headers={"Content-Length": str(MAX_TORRENT_BYTES + 1)},
            chunks=[b"should not be read"],
        )
        session = FakeSession([response])
        client = ProwlarrClient("http://prowlarr:9696", "key", session=session)
        with self.assertRaisesRegex(ServiceError, "too large"):
            client.download_torrent("/api/v1/download/1")
        self.assertFalse(response.iterated)
        self.assertTrue(response.closed)

    def test_streamed_torrent_is_bounded_when_length_is_missing_or_wrong(self):
        one_mebibyte = b"x" * (1024 * 1024)
        response = FakeResponse(
            headers={"Content-Length": "1"},
            chunks=[one_mebibyte] * 17,
        )
        session = FakeSession([response])
        client = ProwlarrClient("http://prowlarr:9696", "key", session=session)
        with self.assertRaisesRegex(ServiceError, "too large"):
            client.download_torrent("/api/v1/download/1")
        self.assertTrue(response.iterated)
        self.assertTrue(response.closed)


class QBittorrentClientTests(unittest.TestCase):
    def test_cookie_login_category_and_magnet_submission(self):
        session = FakeSession([FakeResponse(), FakeResponse(), FakeResponse()])
        client = QBittorrentClient(
            "http://qbittorrent:8080", "huey", "password", session=session
        )
        client.add_magnet("magnet:?xt=urn:btih:abc", "huey-ebooks")
        self.assertEqual(
            [call[1].rsplit("/", 1)[-1] for call in session.calls],
            ["login", "createCategory", "add"],
        )
        self.assertEqual(
            session.calls[0][2]["data"], {"username": "huey", "password": "password"}
        )
        self.assertTrue(
            all(
                call[2]["headers"]["Referer"] == "http://qbittorrent:8080/"
                for call in session.calls
            )
        )
        self.assertEqual(session.calls[2][2]["data"]["category"], "huey-ebooks")

    def test_torrent_file_is_multipart(self):
        session = FakeSession([FakeResponse(), FakeResponse(), FakeResponse()])
        client = QBittorrentClient("http://qbittorrent:8080", "u", "p", session=session)
        client.add_torrent(b"torrent", "huey-roms")
        files = session.calls[2][2]["files"]
        self.assertEqual(files["torrents"][1], b"torrent")

    def test_failed_login_does_not_submit(self):
        session = FakeSession([FakeResponse(text="Fails.")])
        client = QBittorrentClient("http://qbittorrent:8080", "u", "p", session=session)
        with self.assertRaisesRegex(ServiceError, "authentication"):
            client.add_magnet("magnet:?xt=urn:btih:abc", "huey-ebooks")
        self.assertEqual(len(session.calls), 1)

    def test_add_tags_authenticates_and_targets_exact_hash(self):
        session = FakeSession([FakeResponse(), FakeResponse()])
        client = QBittorrentClient("http://qbittorrent:8080", "u", "p", session=session)
        torrent_hash = "a" * 40
        client.add_tags(torrent_hash, "huey-42")
        self.assertTrue(session.calls[-1][1].endswith("/api/v2/torrents/addTags"))
        self.assertEqual(
            session.calls[-1][2]["data"],
            {"hashes": torrent_hash, "tags": "huey-42"},
        )

    def test_expired_session_reauthenticates_during_safe_add_preflight(self):
        session = FakeSession(
            [
                FakeResponse(),
                FakeResponse(status=403),
                FakeResponse(),
                FakeResponse(status=409),
                FakeResponse(),
            ]
        )
        client = QBittorrentClient("http://qbittorrent:8080", "u", "p", session=session)
        client.add_magnet("magnet:?xt=urn:btih:abc", "huey-ebooks")
        endpoints = [call[1].rsplit("/", 1)[-1] for call in session.calls]
        self.assertEqual(
            endpoints,
            ["login", "createCategory", "login", "createCategory", "add"],
        )
        self.assertEqual(endpoints.count("add"), 1)

    def test_expired_session_does_not_replay_torrent_add(self):
        session = FakeSession(
            [FakeResponse(), FakeResponse(), FakeResponse(status=403)]
        )
        client = QBittorrentClient("http://qbittorrent:8080", "u", "p", session=session)
        with self.assertRaisesRegex(ServiceError, "not retried"):
            client.add_magnet("magnet:?xt=urn:btih:abc", "huey-ebooks")
        endpoints = [call[1].rsplit("/", 1)[-1] for call in session.calls]
        self.assertEqual(endpoints.count("add"), 1)
        self.assertEqual(endpoints.count("login"), 1)

    def test_expired_session_retries_idempotent_tag_update_once(self):
        session = FakeSession(
            [FakeResponse(), FakeResponse(status=403), FakeResponse(), FakeResponse()]
        )
        client = QBittorrentClient("http://qbittorrent:8080", "u", "p", session=session)
        client.add_tags("a" * 40, "huey-42")
        endpoints = [call[1].rsplit("/", 1)[-1] for call in session.calls]
        self.assertEqual(endpoints, ["login", "addTags", "login", "addTags"])

    def test_find_torrent_uses_exact_hash_and_is_read_only(self):
        torrent_hash = "a" * 40
        session = FakeSession(
            [
                FakeResponse(),
                FakeResponse(
                    json_value=[{"hash": torrent_hash.upper(), "category": "ebooks"}]
                ),
            ]
        )
        client = QBittorrentClient("http://qbittorrent:8080", "u", "p", session=session)
        found = client.find_torrent(torrent_hash)
        self.assertEqual(found["category"], "ebooks")
        self.assertEqual([call[0] for call in session.calls], ["POST", "GET"])
        self.assertEqual(session.calls[-1][2]["params"], {"hashes": torrent_hash})

    def test_find_torrent_returns_none_only_for_an_empty_exact_result(self):
        session = FakeSession([FakeResponse(), FakeResponse(json_value=[])])
        client = QBittorrentClient("http://qbittorrent:8080", "u", "p", session=session)
        self.assertIsNone(client.find_torrent("b" * 40))

        for value in ({}, [{"hash": "not-the-requested-hash"}], ["invalid"]):
            with self.subTest(value=value):
                session = FakeSession([FakeResponse(), FakeResponse(json_value=value)])
                client = QBittorrentClient(
                    "http://qbittorrent:8080", "u", "p", session=session
                )
                with self.assertRaisesRegex(ServiceError, "invalid torrent response"):
                    client.find_torrent("b" * 40)

    def test_find_torrent_reauthenticates_once_after_expired_session(self):
        torrent_hash = "c" * 40
        session = FakeSession(
            [
                FakeResponse(),
                FakeResponse(status=403),
                FakeResponse(),
                FakeResponse(json_value=[]),
            ]
        )
        client = QBittorrentClient("http://qbittorrent:8080", "u", "p", session=session)
        self.assertIsNone(client.find_torrent(torrent_hash))
        self.assertEqual(
            [call[1].rsplit("/", 1)[-1] for call in session.calls],
            ["login", "info", "login", "info"],
        )


if __name__ == "__main__":
    unittest.main()
