"""0.2.2 T2.3「静止画面 OCR 复用」的离线证据。

**这一项的风险不在于省不省时间，而在于"复用"会不会把"判断"也一起跳过。**
因此本文件的重点不是命中率，而是下面三组不变量：

* 复用只跳过 ONNX 推理：``tracker.process`` 与 ``rules.decide`` 一次都不能少；
* 复用的观测必须换成"本次帧"的时间戳与序号，否则跟踪层会收到更旧的观测；
* 静止消息在复用下跨冷却期仍不重发，画面一变就重新可通知。

10 条验收逐条对应到用例，末尾 ``OcrCallCountComparisonTests`` 给出验收要的
「10 轮静止画面：关闭复用 vs 开启复用」调用次数对照。

全部用例只用合成帧与临时目录：不加载真实模型、不碰微信窗口、不占屏。
"""

from __future__ import annotations

import hashlib
import importlib
import os
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import fields, is_dataclass
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from deal_watch.config import AppConfig, ConfigStore  # noqa: E402
from deal_watch.metrics import STAGE_OCR_INFER  # noqa: E402
from deal_watch.models import (  # noqa: E402
    FrameSample,
    MessageCandidate,
    OcrObservation,
    Rect,
    TextBlock,
)
from deal_watch.ocr_cache import (  # noqa: E402
    DEFAULT_MAX_AGE_SECONDS,
    OcrCacheContext,
    ReusableOcr,
    ReuseStats,
    pixel_signature,
)
from deal_watch.rules import RuleEngine  # noqa: E402
from deal_watch.service import MonitorService  # noqa: E402
from deal_watch.storage import Storage  # noqa: E402
from region_fixtures import ready_region  # noqa: E402


def frame(
    image: object,
    *,
    region_id: str = "r1",
    sequence: int = 1,
    captured_at: float = 1.0,
) -> FrameSample:
    return FrameSample(region_id, sequence, captured_at, image, Rect(0, 0, 320, 480))


def context(
    *,
    region_id: str = "r1",
    generation: int = 0,
    reference_width: int = 800,
    reference_height: int = 1280,
    backend: str = "wgc",
    roi: tuple[int, int, int, int] = (10, 20, 300, 400),
) -> OcrCacheContext:
    return OcrCacheContext(
        region_id=region_id,
        generation=generation,
        reference_width=reference_width,
        reference_height=reference_height,
        backend=backend,
        roi=roi,
    )


def roi_image(value: int = 128) -> np.ndarray:
    """固定内容的 ROI 帧；``value`` 改一个数就是"改一个像素"。"""
    image = np.full((30, 20, 3), value, dtype=np.uint8)
    image[5, 5] = [1, 2, 3]
    return image


def _walk(value: object):
    """深度遍历 dataclass / list / tuple，用于"缓存里到底有什么"的断言。"""
    yield value
    if isinstance(value, list | tuple):
        for item in value:
            yield from _walk(item)
    elif is_dataclass(value):
        for field in fields(value):
            yield from _walk(getattr(value, field.name))


