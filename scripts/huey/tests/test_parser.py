import sys
import unittest
from pathlib import Path


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

from parser import RequestParseError, parse_request


class ParserTests(unittest.TestCase):
    def test_ebook_by_format(self):
        parsed = parse_request(
            "Harry Potter and the Order of the Phoenix by J.K. Rowling", "ebooks"
        )
        self.assertEqual(parsed["title"], "Harry Potter and the Order of the Phoenix")
        self.assertEqual(parsed["author"], "J.K. Rowling")

    def test_audiobook_author_is_case_insensitive(self):
        parsed = parse_request("Dune BY Frank Herbert", "audiobooks")
        self.assertEqual(parsed["title"], "Dune")
        self.assertEqual(parsed["author"], "Frank Herbert")

    def test_one_word_author_is_supported(self):
        parsed = parse_request("Dune by Tolkien", "ebooks")
        self.assertEqual(parsed, {"title": "Dune", "author": "Tolkien"})

    def test_title_only(self):
        parsed = parse_request("  The   Left Hand of Darkness  ", "ebooks")
        self.assertEqual(parsed["title"], "The Left Hand of Darkness")
        self.assertIsNone(parsed["author"])

    def test_stand_by_me_is_not_split(self):
        parsed = parse_request("Stand by Me", "ebooks")
        self.assertEqual(parsed["title"], "Stand by Me")
        self.assertIsNone(parsed["author"])

    def test_stand_by_me_can_still_have_an_author(self):
        parsed = parse_request("Stand by Me by Stephen King", "audiobooks")
        self.assertEqual(parsed["title"], "Stand by Me")
        self.assertEqual(parsed["author"], "Stephen King")

    def test_non_book_channels_do_not_split_by(self):
        parsed = parse_request("Romance by the Bay", "manga-comics")
        self.assertEqual(parsed["title"], "Romance by the Bay")
        self.assertIsNone(parsed["author"])

    def test_movie_and_tv_structured_forms(self):
        self.assertEqual(parse_request("MOVIE: Arrival", "movies-tv")["kind"], "movie")
        parsed = parse_request("tv Severance", "movies-tv")
        self.assertEqual(parsed["kind"], "tv")
        self.assertEqual(parsed["title"], "Severance")

    def test_movie_tv_rejects_untyped_title(self):
        with self.assertRaisesRegex(RequestParseError, "movie"):
            parse_request("Arrival", "movies-tv")

    def test_blank_and_missing_structured_title_are_rejected(self):
        for value, media_type in (("  \n ", "ebooks"), ("movie:", "movies-tv")):
            with self.subTest(value=value):
                with self.assertRaises(RequestParseError):
                    parse_request(value, media_type)


if __name__ == "__main__":
    unittest.main()
