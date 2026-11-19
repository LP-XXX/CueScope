"""0.2.2 T1.4：圈选画布（帧内物理像素真值、松手保留、缩放不漂移）。

全部 offscreen：用 QTest 合成鼠标事件，用 grab() 做像素采样，不依赖真机。
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPixmap  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QWidget  # noqa: E402

from deal_watch.models import Rect  # noqa: E402
from deal_watch.ui.region import (  # noqa: E402
    MIN_DRAG_PIXELS,
    RegionEditDialog,
    SelectionLabel,
    fit_dialog_to_screen,
    screen_for_widget,
)

# 合成帧 1000×600 放进 700×700 的控件：等比缩放后图像区域是 (0,140,700,420)，
# 因此 1 帧内像素 = 0.7 控件像素，便于把期望值写成整数。
FRAME_SIZE = (1000, 600)
WIDGET_SIZE = (700, 700)
EXPECTED_TARGET = QRect(0, 140, 700, 420)


class SelectionCanvasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def make_label(self) -> SelectionLabel:
        image = QImage(FRAME_SIZE[0], FRAME_SIZE[1], QImage.Format.Format_RGB32)
        image.fill(0xFFFFFFFF)
        label = SelectionLabel(QPixmap.fromImage(image))
        label.resize(*WIDGET_SIZE)
        return label

    def drag(self, label: SelectionLabel, start: QPoint, end: QPoint) -> None:
        QTest.mousePress(
            label, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start
        )
        QTest.mouseMove(label, end)
        QTest.mouseRelease(
            label, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end
        )

    def test_target_projection_is_scaled_and_centered(self):
        label = self.make_label()
        self.assertEqual(label._target(), EXPECTED_TARGET)

    def test_drag_maps_to_frame_physical_pixels(self):
        label = self.make_label()
        self.drag(label, QPoint(70, 210), QPoint(420, 420))
        self.assertEqual(label.image_rect(), QRect(100, 100, 500, 300))
        self.assertEqual(label.selection_image_rect(), Rect(100, 100, 500, 300))

    def test_all_four_drag_directions_agree(self):
        label = self.make_label()
        corners = [(70, 210), (420, 420)]
        self.drag(label, QPoint(*corners[0]), QPoint(*corners[1]))
        forward = label.selection_image_rect()
        label.clear_selection()
        self.drag(label, QPoint(*corners[1]), QPoint(*corners[0]))
        self.assertEqual(label.selection_image_rect(), forward)

        label.clear_selection()
        # 左上→右下 与 右上→左下 也必须一致（只取 x/y 的反向组合）。
        self.drag(label, QPoint(420, 210), QPoint(70, 420))
        self.assertEqual(label.selection_image_rect(), forward)
        label.clear_selection()
        self.drag(label, QPoint(70, 420), QPoint(420, 210))
        self.assertEqual(label.selection_image_rect(), forward)

    def test_selection_is_kept_after_release_and_painted(self):
        """D1：松手后画面上必须仍能看到选区（用像素采样证明，不靠真机）。"""
        label = self.make_label()
        self.drag(label, QPoint(70, 210), QPoint(420, 420))
        self.assertIsNotNone(label.selection_image_rect())
        shot = label.grab().toImage()
        inside = shot.pixelColor(300, 400)
        outside = shot.pixelColor(600, 160)
        # 选区内部有半透明蓝色填充，绝不是纯白。
        self.assertNotEqual(inside.rgb(), inside.fromRgb(255, 255, 255).rgb())
        self.assertGreater(inside.blue(), inside.red())
        # 图像范围内、选区以外的像素仍是被绘制的原图（纯白）。
        self.assertEqual(outside.red(), 255)
        self.assertEqual(outside.green(), 255)
        self.assertEqual(outside.blue(), 255)
        # 选区上边界附近存在深蓝边框，证明“松手后仍绘制”。
        border_hit = any(
            shot.pixelColor(x, y).blue() > 150 and shot.pixelColor(x, y).red() < 90
            for x in range(70, 420, 7)
            for y in range(206, 214)
        )
        self.assertTrue(border_hit)

    def test_resize_keeps_the_same_frame_rect(self):
        """D2：拉伸对话框不得改变选区真值，也不得改变映射结果。"""
        label = self.make_label()
        self.drag(label, QPoint(70, 210), QPoint(420, 420))
        before = label.selection_image_rect()
        label.resize(700, 700)
        label.resize(1100, 760)
        self.assertEqual(label.selection_image_rect(), before)
        self.assertEqual(label.image_rect(), QRect(100, 100, 500, 300))
        # 新尺寸下重新投影：1 帧内像素 = 1.1 控件像素（高度受限），仍是同一块画面。
        self.assertEqual(label._target(), QRect(0, 50, 1100, 660))
        self.assertEqual(label.selected, QRect(110, 160, 550, 330))

    def test_selection_stays_visible_and_roi_stable_across_resize_and_zoom(self):
        """松手后改变预览缩放（控件尺寸）仍要看得见选区，且物理像素 ROI 不变。"""
        label = self.make_label()
        self.drag(label, QPoint(70, 210), QPoint(420, 420))
        before = label.selection_image_rect()
        for size in ((700, 700), (1100, 760), (430, 320), (900, 500)):
            label.resize(*size)
            self.app.processEvents()
            self.assertEqual(label.selection_image_rect(), before, f"{size} 下 ROI 被改了")
            widget_rect = label.selected
            shot = label.grab().toImage()
            border = QColor("#175cd3")
            for point in (
                QPoint(widget_rect.left(), widget_rect.center().y()),
                QPoint(widget_rect.right(), widget_rect.center().y()),
                QPoint(widget_rect.center().x(), widget_rect.top()),
                QPoint(widget_rect.center().x(), widget_rect.bottom()),
            ):
                colour = shot.pixelColor(point)
                self.assertLess(
                    abs(colour.red() - border.red())
                    + abs(colour.green() - border.green())
                    + abs(colour.blue() - border.blue()),
                    120,
                    f"{size} 下 {point} 看不到选区边框，实际 {colour.name()}",
                )

    def test_click_on_blank_area_clears_selection(self):
        label = self.make_label()
        self.drag(label, QPoint(70, 210), QPoint(420, 420))
        self.assertIsNotNone(label.selection_image_rect())
        # y=50 落在图像上方的留白里：不得生成选区。
        self.drag(label, QPoint(200, 50), QPoint(400, 60))
        self.assertIsNone(label.selection_image_rect())
        self.assertFalse(label.image_rect().isValid())
        self.assertIn("画面", label.hint())

    def test_click_without_drag_is_cancelled_with_reason(self):
        """D3：按下即松手要被显式取消并说明原因，不能静默当成空选区。"""
        label = self.make_label()
        point = QPoint(300, 300)
        self.drag(label, point, point)
        self.assertIsNone(label.selection_image_rect())
        self.assertIn("过小", label.hint())
        self.assertLess(MIN_DRAG_PIXELS, 40)

    def test_drag_is_clamped_inside_the_frame(self):
        label = self.make_label()
        self.drag(label, QPoint(650, 500), QPoint(2000, 2000))
        rect = label.selection_image_rect()
        self.assertIsNotNone(rect)
        self.assertLessEqual(rect.x + rect.width, FRAME_SIZE[0])
        self.assertLessEqual(rect.y + rect.height, FRAME_SIZE[1])

    def test_selected_compatibility_property_round_trips(self):
        """0.2.1 兼容：`selected` 仍是控件坐标，但真值已是帧内坐标。"""
        label = self.make_label()
        label.selected = QRect(70, 210, 350, 210)
        self.assertEqual(label.image_rect(), QRect(100, 100, 500, 300))
        self.assertEqual(label.selected, QRect(70, 210, 350, 210))


class FakeWindow:
    def __init__(self, hwnd: int, title: str) -> None:
        self.hwnd = hwnd
        self.title = title
        self.class_name = "WeChatMainWndForPC"
        self.minimized = False
        self.process_id = 4321
        self.process_path = "C:/Program Files/WeChat/WeChat.exe"
        self.rect = Rect(0, 0, 800, 1280)


class FakeWindows:
    def __init__(self, windows: list[FakeWindow]) -> None:
        self._windows = windows

    def list_windows(self) -> list[FakeWindow]:
        return list(self._windows)

    def window_from_hwnd(self, hwnd: int):
        return next((item for item in self._windows if item.hwnd == hwnd), None)


class RegionSelectionDialogStateTests(unittest.TestCase):
    """圈选对话框：按钮文案、屏幕适配、原因是“未选择/过小/超出帧”。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_dialog(self, frame_size=(800, 1280)):
        import numpy as np

        from deal_watch.ui.region import RegionSelectionDialog

        image = np.zeros((frame_size[1], frame_size[0], 3), dtype=np.uint8)
        image[:, :] = 255
        dialog = RegionSelectionDialog(
            FakeWindow(101, "合成群"), image, frame_size[0], frame_size[1]
        )
        # offscreen 下对话框没有真正显示，手动激活布局才能得到控件真实尺寸。
        dialog.layout().activate()
        return dialog

    def test_ok_button_text_and_screen_fit(self):

        dialog = self.make_dialog()
        self.assertEqual(dialog.ok_button.text(), "使用此区域")
        available = QGuiApplication.primaryScreen().availableGeometry()
        self.assertLessEqual(dialog.width(), max(240, int(available.width() * 0.9)))
        self.assertLessEqual(dialog.height(), max(180, int(available.height() * 0.9)))
        # 说明文字必须换行，否则关键指引会被截断。
        self.assertTrue(dialog.selector.minimumSize().width() < 400)
        dialog.deleteLater()

    def test_reason_text_distinguishes_not_selected_and_too_small(self):
        dialog = self.make_dialog()
        self.assertIn("尚未选择区域", dialog._missing_reason(""))
        self.assertIn("过小", dialog._missing_reason("拖动距离过小（需大于 4 像素），已取消选区"))
        self.assertTrue(dialog.rect_problems(Rect(0, 0, 10, 10)))
        self.assertTrue(dialog.rect_problems(Rect(700, 100, 300, 300)))
        self.assertEqual(dialog.rect_problems(Rect(10, 20, 400, 600)), [])
        dialog.deleteLater()

    def test_accept_requires_a_valid_selection(self):
        dialog = self.make_dialog()
        with patch("deal_watch.ui.region.QMessageBox.warning") as warning:
            dialog.accept()
        self.assertTrue(warning.called)
        self.assertNotEqual(dialog.result(), dialog.DialogCode.Accepted)
        dialog.deleteLater()

    def test_frame_rect_returns_physical_pixels(self):
        dialog = self.make_dialog()
        target = dialog.selector._target()
        self.assertFalse(target.isEmpty())
        dialog.selector.set_widget_selection(target.adjusted(7, 7, -7, -7))
        rect = dialog.frame_rect()
        self.assertGreater(rect.width, 0)
        self.assertLessEqual(rect.x + rect.width, 800)
        self.assertLessEqual(rect.y + rect.height, 1280)
        self.assertEqual(rect.x, round(7 * 800 / target.width()))
        dialog.deleteLater()

    def test_resize_keeps_the_selection_painted_and_roi_unchanged(self):
        """调整弹窗尺寸（预览缩放）后，选区仍可见且 ROI 不变。"""
        dialog = self.make_dialog()
        dialog.selector.set_widget_selection(dialog.selector._target().adjusted(20, 20, -20, -20))
        before = dialog.frame_rect()
        self.assertTrue(dialog.ok_button.isEnabled())
        for size in ((900, 640), (760, 820), (1100, 760)):
            dialog.resize(*size)
            dialog.layout().activate()
            self.app.processEvents()
            # 第一次 grab 只用来强制布局把新尺寸传给画布；随后的 rect 才是画出来的位置。
            dialog.selector.grab()
            self.assertEqual(dialog.frame_rect(), before, f"{size} 下 ROI 被改了")
            widget_rect = dialog.selector.selected
            shot = dialog.selector.grab().toImage()
            self.assertEqual(shot.size(), dialog.selector.size())
            inside = shot.pixelColor(widget_rect.center())
            self.assertGreater(inside.blue(), inside.red(), f"{size} 下选区填充消失")
            border = QColor("#175cd3")
            for point in (
                QPoint(widget_rect.left(), widget_rect.center().y()),
                QPoint(widget_rect.center().x(), widget_rect.top()),
            ):
                colour = shot.pixelColor(point)
                self.assertLess(
                    abs(colour.red() - border.red())
                    + abs(colour.green() - border.green())
                    + abs(colour.blue() - border.blue()),
                    120,
                    f"{size} 下 {point} 看不到选区边框，实际 {colour.name()}",
                )
        dialog.deleteLater()


