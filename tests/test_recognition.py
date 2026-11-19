from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from deal_watch.models import MessageCandidate, Rect
from deal_watch.recognition import RecognitionStore


def message(text: str, tracking_id: str) -> MessageCandidate:
    return MessageCandidate(
        region_id="r1", text=text, box=Rect(0, 0, 10, 10), first_seen=1,
        last_seen=1, tracking_id=tracking_id,
    )


class RecognitionStoreTests(unittest.TestCase):
    def test_same_storage_clock_retains_insertion_order_across_batches_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with closing(RecognitionStore(path, limit=2)) as store:
                with mock.patch("deal_watch.recognition.time.time", return_value=100), \
                     mock.patch("deal_watch.recognition.new_id", side_effect=["z", "y", "a"]):
                    store.record_candidates("r", "synthetic", 10, [message("first", "1"), message("second", "2")], {})
                    store.record_candidates("r", "synthetic", 10, [message("third", "3")], {})
            with closing(RecognitionStore(path, limit=2)) as store:
                self.assertEqual([row["text"] for row in store.list_records(1)], ["third"])
                self.assertEqual([row["text"] for row in store.list_records(1, 1)], ["second"])

    @staticmethod
    def _create_legacy(path: Path, *, count: int = 1, wal: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(path)
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.executescript(
            """CREATE TABLE candidates (
                id TEXT PRIMARY KEY, region_id TEXT NOT NULL, region_alias TEXT NOT NULL,
                observed_at REAL NOT NULL, text TEXT NOT NULL, kind TEXT NOT NULL,
                boundary_uncertain INTEGER NOT NULL, matched_keywords TEXT NOT NULL,
                model_score REAL, decision TEXT NOT NULL, char_matches TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE labels (id TEXT, keyword TEXT, label INTEGER);
            """
        )
        connection.executemany(
            """INSERT INTO candidates VALUES (?, 'r1', '群 A', ?, ?, 'normal_text',
                0, '[]', NULL, 'not_matched', '[]', ?)""",
            [(f"old-{i}", i, f"旧记录{i}", i) for i in range(count)],
        )
        connection.commit()
        return connection

    def test_records_candidates_and_enforces_latest_n_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecognitionStore(Path(directory), limit=2)
            self.assertEqual(store.records_revision, 0)
            store.record_candidates(
                "r1", "群 A", 10,
                [message("候选一", "id1"), message("候选二", "id2")],
                {"id1": ["手机"], "id2": []},
                {"id1": "should_notify", "id2": "not_matched"},
            )
            store.record_candidates(
                "r1", "群 A", 10.5, [message("候选二更新", "id2")], {"id2": []}
            )
            store.record_candidates("r1", "群 A", 11, [message("候选三", "id3")], {})
            self.assertEqual(store.count(), 2)
            records = store.list_records(limit=10)
            self.assertEqual([item["text"] for item in records], ["候选三", "候选二更新"])
            self.assertNotEqual(records[1]["id"], "id2")
            self.assertEqual(json.loads(records[1]["matched_keywords"]), [])
            self.assertEqual(records[1]["char_matches"], "[]")
            self.assertNotIn("model_score", records[0])
            self.assertEqual(store.records_revision, 3)
            store.close()

    def test_set_limit_trims_immediately_and_rejects_non_positive_values(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecognitionStore(Path(directory), limit=3)
            for index in range(3):
                store.record_candidates(
                    "r1", "群 A", index,
                    [message(f"候选{index}", f"id{index}")], {},
                )
            store.set_limit(1)
            self.assertEqual(store.count(), 1)
            self.assertEqual(store.list_records()[0]["text"], "候选2")
            for invalid in (0, -1, True, 1.5):
                with self.subTest(limit=invalid), self.assertRaises(ValueError):
                    store.set_limit(invalid)
            store.close()

    def test_same_tracking_id_on_two_samples_creates_two_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecognitionStore(Path(directory), limit=5)
            ids1 = store.record_candidates(
                "r1", "群 A", 1, [message("第一次出现", "same-tracking")], {}
            )
            ids2 = store.record_candidates(
                "r1", "群 A", 2, [message("第二次采样", "same-tracking")], {}
            )
            rows = store.list_records(limit=5)
            self.assertEqual(store.count(), 2)
            self.assertNotEqual(ids1[0], ids2[0])
            self.assertEqual({row["text"] for row in rows}, {"第一次出现", "第二次采样"})
            store.close()

    def test_observed_at_age_does_not_expire_rows_before_count_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecognitionStore(Path(directory), limit=2)
            with mock.patch("deal_watch.recognition.time.time", side_effect=[100, 101, 102]):
                store.record_candidates(
                    "r1", "群 A", -10_000_000, [message("很久以前的观察", "old")], {}
                )
                store.record_candidates(
                    "r1", "群 A", 100, [message("较新观察", "new")], {}
                )
                self.assertEqual(store.count(), 2)
                self.assertIn("很久以前的观察", {r["text"] for r in store.list_records(10)})
                store.record_candidates(
                    "r1", "群 A", 101, [message("第三条", "third")], {}
                )
            self.assertEqual(store.count(), 2)
            self.assertNotIn("很久以前的观察", {r["text"] for r in store.list_records(10)})
            store.close()

    def test_set_limit_sql_failure_restores_previous_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecognitionStore(Path(directory), limit=3)
            store._conn.execute("DROP TABLE records")
            store._conn.commit()
            with self.assertRaises(sqlite3.OperationalError):
                store.set_limit(1)
            self.assertEqual(store.limit, 3)
            store.close()

    def test_prepare_migrates_verified_candidates_and_removes_old_learning_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = sqlite3.connect(root / "feedback.db")
            legacy.executescript(
                """CREATE TABLE candidates (
                    id TEXT PRIMARY KEY, region_id TEXT NOT NULL, region_alias TEXT NOT NULL,
                    observed_at REAL NOT NULL, text TEXT NOT NULL, kind TEXT NOT NULL,
                    boundary_uncertain INTEGER NOT NULL, matched_keywords TEXT NOT NULL,
                    model_score REAL, decision TEXT NOT NULL, char_matches TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE labels (id TEXT, keyword TEXT, label INTEGER);
                """
            )
            legacy.execute(
                """INSERT INTO candidates VALUES
                ('old-a','r1','群 A',1,'普通文字','normal_text',0,'[]',NULL,'not_matched','[]',1),
                ('old-b','r2','群 B',2,'手机优惠','normal_text',0,'[\"手机\"]',0.99,'should_notify','[\"手机\"]',2)"""
            )
            legacy.execute("INSERT INTO labels VALUES ('label','手机',1)")
            legacy.commit()
            legacy.close()
            learning = root / "learning_models"
            learning.mkdir()
            (learning / "marker.bin").write_bytes(b"local artifact")

            store = RecognitionStore(root, limit=1)
            result = store.prepare()
            self.assertEqual(result["migrated"], 1)
            self.assertEqual(result["count"], 1)
            self.assertFalse((root / "feedback.db").exists())
            self.assertFalse(learning.exists())
            record = store.list_records()[0]
            self.assertEqual(record["id"], "old-b")
            self.assertEqual(record["text"], "手机优惠")
            self.assertEqual(record["region_id"], "r2")
            self.assertEqual(record["decision"], "should_notify")
            self.assertEqual(record["char_matches"], '["手机"]')
            self.assertNotIn("model_score", record)
            self.assertNotIn("labels", record)
            store.close()

    def test_prepare_without_old_store_is_normal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecognitionStore(Path(directory))
            self.assertEqual(store.prepare()["count"], 0)
            self.assertTrue((Path(directory) / "recognition.db").exists())
            store.close()

    def test_prepare_reads_committed_rows_still_in_wal_with_source_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._create_legacy(root / "feedback.db", count=2, wal=True)
            wal_path = root / "feedback.db-wal"
            self.assertTrue(wal_path.exists())
            self.assertGreater(wal_path.stat().st_size, 0)
            store = RecognitionStore(root)
            try:
                # An open source handle can prevent unlink on Windows. The
                # destination commit and verification must finish before that.
                with self.assertRaises(PermissionError):
                    store.prepare()
                self.assertEqual(store.count(), 2)
                self.assertEqual(
                    {row["text"] for row in store.list_records(limit=10)},
                    {"旧记录0", "旧记录1"},
                )
            finally:
                source.close()
            result = store.prepare()
            self.assertEqual(result["count"], 2)
            self.assertFalse((root / "feedback.db").exists())
            store.close()

    def test_validation_failure_keeps_source_and_does_not_mark_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._create_legacy(root / "feedback.db", count=2)
            source.close()
            store = RecognitionStore(root)
            with mock.patch.object(
                RecognitionStore, "_summary", side_effect=["actual", "source"]
            ):
                with self.assertRaisesRegex(RuntimeError, "迁移校验失败"):
                    store.prepare()
            self.assertTrue((root / "feedback.db").exists())
            marker = store._conn.execute(
                "SELECT value FROM meta WHERE key = 'feedback_migration_v1'"
            ).fetchone()
            self.assertIsNone(marker)
            store.close()

    def test_migration_ignores_non_normal_legacy_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._create_legacy(root / "feedback.db", count=1)
            source.execute(
                """INSERT INTO candidates VALUES (
                    'manual','manual','群 A',500,'手动文字','manual_text',0,'[]',NULL,
                    'manual','[]',500
                )"""
            )
            source.commit()
            source.close()
            store = RecognitionStore(root, limit=1)
            store.prepare()
            self.assertEqual(store.count(), 1)
            self.assertEqual(store.list_records()[0]["id"], "old-0")
            store.close()

    def test_destination_transaction_failure_keeps_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._create_legacy(root / "feedback.db", count=1)
            source.close()
            store = RecognitionStore(root)
            store._conn.execute(
                """CREATE TRIGGER fail_candidate_insert BEFORE INSERT ON records
                BEGIN SELECT RAISE(ABORT, 'forced destination failure'); END"""
            )
            store._conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                store.prepare()
            self.assertTrue((root / "feedback.db").exists())
            self.assertEqual(store.count(), 0)
            store.close()

    def test_legacy_delete_failure_retries_without_duplicate_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._create_legacy(root / "feedback.db", count=2)
            source.close()
            store = RecognitionStore(root)
            with mock.patch.object(
                RecognitionStore, "_remove_legacy_files", side_effect=PermissionError("locked")
            ):
                with self.assertRaises(PermissionError):
                    store.prepare()
            self.assertEqual(store.count(), 2)
            self.assertTrue((root / "feedback.db").exists())
            store.prepare()
            self.assertEqual(store.count(), 2)
            self.assertFalse((root / "feedback.db").exists())
            self.assertEqual(store._conn.execute(
                "SELECT value FROM meta WHERE key = 'feedback_migration_v1'"
            ).fetchone()[0], "complete")
            store.close()


if __name__ == "__main__":
    unittest.main()
