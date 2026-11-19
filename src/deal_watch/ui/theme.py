"""Shared light/dark Qt appearance; monitoring configuration stays independent."""
from __future__ import annotations

import sys
from enum import StrEnum
from pathlib import Path
from string import Template

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from .preferences import PreferenceWriter, load_theme


class ThemeMode(StrEnum):
    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"


LIGHT = {
    "background": "#F5F6F8", "surface": "#FFFFFF", "raised": "#FFFFFF",
    "text": "#18202C", "secondary": "#5C6675", "border": "#E1E5EB",
    "accent": "#28675D", "accent_hover": "#20564E", "accent_pressed": "#19473F",
    "on_accent": "#FFFFFF", "selected": "#E5F2ED", "hover": "#F0F3F8",
    "pressed": "#E4EAF3", "alternate": "#FAFBFD", "rail": "#EEF1F5",
    "disabled": "#747F8F", "disabled_background": "#E9EDF3",
    "success": "#137A46", "success_background": "#EAF6EE",
    "warning": "#925100", "warning_background": "#FFF5DF",
    "danger": "#B4232B", "danger_background": "#FCEDEF",
    "tooltip_background": "#18202C", "tooltip_text": "#FFFFFF", "scrollbar": "#B8C2D0",
}
DARK = {
    "background": "#12161D", "surface": "#1B222C", "raised": "#222B37",
    "text": "#E8EDF5", "secondary": "#A4AEBC", "border": "#323D4B",
    "accent": "#8ED4C2", "accent_hover": "#A8E0D2", "accent_pressed": "#70C3AD",
    "on_accent": "#102C25", "selected": "#24443B", "hover": "#26313F",
    "pressed": "#304057", "alternate": "#1E2732", "rail": "#171D26",
    "disabled": "#8290A2", "disabled_background": "#252E3B",
    "success": "#79D5A4", "success_background": "#203C30",
    "warning": "#F2C36B", "warning_background": "#3D3320",
    "danger": "#FF9BA5", "danger_background": "#442B33",
    "tooltip_background": "#E8EDF5", "tooltip_text": "#18202C", "scrollbar": "#526176",
}


def colors_for(theme: str | ThemeMode) -> dict[str, str]:
    return dict(DARK if str(theme) == "dark" else LIGHT)


