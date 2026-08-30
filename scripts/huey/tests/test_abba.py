import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import requests


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

from clients import (
    AbbaClient,
    CanonicalAcquisition,
    ServiceError,
    SubmissionUncertain,
)
from database import RequestStore
from huey import reconcile_abba_requests
from orchestrator import RequestProcessor
from results import SELECTION_DECLINE_STATUSES, result
from services import ServiceRegistry


CANDIDATE_A = "abba:" + ("a" * 64)
CANDIDATE_B = "abba:" + ("b" * 64)
INFO_HASH = "c" * 40


class FakeResponse:
    def __init__(self, value, status=200):
        self.status_code = status
        self.value = value

    def json(self):
        return self.value


class ScriptedSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"Unexpected HTTP request: {method} {url}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def candidate(
    candidate_id=CANDIDATE_A,
    *,
    title="Dune",
    author="Frank Herbert",
    narrator="Simon Vance",
    year=1965,
    format="M4B",
    edition="Unabridged",
    size_bytes=1024**3,
):
    return {
        "id": candidate_id,
        "title": title,
        "author": author,
        "narrator": narrator,
        "year": year,
        "format": format,
        "edition": edition,
        "size_bytes": size_bytes,
    }


def search_response(*values):
    return FakeResponse({"results": list(values), "cached": False})


def status_missing():
    return FakeResponse({"found": False})


def job(
    request_id=42,
    *,
    candidate_id=CANDIDATE_A,
    status="queued",
    info_hash=INFO_HASH,
    error=None,
):
    value = {
        "correlation_id": f"huey:{request_id}",
        "candidate_id": candidate_id,
        "status": status,
        "info_hash": info_hash,
        "title": "Dune",
        "category": "audiobooks",
        "save_path": "/downloads/audiobooks",
        "tags": [f"huey-{request_id}"],
    }
    if error is not None:
        value["error"] = error
    return value


def grab_response(**kwargs):
    return FakeResponse({"job": job(**kwargs)})


def status_response(**kwargs):
    return FakeResponse({"found": True, "job": job(**kwargs)})


