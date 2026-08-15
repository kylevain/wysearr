import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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
    SubmissionUncertain,
    ShelfarrClient,
    SonarrClient,
)
from results import normalize_result


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


class ShelfarrClientTests(unittest.TestCase):
    def metadata_result(self, **overrides):
        value = {
            "work_id": "openlibrary:OL893415W",
            "title": "Dune",
            "author": "Frank Herbert",
            "year": 1965,
            "content_kind": "book",
            "available_book_types": ["ebook", "audiobook"],
            "sources": [
                {"work_id": "openlibrary:OL893415W"},
                {"work_id": "hardcover:book:123"},
            ],
        }
        value.update(overrides)
        return value

    def request_payload(self, **overrides):
        request_overrides = dict(overrides.pop("request", {}))
        book_overrides = dict(overrides.pop("book", {}))
        value = {
            "id": 73,
            "status": "pending",
            "attention_needed": False,
            "issue_description": None,
        }
        value.update(overrides)
        value["request"] = {
            "id": value["id"],
            "status": value["status"],
            "attention_needed": value["attention_needed"],
            "created_via": "api",
            "external_source": "huey:42",
            "request_scope": "single",
            **request_overrides,
        }
        value["book"] = {
            "id": 9,
            "title": "Dune",
            "author": "Frank Herbert",
            "book_type": "ebook",
            "content_kind": "book",
            "work_id": "openlibrary:OL893415W",
            **book_overrides,
        }
        return value

    def create_response(self, request=None, **overrides):
        value = {
            "requests": [request or self.request_payload()],
            "queued": False,
            "warnings": [],
            "errors": [],
        }
        value.update(overrides)
        return value

    def test_search_match_and_create_preserve_huey_correlation(self):
        session = FakeSession(
            [
                FakeResponse(json_value={"results": [self.metadata_result()]}),
                FakeResponse(json_value={"requests": []}),
                FakeResponse(json_value={"requests": []}),
                FakeResponse(status=201, json_value=self.create_response()),
            ]
        )
        client = ShelfarrClient("http://shelfarr", "shf_secret", session=session)

        response = client.submit(
            "ebooks",
            "Dune",
            "Frank Herbert",
            42,
            discord_user_id="1001",
            discord_channel_id="2002",
        )

        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["service"], "shelfarr")
        self.assertEqual(response["external_id"], "73")
        self.assertEqual(response["external_status"], "pending")
        self.assertEqual(
            [call[0] for call in session.calls], ["GET", "GET", "GET", "POST"]
        )
        search_call, recovery_call, work_call, create_call = session.calls
        self.assertTrue(search_call[1].endswith("/api/v1/search"))
        self.assertEqual(
            search_call[2]["params"],
            {"q": "Dune Frank Herbert", "limit": 10, "content_kind": "book"},
        )
        for call in session.calls:
            self.assertEqual(
                call[2]["headers"]["Authorization"], "Bearer shf_secret"
            )
        payload = create_call[2]["json"]
        self.assertEqual(payload["work_id"], "openlibrary:OL893415W")
        self.assertEqual(payload["book_type"], "ebook")
        self.assertEqual(payload["notes"], "Huey request #42")
        self.assertTrue(recovery_call[1].endswith("/api/v1/requests"))
        self.assertEqual(work_call[2]["params"], {"limit": 100})
        self.assertEqual(payload["external_source"], "huey:42")
        self.assertEqual(payload["external_user_id"], "1001")
        self.assertEqual(payload["external_chat_id"], "2002")
        self.assertEqual(
            payload["source_work_ids"],
            ["openlibrary:OL893415W", "hardcover:book:123"],
        )

    def test_ambiguous_metadata_never_creates_request(self):
        session = FakeSession(
            [
                FakeResponse(
                    json_value={
                        "results": [
                            self.metadata_result(author="Frank Herbert"),
                            self.metadata_result(
                                work_id="openlibrary:OTHER",
                                author="Another Author",
                                sources=[{"work_id": "openlibrary:OTHER"}],
                            ),
                        ]
                    }
                )
            ]
        )
        response = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=session
        ).submit("audiobooks", "Dune")

        self.assertEqual(response["status"], "awaiting_selection")
        self.assertEqual(response["service"], "shelfarr")
        self.assertEqual(len(response["selection_proposal"]), 2)
        self.assertEqual(len(session.calls), 1)

    def test_requested_format_must_be_available_in_metadata_result(self):
        session = FakeSession(
            [
                FakeResponse(
                    json_value={
                        "results": [
                            self.metadata_result(available_book_types=["ebook"])
                        ]
                    }
                )
            ]
        )

        response = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=session
        ).submit("audiobooks", "Dune", "Frank Herbert", 42)

        self.assertEqual(response["status"], "needs_selection")
        self.assertEqual(response["selection_proposal"], ())

    def test_supplied_author_must_match_all_metadata_author_tokens_before_post(self):
        for candidate_author in (None, "Herbert", "Brian Herbert"):
            with self.subTest(author=candidate_author):
                session = FakeSession(
                    [
                        FakeResponse(
                            json_value={
                                "results": [
                                    self.metadata_result(author=candidate_author)
                                ]
                            }
                        )
                    ]
                )
                response = ShelfarrClient(
                    "http://shelfarr", "shf_secret", session=session
                ).submit("ebooks", "Dune", "Frank Herbert", 42)

                self.assertEqual(response["status"], "needs_selection")
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(session.calls[0][0], "GET")
        self.assertEqual(len(session.calls), 1)

    def test_ambiguous_proposal_is_bounded_sanitized_and_normalized(self):
        candidates = []
        for index in range(4):
            work_id = f"openlibrary:OL-OPTION-{index}"
            candidates.append(
                self.metadata_result(
                    work_id=work_id,
                    author=f"Author {index}",
                    sources=[
                        {
                            "work_id": work_id,
                            "source_url": f"https://metadata.invalid/{index}?token=hidden",
                        }
                    ],
                    canonical_key=f"isbn:private-{index}",
                )
            )
        session = FakeSession([FakeResponse(json_value={"results": candidates})])

        response = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=session
        ).submit("ebooks", "Dune")
        normalized = normalize_result(response)

        self.assertEqual(response["status"], "awaiting_selection")
        self.assertEqual(len(response["selection_proposal"]), 3)
        self.assertEqual(normalized["selection_proposal"], response["selection_proposal"])
        expected_keys = {
            "fingerprint",
            "label",
            "work_id",
            "source_work_ids",
            "title",
            "author",
            "year",
            "content_kind",
            "media_type",
            "book_type",
        }
        for proposal in response["selection_proposal"]:
            self.assertEqual(set(proposal), expected_keys)
            self.assertRegex(proposal["fingerprint"], r"\A[0-9a-f]{64}\Z")
            self.assertEqual(proposal["content_kind"], "book")
            self.assertEqual(proposal["media_type"], "ebooks")
            self.assertEqual(proposal["book_type"], "ebook")
        rendered = repr(response["selection_proposal"])
        self.assertNotIn("https://", rendered)
        self.assertNotIn("token=", rendered)
        self.assertNotIn("canonical_key", rendered)

    def test_selected_proposal_is_freshly_revalidated_before_one_create(self):
        other = self.metadata_result(
            work_id="openlibrary:OTHER",
            author="Another Author",
            sources=[{"work_id": "openlibrary:OTHER"}],
        )
        initial_session = FakeSession(
            [
                FakeResponse(
                    json_value={"results": [self.metadata_result(), other]}
                )
            ]
        )
        initial = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=initial_session
        ).submit("ebooks", "Dune")
        selected = next(
            proposal
            for proposal in initial["selection_proposal"]
            if proposal["work_id"] == "openlibrary:OL893415W"
        )

        session = FakeSession(
            [
                FakeResponse(json_value={"results": [self.metadata_result(), other]}),
                FakeResponse(json_value={"requests": []}),
                FakeResponse(json_value={"requests": []}),
                FakeResponse(status=201, json_value=self.create_response()),
            ]
        )
        response = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=session
        ).submit_selected(
            "ebooks",
            "Dune",
            None,
            42,
            selected_candidate=selected,
            discord_user_id="1001",
            discord_channel_id="2002",
        )

        self.assertEqual(response["status"], "queued")
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET", "GET", "POST"])
        self.assertEqual(sum(call[0] == "POST" for call in session.calls), 1)
        self.assertFalse(
            any(
                call[1].endswith("/grab") or call[1].endswith("/retry")
                for call in session.calls
            )
        )
        payload = session.calls[-1][2]["json"]
        self.assertEqual(payload["work_id"], selected["work_id"])
        self.assertEqual(payload["source_work_ids"], list(selected["source_work_ids"]))
        self.assertEqual(payload["external_source"], "huey:42")

    def test_selected_create_hook_runs_once_immediately_before_post(self):
        selected = ShelfarrClient._candidate_snapshot(
            self.metadata_result(), "ebooks"
        )
        self.assertIsNotNone(selected)
        events = []

        class OrderingSession(FakeSession):
            def request(self, method, url, **kwargs):
                if method == "POST":
                    events.append("post")
                return super().request(method, url, **kwargs)

        session = OrderingSession(
            [
                FakeResponse(json_value={"results": [self.metadata_result()]}),
                FakeResponse(json_value={"requests": []}),
                FakeResponse(json_value={"requests": []}),
                FakeResponse(status=201, json_value=self.create_response()),
            ]
        )
        response = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=session
        ).submit_selected(
            "ebooks",
            "Dune",
            "Frank Herbert",
            42,
            selected_candidate=selected,
            before_create=lambda: events.append("callback"),
        )

        self.assertEqual(response["status"], "queued")
        self.assertEqual(events, ["callback", "post"])
        self.assertEqual(sum(call[0] == "POST" for call in session.calls), 1)

    def test_selected_create_hook_failure_prevents_post(self):
        selected = ShelfarrClient._candidate_snapshot(
            self.metadata_result(), "ebooks"
        )
        self.assertIsNotNone(selected)
        callback_calls = []
        session = FakeSession(
            [
                FakeResponse(json_value={"results": [self.metadata_result()]}),
                FakeResponse(json_value={"requests": []}),
                FakeResponse(json_value={"requests": []}),
            ]
        )

        def fail_before_create():
            callback_calls.append("called")
            raise RuntimeError("could not persist dispatch intent")

        with self.assertRaisesRegex(RuntimeError, "persist dispatch intent"):
            ShelfarrClient(
                "http://shelfarr", "shf_secret", session=session
            ).submit_selected(
                "ebooks",
                "Dune",
                "Frank Herbert",
                42,
                selected_candidate=selected,
                before_create=fail_before_create,
            )

        self.assertEqual(callback_calls, ["called"])
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET", "GET"])
        self.assertEqual(sum(call[0] == "POST" for call in session.calls), 0)

    def test_selected_create_hook_is_not_called_for_stale_or_reused_work(self):
        selected = ShelfarrClient._candidate_snapshot(
            self.metadata_result(), "ebooks"
        )
        self.assertIsNotNone(selected)

        stale_hook = Mock()
        stale_session = FakeSession(
            [FakeResponse(json_value={"results": [self.metadata_result(year=1966)]})]
        )
        stale = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=stale_session
        ).submit_selected(
            "ebooks",
            "Dune",
            "Frank Herbert",
            42,
            selected_candidate=selected,
            before_create=stale_hook,
        )
        self.assertEqual(stale["status"], "needs_selection")
        stale_hook.assert_not_called()

        reused_hook = Mock()
        reused_session = FakeSession(
            [
                FakeResponse(json_value={"results": [self.metadata_result()]}),
                FakeResponse(json_value={"requests": []}),
                FakeResponse(json_value={"requests": [self.request_payload()]}),
            ]
        )
        reused = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=reused_session
        ).submit_selected(
            "ebooks",
            "Dune",
            "Frank Herbert",
            42,
            selected_candidate=selected,
            before_create=reused_hook,
        )
        self.assertEqual(reused["status"], "queued")
        reused_hook.assert_not_called()
        self.assertEqual(sum(call[0] == "POST" for call in reused_session.calls), 0)

    def test_create_requires_exact_pinned_response_without_a_second_post(self):
        exact = self.create_response()
        missing_queued = dict(exact)
        missing_queued.pop("queued")
        missing_warnings = dict(exact)
        missing_warnings.pop("warnings")
        missing_errors = dict(exact)
        missing_errors.pop("errors")

        malformed = (
            ("HTTP 200", FakeResponse(status=200, json_value=exact)),
            ("HTTP 202", FakeResponse(status=202, json_value=exact)),
            ("missing queued", FakeResponse(status=201, json_value=missing_queued)),
            (
                "non-boolean queued",
                FakeResponse(status=201, json_value=self.create_response(queued=0)),
            ),
            (
                "queued response",
                FakeResponse(status=201, json_value=self.create_response(queued=True)),
            ),
            (
                "missing warnings",
                FakeResponse(status=201, json_value=missing_warnings),
            ),
            (
                "non-list warnings",
                FakeResponse(status=201, json_value=self.create_response(warnings={})),
            ),
            (
                "warning present",
                FakeResponse(
                    status=201, json_value=self.create_response(warnings=["partial"])
                ),
            ),
            ("missing errors", FakeResponse(status=201, json_value=missing_errors)),
            (
                "non-list errors",
                FakeResponse(status=201, json_value=self.create_response(errors={})),
            ),
            (
                "error present",
                FakeResponse(
                    status=201, json_value=self.create_response(errors=["failed"])
                ),
            ),
            (
                "multiple requests",
                FakeResponse(
                    status=201,
                    json_value=self.create_response(
                        requests=[self.request_payload(), self.request_payload(id=74)]
                    ),
                ),
            ),
            (
                "empty requests",
                FakeResponse(
                    status=201,
                    json_value=self.create_response(requests=[]),
                ),
            ),
            (
                "non-list requests",
                FakeResponse(
                    status=201,
                    json_value=self.create_response(requests={}),
                ),
            ),
            (
                "wrong correlation",
                FakeResponse(
                    status=201,
                    json_value=self.create_response(
                        self.request_payload(
                            request={"external_source": "huey:another-request"}
                        )
                    ),
                ),
            ),
            (
                "missing nested request",
                FakeResponse(
                    status=201,
                    json_value=self.create_response(
                        {
                            key: value
                            for key, value in self.request_payload().items()
                            if key != "request"
                        }
                    ),
                ),
            ),
            (
                "mismatched nested id",
                FakeResponse(
                    status=201,
                    json_value=self.create_response(
                        self.request_payload(request={"id": 999})
                    ),
                ),
            ),
            (
                "mismatched nested status",
                FakeResponse(
                    status=201,
                    json_value=self.create_response(
                        self.request_payload(request={"status": "searching"})
                    ),
                ),
            ),
            (
                "mismatched nested attention",
                FakeResponse(
                    status=201,
                    json_value=self.create_response(
                        self.request_payload(request={"attention_needed": True})
                    ),
                ),
            ),
            (
                "wrong origin",
                FakeResponse(
                    status=201,
                    json_value=self.create_response(
                        self.request_payload(request={"created_via": "web"})
                    ),
                ),
            ),
            (
                "collection response",
                FakeResponse(
                    status=201,
                    json_value=self.create_response(
                        self.request_payload(request={"request_scope": "collection"})
                    ),
                ),
            ),
            (
                "wrong content kind",
                FakeResponse(
                    status=201,
                    json_value=self.create_response(
                        self.request_payload(book={"content_kind": "comic"})
                    ),
                ),
            ),
            (
                "unsafe work identity",
                FakeResponse(
                    status=201,
                    json_value=self.create_response(
                        self.request_payload(book={"work_id": "openlibrary:token"})
                    ),
                ),
            ),
        )

        for label, post_response in malformed:
            with self.subTest(label=label):
                session = FakeSession(
                    [
                        FakeResponse(json_value={"results": [self.metadata_result()]}),
                        FakeResponse(json_value={"requests": []}),
                        FakeResponse(json_value={"requests": []}),
                        post_response,
                        FakeResponse(json_value={"requests": []}),
                    ]
                )
                with self.assertRaises(SubmissionUncertain):
                    ShelfarrClient(
                        "http://shelfarr", "shf_secret", session=session
                    ).submit("ebooks", "Dune", "Frank Herbert", 42)
                self.assertEqual(
                    [call[0] for call in session.calls],
                    ["GET", "GET", "GET", "POST", "GET"],
                )
                self.assertEqual(sum(call[0] == "POST" for call in session.calls), 1)

    def test_selected_proposal_change_or_tampering_never_creates(self):
        other = self.metadata_result(
            work_id="openlibrary:OTHER",
            author="Another Author",
            sources=[{"work_id": "openlibrary:OTHER"}],
        )
        initial = ShelfarrClient(
            "http://shelfarr",
            "shf_secret",
            session=FakeSession(
                [FakeResponse(json_value={"results": [self.metadata_result(), other]})]
            ),
        ).submit("audiobooks", "Dune")
        selected = next(
            proposal
            for proposal in initial["selection_proposal"]
            if proposal["work_id"] == "openlibrary:OL893415W"
        )

        changed_session = FakeSession(
            [
                FakeResponse(
                    json_value={
                        "results": [self.metadata_result(year=1966), other]
                    }
                )
            ]
        )
        changed = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=changed_session
        ).submit_selected(
            "audiobooks",
            "Dune",
            None,
            42,
            selected_candidate=selected,
        )
        self.assertEqual(changed["status"], "needs_selection")
        self.assertEqual([call[0] for call in changed_session.calls], ["GET"])

        tampered = dict(selected)
        tampered["title"] = "A Different Book"
        no_call_session = FakeSession()
        rejected = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=no_call_session
        ).submit_selected(
            "audiobooks",
            "Dune",
            None,
            42,
            selected_candidate=tampered,
        )
        self.assertEqual(rejected["status"], "needs_selection")
        self.assertEqual(no_call_session.calls, [])

        invalid_fingerprint = dict(selected)
        invalid_fingerprint["fingerprint"] = "é" * 64
        invalid_fingerprint_session = FakeSession()
        rejected_fingerprint = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=invalid_fingerprint_session
        ).submit_selected(
            "audiobooks",
            "Dune",
            None,
            42,
            selected_candidate=invalid_fingerprint,
        )
        self.assertEqual(rejected_fingerprint["status"], "needs_selection")
        self.assertEqual(invalid_fingerprint_session.calls, [])

        wrong_format_session = FakeSession()
        wrong_format = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=wrong_format_session
        ).submit_selected(
            "ebooks",
            "Dune",
            None,
            42,
            selected_candidate=selected,
        )
        self.assertEqual(wrong_format["status"], "needs_selection")
        self.assertEqual(wrong_format_session.calls, [])

        with self.assertRaisesRegex(ValueError, "positive Huey request ID"):
            ShelfarrClient(
                "http://shelfarr", "shf_secret", session=FakeSession()
            ).submit_selected(
                "audiobooks",
                "Dune",
                None,
                0,
                selected_candidate=selected,
            )

    def test_recovered_request_must_match_confirmed_candidate_alias_and_format(self):
        selected = ShelfarrClient._candidate_snapshot(
            self.metadata_result(), "ebooks"
        )
        self.assertIsNotNone(selected)
        persisted = {**selected, "source_work_ids": list(selected["source_work_ids"])}

        self.assertTrue(
            ShelfarrClient.recovered_request_matches_candidate(
                self.request_payload(), persisted, "ebooks"
            )
        )
        self.assertTrue(
            ShelfarrClient.recovered_request_matches_candidate(
                self.request_payload(book={"work_id": "hardcover:book:123"}),
                persisted,
                "ebooks",
            )
        )

        mismatches = (
            self.request_payload(book={"work_id": "openlibrary:OTHER"}),
            self.request_payload(book={"book_type": "audiobook"}),
            self.request_payload(book={"content_kind": "graphic"}),
            self.request_payload(request={"status": "searching"}),
        )
        for remote in mismatches:
            with self.subTest(remote=remote):
                self.assertFalse(
                    ShelfarrClient.recovered_request_matches_candidate(
                        remote, persisted, "ebooks"
                    )
                )

        tampered = {**persisted, "fingerprint": "f" * 64}
        self.assertFalse(
            ShelfarrClient.recovered_request_matches_candidate(
                self.request_payload(), tampered, "ebooks"
            )
        )
        self.assertFalse(
            ShelfarrClient.recovered_request_matches_candidate(
                self.request_payload(), persisted, "audiobooks"
            )
        )

    def test_existing_correlation_recovers_without_duplicate_post(self):
        correlated = self.request_payload(
            request={"id": 73, "external_source": "huey:42"}
        )
        session = FakeSession(
            [
                FakeResponse(json_value={"results": [self.metadata_result()]}),
                FakeResponse(json_value={"requests": [correlated]}),
            ]
        )
        response = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=session
        ).submit("ebooks", "Dune", "Frank Herbert", 42)

        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["external_id"], "73")
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET"])

    def test_existing_malformed_correlation_remains_uncertain(self):
        correlated = self.request_payload(
            book={"title": "Dune", "book_type": "audiobook"},
            request={"id": 73, "external_source": "huey:42"},
        )
        session = FakeSession(
            [
                FakeResponse(json_value={"results": [self.metadata_result()]}),
                FakeResponse(json_value={"requests": [correlated]}),
            ]
        )

        with self.assertRaises(SubmissionUncertain):
            ShelfarrClient(
                "http://shelfarr", "shf_secret", session=session
            ).submit("ebooks", "Dune", "Frank Herbert", 42)

        self.assertEqual([call[0] for call in session.calls], ["GET", "GET"])

    def test_duplicate_correlations_remain_uncertain(self):
        first = self.request_payload(
            request={"id": 73, "external_source": "huey:42"}
        )
        second = self.request_payload(
            id=74, request={"id": 74, "external_source": "huey:42"}
        )
        session = FakeSession(
            [
                FakeResponse(json_value={"results": [self.metadata_result()]}),
                FakeResponse(json_value={"requests": [first, second]}),
            ]
        )

        with self.assertRaisesRegex(SubmissionUncertain, "duplicate"):
            ShelfarrClient(
                "http://shelfarr", "shf_secret", session=session
            ).submit("ebooks", "Dune", "Frank Herbert", 42)

        self.assertEqual([call[0] for call in session.calls], ["GET", "GET"])

    def test_request_list_horizon_is_never_treated_as_authoritative_absence(self):
        def page(count, *, include_correlation=False):
            requests = [
                self.request_payload(
                    id=index + 1,
                    request={"external_source": f"huey:{1000 + index}"},
                )
                for index in range(count)
            ]
            if include_correlation:
                requests[-1] = self.request_payload(
                    id=count,
                    request={"external_source": "huey:42"},
                )
            return {"requests": requests}

        for count in (100, 101):
            with self.subTest(kind="correlation", count=count):
                client = ShelfarrClient(
                    "http://shelfarr",
                    "shf_secret",
                    session=FakeSession([FakeResponse(json_value=page(count))]),
                )
                with self.assertRaises(SubmissionUncertain):
                    client.recover_request(42)
            with self.subTest(kind="work", count=count):
                client = ShelfarrClient(
                    "http://shelfarr",
                    "shf_secret",
                    session=FakeSession([FakeResponse(json_value=page(count))]),
                )
                with self.assertRaises(SubmissionUncertain):
                    client._find_existing_work_request(
                        {"openlibrary:NOT-IN-PAGE"}, "ebook"
                    )

        client = ShelfarrClient(
            "http://shelfarr",
            "shf_secret",
            session=FakeSession(
                [FakeResponse(json_value=page(100, include_correlation=True))]
            ),
        )
        self.assertEqual(client.recover_request(42)["id"], 100)

    def test_completed_evaluation_work_is_reused_by_new_huey_request(self):
        completed = self.request_payload(
            status="completed",
            request={"external_source": "huey:900200012"},
        )
        completed["book"] = {
            **completed["book"],
            "work_id": "openlibrary:OL893415W",
        }
        session = FakeSession(
            [
                FakeResponse(json_value={"results": [self.metadata_result()]}),
                FakeResponse(json_value={"requests": []}),
                FakeResponse(json_value={"requests": [completed]}),
            ]
        )

        response = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=session
        ).submit("ebooks", "Dune", "Frank Herbert", 42)

        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["external_id"], "73")
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET", "GET"])

    def test_completed_exact_work_wins_over_duplicate_active_match(self):
        active = self.request_payload(id=80, status="downloading")
        completed = self.request_payload(id=70, status="completed")
        for request in (active, completed):
            request["book"] = {
                **request["book"],
                "work_id": "openlibrary:OL893415W",
            }
        session = FakeSession(
            [
                FakeResponse(json_value={"results": [self.metadata_result()]}),
                FakeResponse(json_value={"requests": []}),
                FakeResponse(json_value={"requests": [active, completed]}),
            ]
        )

        response = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=session
        ).submit("ebooks", "Dune", "Frank Herbert", 42)

        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["external_id"], "70")

    def test_lost_create_response_recovers_by_correlation(self):
        class LostPostSession(FakeSession):
            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                if len(self.calls) == 1:
                    return FakeResponse(json_value={"results": [self_test.metadata_result()]})
                if len(self.calls) == 2:
                    return FakeResponse(json_value={"requests": []})
                if len(self.calls) == 3:
                    return FakeResponse(json_value={"requests": []})
                if len(self.calls) == 4:
                    raise requests.ReadTimeout("lost response")
                return FakeResponse(
                    json_value={
                        "requests": [
                            self_test.request_payload(
                                request={"id": 73, "external_source": "huey:42"}
                            )
                        ]
                    }
                )

        self_test = self
        session = LostPostSession()
        response = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=session
        ).submit("ebooks", "Dune", "Frank Herbert", 42)

        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["external_id"], "73")
        self.assertEqual(
            [call[0] for call in session.calls],
            ["GET", "GET", "GET", "POST", "GET"],
        )

    def test_lost_create_and_unavailable_recovery_is_explicitly_uncertain(self):
        class UncertainPostSession(FakeSession):
            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                if len(self.calls) == 1:
                    return FakeResponse(json_value={"results": [self_test.metadata_result()]})
                if len(self.calls) in {2, 3}:
                    return FakeResponse(json_value={"requests": []})
                raise requests.ReadTimeout("lost response")

        self_test = self
        client = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=UncertainPostSession()
        )
        with self.assertRaisesRegex(SubmissionUncertain, "correlation recovery"):
            client.submit("ebooks", "Dune", "Frank Herbert", 42)

    def test_server_error_post_is_uncertain_until_later_correlation(self):
        session = FakeSession(
            [
                FakeResponse(json_value={"results": [self.metadata_result()]}),
                FakeResponse(json_value={"requests": []}),
                FakeResponse(json_value={"requests": []}),
                FakeResponse(status=500),
                FakeResponse(json_value={"requests": []}),
            ]
        )
        with self.assertRaises(SubmissionUncertain):
            ShelfarrClient(
                "http://shelfarr", "shf_secret", session=session
            ).submit("ebooks", "Dune", "Frank Herbert", 42)

    def test_malformed_success_post_is_uncertain_until_later_correlation(self):
        session = FakeSession(
            [
                FakeResponse(json_value={"results": [self.metadata_result()]}),
                FakeResponse(json_value={"requests": []}),
                FakeResponse(json_value={"requests": []}),
                FakeResponse(json_value={"unexpected": True}),
                FakeResponse(json_value={"requests": []}),
            ]
        )
        with self.assertRaises(SubmissionUncertain):
            ShelfarrClient(
                "http://shelfarr", "shf_secret", session=session
            ).submit("ebooks", "Dune", "Frank Herbert", 42)

    def test_lost_response_with_mismatched_correlation_remains_uncertain(self):
        mismatched = self.request_payload(
            book={"title": "Dune", "book_type": "audiobook"},
            request={"id": 73, "external_source": "huey:42"},
        )
        session = FakeSession(
            [
                FakeResponse(json_value={"results": [self.metadata_result()]}),
                FakeResponse(json_value={"requests": []}),
                FakeResponse(json_value={"requests": []}),
                FakeResponse(json_value={"requests": [mismatched]}),
                FakeResponse(json_value={"requests": [mismatched]}),
            ]
        )

        with self.assertRaises(SubmissionUncertain):
            ShelfarrClient(
                "http://shelfarr", "shf_secret", session=session
            ).submit("ebooks", "Dune", "Frank Herbert", 42)

        self.assertEqual(
            [call[0] for call in session.calls],
            ["GET", "GET", "GET", "POST", "GET"],
        )

    def test_request_status_probe_is_read_only_and_validated(self):
        session = FakeSession(
            [FakeResponse(json_value=self.request_payload(status="processing"))]
        )
        response = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=session
        ).get_request("73")
        self.assertEqual(response["status"], "processing")
        self.assertEqual(session.calls[0][0], "GET")
        self.assertTrue(session.calls[0][1].endswith("/api/v1/requests/73"))

        with self.assertRaisesRegex(ServiceError, "invalid request ID"):
            ShelfarrClient("http://shelfarr", "shf_secret").get_request("bad")

    def test_cancel_requires_confirmed_terminal_response(self):
        session = FakeSession(
            [FakeResponse(json_value=self.request_payload(status="failed"))]
        )
        response = ShelfarrClient(
            "http://shelfarr", "shf_secret", session=session
        ).cancel_request(73)
        self.assertEqual(response["status"], "failed")
        self.assertEqual(session.calls[0][0], "DELETE")

    def test_terminal_attention_result_requires_manual_intervention_route(self):
        response = ShelfarrClient._submission_result(
            self.request_payload(status="failed", attention_needed=True),
            book_type="ebook",
            fallback_title="Dune",
        )
        self.assertEqual(response["status"], "failed")
        self.assertTrue(response["manual_intervention"])

    def test_invalid_or_unsupported_response_fails_closed(self):
        for response in (
            {"id": 73, "status": "unknown", "book": {}},
            {"id": 73, "status": "pending", "book": None},
        ):
            with self.subTest(response=response):
                client = ShelfarrClient(
                    "http://shelfarr",
                    "shf_secret",
                    session=FakeSession([FakeResponse(json_value=response)]),
                )
                with self.assertRaisesRegex(ServiceError, "invalid request response"):
                    client.get_request(73)


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
