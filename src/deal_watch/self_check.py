"""便携包自身执行的隔离离线自检，不以“进程仍活着”代替成功。"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import tempfile
import time
from pathlib import Path

# 版本号格式（``主.次.修订``）。自检只验证“版本号本身合法”，**不**把它与某个
# 字面量比较：0.2.1 在这里写死 ``"0.2.1"``，结果一升版自检立刻抛错、发版自检
# 全盘失败。版本是否与 ``pyproject.toml`` 一致由测试用例交叉校验（漂移应当被
# 测试发现，而不是让自检在用户机器上炸掉）。
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


def run_self_check(app, base_dir: Path, report_path: Path | None) -> int:
    from PySide6.QtCore import QTimer

    from . import __version__
    from .config import ModelConfig
    from .ocr import LocalOcr
    from .ui.main import MainWindow

    report_path = report_path or Path.cwd() / "cuescope-self-check.json"
    report = {
        "kind": "isolated_offline_self_check", "passed": False,
        "version": __version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": platform.python_version(), "started_at": time.time(),
        "no_real_desktop_or_notification": True,
    }
    previous = os.environ.get("LOCALAPPDATA")
    window = None
    try:
        with tempfile.TemporaryDirectory(prefix="deal-watch-self-check-") as temporary:
            os.environ["LOCALAPPDATA"] = temporary
            if not VERSION_PATTERN.fullmatch(__version__):
                raise RuntimeError(f"Malformed application version: {__version__!r}")
            if getattr(sys, "frozen", False):
                capture = base_dir.parent / "capture-component" / "DealWatchCapture.exe"
                if not capture.is_file():
                    raise RuntimeError("Missing capture component")
                report["capture_component"] = True
            engine = LocalOcr(ModelConfig(), base_dir)
            engine.verify_models()
            report["models_verified"] = 3
            import numpy as np
            from PIL import Image, ImageDraw, ImageFont

            from .models import FrameSample, Rect

            image = Image.new("RGB", (650, 300), (250, 250, 250))
            draw = ImageDraw.Draw(image)
            # Pillow supplies this scalable font even on Windows without CJK fonts.
            font = ImageFont.load_default(size=32)
            draw.text((30, 50), "CueScope 4999", font=font, fill="black")
            frame = FrameSample(
                "self-check", 1, time.time(), np.asarray(image)[:, :, ::-1].copy(),
                Rect(0, 0, 650, 300),
            )
            frame.content_mode = "generic_line"
            observation = engine.recognize(frame)
            if not any("4999" in block.text for block in observation.blocks):
                raise RuntimeError("Synthetic OCR missing expected price")
            report["ocr_ms"] = round(observation.inference_ms, 2)
            from .ui.qq_binding import matching_recipient

            # Import the packaged Qt WebSockets extension and exercise pairing without a connection.
            packet = {"op": 0, "t": "C2C_MESSAGE_CREATE",
                      "d": {"content": "fixture", "author": {"user_openid": "synthetic_self"}}}
            if matching_recipient(packet, "private", "fixture") != "synthetic_self":
                raise RuntimeError("QQ pairing module failed offline check")
            report["qq_pairing_module"] = True
            window = MainWindow(base_dir)
            window.show()
            if window.pages.count() != 4 or window.records_tabs.count() != 2:
                raise RuntimeError("Main window is incomplete")
            report["ui_pages"] = 4
            report["record_views"] = 2
            QTimer.singleShot(150, window.exit_app)
            app.exec()
            if not window._shutdown_done or window.dispatcher.is_alive():
                raise RuntimeError("Workers did not exit cleanly")
            report["clean_shutdown"] = True
            report["passed"] = True
    except Exception as error:
        report["error_type"] = type(error).__name__
        # 此路径的数据为合成内容，不输出堆栈或可能包含凭据的异常正文。
        if window and not window._shutdown_started:
            window.exit_app()
            app.exec()
    finally:
        if previous is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = previous
    report["finished_at"] = time.time()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1
