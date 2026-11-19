"""0.2.2 T2.1 分段耗时骨架的离线证据。

对应任务书的 7 条验收里能离线量化的 5 条（1~4、6），外加报告格式的格式级用例：

1. 已知数列的 P50/P95/max 精确值（不是"非 None"）；
2. 冷启动只吃前 ``COLD_SAMPLE_COUNT`` 个样本：前 3 个 1000ms + 之后 10ms ⇒
   cold P50 ≈ 1000ms、steady P50 ≈ 10ms；
3. ``in_flight`` 处理中为正、空闲归零，且**队列空但 OCR 仍在处理**时仍 > 0；
4. 环形缓冲上限 240：写 300 个样本后长度不变，且保留最近 240 个；
6. 埋点开销：120 个周期的单周期增量（提醒级，不是硬门限）。

另外两头堵住"看起来有埋点、其实没接上"：

* `ServiceWiringTests`：跑真 `MonitorService`（假采集器 + 假 OCR），用**固定**耗时断言
  各段真的落了样本，且采集段复用 `CaptureTrace.total_ms` 而不是再墙钟一次；
* `ProbeInstrumentationTests`：`model_init` / `notify_send` 只存在于探针侧，
  单独验证这两个包装器各记一次、不重复计数。

本文件只用合成假件：不碰真实微信窗口、不起采集子进程、不写用户目录。
"""

from __future__ import annotations

import importlib
import math
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deal_watch.config import AppConfig, ModelConfig
from deal_watch.diagnostics import format_stage_timings, stage_timing_rows
from deal_watch.metrics import (
    COLD_BUCKET_CAPACITY,
    COLD_SAMPLE_COUNT,
    SAMPLE_CAPACITY,
    STAGE_CAPTURE,
    STAGE_MODEL_INIT,
    STAGE_NOTIFY_QUEUE,
    STAGE_NOTIFY_RATE_LIMIT,
    STAGE_NOTIFY_SEND,
    STAGE_OCR_ENGINE,
    STAGE_OCR_INFER,
    STAGE_OCR_QUEUE,
    STAGE_PENDING_REASONS,
    STAGE_POSTPROCESS,
    STAGE_PREPROCESS,
    STAGE_SCHEDULE_WAIT,
    STAGE_STORAGE_WRITE,
    STAGE_TRACKING_RULES,
    STAGE_WINDOWS,
    STAGES,
    OcrStageRecorder,
    OcrStageSample,
    RoundCompletion,
    StageStats,
    TimingRecorder,
    measure_overhead,
    percentile,
)
from deal_watch.models import (
    FrameSample,
    MatchEvent,
    OcrObservation,
    Rect,
    TextBlock,
)
from deal_watch.notify import NotifyMetrics
from deal_watch.service import MonitorService
from deal_watch.storage import Storage
from region_fixtures import ready_region

ROOT = Path(__file__).resolve().parents[1]


