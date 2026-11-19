"""Appearance changes preserve drafts/state and never touch monitoring configuration."""

from __future__ import annotations

import json
import threading
import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QPlainTextEdit, QTableWidget, QTableWidgetItem, QWidget

from deal_watch.ui import preferences
from deal_watch.ui.icons import _DRAWINGS, icon
from deal_watch.ui.preferences import PreferenceWriter, load_theme, save_theme
from deal_watch.ui.theme import ThemeManager, ThemeMode, colors_for


@pytest.fixture
def appearance(tmp_path):
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    before = app.palette(), QFont(app.font()), app.styleSheet()
    managers = []

    def create(folder=tmp_path, singleton=False):
        manager = ThemeManager.for_app(app, folder) if singleton else ThemeManager(app, folder)
        if manager not in managers:
            managers.append(manager)
        return manager

    yield app, create
    for manager in managers:
        assert manager.close(2)
        manager.deleteLater()
    if getattr(app, "_deal_watch_theme_manager", None) in managers:
        app._deal_watch_theme_manager = None
    app.processEvents()
    app.setPalette(before[0])
    app.setFont(before[1])
    app.setStyleSheet(before[2])


@pytest.mark.parametrize("content", [None, b"not json", b"[]", b'{"theme":"unknown"}',
                                     b'{"theme":true}', b"x" * 16385])
def test_missing_or_invalid_preferences_fall_back_without_modifying_core(tmp_path, content):
    core = tmp_path / "config.json"
    core.write_bytes(b'{"version":3,"regions":[]}')
    path = tmp_path / "ui_preferences.json"
    if content is not None:
        path.write_bytes(content)
    assert load_theme(path) == "system"
    assert core.read_bytes() == b'{"version":3,"regions":[]}'


def test_preference_write_is_independent_and_round_trips(tmp_path):
    core = tmp_path / "config.json"
    core.write_bytes(b"unchanged monitoring configuration")
    path = tmp_path / "ui_preferences.json"
    save_theme(path, "dark")
    assert load_theme(path) == "dark"
    assert json.loads(path.read_text()) == {"version": 1, "theme": "dark"}
    assert core.read_bytes() == b"unchanged monitoring configuration"
    assert list(tmp_path.glob("*.tmp")) == []


def test_live_theme_change_preserves_widgets_and_unsaved_input(appearance):
    app, create = appearance
    manager = create()
    parent = QWidget()
    edit = QPlainTextEdit(parent)
    edit.setPlainText("未保存的关键词\n手机优惠")
    cursor = edit.textCursor()
    cursor.setPosition(5)
    edit.setTextCursor(cursor)
    table = QTableWidget(3, 1, parent)
    for row in range(3):
        table.setItem(row, 0, QTableWidgetItem(str(row)))
    table.setCurrentCell(1, 0)
    for mode in ("dark", "light", "dark"):
        manager.set_mode(mode)
        app.processEvents()
        assert edit.toPlainText() == "未保存的关键词\n手机优惠"
        assert edit.textCursor().position() == 5
        assert table.currentRow() == 1
        assert edit.parent() is parent
        assert app.palette().color(QPalette.ColorRole.Window).name() == colors_for(mode)["background"].lower()
    assert manager.wait_saved(2)
    assert load_theme(manager.path) == "dark"
    parent.deleteLater()


def test_system_changes_apply_only_in_follow_system_mode(appearance, monkeypatch):
    _, create = appearance
    manager = create()
    monkeypatch.setattr(manager, "_system_theme", lambda: "dark")
    manager.set_mode(ThemeMode.SYSTEM)
    assert manager.effective_theme == "dark"
    manager.set_mode(ThemeMode.LIGHT)
    manager._hints.colorSchemeChanged.emit(Qt.ColorScheme.Dark)
    assert manager.effective_theme == "light"
    manager.set_mode(ThemeMode.SYSTEM)
    monkeypatch.setattr(manager, "_system_theme", lambda: "light")
    manager._hints.colorSchemeChanged.emit(Qt.ColorScheme.Light)
    assert manager.effective_theme == "light"


def test_failed_atomic_save_keeps_previous_preference_and_current_visual(appearance, monkeypatch):
    app, create = appearance
    manager = create()
    save_theme(manager.path, "light")
    errors = []
    manager.save_failed.connect(errors.append)
    def fail_replace(*_args):
        raise PermissionError("synthetic denied write")
    monkeypatch.setattr(preferences.os, "replace", fail_replace)
    manager.set_mode("dark")
    assert manager.wait_saved(2)
    deadline = time.monotonic() + 1
    while not errors and time.monotonic() < deadline:
        app.processEvents()
    assert errors and "未保存" in errors[0]
    assert manager.effective_theme == "dark"
    assert load_theme(manager.path) == "light"
    assert list(manager.path.parent.glob("*.tmp")) == []


