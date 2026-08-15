import sys
import unittest
from pathlib import Path
from unittest.mock import Mock


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

from acquisition import (
    PROWLARR_CATEGORIES,
    DirectAcquirer,
    UnsupportedTorrentVersion,
    ambiguity_message,
    magnet_info_hash,
    torrent_info_hash,
)
from matching import (
    normalize_text,
    select_arr_candidate,
    select_release,
    select_shelfarr_candidate,
)
from results import safe_display_title


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

    def test_arr_duplicate_exact_titles_require_a_year(self):
        candidates = [
            {"title": "King Kong", "year": 1933, "tmdbId": 244},
            {"title": "King Kong", "year": 2005, "tmdbId": 254},
        ]
        self.assertIsNone(select_arr_candidate("King Kong", candidates))
        self.assertEqual(
            select_arr_candidate("King Kong 2005", candidates)["tmdbId"], 254
        )

    def test_arr_query_year_rejects_wrong_year_even_when_title_is_exact(self):
        candidates = [
            {"title": "The Thing", "year": 1982, "tmdbId": 1091},
            {"title": "The Thing", "year": 2011, "tmdbId": 60935},
        ]
        self.assertEqual(
            select_arr_candidate("The Thing 1982", reversed(candidates))["tmdbId"],
            1091,
        )

    def test_display_sanitizer_keeps_titles_but_rejects_embedded_credentials(self):
        self.assertEqual(safe_display_title("The Secret Garden"), "The Secret Garden")
        self.assertEqual(
            safe_display_title("Release token=do-not-show", "Requested Book"),
            "Requested Book",
        )

    def test_book_metadata_requires_every_requested_author_token_before_deduping(self):
        def candidate(author, *, work_id="openlibrary:OL893415W"):
            return {
                "work_id": work_id,
                "title": "Dune",
                "author": author,
                "content_kind": "book",
                "available_book_types": ["ebook"],
            }

        for author in (None, "", "Herbert", "Brian Herbert"):
            with self.subTest(author=author):
                selection = select_shelfarr_candidate(
                    "Dune", "Frank Herbert", "ebooks", [candidate(author)]
                )
                self.assertIsNone(selection.selected)
                self.assertEqual(selection.reason, "no_results")

        for author in (
            "Frank Herbert",
            "HERBERT, FRANK",
            "Frank-Herbert",
        ):
            with self.subTest(author=author):
                selection = select_shelfarr_candidate(
                    "Dune", "Frank Herbert", "ebooks", [candidate(author)]
                )
                self.assertEqual(selection.reason, "selected")

        # An invalid first duplicate cannot shadow a later valid copy.
        selection = select_shelfarr_candidate(
            "Dune",
            "Frank Herbert",
            "ebooks",
            [candidate("Brian Herbert"), candidate("Herbert, Frank")],
        )
        self.assertEqual(selection.reason, "selected")
        self.assertEqual(selection.selected["author"], "Herbert, Frank")


