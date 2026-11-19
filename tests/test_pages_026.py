"""Secondary-page interaction and size checks use synthetic data only."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QPlainTextEdit, QScrollArea

from deal_watch.ui.pages import (
    KeywordEditor,
    RecordDetailDialog,
    build_keywords_page,
    build_records_page,
    build_settings_page,
)
from deal_watch.ui.theme import APP_STYLE


class FakeTheme(QObject):
    changed = Signal(str)
    save_failed = Signal(str)

    def __init__(self):
        super().__init__()
        self.mode = "system"
        self.calls = []

    def set_mode(self, mode):
        self.mode = mode
        self.calls.append(mode)
        self.changed.emit("light" if mode == "system" else mode)


@pytest.fixture
def app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    application.setStyleSheet(APP_STYLE)
    yield application
    application.processEvents()


@pytest.fixture
def host(tmp_path, app):
    return SimpleNamespace(
        config=SimpleNamespace(keywords=["手机", "优惠券"], rule_groups=[], regions=[]),
        save_rule_groups=Mock(), _change_event_filter=Mock(),
        save_keywords=Mock(), save_settings=Mock(), send_test=Mock(), export_records=Mock(),
        show_record_detail=Mock(), _change_recognition_page=Mock(), _begin_recognition_prepare=Mock(),
        refresh_records=Mock(), refresh_recognition=Mock(),
        data_dir=tmp_path, config_store=SimpleNamespace(path=tmp_path / "config.json"),
        theme_manager=FakeTheme(),
    )


def dispose(widget, app):
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_keyword_entries_preserve_order_counts_and_explicit_dirty_save(host, app):
    page = build_keywords_page(host)
    editor = host.keywords_edit
    assert editor.toPlainText() == "手机\n优惠券"
    assert not host.keywords_save_button.isEnabled()
    editor.input.setText("  免单  ")
    editor.add_button.click()
    assert editor.values() == ["手机", "优惠券", "免单"]
    assert editor.isModified()
    assert host.keywords_dirty_label.text() == "未保存"
    editor.add_text("免单\n AA \n\nAA\nA A\n")
    assert editor.values() == ["手机", "优惠券", "免单", "AA", "A A"]
    host.keywords_save_button.click()
    host.save_keywords.assert_called_once()
    # UI cannot declare a failed/ongoing transaction saved on its own.
    assert editor.isModified()
    editor.setModified(False)
    assert not host.keywords_save_button.isEnabled()
    assert host.keywords_dirty_label.text() == ""
    dispose(page, app)


def test_keyword_multiline_paste_is_split_without_overwriting_unsubmitted_input(app):
    editor = KeywordEditor()
    editor.setPlainText("原有")
    editor.input.setText("尚未提交")
    editor.input.paste_lines.emit("甲\n乙\n甲")
    assert editor.values() == ["原有", "甲", "乙"]
    assert editor.input.text() == "尚未提交"
    assert editor.isModified()
    dispose(editor, app)


def test_keyword_item_edit_deduplicates_and_delete_is_selection_scoped(app):
    editor = KeywordEditor()
    editor.setPlainText("甲\n乙\n丙")
    editor.items.item(1).setText("  甲  ")
    assert editor.values() == ["甲", "丙"]
    editor.items.item(1).setSelected(True)
    QTest.keyClick(editor.items, Qt.Key.Key_Delete)
    assert editor.values() == ["甲"]
    assert not editor.delete_button.isEnabled()
    dispose(editor, app)


def test_keyword_keyboard_edit_commits_a_single_draft_change(app):
    editor = KeywordEditor()
    editor.setPlainText("original\nsecond")
    editor.show()
    editor.items.setCurrentRow(0)
    editor.items.setFocus()
    QTest.keyClick(editor.items, Qt.Key.Key_F2)
    app.processEvents()
    field = editor.items.findChild(QLineEdit)
    assert field is not None
    field.setText("  changed  ")
    QTest.keyClick(field, Qt.Key.Key_Return)
    app.processEvents()
    assert editor.values() == ["changed", "second"]
    assert editor.isModified()
    dispose(editor, app)


def test_batch_editor_only_applies_accepted_draft(monkeypatch, app):
    editor = KeywordEditor()
    editor.setPlainText("已有")
    def reject(dialog):
        dialog.findChild(QPlainTextEdit).setPlainText("不应保存")
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(QDialog, "exec", reject)
    editor.edit_batch()
    assert editor.values() == ["已有"]
    def accept(dialog):
        dialog.findChild(QPlainTextEdit).setPlainText("甲\n甲\n  乙 ")
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, "exec", accept)
    editor.edit_batch()
    assert editor.values() == ["甲", "乙"]
    assert editor.isModified()
    dispose(editor, app)


def test_records_default_to_notifications_and_detail_keyboard_uses_current_kind(host, app):
    from PySide6.QtWidgets import QTableWidgetItem
    page = build_records_page(host)
    assert host.records_tabs.currentIndex() == 0
    assert host.records_tabs.tabText(1) == "全部识别"
    assert host.record_table.columnCount() == host.candidate_table.columnCount() == 5
    host.record_table.setRowCount(1)
    host.record_table.setItem(0, 0, QTableWidgetItem("synthetic"))
    host.record_table.selectRow(0)
    QTest.keyClick(host.record_table, Qt.Key.Key_Return)
    host.show_record_detail.assert_called_once_with("notifications")
    host.records_tabs.setCurrentIndex(1)
    host.recognition_next.click()
    host._change_recognition_page.assert_called_once_with(1)
    dispose(page, app)


def test_records_refit_columns_when_vertical_scrollbar_appears(host, app):
    page = build_records_page(host)
    page.resize(620, 440)
    page.show()
    app.processEvents()
    host.record_table.setRowCount(200)
    app.processEvents()
    assert host.record_table.verticalScrollBar().maximum() > 0
    assert host.record_table.horizontalHeader().length() <= host.record_table.viewport().width()
    assert host.record_table.horizontalScrollBar().maximum() == 0
    dispose(page, app)


def test_record_read_retries_are_hidden_until_failure_and_use_query_callbacks(host, app):
    page = build_records_page(host)
    assert host.records_retry_button.isHidden()
    assert host.recognition_query_retry.isHidden()
    host.records_retry_button.show()
    host.records_retry_button.click()
    host.refresh_records.assert_called_once_with(force=True)
    host.recognition_query_retry.show()
    host.recognition_query_retry.click()
    host.refresh_recognition.assert_called_once_with(force=True)
    host._begin_recognition_prepare.assert_not_called()
    dispose(page, app)


def test_record_detail_keeps_full_text_and_copies_only_on_explicit_click(app, monkeypatch):
    copied = []
    monkeypatch.setattr(QApplication, "clipboard", lambda: SimpleNamespace(setText=copied.append))
    message = "完整正文" * 100
    dialog = RecordDetailDialog("通知详情", [("正文", message), ("事件编号", "synthetic-event")])
    assert message in dialog.body.toPlainText()
    assert copied == []
    dialog.copy_button.click()
    assert copied == [message]
    dispose(dialog, app)


def test_settings_keep_credentials_masked_and_actions_explicit(host, app):
    page = build_settings_page(host)
    assert host.webhook_edit.echoMode() == QLineEdit.EchoMode.Password
    assert host.secret_edit.echoMode() == QLineEdit.EchoMode.Password
    assert (host.poll_seconds.minimum(), host.poll_seconds.maximum()) == (10, 300)
    assert host.record_limit.minimum() == 1
    host.theme_mode_combo.setCurrentIndex(2)
    assert host.theme_manager.calls == ["dark"]
    host.save_settings.assert_not_called()
    host.send_test.assert_not_called()
    host.test_button.click()
    host.send_test.assert_called_once()
    host.settings_save_button.click()
    host.save_settings.assert_called_once()
    dispose(page, app)


def test_theme_binding_does_not_retain_destroyed_controls(host, app, capsys):
    page = build_settings_page(host)
    dispose(page, app)
    host.theme_manager.changed.emit("dark")
    host.theme_manager.save_failed.emit("synthetic save error")
    app.processEvents()
    assert "RuntimeError" not in capsys.readouterr().err


@pytest.mark.parametrize("builder", [build_keywords_page, build_records_page, build_settings_page])
def test_secondary_pages_fit_narrow_content_without_horizontal_scroll(host, app, builder):
    page = builder(host)
    page.resize(620, 440)
    page.show()
    app.processEvents()
    assert page.minimumSizeHint().width() <= 620
    for scroll in page.findChildren(QScrollArea):
        assert scroll.horizontalScrollBar().maximum() == 0
    if builder is build_settings_page:
        assert host.settings_save_button.mapTo(page, host.settings_save_button.rect().bottomRight()).y() < page.height()
    dispose(page, app)
