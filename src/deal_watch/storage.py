"""SQLite 事件、去重索引和持久 outbox。事件与去重占位在同一事务提交。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .models import MatchEvent, NotificationJob

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    region_id TEXT NOT NULL,
    region_alias TEXT NOT NULL,
    keyword TEXT NOT NULL,
    text TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    observed_at REAL NOT NULL,
    tracking_id TEXT NOT NULL,
    boundary_uncertain INTEGER NOT NULL DEFAULT 0,
    duplicate_of TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);
CREATE TABLE IF NOT EXISTS dedupe (
    dedupe_key TEXT PRIMARY KEY,
    first_event_id TEXT NOT NULL,
    first_enqueued_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS outbox (
    event_id TEXT PRIMARY KEY REFERENCES events(id),
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0,
    next_part INTEGER NOT NULL DEFAULT 0,
    retry_exhausted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(state, next_attempt_at);
CREATE TABLE IF NOT EXISTS delivery_health_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_health_events (
    event_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_delivery_health_pending
ON delivery_health_events(acknowledged, created_at);
"""


class Storage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # 记录表数据版本号：每次改变 ``list_events`` 可见内容的写入都 +1。
        # 只存在内存里——它服务的是同进程的界面刷新判断，不需要跨进程持久化，
        # 也不需要落在事务里（多一次写就多一次 fsync，与 T2.2 的合并事务目标相反）。
        #
        # ⚠️ 版本号由**独立的短锁**保护：``self._lock`` 覆盖整段 SQLite 事务
        # （``BEGIN IMMEDIATE`` 可能在等别的连接释放写锁，最长等到 timeout），
        # 界面心跳若读版本号也抢这把锁，就会在后台写库期间被拖住。短锁只保护
        # 一个整数；锁顺序固定为 ``_lock → _revision_lock``，不会反向获取。
        self._revision_lock = threading.Lock()
        self._records_revision = 0
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        # 可升级已有交付产生的数据库，不重建或丢弃待发事件。
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(outbox)")}
        for name in ("next_part", "retry_exhausted"):
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE outbox ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                )
        self._conn.commit()

    @property
    def records_revision(self) -> int:
        """记录表的数据版本号；界面据此判断"要不要重查一次"。

        单调递增，只在本进程内有效。读它不查库，也**不碰** SQLite 事务锁：
        只抢保护计数器的短锁，后台写库再慢也不会把界面心跳卡住。
        """
        with self._revision_lock:
            return self._records_revision

    def _bump_records_locked(self) -> None:
        """在写入已提交后调用；只取短锁，可以在持有 ``self._lock`` 时调用。"""
        with self._revision_lock:
            self._records_revision += 1

    def record_match(
        self, event: MatchEvent, cooldown_seconds: int = 300, now: float | None = None,
        *, idempotent: bool = False,
    ) -> bool:
        now = time.time() if now is None else now
        payload = self.event_payload(event)
        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                if idempotent:
                    recorded = cursor.execute(
                        "SELECT state FROM events WHERE id = ?", (event.id,)
                    ).fetchone()
                    if recorded is not None:
                        event.state = recorded["state"]
                        self._conn.commit()
                        return False
                existing = cursor.execute(
                    "SELECT * FROM dedupe WHERE dedupe_key = ?", (event.dedupe_key,)
                ).fetchone()
                duplicate = bool(
                    existing
                    and 0 <= now - existing["first_enqueued_at"] < cooldown_seconds
                )
                local_only = getattr(event, "delivery_mode", "feishu") == "local"
                event.state = "duplicate" if duplicate else ("local" if local_only else "pending")
                if duplicate:
                    event.duplicate_of = existing["first_event_id"]
                cursor.execute(
                    """INSERT INTO events(
                        id, region_id, region_alias, keyword, text, dedupe_key,
                        observed_at, tracking_id, boundary_uncertain, duplicate_of,
                        state, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.id,
                        event.region_id,
                        event.region_alias,
                        event.keyword,
                        event.text,
                        event.dedupe_key,
                        event.observed_at,
                        event.tracking_id,
                        int(event.boundary_uncertain),
                        event.duplicate_of,
                        event.state,
                        now,
                    ),
                )
                if duplicate:
                    cursor.execute(
                        """UPDATE dedupe SET last_seen_at = ?, occurrences = occurrences + 1
                        WHERE dedupe_key = ?""",
                        (now, event.dedupe_key),
                    )
                else:
                    cursor.execute(
                        """INSERT OR REPLACE INTO dedupe(
                            dedupe_key, first_event_id, first_enqueued_at,
                            last_seen_at, occurrences
                        ) VALUES (?, ?, ?, ?, 1)""",
                        (event.dedupe_key, event.id, now, now),
                    )
                    if not local_only:
                        cursor.execute(
                            """INSERT INTO outbox(
                                event_id, payload, created_at, state, next_attempt_at, updated_at
                            ) VALUES (?, ?, ?, 'pending', ?, ?)""",
                            (event.id, json.dumps(payload, ensure_ascii=False), now, now, now),
                        )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            self._bump_records_locked()
            return not duplicate

    @staticmethod
    def event_payload(event: MatchEvent) -> dict[str, Any]:
        return {
            "event_id": event.id,
            "region_id": event.region_id,
            "region_alias": event.region_alias,
            "keyword": event.keyword,
            "observed_at": event.observed_at,
            "text": event.text,
            "boundary_uncertain": event.boundary_uncertain,
        }

    def due_jobs(self, now: float | None = None, limit: int = 20) -> list[NotificationJob]:
        now = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                """SELECT o.*, e.region_id AS source_region_id FROM outbox o
                JOIN events e ON e.id = o.event_id
                WHERE o.state IN ('pending', 'unknown') AND o.retry_exhausted = 0
                AND o.next_attempt_at <= ?
                ORDER BY o.created_at LIMIT ?""",
                (now, limit),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def mark_attempt(self, event_id: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE outbox SET attempts = attempts + 1, updated_at = ? WHERE event_id = ?",
                    (now, event_id),
                )
            self._bump_records_locked()

    def mark_sent(self, event_id: str, now: float | None = None) -> None:
        self._finish(event_id, "sent", "", now)

    def next_part(self, event_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT next_part FROM outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def mark_part_sent(self, event_id: str, next_part: int) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE outbox SET next_part = MAX(next_part, ?) WHERE event_id = ?",
                    (next_part, event_id),
                )
            self._bump_records_locked()

    def mark_unknown_terminal(
        self, event_id: str, error: str, now: float | None = None
    ) -> None:
        """停止自动重试，但不把结果未知宣称为确定失败。"""
        now = time.time() if now is None else now
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """UPDATE outbox SET state = 'unknown', retry_exhausted = 1,
                    last_error = ?, updated_at = ? WHERE event_id = ?""",
                    (error[:500], now, event_id),
                )
                self._conn.execute("UPDATE events SET state = 'unknown' WHERE id = ?", (event_id,))
            self._bump_records_locked()

    def mark_failed(self, event_id: str, error: str, now: float | None = None) -> None:
        self._finish(event_id, "failed", error[:500], now)

    def mark_expired(self, event_id: str, now: float | None = None) -> None:
        self._finish(event_id, "expired", "超过五分钟补发期限", now)

    def schedule_retry(
        self, event_id: str, delay: float, error: str, state: str = "pending", now: float | None = None
    ) -> None:
        now = time.time() if now is None else now
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """UPDATE outbox SET state = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
                    WHERE event_id = ?""",
                    (state, now + delay, error[:500], now, event_id),
                )
                self._conn.execute("UPDATE events SET state = ? WHERE id = ?", (state, event_id))
            self._bump_records_locked()

    def _finish(self, event_id: str, state: str, error: str, now: float | None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """UPDATE outbox SET state = ?, last_error = ?, updated_at = ?
                    WHERE event_id = ?""",
                    (state, error, now, event_id),
                )
                self._conn.execute("UPDATE events SET state = ? WHERE id = ?", (state, event_id))
            self._bump_records_locked()

    def list_events(
        self, limit: int = 200, *, event_kind: str = "all"
    ) -> list[dict[str, Any]]:
        predicates = {"all": "", "system": "WHERE e.region_id = 'system'",
                      "matches": "WHERE e.region_id != 'system'"}
        if event_kind not in predicates:
            raise ValueError("未知记录类型")
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT e.id, e.region_id, e.region_alias, e.keyword, e.text, e.observed_at,
                    e.state, e.boundary_uncertain, e.duplicate_of,
                    o.attempts, o.last_error
                FROM events e LEFT JOIN outbox o ON o.event_id = e.id
                {predicates[event_kind]}
                ORDER BY e.created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_delivery_health(self, limit: int = 128) -> tuple[dict, list[dict]]:
        """Background health manager state; UI polls its in-memory copy instead."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM delivery_health_state WHERE singleton = 1"
            ).fetchone()
            events = self._conn.execute(
                """SELECT payload FROM delivery_health_events WHERE acknowledged = 0
                ORDER BY rowid LIMIT ?""", (limit,)
            ).fetchall()
        return (json.loads(row["payload"]) if row else {},
                [json.loads(item["payload"]) for item in events])

    def save_delivery_health(
        self, snapshot: dict, transitions: list[dict], acknowledgements: list[str]
    ) -> None:
        """State and stable transition IDs commit together, without network I/O."""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO delivery_health_state(singleton, payload) VALUES (1, ?)",
                    (json.dumps(snapshot, ensure_ascii=False),),
                )
                self._conn.executemany(
                    """INSERT OR IGNORE INTO delivery_health_events(event_id, payload, created_at)
                    VALUES (?, ?, ?)""",
                    [(item["event_id"], json.dumps(item, ensure_ascii=False), item["occurred_at"])
                     for item in transitions],
                )
                self._conn.executemany(
                    "UPDATE delivery_health_events SET acknowledged = 1 WHERE event_id = ?",
                    [(event_id,) for event_id in acknowledgements],
                )

    def queue_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS count FROM outbox GROUP BY state"
            ).fetchall()
        return {row["state"]: row["count"] for row in rows}

    def cleanup(self, now: float | None = None, event_days: int = 30) -> None:
        now = time.time() if now is None else now
        cutoff = now - event_days * 86400
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "DELETE FROM dedupe WHERE last_seen_at < ?", (now - 86400,)
                )
                self._conn.execute(
                    "DELETE FROM delivery_health_events WHERE acknowledged = 1 AND created_at < ?",
                    (cutoff,),
                )
                self._conn.execute(
                    """DELETE FROM outbox WHERE created_at < ?
                    AND (state IN ('sent', 'failed', 'expired') OR retry_exhausted = 1)""",
                    (cutoff,),
                )
                self._conn.execute(
                    """DELETE FROM events WHERE created_at < ? AND id NOT IN (
                        SELECT event_id FROM outbox WHERE state IN ('pending', 'unknown')
                        AND retry_exhausted = 0
                    )""",
                    (cutoff,),
                )
            self._bump_records_locked()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> NotificationJob:
        payload = json.loads(row["payload"])
        # The event row is authoritative even for legacy payloads or renamed aliases.
        if "source_region_id" in row.keys():
            payload["region_id"] = row["source_region_id"]
        return NotificationJob(
            event_id=row["event_id"],
            payload=payload,
            created_at=row["created_at"],
            state=row["state"],
            attempts=row["attempts"],
            next_attempt_at=row["next_attempt_at"],
            last_error=row["last_error"],
            updated_at=row["updated_at"],
        )
