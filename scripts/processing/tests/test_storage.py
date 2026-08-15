from __future__ import annotations

import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from bookbot_lib.config import CATEGORY_SPECS
from bookbot_lib.errors import UnsafeSourceError, UnsupportedMediaError
from bookbot_lib.storage import (
    AudiobookMetadata,
    LibraryImporter,
    sanitize_component,
)


HASH_A = "a" * 40
HASH_B = "b" * 40


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.downloads = root / "downloads"
        self.media = root / "media"
        self.downloads.mkdir()
        self.media.mkdir()
        for name in CATEGORY_SPECS:
            (self.downloads / name).mkdir()
        self.importer = LibraryImporter(self.downloads, self.media)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_single_ebook_is_copied_and_source_is_preserved(self) -> None:
        source = self.downloads / "ebooks" / (
            "Skunk Works (z-library.sk, 1lib.sk, z-lib.sk).EPUB"
        )
        source.write_bytes(b"ebook")

        result = self.importer.import_payload(
            str(source), CATEGORY_SPECS["ebooks"], HASH_A
        )

        destination = self.media / "ebooks" / "Books" / "Skunk Works"
        self.assertEqual(destination, result.destination)
        self.assertEqual(b"ebook", (destination / "Skunk Works.epub").read_bytes())
        self.assertEqual(b"ebook", source.read_bytes())
        self.assertTrue((destination / ".bookbot-import.json").is_file())
        self.importer.clear_import_marker(destination, HASH_A)
        self.assertFalse((destination / ".bookbot-import.json").exists())

    def test_nested_audiobook_structure_is_preserved(self) -> None:
        source = self.downloads / "audiobooks" / "Book: One"
        (source / "Disc 1").mkdir(parents=True)
        (source / "Disc 1" / "01: Start.MP3").write_bytes(b"track")
        (source / "cover.JPG").write_bytes(b"image")

        result = self.importer.import_payload(
            str(source), CATEGORY_SPECS["audiobooks"], HASH_A
        )

        self.assertEqual("Book- One", result.title)
        self.assertEqual(
            b"track", (result.destination / "Disc 1" / "01- Start.mp3").read_bytes()
        )
        self.assertEqual(b"image", (result.destination / "cover.jpg").read_bytes())
        self.assertTrue(source.exists())

    def test_audiobook_metadata_is_xml_escaped_and_source_is_preserved(self) -> None:
        source = self.downloads / "audiobooks" / "Tourist Season.m4b"
        source.write_bytes(b"audiobook")
        metadata = AudiobookMetadata(
            'Tourist & <Season> "One"',
            "Brynne & Weaver",
        )

        result = self.importer.import_payload(
            str(source),
            CATEGORY_SPECS["audiobooks"],
            HASH_A,
            audiobook_metadata=metadata,
        )

        self.assertEqual(
            self.media / "audiobooks" / 'Tourist & -Season- -One-',
            result.destination,
        )
        self.assertEqual('Tourist & -Season- -One-', result.title)
        sidecar = result.destination / "metadata.opf"
        raw_sidecar = sidecar.read_bytes()
        self.assertIn(b"Tourist &amp; &lt;Season&gt;", raw_sidecar)
        root = ET.fromstring(raw_sidecar)
        namespace = {"dc": "http://purl.org/dc/elements/1.1/"}
        self.assertEqual(
            metadata.title, root.findtext(".//dc:title", namespaces=namespace)
        )
        self.assertEqual(
            metadata.author, root.findtext(".//dc:creator", namespaces=namespace)
        )
        self.assertEqual(b"audiobook", source.read_bytes())

    def test_source_opf_and_nfo_sidecars_are_preserved_without_generation(self) -> None:
        for suffix, original, torrent_hash in (
            ("opf", b"source opf bytes", HASH_A),
            ("nfo", b"source nfo bytes", HASH_B),
        ):
            with self.subTest(suffix=suffix):
                source = self.downloads / "audiobooks" / f"Book {suffix}"
                source.mkdir()
                (source / "Book.m4b").write_bytes(b"audiobook")
                source_sidecar = source / f"source.{suffix}"
                source_sidecar.write_bytes(original)

                result = self.importer.import_payload(
                    str(source),
                    CATEGORY_SPECS["audiobooks"],
                    torrent_hash,
                    audiobook_metadata=AudiobookMetadata(
                        f"Trusted {suffix} title", "Trusted author"
                    ),
                )

                self.assertEqual(
                    original,
                    (result.destination / f"source.{suffix}").read_bytes(),
                )
                self.assertEqual(original, source_sidecar.read_bytes())
                self.assertFalse((result.destination / "metadata.opf").exists())
                adopted = self.importer.import_payload(
                    str(source),
                    CATEGORY_SPECS["audiobooks"],
                    torrent_hash,
                    audiobook_metadata=AudiobookMetadata(
                        f"Trusted {suffix} title", "Trusted author"
                    ),
                )
                self.assertTrue(adopted.adopted)
                self.assertEqual(
                    original,
                    (adopted.destination / f"source.{suffix}").read_bytes(),
                )
                self.assertFalse((adopted.destination / "metadata.opf").exists())

    def test_trusted_title_collision_is_archived_before_replacement(self) -> None:
        source = self.downloads / "audiobooks" / "Author - Source Title"
        source.mkdir()
        (source / "Book.m4b").write_bytes(b"new audiobook")
        destination = self.media / "audiobooks" / "Trusted- Title"
        destination.mkdir(parents=True)
        (destination / "old.m4b").write_bytes(b"old audiobook")

        result = self.importer.import_payload(
            str(source),
            CATEGORY_SPECS["audiobooks"],
            HASH_A,
            audiobook_metadata=AudiobookMetadata(
                "Trusted: Title", "Trusted author"
            ),
        )

        self.assertEqual(destination, result.destination)
        self.assertEqual(b"new audiobook", (destination / "Book.m4b").read_bytes())
        self.assertIsNotNone(result.archived_path)
        assert result.archived_path is not None
        self.assertEqual(
            b"old audiobook", (result.archived_path / "old.m4b").read_bytes()
        )
        self.assertTrue(source.is_dir())

    def test_unsupported_file_rejects_entire_payload(self) -> None:
        source = self.downloads / "ebooks" / "Unsafe bundle"
        source.mkdir()
        (source / "book.epub").write_bytes(b"book")
        (source / "program.exe").write_bytes(b"program")

        with self.assertRaises(UnsupportedMediaError):
            self.importer.import_payload(
                str(source), CATEGORY_SPECS["ebooks"], HASH_A
            )
        self.assertFalse((self.media / "ebooks" / "Books").exists())

    def test_payload_without_primary_media_is_rejected(self) -> None:
        source = self.downloads / "ebooks" / "Only metadata"
        source.mkdir()
        (source / "cover.jpg").write_bytes(b"image")
        with self.assertRaises(UnsupportedMediaError):
            self.importer.plan(str(source), CATEGORY_SPECS["ebooks"])

    def test_symlink_inside_payload_is_rejected(self) -> None:
        outside = Path(self.temporary.name) / "outside.epub"
        outside.write_bytes(b"outside")
        source = self.downloads / "ebooks" / "Linked"
        source.mkdir()
        (source / "book.epub").write_bytes(b"book")
        (source / "leak.epub").symlink_to(outside)
        with self.assertRaises(UnsafeSourceError):
            self.importer.plan(str(source), CATEGORY_SPECS["ebooks"])

    def test_symlink_in_content_path_is_rejected(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "book.epub").write_bytes(b"outside")
        linked = self.downloads / "ebooks" / "linked"
        linked.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(UnsafeSourceError):
            self.importer.plan(str(linked), CATEGORY_SPECS["ebooks"])

    def test_source_outside_torrent_root_is_rejected(self) -> None:
        source = Path(self.temporary.name) / "outside.epub"
        source.write_bytes(b"outside")
        with self.assertRaises(UnsafeSourceError):
            self.importer.plan(str(source), CATEGORY_SPECS["ebooks"])

    def test_torrent_root_itself_is_rejected(self) -> None:
        with self.assertRaises(UnsafeSourceError):
            self.importer.plan(str(self.downloads), CATEGORY_SPECS["ebooks"])

    def test_symlinked_existing_destination_is_rejected(self) -> None:
        source = self.downloads / "ebooks" / "Book.epub"
        source.write_bytes(b"new")
        destination_root = self.media / "ebooks" / "Books"
        destination_root.mkdir(parents=True)
        outside = Path(self.temporary.name) / "outside-destination"
        outside.mkdir()
        (destination_root / "Book").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(UnsafeSourceError):
            self.importer.import_payload(
                str(source), CATEGORY_SPECS["ebooks"], HASH_A
            )
        self.assertEqual([], list(outside.iterdir()))

    def test_conflict_is_archived_before_replacement(self) -> None:
        source = self.downloads / "ebooks" / "Book.epub"
        source.write_bytes(b"version one")
        first = self.importer.import_payload(
            str(source), CATEGORY_SPECS["ebooks"], HASH_A
        )
        self.importer.clear_import_marker(first.destination, HASH_A)
        source.write_bytes(b"version two")

        second = self.importer.import_payload(
            str(source), CATEGORY_SPECS["ebooks"], HASH_B
        )

        self.assertEqual(b"version two", (second.destination / "Book.epub").read_bytes())
        self.assertIsNotNone(second.archived_path)
        assert second.archived_path is not None
        self.assertEqual(
            b"version one", (second.archived_path / "Book.epub").read_bytes()
        )
        self.assertIn(
            self.media / "duplicates" / "ebooks", second.archived_path.parents
        )

    def test_interrupted_copy_is_adopted_idempotently(self) -> None:
        source = self.downloads / "ebooks" / "Book.epub"
        source.write_bytes(b"book")
        first = self.importer.import_payload(
            str(source), CATEGORY_SPECS["ebooks"], HASH_A
        )
        second = self.importer.import_payload(
            str(source), CATEGORY_SPECS["ebooks"], HASH_A
        )
        self.assertTrue(second.adopted)
        self.assertEqual(first.destination, second.destination)
        self.assertFalse((self.media / "duplicates").exists())

    def test_interrupted_metadata_import_is_adopted_only_when_opf_matches(self) -> None:
        source = self.downloads / "audiobooks" / "Book"
        source.mkdir()
        (source / "Book.m4b").write_bytes(b"audiobook")
        metadata = AudiobookMetadata("Original title", "Original author")
        first = self.importer.import_payload(
            str(source),
            CATEGORY_SPECS["audiobooks"],
            HASH_A,
            audiobook_metadata=metadata,
        )

        second = self.importer.import_payload(
            str(source),
            CATEGORY_SPECS["audiobooks"],
            HASH_A,
            audiobook_metadata=metadata,
        )
        self.assertTrue(second.adopted)
        self.assertEqual(1, len(list(second.destination.glob("*.opf"))))
        self.assertFalse((self.media / "duplicates").exists())

        replacement = self.importer.import_payload(
            str(source),
            CATEGORY_SPECS["audiobooks"],
            HASH_A,
            audiobook_metadata=AudiobookMetadata(
                "Original title", "Corrected author"
            ),
        )
        self.assertFalse(replacement.adopted)
        self.assertIsNotNone(replacement.archived_path)
        root = ET.fromstring((replacement.destination / "metadata.opf").read_bytes())
        namespace = {"dc": "http://purl.org/dc/elements/1.1/"}
        self.assertEqual(
            "Corrected author",
            root.findtext(".//dc:creator", namespaces=namespace),
        )

    def test_changed_trusted_title_cannot_orphan_interrupted_import(self) -> None:
        source = self.downloads / "audiobooks" / "Author - Book"
        source.mkdir()
        (source / "Book.m4b").write_bytes(b"audiobook")
        original = self.importer.import_payload(
            str(source),
            CATEGORY_SPECS["audiobooks"],
            HASH_A,
            audiobook_metadata=AudiobookMetadata(
                "Original title", "Original author"
            ),
        )

        with self.assertRaisesRegex(
            UnsafeSourceError, "interrupted import at a different destination"
        ):
            self.importer.import_payload(
                str(source),
                CATEGORY_SPECS["audiobooks"],
                HASH_A,
                audiobook_metadata=AudiobookMetadata(
                    "Corrected title", "Original author"
                ),
            )

        self.assertTrue(original.destination.is_dir())
        self.assertTrue((original.destination / ".bookbot-import.json").is_file())
        self.assertFalse(
            (self.media / "audiobooks" / "Corrected title").exists()
        )

    def test_hidden_interrupted_staging_does_not_block_safe_retry(self) -> None:
        source = self.downloads / "audiobooks" / "Author - Book.m4b"
        source.write_bytes(b"audiobook")
        destination_root = self.media / "audiobooks"
        destination_root.mkdir()
        interrupted_staging = destination_root / f".bookbot-{HASH_A[:12]}-stale"
        interrupted_staging.mkdir()
        self.importer._write_import_marker(interrupted_staging, HASH_A)

        result = self.importer.import_payload(
            str(source),
            CATEGORY_SPECS["audiobooks"],
            HASH_A,
            audiobook_metadata=AudiobookMetadata("Trusted title", "Author"),
        )

        self.assertEqual(destination_root / "Trusted title", result.destination)
        self.assertTrue(result.destination.is_dir())
        self.assertTrue(interrupted_staging.is_dir())

    def test_trusted_title_slashes_remain_one_destination_component(self) -> None:
        source = self.downloads / "audiobooks" / "Author - Book.m4b"
        source.write_bytes(b"audiobook")

        for title, expected in (("AC/DC", "AC-DC"), ("A/../B", "A-..-B")):
            with self.subTest(title=title):
                result = self.importer.import_payload(
                    str(source),
                    CATEGORY_SPECS["audiobooks"],
                    HASH_A,
                    dry_run=True,
                    audiobook_metadata=AudiobookMetadata(title, "Author"),
                )
                self.assertEqual(expected, result.destination.name)
                self.assertEqual(
                    self.media / "audiobooks", result.destination.parent
                )
                self.assertEqual(
                    1,
                    len(result.destination.relative_to(self.media / "audiobooks").parts),
                )

    def test_trusted_title_dry_run_plans_without_writes(self) -> None:
        source = self.downloads / "audiobooks" / "Author - Source Title.m4b"
        source.write_bytes(b"audiobook")

        result = self.importer.import_payload(
            str(source),
            CATEGORY_SPECS["audiobooks"],
            HASH_A,
            dry_run=True,
            audiobook_metadata=AudiobookMetadata(
                "Trusted: Title", "Trusted author"
            ),
        )

        self.assertEqual(
            self.media / "audiobooks" / "Trusted- Title", result.destination
        )
        self.assertEqual("Trusted- Title", result.title)
        self.assertFalse((self.media / "audiobooks").exists())

    def test_dry_run_plans_without_creating_media_directories(self) -> None:
        source = self.downloads / "roms" / "Game.ZIP"
        source.write_bytes(b"rom")
        result = self.importer.import_payload(
            str(source), CATEGORY_SPECS["roms"], HASH_A, dry_run=True
        )
        self.assertEqual(self.media / "roms" / "Game", result.destination)
        self.assertFalse((self.media / "roms").exists())

    def test_duplicate_sanitized_names_are_rejected(self) -> None:
        source = self.downloads / "ebooks" / "Collision"
        source.mkdir()
        (source / "A:B.epub").write_bytes(b"one")
        (source / "A-B.epub").write_bytes(b"two")
        with self.assertRaises(UnsafeSourceError):
            self.importer.plan(str(source), CATEGORY_SPECS["ebooks"])

    def test_casefolded_destination_collisions_are_rejected(self) -> None:
        source = self.downloads / "ebooks" / "Collision"
        source.mkdir()
        (source / "Book.epub").write_bytes(b"one")
        (source / "book.EPUB").write_bytes(b"two")
        with self.assertRaises(UnsafeSourceError):
            self.importer.plan(str(source), CATEGORY_SPECS["ebooks"])

    def test_multibyte_names_are_truncated_to_safe_component_bytes(self) -> None:
        value = sanitize_component("📚" * 100)
        self.assertLessEqual(len(value.encode("utf-8")), 180)

    def test_sanitize_rejects_hidden_and_traversal_names(self) -> None:
        for unsafe in (".", "..", "   ", ".hidden"):
            with self.subTest(unsafe=unsafe), self.assertRaises(UnsafeSourceError):
                sanitize_component(unsafe)


if __name__ == "__main__":
    unittest.main()
