from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from deal_watch.health import DeliveryHealth
from deal_watch.models import MatchEvent
from deal_watch.notify import FeishuClient, NotificationDispatcher, NotifyError
from deal_watch.storage import Storage


class NoWait:
    def acquire(self):
        pass


class FakeClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def send(self, payload, now=None):
        self.calls.append(payload["event_id"])
        if self.fail:
            raise NotifyError("synthetic rejection")


def record(storage, event_id, *, source="region", delivery_mode="feishu", now=None):
    now = time.time() if now is None else now
    # A namespace also verifies compatibility with older MatchEvent definitions.
    event = SimpleNamespace(
        id=event_id, region_id=source, region_alias="renamed source", keyword="synthetic",
        text="synthetic", dedupe_key=event_id, observed_at=now, tracking_id="",
        boundary_uncertain=False, duplicate_of="", state="pending", delivery_mode=delivery_mode,
    )
    storage.record_match(event, now=now)
    return event


def dispatcher(storage, client):
    return NotificationDispatcher(storage, lambda: client, limiter=NoWait(), max_attempts=1)


def test_health_delivery_neither_creates_failure_nor_recovers_existing_incident():
    with_storage = Storage(":memory:")
    try:
        client = FakeClient(fail=True)
        worker = dispatcher(with_storage, client)
        record(with_storage, "health-failure", source="system")
        worker.process_once()
        assert worker.delivery_health.state == "unknown"
        assert worker.last_error == ""
        assert worker.poll_delivery_health_events() == []

        record(with_storage, "ordinary-failure")
        worker.process_once()
        incident = worker.delivery_health.incident_id
        assert worker.delivery_health.state == "failed"
        client.fail = False
        record(with_storage, "health-success", source="system")
        worker.process_once()
        assert worker.delivery_health.state == "failed"
        assert worker.delivery_health.incident_id == incident
        assert worker.last_error
        assert [event.kind for event in worker.poll_delivery_health_events()] == ["failed"]
    finally:
        with_storage.close()


def test_legacy_health_payload_uses_event_identity_not_display_alias():
    storage = Storage(":memory:")
    try:
        record(storage, "legacy-health", source="system")
        payload = json.loads(storage._conn.execute("SELECT payload FROM outbox").fetchone()[0])
        payload.pop("region_id")
        payload["region_alias"] = "ordinary-looking label"
        storage._conn.execute("UPDATE outbox SET payload = ?", (json.dumps(payload),))
        storage._conn.commit()
        worker = dispatcher(storage, FakeClient(fail=True))
        worker.process_once()
        assert worker.delivery_health.state == "unknown"
        assert worker.poll_delivery_health_events() == []
    finally:
        storage.close()


def test_only_a_new_ordinary_request_can_end_delivery_incident():
    storage = Storage(":memory:")
    try:
        client = FakeClient(fail=True)
        worker = dispatcher(storage, client)
        record(storage, "failure")
        worker.process_once()
        failed = worker.poll_delivery_health_events()[0]
        record(storage, "already-confirmed")
        storage.mark_part_sent("already-confirmed", 1)
        client.fail = False
        worker.process_once()
        assert client.calls == ["failure"]
        assert worker.delivery_health.state == "failed"
        assert [event.kind for event in worker.poll_delivery_health_events()] == ["failed"]

        record(storage, "fresh-success")
        worker.process_once()
        events = worker.poll_delivery_health_events()
        assert [event.kind for event in events] == ["failed", "recovered"]
        assert events[0].incident_id == events[1].incident_id == failed.incident_id
        assert events[0].event_id != events[1].event_id
        assert worker.delivery_health.last_success_event_id == "fresh-success"
        assert worker.delivery_health.state == "healthy"
        assert worker.last_error == ""
    finally:
        storage.close()