def _load_probe(name: str):
    """按名字加载 ``tools/`` 下的探针模块（不依赖 PYTHONPATH，不改调用方环境）。"""
    for entry in (str(ROOT / "src"), str(ROOT), str(ROOT / "tools")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    return importlib.import_module(name)


class PercentileTests(unittest.TestCase):
    """验收 1 的前置：百分位口径先钉死，后面的 P50/P95 才有意义。"""

    def test_known_series_percentiles_are_exact(self):
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        # 线性插值 (len-1)*ratio：P50 落在正中，P95 落在 40 与 50 之间 80% 处。
        self.assertEqual(percentile(values, 0.5), 30.0)
        self.assertEqual(percentile(values, 0.95), 48.0)
        self.assertEqual(percentile([7.0], 0.95), 7.0)
        self.assertEqual(percentile([], 0.5), 0.0)

    def test_stage_stats_of_known_series(self):
        stats = StageStats.of([10.0, 20.0, 30.0, 40.0, 50.0])
        self.assertEqual(stats.count, 5)
        self.assertEqual(stats.p50, 30.0)
        self.assertEqual(stats.p95, 48.0)
        self.assertEqual(stats.maximum, 50.0)
        self.assertEqual(
            stats.as_dict(), {"count": 5, "p50_ms": 30.0, "p95_ms": 48.0, "max_ms": 50.0}
        )

    def test_stage_stats_of_empty_is_all_zero(self):
        stats = StageStats.of([])
        self.assertEqual((stats.count, stats.p50, stats.p95, stats.maximum), (0, 0.0, 0.0, 0.0))


class RecorderStatTests(unittest.TestCase):
    """验收 1：整条链路上的 P50/P95/max，用已知数列断言精确值。"""

    def test_steady_bucket_percentiles_on_known_series(self):
        # cold_samples=0 把所有样本压进 steady，便于直接对上已知数列。
        recorder = TimingRecorder(cold_samples=0)
        for value in (10.0, 20.0, 30.0, 40.0, 50.0):
            recorder.record(STAGE_OCR_INFER, value, "r1")

        timing = recorder.stats(STAGE_OCR_INFER, "r1")
        self.assertEqual(timing.steady.count, 5)
        self.assertEqual(timing.steady.p50, 30.0)
        self.assertEqual(timing.steady.p95, 48.0)
        self.assertEqual(timing.steady.maximum, 50.0)
        self.assertEqual(timing.cold.count, 0)

    def test_cold_and_steady_split_on_known_series(self):
        # 冷启动必须**显式声明**：前 3 个标 cold=True，其余进 steady，两者都不混。
        recorder = TimingRecorder()
        self.assertEqual(recorder.cold_samples, COLD_BUCKET_CAPACITY)
        for value in (10.0, 20.0, 30.0):
            recorder.record(STAGE_CAPTURE, value, "r1", cold=True)
        for value in (40.0, 50.0):
            recorder.record(STAGE_CAPTURE, value, "r1")

        timing = recorder.stats(STAGE_CAPTURE, "r1")
        self.assertEqual(timing.cold.count, 3)
        self.assertEqual(timing.cold.p50, 20.0)
        self.assertEqual(timing.cold.p95, 29.0)
        self.assertEqual(timing.cold.maximum, 30.0)
        self.assertEqual(timing.steady.count, 2)
        self.assertEqual(timing.steady.p50, 45.0)
        self.assertEqual(timing.steady.p95, 49.5)
        self.assertEqual(timing.steady.maximum, 50.0)
        self.assertEqual(timing.total, 5)

    def test_cold_is_not_inferred_from_sample_count(self):
        """不显式标记的样本一律进 steady：每个区域前 N 个不再是默认冷启动。

        按"前 3 个样本"分桶会把第 4 个区域第一次识别（真实冷启动）算成 steady，
        又会把第 1 个区域换窗口后的热识别算成 cold，两个方向都错。
        """
        recorder = TimingRecorder()
        for index in range(10):
            recorder.record(STAGE_OCR_ENGINE, float(100 + index), "r1")
        timing = recorder.stats(STAGE_OCR_ENGINE, "r1")
        self.assertEqual(timing.cold.count, 0, "未标记的样本不得自动进 cold")
        self.assertEqual(timing.steady.count, 10)
        # 显式标记的仍然进 cold，且不会把 steady 的分布拖高。
        recorder.record(STAGE_OCR_ENGINE, 5000.0, "r2", cold=True)
        recorder.record(STAGE_OCR_ENGINE, 5.0, "r2")
        other = recorder.stats(STAGE_OCR_ENGINE, "r2")
        self.assertEqual((other.cold.count, other.steady.count), (1, 1))
        self.assertEqual(other.cold.p50, 5000.0)
        self.assertEqual(other.steady.p50, 5.0)

    def test_regions_do_not_mix(self):
        recorder = TimingRecorder(cold_samples=0)
        recorder.record(STAGE_CAPTURE, 10.0, "r1")
        recorder.record(STAGE_CAPTURE, 200.0, "r2")
        self.assertEqual(recorder.stats(STAGE_CAPTURE, "r1").steady.p50, 10.0)
        self.assertEqual(recorder.stats(STAGE_CAPTURE, "r2").steady.p50, 200.0)
        self.assertEqual(recorder.region_ids(), ["r1", "r2"])

    def test_bad_values_are_ignored(self):
        recorder = TimingRecorder()
        for value in (float("nan"), float("inf"), -1.0, "abc", None):
            recorder.record(STAGE_CAPTURE, value, "r1")  # type: ignore[arg-type]
        self.assertEqual(recorder.stats(STAGE_CAPTURE, "r1").total, 0)
        self.assertEqual(recorder.region_ids(), [])


class ColdStartIsolationTests(unittest.TestCase):
    """验收 2：冷启动只统计**真的**初始化/首次调用，steady 的 P50 必须不被拖高。"""

    def test_first_three_samples_are_cold_and_rest_are_steady(self):
        recorder = TimingRecorder()
        for _ in range(3):
            recorder.record(STAGE_OCR_INFER, 1000.0, "r1", cold=True)
        for _ in range(10):
            recorder.record(STAGE_OCR_INFER, 10.0, "r1")

        timing = recorder.stats(STAGE_OCR_INFER, "r1")
        self.assertEqual(timing.cold.count, 3, "显式标记为冷启动的样本必须计入 cold")
        self.assertAlmostEqual(timing.cold.p50, 1000.0, places=3)
        self.assertEqual(timing.steady.count, 10, "其余样本必须计入 steady")
        self.assertAlmostEqual(timing.steady.p50, 10.0, places=3, msg="steady 不能被冷启动拖高")
        self.assertEqual(timing.steady.p95, 10.0)
        self.assertEqual(timing.steady.maximum, 10.0)

    def test_cold_bucket_survives_ring_eviction(self):
        # cold 桶独立缓冲：跑满环形缓冲后，冷启动样本仍然读得到。
        recorder = TimingRecorder(capacity=10, cold_samples=3)
        for _ in range(3):
            recorder.record(STAGE_CAPTURE, 900.0, "r1", cold=True)
        for _ in range(50):
            recorder.record(STAGE_CAPTURE, 5.0, "r1")

        timing = recorder.stats(STAGE_CAPTURE, "r1")
        self.assertEqual(timing.cold.count, 3)
        self.assertEqual(timing.cold.p50, 900.0)
        self.assertEqual(timing.steady.count, 10, "steady 只保留环形缓冲内的最近样本")

    def test_reuse_round_is_not_diluted_by_zero_infer(self):
        """复用轮根本不该写推理样本：0ms 会把 P50/P95 稀释成假象。

        这里模拟"真实推理 12ms + 复用轮不记样本"：steady 的 P50 必须仍是 12ms。
        若实现把复用轮写成 ``record(STAGE_OCR_INFER, 0.0)``，P50 会掉到 6ms。
        """
        recorder = TimingRecorder()
        recorder.record(STAGE_OCR_INFER, 12.0, "r1", cold=True)
        for _ in range(9):
            recorder.record(STAGE_OCR_INFER, 12.0, "r1")
        timing = recorder.stats(STAGE_OCR_INFER, "r1")
        self.assertEqual(timing.steady.count, 9)
        self.assertEqual(timing.steady.p50, 12.0)
        self.assertEqual(timing.steady.p95, 12.0)
        self.assertEqual(timing.cold.p50, 12.0, "首次调用由 LocalOcr 显式标记为冷启动")


class RingBufferTests(unittest.TestCase):
    """验收 4：环形缓冲上限固定，内存不随时间增长，且保留的是最近样本。"""

    def test_write_300_samples_keeps_last_240(self):
        recorder = TimingRecorder()
        self.assertEqual(recorder.capacity, SAMPLE_CAPACITY)
        self.assertEqual(SAMPLE_CAPACITY, 240)

        for index in range(300):
            recorder.record(STAGE_CAPTURE, float(index), "r1", cold=index < 3)

        self.assertEqual(recorder.recent_count(STAGE_CAPTURE, "r1"), SAMPLE_CAPACITY)
        values = recorder.recent_values(STAGE_CAPTURE, "r1")
        self.assertEqual(len(values), SAMPLE_CAPACITY)
        self.assertEqual(values[0], 60.0, "第 61 个样本（索引 60）应是缓冲里的第一个")
        self.assertEqual(values[-1], 299.0)
        self.assertEqual(recorder.total_samples(STAGE_CAPTURE), 300, "累计计数含已淘汰样本")

        # steady 桶同样只保留最近 240 个（前 3 个显式标成冷启动）。
        timing = recorder.stats(STAGE_CAPTURE, "r1")
        self.assertEqual(timing.cold.count, 3)
        self.assertEqual(timing.steady.count, SAMPLE_CAPACITY)
        self.assertEqual(timing.steady.maximum, 299.0)


class InFlightTests(unittest.TestCase):
    """验收 3：in_flight 能区分"队列空了"和"处理完了"。"""

    def test_recorder_counts_peak_and_samples(self):
        recorder = TimingRecorder()
        self.assertEqual(recorder.in_flight, 0)
        recorder.set_in_flight(2)
        self.assertEqual(recorder.in_flight, 2)
        recorder.record_in_flight(5)
        recorder.record_in_flight(1)
        self.assertEqual(recorder.in_flight, 1)
        self.assertEqual(recorder.peak_in_flight, 5)
        self.assertEqual(recorder.in_flight_samples, 2)
        recorder.set_in_flight(-3)  # 负值夹到 0，不能把计数器拖成负数
        self.assertEqual(recorder.in_flight, 0)

    def test_in_flight_positive_while_processing_and_zero_when_idle(self):
        health = []
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("合成区域", rect=Rect(0, 0, 300, 500))
            config = AppConfig(
                regions=[region], keywords=["手机"], save_hit_images=False, poll_interval_ms=100
            )
            storage = Storage(Path(directory) / "inflight.db")
            recorder = TimingRecorder()
            release = threading.Event()
            started = threading.Event()

            class SingleShotCapturer:
                """只交一帧：之后返回 None，保证"队列里没有待处理帧"是成立的。"""

                def __init__(self):
                    self.sent = False

                def reset(self, region_id):
                    pass

                def capture(self, source, force=False):
                    if self.sent:
                        return None
                    self.sent = True
                    return FrameSample(
                        source.id, 1, time.time(), ["普通消息"], source.rect
                    )

            class BlockingOcr:
                def recognize(self, frame):
                    started.set()
                    if not release.wait(5):
                        raise TimeoutError("OCR 未被释放")
                    return OcrObservation(
                        region_id=frame.region_id,
                        sampled_at=frame.captured_at,
                        frame_sequence=frame.sequence,
                        blocks=[TextBlock("普通消息", 0.99, Rect(10, 10, 250, 25))],
                        inference_ms=1.0,
                    )

            service = MonitorService(
                config,
                storage,
                BlockingOcr(),
                capturer=SingleShotCapturer(),
                data_dir=Path(directory),
                on_health=health.append,
                metrics=recorder,
            )
            service.start()
            try:
                self.assertTrue(started.wait(5), "OCR 线程没有取到帧")
                # 关键场景：帧已从队列取出、OCR 还在处理 —— 队列为空但没处理完。
                self.assertEqual(len(service._pending), 0, "队列已空")
                self.assertGreater(service.in_flight(), 0, "队列空但 OCR 仍在处理时 in_flight 必须 > 0")
                self.assertEqual(service.in_flight(), 1)
                self.assertGreater(recorder.in_flight, 0)
                # 真服务 + 真 in_flight：即使序号已上报，也不能因"队列已空"提前收工。
                tracker = RoundCompletion()
                tracker.mark(region.id, 1)
                self.assertFalse(
                    tracker.is_settled({region.id: 1}, service.in_flight()),
                    "队列已空但 OCR 仍在处理时，验收不得提前结束",
                )

                release.set()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and service.in_flight() > 0:
                    time.sleep(0.01)
                self.assertEqual(service.in_flight(), 0, "空闲后 in_flight 必须归零")
                self.assertEqual(recorder.in_flight, 0)
            finally:
                release.set()
                service.stop()
                storage.close()

        self.assertTrue(health, "至少应有一次健康快照")
        self.assertTrue(
            any(snapshot.queue_depth == 0 and snapshot.in_flight > 0 for snapshot in health),
            "健康快照里应能看到「队列已空、但仍有帧在处理」的那一拍",
        )


class ServiceWiringTests(unittest.TestCase):
    """验收 1 + 5 的接线证据：埋点真的落了样本，且用的是**已有真值源**。

    采集段必须复用 ``CaptureTrace.total_ms``，不能再墙钟一次——这里把 trace 钉成
    123ms，若实现改成自己计时，P50 立刻不等于 123.0，用例就会红。
    """

    def test_service_records_every_stage_with_fixed_timings(self):
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("合成区域", rect=Rect(0, 0, 300, 500))
            config = AppConfig(
                regions=[region], keywords=["手机"], save_hit_images=False, poll_interval_ms=100
            )
            storage = Storage(Path(directory) / "wiring.db")
            recorder = TimingRecorder()
            recognized = threading.Event()

            class TracedCapturer:
                """假采集器：固定 123ms 采集耗时，交固定帧。"""

                last_trace = SimpleNamespace(total_ms=123.0, request_id="fixed")

                def __init__(self):
                    self.sequence = 0

                def reset(self, region_id):
                    pass

                def capture(self, source, force=False):
                    self.sequence += 1
                    return FrameSample(
                        source.id,
                        self.sequence,
                        time.time(),
                        ["普通消息", "手机 优惠 4999 元"],
                        source.rect,
                    )

            class FixedOcr:
                """假 OCR：固定 7ms 推理耗时。"""

                def __init__(self):
                    self.calls = 0

                def recognize(self, frame):
                    self.calls += 1
                    if self.calls >= 6:
                        recognized.set()
                    return OcrObservation(
                        region_id=frame.region_id,
                        sampled_at=frame.captured_at,
                        frame_sequence=frame.sequence,
                        blocks=[
                            TextBlock(text, 0.99, Rect(10, 10 + index * 60, 250, 25))
                            for index, text in enumerate(frame.image)
                        ],
                        inference_ms=7.0,
                    )

            ocr = FixedOcr()
            service = MonitorService(
                config,
                storage,
                ocr,
                capturer=TracedCapturer(),
                data_dir=Path(directory),
                metrics=recorder,
            )
            service.start()
            try:
                self.assertTrue(recognized.wait(10), "OCR 没有跑到 6 帧")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and still_processing(recorder):
                    time.sleep(0.01)
            finally:
                service.stop()
                storage.close()

            # 采集段 = CaptureTrace.total_ms（不是墙钟）
            capture = recorder.stats(STAGE_CAPTURE, region.id)
            self.assertGreater(capture.total, 0, "采集段必须落样本")
            self.assertEqual(capture.steady.p50, 123.0)
            self.assertEqual(capture.steady.maximum, 123.0)
            self.assertEqual(
                capture.cold.count,
                0,
                "采集段没有冷启动语义：服务侧不显式标记就不得自动进 cold",
            )

            # OCR 推理段 = OcrObservation.inference_ms
            infer = recorder.stats(STAGE_OCR_INFER, region.id)
            self.assertGreaterEqual(infer.total, 6)
            self.assertEqual(infer.steady.p50, 7.0)
            self.assertEqual(infer.steady.p95, 7.0)

            # 其余各段都必须有样本；否则报告里会出现一堆"无样本"，看报告的人
            # 无法区分"这段本来没有"和"漏埋点"。
            for stage in (
                STAGE_SCHEDULE_WAIT,
                STAGE_OCR_QUEUE,
                STAGE_POSTPROCESS,
                STAGE_TRACKING_RULES,
                STAGE_STORAGE_WRITE,
            ):
                self.assertGreater(
                    recorder.total_samples(stage), 0, f"{stage} 段没有样本，埋点没接上"
                )

            # 报告层：每个区域的快照能摊成带起止点的行。
            rows = stage_timing_rows(recorder.region_payload(region.id))
            self.assertEqual(len(rows), len(STAGES))
            self.assertTrue(all(row["start"] and row["end"] for row in rows))


    def test_queue_eviction_leaves_no_orphan_queued_at(self):
        """不变量：``_queued_at`` 的键必须始终等于 ``_pending`` 的键。

        队列满时被挤掉的帧不会再被 OCR 取出，它的入队时刻必须一起清掉；否则
        ``ocr_queue`` 段会读到一个跟本轮无关的旧时间戳（P95 会虚高到离谱）。
        """
        with tempfile.TemporaryDirectory() as directory:
            first = ready_region("区域甲", rect=Rect(0, 0, 300, 500))
            second = ready_region("区域乙", rect=Rect(0, 0, 300, 500))
            config = AppConfig(
                regions=[first, second],
                keywords=["手机"],
                save_hit_images=False,
                max_queue_size=1,
            )
            storage = Storage(Path(directory) / "evict.db")
            service = MonitorService(
                config,
                storage,
                ocr_engine=SimpleNamespace(recognize=lambda frame: None),
                capturer=object(),
                data_dir=Path(directory),
            )
            try:
                service._submit(FrameSample(first.id, 1, time.time(), ["普通消息"], first.rect))
                self.assertEqual(set(service._queued_at), {first.id})
                service._submit(FrameSample(second.id, 2, time.time(), ["普通消息"], second.rect))
                self.assertEqual(set(service._pending), {second.id})
                self.assertEqual(set(service._queued_at), set(service._pending))
                self.assertEqual(service.in_flight(), 1)
            finally:
                service.request_stop()
                storage.close()


def still_processing(recorder: TimingRecorder) -> bool:
    """收尾辅助：等 in_flight 归零，避免在写库中途读快照。"""
    return recorder.in_flight > 0


class LocalOcrStageObservationTests(unittest.TestCase):
    """OCR 分段观测：应用与探针共用 ``LocalOcr(observer=...)``，不再包一层计时壳。

    老实现是探针侧的 ``TimedOcr``：它把**整个 recognize** 记成 ``model_init``，
    与 ``inference_ms`` 完全重叠——同一段时间在报告里出现两次。这里钉死新的口径：

    * ``model_init`` 只有真正构造引擎的那一次有样本；
    * ``preprocess`` / ``ocr_engine`` / ``postprocess`` 每次识别各一条；
    * **不写** ``ocr_infer``（那一段由服务层用 inference_ms 记录），避免重复计数；
    * 冷启动按"首次调用/真实初始化"显式标记，不是按样本序号。
    """

    def _ocr(self, observer):
        import numpy as np

        from deal_watch.ocr import LocalOcr

        return LocalOcr(ModelConfig(), ROOT, observer=observer), np

    def test_stage_samples_are_explicit_and_not_duplicated(self):
        from deal_watch.ocr import LocalOcr

        recorder = TimingRecorder()
        samples: list[OcrStageSample] = []
        observer = OcrStageRecorder(recorder, on_complete=samples.append)
        ocr, np = self._ocr(observer)
        image = np.full((120, 160, 3), 250, dtype=np.uint8)
        frame = FrameSample("r1", 7, 1.0, image, Rect(0, 0, 160, 120))
        engine = SimpleNamespace(boxes=None, txts=None, scores=None)

        def load(_self):
            time.sleep(0.01)  # 真实初始化耗时：必须只出现在 model_init 段
            return lambda _prepared: engine

        with patch.object(LocalOcr, "_load", load):
            ocr.recognize(frame)
            ocr.recognize(FrameSample("r2", 9, 2.0, image, Rect(0, 0, 160, 120)))

        self.assertEqual(len(samples), 2)
        self.assertTrue(samples[0].cold_start, "首次调用是冷启动")
        self.assertFalse(samples[1].cold_start, "第二个区域复用已加载引擎，不是冷启动")
        self.assertGreater(samples[0].model_init_ms, 0.0, "第一次必须量到真实初始化")
        self.assertEqual(samples[1].model_init_ms, 0.0, "复用引擎时初始化耗时为 0")
        self.assertGreaterEqual(samples[0].total_ms, samples[0].model_init_ms)
        self.assertEqual(samples[0].frame_sequence, 7)

        # 初始化与首次识别总耗时分开：model_init 只有 1 个样本，总耗时每帧一条。
        self.assertEqual(recorder.total_samples(STAGE_MODEL_INIT), 1)
        self.assertEqual(recorder.total_samples(STAGE_PREPROCESS), 2)
        self.assertEqual(recorder.total_samples(STAGE_OCR_ENGINE), 2)
        self.assertEqual(recorder.total_samples(STAGE_POSTPROCESS), 2)
        self.assertEqual(
            recorder.total_samples(STAGE_OCR_INFER),
            0,
            "ocr_infer 由服务层用 inference_ms 记录；观测器再写一次就是重复计数",
        )
        engine_stats = recorder.stats(STAGE_OCR_ENGINE, "r1")
        self.assertEqual(engine_stats.cold.count, 1, "冷启动只落在真正的第一次调用上")
        self.assertEqual(engine_stats.steady.count, 0)

    def test_observer_failure_never_breaks_recognition(self):
        ocr, np = self._ocr(lambda _sample: (_ for _ in ()).throw(RuntimeError("boom")))
        image = np.full((120, 160, 3), 250, dtype=np.uint8)
        ocr._engine = lambda _prepared: SimpleNamespace(boxes=None, txts=None, scores=None)
        observation = ocr.recognize(FrameSample("r1", 1, 1.0, image, Rect(0, 0, 160, 120)))
        self.assertEqual(observation.frame_sequence, 1, "埋点异常不得影响识别结果")
        self.assertGreater(observation.inference_ms, 0.0)


class NotifyMetricsTests(unittest.TestCase):
    """通知分发路径：排队等待、限速等待、请求耗时、业务结果，四段都要有真值。"""

    def _payload(self) -> dict:
        return {
            "text": "手机 4999 元",
            "region_alias": "合成群",
            "keyword": "手机",
            "observed_at": 0.0,
            "event_id": "e1",
        }

    def test_feishu_client_records_request_duration_and_outcome(self):
        from deal_watch.notify import FeishuClient

        recorder = TimingRecorder()
        metrics = NotifyMetrics(recorder)
        client = FeishuClient("http://127.0.0.1:9/ok", metrics=metrics)
        sent: list[dict] = []
        with patch.object(FeishuClient, "_send_message", lambda self, message: sent.append(message)):
            client.send(self._payload())
        self.assertEqual(len(sent), 1)
        self.assertEqual(metrics.as_dict()["requests"], 1)
        self.assertEqual(metrics.request_outcomes, {"ok": 1})
        self.assertEqual(recorder.total_samples(STAGE_NOTIFY_SEND), 1)

    def test_dispatcher_records_queue_rate_limit_and_result(self):
        from deal_watch.notify import NotificationDispatcher

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "notify.db")
            recorder = TimingRecorder()
            metrics = NotifyMetrics(recorder)
            sent: list[dict] = []

            class FakeClient:
                def send(self, payload, **_kwargs):
                    sent.append(payload)

            dispatcher = NotificationDispatcher(
                storage, lambda: FakeClient(), metrics=metrics
            )
            try:
                storage.record_match(
                    MatchEvent("r1", "合成群", "手机", "手机 4999 元", "k1", time.time())
                )
                self.assertTrue(dispatcher.process_once())
            finally:
                storage.close()

            self.assertEqual(len(sent), 1)
            self.assertEqual(recorder.total_samples(STAGE_NOTIFY_QUEUE), 1)
            self.assertEqual(recorder.total_samples(STAGE_NOTIFY_RATE_LIMIT), 1)
            self.assertEqual(recorder.total_samples(STAGE_NOTIFY_SEND), 1, "兼容分支也要计时")
            self.assertEqual(recorder.outcome_counts("notify"), {"sent": 1})
            self.assertEqual(metrics.as_dict()["job_outcomes"], {"sent": 1})


