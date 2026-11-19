"""区域配置变更的事务边界（纯数据层，不导入 Qt）。

0.2.1 的缺陷根因是「``append`` 与 ``save`` 之间没有事务边界」：内存已经多了一条、
磁盘写失败、表格从未刷新，而异常从 Qt 槽函数逃逸后只进了不可见的 stderr。
本模块把顺序固定为

    校验 → 构造候选快照 → 写临时文件 → 回读校验临时文件 → 原子替换 →（ok 才）提交内存

回读校验在替换**之前**完成（0.2.2 D3），因此任何失败都意味着正式配置文件一个
字节都没变。并把结果压成一个可直接展示的 :class:`RegionSaveResult`。返回值里的
中文文案就是界面要提示的文字，调用方不需要再翻译一遍。

**不变量 I-1**：任何时刻 ``config.json.regions`` == ``config.regions`` ==
表格行数，除非正处于“保存失败、编辑态保留”的可识别状态；那种状态下内存与
表格都必须停留在**旧**快照，而待保存的变更由界面保留以便重试（0.2.2 D1）。
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from .config import ConfigSaveError, ConfigStore
from .models import RegionConfig
from .readiness import (
    MIN_REGION_SIDE,
    READY_CAPTURE_BACKENDS,
    WindowLookup,
    region_identity_problems,
)

# 区域配置变更动作；与 ``RegionSaveResult.action`` 取值一致。
SaveAction = Literal["add", "update", "delete", "toggle", "config"]

ACTION_LABELS: dict[str, str] = {
    "add": "添加区域",
    "update": "更新区域",
    "delete": "删除区域",
    "toggle": "暂停 / 启用区域",
    "config": "保存配置",
}


@dataclass(frozen=True)
class RegionSaveResult:
    """一次配置变更的结果；``message`` 可直接放进 ``QMessageBox``。

    - ``ok=False`` 且 ``problems`` 非空表示**校验**未通过（``error_stage="validate"``）。
    - ``ok=False`` 且 ``error_stage`` 属于 ``ConfigStore`` 阶段名表示**写盘**失败。
    """

    ok: bool
    region_id: str = ""
    action: str = "add"
    problems: list[str] = field(default_factory=list)
    error: str = ""
    error_stage: str = ""
    elapsed_ms: float = 0.0

    def summary(self) -> str:
        """给状态栏用的一行摘要。"""
        label = ACTION_LABELS.get(self.action, self.action)
        if self.ok:
            return f"{label}已保存（{self.elapsed_ms:.0f}ms）"
        return f"{label}失败：{self.message}"

    @property
    def message(self) -> str:
        """失败原因的中文说明；成功时为空字符串。"""
        if self.ok:
            return ""
        if self.problems:
            return "；".join(item for item in self.problems if item)
        return self.error or "保存失败，原因未知"


def alias_needs_duplicate_check(
    action: SaveAction,
    region: RegionConfig | None,
    original: RegionConfig | None,
) -> bool:
    """判断本次变更是否需要检查“别名撞车”（0.2.2 D2）。

    规则不看动作类型，而看**别名有没有变**：

    * ``add``：一律检查（新区域没有原名可比）。
    * ``update``：别名与该区域**原有别名**一致 → 跳过检查，历史遗留的同名配置
      不会因为“重新校准”而被卡住；别名被改动 → 与新增同等检查，避免把 B 改名成
      A 之后出现两份无法区分的区域。
    * ``toggle``：只切 ``enabled``，从不改别名 → 不检查。
    * ``delete`` / ``config``：不涉及别名 → 不检查。
    """
    if action == "add":
        return True
    if action != "update" or region is None:
        return False
    if original is None:
        # 找不到原区域（例如配置里缺 id）：按“别名已改动”从严处理。
        return True
    return region.alias.strip() != original.alias.strip()


def validate_region_config(
    region: RegionConfig | None,
    *,
    existing: Sequence[RegionConfig] = (),
    windows: WindowLookup | None = None,
    max_regions: int | None = None,
    check_duplicate_alias: bool = True,
) -> list[str]:
    """纯校验：返回问题清单，空列表表示可以落盘。

    与 ``readiness.region_readiness_problems`` 的区别：**不检查** ``previewed``。
    未预览确认的区域必须能被保存下来并显示在表格里（用户最痛的点是“保存了却
    不显示”），预览门禁只决定它能否进入自动监控。
    """
    problems: list[str] = []
    if region is None:
        return ["没有待保存的区域配置"]
    if not str(region.alias or "").strip():
        problems.append("请填写来源别名")
    elif check_duplicate_alias and any(
        item.id != region.id and item.alias.strip() == region.alias.strip() for item in existing
    ):
        problems.append(f"已存在同名区域“{region.alias.strip()}”，请换一个别名")
    if region.needs_calibration:
        problems.append("配置来自旧版桌面坐标，需重新圈选消息区域")
    if region.coordinate_space != "frame":
        problems.append("坐标模式不是采集帧相对坐标（frame）")
    if region.capture_backend not in READY_CAPTURE_BACKENDS:
        problems.append(f"未知采集后端：{region.capture_backend}")
    if region.window_hwnd <= 0:
        problems.append("未绑定窗口句柄")
    if region.reference_frame_width <= 0 or region.reference_frame_height <= 0:
        problems.append("未记录参考帧尺寸")
    rect = region.rect
    if rect is None or rect.width < MIN_REGION_SIDE or rect.height < MIN_REGION_SIDE:
        problems.append(f"圈选区域过小（小于 {MIN_REGION_SIDE}×{MIN_REGION_SIDE} 像素）")
    if rect is not None:
        if rect.x < 0 or rect.y < 0:
            problems.append("圈选区域超出采集帧")
        elif region.reference_frame_width > 0 and region.reference_frame_height > 0:
            if (
                rect.x + rect.width > region.reference_frame_width
                or rect.y + rect.height > region.reference_frame_height
            ):
                problems.append("圈选区域超出采集帧")
    others = [item for item in existing if item.id != region.id]
    if max_regions is not None and max_regions > 0 and len(others) >= max_regions:
        problems.append(f"区域数量已达上限（{max_regions} 个），请先删除不再使用的区域")
    problems.extend(region_identity_problems(region, windows))
    return problems


def apply_region_change(
    config: Any,
    action: SaveAction,
    region: RegionConfig | None = None,
    index: int | None = None,
) -> int:
    """把变更作用到 ``config.regions``；返回变更后的行号（-1 表示不适用）。

    这是**唯一**修改内存的地方。默认由 :func:`commit_region_change` 在写盘成功
    之后调用；界面层需要把“提交内存”留在 UI 线程时，用
    ``commit_region_change(..., commit_memory=False)`` 然后自己在主线程调用它。
    """
    regions: list[RegionConfig] = config.regions
    if action == "add":
        if region is None:
            raise ValueError("添加区域缺少 RegionConfig")
        regions.append(region)
        return len(regions) - 1
    if action in {"update", "toggle"}:
        if region is None:
            raise ValueError("更新区域缺少 RegionConfig")
        position = _resolve_index(regions, region, index)
        regions[position] = region
        return position
    if action == "delete":
        position = _resolve_index(regions, region, index)
        del regions[position]
        return -1
    if action == "config":
        # 全局配置变更（关键词/设置/启动快照）：内存提交由调用方的 apply_change 负责。
        return -1
    raise ValueError(f"未知的区域变更动作：{action}")


def _resolve_index(
    regions: Sequence[RegionConfig], region: RegionConfig | None, index: int | None
) -> int:
    """定位要替换/删除的行：优先按 ``id`` 匹配，其次用显式下标。

    ``id`` 优先的理由：下标是调用方在写盘前算出来的，一旦列表在保存期间被
    改动（应该不会发生），按下标会替换/删除到**错误**的区域。按 ``id`` 匹配
    至少能命中同一个区域；只有在新区域尚未入库（无 id）时才退回下标。
    """
    if region is not None:
        for position, item in enumerate(regions):
            if item.id == region.id:
                return position
    if index is not None and 0 <= index < len(regions):
        return index
    if region is None:
        raise ValueError("缺少目标区域")
    raise ValueError("目标区域已不在配置中")


def commit_region_change(
    config_store: ConfigStore,
    config: Any,
    action: SaveAction,
    region: RegionConfig | None = None,
    index: int | None = None,
    *,
    windows: WindowLookup | None = None,
    max_regions: int | None = None,
    commit_memory: bool = True,
    validate: bool = True,
) -> RegionSaveResult:
    """区域变更的事务入口：校验 → 写盘（临时文件 + 回读校验 + 原子替换）→ 提交内存。

    ``commit_memory=False`` 供后台线程使用：写盘与回读在后台完成，内存提交
    留给 UI 线程（跨线程改 ``config.regions`` 会让表格与服务读到半成品）。
    失败时 ``ok=False`` 且 ``error_stage`` 指出阶段，调用方可据此判断能否直接重试。
    """
    started = time.monotonic()
    region_id = str(getattr(region, "id", "") or "")
    is_enable = action == "toggle" and region is not None and bool(region.enabled)
    needs_validation = action in {"add", "update"} or is_enable
    if validate and needs_validation:
        existing = [item for item in config.regions if item.id != region_id]
        # 原区域用于判断“别名是否被改动”，而不是只看动作类型（0.2.2 D2）。
        original = next((item for item in config.regions if item.id == region_id), None)
        problems = validate_region_config(
            region,
            existing=existing,
            windows=windows,
            max_regions=max_regions,
            check_duplicate_alias=alias_needs_duplicate_check(action, region, original),
        )
        if problems:
            return RegionSaveResult(
                ok=False,
                region_id=region_id,
                action=action,
                problems=problems,
                error="；".join(problems),
                error_stage="validate",
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
    candidate = copy.deepcopy(config)
    try:
        apply_region_change(candidate, action, region, index)
    except (ValueError, KeyError) as error:
        return RegionSaveResult(
            ok=False,
            region_id=region_id,
            action=action,
            problems=[str(error)],
            error=str(error),
            error_stage="validate",
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    try:
        config_store.save(candidate, verify=True)
    except ConfigSaveError as error:
        return RegionSaveResult(
            ok=False,
            region_id=region_id,
            action=action,
            error=str(error),
            error_stage=error.stage,
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    except OSError as error:  # 兜底：非 ConfigSaveError 的 IO 失败同样不得提交内存
        return RegionSaveResult(
            ok=False,
            region_id=region_id,
            action=action,
            error=f"配置保存失败（{type(error).__name__}: {error}）。",
            error_stage="io",
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    if commit_memory:
        apply_region_change(config, action, region, index)
    return RegionSaveResult(
        ok=True,
        region_id=region_id,
        action=action,
        elapsed_ms=(time.monotonic() - started) * 1000,
    )


def commit_config_change(
    config_store: ConfigStore,
    config: Any,
    apply_change: Callable[[Any], None],
    *,
    action: SaveAction = "config",
    verify: bool = True,
) -> RegionSaveResult:
    """全局配置变更（关键词/设置/启动前快照）的事务入口。

    ``apply_change`` 先在**深拷贝**上执行并落盘，回读校验通过后才在原对象上
    再执行一次。因此写盘失败时内存一个字段都不会变，不需要调用方手工回滚。
    """
    started = time.monotonic()
    candidate = copy.deepcopy(config)
    try:
        apply_change(candidate)
    except Exception as error:  # noqa: BLE001 - 变更函数由调用方提供，需统一收敛
        return RegionSaveResult(
            ok=False,
            action=action,
            problems=[f"配置变更无法应用：{error}"],
            error=f"配置变更无法应用：{error}",
            error_stage="apply",
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    try:
        config_store.save(candidate, verify=verify)
    except ConfigSaveError as error:
        return RegionSaveResult(
            ok=False,
            action=action,
            error=str(error),
            error_stage=error.stage,
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    except OSError as error:
        return RegionSaveResult(
            ok=False,
            action=action,
            error=f"配置保存失败（{type(error).__name__}: {error}）。",
            error_stage="io",
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    apply_change(config)
    return RegionSaveResult(
        ok=True, action=action, elapsed_ms=(time.monotonic() - started) * 1000
    )
