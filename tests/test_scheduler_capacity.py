from __future__ import annotations

import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path

from deal_watch.config import AppConfig
from deal_watch.models import FrameSample, OcrObservation, Rect
from deal_watch.service import MonitorService
from deal_watch.storage import Storage
from region_fixtures import ready_region


class _EmptyOcr:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay

    def recognize(self, frame):
        if self.delay:
            time.sleep(self.delay)
        return OcrObservation(frame.region_id, frame.captured_at, frame.sequence, [], inference_ms=1.0)


class _CountingCapturer:
    def __init__(self, *, failing_region: str = "", capture_delay: float = 0.0) -> None:
        self.failing_region = failing_region
        self.capture_delay = capture_delay
        self.calls: Counter[str] = Counter()
        self.active: Counter[str] = Counter()
        self.overlaps: list[str] = []
        self.lock = threading.Lock()

    def reset(self, _region_id: str) -> None:
        pass

    def capture(self, region, force=False):
        with self.lock:
            self.calls[region.id] += 1
            self.active[region.id] += 1
            if self.active[region.id] > 1:
                self.overlaps.append(region.id)
            sequence = self.calls[region.id]
        try:
            if self.capture_delay:
                time.sleep(self.capture_delay)
            if region.id == self.failing_region:
                raise TimeoutError("simulated persistent timeout")
            return FrameSample(region.id, sequence, time.time(), [], region.rect)
        finally:
            with self.lock:
                self.active[region.id] -= 1


class _OverlapOcr(_EmptyOcr):
    def __init__(self, capturer: _CountingCapturer, delay: float) -> None:
        super().__init__(delay)
        self.capturer = capturer
        self.ocr_active: set[str] = set()
        self.entered = threading.Event()
        self.lock = threading.Lock()

    def recognize(self, frame):
        with self.lock:
            self.ocr_active.add(frame.region_id)
            self.entered.set()
        try:
            time.sleep(self.delay)
            return OcrObservation(frame.region_id, frame.captured_at, frame.sequence, [])
        finally:
            with self.lock:
                self.ocr_active.discard(frame.region_id)


