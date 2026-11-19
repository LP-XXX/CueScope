"""Rule-editor user flows with synthetic data and an asynchronous save owner."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from deal_watch.rule_config import RuleCondition, RuleGroup
from deal_watch.ui.rule_workspace import RuleWorkspace
from deal_watch.ui.theme import palette_for, stylesheet_for


@pytest.fixture
def app():
    value = QApplication.instance() or QApplication([])
    value.setQuitOnLastWindowClosed(False)
    value.setPalette(palette_for("light"))
    value.setStyleSheet(stylesheet_for("light"))
    yield value
    value.processEvents()


@pytest.fixture
def workspace(app):
    groups = [
        RuleGroup(name="失败提醒", id="errors", conditions=[RuleCondition("failed")]),
        RuleGroup(name="完成提醒", id="done", conditions=[RuleCondition("done", mode="exact")]),
    ]
    host = SimpleNamespace(config=SimpleNamespace(rule_groups=groups, regions=[]), save_rule_groups=Mock(return_value=None))
    widget = RuleWorkspace(host)
    yield widget, host
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_drafts_survive_group_changes_and_same_config_refresh(workspace):
    widget, host = workspace
    widget.condition_text.setPlainText("fatal")
    widget.group_combo.setCurrentIndex(1)
    widget.name_edit.setText("完成后的通知")
    widget.group_combo.setCurrentIndex(0)
    assert widget.condition_text.toPlainText() == "fatal"
    widget.set_groups(host.config.rule_groups)
    assert widget.condition_text.toPlainText() == "fatal"
    widget.group_combo.setCurrentIndex(1)
    assert widget.name_edit.text() == "完成后的通知"
    assert widget.modified
    assert host.config.rule_groups[0].conditions[0].text == "failed"


def test_invalid_group_cannot_be_saved_and_valid_save_is_explicit(workspace):
    widget, host = workspace
    widget.condition_text.setPlainText("")
    widget.save_button.click()
    host.save_rule_groups.assert_not_called()
    assert "条件文本" in widget.status_label.text()
    widget.condition_text.setPlainText("fatal")
    widget.save_button.click()
    saved = host.save_rule_groups.call_args.args[0]
    assert saved[0].conditions[0].text == "fatal"
    assert not widget.save_button.isEnabled()
    assert not widget.editor.isEnabled()
    assert widget.modified  # An in-flight transaction is not declared saved.
    widget.show_save_result(False)
    assert widget.save_button.isEnabled()
    assert widget.condition_text.toPlainText() == "fatal"
    assert widget.modified
    widget.save_button.click()
    widget.show_save_result(True)
    assert not widget.modified
    assert widget.condition_text.toPlainText() == "fatal"


def test_referenced_rule_group_cannot_be_deleted(workspace):
    widget, host = workspace
    host.config.regions = [SimpleNamespace(rule_group_id="errors")]
    widget.delete_button.click()
    assert widget.group_combo.count() == 2
    assert "更换规则组" in widget.status_label.text()
    widget.group_combo.setCurrentIndex(1)
    widget.delete_button.click()
    assert widget.group_combo.count() == 1
    assert widget.group_combo.currentData() == "errors"


def test_exclusion_preview_and_character_mode_are_real_controls(workspace):
    widget, _ = workspace
    widget.sample_edit.setPlainText("test failed")
    widget._preview()
    assert "条件满足" in widget.preview_status.text()
    widget.exclude_button.click()
    widget.condition_text.setPlainText("test")
    widget._preview()
    assert widget.preview_status.text() == "已排除：test"
    assert "排除" in widget.preview_details.toPlainText()
    widget.mode_combo.setCurrentIndex(widget.mode_combo.findData("char_multiset"))
    assert not widget.whitespace_combo.isEnabled()
    widget.condition_text.setPlainText("TSET")
    widget._preview()
    assert widget.preview_status.text() == "已排除：TSET"
    widget.case_sensitive.setChecked(True)
    widget._preview()
    assert "条件满足" in widget.preview_status.text()


def test_any_all_only_affects_the_selected_group(workspace):
    widget, host = workspace
    widget.add_button.click()
    widget.condition_text.setPlainText("retry")
    widget.sample_edit.setPlainText("failed")
    widget.combination_combo.setCurrentIndex(widget.combination_combo.findData("all"))
    widget._preview()
    assert widget.preview_status.text() == "条件未满足"
    widget.sample_edit.setPlainText("failed retry")
    widget._preview()
    assert "条件满足" in widget.preview_status.text()
    assert host.config.rule_groups[0].combination == "any"


def test_theme_and_narrow_layout_preserve_editor_and_sample(workspace, app):
    widget, _ = workspace
    widget.resize(400, 520)
    widget.show()
    widget.condition_text.setPlainText("a long edited phrase " * 10)
    widget.sample_edit.setPlainText("synthetic example")
    app.processEvents()
    for theme in ("dark", "light"):
        app.setPalette(palette_for(theme))
        app.setStyleSheet(stylesheet_for(theme))
        app.processEvents()
        assert widget.width() == 400
        assert widget.minimumSizeHint().width() <= 400
        assert widget.condition_text.toPlainText() == "a long edited phrase " * 10
        assert widget.sample_edit.toPlainText() == "synthetic example"
        assert widget.scroll.verticalScrollBar().maximum() > 0


def test_new_group_is_a_draft_until_validated(workspace):
    widget, host = workspace
    widget.new_button.click()
    assert widget.table.rowCount() == 1
    assert widget.condition_text.toPlainText() == ""
    widget.save_button.click()
    host.save_rule_groups.assert_not_called()
    widget.name_edit.setText("连接状态")
    widget.condition_text.setPlainText("connection failed")
    widget.save_button.click()
    saved = host.save_rule_groups.call_args.args[0]
    assert saved[-1].name == "连接状态"
    assert saved[-1].conditions[0].text == "connection failed"


def test_busy_editor_does_not_mutate_or_start_another_save(workspace):
    widget, host = workspace
    widget.set_busy(True)
    widget._new_group()
    widget._save()
    assert widget.group_combo.count() == 2
    host.save_rule_groups.assert_not_called()
    widget.set_busy(False)
    assert widget.save_button.isEnabled()
