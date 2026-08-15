import asyncio
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

from clients import ServiceError, SubmissionUncertain
from database import EbookCascadeStateError, EbookIdentityCollision, RequestStore
from huey import (
    _OneShotAsyncRecovery,
    build_client,
    reconcile_lazylibrarian_requests,
    reconcile_shelfarr_requests,
)
from orchestrator import RequestProcessor
from results import result
from services import ServiceRegistry


LL_BOOK_ID = "OL893415W"
LL_HASH = "1" * 40
SHELFARR_WORK_ID = "openlibrary:OL893415W"


def identity(
    *,
    fingerprint="a" * 64,
    work_id="lazylibrarian:" + "a" * 64,
    title="Dune",
    author="Frank Herbert",
    year=1965,
    source_work_ids=None,
):
    return {
        "fingerprint": fingerprint,
        "label": f"{title} by {author or 'Unknown'}",
        "work_id": work_id,
        "source_work_ids": tuple(source_work_ids or (work_id,)),
        "title": title,
        "author": author,
        "year": year,
        "content_kind": "book",
        "media_type": "ebooks",
        "book_type": "ebook",
    }


def shelfarr_identity(**overrides):
    values = {
        "fingerprint": "b" * 64,
        "work_id": SHELFARR_WORK_ID,
        "title": "Dune",
        "author": "Frank Herbert",
        "year": 1965,
    }
    values.update(overrides)
    return identity(**values)


def delivery(message_id="100", content="Dune by Frank Herbert"):
    return {
        "discord_user_id": "10",
        "discord_username": "reader",
        "channel_id": "20",
        "message_id": str(message_id),
        "media_type": "ebooks",
        "content": content,
    }


class ScriptedBackends:
    def __init__(self, actions, backends=("lazylibrarian", "shelfarr")):
        self.ebook_acquisition_backends = tuple(backends)
        self.actions = list(actions)
        self.calls = []

    def submit_ebook_backend(
        self,
        request,
        backend,
        *,
        resolved_identity=None,
        selected_candidate=None,
    ):
        self.calls.append(
            {
                "backend": backend,
                "resolved_identity": resolved_identity,
                "selected_candidate": selected_candidate,
            }
        )
        if not self.actions:
            raise AssertionError("Unexpected ebook backend dispatch")
        action = self.actions.pop(0)
        return action(
            request,
            backend,
            resolved_identity,
            selected_candidate,
        )


def ll_success(work=None, *, download_id=LL_HASH):
    resolved = work or identity()

    def action(request, backend, _authoritative, _selected):
        assert backend == "lazylibrarian"
        request["_on_resolved"](resolved, LL_BOOK_ID)
        request["_before_dispatch"](LL_BOOK_ID)
        return result(
            "queued",
            "backend accepted",
            service=backend,
            external_id=download_id,
            external_title=resolved["title"],
            external_status="queued",
            resolved_identity=resolved,
        )

    return action


def ll_miss(work=None):
    resolved = work or identity()

    def action(request, backend, _authoritative, _selected):
        assert backend == "lazylibrarian"
        request["_on_resolved"](resolved, LL_BOOK_ID)
        return result(
            "needs_selection",
            "pre-mutation provider probe found no usable release",
            service=backend,
            external_status="not_found",
            backend_outcome="miss",
            resolved_identity=resolved,
        )

    return action


def metadata_miss(request, backend, _authoritative, _selected):
    return result(
        "needs_selection",
        "metadata source had no result",
        service=backend,
        backend_outcome="miss",
    )


def shelfarr_success(work=None, *, request_id="73", mutate=True):
    resolved = work or shelfarr_identity()

    def action(request, backend, authoritative, _selected):
        assert backend == "shelfarr"
        request["_on_resolved"](resolved, resolved["work_id"])
        if mutate:
            request["_before_dispatch"]()
        return result(
            "queued",
            "backend accepted",
            service=backend,
            external_id=request_id,
            external_title=resolved["title"],
            external_status="pending",
            resolved_identity=authoritative or resolved,
        )

    return action


def shelfarr_miss(request, backend, authoritative, _selected):
    return result(
        "needs_selection",
        "no exact provider mapping",
        service=backend,
        backend_outcome="miss",
        resolved_identity=authoritative,
    )


class EbookCascadeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = RequestStore(Path(self.temporary.name) / "huey.db")
        self.store.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def test_atomic_intake_stages_one_generic_acceptance_before_dispatch(self):
        observed = {}

        def action(request, backend, _authoritative, _selected):
            observed["request"] = self.store.get_request(int(request["id"]))
            observed["cascade"] = self.store.get_ebook_cascade(int(request["id"]))
            observed["deliveries"] = self.store.pending_notification_deliveries()
            return ll_success()(request, backend, None, None)

        services = ScriptedBackends([action])
        response = RequestProcessor(self.store, services=services).process(delivery())

        self.assertEqual(response["status"], "queued")
        self.assertEqual(observed["request"]["status"], "processing")
        self.assertEqual(observed["cascade"]["policy"], ("lazylibrarian", "shelfarr"))
        accepted = [
            row for row in observed["deliveries"]
            if row["event_key"] == "request_accepted"
        ]
        self.assertEqual(len(accepted), 1)
        self.assertIn("Huey is searching", accepted[0]["message"])

    def test_primary_success_never_calls_fallback_and_records_bookbot(self):
        services = ScriptedBackends([ll_success()])
        response = RequestProcessor(self.store, services=services).process(delivery())

        self.assertEqual([call["backend"] for call in services.calls], ["lazylibrarian"])
        cascade = self.store.get_ebook_cascade(response["request_id"])
        self.assertEqual(cascade["state"], "queued")
        self.assertEqual(cascade["final_backend"], "lazylibrarian")
        self.assertEqual(cascade["finalizer"], "bookbot")
        self.assertEqual(cascade["attempts"][0]["status"], "queued")
        self.assertIsNotNone(cascade["attempts"][0]["mutation_started_at"])
        self.assertIsNotNone(cascade["attempts"][0]["mutation_resolved_at"])
        self.assertEqual(cascade["attempts"][1]["status"], "pending")

    def test_primary_clean_miss_falls_back_with_same_authoritative_identity(self):
        authoritative = identity(year=None)
        enriched = shelfarr_identity(year=1965)
        services = ScriptedBackends([ll_miss(authoritative), shelfarr_success(enriched)])
        response = RequestProcessor(self.store, services=services).process(delivery())

        self.assertEqual(response["status"], "queued")
        self.assertEqual(
            [call["backend"] for call in services.calls],
            ["lazylibrarian", "shelfarr"],
        )
        self.assertEqual(services.calls[1]["resolved_identity"]["year"], None)
        cascade = self.store.get_ebook_cascade(response["request_id"])
        self.assertEqual(cascade["identity"]["year"], None)
        self.assertEqual(cascade["attempts"][0]["status"], "miss")
        self.assertEqual(cascade["attempts"][1]["backend_identity"], SHELFARR_WORK_ID)
        self.assertEqual(cascade["final_backend"], "shelfarr")
        self.assertEqual(cascade["finalizer"], "shelfarr")

    def test_disabled_or_unavailable_primary_is_a_pre_mutation_skip(self):
        def unavailable(*_args):
            raise ServiceError("disabled")

        services = ScriptedBackends([unavailable, shelfarr_success()])
        response = RequestProcessor(self.store, services=services).process(delivery())

        cascade = self.store.get_ebook_cascade(response["request_id"])
        self.assertEqual(response["status"], "queued")
        self.assertEqual(cascade["attempts"][0]["status"], "unavailable")
        self.assertIsNone(cascade["attempts"][0]["mutation_started_at"])
        self.assertEqual(cascade["final_backend"], "shelfarr")

    def test_post_mutation_primary_no_handoff_is_quarantined_without_fallback(self):
        def uncertain(request, backend, _authoritative, _selected):
            request["_on_resolved"](identity(), LL_BOOK_ID)
            request["_before_dispatch"](LL_BOOK_ID)
            raise SubmissionUncertain("no exact post-search handoff evidence")

        services = ScriptedBackends([uncertain, shelfarr_success()])
        response = RequestProcessor(self.store, services=services).process(delivery())

        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["external_status"], "submission_uncertain")
        self.assertEqual([call["backend"] for call in services.calls], ["lazylibrarian"])
        cascade = self.store.get_ebook_cascade(response["request_id"])
        self.assertEqual(cascade["state"], "uncertain")
        self.assertEqual(cascade["mutation_backend"], "lazylibrarian")
        self.assertIsNone(cascade["final_backend"])

    def test_both_backends_miss_once_and_release_reservations(self):
        services = ScriptedBackends([ll_miss(), shelfarr_miss])
        response = RequestProcessor(self.store, services=services).process(delivery())

        self.assertEqual(response["status"], "failed")
        cascade = self.store.get_ebook_cascade(response["request_id"])
        self.assertEqual([a["status"] for a in cascade["attempts"]], ["miss", "miss"])
        self.assertIsNone(cascade["final_backend"])
        with self.store.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM ebook_backend_reservations WHERE request_id = ?",
                (response["request_id"],),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_confirmed_primary_identity_falls_back_without_second_prompt(self):
        choices = (
            identity(fingerprint="c" * 64, work_id="lazylibrarian:" + "c" * 64),
            identity(
                fingerprint="d" * 64,
                work_id="lazylibrarian:" + "d" * 64,
                title="Dune Messiah",
                year=1969,
            ),
        )

        def ambiguous(_request, backend, _authoritative, _selected):
            return result(
                "awaiting_selection",
                "multiple matches",
                service=backend,
                selection_proposal=choices,
                backend_outcome="ambiguous",
            )

        services = ScriptedBackends([ambiguous, ll_miss(choices[0]), shelfarr_success()])
        processor = RequestProcessor(self.store, services=services)
        initial = processor.process(delivery())
        self.assertEqual(initial["status"], "awaiting_selection")
        self.assertTrue(
            self.store.bind_candidate_prompt(
                initial["request_id"], "999999999999999999"
            )
        )
        confirmed = processor.process_candidate_reply(
            selection_delivery := {
                "prompt_message_id": "999999999999999999",
                "message_id": "999999999999999998",
                "discord_user_id": "10",
                "channel_id": "20",
                "ordinal": 1,
            }
        )

        self.assertEqual(confirmed["status"], "queued")
        self.assertEqual(len(services.calls), 3)
        self.assertEqual(services.calls[2]["resolved_identity"]["title"], "Dune")
        confirmation = self.store.get_candidate_confirmation(initial["request_id"])
        self.assertEqual(confirmation["status"], "claimed")
        replay = processor.process_candidate_reply(selection_delivery)
        self.assertEqual(replay["selection_outcome"], "duplicate")
        self.assertEqual(len(services.calls), 3)

    def test_authoritative_identity_never_opens_a_second_ambiguity_prompt(self):
        def provider_ambiguous(_request, backend, authoritative, _selected):
            return result(
                "needs_selection",
                "multiple exact mappings",
                service=backend,
                backend_outcome="ambiguous",
                resolved_identity=authoritative,
            )

        services = ScriptedBackends([ll_miss(), provider_ambiguous])
        response = RequestProcessor(self.store, services=services).process(delivery())

        self.assertEqual(response["status"], "failed")
        self.assertIsNone(self.store.get_candidate_confirmation(response["request_id"]))
        cascade = self.store.get_ebook_cascade(response["request_id"])
        self.assertEqual([a["status"] for a in cascade["attempts"]], ["miss", "miss"])

    def test_every_pre_mutation_prompt_cleanup_closes_cascade_and_reservations(self):
        choices = (
            identity(fingerprint="c" * 64, work_id="lazylibrarian:" + "c" * 64),
            identity(
                fingerprint="d" * 64,
                work_id="lazylibrarian:" + "d" * 64,
                title="Dune Messiah",
                year=1969,
            ),
        )

        def ambiguous(_request, backend, _authoritative, _selected):
            return result(
                "awaiting_selection",
                "multiple matches",
                service=backend,
                selection_proposal=choices,
                backend_outcome="ambiguous",
            )

        for case in ("prompt_failure", "ttl", "startup_unbound", "claim_expiry"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                store = RequestStore(Path(directory) / "huey.db")
                store.initialize()
                processor = RequestProcessor(
                    store,
                    services=ScriptedBackends(
                        [ambiguous], backends=("lazylibrarian",)
                    ),
                )
                response = processor.process(
                    delivery(f"prompt-{case}", f"Dune {case} by Frank Herbert")
                )
                request_id = response["request_id"]
                if case == "prompt_failure":
                    self.assertTrue(
                        store.fail_candidate_prompt(
                            request_id, "Discord prompt could not be bound"
                        )
                    )
                elif case == "ttl":
                    self.assertEqual(
                        [row["id"] for row in store.expire_candidate_confirmations(
                            now=datetime(9999, 1, 1, tzinfo=timezone.utc)
                        )],
                        [request_id],
                    )
                elif case == "startup_unbound":
                    store.initialize()
                else:
                    self.assertTrue(store.bind_candidate_prompt(request_id, "900"))
                    with store.connect() as connection:
                        connection.execute(
                            """
                            UPDATE candidate_confirmations
                            SET expires_at = '2000-01-01T00:00:00+00:00'
                            WHERE request_id = ?
                            """,
                            (request_id,),
                        )
                    expired = processor.process_candidate_reply(
                        {
                            "prompt_message_id": "900",
                            "message_id": "901",
                            "discord_user_id": "10",
                            "channel_id": "20",
                            "ordinal": 1,
                        }
                    )
                    self.assertEqual(expired["selection_outcome"], "expired")

                saved = store.get_request(request_id)
                cascade = store.get_ebook_cascade(request_id)
                self.assertEqual(saved["status"], "needs_selection")
                self.assertEqual(cascade["state"], "failed")
                self.assertEqual(cascade["attempts"][0]["status"], "failed")
                self.assertEqual(cascade["attempts"][0]["backend_identities"], ())

    def test_mutation_lock_is_one_shot_even_for_same_backend_identity(self):
        request, _ = self.store.create_request(
            discord_user_id="10",
            discord_username="reader",
            channel_id="20",
            message_id="100",
            media_type="ebooks",
            raw_request="Dune",
            title="Dune",
            author="Frank Herbert",
            target_key="target:lock",
            ebook_backends=("lazylibrarian", "shelfarr"),
        )
        self.store.begin_ebook_attempt(request["id"], "lazylibrarian")
        self.store.set_ebook_identity(
            request["id"], "lazylibrarian", identity(), backend_identity=LL_BOOK_ID
        )
        self.assertTrue(
            self.store.lock_ebook_mutation(
                request["id"], "lazylibrarian", backend_identity=LL_BOOK_ID
            )
        )
        self.assertFalse(
            self.store.lock_ebook_mutation(
                request["id"], "lazylibrarian", backend_identity=LL_BOOK_ID
            )
        )

    def test_typed_miss_cannot_advance_after_mutation_marker(self):
        request, _ = self.store.create_request(
            discord_user_id="10",
            discord_username="reader",
            channel_id="20",
            message_id="100",
            media_type="ebooks",
            raw_request="Dune",
            title="Dune",
            author="Frank Herbert",
            target_key="target:post-marker-miss",
            ebook_backends=("lazylibrarian", "shelfarr"),
        )
        self.store.begin_ebook_attempt(request["id"], "lazylibrarian")
        self.store.set_ebook_identity(
            request["id"], "lazylibrarian", identity(), backend_identity=LL_BOOK_ID
        )
        self.assertTrue(
            self.store.lock_ebook_mutation(
                request["id"], "lazylibrarian", backend_identity=LL_BOOK_ID
            )
        )
        with self.assertRaises(EbookCascadeStateError):
            self.store.advance_ebook_backend(
                request["id"],
                "lazylibrarian",
                "miss",
                result(
                    "needs_selection",
                    "claimed miss after mutation",
                    service="lazylibrarian",
                    backend_outcome="miss",
                    resolved_identity=identity(),
                ),
                final_message="failed",
            )
        cascade = self.store.get_ebook_cascade(request["id"])
        self.assertEqual(cascade["state"], "mutating")
        self.assertEqual(cascade["mutation_backend"], "lazylibrarian")
        self.assertEqual(cascade["attempts"][0]["status"], "mutating")
        self.assertEqual(cascade["attempts"][1]["status"], "pending")

    def test_resolved_identity_collision_aliases_delivery_and_emits_no_failure(self):
        services = ScriptedBackends([ll_success(), ll_success()])
        processor = RequestProcessor(self.store, services=services)
        first = processor.process(delivery("100", "Dune"))
        second = processor.process(delivery("101", "Dune novel by Frank Herbert"))

        self.assertEqual(first["status"], "queued")
        self.assertTrue(second["duplicate"])
        self.assertIn(f"request #{first['request_id']}", second["message"])
        self.assertEqual(
            self.store.get_by_message_id("101")["id"], first["request_id"]
        )
        pending = self.store.pending_notification_deliveries()
        self.assertFalse(
            any(row["request_id"] == second["request_id"] for row in pending)
        )
        self.assertNotIn("request_failed", {
            row["event_key"] for row in pending if row["request_id"] == second["request_id"]
        })

    def test_restart_resumes_advanced_fallback_once(self):
        request, _ = self.store.create_request(
            discord_user_id="10",
            discord_username="reader",
            channel_id="20",
            message_id="100",
            media_type="ebooks",
            raw_request="Dune",
            title="Dune",
            author="Frank Herbert",
            target_key="target:restart",
            ebook_backends=("lazylibrarian", "shelfarr"),
        )
        self.store.begin_ebook_attempt(request["id"], "lazylibrarian")
        self.store.set_ebook_identity(
            request["id"], "lazylibrarian", identity(), backend_identity=LL_BOOK_ID
        )
        self.store.advance_ebook_backend(
            request["id"],
            "lazylibrarian",
            "miss",
            result(
                "needs_selection",
                "safe miss",
                service="lazylibrarian",
                backend_outcome="miss",
                resolved_identity=identity(),
            ),
            final_message="failed",
        )
        services = ScriptedBackends([shelfarr_success()])
        processor = RequestProcessor(self.store, services=services)

        self.assertEqual(processor.resume_ebook_cascades(), 1)
        self.assertEqual(processor.resume_ebook_cascades(), 0)
        self.assertEqual([call["backend"] for call in services.calls], ["shelfarr"])

    def test_terminal_trigger_keeps_backend_finalizer_provenance(self):
        for message_id, backends, action, backend, finalizer in (
            ("100", ("lazylibrarian",), ll_success(), "lazylibrarian", "bookbot"),
            (
                "101",
                ("shelfarr",),
                shelfarr_success(
                    shelfarr_identity(
                        fingerprint="e" * 64,
                        work_id="openlibrary:OL2W",
                        title="Dune Messiah",
                        year=1969,
                    )
                ),
                "shelfarr",
                "shelfarr",
            ),
        ):
            with self.subTest(backend=backend):
                services = ScriptedBackends([action], backends=backends)
                response = RequestProcessor(self.store, services=services).process(
                    delivery(message_id, f"Dune {message_id} by Frank Herbert")
                )
                with self.store.connect() as connection:
                    connection.execute(
                        "UPDATE requests SET status = 'completed' WHERE id = ?",
                        (response["request_id"],),
                    )
                cascade = self.store.get_ebook_cascade(response["request_id"])
                self.assertEqual(cascade["state"], "completed")
                self.assertEqual(cascade["final_backend"], backend)
                self.assertEqual(cascade["finalizer"], finalizer)
                self.assertEqual(cascade["attempts"][0]["status"], "completed")

    def test_terminal_trigger_distinguishes_prehandoff_and_downstream_failure(self):
        prehandoff, _ = self.store.create_request(
            discord_user_id="10",
            discord_username="reader",
            channel_id="20",
            message_id="pre-failure",
            media_type="ebooks",
            raw_request="Children of Dune",
            title="Children of Dune",
            author="Frank Herbert",
            target_key="target:pre-failure",
            ebook_backends=("shelfarr",),
        )
        pre_identity = shelfarr_identity(
            fingerprint="7" * 64,
            work_id="openlibrary:OL-PREFAIL",
            title="Children of Dune",
            year=1976,
        )
        self.store.begin_ebook_attempt(prehandoff["id"], "shelfarr")
        self.store.set_ebook_identity(
            prehandoff["id"],
            "shelfarr",
            pre_identity,
            backend_identity=pre_identity["work_id"],
        )
        self.assertTrue(
            self.store.lock_ebook_mutation(
                prehandoff["id"],
                "shelfarr",
                backend_identity=pre_identity["work_id"],
            )
        )
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE requests SET status = 'failed', error = 'definitive failure' "
                "WHERE id = ?",
                (prehandoff["id"],),
            )
        cascade = self.store.get_ebook_cascade(prehandoff["id"])
        self.assertEqual(cascade["state"], "failed")
        self.assertEqual(cascade["mutation_backend"], "shelfarr")
        self.assertIsNone(cascade["final_backend"])
        self.assertIsNone(cascade["finalizer"])
        self.assertEqual(cascade["attempts"][0]["status"], "failed")
        self.assertIsNotNone(cascade["attempts"][0]["mutation_resolved_at"])
        self.assertEqual(cascade["attempts"][0]["backend_identities"], ())

        services = ScriptedBackends(
            [
                ll_success(
                    identity(
                        fingerprint="8" * 64,
                        work_id="lazylibrarian:" + "8" * 64,
                        title="God Emperor of Dune",
                        year=1981,
                    )
                )
            ],
            backends=("lazylibrarian",),
        )
        downstream = RequestProcessor(self.store, services=services).process(
            delivery("downstream-failure", "God Emperor of Dune by Frank Herbert")
        )
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE requests SET status = 'failed', error = 'import failure' "
                "WHERE id = ?",
                (downstream["request_id"],),
            )
        cascade = self.store.get_ebook_cascade(downstream["request_id"])
        self.assertEqual(cascade["state"], "failed")
        self.assertEqual(cascade["final_backend"], "lazylibrarian")
        self.assertEqual(cascade["finalizer"], "bookbot")
        self.assertEqual(cascade["attempts"][0]["status"], "failed")
        self.assertEqual(cascade["attempts"][0]["backend_identities"], ())

        recovered_complete, _ = self.store.create_request(
            discord_user_id="10",
            discord_username="reader",
            channel_id="20",
            message_id="uncertain-complete",
            media_type="ebooks",
            raw_request="Chapterhouse Dune",
            title="Chapterhouse Dune",
            author="Frank Herbert",
            target_key="target:uncertain-complete",
            ebook_backends=("shelfarr",),
        )
        completed_identity = shelfarr_identity(
            fingerprint="6" * 64,
            work_id="openlibrary:OL-COMPLETE",
            title="Chapterhouse Dune",
            year=1985,
        )
        self.store.begin_ebook_attempt(recovered_complete["id"], "shelfarr")
        self.store.set_ebook_identity(
            recovered_complete["id"],
            "shelfarr",
            completed_identity,
            backend_identity=completed_identity["work_id"],
        )
        self.store.lock_ebook_mutation(
            recovered_complete["id"],
            "shelfarr",
            backend_identity=completed_identity["work_id"],
        )
        self.store.persist_ebook_result(
            recovered_complete["id"],
            "shelfarr",
            result(
                "queued",
                "uncertain",
                service="shelfarr",
                external_status="submission_uncertain",
            ),
        )
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE requests SET status = 'completed' WHERE id = ?",
                (recovered_complete["id"],),
            )
        cascade = self.store.get_ebook_cascade(recovered_complete["id"])
        self.assertEqual(cascade["state"], "completed")
        self.assertEqual(cascade["final_backend"], "shelfarr")
        self.assertEqual(cascade["finalizer"], "shelfarr")
        self.assertEqual(cascade["attempts"][0]["status"], "completed")

    def test_recovered_handoff_fills_provenance_without_releasing_identity(self):
        request, _ = self.store.create_request(
            discord_user_id="10",
            discord_username="reader",
            channel_id="20",
            message_id="100",
            media_type="ebooks",
            raw_request="Dune",
            title="Dune",
            author="Frank Herbert",
            target_key="target:recover",
            ebook_backends=("lazylibrarian",),
        )
        self.store.begin_ebook_attempt(request["id"], "lazylibrarian")
        self.store.set_ebook_identity(
            request["id"], "lazylibrarian", identity(), backend_identity=LL_BOOK_ID
        )
        self.store.lock_ebook_mutation(
            request["id"], "lazylibrarian", backend_identity=LL_BOOK_ID
        )
        self.store.persist_ebook_result(
            request["id"],
            "lazylibrarian",
            result(
                "queued",
                "uncertain",
                service="lazylibrarian",
                external_status="submission_uncertain",
            ),
        )
        with self.assertRaises(EbookCascadeStateError):
            self.store.record_ebook_recovered_handoff(
                request["id"],
                "lazylibrarian",
                LL_HASH,
                "Dune",
                "queued",
                "wrong identity",
                backend_identity="OL-OTHER",
                notifications=(("recovered_test", "download-queue", "not staged"),),
            )
        before = self.store.get_request(request["id"])
        self.assertIsNone(before["external_id"])
        self.assertEqual(self.store.get_ebook_cascade(request["id"])["state"], "uncertain")
        self.assertFalse(
            any(
                row["event_key"] == "recovered_test"
                for row in self.store.pending_notification_deliveries()
            )
        )
        self.assertTrue(
            self.store.record_ebook_recovered_handoff(
                request["id"],
                "lazylibrarian",
                LL_HASH,
                "Dune",
                "queued",
                "recovered",
                backend_identity=LL_BOOK_ID,
                notifications=(
                    ("recovered_test", "download-queue", "generic recovery"),
                ),
            )
        )
        cascade = self.store.get_ebook_cascade(request["id"])
        saved = self.store.get_request(request["id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["external_id"], LL_HASH)
        self.assertEqual(cascade["state"], "queued")
        self.assertEqual(cascade["final_backend"], "lazylibrarian")
        self.assertEqual(cascade["finalizer"], "bookbot")
        self.assertEqual(cascade["attempts"][0]["status"], "queued")
        self.assertEqual(cascade["attempts"][0]["external_id"], LL_HASH)
        self.assertIsNotNone(cascade["attempts"][0]["mutation_resolved_at"])
        self.assertEqual(
            sum(
                row["event_key"] == "recovered_test"
                for row in self.store.pending_notification_deliveries()
            ),
            1,
        )

    def test_restart_after_ll_mutation_reconciles_exact_handoff_without_fallback(self):
        request, _ = self.store.create_request(
            discord_user_id="10",
            discord_username="reader",
            channel_id="20",
            message_id="ll-restart",
            media_type="ebooks",
            raw_request="Dune",
            title="Dune",
            author="Frank Herbert",
            target_key="target:ll-restart",
            ebook_backends=("lazylibrarian", "shelfarr"),
        )
        self.store.begin_ebook_attempt(request["id"], "lazylibrarian")
        self.store.set_ebook_identity(
            request["id"], "lazylibrarian", identity(), backend_identity=LL_BOOK_ID
        )
        self.store.lock_ebook_mutation(
            request["id"], "lazylibrarian", backend_identity=LL_BOOK_ID
        )
        self.store.persist_ebook_result(
            request["id"],
            "lazylibrarian",
            result(
                "queued",
                "uncertain",
                service="lazylibrarian",
                external_status="submission_uncertain",
            ),
        )
        fallback = ScriptedBackends([shelfarr_success()])
        self.assertEqual(
            RequestProcessor(self.store, services=fallback).resume_ebook_cascades(),
            0,
        )
        self.assertEqual(fallback.calls, [])

        ll_client = types.SimpleNamespace(
            recover_submission=Mock(
                return_value={
                    "state": "queued",
                    "book_id": LL_BOOK_ID,
                    "external_id": LL_HASH,
                    "external_title": "Dune",
                    "external_status": "queued",
                }
            )
        )

        def unavailable_qbit():
            raise ServiceError("status unavailable")

        services = types.SimpleNamespace(
            lazylibrarian=lambda: ll_client,
            qbittorrent=unavailable_qbit,
        )
        self.assertEqual(
            reconcile_lazylibrarian_requests(self.store, services), 1
        )
        self.assertEqual(
            reconcile_lazylibrarian_requests(self.store, services), 0
        )
        ll_client.recover_submission.assert_called_once_with(
            LL_BOOK_ID, request_id=request["id"]
        )
        cascade = self.store.get_ebook_cascade(request["id"])
        self.assertEqual(cascade["state"], "queued")
        self.assertEqual(cascade["final_backend"], "lazylibrarian")
        self.assertEqual(cascade["attempts"][1]["status"], "pending")

    def test_shelfarr_recovery_accepts_reserved_alias_and_quarantines_unlisted(self):
        primary = "openlibrary:OL893415W"
        alias = "hardcover:12345"

        def uncertain_request(message_id, target_key):
            request, _ = self.store.create_request(
                discord_user_id="10",
                discord_username="reader",
                channel_id="20",
                message_id=message_id,
                media_type="ebooks",
                raw_request="Dune",
                title="Dune",
                author="Frank Herbert",
                target_key=target_key,
                ebook_backends=("shelfarr",),
            )
            selected = shelfarr_identity(
                work_id=primary,
                source_work_ids=(primary, alias),
            )
            self.store.begin_ebook_attempt(request["id"], "shelfarr")
            self.store.set_ebook_identity(
                request["id"],
                "shelfarr",
                selected,
                backend_identity=primary,
                backend_aliases=selected["source_work_ids"],
            )
            self.store.lock_ebook_mutation(
                request["id"], "shelfarr", backend_identity=primary
            )
            self.store.persist_ebook_result(
                request["id"],
                "shelfarr",
                result(
                    "queued",
                    "uncertain",
                    service="shelfarr",
                    external_status="submission_uncertain",
                ),
            )
            return request

        request = uncertain_request("100", "target:alias")
        remote = {
            "id": 73,
            "status": "pending",
            "book": {
                "title": "Dune",
                "work_id": alias,
                "book_type": "ebook",
                "content_kind": "book",
            },
        }
        client = types.SimpleNamespace(
            recover_request=Mock(return_value=remote),
            get_request=Mock(return_value=remote),
        )
        services = types.SimpleNamespace(shelfarr=lambda: client)
        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 1)
        cascade = self.store.get_ebook_cascade(request["id"])
        self.assertEqual(cascade["state"], "queued")
        self.assertEqual(cascade["final_backend"], "shelfarr")
        self.assertEqual(cascade["attempts"][0]["external_id"], "73")
        self.assertIn(alias, cascade["attempts"][0]["backend_identities"])

        # A fresh quarantined request with no reserved alias cannot be attached.
        other_primary = "openlibrary:OL2W"
        other, _ = self.store.create_request(
            discord_user_id="10",
            discord_username="reader",
            channel_id="20",
            message_id="101",
            media_type="ebooks",
            raw_request="Dune Messiah",
            title="Dune Messiah",
            author="Frank Herbert",
            target_key="target:unlisted",
            ebook_backends=("shelfarr",),
        )
        other_identity = shelfarr_identity(
            fingerprint="f" * 64,
            work_id=other_primary,
            title="Dune Messiah",
            year=1969,
        )
        self.store.begin_ebook_attempt(other["id"], "shelfarr")
        self.store.set_ebook_identity(
            other["id"],
            "shelfarr",
            other_identity,
            backend_identity=other_primary,
            backend_aliases=(other_primary,),
        )
        self.store.lock_ebook_mutation(
            other["id"], "shelfarr", backend_identity=other_primary
        )
        self.store.persist_ebook_result(
            other["id"],
            "shelfarr",
            result(
                "queued",
                "uncertain",
                service="shelfarr",
                external_status="submission_uncertain",
            ),
        )
        fallback = ScriptedBackends([shelfarr_success()], backends=("shelfarr",))
        self.assertEqual(
            RequestProcessor(self.store, services=fallback).resume_ebook_cascades(),
            0,
        )
        self.assertEqual(fallback.calls, [])
        wrong_remote = {
            **remote,
            "id": 74,
            "book": {**remote["book"], "work_id": "hardcover:unlisted"},
        }
        client.recover_request.return_value = wrong_remote
        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 0)
        self.assertEqual(self.store.get_ebook_cascade(other["id"])["state"], "uncertain")
        self.assertIsNone(self.store.get_request(other["id"])["external_id"])

    def test_shelfarr_alias_collision_rolls_back_every_new_reservation(self):
        common_alias = "hardcover:shared"

        def searching_request(message_id, title, target_key):
            request, _ = self.store.create_request(
                discord_user_id="10",
                discord_username="reader",
                channel_id="20",
                message_id=message_id,
                media_type="ebooks",
                raw_request=title,
                title=title,
                author="Frank Herbert",
                target_key=target_key,
                ebook_backends=("shelfarr",),
            )
            self.store.begin_ebook_attempt(request["id"], "shelfarr")
            return request

        first = searching_request("100", "Dune", "target:first")
        first_identity = shelfarr_identity(
            work_id="openlibrary:OL1W",
            source_work_ids=("openlibrary:OL1W", common_alias),
        )
        self.store.set_ebook_identity(
            first["id"],
            "shelfarr",
            first_identity,
            backend_identity="openlibrary:OL1W",
            backend_aliases=first_identity["source_work_ids"],
        )

        second = searching_request("101", "Dune Messiah", "target:second")
        second_identity = shelfarr_identity(
            fingerprint="9" * 64,
            work_id="openlibrary:OL2W",
            title="Dune Messiah",
            year=1969,
            source_work_ids=(
                "openlibrary:OL2W",
                "hardcover:unique-before-collision",
                common_alias,
            ),
        )
        with self.assertRaises(EbookIdentityCollision):
            self.store.set_ebook_identity(
                second["id"],
                "shelfarr",
                second_identity,
                backend_identity="openlibrary:OL2W",
                backend_aliases=second_identity["source_work_ids"],
            )
        with self.store.connect() as connection:
            second_reservations = connection.execute(
                """
                SELECT backend_identity FROM ebook_backend_reservations
                WHERE request_id = ?
                """,
                (second["id"],),
            ).fetchall()
        self.assertEqual(second_reservations, [])
        self.assertIsNone(self.store.get_ebook_cascade(second["id"])["identity"])

    def test_recovered_failed_shelfarr_has_mutation_audit_but_no_success_provenance(self):
        request, _ = self.store.create_request(
            discord_user_id="10",
            discord_username="reader",
            channel_id="20",
            message_id="recovered-failed",
            media_type="ebooks",
            raw_request="Dune",
            title="Dune",
            author="Frank Herbert",
            target_key="target:recovered-failed",
            ebook_backends=("shelfarr",),
        )
        selected = shelfarr_identity()
        self.store.begin_ebook_attempt(request["id"], "shelfarr")
        self.store.set_ebook_identity(
            request["id"],
            "shelfarr",
            selected,
            backend_identity=selected["work_id"],
            backend_aliases=selected["source_work_ids"],
        )
        self.store.lock_ebook_mutation(
            request["id"], "shelfarr", backend_identity=selected["work_id"]
        )
        self.store.persist_ebook_result(
            request["id"],
            "shelfarr",
            result(
                "queued",
                "uncertain",
                service="shelfarr",
                external_status="submission_uncertain",
            ),
        )
        recovered = {
            "id": 73,
            "status": "failed",
            "attention_needed": False,
            "issue_description": "no release",
            "book": {
                "title": "Dune",
                "work_id": selected["work_id"],
                "book_type": "ebook",
                "content_kind": "book",
            },
        }
        services = types.SimpleNamespace(
            shelfarr=lambda: types.SimpleNamespace(
                recover_request=Mock(return_value=recovered)
            )
        )

        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 1)
        saved = self.store.get_request(request["id"])
        cascade = self.store.get_ebook_cascade(request["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["external_id"], "73")
        self.assertEqual(cascade["state"], "failed")
        self.assertEqual(cascade["mutation_backend"], "shelfarr")
        self.assertIsNone(cascade["final_backend"])
        self.assertIsNone(cascade["finalizer"])
        self.assertEqual(cascade["attempts"][0]["status"], "failed")
        self.assertIsNotNone(cascade["attempts"][0]["mutation_resolved_at"])
        self.assertEqual(cascade["attempts"][0]["backend_identities"], ())
        for row in self.store.pending_notification_deliveries():
            self.assertNotIn("shelfarr", row["message"].casefold())

    def test_ebook_messages_are_backend_neutral(self):
        services = ScriptedBackends([ll_miss(), shelfarr_success()])
        response = RequestProcessor(self.store, services=services).process(delivery())
        messages = [response["message"]] + [
            row["message"] for row in self.store.pending_notification_deliveries()
        ]
        forbidden = ("lazylibrarian", "shelfarr", "prowlarr", "qbittorrent", "bookbot")
        for message in messages:
            for name in forbidden:
                self.assertNotIn(name, message.casefold())

    def test_existing_completed_work_is_handler_completion_not_new_library_import(self):
        selected = shelfarr_identity()

        def existing(request, backend, _authoritative, _selected):
            request["_on_resolved"](
                selected,
                selected["work_id"],
            )
            return result(
                "completed",
                "already completed",
                service=backend,
                external_id="73",
                external_title="Dune",
                external_status="completed",
                resolved_identity=selected,
            )

        response = RequestProcessor(
            self.store,
            services=ScriptedBackends([existing], backends=("shelfarr",)),
        ).process(delivery())
        self.assertEqual(response["status"], "completed")
        event_types = {
            row["event_type"]
            for row in self.store.events_for(response["request_id"])
        }
        self.assertIn("handler_completed", event_types)
        event_keys = {
            row["event_key"]
            for row in self.store.pending_notification_deliveries()
        }
        self.assertNotIn("library_imported", event_keys)


class EbookPolicyAndPreflightTests(unittest.TestCase):
    def test_backend_policy_parser_is_strict_and_legacy_direct_is_isolated(self):
        registry = ServiceRegistry(
            {
                "EBOOK_ACQUISITION_BACKENDS": " lazylibrarian , shelfarr ",
                "EBOOK_ACQUISITION_OWNER": "lazylibrarian",
            }
        )
        self.assertEqual(
            registry.ebook_acquisition_backends,
            ("lazylibrarian", "shelfarr"),
        )
        for raw in (
            "LazyLibrarian,shelfarr",
            "lazylibrarian,,shelfarr",
            "lazylibrarian,lazylibrarian",
            "direct",
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                ServiceRegistry({"EBOOK_ACQUISITION_BACKENDS": raw})
        with self.assertRaises(ValueError):
            ServiceRegistry(
                {
                    "EBOOK_ACQUISITION_BACKENDS": "lazylibrarian,shelfarr",
                    "EBOOK_ACQUISITION_OWNER": "shelfarr",
                }
            )
        direct = ServiceRegistry({"EBOOK_ACQUISITION_OWNER": "direct"})
        self.assertEqual(direct.ebook_acquisition_backends, ())
        self.assertEqual(direct.ebook_acquisition_owner, "direct")
        self.assertEqual(
            ServiceRegistry({}).ebook_acquisition_backends, ("shelfarr",)
        )

    def registry_with_results(self, results):
        registry = ServiceRegistry({"EBOOK_ACQUISITION_OWNER": "direct"})
        search = Mock(return_value=list(results))
        registry._clients["prowlarr"] = types.SimpleNamespace(search=search)
        return registry, search

    @staticmethod
    def release(title="Dune Frank Herbert EPUB", **overrides):
        value = {
            "title": title,
            "protocol": "torrent",
            "magnetUrl": f"magnet:?xt=urn:btih:{LL_HASH}",
            "seeders": 50,
        }
        value.update(overrides)
        return value

    def test_deployed_protocol_shape_and_formatless_release_are_plausible(self):
        for title in ("Dune Frank Herbert EPUB", "DUNE - HERBERT, FRANK"):
            registry, search = self.registry_with_results([self.release(title)])
            self.assertTrue(registry.ebook_release_available("Dune", "Frank Herbert"))
            self.assertEqual(search.call_args.args[1], (7020,))

    def test_author_is_a_hard_full_token_gate(self):
        for title, expected in (
            ("Dune Frank Herbert EPUB", True),
            ("Dune HERBERT, Frank EPUB", True),
            ("Dune EPUB", False),
            ("Dune Brian Herbert EPUB", False),
        ):
            with self.subTest(title=title):
                registry, _ = self.registry_with_results([self.release(title)])
                self.assertEqual(
                    registry.ebook_release_available("Dune", "Frank Herbert"),
                    expected,
                )

    def test_wrong_lane_and_explicit_unsupported_formats_are_clean_zero(self):
        for title in (
            "Dune Frank Herbert M4A",
            "Dune Frank Herbert Audio Book",
            "Dune Frank Herbert Audio-Book",
            "Dune Frank Herbert Audiobooks",
            "Dune Frank Herbert TXT",
            "Dune Frank Herbert AZW",
            "Dune Frank Herbert AZW4",
            "Dune Frank Herbert KFX",
            "Dune Frank Herbert PRC",
            "Dune Frank Herbert TPZ",
            "Dune Frank Herbert ACSM",
            "Dune Frank Herbert CBR",
            "Dune Frank Herbert Comics",
            "Dune Frank Herbert Magazines",
            "Dune Frank Herbert Graphic Novel",
            "Dune Frank Herbert Graphic-Novels",
            "Dune Frank Herbert Mangas",
            "Dune Frank Herbert Manhwa",
            "Dune Frank Herbert Manhuas",
            "Dune Frank Herbert Webtoons",
        ):
            with self.subTest(title=title):
                registry, _ = self.registry_with_results(
                    [self.release(title, seeders=10000)]
                )
                self.assertFalse(
                    registry.ebook_release_available("Dune", "Frank Herbert")
                )

    def test_malformed_items_are_unavailable_unless_any_plausible_item_exists(self):
        malformed = {
            "title": "Dune Frank Herbert EPUB",
            "magnetUrl": "magnet:?xt=urn:btih:not-a-hash",
        }
        good = self.release()
        for results in ([malformed], [malformed, good], [good, malformed]):
            with self.subTest(results=len(results)):
                registry, _ = self.registry_with_results(results)
                if len(results) == 1:
                    with self.assertRaises(ServiceError):
                        registry.ebook_release_available("Dune", "Frank Herbert")
                else:
                    self.assertTrue(
                        registry.ebook_release_available("Dune", "Frank Herbert")
                    )

    def test_source_urls_and_protocol_conflicts_fail_closed(self):
        v2_hash = "2" * 64
        invalid = (
            self.release(magnetUrl="javascript:alert(1)"),
            self.release(magnetUrl="magnet://[broken"),
            self.release(magnetUrl=f"magnet:?xt=urn:btih:{v2_hash}"),
            self.release(magnetUrl=f"magnet:?xt=urn:btmh:1220{v2_hash}"),
            self.release(
                magnetUrl=(
                    f"magnet:?xt=urn:btih:{LL_HASH}&xt=urn:btmh:1220{v2_hash}"
                ),
                downloadUrl="https://example.invalid/api/v1/download/hybrid",
            ),
            self.release(magnetUrl="", downloadUrl="javascript:alert(1)"),
            self.release(magnetUrl="", downloadUrl="https://user:pass@example/a"),
            self.release(protocol="torrent", downloadProtocol="usenet"),
        )
        for candidate in invalid:
            registry, _ = self.registry_with_results([candidate])
            with self.assertRaises(ServiceError):
                registry.ebook_release_available("Dune", "Frank Herbert")
        for reference in (
            "https://example.invalid/api/v1/download/1",
            "/api/v1/download/1",
            "api/v1/download/1",
        ):
            registry, _ = self.registry_with_results(
                [self.release(magnetUrl="", downloadUrl=reference)]
            )
            self.assertTrue(registry.ebook_release_available("Dune", "Frank Herbert"))
        registry, _ = self.registry_with_results(
            [
                self.release(
                    magnetUrl="magnet:?xt=urn:btih:not-a-v1-hash",
                    downloadUrl="/api/v1/download/valid-fallback",
                )
            ]
        )
        self.assertTrue(registry.ebook_release_available("Dune", "Frank Herbert"))

    def test_wrong_protocol_and_low_confidence_are_clean_zero(self):
        for candidate in (
            self.release(protocol="usenet"),
            self.release("Unrelated Frank Herbert EPUB"),
        ):
            with self.subTest(candidate=candidate):
                registry, _ = self.registry_with_results([candidate])
                self.assertFalse(
                    registry.ebook_release_available("Dune", "Frank Herbert")
                )

    def test_preflight_queries_are_bounded_authoritative_and_deterministic(self):
        registry = ServiceRegistry({"EBOOK_ACQUISITION_OWNER": "direct"})
        search = Mock(side_effect=[[], [], [], []])
        registry._clients["prowlarr"] = types.SimpleNamespace(search=search)

        self.assertFalse(
            registry.ebook_release_available("Dune (Novel)", "Frank Herbert")
        )
        self.assertEqual(
            [call.args for call in search.call_args_list],
            [
                ("Dune (Novel) Frank Herbert", (7020,)),
                ("Dune (Novel)", (7020,)),
                ("Dune Frank Herbert", (7020,)),
                ("Dune", (7020,)),
            ],
        )

    def test_transport_error_is_one_bounded_unavailable_probe(self):
        registry = ServiceRegistry({"EBOOK_ACQUISITION_OWNER": "direct"})
        search = Mock(side_effect=ServiceError("timeout"))
        registry._clients["prowlarr"] = types.SimpleNamespace(search=search)
        with self.assertRaises(ServiceError):
            registry.ebook_release_available("Dune (Novel)", "Frank Herbert")
        self.assertEqual(search.call_count, 1)


class EbookSchemaMigrationTests(unittest.TestCase):
    def test_pre_cascade_database_migrates_idempotently_and_accepts_atomic_intake(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                """
                CREATE TABLE requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    discord_user_id TEXT NOT NULL,
                    discord_username TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    raw_request TEXT NOT NULL,
                    title TEXT,
                    author TEXT,
                    status TEXT NOT NULL DEFAULT 'new',
                    service TEXT,
                    external_id TEXT,
                    external_title TEXT,
                    error TEXT,
                    notified_at TEXT
                );
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    FOREIGN KEY(request_id) REFERENCES requests(id)
                );
                INSERT INTO requests (
                    discord_user_id, discord_username, channel_id, message_id,
                    media_type, raw_request, title, author, status
                ) VALUES (
                    '10', 'reader', '20', 'legacy', 'ebooks',
                    'Legacy Dune', 'Legacy Dune', 'Frank Herbert', 'completed'
                );
                """
            )
            connection.commit()
            connection.close()

            store = RequestStore(database_path)
            store.initialize()
            store.initialize()
            self.assertEqual(store.get_request(1)["message_id"], "legacy")
            with store.connect() as migrated:
                objects = {
                    (row["type"], row["name"])
                    for row in migrated.execute(
                        """
                        SELECT type, name FROM sqlite_master
                        WHERE name IN (
                            'ebook_cascades', 'ebook_backend_attempts',
                            'ebook_backend_reservations',
                            'ebook_request_terminal_sync',
                            'ebook_cascades_active_identity_uq'
                        )
                        """
                    ).fetchall()
                }
            self.assertEqual(
                objects,
                {
                    ("table", "ebook_cascades"),
                    ("table", "ebook_backend_attempts"),
                    ("table", "ebook_backend_reservations"),
                    ("trigger", "ebook_request_terminal_sync"),
                    ("index", "ebook_cascades_active_identity_uq"),
                },
            )
            request, created = store.create_request(
                discord_user_id="10",
                discord_username="reader",
                channel_id="20",
                message_id="new-cascade",
                media_type="ebooks",
                raw_request="Dune Messiah",
                title="Dune Messiah",
                author="Frank Herbert",
                target_key="target:migrated-cascade",
                ebook_backends=("lazylibrarian", "shelfarr"),
            )
            self.assertTrue(created)
            self.assertEqual(request["status"], "processing")
            cascade = store.get_ebook_cascade(request["id"])
            self.assertEqual(cascade["policy"], ("lazylibrarian", "shelfarr"))
            self.assertEqual(
                [attempt["status"] for attempt in cascade["attempts"]],
                ["pending", "pending"],
            )

    def test_schema_rejects_incomplete_identity_and_invalid_finalizer_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RequestStore(Path(directory) / "huey.db")
            store.initialize()
            request, _ = store.create_request(
                discord_user_id="10",
                discord_username="reader",
                channel_id="20",
                message_id="constraints",
                media_type="ebooks",
                raw_request="Dune",
                title="Dune",
                author="Frank Herbert",
                target_key="target:constraints",
                ebook_backends=("shelfarr",),
            )
            with store.connect() as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE ebook_cascades SET identity_key = ? WHERE request_id = ?",
                        ("a" * 64, request["id"]),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        UPDATE ebook_cascades
                        SET final_backend = 'lazylibrarian', finalizer = 'shelfarr'
                        WHERE request_id = ?
                        """,
                        (request["id"],),
                    )


class ResumeDrainTests(unittest.TestCase):
    def test_resume_drains_more_than_one_batch_and_rejects_corrupt_claim(self):
        class Store:
            def __init__(self):
                self.rows = [{"id": value} for value in range(205)]

            def resumable_ebook_requests(self, limit=100):
                return self.rows[:limit]

            def get_candidate_confirmation(self, request_id):
                return None

        store = Store()
        processor = RequestProcessor(store, services=object())

        def consume(request, **_kwargs):
            store.rows = [row for row in store.rows if row["id"] != request["id"]]

        processor._run_ebook_cascade = consume
        self.assertEqual(processor.resume_ebook_cascades(limit=100), 205)

        store.rows = [{"id": 1}]
        store.get_candidate_confirmation = lambda _request_id: {
            "status": "claimed",
            "selected_ordinal": 2,
            "options": [],
        }
        with self.assertRaises(EbookCascadeStateError):
            processor.resume_ebook_cascades()


class StartupRecoveryGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_waiters_share_one_successful_recovery(self):
        calls = []
        started = asyncio.Event()
        release = asyncio.Event()

        def recovery():
            calls.append(1)
            return 7

        async def fake_to_thread(callback, *args):
            value = callback(*args)
            started.set()
            await release.wait()
            return value

        with patch("huey.asyncio.to_thread", new=fake_to_thread):
            gate = _OneShotAsyncRecovery(recovery)
            waiters = [asyncio.create_task(gate.ensure()) for _ in range(4)]
            await started.wait()
            self.assertEqual(calls, [1])
            self.assertFalse(gate.ready.is_set())
            release.set()
            self.assertEqual(await asyncio.gather(*waiters), [7, 7, 7, 7])
            self.assertTrue(gate.complete)
            self.assertEqual(await gate.ensure(), 7)
            self.assertEqual(calls, [1])

    async def test_failed_recovery_exception_is_cached_for_every_waiter(self):
        calls = []

        def recovery():
            calls.append(1)
            raise RuntimeError("corrupt startup state")

        async def fake_to_thread(callback, *args):
            return callback(*args)

        with patch("huey.asyncio.to_thread", new=fake_to_thread):
            gate = _OneShotAsyncRecovery(recovery)
            outcomes = await asyncio.gather(
                gate.ensure(), gate.ensure(), return_exceptions=True
            )
            self.assertEqual(calls, [1])
            self.assertTrue(all(isinstance(value, RuntimeError) for value in outcomes))
            self.assertFalse(gate.complete)
            with self.assertRaisesRegex(RuntimeError, "corrupt startup"):
                await gate.ensure()
            self.assertEqual(calls, [1])

    async def test_build_client_gates_ready_new_intake_and_candidate_replies_once(self):
        started = asyncio.Event()
        release = asyncio.Event()
        recovery_calls = []

        def blocked_recovery(_store, _services):
            recovery_calls.append(1)
            return 1

        async def fake_to_thread(callback, *args):
            if callback is blocked_recovery:
                started.set()
                await release.wait()
            return callback(*args)

        class Intents:
            message_content = False

            @classmethod
            def default(cls):
                return cls()

        class Client:
            def __init__(self, *, intents):
                self.intents = intents
                self.user = types.SimpleNamespace(id=777)
                self.closed = True

            def event(self, callback):
                setattr(self, callback.__name__, callback)
                return callback

            def is_closed(self):
                return self.closed

            async def close(self):
                self.closed = True

        class Message:
            def __init__(self, message_id, *, reference_id=None):
                self.id = message_id
                self.content = "1" if reference_id is not None else "Dune"
                self.channel = types.SimpleNamespace(id=2)
                self.author = types.SimpleNamespace(id=10, bot=False)
                self.webhook_id = None
                self.reference = (
                    types.SimpleNamespace(message_id=reference_id)
                    if reference_id is not None
                    else None
                )
                self.replies = []

            async def reply(self, message):
                self.replies.append(message)
                return types.SimpleNamespace(id=900)

        request_row = {
            "id": 1,
            "media_type": "ebooks",
            "status": "queued",
            "service": "shelfarr",
            "external_id": "73",
            "external_status": "pending",
            "title": "Dune",
            "author": "Frank Herbert",
            "error": None,
        }
        store = types.SimpleNamespace(get_request=Mock(return_value=request_row))
        processor = types.SimpleNamespace(
            store=store,
            services=object(),
            selection_ttl_seconds=900,
            process=Mock(
                return_value={
                    "request_id": 1,
                    "status": "queued",
                    "message": "queued safely",
                    "duplicate": False,
                    "service": "shelfarr",
                    "external_id": "73",
                    "external_status": "pending",
                }
            ),
            process_candidate_reply=Mock(
                return_value={
                    "request_id": 1,
                    "selection_outcome": "duplicate",
                }
            ),
        )
        config = types.SimpleNamespace(request_channels={"2": "ebooks"})
        discord_module = types.SimpleNamespace(Intents=Intents, Client=Client)
        new_message = Message(100)
        selection_message = Message(101, reference_id=900)

        async def no_channel_validation(*_args):
            return None

        async def no_notification_delivery(*_args):
            return None

        with (
            patch.dict(sys.modules, {"discord": discord_module}),
            patch("huey.reconcile_ebook_cascades", new=blocked_recovery),
            patch("huey.validate_discord_channels", new=no_channel_validation),
            patch("huey.write_ready_marker"),
            patch("huey.response_notifications", return_value=()),
            patch("huey.reconcile_notifications", new=no_notification_delivery),
            patch("huey.asyncio.to_thread", new=fake_to_thread),
        ):
            client = build_client(config, processor, Path("/tmp/huey-test-ready"))
            ready_waiter = asyncio.create_task(client.on_ready())
            new_waiter = asyncio.create_task(client.on_message(new_message))
            selection_waiter = asyncio.create_task(
                client.on_message(selection_message)
            )
            await started.wait()
            await asyncio.sleep(0)
            self.assertEqual(recovery_calls, [])
            processor.process.assert_not_called()
            processor.process_candidate_reply.assert_not_called()

            release.set()
            await asyncio.gather(ready_waiter, new_waiter, selection_waiter)
            self.assertEqual(recovery_calls, [1])
            processor.process.assert_called_once()
            processor.process_candidate_reply.assert_called_once()

            # Discord reconnects must observe the same cached result.
            await client.on_ready()
            self.assertEqual(recovery_calls, [1])
            await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
