from __future__ import annotations

import threading
import time

from deal_watch.health import IncidentTracker
from deal_watch.health_queue import HealthEventWriter
from deal_watch.models import MatchEvent
from deal_watch.storage import Storage


def event(event_id="synthetic-health"):
    return MatchEvent(
        id=event_id, region_id="system", region_alias="health", keyword="synthetic",
        text="synthetic", dedupe_key=event_id, observed_at=time.time(),
    )


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


def test_incident_tracker_keeps_active_source_identity_and_restarts_after_recovery():
    tracker = IncidentTracker()
    incident = tracker.begin("first", "failure")
    assert tracker.begin("first", "new failure detail") == incident
    assert tracker.begin("second", "same failure") != incident
    assert tracker.recover("first") == incident
    assert tracker.recover("first") is None
    assert tracker.begin("first", "a later failure") != incident


def test_submit_never_waits_for_storage_and_coalesces_inflight_ids():
    started, release, saved = threading.Event(), threading.Event(), threading.Event()

    class BlockedStore:
        def __init__(self):
            self.calls = 0

        def record_match(self, value, cooldown, *, idempotent):
            self.calls += 1
            assert idempotent
            started.set()
            release.wait(2)

    store = BlockedStore()
    writer = HealthEventWriter(store, 300, on_saved=lambda value: saved.set(), capacity=1)
    try:
        assert writer.submit(event())
        assert started.wait(2)
        before = time.monotonic()
        assert writer.submit(event())
        assert not writer.submit(event("another"))
        assert time.monotonic() - before < 0.5
        assert writer.last_error
        assert writer.pending_count == 1
        release.set()
        assert saved.wait(2)
        assert store.calls == 1
    finally:
        release.set()
        assert writer.close(2)


def test_failed_write_has_bounded_retries_and_is_retained_until_retry():
    callbacks = []

    class BrokenStore:
        def __init__(self):
            self.calls = 0
            self.fail = True

        def record_match(self, value, cooldown, *, idempotent):
            self.calls += 1
            if self.fail:
                raise OSError("synthetic do-not-echo")

    store = BrokenStore()
    writer = HealthEventWriter(
        store, 300, on_saved=lambda value: callbacks.append(value.id),
        max_attempts=2, retry_delay=0.01,
    )
    try:
        writer.submit(event())
        wait_for(lambda: writer.failed_count == 1)
        assert store.calls == 2
        assert callbacks == []
        assert writer.pending_count == 1
        assert "do-not-echo" not in writer.last_error
        time.sleep(0.04)
        assert store.calls == 2
        store.fail = False
        assert writer.retry_failed() == 1
        wait_for(lambda: callbacks == ["synthetic-health"])
        assert store.calls == 3
        assert writer.pending_count == 0
    finally:
        writer.close(2)


def test_idempotent_existing_event_still_acknowledges_success():
    storage = Storage(":memory:")
    health_event = event()
    storage.record_match(health_event)
    saved = threading.Event()
    writer = HealthEventWriter(storage, 300)
    try:
        writer.submit(health_event, on_saved=lambda value: saved.set())
        assert saved.wait(2)
        assert len(storage.list_events()) == 1
    finally:
        assert writer.close(2)
        storage.close()


def test_shutdown_join_is_bounded_and_reports_active_write():
    entered, release = threading.Event(), threading.Event()

    class BlockedStore:
        def record_match(self, *args, **kwargs):
            entered.set()
            release.wait(2)

    writer = HealthEventWriter(BlockedStore(), 300)
    writer.submit(event())
    assert entered.wait(2)
    try:
        before = time.monotonic()
        assert not writer.close(0.02)
        assert time.monotonic() - before < 0.25
        assert writer.is_alive()
        assert not writer.submit(event("after-close"))
    finally:
        release.set()
        wait_for(lambda: not writer.is_alive())