def test_failure_and_pending_transition_survive_restart_and_enqueue_is_idempotent(tmp_path):
    path = tmp_path / "notifications.db"
    storage = Storage(path)
    record(storage, "failure")
    first = dispatcher(storage, FakeClient(fail=True))
    first.process_once()
    event = first.poll_delivery_health_events()[0]
    storage.close()

    storage = Storage(path)
    try:
        worker = dispatcher(storage, FakeClient(fail=True))
        assert worker.delivery_health.state == "failed"
        assert worker.poll_delivery_health_events() == [event]
        record(storage, "continued-failure")
        worker.process_once()
        assert worker.poll_delivery_health_events() == [event]
        health_event = MatchEvent(
            id=event.event_id, region_id="system", region_alias="health",
            keyword="failure", text="synthetic", dedupe_key=event.event_id,
            observed_at=event.occurred_at,
        )
        assert storage.record_match(health_event, idempotent=True)
        assert not storage.record_match(health_event, idempotent=True)
        assert len(storage.list_events(event_kind="system")) == 1
        worker.ack_delivery_health_event(event.event_id)
        assert worker.poll_delivery_health_events() == []
        worker.process_once()  # flush ack; failed health delivery cannot change state
        reopened = dispatcher(storage, FakeClient())
        assert reopened.poll_delivery_health_events() == []
        assert reopened.delivery_health.incident_id == event.incident_id
    finally:
        storage.close()


def test_alternating_health_delivery_results_cannot_feed_themselves():
    storage = Storage(":memory:")
    try:
        class AlternatingClient:
            def __init__(self):
                self.kinds = []

            def send(self, payload, now=None):
                kind = payload["keyword"]
                self.kinds.append(kind)
                if kind != "failed":
                    raise NotifyError("synthetic rejection")

        client = AlternatingClient()
        worker = dispatcher(storage, client)
        record(storage, "ordinary-seed")
        created = []
        for _ in range(10):
            worker.process_once()
            for event in worker.poll_delivery_health_events():
                created.append(event.kind)
                notice = MatchEvent(
                    id=event.event_id, region_id="system", region_alias="health",
                    keyword=event.kind, text="synthetic", dedupe_key=event.event_id,
                    observed_at=event.occurred_at,
                )
                storage.record_match(notice, idempotent=True)
                worker.ack_delivery_health_event(event.event_id)
        assert created == ["failed"]
        assert client.kinds == ["synthetic", "failed"]
        assert worker.delivery_health.state == "failed"
    finally:
        storage.close()


def test_local_events_keep_dedupe_and_filter_before_limiting(tmp_path):
    path = tmp_path / "notifications.db"
    storage = Storage(path)
    local = record(storage, "local", delivery_mode="local", now=1000)
    assert local.state == "local"
    assert storage.queue_counts() == {}
    for index in range(3):
        record(storage, f"system-{index}", source="system", delivery_mode="local", now=1001 + index)
    local.id = "duplicate-local"
    assert not storage.record_match(local, now=1004)
    assert local.state == "duplicate"
    storage.close()
    storage = Storage(path)
    try:
        assert storage.due_jobs(now=2000) == []
        assert storage.list_events(limit=1, event_kind="system")[0]["region_id"] == "system"
        matches = storage.list_events(limit=2, event_kind="matches")
        assert [item["id"] for item in matches] == ["duplicate-local", "local"]
        assert matches[1]["state"] == "local"
    finally:
        storage.close()


def test_health_snapshot_and_poll_do_not_wait_on_database_lock():
    storage = Storage(":memory:")
    health = DeliveryHealth(storage)
    health.fail("synthetic")
    held, release = threading.Event(), threading.Event()

    def hold():
        with storage._lock:
            held.set()
            release.wait(2)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert held.wait(2)
    try:
        before = time.monotonic()
        assert health.snapshot.state == "failed"
        event = health.poll()[0]
        health.acknowledge(event.event_id)
        assert time.monotonic() - before < 0.5
    finally:
        release.set()
        thread.join(2)
        storage.close()


def test_failed_health_persistence_is_retried_without_more_outbound_work():
    storage = Storage(":memory:")
    try:
        client = FakeClient(fail=True)
        worker = dispatcher(storage, client)
        record(storage, "first")
        record(storage, "second")
        with patch.object(storage, "save_delivery_health", side_effect=OSError("synthetic")):
            worker.process_once()
            assert worker.delivery_health.state == "failed"
            assert not worker.process_once()
            assert client.calls == ["first"]
        assert worker.process_once()
        assert client.calls == ["first", "second"]
        assert len(worker.poll_delivery_health_events()) == 1
    finally:
        storage.close()


