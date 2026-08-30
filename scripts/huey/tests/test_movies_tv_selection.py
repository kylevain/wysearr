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
    _is_selection_rejection,
    _reply_targets_huey_candidate_prompt,
    _selection_ordinal,
    format_candidate_prompt,
    selection_correction,
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


class PickerFixture:
    """The shared ARR picker fixture, without any test methods."""

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


class MoviesTvPickerTests(PickerFixture, unittest.TestCase):
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

    def redriven(self, content, results, *, message_id):
        session = FakeSession({content.split(": ", 1)[1]: results})
        registry = ServiceRegistry()
        registry._clients["radarr"] = radarr(session)
        processor = RequestProcessor(self.store, services=registry)
        response = processor.process(
            {**self.delivery, "message_id": message_id, "content": content}
        )
        return response, session

    def test_a_lone_strong_result_is_offered_as_a_confirmation(self):
        # Backlog row: the requester's year is right by every consumer source
        # and TMDb disagrees, leaving exactly one candidate at 0.98. Refusing
        # to mention it because there is only one of it is the same error as
        # discarding ranked candidates.
        response, session = self.redriven(
            "movie: personal shopper 2017",
            [{"title": "Personal Shopper", "year": 2016, "tmdbId": 381518}],
            message_id="170",
        )

        self.assertEqual(response["status"], "awaiting_selection")
        self.assertEqual(len(response["selection_proposal"]), 1)
        self.assertEqual(
            response["selection_proposal"][0]["work_id"], "radarr:tmdb:381518"
        )
        saved = self.store.get_request(response["request_id"])
        self.assertEqual(saved["status"], "awaiting_selection")
        # Confirming is not accepting: nothing reached Radarr.
        self.assertEqual([call for call in session.calls if call[0] != "GET"], [])

    def test_a_lone_result_below_the_confirmation_floor_still_bails(self):
        # 0.596 against "The Wild Life". Here the identity itself is what is
        # in doubt, not the year, so a confirmation would be a backdoor to
        # accepting what the gate refused.
        response, _ = self.redriven(
            "movie: the wildlife 1984",
            [{"title": "The Wild Life", "year": 1984, "tmdbId": 30765}],
            message_id="171",
        )

        self.assertEqual(response["status"], "needs_selection")
        self.assertEqual(response["selection_proposal"], ())

    def test_a_lone_confirmation_can_be_claimed_with_one(self):
        response, _ = self.redriven(
            "movie: crime zone 1989",
            [{"title": "Crime Zone", "year": 1988, "tmdbId": 124506}],
            message_id="172",
        )
        request_id = response["request_id"]
        self.assertTrue(self.store.bind_candidate_prompt(request_id, "880000111222"))

        claim = self.store.claim_candidate_selection(
            prompt_message_id="880000111222",
            reply_message_id="880000111333",
            discord_user_id="1",
            channel_id="2",
            ordinal=1,
        )
        self.assertEqual(claim["outcome"], "claimed")

    def test_a_confirmation_prompt_asks_instead_of_listing(self):
        one = format_candidate_prompt(
            "movies-tv",
            {
                "request_id": 231,
                "service": "radarr",
                "selection_proposal": [
                    {"label": "Personal Shopper (2016)", "work_id": "radarr:tmdb:381518"}
                ],
            },
            ttl_seconds=900,
        )

        self.assertIn("Did you mean Personal Shopper (2016)?", one)
        self.assertIn("send 1 to confirm", one)
        self.assertNotIn("1. ", one)

    def test_a_multi_option_prompt_keeps_the_numbered_list(self):
        many = format_candidate_prompt(
            "movies-tv",
            {
                "request_id": 232,
                "service": "radarr",
                "selection_proposal": [
                    {"label": "The Thing (1982)", "work_id": "radarr:tmdb:1091"},
                    {"label": "The Thing (2011)", "work_id": "radarr:tmdb:54580"},
                ],
            },
            ttl_seconds=900,
        )

        self.assertIn("1. The Thing (1982)", many)
        self.assertIn("2. The Thing (2011)", many)
        self.assertNotIn("Did you mean", many)

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


