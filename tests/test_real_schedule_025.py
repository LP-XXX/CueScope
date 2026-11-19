"""Production service probe wiring, without reading user config or capturing windows."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from deal_watch import window_capture
from deal_watch.capture import CaptureError
from deal_watch.models import CaptureErrorCode, Rect

_spec = importlib.util.spec_from_file_location(
    "real_schedule_025_test", Path(__file__).resolve().parents[1] / "tools/real_schedule_60s.py"
)
assert _spec is not None and _spec.loader is not None
schedule = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(schedule)


def test_recording_adapter_uses_persistent_component_and_forwards_lifecycle(tmp_path, monkeypatch):
    client = Mock()
    client_factory = Mock(return_value=client)
    inner = Mock(client=client, windows=object(), last_trace=object())
    adapter_factory = Mock(return_value=inner)
    monkeypatch.setattr(window_capture, "WindowCaptureClient", client_factory)
    monkeypatch.setattr(window_capture, "WindowFrameCapturer", adapter_factory)
    component = tmp_path / "DealWatchCapture.exe"
    capturer = schedule.RecordingCapturer(component_path=component)
    client_factory.assert_called_once_with(timeout=5.0, persistent=True, component_path=component)
    adapter_factory.assert_called_once_with(client=client)
    assert capturer.client is client
    assert capturer.windows is inner.windows
    assert capturer.last_trace is inner.last_trace
    capturer.prepare()
    inner.wait_ready.return_value = True
    assert capturer.wait_ready(5.0) is True
    capturer.reset("region-a")
    capturer.close()
    inner.prepare.assert_called_once_with()
    inner.wait_ready.assert_called_once_with(5.0)
    inner.reset.assert_called_once_with("region-a")
    inner.close.assert_called_once_with()


def test_capture_component_argument_and_default_preserve_source_mode():
    assert schedule.parse_args([]).capture_component is None
    args = schedule.parse_args(["--capture-component", "candidate.exe",
                                "--rounds", "10", "--interval-seconds", "30"])
    assert args.capture_component == Path("candidate.exe")
    assert args.rounds == 10
    assert args.interval_seconds == 30
    assert "src/deal_watch/capture_pool.py" in schedule.FINGERPRINT_FILES


def test_real_chat_temporary_storage_is_inside_build(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "ROOT", tmp_path)
    with schedule.temporary_storage() as directory:
        path = Path(directory)
        assert path.is_relative_to(tmp_path / "build" / "validation025")
        assert path.is_dir()
    assert not path.exists()


@pytest.mark.parametrize("device,rect", [
    (r"\\.\DISPLAY2", Rect(0, 0, 100, 100)),
    (r"\\.\DISPLAY1", Rect(950, 0, 100, 100)),
])
def test_capture_refuses_other_monitor_before_touching_pixels(monkeypatch, device, rect):
    inner = Mock(last_trace=object())
    monkeypatch.setattr(window_capture, "WindowFrameCapturer", Mock(return_value=inner))
    monkeypatch.setattr(window_capture, "WindowCaptureClient", Mock())
    monkeypatch.setattr(schedule.real_env, "monitor_device", lambda hwnd: device)
    monkeypatch.setattr(schedule.real_env, "visible_rect", lambda hwnd: rect)
    monkeypatch.setattr(schedule.real_env, "monitor_rect", lambda: {
        "left": 0, "top": 0, "width": 1000, "height": 1000})
    capturer = schedule.RecordingCapturer()
    region = SimpleNamespace(id="region-a", alias="private group", window_hwnd=123)
    with pytest.raises(CaptureError) as failure:
        capturer.capture(region)
    assert failure.value.code is CaptureErrorCode.INVALID_REGION
    inner.capture.assert_not_called()
    assert capturer.last_trace is None
    assert capturer.failure_summary() == {"invalid_region": 1}
    assert "private group" not in repr(capturer.failure_samples())