class DirectAcquirerTests(unittest.TestCase):
    def setUp(self):
        self.prowlarr = Mock()
        self.qbittorrent = Mock()
        self.qbittorrent.find_torrent.return_value = None
        self.acquirer = DirectAcquirer(self.prowlarr, self.qbittorrent)

    def test_category_filters_match_media_policy(self):
        self.assertEqual(PROWLARR_CATEGORIES["ebooks"], (7020, 7000))
        self.assertEqual(PROWLARR_CATEGORIES["audiobooks"], (3030, 3000))
        self.assertEqual(PROWLARR_CATEGORIES["manga-comics"], (7030, 7000))
        self.assertEqual(PROWLARR_CATEGORIES["roms"], (4050, 1000, 8000))
        self.assertEqual(PROWLARR_CATEGORIES["sheet-music"], (7010, 7000))

    def test_magnet_is_submitted_with_media_category(self):
        info_hash = "a" * 40
        self.prowlarr.search.return_value = [
            {
                "title": "The Hobbit JRR Tolkien EPUB",
                "seeders": 20,
                "guid": "g1",
                "infoHash": info_hash,
                "magnetUrl": f"magnet:?xt=urn:btih:{info_hash}",
            }
        ]
        response = self.acquirer.submit("ebooks", "The Hobbit", "JRR Tolkien", 42)
        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["external_id"], info_hash)
        self.prowlarr.search.assert_called_once_with(
            "The Hobbit JRR Tolkien", (7020, 7000)
        )
        self.qbittorrent.add_magnet.assert_called_once_with(
            f"magnet:?xt=urn:btih:{info_hash}", "ebooks", "huey-42"
        )
        self.qbittorrent.add_tags.assert_called_once_with(info_hash, "huey-42")

    def test_torrent_download_is_forwarded_as_bytes(self):
        info = b"d4:name14:Chrono Triggere"
        torrent = b"d4:info" + info + b"e"
        info_hash = __import__("hashlib").sha1(info).hexdigest()
        self.prowlarr.search.return_value = [
            {
                "title": "Chrono Trigger SNES USA ROM",
                "seeders": 40,
                "guid": "g2",
                "infoHash": info_hash,
                "downloadUrl": "https://prowlarr.invalid/download?secret=redacted",
            }
        ]
        self.prowlarr.download_torrent.return_value = torrent
        response = self.acquirer.submit("roms", "Chrono Trigger", request_id=43)
        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["external_id"], info_hash)
        self.qbittorrent.add_torrent.assert_called_once_with(
            torrent, "roms", "huey-43"
        )
        self.qbittorrent.add_tags.assert_called_once_with(info_hash, "huey-43")

    def test_malformed_torrent_never_falls_back_to_supplied_hash(self):
        self.prowlarr.search.return_value = [
            {
                "title": "Chrono Trigger SNES USA ROM",
                "seeders": 40,
                "infoHash": "b" * 40,
                "downloadUrl": "https://prowlarr.invalid/download/2",
            }
        ]
        self.prowlarr.download_torrent.return_value = b"not bencoded metainfo"
        response = self.acquirer.submit("roms", "Chrono Trigger", request_id=44)
        self.assertEqual(response["status"], "needs_selection")
        self.assertIn("payload identity", response["message"])
        self.qbittorrent.add_torrent.assert_not_called()
        self.qbittorrent.add_tags.assert_not_called()

    def test_present_but_malformed_supplied_hash_is_rejected(self):
        info = b"d4:name14:Chrono Triggere"
        torrent = b"d4:info" + info + b"e"
        self.prowlarr.search.return_value = [
            {
                "title": "Chrono Trigger SNES USA ROM",
                "seeders": 40,
                "infoHash": "not-a-valid-info-hash",
                "downloadUrl": "https://prowlarr.invalid/download/invalid-hash",
            }
        ]
        self.prowlarr.download_torrent.return_value = torrent
        response = self.acquirer.submit("roms", "Chrono Trigger", request_id=46)
        self.assertEqual(response["status"], "needs_selection")
        self.assertIn("invalid torrent identity", response["message"])
        self.prowlarr.download_torrent.assert_not_called()
        self.qbittorrent.add_torrent.assert_not_called()
        self.qbittorrent.add_tags.assert_not_called()

    def test_v2_or_hybrid_torrent_is_explicitly_rejected(self):
        info = b"d12:meta versioni2e4:name4:teste"
        torrent = b"d4:info" + info + b"e"
        self.prowlarr.search.return_value = [
            {
                "title": "Chrono Trigger SNES USA ROM",
                "seeders": 40,
                "infoHash": "c" * 64,
                "downloadUrl": "https://prowlarr.invalid/download/3",
            }
        ]
        self.prowlarr.download_torrent.return_value = torrent
        response = self.acquirer.submit("roms", "Chrono Trigger", request_id=45)
        self.assertEqual(response["status"], "needs_selection")
        self.assertIn("BitTorrent v2 or hybrid", response["message"])
        self.qbittorrent.add_torrent.assert_not_called()
        self.qbittorrent.add_tags.assert_not_called()
        with self.assertRaises(UnsupportedTorrentVersion):
            torrent_info_hash(torrent)

    def test_ambiguous_results_never_submit(self):
        self.prowlarr.search.return_value = [
            {"title": "Clair de Lune sheet music PDF", "seeders": 20, "guid": "a"},
            {"title": "Clair de Lune score PDF", "seeders": 19, "guid": "b"},
        ]
        response = self.acquirer.submit("sheet-music", "Clair de Lune")
        self.assertEqual(response["status"], "needs_selection")
        self.qbittorrent.add_magnet.assert_not_called()
        self.qbittorrent.add_torrent.assert_not_called()

    def test_ambiguous_response_lists_at_most_three_safe_distinctions(self):
        self.prowlarr.search.return_value = [
            {
                "title": "The Archive Jane Doe EPUB retail edition",
                "seeders": 20,
                "size": 2 * 1024 * 1024,
                "guid": "a",
            },
            {
                "title": "The Archive Jane Doe EPUB revised edition",
                "seeders": 20,
                "size": 3 * 1024 * 1024,
                "guid": "b",
            },
            {
                "title": "The Archive Jane Doe PDF illustrated edition",
                "seeders": 20,
                "size": 4 * 1024 * 1024,
                "guid": "c",
            },
            {
                "title": "The Archive Jane Doe MOBI anniversary edition",
                "seeders": 20,
                "size": 5 * 1024 * 1024,
                "guid": "d",
            },
        ]
        response = self.acquirer.submit("ebooks", "The Archive", "Jane Doe")
        self.assertEqual(response["status"], "needs_selection")
        candidate_lines = [
            line for line in response["message"].splitlines() if line.startswith("- ")
        ]
        self.assertEqual(len(candidate_lines), 3)
        self.assertIn("EPUB", response["message"])
        self.assertIn("MiB", response["message"])
        self.assertEqual(
            sum(
                phrase in response["message"]
                for phrase in (
                    "retail edition",
                    "revised edition",
                    "illustrated edition",
                    "anniversary edition",
                )
            ),
            3,
        )
        self.qbittorrent.add_magnet.assert_not_called()
        self.qbittorrent.add_torrent.assert_not_called()

    def test_ambiguous_candidate_metadata_is_sanitized(self):
        candidates = [
            {
                "title": "The Archive Jane Doe EPUB @everyone",
                "seeders": 20,
                "guid": "safe-a",
            },
            {
                "title": "The Archive Jane Doe PDF https://tracker.invalid/?token=do-not-show",
                "seeders": 20,
                "guid": "secret-guid",
            },
            {
                "title": "The Archive Jane Doe MOBI `retail`",
                "seeders": 20,
                "guid": "safe-c",
            },
        ]
        selection = self.acquirer.choose("ebooks", "The Archive", "Jane Doe", candidates)
        message = ambiguity_message(
            selection,
            minimum_confidence=self.acquirer.minimum_confidence,
            runner_up_gap=self.acquirer.runner_up_gap,
        )
        self.assertNotIn("https://", message)
        self.assertNotIn("do-not-show", message)
        self.assertNotIn("secret-guid", message)
        self.assertNotIn("@everyone", message)
        self.assertNotIn("`", message)
        self.assertIn("＠everyone", message)

    def test_low_confidence_response_does_not_dump_candidates(self):
        self.prowlarr.search.return_value = [
            {"title": "Unrelated private release", "seeders": 900, "guid": "hidden"}
        ]
        response = self.acquirer.submit("ebooks", "The Archive", "Jane Doe")
        self.assertEqual(response["status"], "needs_selection")
        self.assertNotIn("Closest safe distinctions", response["message"])
        self.assertNotIn("Unrelated private release", response["message"])

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

    def test_missing_stable_identity_never_submits(self):
        self.prowlarr.search.return_value = [
            {
                "title": "The Hobbit JRR Tolkien EPUB",
                "seeders": 20,
                "magnetUrl": "magnet:?dn=missing-hash",
            }
        ]
        response = self.acquirer.submit("ebooks", "The Hobbit", "JRR Tolkien")
        self.assertEqual(response["status"], "needs_selection")
        self.qbittorrent.add_magnet.assert_not_called()

    def test_v2_or_hybrid_magnet_is_explicitly_rejected(self):
        v1_hash = "a" * 40
        v2_hash = "b" * 64
        self.prowlarr.search.return_value = [
            {
                "title": "The Hobbit JRR Tolkien EPUB",
                "seeders": 20,
                "infoHash": v1_hash,
                "magnetUrl": (
                    f"magnet:?xt=urn:btih:{v1_hash}&xt=urn:btmh:1220{v2_hash}"
                ),
            }
        ]
        response = self.acquirer.submit("ebooks", "The Hobbit", "JRR Tolkien")
        self.assertEqual(response["status"], "needs_selection")
        self.assertIn("BitTorrent v2 or hybrid", response["message"])
        self.qbittorrent.add_magnet.assert_not_called()
        self.qbittorrent.add_tags.assert_not_called()

    def test_mismatched_result_and_magnet_hash_never_submit(self):
        self.prowlarr.search.return_value = [
            {
                "title": "The Hobbit JRR Tolkien EPUB",
                "seeders": 20,
                "infoHash": "a" * 40,
                "magnetUrl": f"magnet:?xt=urn:btih:{'b' * 40}",
            }
        ]
        response = self.acquirer.submit("ebooks", "The Hobbit", "JRR Tolkien")
        self.assertEqual(response["status"], "needs_selection")
        self.qbittorrent.add_magnet.assert_not_called()
        self.qbittorrent.add_tags.assert_not_called()

    def test_tag_failure_after_successful_add_remains_queued_by_hash(self):
        info_hash = "c" * 40
        self.prowlarr.search.return_value = [
            {
                "title": "The Hobbit JRR Tolkien EPUB",
                "seeders": 20,
                "infoHash": info_hash,
                "magnetUrl": f"magnet:?xt=urn:btih:{info_hash}",
            }
        ]
        from clients import ServiceError

        self.qbittorrent.add_tags.side_effect = ServiceError("qBittorrent is unavailable.")
        response = self.acquirer.submit("ebooks", "The Hobbit", "JRR Tolkien", 48)
        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["external_id"], info_hash)
        self.qbittorrent.add_magnet.assert_called_once()

    def test_existing_exact_torrent_is_tagged_without_duplicate_add(self):
        info_hash = "d" * 40
        self.prowlarr.search.return_value = [
            {
                "title": "The Hobbit JRR Tolkien EPUB",
                "seeders": 20,
                "infoHash": info_hash,
                "magnetUrl": f"magnet:?xt=urn:btih:{info_hash}",
            }
        ]
        self.qbittorrent.find_torrent.return_value = {
            "hash": info_hash,
            "category": "ebooks",
        }
        response = self.acquirer.submit("ebooks", "The Hobbit", "JRR Tolkien", 49)
        self.assertEqual(response["status"], "queued")
        self.assertIn("already queued", response["message"])
        self.assertEqual(response["external_id"], info_hash)
        self.qbittorrent.add_tags.assert_called_once_with(info_hash, "huey-49")
        self.qbittorrent.add_magnet.assert_not_called()
        self.qbittorrent.add_torrent.assert_not_called()

    def test_existing_imported_torrent_waits_for_bookbot_ledger_verification(self):
        info_hash = "e" * 40
        self.prowlarr.search.return_value = [
            {
                "title": "The Hobbit JRR Tolkien EPUB",
                "seeders": 20,
                "infoHash": info_hash,
                "magnetUrl": f"magnet:?xt=urn:btih:{info_hash}",
            }
        ]
        self.qbittorrent.find_torrent.return_value = {
            "hash": info_hash,
            "category": "ebooks-imported",
        }
        response = self.acquirer.submit("ebooks", "The Hobbit", "JRR Tolkien", 50)
        self.assertEqual(response["status"], "queued")
        self.assertIn("BookBot will verify", response["message"])
        self.assertNotEqual(response["status"], "completed")
        self.qbittorrent.add_magnet.assert_not_called()

    def test_existing_torrent_in_other_category_fails_closed(self):
        info_hash = "f" * 40
        self.prowlarr.search.return_value = [
            {
                "title": "The Hobbit JRR Tolkien EPUB",
                "seeders": 20,
                "infoHash": info_hash,
                "magnetUrl": f"magnet:?xt=urn:btih:{info_hash}",
            }
        ]
        self.qbittorrent.find_torrent.return_value = {
            "hash": info_hash,
            "category": "movies",
        }
        response = self.acquirer.submit("ebooks", "The Hobbit", "JRR Tolkien", 51)
        self.assertEqual(response["status"], "needs_selection")
        self.assertIn("outside this media category", response["message"])
        self.assertIs(response["manual_intervention"], True)
        self.qbittorrent.add_tags.assert_not_called()
        self.qbittorrent.add_magnet.assert_not_called()
        self.qbittorrent.add_torrent.assert_not_called()

    def test_selected_indexer_title_is_sanitized_before_reply_and_persistence(self):
        info_hash = "1" * 40
        secret_title = "The Archive EPUB @everyone https://tracker.invalid/?token=do-not-show"
        self.prowlarr.search.return_value = [
            {
                "title": secret_title,
                "seeders": 20,
                "infoHash": info_hash,
                "magnetUrl": f"magnet:?xt=urn:btih:{info_hash}",
            }
        ]
        response = self.acquirer.submit("ebooks", "The Archive", request_id=52)
        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["external_title"], "The Archive")
        self.assertNotIn("@everyone", response["message"])
        self.assertNotIn("https://", response["message"])
        self.assertNotIn("do-not-show", response["message"])

    def test_existing_qbit_reply_also_uses_sanitized_selected_title(self):
        info_hash = "2" * 40
        self.prowlarr.search.return_value = [
            {
                "title": "The Archive EPUB `@everyone`",
                "seeders": 20,
                "infoHash": info_hash,
                "magnetUrl": f"magnet:?xt=urn:btih:{info_hash}",
            }
        ]
        self.qbittorrent.find_torrent.return_value = {
            "hash": info_hash,
            "category": "ebooks",
        }
        response = self.acquirer.submit("ebooks", "The Archive", request_id=53)
        self.assertEqual(response["status"], "queued")
        self.assertNotIn("@everyone", response["message"])
        self.assertNotIn("`", response["message"])
        self.assertIn("＠everyone", response["message"])

    def test_info_hash_helpers_support_magnet_and_torrent_file(self):
        info = b"d4:name4:teste"
        torrent = b"d4:info" + info + b"e"
        expected = __import__("hashlib").sha1(info).hexdigest()
        self.assertEqual(torrent_info_hash(torrent), expected)
        self.assertEqual(
            magnet_info_hash(f"magnet:?xt=urn:btih:{expected.upper()}"), expected
        )


if __name__ == "__main__":
    unittest.main()
