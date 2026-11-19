"""QA 独立验证（0.2.2 阶段一 C 组）：采集请求预算、故障分类与阶段日志。

和工程师自测的差别：

* 预算用**真实子进程**证明（含“孙进程握着管道写端”的 W2 场景），不是伪造
  ``TimeoutExpired``；
* ``capture_wgc`` 的会话生命周期用**假 ``windows_capture`` 模块**驱动，
  覆盖“等帧超时 / 会话退出卡住 / 会话已关闭 / 拿到帧但退不出”四种形态；
* 状态机与界面断言使用**真实的** ``MainWindow.on_health``，不复制一份规则；
* 日志零泄漏用“带中文商品文案的假阶段值”直接撞白名单。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from deal_watch.capture import CaptureError, WindowInfo
from deal_watch.capture_worker import StageChannel, capture_wgc, stop_session
from deal_watch.config import AppConfig
from deal_watch.diagnostics import (
    STAGE_FIELD_ALLOWLIST,
    log_capture_stage,
    sanitize_stage_fields,
)
from deal_watch.models import (
    CaptureErrorCode,
    FrameSample,
    HealthSnapshot,
    OcrObservation,
    Rect,
    RegionStatus,
    TextBlock,
)
from deal_watch.service import _PAUSE_DETAILS, DIAGNOSTIC_CAPTURE_TIMEOUT_MS, MonitorService
from deal_watch.storage import Storage
from deal_watch.window_capture import (
    CAPTURE_KILL_GRACE_SECONDS,
    START_SUPERVISOR,
    CaptureTrace,
    WindowCaptureClient,
    WindowCaptureError,
    WindowFrameCapturer,
)
from region_fixtures import ready_region
from stage_log_fixture import StageLogCapture, find_lines, parse_line

REPO_ROOT = Path(__file__).resolve().parents[1]
CJK = tuple(chr(code) for code in range(0x4E00, 0x9FFF))


class FakeOcr:
    """记录调用次数的假 OCR：超时后必须一次都不被调用。"""

    def __init__(self) -> None:
        self.frames: list[str] = []

    def recognize(self, frame):
        self.frames.append(frame.region_id)
        return OcrObservation(
            region_id=frame.region_id,
            sampled_at=frame.captured_at,
            frame_sequence=frame.sequence,
            blocks=[TextBlock("普通消息", 0.99, Rect(10, 10, 200, 25))],
            inference_ms=1.0,
        )


class HungTraceMixin:
    @staticmethod
    def hung_trace(region_id: str, request_id: str = "3f9c1d2a") -> CaptureTrace:
        return CaptureTrace(
            request_id=request_id,
            region_id=region_id,
            popen_ms=12.0,
            wait_ms=4980.0,
            kill_ms=8.0,
            total_ms=5003.0,
            stage_failed="session_exit",
            stages={"wgc_start": 18.2},
        )


# ---------------------------------------------------------------------------
# 采集组件会话生命周期（子进程侧 W3/W4）
# ---------------------------------------------------------------------------


class _Control:
    def __init__(self, wait_behaviour) -> None:
        self._wait_behaviour = wait_behaviour
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True

    def wait(self) -> None:
        self._wait_behaviour()


class _Frame:
    def __init__(self, height: int = 16, width: int = 12) -> None:
        self.width = width
        self.height = height

    def convert_to_bgr(self):
        parent = self

        class _Bgr:
            frame_buffer = np.full((parent.height, parent.width, 3), 90, dtype=np.uint8)

        return _Bgr()


class _FakeCapture:
    """``windows_capture.WindowsCapture`` 替身：按场景触发回调。"""

    def __init__(self, *, emit_frame: bool, emit_closed: bool, wait_behaviour, **kwargs) -> None:
        self._emit_frame = emit_frame
        self._emit_closed = emit_closed
        self._wait_behaviour = wait_behaviour
        self._handlers: dict[str, object] = {}

    def event(self, callback):
        self._handlers[callback.__name__] = callback
        return callback

    def start_free_threaded(self):
        control = _Control(self._wait_behaviour)
        if self._emit_closed:
            self._handlers["on_closed"]()
        if self._emit_frame:
            self._handlers["on_frame_arrived"](_Frame(), control)
        return control


def fake_windows_capture(**options) -> types.ModuleType:
    module = types.ModuleType("windows_capture")

    def factory(**_kwargs):
        return _FakeCapture(**options)

    module.WindowsCapture = factory
    return module


def run_capture_wgc(options: dict, *, timeout: float = 0.25, exit_timeout: float = 0.15):
    """注入假模块后调用真实的 ``capture_wgc``，返回（耗时, 异常, 阶段行）。"""
    module = fake_windows_capture(**options)
    channel = StageChannel()
    started = time.monotonic()
    try:
        with patch.dict(sys.modules, {"windows_capture": module}):
            result = capture_wgc(0x1234, timeout=timeout, session_exit_timeout=exit_timeout,
                                 channel=channel)
        error: BaseException | None = None
    except BaseException as exception:  # noqa: BLE001 - 用例要按类型断言
        result = None
        error = exception
    return time.monotonic() - started, result, error, channel


class CaptureWorkerSessionTests(unittest.TestCase):
    """C 组 13：组件启动慢 / 无帧 / 会话退出卡住 / 迟到结果 四类形态可区分。"""

    @staticmethod
    def block_forever() -> None:
        threading.Event().wait(30)

    def test_session_exit_hang_is_bounded_and_reported(self):
        """W3：原生线程退出卡死时，子进程自己必须先报“会话退出超时”。"""
        elapsed, result, error, channel = run_capture_wgc(
            {"emit_frame": False, "emit_closed": False, "wait_behaviour": self.block_forever}
        )
        self.assertIsNone(result)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("WGC_SESSION_EXIT_TIMEOUT", str(error))
        self.assertLess(elapsed, 1.5, f"会话退出卡住时用了 {elapsed:.2f} 秒")
        self.assertIn("session_stop_ms", channel.stages)
        self.assertIn("session_exit_ms", channel.stages)

    def test_no_frame_with_healthy_exit_reports_frame_timeout(self):
        elapsed, result, error, channel = run_capture_wgc(
            {"emit_frame": False, "emit_closed": False, "wait_behaviour": lambda: None}
        )
        self.assertIsInstance(error, TimeoutError)
        self.assertIn("WGC_FRAME_TIMEOUT", str(error))
        self.assertLess(elapsed, 0.6)
        self.assertNotIn("first_valid_frame_ms", channel.stages, "没有帧却打了首帧阶段")

    def test_session_closed_is_not_confused_with_frame_timeout(self):
        _, _, error, _ = run_capture_wgc(
            {"emit_frame": False, "emit_closed": True, "wait_behaviour": lambda: None}
        )
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("WGC_SESSION_CLOSED", str(error))

    def test_frame_arrived_but_session_cannot_exit_still_reports_hang(self):
        """拿到帧但退不出：结果必须作废，不能拿旧帧冒充成功。"""
        elapsed, result, error, _ = run_capture_wgc(
            {"emit_frame": True, "emit_closed": False, "wait_behaviour": self.block_forever}
        )
        self.assertIsNone(result, "会话退出卡住时仍返回了帧，等于用旧帧冒充成功")
        self.assertIn("WGC_SESSION_EXIT_TIMEOUT", str(error))
        self.assertLess(elapsed, 1.5)

    def test_stop_session_never_blocks_longer_than_the_budget(self):
        """C 组 13：``CaptureControl.wait()`` 没有 timeout，必须由 daemon 线程兜住。"""
        hanging = _Control(self.block_forever)
        started = time.monotonic()
        self.assertFalse(stop_session(hanging, 0.2))
        self.assertLess(time.monotonic() - started, 0.6)
        self.assertTrue(hanging.stopped)
        quick = _Control(lambda: None)
        self.assertTrue(stop_session(quick, 0.2))


# ---------------------------------------------------------------------------
# 父进程预算（真实子进程）
# ---------------------------------------------------------------------------


class RealChildClient(WindowCaptureClient):
    """把采集组件换成一段真实 python 脚本：仍然走 Popen + 双读线程 + 预算。"""

    def __init__(self, script: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.script = script

    def _command(self) -> list[str]:
        return [sys.executable, "-c", self.script]


class ParentBudgetTests(unittest.TestCase):
    """C 组 14：整条请求墙钟 ≤ 5.0s + 0.5s 宽限；不产生孤儿组件。"""

    def test_six_second_child_is_abandoned_within_budget_and_reaped(self):
        client = RealChildClient("import time; time.sleep(8)", timeout=5.0)
        started = time.monotonic()
        with self.assertRaises(WindowCaptureError) as context:
            client.capture(0x1234, "wgc", request_id="abcdef01", region_id="r1")
        elapsed = time.monotonic() - started
        self.assertLessEqual(elapsed, 5.0 + CAPTURE_KILL_GRACE_SECONDS + 0.1, f"{elapsed:.2f}s")
        self.assertEqual(context.exception.code, CaptureErrorCode.CAPTURE_TIMEOUT)
        trace = context.exception.trace
        self.assertFalse(trace.orphan_suspected)
        self.assertIsNotNone(trace.exit_code, "子进程没有被回收，可能留下孤儿组件")
        # 5.0 秒总预算里预留 0.5 秒收尾，等待段因此约 4.5 秒。
        self.assertGreaterEqual(trace.wait_ms, 4400.0)
        self.assertLessEqual(trace.total_ms, 5600.0)

    def test_grandchild_holding_the_pipe_cannot_extend_the_budget(self):
        """W2：孙进程继承 stdout/stderr 写端时，收尾也不得无界等待 EOF。"""
        script = (
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'])\n"
            "time.sleep(30)\n"
        )
        client = RealChildClient(script, timeout=1.0)
        started = time.monotonic()
        with self.assertRaises(WindowCaptureError) as context:
            client.capture(0x1234, "wgc", request_id="abcdef02")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0, f"孙进程握着管道时父进程等了 {elapsed:.2f} 秒")
        self.assertEqual(context.exception.code, CaptureErrorCode.CAPTURE_TIMEOUT)
        self.assertFalse(context.exception.trace.orphan_suspected)

    def test_late_result_after_the_deadline_is_discarded(self):
        """迟到结果：超时后子进程才打印的 payload 绝不能变成成功帧。"""
        script = (
            "import json, time\n"
            "time.sleep(1.2)\n"
            "print(json.dumps({'ok': True, 'width': 1, 'height': 1, 'image': ''}))\n"
        )
        client = RealChildClient(script, timeout=0.6)
        with self.assertRaises(WindowCaptureError) as context:
            client.capture(0x1234, "wgc", request_id="abcdef03")
        self.assertEqual(context.exception.code, CaptureErrorCode.CAPTURE_TIMEOUT)
        self.assertEqual(context.exception.trace.request_id, "abcdef03")

    def test_stage_lines_survive_the_kill_and_name_the_stuck_stage(self):
        script = (
            "import json, sys, time\n"
            "print(json.dumps({'stage': 'wgc_start', 't_ms': 12.0}), file=sys.stderr, flush=True)\n"
            "print(json.dumps({'stage': 'first_valid_frame', 't_ms': 88.0}), "
            "file=sys.stderr, flush=True)\n"
            "time.sleep(30)\n"
        )
        client = RealChildClient(script, timeout=0.8)
        with self.assertRaises(WindowCaptureError) as context:
            client.capture(0x1234, "wgc", request_id="abcdef04", region_id="r9")
        trace = context.exception.trace
        self.assertEqual(trace.stage_failed, "first_valid_frame")
        self.assertEqual(trace.stages, {"wgc_start": 12.0, "first_valid_frame": 88.0})

    def test_slow_component_start_burns_the_budget_and_is_named_as_such(self):
        """W1：CreateProcess 自己吃掉预算时，不能报成“等帧太短”。"""
        class Result:
            returncode = 0
            stdout = None
            stderr = None
            killed = False
            waits = 0

            def kill(self) -> None:
                self.killed = True

            def wait(self, timeout=None):
                self.waits += 1
                return 0

        client = WindowCaptureClient(timeout=0.2)
        result = Result()

        def slow_spawn(*_args, **_kwargs):
            time.sleep(0.3)
            return result

        started = time.monotonic()
        with patch("deal_watch.window_capture.spawn", side_effect=slow_spawn):
            with self.assertRaises(WindowCaptureError) as context:
                client.capture(0x1234, "wgc", request_id="abcdef07")
            elapsed = time.monotonic() - started
            self.assertTrue(START_SUPERVISOR.wait_idle(3.0), "监督任务没有收尾")
        self.assertLess(elapsed, 1.0)
        self.assertEqual(context.exception.code, CaptureErrorCode.CAPTURE_POPEN_TIMEOUT)
        self.assertEqual(context.exception.trace.stage_failed, "component_start")
        # 启动段被预算截断（0.2 - 0.02 收尾预留），而不是傻等 CreateProcess。
        self.assertGreaterEqual(context.exception.trace.popen_ms, 100.0)
        self.assertLess(context.exception.trace.popen_ms, 250.0)
        self.assertTrue(result.killed, "迟到启动的子进程没有被监督任务回收")

    def test_unreapable_child_is_flagged_as_orphan_instead_of_waiting_forever(self):
        class Stubborn:
            def __init__(self) -> None:
                self.killed = False
                self.returncode = None
                import io
                self.stdout = io.StringIO("")
                self.stderr = io.StringIO("")

            def kill(self) -> None:
                self.killed = True

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                import subprocess

                raise subprocess.TimeoutExpired(cmd=["worker"], timeout=timeout or 0)

        stubborn = Stubborn()
        from deal_watch.window_capture import PROCESS_REGISTRY
        active_before = PROCESS_REGISTRY.active

        def release_fake_orphan():
            stubborn.returncode = -9
            deadline = time.monotonic() + 2
            while PROCESS_REGISTRY.active > active_before and time.monotonic() < deadline:
                time.sleep(0.01)

        self.addCleanup(release_fake_orphan)
        client = WindowCaptureClient(timeout=0.1)
        started = time.monotonic()
        with patch("deal_watch.window_capture.spawn", return_value=stubborn):
            with self.assertRaises(WindowCaptureError) as context:
                client.capture(0x1234, "wgc", request_id="abcdef05")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertTrue(stubborn.killed)
        self.assertEqual(context.exception.code, CaptureErrorCode.CAPTURE_ORPHAN)
        self.assertTrue(context.exception.trace.orphan_suspected)



# ---------------------------------------------------------------------------
# 服务层：作废结果、故障分类、同轮继续、阶段日志
# ---------------------------------------------------------------------------


class FakeWindows:
    def __init__(self, window: WindowInfo) -> None:
        self.window = window

    def desktop_locked(self) -> bool:
        return False

    def window_from_hwnd(self, hwnd: int):
        return self.window if hwnd == self.window.hwnd else None

    def list_windows(self):
        return [self.window]


def make_window_info(region) -> WindowInfo:
    return WindowInfo(
        hwnd=region.window_hwnd,
        title=region.window_title,
        class_name=region.window_class,
        rect=Rect(0, 0, region.reference_frame_width, region.reference_frame_height),
        process_id=region.process_id,
        process_path=region.process_path,
    )


class ServiceFailureTests(HungTraceMixin, unittest.TestCase):
    """C 组 15-18：超时后作废结果、分类为故障、其他区域继续、不重复通知。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def build_service(self, regions, capturer, ocr, *, poll_ms=100):
        config = AppConfig(
            regions=regions, keywords=["手机"], save_hit_images=False, poll_interval_ms=poll_ms
        )
        storage = Storage(self.directory / "qa.db")
        service = MonitorService(config, storage, ocr, capturer=capturer)
        return service, storage

    def test_timeout_discards_frame_and_never_submits_ocr(self):
        region = ready_region("超时群", window_title="微信", rect=Rect(0, 0, 300, 500))
        ocr = FakeOcr()
        snapshots: list[HealthSnapshot] = []

        class HangingCapturer:
            last_trace = None

            def reset(self, region_id):
                pass

            def capture(self, source, force=False):
                self.last_trace = HungTraceMixin.hung_trace(source.id)
                raise CaptureError(
                    CaptureErrorCode.CAPTURE_SESSION_HUNG, "窗口采集组件超过 5.0 秒未返回"
                )

        service, storage = self.build_service([region], HangingCapturer(), ocr)
        service.on_health = snapshots.append
        try:
            service.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if any(item.status is RegionStatus.ERROR for item in snapshots):
                    break
                time.sleep(0.02)
            self.assertEqual(ocr.frames, [], "超时后仍然把帧交给了 OCR")
            self.assertEqual(service._pending, {}, "超时后仍有待处理帧")
            self.assertEqual(service._runtime[region.id].status, RegionStatus.ERROR)
            detail = service._runtime[region.id].detail
            self.assertIn("请求 3f9c1d2a", detail)
            self.assertIn("卡在 session_exit", detail)
            self.assertIn("capture_session_hung", detail)
            self.assertNotIn("已暂停", detail)
        finally:
            service.stop()
            storage.close()

    def test_failed_region_is_not_retried_this_round_and_others_continue(self):
        bad = ready_region("坏区域", window_title="微信", rect=Rect(0, 0, 300, 500))
        good = ready_region("好区域", window_title="微信", rect=Rect(0, 0, 300, 500))
        ocr = FakeOcr()

        class MixedCapturer:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.last_trace = None

            def reset(self, region_id):
                pass

            def capture(self, source, force=False):
                self.calls.append(source.id)
                if source.id == bad.id:
                    self.last_trace = HungTraceMixin.hung_trace(source.id, "bad00001")
                    raise CaptureError(CaptureErrorCode.CAPTURE_POPEN_TIMEOUT, "启动组件超预算")
                return FrameSample(source.id, 1, time.time(), np.zeros((500, 300, 3), np.uint8),
                                   source.rect)

        capturer = MixedCapturer()
        service, storage = self.build_service([bad, good], capturer, ocr, poll_ms=60000)
        try:
            service.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if service._runtime[good.id].status is RegionStatus.RUNNING:
                    break
                time.sleep(0.02)
            self.assertEqual(service._runtime[bad.id].status, RegionStatus.ERROR)
            self.assertEqual(service._runtime[good.id].status, RegionStatus.RUNNING)
            self.assertEqual(ocr.frames, [good.id], "故障区域阻塞了同轮的其他区域")
            # 本轮不重试：失败区域的下一轮时刻必须是“计划时刻 + 采样间隔”。
            remaining = service._next_due[bad.id] - time.monotonic()
            self.assertGreater(remaining, 50.0, f"故障区域被提前重试：{remaining:.1f}s")
            self.assertEqual(capturer.calls.count(bad.id), 1)
        finally:
            service.stop()
            storage.close()

    def test_new_fault_codes_never_take_the_pause_branch(self):
        """C 组 18：三个新错误码一旦落入 `_PAUSE_DETAILS`，超时会被当成“已暂停”。"""
        new_codes = (
            CaptureErrorCode.CAPTURE_SESSION_HUNG,
            CaptureErrorCode.CAPTURE_POPEN_TIMEOUT,
            CaptureErrorCode.CAPTURE_ORPHAN,
        )
        for code in new_codes:
            with self.subTest(code=code):
                self.assertNotIn(code, _PAUSE_DETAILS)
        self.assertEqual(set(_PAUSE_DETAILS), {
            CaptureErrorCode.WINDOW_MINIMIZED,
            CaptureErrorCode.DESKTOP_LOCKED,
        })
        region = ready_region("暂停群", window_title="微信", rect=Rect(0, 0, 300, 500))
        for code in new_codes:
            with self.subTest(behaviour=code):
                snapshots: list[HealthSnapshot] = []

                class FailingCapturer:
                    last_trace = None

                    def __init__(self, code=code):
                        self.code = code

                    def reset(self, region_id):
                        pass

                    def capture(self, source, force=False):
                        raise CaptureError(self.code, "模拟采集故障")

                service, storage = self.build_service([region], FailingCapturer(), FakeOcr(),
                                                     poll_ms=60000)
                service.on_health = snapshots.append
                try:
                    service.start()
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and not snapshots:
                        time.sleep(0.02)
                    statuses = {item.status for item in snapshots}
                    self.assertIn(RegionStatus.ERROR, statuses, f"{code} 没有进入故障状态")
                    self.assertNotIn(RegionStatus.PAUSED, statuses, f"{code} 被当成了暂停")
                finally:
                    service.stop()
                    storage.close()

    def test_ui_shows_fault_once_and_recovery_once_without_history_replay(self):
        """C 组 15/17：界面不得显示正常监控；故障与恢复各只通知一次。"""
        os_environ = patch.dict("os.environ", {"LOCALAPPDATA": self.temp.name})
        os_environ.start()
        try:
            from PySide6.QtWidgets import QApplication

            application = QApplication.instance() or QApplication([])
            application.setQuitOnLastWindowClosed(False)
            from deal_watch.ui.main import MainWindow

            region = ready_region("故障群", window_title="微信", rect=Rect(0, 0, 300, 500))
            window = MainWindow(REPO_ROOT)
            try:
                window.config.regions = [region]
                window.config_store.save(window.config)
                window._load_regions()
                window.service = object()  # 只需真值，on_health 不会调用它的方法
                messages: list[tuple[str, str]] = []
                with patch.object(window, "queue_system_message",
                                  side_effect=lambda key, text, **kwargs: messages.append((key, text))):
                    detail = "窗口采集组件超过 5.0 秒未返回（请求 3f9c1d2a｜卡在 session_exit）"
                    for _ in range(3):  # 持续故障连报三次
                        window.on_health(
                            HealthSnapshot(region.id, RegionStatus.ERROR, detail)
                        )
                    self.assertEqual(
                        [key for key, _ in messages], ["监控故障"], "持续故障被重复通知"
                    )
                    self.assertTrue(
                        window.region_table.item(0, 1).text().startswith("监控故障"),
                        f"故障期间表格仍显示：{window.region_table.item(0, 1).text()}",
                    )
                    self.assertIn("异常", window.tray_summary())
                    self.assertNotIn("监控中", window.tray_summary())
                    # 恢复：BASELINING 不通知，RUNNING 只通知一次。
                    window.on_health(
                        HealthSnapshot(region.id, RegionStatus.BASELINING, "恢复后重新建立基线")
                    )
                    window.on_health(HealthSnapshot(region.id, RegionStatus.RUNNING, "监控中"))
                    window.on_health(HealthSnapshot(region.id, RegionStatus.RUNNING, "监控中"))
                    self.assertEqual(
                        [key for key, _ in messages], ["监控故障", "监控恢复"], "恢复通知次数不对"
                    )
                    self.assertIn("不补发", messages[-1][1])
                    self.assertIn("监控中", window.tray_summary())
            finally:
                window.service = None  # 让 exit_app 走正常停止路径，不调用假对象的方法
                window.exit_app()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not window._shutdown_done:
                    application.processEvents()
                    time.sleep(0.01)
                window.deleteLater()
                application.processEvents()
        finally:
            os_environ.stop()


