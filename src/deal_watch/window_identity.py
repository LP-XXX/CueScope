"""One identity policy for readiness and capture, including failed identity queries."""
from __future__ import annotations

import ntpath

from .models import CaptureErrorCode, RegionConfig


def canonical_process_path(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(value)) if value else ""


def identity_issues(region: RegionConfig, window: object) -> list[tuple[CaptureErrorCode, str]]:
    issues = []
    if region.process_id and getattr(window, "process_id", 0) != region.process_id:
        issues.append((CaptureErrorCode.PROCESS_MISMATCH, "窗口进程已变化（PID 不一致），请重新绑定"))
    if region.process_path and canonical_process_path(getattr(window, "process_path", "")) != canonical_process_path(region.process_path):
        issues.append((CaptureErrorCode.PROCESS_MISMATCH, "窗口进程身份已变化（可执行文件路径不一致或无法确认），请重新绑定"))
    if region.process_start_id and getattr(window, "process_start_id", "") != region.process_start_id:
        issues.append((CaptureErrorCode.PROCESS_MISMATCH, "窗口进程已重启或无法确认，请重新绑定"))
    if region.window_class and getattr(window, "class_name", "") != region.window_class:
        issues.append((CaptureErrorCode.NEEDS_CALIBRATION, "窗口类名身份已变化，请重新圈选"))
    if region.title_policy == "strict" and region.window_title and getattr(window, "title", "") != region.window_title:
        issues.append((CaptureErrorCode.NEEDS_CALIBRATION, "窗口标题身份已变化，请重新圈选"))
    if region.reference_dpi and getattr(window, "dpi", 0) != region.reference_dpi:
        issues.append((CaptureErrorCode.NEEDS_CALIBRATION, "窗口显示缩放已变化，请重新确认区域"))
    return issues
