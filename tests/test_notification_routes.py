"""In-memory receivers and private synthetic databases only."""
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile

import pytest

from deal_watch.config import AppConfig
from deal_watch.diagnostics import HitImageStore
from deal_watch.models import MatchEvent
from deal_watch.notify import NotificationDispatcher, NotifyError, UnknownNotifyError
from deal_watch.qq_notify import PushPlusClient, QQTarget
from deal_watch.quiet_hours import QuietHours
from deal_watch.security import DestinationCredentialStore, destination_id
from deal_watch.storage import Storage
from deal_watch.support_bundle import support_counts, write_support_bundle


def make_event(key="sample"):
    return MatchEvent("task", "synthetic", "match", "synthetic text", key, 100, id=key)


class Receiver:
    def __init__(self, reject=False):
        self.calls = []
        self.reject = reject

    def send(self, payload, **_kwargs):
        self.calls.append(payload["event_id"])
        if self.reject:
            raise NotifyError("synthetic refusal")


class NoWait:
    def acquire(self):
        pass


def worker(store, target, receiver):
    return NotificationDispatcher(store.for_destination(target), lambda: receiver, limiter=NoWait())


def test_one_event_per_distinct_match_and_independent_destinations():
    with closing(Storage(":memory:")) as store, patch("deal_watch.notify.time.time", return_value=100):
        targets = ["a", "b"]
        store.set_notification_routes(lambda region_id: targets if region_id == "task" else ())
        assert store.record_match(make_event(), now=100)
        dupe = make_event("same-text")
        dupe.dedupe_key = "sample"
        assert not store.record_match(dupe, now=100)
        targets.append("c")
        good, bad, redirected = Receiver(), Receiver(reject=True), Receiver()
        original_a, original_b = worker(store, "a", good), worker(store, "b", bad)
        assert not worker(store, "c", redirected).process_once()
        assert original_b.process_once()
        assert original_a.process_once()
        assert original_b.delivery_health.state == "failed"
        assert original_a.delivery_health.state == "healthy"
        detail = next(row for row in store.list_events() if row["id"] == "sample")
        assert detail["state"] == "partial"
        assert [(row["endpoint_id"], row["state"]) for row in detail["deliveries"]] == [("a", "sent"), ("b", "failed")]
        assert good.calls == bad.calls == ["sample"] and not redirected.calls
        assert not store.list_deliveries("same-text")
        assert not original_a.process_once()


class Relay:
    def __init__(self):
        self.calls = []
        self.result = "forwarded"

    def send_tracked(self, payload, *, now, before_request, receipt, on_receipt):
        before_request()
        self.calls.append(receipt)
        if not receipt and self.result == "unknown":
            raise UnknownNotifyError("unknown")
        if not receipt:
            on_receipt("synthetic-reference")
        if self.result == "no-permission":
            raise NotifyError("unavailable")
        return self.result


def test_relay_restarts_query_only_and_honors_ttl(tmp_path):
    path = tmp_path / "synthetic.db"
    relay = Relay()
    with closing(Storage(path)) as store, patch("deal_watch.notify.time.time", return_value=100):
        store.set_notification_routes(lambda: ("route",))
        store.record_match(make_event(), now=100)
        assert worker(store, "route", relay).process_once()
        assert store.list_deliveries("sample")[0]["state"] == "forwarded"
    with closing(Storage(path)) as store, patch("deal_watch.notify.time.time", return_value=106):
        assert worker(store, "route", relay).process_once()
        assert relay.calls == ["", "synthetic-reference"]
        assert store.list_deliveries("sample")[0]["attempts"] == 1
    with closing(Storage(path)) as store, patch("deal_watch.notify.time.time", return_value=410):
        assert worker(store, "route", relay).process_once()
        assert store.list_deliveries("sample")[0]["retry_exhausted"] == 1
        assert store.list_deliveries("sample")[0]["state"] == "forwarded"
        assert len(relay.calls) == 2


def test_unknown_submission_and_restart_mid_request_never_resubmit():
    relay = Relay()
    relay.result = "unknown"
    with closing(Storage(":memory:")) as store, patch("deal_watch.notify.time.time", return_value=100):
        store.set_notification_routes(lambda: ("r",))
        store.record_match(make_event(), now=100)
        w = worker(store, "r", relay)
        assert w.process_once() and not w.process_once()
        assert store.list_events()[0]["state"] == "unknown"
        store.record_match(make_event("interrupted"), now=100)
        store.for_destination("r").mark_submitting("interrupted")
        assert w.process_once() and not w.process_once()
        assert relay.calls == [""]
        assert store.list_deliveries("interrupted")[0]["state"] == "unknown"