class StageLogSegmentTests(unittest.TestCase):
    """C 组 19：某区域真卡 4 秒时，阶段日志要能读出整条请求的分段与参数。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_four_second_stall_leaves_readable_segments_with_one_request_id(self):
        region = ready_region("卡死群", window_title="微信", rect=Rect(0, 0, 300, 500))
        client = RealChildClient("import time; time.sleep(30)", timeout=4.0)
        capturer = WindowFrameCapturer(client=client, windows=FakeWindows(make_window_info(region)))
        config = AppConfig(
            regions=[region], keywords=["手机"], save_hit_images=False, poll_interval_ms=100
        )
        storage = Storage(self.directory / "stall.db")
        service = MonitorService(config, storage, FakeOcr(), capturer=capturer)
        try:
            with StageLogCapture() as messages:
                service.start()
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if find_lines(messages, "fault"):
                        break
                    time.sleep(0.05)
                service.stop()
        finally:
            storage.close()
        faults = [
            line for line in find_lines(messages, "fault") if line.get("region_id") == region.id
        ]
        self.assertTrue(faults, f"没有本区域的 fault 阶段行：{messages}")
        request_id = faults[0]["request_id"]
        self.assertTrue(request_id, "fault 行没有 request_id")
        # 同一 request_id 的分段耗时必须都在：kill / request_total 由父进程写。
        totals = [line for line in find_lines(messages, "request_total")
                  if line.get("request_id") == request_id]
        kills = [line for line in find_lines(messages, "kill")
                 if line.get("request_id") == request_id]
        self.assertTrue(totals, f"同一个 request_id 没有 request_total 行：{messages}")
        self.assertTrue(kills, f"同一个 request_id 没有 kill 行：{messages}")
        self.assertEqual(faults[0]["error_code"], CaptureErrorCode.CAPTURE_TIMEOUT.value)
        # 预算与生产参数必须能直接从 cycle 行读出来（P2/P3 的判据）。
        # 只取本次用例自己的预算值，避免其它用例残留线程写进同一个全局 logger。
        cycles = [
            line for line in find_lines(messages, "cycle")
            if line.get("capture_timeout_ms") == "4000"
        ]
        self.assertTrue(cycles, f"没有本次预算的 cycle 行：{messages}")
        cycle = cycles[0]
        self.assertEqual(cycle["interval_ms"], "100")
        self.assertIn("schedule_wait_ms", cycle)
        self.assertIn("in_flight", cycle)
        self.assertIn("wgc_timeout_ms", cycle)


class StageLogLeakageTests(unittest.TestCase):
    """C 组 20：阶段日志零泄漏（图像 / 聊天正文 / 标题原文 / 凭据）。"""

    def assert_no_user_content(self, text: str) -> None:
        for char in CJK:
            if char in text:
                self.fail(f"阶段日志出现中文字符 {char!r}：{text}")
        for token in ("hook/", "open-apis", "iVBORw0KGgo"):
            self.assertNotIn(token, text, f"阶段日志出现疑似凭据/图像内容：{text}")

    def test_hostile_values_are_stripped_or_dropped(self):
        cleaned = sanitize_stage_fields(
            {
                "stage": "session_exit",
                "request_id": "群名(3)",
                "region_id": "region-1",
                "outcome": "订单 4999 元 手机 iPhone15",
                "error_code": "capture_timeout",
                "elapsed_ms": "4999 元",
                # 不在白名单：图像、聊天正文、窗口标题、凭据
                "image": "iVBORw0KGgo=",
                "text": "手机 优惠 4999 元",
                "window_title": "某某福利群(12)",
                "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/secret",
            }
        )
        self.assertTrue(set(cleaned) <= set(STAGE_FIELD_ALLOWLIST))
        for dropped in ("image", "text", "window_title", "webhook"):
            self.assertNotIn(dropped, cleaned, f"{dropped} 也被写进了阶段日志")
        self.assertNotIn("elapsed_ms", cleaned, "字符串数字被当成了诊断数值")
        # 中文（聊天正文/窗口标题）必须被剔除，标识符字符保留。
        self.assert_no_user_content(json.dumps(cleaned, ensure_ascii=False))
        self.assertNotIn("群名", str(cleaned))
        self.assertNotIn("福利群", str(cleaned))

    def test_stage_log_written_by_a_real_failing_child_has_no_chat_content(self):
        hostile_script = (
            "import json, sys, time\n"
            "print(json.dumps({'stage': '聊天正文', 't_ms': 1.0}), file=sys.stderr, flush=True)\n"
            "print(json.dumps({'stage': 'wgc_start', 't_ms': 2.0, 'request_id': '群名(3)'}), "
            "file=sys.stderr, flush=True)\n"
            "time.sleep(30)\n"
        )
        client = RealChildClient(hostile_script, timeout=0.6)
        with StageLogCapture() as messages:
            with self.assertRaises(WindowCaptureError):
                client.capture(0x1234, "wgc", request_id="abcdef06", region_id="r7")
        joined = "\n".join(messages)
        self.assertIn("stage=child.wgc_start", joined)
        self.assertNotIn("聊天正文", joined)
        self.assert_no_user_content(joined)
        for message in messages:
            fields = parse_line(message)
            self.assertTrue(set(fields) <= set(STAGE_FIELD_ALLOWLIST), message)
            self.assertIn("stage", fields)

    def test_log_capture_stage_refuses_a_line_without_stage(self):
        import logging

        logger = logging.getLogger("deal_watch.capture_stages")
        logger.addHandler(logging.NullHandler())
        self.assertEqual(log_capture_stage(logger, request_id="abcdef01"), {})


class ProductionConstantTests(unittest.TestCase):
    """D 组 22：5.0 / 3.0 超时保持，默认采样30秒。"""

    def test_budgets_and_interval_are_locked(self):
        import inspect

        self.assertEqual(WindowCaptureClient().timeout, 5.0)
        default = inspect.signature(capture_wgc).parameters["timeout"].default
        self.assertEqual(default, 3.0)
        self.assertEqual(AppConfig().poll_interval_ms, 30000)
        self.assertEqual(DIAGNOSTIC_CAPTURE_TIMEOUT_MS, 5000.0)
        # 会话退出上限是新增参数，不得放宽到秒级。
        self.assertLessEqual(WindowCaptureClient(session_exit_timeout_ms=5000)
                             .session_exit_timeout_ms, 1000)


if __name__ == "__main__":
    unittest.main()
