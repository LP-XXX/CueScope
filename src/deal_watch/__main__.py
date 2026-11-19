"""应用入口：DPI、单实例、托盘生命周期。"""

from __future__ import annotations

import argparse
import ctypes
import getpass
import hashlib
import io
import os
import sys
from pathlib import Path


def _ensure_standard_streams() -> None:
    """窗口化打包（``console=False``）下 ``sys.stdout`` / ``sys.stderr`` 是 ``None``。

    此时 ``argparse`` 的 ``--help``、参数错误提示以及任何 ``print`` 都会抛
    ``AttributeError``：用户双击图标后“什么都没发生”，也没有任何提示——正是
    0.2.2 要修的那一类“静默失败”。这里用黑洞流占位，让流程在任何启动方式下
    都能走完；真正的诊断信息一律写日志文件，不依赖 stdout。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        sink = open(os.devnull, "w", encoding="utf-8")
    except OSError:
        sink = io.StringIO()
    if sys.stdout is None:
        sys.stdout = sink
    if sys.stderr is None:
        sys.stderr = sink


def enable_dpi_awareness() -> None:
    if sys.platform == "win32":
        try:
            # PER_MONITOR_DPI_AWARE_V2，必须在创建 QApplication 前设置。
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            ctypes.windll.shcore.SetProcessDpiAwareness(2)


def application_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        # PyInstaller 6 onedir 的数据文件默认位于 _internal，而非 exe 同级。
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[2]


def main() -> None:
    # 必须在 argparse 之前：窗口化打包下 --help / 参数错误都会写 stdout。
    _ensure_standard_streams()
    from deal_watch.brand import DISPLAY_NAME, TAGLINE
    parser = argparse.ArgumentParser(description=f"{DISPLAY_NAME} · {TAGLINE}")
    parser.add_argument("--minimized", action="store_true", help="启动后仅显示托盘")
    parser.add_argument("--local-demo", action="store_true", help="生成示例、离线识别并显示本地提醒")
    parser.add_argument("--self-check", action="store_true", help="隔离离线自检，不读取用户配置或发送通知")
    parser.add_argument("--report", type=Path, help="自检 JSON 报告路径")
    args = parser.parse_args()
    enable_dpi_awareness()
    if args.local_demo:
        from deal_watch.ui.local_demo import run_demo

        raise SystemExit(run_demo(application_base_dir()))
    if args.self_check:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QApplication

    from deal_watch.config import default_data_dir
    from deal_watch.diagnostics import (
        configure_capture_stage_log,
        configure_logging,
        install_exception_hooks,
        install_qt_message_handler,
    )

    # 全局异常出口必须在窗口构造之前装好：构造期的异常同样不能只落到
    # 不可见的 stderr（打包为窗口程序后 stderr 为空）。
    # ``--self-check`` 走隔离自检，不碰用户数据目录，因此不在这里建日志。
    bootstrap_logger = None
    if not args.self_check:
        bootstrap_logger = configure_logging(default_data_dir())
        configure_capture_stage_log(default_data_dir())
        from deal_watch import __version__
        component = application_base_dir().parent / "capture-component" / "DealWatchCapture.exe"
        component_hash = (
            hashlib.sha256(component.read_bytes()).hexdigest()
            if component.is_file() else "source"
        )
        executable_hash = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest() if getattr(sys, "frozen", False) else "source"
        source_hash = hashlib.sha256()
        if not getattr(sys, "frozen", False):
            for source in sorted(Path(__file__).resolve().parent.rglob("*.py")):
                source_hash.update(source.relative_to(Path(__file__).resolve().parent).as_posix().encode())
                source_hash.update(source.read_bytes())
        bootstrap_logger.info(
            "launch version=%s capture_component_sha256=%s executable_sha256=%s source_sha256=%s",
            __version__, component_hash, executable_hash, source_hash.hexdigest() if executable_hash == "source" else "embedded",
        )
        install_exception_hooks(bootstrap_logger)

    app = QApplication(sys.argv[:1])
    app.setApplicationName(DISPLAY_NAME)
    app.setApplicationDisplayName(DISPLAY_NAME)
    app.setQuitOnLastWindowClosed(False)
    if args.self_check:
        from deal_watch.self_check import run_self_check

        raise SystemExit(run_self_check(app, application_base_dir(), args.report))
    identity = hashlib.sha256(getpass.getuser().encode("utf-8")).hexdigest()[:12]
    name = f"DealWatch-{identity}"
    probe = QLocalSocket()
    probe.connectToServer(name)
    if probe.waitForConnected(200):
        probe.write(b"show")
        probe.flush()
        probe.waitForBytesWritten(200)
        return

    class SingleInstance(QObject):
        show_requested = Signal()

        def __init__(self) -> None:
            super().__init__()
            self.server = QLocalServer(self)
            QLocalServer.removeServer(name)
            if not self.server.listen(name):
                raise RuntimeError("无法创建单实例通信端点")
            self.server.newConnection.connect(self._receive)

        def _receive(self) -> None:
            socket = self.server.nextPendingConnection()
            if socket:
                socket.readAll()
                socket.disconnectFromServer()
            self.show_requested.emit()

    instance = SingleInstance()
    # PyInstaller 将本文件作为脚本入口，__package__ 可能为空。
    from deal_watch.ui.main import MainWindow

    window = MainWindow(application_base_dir())
    # Qt 消息处理函数只能在主窗口构造之后安装：构造期的 Qt 警告走默认处理，
    # offscreen 测试里根本不安装（测试不启动本入口）。
    install_qt_message_handler(bootstrap_logger or window.logger)
    instance.show_requested.connect(window.show_normal)
    if not args.minimized:
        window.show()
    app.aboutToQuit.connect(window.exit_app)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
