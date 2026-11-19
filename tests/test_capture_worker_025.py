"""0.2.5 persistent worker protocol and bounded native lifecycle (synthetic only)."""

from __future__ import annotations

import io
import json
import sys
import threading
import time
import types
from unittest.mock import Mock, patch

import numpy as np
import pytest

from deal_watch import capture_worker as worker


def request(request_id="r1", **changes):
    result = {
        "type": "capture", "protocol_version": 2, "request_id": request_id,
        "region_id": "region1", "generation": 2, "hwnd": 123,
        "backend": "wgc", "roi": [1, 1, 10, 8],
        "reference_frame_width": 40, "reference_frame_height": 30,
        "wgc_timeout_ms": 3000, "session_exit_timeout_ms": 500,
    }
    result.update(changes)
    return result


def frame_payload(**changes):
    result = {"ok": True, "image": "cG5n", "roi": [1, 1, 10, 8],
              "frame_width": 40, "frame_height": 30, "width": 10, "height": 8,
              "captured_at": 1.0}
    result.update(changes)
    return result


def run_server(messages, capture=None, dependencies=None):
    source = io.StringIO("".join(json.dumps(x) + "\n" for x in messages))
    output = io.StringIO()
    with patch.object(worker, "set_dpi_awareness", return_value={"request": "test"}), \
         patch.object(worker, "query_dpi_awareness", return_value="test"), \
         patch.object(worker, "import_self_test", return_value=(
             dependencies if dependencies is not None else {"ok": True, "numpy": "2", "cv2": "5"}
         )) as preloader, \
         patch.object(worker, "capture_wgc", side_effect=capture or (lambda *a, **kw: frame_payload())) as capturer:
        code = worker.serve(source, output)
    return code, [json.loads(x) for x in output.getvalue().splitlines()], preloader, capturer


class Control:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True

    def wait(self):
        return None


class Frame:
    def __init__(self, *, fail=False):
        self.fail = fail

    def convert_to_bgr(self):
        if self.fail:
            raise ValueError("private callback detail")
        image = np.full((30, 40, 3), 240, dtype=np.uint8)
        image[5:20, 6:30] = 30
        return types.SimpleNamespace(frame_buffer=image)


class Capture:
    def __init__(self, frame=None, start_gate=None, control=None, construct_delay=0):
        if construct_delay:
            time.sleep(construct_delay)
        self.handlers = {}
        self.frame = frame
        self.start_gate = start_gate
        self.control = control or Control()

    def event(self, callback):
        self.handlers[callback.__name__] = callback
        return callback

    def start_free_threaded(self):
        if self.start_gate:
            self.start_gate.wait(2)
        if self.frame:
            self.handlers["on_frame_arrived"](self.frame, self.control)
        return self.control


def fake_module(factory):
    return types.SimpleNamespace(WindowsCapture=lambda **kw: factory())


def test_stop_call_itself_is_in_combined_exit_budget():
    gate = threading.Event()
    control = Control()
    control.stop = lambda: gate.wait(2)
    started = time.monotonic()
    try:
        assert worker.stop_session(control, 0.05) is False
        assert time.monotonic() - started < 0.4
    finally:
        gate.set()


def test_exit_wait_exception_is_not_reported_as_success():
    control = Control()
    control.wait = lambda: (_ for _ in ()).throw(OSError("invalid handle"))
    assert worker.stop_session(control, 0.1) is False


def test_already_stopped_session_requires_confirmed_wait():
    control = Control()
    control.stop = lambda: (_ for _ in ()).throw(RuntimeError("already stopped"))
    assert worker.stop_session(control, 0.1) is True


def test_constructor_hang_uses_start_budget_without_capture():
    gate = threading.Event()
    returned = threading.Event()

    def factory():
        gate.wait(2)
        returned.set()
        return Capture(Frame())

    started = time.monotonic()
    try:
        with patch.dict(sys.modules, {"windows_capture": fake_module(factory)}):
            with pytest.raises(TimeoutError, match="WGC_START_TIMEOUT"):
                worker.capture_wgc(123, timeout=0.05, channel=worker.StageChannel())
        assert time.monotonic() - started < 0.4
    finally:
        gate.set()
        assert returned.wait(1)


