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
from matching import request_target_key


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


class DuplicateTargetBatchTests(unittest.TestCase):
    """A batch must ask about one film once, however many rows name it."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "huey.db"
        self.store = RequestStore(self.path)
        self.store.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def stick(self, *, message_id, target_key="v1:shared", raw="movie: the thing"):
        request, _ = self.store.create_request(
            discord_user_id="1", discord_username="kyle", channel_id="2",
            message_id=message_id, media_type="movies-tv", raw_request=raw,
            title=raw, author=None, target_key=target_key,
        )
        self.store.transition(
            request["id"], "needs_selection", "could not identify one safe match"
        )
        return int(request["id"])

    def run_batch(self, poster, limit=5):
        return redrive_selection.redrive_batch(
            self.store,
            StubbedServices(StubbedRadarr(THE_THING)),
            channel_id="2",
            poster=poster,
            limit=limit,
            pause_seconds=0,
            sleep=lambda _seconds: None,
        )

    def status(self, request_id):
        return self.store.get_request(request_id)["status"]

    def test_one_prompt_is_posted_for_a_cluster(self):
        first, second, third = (
            self.stick(message_id="10"),
            self.stick(message_id="11"),
            self.stick(message_id="12"),
        )
        poster = FakePoster()

        results = self.run_batch(poster)

        self.assertEqual(len(poster.posted), 1)
        self.assertEqual(self.status(first), "awaiting_selection")
        for sibling in (second, third):
            self.assertEqual(self.status(sibling), "needs_selection")
            skipped = next(r for r in results if r["request_id"] == sibling)
            self.assertEqual(skipped["action"], "skipped")
            self.assertEqual(skipped["duplicate_of"], first)

    def test_a_distinct_target_in_the_same_batch_is_still_prompted(self):
        first = self.stick(message_id="10")
        other = self.stick(message_id="11", target_key="v1:other")
        poster = FakePoster()

        self.run_batch(poster)

        self.assertEqual(len(poster.posted), 2)
        self.assertEqual(self.status(first), "awaiting_selection")
        self.assertEqual(self.status(other), "awaiting_selection")

    def test_a_row_whose_target_is_already_owned_is_never_prompted(self):
        owned = self.stick(message_id="10")
        stranded = self.stick(message_id="11")
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE requests SET status = 'queued', service = 'radarr' "
                "WHERE id = ?",
                (owned,),
            )
        poster = FakePoster()

        results = self.run_batch(poster)

        self.assertEqual(poster.posted, [])
        self.assertEqual(self.status(stranded), "needs_selection")
        skipped = next(r for r in results if r["request_id"] == stranded)
        self.assertEqual(skipped["duplicate_of"], owned)
        self.assertEqual(skipped["owner_status"], "queued")

    def test_planning_a_cluster_still_writes_nothing(self):
        first, second = self.stick(message_id="10"), self.stick(message_id="11")

        results = redrive_selection.redrive_batch(
            self.store,
            StubbedServices(StubbedRadarr(THE_THING)),
            channel_id="2",
            poster=None,
            limit=5,
            pause_seconds=0,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(self.status(first), "needs_selection")
        self.assertEqual(self.status(second), "needs_selection")
        self.assertEqual(
            [r["action"] for r in results], ["would_prompt", "skipped"]
        )

    def test_an_unkeyed_row_reserves_its_target_when_re_driven(self):
        with self.store.connect() as connection:
            unkeyed = int(
                connection.execute(
                    """
                    INSERT INTO requests (
                        discord_user_id, discord_username, channel_id, message_id,
                        media_type, raw_request, title, status
                    ) VALUES ('1', 'kyle', '2', '99', 'movies-tv',
                              'movie: the thing', 'the thing', 'needs_selection')
                    """
                ).lastrowid
            )

        self.run_batch(FakePoster())

        with self.store.connect() as connection:
            stored = connection.execute(
                "SELECT target_key FROM requests WHERE id = ?", (unkeyed,)
            ).fetchone()
        self.assertEqual(
            stored["target_key"],
            request_target_key("movies-tv", {"kind": "movie", "title": "the thing"}),
        )


class CollapseTests(unittest.TestCase):
    """``collapse`` reports before it writes, exactly as ``report`` does."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "huey.db"
        self.store = RequestStore(self.path)
        self.store.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def strand(self, *, owner_user="1", alias_user="1"):
        """One acquired owner and one row stranded behind it, as in production."""

        key = request_target_key("movies-tv", {"kind": "movie", "title": "coda 2021"})
        ids = []
        with self.store.connect() as connection:
            for index, user in enumerate((owner_user, alias_user)):
                ids.append(
                    int(
                        connection.execute(
                            """
                            INSERT INTO requests (
                                discord_user_id, discord_username, channel_id,
                                message_id, media_type, raw_request, title,
                                target_key, status
                            ) VALUES (?, 'kyle', '2', ?, 'movies-tv',
                                      'movie: coda 2021', 'coda 2021', ?,
                                      'needs_selection')
                            """,
                            (user, str(700 + index), key),
                        ).lastrowid
                    )
                )
            connection.execute(
                "UPDATE requests SET status = 'queued', service = 'radarr' "
                "WHERE id = ?",
                (ids[0],),
            )
        return ids[0], ids[1]

    def test_the_plan_writes_nothing(self):
        owner, stranded = self.strand()

        plan = redrive_selection.collapse(self.path, apply=False)

        self.assertFalse(plan["applied"])
        self.assertEqual(plan["total"], 1)
        self.assertEqual(plan["rows"][0]["request_id"], stranded)
        self.assertEqual(plan["rows"][0]["owner_request_id"], owner)
        self.assertEqual(
            self.store.get_request(stranded)["status"], "needs_selection"
        )

    def test_applying_aliases_the_stranded_row(self):
        owner, stranded = self.strand()

        value = redrive_selection.collapse(self.path, apply=True)

        self.assertTrue(value["applied"])
        self.assertEqual(value["collapsed"], 1)
        self.assertEqual(value["rows"][0]["collapsed_onto"], owner)
        saved = self.store.get_request(stranded)
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["canonical_request_id"], owner)

    def test_a_cluster_with_no_owner_is_left_alone(self):
        """Nothing has been acquired, so there is no winner to pick."""

        key = request_target_key("movies-tv", {"kind": "movie", "title": "coda 2021"})
        with self.store.connect() as connection:
            for index in range(2):
                connection.execute(
                    """
                    INSERT INTO requests (
                        discord_user_id, discord_username, channel_id, message_id,
                        media_type, raw_request, title, target_key, status
                    ) VALUES ('1', 'kyle', '2', ?, 'movies-tv',
                              'movie: coda 2021', 'coda 2021', ?, 'needs_selection')
                    """,
                    (str(800 + index), key),
                )

        self.assertEqual(redrive_selection.collapse(self.path, apply=False)["total"], 0)

    def test_the_plan_says_whether_the_requester_will_be_told(self):
        _, stranded = self.strand(owner_user="1", alias_user="99")

        plan = redrive_selection.collapse(self.path, apply=False)
        self.assertTrue(plan["rows"][0]["notifies_requester"])

        redrive_selection.collapse(self.path, apply=True)
        with self.store.connect() as connection:
            notices = [
                dict(row)
                for row in connection.execute(
                    "SELECT request_id, event_key FROM notification_deliveries"
                )
            ]
        self.assertEqual(notices, [{"request_id": stranded, "event_key": "request_accepted"}])

    def test_applying_twice_is_a_no_op(self):
        redrive_selection.collapse(self.path, apply=True)
        _, stranded = self.strand()
        redrive_selection.collapse(self.path, apply=True)

        second = redrive_selection.collapse(self.path, apply=True)
        self.assertEqual(second["total"], 0)
        self.assertEqual(
            self.store.get_request(stranded)["status"], "failed"
        )


if __name__ == "__main__":
    unittest.main()
