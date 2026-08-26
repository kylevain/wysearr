import sys
import unittest
from pathlib import Path


LOUIE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LOUIE_ROOT))

import server


class QueueProgressTests(unittest.TestCase):
    def test_progress_comes_from_arr_byte_counts(self):
        self.assertEqual(
            server.queue_progress({"size": 1000, "sizeleft": 250}), 75.0
        )
        self.assertEqual(server.queue_progress({"size": 1000, "sizeleft": 1000}), 0.0)
        self.assertEqual(server.queue_progress({"size": 1000, "sizeleft": 0}), 100.0)

    def test_unusable_byte_counts_stay_none(self):
        for record in (
            {"size": 1000},                      # sizeleft absent: the old bug
            {"size": 1000, "sizeleft": None},
            {"size": 0, "sizeleft": 0},
            {"size": None, "sizeleft": 5},
            {"size": 100, "sizeleft": 500},      # left > size is nonsense
            {"size": 100, "sizeleft": -1},
            {"size": "big", "sizeleft": "small"},
        ):
            with self.subTest(record=record):
                self.assertIsNone(server.queue_progress(record))

    def test_torrent_fraction_is_scaled(self):
        self.assertEqual(server.torrent_progress({"progress": 0.5}), 50.0)
        self.assertEqual(server.torrent_progress({"progress": 0}), 0.0)
        self.assertEqual(server.torrent_progress({"progress": 1}), 100.0)
        self.assertEqual(server.torrent_progress({"progress": 0.8125}), 81.25)

    def test_missing_or_out_of_range_torrent_progress_is_none(self):
        for torrent in ({}, {"progress": None}, {"progress": 1.5}, {"progress": -0.1}):
            with self.subTest(torrent=torrent):
                self.assertIsNone(server.torrent_progress(torrent))

    def test_sab_queue_and_history(self):
        self.assertEqual(server.sab_progress({"mb": "100", "mbleft": "25"}), 75.0)
        self.assertEqual(
            server.sab_progress({"_mode": "history", "mb": "0", "mbleft": "0"}), 100.0
        )
        self.assertIsNone(server.sab_progress({"_mode": "queue", "mb": "0"}))


class EnrichProgressTests(unittest.TestCase):
    """enrich() with every upstream stubbed, so only the join logic is exercised."""

    def setUp(self):
        self.arr = {
            "radarr": {"library": [], "queue": [], "wanted": [], "history": []},
            "sonarr": {"library": [], "queue": [], "wanted": [], "history": []},
            "lidarr": {"library": [], "queue": [], "wanted": [], "history": []},
        }
        self.torrents = {}
        self.sab = {}
        self._real_arr = server.arr_snapshot
        self._real_download = server.download_snapshot
        server.arr_snapshot = lambda service: self.arr[service]
        server.download_snapshot = lambda: (self.torrents, self.sab)
        self.addCleanup(self._restore)

    def _restore(self):
        server.arr_snapshot = self._real_arr
        server.download_snapshot = self._real_download

    def item(self, **overrides):
        base = {
            "request_id": "1", "routing_state": "live", "service": "radarr",
            "external_id": "55", "status": "submitted", "progress_pct": None,
            "file_name": None, "download_client": None, "protocol": None,
            "size": None, "eta": None, "searching_since": None,
        }
        base.update(overrides)
        return base

    def queued(self, **overrides):
        record = {"movieId": 55, "size": 2000, "sizeleft": 500, "downloadId": "ABC123"}
        record.update(overrides)
        self.arr["radarr"]["queue"] = [record]

    def test_progress_survives_an_unreachable_download_client(self):
        # This is the reported symptom: qBittorrent contributes nothing.
        self.queued()
        items = [self.item()]
        server.enrich(items)
        self.assertEqual(items[0]["status"], "downloading")
        self.assertEqual(items[0]["progress_pct"], 75.0)

    def test_progress_survives_a_hash_that_does_not_match(self):
        self.queued(downloadId="ABC123")
        self.torrents = {"999999": {"progress": 0.9, "name": "other"}}
        items = [self.item()]
        server.enrich(items)
        self.assertEqual(items[0]["progress_pct"], 75.0)

    def test_matching_torrent_refines_the_arr_estimate(self):
        self.queued()
        self.torrents = {"abc123": {"progress": 0.8125, "name": "file.mkv", "eta": 90}}
        items = [self.item()]
        server.enrich(items)
        self.assertEqual(items[0]["progress_pct"], 81.25)
        self.assertEqual(items[0]["file_name"], "file.mkv")
        self.assertEqual(items[0]["eta"], 90)

    def test_usenet_progress_uses_sabnzbd_and_is_case_sensitive(self):
        self.queued(protocol="usenet", downloadId="SABnzbd_nzo_XyZ")
        self.sab = {"SABnzbd_nzo_XyZ": {"_mode": "queue", "mb": "800", "mbleft": "200"}}
        items = [self.item()]
        server.enrich(items)
        self.assertEqual(items[0]["download_client"], "sabnzbd")
        self.assertEqual(items[0]["progress_pct"], 75.0)

    def test_usenet_without_a_matching_slot_still_reports_arr_progress(self):
        self.queued(protocol="usenet", downloadId="SABnzbd_nzo_XyZ")
        items = [self.item()]
        server.enrich(items)
        self.assertEqual(items[0]["progress_pct"], 75.0)

    def test_queue_without_byte_counts_reports_no_progress(self):
        self.queued(size=None, sizeleft=None)
        items = [self.item()]
        server.enrich(items)
        self.assertEqual(items[0]["status"], "downloading")
        self.assertIsNone(items[0]["progress_pct"])

    def test_a_genuine_zero_is_still_reported_as_zero(self):
        # Not every 0.0% is a bug: a queued torrent really is at zero.
        self.queued(size=2000, sizeleft=2000)
        self.torrents = {"abc123": {"progress": 0.0, "name": "file.mkv"}}
        items = [self.item()]
        server.enrich(items)
        self.assertEqual(items[0]["progress_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
