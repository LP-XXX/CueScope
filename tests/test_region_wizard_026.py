"""The region wizard uses one modal lifetime and explicit, current OCR evidence."""

from __future__ import annotations

import os
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QObject, QPoint, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from deal_watch.capture import WindowInfo
from deal_watch.models import OcrObservation, Rect, TextBlock
from deal_watch.ui import region as ui


class CaptureWorker(QObject):
    completed = Signal(object, object, int, int, str)
    instances = []

    def __init__(self, hwnd, backend, parent=None):
        super().__init__(parent)
        self.hwnd, self.backend = hwnd, backend
        self.running = False
        self.instances.append(self)

    def start(self):
        self.running = True

    def isRunning(self):  # noqa: N802
        return self.running

    def deliver(self, window, error=""):
        self.running = False
        self.completed.emit(window, np.full((240, 320, 3), 245, np.uint8), 320, 240, error)


class PreviewWorker(QObject):
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

    def deliver(self, *, text=True, error=""):
        self.running = False
        blocks = [TextBlock("合成文字", .99, Rect(10, 10, 90, 30), message_id="m1")] if text else []
        observation = OcrObservation(self.region.id, self.frame.captured_at, 1, blocks, inference_ms=12)
        self.completed.emit(ui.array_to_qimage(self.frame.image), observation, error)


@pytest.fixture
def wizard(monkeypatch):
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    windows = [WindowInfo(1001, "Synthetic A", "Qt", Rect(100, 100, 320, 240),
                          123, "C:/Weixin.exe"),
               WindowInfo(1002, "Synthetic B", "Qt", Rect(500, 100, 320, 240),
                          123, "C:/Weixin.exe")]
    provider = SimpleNamespace(list_windows=lambda: windows,
                               window_from_hwnd=lambda hwnd: next((w for w in windows if w.hwnd == hwnd), None))
    monkeypatch.setattr(ui, "WindowsWindows", lambda: provider)
    monkeypatch.setattr(ui, "SelectionCaptureWorker", CaptureWorker)
    monkeypatch.setattr(ui, "PreviewWorker", PreviewWorker)
    CaptureWorker.instances = []
    PreviewWorker.instances = []
    dialog = ui.RegionEditDialog()
    dialog.alias.setText("合成区域")
    dialog.show()
    app.processEvents()
    yield app, dialog, windows
    dialog.reject()
    for worker in CaptureWorker.instances + PreviewWorker.instances:
        worker.running = False
    dialog.deleteLater()
    app.processEvents()


def select_roi(app, dialog, windows):
    dialog.choose_region()
    CaptureWorker.instances[-1].deliver(windows[0])
    app.processEvents()
    assert dialog.steps.currentIndex() == 1
    target = dialog.selector._target()
    dialog.selector.set_widget_selection(target.adjusted(5, 5, -5, -5))
    app.processEvents()
    assert dialog.selected_rect is not None


def preview(app, dialog, *, text=True, error=""):
    dialog.start_preview()
    PreviewWorker.instances[-1].deliver(text=text, error=error)
    app.processEvents()


def test_selection_does_not_auto_confirm_but_can_save_pending(wizard):
    app, dialog, windows = wizard
    select_roi(app, dialog, windows)
    assert dialog.pending_button.isEnabled()
    assert not dialog.ok_button.isEnabled()
    dialog.accept_pending()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert not dialog.region().previewed


def test_explicit_confirmation_saves_exact_previewed_draft(wizard):
    app, dialog, windows = wizard
    select_roi(app, dialog, windows)
    preview(app, dialog)
    worker = PreviewWorker.instances[-1]
    assert worker.frame.rect == dialog.selected_rect
    assert worker.frame.image.shape[:2] == (dialog.selected_rect.height, dialog.selected_rect.width)
    assert np.all(worker.frame.image == 245), "selection overlay must not alter OCR pixels"
    assert not dialog.region().previewed
    assert dialog.ok_button.isEnabled()
    dialog.ok_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.region().previewed
    assert dialog.region().id == worker.region.id


@pytest.mark.parametrize("text,error", [(False, ""), (True, "synthetic OCR failure")])
def test_failed_or_empty_preview_cannot_confirm(wizard, text, error):
    app, dialog, windows = wizard
    select_roi(app, dialog, windows)
    preview(app, dialog, text=text, error=error)
    assert not dialog.ok_button.isEnabled()
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.pending_button.isEnabled()