def _load_probe(name: str):
    """按名字加载 ``tools/`` 下的探针模块（与 ``test_metrics`` 同一做法）。"""
    root = Path(__file__).resolve().parents[1]
    for entry in (str(root / "src"), str(root), str(root / "tools")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    return importlib.import_module(name)


class CountingOcr:
    """计数假 OCR：每次真正识别都要被数到。"""

    def __init__(self, text: str = "手机 优惠 4999 元") -> None:
        self.calls = 0
        self.text = text
        self.inference_ms = 12.0

    def recognize(self, sample: FrameSample) -> OcrObservation:
        self.calls += 1
        return OcrObservation(
            region_id=sample.region_id,
            sampled_at=sample.captured_at,
            frame_sequence=sample.sequence,
            blocks=[TextBlock(self.text, 0.99, Rect(10, 10, 250, 25))],
            inference_ms=self.inference_ms,
        )


class ReusableOcrCoreTests(unittest.TestCase):
    """验收 1/2/3/4：命中、像素变化、过期、关闭等价。"""

    def test_hit_does_not_call_inner_ocr_again(self):
        """验收 1：两帧完全相同 → 第 2 帧 reused=True，OCR 调用次数不变。"""
        inner = CountingOcr()
        cache = ReusableOcr(inner, max_age_seconds=300)
        image = roi_image()

        first = cache.recognize(frame(image, sequence=1, captured_at=1.0), context())
        second = cache.recognize(frame(image, sequence=2, captured_at=2.0), context())

        self.assertFalse(first.reused)
        self.assertTrue(second.reused, "像素完全相同的第二帧必须复用")
        self.assertEqual(inner.calls, 1, "复用不得再调一次 OCR")
        stats = cache.stats_for("r1")
        self.assertEqual((stats.lookups, stats.hits, stats.reused), (2, 1, 1))

        # 复用的观测要换成本次帧的时间戳与序号（否则跟踪层收到"更旧"的观测）。
        self.assertEqual(second.sampled_at, 2.0)
        self.assertEqual(second.frame_sequence, 2)
        self.assertEqual(second.inference_ms, 0.0, "复用帧没有推理耗时")
        self.assertEqual([block.text for block in second.blocks], [inner.text])

    def test_digest_covers_bottom_rows(self):
        """摘要必须覆盖 ROI 全部像素：改最后一行也必须重新识别。

        变异验证（team-lead 的 B）：把 ``pixel_signature`` 里的
        ``array.tobytes()`` 改成 ``array[:8].tobytes()``，本条必须失败。
        新消息出现在聊天窗**底部**，一个只哈希上半部分的实现会把它当静止画面，
        直接漏报 —— 这是本项最危险的失败模式。
        """
        inner = CountingOcr()
        cache = ReusableOcr(inner, max_age_seconds=300)
        first_image = roi_image()
        changed = roi_image()
        changed[-1, -1] = [9, 9, 9]

        cache.recognize(frame(first_image), context())
        result = cache.recognize(frame(changed), context())

        self.assertFalse(result.reused, "只改了最后一个像素就被当静止画面：摘要没覆盖全像素")
        self.assertEqual(inner.calls, 2)

    def test_new_bottom_row_forces_real_ocr(self):
        """底部新出现一条消息（最后一行整行变化）→ 必须重新识别。"""
        inner = CountingOcr()
        cache = ReusableOcr(inner, max_age_seconds=300)
        first_image = roi_image()
        changed = roi_image()
        changed[-1, :] = [200, 30, 30]

        cache.recognize(frame(first_image), context())
        result = cache.recognize(frame(changed), context())

        self.assertFalse(result.reused, "最后一行整行变化被当成静止画面")
        self.assertEqual(inner.calls, 2)

    def test_digest_covers_all_edges(self):
        """四角/上下左右任一处变化都必须重新识别。

        ``test_one_pixel_change_forces_real_ocr`` 改的是 ``[7, 9]`` —— 恰好落在
        "只哈希前 8 行"这类错误实现的覆盖区内，因此那条用例单独存在时证明不了
        覆盖范围。这里把四条边各试一次，堵住"只哈希某个角落"的所有变体。
        """

        def top_left(image):
            image[0, 0] = [9, 9, 9]

        def bottom_right(image):
            image[-1, -1] = [9, 9, 9]

        def last_row(image):
            image[-1, :] = [200, 30, 30]

        def last_column(image):
            image[:, -1] = [200, 30, 30]

        for label, mutate in (
            ("左上角", top_left),
            ("右下角", bottom_right),
            ("最后一行整行", last_row),
            ("最后一列整列", last_column),
        ):
            inner = CountingOcr()
            cache = ReusableOcr(inner, max_age_seconds=300)
            first_image = roi_image()
            changed = roi_image()
            mutate(changed)

            cache.recognize(frame(first_image), context())
            result = cache.recognize(frame(changed), context())

            self.assertFalse(result.reused, f"{label} 变化被当成静止画面：摘要没覆盖全像素")
            self.assertEqual(inner.calls, 2, f"{label} 变化必须重新识别")

    def test_one_pixel_change_forces_real_ocr(self):
        """验收 2：改动 1 个像素 → reused=False，重新调 OCR。"""
        inner = CountingOcr()
        cache = ReusableOcr(inner, max_age_seconds=300)
        first_image = roi_image()
        changed = roi_image()
        changed[7, 9] = [9, 9, 9]

        cache.recognize(frame(first_image), context())
        result = cache.recognize(frame(changed), context())

        self.assertFalse(result.reused)
        self.assertEqual(inner.calls, 2, "像素变了必须重新识别")

    def test_expired_entry_is_not_reused(self):
        """验收 3：超过 max_age → 不复用（这里把上限设成 1 秒）。"""
        inner = CountingOcr()
        clock = {"now": 1000.0}
        cache = ReusableOcr(inner, max_age_seconds=1, clock=lambda: clock["now"])
        image = roi_image()

        cache.recognize(frame(image, sequence=1), context())
        clock["now"] += 0.4
        self.assertTrue(cache.recognize(frame(image, sequence=2), context()).reused)
        clock["now"] += 1.5  # 距离第一次存储已 1.9 秒 > 1 秒
        expired = cache.recognize(frame(image, sequence=3), context())

        self.assertFalse(expired.reused, "超过有效期必须重新识别")
        stats = cache.stats_for("r1")
        self.assertEqual(stats.hits, 2, "键仍在，所以两次都算命中")
        self.assertEqual(stats.reused, 1, "只有 1 次真正复用")
        self.assertEqual(inner.calls, 2)

    def test_zero_max_age_disables_reuse_entirely(self):
        """验收 4：max_age=0 → 全程不复用，连缓存都不写。"""
        inner = CountingOcr()
        cache = ReusableOcr(inner, max_age_seconds=0)
        image = roi_image()
        results = [
            cache.recognize(frame(image, sequence=index), context()) for index in range(5)
        ]
        self.assertEqual(inner.calls, 5)
        self.assertTrue(all(not item.reused for item in results))
        self.assertEqual(cache.cached_regions(), [])
        self.assertEqual(cache.stats_for("r1").lookups, 0, "关闭时连计数都不该有")
        self.assertFalse(cache.enabled)

    def test_non_image_frames_bypass_cache(self):
        """离线用例里的 ``image=["文本"]`` 之类不是图像：必须直通，不能崩。"""
        inner = CountingOcr()
        cache = ReusableOcr(inner, max_age_seconds=300)
        sample = frame(["普通消息"])
        self.assertIsNone(pixel_signature(sample))

        cache.recognize(sample, context())
        cache.recognize(sample, context())
        self.assertEqual(inner.calls, 2)
        self.assertEqual(cache.cached_regions(), [])

    def test_missing_context_bypasses_cache(self):
        inner = CountingOcr()
        cache = ReusableOcr(inner, max_age_seconds=300)
        image = roi_image()
        cache.recognize(frame(image, sequence=1))  # 不传上下文
        cache.recognize(frame(image, sequence=2))
        self.assertEqual(inner.calls, 2, "没有区域上下文时一律直通")


class CacheKeyTests(unittest.TestCase):
    """缓存键必须能区分"长得一样但不是同一块屏幕"。"""

    def test_context_change_invalidates_region_bucket(self):
        inner = CountingOcr()
        cache = ReusableOcr(inner, max_age_seconds=300)
        image = roi_image()

        cache.recognize(frame(image, sequence=1), context())
        self.assertEqual(cache.entry_count("r1"), 1)

        # 换后端：像素一模一样，也不许复用（同一张图可能来自不同渲染路径）。
        changed_backend = cache.recognize(
            frame(image, sequence=2), context(backend="printwindow")
        )
        self.assertFalse(changed_backend.reused)
        self.assertEqual(inner.calls, 2)

        # 参考帧尺寸 / ROI / generation 各自单独验一遍。
        for kwargs in (
            {"reference_width": 1024, "reference_height": 1536},
            {"roi": (0, 0, 300, 400)},
            {"generation": 7},
        ):
            cache.recognize(frame(image, sequence=3), context())
            before = inner.calls
            result = cache.recognize(frame(image, sequence=4), context(**kwargs))
            self.assertFalse(result.reused, f"{kwargs} 变化后不得复用")
            self.assertEqual(inner.calls, before + 1)

    def test_different_regions_do_not_share_entries(self):
        inner = CountingOcr()
        cache = ReusableOcr(inner, max_age_seconds=300)
        image = roi_image()
        cache.recognize(frame(image), context(region_id="r1"))
        shared = cache.recognize(frame(image), context(region_id="r2"))
        self.assertFalse(shared.reused, "两个区域即使像素相同也不能互相复用")
        self.assertEqual(inner.calls, 2)

    def test_shape_change_is_not_a_hit(self):
        """验收：帧尺寸变化 → 摘要算法即使碰巧相同也不能算命中。"""
        inner = CountingOcr()
        cache = ReusableOcr(inner, max_age_seconds=300)
        tall = np.full((30, 20, 3), 7, dtype=np.uint8)
        wide = np.full((30, 40, 3), 7, dtype=np.uint8)
        cache.recognize(frame(tall), context())
        result = cache.recognize(frame(wide), context())
        self.assertFalse(result.reused, "不同尺寸的帧不得复用")
        self.assertEqual(inner.calls, 2)
        # shape 不同时连摘要都不该复用：两条独立条目，不是"同一条被覆盖"。
        self.assertEqual(cache.entry_count("r1"), 2)

    def test_dtype_change_is_not_a_hit(self):
        """验收：uint8 → float32（像素值相同）也不得复用。"""
        inner = CountingOcr()
        cache = ReusableOcr(inner, max_age_seconds=300)
        plain = np.full((30, 20, 3), 9, dtype=np.uint8)
        floating = plain.astype(np.float32)
        cache.recognize(frame(plain), context())
        result = cache.recognize(frame(floating), context())
        self.assertFalse(result.reused, "dtype 变化后不得复用")
        self.assertEqual(inner.calls, 2)

    def test_cache_never_holds_image_data(self):
        """红线：缓存条目里不许出现任何图像（``.shape`` / ``.nbytes``）对象。"""
        inner = CountingOcr()
        cache = ReusableOcr(inner, max_age_seconds=300)
        image = roi_image()
        cache.recognize(frame(image, sequence=1), context())

        entries = cache.inspect_entries("r1")
        self.assertEqual(len(entries), 1)
        inspected = 0
        for value in _walk(entries[0]):
            inspected += 1
            self.assertFalse(
                isinstance(value, np.ndarray),
                "缓存里出现了 ndarray：整帧图像被钉在内存里了",
            )
            self.assertFalse(
                hasattr(value, "nbytes") and hasattr(value, "shape"),
                f"缓存里出现了形似图像的对象：{type(value).__name__}",
            )
        self.assertGreater(inspected, 3, "扫描本身不能是空转")

    def test_cache_is_not_polluted_by_caller_mutation(self):
        """``blocks`` / ``uncovered_boxes`` 进出都必须拷贝。

        变异验证（team-lead 的 C）：把 ``store()`` 里的 ``blocks=list(...)`` 去掉，
        本条必须失败。``OcrObservation`` 是非 frozen dataclass，``blocks`` 是可变
        list —— 缓存与返回值共用同一个容器时，下游任何一个 ``append`` / ``pop`` /
        ``sort`` 都会**直接改写缓存**，下一轮复用就拿到被改过的结果。
        """
        inner = CountingOcr()
        cache = ReusableOcr(inner, max_age_seconds=300)
        image = roi_image()

        first = cache.recognize(frame(image), context())
        baseline = len(first.blocks)
        self.assertEqual(baseline, 1)
        # 模拟下游原地改写（今天没有这样的消费方，但这是"现在不炸"而非"设计安全"）。
        first.blocks.append(first.blocks[0])
        first.uncovered_boxes.append(Rect(0, 0, 1, 1))
        first.inference_ms = 999.0

        second = cache.recognize(frame(image), context())

        self.assertTrue(second.reused)
        self.assertEqual(len(second.blocks), baseline, "下游 append 污染了缓存里的 blocks")
        self.assertEqual(len(second.uncovered_boxes), 0, "下游 append 污染了 uncovered_boxes")
        self.assertEqual(second.inference_ms, 0.0, "复用轮的 inference_ms 必须是 0.0")
        # 缓存里那条也要干净：不是"返回时拷了一份、缓存已被改坏"。
        stored = cache.inspect_entries("r1")[0][0]
        self.assertEqual(len(stored.blocks), baseline, "缓存条目里的 blocks 被下游改写了")
        self.assertEqual(len(stored.uncovered_boxes), 0)

    def test_invalidate_clears_only_that_region(self):
        inner = CountingOcr()
        cache = ReusableOcr(inner, max_age_seconds=300)
        image = roi_image()
        cache.recognize(frame(image), context(region_id="r1"))
        cache.recognize(frame(image), context(region_id="r2"))

        self.assertTrue(cache.invalidate("r1"))
        self.assertEqual(cache.cached_regions(), ["r2"])
        later = cache.recognize(frame(image), context(region_id="r1"))
        self.assertFalse(later.reused)
        self.assertEqual(inner.calls, 3)

    def test_invalidate_during_inference_does_not_write_back(self):
        """故障在推理途中发生：结果不得写回缓存，否则下一轮会复用故障前画面。"""
        gate = threading.Event()
        entered = threading.Event()

        class SlowOcr:
            def __init__(self) -> None:
                self.calls = 0

            def recognize(self, sample):
                self.calls += 1
                entered.set()
                if not gate.wait(5):
                    raise TimeoutError("未释放")
                return OcrObservation(sample.region_id, sample.captured_at, sample.sequence, [])

        inner = SlowOcr()
        cache = ReusableOcr(inner, max_age_seconds=300)
        image = roi_image()
        result: list[OcrObservation] = []

        worker = threading.Thread(
            target=lambda: result.append(cache.recognize(frame(image), context()))
        )
        worker.start()
        self.assertTrue(entered.wait(5))
        cache.invalidate("r1")  # 推理期间：故障 / 恢复 / 重新校准
        gate.set()
        worker.join(5)

        self.assertEqual(len(result), 1)
        self.assertEqual(cache.cached_regions(), [], "失效之后不许写回")
        self.assertFalse(cache.recognize(frame(image, sequence=2), context()).reused)

    def test_entries_are_bounded_per_region(self):
        inner = CountingOcr()
        cache = ReusableOcr(inner, max_age_seconds=300, max_entries=4)
        for index in range(20):
            cache.recognize(frame(roi_image(value=100 + index)), context())
        self.assertEqual(cache.entry_count("r1"), 4, "每区域最多 4 条（LRU）")

    def test_stats_and_summary_shapes(self):
        empty = ReuseStats()
        self.assertEqual(empty.hit_rate, 0.0)
        inner = CountingOcr()
        cache = ReusableOcr(inner, max_age_seconds=300)
        image = roi_image()
        for index in range(4):
            cache.recognize(frame(image, sequence=index), context())
        summary = cache.summary()
        self.assertEqual(summary["lookups"], 4)
        self.assertEqual(summary["reused"], 3)
        self.assertEqual(summary["cache_hits"], 3)
        self.assertAlmostEqual(summary["hit_rate"], 0.75)
        self.assertEqual(summary["per_region"]["r1"]["reused"], 3)


class ConfigTests(unittest.TestCase):
    """验收 4 的配置侧：默认 300、0 表示关闭、旧配置无该键取默认。"""

    def test_default_is_three_hundred_seconds(self):
        self.assertEqual(AppConfig().ocr_reuse_max_age_seconds, 300.0)
        self.assertEqual(DEFAULT_MAX_AGE_SECONDS, 300.0)

    def test_negative_is_rejected(self):
        with self.assertRaises(ValueError):
            AppConfig(ocr_reuse_max_age_seconds=-1)

    def test_old_config_without_key_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory))
            store.path.write_text(
                '{"version": 2, "regions": [], "keywords": ["手机"], "poll_interval_ms": 60000}',
                encoding="utf-8",
            )
            self.assertEqual(store.load().ocr_reuse_max_age_seconds, 300.0)

    def test_value_round_trips_through_save_and_load(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory))
            store.save(AppConfig(keywords=["手机"], ocr_reuse_max_age_seconds=0), verify=True)
            self.assertEqual(store.load().ocr_reuse_max_age_seconds, 0.0, "0 必须原样保存")

    def test_force_ocr_seconds_is_untouched(self):
        """复用上限与 force_ocr_seconds 是两套语义，不许互相改写。"""
        config = AppConfig()
        self.assertEqual(config.force_ocr_seconds, 10.0)
        self.assertEqual(config.ocr_reuse_max_age_seconds, 300.0)
        changed = AppConfig(ocr_reuse_max_age_seconds=0)
        self.assertEqual(changed.force_ocr_seconds, 10.0)