def test_expire_retired_route_preserves_accepted_receipt():
    with closing(Storage(":memory:")) as store:
        store.set_notification_routes(lambda: ("a", "b"))
        store.record_match(make_event(), now=100)
        store.for_destination("a").save_receipt("sample", "reference")
        store.expire_unavailable_routes([], 300, now=401)
        rows = store.list_deliveries("sample")
        assert [r["state"] for r in rows] == ["forwarded", "expired"]
        assert all(r["retry_exhausted"] for r in rows)


def test_task_health_uses_task_routes_and_remains_system_event():
    with closing(Storage(":memory:")) as store:
        store.set_notification_routes(lambda source: () if source == "private-task" else ("global",))
        event = make_event()
        event.region_id = "system"
        event.route_region_id = "private-task"
        assert store.record_match(event, now=100)
        assert not store.list_deliveries("sample")
        assert store.list_events(event_kind="system")[0]["state"] == "local"


def test_task_revocation_during_rate_wait_stops_attempt_and_is_durable():
    allowed = {"task": ["a"]}
    receiver = Receiver()
    with closing(Storage(":memory:")) as store, patch("deal_watch.notify.time.time", return_value=100):
        store.set_notification_routes(lambda source: allowed.get(source, ()))
        store.record_match(make_event(), now=100)
        scoped = store.for_destination("a")
        class RemoveTask:
            def acquire(self):
                allowed["task"] = []
        w = NotificationDispatcher(scoped, lambda: receiver, limiter=RemoveTask())
        assert not w.process_once()
        assert not receiver.calls
        assert store.list_deliveries("sample")[0]["state"] == "revoked"
        allowed["task"] = ["a"]
        assert not w.process_once()


def test_pending_retry_revoked_by_snapshot_stays_revoked_after_reenable():
    with closing(Storage(":memory:")) as store:
        store.set_notification_routes(lambda source: ("a",))
        store.record_match(make_event(), now=100)
        scoped = store.for_destination("a")
        scoped.schedule_retry("sample", 60, "test", now=100)
        store.revoke_unselected_deliveries(lambda source: ())
        assert not scoped.due_jobs(now=200)
        assert store.list_deliveries("sample")[0]["state"] == "revoked"


def test_v4_database_backup_and_unbound_queue(tmp_path):
    path = tmp_path / "old.db"
    with closing(Storage(path)) as store:
        store.record_match(make_event(), now=100)
        store.mark_attempt("sample")
        store.mark_part_sent("sample", 1)
    # Rebuild only the original single-receiver table, with real persisted progress.
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript("""ALTER TABLE outbox RENAME TO sample_new;
            CREATE TABLE outbox AS SELECT event_id,payload,created_at,state,attempts,next_attempt_at,
                last_error,updated_at,next_part,retry_exhausted FROM sample_new;
            DROP TABLE sample_new;""")
    with closing(Storage(path)) as store:
        row = store.list_deliveries("sample")[0]
        assert (row["endpoint_id"], row["state"], row["next_part"], row["attempts"]) == ("legacy-unbound", "unknown", 1, 1)
        assert not store.for_destination("new").due_jobs(now=101)
    backup, = tmp_path.glob("*.bak")
    with closing(sqlite3.connect(backup)) as conn:
        assert conn.execute("SELECT state,attempts,next_part FROM outbox").fetchone() == ("pending", 1, 1)


def test_destination_identity_and_protected_store(tmp_path):
    assert destination_id("relay", "a") == destination_id("relay", "a")
    assert destination_id("relay", "a") != destination_id("relay", "b")
    vault = DestinationCredentialStore(tmp_path)
    with patch("deal_watch.security.protect", side_effect=lambda value: value[::-1]), \
         patch("deal_watch.security.unprotect", side_effect=lambda value: value[::-1]):
        vault.save({"token": "synthetic-value"})
        assert vault.load() == {"token": "synthetic-value"}
        assert b"synthetic-value" not in vault.path.read_bytes()
        with patch("deal_watch.security.protect", side_effect=OSError), pytest.raises(OSError):
            vault.save({"token": "other"})
        assert vault.load() == {"token": "synthetic-value"}


def test_formerly_local_task_configuration_missing_is_not_a_new_install(tmp_path):
    vault = DestinationCredentialStore(tmp_path)
    assert vault.load() == {}
    with patch("deal_watch.security.protect", side_effect=lambda value: value[::-1]), \
         patch("deal_watch.security.unprotect", side_effect=lambda value: value[::-1]):
        rules = {"targets": [], "tasks": {"private": [], "other": ["qq-receiver"]}}
        vault.save(rules)
        assert vault.load() == rules
        vault.path.unlink()
        with pytest.raises(OSError, match="缺失"):
            vault.load()
        assert vault.marker.read_bytes() == b"1\n"