def test_back_then_reselect_discards_late_ocr_result(wizard):
    app, dialog, windows = wizard
    select_roi(app, dialog, windows)
    dialog.start_preview()
    old = PreviewWorker.instances[-1]
    dialog.go_back()
    assert dialog.steps.currentIndex() == 1
    old.deliver()
    app.processEvents()
    assert not dialog.ok_button.isEnabled()
    assert not dialog._preview_verified
    assert dialog.steps.currentIndex() == 1


def test_backend_change_clears_every_calibration_and_confirmation_field(wizard):
    app, dialog, windows = wizard
    select_roi(app, dialog, windows)
    preview(app, dialog)
    dialog.backend_combo.setCurrentIndex(1)
    assert dialog.selected_rect is None
    assert dialog.selected_frame_size is None
    assert dialog.selected_window is None
    assert dialog._frame_image is None
    assert not dialog.ok_button.isEnabled()
    assert not dialog.pending_button.isEnabled()


def test_resize_during_preview_cannot_confirm_stale_frame(wizard):
    app, dialog, windows = wizard
    select_roi(app, dialog, windows)
    dialog.start_preview()
    windows[0] = replace(windows[0], rect=Rect(100, 100, 360, 240))
    PreviewWorker.instances[-1].deliver()
    assert not dialog.ok_button.isEnabled()
    assert dialog.selection_problems()


def test_window_move_keeps_frame_coordinates_and_preview_valid(wizard):
    app, dialog, windows = wizard
    select_roi(app, dialog, windows)
    roi = dialog.selected_rect
    windows[0] = replace(windows[0], rect=Rect(160, 160, 320, 240))
    preview(app, dialog)
    assert dialog.selected_rect == roi
    assert dialog.ok_button.isEnabled()


def test_close_cancels_capture_without_showing_a_late_result(wizard):
    app, dialog, windows = wizard
    dialog.choose_region()
    worker = CaptureWorker.instances[-1]
    dialog.reject()
    worker.deliver(windows[0])
    app.processEvents()
    assert not dialog.isVisible()
    assert dialog.selector is None
    assert dialog.selected_rect is None


def test_same_outer_exec_survives_capture_and_inline_selection(wizard):
    app, dialog, windows = wizard
    dialog.hide()  # Production opens a fresh hidden dialog with exec().
    seen = []
    def complete():
        dialog.choose_region()
        CaptureWorker.instances[-1].deliver(windows[0])
        app.processEvents()
        seen.append(app.activeModalWidget() is dialog)
        dialog.selector.set_widget_selection(dialog.selector._target())
        dialog.pending_button.click()
    QTimer.singleShot(0, complete)
    assert dialog.exec() == QDialog.DialogCode.Accepted
    assert seen == [True]


def test_confirmation_revalidates_binding_at_final_click(wizard):
    app, dialog, windows = wizard
    select_roi(app, dialog, windows)
    preview(app, dialog)
    windows[0] = replace(windows[0], process_id=999)
    dialog.ok_button.click()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert not dialog.region().previewed


def test_preview_uses_only_decide_and_never_sends_or_enqueues(wizard):
    app, dialog, windows = wizard
    decide = Mock(return_value=SimpleNamespace(matched_keywords=["合成"]))
    process = Mock()
    dialog.rules = SimpleNamespace(decide=decide, process=process, configure_regions=Mock(),
        explain=Mock(return_value=SimpleNamespace(should_notify=True, matched_labels=["合成"], excluded_by=[])))
    select_roi(app, dialog, windows)
    preview(app, dialog)
    assert decide.called
    process.assert_not_called()


def test_wizard_fits_minimum_window_and_keeps_required_controls(wizard):
    app, dialog, windows = wizard
    dialog.resize(700, 480)
    select_roi(app, dialog, windows)
    assert dialog.minimumSizeHint().width() <= 760
    for button in (dialog.pending_button, dialog.preview_button, dialog.cancel_button, dialog.back_button):
        assert dialog.rect().contains(button.mapTo(dialog, QPoint(0, 0)))
        assert dialog.rect().contains(button.mapTo(dialog, button.rect().bottomRight()))
        assert button.isVisible()


def test_previews_have_a_nonblocking_single_worker_slot(monkeypatch):
    frame = SimpleNamespace()
    engine = Mock()
    monkeypatch.setattr(ui, "LocalOcr", engine)
    assert ui._PREVIEW_SLOT.acquire(blocking=False)
    try:
        # The wizard fixture does not replace the real worker in this independent test.
        worker = ui.PreviewWorker(SimpleNamespace(), SimpleNamespace(), None, frame)
        outputs = []
        worker.completed.connect(lambda *args: outputs.append(args))
        started = time.monotonic()
        worker.run()
        assert time.monotonic() - started < .2
        assert outputs and "稍后重试" in outputs[0][2]
        engine.assert_not_called()
    finally:
        ui._PREVIEW_SLOT.release()