class ServiceWiringTests(unittest.TestCase):
    """验收 5：服务层的失效清理逐项验证（每项单独验，不合成一条）。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        region = ready_region("合成区域", rect=Rect(0, 0, 300, 400))
        self.region_id = region.id
        self.config = AppConfig(
            regions=[region],
            keywords=["手机"],
            save_hit_images=False,
            poll_interval_ms=100,
            ocr_reuse_max_age_seconds=300,
        )
        self.storage = Storage(Path(self.temp.name) / "svc.db")
        self.ocr = CountingOcr()
        self.service = MonitorService(
            self.config, self.storage, self.ocr, capturer=object(), data_dir=Path(self.temp.name)
        )

    def tearDown(self) -> None:
        self.service.request_stop()
        self.storage.close()
        self.temp.cleanup()

    def _prime(self) -> None:
        """塞一条缓存，模拟"已经识别过一帧静止画面"。"""
        region = self.service._regions[self.region_id]
        ctx = self.service._ocr_context(region, self.region_id, 0)
        sample = FrameSample(self.region_id, 1, 1.0, roi_image(), region.rect)
        self.service._recognize(sample, ctx)
        self.assertEqual(self.service.ocr_reuse.entry_count(self.region_id), 1)

    def test_fault_clears_cache(self):
        self._prime()
        self.service._fail_region(self.region_id, 0, "模拟采集失败")
        self.assertEqual(self.service.ocr_reuse.cached_regions(), [])

    def test_pause_clears_cache(self):
        self._prime()
        self.service.pause_region(self.region_id)
        self.assertEqual(self.service.ocr_reuse.cached_regions(), [])

    def test_resume_clears_cache(self):
        self._prime()
        self.service.resume_region(self.region_id)
        self.assertEqual(self.service.ocr_reuse.cached_regions(), [])

    def test_stop_clears_cache(self):
        self._prime()
        self.service.request_stop()
        self.assertEqual(self.service.ocr_reuse.cached_regions(), [])

    def test_unready_gate_clears_cache(self):
        self._prime()
        self.service._mark_unready(self.region_id, ["窗口身份不一致"])
        self.assertEqual(self.service.ocr_reuse.cached_regions(), [])

    def test_backend_change_clears_cache(self):
        self._prime()
        region = self.service._regions[self.region_id]
        region.capture_backend = "printwindow"
        ctx = self.service._ocr_context(region, self.region_id, 0)
        sample = FrameSample(self.region_id, 2, 2.0, roi_image(), region.rect)
        result = self.service._recognize(sample, ctx)
        self.assertFalse(result.reused, "换后端后同一像素不得复用")
        self.assertEqual(self.ocr.calls, 2)

    def test_frame_size_change_clears_cache(self):
        self._prime()
        region = self.service._regions[self.region_id]
        region.reference_frame_width = 1024
        region.reference_frame_height = 1536
        ctx = self.service._ocr_context(region, self.region_id, 0)
        sample = FrameSample(self.region_id, 2, 2.0, roi_image(), region.rect)
        self.assertFalse(self.service._recognize(sample, ctx).reused)
        self.assertEqual(self.ocr.calls, 2)

    def test_generation_change_clears_cache(self):
        self._prime()
        region = self.service._regions[self.region_id]
        # generation 变化来自 _invalidate_locked（故障/恢复/重新校准都会走它）。
        with self.service._condition:
            self.service._invalidate_locked(self.region_id)
        ctx = self.service._ocr_context(region, self.region_id, self.service._generation[self.region_id])
        sample = FrameSample(self.region_id, 2, 2.0, roi_image(), region.rect)
        self.assertFalse(self.service._recognize(sample, ctx).reused)

    def test_disabled_reuse_is_none_and_passes_through(self):
        config = AppConfig(
            regions=self.config.regions,
            keywords=["手机"],
            save_hit_images=False,
            ocr_reuse_max_age_seconds=0,
        )
        service = MonitorService(
            config, self.storage, self.ocr, capturer=object(), data_dir=Path(self.temp.name)
        )
        try:
            self.assertIsNone(service.ocr_reuse, "0 秒必须完全等价于关闭复用")
            self.assertIs(service.ocr, self.ocr, "关闭复用时不得改动注入的引擎对象")
            region = service._regions[self.region_id]
            self.assertIsNone(service._ocr_context(region, self.region_id, 0))
            sample = FrameSample(self.region_id, 1, 1.0, roi_image(), region.rect)
            service._recognize(sample, None)
            self.assertEqual(self.ocr.calls, 1)
            self.assertEqual(service.ocr_reuse_summary()["enabled"], False)
        finally:
            service.request_stop()

    def test_health_snapshot_carries_real_reuse_numbers(self):
        self._prime()
        region = self.service._regions[self.region_id]
        ctx = self.service._ocr_context(region, self.region_id, 0)
        sample = FrameSample(self.region_id, 2, 2.0, roi_image(), region.rect)
        self.service._recognize(sample, ctx)

        snapshots = []
        self.service.on_health = snapshots.append
        self.service._emit_health(self.region_id)
        self.assertEqual(len(snapshots), 1)
        health = snapshots[0]
        self.assertEqual(health.ocr_reused, 1)
        self.assertEqual(health.ocr_cache_hits, 1)
        self.assertAlmostEqual(health.ocr_hit_rate, 0.5)


def _digest(image: object) -> str:
    return hashlib.blake2b(np.ascontiguousarray(image).tobytes(), digest_size=8).hexdigest()


def picture(value: int) -> np.ndarray:
    """按数值生成的整帧图：值不同 = 内容不同（模拟"画面变了"）。"""
    return np.full((480, 320, 3), value, dtype=np.uint8)


BLOCK_BOX = Rect(10, 10, 250, 25)


class PictureOcr:
    """按**像素内容**给文字的假 OCR。

    与"文字与画面无关"的桩相比，这一版保证"像素没变 ⇒ 文字没变"，因此复用路径
    拿到的文字必然与真跑一致 —— 这正是生产里的情形，也让"复用会不会吞掉新消息"
    这条风险在离线环境下可以被真实地检验。
    """

    def __init__(self, mapping: dict[str, list[str]], inference_ms: float = 9.0) -> None:
        self.mapping = mapping
        self.calls = 0
        self.inference_ms = inference_ms

    def recognize(self, sample: FrameSample) -> OcrObservation:
        self.calls += 1
        texts = self.mapping.get(_digest(sample.image), [])
        return OcrObservation(
            region_id=sample.region_id,
            sampled_at=sample.captured_at,
            frame_sequence=sample.sequence,
            blocks=[TextBlock(text, 0.99, BLOCK_BOX) for text in texts],
            inference_ms=self.inference_ms,
        )


class ServiceEndToEndTests(unittest.TestCase):
    """验收 6/7：复用路径下跟踪照跑；静止画面跨冷却期不重发，内容变了一通知。"""

    def _run(
        self,
        reuse_seconds: float,
        frames: list[np.ndarray],
        ocr: object,
        prepare: object = None,
        on_frame_processed: object = None,
    ):
        """按 ``frames`` 顺序驱动真实服务，返回 (服务, OCR, 事件, 存储, 区域 id)。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        region = ready_region("合成区域", rect=Rect(0, 0, 300, 400))
        config = AppConfig(
            regions=[region],
            keywords=["手机"],
            save_hit_images=False,
            poll_interval_ms=100,
            ocr_reuse_max_age_seconds=reuse_seconds,
        )
        storage = Storage(Path(temporary.name) / "e2e.db")
        self.addCleanup(storage.close)
        events: list = []
        cursor = {"index": 0}

        class Capturer:
            """只负责把"下一帧"交出去；轮次由 ``frames`` 决定。"""

            def reset(self, region_id):
                pass

            def capture(self, source_region, force=False):
                if cursor["index"] >= len(frames):
                    return None
                image = frames[cursor["index"]]
                cursor["index"] += 1
                # region_id 必须用 ready_region() 的 uuid：_submit 会拿它查 _generation。
                return FrameSample(
                    region.id,
                    cursor["index"],
                    time.time(),
                    image.copy(),
                    Rect(0, 0, 320, 480),
                )

        service = MonitorService(
            config,
            storage,
            ocr,
            capturer=Capturer(),
            data_dir=Path(temporary.name),
            on_events=events.extend,
            on_frame_processed=on_frame_processed,
        )
        if prepare is not None:
            prepare(service)
        service.start()
        try:
            deadline = time.monotonic() + 30
            # 先等帧全部交出去，再等全部处理完（复用时 lookups 才是"处理轮次"）。
            while time.monotonic() < deadline and cursor["index"] < len(frames):
                time.sleep(0.01)
            while time.monotonic() < deadline and not self._settled(service, ocr, region.id, len(frames)):
                time.sleep(0.01)
            time.sleep(0.3)  # 让最后一轮走完跟踪、规则与通知
        finally:
            service.stop()
        return service, ocr, events, storage, region.id

    @staticmethod
    def _settled(service, ocr, region_id: str, rounds: int) -> bool:
        if service.ocr_reuse is None:
            return ocr.calls >= rounds
        return service.ocr_reuse.stats_for(region_id).lookups >= rounds

    def test_reuse_still_runs_tracking_and_decisions(self):
        """验收 6：复用路径下 tracker.process 与 rules.decide 一次都不能少。"""
        image = picture(10)
        ocr = PictureOcr({_digest(image): ["手机 优惠 4999 元"]})
        calls = {"track": 0, "decide": 0}

        def prepare(service):
            real_track = service.tracker.process
            real_decide = service.rules.decide

            def track_spy(observation, **kwargs):
                calls["track"] += 1
                return real_track(observation, **kwargs)

            def decide_spy(message):
                calls["decide"] += 1
                return real_decide(message)

            service.tracker.process = track_spy
            service.rules.decide = decide_spy

        rounds = 6
        service, ocr, _events, _storage, _region_id = self._run(
            300, [image] * rounds, ocr, prepare
        )

        summary = service.ocr_reuse_summary()
        self.assertGreaterEqual(summary["reused"], 3, "静止画面必须真的走了复用")
        self.assertGreaterEqual(calls["track"], rounds, "复用不得跳过 tracker.process")
        self.assertGreaterEqual(calls["decide"], rounds, "复用不得跳过 rules.decide")

    def test_frame_processed_callback_covers_reuse_rounds(self):
        """验收工具的完成标记必须覆盖**复用命中轮**（2026-11-05 真机首跑缺陷）。

        十轮真机探针曾把完成标记挂在 OCR 观测回调上：73% 复用率的运行里，
        复用轮不进引擎 → 回调不触发 → ``round_settled_by_sequence`` 永远等不到
        序号，报告误判 8/10 轮"未完成"。权威标记点因此移到服务层
        ``on_frame_processed``：帧处理完就打，与是否真推理无关。
        """
        image = picture(10)
        ocr = PictureOcr({_digest(image): ["手机 优惠 4999 元"]})
        marks: list[tuple[str, int]] = []
        rounds = 4
        service, ocr, _events, _storage, region_id = self._run(
            300,
            [image] * rounds,
            ocr,
            on_frame_processed=lambda rid, seq: marks.append((rid, seq)),
        )

        self.assertEqual(
            [sequence for _rid, sequence in marks],
            list(range(1, rounds + 1)),
            "每一帧（含复用命中轮）都必须上报处理完成序号",
        )
        self.assertTrue(all(rid == region_id for rid, _ in marks), "区域 id 必须随序号一起上报")
        summary = service.ocr_reuse_summary()
        self.assertGreater(summary["reused"], 0, "前提自检：本用例必须真的发生了复用")
        self.assertLess(ocr.calls, rounds, "前提自检：复用轮不得真跑 OCR")

    def test_reuse_round_passes_current_frame_to_tracker(self):
        """复用轮必须把**本次新帧**交给 tracker；传 ``None`` 会让离开判定永久冻结。

        变异验证（team-lead 的 E）：把 ``service.py`` 里
        ``image=frame.image`` 改成 ``image=None``，本条必须失败。

        后果链：``_pixels(None, box)`` → ``None`` ⇒ ``pixels_changed`` 恒 False
        ⇒ ``changed_misses`` 恒 0 ⇒ 「连续三次未识别才确认离开」永不成立
        ⇒ 轨迹永不删除、``missed`` 无限增长。
        """
        image = picture(10)
        ocr = PictureOcr({_digest(image): ["手机 优惠 4999 元"]})
        seen: list = []

        def prepare(service):
            original = service.tracker.process

            def spy(observation, *, image=None, current_messages=None):
                seen.append(image)
                return original(observation, image=image, current_messages=current_messages)

            service.tracker.process = spy

        rounds = 5
        service, _ocr, _events, _storage, _region_id = self._run(
            300, [image] * rounds, ocr, prepare
        )
        self.assertGreaterEqual(service.ocr_reuse_summary()["reused"], 3, "必须是复用路径")
        self.assertGreaterEqual(len(seen), rounds, "tracker.process 一次都不能少")
        for index, passed in enumerate(seen):
            self.assertIsNotNone(
                passed, f"第 {index} 轮把 image=None 传给 tracker：轨迹将永不删除"
            )
            self.assertEqual(
                getattr(passed, "shape", None),
                image.shape,
                f"第 {index} 轮传给 tracker 的不是当前帧",
            )

    def test_static_message_notifies_once_across_cooldown(self):
        """验收 7：同一画面连续 10 轮只通知 1 次；内容变了重新可通知。"""
        blank = picture(0)  # 基线：没有任何文字
        steady = picture(10)  # 新消息出现后画面静止
        other = picture(20)  # 内容变化（价格档位变了）
        ocr = PictureOcr(
            {
                _digest(steady): ["手机 优惠 4999 元"],
                _digest(other): ["手机 优惠 3999 元"],
            }
        )
        frames = [blank] + [steady] * 10 + [other] * 2

        service, ocr, events, _storage, _region_id = self._run(300, frames, ocr)

        summary = service.ocr_reuse_summary()
        self.assertTrue(summary["enabled"])
        self.assertGreaterEqual(summary["reused"], 8, "连续静止的 10 轮必须复用")
        self.assertEqual(
            [event.text for event in events],
            ["手机 优惠 4999 元", "手机 优惠 3999 元"],
            "静止画面跨冷却期只通知 1 次；内容变化后必须重新可通知",
        )
        self.assertEqual(ocr.calls, 3, "三种画面各只需真实识别一次")

    def test_changed_text_notifies_again(self):
        """关闭复用时的同一语义（证明本轮改动没有动到"内容变→再通知"）。"""
        blank = picture(0)
        steady = picture(10)
        other = picture(20)
        ocr = PictureOcr(
            {
                _digest(steady): ["手机 优惠 4999 元"],
                _digest(other): ["手机 优惠 3999 元"],
            }
        )
        frames = [blank, blank, steady, other]

        service, ocr, events, _storage, _region_id = self._run(0, frames, ocr)

        self.assertIsNone(service.ocr_reuse, "0 秒必须完全不创建复用层")
        self.assertEqual(ocr.calls, 4, "关闭复用时每一轮都要真识别")
        self.assertEqual(
            [event.text for event in events], ["手机 优惠 4999 元", "手机 优惠 3999 元"]
        )


