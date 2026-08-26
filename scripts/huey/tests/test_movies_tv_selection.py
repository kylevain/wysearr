import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

from clients import RadarrClient, ServiceError, SonarrClient
from database import RequestStore
from huey import (
    _reply_targets_huey_candidate_prompt,
    _selection_ordinal,
    format_candidate_prompt,
)
from orchestrator import RequestProcessor
from services import ServiceRegistry


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeSession:
    """Serve canned ARR payloads and record every mutating call."""

    def __init__(self, lookups, entities=None):
        self.lookups = lookups
        self.entities = entities or {}
        self.calls = []
        self.next_id = 700

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        if "/lookup" in url:
            term = kwargs.get("params", {}).get("term", "")
            return FakeResponse(self.lookups.get(term, []))
        if method == "POST" and url.endswith("/command"):
            return FakeResponse({"id": 1})
        if method == "POST":
            payload = dict(kwargs.get("json") or {})
            payload["id"] = self.next_id
            self.next_id += 1
            return FakeResponse(payload)
        if method == "PUT":
            return FakeResponse(dict(kwargs.get("json") or {}))
        if method == "GET" and "/rootfolder" in url:
            return FakeResponse([{"id": 1, "path": "/media/movies"}])
        if method == "GET" and "/qualityprofile" in url:
            return FakeResponse([{"id": 4, "name": "HD"}])
        if method == "GET":
            entity_id = int(url.rstrip("/").rsplit("/", 1)[-1])
            return FakeResponse(self.entities.get(entity_id, {"id": entity_id}))
        raise AssertionError(f"Unexpected {method} {url}")


WRECKING_CREW = [
    {"title": "The Wrecking Crew", "year": 2026, "tmdbId": 111},
    {"title": "The Wrecking Crew", "year": 1968, "tmdbId": 222},
    {"title": "The Wrecking Crew", "year": 2008, "tmdbId": 333},
]


def radarr(session):
    return RadarrClient("http://radarr:7878", "key", session=session)


class MoviesTvPickerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = RequestStore(Path(self.temporary.name) / "huey.db")
        self.store.initialize()
        self.session = FakeSession(
            {
                "the wrecking crew": WRECKING_CREW,
                "tmdb:111": [WRECKING_CREW[0]],
                "tmdb:222": [WRECKING_CREW[1]],
                "tmdb:333": [WRECKING_CREW[2]],
            }
        )
        self.registry = ServiceRegistry()
        self.registry._clients["radarr"] = radarr(self.session)
        self.processor = RequestProcessor(self.store, services=self.registry)
        self.delivery = {
            "discord_user_id": "1",
            "discord_username": "kyle",
            "channel_id": "2",
            "message_id": "142",
            "media_type": "movies-tv",
            "content": "movie: the wrecking crew",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_ambiguous_lookup_persists_a_candidate_prompt(self):
        response = self.processor.process(self.delivery)

        self.assertEqual(response["status"], "awaiting_selection")
        self.assertEqual(len(response["selection_proposal"]), 3)
        self.assertEqual(
            [option["work_id"] for option in response["selection_proposal"]],
            ["radarr:tmdb:111", "radarr:tmdb:333", "radarr:tmdb:222"],
        )
        self.assertEqual(
            [option["book_type"] for option in response["selection_proposal"]],
            ["movie", "movie", "movie"],
        )
        saved = self.store.get_request(response["request_id"])
        self.assertEqual(saved["status"], "awaiting_selection")
        self.assertEqual(saved["service"], "radarr")
        # Nothing was added to Radarr while the requester is still choosing.
        self.assertEqual(
            [call for call in self.session.calls if call[0] != "GET"], []
        )

    def test_prompt_text_is_recognised_as_a_reply_target(self):
        response = self.processor.process(self.delivery)
        prompt = format_candidate_prompt("movies-tv", response, ttl_seconds=900)

        self.assertIn("needs one title choice", prompt)
        self.assertIn("1. The Wrecking Crew (2026)", prompt)
        self.assertIn("3. The Wrecking Crew (1968)", prompt)

        client = SimpleNamespace(user=SimpleNamespace(id=9))
        message = SimpleNamespace(
            reference=SimpleNamespace(
                resolved=SimpleNamespace(
                    author=SimpleNamespace(id=9), content=prompt
                ),
                cached_message=None,
            )
        )
        self.assertIs(
            asyncio.run(
                _reply_targets_huey_candidate_prompt(client, message, "5000")
            ),
            True,
        )

    def test_numeric_reply_adds_the_confirmed_identity(self):
        response = self.processor.process(self.delivery)
        request_id = response["request_id"]
        self.assertTrue(self.store.bind_candidate_prompt(request_id, "5000"))

        confirmed = self.processor.process_candidate_reply(
            {
                **self.delivery,
                "message_id": "143",
                "prompt_message_id": "5000",
                "ordinal": _selection_ordinal("2"),
            }
        )

        self.assertEqual(confirmed["selection_outcome"], "claimed")
        self.assertEqual(confirmed["status"], "queued")
        saved = self.store.get_request(request_id)
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["external_id"], "700")
        self.assertEqual(saved["external_title"], "The Wrecking Crew")
        # The 2008 entry the requester picked is what reached Radarr.
        added = [
            call for call in self.session.calls
            if call[0] == "POST" and call[1].endswith("/movie")
        ]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0][2]["tmdbId"], 333)
        self.assertEqual(added[0][2]["rootFolderPath"], "/media/movies")

    def test_dispatch_boundary_is_crossed_before_the_add(self):
        response = self.processor.process(self.delivery)
        request_id = response["request_id"]
        self.store.bind_candidate_prompt(request_id, "5000")
        self.processor.process_candidate_reply(
            {
                **self.delivery,
                "message_id": "143",
                "prompt_message_id": "5000",
                "ordinal": 1,
            }
        )
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT status, dispatch_started_at FROM candidate_confirmations "
                "WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        self.assertEqual(row["status"], "claimed")
        self.assertIsNotNone(row["dispatch_started_at"])

    def test_replayed_reply_is_not_a_second_request(self):
        response = self.processor.process(self.delivery)
        self.store.bind_candidate_prompt(response["request_id"], "5000")
        selection = {
            **self.delivery,
            "message_id": "143",
            "prompt_message_id": "5000",
            "ordinal": 1,
        }
        self.processor.process_candidate_reply(selection)
        replay = self.processor.process_candidate_reply(selection)
        self.assertEqual(replay["selection_outcome"], "duplicate")

    def test_out_of_range_number_is_rejected(self):
        response = self.processor.process(self.delivery)
        self.store.bind_candidate_prompt(response["request_id"], "5000")
        invalid = self.processor.process_candidate_reply(
            {
                **self.delivery,
                "message_id": "143",
                "prompt_message_id": "5000",
                "ordinal": 9,
            }
        )
        self.assertEqual(invalid["selection_outcome"], "invalid")

    def test_confident_match_still_auto_queues(self):
        session = FakeSession({"arrival 2016": [{"title": "Arrival", "year": 2016, "tmdbId": 44}]})
        registry = ServiceRegistry()
        registry._clients["radarr"] = radarr(session)
        processor = RequestProcessor(self.store, services=registry)
        response = processor.process(
            {**self.delivery, "message_id": "150", "content": "movie: arrival 2016"}
        )
        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["selection_proposal"], ())

    def test_single_weak_result_keeps_the_existing_message(self):
        session = FakeSession({"something obscure": [{"title": "Wholly Unrelated", "year": 1999, "tmdbId": 5}]})
        registry = ServiceRegistry()
        registry._clients["radarr"] = radarr(session)
        processor = RequestProcessor(self.store, services=registry)
        response = processor.process(
            {**self.delivery, "message_id": "151", "content": "movie: something obscure"}
        )
        self.assertEqual(response["status"], "needs_selection")
        self.assertIn("could not identify one safe match", response["message"])

    def test_sonarr_offers_tvdb_identities(self):
        session = FakeSession(
            {
                "the office": [
                    {"title": "The Office", "year": 2005, "tvdbId": 73244},
                    {"title": "The Office", "year": 2001, "tvdbId": 78107},
                ]
            }
        )
        registry = ServiceRegistry()
        registry._clients["sonarr"] = SonarrClient(
            "http://sonarr:8989", "key", session=session
        )
        processor = RequestProcessor(self.store, services=registry)
        response = processor.process(
            {**self.delivery, "message_id": "160", "content": "tv: the office"}
        )
        self.assertEqual(response["status"], "awaiting_selection")
        self.assertEqual(
            [option["work_id"] for option in response["selection_proposal"]],
            ["sonarr:tvdb:73244", "sonarr:tvdb:78107"],
        )
        self.assertEqual(
            self.store.get_request(response["request_id"])["service"], "sonarr"
        )

    def test_selected_identity_must_resolve_to_exactly_one_entry(self):
        client = radarr(FakeSession({"tmdb:999": []}))
        with self.assertRaises(ServiceError):
            client.submit_selected("radarr:tmdb:999")

    def test_foreign_identity_is_refused(self):
        client = radarr(FakeSession({}))
        with self.assertRaises(ServiceError):
            client.submit_selected("sonarr:tvdb:1")


