"""T1.6 采集故障分类与状态机对齐。

锁住五条不变式：

1. 新增的三个错误码**绝不**进 ``_PAUSE_DETAILS``（否则超时被当成“暂停”，
   不进故障、不发通知）；
2. 持续故障每轮只产生 1 次“进入故障”、恢复只产生 1 次“监控恢复”；
3. A 区域故障不影响 B 区域在同一轮被采集与提交；
4. 故障区域本轮不重试——``_fail_region`` 不得提前推进 ``_next_due``；
5. 故障说明与阶段日志都带 ``request_id`` / ``stage_failed`` / 分段耗时，
   并且每轮采集循环都留下 ``cycle_elapsed_ms``（P2 与 P3 的判据）。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from deal_watch.capture import CaptureError, WindowInfo
from deal_watch.config import AppConfig
from deal_watch.models import (
    CaptureErrorCode,
    FrameSample,
    OcrObservation,
    Rect,
    RegionStatus,
    TextBlock,
)
from deal_watch.service import _PAUSE_DETAILS, MonitorService, describe_failure
from deal_watch.storage import Storage
from deal_watch.window_capture import (
    CapturedWindowFrame,
    CaptureTrace,
    WindowCaptureError,
    WindowFrameCapturer,
)
from region_fixtures import (
    READY_PROCESS_ID,
    READY_PROCESS_PATH,
    READY_WINDOW_CLASS,
    ready_region,
)
from stage_log_fixture import StageLogCapture, find_lines

NEW_FAULT_CODES = (
    CaptureErrorCode.CAPTURE_SESSION_HUNG,
    CaptureErrorCode.CAPTURE_POPEN_TIMEOUT,
    CaptureErrorCode.CAPTURE_ORPHAN,
)


class FakeOcr:
    def recognize(self, frame):
        return OcrObservation(
            region_id=frame.region_id,
            sampled_at=frame.captured_at,
            frame_sequence=frame.sequence,
            blocks=[
                TextBlock(text, 0.99, Rect(10, 10 + index * 60, 250, 25))
                for index, text in enumerate(frame.image)
            ],
            inference_ms=1.0,
        )


class HealthTransitions:
    """与 ``ui/main.on_health`` 同规则的记录器：进入故障一次、恢复一次。"""

    def __init__(self) -> None:
        self.active: set[str] = set()
        self.errors: list = []
        self.recoveries: list = []
        self.statuses: list[str] = []
        self.snapshots: list = []

    def __call__(self, snapshot) -> None:
        self.snapshots.append(snapshot)
        self.statuses.append(snapshot.status.value)
        if snapshot.status is RegionStatus.ERROR:
            if snapshot.region_id not in self.active:
                self.active.add(snapshot.region_id)
                self.errors.append(snapshot)
        elif snapshot.status is RegionStatus.RUNNING:
            if snapshot.region_id in self.active:
                self.active.discard(snapshot.region_id)
                self.recoveries.append(snapshot)


def hung_trace(region_id: str) -> CaptureTrace:
    return CaptureTrace(
        request_id="3f9c1d2a",
        region_id=region_id,
        popen_ms=12.0,
        wait_ms=4980.0,
        kill_ms=8.0,
        total_ms=5003.0,
        stage_failed="session_exit",
        stages={"wgc_start": 18.2},
    )


class PauseClassificationTests(unittest.TestCase):
    def test_new_error_codes_never_become_pause(self):
        for code in NEW_FAULT_CODES:
            with self.subTest(code=code):
                self.assertNotIn(code, _PAUSE_DETAILS)
        # 0.2.1 的两个暂停语义保持不变。
        self.assertEqual(
            _PAUSE_DETAILS[CaptureErrorCode.WINDOW_MINIMIZED], "已暂停：窗口最小化"
        )
        self.assertEqual(_PAUSE_DETAILS[CaptureErrorCode.DESKTOP_LOCKED], "已暂停：桌面已锁定")

    def test_describe_failure_keeps_existing_text_without_trace(self):
        self.assertEqual(describe_failure("模拟遮挡"), "模拟遮挡")
        self.assertEqual(describe_failure("模拟遮挡", CaptureErrorCode.WINDOW_OCCLUDED), "模拟遮挡")
        long_text = "错误" * 400
        self.assertEqual(len(describe_failure(long_text)), 300)

    def test_describe_failure_appends_request_stage_and_timings(self):
        detail = describe_failure(
            "窗口采集组件超过 5.0 秒未返回",
            CaptureErrorCode.CAPTURE_SESSION_HUNG,
            hung_trace("r1"),
        )
        self.assertIn("窗口采集组件超过 5.0 秒未返回", detail)
        self.assertIn("capture_session_hung", detail)
        self.assertIn("请求 3f9c1d2a", detail)
        self.assertIn("卡在 session_exit", detail)
        self.assertIn("总5003ms", detail)

    def test_describe_failure_survives_broken_trace(self):
        class Broken:
            def detail_suffix(self):
                raise RuntimeError("boom")

        self.assertEqual(describe_failure("采集失败", CaptureErrorCode.CAPTURE_FAILED, Broken()), "采集失败")


class ServiceFaultStateTests(unittest.TestCase):
    def _service(self, directory: str, region, capturer, **config_kwargs):
        config = AppConfig(
            regions=[region],
            keywords=["手机"],
            save_hit_images=False,
            **config_kwargs,
        )
        storage = Storage(Path(directory) / "fault.db")
        service = MonitorService(config, storage, FakeOcr(), capturer=capturer)
        return service, storage

    def test_repeated_session_hang_notifies_fault_once_and_recovers_once(self):
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("测试群", window_title="微信", rect=Rect(0, 0, 300, 500))
            config = AppConfig(
                regions=[region], keywords=["手机"], save_hit_images=False, poll_interval_ms=100
            )
            storage = Storage(Path(directory) / "hung.db")
            recorder = HealthTransitions()

            class HangingCapturer:
                def __init__(self) -> None:
                    self.calls = 0
                    self.last_trace = None

                def reset(self, region_id):
                    pass

                def capture(self, source, force=False):
                    self.calls += 1
                    if self.calls <= 2:
                        self.last_trace = hung_trace(source.id)
                        raise CaptureError(
                            CaptureErrorCode.CAPTURE_SESSION_HUNG,
                            "窗口采集组件超过 5.0 秒未返回",
                        )
                    return FrameSample(source.id, self.calls, time.time(), ["普通消息"], source.rect)

            capturer = HangingCapturer()
            service = MonitorService(config, storage, FakeOcr(), capturer=capturer)
            service.on_health = recorder
            service.start()
            try:
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline and not recorder.recoveries:
                    time.sleep(0.02)
                self.assertGreaterEqual(capturer.calls, 3, "故障区域没有被按间隔重试")
                self.assertTrue(recorder.errors, "持续故障没有进入故障状态")
                self.assertFalse(
                    any(status == RegionStatus.PAUSED.value for status in recorder.statuses),
                    "新错误码被误判成暂停",
                )
                self.assertEqual(len(recorder.errors), 1, "同一持续故障只通知一次")
                self.assertEqual(len(recorder.recoveries), 1, "恢复只通知一次")
                self.assertEqual(recorder.errors[0].status, RegionStatus.ERROR)
                self.assertIn("请求 3f9c1d2a", recorder.errors[0].detail)
                self.assertIn("卡在 session_exit", recorder.errors[0].detail)
                self.assertEqual(recorder.recoveries[0].status, RegionStatus.RUNNING)
                # 恢复帧先建基线：BASELINING 那次快照必须说清楚。
                self.assertTrue(
                    any(
                        snapshot.detail == "恢复后重新建立基线"
                        for snapshot in recorder.snapshots
                    ),
                    "恢复后没有回到“重新建立基线”",
                )
                self.assertEqual(storage.list_events(), [])
            finally:
                service.stop()
                storage.close()

    def test_failing_region_does_not_block_other_region_in_same_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = ready_region("坏区域", window_title="微信", rect=Rect(0, 0, 100, 100))
            good = ready_region("好区域", window_title="微信", rect=Rect(0, 0, 100, 100))
            config = AppConfig(
                regions=[bad, good], keywords=["手机"], save_hit_images=False
            )
            storage = Storage(Path(directory) / "mixed.db")
            submitted: list[str] = []

            class MixedCapturer:
                def reset(self, region_id):
                    pass

                def capture(self, source, force=False):
                    if source.id == bad.id:
                        self.last_trace = hung_trace(bad.id)
                        raise CaptureError(
                            CaptureErrorCode.CAPTURE_TIMEOUT, "窗口采集组件超过 5.0 秒未返回"
                        )
                    submitted.append(source.id)
                    return FrameSample(source.id, 1, time.time(), ["普通消息"], source.rect)

            service = MonitorService(config, storage, FakeOcr(), capturer=MixedCapturer())
            service.start()
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if service._runtime[good.id].status is RegionStatus.RUNNING:
                        break
                    time.sleep(0.02)
                self.assertEqual(service._runtime[bad.id].status, RegionStatus.ERROR)
                self.assertEqual(service._runtime[good.id].status, RegionStatus.RUNNING)
                self.assertEqual(submitted, [good.id])
                self.assertGreater(service._runtime[good.id].last_frame_at, 0.0)
            finally:
                service.stop()
                storage.close()

    def test_fail_region_does_not_advance_next_due(self):
        """故障区域本轮不重试：``_fail_region`` 不得把 ``_next_due`` 拉回当前时间。"""
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("测试群", window_title="微信", rect=Rect(0, 0, 300, 500))

            class NoopCapturer:
                def reset(self, region_id):
                    pass

                def capture(self, source, force=False):
                    return None

            service, storage = self._service(directory, region, NoopCapturer())
            try:
                far_future = time.monotonic() + 60.0
                service._next_due[region.id] = far_future
                service._fail_region(
                    region.id,
                    service._generation[region.id],
                    "窗口采集组件超过 5.0 秒未返回",
                    code=CaptureErrorCode.CAPTURE_TIMEOUT,
                    trace=hung_trace(region.id),
                )
                self.assertEqual(service._next_due[region.id], far_future)
                self.assertIs(service._runtime[region.id].status, RegionStatus.ERROR)
                self.assertTrue(service._runtime[region.id].failed)
            finally:
                storage.close()

    def test_fault_stage_line_carries_region_and_request(self):
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("测试群", window_title="微信", rect=Rect(0, 0, 300, 500))

            class NoopCapturer:
                def reset(self, region_id):
                    pass

                def capture(self, source, force=False):
                    return None

            service, storage = self._service(directory, region, NoopCapturer())
            try:
                with StageLogCapture() as messages:
                    service._fail_region(
                        region.id,
                        service._generation[region.id],
                        "窗口采集组件超过 5.0 秒未返回",
                        code=CaptureErrorCode.CAPTURE_SESSION_HUNG,
                        trace=hung_trace(region.id),
                    )
            finally:
                storage.close()
            joined = "\n".join(messages)
            self.assertIn("stage=fault", joined)
            self.assertIn(f"region_id={region.id}", joined)
            self.assertIn("request_id=3f9c1d2a", joined)
            self.assertIn("error_code=capture_session_hung", joined)
            self.assertIn("elapsed_ms=5003", joined)


class CycleTimingTests(unittest.TestCase):
    def test_every_cycle_logs_elapsed_and_interval(self):
        """P2/P3 的判据：每轮循环体墙钟 + 采样间隔都要留在日志里。"""
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("测试群", window_title="微信", rect=Rect(0, 0, 300, 500))
            config = AppConfig(
                regions=[region], keywords=["手机"], save_hit_images=False, poll_interval_ms=100
            )
            storage = Storage(Path(directory) / "cycle.db")

            class InstantCapturer:
                def reset(self, region_id):
                    pass

                def capture(self, source, force=False):
                    return FrameSample(source.id, 1, time.time(), ["普通消息"], source.rect)

            service = MonitorService(config, storage, FakeOcr(), capturer=InstantCapturer())
            try:
                with StageLogCapture() as messages:
                    with self.assertLogs("deal_watch.service", level="INFO") as logs:
                        service.start()
                        try:
                            deadline = time.monotonic() + 5
                            while time.monotonic() < deadline:
                                if service._runtime[region.id].status is RegionStatus.RUNNING:
                                    break
                                time.sleep(0.02)
                        finally:
                            service.stop()
                cycle_logs = [
                    record for record in logs.records if "cycle_elapsed_ms" in record.getMessage()
                ]
                self.assertTrue(cycle_logs, "采集循环没有留下 cycle_elapsed_ms")
                message = cycle_logs[0].getMessage()
                for field in (
                    "cycle_started_at",
                    "cycle_finished_at",
                    "cycle_elapsed_ms",
                    "interval_ms",
                ):
                    self.assertIn(field, message)
                self.assertIn("interval_ms=100", message)
                # 阶段行必须是纯 stage=cycle + 独立数值槽（不再把间隔编进阶段名）。
                cycles = find_lines(messages, "cycle")
                self.assertTrue(cycles, f"阶段日志里没有 cycle 行：{messages}")
                self.assertEqual(cycles[0]["stage"], "cycle")
                self.assertEqual(cycles[0]["interval_ms"], "100")
                self.assertIn("schedule_wait_ms", cycles[0])
                self.assertFalse(any("stage=cycle_" in line for line in messages))
            finally:
                storage.close()

    def test_recovery_writes_a_stage_line(self):
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("测试群", window_title="微信", rect=Rect(0, 0, 300, 500))
            config = AppConfig(
                regions=[region], keywords=["手机"], save_hit_images=False, poll_interval_ms=100
            )
            storage = Storage(Path(directory) / "recover.db")

            class OnceFailingCapturer:
                def __init__(self) -> None:
                    self.calls = 0

                def reset(self, region_id):
                    pass

                def capture(self, source, force=False):
                    self.calls += 1
                    if self.calls == 1:
                        raise CaptureError(CaptureErrorCode.CAPTURE_ORPHAN, "子进程可能残留")
                    return FrameSample(source.id, self.calls, time.time(), ["普通消息"], source.rect)

            service = MonitorService(
                config, storage, FakeOcr(), capturer=OnceFailingCapturer()
            )
            try:
                with StageLogCapture() as messages:
                    service.start()
                    try:
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            if any(line.startswith("stage=recovered") for line in messages):
                                break
                            time.sleep(0.02)
                    finally:
                        service.stop()
            finally:
                storage.close()
            joined = "\n".join(messages)
            self.assertIn("stage=fault", joined)
            self.assertIn("stage=recovered", joined)
            self.assertIn(f"region_id={region.id}", joined)


class LastTraceTests(unittest.TestCase):
    def test_last_trace_is_exposed_and_cleared(self):
        class Windows:
            def desktop_locked(self):
                return False

            def window_from_hwnd(self, hwnd):
                return WindowInfo(
                    hwnd=hwnd,
                    title="微信",
                    class_name=READY_WINDOW_CLASS,
                    rect=Rect(0, 0, 100, 80),
                    process_id=READY_PROCESS_ID,
                    process_path=READY_PROCESS_PATH,
                )

        class Client:
            def __init__(self) -> None:
                self.fail = False

            def capture(self, hwnd, backend="wgc", **kwargs):
                trace = CaptureTrace(
                    request_id=kwargs.get("request_id", ""),
                    region_id=kwargs.get("region_id", ""),
                    total_ms=12.0,
                )
                if self.fail:
                    raise WindowCaptureError(CaptureErrorCode.CAPTURE_FAILED, "采集失败", trace)
                return CapturedWindowFrame(
                    image=np.zeros((80, 100, 3), dtype=np.uint8),
                    width=100,
                    height=80,
                    captured_at=1.0,
                    backend=backend,
                    trace=trace,
                )

        region = ready_region(
            "测试群",
            window_title="微信",
            rect=Rect(0, 0, 60, 40),
            frame_size=(100, 80),
        )
        client = Client()
        capturer = WindowFrameCapturer(client=client, windows=Windows())
        frame = capturer.capture(region)
        self.assertIsNotNone(frame)
        self.assertIsNotNone(capturer.last_trace)
        self.assertEqual(capturer.last_trace.region_id, region.id)
        self.assertTrue(capturer.last_trace.request_id)

        client.fail = True
        with self.assertRaises(CaptureError) as context:
            capturer.capture(region)
        self.assertEqual(context.exception.code, CaptureErrorCode.CAPTURE_FAILED)
        self.assertEqual(capturer.last_trace.region_id, region.id)

        # 校验阶段就失败时不能把上一次请求的 trace 报上去。
        region.window_hwnd = 0
        with self.assertRaises(CaptureError):
            capturer.capture(region)
        self.assertIsNone(capturer.last_trace)


if __name__ == "__main__":
    unittest.main()
