from __future__ import annotations

import importlib.util
import os
import re
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QCoreApplication, QEvent, QRect  # noqa: E402
from PySide6.QtGui import QFontDatabase, QImage, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit  # noqa: E402

from deal_watch.models import (
    HealthSnapshot,
    MatchEvent,
    Rect,
    RegionConfig,
    RegionStatus,
)
from deal_watch.ui.main import (
    MainWindow,
    RecordsRefreshCoordinator,
    decision_label,
    delivery_label,
)
from deal_watch.ui.region import PreviewDialog, SelectionLabel  # noqa: E402
from deal_watch.ui.region_list import RegionColumn  # noqa: E402
from deal_watch.ui.shell import PageId, RecordView  # noqa: E402
from region_fixtures import ready_region  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def pyproject_version() -> str:
    """``pyproject.toml`` 里的 ``project.version``：版本号的唯一真值源。"""
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def load_version_metadata():
    """加载 ``packaging/version_metadata.py``：Windows 版本资源的渲染逻辑。"""
    path = REPO_ROOT / "packaging" / "version_metadata.py"
    spec = importlib.util.spec_from_file_location("version_metadata_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UiRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if not QFontDatabase.families():
            for name in ("segoeui.ttf", "seguisb.ttf", "msyh.ttc", "msyhbd.ttc"):
                font_path = Path("C:/Windows/Fonts") / name
                if font_path.exists():
                    QFontDatabase.addApplicationFont(str(font_path))
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.environment.start()
        self.window = MainWindow(Path(__file__).resolve().parents[1])
        ready_deadline = time.monotonic() + 5
        while not self.window._recognition_ready and time.monotonic() < ready_deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(self.window._recognition_ready)

    def pump_until(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not condition():
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(condition())

    def tearDown(self):
        self.window.exit_app()
        self.pump_until(lambda: self.window._shutdown_done)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.environment.stop()
        self.temp.cleanup()

    def test_stop_does_not_block_event_loop_and_exit_closes_workers(self):
        release = threading.Event()

        class SlowService:
            def __init__(self):
                self.cancelled = False

            def request_stop(self):
                self.cancelled = True

            def stop(self):
                release.wait(2)

            def is_alive(self):
                return not release.is_set()

        service = SlowService()
        self.window.service = service
        started = time.monotonic()
        self.window.stop_monitoring()
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertTrue(service.cancelled)
        self.assertFalse(self.window.start_button.isEnabled())
        release.set()
        self.pump_until(lambda: self.window._stopping_service is None)
        self.assertTrue(self.window.start_button.isEnabled())

    def test_fault_baseline_running_reports_recovery_once(self):
        region = RegionConfig("合成群", "synthetic", Rect(0, 0, 200, 200), id="r1")
        self.window.config.regions = [region]
        self.window._load_regions()
        self.window.service = object()
        try:
            with patch.object(self.window, "queue_system_message") as queue:
                for state in (RegionStatus.ERROR, RegionStatus.ERROR, RegionStatus.BASELINING,
                              RegionStatus.RUNNING, RegionStatus.RUNNING):
                    self.window.on_health(HealthSnapshot("r1", state, "合成状态"))
                self.assertEqual([call.args[0] for call in queue.call_args_list], ["监控故障", "监控恢复"])
        finally:
            self.window.service = None

    def test_webhook_masked_and_invalid_save_preserves_credentials(self):
        self.assertEqual(self.window.webhook_edit.echoMode(), QLineEdit.EchoMode.Password)
        self.window.webhook_edit.setText("http://127.0.0.1:18080/ok")
        self.window.secret_edit.setText("synthetic-secret")
        with patch("deal_watch.ui.main.set_windows_autostart"):
            self.window.save_settings()
        self.pump_until(lambda: not self.window._settings_busy)
        original = self.window._credential_snapshot
        self.window.webhook_edit.setText("http://outside.invalid/private")
        with patch("deal_watch.ui.main.QMessageBox.critical"):
            self.window.save_settings()
        self.assertEqual(self.window._credential_snapshot, original)
        self.assertEqual(self.window.credentials.load(), original)

    def test_selection_maps_scaled_preview_to_physical_pixels(self):
        image = QImage(1000, 600, QImage.Format.Format_RGB32)
        image.fill(0xFFFFFFFF)
        selector = SelectionLabel(QPixmap.fromImage(image))
        selector.resize(700, 700)
        target = selector._target()
        selector.selected = QRect(target.x() + 70, target.y() + 70, 350, 210)
        self.assertEqual(selector.image_rect(), QRect(100, 100, 500, 300))

    def test_preview_cannot_destroy_running_ocr_thread(self):
        class Worker:
            def isRunning(self):
                return True

        dialog = PreviewDialog.__new__(PreviewDialog)
        QDialog.__init__(dialog)
        from PySide6.QtWidgets import QLabel

        dialog.worker = Worker()
        dialog.status = QLabel()
        worker = dialog.worker
        with patch("deal_watch.ui.region._detach_capture") as detach:
            dialog.done(QDialog.DialogCode.Rejected)
        detach.assert_called_once_with(worker)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertTrue(dialog._closed)
        self.assertIsNone(dialog.worker)
        dialog.deleteLater()


    def test_records_show_chinese_judgment_and_delivery_labels(self):
        headers = [
            self.window.candidate_table.horizontalHeaderItem(4).text(),
            self.window.record_table.horizontalHeaderItem(4).text(),
        ]
        self.assertEqual(headers, ["处理结果", "投递状态"])
        self.assertIn("不等于手机送达", self.window.record_table.horizontalHeaderItem(4).toolTip())
        # 0.2.1 关闭学习路径：识别记录不再有“模型分值”列。
        candidate_headers = [
            self.window.candidate_table.horizontalHeaderItem(index).text()
            for index in range(self.window.candidate_table.columnCount())
        ]
        self.assertEqual(
            candidate_headers,
            ["识别时间", "来源", "消息文字", "命中词", "处理结果"],
        )
        self.assertEqual(decision_label("not_matched"), "未命中")
        self.assertEqual(decision_label("queued"), "判断应通知·已入队")
        self.assertEqual(decision_label("duplicate"), "判断应通知·重复合并")
        self.assertEqual(decision_label("already_processed"), "同一消息已处理")
        self.assertEqual(decision_label("boundary_uncertain"), "边界不确定，未判断")
        self.assertEqual(decision_label("unsupported_message_type"), "非普通文字，跳过")
        # 未知状态保持原值，不隐藏新代码。
        self.assertEqual(decision_label("model_degraded_char_rule"), "model_degraded_char_rule")
        self.assertEqual(delivery_label("sent"), "平台已接受")
        self.assertEqual(delivery_label("pending"), "等待发送")
        self.assertEqual(delivery_label("not_a_state"), "not_a_state")
        self.window.refresh_recognition()
        self.window.refresh_records()

    def test_version_matches_pyproject_and_learning_page_is_gone(self):
        """版本号只有一处真值源；漂移必须由这条用例发现。

        0.2.1 时代这里断言 ``__version__ == "0.2.1"``：升版要同时改源码、pyproject
        与这条断言，漏一处就静默漂移；``self_check`` 又写死同一个字面量，于是“升版”
        变成发版必炸的雷。现在源码与 ``pyproject.toml`` 交叉校验，Windows 版本资源
        与打包脚本也一并核对。
        """
        from deal_watch import __version__
        from deal_watch.self_check import VERSION_PATTERN

        expected = pyproject_version()
        self.assertRegex(expected, VERSION_PATTERN, "pyproject 版本号格式非法")
        self.assertEqual(
            __version__,
            expected,
            f"deal_watch.__version__={__version__} 与 pyproject project.version={expected} 不一致",
        )
        parts = [int(item) for item in expected.split(".")]
        # Windows 版本资源是**模板**：本身不含版本数字，构建时按 pyproject 渲染成
        # build/version_info.txt 再交给 PyInstaller。0.2.1 时代它把版本号写死四处，
        # 升版漏改就会让 EXE 属性显示旧版本——与 self_check 写死版本号同类。
        version_metadata = load_version_metadata()
        template = (REPO_ROOT / "packaging" / version_metadata.TEMPLATE_NAME).read_text(
            encoding="utf-8"
        )
        self.assertIsNone(
            re.search(r"\d+\.\d+\.\d+", template),
            "packaging/version_info.txt 是模板，出现版本字面量就会在升版后让 EXE 属性显示旧版本",
        )
        rendered = version_metadata.render_version_info(template, expected)
        quadruple = f"({parts[0]}, {parts[1]}, {parts[2]}, 0)"
        for field in ("filevers", "prodvers"):
            self.assertIn(
                f"{field}={quadruple}",
                rendered,
                f"渲染出的 Windows 版本资源 {field} 与 pyproject 版本号不一致",
            )
        for field in ("FileVersion", "ProductVersion"):
            self.assertIn(
                f"'{field}', '{expected}'",
                rendered,
                f"渲染出的 Windows 版本资源 {field} 与 pyproject 版本号不一致",
            )
        # 渲染必须真的发生在打包前，且 spec 交给 PyInstaller 的是渲染产物（不是模板：
        # 模板里没有版本数字，直接把模板递过去会得到“无版本资源”的 EXE）。
        for relative, needle in (
            ("packaging/deal_watch.spec", "version=str(VERSION_INFO)"),
            ("tools/build.py", "ensure_version_info(ROOT)"),
        ):
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            self.assertIn(
                needle,
                text,
                f"{relative} 没有把渲染后的版本资源接进打包链路，EXE 属性会与 pyproject 脱钩",
            )
        # 打包链路不得再持有版本字面量：目录名/报告名一律由 pyproject 派生。
        for relative in ("packaging/deal_watch.spec", "tools/build.py", "tools/finalize_package.py"):
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            self.assertNotRegex(
                text,
                r'DealWatch-0\.\d',
                f"{relative} 写死了产物版本号，升版会与其派生来源错位",
            )
        self.assertIn(__version__, self.window.windowTitle())
        self.assertEqual(self.window.pages.count(), 4)
        self.assertEqual(self.window.records_tabs.count(), 2)
        self.assertEqual([self.window.records_tabs.tabText(i) for i in range(2)], ["事件与通知", "全部识别"])
        titles = [self.window.tabs.tabText(index) for index in range(self.window.tabs.count())]
        self.assertNotIn("反馈学习", titles)
        self.assertFalse(hasattr(self.window, "model_registry"))
        self.assertFalse(hasattr(self.window, "model_runtime"))
        self.assertNotIn("监控中", self.window.shell.status.text())

    def test_paused_state_does_not_queue_feishu_notification(self):
        region = RegionConfig("合成群", "synthetic", Rect(0, 0, 200, 200), id="r1")
        self.window.config.regions = [region]
        self.window._load_regions()
        self.window.service = object()
        try:
            with patch.object(self.window, "queue_system_message") as queue:
                self.window.on_health(
                    HealthSnapshot("r1", RegionStatus.PAUSED, "已暂停：窗口最小化")
                )
                self.window.on_health(
                    HealthSnapshot("r1", RegionStatus.PAUSED, "已暂停：桌面已锁定")
                )
                self.window.on_health(HealthSnapshot("r1", RegionStatus.RUNNING, "监控中"))
                queue.assert_not_called()
        finally:
            self.window.service = None

    def test_health_only_notifies_when_state_changes(self):
        region = RegionConfig("合成群", "synthetic", Rect(0, 0, 200, 200), id="r1")
        self.window.config.regions = [region]
        self.window._load_regions()
        self.window.service = object()
        try:
            with patch.object(self.window, "queue_system_message") as queue:
                for _ in range(3):
                    self.window.on_health(HealthSnapshot("r1", RegionStatus.RUNNING, "监控中"))
                queue.assert_not_called()
                self.window.on_health(HealthSnapshot("r1", RegionStatus.ERROR, "采集失败"))
                self.window.on_health(HealthSnapshot("r1", RegionStatus.ERROR, "采集失败"))
                self.assertEqual([call.args[0] for call in queue.call_args_list], ["监控故障"])
        finally:
            self.window.service = None

    def test_start_monitoring_requires_confirmed_preview(self):
        region = RegionConfig("合成群", "synthetic", Rect(0, 0, 200, 200), id="r1")
        self.window.config.regions = [region]
        self.window.config.keywords = ["手机"]
        self.window.webhook = "http://127.0.0.1:18080/ok"
        self.window._load_regions()
        with patch("deal_watch.ui.main.QMessageBox.warning") as warning:
            self.window.start_monitoring()
        self.assertIsNone(self.window.service)
        self.assertEqual(warning.call_args.args[1], "需要先预览")
        self.assertIn("合成群", warning.call_args.args[2])

    def test_unready_region_blocks_start_with_readiness_reason(self):
        # 只差“未记录参考帧尺寸/未绑定句柄”等结构性条件：标题必须是“区域未就绪”，
        # 且正文列出别名与原因，不能静默启动一个采集不到东西的区域。
        region = ready_region("合成群", previewed=True)
        region.window_hwnd = 0
        self.window.config.regions = [region]
        self.window.config.keywords = ["手机"]
        self.window.webhook = "http://127.0.0.1:18080/ok"
        self.window._load_regions()
        with patch("deal_watch.ui.main.QMessageBox.warning") as warning:
            self.window.start_monitoring()
        self.assertIsNone(self.window.service)
        self.assertEqual(warning.call_args.args[1], "区域未就绪")
        self.assertIn("合成群", warning.call_args.args[2])
        self.assertIn("未绑定窗口句柄", warning.call_args.args[2])

    def test_confirm_region_sets_previewed_and_unblocks_start(self):
        class Service:
            def __init__(self, *args, **kwargs):
                self.started = False

            def start(self):
                self.started = True

            def request_stop(self):
                return None

            def stop(self):
                return None

            def is_alive(self):
                return False

        region = ready_region("合成群", rect=Rect(0, 0, 200, 200), previewed=False, id="r1")
        self.window.config.regions = [region]
        self.window.config.keywords = ["手机"]
        self.window.webhook = "http://127.0.0.1:18080/ok"
        self.window._load_regions()
        from PySide6.QtWidgets import QPushButton

        from deal_watch.ui.region import PreviewDialog

        dialog = PreviewDialog.__new__(PreviewDialog)
        QDialog.__init__(dialog)
        dialog.region = region
        dialog.confirm_button = QPushButton()
        dialog.accept = lambda: None
        # 现已要求“识别到文字块”这一证据才能确认：本用例模拟一次成功识别。
        dialog._last_block_count = 3
        with patch("deal_watch.ui.region.QMessageBox.information"):
            dialog.confirm_region()
        self.assertTrue(region.previewed)
        self.window._load_regions()
        self.assertEqual(
            self.window.region_table.item(0, RegionColumn.STATUS).text(), "已就绪"
        )
        with patch("deal_watch.ui.main.MonitorService", Service), patch(
            "deal_watch.ui.main.WindowFrameCapturer", lambda *args, **kwargs: object()
        ), patch("deal_watch.ui.main.LocalOcr", lambda *args, **kwargs: object()):
            self.window.start_monitoring()
        self.assertIsNotNone(self.window.service)
        self.assertTrue(self.window.service.started)
        self.window.service = None
        dialog.deleteLater()

    def test_shared_timing_recorder_wired_to_ocr_service_capture_and_notifier(self):
        """一条监控链路共用一个 TimingRecorder：OCR 子段、服务段、通知段同源。

        只断言接线身份，不跑真实采集：四条通路都必须指向 ``window.metrics``，
        否则报告里会出现"服务有样本、通知段空白"的分裂口径。
        """
        captured: dict[str, object] = {}

        class Service:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)

            def start(self):
                return None

            def request_stop(self):
                return None

            def stop(self):
                return None

            def is_alive(self):
                return False

        def ocr_factory(*args, **kwargs):
            captured["ocr_observer"] = kwargs.get("observer")
            return object()

        def capturer_factory(*args, **kwargs):
            captured["capturer_kwargs"] = kwargs
            return object()

        region = ready_region("合成群", rect=Rect(0, 0, 200, 200), previewed=True, id="r1")
        self.window.config.regions = [region]
        self.window.config.keywords = ["手机"]
        self.window.config.capture_roi_mode = False
        self.window.webhook = "http://127.0.0.1:18080/ok"
        self.window._load_regions()
        with patch("deal_watch.ui.main.MonitorService", Service), patch(
            "deal_watch.ui.main.WindowFrameCapturer", capturer_factory
        ), patch("deal_watch.ui.main.LocalOcr", ocr_factory):
            self.window.start_monitoring()
        self.addCleanup(setattr, self.window, "service", None)
        self.assertIs(captured.get("metrics"), self.window.metrics, "服务没有用到共享记录器")
        observer = captured.get("ocr_observer")
        self.assertIs(getattr(observer, "recorder", None), self.window.metrics, "OCR 子段没有接到共享记录器")
        self.assertIs(
            self.window.dispatcher.metrics.recorder,
            self.window.metrics,
            "通知段没有接到共享记录器",
        )
        self.assertIs(
            captured.get("capturer_kwargs", {}).get("capture_roi_mode"),
            False,
            "ROI 开关没有从配置传到采集器",
        )


class FakeWindow:
    def __init__(self, hwnd: int, title: str, minimized: bool = False) -> None:
        self.hwnd = hwnd
        self.title = title
        self.class_name = "WeChatMainWndForPC"
        self.minimized = minimized
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


class RegionSelectionGenerationTests(unittest.TestCase):
    """A1：圈选代数、迟到结果丢弃、保存前六项复核。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if not QFontDatabase.families():
            for name in ("segoeui.ttf", "seguisb.ttf", "msyh.ttc", "msyhbd.ttc"):
                font_path = Path("C:/Windows/Fonts") / name
                if font_path.exists():
                    QFontDatabase.addApplicationFont(str(font_path))
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        from deal_watch.ui.region import RegionEditDialog

        self.windows = [FakeWindow(101, "群 A"), FakeWindow(202, "群 B")]
        self.fake = FakeWindows(self.windows)
        self.patcher = patch("deal_watch.ui.region.WindowsWindows", return_value=self.fake)
        self.patcher.start()
        self.dialog = RegionEditDialog()
        self.dialog.alias.setText("群 B")

    def tearDown(self):
        self.patcher.stop()
        self.dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_switching_window_or_backend_invalidates_selection(self):
        generation = self.dialog._selection_generation
        self.dialog.window_combo.setCurrentIndex(1)
        self.assertGreater(self.dialog._selection_generation, generation)
        self.assertIsNone(self.dialog.selected_rect)
        self.assertIn("重新圈选", self.dialog.selection_label.text())
        self.dialog.backend_combo.setCurrentIndex(1)
        self.assertIsNone(self.dialog._selection)

    def test_late_result_with_old_generation_is_discarded(self):
        image = object()
        stale = self.dialog._selection_generation
        self.dialog.window_combo.setCurrentIndex(1)
        with patch("deal_watch.ui.region.RegionSelectionDialog") as selection:
            self.dialog._on_selection_completed(
                stale, 101, "wgc", self.windows[0], image, 800, 1280, ""
            )
            selection.assert_not_called()
        self.assertIsNone(self.dialog.selected_rect)

    def test_result_with_mismatched_identity_is_rejected(self):
        with patch("deal_watch.ui.region.RegionSelectionDialog") as selection, patch(
            "deal_watch.ui.region.QMessageBox.warning"
        ) as warning:
            self.dialog._on_selection_completed(
                self.dialog._selection_generation, 999, "wgc",
                self.windows[0], object(), 800, 1280, "",
            )
            selection.assert_not_called()
            warning.assert_not_called()
        self.assertIn("变化", self.dialog.capture_status.text())
        self.assertIsNone(self.dialog.selector)
        self.assertIsNone(self.dialog.selected_rect)

    def _complete_inline_selection(self):
        import numpy as np
        self.dialog.window_combo.setCurrentIndex(1)
        generation = self.dialog._selection_generation
        self.dialog._on_selection_completed(
            generation, 202, "wgc", self.windows[1],
            np.full((1280, 800, 3), 245, dtype=np.uint8), 800, 1280, "",
        )
        self.assertIsNotNone(self.dialog.selector)
        self.dialog.selector.resize(400, 640)
        self.dialog.selector.set_widget_selection(
            self.dialog.selector._to_widget_rect(Rect(10, 10, 700, 900))
        )

    def test_completed_selection_feeds_region_and_passes_review(self):
        self._complete_inline_selection()
        self.assertEqual(self.dialog.selection_problems(), [])
        region = self.dialog.region()
        self.assertEqual(region.window_hwnd, 202)
        self.assertEqual(region.capture_backend, "wgc")
        self.assertEqual(region.reference_frame_width, 800)
        self.assertEqual(region.rect, self.dialog.selector.selection_image_rect())
        self.assertGreater(region.rect.width, 0)
        self.assertGreater(region.rect.height, 0)
        self.assertLessEqual(region.rect.x + region.rect.width, region.reference_frame_width)
        self.assertLessEqual(region.rect.y + region.rect.height, region.reference_frame_height)
        # 新圈选的区域必须先预览确认，不能直接启动监控。
        self.assertFalse(region.previewed)

    def test_review_rejects_backend_change_after_selection(self):
        self._complete_inline_selection()
        # 后端被改成 PrintWindow：旧圈选的坐标不能沿用。
        self.dialog.backend_combo.setCurrentIndex(1)
        problems = self.dialog.selection_problems()
        self.assertTrue(problems)
        with patch("deal_watch.ui.region.QMessageBox.warning") as warning:
            self.dialog.accept_pending()
            warning.assert_not_called()
        self.assertIn("圈选", self.dialog.capture_status.text())
        self.assertNotEqual(self.dialog.result(), QDialog.DialogCode.Accepted)


def pump_events(app, condition, timeout: float = 5.0) -> bool:
    """推进 Qt 事件循环直到条件成立；返回是否在超时前成立。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not condition():
        app.processEvents()
        time.sleep(0.005)
    return condition()


class TabScopedRefreshTests(unittest.TestCase):
    """要求 1：两张表分别按“所属页签是否可见”决定查不查、重不重建。

    设计前提：用户停在“监控区域/设置”页或把窗口收进托盘时，看不见的表既不该
    查询也不该重建；切回记录页必须补刷一次，而不是等下一次心跳。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if not QFontDatabase.families():
            for name in ("segoeui.ttf", "seguisb.ttf", "msyh.ttc", "msyhbd.ttc"):
                font_path = Path("C:/Windows/Fonts") / name
                if font_path.exists():
                    QFontDatabase.addApplicationFont(str(font_path))
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.environment.start()
        self.window = MainWindow(Path(__file__).resolve().parents[1])
        ready_deadline = time.monotonic() + 5
        while not self.window._recognition_ready and time.monotonic() < ready_deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(self.window._recognition_ready)
        # 手动驱动心跳，避免真实定时器在断言中间插进来。
        # This fixture isolates record visibility. A live dispatcher would update
        # outbox revisions concurrently, correctly making a query stale; its
        # retry-on-next-heartbeat contract is covered by StaleResultTests below.
        self.window.dispatcher.stop()
        self.window.timer.stop()
        self.window.health_timer.stop()

    def tearDown(self):
        self.window.exit_app()
        pump_events(self.app, lambda: self.window._shutdown_done)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.environment.stop()
        self.temp.cleanup()

    def show_window(self) -> None:
        self.window.show()
        self.app.processEvents()
        self.window.timer.stop()
        self.window.health_timer.stop()

    @staticmethod
    def event(key: str) -> MatchEvent:
        return MatchEvent("r1", "合成群", "手机", f"文本 {key}", key, time.time())

    def counts(self) -> tuple[int, int]:
        return (
            self.window.records_refresh.query_count,
            self.window.recognition_refresh.query_count,
        )

    def test_hidden_tabs_never_query_and_entry_refreshes_exactly_once(self):
        self.show_window()
        self.assertEqual(self.window.tabs.currentIndex(), 0)

        for _ in range(5):
            self.window.refresh_runtime_views()
        self.assertEqual(self.counts(), (0, 0), "停在监控页时不得查询任何记录表")

        # 记录页隐藏期间有新数据：仍然零查询（版本号推进不构成查询理由）。
        self.window.storage.record_match(self.event("hidden-1"), 300)
        from deal_watch.models import MessageCandidate
        self.window.recognition.record_candidates(
            "r1", "合成群", time.time(),
            [MessageCandidate("r1", "隐藏期间的候选", Rect(0, 0, 100, 20), 1.0, 1.0)], {},
        )
        for _ in range(5):
            self.window.refresh_runtime_views()
        self.assertEqual(self.counts(), (0, 0), "非记录页不得自动查询记录表")

        # 切到通知记录页：只补刷这一张表，并真的把数据填进表。
        self.window.navigate(PageId.RECORDS, RecordView.NOTIFICATIONS)
        self.assertEqual(self.counts(), (1, 0), "切入记录页必须补刷且只刷这一张表")
        self.assertTrue(
            pump_events(self.app, lambda: self.window.record_table.rowCount() == 1),
            "切入记录页后的补刷没有填进表格",
        )

        # 停在设置页：新事件不触发查询；切回记录页再补一次。
        self.window.navigate(PageId.SETTINGS)
        self.window.storage.record_match(self.event("hidden-2"), 300)
        for _ in range(5):
            self.window.refresh_runtime_views()
        self.assertEqual(self.counts(), (1, 0), "设置页不得查询通知记录表")
        self.window.navigate(PageId.RECORDS, RecordView.NOTIFICATIONS)
        self.assertEqual(self.counts(), (2, 0), "切回记录页必须补刷最新版本")
        self.assertTrue(
            pump_events(self.app, lambda: self.window.record_table.rowCount() == 2)
        )

        # 识别记录页同理，且互不牵连。
        self.window.navigate(PageId.RECORDS, RecordView.RECOGNITION)
        self.assertEqual(self.counts(), (2, 1), "识别记录页只补刷候选表")
        self.assertTrue(
            pump_events(self.app, lambda: self.window.candidate_table.rowCount() == 1)
        )
        for _ in range(3):
            self.window.refresh_runtime_views()
        self.assertEqual(self.counts(), (2, 1), "同一版本号不得重复查询")

        # 托盘隐藏：即使停在记录页也零查询。
        self.window.hide()
        self.app.processEvents()
        self.window.storage.record_match(self.event("hidden-3"), 300)
        for _ in range(5):
            self.window.refresh_runtime_views()
        self.assertEqual(self.counts(), (2, 1), "窗口收起时记录表不得自动查询")

    def test_unchanged_rows_are_not_repainted(self):
        self.show_window()
        self.window.navigate(PageId.RECORDS, RecordView.NOTIFICATIONS)
        pump_events(self.app, lambda: not self.window.records_refresh.busy)
        self.window.storage.record_match(self.event("paint-1"), 300)
        self.window.refresh_runtime_views()
        self.assertTrue(
            pump_events(self.app, lambda: self.window.record_table.rowCount() == 1)
        )

        repaints: list[int] = []
        original = self.window.record_table.setRowCount

        def counting(count: int) -> None:
            repaints.append(count)
            original(count)

        self.window.record_table.setRowCount = counting
        try:
            # 版本号推进但可见字段没变（cleanup 不删新鲜事件）：允许查询，不许重绘。
            self.window.storage.cleanup()
            before = self.window.records_refresh.query_count
            self.window.refresh_runtime_views()
            self.assertEqual(
                self.window.records_refresh.query_count, before + 1, "版本号推进必须查一次"
            )
            pump_events(self.app, lambda: not self.window.records_refresh.busy)
            self.assertEqual(repaints, [], "数据没变却重建了整表")

            # 真正的新数据才重绘，且只重绘一次。
            self.window.storage.record_match(self.event("paint-2"), 300)
            self.window.refresh_runtime_views()
            self.assertTrue(
                pump_events(self.app, lambda: self.window.record_table.rowCount() == 2)
            )
            self.assertEqual(repaints, [2], "新数据必须重绘一次")
        finally:
            self.window.record_table.setRowCount = original


class StaleResultTests(unittest.TestCase):
    """要求 1：查询期间版本号又变了，这批结果已过期，必须丢弃且不记已送达。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if not QFontDatabase.families():
            for name in ("segoeui.ttf", "seguisb.ttf", "msyh.ttc", "msyhbd.ttc"):
                font_path = Path("C:/Windows/Fonts") / name
                if font_path.exists():
                    QFontDatabase.addApplicationFont(str(font_path))

    def test_result_superseded_by_newer_revision_is_dropped(self):
        state = {"revision": 5}

        def fetch() -> list[dict]:
            state["revision"] = 6  # 查询期间又写入了一笔
            return [{"stale": True}]

        coordinator = RecordsRefreshCoordinator(
            fetch, revision_source=lambda: state["revision"], name="stale"
        )
        rows: list[list[dict]] = []
        coordinator.rows_ready.connect(rows.append)
        try:
            self.assertTrue(coordinator.tick(True, 5))
            self.assertTrue(pump_events(self.app, lambda: not coordinator.busy))
            self.assertEqual(rows, [], "过期结果不得填表")
            self.assertEqual(coordinator.stale_count, 1)
            self.assertIsNone(
                coordinator.delivered_revision, "过期结果不得被记成已送达"
            )

            # 版本号稳定后，下一次心跳才把最新结果送达。
            self.assertTrue(coordinator.tick(True, 6))
            self.assertTrue(pump_events(self.app, lambda: rows))
            self.assertEqual(coordinator.delivered_revision, 6)
            self.assertEqual(rows, [[{"stale": True}]])

            # 同一版本号不再查。
            self.assertFalse(coordinator.tick(True, 6))
        finally:
            coordinator.shutdown()


class WriteLockResponsivenessTests(unittest.TestCase):
    """要求 2：后台持有数据库写锁时，UI 心跳与用户取消仍必须立即返回。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if not QFontDatabase.families():
            for name in ("segoeui.ttf", "seguisb.ttf", "msyh.ttc", "msyhbd.ttc"):
                font_path = Path("C:/Windows/Fonts") / name
                if font_path.exists():
                    QFontDatabase.addApplicationFont(str(font_path))
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.environment.start()
        self.window = MainWindow(Path(__file__).resolve().parents[1])
        ready_deadline = time.monotonic() + 5
        while not self.window._recognition_ready and time.monotonic() < ready_deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(self.window._recognition_ready)
        self.window.timer.stop()
        self.window.health_timer.stop()
        self.window.show()
        self.app.processEvents()
        self.window.timer.stop()
        self.window.health_timer.stop()

    def tearDown(self):
        self.window.exit_app()
        pump_events(self.app, lambda: self.window._shutdown_done)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.environment.stop()
        self.temp.cleanup()

    def test_heartbeat_and_cancel_stay_responsive_while_write_lock_is_held(self):
        holding = threading.Event()
        release = threading.Event()

        def hold_write_lock() -> None:
            # 模拟服务线程正在一段写事务里（真实实现会在同一把锁内 BEGIN/COMMIT）。
            with self.window.storage._lock:
                holding.set()
                release.wait(10)

        thread = threading.Thread(target=hold_write_lock, name="fake-db-writer", daemon=True)
        thread.start()
        self.assertTrue(holding.wait(5), "后台写锁没有成功持有")
        try:
            started = time.monotonic()
            for _ in range(5):
                self.window.refresh_runtime_views()
            self.assertLess(
                time.monotonic() - started, 1.0,
                "数据库写锁被后台持有时，UI 心跳被阻塞",
            )
            # 版本号读取本身也不得等事务锁。
            self.assertIsInstance(self.window.storage.records_revision, int)
            self.assertIsInstance(self.window.recognition.records_revision, int)

            class CancelDialog:
                def __init__(self, *_args, **_kwargs) -> None:
                    pass

                def exec(self) -> int:
                    return QDialog.DialogCode.Rejected

            started = time.monotonic()
            with patch("deal_watch.ui.main.RegionEditDialog", CancelDialog):
                self.window.add_region()
            self.assertLess(
                time.monotonic() - started, 1.0,
                "数据库写锁被后台持有时，用户取消操作被阻塞",
            )
            self.assertFalse(self.window._saving)
            self.assertEqual(self.window.config.regions, [])
        finally:
            release.set()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
