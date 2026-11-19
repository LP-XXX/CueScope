"""Synthetic protocol/deadline checks; no desktop capture or actual WGC session."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from deal_watch import capture_pool
from deal_watch.capture_pool import CapturePool, PoolFailure
from deal_watch.models import CaptureErrorCode
from deal_watch.window_capture import PROCESS_REGISTRY, WindowCaptureError
from test_capture_pool_025 import SCRIPT, client_for


@pytest.fixture
def clients():
    made = []
    yield made
    for client in made:
        client.close()
    deadline = time.monotonic() + 6
    while PROCESS_REGISTRY.active and time.monotonic() < deadline:
        time.sleep(0.025)
    assert PROCESS_REGISTRY.active == 0


def test_ready_response_after_startup_deadline_is_rejected(monkeypatch):
    tick = [time.monotonic()]
    deadline = tick[0] + 1
    monkeypatch.setattr(capture_pool, "time", SimpleNamespace(monotonic=lambda: tick[0]))

    class LateResponse:
        def get(self, timeout):
            tick[0] = deadline + 0.01
            return {"type": "ready", "protocol_version": 2, "ok": True}

    worker = SimpleNamespace(responses=LateResponse(), broken=threading.Event())
    with pytest.raises(PoolFailure) as failure:
        CapturePool._receive(worker, deadline)
    assert failure.value.stage == "deps_init"
    assert failure.value.timeout


def test_wait_ready_cannot_accept_a_worker_after_its_own_budget(monkeypatch):
    tick = [time.monotonic()]
    deadline = tick[0] + 1
    monkeypatch.setattr(capture_pool, "time", SimpleNamespace(monotonic=lambda: tick[0]))
    pool = CapturePool(["unused"])
    monkeypatch.setattr(pool, "prepare", lambda: None)
    worker = SimpleNamespace(ready=False, broken=threading.Event())
    pool._workers = [worker]

    class WakeAfterBudget:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def wait(self, timeout):
            tick[0] = deadline + 0.01
            worker.ready = True

    pool._condition = WakeAfterBudget()
    assert not pool.wait_ready(1)


@pytest.mark.parametrize("failure_kind", ["slow_start_log", "slow_ready_log", "raise_start_log"])
def test_startup_diagnostics_cannot_leak_or_publish_a_late_worker(monkeypatch, failure_kind):
    tick = [time.monotonic()]
    monkeypatch.setattr(capture_pool, "time", SimpleNamespace(monotonic=lambda: tick[0]))
    process = SimpleNamespace(pid=12345)
    job = SimpleNamespace(event=SimpleNamespace(wait=lambda timeout: True),
                          claim=lambda: process, token="synthetic", abandon=Mock())
    monkeypatch.setattr(capture_pool.wc.START_SUPERVISOR, "start", lambda *args: job)
    worker = SimpleNamespace(ready=False, start_io=Mock(), retire=Mock())
    monkeypatch.setattr(capture_pool, "_Worker", lambda *args: worker)
    pool = CapturePool(["unused"], startup_timeout=1)
    monkeypatch.setattr(pool, "_receive", lambda *args: {
        "type": "ready", "protocol_version": 2, "ok": True,
    })

    def diagnostic(*args, stage, **kwargs):
        if failure_kind == "raise_start_log" and stage == "pool_component_start":
            raise OSError("synthetic log failure")
        if ((failure_kind == "slow_start_log" and stage == "pool_component_start")
                or (failure_kind == "slow_ready_log" and stage == "pool_dependencies_ready")):
            tick[0] += 2

    monkeypatch.setattr(capture_pool.wc, "log_capture_stage", diagnostic)
    with pytest.raises((PoolFailure, OSError)):
        pool._launch()
    worker.retire.assert_called_once()
    assert not worker.ready


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


def test_wgc_failure_can_recover_when_the_spare_is_still_initializing(clients, tmp_path):
    first_marker = tmp_path / "first-component"
    release_spare = tmp_path / "release-spare"
    startup_gate = f"""
try:
 fd=os.open({str(first_marker)!r},os.O_WRONLY|os.O_CREAT|os.O_EXCL);os.close(fd)
except FileExistsError:
 while not os.path.exists({str(release_spare)!r}):time.sleep(.01)
"""
    ready_line = "print(json.dumps({'type':'ready'"
    script = SCRIPT.replace(ready_line, startup_gate + ready_line, 1)
    client = client_for(clients, script=script)
    assert client.wait_ready(5)
    wait_until(lambda: client._pool._starts == 2)
    assert len(client._pool._workers) == 1  # the spare has a lease but no ready handshake
    first_pid = client._pool._workers[0].process.pid
    try:
        started = time.monotonic()
        with pytest.raises(WindowCaptureError) as failure:
            client.capture(99, region_id="failing-region")
        assert failure.value.code is CaptureErrorCode.CAPTURE_TIMEOUT
        assert failure.value.trace.stage_failed == "wgc_construct_started"
        assert time.monotonic() - started < 3.8
        assert PROCESS_REGISTRY.active <= 2
    finally:
        release_spare.touch()
    recovered = client.capture(2, region_id="next-region", generation=4)
    assert recovered.trace.worker_id != first_pid
    assert recovered.trace.region_id == "next-region"
    assert recovered.trace.generation == 4
    assert recovered.image.shape == (3, 4, 3)
    assert PROCESS_REGISTRY.active <= 2


def test_late_transport_is_quarantined_and_another_region_uses_the_warm_spare(clients):
    # Accelerated transport deadline only; this is not a production timing claim.
    script = SCRIPT.replace(" if r['hwnd']==98:", " if r['hwnd']==94:time.sleep(.6)\n if r['hwnd']==98:")
    client = client_for(clients, script=script, timeout=0.3)
    assert client.wait_ready(5)
    wait_until(lambda: len(client._pool._workers) == 2)
    with pytest.raises(WindowCaptureError) as failure:
        client.capture(94, region_id="late-region", generation=1)
    assert failure.value.code is CaptureErrorCode.CAPTURE_TIMEOUT
    assert failure.value.trace.stage_failed == "transport"
    failed_pid = failure.value.trace.worker_id
    result = client.capture(2, region_id="other-region", generation=2)
    assert result.trace.worker_id != failed_pid
    assert result.trace.region_id == "other-region"
    assert result.trace.generation == 2
    assert result.image.shape == (3, 4, 3)
    assert PROCESS_REGISTRY.active <= 2
