"""SQLite 事件、去重索引和持久 outbox。事件与去重占位在同一事务提交。"""

from __future__ import annotations

import copy
import inspect
import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import MatchEvent, NotificationJob
from .quiet_hours import QuietHours

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
"""

_OUTBOX = """
CREATE TABLE IF NOT EXISTS outbox (
    event_id TEXT NOT NULL REFERENCES events(id),
    endpoint_id TEXT NOT NULL,
    route_region_id TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0,
    next_part INTEGER NOT NULL DEFAULT 0,
    retry_exhausted INTEGER NOT NULL DEFAULT 0,
    receipt TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (event_id, endpoint_id)
);
"""

_SCHEMA += """
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
CREATE TABLE IF NOT EXISTS route_health_state (
    endpoint_id TEXT PRIMARY KEY, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quiet_settings (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quiet_buckets (
    endpoint_id TEXT NOT NULL, ends_at REAL NOT NULL, event_count INTEGER NOT NULL,
    PRIMARY KEY(endpoint_id,ends_at)
);
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
        self._owner = self
        self._endpoint_id = "legacy"
        self._routes: Callable[[str], tuple[str, ...]] = lambda _region_id: ("legacy",)
        self._route_filter_enabled = False
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        try:
            old_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(outbox)")}
            if old_columns and "endpoint_id" not in old_columns and self.path != Path(":memory:"):
                self._backup_legacy_database()
            self._conn.executescript(_SCHEMA)
            self._initialize_destinations()
            saved = self._conn.execute("SELECT payload FROM quiet_settings WHERE singleton=1").fetchone()
            self._quiet = QuietHours(**json.loads(saved[0])) if saved else QuietHours()
        except BaseException:
            self._conn.close()
            raise

    def _backup_legacy_database(self) -> None:
        # The SQLite backup API includes committed WAL pages and produces a
        # standalone database that can be restored with the application stopped.
        backup = self.path.with_name(f"{self.path.name}.before-destinations-{uuid4().hex[:12]}.bak")
        temporary = backup.with_suffix(".tmp")
        connection = sqlite3.connect(temporary)
        try:
            try:
                self._conn.backup(connection)
                if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise OSError("通知队列备份校验失败")
            finally:
                connection.close()
            os.replace(temporary, backup)
        finally:
            temporary.unlink(missing_ok=True)

    def _initialize_destinations(self) -> None:
        # DDL and copying rows must commit together, including interrupted upgrades.
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(outbox)")}
            old = bool(columns and "endpoint_id" not in columns)
            if old:
                self._conn.execute("ALTER TABLE outbox RENAME TO outbox_before_routes")
            self._conn.execute(_OUTBOX)
            if columns and not old:
                for name in ("next_part", "retry_exhausted"):
                    if name not in columns:
                        self._conn.execute(f"ALTER TABLE outbox ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
                if "route_region_id" not in columns:
                    self._conn.execute("ALTER TABLE outbox ADD COLUMN route_region_id TEXT NOT NULL DEFAULT ''")
            if old:
                names = ["event_id", "payload", "created_at", "state", "attempts",
                         "next_attempt_at", "last_error", "updated_at", "next_part", "retry_exhausted"]
                fields = ", ".join(name for name in names if name in columns)
                self._conn.execute(
                    f"INSERT INTO outbox(endpoint_id, {fields}) SELECT 'legacy-unbound', {fields} FROM outbox_before_routes"
                )
                # No old row identifies its original recipient. Keep all progress and
                # the original state. Never deliver an old notice to a new recipient.
                self._conn.execute("""UPDATE outbox SET state='unknown', retry_exhausted=1,
                    last_error='历史投递缺少原接收端标识，请人工核对'
                    WHERE endpoint_id='legacy-unbound' AND state IN ('pending','unknown')""")
                self._conn.execute("""UPDATE events SET state='unknown'
                    WHERE id IN (SELECT event_id FROM outbox WHERE endpoint_id='legacy-unbound' AND state='unknown')""")
                self._conn.execute("DROP TABLE outbox_before_routes")
            self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_route_due
                ON outbox(endpoint_id,state,next_attempt_at)""")
            health_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(delivery_health_events)")}
            if "endpoint_id" not in health_cols:
                self._conn.execute("ALTER TABLE delivery_health_events ADD COLUMN endpoint_id TEXT NOT NULL DEFAULT 'legacy'")
            self._conn.execute("""INSERT OR IGNORE INTO route_health_state(endpoint_id, payload)
                SELECT 'legacy', payload FROM delivery_health_state WHERE singleton=1""")

    def set_notification_routes(self, routes: Callable[..., tuple[str, ...]]) -> None:
        try:
            inspect.signature(routes).bind("region")
        except TypeError:
            inspect.signature(routes).bind()
            def by_region(_region_id):
                return routes()
        else:
            by_region = routes
        with self._lock:
            self._owner._routes = by_region
            self._owner._route_filter_enabled = True

    def for_destination(self, endpoint_id: str) -> Storage:
        """Get a dispatcher's view of this connection; the owning Storage closes it."""
        if not isinstance(endpoint_id, str) or not endpoint_id or endpoint_id in ("legacy-unbound", "@local"):
            raise ValueError("通知目标无效")
        scoped = copy.copy(self)
        scoped._endpoint_id = endpoint_id
        return scoped

    @property
    def quiet_hours(self) -> QuietHours:
        return self._owner._quiet

    def local_notifications_muted(self, now: float | None = None) -> bool:
        return self.quiet_hours.ends_at(time.time() if now is None else now) is not None

    def save_quiet_hours(self, hours: QuietHours) -> None:
        if not isinstance(hours, QuietHours):
            raise ValueError("免打扰设置无效")
        with self._lock:
            with self._conn:
                self._conn.execute("INSERT OR REPLACE INTO quiet_settings VALUES (1,?)",
                                   (json.dumps(asdict(hours)),))
                if not hours.enabled or not hours.summary:
                    self._conn.execute("DELETE FROM quiet_buckets")
            self._owner._quiet = hours

    def _add_to_digest(self, endpoint_id: str, ends_at: float) -> None:
        self._conn.execute("""INSERT INTO quiet_buckets VALUES (?,?,1)
            ON CONFLICT(endpoint_id,ends_at) DO UPDATE SET event_count=event_count+1""", (endpoint_id, ends_at))

    def _release_digest(self, endpoint_id: str, now: float, ttl_seconds: int) -> int:
        if not self.quiet_hours.summary or self.local_notifications_muted(now):
            return 0
        with self._lock:
            with self._conn:
                bucket = self._conn.execute("""SELECT ends_at,event_count FROM quiet_buckets
                    WHERE endpoint_id=? AND ends_at<=? ORDER BY ends_at LIMIT 1""", (endpoint_id, now)).fetchone()
                if not bucket:
                    return 0
                count = bucket["event_count"]
                self._conn.execute("DELETE FROM quiet_buckets WHERE endpoint_id=? AND ends_at=?",
                                   (endpoint_id, bucket["ends_at"]))
                if now - bucket["ends_at"] > ttl_seconds:
                    return 0
                if endpoint_id != "@local":
                    event = MatchEvent("system", "免打扰摘要", "免打扰期间命中", f"共记录 {count} 条命中，可在本地活动记录查看。",
                                       "", bucket["ends_at"])
                    self._conn.execute("""INSERT INTO events(id,region_id,region_alias,keyword,text,dedupe_key,
                        observed_at,tracking_id,state,created_at) VALUES (?,?,?,?,?,?,?,'','pending',?)""",
                        (event.id, event.region_id, event.region_alias, event.keyword, event.text, "",
                         event.observed_at, now))
                    self._conn.execute("""INSERT INTO outbox(event_id,endpoint_id,payload,created_at,state,next_attempt_at)
                        VALUES (?,?,?,?,'pending',?)""", (event.id, endpoint_id, json.dumps(dict(self.event_payload(event), digest_count=count)),
                                                          bucket["ends_at"], now))
            self._bump_records_locked()
            return count

    def flush_local_digest(self, now: float | None = None, ttl_seconds: int = 300) -> int:
        return self._release_digest("@local", time.time() if now is None else now, ttl_seconds)

    def flush_destination_digest(self, now: float, ttl_seconds: int) -> int:
        return self._release_digest(self._endpoint_id, now, ttl_seconds)

    def suppress_during_quiet(self, job: NotificationJob, now: float) -> bool:
        """Handle a job that crossed into quiet hours while queued or rate-limited."""
        end = self.quiet_hours.ends_at(now)
        if end is None:
            return False
        with self._lock:
            with self._conn:
                row = self._conn.execute("""SELECT state,attempts,receipt,next_part FROM outbox
                    WHERE event_id=? AND endpoint_id=?""", (job.event_id, self._endpoint_id)).fetchone()
                if not row:
                    return True
                if row["attempts"] or row["receipt"] or row["next_part"]:
                    self._conn.execute("UPDATE outbox SET next_attempt_at=? WHERE event_id=? AND endpoint_id=?",
                                       (end, job.event_id, self._endpoint_id))
                elif row["state"] == "pending":
                    summarize = self.quiet_hours.summary and job.payload.get("region_id") != "system"
                    if summarize:
                        self._add_to_digest(self._endpoint_id, end)
                    self._conn.execute("""UPDATE outbox SET state=?, retry_exhausted=1, updated_at=?
                        WHERE event_id=? AND endpoint_id=?""",
                        ("digest_counted" if summarize else "muted", now, job.event_id, self._endpoint_id))
                    self._refresh_event_state(job.event_id)
            self._bump_records_locked()
        return True

    @property
    def records_revision(self) -> int:
        """记录表的数据版本号；界面据此判断"要不要重查一次"。

        单调递增，只在本进程内有效。读它不查库，也**不碰** SQLite 事务锁：
        只抢保护计数器的短锁，后台写库再慢也不会把界面心跳卡住。
        """
        with self._revision_lock:
            return self._owner._records_revision

    def _bump_records_locked(self) -> None:
        """在写入已提交后调用；只取短锁，可以在持有 ``self._lock`` 时调用。"""
        with self._revision_lock:
            self._owner._records_revision += 1

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
                source = getattr(event, "route_region_id", "") or event.region_id
                routes = () if event.delivery_mode == "local" else tuple(dict.fromkeys(self._owner._routes(source)))
                if any(not isinstance(key, str) or not key or len(key) > 128 or key in ("legacy-unbound", "@local") for key in routes):
                    raise ValueError("通知目标无效")
                local_only = not routes
                end = self.quiet_hours.ends_at(now)
                summarize = end is not None and self.quiet_hours.summary and event.region_id != "system"
                initial_state = ("digest_counted" if summarize else "muted") if end is not None else "pending"
                event.state = "local" if local_only and end is None else initial_state
                if duplicate:
                    event.state = "duplicate"
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
                        cursor.executemany(
                            """INSERT INTO outbox(
                                event_id, endpoint_id, route_region_id, payload, created_at, state, next_attempt_at, updated_at, retry_exhausted
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            [(event.id, key, source, json.dumps(payload, ensure_ascii=False), now, initial_state,
                              now, now, int(end is not None)) for key in routes],
                        )
                    if summarize:
                        for key in ("@local", *routes):
                            self._add_to_digest(key, end)
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
                WHERE o.endpoint_id = ? AND o.state IN ('pending', 'unknown', 'submitting', 'forwarded') AND o.retry_exhausted = 0
                AND o.next_attempt_at <= ?
                ORDER BY o.created_at LIMIT ?""",
                (self._endpoint_id, now, limit),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def mark_attempt(self, event_id: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE outbox SET attempts = attempts + 1, updated_at = ? WHERE event_id = ? AND endpoint_id = ?",
                    (now, event_id, self._endpoint_id),
                )
            self._bump_records_locked()

    def _revoke_locked(self, event_id: str, endpoint_id: str) -> None:
        self._conn.execute("""UPDATE outbox SET state=CASE
            WHEN receipt!='' THEN 'forwarded' WHEN next_part>0 THEN 'partial'
            WHEN state IN ('unknown','submitting') THEN 'unknown' ELSE 'revoked' END,
            retry_exhausted=1, last_error='接收端授权已撤销，停止后续投递', updated_at=?
            WHERE event_id=? AND endpoint_id=? AND retry_exhausted=0
            AND state IN ('pending','unknown','submitting','forwarded')""", (time.time(), event_id, endpoint_id))
        self._refresh_event_state(event_id)

    def revoke_unselected_deliveries(self, routes_for_region: Callable[[str], tuple[str, ...]]) -> None:
        """Cancel outgoing jobs removed by an explicit routing edit, including delayed retries."""
        with self._lock:
            with self._conn:
                rows = self._conn.execute("""SELECT o.event_id, o.endpoint_id,
                    COALESCE(NULLIF(o.route_region_id,''),e.region_id) AS source
                    FROM outbox o JOIN events e ON e.id=o.event_id WHERE o.retry_exhausted=0
                    AND o.state IN ('pending','unknown','submitting','forwarded')""").fetchall()
                allowed = {source: set(routes_for_region(source)) for source in {row["source"] for row in rows}}
                revoked = [row for row in rows if row["endpoint_id"] not in allowed[row["source"]]]
                for row in revoked:
                    self._revoke_locked(row["event_id"], row["endpoint_id"])
            if revoked:
                self._bump_records_locked()

    def cancel_if_unauthorized(self, job: NotificationJob) -> bool:
        """Evaluate current permissions again while waiting, including between parts."""
        if not self._owner._route_filter_enabled:
            return False
        source = job.route_region_id or job.payload.get("region_id", "")
        with self._lock:
            if self._endpoint_id in self._owner._routes(source):
                row = self._conn.execute("SELECT retry_exhausted FROM outbox WHERE event_id=? AND endpoint_id=?",
                                         (job.event_id, self._endpoint_id)).fetchone()
                return not row or bool(row[0])
            with self._conn:
                self._revoke_locked(job.event_id, self._endpoint_id)
            self._bump_records_locked()
            return True

    def mark_sent(self, event_id: str, now: float | None = None) -> None:
        self._finish(event_id, "sent", "", now)

    def next_part(self, event_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT next_part FROM outbox WHERE event_id = ? AND endpoint_id = ?", (event_id, self._endpoint_id)
            ).fetchone()
        return int(row[0]) if row else 0

    def mark_part_sent(self, event_id: str, next_part: int) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE outbox SET next_part = MAX(next_part, ?) WHERE event_id = ? AND endpoint_id = ?",
                    (next_part, event_id, self._endpoint_id),
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
                    last_error = ?, updated_at = ? WHERE event_id = ? AND endpoint_id=?""",
                    (error[:500], now, event_id, self._endpoint_id),
                )
                self._refresh_event_state(event_id)
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
                    WHERE event_id = ? AND endpoint_id=? AND retry_exhausted=0""",
                    (state, now + delay, error[:500], now, event_id, self._endpoint_id),
                )
                self._refresh_event_state(event_id)
            self._bump_records_locked()

    def _finish(self, event_id: str, state: str, error: str, now: float | None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """UPDATE outbox SET state = ?, last_error = ?, updated_at = ?
                    WHERE event_id = ? AND endpoint_id=?""",
                    (state, error, now, event_id, self._endpoint_id),
                )
                self._refresh_event_state(event_id)
            self._bump_records_locked()

    def _refresh_event_state(self, event_id: str) -> None:
        states = {row[0] for row in self._conn.execute("SELECT state FROM outbox WHERE event_id=?", (event_id,))}
        if states:
            state = next(iter(states)) if len(states) == 1 else "partial"
            self._conn.execute("UPDATE events SET state=? WHERE id=?", (state, event_id))

    def mark_submitting(self, event_id: str) -> None:
        """Persist the intent before a relay request whose result cannot be replayed."""
        self._finish(event_id, "submitting", "", None)

    def save_receipt(self, event_id: str, receipt: str) -> None:
        if not isinstance(receipt, str) or not receipt.strip() or len(receipt) > 1024:
            raise ValueError("中转流水号无效")
        with self._lock:
            with self._conn:
                current = self._conn.execute("SELECT receipt FROM outbox WHERE event_id=? AND endpoint_id=?",
                                             (event_id, self._endpoint_id)).fetchone()
                if current is None or (current[0] and current[0] != receipt):
                    raise ValueError("中转流水号不一致")
                self._conn.execute("""UPDATE outbox SET receipt=?, state='forwarded', last_error='', updated_at=?
                    WHERE event_id=? AND endpoint_id=?""", (receipt, time.time(), event_id, self._endpoint_id))
                self._refresh_event_state(event_id)
            self._bump_records_locked()

    def finish_forwarded(self, event_id: str, detail: str) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute("""UPDATE outbox SET state='forwarded', retry_exhausted=1, last_error=?, updated_at=?
                    WHERE event_id=? AND endpoint_id=?""", (detail[:500], time.time(), event_id, self._endpoint_id))
                self._refresh_event_state(event_id)
            self._bump_records_locked()

    def list_deliveries(self, event_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("""SELECT endpoint_id, state, attempts, last_error, next_part, retry_exhausted
                FROM outbox WHERE event_id=? ORDER BY endpoint_id""", (event_id,)).fetchall()
        return [dict(row) for row in rows]

    def expire_unavailable_routes(self, active_endpoint_ids, ttl_seconds: int, now: float | None = None) -> None:
        """Finalize aged, unsendable jobs without contacting removed recipients."""
        now = time.time() if now is None else now
        active = set(active_endpoint_ids)
        with self._lock:
            with self._conn:
                rows = self._conn.execute("""SELECT event_id, endpoint_id, state, receipt FROM outbox
                    WHERE retry_exhausted=0 AND state IN ('pending','unknown','forwarded','submitting')
                    AND created_at < ?""", (now - ttl_seconds,)).fetchall()
                changed = set()
                for row in rows:
                    if row["endpoint_id"] in active:
                        continue
                    state = ("forwarded" if row["receipt"] else
                             "unknown" if row["state"] in ("unknown", "submitting") else "expired")
                    self._conn.execute("""UPDATE outbox SET state=?, retry_exhausted=1,
                        last_error='接收端未启用，已超过投递期限', updated_at=? WHERE event_id=? AND endpoint_id=?""",
                        (state, now, row["event_id"], row["endpoint_id"]))
                    changed.add(row["event_id"])
                for event_id in changed:
                    self._refresh_event_state(event_id)
            if changed:
                self._bump_records_locked()

    def list_events(
        self, limit: int = 200, *, event_kind: str = "all", search=None
    ) -> list[dict[str, Any]]:
        predicates = {"all": "", "system": "e.region_id = 'system'",
                      "matches": "e.region_id != 'system'"}
        if event_kind not in predicates:
            raise ValueError("未知记录类型")
        clauses, values = search.conditions() if search else ([], [])
        if predicates[event_kind]:
            clauses.append(predicates[event_kind])
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT e.id, e.region_id, e.region_alias, e.keyword, e.text, e.observed_at,
                    e.state, e.boundary_uncertain, e.duplicate_of,
                    (SELECT SUM(o.attempts) FROM outbox o WHERE o.event_id=e.id) AS attempts,
                    (SELECT o.last_error FROM outbox o WHERE o.event_id=e.id ORDER BY (o.last_error!='') DESC, o.updated_at DESC LIMIT 1) AS last_error
                FROM events e
                {where}
                ORDER BY e.created_at DESC LIMIT ?""",
                (*values, limit),
            ).fetchall()
            result = [dict(row, deliveries=[]) for row in rows]
            by_id = {row["id"]: row["deliveries"] for row in result}
            ids = list(by_id)
            # Limit bind parameters on SQLite builds that allow only 999.
            for offset in range(0, len(ids), 500):
                batch = ids[offset:offset + 500]
                slots = ",".join("?" for _ in batch)
                details = self._conn.execute(f"""SELECT event_id, endpoint_id, state, attempts,
                    next_part, last_error, retry_exhausted FROM outbox
                    WHERE event_id IN ({slots}) ORDER BY event_id, endpoint_id""", batch)
                for item in details:
                    delivery = dict(item)
                    by_id[delivery.pop("event_id")].append(delivery)
        return result

    def load_delivery_health(self, limit: int = 128) -> tuple[dict, list[dict]]:
        """Background health manager state; UI polls its in-memory copy instead."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM route_health_state WHERE endpoint_id=?", (self._endpoint_id,)
            ).fetchone()
            events = self._conn.execute(
                """SELECT payload FROM delivery_health_events WHERE acknowledged = 0 AND endpoint_id=?
                ORDER BY rowid LIMIT ?""", (self._endpoint_id, limit)
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
                    "INSERT OR REPLACE INTO route_health_state(endpoint_id, payload) VALUES (?, ?)",
                    (self._endpoint_id, json.dumps(snapshot, ensure_ascii=False)),
                )
                self._conn.executemany(
                    """INSERT OR IGNORE INTO delivery_health_events(event_id, endpoint_id, payload, created_at)
                    VALUES (?, ?, ?, ?)""",
                    [(item["event_id"], self._endpoint_id, json.dumps(item, ensure_ascii=False), item["occurred_at"])
                     for item in transitions],
                )
                self._conn.executemany(
                    "UPDATE delivery_health_events SET acknowledged = 1 WHERE event_id = ? AND endpoint_id=?",
                    [(event_id, self._endpoint_id) for event_id in acknowledgements],
                )

    def queue_counts(self) -> dict[str, int]:
        with self._lock:
            if self is self._owner:
                rows = self._conn.execute("SELECT state, COUNT(*) AS count FROM outbox GROUP BY state").fetchall()
            else:
                rows = self._conn.execute("""SELECT state, COUNT(*) AS count FROM outbox
                    WHERE endpoint_id=? GROUP BY state""", (self._endpoint_id,)).fetchall()
        return {row["state"]: row["count"] for row in rows}

    def cleanup(self, now: float | None = None, event_days: int = 30) -> None:
        now = time.time() if now is None else now
        cutoff = now - event_days * 86400
        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM quiet_buckets WHERE ends_at < ?", (cutoff,))
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
                        SELECT event_id FROM outbox
                    )""",
                    (cutoff,),
                )
            self._bump_records_locked()

    def close(self) -> None:
        if self is not self._owner:
            return
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
            receipt=row["receipt"],
            next_part=row["next_part"],
            route_region_id=row["route_region_id"] or payload.get("region_id", ""),
        )
