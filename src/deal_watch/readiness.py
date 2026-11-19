"""区域就绪门禁：把“这个区域能否进入自动监控”的判据集中到一处。

本模块是纯函数集合：不导入 Qt、不导入 ``MonitorService``，也不主动访问桌面。
窗口身份校验通过注入的 ``windows`` 对象完成（只要有 ``window_from_hwnd(hwnd)``
方法即可），因此界面层、服务层和测试可以用同一个判据，不会出现三套口径。

所有函数返回中文问题清单：空列表表示就绪，非空表示还不能自动监控。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from .models import RegionConfig
from .window_identity import identity_issues

# 采集后端白名单：与 ``window_capture.WindowFrameCapturer.validate`` 保持一致。
READY_CAPTURE_BACKENDS: frozenset[str] = frozenset({"wgc", "print-window"})
# 消息区域最小边长（像素）：与圈选对话框的接受门槛一致。
MIN_REGION_SIDE = 40
# 未就绪时统一的后续动作提示；界面与服务层的 detail 都用它收尾。
READY_RETRY_HINT = "请重新圈选或预览确认"


class WindowLookup(Protocol):
    """窗口身份查询接口：``WindowsWindows`` 与本测试替身都满足它。"""

    def window_from_hwnd(self, hwnd: int) -> Any:  # pragma: no cover - 仅协议声明
        ...


def _window_identity(window: Any) -> dict[str, Any]:
    """从窗口对象提取参与比对的四个身份字段；缺失字段按空值处理。"""
    return {
        "process_id": int(getattr(window, "process_id", 0) or 0),
        "process_path": str(getattr(window, "process_path", "") or ""),
        "class_name": str(getattr(window, "class_name", "") or ""),
        "title": str(getattr(window, "title", "") or ""),
    }


def region_identity_problems(region: RegionConfig, windows: WindowLookup | None) -> list[str]:
    """窗口身份校验：窗口是否存在、进程/路径/类名/标题是否仍与配置一致。

    ``windows`` 为空（例如尚未启动服务、或采集器不提供窗口查询）时跳过，
    调用方不会因此被误判为未就绪。
    """
    if windows is None or not hasattr(windows, "window_from_hwnd"):
        return []
    if region.window_hwnd <= 0:
        # 没有句柄就没法做身份校验；句柄缺失本身已由调用方单独报告。
        return []
    window = windows.window_from_hwnd(region.window_hwnd)
    if window is None:
        return ["绑定窗口已不存在或已关闭"]
    return [message for _code, message in identity_issues(region, window)]


def region_readiness_problems(
    region: RegionConfig, windows: WindowLookup | None = None
) -> list[str]:
    """按固定顺序返回区域未就绪的中文问题清单；空列表表示就绪。

    判据顺序（前者更根本，避免一次报一堆噪声）：
    旧版坐标 → 未预览确认 → 句柄/坐标模式/后端 → 区域尺寸 → 参考帧 →
    窗口身份。
    """
    problems: list[str] = []
    if region.needs_calibration:
        problems.append("配置来自旧版桌面坐标，需重新圈选消息区域")
    if not region.previewed:
        problems.append("尚未检查确认当前可见内容")
    if region.window_hwnd <= 0:
        problems.append("未绑定窗口句柄")
    if region.coordinate_space != "frame":
        problems.append("坐标模式不是采集帧相对坐标（frame）")
    if region.capture_backend not in READY_CAPTURE_BACKENDS:
        problems.append(f"未知采集后端：{region.capture_backend}")
    if region.rect.width < MIN_REGION_SIDE or region.rect.height < MIN_REGION_SIDE:
        problems.append(f"圈选区域过小（小于 {MIN_REGION_SIDE}×{MIN_REGION_SIDE} 像素）")
    if region.reference_frame_width <= 0 or region.reference_frame_height <= 0:
        problems.append("未记录参考帧尺寸")
    elif (
        region.rect.x < 0
        or region.rect.y < 0
        or region.rect.x + region.rect.width > region.reference_frame_width
        or region.rect.y + region.rect.height > region.reference_frame_height
    ):
        problems.append("圈选区域超出参考帧")
    problems.extend(region_identity_problems(region, windows))
    return problems


def is_region_ready(region: RegionConfig, windows: WindowLookup | None = None) -> bool:
    """就绪判定的布尔快捷方式；语义与 ``region_readiness_problems`` 完全一致。"""
    return not region_readiness_problems(region, windows)


def readiness_detail(problems: Sequence[str]) -> str:
    """把问题清单压成一行 detail：``区域未就绪：<原因>；请重新圈选或预览确认``。"""
    reasons = [item for item in problems if item]
    reason = "；".join(reasons) if reasons else "条件不满足"
    return f"区域未就绪：{reason}；{READY_RETRY_HINT}"


def readiness_summary(problems: Sequence[str]) -> str:
    """给对话框用的多行摘要：每个原因一行，便于用户逐条处理。"""
    reasons = [item for item in problems if item]
    if not reasons:
        return ""
    return "\n".join(f"· {item}" for item in reasons)
