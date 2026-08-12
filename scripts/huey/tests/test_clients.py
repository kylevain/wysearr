import sys
import unittest
from pathlib import Path

import requests


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

from clients import (
    LidarrClient,
    ProwlarrClient,
    QBittorrentClient,
    RadarrClient,
    ServiceError,
    SonarrClient,
)


class FakeResponse:
    def __init__(self, status=200, json_value=None, text="Ok.", content=b"", headers=None):
        self.status_code = status
        self._json = json_value
        self.text = text
        self.content = content
        self.headers = dict(headers or {})

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


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
        self.assertEqual(response["external_id"], "329865")
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

    def test_existing_arr_item_only_triggers_search(self):
        session = FakeSession(
            [
                FakeResponse(json_value=[{"title": "Arrival", "tmdbId": 329865, "id": 44}]),
                FakeResponse(json_value={"title": "Arrival", "id": 44, "monitored": True}),
                FakeResponse(json_value={"id": 9}),
            ]
        )
        response = RadarrClient("http://radarr:7878", "key", session=session).submit("Arrival")
        self.assertEqual(response["status"], "queued")
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(session.calls[1][0], "PUT")
        self.assertTrue(session.calls[1][2]["json"]["monitored"])
        self.assertTrue(session.calls[2][1].endswith("/api/v3/command"))

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


class ProwlarrClientTests(unittest.TestCase):
    def test_search_uses_required_categories_and_header_key(self):
        session = FakeSession([FakeResponse(json_value=[])])
        client = ProwlarrClient("http://prowlarr:9696", "key", session=session)
        self.assertEqual(client.search("Dune", (7020, 7000)), [])
        call = session.calls[0]
        self.assertTrue(call[1].endswith("/api/v1/search"))
        self.assertEqual(call[2]["params"]["categories"], [7020, 7000])
        self.assertEqual(call[2]["headers"]["X-Api-Key"], "key")

    def test_download_returns_bytes(self):
        session = FakeSession([FakeResponse(content=b"torrent bytes")])
        client = ProwlarrClient("http://prowlarr:9696", "key", session=session)
        self.assertEqual(client.download_torrent("/api/v1/download/1"), b"torrent bytes")
        self.assertEqual(session.calls[0][2]["headers"]["X-Api-Key"], "key")

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


if __name__ == "__main__":
    unittest.main()
