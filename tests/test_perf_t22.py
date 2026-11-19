"""0.2.2 T2.2 「减少持续重复工作」的离线证据。

四项优化各有**客观可数**的指标，不依赖机器快慢：

1. 事务合并 —— ``sqlite3`` trace 回调数 ``BEGIN``/``COMMIT``；
2. 记录表刷新收敛 —— 页面不可见时查询次数必须为 0；
3. 在途保护 —— 查询阻塞时连发 5 次心跳只允许 1 次查询；
4. 通知健康检查独立 —— 记录页不刷新时，健康检查时间戳仍要推进；
5. 结构长度上界 —— 跑 200 轮后相关容器不得增长。

本文件只用合成假件与临时目录：不碰真实微信窗口、不占屏、不写用户目录。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from deal_watch.config import AppConfig  # noqa: E402
from deal_watch.metrics import STAGES, TimingRecorder  # noqa: E402
from deal_watch.models import (  # noqa: E402
    FrameSample,
    MatchEvent,
    MessageCandidate,
    OcrObservation,
    Rect,
    TextBlock,
)
from deal_watch.recognition import RecognitionStore  # noqa: E402
from deal_watch.rules import RuleEngine  # noqa: E402
from deal_watch.service import MonitorService  # noqa: E402
from deal_watch.storage import Storage  # noqa: E402
from deal_watch.ui.main import (  # noqa: E402
    UI_HEARTBEAT_MS,
    MainWindow,
    RecordsRefreshCoordinator,
)
from deal_watch.ui.shell import PageId, RecordView  # noqa: E402
from region_fixtures import ready_region  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _is_transaction_boundary(statement: str) -> bool:
    """``BEGIN`` / ``COMMIT`` / ``ROLLBACK`` 才算事务边界。"""
    head = statement.split()[0].upper() if statement.split() else ""
    return head in {"BEGIN", "COMMIT", "ROLLBACK"}


class TraceRecorder:
    """挂在 sqlite3 连接上的语句计数器（只在用例里用，不进生产路径）。"""

    def __init__(self, store) -> None:
        self.store = store
        self.statements: list[str] = []

    def __enter__(self) -> TraceRecorder:
        self.store._conn.set_trace_callback(self.statements.append)
        return self

    def __exit__(self, *_exc) -> None:
        self.store._conn.set_trace_callback(None)

    def boundaries(self) -> list[str]:
        return [item for item in self.statements if _is_transaction_boundary(item)]

    @property
    def transaction_count(self) -> int:
        """一个事务 = 一个 BEGIN/COMMIT 对；``record_match`` 用的是 BEGIN IMMEDIATE。"""
        return sum(1 for item in self.boundaries() if item.split()[0].upper() == "COMMIT")


def _candidates(count: int, region_id: str = "r1") -> list[MessageCandidate]:
    return [
        MessageCandidate(
            region_id=region_id,
            text=f"合成消息 {index}",
            box=Rect(10, 10 + index * 40, 250, 25),
            first_seen=1.0,
            last_seen=1.0,
            tracking_id=f"t{index}",
        )
        for index in range(count)
    ]


class TransactionTests(unittest.TestCase):
    """一帧候选和最终判断只写一个事务，失败整批回滚。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = RecognitionStore(Path(self.temp.name))
        self.messages = _candidates(5)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _record(self, messages=None) -> list[str]:
        return self.store.record_candidates(
            "r1", "合成群", 1.0, messages or self.messages,
            {message.tracking_id: ["手机"] for message in self.messages},
            decisions_by_tracking={message.tracking_id: "queued" for message in self.messages},
        )

    def test_one_frame_is_one_transaction(self):
        with TraceRecorder(self.store) as trace:
            ids = self._record()
        self.assertEqual(len(ids), 5)
        self.assertEqual(trace.transaction_count, 1)
        self.assertEqual({row["decision"] for row in self.store.list_records()}, {"queued"})

    def test_batch_rolls_back_as_a_whole(self):
        self._record()
        before = self.store.list_records()
        broken = _candidates(2)
        broken[1].text = {"invalid": "binding"}
        with self.assertRaises(sqlite3.Error):
            self._record(broken)
        self.assertEqual(self.store.list_records(), before)


