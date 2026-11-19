"""0.2.1 收尾回归：区域就绪门禁、故障事件化、托盘汇总。

全部为离线用例：合成区域、合成窗口、合成采集器，不读取真实聊天截图，
不启动真实探针，也不访问真实桌面。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from deal_watch.config import AppConfig  # noqa: E402
from deal_watch.models import (  # noqa: E402
    HealthSnapshot,
    OcrObservation,
    Rect,
    RegionConfig,
    RegionStatus,
    TextBlock,
)
from deal_watch.readiness import (  # noqa: E402
    readiness_detail,
    region_readiness_problems,
)
from deal_watch.service import MonitorService  # noqa: E402
from deal_watch.storage import Storage  # noqa: E402
from region_fixtures import ready_region  # noqa: E402


class _IdleOcr:
    """没有采集帧时不会被调用；一旦被调用即返回空观察。"""

    def recognize(self, frame):
        return OcrObservation(
            region_id=frame.region_id,
            sampled_at=frame.captured_at,
            frame_sequence=frame.sequence,
            blocks=[
                TextBlock(text, 0.99, Rect(10, 10 + index * 60, 250, 25))
                for index, text in enumerate(frame.image)
            ],
            inference_ms=1.0,
        )


class _RecordingCapturer:
    """记录每个区域被采集的次数；不产生帧，避免触发 OCR。"""

    def __init__(self) -> None:
        self.captured: dict[str, int] = {}

    def capture(self, region, force: bool = False):
        self.captured[region.id] = self.captured.get(region.id, 0) + 1
        return None

    def reset(self, region_id: str) -> None:
        return None


class ReadinessFunctionTests(unittest.TestCase):
    """门禁是纯函数：逐项覆盖，避免界面与服务两套口径。"""

    def test_ready_region_has_no_problems(self) -> None:
        self.assertEqual(region_readiness_problems(ready_region()), [])

    def test_unconfirmed_and_legacy_regions_report_chinese_reasons(self) -> None:
        legacy = ready_region(previewed=False)
        legacy.needs_calibration = True
        legacy.window_hwnd = 0
        legacy.coordinate_space = "desktop"
        legacy.capture_backend = "unknown"
        legacy.rect = Rect(0, 0, 10, 10)
        legacy.reference_frame_width = 0
        legacy.reference_frame_height = 0
        problems = region_readiness_problems(legacy)
        joined = "；".join(problems)
        for expected in (
            "需重新圈选",
            "尚未检查确认当前可见内容",
            "未绑定窗口句柄",
            "坐标模式不是采集帧相对坐标",
            "未知采集后端",
            "圈选区域过小",
            "未记录参考帧尺寸",
        ):
            self.assertIn(expected, joined)
        self.assertIn("请重新圈选或预览确认", readiness_detail(problems))

    def test_identity_mismatch_is_reported_when_windows_available(self) -> None:
        class Window:
            hwnd = 1001
            title = "改了标题的群"
            class_name = "WeChatMainWndForPC"
            process_id = 9999
            process_path = "C:/Other/Other.exe"

        class Windows:
            def window_from_hwnd(self, hwnd):
                return Window() if hwnd == 1001 else None

        problems = region_readiness_problems(ready_region(), Windows())
        joined = "；".join(problems)
        self.assertIn("PID 不一致", joined)
        self.assertIn("可执行文件路径不一致", joined)
        self.assertIn("窗口标题身份已变化", joined)

    def test_missing_window_is_reported(self) -> None:
        class Windows:
            def window_from_hwnd(self, hwnd):
                return None

        problems = region_readiness_problems(ready_region(), Windows())
        self.assertIn("绑定窗口已不存在或已关闭", "；".join(problems))


class ServiceReadinessGateTests(unittest.TestCase):
    """未就绪区域在 start 与 resume 两条路径上都必须被挡住。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "gates.db")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    def test_start_never_captures_unconfirmed_region(self) -> None:
        confirmed = ready_region("已确认群", rect=Rect(0, 0, 300, 500))
        unconfirmed = ready_region("未确认群", rect=Rect(0, 0, 300, 500), previewed=False)
        config = AppConfig(
            regions=[confirmed, unconfirmed],
            keywords=[],
            save_hit_images=False,
            poll_interval_ms=100,
        )
        capturer = _RecordingCapturer()
        health: list[HealthSnapshot] = []
        service = MonitorService(
            config, self.storage, _IdleOcr(), capturer=capturer, on_health=health.append
        )
        service.start()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not capturer.captured.get(confirmed.id):
                time.sleep(0.02)
            self.assertTrue(capturer.captured.get(confirmed.id), "已确认区域没有进入采集")
            time.sleep(0.2)
            self.assertNotIn(unconfirmed.id, capturer.captured, "未确认区域被采集了")
            self.assertNotIn(unconfirmed.id, service._regions)
            runtime = service._runtime[unconfirmed.id]
            self.assertIs(runtime.status, RegionStatus.PAUSED)
            self.assertFalse(runtime.failed)
            self.assertIn("区域未就绪", runtime.detail)
            self.assertIn("尚未检查确认当前可见内容", runtime.detail)
            # 未就绪不是故障：健康流里不得出现 ERROR。
            self.assertFalse(
                [
                    snapshot
                    for snapshot in health
                    if snapshot.region_id == unconfirmed.id
                    and snapshot.status is RegionStatus.ERROR
                ]
            )
        finally:
            service.stop()

    def test_resume_unconfirmed_region_stays_paused_and_never_captures(self) -> None:
        unconfirmed = ready_region("未确认群", rect=Rect(0, 0, 300, 500), previewed=False)
        config = AppConfig(
            regions=[unconfirmed], keywords=[], save_hit_images=False, poll_interval_ms=100
        )
        capturer = _RecordingCapturer()
        service = MonitorService(config, self.storage, _IdleOcr(), capturer=capturer)
        service.start()
        try:
            resumed = service.resume_region(unconfirmed.id)
            self.assertFalse(resumed)
            time.sleep(0.2)
            self.assertNotIn(unconfirmed.id, capturer.captured)
            self.assertNotIn(unconfirmed.id, service._regions)
            runtime = service._runtime[unconfirmed.id]
            self.assertIs(runtime.status, RegionStatus.PAUSED)
            self.assertIn("区域未就绪", runtime.detail)
        finally:
            service.stop()

    def test_resume_ready_region_rebaselines_and_returns_true(self) -> None:
        region = ready_region("已确认群", rect=Rect(0, 0, 300, 500))
        config = AppConfig(
            regions=[region], keywords=[], save_hit_images=False, poll_interval_ms=100
        )
        service = MonitorService(config, self.storage, _IdleOcr(), capturer=_RecordingCapturer())
        try:
            self.assertTrue(service.resume_region(region.id))
            runtime = service._runtime[region.id]
            self.assertIs(runtime.status, RegionStatus.BASELINING)
            self.assertIn(region.id, service._regions)
        finally:
            service.stop()

    def test_resume_unknown_region_id_does_not_raise(self) -> None:
        region = ready_region("已确认群", rect=Rect(0, 0, 300, 500))
        config = AppConfig(regions=[region], keywords=[], save_hit_images=False)
        service = MonitorService(config, self.storage, _IdleOcr(), capturer=_RecordingCapturer())
        try:
            self.assertFalse(service.resume_region("不存在的区域"))
            self.assertNotIn("不存在的区域", service._regions)
        finally:
            service.stop()

    def test_start_with_no_ready_region_does_not_crash(self) -> None:
        unready = ready_region("未确认群", rect=Rect(0, 0, 300, 500), previewed=False)
        config = AppConfig(
            regions=[unready], keywords=[], save_hit_images=False, poll_interval_ms=100
        )
        capturer = _RecordingCapturer()
        service = MonitorService(config, self.storage, _IdleOcr(), capturer=capturer)
        service.start()
        try:
            self.assertTrue(service.is_alive(), "全部未就绪时后台线程未能启动")
            time.sleep(0.25)
            self.assertEqual(capturer.captured, {}, "全部未就绪却发生了采集")
            self.assertEqual(service._regions, {})
            self.assertIs(service._runtime[unready.id].status, RegionStatus.PAUSED)
        finally:
            service.stop()
        self.assertFalse(service.is_alive())