def test_burst_of_changes_coalesces_to_latest_preference(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    writes, errors = [], []
    def save(_path, mode):
        writes.append(mode)
        if len(writes) == 1:
            entered.set()
            assert release.wait(2)
    monkeypatch.setattr(preferences, "save_theme", save)
    writer = PreferenceWriter(tmp_path / "ui_preferences.json", errors.append)
    try:
        writer.submit("light")
        assert entered.wait(1)
        for index in range(100):
            writer.submit("light" if index % 2 else "system")
        writer.submit("dark")
        release.set()
        assert writer.wait_idle(2)
        assert writes == ["light", "dark"]
        assert not errors
    finally:
        release.set()
        assert writer.close(2)


def test_singleton_is_reused_only_for_same_open_profile(appearance, tmp_path):
    _, create = appearance
    first = create(tmp_path / "one", singleton=True)
    assert create(tmp_path / "one", singleton=True) is first
    first.set_mode("dark")
    second = create(tmp_path / "two", singleton=True)
    assert second is not first and first._closed
    assert load_theme(first.path) == "dark"
    assert second.mode is ThemeMode.SYSTEM
    assert not second.path.exists()
    second.close()
    third = create(tmp_path / "two", singleton=True)
    assert third is not second


def _luminance(color):
    channels = QColor(color).getRgbF()[:3]
    linear = [x / 12.92 if x <= .04045 else ((x + .055) / 1.055) ** 2.4 for x in channels]
    return sum(a * b for a, b in zip(linear, (.2126, .7152, .0722), strict=True))


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_reading_and_action_color_pairs_meet_normal_text_contrast(mode):
    colors = colors_for(mode)
    for text, background in (("text", "surface"), ("text", "background"),
                             ("secondary", "surface"), ("secondary", "background"),
                             ("on_accent", "accent"), ("success", "success_background"),
                             ("warning", "warning_background"), ("danger", "danger_background")):
        light, dark = sorted((_luminance(colors[text]), _luminance(colors[background])), reverse=True)
        assert (light + .05) / (dark + .05) >= 4.5, (mode, text, background)


def test_all_local_icons_render_in_both_themes(appearance):
    _, create = appearance
    manager = create()
    for mode in ("light", "dark"):
        manager.set_mode(mode)
        for name in _DRAWINGS:
            result = icon(name)
            assert not result.isNull(), (mode, name)
            assert not result.pixmap(20, 20).isNull()


def test_native_caption_does_not_request_handles_in_offscreen_mode(monkeypatch):
    from deal_watch.ui import theme
    class NoHandle:
        def isWindow(self):
            raise AssertionError("No native operation is allowed for offscreen widgets")
    monkeypatch.setattr(theme.sys, "platform", "win32")
    assert not theme.apply_native_caption(NoHandle(), True, platform_name="offscreen")


def test_native_caption_requires_existing_handle_and_falls_back_to_older_attribute(monkeypatch):
    import ctypes
    from types import SimpleNamespace

    from deal_watch.ui import theme
    calls = []
    def set_attribute(_hwnd, attribute, _value, _size):
        calls.append(attribute)
        return -1 if attribute == 20 else 0
    monkeypatch.setattr(theme.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args: SimpleNamespace(DwmSetWindowAttribute=set_attribute))
    no_native = SimpleNamespace(isWindow=lambda: True, internalWinId=lambda: 0)
    assert not theme.apply_native_caption(no_native, True, platform_name="windows")
    assert not calls
    native = SimpleNamespace(isWindow=lambda: True, internalWinId=lambda: 123)
    assert theme.apply_native_caption(native, True, platform_name="windows")
    assert calls == [20, 19]


def test_native_caption_refresh_is_deferred_coalesced_and_cancelled_on_close(appearance, monkeypatch):
    from deal_watch.ui import theme
    app, create = appearance
    manager = create()
    widget = QWidget()
    calls = []
    monkeypatch.setattr(theme.sys, "platform", "win32")
    monkeypatch.setattr(app, "platformName", lambda: "windows")
    monkeypatch.setattr(app, "topLevelWidgets", lambda: [widget])
    monkeypatch.setattr(theme, "apply_native_caption", lambda target, dark: calls.append((target, dark)))
    manager.set_mode("dark")
    manager._schedule_caption_refresh()
    manager._schedule_caption_refresh()
    assert not calls
    app.processEvents()
    assert calls == [(widget, True)]
    manager._schedule_caption_refresh()
    manager.close()
    app.processEvents()
    assert len(calls) == 1
    widget.deleteLater()