class OcrCallCountComparisonTests(unittest.TestCase):
    """验收交付项：10 轮静止画面，关闭复用 vs 开启复用的 OCR 调用次数。"""

    def _count(self, reuse_seconds: float, rounds: int = 10) -> int:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        region = ready_region("合成区域", rect=Rect(0, 0, 300, 400))
        config = AppConfig(
            regions=[region],
            keywords=["手机"],
            save_hit_images=False,
            poll_interval_ms=100,
            ocr_reuse_max_age_seconds=reuse_seconds,
        )
        storage = Storage(Path(temporary.name) / "count.db")
        self.addCleanup(storage.close)
        ocr = CountingOcr()
        image = roi_image()
        left = {"value": rounds}

        class Capturer:
            def reset(self, region_id):
                pass

            def capture(self, source_region, force=False):
                if left["value"] <= 0:
                    return None
                left["value"] -= 1
                return FrameSample(
                    region.id,
                    rounds - left["value"],
                    time.time(),
                    image.copy(),
                    Rect(0, 0, 320, 480),
                )

        service = MonitorService(
            config, storage, ocr, capturer=Capturer(), data_dir=Path(temporary.name)
        )
        service.start()
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and service.ocr_reuse is not None and (
                service.ocr_reuse.stats_for(region.id).lookups < rounds
            ):
                time.sleep(0.01)
            while time.monotonic() < deadline and service.ocr_reuse is None and ocr.calls < rounds:
                time.sleep(0.01)
            time.sleep(0.2)
        finally:
            service.stop()
        return ocr.calls

    def test_ten_static_rounds_before_and_after(self):
        disabled = self._count(0)
        enabled = self._count(300)
        # 数值留档：关闭复用 10 次全跑；开启复用只跑 1 次。
        self.assertEqual(disabled, 10, "关闭复用时每一轮都要真识别")
        self.assertEqual(enabled, 1, "10 轮静止画面只需要识别 1 次")