class SchedulerCapacityTests(unittest.TestCase):
    def _service(self, regions, capturer, ocr, directory, *, interval=100, queue=4):
        config = AppConfig(
            regions=regions,
            save_hit_images=False,
            poll_interval_ms=interval,
            max_queue_size=queue,
        )
        storage = Storage(Path(directory) / "scheduler.db")
        return MonitorService(config, storage, ocr, capturer=capturer), storage

    def test_five_eight_and_twenty_regions_are_served_without_queue_growth(self):
        for count in (5, 8, 20):
            with self.subTest(regions=count), tempfile.TemporaryDirectory() as directory:
                regions = [
                    ready_region(f"r{i}", window_title=f"Synthetic {i}", rect=Rect(0, 0, 100, 100))
                    for i in range(count)
                ]
                capturer = _CountingCapturer()
                service, storage = self._service(regions, capturer, _EmptyOcr(0.01), directory, queue=8)
                try:
                    service.start()
                    deadline = time.monotonic() + 4
                    while time.monotonic() < deadline:
                        with service._condition:
                            depth = len(service._pending)
                        if all(capturer.calls[region.id] >= 1 for region in regions):
                            break
                        self.assertLessEqual(depth, 4)
                        time.sleep(0.005)
                    self.assertTrue(
                        all(capturer.calls[region.id] >= 1 for region in regions),
                        f"有区域未获采样：{count} 区域 / {capturer.calls}",
                    )
                    with service._condition:
                        self.assertLessEqual(len(service._pending), 4)
                    self.assertEqual(capturer.overlaps, [])
                finally:
                    service.stop()
                    storage.close()

    def test_persistent_capture_timeout_does_not_starve_other_regions(self):
        with tempfile.TemporaryDirectory() as directory:
            regions = [
                ready_region(f"r{i}", window_title=f"Synthetic {i}", rect=Rect(0, 0, 100, 100))
                for i in range(8)
            ]
            capturer = _CountingCapturer(failing_region=regions[0].id)
            service, storage = self._service(regions, capturer, _EmptyOcr(0.01), directory)
            try:
                service.start()
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if all(capturer.calls[region.id] >= 1 for region in regions[1:]):
                        break
                    time.sleep(0.005)
                self.assertGreaterEqual(capturer.calls[regions[0].id], 1)
                self.assertTrue(all(capturer.calls[region.id] >= 1 for region in regions[1:]))
                self.assertEqual(service._runtime[regions[0].id].status.value, "error")
                self.assertEqual(capturer.overlaps, [])
            finally:
                service.stop()
                storage.close()

    def test_slow_ocr_keeps_region_busy_without_overlapping_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("r0", window_title="Synthetic", rect=Rect(0, 0, 100, 100))
            capturer = _CountingCapturer()
            ocr = _OverlapOcr(capturer, 0.16)
            service, storage = self._service([region], capturer, ocr, directory)
            original_capture = capturer.capture
            original_submit = service._submit

            def guarded_capture(source, force=False):
                with ocr.lock:
                    if source.id in ocr.ocr_active:
                        capturer.overlaps.append(source.id)
                return original_capture(source, force)

            capturer.capture = guarded_capture

            def submit_and_wait_until_ocr_owns_region(frame, generation=None):
                original_submit(frame, generation)
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    with service._condition:
                        if frame.region_id in service._ocr_busy:
                            break
                    time.sleep(0.001)
                self.assertTrue(ocr.entered.wait(1), "OCR worker did not claim submitted frame")

            service._submit = submit_and_wait_until_ocr_owns_region
            try:
                service.start()
                deadline = time.monotonic() + 2
                while capturer.calls[region.id] < 3 and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertGreaterEqual(capturer.calls[region.id], 3)
                self.assertEqual(capturer.overlaps, [])
            finally:
                service.stop()
                storage.close()

    def test_invalidating_pending_frame_releases_its_busy_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            r1 = ready_region("r1", window_title="Synthetic 1", rect=Rect(0, 0, 100, 100))
            r2 = ready_region("r2", window_title="Synthetic 2", rect=Rect(0, 0, 100, 100))
            capturer = _CountingCapturer()
            entered_r1, release_r1 = threading.Event(), threading.Event()

            class BlockingOcr(_EmptyOcr):
                def recognize(self, frame):
                    if frame.region_id == r1.id and frame.sequence == 1:
                        entered_r1.set()
                        if not release_r1.wait(2):
                            raise TimeoutError("test did not release OCR")
                    return super().recognize(frame)

            service, storage = self._service([r1, r2], capturer, BlockingOcr(), directory)
            try:
                service.start()
                self.assertTrue(entered_r1.wait(1), "r1 OCR did not block")
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    with service._condition:
                        if r2.id in service._pending:
                            break
                    time.sleep(0.002)
                with service._condition:
                    self.assertIn(r2.id, service._pending)
                    first_generation = service._pending_generation[r2.id]
                service.pause_region(r2.id)
                self.assertNotIn(r2.id, service._busy_generation)
                self.assertTrue(service.resume_region(r2.id))
                release_r1.set()
                deadline = time.monotonic() + 1
                while capturer.calls[r2.id] < 2 and time.monotonic() < deadline:
                    time.sleep(0.002)
                self.assertGreaterEqual(capturer.calls[r2.id], 2)
                self.assertNotEqual(service._generation[r2.id], first_generation)
            finally:
                release_r1.set()
                service.stop()
                storage.close()

    def test_stop_clears_pending_busy_before_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            r1 = ready_region("r1", window_title="Synthetic 1", rect=Rect(0, 0, 100, 100))
            r2 = ready_region("r2", window_title="Synthetic 2", rect=Rect(0, 0, 100, 100))
            capturer = _CountingCapturer()
            entered_r1, release_r1 = threading.Event(), threading.Event()

            class FirstFrameBlockingOcr(_EmptyOcr):
                def recognize(self, frame):
                    if frame.region_id == r1.id and frame.sequence == 1:
                        entered_r1.set()
                        if not release_r1.wait(2):
                            raise TimeoutError("test did not release OCR")
                    return super().recognize(frame)

            service, storage = self._service(
                [r1, r2], capturer, FirstFrameBlockingOcr(), directory
            )
            try:
                service.start()
                self.assertTrue(entered_r1.wait(1))
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    with service._condition:
                        if r2.id in service._pending:
                            break
                    time.sleep(0.002)
                with service._condition:
                    self.assertIn(r2.id, service._pending)
                service.request_stop()
                with service._condition:
                    self.assertNotIn(r2.id, service._pending)
                    self.assertNotIn(r2.id, service._busy_generation)
                release_r1.set()
                service.stop()
                before_restart = capturer.calls[r2.id]
                service.start()
                deadline = time.monotonic() + 1
                while capturer.calls[r2.id] <= before_restart and time.monotonic() < deadline:
                    time.sleep(0.002)
                self.assertGreater(capturer.calls[r2.id], before_restart)
            finally:
                release_r1.set()
                service.stop()
                storage.close()

    def test_poll_interval_update_wakes_waiter_and_stop_wakes_long_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("r0", window_title="Synthetic", rect=Rect(0, 0, 100, 100))
            capturer = _CountingCapturer()
            service, storage = self._service([region], capturer, _EmptyOcr(), directory, interval=60_000)
            try:
                service.start()
                deadline = time.monotonic() + 1
                while capturer.calls[region.id] < 1 and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(capturer.calls[region.id], 1)
                service.update_poll_interval(100)
                deadline = time.monotonic() + 1
                while capturer.calls[region.id] < 2 and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertGreaterEqual(capturer.calls[region.id], 2)
                self.assertEqual(service.config.poll_interval_ms, 100)
                stopped = time.monotonic()
                service.stop()
                self.assertLess(time.monotonic() - stopped, 0.5)
            finally:
                service.stop()
                storage.close()

    def test_cycle_elapsed_excludes_interval_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            region = ready_region("r0", window_title="Synthetic", rect=Rect(0, 0, 100, 100))
            capturer = _CountingCapturer()
            service, storage = self._service([region], capturer, _EmptyOcr(), directory, interval=400)
            cycle_elapsed: list[float] = []

            def record_cycle(started, _interval_ms, _sampled, _wait_ms=0.0, _region_id=""):
                cycle_elapsed.append(time.monotonic() - started)

            service._log_cycle = record_cycle
            try:
                service.start()
                deadline = time.monotonic() + 2
                while len(cycle_elapsed) < 2 and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertGreaterEqual(len(cycle_elapsed), 2)
                self.assertLess(cycle_elapsed[1], 0.15)
            finally:
                service.stop()
                storage.close()


if __name__ == "__main__":
    unittest.main()
