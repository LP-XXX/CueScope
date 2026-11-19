"""Bounded ordinary OCR candidate history and retryable feedback migration."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .models import MessageCandidate, new_id
from .record_search import RecordSearch

_RECORD_FIELDS = (
    "id", "region_id", "region_alias", "observed_at", "text", "kind",
    "boundary_uncertain", "matched_keywords", "decision", "char_matches", "created_at",
)
_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    region_id TEXT NOT NULL,
    region_alias TEXT NOT NULL,
    observed_at REAL NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL,
    boundary_uncertain INTEGER NOT NULL,
    matched_keywords TEXT NOT NULL,
    decision TEXT NOT NULL,
    char_matches TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_created ON records(created_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class RecognitionStore:
    """Independent, size-bounded store of ordinary text candidates.

    Construction only creates the new schema. ``prepare`` performs the old store
    migration and local learning artifact cleanup and must finish before monitoring.
    """

    def __init__(self, data_dir: Path, limit: int = 20000) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "recognition.db"
        self._validate_limit(limit)
        self.limit = limit
        self._lock = threading.RLock()
        self._revision_lock = threading.Lock()
        self._records_revision = 0
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("识别记录上限必须为正整数")

    @property
    def records_revision(self) -> int:
        with self._revision_lock:
            return self._records_revision

    def _bump_revision_locked(self) -> None:
        with self._revision_lock:
            self._records_revision += 1

    @staticmethod
    def _summary(rows: list[sqlite3.Row] | list[dict[str, Any]]) -> str:
        digest = hashlib.sha256()
        for row in rows:
            values = [row[key] for key in _RECORD_FIELDS]
            digest.update(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    def prepare(self) -> dict[str, Any]:
        """Migrate the legacy candidate history and remove its learning artifacts.

        Source reads use an explicit read-only connection and a single WAL snapshot.
        A migration marker is committed only after destination count and digest
        match the source's newest ``limit`` records. If source deletion fails, the
        marker lets a later call retry deletion without copying or losing records.
        """
        with self._lock:
            legacy_path = self.data_dir / "feedback.db"
            meta = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'feedback_migration_v1'"
            ).fetchone()
            migration_done = bool(meta and meta["value"] == "complete")
            migrated_count = 0
            if legacy_path.exists() and not migration_done:
                source_rows = self._read_legacy_snapshot(legacy_path)
                rows = source_rows[: self.limit]
                with self._conn:
                    self._conn.executemany(
                        """INSERT OR IGNORE INTO records(
                            id, region_id, region_alias, observed_at, text, kind,
                            boundary_uncertain, matched_keywords, decision,
                            char_matches, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        [tuple(row[key] for key in _RECORD_FIELDS) for row in rows],
                    )
                    self._trim_locked()
                actual = self._conn.execute(
                    "SELECT * FROM records ORDER BY created_at DESC, id DESC LIMIT ?",
                    (self.limit,),
                ).fetchall()
                if len(actual) != len(rows) or self._summary(actual) != self._summary(rows):
                    raise RuntimeError("旧识别记录迁移校验失败，原数据库已保留")
                with self._conn:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, 'complete')",
                        ("feedback_migration_v1",),
                    )
                migration_done = True
                migrated_count = len(rows)
                if rows:
                    self._bump_revision_locked()
            else:
                before = self.count()
                with self._conn:
                    self._trim_locked()
                if self.count() != before:
                    self._bump_revision_locked()

            if migration_done:
                self._remove_legacy_files(legacy_path)

            learning_dir = self.data_dir / "learning_models"
            if learning_dir.exists() or learning_dir.is_symlink():
                self._remove_learning_dir(learning_dir)

            count = int(self._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0])
            return {
                "ready": True,
                "migrated": migrated_count,
                "count": count,
                "limit": self.limit,
                "legacy_removed": not legacy_path.exists(),
                "learning_models_removed": not learning_dir.exists(),
            }

    def _read_legacy_snapshot(self, path: Path) -> list[dict[str, Any]]:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        source = sqlite3.connect(uri, uri=True, timeout=10)
        source.row_factory = sqlite3.Row
        try:
            source.execute("PRAGMA query_only=ON")
            source.execute("BEGIN")
            tables = {row[0] for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if "candidates" not in tables:
                raise RuntimeError("旧反馈数据库缺少候选记录表，原数据库已保留")
            columns = {row[1] for row in source.execute("PRAGMA table_info(candidates)")}
            required = set(_RECORD_FIELDS)
            required.update({"boundary_uncertain", "matched_keywords", "char_matches"})
            missing = required - columns
            if missing:
                raise RuntimeError("旧反馈数据库结构不完整，原数据库已保留")
            rows = source.execute(
                """SELECT id, region_id, region_alias, observed_at, text, kind,
                    boundary_uncertain, matched_keywords, decision, char_matches, created_at
                    FROM candidates WHERE kind = 'normal_text'
                    ORDER BY created_at DESC, id DESC LIMIT ?""",
                (self.limit,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            source.rollback()
            source.close()

    @staticmethod
    def _remove_legacy_files(path: Path) -> None:
        # Remove the database first. Sidecars are then inert and can be retried
        # independently if Windows temporarily holds one open.
        path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)

    def _remove_learning_dir(self, path: Path) -> None:
        expected = self.data_dir.resolve() / "learning_models"
        if path.resolve() != expected:
            raise RuntimeError("学习模型目录路径校验失败")
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

    def _trim_locked(self) -> None:
        self._conn.execute(
            """DELETE FROM records WHERE id IN (
                SELECT id FROM records ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?
            )""",
            (self.limit,),
        )

    def record_candidates(
        self,
        region_id: str,
        region_alias: str,
        observed_at: float,
        messages: list[MessageCandidate],
        matched_keywords_by_tracking: dict[str, list[str]],
        decisions_by_tracking: dict[str, str] | None = None,
    ) -> list[str]:
        now = time.time()
        decisions = decisions_by_tracking or {}
        rows: list[tuple[Any, ...]] = []
        ids: list[str] = []
        for message in messages:
            if message.kind != "normal_text":
                continue
            matched = matched_keywords_by_tracking.get(message.tracking_id, [])
            candidate_id = new_id()
            ids.append(candidate_id)
            rows.append((
                candidate_id, region_id, region_alias, observed_at, message.text, message.kind,
                int(message.boundary_uncertain), json.dumps(matched, ensure_ascii=False),
                decisions.get(message.tracking_id, "observed"),
                json.dumps(matched, ensure_ascii=False), now,
            ))
        if not rows:
            return ids
        with self._lock:
            with self._conn:
                self._conn.executemany(
                    """INSERT INTO records(
                        id, region_id, region_alias, observed_at, text, kind,
                        boundary_uncertain, matched_keywords, decision, char_matches, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    rows,
                )
                self._trim_locked()
            self._bump_revision_locked()
        return ids

    def list_records(self, limit: int = 200, offset: int = 0, *, search: RecordSearch | None = None) -> list[dict[str, Any]]:
        if limit < 0 or offset < 0:
            raise ValueError("limit 和 offset 不能为负数")
        clauses, values = search.conditions() if search else ([], [])
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM records {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def count(self, *, search: RecordSearch | None = None) -> int:
        clauses, values = search.conditions() if search else ([], [])
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            return int(self._conn.execute(f"SELECT COUNT(*) FROM records {where}", values).fetchone()[0])

    def set_limit(self, limit: int) -> None:
        self._validate_limit(limit)
        with self._lock:
            previous_limit = self.limit
            try:
                self.limit = limit
                with self._conn:
                    self._trim_locked()
            except Exception:
                self.limit = previous_limit
                raise
            self._bump_revision_locked()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
