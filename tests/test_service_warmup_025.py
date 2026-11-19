"""组件预热在采集后台线程完成，独立于每次采样预算。"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from deal_watch.config import AppConfig
from deal_watch.models import RegionStatus
from deal_watch.service import MonitorService
from deal_watch.storage import Storage
from region_fixtures import ready_region


@pytest.fixture
def runtime(tmp_path):
    regions = [ready_region(f"合成区域 {i + 1}") for i in range(5)]
    storage = Storage(tmp_path / "warmup.db")
    captured = threading.Event()
    capturer = SimpleNamespace(
        prepare=Mock(), close=Mock(), reset=Mock(),
        capture=Mock(side_effect=lambda *args, **kwargs: captured.set()),
    )
    snapshots = []
    service = MonitorService(
        AppConfig(regions=regions, poll_interval_ms=30000, save_hit_images=False),
        storage, SimpleNamespace(recognize=Mock()), capturer=capturer,
        on_health=snapshots.append,
    )
    yield service, capturer, snapshots, captured
    service.stop()
    storage.close()


def test_first_capture_waits_for_preheated_worker_without_blocking_start(runtime):
    service, capturer, snapshots, captured = runtime
    entered, release = threading.Event(), threading.Event()

    def ready(timeout):
        assert timeout == 5.0
        entered.set()
        assert release.wait(2)
        return True

    capturer.wait_ready = Mock(side_effect=ready)
    capturer.close = release.set
    try:
        started = time.monotonic()
        service.start()
        assert time.monotonic() - started < 0.5
        assert entered.wait(1)
        assert not captured.is_set()
        assert len(snapshots) == 5
        assert all(s.status is RegionStatus.BASELINING for s in snapshots)
        assert all("尚未开始采样" in s.detail for s in snapshots)
        release.set()
        assert captured.wait(1)
        capturer.wait_ready.assert_called_once_with(5.0)
        assert not any(s.status is RegionStatus.ERROR for s in snapshots)
    finally:
        release.set()


def test_failed_warmup_reports_each_region_once_without_serial_capture_timeouts(runtime):
    service, capturer, snapshots, captured = runtime
    failures_reported = threading.Event()

    def health(snapshot):
        snapshots.append(snapshot)
        if sum(s.status is RegionStatus.ERROR for s in snapshots) == 5:
            failures_reported.set()

    service.on_health = health
    capturer.wait_ready = Mock(return_value=False)
    service.start()
    assert failures_reported.wait(1)
    assert not captured.wait(0.1)
    errors = [s for s in snapshots if s.status is RegionStatus.ERROR]
    assert len(errors) == 5
    assert {s.region_id for s in errors} == set(service._regions)
    assert all(s.capture_error_stage == "worker_ready" for s in errors)
    assert all(s.capture_fault_count == 1 for s in errors)
    assert all(28 < s.next_retry_at - time.time() <= 31 for s in errors)
    capturer.wait_ready.assert_called_once_with(5.0)
    capturer.capture.assert_not_called()


def test_stop_during_warmup_cancels_without_reporting_faults(runtime):
    service, capturer, snapshots, captured = runtime
    entered, cancelled = threading.Event(), threading.Event()

    def ready(_timeout):
        entered.set()
        cancelled.wait(2)
        return False

    capturer.wait_ready = ready
    capturer.close = cancelled.set
    service.start()
    assert entered.wait(1)
    service.stop()
    assert not service.is_alive()
    assert not captured.is_set()
    assert not any(s.status is RegionStatus.ERROR for s in snapshots)


def test_incompatible_component_remains_explicit_in_production_health(runtime):
    from deal_watch.models import CaptureErrorCode
    from deal_watch.window_capture import CaptureTrace, WindowCaptureError
    service, capturer, snapshots, captured = runtime
    completed = threading.Event()
    def health(snapshot):
        snapshots.append(snapshot)
        if sum(s.status is RegionStatus.ERROR for s in snapshots) == 5:
            completed.set()
    service.on_health = health
    capturer.wait_ready = Mock(side_effect=WindowCaptureError(
        CaptureErrorCode.CAPTURE_FAILED, '采集组件协议版本不匹配，请使用同版完整便携包',
        CaptureTrace(stage_failed='component_version'),
    ))
    service.start()
    assert completed.wait(2)
    failures = [s for s in snapshots if s.status is RegionStatus.ERROR]
    assert all(s.capture_error_stage == 'component_version' for s in failures)
    assert all('版本不匹配' in s.detail for s in failures)
    assert not captured.is_set()