class AbbaClientTests(unittest.TestCase):
    def test_auto_selection_uses_exact_contract_and_lowercases_hash(self):
        events = []
        session = ScriptedSession(
            search_response(candidate()),
            status_missing(),
            grab_response(info_hash=INFO_HASH.upper()),
        )
        client = AbbaClient("http://abba:8080", session=session)

        def before_grab(candidate_id):
            events.append((candidate_id, [call[0] for call in session.calls]))

        response = client.submit(
            "audiobooks", "Dune", "Frank Herbert", 42, before_create=before_grab
        )

        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["external_id"], INFO_HASH)
        self.assertEqual(events, [(CANDIDATE_A, ["POST", "GET"])])
        self.assertEqual([call[0] for call in session.calls], ["POST", "GET", "POST"])
        self.assertEqual(
            session.calls[0][2]["json"],
            {"title": "Dune", "author": "Frank Herbert", "limit": 10},
        )
        self.assertEqual(
            session.calls[-1][2]["json"],
            {"candidate_id": CANDIDATE_A, "correlation_id": "huey:42"},
        )
        self.assertNotIn("category", session.calls[-1][2]["json"])
        self.assertNotIn("tags", session.calls[-1][2]["json"])

    def test_release_title_ranking_handles_missing_structured_author(self):
        release = candidate(
            title="Frank Herbert - Dune [Unabridged M4B]",
            author=None,
        )
        for requested_author in (None, "Frank Herbert"):
            with self.subTest(requested_author=requested_author):
                session = ScriptedSession(
                    search_response(release), status_missing(), grab_response()
                )
                client = AbbaClient("http://abba:8080", session=session)
                response = client.submit(
                    "audiobooks", "Dune", requested_author, 42
                )
                self.assertEqual(response["status"], "queued")
                self.assertEqual(sum(call[1].endswith("/api/grab") for call in session.calls), 1)

        parsed = AbbaClient._search_candidate(release)
        snapshot = AbbaClient._candidate_snapshot(parsed)
        self.assertIsNone(snapshot["author"])

    def test_release_title_ranking_rejects_wrong_author_and_related_title(self):
        values = (
            (candidate(title="Brian Herbert - Dune [M4B]", author=None), "Frank Herbert"),
            (
                candidate(
                    title="Frank Herbert - Children of Dune [M4B]", author=None
                ),
                "Frank Herbert",
            ),
        )
        for release, requested_author in values:
            with self.subTest(release=release["title"]):
                session = ScriptedSession(search_response(release))
                response = AbbaClient(
                    "http://abba:8080", session=session
                ).submit("audiobooks", "Dune", requested_author, 42)
                self.assertEqual(response["status"], "needs_selection")
                self.assertEqual(len(session.calls), 1)

    def test_failed_job_may_omit_hash_but_nonfailed_job_may_not(self):
        failed_session = ScriptedSession(
            search_response(candidate()),
            status_missing(),
            grab_response(status="failed", info_hash=None, error="magnet unavailable"),
        )
        response = AbbaClient(
            "http://abba:8080", session=failed_session
        ).submit("audiobooks", "Dune", "Frank Herbert", 42)
        self.assertEqual(response["status"], "failed")
        self.assertIsNone(response["external_id"])
        self.assertEqual(response["external_status"], "failed")

        with self.assertRaises(ServiceError):
            AbbaClient._job_payload(
                {"job": job(info_hash=None)}, expected_correlation="huey:42"
            )

    def test_search_rejects_oversize_malformed_and_sensitive_results(self):
        too_many = ScriptedSession(
            FakeResponse(
                {
                    "results": [
                        candidate("abba:" + f"{index:064x}") for index in range(3)
                    ],
                    "cached": False,
                }
            )
        )
        with self.assertRaisesRegex(ServiceError, "too many"):
            AbbaClient(
                "http://abba:8080", session=too_many, search_limit=2
            ).search("Dune")

        malformed_values = (
            {"results": [], "cached": False, "debug": "no"},
            {"results": []},
            {"results": [candidate() | {"magnet": "magnet:?xt=secret"}], "cached": False},
            {
                "results": [candidate(narrator="https://private.invalid/token")],
                "cached": False,
            },
            {
                "results": [candidate(), candidate()],
                "cached": False,
            },
            {
                "results": [candidate(size_bytes="1073741824")],
                "cached": False,
            },
            {
                "results": [candidate(title=123)],
                "cached": False,
            },
        )
        for value in malformed_values:
            with self.subTest(value=value):
                with self.assertRaises(ServiceError):
                    AbbaClient(
                        "http://abba:8080",
                        session=ScriptedSession(FakeResponse(value)),
                    ).search("Dune")

    def test_unreachable_abba_is_sanitized_and_never_attempts_grab(self):
        session = ScriptedSession(
            requests.ConnectionError("http://private.invalid/?token=secret")
        )
        with self.assertRaisesRegex(ServiceError, "ABBA is unavailable") as context:
            AbbaClient("http://abba:8080", session=session).submit(
                "audiobooks", "Dune", "Frank Herbert", 42
            )
        self.assertNotIn("private", str(context.exception))
        self.assertNotIn("secret", str(context.exception))
        self.assertEqual(len(session.calls), 1)

    def test_ambiguity_is_bounded_to_three_sanitized_useful_candidates(self):
        candidates = [
            candidate(
                "abba:" + f"{index + 1:064x}",
                author=f"Author {index}",
                narrator=f"Narrator {index}",
                format="M4B",
                edition=f"Edition {index}",
            )
            for index in range(4)
        ]
        response = AbbaClient(
            "http://abba:8080", session=ScriptedSession(search_response(*candidates))
        ).submit("audiobooks", "Dune", None, 42)

        self.assertEqual(response["status"], "awaiting_selection")
        self.assertEqual(len(response["selection_proposal"]), 3)
        for proposal in response["selection_proposal"]:
            self.assertEqual(
                set(proposal),
                {
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
                },
            )
            self.assertIn("Narrator", proposal["label"])
            self.assertIn("M4B", proposal["label"])
            self.assertRegex(proposal["work_id"], r"\Aabba:[0-9a-f]{64}\Z")
        self.assertNotIn("http", repr(response["selection_proposal"]).casefold())
        self.assertNotIn("magnet", repr(response["selection_proposal"]).casefold())

    def test_zero_results_and_low_confidence_never_grab(self):
        for values in ((), (candidate(title="Unrelated Work"),)):
            with self.subTest(values=values):
                session = ScriptedSession(search_response(*values))
                response = AbbaClient(
                    "http://abba:8080", session=session
                ).submit("audiobooks", "Dune", "Frank Herbert", 42)
                self.assertEqual(response["status"], "needs_selection")
                self.assertEqual(len(session.calls), 1)

    def test_selected_candidate_is_freshly_revalidated_and_stale_result_is_not_grabbed(self):
        parsed = AbbaClient._search_candidate(candidate())
        persisted = AbbaClient._candidate_snapshot(parsed)
        session = ScriptedSession(
            search_response(candidate(narrator="A different narrator"))
        )
        callback = Mock()
        response = AbbaClient("http://abba:8080", session=session).submit_selected(
            "audiobooks",
            "Dune",
            "Frank Herbert",
            42,
            selected_candidate=persisted,
            before_create=callback,
        )
        self.assertEqual(response["status"], "needs_selection")
        callback.assert_not_called()
        self.assertEqual([call[0] for call in session.calls], ["POST"])

    def test_lost_grab_response_recovers_exact_job_without_second_grab(self):
        session = ScriptedSession(
            search_response(candidate()),
            status_missing(),
            requests.ConnectionError("lost response"),
            status_response(),
        )
        response = AbbaClient("http://abba:8080", session=session).submit(
            "audiobooks", "Dune", "Frank Herbert", 42
        )
        self.assertEqual(response["status"], "queued")
        self.assertEqual(sum(call[1].endswith("/api/grab") for call in session.calls), 1)

    def test_existing_correlation_for_different_candidate_is_quarantined(self):
        session = ScriptedSession(
            search_response(candidate()), status_response(candidate_id=CANDIDATE_B)
        )
        callback = Mock()
        with self.assertRaises(SubmissionUncertain):
            AbbaClient("http://abba:8080", session=session).submit(
                "audiobooks",
                "Dune",
                "Frank Herbert",
                42,
                before_create=callback,
            )
        callback.assert_called_once_with(CANDIDATE_A)
        self.assertFalse(any(call[1].endswith("/api/grab") for call in session.calls))

    def test_existing_exact_correlation_still_runs_durable_candidate_hook(self):
        session = ScriptedSession(
            search_response(candidate()), status_response()
        )
        callback = Mock()
        response = AbbaClient("http://abba:8080", session=session).submit(
            "audiobooks",
            "Dune",
            "Frank Herbert",
            42,
            before_create=callback,
        )
        self.assertEqual(response["status"], "queued")
        callback.assert_called_once_with(CANDIDATE_A)
        self.assertFalse(any(call[1].endswith("/api/grab") for call in session.calls))

    def test_strict_job_routing_and_status_shape(self):
        malformed = (
            {"job": job(candidate_id=CANDIDATE_B)},
            {"job": job() | {"tags": ["huey-41"]}},
            {"job": job() | {"category": "books"}},
            {"job": job() | {"save_path": "/tmp"}},
            {"job": job() | {"correlation_id": "huey:41"}},
            {"job": job(), "debug": True},
            {"job": job(status="completed")},
        )
        for value in malformed:
            with self.subTest(value=value):
                with self.assertRaises(ServiceError):
                    AbbaClient._job_payload(
                        value,
                        expected_correlation="huey:42",
                        expected_candidate_id=CANDIDATE_A,
                    )

    def test_resume_grab_uses_only_persisted_candidate_and_correlation(self):
        session = ScriptedSession(status_missing(), grab_response())
        response = AbbaClient("http://abba:8080", session=session).resume_grab(
            42, CANDIDATE_A
        )
        self.assertEqual(response["external_id"], INFO_HASH)
        self.assertEqual(
            session.calls[-1][2]["json"],
            {"candidate_id": CANDIDATE_A, "correlation_id": "huey:42"},
        )
        with self.assertRaises(ValueError):
            AbbaClient("http://abba:8080", session=ScriptedSession()).resume_grab(
                42, "https://unsafe.invalid/result"
            )

    def test_duplicate_hash_job_is_a_typed_canonical_acquisition(self):
        duplicate = job(request_id=2, candidate_id=CANDIDATE_B)
        duplicate.update(
            {
                "status": "duplicate",
                "tags": ["huey-1"],
                "canonical_correlation_id": "huey:1",
                "canonical_candidate_id": CANDIDATE_A,
            }
        )
        parsed = AbbaClient._job_payload(
            {"job": duplicate},
            expected_correlation="huey:2",
            expected_candidate_id=CANDIDATE_B,
        )
        with self.assertRaises(CanonicalAcquisition) as raised:
            AbbaClient._submission_result(parsed)
        self.assertEqual(raised.exception.owner_request_id, 1)
        self.assertEqual(raised.exception.info_hash, INFO_HASH)

    def test_canonical_correlation_is_bounded_to_sqlite_request_ids(self):
        maximum = 9_223_372_036_854_775_807
        duplicate = job(request_id=2, candidate_id=CANDIDATE_B)
        duplicate.update(
            {
                "status": "duplicate",
                "tags": [f"huey-{maximum}"],
                "canonical_correlation_id": f"huey:{maximum}",
                "canonical_candidate_id": CANDIDATE_A,
            }
        )
        parsed = AbbaClient._job_payload(
            {"job": duplicate},
            expected_correlation="huey:2",
            expected_candidate_id=CANDIDATE_B,
        )
        with self.assertRaises(CanonicalAcquisition) as raised:
            AbbaClient._submission_result(parsed)
        self.assertEqual(raised.exception.owner_request_id, maximum)

        for invalid in (
            "9223372036854775808",
            "9999999999999999999",
        ):
            with self.subTest(invalid=invalid):
                malformed = dict(duplicate)
                malformed["canonical_correlation_id"] = f"huey:{invalid}"
                malformed["tags"] = [f"huey-{invalid}"]
                with self.assertRaises(ServiceError):
                    AbbaClient._job_payload(
                        {"job": malformed},
                        expected_correlation="huey:2",
                        expected_candidate_id=CANDIDATE_B,
                    )