class RoundCompletionTests(unittest.TestCase):
    """采样完成条件：按（区域, 序号）处理完成，不看队列深度与旧健康快照。"""

    def test_queue_empty_is_not_completion(self):
        tracker = RoundCompletion()
        targets = {"r1": 5, "r2": 5}
        # r1 已被 OCR 线程取走、正在处理：queue 为空但序号还没到。
        tracker.mark("r1", 4)
        tracker.mark("r2", 5)
        self.assertFalse(tracker.is_complete(targets))
        self.assertEqual(tracker.pending(targets), {"r1": 5})

        tracker.mark("r1", 5)
        self.assertTrue(tracker.is_complete(targets))
        self.assertEqual(tracker.pending(targets), {})

    def test_sequence_never_goes_backwards(self):
        tracker = RoundCompletion()
        tracker.mark("r1", 9)
        tracker.mark("r1", 3)  # 迟到的旧结果不能把进度拖回去
        self.assertEqual(tracker.completed_sequence("r1"), 9)
        tracker.mark("r1", 0)
        tracker.mark("r1", -1)
        tracker.mark("r1", "abc")
        self.assertEqual(tracker.completed_sequence("r1"), 9)
        self.assertFalse(tracker.is_complete({"r1": 10}))

    def test_snapshot_exposes_targets_and_pending(self):
        tracker = RoundCompletion()
        tracker.mark("r1", 2)
        snapshot = tracker.snapshot({"r1": 3, "r2": 1})
        self.assertEqual(snapshot["targets"], {"r1": 3, "r2": 1})
        self.assertEqual(snapshot["completed"], {"r1": 2})
        self.assertEqual(snapshot["pending"], {"r1": 3, "r2": 1})

    def test_in_flight_work_blocks_settlement(self):
        """序号到了但还有在途任务：验收不能提前结束。

        这正是"队列已空但 OCR 仍在处理"的形态：帧已从队列取走（queue_depth=0），
        OCR 线程还在算。只按序号判定会立刻放过，只按队列判定也会立刻放过；
        两个条件同时成立才允许结束。
        """
        tracker = RoundCompletion()
        targets = {"r1": 3}
        tracker.mark("r1", 3)
        self.assertTrue(tracker.is_complete(targets))
        self.assertFalse(tracker.is_settled(targets, 1), "在途任务 > 0 时不得算完成")
        self.assertFalse(tracker.is_settled(targets, 2))
        self.assertTrue(tracker.is_settled(targets, 0))
        self.assertTrue(tracker.is_settled(targets, -1), "负值按 0 处理，不能卡死")


