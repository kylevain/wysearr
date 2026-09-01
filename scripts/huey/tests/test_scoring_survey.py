import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

import scoring_survey as survey
from clients import AbbaClient
from database import RequestStore
from matching import agreement_promoted


class FakeResponse:
    status_code = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass

    @property
    def text(self):
        return json.dumps(self._payload)


class SearchOnlySession:
    """Answers /api/search and raises on anything that would mutate."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if not url.endswith("/api/search"):
            raise AssertionError(f"Survey reached a non-search endpoint: {url}")
        return FakeResponse({"results": self.results, "cached": False})


def release(index, title):
    return {
        "id": "abba:" + f"{index:064x}",
        "title": title,
        "author": None,
        "narrator": None,
        "year": None,
        "format": None,
        "edition": None,
        "size_bytes": None,
    }


class Registry:
    shelfarr_enabled = False

    def __init__(self, client):
        self._client = client

    def abba(self):
        return self._client


KAIJU = (
    release(1, "Kaiju: Battlefield Surgeon: A LitRPG Adventure - Matt Dinniman"),
    release(2, "Matt Dinniman Audio Book Collection"),
)


class PromotionTests(unittest.TestCase):
    def test_only_complete_agreement_is_credited(self):
        # Partial overlap is already what the similarity score measures.
        self.assertEqual(survey.promote(0.70, 0.99, 0.10), 0.70)
        self.assertAlmostEqual(survey.promote(0.70, 1.0, 0.10), 0.80)

    def test_promotion_is_capped_at_a_whole_score(self):
        self.assertEqual(survey.promote(0.95, 1.0, 0.12), 1.0)

    def test_the_zero_bonus_is_the_identity(self):
        self.assertEqual(survey.promote(0.4321, 1.0, 0.0), 0.4321)

    def test_the_sweep_is_the_year_rules_step_and_cap(self):
        # Asserted against the sweep itself, not against BONUSES: BONUSES also
        # carries the shipped bonus, so testing it here would fail for a change
        # to the shipped constant while claiming the sweep had moved.
        self.assertEqual(survey.SWEEP[0], 0.0)
        self.assertEqual(survey.SWEEP[1], survey.AGREEMENT_STEP)
        self.assertEqual(survey.SWEEP[-1], survey.AGREEMENT_CAP)
        self.assertEqual(
            len(survey.SWEEP), int(survey.AGREEMENT_CAP / survey.AGREEMENT_STEP) + 1
        )

    def test_the_shipped_bonus_is_always_in_the_sweep(self):
        # Every projection is a delta from the shipped column, so it has to be
        # a column. A shipped value outside the sweep would make the summary
        # measure against a baseline that was never computed.
        self.assertIn(survey.COMPLETE_AGREEMENT_BONUS, survey.BONUSES)
        self.assertIn(survey.BASELINE, [survey.label(b) for b in survey.BONUSES])

    def test_an_off_grid_bonus_gets_its_own_column(self):
        # The value the next person tries when a threshold feels wrong. The
        # baseline must name the column that was actually computed with it,
        # not a rounded neighbour that was never shipped.
        self.assertEqual(survey.label(0.025), "0.025")
        self.assertNotEqual(survey.label(0.025), survey.label(0.03))

    def test_the_baseline_names_a_column_computed_at_the_shipped_bonus(self):
        columns = {survey.label(bonus): bonus for bonus in survey.BONUSES}

        self.assertEqual(columns[survey.BASELINE], survey.COMPLETE_AGREEMENT_BONUS)

    def test_the_survey_knob_agrees_with_the_shipped_rule(self):
        # Two statements of one rule. If they drift, every projection is a lie.
        for score, title, candidate in (
            (0.767, "Kaiju: Battlefield Surgeon", "Kaiju: Battlefield Surgeon: A LitRPG Adventure"),
            (0.688, "Dune", "Dune Messiah - Frank Herbert"),
            (0.500, "Leaders Eat Last", "Leaders - Simon Sinek"),
            (0.995, "Dune", "Dune"),
        ):
            with self.subTest(candidate=candidate):
                self.assertAlmostEqual(
                    survey.promote(
                        score,
                        survey.title_agreement(title, candidate),
                        survey.COMPLETE_AGREEMENT_BONUS,
                    ),
                    agreement_promoted(score, title, candidate),
                )


class RowSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "huey.db"
        RequestStore(self.path).initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def insert(self, **overrides):
        self.message = getattr(self, "message", 0) + 1
        row = {
            "media_type": "audiobooks",
            "raw_request": "Dune by Frank Herbert",
            "status": "needs_selection",
            "canonical_request_id": None,
        }
        row.update(overrides)
        with sqlite3.connect(self.path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO requests (discord_user_id, discord_username, channel_id,
                    message_id, media_type, raw_request, status, canonical_request_id)
                VALUES ('1', 'kyle', '9', ?, ?, ?, ?, ?)
                """,
                (
                    str(self.message),
                    row["media_type"],
                    row["raw_request"],
                    row["status"],
                    row["canonical_request_id"],
                ),
            )
            return int(cursor.lastrowid)

    def ids(self, statuses):
        with survey.open_readonly(self.path) as connection:
            return [row["id"] for row in survey.survey_rows(connection, statuses)]

    def test_only_the_requested_statuses_are_surveyed(self):
        stuck = self.insert(status="needs_selection")
        self.insert(status="queued")

        self.assertEqual(self.ids(survey.STUCK_STATUSES), [stuck])

    def test_committed_rows_are_available_as_a_regression_check(self):
        self.insert(status="needs_selection")
        queued = self.insert(status="queued")

        self.assertEqual(self.ids(survey.COMMITTED_STATUSES), [queued])

    def test_aliased_rows_are_excluded(self):
        owner = self.insert(status="queued")
        self.insert(status="needs_selection", canonical_request_id=owner)

        self.assertEqual(self.ids(survey.STUCK_STATUSES), [])

    def test_movies_tv_rows_are_out_of_scope(self):
        self.insert(media_type="movies-tv", raw_request="movie: dune")

        self.assertEqual(self.ids(survey.STUCK_STATUSES), [])

    def test_the_survey_connection_cannot_write(self):
        self.insert()
        with survey.open_readonly(self.path) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("UPDATE requests SET status = 'queued'")


