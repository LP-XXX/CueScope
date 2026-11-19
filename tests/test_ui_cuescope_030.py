"""CueScope integration regressions; only synthetic windows and local storage."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from PySide6.QtWidgets import QApplication, QDialog

from deal_watch.capture import WindowInfo
from deal_watch.models import MessageCandidate, MessageDecision, OcrObservation, Rect, TextBlock
from deal_watch.rule_config import RuleCondition, RuleGroup
from deal_watch.rules import RuleEngine
from deal_watch.storage import Storage
from deal_watch.ui import main as main_ui
from deal_watch.ui import region as region_ui
from deal_watch.ui.rule_workspace import RuleWorkspace


@pytest.fixture
def app():
    value = QApplication.instance() or QApplication([])
    value.setQuitOnLastWindowClosed(False)
    yield value
    value.processEvents()


def dispose(widget, app):
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_manual_send_rechecks_current_rules_instead_of_trusting_old_preview(tmp_path):
    storage = Storage(tmp_path / "manual.sqlite")
    region = SimpleNamespace(id="r", alias="Synthetic", rule_group_id="errors", dedupe_scope="task", dedupe_group="")
    old_group = RuleGroup("Errors", [RuleCondition("failed")], id="errors")
    current_group = RuleGroup("Errors", [RuleCondition("failed")], exclusions=[RuleCondition("failed")], id="errors")
    message = MessageCandidate("r", "failed", Rect(0, 0, 120, 30), 1, 1)
    try:
        old = RuleEngine(storage, [], regions=[region], rule_groups=[old_group])
        old_preview = old.decide(message)
        assert old_preview.should_notify
        current = RuleEngine(storage, [], regions=[region], rule_groups=[current_group], delivery_mode="local")
        host = SimpleNamespace(_decision_rules=lambda: current, refresh_records=Mock())
        assert main_ui.MainWindow.manual_send(host, region, [message], {message.tracking_id: old_preview}) == 0
        assert storage.list_events() == []
        # A stale non-match cannot suppress a current match either.
        current.configure_regions([region], [old_group])
        old_nonmatch = MessageDecision(message.tracking_id, should_notify=False)
        assert main_ui.MainWindow.manual_send(host, region, [message], {message.tracking_id: old_nonmatch}) == 1
        assert storage.queue_counts().get("pending", 0) == 0
    finally:
        storage.close()


@pytest.mark.parametrize("flag", ["_settings_busy", "_saving", "_shutdown_started"])
def test_manual_send_is_rejected_while_configuration_changes(flag):
    host = SimpleNamespace(_decision_rules=Mock(side_effect=AssertionError("rules accessed during save")))
    setattr(host, flag, True)
    assert main_ui.MainWindow.manual_send(host, SimpleNamespace(alias="Synthetic"), []) == 0
    host._decision_rules.assert_not_called()


@pytest.mark.parametrize("transition", ["stop", "disable"])
def test_new_capture_incident_after_user_stop_or_disable_is_not_deduplicated(tmp_path, transition):
    from deal_watch.health import IncidentTracker
    from deal_watch.models import HealthSnapshot, MatchEvent, RegionStatus

    storage = Storage(tmp_path / "incidents.sqlite")
    region = SimpleNamespace(id="r", alias="Synthetic")
    host = SimpleNamespace(
        service=object(), _shutdown_started=False, region_rows={"r": 0},
        config=SimpleNamespace(regions=[region]), active_faults={},
        capture_incidents=IncidentTracker(), health_states={}, health_details={}, health_snapshots={},
        refresh_tray_tooltip=Mock(), _render_region_health=Mock(), tray=Mock(),
        _stopping_service=None, _saving=False, rule_workspace=Mock(), start_button=Mock(), stop_button=Mock(),
        configuration_buttons=[], _load_regions=Mock(), statusBar=lambda: Mock(), save_status=Mock(),
        _pending_after_stop=None,
    )

    def persist(keyword, text, *, event_id, route_region_id):
        assert route_region_id == "r"
        return storage.record_match(MatchEvent(
            "system", keyword, keyword, text, event_id, 1.0,
            id=event_id, delivery_mode="local",
        ), 300, now=1.0, idempotent=True)

    host.queue_system_message = persist
    try:
        main_ui.MainWindow.on_health(host, HealthSnapshot("r", RegionStatus.ERROR, "synthetic failure"))
        if transition == "stop":
            main_ui.MainWindow._on_monitor_stopped(host, None)
        else:
            main_ui.MainWindow.on_health(host, HealthSnapshot("r", RegionStatus.DISABLED))
        host.service = object()
        main_ui.MainWindow.on_health(host, HealthSnapshot("r", RegionStatus.ERROR, "synthetic failure"))
        records = storage.list_events(event_kind="system")
        assert len(records) == 2
        assert len({record["id"] for record in records}) == 2
        assert all(record["keyword"] == "监控故障" for record in records)
    finally:
        storage.close()


def test_preview_does_not_open_with_an_inflight_rule_save(monkeypatch):
    dialog = Mock()
    info = Mock()
    monkeypatch.setattr(main_ui, "PreviewDialog", dialog)
    monkeypatch.setattr(main_ui.QMessageBox, "information", info)
    host = SimpleNamespace(_shutdown_started=False, _saving=False, _settings_busy=True,
                           selected_region=Mock(side_effect=AssertionError("stale selection read")))
    main_ui.MainWindow.preview_region(host)
    dialog.assert_not_called()
    host.selected_region.assert_not_called()
    info.assert_called_once()


@pytest.mark.parametrize("flag", ["service", "_stopping_service", "_settings_busy", "_saving", "_shutdown_started"])
def test_rule_save_rejection_releases_editor_busy_state(app, flag):
    group = RuleGroup("Synthetic", [RuleCondition("failed")])
    host = SimpleNamespace(config=SimpleNamespace(rule_groups=[group], regions=[]),
                           service=None, _stopping_service=None, _settings_busy=False,
                           _saving=False, _shutdown_started=False,
                           statusBar=lambda: SimpleNamespace(showMessage=Mock()))
    setattr(host, flag, True)
    host.save_rule_groups = lambda groups: main_ui.MainWindow.save_rule_groups(host, groups)
    widget = RuleWorkspace(host)
    try:
        widget.condition_text.setPlainText("updated draft")
        widget.save_button.click()
        assert widget.editor.isEnabled()
        assert widget.save_button.isEnabled()
        assert not widget._busy and not widget._saving
        assert widget.modified
        assert widget.condition_text.toPlainText() == "updated draft"
    finally:
        dispose(widget, app)


class SyntheticCapture(QObject):
    completed = Signal(object, object, int, int, str)
    instances = []

    def __init__(self, hwnd, backend, parent=None):
        super().__init__(parent)
        self.running = False
        self.instances.append(self)

    def start(self):
        self.running = True

    def isRunning(self):  # noqa: N802
        return self.running

    def deliver(self, window):
        self.running = False
        self.completed.emit(window, np.full((240, 320, 3), 245, np.uint8), 320, 240, "")


class SyntheticPreview(QObject):
    completed = Signal(object, object, str)
    instances = []

    def __init__(self, region, model, base_dir, frame):
        super().__init__()
        self.region, self.frame = region, frame
        self.running = False
        self.instances.append(self)

    def start(self):
        self.running = True

    def isRunning(self):  # noqa: N802
        return self.running

    def deliver(self):
        self.running = False
        observation = OcrObservation(self.region.id, 1, 1,
                                     [TextBlock("Synthetic content", 0.99, Rect(10, 10, 140, 28))],
                                     content_mode=self.region.content_mode)
        self.completed.emit(region_ui.array_to_qimage(self.frame.image), observation, "")


@pytest.fixture
def wizard(app, monkeypatch):
    windows = [WindowInfo(1001, "Document A", "SyntheticClass", Rect(100, 100, 320, 240),
                          process_id=17, process_path="C:/Synthetic/App.exe", process_start_id="creation-1", dpi=144)]
    provider = SimpleNamespace(list_windows=lambda: windows,
                               window_from_hwnd=lambda hwnd: next((item for item in windows if item.hwnd == hwnd), None))
    monkeypatch.setattr(region_ui, "WindowsWindows", lambda: provider)
    monkeypatch.setattr(region_ui, "SelectionCaptureWorker", SyntheticCapture)
    monkeypatch.setattr(region_ui, "PreviewWorker", SyntheticPreview)
    SyntheticCapture.instances = []
    SyntheticPreview.instances = []
    dialog = region_ui.RegionEditDialog()
    dialog.alias.setText("Synthetic task")
    dialog.show()
    app.processEvents()
    yield app, dialog, windows
    for worker in SyntheticCapture.instances + SyntheticPreview.instances:
        worker.running = False
    dialog.reject()
    dispose(dialog, app)


def select_roi(app, dialog, windows):
    dialog.choose_region()
    SyntheticCapture.instances[-1].deliver(windows[0])
    app.processEvents()
    dialog.selector.set_widget_selection(dialog.selector._target().adjusted(5, 5, -5, -5))
    app.processEvents()
    assert dialog.selected_rect is not None


def preview(app, dialog):
    dialog.start_preview()
    SyntheticPreview.instances[-1].deliver()
    app.processEvents()


def test_follow_window_accepts_title_changes_during_capture_and_preview(wizard):
    app, dialog, windows = wizard
    dialog.task_options.title_policy.setCurrentIndex(dialog.task_options.title_policy.findData("window"))
    dialog.choose_region()
    windows[0] = replace(windows[0], title="Document B")
    SyntheticCapture.instances[-1].deliver(windows[0])
    app.processEvents()
    dialog.selector.set_widget_selection(dialog.selector._target())
    dialog.start_preview()
    windows[0] = replace(windows[0], title="Document C")
    SyntheticPreview.instances[-1].deliver()
    app.processEvents()
    assert dialog.ok_button.isEnabled()
    dialog.ok_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.region().previewed
    assert dialog.region().title_policy == "window"


@pytest.mark.parametrize("changes", [
    {"process_id": 18}, {"process_path": "C:/Other/App.exe"}, {"process_path": ""},
    {"process_start_id": "creation-2"}, {"dpi": 192},
    {"rect": Rect(100, 100, 360, 240)}, {"class_name": "OtherClass"},
])
def test_follow_window_never_reuses_confirmation_after_identity_or_geometry_change(wizard, changes):
    app, dialog, windows = wizard
    dialog.task_options.title_policy.setCurrentIndex(dialog.task_options.title_policy.findData("window"))
    select_roi(app, dialog, windows)
    preview(app, dialog)
    assert dialog.ok_button.isEnabled()
    windows[0] = replace(windows[0], **changes)
    dialog.ok_button.click()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert not dialog._preview_verified
    assert not dialog.region().previewed
    assert dialog.selection_problems()


def test_strict_title_rejects_a_changed_title_before_confirmation(wizard):
    app, dialog, windows = wizard
    select_roi(app, dialog, windows)
    preview(app, dialog)
    windows[0] = replace(windows[0], title="Another document")
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert not dialog._preview_verified


def test_title_policy_change_invalidates_capture_and_late_result(wizard):
    app, dialog, windows = wizard
    dialog.choose_region()
    worker = SyntheticCapture.instances[-1]
    generation = dialog._selection_generation
    dialog.task_options.title_policy.setCurrentIndex(dialog.task_options.title_policy.findData("window"))
    assert dialog._selection_generation > generation
    worker.deliver(windows[0])
    app.processEvents()
    assert dialog._frame_image is None
    assert dialog.selected_rect is None
    assert dialog._selection is None
    assert not dialog.ok_button.isEnabled()


def test_title_policy_change_discards_previously_confirmable_preview(wizard):
    app, dialog, windows = wizard
    select_roi(app, dialog, windows)
    preview(app, dialog)
    assert dialog._preview_verified
    dialog.task_options.title_policy.setCurrentIndex(dialog.task_options.title_policy.findData("window"))
    assert dialog._frame_image is None
    assert dialog.selected_rect is None
    assert not dialog._preview_verified
    assert not dialog.ok_button.isEnabled()


def test_content_mode_change_keeps_roi_draft_but_rejects_late_preview(wizard):
    app, dialog, windows = wizard
    select_roi(app, dialog, windows)
    roi = dialog.selected_rect
    generation = dialog._selection_generation
    dialog.start_preview()
    old = SyntheticPreview.instances[-1]
    dialog.task_options.mode.setCurrentIndex(dialog.task_options.mode.findData("text_block"))
    old.deliver()
    app.processEvents()
    assert dialog.selected_rect == roi
    assert dialog._selection_generation == generation
    assert not dialog._preview_verified
    assert not dialog.ok_button.isEnabled()
