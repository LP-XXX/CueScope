"""User-facing task creation, routing independence, and record search on synthetic data."""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from deal_watch.config import AppConfig
from deal_watch.models import MatchEvent, MessageCandidate, Rect, RegionStatus
from deal_watch.recognition import RecognitionStore
from deal_watch.record_search import RecordSearch
from deal_watch.rule_config import RuleCondition, RuleGroup
from deal_watch.rules import RuleEngine
from deal_watch.service import MonitorService
from deal_watch.storage import Storage
from deal_watch.ui import main as ui
from deal_watch.ui.local_demo import LocalDemo
from deal_watch.ui.region import preview_unit_lines
from deal_watch.ui.task_options import TaskOptions
from region_fixtures import ready_region


def until(app, condition):
    end = time.monotonic() + 5
    while not condition() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)
    assert condition()


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    host = ui.MainWindow(Path(__file__).resolve().parents[1])
    until(app, lambda: host._recognition_ready)
    yield app, host
    host.exit_app()
    until(app, lambda: host._shutdown_done)
    host.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("accept", [False, True])
def test_task_and_inline_phrase_are_committed_together(window, monkeypatch, accept):
    app, host = window
    options = TaskOptions(host.config)
    options.quick_phrase.setText("任务完成")
    group = options.draft_rule()
    task = ready_region("Synthetic", rule_group_id=group.id)
    dialog = SimpleNamespace(task_options=options, region=lambda: task,
                             exec=lambda: QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected)
    monkeypatch.setattr(ui, "RegionEditDialog", lambda *args: dialog)
    host.add_region()
    until(app, lambda: not host._saving)
    for config in [host.config, host.config_store.load()]:
        assert len(config.rule_groups) == len(config.regions) == int(accept)
        if accept:
            assert config.regions[0].rule_group_id == config.rule_groups[0].id == group.id
            assert config.rule_groups[0].conditions[0].text == "任务完成"
    options.deleteLater()


def test_preview_includes_all_units_and_exclusions(tmp_path):
    store = Storage(tmp_path / "events.db")
    group = RuleGroup("done", [RuleCondition("完成")], exclusions=[RuleCondition("演练")])
    task = ready_region("demo", rule_group_id=group.id)
    rules = RuleEngine(store, [], regions=[task], rule_groups=[group])
    try:
        texts = ["等待", "普通文字", "任务完成", "演练完成", "另外一行"]
        units = [MessageCandidate(task.id, text, Rect(0, 0, 40, 20), 0, 1) for text in texts]
        result = preview_unit_lines(units, rules)
        assert all(text in result for text in texts)
        assert "排除条件 · 演练" in result and "命中 · 完成" in result and "未满足" in result
        assert store.list_events() == []
    finally:
        store.close()


