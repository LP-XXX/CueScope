"""Persistent delivery incidents, independent of the delivery of health notices.

Only ordinary notification failures and real successful ordinary deliveries enter
this manager. UI reads/polls take a short memory lock and never wait on SQLite.
Acknowledgements are flushed by the notification worker; repeated polls after a
crash retain the same event ID so the caller can enqueue idempotently.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, replace
from uuid import NAMESPACE_URL, uuid4, uuid5

from .storage import Storage


@dataclass(frozen=True, slots=True)
class DeliveryHealthSnapshot:
    state: str = "unknown"
    error: str = ""
    incident_id: str = ""
    revision: int = 0
    changed_at: float = 0.0
    last_failure_at: float = 0.0
    last_success_at: float = 0.0
    last_success_event_id: str = ""


@dataclass(frozen=True, slots=True)
class DeliveryHealthEvent:
    event_id: str
    incident_id: str
    kind: str
    detail: str
    occurred_at: float


class IncidentTracker:
    """One ID per active source incident; a new failure after recovery gets a new ID."""

    def __init__(self):
        self._active: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()

    def begin(self, key: str, detail: str) -> str:
        with self._lock:
            current = self._active.get(key)
            incident_id = current[0] if current else uuid4().hex
            self._active[key] = (incident_id, detail)
            return incident_id

    def recover(self, key: str) -> str | None:
        with self._lock:
            current = self._active.pop(key, None)
            return current[0] if current else None


class DeliveryHealth:
    CACHE_LIMIT = 128

    def __init__(self, storage: Storage):
        self.storage = storage
        state, pending = storage.load_delivery_health(self.CACHE_LIMIT)
        self._snapshot = DeliveryHealthSnapshot(**state) if state else DeliveryHealthSnapshot()
        self._pending = {item["event_id"]: DeliveryHealthEvent(**item) for item in pending}
        self._unsaved: dict[str, DeliveryHealthEvent] = {}
        self._acks: set[str] = set()
        self._dirty = False
        self._lock = threading.Lock()
        self._writer_lock = threading.RLock()

    @property
    def snapshot(self) -> DeliveryHealthSnapshot:
        with self._lock:
            return self._snapshot

    def poll(self, limit: int = 20) -> list[DeliveryHealthEvent]:
        with self._lock:
            return list(self._pending.values())[:max(0, min(limit, self.CACHE_LIMIT))]

    def acknowledge(self, event_id: str) -> None:
        with self._lock:
            if event_id in self._pending:
                self._pending.pop(event_id)
                self._acks.add(event_id)

    def _transition(self, kind: str, detail: str, now: float) -> None:
        incident = self._snapshot.incident_id
        event_id = uuid5(NAMESPACE_URL, f"cuescope:delivery:{incident}:{kind}").hex
        event = DeliveryHealthEvent(event_id, incident, kind, detail, now)
        self._unsaved[event_id] = event
        if len(self._pending) < self.CACHE_LIMIT:
            self._pending[event_id] = event

    def fail(self, detail: str, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        detail = detail[:500]
        with self._writer_lock:
            with self._lock:
                previous = self._snapshot
                new_incident = previous.state != "failed"
                self._snapshot = replace(
                    previous, state="failed", error=detail,
                    incident_id=uuid4().hex if new_incident else previous.incident_id,
                    revision=previous.revision + int(new_incident or previous.error != detail),
                    changed_at=now if new_incident else previous.changed_at,
                    last_failure_at=now,
                )
                if new_incident:
                    self._transition("failed", detail, now)
                self._dirty = True
            self.flush()

    def succeeded(self, event_id: str, *, now: float | None = None) -> None:
        """Caller must have completed a fresh, ordinary business request successfully."""
        now = time.time() if now is None else now
        with self._writer_lock:
            with self._lock:
                previous = self._snapshot
                self._snapshot = replace(
                    previous, state="healthy", error="", last_success_at=now,
                    last_success_event_id=event_id,
                    revision=previous.revision + int(previous.state != "healthy"),
                    changed_at=now if previous.state != "healthy" else previous.changed_at,
                )
                if previous.state == "failed":
                    self._transition(
                        "recovered", "普通通知已获得平台成功响应；历史结果未知的记录仍需核对。", now
                    )
                self._dirty = True
            self.flush()

    def flush(self) -> bool:
        """Worker-only disk I/O; failed writes stay pending and never imply recovery."""
        with self._writer_lock:
            with self._lock:
                if not self._dirty and not self._acks:
                    return True
                snapshot = asdict(self._snapshot)
                unsaved = dict(self._unsaved)
                acks = set(self._acks)
            try:
                self.storage.save_delivery_health(
                    snapshot, [asdict(item) for item in unsaved.values()], list(acks)
                )
                _, pending = self.storage.load_delivery_health(self.CACHE_LIMIT)
            except Exception:  # noqa: BLE001 - keep current failure visible if persistence is broken
                return False
            with self._lock:
                for event_id in unsaved:
                    self._unsaved.pop(event_id, None)
                self._acks.difference_update(acks)
                self._dirty = False
                self._pending = {
                    item["event_id"]: DeliveryHealthEvent(**item) for item in pending
                    if item["event_id"] not in self._acks
                }
            return True