class RejectionOutcomeMigrationTests(unittest.TestCase):
    """The replies CHECK is rebuilt so 'rejected' can be recorded honestly."""

    NARROW_TABLE = """
        CREATE TABLE candidate_confirmation_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            confirmation_id INTEGER NOT NULL,
            reply_message_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            discord_user_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            outcome TEXT NOT NULL
                CHECK (outcome IN ('claimed', 'invalid', 'expired', 'duplicate')),
            FOREIGN KEY(confirmation_id)
                REFERENCES candidate_confirmations(id) ON DELETE CASCADE
        )
    """

    def old_schema_store(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "huey.db"
        store = RequestStore(path)
        store.initialize()
        with sqlite3.connect(path) as setup:
            setup.execute("PRAGMA foreign_keys = OFF")
            setup.execute("DROP TABLE candidate_confirmation_replies")
            setup.execute(self.NARROW_TABLE)
            setup.execute(
                "INSERT INTO candidate_confirmations "
                "(id, request_id, shelfarr_correlation, created_at, updated_at, "
                "expires_at) VALUES (1, 1, 'huey:1', '2026-01-01 00:00:00', "
                "'2026-01-01 00:00:00', '2026-01-01 00:15:00')"
            )
            setup.execute(
                "INSERT INTO candidate_confirmation_replies "
                "(confirmation_id, reply_message_id, created_at, discord_user_id, "
                "channel_id, ordinal, outcome) "
                "VALUES (1, '9001', '2026-01-01 00:01:00', '1', '2', 2, 'claimed')"
            )
        return path

    def test_the_old_constraint_refuses_a_rejection(self):
        path = self.old_schema_store()

        with sqlite3.connect(path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO candidate_confirmation_replies "
                    "(confirmation_id, reply_message_id, created_at, "
                    "discord_user_id, channel_id, ordinal, outcome) "
                    "VALUES (1, '9002', '2026-01-01 00:02:00', '1', '2', 0, 'rejected')"
                )

    def test_migrating_admits_rejections_and_keeps_existing_replies(self):
        path = self.old_schema_store()

        RequestStore(path).initialize()

        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO candidate_confirmation_replies "
                "(confirmation_id, reply_message_id, created_at, discord_user_id, "
                "channel_id, ordinal, outcome) "
                "VALUES (1, '9002', '2026-01-01 00:02:00', '1', '2', 0, 'rejected')"
            )
            rows = dict(
                connection.execute(
                    "SELECT reply_message_id, outcome "
                    "FROM candidate_confirmation_replies"
                ).fetchall()
            )
        self.assertEqual(rows, {"9001": "claimed", "9002": "rejected"})

    def test_migrating_twice_is_a_no_op(self):
        path = self.old_schema_store()

        RequestStore(path).initialize()
        RequestStore(path).initialize()

        with sqlite3.connect(path) as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM candidate_confirmation_replies"
            ).fetchone()[0]
            self.assertEqual(total, 1)
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = "
                    "'candidate_confirmation_replies_migrated'"
                ).fetchone()
            )

    def test_a_still_invalid_outcome_is_refused_after_migrating(self):
        path = self.old_schema_store()
        RequestStore(path).initialize()

        with sqlite3.connect(path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO candidate_confirmation_replies "
                    "(confirmation_id, reply_message_id, created_at, "
                    "discord_user_id, channel_id, ordinal, outcome) "
                    "VALUES (1, '9003', '2026-01-01 00:03:00', '1', '2', 0, 'banana')"
                )


class RejectionTokenTests(unittest.TestCase):
    """The token must not collide with the ordinal parser's invalid sentinel."""

    def test_zero_is_indistinguishable_from_an_unreadable_reply(self):
        # Why rejection is not spelled "0": _SELECTION_ORDINAL is ^[1-9][0-9]*$,
        # so these all already collapse to the same value.
        for unreadable in ("0", "banana", "", "0001", " 1", "1234567"):
            self.assertEqual(_selection_ordinal(unreadable), 0)

    def test_the_rejection_tokens_are_accepted(self):
        for token in ("n", "N", "no", "No", "none", " n "):
            self.assertTrue(_is_selection_rejection(token))

    def test_an_ordinal_is_never_a_rejection(self):
        for token in ("1", "2", "3", "0", "banana", "nine"):
            self.assertFalse(_is_selection_rejection(token))


class RejectionPromptTests(unittest.TestCase):
    def prompt(self, labels):
        return format_candidate_prompt(
            "movies-tv",
            {
                "request_id": 113,
                "service": "sonarr",
                "selection_proposal": [
                    {"label": label, "work_id": f"sonarr:tvdb:{index}"}
                    for index, label in enumerate(labels, start=1)
                ],
            },
            ttl_seconds=900,
        )

    def test_the_picker_offers_a_way_out(self):
        prompt = self.prompt(
            ["Brooklyn DA (2013)", "Brooklyn 11223 (2012)", "Brooklyn South (1997)"]
        )

        self.assertIn("or n if none of these match.", prompt)
        self.assertIn("1. Brooklyn DA (2013)", prompt)

    def test_the_confirmation_offers_a_way_to_say_no(self):
        prompt = self.prompt(["Brooklyn Nine-Nine (2013)"])

        self.assertIn("Did you mean Brooklyn Nine-Nine (2013)?", prompt)
        self.assertIn("or n if that is not it.", prompt)

    def test_both_prompts_are_recognised_as_reply_targets(self):
        client = SimpleNamespace(user=SimpleNamespace(id=9))
        for labels in (
            ["Brooklyn DA (2013)", "Brooklyn South (1997)"],
            # The single-option prompt was never matched here before.
            ["Brooklyn Nine-Nine (2013)"],
        ):
            message = SimpleNamespace(
                reference=SimpleNamespace(
                    resolved=SimpleNamespace(
                        author=SimpleNamespace(id=9), content=self.prompt(labels)
                    ),
                    cached_message=None,
                )
            )
            self.assertIs(
                asyncio.run(
                    _reply_targets_huey_candidate_prompt(client, message, "5000")
                ),
                True,
                msg=f"{len(labels)} option(s) not recognised",
            )

    def test_the_acknowledgement_names_the_released_request(self):
        text = selection_correction("rejected", 113)

        self.assertIn("#113", text)
        self.assertIn("more detail", text)


