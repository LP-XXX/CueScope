"""测试用的“已就绪区域”构造器。

0.2.1 起 ``MonitorService`` 只采集通过就绪门禁的区域（见 ``deal_watch.readiness``）。
合成用例走采集/OCR 路径时必须显式声明窗口句柄、参考帧尺寸与预览确认，
否则区域会停在“区域未就绪”状态，用例会退化成空转。
"""

from __future__ import annotations

from typing import Any

from deal_watch.models import Rect, RegionConfig

READY_HWND = 1001
READY_FRAME_SIZE = (800, 1280)
READY_PROCESS_ID = 4321
READY_PROCESS_PATH = "C:/Program Files/WeChat/WeChat.exe"
READY_WINDOW_CLASS = "WeChatMainWndForPC"


def ready_region(
    alias: str = "合成群",
    *,
    rect: Rect | None = None,
    window_title: str = "Synthetic",
    window_class: str = READY_WINDOW_CLASS,
    process_id: int = READY_PROCESS_ID,
    process_path: str = READY_PROCESS_PATH,
    hwnd: int = READY_HWND,
    backend: str = "wgc",
    frame_size: tuple[int, int] = READY_FRAME_SIZE,
    previewed: bool = True,
    **overrides: Any,
) -> RegionConfig:
    """返回一个默认能通过 ``region_readiness_problems`` 的区域配置。"""
    width, height = frame_size
    values: dict[str, Any] = {
        "alias": alias,
        "window_title": window_title,
        "rect": rect if rect is not None else Rect(0, 0, 300, 500),
        "window_class": window_class,
        "window_hwnd": hwnd,
        "process_id": process_id,
        "process_path": process_path,
        "capture_backend": backend,
        "coordinate_space": "frame",
        "reference_frame_width": width,
        "reference_frame_height": height,
        "needs_calibration": False,
        "enabled": True,
        "previewed": previewed,
    }
    values.update(overrides)
    return RegionConfig(**values)
