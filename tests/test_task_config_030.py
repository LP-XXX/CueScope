"""Task configuration, safe bindings and scheduling without real window access."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from deal_watch.capture import CaptureError
from deal_watch.config import CONFIG_VERSION, AppConfig, ConfigSaveError, ConfigStore
from deal_watch.models import CaptureErrorCode, Rect, RegionConfig
from deal_watch.readiness import region_identity_problems
from deal_watch.rule_config import RuleCondition, RuleGroup
from deal_watch.service import MonitorService
from deal_watch.storage import Storage
from deal_watch.window_capture import WindowFrameCapturer
from deal_watch.window_identity import canonical_process_path, identity_issues
from region_fixtures import ready_region


def old_config():
    return {
        "version": 3, "keywords": ["连接失败"], "match_mode": "char_multiset",
        "poll_interval_ms": 30000, "dedupe_seconds": 300,
        "regions": [{
            "id": "legacy", "alias": "Synthetic", "window_title": "Synthetic",
            "rect": {"x": 0, "y": 0, "width": 120, "height": 80},
            "window_hwnd": 1001, "process_id": 17, "process_path": "C:/Synthetic/app.exe",
            "window_class": "SyntheticClass", "reference_frame_width": 400,
            "reference_frame_height": 300, "coordinate_space": "frame", "previewed": True,
        }],
    }


def test_v3_migration_preserves_preview_rules_binding_and_original_bytes(tmp_path):
    store = ConfigStore(tmp_path)
    original = json.dumps(old_config(), ensure_ascii=False, indent=3).encode("utf-8") + b"\r\n"
    store.path.write_bytes(original)
    migrated = store.load()
    assert store.path.read_bytes() == original  # Loading is read-only.
    assert migrated.version == CONFIG_VERSION == 4
    region = migrated.regions[0]
    assert region.previewed and region.window_hwnd == 1001 and region.process_id == 17
    assert region.content_mode == "wechat_chat" and region.title_policy == "strict"
    assert region.rule_group_id == "" and migrated.rule_groups == []
    assert migrated.match_mode == "char_multiset" and migrated.keywords == ["连接失败"]
    assert region.dedupe_scope == "global" and migrated.dedupe_seconds == 300
    assert region.poll_interval_ms == 0
    store.save(migrated, verify=True)
    backup = tmp_path / "config.pre-v4.json"
    assert backup.read_bytes() == original
    migrated.keywords.append("异常")
    store.save(migrated, verify=True)
    assert backup.read_bytes() == original
    assert store.load().keywords == ["连接失败", "异常"]


def test_migration_cannot_trust_an_existing_incomplete_backup(tmp_path):
    store = ConfigStore(tmp_path)
    original = json.dumps(old_config()).encode()
    store.path.write_bytes(original)
    (tmp_path / "config.pre-v4.json").write_bytes(b'{"version":')
    with pytest.raises(ConfigSaveError):
        store.save(store.load(), verify=True)
    assert store.path.read_bytes() == original


def test_v4_roundtrip_preserves_task_and_rule_settings(tmp_path):
    rule = RuleGroup("Errors", [RuleCondition("ERROR", case_sensitive=True)], id="errors")
    region = ready_region(
        "Synthetic", content_mode="generic_line", title_policy="window", process_start_id="creation-123",
        reference_dpi=144, poll_interval_ms=10000, rule_group_id=rule.id,
        dedupe_scope="group", dedupe_group="same-signal",
    )
    original = AppConfig(regions=[region], rule_groups=[rule], notification_mode="local")
    store = ConfigStore(tmp_path)
    store.save(original, verify=True)
    restored = store.load()
    assert restored.regions[0] == region
    assert restored.rule_groups == [rule]
    assert restored.notification_mode == "local"


@pytest.mark.parametrize("interval", [True, -1, 9999, 300001, 10000.5, "10000"])
def test_task_interval_rejects_invalid_range_or_type(interval):
    with pytest.raises(ValueError):
        RegionConfig("Synthetic", "Synthetic", Rect(0, 0, 80, 80), poll_interval_ms=interval)


@pytest.mark.parametrize("interval", [0, 10000, 30000, 300000])
def test_valid_task_interval_is_preserved(interval):
    item = RegionConfig("Synthetic", "Synthetic", Rect(0, 0, 80, 80), poll_interval_ms=interval)
    assert item.poll_interval_ms == interval


def identity_pair(**changes):
    region = ready_region("Synthetic", window_title="Document A", window_class="TextWindow",
                          process_id=17, process_path="C:/Synthetic/App.exe", process_start_id="creation-1",
                          reference_dpi=144, title_policy="strict")
    window = SimpleNamespace(title="Document A", class_name="TextWindow", process_id=17,
                             process_path="C:\\Synthetic\\App.exe", process_start_id="creation-1",
                             dpi=144, minimized=False)
    for key, value in changes.items():
        setattr(window, key, value)
    lookup = SimpleNamespace(window_from_hwnd=lambda _hwnd: window, desktop_locked=lambda: False)
    return region, window, lookup


def test_strict_title_and_follow_window_have_distinct_explicit_contracts():
    region, window, lookup = identity_pair(title="Document B")
    assert identity_issues(region, window)
    assert region_identity_problems(region, lookup)
    capturer = WindowFrameCapturer(client=SimpleNamespace(), windows=lookup)
    with pytest.raises(CaptureError):
        capturer.validate(region)
    region.title_policy = "window"
    assert identity_issues(region, window) == []
    assert region_identity_problems(region, lookup) == []
    assert capturer.validate(region) is window


@pytest.mark.parametrize(("changes", "code"), [
    ({"process_id": 18}, CaptureErrorCode.PROCESS_MISMATCH),
    ({"process_start_id": "creation-2"}, CaptureErrorCode.PROCESS_MISMATCH),
    ({"process_start_id": ""}, CaptureErrorCode.PROCESS_MISMATCH),
    ({"process_path": ""}, CaptureErrorCode.PROCESS_MISMATCH),
    ({"process_path": "C:/Other/app.exe"}, CaptureErrorCode.PROCESS_MISMATCH),
    ({"class_name": "OtherWindow"}, CaptureErrorCode.NEEDS_CALIBRATION),
    ({"dpi": 192}, CaptureErrorCode.NEEDS_CALIBRATION),
])
def test_follow_window_never_ignores_process_identity_or_dpi_failure(changes, code):
    region, window, lookup = identity_pair(**changes)
    region.title_policy = "window"
    problems = identity_issues(region, window)
    assert problems[0][0] == code
    assert region_identity_problems(region, lookup) == [message for _, message in problems]
    with pytest.raises(CaptureError) as raised:
        WindowFrameCapturer(client=SimpleNamespace(), windows=lookup).validate(region)
    assert raised.value.code == code


def test_windows_path_case_and_separators_do_not_force_rebinding():
    region, window, lookup = identity_pair(process_path="c:/SYNTHETIC/./APP.EXE")
    assert canonical_process_path(region.process_path) == canonical_process_path(window.process_path)
    assert region_identity_problems(region, lookup) == []


class NoDesktopCapturer:
    def reset(self, _region_id):
        pass

    def capture(self, _region, force=False):
        raise AssertionError("test must explicitly replace the synthetic capturer")


def make_service(tmp_path, regions, **kwargs):
    storage = Storage(tmp_path / "scheduler.sqlite")
    service = MonitorService(
        AppConfig(regions=regions, save_hit_images=False, **kwargs), storage,
        SimpleNamespace(recognize=lambda _frame: None), capturer=NoDesktopCapturer(),
    )
    return service, storage


def test_default_interval_update_keeps_task_overrides_and_updates_due_time(tmp_path):
    fast = ready_region("fast", poll_interval_ms=10000)
    inherited = ready_region("default", poll_interval_ms=0)
    service, storage = make_service(tmp_path, [fast, inherited])
    try:
        service._runtime[fast.id].last_capture_start = 100.0
        service._runtime[inherited.id].last_capture_start = 100.0
        service.update_poll_interval(60000)
        assert service._next_due[fast.id] == 110.0
        assert service._next_due[inherited.id] == 160.0
        assert service._task_interval(fast.id) == 10000
        assert service._task_interval(inherited.id) == 60000
        assert service._ocr_context(fast, fast.id, 1).content_mode == fast.content_mode
    finally:
        storage.close()


def test_scheduler_uses_each_task_interval_without_wall_clock_waits(tmp_path, monkeypatch):
    import deal_watch.service as module

    fast = ready_region("fast", poll_interval_ms=10000)
    slow = ready_region("slow", poll_interval_ms=30000)
    service, storage = make_service(tmp_path, [fast, slow])
    clock = [100.0]
    calls = []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(service._condition, "wait", lambda timeout=None: clock.__setitem__(0, clock[0] + (timeout or 0.01)))
    service._next_due = {fast.id: 100.0, slow.id: 100.0}

    def capture(region, force=False):
        calls.append((region.id, clock[0]))
        if len(calls) >= 6:
            service._stop.set()
        return None

    service.capturer.capture = capture
    try:
        service._capture_loop()
        fast_times = [at for region_id, at in calls if region_id == fast.id]
        slow_times = [at for region_id, at in calls if region_id == slow.id]
        assert fast_times == [100.0, 110.0, 120.0, 130.0]
        assert slow_times == [100.0, 130.0]
        assert service._pending == {}
        assert service._busy_generation == {}
    finally:
        service._stop.set()
        storage.close()