def test_native_start_hang_uses_same_budget_and_drops_late_frame():
    gate = threading.Event()
    capture = Capture(Frame(), start_gate=gate)
    channel = worker.StageChannel()
    try:
        with patch.dict(sys.modules, {"windows_capture": fake_module(lambda: capture)}):
            with pytest.raises(TimeoutError, match="WGC_START_TIMEOUT"):
                worker.capture_wgc(123, timeout=0.05, channel=channel)
        assert "first_valid_frame_ms" not in channel.stages
    finally:
        gate.set()


def test_constructor_and_frame_wait_do_not_each_get_full_budget():
    capture = Capture()

    def factory():
        time.sleep(0.07)
        return capture

    started = time.monotonic()
    with patch.dict(sys.modules, {"windows_capture": fake_module(factory)}):
        with pytest.raises(TimeoutError, match="WGC_FRAME_TIMEOUT"):
            worker.capture_wgc(123, timeout=0.12, channel=worker.StageChannel())
    assert time.monotonic() - started < 0.18
    assert capture.control.stopped


def test_conversion_error_wakes_waiter_and_is_sanitized():
    capture = Capture(Frame(fail=True))
    with patch.dict(sys.modules, {"windows_capture": fake_module(lambda: capture)}):
        with pytest.raises(RuntimeError, match="^WGC_CALLBACK_FAILED$"):
            worker.capture_wgc(123, timeout=0.5, channel=worker.StageChannel())
    assert capture.control.stopped


def test_server_preloads_once_and_echoes_each_request_identity():
    code, output, preloader, capturer = run_server([
        request(), request("r2", region_id="region2", generation=9, hwnd=456),
        {"type": "shutdown", "protocol_version": 2},
    ])
    assert code == 0
    assert preloader.call_count == 1
    assert capturer.call_count == 2
    assert output[0]["type"] == "ready" and output[0]["ok"]
    for response, rid, region, generation, hwnd in (
        (output[1], "r1", "region1", 2, 123), (output[2], "r2", "region2", 9, 456),
    ):
        assert response["type"] == "result" and response["protocol_version"] == 2
        assert response["request_id"] == rid and response["region_id"] == region
        assert response["generation"] == generation and response["hwnd"] == hwnd
        assert response["worker_reusable"]
        assert response["reference_frame_width"] == 40


def test_server_idle_eof_does_not_start_a_capture():
    code, output, _, capturer = run_server([])
    assert code == 0 and len(output) == 1
    capturer.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"generation": True}, {"generation": -1}, {"hwnd": False}, {"hwnd": 0},
    {"protocol_version": True}, {"protocol_version": 1}, {"type": "other"},
    {"wgc_timeout_ms": 3001}, {"session_exit_timeout_ms": 1001},
    {"roi": [0, 0, 0, 8]}, {"roi": [False, 0, 10, 8]}, {"roi": "1,2,3,4"},
    {"backend": "desktop"}, {"reference_frame_width": -1},
    {"request_id": "private title"}, {"region_id": "private title"},
])
def test_malformed_request_exits_without_capture_or_echoing_input(changes):
    code, output, _, capturer = run_server([request(**changes)])
    expected = "PROTOCOL_VERSION_MISMATCH" if changes.get("protocol_version") == 1 and type(changes.get("protocol_version")) is int else "PROTOCOL_ERROR"
    assert code == 1 and output[-1]["error"] == expected
    capturer.assert_not_called()
    assert "private title" not in json.dumps(output)


def test_unknown_keys_are_rejected():
    code, output, _, capturer = run_server([request(secret="do not echo")])
    assert code == 1 and output[-1]["error"] == "PROTOCOL_ERROR"
    assert "do not echo" not in json.dumps(output)
    capturer.assert_not_called()


def test_oversized_line_is_bounded_and_rejected():
    output = io.StringIO()
    with patch.object(worker, "import_self_test", return_value={"ok": True}), \
         patch.object(worker, "set_dpi_awareness", return_value={}), \
         patch.object(worker, "query_dpi_awareness", return_value="test"):
        assert worker.serve(io.StringIO("x" * (worker.MAX_REQUEST_CHARS + 100)), output) == 1
    assert json.loads(output.getvalue().splitlines()[-1])["error"] == "PROTOCOL_ERROR"


