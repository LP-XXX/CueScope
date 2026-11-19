"""0.2.4采集所有权、待回收额度与读槽资源的纯模拟回归。"""

from __future__ import annotations

import subprocess
import threading
import time
from io import StringIO
from unittest.mock import patch

from deal_watch.models import CaptureErrorCode
from deal_watch.window_capture import (
    _READER_SLOTS,
    MAX_LIVE_READERS,
    PROCESS_REGISTRY,
    WindowCaptureClient,
    WindowCaptureError,
    _ProcessRegistry,
    _start_readers,
    _StartJob,
)


class DelayedExitProcess:
    def __init__(self, exits_after: float = 0.5) -> None:
        self.stdout = None
        self.stderr = None
        self.returncode = None
        self.kills = 0
        self.exits_after = exits_after
        self.created = time.monotonic()

    def kill(self) -> None:
        self.kills += 1

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("fake", timeout)

    def poll(self):
        if time.monotonic() - self.created >= self.exits_after:
            self.returncode = -9
        return self.returncode


class TextStream:
    def __iter__(self):
        yield from ()

    def close(self):
        pass


class WaitOSErrorProcess:
    def __init__(self) -> None:
        self.stdout = StringIO("")
        self.stderr = StringIO("")
        self.returncode = None
        self.kills = 0
        self.poll_calls = 0

    def wait(self, timeout=None):
        raise OSError("simulated wait failure")

    def kill(self):
        self.kills += 1
        self.returncode = -9

    def poll(self):
        self.poll_calls += 1
        if self.poll_calls == 1:
            raise OSError("simulated poll failure")
        return self.returncode


def test_start_job_has_one_atomic_claim_and_cannot_reclaim_after_abandon():
    job = _StartJob(object(), time.monotonic() + 1.0)
    process = object()
    job.ready(process, None)
    claims = []
    threads = [threading.Thread(target=lambda: claims.append(job.claim())) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert claims.count(process) == 1
    assert claims.count(None) == 7
    job.abandon()
    assert job.claim() is None
    assert job.state == "claimed"


def test_abandoned_start_cannot_be_claimed_after_spawn_returns():
    job = _StartJob(object(), time.monotonic() + 1.0)
    job.abandon()
    job.ready(object(), None)
    assert job.event.is_set()
    assert job.claim() is None
    assert job.state == "abandoned"


def test_claim_after_shared_deadline_is_rejected():
    job = _StartJob(object(), time.monotonic() - 0.01)
    job.ready(object(), None)
    assert job.claim() is None
    assert job.state == "abandoned"


def test_orphan_keeps_capacity_until_background_poll_confirms_exit():
    registry = _ProcessRegistry(limit=1)
    token = registry.reserve()
    process = DelayedExitProcess(exits_after=0.45)
    assert token is not None
    assert registry.track_orphan(process, token) is False
    assert process.kills == 1
    assert registry.active == registry.orphans == 1
    assert registry.reserve() is None
    deadline = time.monotonic() + 2.0
    while registry.active and time.monotonic() < deadline:
        time.sleep(0.02)
    assert registry.active == 0
    assert registry.orphans == 0
    assert process.kills >= 1
    assert registry.reserve() is not None


def test_wait_and_poll_errors_register_for_background_reap():
    registry = _ProcessRegistry(limit=1)

    class WaitAndPollErrorProcess(DelayedExitProcess):
        def __init__(self):
            super().__init__(exits_after=0)
            self.poll_calls = 0

        def wait(self, timeout=None):
            raise OSError("wait failed")

        def poll(self):
            self.poll_calls += 1
            if self.poll_calls == 1:
                raise OSError("poll failed")
            self.returncode = -9
            return self.returncode

    token = registry.reserve()
    process = WaitAndPollErrorProcess()
    assert registry.track_orphan(process, token) is False
    assert registry.active == registry.orphans == 1
    deadline = time.monotonic() + 2.0
    while registry.active and time.monotonic() < deadline:
        time.sleep(0.02)
    assert registry.active == registry.orphans == 0


def test_claimed_wait_failure_maps_to_capture_error_and_repeated_quota_recovers():
    client = WindowCaptureClient(timeout=0.35)
    baseline = PROCESS_REGISTRY.active
    for _ in range(4):
        process = WaitOSErrorProcess()
        with patch("deal_watch.window_capture.spawn", return_value=process):
            try:
                client.capture(1)
            except WindowCaptureError as error:
                assert error.code == CaptureErrorCode.CAPTURE_FAILED
                assert error.trace.stage_failed == "wait_exit"
            else:
                raise AssertionError("wait failure must become WindowCaptureError")
        deadline = time.monotonic() + 2.0
        while PROCESS_REGISTRY.active > baseline and time.monotonic() < deadline:
            time.sleep(0.02)
        assert PROCESS_REGISTRY.active == baseline
        assert process.kills >= 1


def test_reader_start_oserror_is_wrapped_and_releases_process_quota():
    client = WindowCaptureClient(timeout=0.35)
    process = WaitOSErrorProcess()
    baseline = PROCESS_REGISTRY.active
    process.wait = lambda timeout=None: setattr(process, "returncode", 0) or 0
    with patch("deal_watch.window_capture.spawn", return_value=process), patch(
        "deal_watch.window_capture._start_readers", side_effect=OSError("reader failure")
    ):
        try:
            client.capture(1)
        except WindowCaptureError as error:
            assert error.code == CaptureErrorCode.CAPTURE_FAILED
            assert error.trace.stage_failed == "reader_start"
        else:
            raise AssertionError("reader failure must become WindowCaptureError")
    deadline = time.monotonic() + 2.0
    while PROCESS_REGISTRY.active > baseline and time.monotonic() < deadline:
        time.sleep(0.02)
    assert PROCESS_REGISTRY.active == baseline
    assert process.kills >= 1


def test_repeated_unconfirmed_failures_cannot_exceed_lifecycle_limit():
    registry = _ProcessRegistry(limit=2)
    tokens = [registry.reserve(), registry.reserve()]
    assert all(tokens)
    for token in tokens:
        assert registry.track_orphan(DelayedExitProcess(exits_after=0.4), token) is False
    assert registry.active == registry.orphans == 2
    assert registry.reserve() is None
    deadline = time.monotonic() + 2.0
    while registry.active and time.monotonic() < deadline:
        time.sleep(0.02)
    assert registry.active == registry.orphans == 0
    assert registry.reserve() is not None


def test_reader_slots_fail_atomically_then_recover():
    held = MAX_LIVE_READERS - 1
    for _ in range(held):
        assert _READER_SLOTS.acquire(blocking=False)
    process = type("P", (), {"stdout": TextStream(), "stderr": TextStream()})()
    assert _start_readers(process, [], []) == []
    for _ in range(held):
        _READER_SLOTS.release()
    readers = _start_readers(process, [], [])
    assert len(readers) == 2
    for reader in readers:
        reader.join(timeout=1.0)
        assert not reader.is_alive()
    assert _READER_SLOTS.acquire(blocking=False)
    _READER_SLOTS.release()