def test_inline_rule_write_failure_and_retry_leave_no_orphan(window, monkeypatch):
    app, host = window
    options = TaskOptions(host.config)
    options.quick_phrase.setText("任务完成")
    task = ready_region("Synthetic", rule_group_id=options.draft_rule().id)
    dialog = SimpleNamespace(task_options=options, region=lambda: task, exec=lambda: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(ui, "RegionEditDialog", lambda *args: dialog)
    monkeypatch.setattr(ui.QMessageBox, "critical", lambda *args: None)
    real_save = host.config_store.save

    def fail(*args, **kwargs):
        raise OSError("synthetic disk error")

    monkeypatch.setattr(host.config_store, "save", fail)
    host.add_region()
    until(app, lambda: not host._saving)
    assert host.config.rule_groups == [] and host.config.regions == []
    monkeypatch.setattr(host.config_store, "save", real_save)
    host.retry_region_save()
    until(app, lambda: not host._saving)
    persisted = host.config_store.load()
    assert len(persisted.rule_groups) == len(persisted.regions) == 1
    assert persisted.regions[0].rule_group_id == persisted.rule_groups[0].id
    options.deleteLater()


def test_activity_search_filters_before_limit_and_counts(tmp_path):
    store = Storage(tmp_path / "events.db")
    history = RecognitionStore(tmp_path)
    now = time.time()
    try:
        for i, (alias, text, at) in enumerate([
            ("Alpha", "export 50%_成功", now), ("Alpha", "export 501成功", now),
            ("Beta", "export 50%_成功", now), ("Alpha", "export 50%_成功", now - 86500),
        ]):
            unit = MessageCandidate(str(i), text, Rect(0, 0, 40, 20), at, at)
            history.record_candidates(str(i), alias, at, [unit], {})
            store.record_match(MatchEvent(str(i), alias, "export", text, str(i), at, delivery_mode="local"))
        search = RecordSearch("ALP", "50%_", 24)
        assert [row["region_id"] for row in store.list_events(1, search=search)] == ["0"]
        assert [row["region_id"] for row in history.list_records(1, search=search)] == ["0"]
        assert history.count(search=search) == 1 and history.count() == 4
    finally:
        history.close()
        store.close()


def test_apply_single_rebind_preserves_unrelated_schedule(tmp_path):
    a, b = ready_region("A", hwnd=1), ready_region("B", hwnd=2)
    store = Storage(tmp_path / "events.db")
    config = AppConfig(regions=[a, b], keywords=["完成"])
    service = MonitorService(config, store, Mock(), capturer=SimpleNamespace(), data_dir=tmp_path)
    try:
        baseline = service._generation[b.id]
        service._next_due[b.id] = 200
        a = replace(a, enabled=False)
        config.regions[0] = a
        service.apply_saved_region(a)
        assert a.id not in service._regions
        assert b.id in service._regions and service._generation[b.id] == baseline
        a = replace(a, enabled=True, window_hwnd=3)
        config.regions[0] = a
        service.apply_saved_region(a)
        assert service._regions[a.id].window_hwnd == 3
        assert service._runtime[a.id].status == RegionStatus.BASELINING
        assert service._next_due[b.id] == 200 and service._generation[b.id] == baseline
    finally:
        store.close()


@pytest.mark.parametrize("accept", [False, True])
def test_live_editor_borrows_client_and_only_pauses_selected_task(window, monkeypatch, accept):
    app, host = window
    a, b = ready_region("A", hwnd=1), ready_region("B", hwnd=2)
    host.config.regions = [a, b]
    host.config_store.save(host.config)
    host._load_regions()
    host._select_region_row(a.id)
    client = SimpleNamespace(persistent=True)
    service = MonitorService(host.config, host.storage, Mock(), capturer=SimpleNamespace(client=client))
    b_generation = service._generation[b.id]
    host.service = service
    try:
        class Editor:
            def __init__(self, parent, region):
                assert region.id == a.id

            def exec(self):
                assert self.capture_client is client
                assert a.id not in service._regions and b.id in service._regions
                return QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected

            def region(self):
                return replace(a, window_hwnd=33)

        monkeypatch.setattr(ui, "RegionEditDialog", Editor)
        monkeypatch.setattr(host, "stop_monitoring", lambda: pytest.fail("stopped all tasks"))
        host.edit_region()
        until(app, lambda: not host._saving)
        assert service._regions[a.id].window_hwnd == (33 if accept else 1)
        assert service._runtime[a.id].status == RegionStatus.BASELINING
        assert service._generation[b.id] == b_generation
    finally:
        host.service = None


def test_demo_alert_uses_recognized_text_not_source_label():
    app = QApplication.instance() or QApplication([])
    demo = LocalDemo(Path("."))
    demo.target.setText("任务完成")
    demo.show_result(["等待中"], "")
    assert demo.alert.text() == "未触发提醒"
    demo.show_result(["DEMO-0001 任务完成"], "")
    assert demo.output.text() == "DEMO-0001 任务完成"
    assert demo.alert.property("matched")
    demo.close()
    app.processEvents()