def test_startup_failure_is_fixed_code_without_private_dependency_error():
    code, output, _, capturer = run_server([], dependencies={
        "ok": False, "error": "sensitive path or token", "error_detail": "private traceback",
    })
    assert code == 1
    assert output[0]["error"] == "DEPENDENCY_INIT_FAILED"
    assert "private" not in json.dumps(output) and "sensitive" not in json.dumps(output)
    capturer.assert_not_called()


@pytest.mark.parametrize("token", ["WGC_START_TIMEOUT", "WGC_FRAME_TIMEOUT",
                                   "WGC_SESSION_EXIT_TIMEOUT", "WGC_CALLBACK_FAILED"])
def test_poisoned_worker_exits_before_second_request(token):
    def fail(*args, **kwargs):
        raise RuntimeError(token)

    code, output, _, capturer = run_server([request(), request("r2")], capture=fail)
    assert code == 1 and capturer.call_count == 1
    assert len(output) == 2 and output[1]["error"] == token
    assert output[1]["worker_reusable"] is False
    assert "image" not in output[1]


def test_clean_frame_rejection_does_not_poison_worker():
    def reject(*args, **kwargs):
        raise RuntimeError("BLACK_FRAME")

    code, output, _, capturer = run_server([request(), request("r2")], capture=reject)
    assert code == 0 and capturer.call_count == 2
    assert output[1]["worker_reusable"] and output[2]["worker_reusable"]


def test_reference_dimension_mismatch_never_returns_pixels():
    code, output, _, _ = run_server([request(reference_frame_width=41)])
    assert code == 0
    assert output[1]["error"] == "FRAME_SIZE_CHANGED"
    assert "image" not in output[1]


def test_duplicate_request_is_rejected_without_resampling():
    code, output, _, capturer = run_server([request(), request()])
    assert code == 1 and capturer.call_count == 1
    assert output[-1]["error"] == "PROTOCOL_ERROR"


def test_stage_timestamps_have_separate_delta_and_request_identity():
    channel = worker.StageChannel()
    channel.reset("r1")
    channel.region_id = "region1"
    channel.generation = 7
    output = io.StringIO()
    with patch.object(sys, "stderr", output):
        channel.mark("request_received")
        channel.mark("worker_complete")
    first, second = [json.loads(line) for line in output.getvalue().splitlines()]
    assert first["request_id"] == "r1" and first["region_id"] == "region1"
    assert second["generation"] == 7 and second["pid"] > 0
    assert second["t_ms"] >= second["delta_ms"] >= 0


def test_sequential_native_requests_release_state_and_ignore_late_callback():
    captures = [Capture(Frame()), Capture(Frame())]
    for capture in captures:
        capture.frame_handler = object()
        capture.closed_handler = object()
        capture.capture = object()
    factory_values = iter(captures)
    channels = [worker.StageChannel(), worker.StageChannel()]
    encoded = []

    def encode(image, width, height, **kwargs):
        encoded.append(image.copy())
        return frame_payload(image=str(len(encoded)))

    with patch.dict(sys.modules, {"windows_capture": fake_module(lambda: next(factory_values))}), \
         patch.object(worker, "encode", side_effect=encode):
        first = worker.capture_wgc(123, roi=(1, 1, 10, 8), channel=channels[0])
        old_stages = dict(channels[0].stages)
        captures[0].handlers["on_frame_arrived"](Frame(fail=True), captures[0].control)
        second = worker.capture_wgc(456, roi=(1, 1, 10, 8), channel=channels[1])
    assert first["image"] == "1" and second["image"] == "2"
    assert channels[0].stages == old_stages
    assert all(image.shape == (8, 10, 3) for image in encoded)
    for capture in captures:
        assert capture.frame_handler is None and capture.closed_handler is None
        assert capture.capture is None and capture.control.stopped


def test_serve_limits_opencv_threads_once_without_changing_environment():
    import os

    setter = Mock()
    before = dict(os.environ)
    with patch.dict(sys.modules, {"cv2": types.SimpleNamespace(setNumThreads=setter)}):
        code, _, _, capturer = run_server([request(), request("r2")])
    assert code == 0 and capturer.call_count == 2
    setter.assert_called_once_with(1)
    assert dict(os.environ) == before