class RegionEditDialogInvalidationTests(unittest.TestCase):
    """切换窗口/采集后端后，旧圈选必须失效（沿用 0.2.1 的代数语义）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.windows = [FakeWindow(101, "群 A"), FakeWindow(202, "群 B")]
        self.patcher = patch(
            "deal_watch.ui.region.WindowsWindows", return_value=FakeWindows(self.windows)
        )
        self.patcher.start()
        self.dialog = RegionEditDialog()
        self.dialog.alias.setText("群 B")

    def tearDown(self):
        self.patcher.stop()
        self.dialog.deleteLater()
        self.app.processEvents()

    def test_selection_survives_then_backend_change_clears_it(self):
        import numpy as np
        self.dialog.window_combo.setCurrentIndex(1)
        self.dialog.resize(900, 680)
        self.dialog.show()
        self.app.processEvents()
        self.dialog._on_selection_completed(
            self.dialog._selection_generation, 202, "wgc", self.windows[1],
            np.full((1280, 800, 3), 240, dtype=np.uint8), 800, 1280, "",
        )
        self.app.processEvents()
        self.dialog.selector.set_widget_selection(self.dialog.selector._target().adjusted(8, 8, -8, -8))
        self.app.processEvents()
        self.assertIsNotNone(self.dialog.selected_rect)
        self.assertIn("已选", self.dialog.selection_label.text())
        generation = self.dialog._selection_generation
        # 换采集后端：旧圈选立即失效，必须重新圈选。
        self.dialog.backend_combo.setCurrentIndex(1)
        self.assertGreater(self.dialog._selection_generation, generation)
        self.assertIsNone(self.dialog.selected_rect)
        self.assertIsNone(self.dialog._selection)
        self.assertIn("重新圈选", self.dialog.selection_label.text())
        self.assertFalse(self.dialog.ok_button.isEnabled())


class FakeScreen:
    """带可用区域的假屏幕：用来把“父窗口在哪块屏”这件事变得可断言。"""

    def __init__(self, rect: QRect) -> None:
        self._rect = rect

    def availableGeometry(self) -> QRect:
        return self._rect


def win32_monitors() -> list[tuple[str, bool, QRect]]:
    r"""用 Win32 枚举真实显示器：返回 ``[(设备名, 是否主显示器, 物理矩形)]``。

    必须用 Win32 而不是 Qt：Qt 的 ``primaryScreen()`` 只是“主屏”，在这台机器上
    主屏是 ``\\.\DISPLAY5``，``\\.\DISPLAY1`` 反而是 2560 起的第二块屏。把二者
    当成同一个东西，占屏测试就会跑到错误的显示器上。
    """
    import ctypes
    from ctypes import wintypes

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    user32 = ctypes.windll.user32
    prototype = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )
    monitors: list[tuple[str, bool, QRect]] = []

    @prototype
    def callback(hmonitor, _hdc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            rect = info.rcMonitor
            monitors.append(
                (
                    info.szDevice,
                    bool(info.dwFlags & 0x1),  # MONITORINFOF_PRIMARY
                    QRect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top),
                )
            )
        return True

    user32.EnumDisplayMonitors(None, None, callback, 0)
    return monitors


def win32_display1_rect() -> QRect | None:
    for name, _primary, rect in win32_monitors():
        if name.upper() == r"\\.\DISPLAY1":
            return rect
    return None


class DialogScreenPlacementTests(unittest.TestCase):
    """要求 4：弹窗以父窗口所在屏幕定位，不再无条件使用 Qt 主屏。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_screen_for_widget_prefers_parent_screen(self):
        parent = QWidget()
        dialog = QDialog(parent)
        fake = FakeScreen(QRect(2560, 0, 1067, 1707))
        parent.screen = lambda: fake
        try:
            self.assertIs(screen_for_widget(dialog), fake)
        finally:
            dialog.deleteLater()
            parent.deleteLater()

    def test_fit_places_dialog_inside_parent_screen(self):
        parent = QWidget()
        dialog = QDialog(parent)
        fake_rect = QRect(2560, 0, 1067, 1707)
        parent.screen = lambda: FakeScreen(fake_rect)
        try:
            fit_dialog_to_screen(dialog, 1100, 760)
            geometry = dialog.geometry()
            self.assertTrue(
                fake_rect.contains(geometry),
                f"弹窗没有落在父窗口所在屏幕内：{geometry} vs {fake_rect}",
            )
            self.assertEqual(geometry.width(), int(fake_rect.width() * 0.9))
            self.assertEqual(geometry.height(), min(760, int(fake_rect.height() * 0.9)))
            # 若实现退回 Qt 主屏，x 会接近 0；这里必须留在 2560 那块屏上。
            self.assertGreaterEqual(geometry.x(), fake_rect.x())
        finally:
            dialog.deleteLater()
            parent.deleteLater()

    @unittest.skipUnless(sys.platform == "win32", "Win32 显示器枚举仅 Windows 可用")
    def test_display1_rect_comes_from_win32_not_from_qt_primary(self):
        """用 Win32 确认 DISPLAY1；Qt 主屏不等于屏幕 1，不能互相替代。"""
        monitors = win32_monitors()
        self.assertTrue(monitors, "Win32 没有枚举到任何显示器")
        display1 = win32_display1_rect()
        self.assertIsNotNone(display1, r"未找到 \\.\DISPLAY1；禁止在非 DISPLAY1 上做占屏测试")
        for name, _primary, rect in monitors:
            self.assertTrue(rect.width() > 0 and rect.height() > 0, f"{name} 尺寸非法")
        primary = [item for item in monitors if item[1]]
        self.assertEqual(len(primary), 1, "Win32 主显示器标记不唯一")
        # 这条只是记录事实：本机主屏（DISPLAY5）与 DISPLAY1 是两块不同的屏。
        # 断言的是“实现按父窗口屏定位”（上一用例），而不是“Qt 主屏 == 1 号屏”。
        self.assertTrue(primary[0][0].upper().startswith(r"\\.\DISPLAY"))

    @unittest.skipUnless(sys.platform == "win32", "Win32 显示器枚举仅 Windows 可用")
    def test_dialog_uses_display1_rect_even_when_qt_primary_is_elsewhere(self):
        display1 = win32_display1_rect()
        if display1 is None:  # pragma: no cover - 由上一用例报告缺失
            self.skipTest("本机没有 DISPLAY1")
        parent = QWidget()
        dialog = QDialog(parent)
        parent.screen = lambda: FakeScreen(display1)
        try:
            fit_dialog_to_screen(dialog, 1100, 760)
            self.assertTrue(display1.contains(dialog.geometry()))
            self.assertGreaterEqual(dialog.geometry().x(), display1.x())
        finally:
            dialog.deleteLater()
            parent.deleteLater()


if __name__ == "__main__":
    unittest.main()
