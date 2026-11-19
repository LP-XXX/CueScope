from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from deal_watch.capture import CaptureError
from deal_watch.config import AppConfig
from deal_watch.models import (
    CaptureErrorCode,
    FrameSample,
    OcrObservation,
    Rect,
    TextBlock,
)
from deal_watch.service import MonitorService
from deal_watch.storage import Storage
from region_fixtures import ready_region


class FakeCapturer:
    def __init__(self):
        self.sequences = {}

    def capture(self, region, force=False):
        sequence = self.sequences.get(region.id, 0) + 1
        self.sequences[region.id] = sequence
        texts = ["普通消息"] if sequence == 1 else ["普通消息", "手机 优惠 4999 元"]
        return FrameSample(region.id, sequence, time.time(), texts, region.rect)

    def reset(self, region_id):
        pass


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


class ServiceTests(unittest.TestCase):
    def test_automatic_capture_recovery_baselines_before_accepting_new_content(self):
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("合成区域", window_title="Synthetic", rect=Rect(0, 0, 300, 500))
            config = AppConfig(regions=[region], keywords=["手机"], save_hit_images=False, poll_interval_ms=100)
            storage = Storage(Path(directory) / "recovery.db")
            blocked, release, recovered_capture, recovered_baseline = (
                threading.Event() for _ in range(4)
            )

            class RecoveryCapturer:
                sequence = 0

                def reset(self, region_id):
                    pass

                def capture(self, source, force=False):
                    self.sequence += 1
                    if self.sequence > 4:
                        return None
                    if self.sequence == 3:
                        if not blocked.wait(3):
                            raise TimeoutError("OCR not reached")
                        raise CaptureError(CaptureErrorCode.WINDOW_OCCLUDED, "模拟遮挡")
                    if self.sequence == 4:
                        recovered_capture.set()
                    texts = (
                        ["普通消息", "手机 优惠 4999 元"]
                        if self.sequence == 4
                        else ["普通消息"]
                    )
                    return FrameSample(source.id, self.sequence, time.time(), texts, source.rect)

            class RecoveryOcr(FakeOcr):
                last_sequence = 0

                def recognize(self, frame):
                    if frame.sequence == 2:
                        blocked.set()
                        if not release.wait(3):
                            raise TimeoutError("OCR not released")
                    self.last_sequence = frame.sequence
                    return super().recognize(frame)

            engine = RecoveryOcr()

            def health(snapshot):
                if snapshot.status.value == "running" and engine.last_sequence == 4:
                    recovered_baseline.set()

            service = MonitorService(config, storage, engine, capturer=RecoveryCapturer(), on_health=health)
            try:
                service.start()
                self.assertTrue(blocked.wait(3), "第二帧 OCR 没有开始")
                release.set()
                self.assertTrue(recovered_capture.wait(3))
                self.assertTrue(recovered_baseline.wait(3))
                self.assertEqual(storage.list_events(), [])
                self.assertEqual(service._runtime[region.id].status.value, "running")
            finally:
                release.set()
                service.stop()
                storage.close()

    def test_pause_discards_inflight_result_without_waiting_for_ocr(self):
        self._assert_inflight_invalidation("pause")

    def test_capture_error_discards_inflight_result_and_pending_frame(self):
        self._assert_inflight_invalidation("error")

    def _assert_inflight_invalidation(self, reason):
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("合成区域", window_title="Synthetic", rect=Rect(0, 0, 300, 500))
            config = AppConfig(regions=[region], keywords=["手机"], save_hit_images=False)
            storage = Storage(Path(directory) / "inflight.db")
            entered, release, processed = threading.Event(), threading.Event(), threading.Event()

            class BlockingOcr(FakeOcr):
                def recognize(self, frame):
                    if frame.sequence == 2:
                        entered.set()
                        if not release.wait(3):
                            raise TimeoutError("test did not release OCR")
                    return super().recognize(frame)

            service = MonitorService(config, storage, BlockingOcr(), capturer=FakeCapturer())
            # Run only the OCR thread so capture timing cannot hide a stale result.
            service._ocr_thread = threading.Thread(target=service._ocr_loop, daemon=True)
            service._ocr_thread.start()
            service.on_health = lambda health: processed.set()

            def submit(sequence, texts):
                processed.clear()
                service._submit(FrameSample(region.id, sequence, time.time(), texts, region.rect))

            try:
                submit(1, ["旧内容"])
                self.assertTrue(processed.wait(2))
                submit(2, ["旧内容", "手机 4999 元"])
                self.assertTrue(entered.wait(2))
                submit(3, ["旧内容", "手机 3999 元"])
                before = time.monotonic()
                if reason == "pause":
                    service.pause_region(region.id)
                    self.assertLess(time.monotonic() - before, 0.5)
                    service.resume_region(region.id)
                else:
                    service._fail_region(region.id, service._generation[region.id], "模拟遮挡")
                    self.assertEqual(service._runtime[region.id].status.value, "error")
                    service.resume_region(region.id)
                self.assertEqual(service._pending, {})
                # Recovery snapshot must be the new baseline, not an event.
                submit(4, ["旧内容", "手机 2999 元"])
                release.set()
                self.assertTrue(processed.wait(2))
                self.assertEqual(storage.list_events(), [])
                submit(5, ["旧内容", "手机 2999 元", "手机 1999 元"])
                self.assertTrue(processed.wait(2))
                self.assertEqual(len(storage.list_events()), 1)
                self.assertEqual(storage.list_events()[0]["text"], "手机 1999 元")
            finally:
                release.set()
                service.stop()
                storage.close()

    def test_stale_capture_generation_cannot_be_submitted_after_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("测试", window_title="Synthetic", rect=Rect(0, 0, 100, 100))
            storage = Storage(Path(directory) / "stale.db")
            service = MonitorService(AppConfig(regions=[region]), storage, FakeOcr(), capturer=FakeCapturer())
            stale_generation = service._generation[region.id]
            service.pause_region(region.id)
            service.resume_region(region.id)
            service._submit(FrameSample(region.id, 1, time.time(), [], region.rect), stale_generation)
            self.assertEqual(service._pending, {})
            storage.close()

    def test_minimized_window_pauses_instead_of_failing(self):
        """A3：窗口最小化/桌面锁定是可恢复暂停，不发故障、恢复只建基线。"""
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("测试群", window_title="微信", rect=Rect(0, 0, 300, 500))
            config = AppConfig(
                regions=[region], keywords=["手机"], save_hit_images=False, poll_interval_ms=100
            )
            storage = Storage(Path(directory) / "pause.db")
            statuses: list[str] = []
            paused = threading.Event()
            recovered = threading.Event()

            class PausingCapturer:
                sequence = 0

                def reset(self, region_id):
                    pass

                def capture(self, source, force=False):
                    self.sequence += 1
                    if self.sequence == 1:
                        raise CaptureError(
                            CaptureErrorCode.WINDOW_MINIMIZED, "窗口已最小化，暂停采集"
                        )
                    if self.sequence >= 2:
                        recovered.set()
                    texts = ["旧内容", "手机 4999 元"]
                    return FrameSample(source.id, self.sequence, time.time(), texts, source.rect)

            def health(snapshot):
                statuses.append(snapshot.status.value)
                if snapshot.status.value == "paused":
                    paused.set()

            service = MonitorService(
                config, storage, FakeOcr(), capturer=PausingCapturer(), on_health=health
            )
            service.start()
            try:
                self.assertTrue(paused.wait(3), "最小化没有被判成暂停")
                self.assertEqual(service._runtime[region.id].status.value, "paused")
                self.assertFalse(service._runtime[region.id].failed)
                self.assertEqual(service._runtime[region.id].detail, "已暂停：窗口最小化")
                self.assertNotIn("error", statuses)
                self.assertTrue(recovered.wait(3), "窗口恢复后没有重新采样")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if service._runtime[region.id].status.value == "running":
                        break
                    time.sleep(0.02)
                # 恢复后的第一帧只建立基线：命中词不会补发。
                self.assertEqual(storage.list_events(), [])
            finally:
                service.stop()
                storage.close()

    def test_desktop_locked_pauses_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("测试群", window_title="微信", rect=Rect(0, 0, 300, 500))
            config = AppConfig(regions=[region], keywords=["手机"], save_hit_images=False)
            storage = Storage(Path(directory) / "lock.db")
            paused = threading.Event()

            class LockedCapturer:
                def reset(self, region_id):
                    pass

                def capture(self, source, force=False):
                    raise CaptureError(CaptureErrorCode.DESKTOP_LOCKED, "桌面已锁定")

            def health(snapshot):
                if snapshot.status.value == "paused":
                    paused.set()

            service = MonitorService(
                config, storage, FakeOcr(), capturer=LockedCapturer(), on_health=health
            )
            service.start()
            try:
                self.assertTrue(paused.wait(3))
                runtime = service._runtime[region.id]
                self.assertEqual(runtime.detail, "已暂停：桌面已锁定")
                self.assertFalse(runtime.failed)
            finally:
                service.stop()
                storage.close()

    def test_offline_service_baseline_then_match(self):
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("测试群", window_title="微信", rect=Rect(0, 0, 300, 500))
            config = AppConfig(
                regions=[region], keywords=["手机"], save_hit_images=False, poll_interval_ms=100
            )
            storage = Storage(Path(directory) / "service.db")
            received = []
            ready = threading.Event()

            def on_events(events):
                received.extend(events)
                ready.set()

            service = MonitorService(
                config,
                storage,
                FakeOcr(),
                capturer=FakeCapturer(),
                data_dir=Path(directory),
                on_events=on_events,
            )
            service.start()
            self.assertTrue(ready.wait(5), "离线服务未在期限内识别新增消息")
            service.stop()
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].region_alias, "测试群")
            self.assertEqual(storage.queue_counts().get("pending"), 1)
            storage.close()

    def test_candidates_and_decisions_written_in_one_store_call(self):
        """一帧候选与最终判断必须同一次写入，且不再补第二条 mark_decisions。

        断言的正是集成前后的差别：旧接线是 ``record_candidates`` + ``mark_decisions``
        两次事务；新接线把已算好的判断随候选一起传下去，只开一次事务。
        """

        class RecordingFeedback:
            def __init__(self) -> None:
                self.batches: list[dict] = []
                self.mark_calls = 0

            def record_candidates(
                self, _region_id, _alias, _observed_at, messages, _matched,
                decisions_by_tracking=None,
            ):
                self.batches.append(
                    {
                        "texts": [message.text for message in messages],
                        "decisions": dict(decisions_by_tracking or {}),
                        "tracking": [message.tracking_id for message in messages],
                    }
                )
                return [f"c{index}" for index in range(len(messages))]

            def mark_decisions(self, decisions) -> None:  # noqa: ANN001 - 鸭子类型
                self.mark_calls += 1

        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("合成区域", window_title="Synthetic", rect=Rect(0, 0, 300, 500))
            config = AppConfig(regions=[region], keywords=["手机", "普通消息"], save_hit_images=False)
            storage = Storage(Path(directory) / "single-tx.db")
            feedback = RecordingFeedback()
            service = MonitorService(
                config, storage, FakeOcr(), capturer=FakeCapturer(), recognition_store=feedback
            )
            service._ocr_thread = threading.Thread(target=service._ocr_loop, daemon=True)
            service._ocr_thread.start()
            processed = threading.Event()
            service.on_health = lambda health: processed.set()

            def submit(sequence, texts):
                processed.clear()
                service._submit(FrameSample(region.id, sequence, time.time(), texts, region.rect))
                self.assertTrue(processed.wait(2), "OCR 结果没有回到健康回调")

            try:
                submit(1, ["普通消息"])
                submit(2, ["普通消息", "手机 优惠 4999 元"])
                self.assertEqual(len(feedback.batches), 2, "每帧只允许一次候选写入")
                self.assertEqual(feedback.mark_calls, 0, "判断已随候选写入，不得再补一次事务")
                self.assertEqual(list(feedback.batches[0]["decisions"].values()), ["baseline"])
                latest = feedback.batches[-1]
                self.assertEqual(latest["texts"], ["普通消息", "手机 优惠 4999 元"])
                self.assertEqual(set(latest["tracking"]), set(latest["decisions"]))
                self.assertEqual(
                    sorted(latest["decisions"].values()),
                    ["queued", "unchanged"],
                    "静止命中与新入队命中必须可区分",
                )
            finally:
                service.stop()
                storage.close()

    def test_pending_queue_is_bounded_and_latest_frame_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            regions = [
                ready_region(str(index), window_title="微信", rect=Rect(0, 0, 100, 100))
                for index in range(2)
            ]
            config = AppConfig(regions=regions, max_queue_size=1, save_hit_images=False)
            storage = Storage(Path(directory) / "queue.db")
            service = MonitorService(config, storage, FakeOcr(), capturer=FakeCapturer())
            first = FrameSample(regions[0].id, 1, time.time(), [], regions[0].rect)
            replacement = FrameSample(regions[1].id, 1, time.time(), [], regions[1].rect)
            service._submit(first)
            service._submit(replacement)
            self.assertEqual(len(service._pending), 1)
            self.assertIn(regions[1].id, service._pending)
            self.assertEqual(service._runtime[regions[0].id].dropped_frames, 1)
            storage.close()


if __name__ == "__main__":
    unittest.main()