def test_one_shot_self_test_keeps_existing_opencv_thread_policy():
    setter = Mock()
    output = io.StringIO()
    with patch.dict(sys.modules, {"cv2": types.SimpleNamespace(setNumThreads=setter)}), \
         patch.object(worker, "import_self_test", return_value={"ok": True}), \
         patch.object(worker, "set_dpi_awareness", return_value={}), \
         patch.object(worker, "query_dpi_awareness", return_value="test"), \
         patch.object(sys, "argv", ["capture_worker", "--self-test"]), \
         patch.object(sys, "stdout", output):
        assert worker.main() == 0
    setter.assert_not_called()


def test_serve_allows_lightweight_test_dependency_without_thread_setter():
    with patch.dict(sys.modules, {"cv2": types.SimpleNamespace()}):
        code, output, _, _ = run_server([])
    assert code == 0 and output[0]["ok"]


def test_blocked_conversion_is_visible_and_late_callback_cannot_publish():
    gate = threading.Event()
    entered = threading.Event()
    callback_done = threading.Event()
    channel = worker.StageChannel()
    callback_thread = None

    class BlockedFrame:
        def convert_to_bgr(self):
            entered.set()
            gate.wait(2)
            return Frame().convert_to_bgr()

    class AsyncCapture(Capture):
        def start_free_threaded(self):
            nonlocal callback_thread

            def deliver():
                try:
                    self.handlers["on_frame_arrived"](BlockedFrame(), self.control)
                finally:
                    callback_done.set()

            # Model native wait faithfully: exit is not confirmed while a frame
            # callback is still running, even though stop has been requested.
            self.control.wait = lambda: callback_done.wait(2)
            callback_thread = threading.Thread(target=deliver, daemon=True)
            callback_thread.start()
            return self.control

    capture = AsyncCapture()
    try:
        with patch.dict(sys.modules, {"windows_capture": fake_module(lambda: capture)}), \
             patch.object(worker, "encode") as encoder:
            started = time.monotonic()
            with pytest.raises(RuntimeError, match="WGC_SESSION_EXIT_TIMEOUT"):
                worker.capture_wgc(123, timeout=0.1, session_exit_timeout=0.05, channel=channel)
            assert time.monotonic() - started < 0.5
            assert entered.is_set()
            assert "frame_callback_enter_ms" in channel.stages
            assert "first_valid_frame_ms" not in channel.stages
            encoder.assert_not_called()
            before_late = dict(channel.stages)
            gate.set()
            assert callback_done.wait(1)
            callback_thread.join(1)
            assert not callback_thread.is_alive()
            assert channel.stages == before_late
            encoder.assert_not_called()
    finally:
        gate.set()
        if callback_thread is not None:
            callback_thread.join(1)
    # An independent fresh request works normally; the prior callback state is
    # cancelled. The serve-loop tests separately require the poisoned worker exit.
    next_channel = worker.StageChannel()
    with patch.dict(sys.modules, {"windows_capture": fake_module(lambda: Capture(Frame()))}), \
         patch.object(worker, "encode", return_value=frame_payload()) as encoder:
        payload = worker.capture_wgc(456, timeout=0.5, channel=next_channel)
        capture.handlers["on_frame_arrived"](Frame(), capture.control)
    assert payload["ok"]
    encoder.assert_called_once()
    assert channel.stages == before_late
    assert "first_valid_frame_ms" in next_channel.stages


def test_first_callback_diagnostic_fits_complete_one_shot_stage_budget():
    channel = worker.StageChannel()
    output = io.StringIO()
    with patch.object(sys, "stderr", output), \
         patch.dict(sys.modules, {"windows_capture": fake_module(lambda: Capture(Frame()))}), \
         patch.object(worker, "encode", side_effect=lambda *args, **kwargs: (
             kwargs["channel"].mark("encode"), frame_payload())[1]):
        for stage in ("component_enter", "worker_started", "dpi_aware"):
            channel.mark(stage)
        worker.capture_wgc(123, timeout=0.5, channel=channel)
        channel.mark("total")
        channel.mark("worker_complete")
    stages = [json.loads(line)["stage"] for line in output.getvalue().splitlines()]
    assert len(stages) == worker.MAX_STAGE_LINES == 16
    assert stages[-1] == "worker_complete"
    assert stages.index("frame_callback_enter") < stages.index("first_valid_frame")
