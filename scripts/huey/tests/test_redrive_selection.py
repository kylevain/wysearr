import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

import redrive_selection
from clients import RadarrClient, ServiceError
from database import RequestStore


def arr_item(title, tmdb_id, year):
    return {"title": title, "tmdbId": tmdb_id, "year": year}


class StubbedRadarr(RadarrClient):
    """The real client, with only the network lookup replaced.

    Ranking, the auto-match gate, and the picker proposal are the production
    implementations, so the survey is tested against the code it will run.
    """

    def __init__(self, results, error=None):
        super().__init__("http://radarr:7878", "key")
        self.results = results
        self.error = error

    def lookup(self, title):
        if self.error is not None:
            raise self.error
        return self.results


class StubbedServices:
    def __init__(self, client):
        self.client = client

    def arr(self, service):
        return self.client


class ClassificationTests(unittest.TestCase):
    def test_a_clear_single_result_auto_matches(self):
        value = redrive_selection.classify(
            StubbedRadarr([arr_item("Arrival", 329865, 2016)]), "Arrival"
        )

        self.assertEqual(value["outcome"], "auto_match")
        self.assertEqual(value["match"]["title"], "Arrival")

    def test_two_indistinguishable_years_offer_a_picker(self):
        value = redrive_selection.classify(
            StubbedRadarr(
                [arr_item("The Thing", 1091, 1982), arr_item("The Thing", 54580, 2011)]
            ),
            "The Thing",
        )

        self.assertEqual(value["outcome"], "picker")
        self.assertEqual(len(value["options"]), 2)

    def test_no_results_still_bails(self):
        value = redrive_selection.classify(StubbedRadarr([]), "Nothing At All")

        self.assertEqual(value["outcome"], "still_bails")
        self.assertEqual(value["reason"], "no lookup results")


class FakePoster:
    def __init__(self, fail=False):
        self.posted = []
        self.fail = fail
        self.next_id = 880000111000

    def post(self, channel_id, content):
        if self.fail:
            raise RuntimeError("discord unavailable")
        self.next_id += 1
        self.posted.append((channel_id, content))
        return str(self.next_id)


THE_THING = [arr_item("The Thing", 1091, 1982), arr_item("The Thing", 54580, 2011)]
ARRIVAL = [arr_item("Arrival", 329865, 2016)]