class SnapshotShapeTests(unittest.TestCase):
    """报告契约：段名、起止点、无样本原因都要出现在快照里。"""

    def test_snapshot_carries_windows_and_pending_reasons(self):
        recorder = TimingRecorder()
        for value in (1.0, 2.0, 3.0, 12.0):
            recorder.record(STAGE_CAPTURE, value, "r1")
        snapshot = recorder.snapshot()

        self.assertEqual(snapshot["capacity"], SAMPLE_CAPACITY)
        self.assertEqual(snapshot["cold_samples"], COLD_SAMPLE_COUNT)
        self.assertEqual(set(snapshot["stage_windows"]), set(STAGES))
        for stage in STAGES:
            start, end = snapshot["stage_windows"][stage]
            self.assertTrue(start and end, f"{stage} 的起止点不能为空")
        self.assertEqual(snapshot["pending_stages"], dict(STAGE_PENDING_REASONS))
        self.assertIn("r1", snapshot["regions"])
        self.assertEqual(set(snapshot["regions"]["r1"]), set(STAGES))
        self.assertEqual(snapshot["outcomes"], {})

        row = snapshot["regions"]["r1"][STAGE_CAPTURE]
        self.assertEqual(row["steady"]["count"], 4)
        self.assertEqual(row["steady"]["p50_ms"], 2.5)
        self.assertEqual(row["cold"]["count"], 0)
        self.assertEqual(row["window"], list(STAGE_WINDOWS[STAGE_CAPTURE]))

    def test_new_stages_have_windows_and_no_sample_reasons(self):
        """新增的 OCR 子段与通知段都必须有起止点和"无样本"说明。"""
        for stage in (
            STAGE_PREPROCESS,
            STAGE_MODEL_INIT,
            STAGE_OCR_ENGINE,
            STAGE_NOTIFY_QUEUE,
            STAGE_NOTIFY_RATE_LIMIT,
            STAGE_NOTIFY_SEND,
        ):
            self.assertIn(stage, STAGE_WINDOWS)
            start, end = STAGE_WINDOWS[stage]
            self.assertTrue(start and end, f"{stage} 的起止点不能为空")
            self.assertTrue(STAGE_PENDING_REASONS[stage], f"{stage} 缺少无样本说明")

    def test_no_sample_note_is_not_zero_milliseconds(self):
        """没有样本时报告写"无样本（原因）"，绝不写 0.0ms。"""
        recorder = TimingRecorder()
        lines = format_stage_timings(recorder.region_payload("r1"), STAGE_PENDING_REASONS)
        joined = "\n".join(lines)
        self.assertIn("无样本", joined)
        self.assertNotIn("p50=0.0", joined)

    def test_stage_windows_cover_every_stage(self):
        self.assertEqual(set(STAGE_WINDOWS), set(STAGES))
        self.assertEqual(len(set(STAGES)), len(STAGES), "段名不得重复")


