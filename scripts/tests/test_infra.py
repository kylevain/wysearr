import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import backup
import repair_whisparr_quality
import validate


class BackupTests(unittest.TestCase):
    def test_sqlite_backup_is_consistent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            destination = root / "backup" / "source.db"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE example (value TEXT)")
                connection.execute("INSERT INTO example VALUES ('kept')")
            backup.sqlite_backup(source, destination, anchor=root)
            with sqlite3.connect(destination) as connection:
                self.assertEqual(connection.execute("SELECT value FROM example").fetchone()[0], "kept")
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_external_output_does_not_chmod_shared_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o755)
            output = parent / "checkpoint"
            backup.create_backup(output)
            self.assertEqual(parent.stat().st_mode & 0o777, 0o755)
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)


class WhisparrRepairTests(unittest.TestCase):
    def test_quality_23_is_removed_recursively(self):
        value = [
            {"quality": 22, "items": []},
            {"quality": 23, "items": []},
            {"id": 1000, "items": [{"quality": 23}, {"quality": 4}]},
        ]
        self.assertEqual(
            repair_whisparr_quality.filter_quality_23(value),
            [
                {"quality": 22, "items": []},
                {"id": 1000, "items": [{"quality": 4}]},
            ],
        )

    def test_host_endpoint_reads_configurable_address_and_port(self):
        with tempfile.TemporaryDirectory() as directory:
            original = repair_whisparr_quality.STACK_ROOT
            try:
                repair_whisparr_quality.STACK_ROOT = Path(directory)
                (Path(directory) / ".env").write_text(
                    "WYSEARR_BIND_ADDRESS=10.0.0.5\nWHISPARR_PORT=7777\n",
                    encoding="utf-8",
                )
                self.assertEqual(
                    repair_whisparr_quality.host_endpoint(), ("10.0.0.5", 7777)
                )
            finally:
                repair_whisparr_quality.STACK_ROOT = original


class ValidationTests(unittest.TestCase):
    def test_env_parser_ignores_comments_and_preserves_equals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# ignored\nONE=1\nTOKEN=abc=def\n\n", encoding="utf-8")
            self.assertEqual(validate.load_env(path), {"ONE": "1", "TOKEN": "abc=def"})

    def test_writable_check_uses_and_removes_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            check = validate.writable_check(Path(directory), "test")
            self.assertTrue(check.ok)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_bazarr_acceptance_requires_live_integrations_english_defaults_and_all_providers(self):
        settings = {
            "general": {
                "use_sonarr": True,
                "use_radarr": True,
                "serie_default_enabled": True,
                "serie_default_profile": 7,
                "movie_default_enabled": True,
                "movie_default_profile": 7,
                "enabled_providers": [
                    "embeddedsubtitles",
                    "yifysubtitles",
                    "subf2m",
                ],
            }
        }
        profiles = [{"profileId": 7, "name": "English"}]
        status = {"sonarr_version": "4.0", "radarr_version": "6.0"}
        self.assertEqual(
            validate.bazarr_acceptance(settings, profiles, status),
            (True, True, True),
        )
        settings["general"]["enabled_providers"].remove("subf2m")
        status["radarr_version"] = ""
        self.assertEqual(
            validate.bazarr_acceptance(settings, profiles, status),
            (False, True, False),
        )


if __name__ == "__main__":
    unittest.main()
