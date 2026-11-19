"""阶段日志的数值槽与 cycle 行形状（T1.5 收尾）。

三件事：

1. **数值槽必须是真数字**：``interval_ms="4999 元"`` 这类字符串不能伪装成诊断
   字段落盘（否则字段白名单形同虚设）；
2. **一行 ``stage=cycle`` 同时能读到** ``interval_ms`` / ``schedule_wait_ms`` /
   ``capture_timeout_ms`` / ``wgc_timeout_ms`` / ``in_flight`` —— 现场那次 43 秒
   到底是不是生产参数、采集线程有没有被占住，只看这一份文件就能判定；
3. ``schedule_wait_ms`` 确实反映“排队等采集线程空出来”的时间，并在超过阈值时
   署上名（``region_id``），这是 P2 与“子进程自己慢”的分水岭。

同时用断言把 ``service`` 里那两个诊断常量绑到真值上（``WindowCaptureClient.timeout``
与 ``capture_wgc`` 的签名默认值），防止哪天有人只改一边。
"""

from __future__ import annotations

import importlib
import inspect
import sys
import tempfile
import time
import unittest
from pathlib import Path

from deal_watch.config import AppConfig
from deal_watch.diagnostics import STAGE_FIELD_ALLOWLIST, sanitize_stage_fields
from deal_watch.models import (
    CaptureErrorCode,
    FrameSample,
    OcrObservation,
    Rect,
    RegionStatus,
    TextBlock,
)
from deal_watch.service import (
    DIAGNOSTIC_CAPTURE_TIMEOUT_MS,
    DIAGNOSTIC_WGC_TIMEOUT_MS,
    SCHEDULE_WAIT_REPORT_MS,
    MonitorService,
)
from deal_watch.storage import Storage
from deal_watch.window_capture import WindowCaptureClient
from region_fixtures import ready_region
from stage_log_fixture import StageLogCapture, find_lines, parse_line


class FakeOcr:
    def recognize(self, frame):
        return OcrObservation(
            region_id=frame.region_id,
            sampled_at=frame.captured_at,
            frame_sequence=frame.sequence,
            blocks=[TextBlock("普通消息", 0.99, Rect(10, 10, 250, 25))],
            inference_ms=1.0,
        )


class NumericSlotTests(unittest.TestCase):
    def test_allowlist_has_the_diagnostic_numeric_slots(self):
        expected = {
            "elapsed_ms",
            "interval_ms",
            "capture_timeout_ms",
            "wgc_timeout_ms",
            "schedule_wait_ms",
            "in_flight",
        }
        self.assertLessEqual(expected, set(STAGE_FIELD_ALLOWLIST))

    def test_real_numbers_are_kept_and_integers_are_normalised(self):
        clean = sanitize_stage_fields(
            {
                "stage": "cycle",
                "interval_ms": 60000,
                "capture_timeout_ms": 5000.0,
                "wgc_timeout_ms": 3000,
                "schedule_wait_ms": 43125.678,
                "in_flight": 2,
                "elapsed_ms": 18.2365,
            }
        )
        self.assertEqual(clean["interval_ms"], 60000)
        self.assertEqual(clean["capture_timeout_ms"], 5000)
        self.assertEqual(clean["wgc_timeout_ms"], 3000)
        self.assertEqual(clean["schedule_wait_ms"], 43125.678)
        self.assertEqual(clean["in_flight"], 2)
        self.assertEqual(clean["elapsed_ms"], 18.236)

    def test_strings_cannot_disguise_as_numbers(self):
        clean = sanitize_stage_fields(
            {
                "stage": "cycle",
                "interval_ms": "4999 元",
                "schedule_wait_ms": "60000",
                "in_flight": "2",
            }
        )
        self.assertEqual(clean, {"stage": "cycle"})

    def test_bool_nan_inf_and_negative_are_rejected(self):
        for bad in (True, False, float("nan"), float("inf"), -1, -0.5):
            with self.subTest(value=bad):
                clean = sanitize_stage_fields({"stage": "cycle", "interval_ms": bad})
                self.assertEqual(clean, {"stage": "cycle"})

    def test_row_is_readable_and_stable(self):
        clean = sanitize_stage_fields(
            {
                "stage": "cycle",
                "elapsed_ms": 3.1,
                "interval_ms": 60000,
                "capture_timeout_ms": 5000,
                "wgc_timeout_ms": 3000,
                "schedule_wait_ms": 0.0,
                "in_flight": 0,
                "outcome": "ok",
            }
        )
        rendered = " ".join(f"{key}={clean[key]}" for key in STAGE_FIELD_ALLOWLIST if key in clean)
        self.assertEqual(
            rendered,
            "stage=cycle elapsed_ms=3.1 interval_ms=60000 capture_timeout_ms=5000 "
            "wgc_timeout_ms=3000 schedule_wait_ms=0 in_flight=0 outcome=ok",
        )


class DiagnosticConstantTests(unittest.TestCase):
    def test_capture_budget_matches_the_real_default(self):
        self.assertEqual(WindowCaptureClient().timeout * 1000.0, DIAGNOSTIC_CAPTURE_TIMEOUT_MS)

    def test_wgc_timeout_matches_the_component_default(self):
        # capture_worker 导入时会把 .capture-runtime 插到 sys.path 首位，
        # 用完立即还原，避免把运行时自带的 numpy 顶到前面影响后续用例。
        original = list(sys.path)
        try:
            module = importlib.import_module("deal_watch.capture_worker")
            default = inspect.signature(module.capture_wgc).parameters["timeout"].default
        finally:
            sys.path[:] = original
        self.assertEqual(default * 1000.0, DIAGNOSTIC_WGC_TIMEOUT_MS)