LEADERS = "Leaders Eat Last: Why Some Teams Pull Together and Others Don't"


def release(index, title):
    """A scraped AudioBookBay listing: a post title and nothing structured.

    ABB posts frequently carry no parsed author, narrator, or year, which is
    what leaves the release title as the only evidence Huey can rank on.
    """

    return candidate(
        "abba:" + f"{index:064x}",
        title=title,
        author=None,
        narrator=None,
        year=None,
        format=None,
        edition=None,
        size_bytes=None,
    )


class AbbaPickerTests(unittest.TestCase):
    """Requests #279-#281: a real title that bailed instead of asking.

    Every listing for this work carries its subtitle, so nothing reached the
    0.82 automatic-match floor. The releases were ranked, in hand, and thrown
    away.
    """

    SUBTITLED = (
        f"Simon Sinek - {LEADERS}",
        LEADERS,
        "Leaders Eat Last Deluxe: Why Some Teams Pull Together and Others Don't",
    )

    def submit(self, *titles, title="Leaders Eat Last", author=None):
        self.session = ScriptedSession(
            search_response(*(release(i + 1, value) for i, value in enumerate(titles)))
        )
        return AbbaClient("http://abba:8080", session=self.session).submit(
            "audiobooks", title, author, 42
        )

    def labels(self, response):
        return [option["label"] for option in response["selection_proposal"]]

    def test_low_confidence_offers_the_ranked_releases_instead_of_bailing(self):
        response = self.submit(*self.SUBTITLED)

        self.assertEqual(response["status"], "awaiting_selection")
        self.assertEqual(len(response["selection_proposal"]), 3)
        # Offering is not accepting: no grab may follow, and the scripted
        # session would raise on any second call.
        self.assertEqual(len(self.session.calls), 1)

    def test_supplied_author_still_offers_releases_that_never_name_it(self):
        # The blended score docks a correct release 22% for not repeating the
        # author in its release name. A floor on the blend would drop exactly
        # the releases the requester asked for by name.
        response = self.submit(*self.SUBTITLED, author="Simon Sinek")

        self.assertEqual(response["status"], "awaiting_selection")
        self.assertIn(LEADERS, self.labels(response))

    def test_other_books_by_the_same_author_are_never_offered(self):
        # "Leaders eat Last simon sinek" has no "by", so the parser leaves the
        # author inside the title. ABBA ranks release-title variants, so the
        # bare "Simon Sinek" half of an unrelated listing scores 0.57 against
        # that query -- high enough to take a picker slot with the wrong book.
        response = self.submit(
            f"Simon Sinek - {LEADERS}",
            "Start With Why - Simon Sinek",
            "The Infinite Game - Simon Sinek",
            title="Leaders eat Last simon sinek",
        )

        for wrong in ("Start With Why", "The Infinite Game"):
            self.assertNotIn(wrong, repr(self.labels(response)))

    def test_the_author_typed_into_the_title_still_gets_a_choice(self):
        # Request #280's shape. The parser has no "by" to split on, so every
        # release that does not repeat "simon sinek" in its own name scores
        # 0.401 against the query -- the right book, just under ARR's 0.45.
        response = self.submit(
            *self.SUBTITLED,
            "Start With Why - Simon Sinek",
            "The Infinite Game - Simon Sinek",
            title="Leaders eat Last simon sinek",
        )

        self.assertEqual(response["status"], "awaiting_selection")
        labels = self.labels(response)
        self.assertEqual(len(labels), 2)
        for label in labels:
            self.assertIn("Leaders Eat Last", label)

    def test_a_duplicate_listing_no_longer_discards_the_whole_proposal(self):
        # Two ABB posts of the same release render one identical label. The
        # old gate abandoned the entire proposal; keeping the higher-ranked one
        # still leaves a real choice.
        response = self.submit(
            "Leaders Eat Last",
            "Leaders Eat Last",
            f"Simon Sinek - {LEADERS}",
        )

        self.assertEqual(response["status"], "awaiting_selection")
        labels = self.labels(response)
        self.assertEqual(len(labels), 2)
        self.assertEqual(len(set(labels)), 2)

    def test_weak_and_unrelated_releases_stay_out_of_the_picker(self):
        response = self.submit(
            "Eat That Frog! - Brian Tracy",
            "The Last Lecture - Randy Pausch",
            "Extreme Ownership - Jocko Willink",
        )

        self.assertEqual(response["status"], "needs_selection")
        self.assertEqual(response["selection_proposal"], ())

    def test_one_plausible_release_is_not_a_choice(self):
        response = self.submit(LEADERS, "Eat That Frog! - Brian Tracy")

        self.assertEqual(response["status"], "needs_selection")

    def test_automatic_matching_is_unchanged(self):
        # An exact release still auto-accepts, and the display floor never
        # promotes a release the confidence gate declined into a grab.
        session = ScriptedSession(
            search_response(candidate()), status_missing(), grab_response()
        )
        queued = AbbaClient("http://abba:8080", session=session).submit(
            "audiobooks", "Dune", "Frank Herbert", 42
        )

        self.assertEqual(queued["status"], "queued")
        self.assertEqual(self.submit(*self.SUBTITLED)["status"], "awaiting_selection")

    def test_two_identical_listings_are_settled_rather_than_offered(self):
        # Both real ABB hits for this book are titled exactly "Leaders Eat Last
        # - Simon Sinek", both MP3, and /api/search exposes no size, bitrate, or
        # uploader to tell them apart. The requester has nothing to choose on.
        first = "abba:" + f"{1:064x}"
        session = ScriptedSession(
            search_response(
                release(1, "Leaders Eat Last - Simon Sinek"),
                release(2, "Leaders Eat Last - Simon Sinek"),
            ),
            status_missing(),
            grab_response(candidate_id=first),
        )
        response = AbbaClient("http://abba:8080", session=session).submit(
            "audiobooks", "Leaders Eat Last", None, 42
        )

        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["duplicate_listings"], 2)
        # The highest-ranked listing, and exactly one grab.
        self.assertEqual(sum(call[1].endswith("/api/grab") for call in session.calls), 1)

    def test_a_real_alternative_is_offered_rather_than_settled(self):
        response = self.submit(
            "Leaders Eat Last - Simon Sinek",
            "Leaders Eat Last - Simon Sinek",
            "Leaders Eat Last",
        )

        self.assertEqual(response["status"], "awaiting_selection")
        self.assertEqual(len(response["selection_proposal"]), 2)
        # Nothing was acquired: a genuine choice still belongs to the requester.
        self.assertEqual(len(self.session.calls), 1)

    def test_identical_listings_below_the_confidence_floor_are_never_settled(self):
        # Same collapse, but the work itself is unproven. Auto-picking here is
        # exactly the wrong-work risk the confidence gate exists to prevent.
        response = self.submit(LEADERS, LEADERS)

        self.assertEqual(response["status"], "needs_selection")
        self.assertEqual(response["external_status"], "selection_low_confidence")
        self.assertEqual(len(self.session.calls), 1)

    def test_each_decline_reason_is_recorded_on_the_result(self):
        # selection_ambiguous is not listed: an ambiguous band whose labels
        # collapse is now settled automatically, and one whose labels differ
        # becomes a picker. It survives only as a defensive mapping, covered by
        # AudiobookDeclineMessageTests.
        cases = (
            ((), "selection_no_results"),
            (("Something Entirely Different",), "selection_low_confidence"),
        )
        for titles, expected in cases:
            with self.subTest(expected=expected):
                response = self.submit(*titles)
                self.assertEqual(response["status"], "needs_selection")
                self.assertEqual(response["external_status"], expected)


