"""0.2.5：只有新帧处理与落盘完成才能宣布恢复，诊断不影响业务。"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from deal_watch.config import AppConfig
from deal_watch.diagnostics import sanitize_stage_fields
from deal_watch.models import (
    CaptureErrorCode,
    FrameSample,
    HealthSnapshot,
    OcrObservation,
    RegionStatus,
)
from deal_watch.service import MonitorService
from deal_watch.storage import Storage
from deal_watch.ui.main import health_diagnostics_text, health_status_text
from region_fixtures import ready_region


class EmptyOcr:
    def recognize(self, frame):
        return OcrObservation(frame.region_id, frame.captured_at, frame.sequence, [], 1.0)


class BlockingRecords:
    def __init__(self, fail=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail = fail

    def record_candidates(self, *args, **kwargs):
        self.entered.set()
        if not self.release.wait(3):
            raise TimeoutError("synthetic record write was not released")
        if self.fail:
            raise OSError("synthetic disk failure")


@pytest.fixture
def service_fixture(tmp_path):
    region = ready_region()
    store = Storage(tmp_path / "events.db")
    config = AppConfig(
        regions=[region], save_hit_images=False, ocr_reuse_max_age_seconds=0,
    )
    snapshots = []
    capturer = SimpleNamespace(reset=lambda _rid: None)
    service = MonitorService(config, store, EmptyOcr(), capturer=capturer, on_health=snapshots.append)
    yield service, region, snapshots
    if isinstance(service.recognition_store, BlockingRecords):
        service.recognition_store.release.set()
    service.stop()
    store.close()


def start_ocr(service, region):
    done = threading.Event()
    service.on_frame_processed = lambda *_: done.set()
    service._ocr_thread = threading.Thread(target=service._ocr_loop, daemon=True)
    service._ocr_thread.start()
    service._submit(FrameSample(region.id, 1, time.time(), [], region.rect))
    return done


def enter_fault_then_baseline(service, region):
    service._next_due[region.id] = time.monotonic() + 30
    service._fail_region(
        region.id, service._generation[region.id], "synthetic request deadbeef",
        code=CaptureErrorCode.CAPTURE_TIMEOUT,
        trace=SimpleNamespace(stage_failed="wgc_start_requested"),
    )
    assert service.resume_region(region.id)


def test_recovery_waits_for_records_before_reporting_success(service_fixture):
    service, region, snapshots = service_fixture
    records = service.recognition_store = BlockingRecords()
    enter_fault_then_baseline(service, region)
    done = start_ocr(service, region)
    assert records.entered.wait(2)
    assert service._runtime[region.id].status is RegionStatus.BASELINING
    assert service._runtime[region.id].last_success_at == 0
    assert not any(x.status is RegionStatus.RUNNING for x in snapshots)
    # Windows monotonic ticks can be coarser than this tiny synthetic operation.
    service._runtime[region.id].fault_started_at -= 1.0
    records.release.set()
    assert done.wait(2)
    final = snapshots[-1]
    assert final.status is RegionStatus.RUNNING
    assert final.last_success_at > 0
    assert final.capture_recovery_ms > 0
    assert final.capture_fault_count == 1
    assert final.capture_error_stage == ""
    assert service.storage.list_events() == []
    service._fail_region(
        region.id, service._generation[region.id], "second synthetic episode",
        code=CaptureErrorCode.CAPTURE_TIMEOUT,
    )
    assert snapshots[-1].capture_fault_count == 2


def test_failed_record_write_never_announces_recovery(service_fixture):
    service, region, snapshots = service_fixture
    records = service.recognition_store = BlockingRecords(fail=True)
    enter_fault_then_baseline(service, region)
    done = start_ocr(service, region)
    assert records.entered.wait(2)
    records.release.set()
    assert done.wait(2)
    assert not any(x.status is RegionStatus.RUNNING for x in snapshots)
    final = snapshots[-1]
    assert final.status is RegionStatus.ERROR
    assert final.capture_error_stage == "record_write"
    assert final.capture_error_summary == "识别记录保存失败"
    assert final.last_success_at == 0
    assert final.capture_recovery_ms == 0
    assert final.capture_fault_count == 1


def test_pause_during_records_cannot_be_overwritten_by_old_success(service_fixture):
    service, region, snapshots = service_fixture
    records = service.recognition_store = BlockingRecords()
    done = start_ocr(service, region)
    assert records.entered.wait(2)
    service.pause_region(region.id)
    records.release.set()
    assert done.wait(2)
    assert service._runtime[region.id].status is RegionStatus.PAUSED
    assert snapshots[-1].status is RegionStatus.PAUSED
    assert not any(x.status is RegionStatus.RUNNING for x in snapshots)


def test_fault_episodes_retry_time_and_worker_snapshot(service_fixture):
    service, region, snapshots = service_fixture
    stats = {"state": "reaping", "process_starts": 2, "orphan_processes": 1}
    service.capturer.client = SimpleNamespace(worker_stats=lambda: stats)
    service._next_due[region.id] = time.monotonic() + 30
    for _ in range(2):
        service._fail_region(
            region.id, service._generation[region.id], "synthetic timeout",
            code=CaptureErrorCode.CAPTURE_TIMEOUT,
        )
    health = snapshots[-1]
    assert health.capture_fault_count == 1
    assert 28 <= health.next_retry_at - time.time() <= 31
    assert health.capture_worker_state == "reaping"
    assert health.capture_process_stats == stats


def test_broken_diagnostics_provider_does_not_break_health(service_fixture):
    service, region, snapshots = service_fixture
    service.capturer.client = SimpleNamespace(worker_stats=Mock(side_effect=OSError("synthetic")))
    service._emit_health(region.id)
    assert snapshots[-1].capture_process_stats == {}


def test_pool_prepare_and_close_are_optional_and_ordered(service_fixture):
    service, _region, _snapshots = service_fixture
    calls = []
    service.capturer.prepare = lambda: calls.append("prepare")
    service.capturer.capture = lambda *args, **kwargs: None
    service.capturer.close = lambda: calls.append("close_after_stop" if service._stop.is_set() else "bad")
    service.start()
    service.stop()
    assert calls[:2] == ["prepare", "close_after_stop"]


def test_row_summary_separates_user_status_from_technical_diagnostics():
    health = HealthSnapshot(
        "synthetic", RegionStatus.ERROR, "request deadbeef stage wgc_start_requested",
        capture_error_summary="采集会话启动未完成", capture_error_stage="wgc_start_requested",
        capture_worker_state="reaping", next_retry_at=time.time() + 30,
        capture_fault_count=2, capture_recovery_ms=25000,
        capture_process_stats={
            "process_starts": 3, "failures": 2, "active_processes": 2, "orphan_processes": 1,
            "processes": [{"pid": 101, "private_bytes": 1048576, "handles": 30, "threads": 2}],
        },
        stages_summary="capture:p50=100/p95=120/max=150",
    )
    row = health_status_text(health)
    assert "采集会话启动未完成" in row
    assert "正在回收" in row
    assert "下次重试" in row
    assert "deadbeef" not in row
    tooltip = health_diagnostics_text(health)
    assert "deadbeef" in tooltip
    assert "p95=120" in tooltip
    assert "PID 101" in tooltip
    assert "1.0 MiB" in tooltip
    assert "组件启动次数：3" in tooltip


def test_extended_diagnostics_keep_numeric_fields_redacted():
    clean = sanitize_stage_fields({
        "stage": "request_total", "generation": 3, "worker_id": 101,
        "deadline_overrun_ms": 2000.0, "stage_elapsed_ms": 40.0,
        "stage_offset_ms": 500.0, "process_starts": 2, "failures": 1,
        "recovery_ms": 25000.0, "private_bytes": 1048576, "working_set_bytes": 524288,
        "handles": 30, "threads": 2, "cleanup_state": "reaping",
        "window_title": "secret", "image": "private",
    })
    assert clean["deadline_overrun_ms"] == 2000
    assert clean["cleanup_state"] == "reaping"
    assert "window_title" not in clean
    assert "image" not in clean
    for key in ("generation", "worker_id", "deadline_overrun_ms", "stage_elapsed_ms", "stage_offset_ms",
                "process_starts", "failures", "recovery_ms", "private_bytes",
                "working_set_bytes", "handles", "threads"):
        for value in ("private text", True, -1, float("nan")):
            assert key not in sanitize_stage_fields({key: value})