class StartupVisibilityTests(unittest.TestCase):
    """缺陷 1（QA 独立复验发现）：启动瞬间被门禁挡下的区域，界面必须显示为暂停。

    走完整链路：真实 ``MonitorService`` + 真实 ``_apply_readiness_gate``（同步发 health）
    → ``MainWindow.on_health`` → 托盘 / 状态栏 / 表格。修复前这两处会被
    ``start_monitoring()`` 末尾的 clear 抹掉，界面显示“监控中”。
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self) -> None:
        from deal_watch.ui.main import MainWindow

        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.environment.start()
        self.window = MainWindow(Path(__file__).resolve().parents[1])
        ready_deadline = time.monotonic() + 5
        while not self.window._recognition_ready and time.monotonic() < ready_deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(self.window._recognition_ready)
        # 预检阶段全部满足（ready_region），真正拦住它的是采集期的窗口身份校验：
        # window_from_hwnd 返回 None → “绑定窗口已不存在或已关闭”。
        self.region = ready_region("合成群", id="r1")
        self.window.config.regions = [self.region]
        self.window.config.keywords = ["手机"]
        self.window.webhook = "http://127.0.0.1:18080/ok"
        self.window._load_regions()

    def tearDown(self) -> None:
        self.window.service = None
        self.window.exit_app()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.window._shutdown_done:
            self.app.processEvents()
            time.sleep(0.01)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.environment.stop()
        self.temp.cleanup()

    def test_blocked_region_at_startup_is_visible_in_tray_and_status_bar(self) -> None:
        class _ClosedWindows:
            def window_from_hwnd(self, hwnd: int):
                return None

        class _ClosedWindowCapturer:
            """模拟“预检后窗口被关掉”：采集器存在，但窗口身份已查不到。"""

            windows = _ClosedWindows()

            def capture(self, region, force: bool = False):
                raise AssertionError("门禁没拦住的区域被采集了")

            def reset(self, region_id: str) -> None:
                return None

        with patch(
            "deal_watch.ui.main.WindowFrameCapturer",
            lambda *args, **kwargs: _ClosedWindowCapturer(),
        ), patch("deal_watch.ui.main.LocalOcr", lambda *args, **kwargs: object()):
            self.window.start_monitoring()

        # 1) 门禁确实没放行：一次采集都不会发生
        self.assertIsNotNone(self.window.service)
        self.assertEqual(self.window.service._regions, {}, "门禁没拦住却进入了采集队列")
        # 2) 状态没有被启动后的清理抹掉
        self.assertEqual(self.window.health_states, {"r1": "paused"})
        self.assertTrue(
            self.window.health_details["r1"].startswith("区域未就绪"), self.window.health_details
        )
        # 3) 托盘不得显示“监控中”
        self.assertEqual(self.window.tray_summary(), "CueScope｜未就绪 1")
        self.assertEqual(self.window.tray.toolTip(), "CueScope｜未就绪 1")
        # 4) 状态栏必须说明“有区域未就绪、不会被采集”，不能只宣称已启动
        message = self.window.statusBar().currentMessage()
        self.assertIn("未就绪", message)
        self.assertIn("不会被采集", message)
        self.assertIn("合成群", message)
        # 5) 表格行同样是暂停 + 原因
        self.assertIn("未就绪", self.window.region_table.item(0, 1).text())


class PreviewConfirmEvidenceTests(unittest.TestCase):
    """缺陷 2（QA 独立复验发现）：零文字块不得放行“确认此区域”。

    走真实 ``PreviewDialog`` 构造 + 真实 ``PreviewWorker``（只把 ``LocalOcr`` 换成
    返回空/非空观察的替身），而不是直接调 ``show_result``。
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def build_dialog(self, block_count: int):
        import numpy as np

        from deal_watch.config import ModelConfig
        from deal_watch.models import FrameSample
        from deal_watch.ui.region import PreviewDialog

        region = RegionConfig(alias="合成群", window_title="合成", rect=Rect(0, 0, 60, 40))
        observation = OcrObservation(
            region_id="r1",
            sampled_at=1.0,
            frame_sequence=1,
            blocks=[
                TextBlock("普通消息", 0.99, Rect(2, 2, 40, 10))
                for _ in range(block_count)
            ],
            uncovered_boxes=[] if block_count else [Rect(0, 0, 60, 40)],
            inference_ms=1.0,
        )

        class _FakeOcr:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def recognize(self, frame):
                return observation

        frame = FrameSample(
            "r1", 1, 1.0, np.zeros((40, 60, 3), dtype=np.uint8), Rect(0, 0, 60, 40)
        )
        # PreviewWorker.run 调 array_to_qimage(frame.image)，所以帧必须是真实 ndarray。
        # 注意：worker.start() 在构造函数里就发生，patch 必须覆盖到线程跑完为止。
        patcher = patch("deal_watch.ui.region.LocalOcr", _FakeOcr)
        patcher.start()
        try:
            dialog = PreviewDialog(
                region, ModelConfig(), Path(__file__).resolve().parents[1],
                [], lambda *args, **kwargs: 0, None, frame=frame, rules=None,
            )
            self.addCleanup(dialog.deleteLater)
            deadline = time.monotonic() + 5
            while dialog.worker.isRunning() and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            while time.monotonic() < deadline and dialog.status.text() == "正在预览识别……":
                self.app.processEvents()
                time.sleep(0.01)
        finally:
            patcher.stop()
        return dialog, region

    def test_confirm_stays_disabled_when_nothing_was_recognized(self) -> None:
        dialog, region = self.build_dialog(0)
        self.assertEqual(dialog._last_block_count, 0)
        self.assertFalse(dialog.confirm_button.isEnabled(), "零文字块仍然可以点确认")
        self.assertIn("未识别到任何文字块", dialog.status.text())
        with patch("deal_watch.ui.region.QMessageBox.warning") as warning, patch(
            "deal_watch.ui.region.QMessageBox.information"
        ) as information:
            dialog.confirm_region()
        warning.assert_called_once()
        information.assert_not_called()
        self.assertFalse(region.previewed, "零文字块却写入了 previewed")

    def test_confirm_enabled_and_sets_previewed_when_text_recognized(self) -> None:
        dialog, region = self.build_dialog(2)
        self.assertEqual(dialog._last_block_count, 2)
        self.assertTrue(dialog.confirm_button.isEnabled())
        self.assertFalse(dialog.send_button.isEnabled(), "无命中时发送按钮不该可用")
        with patch("deal_watch.ui.region.QMessageBox.information"):
            dialog.confirm_region()
        self.assertTrue(region.previewed)