def test_saved_region_preview_close_ignores_a_late_result(wizard):
    app, edit, windows = wizard
    select_roi(app, edit, windows)
    region = edit.region()
    frame = SimpleNamespace(region_id=region.id, captured_at=time.time(),
                            image=np.zeros((80, 80, 3), dtype=np.uint8), rect=region.rect)
    saved = ui.PreviewDialog(region, edit.model, edit.base_dir, [], Mock(), frame=frame)
    saved.show()
    worker = saved.worker
    assert worker.isRunning()
    saved.reject()
    worker.deliver()
    app.processEvents()
    assert not saved.isVisible()
    assert not saved.confirm_button.isEnabled()
    assert not region.previewed
    saved.deleteLater()


def test_refresh_reloads_windows_and_invalidates_old_evidence(wizard):
    app, dialog, windows = wizard
    select_roi(app, dialog, windows)
    windows.append(replace(windows[0], hwnd=1003, title="New synthetic window"))
    dialog.refresh_windows_button.click()
    assert dialog.window_combo.count() == 3
    assert dialog.selected_frame_size is None
    assert not dialog.pending_button.isEnabled()


def test_resizing_the_wizard_preserves_roi_and_confirmed_preview(wizard):
    app, dialog, windows = wizard
    select_roi(app, dialog, windows)
    preview(app, dialog)
    roi, evidence = dialog.selected_rect, dialog._preview_evidence
    dialog.resize(760, 520)
    app.processEvents()
    assert dialog.selected_rect == roi
    assert dialog._preview_evidence == evidence
    assert dialog.ok_button.isEnabled()


def test_each_step_keeps_primary_action_beside_the_same_cancel_button(wizard):
    app, dialog, windows = wizard
    dialog.resize(760, 520)
    app.processEvents()
    cancel_right = dialog.cancel_button.mapTo(dialog, dialog.cancel_button.rect().topRight()).x()

    def assert_footer(primary):
        app.processEvents()
        visible = [b for b in (dialog.choose_button, dialog.preview_button, dialog.ok_button)
                   if b.isVisible()]
        assert visible == [primary]
        assert primary.isDefault()
        assert dialog.cancel_button.isVisible()
        primary_y = primary.mapTo(dialog, primary.rect().center()).y()
        cancel_y = dialog.cancel_button.mapTo(dialog, dialog.cancel_button.rect().center()).y()
        assert abs(primary_y - cancel_y) <= 2
        assert dialog.cancel_button.mapTo(dialog, dialog.cancel_button.rect().topRight()).x() == cancel_right

    assert_footer(dialog.choose_button)
    select_roi(app, dialog, windows)
    assert_footer(dialog.preview_button)
    preview(app, dialog, error="synthetic retry")
    assert_footer(dialog.preview_button)
    preview(app, dialog)
    assert_footer(dialog.ok_button)


def test_app_shutdown_cancels_dialog_and_waits_for_detached_ocr_off_ui_thread(wizard, monkeypatch):
    import threading
    app, _, windows = wizard
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    class BlockingPreview(QThread):
        completed = Signal(object, object, str)
        def __init__(self, region, model, base_dir, frame):
            super().__init__()
            self.region, self.frame = region, frame
        def run(self):
            entered.set()
            release.wait(3)
            observation = OcrObservation(self.region.id, self.frame.captured_at, 1,
                [TextBlock("Late synthetic text", .99, Rect(1, 1, 50, 20))])
            self.completed.emit(ui.array_to_qimage(self.frame.image), observation, "")
    monkeypatch.setattr(ui, "PreviewWorker", BlockingPreview)
    parent = QWidget()
    dialog = ui.RegionEditDialog(parent)
    dialog.alias.setText("Shutdown fixture")
    dialog.show()
    app.processEvents()
    waiter = None
    try:
        select_roi(app, dialog, windows)
        dialog.start_preview()
        assert entered.wait(1)
        workers = ui.close_region_dialogs(parent)
        assert len(workers) == 1
        assert not dialog.isVisible()
        def wait():
            ui.wait_ui_workers(workers)
            finished.set()
        waiter = threading.Thread(target=wait)
        waiter.start()
        assert not finished.wait(.05)
        release.set()
        assert finished.wait(1)
        app.processEvents()
        assert not dialog._preview_verified
        assert not dialog.ok_button.isEnabled()
    finally:
        release.set()
        if waiter is not None:
            waiter.join(2)
        parent.deleteLater()
        app.processEvents()
