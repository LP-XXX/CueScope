"""0.2.2 T1.1：全局异常出口、Qt 消息处理和采集阶段日志的白名单。

这些用例只验证“异常必须留痕”和“阶段日志不得含图像/正文/标题原文”。
Qt 消息处理函数**不安装**（offscreen 测试里安装会改变 Qt 的全局行为），
只直接调用返回的处理函数验证它的取舍。
"""

from __future__ import annotations

import io
import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from deal_watch.diagnostics import (
    CAPTURE_STAGE_LOG_BACKUPS,
    CAPTURE_STAGE_LOG_BYTES,
    CAPTURE_STAGE_LOGGER_NAME,
    STAGE_FIELD_ALLOWLIST,
    configure_capture_stage_log,
    install_exception_hooks,
    log_capture_stage,
    qt_message_handler,
    sanitize_stage_fields,
    title_fingerprint,
)


class _MemoryHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.stream = io.StringIO()

    def emit(self, record: logging.LogRecord) -> None:
        self.stream.write(self.format(record) + "\n")


def _logger(name: str) -> tuple[logging.Logger, _MemoryHandler]:
    logger = logging.getLogger(name)
    logger.handlers = []
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = _MemoryHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger, handler


class ExceptionHookTests(unittest.TestCase):
    def setUp(self):
        self.logger, self.handler = _logger("deal_watch.tests.excepthook")
        self.original_sys_hook = sys.excepthook
        self.original_thread_hook = threading.excepthook
        install_exception_hooks(self.logger)

    def tearDown(self):
        sys.excepthook = self.original_sys_hook
        threading.excepthook = self.original_thread_hook
        self.logger.handlers = []

    def test_main_thread_exception_is_written_to_log(self):
        try:
            raise OSError("[WinError 5] 合成故障")
        except OSError as error:
            sys.excepthook(type(error), error, error.__traceback__)
        output = self.handler.stream.getvalue()
        self.assertIn("OSError", output)
        self.assertIn("合成故障", output)
        self.assertIn("未捕获异常（主线程）", output)

    def test_thread_exception_is_written_to_log(self):
        try:
            raise RuntimeError("线程里的合成故障")
        except RuntimeError as error:
            args = threading.ExceptHookArgs(
                (RuntimeError, error, error.__traceback__, threading.current_thread())
            )
        threading.excepthook(args)
        output = self.handler.stream.getvalue()
        self.assertIn("线程里的合成故障", output)
        self.assertIn("未捕获异常（线程", output)

    def test_keyboard_interrupt_is_not_reported_as_failure(self):
        def silent(*_args) -> None:
            return None

        sys.excepthook = silent
        install_exception_hooks(self.logger)
        try:
            raise KeyboardInterrupt
        except KeyboardInterrupt as error:
            sys.excepthook(type(error), error, error.__traceback__)
        self.assertEqual(self.handler.stream.getvalue(), "")


class QtMessageHandlerTests(unittest.TestCase):
    def test_only_warning_and_above_are_logged(self):
        from PySide6.QtCore import QtMsgType

        logger, handler = _logger("deal_watch.tests.qt")
        try:
            self.assertTrue(callable(qt_message_handler(logger)))
            handle = qt_message_handler(logger)
            handle(QtMsgType.QtDebugMsg, None, "调试噪声不应落盘")
            handle(QtMsgType.QtInfoMsg, None, "信息噪声不应落盘")
            handle(QtMsgType.QtWarningMsg, None, "布局警告应落盘")
            handle(QtMsgType.QtCriticalMsg, None, "严重错误应落盘")
        finally:
            output = handler.stream.getvalue()
            logger.handlers = []
        self.assertNotIn("调试噪声", output)
        self.assertNotIn("信息噪声", output)
        self.assertIn("布局警告", output)
        self.assertIn("严重错误", output)


class CaptureStageLogTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.dir = Path(self._temp.name)
        self.logger = configure_capture_stage_log(self.dir)
        self.path = self.dir / "logs" / "capture-stages.log"

    def tearDown(self):
        for handler in self.logger.handlers[:]:
            handler.flush()
            handler.close()
            self.logger.removeHandler(handler)
        self._temp.cleanup()

    def test_logger_name_is_separate_from_main_log(self):
        self.assertEqual(self.logger.name, CAPTURE_STAGE_LOGGER_NAME)

    def test_rotation_budget_is_256kb_times_one(self):
        handler = self.logger.handlers[0]
        self.assertEqual(handler.maxBytes, CAPTURE_STAGE_LOG_BYTES)
        self.assertEqual(handler.maxBytes, 4 * 1024 * 1024)
        self.assertEqual(handler.backupCount, CAPTURE_STAGE_LOG_BACKUPS)
        self.assertTrue(str(handler.baseFilename).endswith("capture-stages.log"))
        # 独立文件，不得再冒泡到主日志（否则同一行写两遍）。
        self.assertFalse(self.logger.propagate)

    def test_only_allowlisted_fields_are_written(self):
        kept = log_capture_stage(
            self.logger,
            stage="wgc_start",
            request_id="3f9c1d2a",
            region_id="r1",
            elapsed_ms=18.236,
            outcome="ok",
            error_code="",
            image="iVBORw0KGgoAAAANSUhEUg==",
            chat_text="这是一条真实的聊天正文",
            window_title="羊毛一群(3)",
            webhook="https://open.feishu.cn/secret-token",
        )
        self.assertEqual(set(kept) - set(STAGE_FIELD_ALLOWLIST), set())
        for handler in self.logger.handlers:
            handler.flush()
        raw = self.path.read_text(encoding="utf-8")
        self.assertIn("stage=wgc_start", raw)
        self.assertIn("request_id=3f9c1d2a", raw)
        self.assertIn("elapsed_ms=18.236", raw)
        # 图像、聊天正文、窗口标题原文和凭据都不得出现。
        self.assertNotIn("iVBORw0KGgo", raw)
        self.assertNotIn("聊天正文", raw)
        self.assertNotIn("羊毛一群", raw)
        self.assertNotIn("secret-token", raw)

    def test_line_without_stage_is_dropped(self):
        self.assertEqual(log_capture_stage(self.logger, request_id="deadbeef"), {})
        for handler in self.logger.handlers:
            handler.flush()
        self.assertFalse(self.path.exists() and self.path.read_text(encoding="utf-8").strip())

    def test_sanitize_keeps_identifiers_only(self):
        clean = sanitize_stage_fields(
            {"stage": "wgc_start; DROP", "request_id": "3f9c-1d2a", "elapsed_ms": 12.5}
        )
        self.assertEqual(clean["stage"], "wgc_startDROP")
        self.assertEqual(clean["request_id"], "3f9c-1d2a")
        self.assertEqual(clean["elapsed_ms"], 12.5)

    def test_numeric_slots_reject_strings(self):
        """数值槽只收真数字：字符串（哪怕内容是数字）一律拒收，不许伪装落盘。"""
        clean = sanitize_stage_fields(
            {
                "stage": "cycle",
                "elapsed_ms": "12.5",
                "interval_ms": "4999 元",
                "in_flight": "2",
                "schedule_wait_ms": True,
                "capture_timeout_ms": float("nan"),
                "wgc_timeout_ms": -1,
            }
        )
        self.assertEqual(clean, {"stage": "cycle"})


class TitleFingerprintTests(unittest.TestCase):
    def test_fingerprint_is_short_stable_and_not_reversible(self):
        first = title_fingerprint("羊毛一群")
        self.assertEqual(len(first), 8)
        self.assertEqual(first, title_fingerprint("羊毛一群"))
        self.assertNotEqual(first, title_fingerprint("羊毛一群(3)"))
        self.assertNotIn("羊毛", first)


class ConfigSlotGuardTests(unittest.TestCase):
    def test_slot_failure_is_logged_and_reported_without_raising(self):
        from deal_watch.ui.main import config_slot

        logger, handler = _logger("deal_watch.tests.slot")

        class Holder:
            def __init__(self) -> None:
                self.logger = logger

            @config_slot("合成动作")
            def run(self) -> None:
                raise OSError("[WinError 5] 合成写盘失败")

        with patch("deal_watch.ui.main.QMessageBox.critical") as critical:
            self.assertIsNone(Holder().run())
        output = handler.stream.getvalue()
        logger.handlers = []
        self.assertIn("合成动作", output)
        self.assertIn("OSError", output)
        self.assertTrue(critical.called)
        self.assertEqual(critical.call_args.args[1], "合成动作失败")


if __name__ == "__main__":
    unittest.main()
