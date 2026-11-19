"""Capture validation evidence: fake windows and clocks, no desktop access."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from deal_watch.models import FrameSample, Rect, RegionConfig

_spec = importlib.util.spec_from_file_location(
    "validate_capture_025", Path(__file__).resolve().parents[1] / "tools/validate_capture_025.py"
)
assert _spec is not None and _spec.loader is not None
validation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validation)


def region():
    return RegionConfig(
        alias="PRIVATE GROUP", window_title="PRIVATE TITLE", window_hwnd=123,
        rect=Rect(4, 40, 60, 50), process_id=9, process_path=r"C:\Weixin\Weixin.exe",
        window_class="Weixin", coordinate_space="frame", previewed=True,
        reference_frame_width=100, reference_frame_height=100,
    )


def window_for(item):
    return SimpleNamespace(
        title=item.window_title, class_name=item.window_class, minimized=False,
        process_id=item.process_id, process_path=item.process_path,
    )


class Clock:
    def __init__(self):
        self.value = 100.0

    def time(self):
        return self.value

    def monotonic(self):
        return self.value


def test_audit_uses_windows_device_and_redacts_identifiers():
    item = region()
    api = SimpleNamespace(window_from_hwnd=lambda hwnd: window_for(item))
    records = validation.audit_regions(
        [item], api, lambda hwnd: r"\\.\DISPLAY1",
        {"left": 0, "top": 0, "width": 1000, "height": 1000},
        lambda hwnd: Rect(0, 0, 300, 400),
    )
    assert records[0]["ready"]
    text = json.dumps(records)
    assert "PRIVATE" not in text
    assert item.process_path not in text
    assert records[0]["window"] != "123"


@pytest.mark.parametrize("device,rect,reason", [
    (r"\\.\DISPLAY2", Rect(0, 0, 300, 400), "not_display1"),
    (r"\\.\DISPLAY1", Rect(900, 0, 300, 400), "not_fully_inside_display1"),
])
def test_audit_rejects_other_monitor_and_straddling(device, rect, reason):
    item = region()
    result = validation.audit_regions(
        [item], SimpleNamespace(window_from_hwnd=lambda hwnd: window_for(item)),
        lambda hwnd: device,
        {"left": 0, "top": 0, "width": 1000, "height": 1000}, lambda hwnd: rect,
    )
    assert not result[0]["ready"]
    assert reason in result[0]["reasons"]


def test_audit_rejects_changed_identity_without_exposing_title():
    item = region()
    current = window_for(item)
    current.title = "OTHER PRIVATE TITLE"
    result = validation.audit_regions(
        [item], SimpleNamespace(window_from_hwnd=lambda hwnd: current),
        lambda hwnd: r"\\.\DISPLAY1",
        {"left": 0, "top": 0, "width": 1000, "height": 1000},
        lambda hwnd: Rect(0, 0, 300, 400),
    )
    assert result[0]["reasons"] == ["region_not_ready"]
    assert "PRIVATE" not in json.dumps(result)


def test_capture_discards_pixels_and_records_real_duration():
    item = region()
    clock = Clock()

    def capture(region, force):
        clock.value += .25
        return FrameSample(item.id, 1, clock.time(), np.zeros((50, 60, 3)), item.rect)

    sampler = SimpleNamespace(capture=capture, last_trace=None)
    result = validation.capture_sample(sampler, item, clock=clock)
    assert result["ok"]
    assert result["elapsed_ms"] == 250
    assert "image" not in result
    assert "text" not in result


@pytest.mark.parametrize("sequence,stamp,code", [
    (1, 100, "stale_or_wrong_region_frame"),
    (2, 90, "stale_frame_timestamp"),
])
def test_capture_rejects_stale_results(sequence, stamp, code):
    item = region()
    frame = FrameSample(item.id, sequence, stamp, np.zeros((50, 60, 3)), item.rect)
    result = validation.capture_sample(
        SimpleNamespace(capture=lambda *a, **k: frame, last_trace=None), item,
        previous_sequence=1, clock=Clock(),
    )
    assert not result["ok"]
    assert result["error_code"] == code


def test_capture_records_budget_failure_even_when_client_returns_success():
    item = region()
    clock = Clock()

    def capture(*args, **kwargs):
        clock.value += 5.1
        return FrameSample(item.id, 1, clock.time(), np.zeros((50, 60, 3)), item.rect)

    result = validation.capture_sample(SimpleNamespace(capture=capture, last_trace=None),
                                       item, clock=clock)
    assert not result["ok"]
    assert result["error_code"] == "capture_budget_exceeded"


def test_raw_error_text_is_not_exposed():
    def fail(*args, **kwargs):
        raise ValueError("PRIVATE CHAT webhook secret")

    result = validation.capture_sample(SimpleNamespace(capture=fail, last_trace=None),
                                       region(), clock=Clock())
    assert result["error_code"] == "capture_exception"
    assert "PRIVATE" not in json.dumps(result)


def test_resources_have_explicit_safe_allowlist():
    client = SimpleNamespace(worker_stats=lambda: {
        "state": "ready", "active_processes": 2, "process_starts": 2,
        "webhook": "PRIVATE", "processes": [{"pid": 8, "private_bytes": 1024,
                                              "commandline": "PRIVATE"}],
    })
    result = validation.resource_snapshot(client)
    assert result["active_processes"] == 2
    assert result["processes"] == [{"pid": 8, "private_bytes": 1024}]
    assert "PRIVATE" not in json.dumps(result)


def test_failed_capture_keeps_safe_cold_start_timing():
    def fail(*args, **kwargs):
        raise TimeoutError("PRIVATE TEXT")

    trace = SimpleNamespace(
        request_id="abc123", stage_failed="wgc_start_requested",
        as_fields=lambda: {
            "request_id": "abc123", "stage_failed": "wgc_start_requested",
            "total_ms": 4500.0, "queue_wait_ms": 1700.0, "worker_id": 77,
            "popen_ms": float("nan"), "wait_ms": True, "exit_code": -9,
            "orphan_suspected": True, "text": "PRIVATE TEXT",
            "stages": {"request_received": 0, "wgc_construct_started": 2.0,
                       "wgc_start_requested": 3.0, "PRIVATE_LABEL": 9,
                       "first_valid_frame": float("inf"), "session_exit": True},
        },
    )
    result = validation.capture_sample(SimpleNamespace(capture=fail, last_trace=trace),
                                       region(), clock=Clock())
    assert not result["ok"]
    assert result["trace"]["queue_wait_ms"] == 1700
    assert result["trace"]["worker_id"] == 77
    assert result["trace"]["exit_code"] == -9
    assert result["trace"]["stage_offsets_ms"] == {
        "request_received": 0, "wgc_construct_started": 2.0, "wgc_start_requested": 3.0,
    }
    assert "popen_ms" not in result["trace"]
    assert "wait_ms" not in result["trace"]
    assert "PRIVATE" not in json.dumps(result)


def test_soak_covers_eight_hours_not_959_intervals():
    count = validation.round_count("soak", 10, 8, 30)
    assert count == 961
    assert (count - 1) * 30 == 8 * 3600
    with pytest.raises(validation.ValidationFailure):
        validation.round_count("soak", 10, 7.9, 30)


def test_fixed_deadlines_expose_overload():
    assert validation.round_deadline(100, 2, 30, 145) == (15, 0)
    assert validation.round_deadline(100, 2, 30, 164) == (0, 4000)


def test_atomic_checkpoint_and_output_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, "ROOT", tmp_path)
    target = validation.output_path(tmp_path / "build" / "test.json")
    validation.checkpoint(target, {"completed_rounds": 1, "passed": False})
    validation.checkpoint(target, {"completed_rounds": 2, "passed": False})
    assert json.loads(target.read_text())["completed_rounds"] == 2
    assert not target.with_suffix(".json.tmp").exists()
    with pytest.raises(validation.ValidationFailure):
        validation.output_path(tmp_path / "outside.json")


def test_defaults_do_not_start_capture_and_keep_production_cadence():
    args = validation.build_parser().parse_args([])
    assert args.mode == "audit"
    assert args.rounds == 10
    assert args.interval_seconds == 30
    assert args.hours == 8
    assert args.expected_windows == 5


def test_startup_failure_retains_cost_without_attempting_a_capture():
    clock = Clock()
    actions = []

    def wait_ready(timeout):
        actions.append(("wait_ready", timeout))
        clock.value += timeout
        return False

    result = validation.prepare_candidate(SimpleNamespace(
        prepare=lambda: actions.append("prepare"), wait_ready=wait_ready,
    ), clock=clock)
    assert actions == ["prepare", ("wait_ready", 5.0)]
    assert result == {"startup_ready": False, "startup_timeout_ms": 5000,
                      "startup_elapsed_ms": 5000, "error_code": "candidate_startup_not_ready"}


@pytest.mark.parametrize("startup_ready", [True, False])
def test_ab_end_to_end_is_bounded_alternating_and_never_claims_full_acceptance(
    tmp_path, monkeypatch, startup_ready,
):
    from deal_watch import capture, config, diagnostics, window_capture

    clock = Clock()
    clock.sleep = lambda seconds: setattr(clock, "value", clock.value + seconds)
    monkeypatch.setattr(validation, "time", clock)
    monkeypatch.setattr(validation, "ROOT", tmp_path)
    for name in ("tools/validate_capture_025.py", "src/deal_watch/window_capture.py",
                 "src/deal_watch/capture_worker.py", "src/deal_watch/capture_pool.py"):
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("fixture", encoding="utf-8")
    component = tmp_path / "baseline.exe"
    component.write_bytes(b"fixture only, not executable")
    regions = [region() for _ in range(5)]
    for index, item in enumerate(regions):
        item.window_hwnd = index + 1
    windows = SimpleNamespace(window_from_hwnd=lambda hwnd: window_for(regions[hwnd - 1]))
    monkeypatch.setattr(capture, "WindowsWindows", lambda: windows)
    monkeypatch.setattr(config, "ConfigStore", lambda: SimpleNamespace(
        load=lambda: SimpleNamespace(regions=regions)))
    diagnostic_paths = []
    monkeypatch.setattr(diagnostics, "configure_capture_stage_log", diagnostic_paths.append)
    for name in ("require_windows", "enable_dpi_awareness", "ensure_subprocess_pythonpath"):
        monkeypatch.setattr(validation.real_env, name, lambda: None)
    monkeypatch.setattr(validation.real_env, "monitor_rect", lambda: {
        "left": 0, "top": 0, "width": 1000, "height": 1000})
    monkeypatch.setattr(validation.real_env, "monitor_device", lambda hwnd: r"\\.\DISPLAY1")
    monkeypatch.setattr(validation.real_env, "visible_rect", lambda hwnd: Rect(0, 0, 100, 100))
    clients = []

    class Client:
        def __init__(self, **kwargs):
            self.persistent = kwargs["persistent"]
            self.closed = False
            assert all(client.closed for client in clients)
            clients.append(self)

        def prepare(self):
            assert self.persistent

        def wait_ready(self, timeout):
            assert timeout == 5.0
            clock.value += .2
            return startup_ready

        def worker_stats(self):
            return {"state": "ready", "active_processes": 2 if self.persistent else 0,
                    "process_starts": 2 if self.persistent else 5, "processes": []}

    class Capturer:
        def __init__(self, **kwargs):
            self.sequence = 0

        def capture(self, item, force):
            self.sequence += 1
            clock.value += .01
            return FrameSample(item.id, self.sequence, clock.time(),
                               np.zeros((50, 60, 3)), item.rect)

    monkeypatch.setattr(window_capture, "WindowCaptureClient", Client)
    monkeypatch.setattr(window_capture, "WindowFrameCapturer", Capturer)
    monkeypatch.setattr(validation, "close_client", lambda client: setattr(client, "closed", True))
    output = tmp_path / "build/ab.json"
    code = validation.main(["--mode", "ab", "--baseline-component", str(component),
                            "--output", str(output)])
    report = json.loads(output.read_text(encoding="utf-8"))
    if not startup_ready:
        assert code == 2
        assert not report["passed"]
        assert report["error_code"] == "candidate_startup_not_ready"
        assert report["failures"] == {"candidate_startup_not_ready": 1}
        assert report["completed_rounds"] == 0
        assert report["startup_attempts"][0]["startup_elapsed_ms"] == 200
        assert report["startup_attempts"][0]["startup_ready"] is False
        rows = [json.loads(line) for line in output.with_suffix(".requests.jsonl").read_text().splitlines()]
        assert len(rows) == 5
        assert all(row["implementation"] == "baseline" for row in rows)
        assert all(client.closed for client in clients)
        return
    assert code == 0
    assert report["passed"]
    assert report["full_acceptance_passed"] is False
    assert report["completed_rounds"] == 10
    assert diagnostic_paths == [output.parent / "capture-diagnostics"]
    assert report["rounds"][0]["order"] == ["baseline", "candidate"]
    assert report["rounds"][1]["order"] == ["candidate", "baseline"]
    assert len(clients) == 20 and all(client.closed for client in clients)
    rows = [json.loads(line) for line in output.with_suffix(".requests.jsonl").read_text().splitlines()]
    assert len(rows) == 100
    first_candidate_rows = [row for row in rows
                            if row["implementation"] == "candidate" and row["cold_request"]]
    assert len(first_candidate_rows) == 10
    assert all(row["startup_elapsed_ms"] == 200 for row in first_candidate_rows)
    assert all(row["first_capture_total_ms"] == 210 for row in first_candidate_rows)
    assert all(row["startup_ready"] for row in first_candidate_rows)
    assert len(report["startup_attempts"]) == 10
    assert report["steady_timings"]["candidate"]["count"] == 40
    assert "PRIVATE" not in output.read_text(encoding="utf-8")
    assert "PRIVATE" not in output.with_suffix(".requests.jsonl").read_text()