class _RecordingRecognition:
    """只记录调用形状的假 store：证明 service 把候选与判断合在一次写入里。"""

    def __init__(self) -> None:
        self.batches: list[list[tuple[str, str]]] = []
        self.calls = 0
        self.mark_calls = 0
        self.received = 0

    def record_candidates(
        self, _region_id, _alias, _observed_at, messages, *_args,
        decisions_by_tracking=None, **_kwargs,
    ):
        self.calls += 1
        self.received = len(messages)
        decisions = decisions_by_tracking or {}
        self.batches.append(
            [(message.tracking_id, decisions.get(message.tracking_id, "observed"))
             for message in messages]
        )
        return [f"c{index}" for index in range(len(messages))]

    def mark_decisions(self, decisions) -> None:  # noqa: ANN001 - 鸭子类型
        self.mark_calls += 1

    def mark_decision(self, candidate_ids, decision) -> None:  # noqa: ANN001
        self.mark_calls += 1


class ServiceBatchTests(unittest.TestCase):
    """验收 1 的接线证据：一帧只写一次候选，判断随候选同事务落库。"""

    def test_service_marks_all_candidates_in_one_call(self):
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("合成区域", rect=Rect(0, 0, 300, 500))
            config = AppConfig(
                regions=[region], keywords=["手机"], save_hit_images=False, poll_interval_ms=100
            )
            storage = Storage(Path(directory) / "batch.db")
            feedback = _RecordingRecognition()
            done = threading.Event()

            class Frames:
                def __init__(self) -> None:
                    self.sequence = 0
                    self.sent = False

                def reset(self, region_id):
                    pass

                def capture(self, source, force=False):
                    if self.sent:
                        return None
                    self.sent = True
                    self.sequence = 1
                    return FrameSample(
                        source.id,
                        1,
                        time.time(),
                        ["手机 优惠 4999 元", "普通消息甲", "普通消息乙"],
                        source.rect,
                    )

            class Ocr:
                def recognize(self, frame):
                    done.set()
                    return OcrObservation(
                        region_id=frame.region_id,
                        sampled_at=frame.captured_at,
                        frame_sequence=frame.sequence,
                        blocks=[
                            TextBlock(text, 0.99, Rect(10, 10 + index * 40, 250, 25))
                            for index, text in enumerate(frame.image)
                        ],
                        inference_ms=1.0,
                    )

            service = MonitorService(
                config,
                storage,
                Ocr(),
                capturer=Frames(),
                data_dir=Path(directory),
                recognition_store=feedback,
            )
            service.start()
            try:
                self.assertTrue(done.wait(5), "OCR 没有取到帧")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and feedback.calls == 0:
                    time.sleep(0.01)
            finally:
                service.stop()
                storage.close()

            self.assertEqual(feedback.calls, 1, "一次采样只允许一次候选写入，不能逐条调")
            self.assertEqual(feedback.mark_calls, 0, "判断随候选写入，不得再补一次事务")
            self.assertGreaterEqual(feedback.received, 2, "这一帧应产出多条候选")
            self.assertEqual(
                len(feedback.batches[0]),
                feedback.received,
                "所有候选与判断必须在同一次调用里",
            )
            self.assertTrue(
                all(state for _tracking_id, state in feedback.batches[0]),
                "每条候选都必须带最终判断，缺失键才保持 observed",
            )
            self.assertEqual(
                len({tracking_id for tracking_id, _state in feedback.batches[0]}),
                feedback.received,
                "批量里必须保持候选顺序与唯一 tracking_id（与 normal_messages 严格对齐）",
            )


