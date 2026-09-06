import sys
import unittest
from pathlib import Path


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

from parser import (
    RequestParseError,
    ReservedRequestSyntax,
    parse_request,
    reserved_syntax_notice,
)


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


class TrailingYearAuthorTests(unittest.TestCase):
    """More detail must never produce a worse parse than less detail."""

    def parse(self, raw):
        return parse_request(raw, "audiobooks")

    def test_a_trailing_year_no_longer_swallows_the_author(self):
        # Request #288: every word of the candidate author had to contain a
        # letter, so "2019" rejected the split and the whole string -- "by"
        # and year included -- became a five-word title.
        with_year = self.parse("Kaiju: Battlefield Surgeon by Matt Dinniman 2019")
        without = self.parse("Kaiju: Battlefield Surgeon by Matt Dinniman")

        self.assertEqual(with_year["title"], without["title"])
        self.assertEqual(with_year["author"], without["author"])
        self.assertEqual(with_year["author"], "Matt Dinniman")

    def test_the_year_is_available_as_a_hint(self):
        self.assertEqual(
            self.parse("Kaiju: Battlefield Surgeon by Matt Dinniman 2019")["year"],
            2019,
        )

    def test_the_year_key_is_absent_when_none_was_split_off(self):
        # No existing caller's shape changes.
        self.assertEqual(
            self.parse("Dune by Frank Herbert"), {"title": "Dune", "author": "Frank Herbert"}
        )

    def test_a_number_in_a_name_is_still_not_a_year(self):
        """Narrow on purpose: this is not general numeric tolerance."""

        parsed = self.parse("Something by Blink 182")

        self.assertIsNone(parsed["author"])
        self.assertEqual(parsed["title"], "Something by Blink 182")

    def test_a_bare_year_is_not_an_author(self):
        parsed = self.parse("Something by 2019")

        self.assertIsNone(parsed["author"])
        self.assertEqual(parsed["title"], "Something by 2019")

    def test_a_pronoun_title_survives_a_trailing_year(self):
        parsed = self.parse("Stand by Me 1986")

        self.assertIsNone(parsed["author"])
        self.assertEqual(parsed["title"], "Stand by Me 1986")

    def test_only_four_digits_count(self):
        for raw in ("Something by Author 999", "Something by Author 20199"):
            with self.subTest(raw=raw):
                self.assertIsNone(self.parse(raw)["author"])


class ReservedAuthorSyntaxTests(unittest.TestCase):
    """``Author:`` must never become a request. This is #126's failure mode."""

    BOOK_CHANNELS = ("ebooks", "audiobooks")

    def test_author_prefix_is_rejected_in_both_book_channels(self):
        for media_type in self.BOOK_CHANNELS:
            for raw in (
                "Author: Brandon Sanderson",
                "author:Brandon Sanderson",
                "AUTHOR : Brandon Sanderson",
                "Authors: Brandon Sanderson",
                "  Author:   Brandon Sanderson  ",
            ):
                with self.subTest(media_type=media_type, raw=raw):
                    with self.assertRaises(ReservedRequestSyntax):
                        parse_request(raw, media_type)

    def test_a_bare_keyword_is_reserved_too(self):
        """"Author:" alone names no work either, so it must not parse."""

        with self.assertRaises(ReservedRequestSyntax):
            parse_request("Author:", "audiobooks")

    def test_reserved_syntax_is_a_parse_error_for_existing_callers(self):
        """The backfill and surveys catch RequestParseError and skip the row.

        Subclassing is what keeps them correct with no edit: reserved text
        must reserve no target key.
        """

        with self.assertRaises(RequestParseError):
            parse_request("Author: Brandon Sanderson", "ebooks")

    def test_the_colon_is_required(self):
        """A bare space form would swallow real titles, so it is not reserved."""

        parsed = parse_request("Author Author by Rebecca Kuang", "ebooks")
        self.assertEqual(parsed["title"], "Author Author")
        self.assertEqual(parsed["author"], "Rebecca Kuang")

    def test_a_title_merely_containing_author_is_untouched(self):
        parsed = parse_request("The Death of the Author", "ebooks")
        self.assertEqual(parsed["title"], "The Death of the Author")

    def test_other_channels_keep_the_text_as_a_title(self):
        """There is no author browse outside the book channels to reserve for."""

        parsed = parse_request("Author: Osamu Tezuka", "manga-comics")
        self.assertEqual(parsed["title"], "Author: Osamu Tezuka")

    def test_movies_tv_is_unaffected(self):
        with self.assertRaises(RequestParseError) as caught:
            parse_request("Author: Brandon Sanderson", "movies-tv")
        self.assertNotIsInstance(caught.exception, ReservedRequestSyntax)

    def test_notice_matches_the_parser_and_names_the_channel_rule(self):
        notice = reserved_syntax_notice("Author: Brandon Sanderson", "audiobooks")
        self.assertIsNotNone(notice)
        with self.assertRaises(ReservedRequestSyntax) as caught:
            parse_request("Author: Brandon Sanderson", "audiobooks")
        self.assertEqual(str(caught.exception), notice)

    def test_notice_is_none_for_ordinary_and_non_book_input(self):
        self.assertIsNone(reserved_syntax_notice("Dune by Frank Herbert", "ebooks"))
        self.assertIsNone(reserved_syntax_notice("Author: Osamu Tezuka", "roms"))
        self.assertIsNone(reserved_syntax_notice("Author: X", "movies-tv"))
        self.assertIsNone(reserved_syntax_notice(None, "ebooks"))
        self.assertIsNone(reserved_syntax_notice(12, "ebooks"))


if __name__ == "__main__":
    unittest.main()