def palette_for(theme: str | ThemeMode) -> QPalette:
    colors = colors_for(theme)
    palette = QPalette()
    roles = {
        "Window": "background", "WindowText": "text", "Base": "surface",
        "AlternateBase": "alternate", "Text": "text", "Button": "surface",
        "ButtonText": "text", "Highlight": "accent", "HighlightedText": "on_accent",
        "Link": "accent", "LinkVisited": "accent", "PlaceholderText": "secondary",
        "ToolTipBase": "tooltip_background", "ToolTipText": "tooltip_text",
        "BrightText": "danger", "Light": "raised", "Midlight": "hover",
        "Mid": "border", "Dark": "border", "Shadow": "background",
    }
    for role, token in roles.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(colors[token]))
    for name in ("Text", "WindowText", "ButtonText", "PlaceholderText"):
        palette.setColor(QPalette.ColorGroup.Disabled, getattr(QPalette.ColorRole, name),
                         QColor(colors["disabled"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button,
                     QColor(colors["disabled_background"]))
    return palette


_STYLE = Template("""
QWidget { font-family: 'Segoe UI', 'Microsoft YaHei UI'; font-size: 14px; color: $text; }
QMainWindow, QDialog, QWidget#appShell { background: $background; }
QLabel { background: transparent; }
QLabel#pageTitle { font-size: 22px; font-weight: 600; }
QLabel#brandTitle { font-size: 16px; font-weight: 600; }
QLabel#pageSubtitle, QLabel#muted { color: $secondary; font-size: 12px; }
QLabel#sectionTitle { font-size: 15px; font-weight: 600; }
QWidget#navigationRail { background: $rail; border-right: 1px solid $border; }
QFrame#surface, QWidget#surface, QFrame#sectionFrame { background: $surface; border: 1px solid $border; border-radius: 8px; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QPushButton, QToolButton { background: $surface; border: 1px solid $border; border-radius: 8px; padding: 7px 12px; min-height: 20px; }
QPushButton:hover, QToolButton:hover { background: $hover; border-color: $secondary; }
QPushButton:pressed, QToolButton:pressed { background: $pressed; }
QPushButton:focus, QToolButton:focus { border-color: $accent; }
QPushButton:disabled, QToolButton:disabled { color: $disabled; background: $disabled_background; border-color: $border; }
QPushButton#primary { background: $accent; color: $on_accent; border-color: $accent; font-weight: 600; }
QPushButton#primary:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton#primary:pressed { background: $accent_pressed; border-color: $accent_pressed; }
QPushButton#primary:disabled { background: $disabled_background; color: $disabled; border-color: $border; }
QPushButton#danger { color: $danger; }
QPushButton#danger:hover { background: $danger_background; border-color: $danger; }
QPushButton#danger:disabled { color: $disabled; }
QPushButton#textButton { background: transparent; border: 1px solid transparent; padding: 4px; min-height: 18px; color: $accent; }
QPushButton#textButton:hover { background: $selected; }
QPushButton#textButton:focus { border-color: $accent; }
QPushButton#textButton:disabled { color: $disabled; background: transparent; }
QPushButton#navButton, QToolButton#navButton { background: transparent; border: 1px solid transparent; text-align: left; padding: 9px 12px; color: $secondary; }
QPushButton#navButton:hover, QToolButton#navButton:hover { background: $hover; color: $text; }
QPushButton#navButton:checked, QToolButton#navButton:checked, QPushButton#navButton[active="true"], QToolButton#navButton[active="true"] { background: $selected; color: $accent; font-weight: 600; }
QPushButton#navButton:focus, QToolButton#navButton:focus { border-color: $accent; }
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: $surface; border: 1px solid $border; border-radius: 8px; padding: 7px 10px; selection-background-color: $accent; selection-color: $on_accent; }
QLineEdit:hover, QPlainTextEdit:hover, QTextEdit:hover, QSpinBox:hover, QComboBox:hover { border-color: $secondary; }
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: $accent; }
QLineEdit:disabled, QPlainTextEdit:disabled, QSpinBox:disabled, QComboBox:disabled { background: $disabled_background; color: $disabled; }
QComboBox { padding-right: 26px; min-height: 20px; }
QComboBox::drop-down { width: 24px; border: none; }
QComboBox QAbstractItemView { background: $raised; color: $text; border: 1px solid $border; selection-background-color: $selected; selection-color: $text; outline: none; }
QSpinBox { min-height: 20px; padding-right: 22px; }
QSpinBox::up-button, QSpinBox::down-button { width: 20px; background: $hover; border: none; }
QCheckBox, QRadioButton { spacing: 8px; padding: 3px 0; }
QCheckBox:disabled, QRadioButton:disabled { color: $disabled; }
QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; }
QCheckBox:focus, QRadioButton:focus { color: $accent; }
QTableView, QTableWidget, QListView, QListWidget { background: $surface; alternate-background-color: $alternate; border: 1px solid $border; border-radius: 8px; gridline-color: $border; selection-background-color: $selected; selection-color: $text; outline: none; }
QTableView::item, QTableWidget::item { padding: 6px 8px; border: none; }
QTableView::item:hover, QTableWidget::item:hover, QListView::item:hover { background: $hover; }
QTableView::item:selected, QTableWidget::item:selected, QListView::item:selected { background: $selected; color: $text; }
QTableView::item:focus, QTableWidget::item:focus { border: 1px solid $accent; }
QHeaderView::section { background: $surface; color: $secondary; border: none; border-bottom: 1px solid $border; padding: 10px 8px; font-size: 12px; font-weight: 600; }
QTableCornerButton::section { background: $surface; border: none; }
QTabWidget::pane { background: transparent; border: none; }
QTabBar { background: transparent; }
QTabBar::tab { background: transparent; color: $secondary; border: 1px solid transparent; border-radius: 6px; padding: 7px 14px; margin: 2px; }
QTabBar::tab:hover { background: $hover; color: $text; }
QTabBar::tab:selected { background: $selected; color: $accent; font-weight: 600; }
QTabBar::tab:focus { border-color: $accent; }
QGroupBox { border: 1px solid $border; border-radius: 8px; margin-top: 12px; padding: 16px; background: $surface; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; font-weight: 600; }
QLabel#statusBadge { background: $selected; color: $accent; border-radius: 6px; padding: 4px 8px; font-size: 12px; }
QLabel#statusBadge[tone="success"] { color: $success; background: $success_background; }
QLabel#statusBadge[tone="warning"], QLabel#warning { color: $warning; background: $warning_background; border-radius: 6px; padding: 6px 10px; }
QLabel#statusBadge[tone="danger"] { color: $danger; background: $danger_background; }
QLabel#statusBadge[tone="neutral"] { color: $secondary; background: $hover; }
QMenu { background: $raised; color: $text; border: 1px solid $border; padding: 4px; }
QMenu::item { padding: 8px 24px; border-radius: 4px; }
QMenu::item:selected { background: $selected; }
QMenu::item:disabled { color: $disabled; }
QMenu::separator { height: 1px; background: $border; margin: 4px 8px; }
QToolTip { background: $tooltip_background; color: $tooltip_text; border: none; padding: 6px 8px; }
QStatusBar { background: $background; color: $secondary; border: none; font-size: 12px; }
QStatusBar::item { border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle { background: $scrollbar; border-radius: 4px; min-width: 24px; min-height: 24px; }
QScrollBar::handle:hover { background: $secondary; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QProgressBar { background: $hover; border: none; border-radius: 4px; text-align: center; }
QProgressBar::chunk { background: $accent; border-radius: 4px; }
""")


def stylesheet_for(theme: str | ThemeMode) -> str:
    return _STYLE.substitute(colors_for(theme))


APP_STYLE = stylesheet_for(ThemeMode.LIGHT)


def apply_native_caption(widget: QWidget, dark: bool, *, platform_name: str | None = None) -> bool:
    """Theme only existing app HWNDs; never create a handle or change Windows settings."""
    platform = QApplication.platformName() if platform_name is None else platform_name
    if sys.platform != "win32" or platform != "windows" or not widget.isWindow():
        return False
    hwnd = int(widget.internalWinId())
    if not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        setter = ctypes.WinDLL("dwmapi").DwmSetWindowAttribute
        setter.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        setter.restype = ctypes.c_long
        value = ctypes.c_int(bool(dark))
        for attribute in (20, 19):
            if setter(wintypes.HWND(hwnd), attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                return True
    except (OSError, AttributeError, ValueError):
        pass
    return False


class ThemeManager(QObject):
    changed = Signal(str)
    save_failed = Signal(str)

    def __init__(self, app: QApplication, data_dir: Path):
        super().__init__(app)
        self.app = app
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "ui_preferences.json"
        self.mode = ThemeMode(load_theme(self.path))
        self.effective_theme = "light"
        self._closed = False
        self._refreshing_captions = False
        self._caption_timer = QTimer(self)
        self._caption_timer.setSingleShot(True)
        self._caption_timer.setInterval(0)
        self._caption_timer.timeout.connect(self._refresh_native_captions)
        self._writer = PreferenceWriter(self.path, self._save_error)
        self._hints = app.styleHints()
        self._hints.colorSchemeChanged.connect(self._system_changed)
        app.aboutToQuit.connect(self.close)
        app.installEventFilter(self)
        font = QFont()
        font.setFamilies(["Segoe UI", "Microsoft YaHei UI"])
        font.setPixelSize(14)
        app.setFont(font)
        self.apply()

    @classmethod
    def for_app(cls, app: QApplication, data_dir: Path) -> ThemeManager:
        existing = getattr(app, "_deal_watch_theme_manager", None)
        if existing is not None and not existing._closed and existing.data_dir == Path(data_dir):
            return existing
        if existing is not None:
            existing.close()
            try:
                existing.deleteLater()
            except RuntimeError:
                pass
        manager = cls(app, data_dir)
        app._deal_watch_theme_manager = manager
        return manager

    @property
    def colors(self) -> dict[str, str]:
        return colors_for(self.effective_theme)

    def _system_theme(self) -> str:
        return "dark" if self._hints.colorScheme() == Qt.ColorScheme.Dark else "light"

    def apply(self) -> None:
        effective = self._system_theme() if self.mode is ThemeMode.SYSTEM else self.mode.value
        self.effective_theme = effective
        self.app.setPalette(palette_for(effective))
        self.app.setStyleSheet(stylesheet_for(effective))
        self._schedule_caption_refresh()
        self.changed.emit(effective)

    def eventFilter(self, watched, event):  # noqa: N802 - Qt override
        if (not self._closed and isinstance(watched, QWidget)
                and watched.isWindow()
                and event.type() in (QEvent.Type.Show, QEvent.Type.WinIdChange,
                                     QEvent.Type.PaletteChange, QEvent.Type.StyleChange,
                                     QEvent.Type.ThemeChange)):
            self._schedule_caption_refresh()
        return False

    def _schedule_caption_refresh(self):
        # Qt can apply its own native caption during subsequent palette/style
        # events. Run once after those events, not before Qt has processed them.
        if (not self._closed and not self._refreshing_captions
                and sys.platform == "win32" and self.app.platformName() == "windows"
                and not self._caption_timer.isActive()):
            self._caption_timer.start()

    def _refresh_native_captions(self):
        if self._closed or self._refreshing_captions:
            return
        self._refreshing_captions = True
        try:
            for widget in self.app.topLevelWidgets():
                apply_native_caption(widget, self.effective_theme == "dark")
        finally:
            self._refreshing_captions = False

    def set_mode(self, mode: str | ThemeMode) -> None:
        selected = ThemeMode(mode)
        if self._closed:
            return
        self.mode = selected
        self.apply()
        self._writer.submit(selected.value)

    def _system_changed(self, *_args) -> None:
        if not self._closed and self.mode is ThemeMode.SYSTEM:
            self.apply()

    def _save_error(self, _error: Exception) -> None:
        try:
            self.save_failed.emit("外观已切换，但设置未保存。请检查数据目录权限后重试。")
        except RuntimeError:
            pass

    def wait_saved(self, timeout: float = 1.0) -> bool:
        return self._writer.wait_idle(timeout)

    def close(self, timeout: float = 1.0) -> bool:
        if not self._closed:
            self._closed = True
            self._caption_timer.stop()
            try:
                self._hints.colorSchemeChanged.disconnect(self._system_changed)
            except (RuntimeError, TypeError):
                pass
            self.app.removeEventFilter(self)
            try:
                self.app.aboutToQuit.disconnect(self.close)
            except (RuntimeError, TypeError):
                pass
        return self._writer.close(timeout)