class ForcedFrameTests(unittest.TestCase):
    """校正项 17：``forced`` 与 OCR 复用**完全无关**。

    生产 ``poll_interval_ms=60000`` 下 ``capture.py`` 的 10 秒与
    ``force_ocr_seconds`` 的 10 秒都远小于采样间隔，于是 ``forced`` 每轮都是 True。
    如果让它绕过复用，命中率恒 0，T2.3 就是一段永不执行的死代码。
    因此这条用例的方向是**反向断言**：两帧都是 forced，仍然要复用。
    """

    def test_forced_frames_are_still_reused(self):
        inner = CountingOcr()
        cache = ReusableOcr(inner, max_age_seconds=300)
        image = roi_image()
        forced = Rect(0, 0, 320, 480)

        first = FrameSample("r1", 1, 1.0, image, forced, True)
        second = FrameSample("r1", 2, 2.0, image, forced, True)

        self.assertFalse(cache.recognize(first, context()).reused)
        result = cache.recognize(second, context())

        self.assertTrue(result.reused, "forced 帧同样可以复用（它不是 OCR 语义）")
        self.assertEqual(inner.calls, 1, "两帧像素一致 → 只推理一次")
        stats = cache.stats_for("r1")
        self.assertEqual(stats.reused, 1)
        self.assertEqual(stats.hits, 1)