class AudiobookDeclineMessageTests(unittest.TestCase):
    """Every decline reason gets its own backend-neutral sentence."""

    def rendered(self, external_status):
        return RequestProcessor._generic_audiobook_result(
            result(
                "needs_selection",
                "ABBA found no audiobook with enough confidence.",
                service="abba",
                external_status=external_status,
            )
        )["message"]

    def test_every_reason_maps_to_its_own_sentence(self):
        sentences = {
            status: self.rendered(status) for status in SELECTION_DECLINE_STATUSES
        }

        self.assertEqual(len(set(sentences.values())), len(SELECTION_DECLINE_STATUSES))
        for sentence in sentences.values():
            self.assertNotIn("abba", sentence.casefold())

    def test_an_unmarked_decline_keeps_the_generic_sentence(self):
        self.assertIn("could not prove one exact", self.rendered(None))


class AbbaPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = RequestStore(Path(self.temporary.name) / "huey.db")
        self.store.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def create_request(self, message_id="100"):
        request, created = self.store.create_request(
            discord_user_id="1",
            discord_username="reader",
            channel_id="2",
            message_id=message_id,
            media_type="audiobooks",
            raw_request="Dune by Frank Herbert",
            title="Dune",
            author="Frank Herbert",
        )
        self.assertTrue(created)
        self.store.transition(
            request["id"], "processing", "Searching ABBA", service="abba"
        )
        return request

    @staticmethod
    def choices():
        return tuple(
            {
                "fingerprint": character * 64,
                "label": f"Dune · by Author {ordinal} · narrated by Reader {ordinal}",
                "work_id": candidate_id,
                "source_work_ids": (candidate_id,),
                "title": "Dune",
                "author": f"Author {ordinal}",
                "year": 1965 + ordinal,
                "content_kind": "book",
                "media_type": "audiobooks",
                "book_type": "audiobook",
            }
            for character, ordinal, candidate_id in (
                ("a", 1, CANDIDATE_A),
                ("b", 2, CANDIDATE_B),
            )
        )

    def test_abba_confirmation_authorizes_user_channel_dedupes_and_claims(self):
        request = self.create_request()
        confirmation = self.store.create_candidate_confirmation(
            request["id"], self.choices()
        )
        self.assertEqual(self.store.get_request(request["id"])["status"], "awaiting_selection")
        self.assertEqual(confirmation["status"], "pending")
        self.assertTrue(self.store.bind_candidate_prompt(request["id"], "9001"))

        wrong_user = self.store.claim_candidate_selection(
            prompt_message_id="9001",
            reply_message_id="9101",
            discord_user_id="99",
            channel_id="2",
            ordinal=1,
        )
        wrong_channel = self.store.claim_candidate_selection(
            prompt_message_id="9001",
            reply_message_id="9102",
            discord_user_id="1",
            channel_id="99",
            ordinal=1,
        )
        claimed = self.store.claim_candidate_selection(
            prompt_message_id="9001",
            reply_message_id="9103",
            discord_user_id="1",
            channel_id="2",
            ordinal=1,
        )
        duplicate = self.store.claim_candidate_selection(
            prompt_message_id="9001",
            reply_message_id="9103",
            discord_user_id="1",
            channel_id="2",
            ordinal=1,
        )
        self.assertEqual(wrong_user["outcome"], "invalid")
        self.assertEqual(wrong_channel["outcome"], "invalid")
        self.assertEqual(claimed["outcome"], "claimed")
        self.assertEqual(claimed["option"]["candidate"]["work_id"], CANDIDATE_A)
        self.assertEqual(duplicate["outcome"], "duplicate")

    def test_abba_confirmation_expiry_and_bound_restart_are_durable(self):
        start = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
        request = self.create_request()
        self.store.create_candidate_confirmation(
            request["id"], self.choices(), now=start, ttl_seconds=30
        )
        self.store.bind_candidate_prompt(request["id"], "9001")
        self.store.initialize()
        self.assertEqual(self.store.get_request(request["id"])["status"], "awaiting_selection")
        expired = self.store.claim_candidate_selection(
            prompt_message_id="9001",
            reply_message_id="9101",
            discord_user_id="1",
            channel_id="2",
            ordinal=1,
            now=start + timedelta(seconds=30),
        )
        self.assertEqual(expired["outcome"], "expired")
        self.assertEqual(expired["request"]["status"], "needs_selection")

    def test_dispatch_boundary_persists_exact_candidate_and_restart_recovers_only_it(self):
        request = self.create_request()
        self.assertTrue(
            self.store.mark_request_dispatch_started(
                request["id"], "abba", candidate_id=CANDIDATE_A
            )
        )
        self.assertTrue(
            self.store.mark_request_dispatch_started(
                request["id"], "abba", candidate_id=CANDIDATE_A
            )
        )
        self.assertFalse(
            self.store.mark_request_dispatch_started(
                request["id"], "abba", candidate_id=CANDIDATE_B
            )
        )
        self.store.initialize()
        saved = self.store.get_request(request["id"])
        self.assertEqual(saved["status"], "processing")
        self.assertEqual(saved["abba_candidate_id"], CANDIDATE_A)
        self.assertEqual(
            [item["id"] for item in self.store.interrupted_abba_requests()],
            [request["id"]],
        )

    def test_restart_fails_abba_processing_without_a_dispatch_candidate(self):
        request = self.create_request()
        self.store.initialize()
        saved = self.store.get_request(request["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertIsNone(saved["abba_candidate_id"])

    def test_candidate_collision_atomically_aliases_claimed_confirmation(self):
        owner = self.create_request("owner")
        self.assertEqual(
            self.store.reserve_abba_dispatch(owner["id"], CANDIDATE_A)["id"],
            owner["id"],
        )

        duplicate = self.create_request("duplicate")
        self.store.create_candidate_confirmation(duplicate["id"], self.choices())
        self.store.bind_candidate_prompt(duplicate["id"], "9001")
        claim = self.store.claim_candidate_selection(
            prompt_message_id="9001",
            reply_message_id="9101",
            discord_user_id="1",
            channel_id="2",
            ordinal=1,
        )
        self.assertEqual(claim["outcome"], "claimed")

        canonical = self.store.reserve_abba_dispatch(
            duplicate["id"], CANDIDATE_A
        )
        saved = self.store.get_request(duplicate["id"])
        confirmation = self.store.get_candidate_confirmation(duplicate["id"])
        self.assertEqual(canonical["id"], owner["id"])
        self.assertEqual(saved["canonical_request_id"], owner["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(confirmation["status"], "failed")
        self.assertIsNotNone(saved["notified_at"])
        self.assertNotIn(
            duplicate["id"],
            [row["id"] for row in self.store.pending_notifications()],
        )
        self.assertEqual(self.store.get_by_message_id("duplicate")["id"], owner["id"])
        self.assertEqual(self.store.get_by_message_id("9101")["id"], owner["id"])

    def test_hash_defense_aliases_different_candidates_to_canonical_owner(self):
        owner = self.create_request("owner")
        duplicate = self.create_request("duplicate")
        self.store.reserve_abba_dispatch(owner["id"], CANDIDATE_A)
        self.store.reserve_abba_dispatch(duplicate["id"], CANDIDATE_B)
        self.store.transition(
            owner["id"], "queued", "queued", service="abba",
            external_id=INFO_HASH, external_status="queued",
        )
        canonical = self.store.transition(
            duplicate["id"], "queued", "queued", service="abba",
            external_id=INFO_HASH, external_status="queued",
        )
        self.assertEqual(canonical["id"], owner["id"])
        saved = self.store.get_request(duplicate["id"])
        self.assertEqual(saved["canonical_request_id"], owner["id"])
        self.assertEqual(saved["external_status"], "canonical_duplicate")

    def test_legacy_hash_migration_elects_lowest_request_id_like_adapter(self):
        first = self.create_request("first")
        second = self.create_request("second")
        self.store.reserve_abba_dispatch(first["id"], CANDIDATE_A)
        self.store.reserve_abba_dispatch(second["id"], CANDIDATE_B)
        with self.store.connect() as connection:
            connection.execute("DROP INDEX requests_active_abba_hash_uq")
            connection.execute(
                """
                UPDATE requests
                SET status='processing', external_id=?, external_status='processing'
                WHERE id=?
                """,
                (INFO_HASH, first["id"]),
            )
            connection.execute(
                """
                UPDATE requests
                SET status='queued', external_id=?, external_status='queued'
                WHERE id=?
                """,
                (INFO_HASH, second["id"]),
            )

        self.store.initialize()
        canonical = self.store.get_request(first["id"])
        alias = self.store.get_request(second["id"])
        self.assertIsNone(canonical["canonical_request_id"])
        self.assertEqual(alias["canonical_request_id"], first["id"])
        self.assertEqual(alias["status"], "failed")
        with self.store.connect() as connection:
            indexes = {
                row[1] for row in connection.execute("PRAGMA index_list(requests)")
            }
        self.assertIn("requests_active_abba_hash_uq", indexes)
        self.assertIn("requests_active_abba_candidate_uq", indexes)

    def test_legacy_hash_migration_prefers_nonfailed_owner_like_adapter(self):
        failed = self.create_request("failed-owner")
        active = self.create_request("active-owner")
        self.store.reserve_abba_dispatch(failed["id"], CANDIDATE_A)
        self.store.reserve_abba_dispatch(active["id"], CANDIDATE_B)
        self.store.transition(
            failed["id"],
            "queued",
            "queued",
            service="abba",
            external_id=INFO_HASH,
            external_status="queued",
        )
        self.store.transition(
            failed["id"],
            "failed",
            "post-mutation failure",
            service="abba",
            external_id=INFO_HASH,
            external_status="failed",
            error="post-mutation failure",
        )
        with self.store.connect() as connection:
            connection.execute("DROP INDEX requests_active_abba_hash_uq")
            connection.execute(
                """
                UPDATE requests
                SET status='queued', external_id=?, external_status='queued'
                WHERE id=?
                """,
                (INFO_HASH, active["id"]),
            )

        self.store.initialize()
        self.assertEqual(self.store.get_request(failed["id"])["status"], "failed")
        self.assertIsNone(
            self.store.get_request(failed["id"])["canonical_request_id"]
        )
        self.assertEqual(self.store.get_request(active["id"])["status"], "queued")
        self.assertIsNone(
            self.store.get_request(active["id"])["canonical_request_id"]
        )

    def test_candidate_conflict_migration_replaces_stale_queued_outbox(self):
        owner = self.create_request("candidate-owner")
        conflict = self.create_request("candidate-conflict")
        self.store.reserve_abba_dispatch(owner["id"], CANDIDATE_A)
        self.store.reserve_abba_dispatch(conflict["id"], CANDIDATE_B)
        self.store.transition(
            owner["id"],
            "queued",
            "queued",
            service="abba",
            external_id=INFO_HASH,
            external_status="queued",
        )
        self.store.transition(
            conflict["id"],
            "queued",
            "queued",
            service="abba",
            external_id="d" * 40,
            external_status="queued",
            notifications=(
                ("download_queued", "download-queue", "stale queued message"),
            ),
        )
        with self.store.connect() as connection:
            connection.execute("DROP INDEX requests_active_abba_candidate_uq")
            connection.execute(
                "UPDATE requests SET abba_candidate_id=? WHERE id=?",
                (CANDIDATE_A, conflict["id"]),
            )

        self.store.initialize()
        saved = self.store.get_request(conflict["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["external_status"], "candidate_identity_conflict")
        self.assertIsNone(saved["canonical_request_id"])
        self.assertIsNone(saved["notified_at"])
        self.assertNotIn(
            conflict["id"],
            [
                row["request_id"]
                for row in self.store.pending_notification_deliveries()
            ],
        )
        self.assertIn(
            conflict["id"],
            [row["id"] for row in self.store.pending_notifications()],
        )

    def test_legacy_mixed_axes_quarantine_candidate_before_hash_alias(self):
        requests = [self.create_request(f"mixed-{index}") for index in range(1, 5)]
        candidate_c = "abba:" + "c" * 64
        hash_y = "d" * 40
        bindings = (
            (CANDIDATE_A, INFO_HASH, requests[0]["id"]),
            (CANDIDATE_B, INFO_HASH, requests[1]["id"]),
            (candidate_c, hash_y, requests[2]["id"]),
            (CANDIDATE_B, hash_y, requests[3]["id"]),
        )
        with self.store.connect() as connection:
            connection.execute("DROP INDEX requests_active_abba_hash_uq")
            connection.execute("DROP INDEX requests_active_abba_candidate_uq")
            for candidate_id, info_hash, request_id in bindings:
                connection.execute(
                    """
                    UPDATE requests
                    SET status='queued', abba_candidate_id=?, external_id=?,
                        external_status='queued'
                    WHERE id=?
                    """,
                    (candidate_id, info_hash, request_id),
                )
            connection.execute(
                """
                INSERT INTO notification_deliveries(
                    request_id, event_key, route, message
                ) VALUES (?, 'download_queued', 'download-queue', ?)
                """,
                (requests[3]["id"], "stale B/Y queue message"),
            )

        self.store.initialize()
        a_x, b_x, c_y, b_y = (
            self.store.get_request(request["id"]) for request in requests
        )
        self.assertIsNone(a_x["canonical_request_id"])
        self.assertEqual(b_x["canonical_request_id"], a_x["id"])
        self.assertEqual(b_x["external_status"], "canonical_duplicate")
        self.assertIsNone(c_y["canonical_request_id"])
        self.assertIsNone(b_y["canonical_request_id"])
        self.assertEqual(b_y["status"], "failed")
        self.assertEqual(b_y["external_status"], "candidate_identity_conflict")
        self.assertIsNone(b_y["notified_at"])
        self.assertNotIn(
            b_y["id"],
            [
                delivery["request_id"]
                for delivery in self.store.pending_notification_deliveries()
            ],
        )
        self.assertIn(
            b_y["id"],
            [request["id"] for request in self.store.pending_notifications()],
        )

    def test_candidate_first_migration_never_creates_hash_alias_chains(self):
        global_owner = self.create_request("chain-global")
        first_candidate = self.create_request("chain-candidate-1")
        second_candidate = self.create_request("chain-candidate-2")
        with self.store.connect() as connection:
            connection.execute("DROP INDEX requests_active_abba_hash_uq")
            connection.execute("DROP INDEX requests_active_abba_candidate_uq")
            for candidate_id, request_id in (
                (CANDIDATE_B, global_owner["id"]),
                (CANDIDATE_A, first_candidate["id"]),
                (CANDIDATE_A, second_candidate["id"]),
            ):
                connection.execute(
                    """
                    UPDATE requests
                    SET status='queued', abba_candidate_id=?, external_id=?,
                        external_status='queued'
                    WHERE id=?
                    """,
                    (candidate_id, INFO_HASH, request_id),
                )

        self.store.initialize()
        first = self.store.get_request(first_candidate["id"])
        second = self.store.get_request(second_candidate["id"])
        self.assertEqual(first["canonical_request_id"], global_owner["id"])
        self.assertEqual(second["canonical_request_id"], global_owner["id"])
        self.assertNotEqual(second["canonical_request_id"], first_candidate["id"])

    def test_legacy_hash_migration_reparents_aliases_and_delivery_routes(self):
        lower = self.create_request("lower-root")
        root = self.create_request("older-root")
        alias = self.create_request("older-alias")
        candidate_c = "abba:" + "c" * 64
        self.store.reserve_abba_dispatch(lower["id"], candidate_c)
        self.store.reserve_abba_dispatch(root["id"], CANDIDATE_A)
        self.store.reserve_abba_dispatch(alias["id"], CANDIDATE_B)
        self.store.transition(
            root["id"], "queued", "queued", service="abba",
            external_id=INFO_HASH, external_status="queued",
        )
        self.store.transition(
            alias["id"], "queued", "queued", service="abba",
            external_id=INFO_HASH, external_status="queued",
        )
        self.assertEqual(
            self.store.get_request(alias["id"])["canonical_request_id"],
            root["id"],
        )
        self.assertEqual(
            self.store.get_by_message_id("older-alias")["id"], root["id"]
        )
        with self.store.connect() as connection:
            connection.execute("DROP INDEX requests_active_abba_hash_uq")
            connection.execute(
                """
                UPDATE requests
                SET status='queued', external_id=?, external_status='queued'
                WHERE id=?
                """,
                (INFO_HASH, lower["id"]),
            )

        self.store.initialize()
        self.assertEqual(
            self.store.get_request(root["id"])["canonical_request_id"],
            lower["id"],
        )
        self.assertEqual(
            self.store.get_request(alias["id"])["canonical_request_id"],
            lower["id"],
        )
        self.assertEqual(
            self.store.get_by_message_id("older-alias")["id"], lower["id"]
        )
        with self.store.connect() as connection:
            delivery_owner = connection.execute(
                "SELECT request_id FROM delivery_aliases WHERE message_id=?",
                ("older-alias",),
            ).fetchone()
        self.assertEqual(delivery_owner["request_id"], lower["id"])

    def test_candidate_migration_prefers_proven_owner_over_lower_reservation(self):
        reservation = self.create_request("candidate-reservation")
        owner = self.create_request("candidate-proven-owner")
        self.store.reserve_abba_dispatch(reservation["id"], CANDIDATE_A)
        self.store.reserve_abba_dispatch(owner["id"], CANDIDATE_B)
        self.store.transition(
            owner["id"], "queued", "queued", service="abba",
            external_id=INFO_HASH, external_status="queued",
        )
        with self.store.connect() as connection:
            connection.execute("DROP INDEX requests_active_abba_candidate_uq")
            connection.execute(
                "UPDATE requests SET abba_candidate_id=? WHERE id=?",
                (CANDIDATE_A, owner["id"]),
            )

        self.store.initialize()
        released = self.store.get_request(reservation["id"])
        retained = self.store.get_request(owner["id"])
        self.assertEqual(released["status"], "failed")
        self.assertEqual(
            released["external_status"], "candidate_identity_conflict"
        )
        self.assertIsNone(released["canonical_request_id"])
        self.assertEqual(retained["status"], "queued")
        self.assertIsNone(retained["canonical_request_id"])
        self.assertNotIn(
            reservation["id"],
            [row["id"] for row in self.store.interrupted_abba_requests()],
        )


class AbbaRoutingAndRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = RequestStore(Path(self.temporary.name) / "huey.db")
        self.store.initialize()
        self.delivery = {
            "discord_user_id": "1",
            "discord_username": "reader",
            "channel_id": "2",
            "message_id": "100",
            "media_type": "audiobooks",
            "content": "Dune by Frank Herbert",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def dispatched_request(self, *, message_id="recovery", candidate_id=CANDIDATE_A):
        request, _ = self.store.create_request(
            discord_user_id="1",
            discord_username="reader",
            channel_id="2",
            message_id=message_id,
            media_type="audiobooks",
            raw_request="Dune by Frank Herbert",
            title="Dune",
            author="Frank Herbert",
        )
        self.store.transition(request["id"], "processing", "ABBA", service="abba")
        self.store.mark_request_dispatch_started(
            request["id"], "abba", candidate_id=candidate_id
        )
        return self.store.get_request(request["id"])

    def test_audiobook_routes_abba_and_atomically_persists_qbit_hash(self):
        session = ScriptedSession(
            search_response(candidate()), status_missing(), grab_response(request_id=1)
        )
        registry = ServiceRegistry(
            {
                "ABBA_ENABLED": "true",
                "SHELFARR_ENABLED": "true",
                "SHELFARR_API_TOKEN": "test-token",
            }
        )
        registry._clients["abba"] = AbbaClient("http://abba:8080", session=session)
        processor = RequestProcessor(self.store, services=registry)

        response = processor.process(self.delivery)
        duplicate = processor.process(self.delivery)
        saved = self.store.get_request(response["request_id"])

        self.assertEqual(response["status"], "queued")
        self.assertEqual(saved["service"], "abba")
        self.assertEqual(saved["external_id"], INFO_HASH)
        self.assertEqual(saved["abba_candidate_id"], CANDIDATE_A)
        self.assertIsNotNone(saved["dispatch_started_at"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(sum(call[1].endswith("/api/grab") for call in session.calls), 1)
        requester_text = " ".join(
            [response["message"]]
            + [
                item["message"]
                for item in self.store.pending_notification_deliveries()
                if item["request_id"] == response["request_id"]
            ]
        ).casefold()
        for backend in ("abba", "qbittorrent", "prowlarr", "bookbot"):
            self.assertNotIn(backend, requester_text)

    def abba_processor(self, session):
        registry = ServiceRegistry({"ABBA_ENABLED": "true"})
        registry._clients["abba"] = AbbaClient("http://abba:8080", session=session)
        return RequestProcessor(self.store, services=registry)

    def lifecycle_text(self, request_id):
        return " ".join(
            item["message"]
            for item in self.store.pending_notification_deliveries()
            if item["request_id"] == request_id
        )

    def test_decline_reasons_are_distinguishable_everywhere_they_are_read(self):
        # One generic sentence for all three outcomes meant neither the
        # requester nor anything reading the request afterwards could tell
        # "nothing was found" from "found, but too alike to choose".
        cases = (
            ("The Missing Book", (), "selection_no_results"),
            (
                "The Weak Book",
                (release(1, "Something Entirely Different"),),
                "selection_low_confidence",
            ),
        )
        messages = {}
        for index, (title, results, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                processor = self.abba_processor(ScriptedSession(search_response(*results)))
                response = processor.process(
                    {**self.delivery, "message_id": f"20{index}", "content": title}
                )
                saved = self.store.get_request(response["request_id"])

                self.assertEqual(response["status"], "needs_selection")
                self.assertEqual(saved["external_status"], expected)
                # The reason is readable off the request itself, which is all
                # Louie gets to see.
                self.assertEqual(saved["error"], response["message"])
                # The lifecycle channel repeats the same specific sentence
                # rather than the flattened one.
                self.assertIn(
                    response["message"], self.lifecycle_text(response["request_id"])
                )
                self.assertNotIn("abba", response["message"].casefold())
                messages[expected] = response["message"]

        self.assertEqual(len(set(messages.values())), 2, messages)

    def test_settled_duplicates_are_stated_to_the_requester(self):
        first = "abba:" + f"{1:064x}"
        session = ScriptedSession(
            search_response(
                release(1, "Leaders Eat Last - Simon Sinek"),
                release(2, "Leaders Eat Last - Simon Sinek"),
            ),
            status_missing(),
            grab_response(request_id=1, candidate_id=first),
        )
        response = self.abba_processor(session).process(
            {**self.delivery, "message_id": "400", "content": "Leaders Eat Last"}
        )

        self.assertEqual(response["status"], "queued")
        self.assertIn("2 identical", response["message"])
        self.assertNotIn("abba", response["message"].casefold())
        # The lifecycle channel says it too, instead of the flattened sentence.
        self.assertIn("2 identical", self.lifecycle_text(response["request_id"]))

    def test_low_confidence_intake_persists_a_candidate_prompt(self):
        session = ScriptedSession(
            search_response(
                *(release(i + 1, value) for i, value in enumerate(AbbaPickerTests.SUBTITLED))
            )
        )
        response = self.abba_processor(session).process(
            {**self.delivery, "message_id": "300", "content": "Leaders Eat Last"}
        )
        saved = self.store.get_request(response["request_id"])

        self.assertEqual(response["status"], "awaiting_selection")
        self.assertEqual(len(response["selection_proposal"]), 3)
        self.assertEqual(saved["status"], "awaiting_selection")
        # An intake conversation is not a lifecycle event.
        self.assertEqual(self.lifecycle_text(response["request_id"]), "")

    def test_failed_post_mutation_owner_coalesces_later_hash_without_retry(self):
        owner = self.dispatched_request(message_id="prior")
        self.store.transition(
            owner["id"],
            "queued",
            "queued",
            service="abba",
            external_id=INFO_HASH,
            external_title="Dune",
            external_status="queued",
        )
        self.store.transition(
            owner["id"],
            "failed",
            "prior terminal failure",
            service="abba",
            external_id=INFO_HASH,
            external_title="Dune",
            external_status="failed",
            error="prior terminal failure",
        )
        duplicate_job = job(request_id=2, candidate_id=CANDIDATE_B)
        duplicate_job.update(
            {
                "status": "duplicate",
                "tags": [f"huey-{owner['id']}"],
                "canonical_correlation_id": f"huey:{owner['id']}",
                "canonical_candidate_id": CANDIDATE_A,
            }
        )
        session = ScriptedSession(
            search_response(candidate(candidate_id=CANDIDATE_B)),
            status_missing(),
            FakeResponse({"job": duplicate_job}),
        )
        registry = ServiceRegistry({"ABBA_ENABLED": "true"})
        registry._clients["abba"] = AbbaClient(
            "http://abba:8080", session=session
        )

        response = RequestProcessor(self.store, services=registry).process(
            self.delivery
        )
        alias = self.store.get_request(2)
        self.assertTrue(response["duplicate"])
        self.assertEqual(response["request_id"], owner["id"])
        self.assertEqual(response["status"], "failed")
        self.assertEqual(alias["canonical_request_id"], owner["id"])
        self.assertEqual(alias["status"], "failed")
        self.assertIsNotNone(alias["notified_at"])
        self.assertEqual(
            sum(call[1].endswith("/api/grab") for call in session.calls), 1
        )
        for backend in ("abba", "qbittorrent", "prowlarr", "bookbot"):
            self.assertNotIn(backend, response["message"].casefold())

    def test_candidate_reply_and_queue_text_are_backend_neutral(self):
        session = ScriptedSession(
            search_response(
                candidate(),
                candidate(
                    candidate_id=CANDIDATE_B,
                    narrator="Scott Brick",
                    year=1966,
                ),
            ),
            search_response(
                candidate(),
                candidate(
                    candidate_id=CANDIDATE_B,
                    narrator="Scott Brick",
                    year=1966,
                ),
            ),
            status_missing(),
            grab_response(request_id=1),
        )
        registry = ServiceRegistry({"ABBA_ENABLED": "true"})
        registry._clients["abba"] = AbbaClient(
            "http://abba:8080", session=session
        )
        processor = RequestProcessor(self.store, services=registry)
        initial = processor.process(self.delivery)
        self.assertEqual(initial["status"], "awaiting_selection")
        self.assertTrue(
            self.store.bind_candidate_prompt(initial["request_id"], "9001")
        )
        confirmed = processor.process_candidate_reply(
            {
                "prompt_message_id": "9001",
                "message_id": "9101",
                "discord_user_id": "1",
                "channel_id": "2",
                "ordinal": 1,
            }
        )
        self.assertEqual(confirmed["status"], "queued")
        text = " ".join(
            [initial["message"], confirmed["message"]]
            + [
                item["message"]
                for item in self.store.pending_notification_deliveries()
                if item["request_id"] == initial["request_id"]
            ]
        ).casefold()
        for backend in ("abba", "qbittorrent", "prowlarr", "bookbot"):
            self.assertNotIn(backend, text)

    def test_disabled_abba_uses_direct_even_when_shelfarr_enabled_and_ebooks_stay_shelfarr(self):
        direct = Mock()
        direct.submit.return_value = result("queued", "direct", service="qbittorrent")
        shelfarr = Mock()
        shelfarr.submit.return_value = result("queued", "shelfarr", service="shelfarr")
        registry = ServiceRegistry(
            {
                "ABBA_ENABLED": "false",
                "SHELFARR_ENABLED": "true",
                "SHELFARR_API_TOKEN": "test-token",
            }
        )
        registry._clients.update({"direct": direct, "shelfarr": shelfarr})
        request = {
            "id": 7,
            "media_type": "audiobooks",
            "title": "Dune",
            "author": "Frank Herbert",
        }

        registry.audiobook(request)
        registry.book({**request, "media_type": "ebooks"})

        direct.submit.assert_called_once_with(
            "audiobooks", "Dune", "Frank Herbert", 7
        )
        shelfarr.submit.assert_called_once()
        self.assertEqual(shelfarr.submit.call_args.args[0], "ebooks")

    def test_registry_uses_canonical_abba_port_by_default(self):
        registry = ServiceRegistry({"ABBA_ENABLED": "true"})
        self.assertEqual(registry.abba().base_url, "http://abba:5078/")
        with self.assertRaisesRegex(ValueError, "between 1 and 20"):
            AbbaClient("http://abba:5078", search_limit=21)
        with self.assertRaisesRegex(ValueError, "literal true or false"):
            ServiceRegistry({"ABBA_ENABLED": "TRUE"})

    def test_uncertain_submission_retains_candidate_with_neutral_notification(self):
        session = ScriptedSession(
            search_response(candidate()),
            status_missing(),
            requests.ConnectionError("lost grab response"),
            requests.ConnectionError("status unavailable"),
        )
        registry = ServiceRegistry({"ABBA_ENABLED": "true"})
        registry._clients["abba"] = AbbaClient("http://abba:8080", session=session)
        response = RequestProcessor(self.store, services=registry).process(self.delivery)
        saved = self.store.get_request(response["request_id"])
        pending = self.store.pending_notification_deliveries()

        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["external_status"], "submission_uncertain")
        self.assertEqual(saved["abba_candidate_id"], CANDIDATE_A)
        self.assertEqual([item["id"] for item in self.store.uncertain_abba_requests()], [saved["id"]])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["event_key"], "submission_uncertain")
        for backend in ("ABBA", "qBittorrent", "Prowlarr", "BookBot", "Shelfarr"):
            self.assertNotIn(backend, pending[0]["message"])

    def test_restart_recovery_attaches_exact_hash_and_rejects_wrong_candidate(self):
        request = self.dispatched_request()
        self.store.initialize()
        abba = Mock()
        abba.recover_request.return_value = job(request_id=request["id"])
        abba.get_request.side_effect = lambda request_id: job(request_id=request_id)
        services = Mock(abba_enabled=False)
        services.abba.return_value = abba

        self.assertEqual(reconcile_abba_requests(self.store, services), 1)
        saved = self.store.get_request(request["id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["external_id"], INFO_HASH)

        other = self.dispatched_request(message_id="wrong", candidate_id=CANDIDATE_B)
        self.store.initialize()
        abba.recover_request.return_value = job(
            request_id=other["id"], candidate_id=CANDIDATE_A
        )
        self.assertEqual(reconcile_abba_requests(self.store, services), 0)
        quarantined = self.store.get_request(other["id"])
        self.assertEqual(quarantined["status"], "processing")
        self.assertIsNone(quarantined["external_id"])

    def test_missing_recovery_status_resubmits_only_exact_persisted_candidate(self):
        request = self.dispatched_request()
        self.store.initialize()
        abba = Mock()
        abba.recover_request.return_value = None
        abba.resume_grab.return_value = result(
            "queued",
            "resumed",
            service="abba",
            external_id=INFO_HASH,
            external_title="Dune",
            external_status="queued",
        )
        abba.get_request.return_value = job(request_id=request["id"])
        services = Mock(abba_enabled=True)
        services.abba.return_value = abba

        self.assertEqual(reconcile_abba_requests(self.store, services), 1)
        abba.resume_grab.assert_called_once_with(request["id"], CANDIDATE_A)
        saved = self.store.get_request(request["id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["external_id"], INFO_HASH)

    def test_recovered_failed_job_without_hash_is_terminal_not_none_string(self):
        request = self.dispatched_request()
        self.store.initialize()
        abba = Mock()
        abba.recover_request.return_value = job(
            request_id=request["id"],
            status="failed",
            info_hash=None,
            error="candidate disappeared",
        )
        services = Mock(abba_enabled=True)
        services.abba.return_value = abba

        self.assertEqual(reconcile_abba_requests(self.store, services), 1)
        self.assertEqual(reconcile_abba_requests(self.store, services), 0)
        saved = self.store.get_request(request["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertIsNone(saved["external_id"])
        self.assertNotEqual(saved["external_id"], "none")

    def test_progress_is_one_shot_and_post_hash_failure_without_hash_is_quarantined(self):
        request = self.dispatched_request()
        self.store.transition(
            request["id"],
            "queued",
            "queued",
            service="abba",
            external_id=INFO_HASH,
            external_title="Dune",
            external_status="queued",
        )
        abba = Mock()
        abba.get_request.return_value = job(
            request_id=request["id"], status="downloading"
        )
        services = Mock(abba_enabled=True)
        services.abba.return_value = abba

        self.assertEqual(reconcile_abba_requests(self.store, services), 1)
        self.assertEqual(reconcile_abba_requests(self.store, services), 0)
        active = [
            item
            for item in self.store.pending_notification_deliveries()
            if item["event_key"] == "download_active"
        ]
        self.assertEqual(len(active), 1)

        abba.get_request.return_value = job(
            request_id=request["id"],
            status="failed",
            info_hash=None,
            error="qBittorrent rejected the job",
        )
        self.assertEqual(reconcile_abba_requests(self.store, services), 0)
        self.assertEqual(self.store.get_request(request["id"])["status"], "queued")


if __name__ == "__main__":
    unittest.main()