def test_disabled_delivery_does_not_read_outbox_or_change_incident_then_resumes():
    storage = Storage(":memory:")
    try:
        client = FakeClient()
        enabled = [False]
        worker = NotificationDispatcher(
            storage, lambda: client, limiter=NoWait(), delivery_enabled=lambda: enabled[0],
        )
        worker._delivery_failed("previous synthetic failure")
        previous = worker.delivery_health
        previous_events = worker.poll_delivery_health_events()
        record(storage, "waiting-for-opt-in")
        with patch.object(storage, "due_jobs", side_effect=AssertionError("disabled queue was read")):
            assert not worker.process_once()
        assert client.calls == []
        assert storage.queue_counts() == {"pending": 1}
        assert storage.due_jobs()[0].attempts == 0
        assert worker.delivery_health == previous
        assert worker.poll_delivery_health_events() == previous_events

        enabled[0] = True
        assert worker.process_once()
        assert client.calls == ["waiting-for-opt-in"]
        assert storage.queue_counts() == {"sent": 1}
        assert worker.delivery_health.state == "healthy"
        assert [event.kind for event in worker.poll_delivery_health_events()] == ["failed", "recovered"]
    finally:
        storage.close()


def test_delivery_paused_past_ttl_expires_on_resume_without_network_or_false_recovery():
    storage = Storage(":memory:")
    try:
        client = FakeClient()
        enabled = [False]
        worker = NotificationDispatcher(
            storage, lambda: client, limiter=NoWait(), ttl_seconds=300,
            delivery_enabled=lambda: enabled[0],
        )
        record(storage, "old-pending", now=1000)
        with patch("deal_watch.notify.time.time", return_value=1400):
            assert not worker.process_once()
            assert storage.queue_counts() == {"pending": 1}
            enabled[0] = True
            assert worker.process_once()
        assert storage.queue_counts() == {"expired": 1}
        assert client.calls == []
        assert worker.delivery_health.state == "unknown"
        assert worker.poll_delivery_health_events() == []
    finally:
        storage.close()


def test_disabling_delivery_during_rate_limit_wait_prevents_first_request():
    storage = Storage(":memory:")
    try:
        enabled = [True]
        client = FakeClient()

        class DisableWhileWaiting:
            def acquire(self):
                enabled[0] = False

        worker = NotificationDispatcher(
            storage, lambda: client, limiter=DisableWhileWaiting(),
            delivery_enabled=lambda: enabled[0],
        )
        record(storage, "paused-during-wait")
        assert not worker.process_once()
        assert client.calls == []
        assert storage.queue_counts() == {"pending": 1}
        assert storage.due_jobs()[0].attempts == 0
        assert worker.delivery_health.state == "unknown"
        assert worker.poll_delivery_health_events() == []
        assert worker.last_error == ""
    finally:
        storage.close()


def test_disabling_delivery_after_first_part_stops_remaining_parts_without_false_failure():
    storage = Storage(":memory:")
    try:
        enabled = [True]

        class SyntheticMultipartClient(FeishuClient):
            def __init__(self):
                super().__init__("http://127.0.0.1:18080/synthetic")
                self.calls = 0

            def _send_message(self, message):
                self.calls += 1
                if self.calls == 1:
                    enabled[0] = False

        client = SyntheticMultipartClient()
        worker = NotificationDispatcher(
            storage, lambda: client, limiter=NoWait(), delivery_enabled=lambda: enabled[0],
        )
        notice = MatchEvent(
            id="multipart", region_id="ordinary", region_alias="synthetic",
            keyword="synthetic", text="x" * 2500, dedupe_key="multipart", observed_at=time.time(),
        )
        storage.record_match(notice)
        assert not worker.process_once()
        assert client.calls == 1
        assert storage.next_part(notice.id) == 1
        assert storage.queue_counts() == {"pending": 1}
        assert storage.due_jobs()[0].attempts == 1
        assert worker.delivery_health.state == "unknown"
        assert worker.poll_delivery_health_events() == []

        enabled[0] = True
        assert worker.process_once()
        assert client.calls == 3  # resume only the two not-yet-confirmed parts
        assert storage.next_part(notice.id) == 3
        assert storage.queue_counts() == {"sent": 1}
        assert worker.delivery_health.state == "healthy"
        assert worker.poll_delivery_health_events() == []
    finally:
        storage.close()