class StoredAtTests(unittest.TestCase):
    """校正项 18：命中**不刷新** ``stored_at`` —— TTL 是唯一的陈旧上限。"""

    def test_hit_does_not_refresh_stored_at(self):
        clock = {"now": 500.0}
        inner = CountingOcr()
        cache = ReusableOcr(inner, max_age_seconds=300, clock=lambda: clock["now"])
        image = roi_image()

        cache.recognize(frame(image, sequence=0), context())
        stored_before = cache.inspect_entries("r1")[0][1]

        for index in range(1, 11):
            clock["now"] += 10.0
            self.assertTrue(
                cache.recognize(frame(image, sequence=index), context()).reused,
                f"第 {index} 轮应当复用",
            )
        self.assertEqual(
            cache.inspect_entries("r1")[0][1],
            stored_before,
            "命中时刷新 stored_at 会让静止画面被无限期复用下去",
        )
        self.assertEqual(inner.calls, 1)

        # 跨过 TTL 边界的那一轮必须真跑一次：每 300 秒至少一次真实推理。
        clock["now"] = stored_before + 300.5
        self.assertFalse(cache.recognize(frame(image, sequence=11), context()).reused)
        self.assertEqual(inner.calls, 2, "跨 TTL 边界必须重新推理")


class ServiceMetricHonestyTests(unittest.TestCase):
    """校正项 16/19：复用轮不污染 ``ocr_infer`` 耗时分布，也不覆盖 ``last_ocr_ms``。"""

    def _run(self, rounds: int, region_id_holder: dict):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        region = ready_region("合成区域", rect=Rect(0, 0, 300, 400))
        region_id_holder["id"] = region.id
        config = AppConfig(
            regions=[region],
            keywords=["手机"],
            save_hit_images=False,
            poll_interval_ms=100,
            ocr_reuse_max_age_seconds=300,
        )
        storage = Storage(Path(temporary.name) / "honest.db")
        self.addCleanup(storage.close)
        ocr = CountingOcr()
        image = roi_image()
        left = {"value": rounds}

        class Capturer:
            def reset(self, region_id):
                pass

            def capture(self, source_region, force=False):
                if left["value"] <= 0:
                    return None
                left["value"] -= 1
                return FrameSample(
                    region.id,
                    rounds - left["value"],
                    time.time(),
                    image.copy(),
                    Rect(0, 0, 320, 480),
                )

        service = MonitorService(
            config, storage, ocr, capturer=Capturer(), data_dir=Path(temporary.name)
        )
        service.start()
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and service.ocr_reuse.stats_for(region.id).lookups < rounds:
                time.sleep(0.01)
            time.sleep(0.2)
        finally:
            service.stop()
        return service, ocr, region.id

    def test_reuse_rounds_do_not_write_infer_samples(self):
        holder: dict = {}
        service, ocr, region_id = self._run(8, holder)
        stats = service.ocr_reuse.stats_for(region_id)
        self.assertGreaterEqual(stats.reused, 5, "静止画面必须真的复用")

        # 只有第一次是真实推理，因此 ocr_infer 只该有 1 个样本。
        self.assertEqual(
            service.metrics.total_samples(STAGE_OCR_INFER),
            1,
            "复用轮写 0.0 会把 P50/P95 稀释成一个假象",
        )
        self.assertEqual(service.metrics.recent_values(STAGE_OCR_INFER, region_id), [12.0])

    def test_reuse_round_keeps_last_ocr_ms(self):
        holder: dict = {}
        service, _ocr, region_id = self._run(8, holder)
        runtime = service._runtime[region_id]
        self.assertEqual(
            runtime.last_ocr_ms,
            12.0,
            "复用轮的 inference_ms=0.0 不得盖掉上一次真实推理的耗时",
        )