class ReportFormatTests(unittest.TestCase):
    """诊断报告的格式级用例：有样本写数字，无样本写原因，绝不写 0ms。"""

    def test_pending_stages_report_reason_instead_of_zero_ms(self):
        recorder = TimingRecorder()
        recorder.record(STAGE_SCHEDULE_WAIT, 30.0, "r1")
        recorder.record(STAGE_SCHEDULE_WAIT, 40.0, "r1")
        payload = recorder.region_payload("r1")

        lines = format_stage_timings(payload, STAGE_PENDING_REASONS)
        joined = "\n".join(lines)
        self.assertIn(STAGE_SCHEDULE_WAIT, joined)
        self.assertIn("p50=35.0", joined)
        # notify_send 本期没有样本：必须写原因，不能写 0.0ms 冒充"很快"。
        self.assertIn(f"{STAGE_NOTIFY_SEND:<16} 无样本（{STAGE_PENDING_REASONS[STAGE_NOTIFY_SEND]}）", joined)
        self.assertNotIn(f"{STAGE_NOTIFY_SEND:<16} steady n=0 p50=0.0", joined)

    def test_rows_expose_start_and_end(self):
        recorder = TimingRecorder()
        recorder.record(STAGE_OCR_INFER, 8.0, "r1")
        rows = {row["stage"]: row for row in stage_timing_rows(recorder.region_payload("r1"))}
        self.assertEqual(rows[STAGE_OCR_INFER]["start"], STAGE_WINDOWS[STAGE_OCR_INFER][0])
        self.assertEqual(rows[STAGE_OCR_INFER]["end"], STAGE_WINDOWS[STAGE_OCR_INFER][1])
        self.assertEqual(rows[STAGE_OCR_INFER]["samples"], 1)

    def test_include_pending_false_drops_empty_stages(self):
        recorder = TimingRecorder()
        recorder.record(STAGE_OCR_INFER, 8.0, "r1")
        lines = format_stage_timings(
            recorder.region_payload("r1"), STAGE_PENDING_REASONS, include_pending=False
        )
        self.assertEqual(len(lines), 1)
        self.assertIn(STAGE_OCR_INFER, lines[0])

    def test_health_snapshot_defaults_keep_positional_construction(self):
        # 新增字段必须全部带默认值：老的位置参数构造不能坏。
        from deal_watch.models import HealthSnapshot, RegionStatus

        snapshot = HealthSnapshot("r1", RegionStatus.RUNNING)
        self.assertEqual(snapshot.stages, {})
        self.assertEqual(snapshot.stages_summary, "")
        self.assertEqual(snapshot.in_flight, 0)
        self.assertEqual((snapshot.ocr_reused, snapshot.ocr_cache_hits), (0, 0))
        self.assertEqual(snapshot.ocr_hit_rate, 0.0)
        positional = HealthSnapshot("r1", RegionStatus.RUNNING, "detail", 1.0, 2.0, 3, 4)
        self.assertEqual(positional.queue_depth, 3)
        self.assertEqual(positional.dropped_frames, 4)