def test_cleanup_aged_images_even_when_capture_disabled(tmp_path):
    assert not AppConfig().save_hit_images
    hits = HitImageStore(tmp_path, enabled=False)
    hits.directory.mkdir()
    previous = hits.directory / "synthetic.png"
    previous.write_bytes(b"sample")
    os.utime(previous, (1, 1))
    hits.cleanup()
    assert not previous.exists()


def test_cross_midnight_digest_only_counts_and_survives_restart(tmp_path):
    start = datetime(2026, 1, 7, 22, 0).timestamp()
    end = datetime(2026, 1, 8, 8, 0).timestamp()
    settings = QuietHours(True, 22 * 60, 8 * 60, True)
    assert settings.ends_at(start) == end
    assert settings.ends_at(end) is None
    assert settings.ends_at(end - 1) == end
    path = tmp_path / "demo.db"
    with closing(Storage(path)) as store:
        store.save_quiet_hours(settings)
        store.set_notification_routes(lambda: ("target",))
        for index in range(2):
            store.record_match(make_event(str(index)), now=start)
        assert all(row["state"] == "digest_counted" for row in store.list_events())
        assert not store.for_destination("target").due_jobs(now=end - 1)
    with closing(Storage(path)) as store, patch("deal_watch.notify.time.time", return_value=end + 1):
        assert store.quiet_hours == settings
        assert store.flush_local_digest() == 2 and store.flush_local_digest() == 0
        receiver = Receiver()
        w = worker(store, "target", receiver)
        assert w.process_once() and not w.process_once()
        assert len(receiver.calls) == 1
        summary = store.list_events(event_kind="system")[0]
        assert "2 条" in summary["text"] and "synthetic text" not in summary["text"]
        assert summary["state"] == "sent"
        assert w.delivery_health.state == "unknown"


def test_quiet_hours_catch_jobs_queued_before_start_and_ignore_expired_digests():
    start = datetime(2026, 1, 7, 12, 0).timestamp()
    end = datetime(2026, 1, 7, 13, 0).timestamp()
    with closing(Storage(":memory:")) as store:
        store.set_notification_routes(lambda: ("a",))
        store.save_quiet_hours(QuietHours(True, 720, 780, True))
        store.record_match(make_event(), now=start - 1)
        receiver = Receiver()
        with patch("deal_watch.notify.time.time", return_value=start):
            assert not worker(store, "a", receiver).process_once()
        assert not receiver.calls
        assert store.list_events()[0]["state"] == "digest_counted"
        with patch("deal_watch.notify.time.time", return_value=end + 301):
            assert not worker(store, "a", receiver).process_once()
        assert not receiver.calls and not store.flush_local_digest(now=end + 301)


def test_actual_pushplus_no_query_key_never_resubmits():
    target = QQTarget(kind="pushplus", alias="demo", token="synthetic", scope="user")
    client = PushPlusClient(target)
    with closing(Storage(":memory:")) as store, patch("deal_watch.qq_notify._request",
            return_value={"code": 200, "data": "TestReference123"}) as http:
        store.set_notification_routes(lambda: (target.id,))
        store.record_match(make_event(), now=100)
        with patch("deal_watch.notify.time.time", return_value=101):
            assert worker(store, target.id, client).process_once()
        with patch("deal_watch.notify.time.time", return_value=110):
            w = worker(store, target.id, client)
            assert w.process_once() and not w.process_once()
        assert http.call_count == 1
        row = store.list_deliveries("sample")[0]
        assert row["state"] == "forwarded" and row["retry_exhausted"] == 1


def test_diagnostic_bundle_contains_only_generated_counts(tmp_path):
    secret = "private-title-path-hook"
    regions = [SimpleNamespace(enabled=True, capture_backend=secret, content_mode="generic_line", alias=secret)]
    counts = support_counts(regions, [SimpleNamespace(capture_error_stage=secret)])
    counts["extra"] = secret
    counts["capture_backends"][secret] = 1
    path = tmp_path / "report.zip"
    write_support_bundle(path, counts, {secret: 1, "pending": 2})
    with ZipFile(path) as archive:
        assert archive.namelist() == ["diagnostics.json"]
        data = archive.read("diagnostics.json").decode()
        assert secret not in data
        assert json.loads(data)["delivery_states"] == {"pending": 2}