class ReuseReportTests(unittest.TestCase):
    """验收 10：报告里的 ``ocr_reuse`` 是真值，且与 ``HealthSnapshot`` 同源。"""

    def test_summary_and_health_share_one_source(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        region = ready_region("合成区域", rect=Rect(0, 0, 300, 400))
        config = AppConfig(
            regions=[region],
            keywords=["手机"],
            save_hit_images=False,
            poll_interval_ms=100,
            ocr_reuse_max_age_seconds=300,
        )
        storage = Storage(Path(temporary.name) / "report.db")
        self.addCleanup(storage.close)
        ocr = CountingOcr()
        service = MonitorService(
            config, storage, ocr, capturer=object(), data_dir=Path(temporary.name)
        )
        try:
            ctx = service._ocr_context(service._regions[region.id], region.id, 0)
            for index in (1, 2, 3, 4):
                sample = FrameSample(region.id, index, float(index), roi_image(), region.rect)
                service._recognize(sample, ctx)

            summary = service.ocr_reuse_summary()
            snapshots: list = []
            service.on_health = snapshots.append
            service._emit_health(region.id)
            health = snapshots[0]

            # 三个字段同源：同一份 ReuseStats，不在两处各算一遍。
            self.assertEqual(summary["reused"], health.ocr_reused)
            self.assertEqual(summary["cache_hits"], health.ocr_cache_hits)
            self.assertAlmostEqual(summary["hit_rate"], health.ocr_hit_rate)
            # 真值不是占位：4 轮静止画面 = 1 次推理 + 3 次复用。
            self.assertEqual(summary["reused"], 3)
            self.assertEqual(summary["hit_rate"], 0.75)
            self.assertEqual(summary["infer"], 1)
        finally:
            service.request_stop()

    def test_probe_formats_the_same_numbers(self):
        """探针的终端那一行必须念出服务给的数字，不是另算一份。"""
        module = _load_probe("real_schedule_60s")
        reuse = {
            "enabled": True,
            "lookups": 10,
            "reused": 9,
            "cache_hits": 9,
            "hit_rate": 0.9,
            "max_age_seconds": 300.0,
        }
        line = module.format_ocr_reuse(reuse)
        self.assertIn("查询 10 次", line)
        self.assertIn("复用 9 次", line)
        self.assertIn("命中率 90.0%", line)
        self.assertIn("上限 300 秒", line)
        self.assertIn(
            "已关闭",
            module.format_ocr_reuse({"enabled": False}),
            "关闭时必须明确说'行为与 0.2.1 一致'",
        )

    def test_no_samples_is_reported_as_unobserved_not_zero(self):
        """没有查询样本时写"未观测"，不能写成 0%（那会被读成画面一直在变）。"""
        from deal_watch.diagnostics import reuse_rate_line

        empty = ReuseStats()
        summary = {
            "enabled": True,
            "lookups": 0,
            "reused": 0,
            "cache_hits": 0,
            "content_hit_rate": empty.content_hit_rate,
            "reuse_rate": empty.reuse_rate,
            "max_age_seconds": 300.0,
        }
        line = reuse_rate_line(summary)
        self.assertIn("未观测", line)
        self.assertNotIn("0.0%", line)
        self.assertFalse(empty.observed)

    def test_disabled_reuse_has_no_samples(self):
        """关闭复用：一次查询都没有，统计必须承认"未观测"。"""
        cache = ReusableOcr(CountingOcr(), max_age_seconds=0)
        cache.recognize(frame(roi_image()), context())
        summary = cache.summary()
        self.assertEqual(summary["lookups"], 0)
        self.assertFalse(summary["observed"])
        self.assertEqual(summary["sample_note"], "未观测（没有查询样本）")
        self.assertEqual(summary["content_hit_rate"], 0.0, "字段保持数值型，但语义由 observed 决定")

    def test_ten_static_rounds_all_expired(self):
        """十轮静止且每轮都过期：内容命中率 90%，实际复用率 0%。

        这是 T2.3 最重要的一条口径：``hits`` 与 ``reused`` 不是一回事。
        第 1 轮无条目（miss），第 2–10 轮键都在但都超龄 → 9 次内容命中、0 次复用。
        若把 ``hit_rate`` 定义成 ``hits/(hits+infer)`` 或复用率，这条用例会直接把
        调参依据（"画面很静止，只是上限太短"）抹掉。
        """
        inner = CountingOcr()
        clock = {"now": 1000.0}
        cache = ReusableOcr(inner, max_age_seconds=1, clock=lambda: clock["now"])
        image = roi_image()
        for index in range(10):
            result = cache.recognize(frame(image, sequence=index), context())
            self.assertFalse(result.reused, f"第 {index + 1} 轮已过期，必须重新推理")
            clock["now"] += 60.0  # 每轮 60 秒 ≫ 1 秒上限

        stats = cache.stats_for("r1")
        self.assertEqual(stats.lookups, 10)
        self.assertEqual(stats.hits, 9, "第 2–10 轮键都在")
        self.assertEqual(stats.reused, 0, "每轮都超龄，一次都没有真正复用")
        self.assertEqual(stats.infer, 10)
        self.assertAlmostEqual(stats.content_hit_rate, 0.9)
        self.assertEqual(stats.reuse_rate, 0.0)
        self.assertAlmostEqual(stats.hit_rate, 0.9, msg="旧字段等同内容命中率")

        summary = cache.summary()
        self.assertEqual(summary["lookups"], 10)
        self.assertAlmostEqual(summary["content_hit_rate"], 0.9)
        self.assertEqual(summary["reuse_rate"], 0.0)
        self.assertEqual(summary["unit"], "lookups")
        self.assertIn("内容命中率", summary["hit_rate_semantics"])
        self.assertEqual(inner.calls, 10, "过期轮次都真跑了推理")

    def test_mixed_hits_and_invalidation(self):
        """混合命中 + 缓存失效：分母始终是查询次数，不因失效而漂移。"""
        inner = CountingOcr()
        clock = {"now": 0.0}
        cache = ReusableOcr(inner, max_age_seconds=300, clock=lambda: clock["now"])
        image = roi_image()
        # 1: miss, 2/3: hit+reused
        cache.recognize(frame(image, sequence=1), context())
        cache.recognize(frame(image, sequence=2), context())
        cache.recognize(frame(image, sequence=3), context())
        # 失效后 1 次 miss
        self.assertTrue(cache.invalidate("r1"))
        cache.recognize(frame(image, sequence=4), context())
        # 再来 1 次 hit+reused
        cache.recognize(frame(image, sequence=5), context())

        stats = cache.stats_for("r1")
        self.assertEqual((stats.lookups, stats.hits, stats.reused), (5, 3, 3))
        self.assertAlmostEqual(stats.content_hit_rate, 0.6)
        self.assertAlmostEqual(stats.reuse_rate, 0.6)
        self.assertEqual(stats.infer, 2, "失效后必须再真跑一次")


class RulesHistoryBoundTests(unittest.TestCase):
    """顺带交付：``last_decisions`` 有界（T2.2 遗留、本轮获授权）。"""

    def test_last_decisions_is_bounded_and_keeps_recent(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "bound.db")
            engine = RuleEngine(storage, ["手机"])
            try:
                for index in range(5000):
                    engine.decide(
                        MessageCandidate(
                            "r1", "普通消息", Rect(0, 0, 10, 10), 1.0, 1.0,
                            tracking_id=f"t{index}",
                        )
                    )
                self.assertEqual(len(engine.last_decisions), 4096, "上限必须生效")
                self.assertNotIn("t0", engine.last_decisions, "最旧的应被淘汰")
                self.assertIn("t4999", engine.last_decisions, "最新的必须保留")
            finally:
                storage.close()

    def test_static_message_keeps_single_entry_under_reuse(self):
        """同一 tracking_id 反复判定只占一个键，不会把上限挤爆。"""
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "one.db")
            engine = RuleEngine(storage, ["手机"])
            try:
                for _ in range(500):
                    engine.decide(
                        MessageCandidate(
                            "r1", "手机 优惠 4999 元", Rect(0, 0, 10, 10), 1.0, 1.0,
                            tracking_id="same",
                        )
                    )
                self.assertEqual(len(engine.last_decisions), 1)
                self.assertEqual(engine.last_decisions["same"].should_notify, True)
            finally:
                storage.close()


if __name__ == "__main__":
    unittest.main()