class ThreadSafetyTests(unittest.TestCase):
    """采集线程、OCR 线程、UI 线程都会写：并发写入不能丢样本、不能抛异常。"""

    def test_concurrent_record_keeps_every_sample(self):
        recorder = TimingRecorder(capacity=1000, cold_samples=0)
        writers = 4
        per_writer = 250

        def worker(index: int) -> None:
            for slot in range(per_writer):
                recorder.record(STAGE_TRACKING_RULES, float(slot % 13), f"r{index % 2}")
                recorder.set_in_flight(slot % 4)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(recorder.total_samples(STAGE_TRACKING_RULES), writers * per_writer)
        self.assertGreaterEqual(recorder.peak_in_flight, 3)


class OverheadTests(unittest.TestCase):
    """验收 6：120 个周期的埋点增量。提醒级证据，不是硬门限。"""

    def test_overhead_per_cycle_stays_under_reminder_budget(self):
        # 取三轮最小值，排除调度抖动；测的是稳态成本（内部已预热）。
        results = [measure_overhead(120) for _ in range(3)]
        best = min(results, key=lambda item: item["per_cycle_ms"])

        self.assertEqual(best["cycles"], 120)
        self.assertEqual(best["calls_per_cycle"], 33, "32 次段写入 + 1 次 in_flight")
        self.assertTrue(math.isfinite(best["per_cycle_ms"]))
        self.assertGreater(best["per_cycle_ms"], 0.0)
        # 提醒门槛 2ms/周期：实测远低于此（约 0.02ms）。超了要写进交付说明。
        self.assertLess(
            best["per_cycle_ms"],
            best["budget_ms"],
            f"单周期埋点 {best['per_cycle_ms']}ms 已超提醒门槛 {best['budget_ms']}ms，"
            "需在报告与交付说明里写明",
        )


if __name__ == "__main__":
    unittest.main()