class BatchTests(unittest.TestCase):
    """The three properties the backfill has to hold before it posts."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = RequestStore(Path(self.temporary.name) / "huey.db")
        self.store.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def stick(self, raw, *, message_id, channel_id="2"):
        request, _ = self.store.create_request(
            discord_user_id="1", discord_username="kyle", channel_id=channel_id,
            message_id=message_id, media_type="movies-tv", raw_request=raw,
            title=raw, author=None, target_key=f"v1:{message_id}",
        )
        self.store.transition(
            request["id"], "needs_selection", "could not identify one safe match"
        )
        return int(request["id"])

    def run_batch(self, results, *, poster, limit=5, ttl_seconds=900):
        return redrive_selection.redrive_batch(
            self.store,
            StubbedServices(StubbedRadarr(results)),
            channel_id="2",
            poster=poster,
            limit=limit,
            ttl_seconds=ttl_seconds,
            pause_seconds=0,
            sleep=lambda _seconds: None,
        )

    def status(self, request_id):
        return self.store.get_request(request_id)["status"]

    def test_a_batch_posts_only_to_the_named_channel(self):
        here = self.stick("movie: the thing", message_id="10")
        elsewhere = self.stick("movie: the thing", message_id="11", channel_id="999")
        poster = FakePoster()

        results = self.run_batch(THE_THING, poster=poster)

        self.assertEqual([channel for channel, _ in poster.posted], ["2"])
        self.assertEqual(self.status(here), "awaiting_selection")
        # The other channel's row was not touched at all.
        self.assertEqual(self.status(elsewhere), "needs_selection")
        skipped = next(r for r in results if r["request_id"] == elsewhere)
        self.assertEqual(skipped["reason"], "row belongs to a different channel")

    def test_a_row_that_would_auto_match_is_reported_not_acquired(self):
        request_id = self.stick("movie: arrival", message_id="20")
        poster = FakePoster()

        results = self.run_batch(ARRIVAL, poster=poster)

        self.assertEqual(poster.posted, [])
        self.assertEqual(self.status(request_id), "needs_selection")
        self.assertEqual(results[0]["action"], "skipped")
        self.assertIn("never acquires", results[0]["reason"])

    def test_an_unanswered_prompt_returns_to_needs_selection_and_redrives(self):
        request_id = self.stick("movie: the thing", message_id="30")
        self.run_batch(THE_THING, poster=FakePoster(), ttl_seconds=1)
        self.assertEqual(self.status(request_id), "awaiting_selection")

        time.sleep(1.2)
        self.store.expire_candidate_confirmations()
        self.assertEqual(self.status(request_id), "needs_selection")

        # The whole point of the guard fix: a second pass can prompt it again.
        again = self.run_batch(THE_THING, poster=FakePoster())
        self.assertEqual(again[0]["action"], "prompted")
        self.assertEqual(self.status(request_id), "awaiting_selection")

    def test_a_failed_post_releases_the_row_and_leaves_it_redrivable(self):
        request_id = self.stick("movie: the thing", message_id="40")

        results = self.run_batch(THE_THING, poster=FakePoster(fail=True))

        self.assertEqual(results[0]["action"], "failed")
        self.assertEqual(self.status(request_id), "needs_selection")
        retried = self.run_batch(THE_THING, poster=FakePoster())
        self.assertEqual(retried[0]["action"], "prompted")

    def test_the_batch_size_bounds_what_is_prompted(self):
        for index in range(3):
            self.stick("movie: the thing", message_id=f"5{index}")
        poster = FakePoster()

        results = self.run_batch(THE_THING, poster=poster, limit=2)

        self.assertEqual(len(poster.posted), 2)
        self.assertEqual(
            sum(1 for r in results if r["action"] == "prompted"), 2
        )

    def test_planning_a_batch_writes_nothing_and_posts_nothing(self):
        request_id = self.stick("movie: the thing", message_id="60")

        results = self.run_batch(THE_THING, poster=None)

        self.assertEqual(results[0]["action"], "would_prompt")
        self.assertIn("Request #", results[0]["prompt"])
        self.assertEqual(self.status(request_id), "needs_selection")


class SurveyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "huey.db"
        self.store = RequestStore(self.path)
        self.store.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def stick(self, raw, *, media_type="movies-tv", message_id="1", error=None):
        request, _ = self.store.create_request(
            discord_user_id="1", discord_username="kyle", channel_id="2",
            message_id=message_id, media_type=media_type, raw_request=raw,
            title=raw, author=None,
        )
        self.store.transition(
            request["id"], "needs_selection", "could not identify one safe match",
            error=error,
        )
        return request["id"]

    def rows(self):
        with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            return redrive_selection.stuck_rows(connection)

    def test_only_movies_tv_needs_selection_is_surveyed(self):
        wanted = self.stick("movie: Arrival", message_id="10")
        self.stick("Dune by Frank Herbert", media_type="audiobooks", message_id="11")
        self.stick("Some Ebook", media_type="ebooks", message_id="12")
        queued, _ = self.store.create_request(
            discord_user_id="1", discord_username="kyle", channel_id="2",
            message_id="13", media_type="movies-tv", raw_request="movie: Dune",
            title="Dune", author=None,
        )
        self.store.transition(queued["id"], "queued", "queued")

        self.assertEqual([row["id"] for row in self.rows()], [wanted])

    def test_a_parser_failure_is_skipped_untouched(self):
        request_id = self.stick(
            "Arrival", message_id="20",
            error="Start the request with `movie:` or `tv:` (for example, `movie: Arrival`).",
        )
        row = next(row for row in self.rows() if row["id"] == request_id)

        value = redrive_selection.survey_row(row, StubbedServices(StubbedRadarr([])))
        self.assertEqual(value["outcome"], "skipped_unparsed")

    def test_a_row_with_a_prior_prompt_is_reported_as_blocked(self):
        # create_candidate_confirmation refuses a second confirmation for the
        # same request, so such a row cannot be re-driven at all.
        request_id = self.stick("movie: Arrival", message_id="30")
        self.store.transition(request_id, "processing", "Dispatching", service="radarr")
        self.store.create_candidate_confirmation(
            request_id,
            [
                {"fingerprint": f"{n:064x}", "label": f"Arrival ({2016 + n})",
                 "work_id": f"radarr:tmdb:{n}", "source_work_ids": (f"radarr:tmdb:{n}",),
                 "title": "Arrival", "author": None, "year": 2016 + n,
                 "content_kind": "video", "media_type": "movies-tv",
                 "book_type": "movie"}
                for n in (1, 2)
            ],
            ttl_seconds=1,
        )
        self.store.transition(request_id, "needs_selection", "expired")
        row = next(row for row in self.rows() if row["id"] == request_id)

        value = redrive_selection.survey_row(row, StubbedServices(StubbedRadarr([])))
        self.assertEqual(value["outcome"], "blocked_by_prior_prompt")

    def test_one_dead_upstream_does_not_end_the_survey(self):
        request_id = self.stick("movie: Arrival", message_id="40")
        row = next(row for row in self.rows() if row["id"] == request_id)

        value = redrive_selection.survey_row(
            row, StubbedServices(StubbedRadarr([], error=ServiceError("radarr down")))
        )
        self.assertEqual(value["outcome"], "lookup_failed")
        # The upstream's own words are never echoed into the report.
        self.assertNotIn("down", value["reason"])

    def test_the_survey_cannot_write(self):
        self.stick("movie: Arrival", message_id="50")
        connection = redrive_selection.open_readonly(self.path)
        with self.assertRaises(sqlite3.OperationalError):
            connection.execute("UPDATE requests SET status = 'queued'")
        connection.close()


if __name__ == "__main__":
    unittest.main()