class RejectionFlowTests(PickerFixture, unittest.TestCase):
    """Rejection through the processor, on the real picker fixture."""

    def prompted(self):
        response = self.processor.process(self.delivery)
        self.assertEqual(response["status"], "awaiting_selection")
        request_id = response["request_id"]
        self.assertTrue(self.store.bind_candidate_prompt(request_id, "5000"))
        return request_id

    def reject(self, message_id="144", **overrides):
        return self.processor.process_candidate_rejection(
            {
                **self.delivery,
                "message_id": message_id,
                "prompt_message_id": "5000",
                **overrides,
            }
        )

    def test_rejection_releases_the_row_at_once(self):
        request_id = self.prompted()

        outcome = self.reject()

        self.assertEqual(outcome["selection_outcome"], "rejected")
        self.assertEqual(outcome["request_id"], request_id)
        saved = self.store.get_request(request_id)
        self.assertEqual(saved["status"], "needs_selection")
        # Nothing was added to Radarr on the way out.
        self.assertEqual([call for call in self.session.calls if call[0] != "GET"], [])

    def test_rejection_is_recorded_as_an_answer_not_a_mistake(self):
        request_id = self.prompted()
        self.reject()

        with self.store.connect() as connection:
            reply = connection.execute(
                "SELECT outcome FROM candidate_confirmation_replies"
            ).fetchone()
            events = [
                row["event_type"]
                for row in connection.execute(
                    "SELECT event_type FROM events WHERE request_id = ?", (request_id,)
                )
            ]
        self.assertEqual(reply["outcome"], "rejected")
        self.assertIn("selection_rejected", events)

    def test_the_prompt_becomes_terminal_so_the_row_can_be_asked_again(self):
        request_id = self.prompted()
        self.reject()

        confirmation = self.store.get_candidate_confirmation(request_id)
        self.assertEqual(confirmation["status"], "failed")
        self.store.transition(request_id, "processing", "Re-driving", service="radarr")
        self.store.create_candidate_confirmation(
            request_id,
            [
                {
                    "fingerprint": "d" * 64,
                    "label": "The Wrecking Crew (1968)",
                    "work_id": "radarr:tmdb:222",
                    "source_work_ids": ["radarr:tmdb:222"],
                    "title": "The Wrecking Crew",
                    "author": None,
                    "year": 1968,
                    "content_kind": "video",
                    "media_type": "movies-tv",
                    "book_type": "movie",
                }
            ],
        )
        self.assertEqual(
            self.store.get_request(request_id)["status"], "awaiting_selection"
        )

    def test_a_redelivered_rejection_changes_nothing(self):
        self.prompted()
        self.reject(message_id="144")

        again = self.reject(message_id="144")

        self.assertEqual(again["selection_outcome"], "duplicate")

    def test_another_user_cannot_reject_someone_elses_prompt(self):
        request_id = self.prompted()

        outcome = self.reject(message_id="145", discord_user_id="99")

        self.assertEqual(outcome["selection_outcome"], "invalid")
        self.assertEqual(
            self.store.get_request(request_id)["status"], "awaiting_selection"
        )

    def test_a_rejection_from_another_channel_is_refused(self):
        request_id = self.prompted()

        outcome = self.reject(message_id="146", channel_id="999")

        self.assertEqual(outcome["selection_outcome"], "invalid")
        self.assertEqual(
            self.store.get_request(request_id)["status"], "awaiting_selection"
        )

    def test_rejecting_after_choosing_does_not_undo_the_acquisition(self):
        request_id = self.prompted()
        claimed = self.processor.process_candidate_reply(
            {
                **self.delivery,
                "message_id": "147",
                "prompt_message_id": "5000",
                "ordinal": _selection_ordinal("2"),
            }
        )
        self.assertEqual(claimed["selection_outcome"], "claimed")

        late = self.reject(message_id="148")

        self.assertEqual(late["selection_outcome"], "duplicate")
        self.assertEqual(self.store.get_request(request_id)["status"], "queued")

    def test_an_unknown_prompt_is_not_found(self):
        self.prompted()

        outcome = self.reject(message_id="149", prompt_message_id="404404404")

        self.assertEqual(outcome["selection_outcome"], "not_found")


if __name__ == "__main__":
    unittest.main()
