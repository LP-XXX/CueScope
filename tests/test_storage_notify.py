from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from deal_watch.models import MatchEvent, MessageCandidate, Rect
from deal_watch.notify import (
    FeishuClient,
    NotificationDispatcher,
    NotifyError,
    RateLimiter,
    UnknownNotifyError,
    build_messages,
    feishu_sign,
)
from deal_watch.recognition import RecognitionStore
from deal_watch.storage import Storage

MOCK_WEBHOOK = "http://127.0.0.1:18080/webhook"


class Response:
    def __init__(self, body=b'{"code":0}'):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size):
        return self.body


class NoWaitLimiter:
    def __init__(self):
        self.calls = 0

    def acquire(self):
        self.calls += 1


def event(event_id="e1", text="手机 4999 元"):
    return MatchEvent(
        id=event_id,
        region_id="r1",
        region_alias="测试群",
        keyword="手机",
        text=text,
        dedupe_key=hashlib.sha256(text.encode()).hexdigest(),
        observed_at=1000.0,
    )


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "outbox.db"

    def tearDown(self):
        self.temp.cleanup()

    def test_event_and_outbox_survive_restart(self):
        storage = Storage(self.path)
        self.assertTrue(storage.record_match(event(), now=1000))
        storage.close()
        storage = Storage(self.path)
        jobs = storage.due_jobs(now=1001)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].event_id, "e1")
        storage.close()

    def test_cooldown_starts_at_first_enqueue_and_does_not_extend(self):
        storage = Storage(self.path)
        storage.record_match(event(), now=1000)
        storage.record_match(event("e2"), now=1200)
        self.assertEqual(len(storage.due_jobs(now=1200)), 1)
        self.assertTrue(storage.record_match(event("e3"), now=1300))
        self.assertEqual(len(storage.list_events()), 3)
        storage.close()

    def test_cleanup_deletes_terminal_outbox_before_events(self):
        storage = Storage(self.path)
        storage.record_match(event(), now=1000)
        storage.mark_sent("e1", now=1001)
        storage.cleanup(now=1000 + 31 * 86400)
        self.assertEqual(storage.list_events(), [])
        self.assertEqual(storage.queue_counts(), {})
        storage.close()

    def test_old_database_migrates_without_losing_pending_jobs(self):
        storage = Storage(self.path)
        storage.record_match(event(), now=1000)
        storage.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("ALTER TABLE outbox DROP COLUMN next_part")
            connection.execute("ALTER TABLE outbox DROP COLUMN retry_exhausted")
            connection.commit()
        storage = Storage(self.path)
        self.assertEqual(storage.next_part("e1"), 0)
        self.assertEqual(len(storage.due_jobs(now=1001)), 1)
        storage.mark_part_sent("e1", 1)
        storage.close()
        storage = Storage(self.path)
        self.assertEqual(storage.next_part("e1"), 1)
        storage.close()

    def test_exhausted_unknown_is_retained_but_not_retried_and_is_cleaned(self):
        storage = Storage(self.path)
        storage.record_match(event(), now=1000)
        storage.mark_unknown_terminal("e1", "结果未知", now=1001)
        self.assertEqual(storage.due_jobs(now=1002), [])
        self.assertEqual(storage.list_events()[0]["state"], "unknown")
        storage.cleanup(now=1000 + 31 * 86400)
        self.assertEqual(storage.list_events(), [])
        storage.close()

    def test_records_revision_read_does_not_wait_for_write_lock(self):
        """UI 读版本号不得等 SQLite 事务锁，否则后台写库时界面心跳会卡死。"""
        storage = Storage(self.path)
        holding = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with storage._lock:
                holding.set()
                release.wait(5)

        thread = threading.Thread(target=hold, name="hold-write-lock", daemon=True)
        thread.start()
        self.assertTrue(holding.wait(5))
        try:
            started = time.monotonic()
            for _ in range(100):
                self.assertIsInstance(storage.records_revision, int)
            self.assertLess(
                time.monotonic() - started, 1.0, "读版本号被数据库写锁阻塞了"
            )
        finally:
            release.set()
            thread.join(5)
            storage.close()


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "notify.db")
        self.storage.record_match(event(), now=1000)

    def tearDown(self):
        self.storage.close()
        self.temp.cleanup()

    def test_signature_matches_reference(self):
        timestamp = 1700000000
        secret = "secret"
        expected = base64.b64encode(
            hmac.new(f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
        ).decode()
        self.assertEqual(feishu_sign(timestamp, secret), expected)

    def test_payload_contains_required_fields_and_long_text_parts(self):
        messages = build_messages(self.storage.due_jobs(now=1001)[0].payload, 1001, "s")
        self.assertEqual(messages[0]["msg_type"], "post")
        text = json.dumps(messages[0], ensure_ascii=False)
        for expected in ("测试群", "手机", "事件编号", "e1", "识别时间"):
            self.assertIn(expected, text)
        long_messages = build_messages(
            {**self.storage.due_jobs(now=1001)[0].payload, "text": "长" * 2500}, 1001, "s"
        )
        self.assertEqual(len(long_messages), 3)

    def test_http_200_with_business_failure_is_not_success(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self, size):
                return b'{"code":19001,"msg":"invalid sign"}'

        client = FeishuClient(MOCK_WEBHOOK, "bad")
        with patch("urllib.request.urlopen", return_value=Response()):
            with self.assertRaisesRegex(NotifyError, "19001"):
                client.send(self.storage.due_jobs(now=1001)[0].payload)

    def test_unknown_timeout_retries_then_expires(self):
        class TimeoutClient:
            def send(self, payload, *, now=None):
                raise UnknownNotifyError("timeout")

        dispatcher = NotificationDispatcher(
            self.storage, lambda: TimeoutClient(), ttl_seconds=300, max_attempts=2,
            limiter=RateLimiter(clock=lambda: 0, sleeper=lambda _: None),
        )
        with patch("deal_watch.notify.time.time", return_value=1001):
            dispatcher.process_once()
        job = self.storage.due_jobs(now=1002)[0]
        self.assertEqual(job.state, "unknown")
        self.assertEqual(job.attempts, 1)
        with patch("deal_watch.notify.time.time", return_value=1301):
            dispatcher.process_once()
        self.assertIn("expired", self.storage.queue_counts())

    def test_event_expiring_during_rate_limit_wait_is_not_sent(self):
        class NoWaitLimiter:
            def acquire(self):
                return None

        class MustNotSend:
            def send(self, payload, *, now=None):
                raise AssertionError("过期事件仍被发送")

        dispatcher = NotificationDispatcher(
            self.storage, lambda: MustNotSend(), ttl_seconds=300, limiter=NoWaitLimiter()
        )
        with patch("deal_watch.notify.time.time", side_effect=[1001, 1001, 1301, 1301]):
            dispatcher.process_once()
        self.assertIn("expired", self.storage.queue_counts())

    def test_permanent_failure_does_not_retry(self):
        class FailedClient:
            def send(self, payload, *, now=None):
                raise NotifyError("bad secret")

        dispatcher = NotificationDispatcher(
            self.storage, lambda: FailedClient(), limiter=RateLimiter(clock=lambda: 0)
        )
        with patch("deal_watch.notify.time.time", return_value=1001):
            dispatcher.process_once()
        self.assertIn("failed", self.storage.queue_counts())
        self.assertEqual(self.storage.due_jobs(now=1002), [])

    def test_connection_reset_and_invalid_business_shapes_are_unknown(self):
        for effect in (ConnectionResetError("do-not-echo"),):
            with patch("urllib.request.urlopen", side_effect=effect):
                with self.assertRaises(UnknownNotifyError) as raised:
                    FeishuClient(MOCK_WEBHOOK).send(event_payload())
                self.assertNotIn("do-not-echo", str(raised.exception))
        for body in (b"[]", b"null", b'{"code":false}', b'{"code":[]}', b"not json"):
            with self.subTest(body=body), patch(
                "urllib.request.urlopen", return_value=Response(body)
            ):
                with self.assertRaises(UnknownNotifyError):
                    FeishuClient(MOCK_WEBHOOK).send(event_payload())

    def test_server_error_text_is_not_exposed(self):
        body = json.dumps({"code": 19001, "msg": "secret-token-and-chat-body"}).encode()
        with patch("urllib.request.urlopen", return_value=Response(body)):
            with self.assertRaises(NotifyError) as raised:
                FeishuClient(MOCK_WEBHOOK).send(event_payload())
        self.assertIn("19001", str(raised.exception))
        self.assertNotIn("secret-token", str(raised.exception))

    def test_multipart_limits_every_request_and_retries_only_unconfirmed_parts(self):
        self.storage.mark_sent("e1")
        self.storage.record_match(event("long", "长" * 2500), now=1000)
        limiter = NoWaitLimiter()
        dispatcher = NotificationDispatcher(
            self.storage, lambda: FeishuClient(MOCK_WEBHOOK), limiter=limiter
        )
        with patch("deal_watch.notify.time.time", return_value=1001), patch(
            "urllib.request.urlopen", side_effect=[Response(), TimeoutError()]
        ) as requests:
            dispatcher.process_once()
        self.assertEqual(requests.call_count, 2)
        self.assertEqual(self.storage.next_part("long"), 1)
        self.assertEqual(limiter.calls, 2)
        with patch("deal_watch.notify.time.time", return_value=1003), patch(
            "urllib.request.urlopen", return_value=Response()
        ) as requests:
            dispatcher.process_once()
        self.assertEqual(requests.call_count, 2)
        self.assertEqual(limiter.calls, 4)
        self.assertEqual(self.storage.next_part("long"), 3)
        self.assertEqual(self.storage.list_events()[0]["state"], "sent")

    def test_restart_does_not_exceed_persisted_attempt_limit(self):
        for _ in range(5):
            self.storage.mark_attempt("e1", now=1000)
        dispatcher = NotificationDispatcher(
            self.storage, lambda: FeishuClient(MOCK_WEBHOOK), limiter=NoWaitLimiter()
        )
        with patch("deal_watch.notify.time.time", return_value=1001), patch(
            "urllib.request.urlopen"
        ) as requests:
            dispatcher.process_once()
        requests.assert_not_called()
        self.assertEqual(self.storage.list_events()[0]["attempts"], 5)
        self.assertEqual(self.storage.list_events()[0]["state"], "unknown")
        self.assertEqual(self.storage.due_jobs(now=1002), [])

    def test_exhausted_timeout_remains_unknown(self):
        dispatcher = NotificationDispatcher(
            self.storage, lambda: FeishuClient(MOCK_WEBHOOK),
            limiter=NoWaitLimiter(), max_attempts=1,
        )
        with patch("deal_watch.notify.time.time", return_value=1001), patch(
            "urllib.request.urlopen", side_effect=TimeoutError()
        ):
            dispatcher.process_once()
        self.assertEqual(self.storage.list_events()[0]["state"], "unknown")
        self.assertEqual(self.storage.due_jobs(now=1002), [])

    def test_unexpected_send_error_does_not_kill_worker(self):
        self.storage.mark_sent("e1")
        self.storage.record_match(event("fresh"), now=time.time())
        called = threading.Event()

        class BrokenClient:
            def send(self, payload, *, now=None):
                called.set()
                raise RuntimeError("do-not-echo-secret")

        dispatcher = NotificationDispatcher(
            self.storage, BrokenClient, limiter=NoWaitLimiter(), max_attempts=1
        )
        dispatcher.start()
        self.assertTrue(called.wait(2))
        for _ in range(100):
            if dispatcher.last_error:
                break
            time.sleep(0.005)
        self.assertTrue(dispatcher._thread.is_alive())
        self.assertNotIn("do-not-echo", dispatcher.last_error)
        dispatcher.stop()
        self.assertFalse(dispatcher._thread.is_alive())

    def test_stop_interrupts_rate_limit_without_sending_next_part(self):
        self.storage.mark_sent("e1")
        self.storage.record_match(event("long", "长" * 2500), now=time.time())
        sent = threading.Event()

        def reply(*args, **kwargs):
            sent.set()
            return Response()

        dispatcher = NotificationDispatcher(self.storage, lambda: FeishuClient(MOCK_WEBHOOK))
        with patch("urllib.request.urlopen", side_effect=reply) as requests:
            dispatcher.start()
            self.assertTrue(sent.wait(2))
            started = time.monotonic()
            dispatcher.stop()
            self.assertLess(time.monotonic() - started, 1)
            self.assertFalse(dispatcher._thread.is_alive())
        self.assertEqual(requests.call_count, 1)

    def test_minute_limit_with_empty_second_bucket_does_not_crash(self):
        clock = [0.0]
        limiter = RateLimiter(
            per_second=2, per_minute=2, clock=lambda: clock[0],
            sleeper=lambda delay: clock.__setitem__(0, clock[0] + delay),
        )
        limiter.acquire()
        limiter.acquire()
        clock[0] = 2.0
        limiter.acquire()
        self.assertEqual(clock[0], 60.0)


def event_payload():
    return Storage.event_payload(event())


def _candidate(text: str, tracking_id: str, kind: str = "normal_text") -> MessageCandidate:
    return MessageCandidate(
        region_id="r1",
        text=text,
        box=Rect(0, 0, 200, 20),
        first_seen=1.0,
        last_seen=1.0,
        kind=kind,
        tracking_id=tracking_id,
    )


def _transaction_boundaries(statements: list[str]) -> list[str]:
    return [
        item
        for item in statements
        if item.split() and item.split()[0].upper() in {"BEGIN", "COMMIT", "ROLLBACK"}
    ]


class CandidateRecordTransactionTests(unittest.TestCase):
    """要求 3：一帧多候选（含命中信息与最终判断）只发生一次写入事务。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = RecognitionStore(Path(self.temp.name))

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def frame(self) -> list[MessageCandidate]:
        return [
            _candidate("手机 4999 元", "t0"),
            _candidate("普通消息甲", "t1"),
            _candidate("普通消息乙", "t2"),
            _candidate("[图片]", "t3", kind="image"),
        ]

    def test_one_frame_with_decisions_is_a_single_insert_transaction(self):
        before = self.store.records_revision
        statements: list[str] = []
        self.store._conn.set_trace_callback(statements.append)
        try:
            ids = self.store.record_candidates(
                "r1",
                "合成群",
                1.0,
                self.frame(),
                {"t0": ["手机"], "t1": [], "t2": []},
                {"t0": "queued", "t1": "not_matched", "t2": "boundary_uncertain"},
            )
        finally:
            self.store._conn.set_trace_callback(None)

        commits = [
            item for item in _transaction_boundaries(statements) if item.split()[0].upper() == "COMMIT"
        ]
        self.assertEqual(commits, ["COMMIT"], f"一帧只允许一次提交，实际语句：{statements}")
        self.assertEqual(len(ids), 3, "非普通文字不得写入候选表")
        self.assertEqual(
            self.store.records_revision, before + 1, "一帧只推进一次版本号"
        )

        rows = {row["text"]: row for row in self.store.list_records(50)}
        self.assertEqual(set(rows), {"手机 4999 元", "普通消息甲", "普通消息乙"})
        # 最终判断完整保存，且未命中的普通消息也必须保留。
        self.assertEqual(rows["手机 4999 元"]["decision"], "queued")
        self.assertEqual(rows["普通消息甲"]["decision"], "not_matched")
        self.assertEqual(rows["普通消息乙"]["decision"], "boundary_uncertain")
        self.assertEqual(json.loads(rows["手机 4999 元"]["matched_keywords"]), ["手机"])

    def test_legacy_call_without_decisions_keeps_observed_and_remains_compatible(self):
        self.store.record_candidates(
            "r1",
            "合成群",
            1.0,
            [_candidate("普通消息甲", "t1"), _candidate("普通消息乙", "t2")],
            {"t1": [], "t2": []},
        )
        rows = {row["id"]: row for row in self.store.list_records(50)}
        self.assertEqual({row["decision"] for row in rows.values()}, {"observed"})
    def test_records_revision_read_does_not_wait_for_store_lock(self):
        holding = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with self.store._lock:
                holding.set()
                release.wait(5)

        thread = threading.Thread(target=hold, name="hold-feedback-lock", daemon=True)
        thread.start()
        self.assertTrue(holding.wait(5))
        try:
            started = time.monotonic()
            for _ in range(100):
                self.assertIsInstance(self.store.records_revision, int)
            self.assertLess(
                time.monotonic() - started, 1.0, "读候选版本号被数据库写锁阻塞了"
            )
        finally:
            release.set()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
