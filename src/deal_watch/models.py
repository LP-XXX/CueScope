"""跨模块使用的不可隐式转换数据对象。时间均为 Unix 秒。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


def new_id() -> str:
    return uuid4().hex


class RegionStatus(StrEnum):
    DISABLED = "disabled"
    BASELINING = "baselining"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"


class CaptureErrorCode(StrEnum):
    """采集失败分类。

    0.2.2 追加的三个值都是**故障**语义，必须走 ``ERROR`` 而不是 ``PAUSED``：
    ``service._PAUSE_DETAILS`` 刻意不包含它们，否则超时会被当成“暂停”、
    不进故障状态、也不发“监控故障”通知（设计风险 R5）。
    """

    WINDOW_NOT_FOUND = "window_not_found"
    WINDOW_MINIMIZED = "window_minimized"
    WINDOW_MOVED = "window_moved"
    WINDOW_OCCLUDED = "window_occluded"
    WINDOW_RESIZED = "window_resized"
    PROCESS_MISMATCH = "process_mismatch"
    NEEDS_CALIBRATION = "needs_calibration"
    DESKTOP_LOCKED = "desktop_locked"
    CAPTURE_FAILED = "capture_failed"
    CAPTURE_TIMEOUT = "capture_timeout"
    BLACK_FRAME = "black_frame"
    INVALID_REGION = "invalid_region"
    # WGC 原生会话卡住（启动失败或退出超时）：子进程已明确报告阶段失败。
    CAPTURE_SESSION_HUNG = "capture_session_hung"
    # 连 CreateProcess 都吃掉整个预算：卡在杀软/过滤驱动，与“等帧太短”无关。
    CAPTURE_POPEN_TIMEOUT = "capture_popen_timeout"
    # kill() 之后 0.5 秒仍未退出：子进程可能残留，需要人工清理。
    CAPTURE_ORPHAN = "capture_orphan"


@dataclass(slots=True, frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("区域宽高必须为正数")

    def as_mss(self) -> dict[str, int]:
        return {
            "left": self.x,
            "top": self.y,
            "width": self.width,
            "height": self.height,
        }

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2


@dataclass(slots=True)
class RegionConfig:
    alias: str
    window_title: str
    rect: Rect
    id: str = field(default_factory=new_id)
    window_class: str = ""
    window_rect: Rect | None = None
    window_hwnd: int = 0
    process_id: int = 0
    process_path: str = ""
    capture_backend: str = "wgc"
    coordinate_space: str = "frame"
    reference_frame_width: int = 0
    reference_frame_height: int = 0
    needs_calibration: bool = False
    enabled: bool = True
    monitor_id: str = ""
    previewed: bool = False
    # Missing fields in v1–v3 retain the original chat and identity semantics.
    content_mode: str = "wechat_chat"
    title_policy: str = "strict"
    process_start_id: str = ""
    reference_dpi: int = 0
    poll_interval_ms: int = 0
    rule_group_id: str = ""
    dedupe_scope: str = "global"
    dedupe_group: str = ""

    def __post_init__(self) -> None:
        if self.content_mode not in {"wechat_chat", "generic_line", "text_block"}:
            raise ValueError("未知内容模式")
        if self.title_policy not in {"strict", "window"}:
            raise ValueError("未知窗口跟随策略")
        if self.dedupe_scope not in {"global", "task", "group"}:
            raise ValueError("未知去重范围")
        if self.dedupe_scope == "group" and not self.dedupe_group.strip():
            raise ValueError("请填写去重组名称")
        if isinstance(self.poll_interval_ms, bool) or not isinstance(self.poll_interval_ms, int) or (self.poll_interval_ms != 0 and not 10000 <= self.poll_interval_ms <= 300000):
            raise ValueError("任务采样间隔须为 10–300 秒，或使用默认值")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegionConfig:
        values = dict(data)
        values["rect"] = Rect(**values["rect"])
        if values.get("window_rect"):
            values["window_rect"] = Rect(**values["window_rect"])
        return cls(**values)


@dataclass(slots=True)
class FrameSample:
    region_id: str
    sequence: int
    captured_at: float
    image: Any
    rect: Rect
    forced: bool = False
    content_mode: str = "wechat_chat"


@dataclass(slots=True, frozen=True)
class TextBlock:
    text: str
    confidence: float
    box: Rect
    kind: str = "text"
    message_id: str = ""


@dataclass(slots=True)
class OcrObservation:
    region_id: str
    sampled_at: float
    frame_sequence: int
    blocks: list[TextBlock]
    uncovered_boxes: list[Rect] = field(default_factory=list)
    inference_ms: float = 0.0
    # 0.2.2 T2.3：本次文字结果是否来自静止画面缓存（跳过 ONNX 推理）。
    # 只影响诊断与耗时统计；跟踪、规则、通知判定一律照旧。
    reused: bool = False
    content_mode: str = "wechat_chat"
    strict_text: bool = False


@dataclass(slots=True)
class MessageCandidate:
    region_id: str
    text: str
    box: Rect
    first_seen: float
    last_seen: float
    confidence: float = 0.0
    boundary_uncertain: bool = False
    kind: str = "normal_text"
    tracking_id: str = field(default_factory=new_id)
    is_new: bool = True
    is_update: bool = False
    previous_text: str = ""


@dataclass(slots=True)
class MessageDecision:
    """单条消息的统一判断结果；入队和平台回执由后续阶段单独记录。"""

    tracking_id: str
    matched_keywords: list[str] = field(default_factory=list)
    char_matches: list[str] = field(default_factory=list)
    should_notify: bool = False
    reason: str = "not_matched"
    queue_state: str = "not_applicable"
    event_id: str = ""


@dataclass(slots=True)
class MatchEvent:
    region_id: str
    region_alias: str
    keyword: str
    text: str
    dedupe_key: str
    observed_at: float
    id: str = field(default_factory=new_id)
    tracking_id: str = ""
    matched_keywords: list[str] = field(default_factory=list)
    boundary_uncertain: bool = False
    duplicate_of: str = ""
    state: str = "pending"
    delivery_mode: str = "feishu"
    route_region_id: str = ""


@dataclass(slots=True)
class NotificationJob:
    event_id: str
    payload: dict[str, Any]
    created_at: float
    state: str = "pending"
    attempts: int = 0
    next_attempt_at: float = 0.0
    last_error: str = ""
    updated_at: float = 0.0
    receipt: str = ""
    next_part: int = 0
    route_region_id: str = ""


@dataclass(slots=True)
class HealthSnapshot:
    region_id: str
    status: RegionStatus
    detail: str = ""
    last_frame_at: float = 0.0
    last_ocr_ms: float = 0.0
    queue_depth: int = 0
    dropped_frames: int = 0
    # 0.2.2 T2.1：分段耗时（只看不改）。新字段**全部带默认值**，位置参数构造
    # （``HealthSnapshot(region_id, status, detail, ...)``）继续可用。
    #: ``{段名: {"cold": {...}, "steady": {...}, "window": [起, 止]}}``
    stages: dict[str, Any] = field(default_factory=dict)
    #: 一行可读摘要；没有样本的段不出现（避免 0ms 被读成“很快”）
    stages_summary: str = ""
    #: 当前处理中的任务数 = 采集队列 + OCR 正在处理的帧（队列为空 ≠ 处理完成）
    in_flight: int = 0
    #: OCR 静止帧复用（T2.3 填真值；本期恒为 0，字段名先定死避免改报告格式）
    ocr_reused: int = 0
    ocr_cache_hits: int = 0
    ocr_hit_rate: float = 0.0
    #: 运行时目标间隔、最近实际间隔、到期至采集启动的延迟和OCR队列等待。
    target_interval_ms: int = 0
    actual_interval_ms: float = 0.0
    schedule_delay_ms: float = 0.0
    ocr_queue_ms: float = 0.0
    last_success_at: float = 0.0
    overloaded: bool = False
    # 0.2.5：诊断扩展只追加默认字段，不改变旧快照构造与监控语义。
    capture_fault_count: int = 0
    capture_recovery_ms: float = 0.0
    next_retry_at: float = 0.0
    capture_worker_state: str = ""
    capture_process_stats: dict[str, Any] = field(default_factory=dict)
    capture_error_stage: str = ""
    capture_error_summary: str = ""