class _FakeWindow:
    def __init__(self, hwnd: int, title: str) -> None:
        self.hwnd = hwnd
        self.title = title
        self.class_name = "WeChatMainWndForPC"
        self.minimized = False
        self.process_id = 4321
        self.process_path = "C:/Program Files/WeChat/WeChat.exe"
        self.rect = Rect(0, 0, 800, 1280)


class _FakeWindows:
    def __init__(self, windows: list[_FakeWindow]) -> None:
        self._windows = windows

    def list_windows(self) -> list[_FakeWindow]:
        return list(self._windows)

    def window_from_hwnd(self, hwnd: int):
        return next((item for item in self._windows if item.hwnd == hwnd), None)


class RegionDialogSaveGateTests(unittest.TestCase):
    """保存按钮必须真的不可点；切换窗口/后端要同时清空参考帧尺寸。"""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self) -> None:
        from PySide6.QtWidgets import QDialog

        self.qdialog = QDialog
        self.windows = [_FakeWindow(101, "群 A"), _FakeWindow(202, "群 B")]
        self.patcher = patch(
            "deal_watch.ui.region.WindowsWindows", return_value=_FakeWindows(self.windows)
        )
        self.patcher.start()
        from deal_watch.ui.region import RegionEditDialog

        self.dialog = RegionEditDialog()
        self.dialog.alias.setText("群 B")
        self.dialog.window_combo.setCurrentIndex(1)

    def tearDown(self) -> None:
        self.patcher.stop()
        self.dialog.deleteLater()
        self.app.processEvents()

    def _complete_selection(self) -> None:
        import numpy as np

        self.dialog._on_selection_completed(
            self.dialog._selection_generation, 202, "wgc", self.windows[1],
            np.full((1280, 800, 3), 245, dtype=np.uint8), 800, 1280, "",
        )
        self.dialog.selector.set_widget_selection(self.dialog.selector._target())

    def test_pending_save_enables_after_selection_but_confirmation_waits_for_preview(self) -> None:
        self.assertFalse(self.dialog.ok_button.isEnabled())
        self._complete_selection()
        self.assertTrue(self.dialog.pending_button.isEnabled())
        self.assertFalse(self.dialog.ok_button.isEnabled())
        self.assertEqual(self.dialog.selection_problems(), [])

    def test_backend_switch_disables_ok_button_and_clears_frame_size(self) -> None:
        self._complete_selection()
        self.assertEqual(self.dialog.selected_frame_size, (800, 1280))
        self.dialog.backend_combo.setCurrentIndex(1)
        self.assertFalse(self.dialog.ok_button.isEnabled(), "切换后端后保存按钮仍可点")
        self.assertIsNone(self.dialog.selected_frame_size, "切换后端后参考帧尺寸未清空")
        self.assertIsNone(self.dialog.selected_rect)

    def test_window_switch_disables_ok_button_and_clears_frame_size(self) -> None:
        self._complete_selection()
        self.dialog.window_combo.setCurrentIndex(0)
        self.assertFalse(self.dialog.ok_button.isEnabled())
        self.assertIsNone(self.dialog.selected_frame_size)

    def test_reselection_after_switch_passes_review_again(self) -> None:
        self._complete_selection()
        self.dialog.backend_combo.setCurrentIndex(1)
        self.assertTrue(self.dialog.selection_problems())
        # 切回 WGC 并重新圈选：复核必须重新变干净。
        self.dialog.backend_combo.setCurrentIndex(0)
        self._complete_selection()
        self.assertEqual(self.dialog.selection_problems(), [])
        self.assertTrue(self.dialog.pending_button.isEnabled())
        self.assertFalse(self.dialog.ok_button.isEnabled())