class ProjectionTests(unittest.TestCase):
    def surveyed(self, raw, results, media_type="audiobooks"):
        session = SearchOnlySession(list(results))
        client = AbbaClient("http://abba:8080", session=session)
        surveyor = survey.Surveyor(Registry(client), pause=0, sleep=lambda _: None)
        record = surveyor.row(
            {
                "id": 288,
                "media_type": media_type,
                "status": "needs_selection",
                "external_status": "selection_low_confidence",
                "raw_request": raw,
            }
        )
        self.session = session
        return record

    def test_request_288_moves_straight_to_auto_match(self):
        # The point of the survey, now shipped. 0.8182 blended against a 0.82
        # bar with the runner-up 0.49 behind: the moment the promotion clears
        # the floor the gap gate cannot engage, so the outcome is an
        # acquisition and not a question. The 0.00 column is what this row did
        # before the promotion landed; the baseline column is what it does now.
        record = self.surveyed(
            "Kaiju: Battlefield Surgeon by Matt Dinniman 2019", KAIJU
        )

        self.assertEqual(record["outcomes"][survey.label(0.0)], "decline_low_confidence")
        self.assertEqual(record["outcomes"][survey.BASELINE], "auto_match")
        self.assertEqual(survey.BASELINE, "0.020")
        # The live client score, promoted: 0.78 x (0.767 + 0.02) + 0.22.
        self.assertEqual(record["top_score"], 0.8338)

    def test_a_book_that_merely_contains_the_request_stays_declined(self):
        # "Dune" is wholly present in "Dune Messiah", so recall is complete and
        # the promotion applies to a book nobody asked for.
        record = self.surveyed("Dune", (release(1, "Dune Messiah - Frank Herbert"),))

        self.assertEqual(
            set(record["outcomes"].values()), {"decline_low_confidence"}
        )

    def test_the_survey_never_leaves_the_search_endpoint(self):
        self.surveyed("Kaiju: Battlefield Surgeon by Matt Dinniman", KAIJU)

        self.assertEqual(
            {url for _, url in self.session.calls},
            {"http://abba:8080/api/search"},
        )

    def test_a_search_failure_is_reported_rather_than_fatal(self):
        class Dead:
            def request(self, *args, **kwargs):
                raise OSError("connection refused")

        client = AbbaClient("http://abba:8080", session=Dead())
        surveyor = survey.Surveyor(Registry(client), pause=0, sleep=lambda _: None)
        record = surveyor.row(
            {
                "id": 1,
                "media_type": "audiobooks",
                "status": "needs_selection",
                "external_status": None,
                "raw_request": "Dune by Frank Herbert",
            }
        )

        self.assertIsNone(record["outcomes"])
        self.assertIn("search failed", str(record["skipped"]))

    def test_scoring_drift_from_the_client_is_reported_not_averaged(self):
        # The projections are only worth reading if the survey reproduces the
        # client at a zero bonus. Break the formula and the row must refuse to
        # report an outcome rather than quietly counting a wrong one.
        original = survey.promote
        survey.promote = lambda score, agreement, bonus: score + 0.5
        try:
            record = self.surveyed("Dune by Frank Herbert", KAIJU)
        finally:
            survey.promote = original

        self.assertIsNone(record["outcomes"])
        self.assertIn("disagrees with the client", str(record["skipped"]))


class SummaryTests(unittest.TestCase):
    def record(self, request_id, baseline, promoted, **overrides):
        row = {
            "request_id": request_id,
            "media_type": "audiobooks",
            "status": "needs_selection",
            "outcomes": {survey.label(bonus): baseline for bonus in survey.BONUSES},
        }
        row["outcomes"][survey.label(0.12)] = promoted
        row.update(overrides)
        return row

    def test_a_regression_out_of_auto_match_is_counted_separately(self):
        summary = survey.summarize(
            [
                self.record(1, "decline_low_confidence", "auto_match"),
                self.record(2, "auto_match", "picker_2", status="queued"),
            ]
        )[survey.label(0.12)]

        self.assertEqual(summary["changed"], 2)
        self.assertEqual(summary["into_auto_match"], 1)
        self.assertEqual(summary["out_of_auto_match"], 1)
        self.assertEqual(
            summary["transitions"],
            {
                "auto_match -> picker_2": 1,
                "decline_low_confidence -> auto_match": 1,
            },
        )

    def test_unchanged_rows_are_not_counted(self):
        summary = survey.summarize(
            [self.record(1, "auto_match", "auto_match")]
        )[survey.label(0.12)]

        self.assertEqual(summary["changed"], 0)
        self.assertEqual(summary["request_ids"], [])


if __name__ == "__main__":
    unittest.main()