class CandidateOptionMigrationTests(unittest.TestCase):
    NARROW_TABLE = """
        CREATE TABLE candidate_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            confirmation_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 3),
            fingerprint TEXT NOT NULL
                CHECK (length(fingerprint) = 64 AND fingerprint NOT GLOB '*[^0-9a-f]*'),
            label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 300),
            title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 160),
            author TEXT CHECK (author IS NULL OR length(author) BETWEEN 1 AND 160),
            year INTEGER CHECK (year IS NULL OR year BETWEEN 0 AND 9999),
            book_type TEXT NOT NULL CHECK (book_type IN ('ebook', 'audiobook')),
            candidate_json TEXT NOT NULL CHECK (length(candidate_json) BETWEEN 2 AND 4096),
            FOREIGN KEY(confirmation_id) REFERENCES candidate_confirmations(id) ON DELETE CASCADE,
            UNIQUE(confirmation_id, ordinal),
            UNIQUE(confirmation_id, fingerprint)
        )
    """

    def test_existing_rows_survive_and_movie_options_become_insertable(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "huey.db"

        store = RequestStore(path)
        store.initialize()
        # Rebuild the table with the pre-migration constraint and seed a row.
        with sqlite3.connect(path) as setup:
            setup.execute("PRAGMA foreign_keys = OFF")
            setup.execute("DROP TABLE candidate_options")
            setup.execute(self.NARROW_TABLE)
            setup.execute(
                "INSERT INTO candidate_confirmations "
                "(id, request_id, shelfarr_correlation, created_at, updated_at, expires_at) "
                "VALUES (1, 1, 'huey:1', '2026-01-01 00:00:00', "
                "'2026-01-01 00:00:00', '2026-01-01 00:15:00')"
            )
            setup.execute(
                "INSERT INTO candidate_options "
                "(confirmation_id, ordinal, fingerprint, label, title, author, "
                "year, book_type, candidate_json) "
                "VALUES (1, 1, ?, 'Dune', 'Dune', 'Frank Herbert', 1965, 'ebook', '{}')",
                ("a" * 64,),
            )
            setup.commit()

        RequestStore(path).initialize()

        with sqlite3.connect(path) as verify:
            verify.row_factory = sqlite3.Row
            rows = verify.execute(
                "SELECT ordinal, title, book_type FROM candidate_options"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["title"], "Dune")
            self.assertEqual(rows[0]["book_type"], "ebook")
            verify.execute(
                "INSERT INTO candidate_options "
                "(confirmation_id, ordinal, fingerprint, label, title, author, "
                "year, book_type, candidate_json) "
                "VALUES (1, 2, ?, 'Wrecking', 'Wrecking', NULL, 2026, 'movie', '{}')",
                ("b" * 64,),
            )
            # The ordinal bound is deliberately unchanged by the migration.
            with self.assertRaises(sqlite3.IntegrityError):
                verify.execute(
                    "INSERT INTO candidate_options "
                    "(confirmation_id, ordinal, fingerprint, label, title, author, "
                    "year, book_type, candidate_json) "
                    "VALUES (1, 4, ?, 'Too many', 'Too many', NULL, 2026, 'movie', '{}')",
                    ("c" * 64,),
                )

    def test_migration_is_idempotent(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "huey.db"
        for _ in range(3):
            RequestStore(path).initialize()
        with sqlite3.connect(path) as verify:
            sql = verify.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'candidate_options'"
            ).fetchone()[0]
        self.assertIn("'movie'", sql)
        self.assertIn("ordinal BETWEEN 1 AND 3", sql)


if __name__ == "__main__":
    unittest.main()