class TrayAndFaultEventTests(unittest.TestCase):
    """故障事件化与托盘汇总：只在进入/离开故障时各推一次。"""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self) -> None:
        from deal_watch.ui.main import MainWindow

        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.environment.start()
        self.window = MainWindow(Path(__file__).resolve().parents[1])
        ready_deadline = time.monotonic() + 5
        while not self.window._recognition_ready and time.monotonic() < ready_deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(self.window._recognition_ready)
        self.one = ready_region("一群", id="r1")
        self.two = ready_region("二群", id="r2")
        self.window.config.regions = [self.one, self.two]
        self.window._load_regions()
        self.window.service = object()

    def tearDown(self) -> None:
        self.window.service = None
        self.window.exit_app()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.window._shutdown_done:
            self.app.processEvents()
            time.sleep(0.01)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.environment.stop()
        self.temp.cleanup()

    def push(self, region_id: str, status: RegionStatus, detail: str = "合成状态") -> None:
        self.window.on_health(HealthSnapshot(region_id, status, detail))

    def test_repeated_error_with_baselining_between_reports_one_fault(self) -> None:
        with patch.object(self.window, "queue_system_message") as queue:
            for status in (
                RegionStatus.ERROR,
                RegionStatus.BASELINING,
                RegionStatus.ERROR,
                RegionStatus.BASELINING,
                RegionStatus.ERROR,
            ):
                self.push("r1", status, "采集失败")
            self.assertEqual([call.args[0] for call in queue.call_args_list], ["监控故障"])
            self.push("r1", RegionStatus.RUNNING, "监控中")
            self.assertEqual(
                [call.args[0] for call in queue.call_args_list], ["监控故障", "监控恢复"]
            )
            self.push("r1", RegionStatus.RUNNING, "监控中")
            self.assertEqual(
                [call.args[0] for call in queue.call_args_list], ["监控故障", "监控恢复"]
            )

    def test_pause_does_not_end_fault_event_but_running_does(self) -> None:
        with patch.object(self.window, "queue_system_message") as queue:
            self.push("r1", RegionStatus.ERROR, "采集失败")
            self.push("r1", RegionStatus.PAUSED, "已暂停：窗口最小化")
            self.push("r1", RegionStatus.RUNNING, "监控中")
            self.assertEqual(
                [call.args[0] for call in queue.call_args_list], ["监控故障", "监控恢复"]
            )

    def test_disabled_region_ends_fault_without_recovery_message(self) -> None:
        with patch.object(self.window, "queue_system_message") as queue:
            self.push("r1", RegionStatus.ERROR, "采集失败")
            self.push("r1", RegionStatus.DISABLED, "已停用")
            self.assertEqual([call.args[0] for call in queue.call_args_list], ["监控故障"])

    def test_tray_reports_paused_when_every_enabled_region_is_paused(self) -> None:
        self.push("r1", RegionStatus.PAUSED, "已暂停：窗口最小化")
        self.push("r2", RegionStatus.PAUSED, "已暂停：桌面已锁定")
        tooltip = self.window.tray.toolTip()
        self.assertIn("已暂停", tooltip)
        self.assertNotIn("监控中", tooltip)

    def test_tray_reports_fault_count(self) -> None:
        self.push("r1", RegionStatus.ERROR, "采集失败")
        self.push("r2", RegionStatus.RUNNING, "监控中")
        self.assertIn("异常 1", self.window.tray.toolTip())
        self.assertIn("采样 1", self.window.tray.toolTip())

    def test_tray_reports_partial_pause(self) -> None:
        self.push("r1", RegionStatus.RUNNING, "监控中")
        self.push("r2", RegionStatus.PAUSED, "已暂停：桌面已锁定")
        self.assertIn("暂停 1", self.window.tray.toolTip())
        self.assertIn("采样 1", self.window.tray.toolTip())

    def test_tray_treats_unseen_regions_as_preparing(self) -> None:
        # 未收到健康状态的区域既不是暂停，也不能宣称正常监控；必须显示准备中。
        self.push("r1", RegionStatus.PAUSED, "已暂停：窗口最小化")
        self.assertEqual(self.window.tray.toolTip(), "CueScope｜准备 1｜暂停 1")
        self.assertNotIn("监控中", self.window.tray.toolTip())


if __name__ == "__main__":
    unittest.main()
