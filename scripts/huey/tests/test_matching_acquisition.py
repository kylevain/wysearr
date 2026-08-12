import sys
import unittest
from pathlib import Path
from unittest.mock import Mock


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

from acquisition import PROWLARR_CATEGORIES, DirectAcquirer
from matching import normalize_text, select_release


class MatchingTests(unittest.TestCase):
    def test_normalization_is_case_punctuation_and_accent_insensitive(self):
        self.assertEqual(normalize_text("  L'Étranger: EPUB "), "l etranger epub")

    def test_clear_release_is_selected_deterministically(self):
        candidates = [
            {"title": "The Hobbit JRR Tolkien EPUB", "seeders": 15, "guid": "b"},
            {"title": "Unrelated Book EPUB", "seeders": 500, "guid": "a"},
        ]
        selection = select_release(
            "The Hobbit", "JRR Tolkien", "ebooks", reversed(candidates)
        )
        self.assertEqual(selection.reason, "selected")
        self.assertEqual(selection.selected["guid"], "b")

    def test_close_runner_up_requires_selection(self):
        candidates = [
            {"title": "The Hobbit JRR Tolkien EPUB retail", "seeders": 20, "guid": "a"},
            {"title": "The Hobbit JRR Tolkien EPUB scan", "seeders": 18, "guid": "b"},
        ]
        selection = select_release("The Hobbit", "JRR Tolkien", "ebooks", candidates)
        self.assertIsNone(selection.selected)
        self.assertEqual(selection.reason, "ambiguous")

    def test_low_confidence_and_no_results_are_not_selected(self):
        low = select_release(
            "Chrono Trigger", None, "roms", [{"title": "Final Fantasy VI ROM", "seeders": 99}]
        )
        empty = select_release("Chrono Trigger", None, "roms", [])
        self.assertEqual(low.reason, "low_confidence")
        self.assertEqual(empty.reason, "no_results")


class DirectAcquirerTests(unittest.TestCase):
    def setUp(self):
        self.prowlarr = Mock()
        self.qbittorrent = Mock()
        self.acquirer = DirectAcquirer(self.prowlarr, self.qbittorrent)

    def test_category_filters_match_media_policy(self):
        self.assertEqual(PROWLARR_CATEGORIES["ebooks"], (7020, 7000))
        self.assertEqual(PROWLARR_CATEGORIES["audiobooks"], (3030, 3000))
        self.assertEqual(PROWLARR_CATEGORIES["manga-comics"], (7030, 7000))
        self.assertEqual(PROWLARR_CATEGORIES["roms"], (4050, 1000, 8000))
        self.assertEqual(PROWLARR_CATEGORIES["sheet-music"], (7010, 7000))

    def test_magnet_is_submitted_with_media_category(self):
        self.prowlarr.search.return_value = [
            {
                "title": "The Hobbit JRR Tolkien EPUB",
                "seeders": 20,
                "guid": "g1",
                "magnetUrl": "magnet:?xt=urn:btih:safe",
            }
        ]
        response = self.acquirer.submit("ebooks", "The Hobbit", "JRR Tolkien", 42)
        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["external_id"], "g1")
        self.prowlarr.search.assert_called_once_with(
            "The Hobbit JRR Tolkien", (7020, 7000)
        )
        self.qbittorrent.add_magnet.assert_called_once_with(
            "magnet:?xt=urn:btih:safe", "ebooks", "huey-42"
        )

    def test_torrent_download_is_forwarded_as_bytes(self):
        self.prowlarr.search.return_value = [
            {
                "title": "Chrono Trigger SNES USA ROM",
                "seeders": 40,
                "guid": "g2",
                "downloadUrl": "https://prowlarr.invalid/download?secret=redacted",
            }
        ]
        self.prowlarr.download_torrent.return_value = b"torrent"
        response = self.acquirer.submit("roms", "Chrono Trigger", request_id=43)
        self.assertEqual(response["status"], "queued")
        self.qbittorrent.add_torrent.assert_called_once_with(
            b"torrent", "roms", "huey-43"
        )

    def test_ambiguous_results_never_submit(self):
        self.prowlarr.search.return_value = [
            {"title": "Clair de Lune sheet music PDF", "seeders": 20, "guid": "a"},
            {"title": "Clair de Lune score PDF", "seeders": 19, "guid": "b"},
        ]
        response = self.acquirer.submit("sheet-music", "Clair de Lune")
        self.assertEqual(response["status"], "needs_selection")
        self.qbittorrent.add_magnet.assert_not_called()
        self.qbittorrent.add_torrent.assert_not_called()

    def test_explicit_usenet_result_is_not_submitted_to_qbittorrent(self):
        self.prowlarr.search.return_value = [
            {
                "title": "The Hobbit JRR Tolkien EPUB",
                "seeders": 20,
                "guid": "nzb-1",
                "downloadProtocol": "usenet",
                "downloadUrl": "https://prowlarr.invalid/download/1",
            }
        ]
        response = self.acquirer.submit("ebooks", "The Hobbit", "JRR Tolkien", 42)
        self.assertEqual(response["status"], "needs_selection")
        self.qbittorrent.add_torrent.assert_not_called()

    def test_missing_download_source_needs_selection(self):
        self.prowlarr.search.return_value = [
            {"title": "The Hobbit JRR Tolkien EPUB", "seeders": 20, "guid": "g1"}
        ]
        response = self.acquirer.submit("ebooks", "The Hobbit", "JRR Tolkien")
        self.assertEqual(response["status"], "needs_selection")


if __name__ == "__main__":
    unittest.main()