class StorageRevisionTests(unittest.TestCase):
    """版本号是"要不要重查"的唯一依据，必须每次可见变化都推进。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "rev.db")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    def test_every_visible_write_bumps_revision(self):
        start = self.storage.records_revision
        event = MatchEvent("r1", "合成群", "手机", "文本", "k1", 1.0)
        self.storage.record_match(event, 300, 100.0)
        after_match = self.storage.records_revision
        self.storage.mark_attempt(event.id, 101.0)
        self.storage.mark_sent(event.id, 102.0)
        self.storage.schedule_retry(event.id, 5.0, "x", now=103.0)
        self.storage.mark_failed(event.id, "x", 104.0)
        end = self.storage.records_revision

        self.assertGreater(after_match, start, "新事件必须推进版本号")
        self.assertGreater(end, after_match, "送达状态变化必须推进版本号")
        self.assertEqual(end - after_match, 4)

    def test_reading_revision_does_not_touch_the_database(self):
        with TraceRecorder(self.storage) as trace:
            for _ in range(50):
                _ = self.storage.records_revision
        self.assertEqual(trace.boundaries(), [], "读版本号必须在内存里完成，不查库")


class _BlockingFetch:
    """可阻塞的查询函数，用来验证在途保护。"""

    def __init__(self) -> None:
        self.calls = 0
        self.gate = threading.Event()
        self.entered = threading.Event()

    def __call__(self) -> list[dict]:
        self.calls += 1
        self.entered.set()
        if not self.gate.wait(5):
            raise TimeoutError("查询未被释放")
        return [{"call": self.calls}]


class CoordinatorTests(unittest.TestCase):
    """验收 2/3/4：不可见零查询、在途只一次、版本号收敛。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_invisible_page_never_queries(self):
        """验收 2：页面不可见时连续 10 次 tick，查询次数必须是 0。"""
        fetch = _BlockingFetch()
        fetch.gate.set()
        coordinator = RecordsRefreshCoordinator(fetch, name="test-invisible")
        try:
            for _ in range(10):
                coordinator.tick(visible=False, revision=1)
            self.assertEqual(coordinator.query_count, 0)
            self.assertEqual(fetch.calls, 0)
        finally:
            coordinator.shutdown()

    def _process_until(self, condition, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not condition():
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(condition(), "等待条件超时")

    def test_in_flight_guard_allows_only_one_query(self):
        """验收 3：查询阻塞时连发 5 次 tick，只允许 1 次查询。"""
        fetch = _BlockingFetch()
        coordinator = RecordsRefreshCoordinator(fetch, name="test-inflight")
        delivered: list[list[dict]] = []
        coordinator.rows_ready.connect(delivered.append)
        try:
            self.assertTrue(coordinator.tick(True, 1))
            self.assertTrue(fetch.entered.wait(5), "第一次查询没有进到 fetch")
            # 在途期间版本号连续变化，也不许再发。
            for revision in (1, 2, 3, 4, 5):
                self.assertFalse(coordinator.tick(True, revision))
            self.assertEqual(coordinator.query_count, 1)
            self.assertEqual(fetch.calls, 1)

            fetch.gate.set()
            self._process_until(lambda: delivered)
            self.assertEqual(len(delivered), 1, "在途期间被跳过的请求不得补发多次")

            # 查询完成后，版本号再变才发新的。
            self.assertFalse(coordinator.tick(True, 1), "版本号没变不该再查")
            fetch.gate.clear()
            fetch.gate.set()
            self.assertTrue(coordinator.tick(True, 6), "版本号变了应该发新查询")
            self.assertEqual(coordinator.query_count, 2)
        finally:
            fetch.gate.set()
            coordinator.shutdown()

    def test_changed_revision_refreshes_exactly_once(self):
        """验收 4：可见且版本号变了 → 恰好 1 次；版本号没变 → 不刷新。"""
        fetch = _BlockingFetch()
        fetch.gate.set()
        coordinator = RecordsRefreshCoordinator(fetch, name="test-revision")
        delivered: list[list[dict]] = []
        coordinator.rows_ready.connect(delivered.append)
        try:
            # 从不可见切到可见，且版本号变了 → 1 次
            self.assertTrue(coordinator.tick(True, 7))
            self._wait(lambda: len(delivered) == 1)
            self.assertEqual(coordinator.query_count, 1)
            self.assertEqual(coordinator.delivered_revision, 7)

            # 同一版本号再来 5 次 → 一次都不发
            for _ in range(5):
                self.assertFalse(coordinator.tick(True, 7))
            self.assertEqual(coordinator.query_count, 1)

            # 版本号推进 → 恰好再一次
            self.assertTrue(coordinator.tick(True, 8))
            self._wait(lambda: len(delivered) == 2)
            self.assertEqual(coordinator.query_count, 2)
        finally:
            coordinator.shutdown()

    def test_failure_does_not_mark_revision_delivered(self):
        """查询失败不记版本号：下一次心跳必须重试，不能"失败一次就永远不查"。"""
        attempts = {"count": 0}

        def broken_fetch():
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("模拟查询失败")
            return []

        coordinator = RecordsRefreshCoordinator(broken_fetch, name="test-failure")
        errors: list[str] = []
        coordinator.failed.connect(errors.append)
        rows: list[list[dict]] = []
        coordinator.rows_ready.connect(rows.append)
        try:
            self.assertTrue(coordinator.tick(True, 3))
            self._wait(lambda: errors)
            self.assertIsNone(coordinator.delivered_revision, "失败不得记成已送达")
            self.assertTrue(coordinator.tick(True, 3), "同一版本号失败后必须重试")
            self._wait(lambda: rows)
            self.assertEqual(coordinator.delivered_revision, 3)
            self.assertEqual(coordinator.failure_count, 1)
        finally:
            coordinator.shutdown()

    def test_force_request_is_not_lost_while_busy(self):
        """用户点"刷新"时若正好在途，不能无声丢掉这一下。"""
        fetch = _BlockingFetch()
        coordinator = RecordsRefreshCoordinator(fetch, name="test-force")
        delivered: list[list[dict]] = []
        coordinator.rows_ready.connect(delivered.append)
        try:
            self.assertTrue(coordinator.request(None, force=True))
            self.assertTrue(fetch.entered.wait(5))
            self.assertFalse(coordinator.request(None, force=True), "在途时不并发第二次")
            fetch.gate.set()
            self._wait(lambda: len(delivered) >= 2, timeout=5)
            self.assertGreaterEqual(coordinator.query_count, 2, "被记住的强制刷新必须补上")
        finally:
            fetch.gate.set()
            coordinator.shutdown()

    def test_concurrent_ticks_still_issue_only_one_query(self):
        """在途判断必须原子：8 个线程同时 tick，只允许 1 个线程真的起查询。"""
        fetch = _BlockingFetch()
        coordinator = RecordsRefreshCoordinator(fetch, name="test-race")
        gate = threading.Barrier(8)
        launched: list[bool] = []
        lock = threading.Lock()

        def hammer() -> None:
            gate.wait(5)
            result = coordinator.tick(True, 1)
            with lock:
                launched.append(result)

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertEqual(sum(launched), 1, "8 个线程只能有一个发起查询")
            self.assertEqual(coordinator.query_count, 1)
        finally:
            fetch.gate.set()
            coordinator.shutdown()

    def _wait(self, condition, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not condition():
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(condition(), "等待条件超时")


class MainWindowRefreshTests(unittest.TestCase):
    """验收 2/4/5 的界面级证据：真窗口、真定时器、真数据库。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.environment.start()
        self.window = MainWindow(ROOT)
        # 手驱动心跳，避免真实定时器在断言中间插进来。
        self.window.timer.stop()
        self.window.health_timer.stop()
        self._wait(lambda: not self.window.records_refresh.busy)

    def tearDown(self) -> None:
        self.window.exit_app()
        self._wait(lambda: self.window._shutdown_done)
        self.window.deleteLater()
        self.app.processEvents()
        self.environment.stop()
        self.temp.cleanup()

    def _wait(self, condition, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not condition():
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(condition(), "等待条件超时")

    def test_hidden_window_issues_no_queries(self):
        """验收 2：隐藏窗口后连跑 10 次心跳，两张表一次都不查。"""
        self.window.hide()
        self.app.processEvents()
        self.assertFalse(self.window.isVisible())

        before_records = self.window.records_refresh.query_count
        before_feedback = self.window.recognition_refresh.query_count
        for _ in range(10):
            self.window.refresh_runtime_views()
        # 即使期间有数据变化，只要看不见就不查。
        self.window.storage.record_match(
            MatchEvent("r1", "合成群", "手机", "隐藏窗口期间的新事件", "hidden-1", time.time()),
            300,
        )
        for _ in range(10):
            self.window.refresh_runtime_views()

        self.assertEqual(self.window.records_refresh.query_count, before_records)
        self.assertEqual(self.window.recognition_refresh.query_count, before_feedback)

    def test_visible_window_refreshes_once_per_revision(self):
        """验收 4：可见 + 版本号变化 → 恰好 1 次查询；版本号没变 → 不查。"""
        self.window.show()
        self.app.processEvents()
        # 可见性已按页签细化：先切到通知记录页，断言内容不变（避免依赖默认页签）。
        self.window.navigate(PageId.RECORDS, RecordView.NOTIFICATIONS)
        self.app.processEvents()
        self.assertTrue(self.window.records_visible())

        self._wait(lambda: not self.window.records_refresh.busy)
        base = self.window.records_refresh.query_count
        for _ in range(5):
            self.window.refresh_runtime_views()
        self.assertEqual(
            self.window.records_refresh.query_count, base, "版本号没变时不该重查"
        )

        self.window.storage.record_match(
            MatchEvent("r1", "合成群", "手机", "可见期间的新事件", "visible-1", time.time()), 300
        )
        self.window.refresh_runtime_views()
        self.assertEqual(self.window.records_refresh.query_count, base + 1, "版本号变了只查一次")

        self._wait(
            lambda: not self.window.records_refresh.busy
            and self.window.record_table.rowCount() == 1,
            timeout=5,
        )
        self.assertEqual(self.window.record_table.rowCount(), 1, "查回来的行必须真的填进表")

        for _ in range(5):
            self.window.refresh_runtime_views()
        self.assertEqual(
            self.window.records_refresh.query_count, base + 1, "同一版本号不得重复查"
        )

    def test_health_check_runs_while_records_are_gated(self):
        """验收 5：记录表不刷新时，通知健康检查仍按自己的节奏推进。"""
        self.window.hide()
        self.app.processEvents()
        # setUp 里两条定时器都停了；这里只把健康检查重新拉起来，数据心跳仍然停着。
        self.window.health_timer.start(UI_HEARTBEAT_MS)
        self.assertTrue(self.window.health_timer.isActive(), "健康检查必须有独立定时器")
        self.assertFalse(self.window.timer.isActive(), "数据心跳此刻应当停着")
        self.assertEqual(self.window.health_timer.interval(), UI_HEARTBEAT_MS)

        before = self.window.records_refresh.query_count
        first = self.window.notification_health_checked_at
        self.window.health_timer.timeout.emit()
        time.sleep(0.01)
        self.window.health_timer.timeout.emit()
        second = self.window.notification_health_checked_at

        self.assertGreater(second, first, "健康检查时间戳必须推进")
        self.assertEqual(
            self.window.records_refresh.query_count, before, "健康检查不得顺带查记录表"
        )
        self.window.health_timer.stop()

    def test_health_check_still_reports_dispatcher_error_while_hidden(self):
        """隐藏时也要能报出飞书异常——这正是 0.2.2 之前会漏掉的情形。"""
        self.window.hide()
        self.app.processEvents()
        self.window.dispatcher._delivery_failed("模拟飞书不可达")
        messages: list[str] = []
        original = self.window.queue_system_message
        self.window.queue_system_message = lambda keyword, text, **kwargs: messages.append(keyword)
        try:
            self.window.health_timer.timeout.emit()
        finally:
            self.window.queue_system_message = original
            self.window.dispatcher.last_error = ""
        self.assertEqual(messages, ["通知连接异常"])

    def test_shutdown_stops_both_timers_and_joins_refresh_threads(self):
        self.window.exit_app()
        self.assertFalse(self.window.timer.isActive())
        self.assertFalse(self.window.health_timer.isActive())
        self.assertFalse(self.window.records_refresh.busy)
        self.assertFalse(self.window.recognition_refresh.busy)


class BoundedStructureTests(unittest.TestCase):
    """验收 6：跑 200 轮后相关结构长度必须有上界。"""

    def test_timing_recorder_series_and_cache_are_bounded(self):
        recorder = TimingRecorder()
        regions = [f"r{index}" for index in range(4)]
        for cycle in range(200):
            for region_id in regions:
                for stage in STAGES:
                    recorder.record(stage, float(cycle), region_id)
                # 快照读取会建立 (区域, 段) 的统计缓存；跑多少轮都不该多出键。
                recorder.region_payload(region_id)
        # 4 区域 × 10 段 = 40 个 (区域, 段) 键；跑多少轮都不涨。
        self.assertEqual(len(recorder._series), len(regions) * len(STAGES))
        self.assertEqual(len(recorder._cache), len(regions) * len(STAGES))
        self.assertLessEqual(recorder.recent_count(STAGES[0], regions[0]), 240)

    def test_rule_engine_last_decisions_is_the_one_unbounded_structure(self):
        """把"无界"这件事量化出来，供是否授权的决策使用。"""
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "rules.db")
            engine = RuleEngine(storage, ["手机"])
            try:
                for round_index in range(200):
                    message = MessageCandidate(
                        "r1",
                        "合成消息",
                        Rect(0, 0, 10, 10),
                        1.0,
                        1.0,
                        tracking_id=f"t{round_index}",
                    )
                    engine.decide(message)
                # 200 轮 → 200 条，只增不减：键是 tracking_id，一条消息一个键。
                self.assertEqual(len(engine.last_decisions), 200)
            finally:
                storage.close()


if __name__ == "__main__":
    unittest.main()