class CycleLineTests(unittest.TestCase):
    def _service(self, directory: str, capturer, regions, interval_ms: int = 100):
        config = AppConfig(
            regions=regions,
            keywords=["手机"],
            save_hit_images=False,
            poll_interval_ms=interval_ms,
        )
        storage = Storage(Path(directory) / "cycle.db")
        return MonitorService(config, storage, FakeOcr(), capturer=capturer), storage

    def test_cycle_line_carries_interval_and_budget_beside_schedule_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("测试群", window_title="微信", rect=Rect(0, 0, 300, 500))

            class InstantCapturer:
                def reset(self, region_id):
                    pass

                def capture(self, source, force=False):
                    return FrameSample(source.id, 1, time.time(), ["普通消息"], source.rect)

            service, storage = self._service(directory, InstantCapturer(), [region])
            try:
                with StageLogCapture() as messages:
                    service.start()
                    try:
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            if service._runtime[region.id].status is RegionStatus.RUNNING:
                                break
                            time.sleep(0.02)
                    finally:
                        service.stop()
            finally:
                storage.close()

            cycles = find_lines(messages, "cycle")
            self.assertTrue(cycles, f"没有 stage=cycle 的行：{messages}")
            line = cycles[0]
            # stage 名必须是纯 cycle，不再带毫秒后缀。
            self.assertEqual(line["stage"], "cycle")
            self.assertEqual(line["interval_ms"], "100")
            self.assertEqual(line["capture_timeout_ms"], "5000")
            self.assertEqual(line["wgc_timeout_ms"], "3000")
            self.assertIn("schedule_wait_ms", line)
            self.assertIn("in_flight", line)
            self.assertIn(line["outcome"], {"ok", "overdue"})
            self.assertFalse(any("stage=cycle_" in message for message in messages))

    def test_schedule_wait_reports_the_queued_region(self):
        """A 占住采集线程 250ms 后，B 的排队等待必须体现在 cycle 行上。"""
        with tempfile.TemporaryDirectory() as directory:
            first = ready_region("先采区域", window_title="微信", rect=Rect(0, 0, 100, 100))
            second = ready_region("排队区域", window_title="微信", rect=Rect(0, 0, 100, 100))

            class SlowFirstCapturer:
                def __init__(self) -> None:
                    self.blocked = 0

                def reset(self, region_id):
                    pass

                def capture(self, source, force=False):
                    if source.id == first.id and self.blocked == 0:
                        self.blocked = 1
                        time.sleep(0.25)
                    return FrameSample(source.id, 1, time.time(), ["普通消息"], source.rect)

            service, storage = self._service(
                directory, SlowFirstCapturer(), [first, second]
            )
            try:
                with StageLogCapture() as messages:
                    service.start()
                    try:
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            if service._runtime[second.id].status is RegionStatus.RUNNING:
                                break
                            time.sleep(0.02)
                    finally:
                        service.stop()
            finally:
                storage.close()

            cycles = find_lines(messages, "cycle")
            slow = [
                line
                for line in cycles
                if float(line.get("schedule_wait_ms", "0")) >= SCHEDULE_WAIT_REPORT_MS
            ]
            self.assertTrue(slow, f"没有记录到排队等待：{cycles}")
            self.assertEqual(slow[0]["region_id"], second.id)
            self.assertEqual(slow[0]["outcome"], "overdue")

    def test_fault_and_recovery_lines_keep_their_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("测试群", window_title="微信", rect=Rect(0, 0, 300, 500))

            class NoopCapturer:
                def reset(self, region_id):
                    pass

                def capture(self, source, force=False):
                    return None

            service, storage = self._service(directory, NoopCapturer(), [region])
            try:
                with StageLogCapture() as messages:
                    service._fail_region(
                        region.id,
                        service._generation[region.id],
                        "窗口采集组件超过 5.0 秒未返回",
                        code=CaptureErrorCode.CAPTURE_TIMEOUT,
                    )
            finally:
                storage.close()
            faults = find_lines(messages, "fault")
            self.assertEqual(len(faults), 1)
            self.assertEqual(faults[0]["region_id"], region.id)
            self.assertEqual(faults[0]["error_code"], "capture_timeout")
            self.assertEqual(faults[0]["outcome"], "capture_timeout")
            # 数值槽仍然是数字（没有 trace 时 elapsed_ms 记 0）。
            self.assertEqual(faults[0]["elapsed_ms"], "0")


class RenderedSampleTests(unittest.TestCase):
    """把真实落盘的一行渲染出来，供交付报告贴样例（脱敏，天然无用户内容）。"""

    def test_sample_line_shape(self):
        line = sanitize_stage_fields(
            {
                "stage": "cycle",
                "elapsed_ms": 3.1,
                "interval_ms": 60000,
                "capture_timeout_ms": 5000,
                "wgc_timeout_ms": 3000,
                "schedule_wait_ms": 0.0,
                "in_flight": 0,
                "outcome": "ok",
                "chat_text": "手机 4999 元",
            }
        )
        self.assertNotIn("chat_text", line)
        self.assertEqual(parse_line("stage=cycle elapsed_ms=3.1")["elapsed_ms"], "3.1")


if __name__ == "__main__":
    unittest.main()
