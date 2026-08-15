import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import closing
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import backup
import bootstrap_lazylibrarian
import repair_whisparr_quality
import validate


VALID_CHANNEL_INVENTORY = """\
requests:
  movies-tv: 1
  ebooks: 2
  audiobooks: 3
  manga-comics: 4
  roms: 5
  sheet-music: 6
activity:
  download-queue: 7
  request-status: 8
  recent-additions: 9
system:
  import-errors: 10
  system-health: 11
"""


class BackupTests(unittest.TestCase):
    def test_sqlite_backup_is_consistent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            destination = root / "backup" / "source.db"
            with closing(sqlite3.connect(source)) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE example (value TEXT)")
                connection.execute("INSERT INTO example VALUES ('kept')")
            backup.sqlite_backup(source, destination, anchor=root)
            with closing(sqlite3.connect(destination)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM example").fetchone()[0], "kept")
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertFalse(Path(f"{destination}-wal").exists())
            self.assertFalse(Path(f"{destination}-shm").exists())

    def test_external_output_does_not_chmod_shared_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o755)
            output = parent / "checkpoint"
            with mock.patch.object(backup, "STACK_ROOT", parent):
                backup.create_backup(output)
            self.assertEqual(parent.stat().st_mode & 0o777, 0o755)
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)

    def test_checkpoint_records_committed_generation_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "checkpoint"
            with (
                mock.patch.object(backup, "STACK_ROOT", root),
                mock.patch.object(backup, "git_head", return_value="a" * 40),
                mock.patch.object(backup, "git_dirty", return_value=True),
            ):
                manifest = backup.create_backup(output)

            self.assertEqual(manifest["git_head"], "a" * 40)
            self.assertIs(manifest["git_dirty"], True)
            self.assertEqual(backup.verify_backup(output), manifest)

    def test_checkpoint_includes_bounded_qbittorrent_resume_metadata_not_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / backup.QBITTORRENT_RESUME_RELATIVE
            resume.mkdir(parents=True)
            (resume / "abc.torrent").write_bytes(b"torrent metadata")
            (resume / "abc.fastresume").write_bytes(b"resume metadata")
            payload = root / "state/torrents/complete/movie.mkv"
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"payload bytes")
            # Payload names are untrusted. Database-like suffixes below either
            # excluded payload root must never enter SQLite discovery or make a
            # control-state checkpoint fail.
            (payload.parent / "not-a-database.db").write_bytes(b"payload")
            staged_database = root / "state/shelfarr-staging/ebook/book.sqlite3"
            staged_database.parent.mkdir(parents=True)
            with closing(sqlite3.connect(staged_database)) as connection, connection:
                connection.execute("CREATE TABLE payload (value TEXT)")
            output = root / "checkpoint"

            with mock.patch.object(backup, "STACK_ROOT", root):
                manifest = backup.create_backup(output)

            paths = {entry["path"] for entry in manifest["files"]}
            self.assertIn(
                "config/qbittorrent/qBittorrent/BT_backup/abc.torrent", paths
            )
            self.assertIn(
                "config/qbittorrent/qBittorrent/BT_backup/abc.fastresume", paths
            )
            self.assertNotIn("state/torrents/complete/movie.mkv", paths)
            self.assertNotIn("state/torrents/complete/not-a-database.db", paths)
            self.assertNotIn("state/shelfarr-staging/ebook/book.sqlite3", paths)
            self.assertIn(
                "state/torrents/** download payloads",
                manifest["boundary"]["excludes"],
            )
            self.assertEqual(backup.verify_backup(output), manifest)

    def test_checkpoint_includes_complete_shelfarr_storage_without_sqlite_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = root / backup.SHELFARR_STORAGE_RELATIVE
            blob = storage / "blobs/ab/cd/book.epub"
            blob.parent.mkdir(parents=True)
            blob.write_bytes(b"active storage payload")
            (storage / ".secret_key_base").write_text(
                "private-secret\n", encoding="utf-8"
            )
            evaluation = root / "state" / "shelfarr-evaluation"
            evaluation.mkdir(parents=True)
            (evaluation / "results.json").write_text(
                '{"records":[]}\n', encoding="utf-8"
            )
            database = storage / "production.sqlite3"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE requests (title TEXT)")
                connection.execute("INSERT INTO requests VALUES ('kept')")
            with closing(sqlite3.connect(storage / "Logs.DB")) as connection, connection:
                connection.execute("CREATE TABLE logs (message TEXT)")
                connection.execute("INSERT INTO logs VALUES ('sensitive-log-data')")
            output = root / "checkpoint"

            with mock.patch.object(backup, "STACK_ROOT", root):
                manifest = backup.create_backup(output)

            paths = {entry["path"] for entry in manifest["files"]}
            self.assertIn("config/shelfarr/.secret_key_base", paths)
            self.assertIn("config/shelfarr/blobs/ab/cd/book.epub", paths)
            self.assertIn("config/shelfarr/production.sqlite3", paths)
            self.assertNotIn("config/shelfarr/Logs.DB", paths)
            self.assertIn("state/shelfarr-evaluation/results.json", paths)
            self.assertNotIn("config/shelfarr/production.sqlite3-wal", paths)
            self.assertNotIn("config/shelfarr/production.sqlite3-shm", paths)
            self.assertIn(
                "state/shelfarr-staging/** direct-download staging payloads",
                manifest["boundary"]["excludes"],
            )
            self.assertEqual(backup.verify_backup(output), manifest)

    def test_checkpoint_omits_ephemeral_audiobookshelf_validation_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                "PERMANENT_SETTING=kept\n"
                "AUDIOBOOKSHELF_API_TOKEN=temporary-secret\n"
                "ANOTHER_SETTING=also-kept\n",
                encoding="utf-8",
            )
            output = root / "checkpoint"

            with mock.patch.object(backup, "STACK_ROOT", root):
                manifest = backup.create_backup(output)

            checkpoint_env = (output / ".env").read_text(encoding="utf-8")
            self.assertEqual(
                checkpoint_env,
                "PERMANENT_SETTING=kept\nANOTHER_SETTING=also-kept\n",
            )
            self.assertEqual(backup.verify_backup(output), manifest)

    def test_checkpoint_discovers_abba_sqlite_safely_without_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = root / "config" / "abba"
            storage.mkdir(parents=True)
            database = storage / "abba.db"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    "CREATE TABLE correlations (id INTEGER PRIMARY KEY, value TEXT)"
                )
                connection.execute(
                    "INSERT INTO correlations (value) VALUES ('kept')"
                )
            output = root / "checkpoint"

            with mock.patch.object(backup, "STACK_ROOT", root):
                manifest = backup.create_backup(output)

            paths = {entry["path"] for entry in manifest["files"]}
            self.assertIn("config/abba/abba.db", paths)
            self.assertNotIn("config/abba/abba.db-wal", paths)
            self.assertNotIn("config/abba/abba.db-shm", paths)
            with closing(sqlite3.connect(output / "config" / "abba" / "abba.db")) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM correlations").fetchone()[0],
                    "kept",
                )
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
            self.assertEqual(backup.verify_backup(output), manifest)

    def test_checkpoint_excludes_diagnostic_log_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = root / "config" / "sonarr"
            storage.mkdir(parents=True)
            for filename in ("sonarr.db", "logs.db"):
                with closing(sqlite3.connect(storage / filename)) as connection, connection:
                    connection.execute("CREATE TABLE records (value TEXT)")
                    connection.execute("INSERT INTO records VALUES ('sensitive-log-data')")
            output = root / "checkpoint"

            with mock.patch.object(backup, "STACK_ROOT", root):
                manifest = backup.create_backup(output)

            paths = {entry["path"] for entry in manifest["files"]}
            self.assertIn("config/sonarr/sonarr.db", paths)
            self.assertNotIn("config/sonarr/logs.db", paths)
            self.assertIn(
                "service logs.db diagnostic databases",
                manifest["boundary"]["excludes"],
            )
            self.assertEqual(backup.verify_backup(output), manifest)

    def test_checkpoint_includes_lazylibrarian_config_and_sqlite_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = root / "config" / "lazylibrarian"
            storage.mkdir(parents=True)
            (storage / "config.ini").write_text(
                "[API]\napi_key=private-runtime-value\n", encoding="utf-8"
            )
            database = storage / "lazylibrarian.db"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE books (title TEXT)")
                connection.execute("INSERT INTO books VALUES ('kept')")
            output = root / "checkpoint"

            with mock.patch.object(backup, "STACK_ROOT", root):
                manifest = backup.create_backup(output)

            paths = {entry["path"] for entry in manifest["files"]}
            self.assertIn("config/lazylibrarian/config.ini", paths)
            self.assertIn("config/lazylibrarian/lazylibrarian.db", paths)
            self.assertNotIn("config/lazylibrarian/lazylibrarian.db-wal", paths)
            self.assertNotIn("config/lazylibrarian/lazylibrarian.db-shm", paths)
            self.assertEqual(
                (output / "config/lazylibrarian/config.ini").stat().st_mode & 0o777,
                0o600,
            )
            with closing(
                sqlite3.connect(output / "config/lazylibrarian/lazylibrarian.db")
            ) as connection:
                self.assertEqual(
                    connection.execute("SELECT title FROM books").fetchone()[0],
                    "kept",
                )
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
            self.assertEqual(backup.verify_backup(output), manifest)

    def test_qbittorrent_resume_checkpoint_rejects_links_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / backup.QBITTORRENT_RESUME_RELATIVE
            resume.mkdir(parents=True)
            target = root / "elsewhere"
            target.write_bytes(b"not resume state")
            (resume / "unsafe.fastresume").symlink_to(target)
            output = root / "checkpoint"
            output.mkdir()
            with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                backup.copy_qbittorrent_resume_state(
                    output, [], stack_root=root
                )

            (resume / "unsafe.fastresume").unlink()
            (resume / "large.fastresume").write_bytes(b"1234")
            with mock.patch.object(backup, "MAX_QBITTORRENT_RESUME_FILE_BYTES", 3):
                with self.assertRaisesRegex(RuntimeError, "size limit"):
                    backup.copy_qbittorrent_resume_state(
                        output, [], stack_root=root
                    )

            (resume / "large.fastresume").unlink()
            (resume / "one.fastresume").write_bytes(b"12")
            (resume / "two.fastresume").write_bytes(b"34")
            with mock.patch.object(backup, "MAX_QBITTORRENT_RESUME_TOTAL_BYTES", 3):
                with self.assertRaisesRegex(RuntimeError, "byte limit"):
                    backup.copy_qbittorrent_resume_state(
                        output, [], stack_root=root
                    )

    def test_bounded_copy_never_overwrites_or_removes_an_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.write_bytes(b"new")
            destination.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                backup.copy_stable_bounded_private(
                    source, destination, maximum_bytes=10, anchor=root
                )
            self.assertEqual(destination.read_bytes(), b"keep")

    def test_verify_checkpoint_detects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("TOKEN=private\n", encoding="utf-8")
            output = root / "checkpoint"
            with mock.patch.object(backup, "STACK_ROOT", root):
                backup.create_backup(output)
            (output / ".env").write_text("TOKEN=changed\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                backup.verify_backup(output)


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

    def test_failed_rollback_copy_validation_prevents_live_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "whisparr.db"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "CREATE TABLE QualityDefinitions (Quality INTEGER)"
                )
                connection.execute(
                    "CREATE TABLE QualityProfiles (Id INTEGER, Items TEXT)"
                )
                connection.execute("INSERT INTO QualityDefinitions VALUES (23)")
                connection.execute(
                    "INSERT INTO QualityProfiles VALUES (?, ?)",
                    (1, json.dumps([{"quality": 23}])),
                )

            with mock.patch.object(
                repair_whisparr_quality,
                "create_validated_rollback_copy",
                side_effect=RuntimeError("invalid rollback copy"),
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid rollback copy"):
                    repair_whisparr_quality.repair_database(
                        database, root / "rollback.db"
                    )

            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT Quality FROM QualityDefinitions"
                    ).fetchall(),
                    [(23,)],
                )

    def test_whisparr_rollback_copy_is_integrity_checked_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            destination = root / "rollback.db"
            with closing(sqlite3.connect(source)) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE example (value TEXT)")
                connection.execute("INSERT INTO example VALUES ('kept')")
            with closing(sqlite3.connect(source)) as connection:
                repair_whisparr_quality.create_validated_rollback_copy(
                    connection, destination
                )
            with closing(sqlite3.connect(destination)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertFalse(Path(f"{destination}-wal").exists())
            self.assertFalse(Path(f"{destination}-shm").exists())


class DeploymentCheckpointTests(unittest.TestCase):
    def test_deploy_keeps_qbittorrent_and_arr_config_directories_owner_only(self):
        deploy = (SCRIPTS.parent / "deploy.sh").read_text(encoding="utf-8")
        private_permissions = deploy.index(
            "chmod 700 config/{qbittorrent,prowlarr,sonarr,radarr,lidarr,whisparr}"
        )
        self.assertLess(
            private_permissions, deploy.index("docker compose config --quiet")
        )

    def test_deploy_pairs_pre_and_post_validation_checkpoints(self):
        deploy = (SCRIPTS.parent / "deploy.sh").read_text(encoding="utf-8")
        pre = deploy.index("pre-deploy-$deployment_id")
        validation = deploy.index("python3 scripts/validate.py")
        post = deploy.index("post-deploy-$deployment_id")
        success = deploy.index("PASS: WyseARR production stack deployed")
        self.assertLess(pre, validation)
        self.assertLess(validation, post)
        self.assertLess(post, success)

    def test_deploy_quiesces_and_health_gates_book_owners_before_intake(self):
        deploy = (SCRIPTS.parent / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn('case "$shelfarr_flag" in', deploy)
        self.assertIn('case "$abba_flag" in', deploy)
        self.assertIn('case "$lazylibrarian_flag" in', deploy)
        self.assertIn('shelfarr_flag="$(strict_env_value SHELFARR_ENABLED)"', deploy)
        self.assertIn('abba_flag="$(strict_env_value ABBA_ENABLED)"', deploy)
        self.assertIn(
            'lazylibrarian_flag="$(strict_env_value LAZYLIBRARIAN_ENABLED)"',
            deploy,
        )
        self.assertIn(
            'ebook_backends="$(strict_env_value EBOOK_ACQUISITION_BACKENDS)"',
            deploy,
        )
        self.assertIn(
            'ebook_owner="$(strict_env_value EBOOK_ACQUISITION_OWNER)"', deploy
        )
        self.assertIn("shelfarr_services=(sabnzbd shelfarr)", deploy)
        self.assertIn("abba_services=(abba)", deploy)
        self.assertIn("lazylibrarian_services=(lazylibrarian)", deploy)
        self.assertIn("application_build_services=(bookbot huey)", deploy)
        self.assertIn("application_build_services+=(abba)", deploy)
        self.assertIn(
            'docker compose build --pull "${application_build_services[@]}"',
            deploy,
        )
        self.assertNotIn("docker compose build --pull abba bookbot huey", deploy)
        self.assertIn('if [ "${#shelfarr_services[@]}" -gt 0 ]', deploy)
        self.assertIn('if [ "${#abba_services[@]}" -gt 0 ]', deploy)
        self.assertIn('if [ "${#lazylibrarian_services[@]}" -gt 0 ]', deploy)
        pre_stop = deploy.index(
            "docker compose stop huey bookbot abba lazylibrarian shelfarr sabnzbd",
            deploy.index("trap restore_previous_runtime EXIT"),
        )
        # Ignore the recovery hint in the EXIT trap and select the operational
        # pre-deploy checkpoint created after the owners are quiesced.
        pre_checkpoint = deploy.index("pre-deploy-$deployment_id", pre_stop)
        start_sabnzbd = deploy.index(
            "docker compose up -d --remove-orphans sabnzbd", pre_checkpoint
        )
        stop_sabnzbd = deploy.index("docker compose stop sabnzbd", start_sabnzbd)
        prepare_sabnzbd = deploy.index(
            "python3 scripts/bootstrap_shelfarr.py --prepare-sab-config",
            stop_sabnzbd,
        )
        restart_sabnzbd = deploy.index("docker compose start sabnzbd", prepare_sabnzbd)
        start_shelfarr = deploy.index(
            "docker compose up -d --remove-orphans --no-deps shelfarr",
            restart_sabnzbd,
        )
        bootstrap_shelfarr = deploy.index(
            "python3 scripts/bootstrap_shelfarr.py\n", start_shelfarr
        )
        prepare_lazylibrarian = deploy.index(
            "python3 scripts/bootstrap_lazylibrarian.py --prepare-config",
            bootstrap_shelfarr,
        )
        start_lazylibrarian = deploy.index(
            "docker compose up -d --remove-orphans --no-deps lazylibrarian",
            prepare_lazylibrarian,
        )
        health_lazylibrarian = deploy.index(
            "wait_for_health 300 lazylibrarian", start_lazylibrarian
        )
        bootstrap_lazylibrarian = deploy.index(
            "python3 scripts/bootstrap_lazylibrarian.py\n",
            health_lazylibrarian,
        )
        start_abba = deploy.index(
            "docker compose up -d --remove-orphans --no-deps abba",
            bootstrap_lazylibrarian,
        )
        health_abba = deploy.index("wait_for_health 300 abba", start_abba)
        start_huey = deploy.index(
            "docker compose up -d --remove-orphans --no-deps bookbot huey"
        )
        first_validation = deploy.index("python3 scripts/validate.py")
        post_stop = deploy.index(
            "docker compose stop huey bookbot abba lazylibrarian shelfarr sabnzbd",
            pre_stop + 1,
        )
        post_checkpoint = deploy.index("post-deploy-$deployment_id")
        restart_evaluation = deploy.index(
            "docker compose start sabnzbd shelfarr", post_checkpoint
        )
        restart_lazylibrarian = deploy.index(
            "docker compose start lazylibrarian", post_checkpoint
        )
        restart_abba = deploy.index("docker compose start abba", post_checkpoint)
        restart_intake = deploy.index(
            "docker compose start bookbot huey", post_checkpoint
        )
        restart_intake_health = deploy.index(
            "wait_for_health 180 bookbot huey", restart_intake
        )
        second_validation = deploy.index(
            "python3 scripts/validate.py", first_validation + 1
        )

        self.assertLess(pre_stop, pre_checkpoint)
        self.assertLess(pre_checkpoint, start_sabnzbd)
        self.assertLess(start_sabnzbd, stop_sabnzbd)
        self.assertLess(stop_sabnzbd, prepare_sabnzbd)
        self.assertLess(prepare_sabnzbd, restart_sabnzbd)
        self.assertLess(restart_sabnzbd, start_shelfarr)
        self.assertLess(start_shelfarr, bootstrap_shelfarr)
        self.assertLess(bootstrap_shelfarr, prepare_lazylibrarian)
        self.assertLess(prepare_lazylibrarian, start_lazylibrarian)
        self.assertLess(start_lazylibrarian, health_lazylibrarian)
        self.assertLess(health_lazylibrarian, bootstrap_lazylibrarian)
        self.assertLess(bootstrap_lazylibrarian, start_abba)
        self.assertLess(start_abba, health_abba)
        self.assertLess(health_abba, start_huey)
        self.assertLess(bootstrap_shelfarr, start_huey)
        self.assertLess(start_huey, first_validation)
        self.assertLess(first_validation, post_stop)
        self.assertLess(post_stop, post_checkpoint)
        self.assertLess(post_checkpoint, restart_evaluation)
        self.assertLess(restart_evaluation, restart_lazylibrarian)
        self.assertLess(restart_lazylibrarian, restart_abba)
        self.assertLess(restart_abba, restart_intake)
        self.assertLess(restart_intake, restart_intake_health)
        self.assertLess(restart_intake_health, second_validation)

        abba_if = deploy.index(
            'if [ "${#abba_services[@]}" -gt 0 ]; then',
            bootstrap_lazylibrarian,
        )
        abba_else = deploy.index("\nelse\n", abba_if)
        abba_end = deploy.index("\nfi\n", abba_else)
        self.assertIn("docker compose stop abba", deploy[abba_else:abba_end])

        lazylibrarian_if = deploy.index(
            'if [ "${#lazylibrarian_services[@]}" -gt 0 ]; then'
        )
        lazylibrarian_else = deploy.index("\nelse\n", lazylibrarian_if)
        lazylibrarian_end = deploy.index("\nfi\n", lazylibrarian_else)
        self.assertIn(
            "docker compose stop lazylibrarian",
            deploy[lazylibrarian_else:lazylibrarian_end],
        )

        evaluation_if = deploy.index(
            'if [ "${#shelfarr_services[@]}" -gt 0 ]; then'
        )
        disabled_else = deploy.index("\nelse\n", evaluation_if)
        disabled_end = deploy.index("\nfi\n", disabled_else)
        disabled_branch = deploy[disabled_else:disabled_end]
        offline_prepare = disabled_branch.index(
            "python3 scripts/bootstrap_shelfarr.py --prepare-sab-config"
        )
        first_disabled_sab_start = disabled_branch.index(
            "docker compose up -d --remove-orphans sabnzbd"
        )
        self.assertIn(
            "python3 scripts/bootstrap_shelfarr.py --converge-usenet-only",
            disabled_branch,
        )
        self.assertLess(
            offline_prepare,
            first_disabled_sab_start,
        )
        self.assertLess(
            first_disabled_sab_start,
            disabled_branch.index(
                "python3 scripts/bootstrap_shelfarr.py --converge-usenet-only"
            ),
        )
        self.assertLess(
            disabled_branch.index(
                "python3 scripts/bootstrap_shelfarr.py --converge-usenet-only"
            ),
            disabled_branch.index("docker compose stop shelfarr sabnzbd"),
        )

    def test_deploy_rejects_usenet_without_shelfarr_before_runtime_mutation(self):
        deploy = (SCRIPTS.parent / "deploy.sh").read_text(encoding="utf-8")
        ownership_gate = deploy.index(
            'WYSEARR_USENET_ENABLED requires SHELFARR_ENABLED=true'
        )
        first_stop = deploy.index(
            "docker compose stop huey bookbot abba lazylibrarian shelfarr sabnzbd"
        )
        self.assertLess(ownership_gate, first_stop)

    def test_deploy_recreates_qbittorrent_only_for_stale_gluetun_namespace(self):
        deploy = (SCRIPTS.parent / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn(
            "docker compose up -d --no-deps gluetun",
            deploy,
        )
        self.assertIn(
            'if ! service_is_running qbittorrent || \\\n'
            '    [ "$qbittorrent_network_mode" != "container:$gluetun_container" ]; then\n'
            "    docker compose up -d --no-deps --force-recreate qbittorrent\n"
            "fi",
            deploy,
        )
        self.assertIn(
            "docker compose up -d --no-deps qbittorrent-port-forward",
            deploy,
        )
        self.assertNotIn("docker compose restart qbittorrent", deploy)
        self.assertNotIn("docker restart qbittorrent", deploy)
        for command in (
            'docker compose up -d --remove-orphans --no-deps "${core_services[@]}"',
            "docker compose up -d --remove-orphans --no-deps shelfarr",
            "docker compose up -d --remove-orphans --no-deps lazylibrarian",
            "docker compose up -d --remove-orphans --no-deps abba",
            "docker compose up -d --remove-orphans --no-deps bookbot huey",
        ):
            self.assertIn(command, deploy)

    def test_deploy_validates_ebook_cascade_before_runtime_mutation(self):
        deploy = (SCRIPTS.parent / "deploy.sh").read_text(encoding="utf-8")
        policy_gate = deploy.index(
            "production EBOOK_ACQUISITION_BACKENDS must be exactly lazylibrarian,shelfarr"
        )
        first_stop = deploy.index(
            "docker compose stop huey bookbot abba lazylibrarian shelfarr sabnzbd"
        )
        self.assertLess(policy_gate, first_stop)
        self.assertIn(
            "EBOOK_ACQUISITION_BACKENDS contains a blank backend",
            deploy,
        )
        self.assertIn(
            "EBOOK_ACQUISITION_BACKENDS contains duplicate backend",
            deploy,
        )
        self.assertIn(
            "EBOOK_ACQUISITION_BACKENDS contains unknown or noncanonical backend",
            deploy,
        )
        self.assertIn(
            "EBOOK_ACQUISITION_OWNER must match the first configured ebook backend",
            deploy,
        )
        self.assertIn(
            "EBOOK_ACQUISITION_BACKENDS requires LAZYLIBRARIAN_ENABLED=true",
            deploy,
        )
        self.assertIn(
            "EBOOK_ACQUISITION_BACKENDS requires SHELFARR_ENABLED=true",
            deploy,
        )

    def test_deploy_failure_trap_tracks_all_evaluation_and_intake_services(self):
        deploy = (SCRIPTS.parent / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("trap restore_previous_runtime EXIT", deploy)
        restore = deploy.split("restore_previous_runtime() {", 1)[1].split(
            "\n}\ntrap restore_previous_runtime EXIT", 1
        )[0]
        self.assertIn('if [ "$runtime_replaced" -eq 0 ]', restore)
        self.assertIn(
            "deployment failed after runtime replacement; Huey/BookBot/ABBA/LazyLibrarian/Shelfarr/SABnzbd are left stopped",
            restore,
        )
        self.assertIn(
            "docker compose stop huey bookbot abba lazylibrarian shelfarr sabnzbd",
            restore,
        )
        for service in (
            "huey",
            "bookbot",
            "abba",
            "lazylibrarian",
            "sabnzbd",
            "shelfarr",
        ):
            self.assertIn(f"{service}_was_running=0", deploy)
            self.assertIn(f"service_is_running {service}", deploy)
            self.assertIn(f'if [ "${service}_was_running" -eq 1 ]', restore)
            self.assertIn(f"docker compose start {service}", restore)
            self.assertIn(f"docker compose stop {service}", restore)


class LazyLibrarianDeploymentTests(unittest.TestCase):
    def test_lazylibrarian_is_digest_pinned_private_persistent_and_mount_minimal(self):
        compose = (SCRIPTS.parent / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        match = re.search(
            r"(?ms)^  lazylibrarian:\n(.*?)(?=^  \S|\Z)", compose
        )
        self.assertIsNotNone(match)
        service = match.group(1)
        self.assertIn(
            "lscr.io/linuxserver/lazylibrarian@sha256:"
            "f2fd332fb4c5918571f8babd4d52fbcb9ca514be254ba101a47c275cd57eb33f",
            service,
        )
        self.assertIn("02af0464-ls331", service)
        self.assertIn("<<: *common", service)
        self.assertIn("PUID: ${PUID:-1000}", service)
        self.assertIn("PGID: ${PGID:-1000}", service)
        self.assertIn("TZ: ${TZ:-Pacific/Honolulu}", service)
        self.assertIn('UMASK: "077"', service)
        self.assertIn("./config/lazylibrarian:/config", service)
        self.assertIn(
            '"127.0.0.1:${LAZYLIBRARIAN_ADMIN_PORT:-5299}:5299"', service
        )
        self.assertIn("http://127.0.0.1:5299/home", service)
        self.assertIn("qbittorrent:\n        condition: service_healthy", service)
        self.assertIn("prowlarr:\n        condition: service_healthy", service)
        self.assertNotIn(":/downloads", service)
        self.assertNotIn(":/media", service)
        self.assertNotIn("/mnt/media", service)

    def test_huey_receives_ordered_backends_and_lazylibrarian_api_contract(self):
        compose = (SCRIPTS.parent / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        match = re.search(r"(?ms)^  huey:\n(.*?)(?=^  \S|\Z)", compose)
        self.assertIsNotNone(match)
        huey = match.group(1)
        expected = (
            "EBOOK_ACQUISITION_BACKENDS: ${EBOOK_ACQUISITION_BACKENDS:-}",
            "EBOOK_ACQUISITION_OWNER: ${EBOOK_ACQUISITION_OWNER:-}",
            "LAZYLIBRARIAN_ENABLED: ${LAZYLIBRARIAN_ENABLED:-false}",
            "LAZYLIBRARIAN_URL: ${LAZYLIBRARIAN_URL:-http://lazylibrarian:5299}",
            "LAZYLIBRARIAN_API_KEY: ${LAZYLIBRARIAN_API_KEY:-}",
            "LAZYLIBRARIAN_TIMEOUT_SECONDS: ${LAZYLIBRARIAN_TIMEOUT_SECONDS:-30}",
            "LAZYLIBRARIAN_SEARCH_LIMIT: ${LAZYLIBRARIAN_SEARCH_LIMIT:-10}",
            "LAZYLIBRARIAN_METADATA_SOURCE: ${LAZYLIBRARIAN_METADATA_SOURCE:-OpenLibrary}",
            "HUEY_LAZYLIBRARIAN_MINIMUM_CONFIDENCE: ${HUEY_LAZYLIBRARIAN_MINIMUM_CONFIDENCE:-0.80}",
            "HUEY_LAZYLIBRARIAN_RUNNER_UP_GAP: ${HUEY_LAZYLIBRARIAN_RUNNER_UP_GAP:-0.05}",
        )
        for setting in expected:
            self.assertIn(setting, huey)

        example = (SCRIPTS.parent / ".env.example").read_text(encoding="utf-8")
        self.assertIn(
            "EBOOK_ACQUISITION_BACKENDS=lazylibrarian,shelfarr\n", example
        )
        self.assertIn("EBOOK_ACQUISITION_OWNER=lazylibrarian\n", example)
        self.assertIn("LAZYLIBRARIAN_ENABLED=true\n", example)
        self.assertIn("SHELFARR_ENABLED=true\n", example)
        self.assertIn("LAZYLIBRARIAN_ADMIN_PORT=5299\n", example)
        self.assertIn("LAZYLIBRARIAN_URL=http://lazylibrarian:5299\n", example)
        self.assertIn("LAZYLIBRARIAN_API_KEY=\n", example)
        self.assertNotRegex(example, r"(?m)^LAZYLIBRARIAN_API_KEY=.+$")

    def test_shelfarr_remains_independently_deployable_as_rollback(self):
        compose = (SCRIPTS.parent / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        deploy = (SCRIPTS.parent / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("  shelfarr:\n", compose)
        self.assertIn("shelfarr_services=(sabnzbd shelfarr)", deploy)
        self.assertNotIn("bootstrap_lazylibrarian.py --enable", deploy)
        self.assertNotIn(
            'EBOOK_ACQUISITION_OWNER" = "shelfarr"',
            deploy[deploy.index('case "$shelfarr_flag" in'):],
        )


class ShelfarrDeploymentTests(unittest.TestCase):
    def test_shelfarr_sab_client_follows_usenet_feature_flag(self):
        convergence = (SCRIPTS / "shelfarr_bootstrap.rb").read_text(
            encoding="utf-8"
        )
        self.assertIn('input.fetch("usenet_enabled") == true', convergence)
        self.assertIn(
            "usenet_enabled ? %w[direct usenet torrent] : %w[direct torrent]",
            convergence,
        )
        self.assertIn("enabled: usenet_enabled", convergence)

    def test_shelfarr_ebook_services_are_immutable_private_and_persistent(self):
        compose = (SCRIPTS.parent / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "ghcr.io/pedro-revez-silva/shelfarr:2026.08.09.1@sha256:"
            "5e331192a8a7b55e3bee055d28403f83fd9d4977f52b6dcb11c86adcdbb70083",
            compose,
        )
        self.assertIn(
            "lscr.io/linuxserver/sabnzbd:version-5.0.4@sha256:"
            "4307a4ef4a1687c6f7dfa36fc745c1fc10b78db9916b64970b9ff682539dd03b",
            compose,
        )
        self.assertIn('"127.0.0.1:${SHELFARR_ADMIN_PORT:-5056}:80"', compose)
        self.assertIn('"127.0.0.1:${SABNZBD_ADMIN_PORT:-8085}:8080"', compose)
        self.assertIn("./config/shelfarr:/rails/storage", compose)
        self.assertIn(
            "./state/shelfarr-staging/ebooks:/ebooks/.shelfarr-staging", compose
        )
        self.assertIn("/ebooks/Books:/ebooks", compose)
        shelfarr = re.search(r"(?ms)^  shelfarr:\n(.*?)(?=^  \S|\Z)", compose)
        sabnzbd = re.search(r"(?ms)^  sabnzbd:\n(.*?)(?=^  \S|\Z)", compose)
        self.assertIsNotNone(shelfarr)
        self.assertIsNotNone(sabnzbd)
        self.assertNotIn("/audiobooks", shelfarr.group(1))
        self.assertNotIn("}:/downloads\n", shelfarr.group(1))
        self.assertNotIn("}:/downloads\n", sabnzbd.group(1))
        self.assertIn("/shelfarr:/downloads/shelfarr", shelfarr.group(1))
        self.assertIn("/usenet:/downloads/usenet", shelfarr.group(1))
        self.assertIn(
            "/incomplete/usenet:/downloads/incomplete/usenet",
            sabnzbd.group(1),
        )
        self.assertIn("/usenet:/downloads/usenet", sabnzbd.group(1))

    def test_huey_defaults_to_shelfarr_disabled_and_bookbot_remains_deployed(self):
        compose = (SCRIPTS.parent / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("SHELFARR_ENABLED: ${SHELFARR_ENABLED:-false}", compose)
        self.assertIn("  bookbot:\n", compose)
        self.assertIn("dockerfile: docker/bookbot/Dockerfile", compose)
        self.assertIn(
            "HUEY_SELECTION_TTL_SECONDS: ${HUEY_SELECTION_TTL_SECONDS:-900}",
            compose,
        )
        self.assertIn("ABBA_ENABLED: ${ABBA_ENABLED:-false}", compose)
        self.assertIn("ABBA_URL: ${ABBA_URL:-http://abba:5078}", compose)
        self.assertIn("ABBA_SEARCH_LIMIT: ${ABBA_SEARCH_LIMIT:-10}", compose)
        self.assertIn(
            "HUEY_ABBA_MINIMUM_CONFIDENCE: ${HUEY_ABBA_MINIMUM_CONFIDENCE:-0.82}",
            compose,
        )
        self.assertIn(
            "HUEY_ABBA_RUNNER_UP_GAP: ${HUEY_ABBA_RUNNER_UP_GAP:-0.08}",
            compose,
        )

    def test_huey_has_no_hard_dependency_on_optional_book_services(self):
        compose = (SCRIPTS.parent / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        match = re.search(r"(?ms)^  huey:\n(.*?)(?=^  \S|\Z)", compose)
        self.assertIsNotNone(match)
        huey = match.group(1)
        dependencies = re.search(
            r"(?ms)^    depends_on:\n(.*?)(?=^    \S|\Z)", huey
        )
        self.assertIsNotNone(dependencies)
        dependency_block = dependencies.group(1)
        for optional_service in ("abba", "lazylibrarian", "shelfarr", "sabnzbd"):
            self.assertNotIn(f"      {optional_service}:\n", dependency_block)
        for required_service in (
            "qbittorrent",
            "prowlarr",
            "sonarr",
            "radarr",
            "lidarr",
        ):
            self.assertIn(f"      {required_service}:\n", dependency_block)
        self.assertIn("condition: service_healthy", dependency_block)

    def test_abba_is_private_read_only_and_owns_exact_downloader_path(self):
        compose = (SCRIPTS.parent / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        dockerfile = (SCRIPTS.parent / "docker" / "abba" / "Dockerfile").read_text(
            encoding="utf-8"
        )
        match = re.search(r"(?ms)^  abba:\n(.*?)(?=^  \S|\Z)", compose)
        self.assertIsNotNone(match)
        abba = match.group(1)
        self.assertIn("dockerfile: docker/abba/Dockerfile", abba)
        self.assertIn('user: "${PUID:-1000}:${PGID:-1000}"', abba)
        self.assertIn("read_only: true", abba)
        self.assertIn("no-new-privileges:true", abba)
        self.assertIn("cap_drop:\n      - ALL", abba)
        self.assertIn("./config/abba:/config", abba)
        self.assertNotIn("/downloads:", abba)
        self.assertNotIn("ports:", abba)
        self.assertIn("DL_USERNAME: ${QBITTORRENT_USERNAME:-admin}", abba)
        self.assertIn("DL_PASSWORD: ${QBITTORRENT_PASSWORD}", abba)
        self.assertIn("DL_CATEGORY: audiobooks", abba)
        self.assertIn("SAVE_PATH_BASE: /downloads/audiobooks", abba)
        self.assertIn("ABBA_DB_PATH: /config/abba.db", abba)
        self.assertIn('ABBA_MAX_RESULTS: "10"', abba)
        self.assertIn("PORT: \"5078\"", abba)
        self.assertIn("127.0.0.1:5078/health", abba)
        self.assertNotIn("/api/search", abba)
        self.assertIn(
            "FROM ghcr.io/jamesry96/audiobookbay-automated@sha256:"
            "be58c8a0c2ef4ec4c1a1cc6714791b5b72c8bf62a24774ee8c784257c87a2678",
            dockerfile,
        )
        self.assertNotIn(":latest", dockerfile)

    def test_bookbot_does_not_claim_shelfarr_category(self):
        sys.path.insert(0, str(SCRIPTS / "processing"))
        from bookbot_lib.config import CATEGORY_SPECS

        self.assertNotIn("shelfarr", CATEGORY_SPECS)

    def test_disabled_container_check_rejects_restart_loop(self):
        compose_result = mock.MagicMock(returncode=0, stdout="container-id\n")
        restarting_result = mock.MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "State": {
                        "Status": "restarting",
                        "Running": True,
                        "Restarting": True,
                        "Paused": False,
                    }
                }
            ).join(["[", "]"]),
        )
        with mock.patch.object(
            validate.subprocess, "run", side_effect=[compose_result, restarting_result]
        ):
            check = validate.container_stopped_check("shelfarr")
        self.assertFalse(check.ok)
        self.assertIn("restarting", check.detail)

    def test_disabled_container_check_accepts_compose_stopped(self):
        compose_result = mock.MagicMock(returncode=0, stdout="container-id\n")
        stopped_result = mock.MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "State": {
                        "Status": "exited",
                        "Running": False,
                        "Restarting": False,
                        "Paused": False,
                    }
                }
            ).join(["[", "]"]),
        )
        with mock.patch.object(
            validate.subprocess, "run", side_effect=[compose_result, stopped_result]
        ):
            check = validate.container_stopped_check("shelfarr")
        self.assertTrue(check.ok)


class ValidationTests(unittest.TestCase):
    @staticmethod
    def _create_huey_validation_database(database: Path) -> None:
        schema = (SCRIPTS / "huey" / "schema.sql").read_text(encoding="utf-8")
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.executescript(schema)
            connection.executescript(
                """
                CREATE UNIQUE INDEX requests_message_id_uq
                    ON requests(message_id);
                CREATE UNIQUE INDEX requests_active_target_uq
                    ON requests(target_key)
                    WHERE target_key IS NOT NULL
                      AND status IN (
                          'new', 'processing', 'awaiting_selection',
                          'queued', 'complete', 'completed'
                      );
                CREATE UNIQUE INDEX requests_active_ll_hash_uq
                    ON requests(lower(external_id))
                    WHERE service = 'lazylibrarian'
                      AND external_id IS NOT NULL
                      AND status IN (
                          'processing', 'queued', 'complete', 'completed'
                      );
                CREATE UNIQUE INDEX requests_active_abba_hash_uq
                    ON requests(lower(external_id))
                    WHERE service = 'abba'
                      AND external_id IS NOT NULL
                      AND canonical_request_id IS NULL
                      AND status IN (
                          'processing', 'queued', 'complete', 'completed'
                      );
                CREATE UNIQUE INDEX requests_active_abba_candidate_uq
                    ON requests(abba_candidate_id)
                    WHERE service = 'abba'
                      AND abba_candidate_id IS NOT NULL
                      AND canonical_request_id IS NULL
                      AND status IN (
                          'processing', 'queued', 'complete', 'completed'
                      );
                CREATE INDEX requests_canonical_request_idx
                    ON requests(canonical_request_id);
                """
            )

    @staticmethod
    def _insert_huey_validation_request(
        connection: sqlite3.Connection,
        request_id: int,
        *,
        status: str,
        candidate_id: str | None = None,
        info_hash: str | None = None,
        canonical_request_id: int | None = None,
        service: str = "abba",
    ) -> None:
        connection.execute(
            """
            INSERT INTO requests(
                id, discord_user_id, discord_username, channel_id, message_id,
                media_type, raw_request, status, service, external_id,
                external_status, abba_candidate_id, canonical_request_id
            ) VALUES (?, '1', 'requester', '3', ?, 'audiobooks', 'request',
                      ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                f"message-{request_id}",
                status,
                service,
                info_hash,
                "canonical_duplicate" if canonical_request_id is not None else None,
                candidate_id,
                canonical_request_id,
            ),
        )

    @staticmethod
    def _insert_huey_validation_retry(
        connection: sqlite3.Connection,
        request_id: int,
        *,
        identity_key: str | None = None,
        state: str = "queued",
        metadata: dict[str, object] | None = None,
    ) -> None:
        identity_key = identity_key or ("a" * 64)
        metadata = metadata or {
            "fingerprint": "b" * 64,
            "label": "Example Book — Example Author",
            "work_id": "openlibrary:OL1W",
            "source_work_ids": ["openlibrary:OL1W"],
            "title": "Example Book",
            "author": "Example Author",
            "year": 2026,
            "content_kind": "book",
            "media_type": "ebooks",
            "book_type": "ebook",
        }
        metadata_json = json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        request_status = {
            "queued": "failed",
            "retrying": "processing",
            "awaiting_import": "queued",
            "blocked": "failed",
            "fulfilled": "complete",
            "expired": "failed",
        }[state]
        cascade_state = {
            "queued": "failed",
            "retrying": "searching",
            "awaiting_import": "queued",
            "blocked": "failed",
            "fulfilled": "completed",
            "expired": "failed",
        }[state]
        current_ordinal = 0 if state in {
            "retrying",
            "awaiting_import",
            "blocked",
            "fulfilled",
        } else 1
        final_backend = (
            "lazylibrarian"
            if state in {"awaiting_import", "blocked", "fulfilled"}
            else None
        )
        finalizer = "bookbot" if final_backend else None
        connection.execute(
            """
            INSERT INTO requests(
                id, discord_user_id, discord_username, channel_id, message_id,
                media_type, raw_request, title, author, target_key, status,
                service
            ) VALUES (?, '1', 'requester', '2', ?, 'ebooks',
                      'Example Book by Example Author', 'Example Book',
                      'Example Author', ?, ?, 'lazylibrarian')
            """,
            (
                request_id,
                f"ebook-message-{request_id}",
                f"ebooks:{request_id}",
                request_status,
            ),
        )
        connection.execute(
            """
            INSERT INTO ebook_cascades(
                request_id, policy_json, current_ordinal, state, identity_key,
                identity_fingerprint, identity_json, final_backend, finalizer
            ) VALUES (?, '["lazylibrarian","shelfarr"]', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                current_ordinal,
                cascade_state,
                identity_key,
                metadata["fingerprint"],
                metadata_json,
                final_backend,
                finalizer,
            ),
        )
        first_attempt_status = {
            "queued": "miss",
            "retrying": "searching",
            "awaiting_import": "queued",
            "blocked": "failed",
            "fulfilled": "completed",
            "expired": "miss",
        }[state]
        second_attempt_status = (
            "miss" if state in {"queued", "expired"} else "pending"
        )
        connection.executemany(
            """
            INSERT INTO ebook_backend_attempts(
                request_id, ordinal, backend, status, started_at, finished_at,
                external_id
            ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
            """,
            (
                (
                    request_id,
                    0,
                    "lazylibrarian",
                    first_attempt_status,
                    (
                        None
                        if first_attempt_status in {"searching", "queued"}
                        else "2026-01-01 00:00:00"
                    ),
                    ("c" * 40 if final_backend else None),
                ),
                (
                    request_id,
                    1,
                    "shelfarr",
                    second_attempt_status,
                    (
                        "2026-01-01 00:00:00"
                        if second_attempt_status == "miss"
                        else None
                    ),
                    None,
                ),
            ),
        )
        retained_provider_states = {
            "queued",
            "retrying",
            "awaiting_import",
            "blocked",
            "fulfilled",
        }
        provider_identity = f"OL{request_id}W"
        if state in {"awaiting_import", "blocked"}:
            connection.execute(
                """
                UPDATE requests
                SET external_id = ?, external_status = ?
                WHERE id = ?
                """,
                (
                    "c" * 40,
                    "failed" if state == "blocked" else "queued",
                    request_id,
                ),
            )
        if state in retained_provider_states:
            connection.execute(
                """
                UPDATE ebook_backend_attempts
                SET backend_identity = ?
                WHERE request_id = ? AND ordinal = 0
                """,
                (provider_identity, request_id),
            )
            connection.execute(
                """
                INSERT INTO ebook_backend_reservations(
                    backend, backend_identity, request_id
                ) VALUES ('lazylibrarian', ?, ?)
                """,
                (provider_identity, request_id),
            )
        retry_count = 0 if state == "queued" else 7 if state == "expired" else 1
        last_retry_at = None if retry_count == 0 else "2026-01-08 00:00:00"
        next_retry_at = "2026-01-08 00:00:00" if state == "queued" else None
        final_import_state = "verified" if state == "fulfilled" else "pending"
        fulfilled_at = "2026-01-08 01:00:00" if state == "fulfilled" else None
        expired_at = "2026-07-07 00:00:00" if state == "expired" else None
        last_proof_check_at = (
            "2026-01-08 01:30:00.000001" if state == "blocked" else None
        )
        connection.execute(
            """
            INSERT INTO unavailable_retries(
                request_id, media_type, identity_key, metadata_json,
                canonical_title, canonical_creator, canonical_year,
                discord_user_id, discord_username, channel_id, message_id,
                first_unavailable_at, last_retry_at, last_proof_check_at,
                next_retry_at, retry_count, state, final_import_state,
                fulfilled_at, expired_at
            ) VALUES (?, 'ebooks', ?, ?, 'Example Book', 'Example Author', 2026,
                      '1', 'requester', '2', ?, '2026-01-01 00:00:00', ?, ?,
                      ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                identity_key,
                metadata_json,
                f"ebook-message-{request_id}",
                last_retry_at,
                last_proof_check_at,
                next_retry_at,
                retry_count,
                state,
                final_import_state,
                fulfilled_at,
                expired_at,
            ),
        )

    @staticmethod
    def _create_abba_validation_database(database: Path) -> None:
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE acquisitions (
                    correlation_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    info_hash TEXT,
                    title TEXT,
                    category TEXT NOT NULL,
                    save_path TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    state TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT,
                    error_retryable INTEGER NOT NULL DEFAULT 0,
                    error_http_status INTEGER,
                    canonical_correlation_id TEXT,
                    canonical_candidate_correlation_id TEXT,
                    mutation_started_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX acquisitions_info_hash_idx
                    ON acquisitions(info_hash);
                CREATE UNIQUE INDEX acquisitions_hash_owner_uq
                    ON acquisitions(info_hash)
                    WHERE info_hash IS NOT NULL
                      AND canonical_correlation_id IS NULL
                      AND (
                          state != 'failed'
                          OR mutation_started_at IS NOT NULL
                      );
                CREATE UNIQUE INDEX acquisitions_candidate_owner_uq
                    ON acquisitions(candidate_id)
                    WHERE canonical_candidate_correlation_id IS NULL
                      AND (
                          state != 'failed'
                          OR mutation_started_at IS NOT NULL
                      );
                CREATE INDEX acquisitions_canonical_idx
                    ON acquisitions(canonical_correlation_id);
                CREATE INDEX acquisitions_candidate_canonical_idx
                    ON acquisitions(canonical_candidate_correlation_id);
                """
            )

    @staticmethod
    def _insert_abba_validation_acquisition(
        connection: sqlite3.Connection,
        correlation_id: str,
        *,
        candidate_id: str,
        info_hash: str,
        state: str = "queued",
        canonical_correlation_id: str | None = None,
        canonical_candidate_correlation_id: str | None = None,
        mutation_started_at: float | None = 1.0,
        error_code: str | None = None,
        error_retryable: int = 0,
        error_http_status: int | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO acquisitions(
                correlation_id, candidate_id, info_hash, title, category,
                save_path, tag, state, error_code, error_retryable,
                error_http_status, canonical_correlation_id,
                canonical_candidate_correlation_id, mutation_started_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'title', 'audiobooks',
                      '/downloads/audiobooks', 'huey-tag', ?, ?, ?, ?, ?, ?,
                      ?, 1.0, 1.0)
            """,
            (
                correlation_id,
                candidate_id,
                info_hash,
                state,
                error_code,
                error_retryable,
                error_http_status,
                canonical_correlation_id,
                canonical_candidate_correlation_id,
                mutation_started_at,
            ),
        )

    def test_env_parser_ignores_comments_and_preserves_equals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# ignored\nONE=1\nTOKEN=abc=def\n\n", encoding="utf-8")
            self.assertEqual(validate.load_env(path), {"ONE": "1", "TOKEN": "abc=def"})

    def test_feature_flags_are_literal_and_blank_means_disabled(self):
        for key in validate.STRICT_FEATURE_FLAGS:
            for value, expected_enabled in (
                ("", False),
                ("false", False),
                ("true", True),
            ):
                with self.subTest(key=key, value=value):
                    valid, enabled, _detail = validate._strict_feature_flag(
                        {key: value}, key
                    )
                    self.assertTrue(valid)
                    self.assertIs(enabled, expected_enabled)
            for value in (" true ", "TRUE", "1", "yes"):
                with self.subTest(key=key, value=value):
                    valid, _enabled, _detail = validate._strict_feature_flag(
                        {key: value}, key
                    )
                    self.assertFalse(valid)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("WYSEARR_USENET_ENABLED= true \n", encoding="utf-8")
            loaded = validate.load_env(path)
            self.assertEqual(loaded["WYSEARR_USENET_ENABLED"], " true ")
            self.assertFalse(
                validate._strict_feature_flag(
                    loaded, "WYSEARR_USENET_ENABLED"
                )[0]
            )
            path.write_text(
                "WYSEARR_USENET_ENABLED=true\n"
                "WYSEARR_USENET_ENABLED=true\n",
                encoding="utf-8",
            )
            duplicate = validate.load_env(path)
            self.assertFalse(
                validate._strict_feature_flag(
                    duplicate, "WYSEARR_USENET_ENABLED"
                )[0]
            )

            path.write_text(
                "ABBA_ENABLED=true\nABBA_ENABLED=false\n",
                encoding="utf-8",
            )
            duplicate = validate.load_env(path)
            self.assertFalse(
                validate._strict_feature_flag(duplicate, "ABBA_ENABLED")[0]
            )

    def test_ebook_backend_policy_order_and_availability_are_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "LAZYLIBRARIAN_ENABLED=true\n"
                "LAZYLIBRARIAN_ENABLED=false\n"
                "EBOOK_ACQUISITION_BACKENDS=lazylibrarian,shelfarr\n"
                "EBOOK_ACQUISITION_BACKENDS=lazylibrarian,shelfarr\n"
                "EBOOK_ACQUISITION_OWNER=lazylibrarian\n"
                "EBOOK_ACQUISITION_OWNER=shelfarr\n",
                encoding="utf-8",
            )
            environment = validate.load_env(path)
        self.assertFalse(
            validate._strict_feature_flag(
                environment, "LAZYLIBRARIAN_ENABLED"
            )[0]
        )
        self.assertFalse(validate._strict_ebook_owner(environment)[0])
        self.assertFalse(validate._strict_ebook_backends(environment)[0])

        self.assertEqual(
            validate._strict_ebook_owner({}),
            (True, "shelfarr", "literal shelfarr"),
        )
        for owner in ("shelfarr", "lazylibrarian", "direct"):
            with self.subTest(owner=owner):
                valid, selected, _detail = validate._strict_ebook_owner(
                    {"EBOOK_ACQUISITION_OWNER": owner}
                )
                self.assertTrue(valid)
                self.assertEqual(selected, owner)
        for owner in (" lazylibrarian", "LAZYLIBRARIAN", "other"):
            with self.subTest(invalid_owner=owner):
                self.assertFalse(
                    validate._strict_ebook_owner(
                        {"EBOOK_ACQUISITION_OWNER": owner}
                    )[0]
                )
        self.assertTrue(
            validate._strict_ebook_owner(
                {"EBOOK_ACQUISITION_OWNER": "   "}
            )[0]
        )

        valid, backends, _detail = validate._strict_ebook_backends(
            {
                "EBOOK_ACQUISITION_BACKENDS": " lazylibrarian , shelfarr ",
                "EBOOK_ACQUISITION_OWNER": "lazylibrarian",
            }
        )
        self.assertTrue(valid)
        self.assertEqual(backends, ("lazylibrarian", "shelfarr"))
        self.assertTrue(
            validate.ebook_backend_order_check(
                backends, policy_valid=valid
            ).ok
        )
        reversed_valid, reversed_backends, _detail = (
            validate._strict_ebook_backends(
                {"EBOOK_ACQUISITION_BACKENDS": "shelfarr,lazylibrarian"}
            )
        )
        self.assertTrue(reversed_valid)
        self.assertFalse(
            validate.ebook_backend_order_check(
                reversed_backends, policy_valid=reversed_valid
            ).ok
        )

        invalid_policies = (
            "",
            "lazylibrarian,",
            ",shelfarr",
            "lazylibrarian,,shelfarr",
            "lazylibrarian, ,shelfarr",
            "LazyLibrarian,shelfarr",
            "lazylibrarian,direct",
            "lazylibrarian,lazylibrarian",
        )
        for policy in invalid_policies:
            with self.subTest(invalid_policy=policy):
                environment = {"EBOOK_ACQUISITION_BACKENDS": policy}
                if policy == "":
                    # Empty is a deliberate compatibility fallback, not a
                    # malformed list; it still fails the production order.
                    valid, backends, _detail = validate._strict_ebook_backends(
                        environment
                    )
                    self.assertTrue(valid)
                    self.assertFalse(
                        validate.ebook_backend_order_check(
                            backends, policy_valid=valid
                        ).ok
                    )
                else:
                    self.assertFalse(
                        validate._strict_ebook_backends(environment)[0]
                    )

        self.assertFalse(
            validate._strict_ebook_backends(
                {
                    "EBOOK_ACQUISITION_BACKENDS": "lazylibrarian,shelfarr",
                    "EBOOK_ACQUISITION_OWNER": "shelfarr",
                }
            )[0]
        )
        legacy_valid, legacy_backends, _detail = validate._strict_ebook_backends(
            {"EBOOK_ACQUISITION_OWNER": "direct"}
        )
        self.assertTrue(legacy_valid)
        self.assertEqual(legacy_backends, ("direct",))
        self.assertFalse(
            validate.ebook_backend_order_check(
                legacy_backends, policy_valid=legacy_valid
            ).ok
        )
        self.assertFalse(
            validate._strict_ebook_backends(
                {"EBOOK_ACQUISITION_BACKENDS": "direct"}
            )[0]
        )
        credentials = {
            "LAZYLIBRARIAN_URL": "http://lazylibrarian:5299",
            "LAZYLIBRARIAN_API_KEY": "a" * 32,
            "SHELFARR_URL": "http://shelfarr",
            "SHELFARR_API_TOKEN": "private-token",
        }
        self.assertTrue(
            validate.ebook_backend_availability_check(
                ("lazylibrarian", "shelfarr"),
                policy_valid=True,
                environment=credentials,
                shelfarr_enabled=True,
                shelfarr_flag_valid=True,
                lazylibrarian_enabled=True,
                lazylibrarian_flag_valid=True,
            ).ok
        )
        for unavailable in ("lazylibrarian", "shelfarr"):
            with self.subTest(unavailable=unavailable):
                self.assertFalse(
                    validate.ebook_backend_availability_check(
                        ("lazylibrarian", "shelfarr"),
                        policy_valid=True,
                        environment=credentials,
                        shelfarr_enabled=unavailable != "shelfarr",
                        shelfarr_flag_valid=True,
                        lazylibrarian_enabled=unavailable != "lazylibrarian",
                        lazylibrarian_flag_valid=True,
                    ).ok
                )
        for missing_key in ("LAZYLIBRARIAN_API_KEY", "SHELFARR_API_TOKEN"):
            with self.subTest(missing_credential=missing_key):
                incomplete = dict(credentials)
                incomplete[missing_key] = ""
                self.assertFalse(
                    validate.ebook_backend_availability_check(
                        ("lazylibrarian", "shelfarr"),
                        policy_valid=True,
                        environment=incomplete,
                        shelfarr_enabled=True,
                        shelfarr_flag_valid=True,
                        lazylibrarian_enabled=True,
                        lazylibrarian_flag_valid=True,
                    ).ok
                )

    def test_lazylibrarian_runtime_has_only_config_and_exact_huey_route(self):
        secret = "a" * 32
        prowlarr_secret = "prowlarr-private-key"
        shelfarr_secret = "shelfarr-private-token"
        environment = {
            "PUID": "1000",
            "PGID": "1000",
            "TZ": "Pacific/Honolulu",
            "EBOOK_ACQUISITION_BACKENDS": "lazylibrarian,shelfarr",
            "EBOOK_ACQUISITION_OWNER": "lazylibrarian",
            "LAZYLIBRARIAN_URL": "http://lazylibrarian:5299",
            "LAZYLIBRARIAN_API_KEY": secret,
            "PROWLARR_API_KEY": prowlarr_secret,
            "SHELFARR_API_TOKEN": shelfarr_secret,
        }
        lazylibrarian_inspect = {
            "Config": {
                "Image": (
                    "lscr.io/linuxserver/lazylibrarian@sha256:"
                    "f2fd332fb4c5918571f8babd4d52fbcb9ca514be254ba101a47c275cd57eb33f"
                ),
                "Env": [
                    "PUID=1000",
                    "PGID=1000",
                    "TZ=Pacific/Honolulu",
                    "UMASK=077",
                ],
            },
            "Mounts": [
                {
                    "Destination": "/config",
                    "Source": str(
                        (
                            validate.STACK_ROOT / "config" / "lazylibrarian"
                        ).resolve()
                    ),
                    "RW": True,
                }
            ],
        }
        huey_inspect = {
            "Config": {
                "Env": [
                    "EBOOK_ACQUISITION_BACKENDS=lazylibrarian,shelfarr",
                    "EBOOK_ACQUISITION_OWNER=lazylibrarian",
                    "LAZYLIBRARIAN_ENABLED=true",
                    "LAZYLIBRARIAN_URL=http://lazylibrarian:5299",
                    f"LAZYLIBRARIAN_API_KEY={secret}",
                    f"PROWLARR_API_KEY={prowlarr_secret}",
                    "LAZYLIBRARIAN_TIMEOUT_SECONDS=30",
                    "LAZYLIBRARIAN_SEARCH_LIMIT=10",
                    "LAZYLIBRARIAN_METADATA_SOURCE=OpenLibrary",
                    "HUEY_LAZYLIBRARIAN_MINIMUM_CONFIDENCE=0.80",
                    "HUEY_LAZYLIBRARIAN_RUNNER_UP_GAP=0.05",
                    "SHELFARR_ENABLED=true",
                    "SHELFARR_URL=http://shelfarr",
                    f"SHELFARR_API_TOKEN={shelfarr_secret}",
                    "SHELFARR_TIMEOUT_SECONDS=20",
                    "SHELFARR_SEARCH_LIMIT=10",
                    "SHELFARR_LANGUAGE=en",
                    "HUEY_SHELFARR_MINIMUM_CONFIDENCE=0.80",
                    "HUEY_SHELFARR_RUNNER_UP_GAP=0.05",
                ]
            }
        }

        def runner_for(ll_inspect, huey_details=None):
            huey_details = huey_details or huey_inspect
            return mock.MagicMock(
                side_effect=[
                    mock.MagicMock(returncode=0, stdout="ll-id\n"),
                    mock.MagicMock(returncode=0, stdout=json.dumps([ll_inspect])),
                    mock.MagicMock(returncode=0, stdout="huey-id\n"),
                    mock.MagicMock(returncode=0, stdout=json.dumps([huey_details])),
                ]
            )

        checks = validate.lazylibrarian_runtime_checks(
            environment, runner=runner_for(lazylibrarian_inspect)
        )
        self.assertTrue(all(check.ok for check in checks))
        self.assertNotIn(secret, repr(checks))
        self.assertNotIn(prowlarr_secret, repr(checks))
        self.assertNotIn(shelfarr_secret, repr(checks))

        stale_huey = json.loads(json.dumps(huey_inspect))
        stale_huey["Config"]["Env"] = [
            item
            if not item.startswith("PROWLARR_API_KEY=")
            else "PROWLARR_API_KEY=stale-key"
            for item in stale_huey["Config"]["Env"]
        ]
        checks = {
            check.name: check
            for check in validate.lazylibrarian_runtime_checks(
                environment,
                runner=runner_for(lazylibrarian_inspect, stale_huey),
            )
        }
        self.assertFalse(checks["huey:lazylibrarian-routing"].ok)
        self.assertNotIn("stale-key", repr(checks))
        self.assertNotIn(prowlarr_secret, repr(checks))

        stale_policy = json.loads(json.dumps(huey_inspect))
        stale_policy["Config"]["Env"] = [
            item
            if not item.startswith("EBOOK_ACQUISITION_BACKENDS=")
            else "EBOOK_ACQUISITION_BACKENDS=shelfarr,lazylibrarian"
            for item in stale_policy["Config"]["Env"]
        ]
        checks = {
            check.name: check
            for check in validate.lazylibrarian_runtime_checks(
                environment,
                runner=runner_for(lazylibrarian_inspect, stale_policy),
            )
        }
        self.assertFalse(checks["huey:lazylibrarian-routing"].ok)

        excessive = json.loads(json.dumps(lazylibrarian_inspect))
        excessive["Mounts"].append(
            {"Destination": "/downloads", "Source": "/private/payload", "RW": True}
        )
        checks = {
            check.name: check
            for check in validate.lazylibrarian_runtime_checks(
                environment, runner=runner_for(excessive)
            )
        }
        self.assertFalse(checks["lazylibrarian:persistence"].ok)

    def test_lazylibrarian_admin_port_must_be_loopback(self):
        def runner_for(host_ip):
            return mock.MagicMock(
                side_effect=[
                    mock.MagicMock(returncode=0, stdout="ll-id\n"),
                    mock.MagicMock(
                        returncode=0,
                        stdout=json.dumps(
                            [
                                {
                                    "HostConfig": {
                                        "PortBindings": {
                                            "5299/tcp": [
                                                {"HostIp": host_ip, "HostPort": "5299"}
                                            ]
                                        }
                                    }
                                }
                            ]
                        ),
                    ),
                ]
            )

        with mock.patch.object(validate.subprocess, "run", runner_for("127.0.0.1")):
            self.assertTrue(
                validate.private_published_port_check("lazylibrarian").ok
            )
        with mock.patch.object(validate.subprocess, "run", runner_for("0.0.0.0")):
            self.assertFalse(
                validate.private_published_port_check("lazylibrarian").ok
            )

    def test_lazylibrarian_private_config_accepts_default_elision_and_checks_secrets(self):
        secret = "b" * 32
        qbit_secret = "qbit-private-password"
        environment = {
            "EBOOK_ACQUISITION_OWNER": "lazylibrarian",
            "LAZYLIBRARIAN_ENABLED": "true",
            "LAZYLIBRARIAN_API_KEY": secret,
            "QBITTORRENT_USERNAME": "qbit-user",
            "QBITTORRENT_PASSWORD": qbit_secret,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_path = root / ".env"
            env_path.write_text(
                "".join(f"{key}={value}\n" for key, value in environment.items()),
                encoding="utf-8",
            )
            bootstrap_lazylibrarian.prepare_lazylibrarian_config(root)
            config = root / "config" / "lazylibrarian" / "config.ini"

            checks = validate.lazylibrarian_config_checks(config, environment)
            self.assertTrue(all(check.ok for check in checks))
            self.assertNotIn(secret, repr(checks))
            self.assertNotIn(qbit_secret, repr(checks))

            # The pinned image removes values that equal its defaults, so only
            # the non-default credentials are guaranteed to remain on disk.
            config.write_text(
                "[API]\n"
                f"api_key = {secret}\n"
                "[QBITTORRENT]\n"
                "qbittorrent_user = qbit-user\n"
                f"qbittorrent_pass = {qbit_secret}\n",
                encoding="utf-8",
            )
            config.chmod(0o600)
            checks = validate.lazylibrarian_config_checks(config, environment)
            self.assertTrue(all(check.ok for check in checks))

            exact_config = config.read_text(encoding="utf-8")
            config.write_text(
                exact_config.replace(qbit_secret, "stale-qbit-password"),
                encoding="utf-8",
            )
            stale_qbit_checks = {
                check.name: check
                for check in validate.lazylibrarian_config_checks(
                    config, environment
                )
            }
            self.assertFalse(
                stale_qbit_checks["lazylibrarian:config-secrets"].ok
            )
            self.assertNotIn(qbit_secret, repr(stale_qbit_checks))
            self.assertNotIn("stale-qbit-password", repr(stale_qbit_checks))
            config.write_text(exact_config, encoding="utf-8")

            config.write_text(
                config.read_text(encoding="utf-8").replace(secret, "e" * 32),
                encoding="utf-8",
            )
            checks = {
                check.name: check
                for check in validate.lazylibrarian_config_checks(
                    config, environment
                )
            }
            self.assertFalse(checks["lazylibrarian:config-secrets"].ok)

            config.chmod(0o640)
            checks = {
                check.name: check
                for check in validate.lazylibrarian_config_checks(
                    config, environment
                )
            }
            self.assertFalse(checks["lazylibrarian:config-permissions"].ok)

    def test_lazylibrarian_api_probe_is_read_only_bounded_and_secret_safe(self):
        secret = "c" * 32
        help_text = "\n".join(validate.LAZYLIBRARIAN_API_COMMANDS)

        class Response:
            status = 200

            def __init__(self, payload):
                self.payload = payload

            def getcode(self):
                return self.status

            def read(self, _limit):
                return self.payload

            def close(self):
                return None

        class Opener:
            def __init__(self, missing_command=None, version=None):
                self.commands = []
                self.missing_command = missing_command
                self.version = (
                    validate.EXPECTED_LAZYLIBRARIAN_VERSION
                    if version is None
                    else version
                )

            def open(self, request, timeout):
                self.assert_timeout = timeout
                query = urllib.parse.parse_qs(
                    request.data.decode("utf-8")
                )
                self.commands.append(query["cmd"][0])
                if query["cmd"] == ["getVersion"]:
                    payload = json.dumps(
                        {
                            "Success": True,
                            "current_version": self.version,
                        }
                    ).encode()
                else:
                    rendered = help_text.replace(self.missing_command or "\0", "")
                    payload = rendered.encode()
                return Response(payload)

        opener = Opener()
        check = validate.lazylibrarian_api_capability_check(
            "5299", secret, opener=opener
        )
        self.assertTrue(check.ok)
        self.assertEqual(opener.commands, ["getVersion", "help"])
        self.assertNotIn(secret, repr(check))
        for forbidden in ("findBook", "addBook", "searchBook", "queueBook"):
            self.assertNotIn(forbidden, opener.commands)

        check = validate.lazylibrarian_api_capability_check(
            "5299", secret, opener=Opener(missing_command="searchBook")
        )
        self.assertFalse(check.ok)
        self.assertNotIn(secret, repr(check))

        # The pinned LSIO build may omit source-version metadata. Its exact
        # image digest is validated independently by the runtime gate.
        self.assertTrue(
            validate.lazylibrarian_api_capability_check(
                "5299", secret, opener=Opener(version="")
            ).ok
        )
        self.assertFalse(
            validate.lazylibrarian_api_capability_check(
                "5299", secret, opener=Opener(version="unexpected")
            ).ok
        )

    def test_lazylibrarian_effective_config_reads_every_key_with_semantic_values(self):
        secret = "f" * 32

        class Response:
            status = 200

            def __init__(self, payload):
                self.payload = payload

            def getcode(self):
                return self.status

            def read(self, limit):
                self.limit = limit
                return self.payload

            def close(self):
                return None

        class EffectiveOpener:
            def __init__(self, overrides=None):
                self.overrides = dict(overrides or {})
                self.requests = []

            def open(self, request, timeout):
                self.requests.append(request)
                self.timeout = timeout
                body = urllib.parse.parse_qs(
                    request.data.decode("utf-8"), keep_blank_values=True
                )
                self.asserted_key = body["apikey"][0]
                self.asserted_command = body["cmd"][0]
                section = body["group"][0]
                key = body["name"][0].casefold()
                expected = validate.LAZYLIBRARIAN_EFFECTIVE_SETTINGS[section][key]
                value = self.overrides.get((section, key))
                if value is None:
                    if expected == "0":
                        value = ""
                    elif expected == "1":
                        value = "True"
                    elif (section, key) in validate.LAZYLIBRARIAN_CSV_SETTINGS:
                        value = ",".join(
                            item.strip() for item in expected.split(",")
                        )
                    else:
                        value = expected
                payload = value if isinstance(value, bytes) else f"[{value}]".encode()
                return Response(payload)

        opener = EffectiveOpener()
        check = validate.lazylibrarian_effective_config_check(
            "5299", secret, opener=opener
        )
        expected_count = sum(
            len(section_values)
            for section_values in validate.LAZYLIBRARIAN_EFFECTIVE_SETTINGS.values()
        )
        self.assertTrue(check.ok)
        self.assertEqual(len(opener.requests), expected_count)
        self.assertIn(f"{expected_count} effective settings", check.detail)
        self.assertNotIn(secret, repr(check))
        self.assertTrue(
            validate._lazylibrarian_setting_matches(
                "GENERAL", "audio_tab", "False", "0"
            )
        )
        self.assertTrue(
            validate._lazylibrarian_setting_matches(
                "GENERAL",
                "ebook_type",
                "PDF,EPUB, MOBI, AZW3",
                "epub, mobi, azw3, pdf",
            )
        )
        for request in opener.requests:
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(urllib.parse.urlsplit(request.full_url).query, "")
            self.assertNotIn(secret, request.full_url)
            body = urllib.parse.parse_qs(request.data.decode("utf-8"))
            self.assertEqual(body["cmd"], ["readCFG"])
            self.assertEqual(set(body), {"apikey", "cmd", "group", "name"})

        mismatch = EffectiveOpener({("QBITTORRENT", "qbittorrent_label"): "wrong"})
        check = validate.lazylibrarian_effective_config_check(
            "5299", secret, opener=mismatch
        )
        self.assertFalse(check.ok)
        self.assertEqual(len(mismatch.requests), expected_count)
        self.assertNotIn("wrong", repr(check))
        self.assertNotIn(secret, repr(check))

    def test_lazylibrarian_effective_config_rejects_malformed_and_oversized_responses(self):
        secret = "1" * 32

        class Response:
            status = 200

            def __init__(self, payload):
                self.payload = payload

            def getcode(self):
                return self.status

            def read(self, limit):
                self.limit = limit
                return self.payload

            def close(self):
                return None

        def opener_for(payload):
            opener = mock.MagicMock()
            opener.open.return_value = Response(payload)
            return opener

        for payload in (
            b"not-an-envelope",
            b"[False]\n[injected]",
            b"[" + b"x" * (validate.MAX_PRIVATE_RESPONSE_BYTES + 1) + b"]",
        ):
            with self.subTest(size=len(payload)):
                opener = opener_for(payload)
                check = validate.lazylibrarian_effective_config_check(
                    "5299", secret, opener=opener
                )
                self.assertFalse(check.ok)
                self.assertNotIn(secret, repr(check))
                request = opener.open.call_args.args[0]
                self.assertEqual(request.get_method(), "POST")
                self.assertFalse(urllib.parse.urlsplit(request.full_url).query)
                response = opener.open.return_value
                self.assertEqual(
                    response.limit, validate.MAX_PRIVATE_RESPONSE_BYTES + 1
                )

        with self.assertRaises(ValueError):
            validate._lazylibrarian_api_bytes(
                "5299",
                secret,
                "readCFG",
                {"group": "API", "name": "API_KEY"},
                opener=mock.MagicMock(),
            )

    def test_prowlarr_lazylibrarian_topology_and_torznab_providers_are_exact(self):
        secret = "d" * 32
        prowlarr_secret = "provider-secret-never-rendered"
        indexers = [
            {
                "id": 1,
                "name": "Books Torrent",
                "enable": True,
                "protocol": "torrent",
                "tags": [9],
                "capabilities": {"categories": [{"id": 7020}]},
            },
            {
                "id": 2,
                "name": "Generic Books Torrent",
                "enable": True,
                "protocol": "torrent",
                "tags": [],
                # Generic Books (7000) is insufficient; the safe provider set
                # requires explicit ebook category 7020 support.
                "capabilities": {"categories": [{"id": 7000}]},
            },
            {
                "id": 3,
                "name": "Expired Failure Torrent",
                "enable": True,
                "protocol": "torrent",
                "tags": [],
                "capabilities": {"categories": [{"id": 7020}]},
            },
        ]
        indexer_statuses = [
            {
                "indexerId": 3,
                "initialFailure": "2020-01-01T00:00:00Z",
                "mostRecentFailure": "2020-01-02T00:00:00Z",
                "disabledTill": "2020-01-03T00:00:00Z",
            }
        ]
        tags = [{"id": 9, "label": "lazylibrarian-ebooks"}]
        application = {
            "id": 4,
            "name": "LazyLibrarian",
            "enable": True,
            "implementation": "LazyLibrarian",
            "configContract": "LazyLibrarianSettings",
            "syncLevel": "fullSync",
            "appProfileId": None,
            "tags": [9],
            "fields": [
                {"name": "prowlarrUrl", "value": "http://prowlarr:9696"},
                {"name": "baseUrl", "value": "http://lazylibrarian:5299"},
                {"name": "apiKey", "value": "********"},
                {"name": "authUsername", "value": ""},
                {"name": "authPassword", "value": ""},
                {
                    "name": "syncCategories",
                    "value": [7020],
                },
            ],
        }
        self.assertTrue(
            validate.prowlarr_lazylibrarian_check(
                indexers, tags, [application], indexer_statuses
            ).ok
        )
        self.assertEqual(
            validate._ebook_indexer_contract(indexers, indexer_statuses),
            {"Books Torrent": 1},
        )
        wrong_indexers = json.loads(json.dumps(indexers))
        wrong_indexers[1]["tags"] = [9]
        self.assertFalse(
            validate.prowlarr_lazylibrarian_check(
                wrong_indexers, tags, [application], indexer_statuses
            ).ok
        )
        # A retained failure remains blocking even after disabledTill elapsed.
        expired_failure_tagged = json.loads(json.dumps(indexers))
        expired_failure_tagged[2]["tags"] = [9]
        self.assertFalse(
            validate.prowlarr_lazylibrarian_check(
                expired_failure_tagged,
                tags,
                [application],
                indexer_statuses,
            ).ok
        )
        wrong_application = json.loads(json.dumps(application))
        next(
            field
            for field in wrong_application["fields"]
            if field["name"] == "syncCategories"
        )["value"] = [7000, 7020]
        self.assertFalse(
            validate.prowlarr_lazylibrarian_check(
                indexers, tags, [wrong_application], indexer_statuses
            ).ok
        )
        invented_profile = json.loads(json.dumps(application))
        invented_profile["appProfileId"] = 1
        self.assertFalse(
            validate.prowlarr_lazylibrarian_check(
                indexers, tags, [invented_profile], indexer_statuses
            ).ok
        )

        providers = {
            provider_type: []
            for provider_type in (
                "newznab", "torznab", "rss", "irc", "torrent", "direct"
            )
        }
        providers["torznab"] = [
            {
                "ENABLED": "1",
                "DISPNAME": "Books Torrent (Prowlarr)",
                "HOST": "http://prowlarr:9696/1/api",
                "BOOKCAT": "7020",
                "DLTYPES": "E",
                "MANUAL": "1",
                "AUDIOCAT": "3000,3010",
                "MAGCAT": "",
                "COMICCAT": "7030",
                "API": prowlarr_secret,
            }
        ]

        class Response:
            status = 200

            def getcode(self):
                return self.status

            def read(self, _limit):
                return json.dumps(providers).encode()

            def close(self):
                return None

        opener = mock.MagicMock()
        opener.open.return_value = Response()
        check = validate.lazylibrarian_provider_check(
            "5299",
            secret,
            prowlarr_secret,
            {"Books Torrent": 1},
            opener=opener,
        )
        self.assertTrue(check.ok)
        self.assertNotIn(secret, repr(check))
        self.assertNotIn("provider-secret", repr(check))
        request = opener.open.call_args.args[0]
        self.assertEqual(
            urllib.parse.parse_qs(request.data.decode("utf-8"))["cmd"],
            ["listProviders"],
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(urllib.parse.urlsplit(request.full_url).query, "")
        self.assertNotIn(secret, request.full_url)

        provider_template = json.loads(json.dumps(providers["torznab"][0]))
        for field, bad_value in (
            ("HOST", "http://prowlarr:9696/99/api"),
            ("BOOKCAT", "7020,7030"),
            ("DLTYPES", "A,E"),
            ("MANUAL", "0"),
            ("AUDIOCAT", "3010,3000"),
            ("API", "stale-provider-key"),
        ):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(provider_template))
                changed[field] = bad_value
                providers["torznab"] = [changed]
                changed_check = validate.lazylibrarian_provider_check(
                    "5299",
                    secret,
                    prowlarr_secret,
                    {"Books Torrent": 1},
                    opener=opener,
                )
                self.assertFalse(changed_check.ok)
                if field == "API":
                    self.assertNotIn("stale-provider-key", repr(changed_check))
                    self.assertNotIn(prowlarr_secret, repr(changed_check))
        providers["torznab"] = [provider_template]

        providers["newznab"] = [
            {
                "ENABLED": "1",
                "DISPNAME": "Unexpected Usenet",
                "HOST": "http://prowlarr:9696/2/api",
                "BOOKCAT": "7020",
                "DLTYPES": "E",
                "MANUAL": "1",
            }
        ]
        check = validate.lazylibrarian_provider_check(
            "5299",
            secret,
            prowlarr_secret,
            {"Books Torrent": 1},
            opener=opener,
        )
        self.assertFalse(check.ok)

    def test_lazylibrarian_retained_expired_indexer_failure_remains_blocking(self):
        expired = {
            "indexerId": 7,
            "initialFailure": "2020-01-01T00:00:00Z",
            "mostRecentFailure": "2020-01-02T00:00:00+00:00",
            "disabledTill": "2020-01-03T00:00:00Z",
        }
        self.assertEqual(
            validate._prowlarr_failed_indexer_ids([expired]), {7}
        )
        cleared = {
            "indexerId": 7,
            "initialFailure": None,
            "mostRecentFailure": None,
            "disabledTill": None,
        }
        self.assertEqual(
            validate._prowlarr_failed_indexer_ids([cleared]), set()
        )
        malformed = dict(expired)
        malformed.pop("mostRecentFailure")
        self.assertIsNone(validate._prowlarr_failed_indexer_ids([malformed]))

    def test_ebook_qbittorrent_categories_share_only_the_managed_path(self):
        categories = {
            "ebooks": {"savePath": "/downloads/ebooks"},
            "ebooks-imported": {"savePath": "/downloads/ebooks"},
        }
        self.assertTrue(
            validate.ebook_category_ownership_check(
                categories, "lazylibrarian"
            ).ok
        )
        categories["ebooks-imported"]["savePath"] = "/downloads/other"
        self.assertFalse(
            validate.ebook_category_ownership_check(
                categories, "lazylibrarian"
            ).ok
        )

    def test_qbittorrent_container_credentials_are_exact_and_secret_free(self):
        username = "private-qbit-user"
        password = "private-qbit-password"
        stale_password = "stale-qbit-password"
        environment = {
            "QBITTORRENT_USERNAME": username,
            "QBITTORRENT_PASSWORD": password,
        }
        inspect_payloads = {
            "huey": {
                "QBITTORRENT_URL": "http://qbittorrent:8080",
                "QBITTORRENT_USERNAME": username,
                "QBITTORRENT_PASSWORD": password,
            },
            "bookbot": {
                "QBITTORRENT_URL": "http://qbittorrent:8080",
                "QBITTORRENT_USERNAME": username,
                "QBITTORRENT_PASSWORD": stale_password,
            },
            "abba": {
                "DL_HOST": "qbittorrent",
                "DL_USERNAME": username,
                "DL_PASSWORD": password,
            },
        }
        responses = []
        for service in ("huey", "bookbot", "abba"):
            responses.extend(
                [
                    mock.MagicMock(returncode=0, stdout=f"{service}-id\n"),
                    mock.MagicMock(
                        returncode=0,
                        stdout=json.dumps(
                            [
                                {
                                    "Config": {
                                        "Env": [
                                            f"{key}={value}"
                                            for key, value in inspect_payloads[
                                                service
                                            ].items()
                                        ]
                                    }
                                }
                            ]
                        ),
                    ),
                ]
            )
        runner = mock.MagicMock(side_effect=responses)
        original_compare = validate.secrets.compare_digest
        with mock.patch.object(
            validate.secrets,
            "compare_digest",
            wraps=original_compare,
        ) as compare_digest:
            checks = {
                check.name: check
                for check in validate.qbittorrent_container_credentials_checks(
                    environment,
                    ("huey", "bookbot", "abba"),
                    runner=runner,
                )
            }

        self.assertTrue(checks["huey:qbittorrent-credentials"].ok)
        self.assertFalse(checks["bookbot:qbittorrent-credentials"].ok)
        self.assertTrue(checks["abba:qbittorrent-credentials"].ok)
        self.assertEqual(compare_digest.call_count, 3)
        for secret in (username, password, stale_password):
            self.assertNotIn(secret, repr(checks))

    def test_abba_runtime_contract_is_private_hardened_and_secret_safe(self):
        environment = {
            "PUID": "1000",
            "PGID": "1000",
            "QBITTORRENT_USERNAME": "qbit-user",
            "QBITTORRENT_PASSWORD": "qbit-private-password",
            "ABBA_URL": "http://abba:5078",
            "ABBA_ABB_HOSTNAME": "audiobookbay.lu",
            "ABBA_SEARCH_CACHE_SECONDS": "300",
            "ABBA_SEARCH_MIN_INTERVAL_SECONDS": "2",
            "ABBA_RESULT_TTL_SECONDS": "86400",
            "ABBA_HTTP_TIMEOUT_SECONDS": "15",
        }
        abba_environment = {
            "DOWNLOAD_CLIENT": "qbittorrent",
            "DL_SCHEME": "http",
            "DL_HOST": "qbittorrent",
            "DL_PORT": "8080",
            "DL_USERNAME": "qbit-user",
            "DL_PASSWORD": "qbit-private-password",
            "DL_CATEGORY": "audiobooks",
            "SAVE_PATH_BASE": "/downloads/audiobooks",
            "DL_VERIFY_TLS": "true",
            "ABBA_DB_PATH": "/config/abba.db",
            "ABB_HOSTNAME": "audiobookbay.lu",
            "PAGE_LIMIT": "1",
            "PORT": "5078",
            "ABBA_SEARCH_CACHE_SECONDS": "300",
            "ABBA_SEARCH_MIN_INTERVAL_SECONDS": "2",
            "ABBA_RESULT_TTL_SECONDS": "86400",
            "ABBA_HTTP_TIMEOUT_SECONDS": "15",
            "ABBA_MAX_RESULTS": "10",
        }
        abba_inspect = {
            "Config": {
                "User": "1000:1000",
                "Env": [f"{key}={value}" for key, value in abba_environment.items()],
            },
            "HostConfig": {
                "ReadonlyRootfs": True,
                "Privileged": False,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=67108864"},
                "RestartPolicy": {"Name": "unless-stopped"},
            },
            "Mounts": [
                {
                    "Destination": "/config",
                    "Source": str((validate.STACK_ROOT / "config" / "abba").resolve()),
                    "RW": True,
                }
            ],
        }
        huey_inspect = {
            "Config": {
                "Env": [
                    "ABBA_ENABLED=true",
                    "ABBA_URL=http://abba:5078",
                    "ABBA_TIMEOUT_SECONDS=30",
                    "ABBA_SEARCH_LIMIT=10",
                    "HUEY_ABBA_MINIMUM_CONFIDENCE=0.82",
                    "HUEY_ABBA_RUNNER_UP_GAP=0.08",
                ]
            }
        }
        runner = mock.MagicMock(
            side_effect=[
                mock.MagicMock(returncode=0, stdout="abba-id\n"),
                mock.MagicMock(returncode=0, stdout=json.dumps([abba_inspect])),
                mock.MagicMock(returncode=0, stdout="huey-id\n"),
                mock.MagicMock(returncode=0, stdout=json.dumps([huey_inspect])),
            ]
        )

        checks = validate.abba_configuration_checks(environment, runner=runner)

        self.assertTrue(all(check.ok for check in checks))
        self.assertNotIn(environment["QBITTORRENT_PASSWORD"], repr(checks))

    def test_abba_must_not_publish_a_host_port_or_give_shelfarr_audio_mount(self):
        unpublished_runner = mock.MagicMock(
            side_effect=[
                mock.MagicMock(returncode=0, stdout="abba-id\n"),
                mock.MagicMock(
                    returncode=0,
                    stdout=json.dumps(
                        [{"HostConfig": {"PortBindings": {"5078/tcp": None}}}]
                    ),
                ),
            ]
        )
        self.assertTrue(
            validate.unpublished_service_check(
                "abba", runner=unpublished_runner
            ).ok
        )

        shelfarr_runner = mock.MagicMock(
            side_effect=[
                mock.MagicMock(returncode=0, stdout="shelfarr-id\n"),
                mock.MagicMock(
                    returncode=0,
                    stdout=json.dumps(
                        [{"Mounts": [{"Destination": "/ebooks", "RW": True}]}]
                    ),
                ),
            ]
        )
        self.assertTrue(
            validate.service_mount_absent_check(
                "shelfarr", "/audiobooks", runner=shelfarr_runner
            ).ok
        )

    def test_abba_database_and_readiness_probe_do_not_search_upstream(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "abba.db"
            self._create_abba_validation_database(database)
            with closing(sqlite3.connect(database)) as connection, connection:
                self._insert_abba_validation_acquisition(
                    connection,
                    "huey:1",
                    candidate_id=f"abba:{'1' * 64}",
                    info_hash="a" * 40,
                )
                self._insert_abba_validation_acquisition(
                    connection,
                    "huey:2",
                    candidate_id=f"abba:{'2' * 64}",
                    info_hash="a" * 40,
                    state="prepared",
                    canonical_correlation_id="huey:1",
                    mutation_started_at=None,
                )
                self._insert_abba_validation_acquisition(
                    connection,
                    "huey:3",
                    candidate_id=f"abba:{'1' * 64}",
                    info_hash="a" * 40,
                    state="prepared",
                    canonical_correlation_id="huey:1",
                    canonical_candidate_correlation_id="huey:1",
                    mutation_started_at=None,
                )
                self._insert_abba_validation_acquisition(
                    connection,
                    "huey:4",
                    candidate_id=f"abba:{'2' * 64}",
                    info_hash="b" * 40,
                    state="failed",
                    canonical_candidate_correlation_id="huey:2",
                    mutation_started_at=2.0,
                    error_code="result_changed",
                    error_retryable=0,
                    error_http_status=409,
                )
                self._insert_abba_validation_acquisition(
                    connection,
                    "huey:5",
                    candidate_id=f"abba:{'2' * 64}",
                    info_hash="a" * 40,
                    state="prepared",
                    canonical_correlation_id="huey:1",
                    canonical_candidate_correlation_id="huey:2",
                    mutation_started_at=None,
                )
            check = validate.abba_database_check(database)
            self.assertTrue(check.ok)
            self.assertEqual(
                check.detail,
                "integrity, canonical hash/candidate schema, and alias ownership "
                "valid; "
                "violations=0",
            )
            self.assertFalse(
                validate.abba_database_check(Path(directory) / "missing.db").ok
            )

            legacy_database = Path(directory) / "legacy.db"
            with closing(sqlite3.connect(legacy_database)) as connection, connection:
                connection.execute("CREATE TABLE requests (hash TEXT)")
            self.assertFalse(validate.abba_database_check(legacy_database).ok)

        runner = mock.MagicMock(
            return_value=mock.MagicMock(returncode=0, stdout="ready\n")
        )
        self.assertTrue(validate.abba_api_readiness_check(runner=runner).ok)
        probe = runner.call_args.args[0][-1]
        self.assertIn("http://abba:5078/health", probe)
        self.assertIn("payload.get('service')=='abba'", probe)
        for key in ("database", "qbittorrent", "category", "save_path"):
            self.assertIn(repr(key), probe)
        self.assertNotIn("/api/search", probe)
        self.assertNotIn("audiobookbay", probe.casefold())

    def test_abba_database_rejects_hash_and_candidate_owner_corruption(self):
        corruptions = {
            "duplicate canonical hash owners": """
                DROP INDEX acquisitions_hash_owner_uq;
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, mutation_started_at,
                    created_at, updated_at
                ) VALUES
                    ('huey:1', 'abba:1111111111111111111111111111111111111111111111111111111111111111',
                     'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'one',
                     'audiobooks', '/downloads/audiobooks', 'huey-1',
                     'queued', 1.0, 1.0, 1.0),
                    ('huey:2', 'abba:2222222222222222222222222222222222222222222222222222222222222222',
                     'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'two',
                     'audiobooks', '/downloads/audiobooks', 'huey-2',
                     'queued', 1.0, 1.0, 1.0);
            """,
            "missing canonical correlation": """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, canonical_correlation_id,
                    created_at, updated_at
                ) VALUES (
                    'huey:2', 'abba:2222222222222222222222222222222222222222222222222222222222222222',
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'two',
                    'audiobooks', '/downloads/audiobooks', 'huey-2',
                    'prepared', 'huey:99', 1.0, 1.0
                );
            """,
            "different canonical hash": """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, mutation_started_at,
                    created_at, updated_at
                ) VALUES (
                    'huey:1', 'abba:1111111111111111111111111111111111111111111111111111111111111111',
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'one',
                    'audiobooks', '/downloads/audiobooks', 'huey-1',
                    'queued', 1.0, 1.0, 1.0
                );
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, canonical_correlation_id,
                    created_at, updated_at
                ) VALUES (
                    'huey:2', 'abba:2222222222222222222222222222222222222222222222222222222222222222',
                    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'two',
                    'audiobooks', '/downloads/audiobooks', 'huey-2',
                    'prepared', 'huey:1', 1.0, 1.0
                );
            """,
            "cyclic canonical correlations": """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, canonical_correlation_id,
                    created_at, updated_at
                ) VALUES
                    ('huey:1', 'abba:1111111111111111111111111111111111111111111111111111111111111111',
                     'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'one',
                     'audiobooks', '/downloads/audiobooks', 'huey-1',
                     'prepared', 'huey:2', 1.0, 1.0),
                    ('huey:2', 'abba:2222222222222222222222222222222222222222222222222222222222222222',
                     'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'two',
                     'audiobooks', '/downloads/audiobooks', 'huey-2',
                     'prepared', 'huey:1', 1.0, 1.0);
            """,
            "weakened hash-owner predicate": """
                DROP INDEX acquisitions_hash_owner_uq;
                CREATE UNIQUE INDEX acquisitions_hash_owner_uq
                    ON acquisitions(info_hash)
                    WHERE info_hash IS NOT NULL
                      AND canonical_correlation_id IS NULL;
            """,
            "missing hash-owner index": """
                DROP INDEX acquisitions_hash_owner_uq;
            """,
            "missing hash-canonical index": """
                DROP INDEX acquisitions_canonical_idx;
            """,
            "missing candidate-owner index": """
                DROP INDEX acquisitions_candidate_owner_uq;
            """,
            "missing candidate-canonical index": """
                DROP INDEX acquisitions_candidate_canonical_idx;
            """,
            "weakened candidate-owner predicate": """
                DROP INDEX acquisitions_candidate_owner_uq;
                CREATE UNIQUE INDEX acquisitions_candidate_owner_uq
                    ON acquisitions(candidate_id)
                    WHERE canonical_candidate_correlation_id IS NULL
                      AND canonical_correlation_id IS NULL
                      AND (
                          state != 'failed'
                          OR mutation_started_at IS NOT NULL
                      );
            """,
            "duplicate canonical candidate owners": """
                DROP INDEX acquisitions_candidate_owner_uq;
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, mutation_started_at,
                    created_at, updated_at
                ) VALUES
                    ('huey:1', 'abba:1111111111111111111111111111111111111111111111111111111111111111',
                     'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'one',
                     'audiobooks', '/downloads/audiobooks', 'huey-1',
                     'queued', 1.0, 1.0, 1.0),
                    ('huey:2', 'abba:1111111111111111111111111111111111111111111111111111111111111111',
                     'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'two',
                     'audiobooks', '/downloads/audiobooks', 'huey-2',
                     'queued', 1.0, 1.0, 1.0);
            """,
            "missing canonical candidate correlation": """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, error_code, error_retryable,
                    error_http_status, canonical_candidate_correlation_id,
                    created_at, updated_at
                ) VALUES (
                    'huey:2', 'abba:2222222222222222222222222222222222222222222222222222222222222222',
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'two',
                    'audiobooks', '/downloads/audiobooks', 'huey-2',
                    'failed', 'result_changed', 0, 409, 'huey:99', 1.0, 1.0
                );
            """,
            "candidate target has different candidate": """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, mutation_started_at,
                    created_at, updated_at
                ) VALUES (
                    'huey:1', 'abba:1111111111111111111111111111111111111111111111111111111111111111',
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'one',
                    'audiobooks', '/downloads/audiobooks', 'huey-1',
                    'queued', 1.0, 1.0, 1.0
                );
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, canonical_correlation_id,
                    canonical_candidate_correlation_id,
                    created_at, updated_at
                ) VALUES (
                    'huey:2', 'abba:2222222222222222222222222222222222222222222222222222222222222222',
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'two',
                    'audiobooks', '/downloads/audiobooks', 'huey-2',
                    'prepared', 'huey:1', 'huey:1', 1.0, 1.0
                );
            """,
            "candidate link is not direct": """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, mutation_started_at,
                    created_at, updated_at
                ) VALUES (
                    'huey:1', 'abba:1111111111111111111111111111111111111111111111111111111111111111',
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'one',
                    'audiobooks', '/downloads/audiobooks', 'huey-1',
                    'queued', 1.0, 1.0, 1.0
                );
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, canonical_correlation_id,
                    canonical_candidate_correlation_id,
                    created_at, updated_at
                ) VALUES
                    ('huey:2', 'abba:1111111111111111111111111111111111111111111111111111111111111111',
                     'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'two',
                     'audiobooks', '/downloads/audiobooks', 'huey-2',
                     'prepared', 'huey:1', 'huey:1', 1.0, 1.0),
                    ('huey:3', 'abba:1111111111111111111111111111111111111111111111111111111111111111',
                     'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'three',
                     'audiobooks', '/downloads/audiobooks', 'huey-3',
                     'prepared', 'huey:1', 'huey:2', 1.0, 1.0);
            """,
            "same candidate and hash lacks hash link": """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, mutation_started_at,
                    created_at, updated_at
                ) VALUES (
                    'huey:1', 'abba:1111111111111111111111111111111111111111111111111111111111111111',
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'one',
                    'audiobooks', '/downloads/audiobooks', 'huey-1',
                    'queued', 1.0, 1.0, 1.0
                );
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state,
                    canonical_candidate_correlation_id,
                    created_at, updated_at
                ) VALUES (
                    'huey:2', 'abba:1111111111111111111111111111111111111111111111111111111111111111',
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'two',
                    'audiobooks', '/downloads/audiobooks', 'huey-2',
                    'failed', 'huey:1', 1.0, 1.0
                );
            """,
            "prepared hash root has no mutation marker": """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, created_at, updated_at
                ) VALUES (
                    'huey:1', 'abba:1111111111111111111111111111111111111111111111111111111111111111',
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'one',
                    'audiobooks', '/downloads/audiobooks', 'huey-1',
                    'prepared', 1.0, 1.0
                );
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, canonical_correlation_id,
                    created_at, updated_at
                ) VALUES (
                    'huey:2', 'abba:2222222222222222222222222222222222222222222222222222222222222222',
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'two',
                    'audiobooks', '/downloads/audiobooks', 'huey-2',
                    'prepared', 'huey:1', 1.0, 1.0
                );
            """,
            "prepared candidate root has no mutation marker": """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, created_at, updated_at
                ) VALUES (
                    'huey:1', 'abba:1111111111111111111111111111111111111111111111111111111111111111',
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'one',
                    'audiobooks', '/downloads/audiobooks', 'huey-1',
                    'prepared', 1.0, 1.0
                );
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, error_code, error_retryable,
                    error_http_status, canonical_candidate_correlation_id,
                    created_at, updated_at
                ) VALUES (
                    'huey:2', 'abba:1111111111111111111111111111111111111111111111111111111111111111',
                    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'two',
                    'audiobooks', '/downloads/audiobooks', 'huey-2',
                    'failed', 'result_changed', 0, 409, 'huey:1', 1.0, 1.0
                );
            """,
        }
        for name, corruption in corruptions.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "abba.db"
                self._create_abba_validation_database(database)
                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.executescript(corruption)

                check = validate.abba_database_check(database)

                self.assertFalse(check.ok)
                self.assertRegex(check.detail, r"violations=\d+$")
                self.assertNotIn("huey:", check.detail)
                self.assertNotIn("abba:", check.detail)

    def test_abba_database_requires_exact_candidate_conflict_quarantine(self):
        corruptions = {
            "nonterminal state": "state = 'queued'",
            "wrong error": "error_code = 'request_conflict'",
            "retryable": "error_retryable = 1",
            "wrong status": "error_http_status = 500",
        }
        for name, assignment in corruptions.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "abba.db"
                self._create_abba_validation_database(database)
                with closing(sqlite3.connect(database)) as connection, connection:
                    self._insert_abba_validation_acquisition(
                        connection,
                        "huey:1",
                        candidate_id=f"abba:{'1' * 64}",
                        info_hash="a" * 40,
                    )
                    self._insert_abba_validation_acquisition(
                        connection,
                        "huey:2",
                        candidate_id=f"abba:{'1' * 64}",
                        info_hash="b" * 40,
                        state="failed",
                        canonical_candidate_correlation_id="huey:1",
                        mutation_started_at=2.0,
                        error_code="result_changed",
                        error_retryable=0,
                        error_http_status=409,
                    )
                    connection.execute(
                        f"UPDATE acquisitions SET {assignment} "
                        "WHERE correlation_id = 'huey:2'"
                    )

                check = validate.abba_database_check(database)

                self.assertFalse(check.ok)
                self.assertRegex(check.detail, r"violations=\d+$")
                self.assertNotIn("huey:", check.detail)
                self.assertNotIn("abba:", check.detail)

    def test_writable_check_uses_and_removes_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            check = validate.writable_check(Path(directory), "test")
            self.assertTrue(check.ok)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_private_service_storage_rejects_group_access(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sabnzbd"
            path.mkdir(mode=0o700)
            self.assertTrue(
                validate.private_service_storage_check(path, "sabnzbd:test").ok
            )
            path.chmod(0o750)
            self.assertFalse(
                validate.private_service_storage_check(path, "sabnzbd:test").ok
            )

    def test_evaluation_reports_must_be_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evaluation"
            path.mkdir(mode=0o700)
            report = path / "results.json"
            report.write_text("{}\n", encoding="utf-8")
            report.chmod(0o600)
            self.assertTrue(validate.evaluation_report_permissions_check(path).ok)
            report.chmod(0o640)
            self.assertFalse(validate.evaluation_report_permissions_check(path).ok)

    def test_channel_inventory_requires_unique_positive_request_and_lifecycle_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channels.yml"
            path.write_text(VALID_CHANNEL_INVENTORY, encoding="utf-8")
            inventory = validate.load_channel_inventory(path)
            self.assertEqual(inventory["activity"]["download-queue"], "7")
            self.assertTrue(validate.channel_inventory_check(path).ok)

            invalid_documents = (
                VALID_CHANNEL_INVENTORY.replace("  system-health: 11\n", ""),
                VALID_CHANNEL_INVENTORY.replace(
                    "  system-health: 11", "  system-health: invalid"
                ),
                VALID_CHANNEL_INVENTORY.replace(
                    "  system-health: 11", "  system-health: 10"
                ),
            )
            for document in invalid_documents:
                with self.subTest(document=document):
                    path.write_text(document, encoding="utf-8")
                    self.assertFalse(validate.channel_inventory_check(path).ok)

    def test_huey_ready_marker_must_exist_and_contain_only_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "ready"
            self.assertFalse(validate.huey_ready_check(marker).ok)
            marker.write_text("starting\n", encoding="utf-8")
            self.assertFalse(validate.huey_ready_check(marker).ok)
            marker.write_text("ready\n", encoding="utf-8")
            self.assertTrue(validate.huey_ready_check(marker).ok)

    def test_huey_selection_ttl_is_literal_and_bounded(self):
        self.assertTrue(validate.huey_selection_ttl_check({}).ok)
        for value in ("1", "900", "86400"):
            with self.subTest(value=value):
                self.assertTrue(
                    validate.huey_selection_ttl_check(
                        {"HUEY_SELECTION_TTL_SECONDS": value}
                    ).ok
                )
        for value in ("", "0", "86401", " 900 ", "15m", "1.0", "+900"):
            with self.subTest(value=value):
                self.assertFalse(
                    validate.huey_selection_ttl_check(
                        {"HUEY_SELECTION_TTL_SECONDS": value}
                    ).ok
                )

    def test_huey_database_requires_confirmation_ebook_and_retry_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "huey.db"
            self._create_huey_validation_database(database)

            self.assertTrue(validate.huey_database_check(database).ok)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("DROP INDEX ebook_backend_attempts_state_idx")
            self.assertFalse(validate.huey_database_check(database).ok)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "CREATE INDEX ebook_backend_attempts_state_idx "
                    "ON ebook_backend_attempts(status, request_id, ordinal)"
                )
            self.assertTrue(validate.huey_database_check(database).ok)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("DROP INDEX candidate_confirmations_expiry_idx")
            self.assertFalse(validate.huey_database_check(database).ok)

            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "CREATE INDEX candidate_confirmations_expiry_idx "
                    "ON candidate_confirmations(status, expires_at, id)"
                )
                connection.execute("DROP TRIGGER ebook_request_terminal_sync")
            self.assertFalse(validate.huey_database_check(database).ok)

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "huey.db"
            self._create_huey_validation_database(database)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "ALTER TABLE unavailable_retries "
                    "DROP COLUMN last_proof_check_at"
                )
            self.assertFalse(validate.huey_database_check(database).ok)

    def test_huey_database_requires_retry_indexes_and_terminal_triggers(self):
        objects = (
            ("INDEX", "requests_active_ll_hash_uq"),
            ("INDEX", "unavailable_retries_due_idx"),
            ("INDEX", "unavailable_retries_active_identity_uq"),
            ("TRIGGER", "unavailable_retry_import_failure_sync"),
            ("TRIGGER", "unavailable_retry_blocked_completion_guard"),
            ("TRIGGER", "unavailable_retry_terminal_sync"),
        )
        for object_type, name in objects:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "huey.db"
                self._create_huey_validation_database(database)
                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.execute(f"DROP {object_type} {name}")
                self.assertFalse(validate.huey_database_check(database).ok)

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "huey.db"
            self._create_huey_validation_database(database)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("DROP INDEX requests_active_ll_hash_uq")
                connection.execute(
                    """
                    CREATE UNIQUE INDEX requests_active_ll_hash_uq
                    ON requests(lower(external_id))
                    WHERE service = 'lazylibrarian'
                      AND external_id IS NOT NULL
                      AND status IN ('processing', 'queued')
                    """
                )
            self.assertFalse(validate.huey_database_check(database).ok)

        invalid_predicates = (
            "state IN ('queued', 'retrying', 'awaiting_import')",
            "state IN ('queued', 'retrying', 'awaiting_import', 'blocked', "
            "'fulfilled')",
        )
        for predicate in invalid_predicates:
            with self.subTest(predicate=predicate), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "huey.db"
                self._create_huey_validation_database(database)
                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.execute(
                        "DROP INDEX unavailable_retries_active_identity_uq"
                    )
                    connection.execute(
                        "CREATE UNIQUE INDEX unavailable_retries_active_identity_uq "
                        "ON unavailable_retries(identity_key) WHERE " + predicate
                    )
                self.assertFalse(validate.huey_database_check(database).ok)

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "huey.db"
            self._create_huey_validation_database(database)
            with closing(sqlite3.connect(database)) as connection, connection:
                trigger_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                    "AND name = 'unavailable_retry_terminal_sync'"
                ).fetchone()[0]
                connection.execute("DROP TRIGGER unavailable_retry_terminal_sync")
                connection.execute(
                    str(trigger_sql).replace(
                        "state IN ('retrying', 'awaiting_import', 'blocked')",
                        "state != 'fulfilled'",
                    )
                )
            self.assertFalse(validate.huey_database_check(database).ok)

    def test_huey_database_accepts_consistent_unavailable_retry_states(self):
        for state in (
            "queued",
            "retrying",
            "awaiting_import",
            "blocked",
            "fulfilled",
            "expired",
        ):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "huey.db"
                self._create_huey_validation_database(database)
                with closing(sqlite3.connect(database)) as connection, connection:
                    self._insert_huey_validation_retry(connection, 1, state=state)
                self.assertTrue(validate.huey_database_check(database).ok)

    def test_huey_database_accepts_owned_uncertain_and_blocked_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "huey.db"
            self._create_huey_validation_database(database)
            with closing(sqlite3.connect(database)) as connection, connection:
                self._insert_huey_validation_retry(
                    connection, 1, state="awaiting_import"
                )
                connection.execute(
                    """
                    UPDATE ebook_cascades
                    SET state = 'uncertain',
                        mutation_backend = 'lazylibrarian',
                        mutation_started_at = '2026-01-08 00:00:00',
                        final_backend = NULL, finalizer = NULL
                    WHERE request_id = 1
                    """
                )
                connection.execute(
                    """
                    UPDATE ebook_backend_attempts
                    SET status = 'uncertain',
                        mutation_started_at = '2026-01-08 00:00:00',
                        external_id = NULL,
                        external_status = 'submission_uncertain'
                    WHERE request_id = 1 AND ordinal = 0
                    """
                )

            self.assertTrue(validate.huey_database_check(database).ok)

            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    """
                    UPDATE ebook_backend_attempts
                    SET status = 'failed', finished_at = CURRENT_TIMESTAMP,
                        mutation_resolved_at = CURRENT_TIMESTAMP
                    WHERE request_id = 1 AND ordinal = 0
                    """
                )
                connection.execute(
                    "UPDATE ebook_cascades SET state = 'failed' WHERE request_id = 1"
                )
                connection.execute(
                    "UPDATE requests SET status = 'failed' WHERE id = 1"
                )

            self.assertTrue(validate.huey_database_check(database).ok)

    def test_huey_database_rejects_unavailable_retry_invariant_corruption(self):
        def mismatched_identity(connection: sqlite3.Connection) -> None:
            self._insert_huey_validation_retry(connection, 1)
            connection.execute(
                "UPDATE unavailable_retries SET identity_key = ? WHERE request_id = 1",
                ("c" * 64,),
            )

        def duplicate_owner(connection: sqlite3.Connection) -> None:
            connection.execute("DROP INDEX unavailable_retries_active_identity_uq")
            self._insert_huey_validation_retry(connection, 1)
            self._insert_huey_validation_retry(connection, 2)

        def inconsistent_state(connection: sqlite3.Connection) -> None:
            self._insert_huey_validation_retry(connection, 1)
            connection.execute("UPDATE requests SET status = 'processing' WHERE id = 1")

        def unowned_awaiting_import(connection: sqlite3.Connection) -> None:
            self._insert_huey_validation_retry(
                connection, 1, state="awaiting_import"
            )
            connection.execute(
                """
                UPDATE ebook_cascades
                SET state = 'uncertain', final_backend = NULL, finalizer = NULL
                WHERE request_id = 1
                """
            )

        def unowned_blocked(connection: sqlite3.Connection) -> None:
            self._insert_huey_validation_retry(connection, 1, state="blocked")
            connection.execute(
                """
                UPDATE ebook_cascades
                SET final_backend = NULL, finalizer = NULL
                WHERE request_id = 1
                """
            )

        def released_blocked_reservation(connection: sqlite3.Connection) -> None:
            self._insert_huey_validation_retry(connection, 1, state="blocked")
            connection.execute(
                "DELETE FROM ebook_backend_reservations WHERE request_id = 1"
            )

        def released_retry_reservation(
            connection: sqlite3.Connection, state: str
        ) -> None:
            self._insert_huey_validation_retry(connection, 1, state=state)
            connection.execute(
                "DELETE FROM ebook_backend_reservations WHERE request_id = 1"
            )

        def retained_expired_reservation(connection: sqlite3.Connection) -> None:
            self._insert_huey_validation_retry(connection, 1, state="expired")
            connection.execute(
                """
                INSERT INTO ebook_backend_reservations(
                    backend, backend_identity, request_id
                ) VALUES ('lazylibrarian', 'OL1W', 1)
                """
            )

        def retry_lifecycle_delivery(connection: sqlite3.Connection) -> None:
            self._insert_huey_validation_retry(connection, 1, state="retrying")
            connection.execute(
                """
                INSERT INTO notification_deliveries(
                    request_id, event_key, route, message
                ) VALUES (1, 'download_queued', 'download-queue', 'must be silent')
                """
            )

        def proof_cursor_on_reacquirable_state(
            connection: sqlite3.Connection,
        ) -> None:
            self._insert_huey_validation_retry(connection, 1, state="queued")
            connection.execute(
                "UPDATE unavailable_retries "
                "SET last_proof_check_at = '2026-01-08 01:30:00.000001' "
                "WHERE request_id = 1"
            )

        def premature_completion_delivery(connection: sqlite3.Connection) -> None:
            self._insert_huey_validation_retry(connection, 1)
            connection.execute(
                """
                INSERT INTO notification_deliveries(
                    request_id, event_key, route, message
                ) VALUES (1, 'request_completed', 'request-status', 'too early')
                """
            )

        def sensitive_metadata(connection: sqlite3.Connection) -> None:
            self._insert_huey_validation_retry(connection, 1)
            raw = connection.execute(
                "SELECT metadata_json FROM unavailable_retries WHERE request_id = 1"
            ).fetchone()[0]
            changed = str(raw).replace("Example Book", "https://private.invalid")
            connection.execute(
                "UPDATE unavailable_retries SET metadata_json = ?, "
                "canonical_title = 'https://private.invalid' WHERE request_id = 1",
                (changed,),
            )
            connection.execute(
                "UPDATE ebook_cascades SET identity_json = ? WHERE request_id = 1",
                (changed,),
            )

        corruptions = {
            "mismatched identity": mismatched_identity,
            "duplicate active owner": duplicate_owner,
            "inconsistent lifecycle state": inconsistent_state,
            "unowned awaiting import": unowned_awaiting_import,
            "unowned blocked retry": unowned_blocked,
            "released blocked reservation": released_blocked_reservation,
            "released queued reservation": lambda connection: (
                released_retry_reservation(connection, "queued")
            ),
            "released retrying reservation": lambda connection: (
                released_retry_reservation(connection, "retrying")
            ),
            "released awaiting-import reservation": lambda connection: (
                released_retry_reservation(connection, "awaiting_import")
            ),
            "released fulfilled reservation": lambda connection: (
                released_retry_reservation(connection, "fulfilled")
            ),
            "expired retry retained reservation": retained_expired_reservation,
            "retry lifecycle delivery": retry_lifecycle_delivery,
            "proof cursor on reacquirable state": proof_cursor_on_reacquirable_state,
            "premature completion delivery": premature_completion_delivery,
            "sensitive metadata": sensitive_metadata,
        }
        for name, corrupt in corruptions.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "huey.db"
                self._create_huey_validation_database(database)
                with closing(sqlite3.connect(database)) as connection, connection:
                    corrupt(connection)

                check = validate.huey_database_check(database)

                self.assertFalse(check.ok)
                self.assertRegex(check.detail, r"violations=\d+$")
                self.assertNotIn("private.invalid", check.detail)

    def test_huey_database_accepts_inert_abba_canonical_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "huey.db"
            self._create_huey_validation_database(database)
            with closing(sqlite3.connect(database)) as connection, connection:
                self._insert_huey_validation_request(
                    connection,
                    1,
                    status="queued",
                    candidate_id=f"abba:{'1' * 64}",
                    info_hash="a" * 40,
                )
                self._insert_huey_validation_request(
                    connection,
                    2,
                    status="failed",
                    candidate_id=f"abba:{'2' * 64}",
                    info_hash="a" * 40,
                    canonical_request_id=1,
                )
                connection.execute(
                    """
                    INSERT INTO notification_deliveries(
                        request_id, event_key, route, message, delivered_at
                    ) VALUES (2, 'request_failed', 'request-status',
                              'delivered', CURRENT_TIMESTAMP)
                    """
                )

            check = validate.huey_database_check(database)

            self.assertTrue(check.ok)
            self.assertIn("violations=0", check.detail)

    def test_huey_database_rejects_abba_canonical_ownership_corruption(self):
        candidate_one = f"abba:{'1' * 64}"
        candidate_two = f"abba:{'2' * 64}"
        hash_one = "a" * 40
        hash_two = "b" * 40

        def duplicate_candidate(connection: sqlite3.Connection) -> None:
            connection.execute("DROP INDEX requests_active_abba_candidate_uq")
            self._insert_huey_validation_request(
                connection,
                1,
                status="queued",
                candidate_id=candidate_one,
                info_hash=hash_one,
            )
            self._insert_huey_validation_request(
                connection,
                2,
                status="queued",
                candidate_id=candidate_one,
                info_hash=hash_two,
            )

        def duplicate_hash(connection: sqlite3.Connection) -> None:
            connection.execute("DROP INDEX requests_active_abba_hash_uq")
            self._insert_huey_validation_request(
                connection,
                1,
                status="queued",
                candidate_id=candidate_one,
                info_hash=hash_one,
            )
            self._insert_huey_validation_request(
                connection,
                2,
                status="queued",
                candidate_id=candidate_two,
                info_hash=hash_one,
            )

        def missing_alias(connection: sqlite3.Connection) -> None:
            self._insert_huey_validation_request(
                connection,
                1,
                status="failed",
                candidate_id=candidate_one,
                info_hash=hash_one,
                canonical_request_id=99,
            )

        def self_alias(connection: sqlite3.Connection) -> None:
            self._insert_huey_validation_request(
                connection,
                1,
                status="failed",
                candidate_id=candidate_one,
                info_hash=hash_one,
                canonical_request_id=1,
            )

        def cyclic_alias(connection: sqlite3.Connection) -> None:
            self._insert_huey_validation_request(
                connection,
                1,
                status="failed",
                candidate_id=candidate_one,
                info_hash=hash_one,
                canonical_request_id=2,
            )
            self._insert_huey_validation_request(
                connection,
                2,
                status="failed",
                candidate_id=candidate_two,
                info_hash=hash_one,
                canonical_request_id=1,
            )

        def pending_alias_delivery(connection: sqlite3.Connection) -> None:
            self._insert_huey_validation_request(
                connection,
                1,
                status="queued",
                candidate_id=candidate_one,
                info_hash=hash_one,
            )
            self._insert_huey_validation_request(
                connection,
                2,
                status="failed",
                candidate_id=candidate_two,
                info_hash=hash_one,
                canonical_request_id=1,
            )
            connection.execute(
                """
                INSERT INTO notification_deliveries(
                    request_id, event_key, route, message
                ) VALUES (2, 'request_failed', 'request-status', 'pending')
                """
            )

        corruptions = {
            "duplicate candidate": duplicate_candidate,
            "duplicate hash": duplicate_hash,
            "missing alias": missing_alias,
            "self alias": self_alias,
            "cyclic alias": cyclic_alias,
            "pending alias delivery": pending_alias_delivery,
        }
        for name, corrupt in corruptions.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "huey.db"
                self._create_huey_validation_database(database)
                with closing(sqlite3.connect(database)) as connection, connection:
                    corrupt(connection)

                check = validate.huey_database_check(database)

                self.assertFalse(check.ok)
                self.assertRegex(check.detail, r"violations=\d+$")
                self.assertNotIn(candidate_one, check.detail)
                self.assertNotIn(candidate_two, check.detail)
                self.assertNotIn(hash_one, check.detail)
                self.assertNotIn(hash_two, check.detail)

    def test_shelfarr_storage_requires_all_databases_private_keys_and_no_discord(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            os.chmod(storage, 0o700)
            for filename in (
                "production.sqlite3",
                "production_cache.sqlite3",
                "production_queue.sqlite3",
                "production_cable.sqlite3",
            ):
                with closing(sqlite3.connect(storage / filename)) as connection, connection:
                    connection.execute("CREATE TABLE example (value TEXT)")
            with closing(
                sqlite3.connect(storage / "production.sqlite3")
            ) as connection, connection:
                connection.execute(
                    "CREATE TABLE settings (key TEXT UNIQUE, value TEXT)"
                )
                connection.execute(
                    "INSERT INTO settings VALUES ('discord_enabled', 'false')"
                )
            for filename in (".secret_key_base", ".encryption_keys"):
                path = storage / filename
                path.write_text("private\n", encoding="utf-8")
                os.chmod(path, 0o600)

            checks = {
                check.name: check for check in validate.shelfarr_storage_checks(storage)
            }
            self.assertTrue(all(check.ok for check in checks.values()))

            with closing(
                sqlite3.connect(storage / "production.sqlite3")
            ) as connection, connection:
                connection.execute(
                    "UPDATE settings SET value = 'true' WHERE key = 'discord_enabled'"
                )
            checks = {
                check.name: check for check in validate.shelfarr_storage_checks(storage)
            }
            self.assertFalse(checks["shelfarr:native-discord"].ok)

            os.chmod(storage, 0o755)
            checks = {
                check.name: check for check in validate.shelfarr_storage_checks(storage)
            }
            self.assertFalse(checks["shelfarr:storage-permissions"].ok)

    def test_shelfarr_evaluation_configuration_enforces_isolated_clients_and_token(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            token = "shf_test_token"
            prowlarr_key = "private-prowlarr-key"
            with closing(
                sqlite3.connect(storage / "production.sqlite3")
            ) as connection, connection:
                connection.execute("CREATE TABLE settings (key TEXT, value TEXT)")
                connection.executemany(
                    "INSERT INTO settings VALUES (?, ?)",
                    (
                        ("indexer_provider", "prowlarr"),
                        ("prowlarr_url", "http://prowlarr:9696"),
                        ("prowlarr_api_key", prowlarr_key),
                        ("preferred_download_types", '["direct","usenet","torrent"]'),
                        ("prowlarr_tags", ""),
                        ("ebook_output_path", "/ebooks"),
                        # Legacy Shelfarr state is intentionally inert: the
                        # container no longer has an audiobook library mount.
                        ("audiobook_output_path", "/retired-and-unmounted"),
                        ("download_local_path", "/downloads"),
                        ("immediate_search_enabled", "true"),
                        ("auto_approve_requests", "true"),
                        ("auto_select_enabled", "true"),
                        ("auto_select_confidence_threshold", "90"),
                        ("auto_select_min_seeders", "1"),
                        ("completed_download_import_mode", "copy"),
                        ("default_language", "en"),
                        ("auth_disabled", "false"),
                        ("librivox_enabled", "false"),
                        ("gutenberg_enabled", "true"),
                        ("anna_archive_enabled", "false"),
                        ("zlibrary_enabled", "false"),
                        ("ebooks_com_enabled", "false"),
                        ("discord_enabled", "false"),
                        ("discord_webhook_url", ""),
                        ("webhook_enabled", "false"),
                        ("webhook_url", ""),
                        ("telegram_enabled", "false"),
                    ),
                )
                connection.execute(
                    "CREATE TABLE download_clients "
                    "(client_type TEXT, url TEXT, category TEXT, download_path TEXT, enabled INTEGER, "
                    "password TEXT, api_key TEXT)"
                )
                connection.executemany(
                    "INSERT INTO download_clients VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        (
                            "qbittorrent",
                            "http://qbittorrent:8080",
                            "shelfarr",
                            "/downloads/shelfarr",
                            1,
                            "encrypted",
                            None,
                        ),
                        (
                            "sabnzbd",
                            "http://sabnzbd:8080",
                            "shelfarr",
                            "/downloads/usenet",
                            1,
                            None,
                            "encrypted",
                        ),
                    ),
                )
                connection.execute(
                    "CREATE TABLE api_tokens "
                    "(token_digest TEXT, scopes TEXT, revoked_at TEXT, expires_at TEXT, user_id INTEGER)"
                )
                connection.execute(
                    "CREATE TABLE users (id INTEGER, role INTEGER, deleted_at TEXT)"
                )
                connection.execute("INSERT INTO users VALUES (1, 0, NULL)")
                connection.execute(
                    "INSERT INTO api_tokens VALUES (?, ?, NULL, NULL, 1)",
                    (
                        hashlib.sha256(token.encode()).hexdigest(),
                        '["search:read","requests:read","requests:write"]',
                    ),
                )

            checks = validate.shelfarr_configuration_checks(
                storage,
                {
                    "SHELFARR_API_TOKEN": token,
                    "PROWLARR_API_KEY": prowlarr_key,
                    "WYSEARR_USENET_ENABLED": "true",
                },
            )
            self.assertTrue(all(check.ok for check in checks))

            mismatched_key_checks = {
                check.name: check
                for check in validate.shelfarr_configuration_checks(
                    storage,
                    {
                        "SHELFARR_API_TOKEN": token,
                        "PROWLARR_API_KEY": "rotated-but-not-propagated",
                        "WYSEARR_USENET_ENABLED": "true",
                    },
                )
            }
            self.assertFalse(mismatched_key_checks["shelfarr:prowlarr"].ok)
            self.assertNotIn(prowlarr_key, repr(mismatched_key_checks))
            self.assertNotIn(
                "rotated-but-not-propagated", repr(mismatched_key_checks)
            )

            with closing(
                sqlite3.connect(storage / "production.sqlite3")
            ) as connection, connection:
                connection.execute(
                    "UPDATE settings SET value = ? "
                    "WHERE key = 'preferred_download_types'",
                    ('["direct","torrent"]',),
                )
                connection.execute(
                    "UPDATE download_clients SET enabled = 0 "
                    "WHERE client_type = 'sabnzbd'"
                )
            disabled_checks = validate.shelfarr_configuration_checks(
                storage,
                {
                    "SHELFARR_API_TOKEN": token,
                    "PROWLARR_API_KEY": prowlarr_key,
                    "WYSEARR_USENET_ENABLED": "false",
                },
            )
            self.assertTrue(all(check.ok for check in disabled_checks))

            with closing(
                sqlite3.connect(storage / "production.sqlite3")
            ) as connection, connection:
                connection.execute(
                    "UPDATE download_clients SET category = 'ebooks' "
                    "WHERE client_type = 'qbittorrent'"
                )
            checks = {
                check.name: check
                for check in validate.shelfarr_configuration_checks(
                    storage,
                    {
                        "SHELFARR_API_TOKEN": token,
                        "PROWLARR_API_KEY": prowlarr_key,
                        "WYSEARR_USENET_ENABLED": "false",
                    },
                )
            }
            self.assertFalse(checks["shelfarr:download-clients"].ok)

    def test_shelfarr_direct_staging_requires_owner_only_local_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            staging.mkdir(mode=0o700)
            self.assertTrue(validate.shelfarr_direct_staging_check(staging).ok)
            os.chmod(staging, 0o775)
            self.assertFalse(validate.shelfarr_direct_staging_check(staging).ok)

    def test_sabnzbd_evaluation_requires_isolated_paths_and_category(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "sabnzbd.ini"
            config.write_text(
                "[misc]\n"
                "download_dir = /downloads/incomplete/usenet\n"
                "complete_dir = /downloads/usenet\n"
                "api_key = private\n"
                "username = operator\n"
                "password = private-password\n"
                "api_logging = 0\n"
                "[servers]\n",
                encoding="utf-8",
            )

            def requester(url):
                if "mode=version" in url:
                    return {"version": "5.0.4"}
                if "mode=get_cats" in url:
                    return {"categories": ["*", "shelfarr"]}
                return {"config": {"servers": []}}

            checks = validate.sabnzbd_configuration_checks(
                config, "8085", "operator", requester=requester
            )
            self.assertTrue(all(check.ok for check in checks))

            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "/downloads/usenet", "/wrong"
                ),
                encoding="utf-8",
            )
            checks = {
                check.name: check
                for check in validate.sabnzbd_configuration_checks(
                    config, "8085", "operator", requester=requester
                )
            }
            self.assertFalse(checks["sabnzbd:paths"].ok)

            config.write_text(
                "[misc]\n"
                "download_dir = /downloads/incomplete/usenet\n"
                "complete_dir = /downloads/usenet\n"
                "api_key = private\n"
                "username = operator\n"
                'password = ""\n'
                "api_logging = 1\n",
                encoding="utf-8",
            )
            checks = {
                check.name: check
                for check in validate.sabnzbd_configuration_checks(
                    config, "8085", "operator", requester=requester
                )
            }
            self.assertFalse(checks["sabnzbd:authentication"].ok)

            def no_provider(url):
                if "mode=version" in url:
                    return {"version": "5.0.4"}
                if "mode=get_cats" in url:
                    return {"categories": ["shelfarr"]}
                return {"config": {"servers": []}}

            checks = validate.sabnzbd_configuration_checks(
                config, "8085", "operator", requester=no_provider
            )
            provider = next(
                check for check in checks
                if check.name == "sabnzbd:provider-observation"
            )
            self.assertTrue(provider.ok)
            self.assertIn("unavailable", provider.detail)

    def test_sabnzbd_usenet_provider_requires_exact_live_tls_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "sabnzbd.ini"
            config.write_text(
                "[misc]\n"
                "download_dir = /downloads/incomplete/usenet\n"
                "complete_dir = /downloads/usenet\n"
                "api_key = private\n"
                "username = operator\n"
                "password = private-password\n"
                "api_logging = 0\n",
                encoding="utf-8",
            )
            environment = {
                "WYSEARR_USENET_ENABLED": "true",
                "USENET_SERVER_HOST": "news.example",
                "USENET_SERVER_PORT": "563",
                "USENET_SERVER_SSL": "true",
                "USENET_SERVER_USERNAME": "reader",
                "USENET_SERVER_PASSWORD": "private-provider-password",
                "USENET_SERVER_CONNECTIONS": "8",
                "USENET_SERVER_RETENTION": "3000",
            }

            def requester(url):
                if "mode=version" in url:
                    return {"version": "5.0.4"}
                if "mode=get_cats" in url:
                    return {"categories": ["shelfarr"]}
                return {
                    "config": {
                        "servers": [
                            {
                                "name": "WyseARR Primary",
                                "displayname": "WyseARR Primary",
                                "host": "news.example",
                                "port": 563,
                                "username": "reader",
                                "password": "**********",
                                "connections": 8,
                                "ssl": 1,
                                "ssl_verify": 3,
                                "retention": 3000,
                                "priority": 0,
                                "enable": 1,
                            }
                        ]
                    }
                }

            tester = mock.MagicMock(return_value={"value": {"result": True}})
            checks = {
                check.name: check
                for check in validate.sabnzbd_configuration_checks(
                    config,
                    "8085",
                    "operator",
                    environment,
                    requester=requester,
                    server_tester=tester,
                )
            }
            self.assertTrue(checks["sabnzbd:usenet-provider"].ok)
            parameters = tester.call_args.args[2]
            self.assertEqual(parameters["mode"], "config")
            self.assertEqual(parameters["name"], "test_server")
            self.assertNotIn(environment["USENET_SERVER_PASSWORD"], str(checks))

            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "api_logging = 0", "api_logging = 1"
                ),
                encoding="utf-8",
            )
            tester.reset_mock()
            checks = {
                check.name: check
                for check in validate.sabnzbd_configuration_checks(
                    config,
                    "8085",
                    "operator",
                    environment,
                    requester=requester,
                    server_tester=tester,
                )
            }
            self.assertFalse(checks["sabnzbd:authentication"].ok)
            self.assertFalse(checks["sabnzbd:usenet-provider"].ok)
            tester.assert_not_called()
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "api_logging = 1", "api_logging = 0"
                ),
                encoding="utf-8",
            )

            tester.return_value = {
                "value": {"result": False, "message": "rejected"}
            }
            checks = {
                check.name: check
                for check in validate.sabnzbd_configuration_checks(
                    config,
                    "8085",
                    "operator",
                    environment,
                    requester=requester,
                    server_tester=tester,
                )
            }
            self.assertFalse(checks["sabnzbd:usenet-provider"].ok)

            def duplicate_provider(url):
                value = requester(url)
                if "mode=get_config" in url:
                    value["config"]["servers"].append(
                        {**value["config"]["servers"][0], "name": "wysearr primary"}
                    )
                return value

            checks = {
                check.name: check
                for check in validate.sabnzbd_configuration_checks(
                    config,
                    "8085",
                    "operator",
                    environment,
                    requester=duplicate_provider,
                    server_tester=tester,
                )
            }
            self.assertFalse(checks["sabnzbd:usenet-provider"].ok)

            def wrong_priority(url):
                value = requester(url)
                if "mode=get_config" in url:
                    value["config"]["servers"][0]["priority"] = 99
                return value

            tester.return_value = {"value": {"result": True}}
            checks = {
                check.name: check
                for check in validate.sabnzbd_configuration_checks(
                    config,
                    "8085",
                    "operator",
                    environment,
                    requester=wrong_priority,
                    server_tester=tester,
                )
            }
            self.assertFalse(checks["sabnzbd:usenet-provider"].ok)

    def test_sabnzbd_usenet_provider_rejects_partial_or_disabled_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "sabnzbd.ini"
            config.write_text(
                "[misc]\n"
                "download_dir = /downloads/incomplete/usenet\n"
                "complete_dir = /downloads/usenet\n"
                "api_key = private\n"
                "username = operator\n"
                "password = private-password\n"
                "api_logging = 0\n",
                encoding="utf-8",
            )

            def requester(url):
                if "mode=version" in url:
                    return {"version": "5.0.4"}
                if "mode=get_cats" in url:
                    return {"categories": ["shelfarr"]}
                return {
                    "config": {
                        "servers": [
                            {
                                "name": "WyseARR Primary",
                                "host": "news.example",
                                "enable": 1,
                            }
                        ]
                    }
                }

            enabled = {
                "WYSEARR_USENET_ENABLED": "true",
                "USENET_SERVER_HOST": "news.example",
            }
            checks = {
                check.name: check
                for check in validate.sabnzbd_configuration_checks(
                    config, "8085", "operator", enabled, requester=requester
                )
            }
            self.assertFalse(checks["sabnzbd:usenet-provider"].ok)

            checks = {
                check.name: check
                for check in validate.sabnzbd_configuration_checks(
                    config,
                    "8085",
                    "operator",
                    {"WYSEARR_USENET_ENABLED": "false"},
                    requester=requester,
                )
            }
            self.assertFalse(checks["sabnzbd:usenet-provider"].ok)

    def test_managed_newznab_requires_live_tag_isolation_and_private_contract(self):
        environment = {
            "NEWZNAB_INDEXER_NAME": "WyseARR Books",
            "NEWZNAB_BASE_URL": "https://indexer.example",
            "NEWZNAB_API_PATH": "/api",
            "NEWZNAB_API_KEY": "private-indexer-key",
        }
        indexer = {
            "id": 17,
            "name": "WyseARR Books",
            "enable": True,
            "implementation": "Newznab",
            "configContract": "NewznabSettings",
            "protocol": "usenet",
            "priority": 20,
            "tags": [4],
            "capabilities": {
                "categories": [
                    {
                        "id": 3000,
                        "subCategories": [{"id": 3030, "subCategories": []}],
                    },
                    {
                        "id": 7000,
                        "subCategories": [{"id": 7020, "subCategories": []}],
                    },
                ]
            },
            "fields": [
                {"name": "baseUrl", "value": "https://indexer.example/"},
                {"name": "apiPath", "value": "/api"},
                {"name": "apiKey", "value": "(removed)"},
            ],
        }
        torrent = {
            "id": 18,
            "name": "Torrent Fallback",
            "enable": True,
            "implementation": "Cardigann",
            "protocol": "torrent",
            "tags": [5],
        }
        tags = [
            {"id": 4, "label": "shelfarr"},
            {"id": 5, "label": "wysearr-arr"},
        ]
        applications = [
            {"name": name, "implementation": name, "tags": [5]}
            for name in ("Sonarr", "Radarr", "Lidarr", "Whisparr")
        ]
        check = validate.prowlarr_managed_newznab_check(
            [indexer, torrent],
            tags,
            applications,
            {17},
            environment,
            enabled=True,
        )
        self.assertTrue(check.ok)
        self.assertNotIn(environment["NEWZNAB_API_KEY"], check.detail)

        for changed_indexers, changed_apps, live_ids in (
            ([{**indexer, "tags": []}, torrent], applications, {17}),
            ([indexer, {**torrent, "tags": []}], applications, {17}),
            ([indexer, {**torrent, "tags": [4, 5]}], applications, {17}),
            ([{**indexer, "priority": 99}, torrent], applications, {17}),
            (
                [indexer, torrent],
                [
                    {**app, "tags": []}
                    if app["name"] == "Radarr"
                    else app
                    for app in applications
                ],
                {17},
            ),
            (
                [{**indexer, "tags": [4, 9]}, torrent],
                [
                    {**app, "tags": [5, 9]}
                    if app["name"] == "Sonarr"
                    else app
                    for app in applications
                ],
                {17},
            ),
            ([indexer, torrent], applications, set()),
            (
                [
                    {
                        **indexer,
                        "capabilities": {
                            "categories": [
                                {"id": 2000, "subCategories": []}
                            ]
                        },
                    },
                    torrent,
                ],
                applications,
                {17},
            ),
        ):
            with self.subTest(
                applications=changed_apps,
                live=live_ids,
            ):
                self.assertFalse(
                    validate.prowlarr_managed_newznab_check(
                        changed_indexers,
                        tags,
                        changed_apps,
                        live_ids,
                        environment,
                        enabled=True,
                    ).ok
                )

    def test_managed_newznab_disabled_state_only_rejects_managed_indexer(self):
        disabled = {
            "id": 17,
            "name": "WyseARR Books",
            "enable": False,
            "implementation": "Newznab",
            "protocol": "usenet",
        }
        self.assertTrue(
            validate.prowlarr_managed_newznab_check(
                [disabled], [], [], set(), {}, enabled=False
            ).ok
        )
        self.assertFalse(
            validate.prowlarr_managed_newznab_check(
                [disabled, {**disabled, "id": 18, "name": "wysearr books"}],
                [],
                [],
                set(),
                {},
                enabled=False,
            ).ok
        )
        self.assertFalse(
            validate.prowlarr_managed_newznab_check(
                [{**disabled, "enable": True}], [], [], set(), {}, enabled=False
            ).ok
        )
        self.assertFalse(
            validate.prowlarr_managed_newznab_check(
                [disabled],
                [],
                [],
                set(),
                {"NEWZNAB_INDEXER_NAME": "Custom Books"},
                enabled=False,
            ).ok
        )
        self.assertTrue(
            validate.prowlarr_managed_newznab_check(
                [
                    {
                        **disabled,
                        "name": "Unrelated Usenet",
                        "enable": True,
                    }
                ],
                [],
                [],
                set(),
                {},
                enabled=False,
            ).ok
        )

    def test_stopped_sab_requires_managed_provider_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "sabnzbd.ini"
            config.write_text(
                "[misc]\n"
                "api_key = private\n"
                "[servers]\n"
                "[[WyseARR Primary]]\n"
                "displayname = WyseARR Primary\n"
                "enable = 1\n"
                "host = news.example\n",
                encoding="utf-8",
            )
            self.assertFalse(
                validate.sabnzbd_stopped_managed_provider_check(config).ok
            )
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "enable = 1", "enable = 0"
                ),
                encoding="utf-8",
            )
            self.assertTrue(
                validate.sabnzbd_stopped_managed_provider_check(config).ok
            )

            config.write_text(
                "[servers]\n"
                "[[Unrelated Provider]]\n"
                "enable = 1\n",
                encoding="utf-8",
            )
            self.assertTrue(
                validate.sabnzbd_stopped_managed_provider_check(config).ok
            )
    def test_shelfarr_runtime_parses_client_results_after_rails_logs(self):
        runner = mock.MagicMock()
        runner.return_value = mock.MagicMock(
            returncode=0,
            stdout=(
                "[info] qBittorrent connection successful\n"
                "[info] SABnzbd connection successful\n"
                "WYSEARR_CLIENT_RESULTS="
                '[["qbittorrent","shelfarr",true,true],'
                '["sabnzbd","shelfarr",true,true]]\n'
            ),
        )
        checks = validate.shelfarr_runtime_checks(
            "5056",
            "shf_token",
            True,
            qbit_username="qbit-user",
            qbit_password="qbit-private-password",
            runner=runner,
            requester=lambda *_args, **_kwargs: {"requests": []},
        )
        self.assertTrue(all(check.ok for check in checks))
        self.assertNotIn("qbit-private-password", repr(checks))
        self.assertEqual(
            json.loads(runner.call_args.kwargs["input"]),
            {
                "username": "qbit-user",
                "password": "qbit-private-password",
            },
        )

    def test_shelfarr_runtime_rejects_enabled_sab_when_usenet_is_disabled(self):
        runner = mock.MagicMock()
        runner.return_value = mock.MagicMock(
            returncode=0,
            stdout=(
                "WYSEARR_CLIENT_RESULTS="
                '[["qbittorrent","shelfarr",true,false],'
                '["sabnzbd","shelfarr",true,true]]\n'
            ),
        )
        checks = validate.shelfarr_runtime_checks(
            "5056",
            "shf_token",
            False,
            qbit_username="qbit-user",
            qbit_password="qbit-private-password",
            runner=runner,
            requester=lambda *_args, **_kwargs: {"requests": []},
        )
        self.assertFalse(
            next(check for check in checks if check.name == "shelfarr:client-connectivity").ok
        )
        self.assertFalse(
            next(
                check
                for check in checks
                if check.name == "shelfarr:qbittorrent-credentials"
            ).ok
        )

    def test_arr_native_discord_check_reads_notification_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "arr.db"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE Notifications (
                        Name TEXT, Implementation TEXT,
                        ConfigContract TEXT, Settings TEXT
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO Notifications VALUES (?, ?, ?, ?)",
                    ("Email", "Email", "EmailSettings", "{}"),
                )

            self.assertTrue(
                validate.arr_native_discord_check("radarr", database).ok
            )
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "INSERT INTO Notifications VALUES (?, ?, ?, ?)",
                    ("Lifecycle", "Discord", "DiscordSettings", "{}"),
                )
            self.assertFalse(
                validate.arr_native_discord_check("radarr", database).ok
            )

            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("DELETE FROM Notifications WHERE Name = 'Lifecycle'")
                connection.execute(
                    "INSERT INTO Notifications VALUES (?, ?, ?, ?)",
                    (
                        "Generic webhook",
                        "Webhook",
                        "WebhookSettings",
                        '{"url":"https://discord.com/api/webhooks/1/private"}',
                    ),
                )
            self.assertFalse(
                validate.arr_native_discord_check("radarr", database).ok
            )

    def test_bazarr_native_discord_and_external_webhook_must_be_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "bazarr.db"
            config = root / "config.yaml"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE table_settings_notifier (
                        name TEXT, enabled INTEGER, url TEXT
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO table_settings_notifier VALUES (?, ?, ?)",
                    ("Discord", 0, None),
                )
            config.write_text(
                "general:\n  use_external_webhook: false\n", encoding="utf-8"
            )
            self.assertTrue(
                validate.bazarr_native_discord_check(database, config).ok
            )

            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "UPDATE table_settings_notifier SET enabled = 1 "
                    "WHERE name = 'Discord'"
                )
            self.assertFalse(
                validate.bazarr_native_discord_check(database, config).ok
            )

            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "UPDATE table_settings_notifier SET enabled = 0 "
                    "WHERE name = 'Discord'"
                )
            config.write_text(
                "general:\n  use_external_webhook: true\n", encoding="utf-8"
            )
            self.assertFalse(
                validate.bazarr_native_discord_check(database, config).ok
            )

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

    def test_arr_download_client_acceptance_requires_managed_contract(self):
        resource = {
            "enable": True,
            "implementation": "QBittorrent",
            "removeCompletedDownloads": False,
            "fields": [
                {"name": "host", "value": "qbittorrent"},
                {"name": "port", "value": 8080},
                {"name": "useSsl", "value": False},
                {"name": "username", "value": "admin"},
                {"name": "movieCategory", "value": "movies"},
                {"name": "movieImportedCategory", "value": "movies-imported"},
            ],
        }
        arguments = {
            "username": "admin",
            "category": "movies",
            "category_fields": ("category", "movieCategory"),
            "imported_fields": ("postImportCategory", "movieImportedCategory"),
        }
        self.assertTrue(validate.arr_download_client_accepted(resource, **arguments))
        for field, bad_value in (
            ("host", "localhost"),
            ("port", 9999),
            ("movieCategory", "wrong"),
            ("movieImportedCategory", "wrong-imported"),
        ):
            changed = {**resource, "fields": [dict(item) for item in resource["fields"]]}
            next(item for item in changed["fields"] if item["name"] == field)["value"] = bad_value
            self.assertFalse(validate.arr_download_client_accepted(changed, **arguments))
        changed = dict(resource, removeCompletedDownloads=True)
        self.assertFalse(validate.arr_download_client_accepted(changed, **arguments))

    def test_arr_prowlarr_credentials_reject_one_stale_row_among_many(self):
        current_key = "current-private-prowlarr-key"
        stale_key = "stale-private-prowlarr-key"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "arr.db"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE Indexers (
                        Id INTEGER PRIMARY KEY,
                        Name TEXT NOT NULL,
                        Settings TEXT NOT NULL,
                        EnableRss INTEGER NOT NULL,
                        EnableAutomaticSearch INTEGER NOT NULL,
                        EnableInteractiveSearch INTEGER NOT NULL
                    )
                    """
                )
                rows = []
                for indexer_id, key in (
                    (1, current_key),
                    (2, stale_key),
                    (3, current_key),
                ):
                    rows.append(
                        (
                            indexer_id,
                            f"Indexer {indexer_id} (Prowlarr)",
                            json.dumps(
                                {
                                    "baseUrl": (
                                        f"http://prowlarr:9696/{indexer_id}/"
                                    ),
                                    "apiKey": key,
                                }
                            ),
                            1,
                            1,
                            1,
                        )
                    )
                rows.extend(
                    [
                        (
                            4,
                            "Disabled (Prowlarr)",
                            json.dumps(
                                {
                                    "baseUrl": "http://prowlarr:9696/4/",
                                    "apiKey": stale_key,
                                }
                            ),
                            0,
                            0,
                            0,
                        ),
                        (
                            5,
                            "Independent indexer",
                            json.dumps(
                                {
                                    "baseUrl": "https://indexer.invalid/",
                                    "apiKey": stale_key,
                                }
                            ),
                            1,
                            1,
                            1,
                        ),
                    ]
                )
                connection.executemany(
                    "INSERT INTO Indexers VALUES (?, ?, ?, ?, ?, ?)", rows
                )

            original_compare = validate.secrets.compare_digest
            with mock.patch.object(
                validate.secrets,
                "compare_digest",
                wraps=original_compare,
            ) as compare_digest:
                check = validate.arr_prowlarr_indexer_credentials_check(
                    "sonarr", database, current_key
                )

            self.assertFalse(check.ok)
            self.assertEqual(compare_digest.call_count, 3)
            self.assertEqual(
                check.detail,
                "enabled_prowlarr=3 exact_credentials=2 "
                "mismatched_or_malformed=1",
            )
            self.assertNotIn(current_key, repr(check))
            self.assertNotIn(stale_key, repr(check))

            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "UPDATE Indexers SET Settings = ? WHERE Id = 2",
                    (
                        json.dumps(
                            {
                                "baseUrl": "http://prowlarr:9696/2/",
                                "apiKey": current_key,
                            }
                        ),
                    ),
                )
            repaired = validate.arr_prowlarr_indexer_credentials_check(
                "sonarr", database, current_key
            )
            self.assertTrue(repaired.ok)
            self.assertEqual(
                repaired.detail,
                "enabled_prowlarr=3 exact_credentials=3",
            )

    def test_arr_qbittorrent_credentials_reject_one_stale_row_among_many(self):
        username = "private-qbit-user"
        current_password = "current-private-qbit-password"
        stale_password = "stale-private-qbit-password"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "arr.db"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE DownloadClients (
                        Id INTEGER PRIMARY KEY,
                        Enable INTEGER NOT NULL,
                        Implementation TEXT NOT NULL,
                        Settings TEXT NOT NULL
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO DownloadClients VALUES (?, ?, ?, ?)",
                    (
                        (
                            1,
                            1,
                            "QBittorrent",
                            json.dumps(
                                {
                                    "username": username,
                                    "password": current_password,
                                }
                            ),
                        ),
                        (
                            2,
                            1,
                            "QBittorrent",
                            json.dumps(
                                {
                                    "username": username,
                                    "password": stale_password,
                                }
                            ),
                        ),
                        (
                            3,
                            0,
                            "QBittorrent",
                            json.dumps(
                                {
                                    "username": username,
                                    "password": stale_password,
                                }
                            ),
                        ),
                        (
                            4,
                            1,
                            "Transmission",
                            json.dumps(
                                {
                                    "username": username,
                                    "password": stale_password,
                                }
                            ),
                        ),
                    ),
                )

            check = validate.arr_qbittorrent_download_client_credentials_check(
                "sonarr", database, username, current_password
            )
            self.assertFalse(check.ok)
            self.assertEqual(
                check.detail,
                "enabled_qbittorrent=2 exact_credentials=1 "
                "mismatched_or_malformed=1",
            )
            for secret in (username, current_password, stale_password):
                self.assertNotIn(secret, repr(check))

            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "UPDATE DownloadClients SET Settings = ? WHERE Id = 2",
                    (
                        json.dumps(
                            {
                                "username": username,
                                "password": current_password,
                            }
                        ),
                    ),
                )
            repaired = (
                validate.arr_qbittorrent_download_client_credentials_check(
                    "sonarr", database, username, current_password
                )
            )
            self.assertTrue(repaired.ok)
            self.assertEqual(
                repaired.detail,
                "enabled_qbittorrent=2 exact_credentials=2",
            )

    def test_indexer_live_tests_are_exhaustive_and_upstream_failure_is_nonblocking(self):
        indexers = [
            {"id": 1, "enable": True, "protocol": "torrent"},
            {"id": 2, "enable": True, "protocol": "torrent"},
            {"id": 3, "enable": True, "protocol": "usenet"},
            {
                "id": 4,
                "enableRss": False,
                "enableAutomaticSearch": False,
                "enableInteractiveSearch": False,
                "protocol": "torrent",
            },
        ]
        tested: list[int] = []

        def tester(indexer: dict[str, object]) -> None:
            indexer_id = int(indexer["id"])
            tested.append(indexer_id)
            if indexer_id == 3:
                raise RuntimeError("private failure detail")

        check, live_ids, live_protocols = validate.exhaustive_indexer_live_check(
            "sonarr", indexers, tester, enabled_default=True
        )

        self.assertEqual(tested, [1, 2, 3])
        self.assertFalse(check.ok)
        self.assertFalse(check.blocking)
        self.assertEqual(check.detail, "enabled=3 live=2 failed=1")
        self.assertEqual(live_ids, {1, 2})
        self.assertEqual(live_protocols, {"torrent"})
        self.assertNotIn("private failure detail", repr(check))

    def test_indexer_live_check_blocks_an_empty_enabled_inventory(self):
        check, live_ids, live_protocols = validate.exhaustive_indexer_live_check(
            "sonarr",
            [{"id": 1, "enable": False, "protocol": "torrent"}],
            lambda _indexer: self.fail("disabled indexer was tested"),
            enabled_default=True,
        )

        self.assertFalse(check.ok)
        self.assertTrue(check.blocking)
        self.assertEqual(check.detail, "enabled=0 live=0 failed=0")
        self.assertEqual(live_ids, set())
        self.assertEqual(live_protocols, set())

    def test_validator_exit_ignores_only_explicit_nonblocking_warnings(self):
        with (
            mock.patch.object(sys, "argv", ["validate.py"]),
            mock.patch.object(
                validate,
                "validate",
                return_value=[
                    validate.Check("local:contract", True, "ready"),
                    validate.Check(
                        "upstream:indexer",
                        False,
                        "enabled=2 live=1 failed=1",
                        blocking=False,
                    ),
                ],
            ),
            mock.patch("builtins.print") as printer,
        ):
            self.assertEqual(validate.main(), 0)
        self.assertTrue(
            any(
                str(call.args[0]).startswith("WARN: upstream:indexer")
                for call in printer.call_args_list
            )
        )

        with (
            mock.patch.object(sys, "argv", ["validate.py"]),
            mock.patch.object(
                validate,
                "validate",
                return_value=[validate.Check("local:contract", False, "broken")],
            ),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(validate.main(), 1)

    def test_post_json_ok_uses_api_key_and_json_body(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        with mock.patch("urllib.request.urlopen", return_value=response) as opener:
            validate.post_json_ok(
                "http://service.invalid/api/v1/indexer/test",
                {"id": 4, "enable": True},
                api_key="private-key",
                timeout=60,
            )
        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"id": 4, "enable": True})
        self.assertEqual(request.headers["X-api-key"], "private-key")
        self.assertEqual(opener.call_args.kwargs["timeout"], 60)


if __name__ == "__main__":
    unittest.main()
