"""采集、单 OCR worker、规则和存储的有界后台编排。"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from .capture import CaptureError, MssCapturer
from .config import AppConfig
from .diagnostics import CAPTURE_STAGE_LOGGER_NAME, HitImageStore, log_capture_stage
from .metrics import (
    STAGE_CAPTURE,
    STAGE_OCR_INFER,
    STAGE_OCR_QUEUE,
    STAGE_POSTPROCESS,
    STAGE_SCHEDULE_WAIT,
    STAGE_STORAGE_WRITE,
    STAGE_TRACKING_RULES,
    TimingRecorder,
)
from .models import CaptureErrorCode, FrameSample, HealthSnapshot, MatchEvent, RegionStatus
from .ocr_cache import OcrCacheContext, ReusableOcr, ReuseStats
from .readiness import readiness_detail, region_readiness_problems
from .rules import RuleEngine
from .storage import Storage
from .tracking import FrameTracker, blocks_to_messages

# 窗口最小化 / 桌面锁定属于可恢复的暂停，不算故障，不发“监控故障”。
# 0.2.2 新增的 CAPTURE_SESSION_HUNG / CAPTURE_POPEN_TIMEOUT / CAPTURE_ORPHAN
# **绝不能**加进这张表：它们是真实故障，进了这里就不会进故障状态、也不会发通知。
_PAUSE_DETAILS: dict[CaptureErrorCode, str] = {
    CaptureErrorCode.WINDOW_MINIMIZED: "已暂停：窗口最小化",
    CaptureErrorCode.DESKTOP_LOCKED: "已暂停：桌面已锁定",
}


# 采集预算的两个生产常量。**只用于诊断记录，不参与任何调度决策。**
# 真值仍在 ``WindowCaptureClient(timeout=5.0)`` 与 ``capture_wgc(timeout=3.0)``
# 的签名里；``tests/test_stage_fields.py`` 用断言把这两个数字与真值绑在一起，
# 任何一侧漂移都会立刻失败（不能靠“记得同步”）。
DIAGNOSTIC_CAPTURE_TIMEOUT_MS = 5000.0
DIAGNOSTIC_WGC_TIMEOUT_MS = 3000.0

# 排队等待超过这个毫秒数才在 cycle 行上署名 region_id：
# 60 秒采样下正常抖动是亚毫秒级，只有真正被占住才会达到百毫秒量级。
SCHEDULE_WAIT_REPORT_MS = 100.0

_FAILURE_STAGE_LABELS = {
    "component_start": "采集组件启动未完成",
    "component_version": "采集组件版本不匹配，请更新完整便携包",
    "diagnostics": "诊断处理超时，迟到画面已作废",
    "target_validation": "窗口身份或采集条件已变化",
    "frame_callback_enter": "采集画面转换或检查未完成",
    "startup_queue": "等待采集组件就绪",
    "queue_wait": "等待采集组件空闲",
    "worker_ready": "等待采集组件完成初始化",
    "ready": "采集组件初始化未完成",
    "deps_init_started": "采集依赖加载未完成",
    "deps_init": "采集依赖加载异常",
    "wgc_construct_started": "采集会话创建未完成",
    "wgc_start_requested": "采集会话启动未完成",
    "wgc_start": "等待新画面超时",
    "first_valid_frame": "采集画面处理未完成",
    "session_stop": "采集会话停止未完成",
    "session_exit": "采集会话退出未完成",
    "wait_exit": "采集组件未及时返回",
    "reader_start": "采集结果通道不可用",
    "capture_resources": "采集资源暂不可用",
    "protocol": "采集组件协议或结果校验失败",
    "decode": "采集画面解码失败",
    "transport": "采集结果传输未完成",
    "ocr": "文字识别失败",
    "tracking": "消息判断失败",
    "record_write": "识别记录保存失败",
    "hit_image_write": "命中图片保存失败",
}


def failure_summary(stage: str, code: object = "") -> str:
    """界面短说明不放请求编号；完整诊断保留在日志及详情。"""
    if stage in _FAILURE_STAGE_LABELS:
        return _FAILURE_STAGE_LABELS[stage]
    return {
        CaptureErrorCode.WINDOW_NOT_FOUND: "绑定窗口已关闭，请重新绑定",
        CaptureErrorCode.PROCESS_MISMATCH: "窗口身份已变化，请重新绑定",
        CaptureErrorCode.WINDOW_RESIZED: "窗口尺寸已变化，请重新校准",
        CaptureErrorCode.NEEDS_CALIBRATION: "区域需要重新校准",
        CaptureErrorCode.BLACK_FRAME: "未取得有效画面（黑帧）",
        CaptureErrorCode.CAPTURE_POPEN_TIMEOUT: "采集组件启动超时",
        CaptureErrorCode.CAPTURE_ORPHAN: "采集超时，正在后台回收组件",
        CaptureErrorCode.CAPTURE_SESSION_HUNG: "采集会话未及时退出",
        CaptureErrorCode.CAPTURE_TIMEOUT: "采集超时",
        CaptureErrorCode.INVALID_REGION: "圈选区域无效，请重新校准",
    }.get(code, "监控处理失败，请查看详情")


def _build_ocr_reuse(ocr_engine: object, config: object) -> ReusableOcr | None:
    """按配置决定要不要给 OCR 引擎套一层静止画面复用。

    ``0``（或配置里没有这个键的旧对象）一律返回 ``None``：调用点因此走
    ``self.ocr.recognize(frame)`` 原路径，与 0.2.1 完全一致——没有哈希、没有锁、
    没有多一个对象。
    """
    max_age = float(getattr(config, "ocr_reuse_max_age_seconds", 0.0) or 0.0)
    if max_age <= 0.0:
        return None
    return ReusableOcr(ocr_engine, max_age_seconds=max_age)


def describe_failure(message: str, code: object = "", trace: object = None) -> str:
    """故障说明：原有文案 + 请求编号 / 卡住阶段 / 分段耗时。

    只追加标识符与数字（请求编号、阶段名、毫秒），不含图像、聊天正文或窗口标题。
    ``trace`` 为 ``None``（例如 OCR 失败、假采集器）时保持 0.2.1 的原文案不变。
    """
    text = str(message).strip() or "采集失败"
    suffix = ""
    if trace is not None:
        formatter = getattr(trace, "detail_suffix", None)
        if callable(formatter):
            try:
                suffix = str(formatter())
            except Exception:  # noqa: BLE001 - 诊断字段异常不能掩盖真正的故障
                suffix = ""
    if not suffix:
        return text[:300]
    if code:
        suffix = f"{code}｜{suffix}"
    return f"{text[:300]}（{suffix}）"


def decision_state(decision: object) -> str:
    """把一次最终判断收敛成落库字符串；与旧 ``mark_decisions`` 的口径完全一致。

    ``queue_state`` 是"这条消息最终被怎么处理"（queued / duplicate /
    already_processed / not_matched …）；``not_applicable`` 表示这一路不适用，
    此时退回 ``reason``（例如非普通文字、边界不确定）。判断值原样落库，不翻译。
    """
    state = getattr(decision, "queue_state", "not_applicable")
    if state and state != "not_applicable":
        return str(state)
    return str(getattr(decision, "reason", "observed"))


@dataclass(slots=True)
class _Runtime:
    status: RegionStatus = RegionStatus.BASELINING
    detail: str = "等待建立可见内容基线"
    last_frame_at: float = 0.0
    last_ocr_ms: float = 0.0
    last_submit_at: float = 0.0
    dropped_frames: int = 0
    failed: bool = False
    last_capture_start: float = 0.0
    previous_capture_start: float = 0.0
    actual_interval_ms: float = 0.0
    schedule_delay_ms: float = 0.0
    ocr_queue_ms: float = 0.0
    last_success_at: float = 0.0
    overloaded: bool = False
    capture_fault_count: int = 0
    fault_started_at: float = 0.0
    capture_recovery_ms: float = 0.0
    capture_error_stage: str = ""
    capture_error_summary: str = ""


class MonitorService:
    def __init__(
        self,
        config: AppConfig,
        storage: Storage,
        ocr_engine: object,
        *,
        capturer: MssCapturer | None = None,
        data_dir: Path | None = None,
        on_events: Callable[[list[MatchEvent]], None] | None = None,
        on_health: Callable[[HealthSnapshot], None] | None = None,
        recognition_store: object | None = None,
        metrics: TimingRecorder | None = None,
        on_frame_processed: Callable[[str, int], None] | None = None,
        delivery_mode: str = "feishu",
    ) -> None:
        self.config = config
        self.storage = storage
        # ``self.ocr`` 始终是调用方传进来的那个对象：工具与用例都可能按身份取它
        # （``service.ocr is engine``），包一层会让这些读数悄悄变味。
        self.ocr = ocr_engine
        # 静止画面复用是**可选的一层**：0 = 关闭时不创建，``_recognize`` 直通
        # ``self.ocr``，配置为 0 的部署与 0.2.1 连一条分支都不多走。
        self.ocr_reuse = _build_ocr_reuse(ocr_engine, config)
        self.capturer = capturer or MssCapturer()
        self.tracker = FrameTracker()
        self.recognition_store = recognition_store
        # 分段耗时只观察、不参与调度：任何埋点异常都不允许影响采集与识别。
        self.metrics = metrics if metrics is not None else TimingRecorder()
        # "帧处理完成"回调（region_id, frame.sequence）：验收判定用，必须覆盖
        # **复用命中轮**——复用跳过的是像素→文字的 ONNX，帧本身照样走完跟踪、
        # 规则、写库与通知。挂在 OCR 观测回调上会漏掉复用轮（那些轮不进引擎），
        # 所以权威标记点在这里，不在观测层。
        self.on_frame_processed = on_frame_processed or (lambda _rid, _seq: None)
        self.rules = RuleEngine(
            storage,
            config.keywords,
            config.dedupe_seconds,
            match_mode=config.match_mode,
            regions=config.regions,
            rule_groups=config.rule_groups,
            delivery_mode=delivery_mode,
        )
        self.on_events = on_events or (lambda events: None)
        self.on_health = on_health or (lambda health: None)
        self.hit_images = HitImageStore(data_dir or Path.cwd(), config.save_hit_images)
        self.logger = logging.getLogger("deal_watch.service")
        # 阶段日志与主日志分开：整份文件可以直接交给架构师做现场分析。
        self.stage_logger = logging.getLogger(CAPTURE_STAGE_LOGGER_NAME)
        self._runtime = {region.id: _Runtime() for region in config.regions}
        self._regions = {region.id: region for region in config.regions if region.enabled}
        self._pending: dict[str, FrameSample] = {}
        self._pending_generation: dict[str, int] = {}
        # 入队时刻（perf_counter），用于 ocr_queue 段：入队 → OCR 线程取出。
        # 与 _pending 同锁读写，不进 FrameSample，避免改模型签名。
        self._queued_at: dict[str, float] = {}
        self._generation = {region.id: 0 for region in config.regions}
        # Only the OCR worker touches FrameTracker. UI/capture threads invalidate
        # generations; reset is deferred until that worker accepts a valid frame.
        self._tracker_generation: dict[str, int] = {}
        self._capture_generation: dict[str, int] = {}
        self._order: deque[str] = deque()
        self._next_due: dict[str, float] = {}
        self._busy_generation: dict[str, int] = {}
        self._region_order = [region.id for region in config.regions]
        self._fair_cursor = -1
        self._poll_interval_ms = int(config.poll_interval_ms)
        self._queue_capacity = min(int(config.max_queue_size), 4)
        if config.max_queue_size > 4:
            logging.getLogger("deal_watch.service").warning(
                "configured OCR queue capacity %d is capped at 4", config.max_queue_size
            )
        # 正在 OCR 线程里处理、还没交回结果的帧数（诊断用，与队列长度相加
        # 得到 cycle 行的 in_flight）。
        self._ocr_active = 0
        self._ocr_busy: dict[str, int] = {}
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._ocr_thread: threading.Thread | None = None
        # 初始化时就把未就绪区域挡在采集队列外：即使调用方跳过 start()，
        # 也不会为未确认的区域采集。
        self._apply_readiness_gate(notify=False)

    def readiness_windows(self) -> object | None:
        """用于窗口身份校验的查询对象；采集器不提供时返回 None（跳过身份校验）。"""
        return getattr(self.capturer, "windows", None)

    def _apply_readiness_gate(self, notify: bool = True) -> dict[str, list[str]]:
        """只把就绪区域放进采集队列；未就绪区域保持暂停且不产生故障/恢复通知。

        返回 ``{region_id: [问题…]}``，便于调用方（界面或探针）展示原因。
        """
        windows = self.readiness_windows()
        not_ready: dict[str, list[str]] = {}
        with self._condition:
            self._regions = {}
            for region in self.config.regions:
                if not region.enabled:
                    continue
                self._runtime.setdefault(region.id, _Runtime())
                self._generation.setdefault(region.id, 0)
                problems = region_readiness_problems(region, windows)
                if problems:
                    not_ready[region.id] = problems
                    continue
                self._regions[region.id] = region
            self._region_order = [rid for rid in self._region_order if rid in self._regions]
            self._region_order.extend(rid for rid in self._regions if rid not in self._region_order)
            self._condition.notify_all()
            for region_id, problems in not_ready.items():
                runtime = self._runtime[region_id]
                runtime.failed = False
                runtime.status = RegionStatus.PAUSED
                runtime.detail = readiness_detail(problems)
        if notify:
            for region_id in not_ready:
                self._emit_health(region_id)
        return not_ready

    def _mark_unready(self, region_id: str, problems: list[str]) -> None:
        """把区域标记为“未就绪”：不采集、状态暂停、只更新说明，不发任何通知。"""
        with self._condition:
            self._regions.pop(region_id, None)
            self._runtime.setdefault(region_id, _Runtime())
            self._generation.setdefault(region_id, 0)
            self._invalidate_locked(region_id)
            runtime = self._runtime[region_id]
            runtime.failed = False
            runtime.status = RegionStatus.PAUSED
            runtime.detail = readiness_detail(problems)
        self._emit_health(region_id)

    def start(self) -> None:
        if any(thread and thread.is_alive() for thread in (self._capture_thread, self._ocr_thread)):
            if self._stop.is_set():
                raise RuntimeError("上一次监控任务仍在退出，请稍后重试")
            return
        self._stop.clear()
        # 启动瞬间重新过一遍门禁：运行期间配置可能被改动。
        self._apply_readiness_gate()
        now = time.monotonic()
        with self._condition:
            for region_id in self._runtime:
                self._next_due.setdefault(region_id, now)
        prepare = getattr(self.capturer, "prepare", None)
        if self._regions and callable(prepare):
            prepare()
        self._capture_thread = threading.Thread(target=self._capture_loop, name="capture", daemon=True)
        self._ocr_thread = threading.Thread(target=self._ocr_loop, name="ocr-worker", daemon=True)
        self._capture_thread.start()
        self._ocr_thread.start()

    def request_stop(self) -> None:
        """Invalidate current work immediately; the UI joins workers off-thread."""
        self._stop.set()
        with self._condition:
            for region_id in self._runtime:
                self._invalidate_locked(region_id)
            self._condition.notify_all()

    def update_poll_interval(self, milliseconds: int) -> None:
        """运行时更新采样间隔，并从每区最近一次实际采集开始重算到期时间。"""
        interval = int(milliseconds)
        if interval < 100:
            raise ValueError("采集间隔不能小于 100ms")
        now = time.monotonic()
        with self._condition:
            self._poll_interval_ms = interval
            self.config.poll_interval_ms = interval
            for region_id in self._regions:
                runtime = self._runtime[region_id]
                if runtime.last_capture_start:
                    self._next_due[region_id] = runtime.last_capture_start + self._task_interval(region_id) / 1000
                else:
                    self._next_due[region_id] = now
            self._condition.notify_all()

    def _task_interval(self, region_id: str) -> int:
        region = self._regions.get(region_id)
        return int(getattr(region, "poll_interval_ms", 0) or self._poll_interval_ms)

    def is_alive(self) -> bool:
        return any(thread and thread.is_alive() for thread in (self._capture_thread, self._ocr_thread))

    def in_flight(self) -> int:
        """当前处理中的任务数 = 采集队列长度 + OCR 线程正在处理的帧数。

        **"队列为空"不等于"处理完成"**：单 OCR 线程取走最后一帧后队列就空了，
        但那一帧仍在识别中。诊断必须能看到这个差值，否则会把"还在算"误判成"已空闲"。
        """
        with self._condition:
            return len(self._pending) + self._ocr_active

    def stop(self) -> None:
        self.request_stop()
        close = getattr(self.capturer, "close", None)
        if callable(close):
            close()
        for thread in (self._capture_thread, self._ocr_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=5)

    def pause_region(self, region_id: str) -> None:
        with self._condition:
            self._regions.pop(region_id, None)
            self._generation.setdefault(region_id, 0)
            self._runtime.setdefault(region_id, _Runtime())
            self._invalidate_locked(region_id)
            runtime = self._runtime[region_id]
            runtime.status = RegionStatus.PAUSED
            runtime.detail = "用户暂停"
        self._emit_health(region_id)

    def resume_region(self, region_id: str) -> bool:
        """恢复单个区域的采集；未就绪或区域已删除时保持暂停并返回 False。

        只有通过就绪门禁（已圈选、已预览确认、绑定窗口身份一致、参考帧有效）
        的区域才会重新进入采集队列，恢复后依旧先建立基线。
        """
        region = next((item for item in self.config.regions if item.id == region_id), None)
        if region is None:
            self._mark_unready(region_id, ["配置中已找不到该区域，可能已被删除"])
            return False
        problems = region_readiness_problems(region, self.readiness_windows())
        if problems:
            self._mark_unready(region_id, problems)
            return False
        with self._condition:
            self._generation.setdefault(region_id, 0)
            self._runtime.setdefault(region_id, _Runtime())
            self._regions[region_id] = region
            self._invalidate_locked(region_id, retry_now=True)
            runtime = self._runtime[region_id]
            runtime.status = RegionStatus.BASELINING
            runtime.detail = "恢复后重新建立基线"
            runtime.failed = False
        self._emit_health(region_id)
        return True

    def update_keywords(self, keywords: list[str]) -> None:
        with self._condition:
            self.config.keywords = keywords
            self.rules.set_keywords(keywords)

    def _invalidate_locked(self, region_id: str, retry_now: bool = False) -> None:
        self._generation[region_id] += 1
        pending_generation = self._pending_generation.get(region_id)
        had_pending = region_id in self._pending
        self._pending.pop(region_id, None)
        self._pending_generation.pop(region_id, None)
        self._queued_at.pop(region_id, None)
        self._order = deque(item for item in self._order if item != region_id)
        # A queued frame owns busy after capture returns and before OCR dequeue. If
        # invalidation removes that frame, no OCR finally will run to release it.
        # A capture still in progress has no pending frame, and an OCR owner is
        # explicitly tracked, so neither ownership can be cleared here.
        if had_pending and region_id not in self._ocr_busy:
            if self._busy_generation.get(region_id) == pending_generation:
                self._busy_generation.pop(region_id, None)
        self._runtime[region_id].last_submit_at = 0
        # 故障、暂停、恢复、重新校准、停止都会走到这里，因此"任何状态变化都不许
        # 复用旧画面"只需要挂在这一处；漏掉哪个入口都会变成难查的错复用。
        self._drop_ocr_cache(region_id)
        if retry_now:
            # 用户恢复或重新校准允许下一轮立即建立基线；持续错误仍按采样间隔重试。
            self._next_due[region_id] = time.monotonic()
        self._condition.notify_all()

    def _drop_ocr_cache(self, region_id: str) -> None:
        """丢弃该区域的 OCR 复用缓存；未启用复用（0 秒）时是空操作。"""
        if self.ocr_reuse is not None:
            self.ocr_reuse.invalidate(region_id)

    def _ocr_context(
        self, region: object, region_id: str, generation: int
    ) -> OcrCacheContext | None:
        """组装缓存键上下文；复用未启用或缺区域配置时返回 ``None``（直通）。"""
        if self.ocr_reuse is None or region is None:
            return None
        rect = region.rect
        return OcrCacheContext(
            region_id=region_id,
            generation=generation,
            reference_width=int(getattr(region, "reference_frame_width", 0) or 0),
            reference_height=int(getattr(region, "reference_frame_height", 0) or 0),
            backend=str(getattr(region, "capture_backend", "") or ""),
            roi=(rect.x, rect.y, rect.width, rect.height),
            content_mode=getattr(region, "content_mode", "wechat_chat"),
        )

    def _recognize(self, frame: FrameSample, context: OcrCacheContext | None) -> object:
        """唯一识别入口：有上下文才走复用层，否则原样直通 ``self.ocr``。"""
        if self.ocr_reuse is None or context is None:
            return self.ocr.recognize(frame)
        return self.ocr_reuse.recognize(frame, context=context)

    def ocr_reuse_stats(self, region_id: str) -> ReuseStats:
        """该区域的复用计数；未启用复用或还没采过样时返回零值。"""
        if self.ocr_reuse is None:
            return ReuseStats()
        return self.ocr_reuse.stats_for(region_id)

    def ocr_reuse_summary(self) -> dict:
        """跨区域汇总，供健康快照与探针报告使用（字段名与报告保持一致）。"""
        if self.ocr_reuse is None:
            return {
                "enabled": False,
                "lookups": 0,
                "reused": 0,
                "cache_hits": 0,
                "hit_rate": 0.0,
                "invalidations": 0,
                "max_age_seconds": 0.0,
                "per_region": {},
            }
        summary = self.ocr_reuse.summary()
        summary["enabled"] = True
        return summary

    def _fail_region(
        self,
        region_id: str,
        generation: int,
        detail: str,
        *,
        code: object = "",
        trace: object = None,
        stage: str = "",
    ) -> None:
        """进入故障（ERROR）。

        ``detail`` 里带上 ``request_id`` / ``stage_failed`` / 分段耗时：界面上的
        故障说明与 ``capture-stages.log`` 因此能对上同一次请求。本轮**不重试**，
        其他区域继续采集（语义与 0.2.1 完全一致，只补证据）。
        """
        description = describe_failure(detail, code, trace)
        stage_failed = str(getattr(trace, "stage_failed", "") or stage)
        with self._condition:
            if self._generation[region_id] != generation or region_id not in self._regions:
                return
            self._invalidate_locked(region_id)
            runtime = self._runtime[region_id]
            if not runtime.fault_started_at:
                runtime.capture_fault_count += 1
                runtime.fault_started_at = time.monotonic()
                runtime.capture_recovery_ms = 0.0
            runtime.failed = True
            runtime.status = RegionStatus.ERROR
            runtime.detail = description
            runtime.capture_error_stage = stage_failed
            runtime.capture_error_summary = failure_summary(stage_failed, code)
        request_id = str(getattr(trace, "request_id", "") or "")
        total_ms = float(getattr(trace, "total_ms", 0.0) or 0.0)
        orphan = bool(getattr(trace, "orphan_suspected", False))
        self.logger.warning(
            "region %s entered fault code=%s request_id=%s stage_failed=%s "
            "total_ms=%.1f orphan_suspected=%s detail=%s",
            region_id,
            code or "unknown",
            request_id,
            stage_failed,
            total_ms,
            orphan,
            description,
        )
        # 与请求编号同号的阶段行：P1（跨区域串扰）靠 region_id 区分，
        # P2（采集线程被占住）靠 request_id + 分段耗时定位。
        log_capture_stage(
            self.stage_logger,
            stage="fault",
            region_id=region_id,
            request_id=request_id,
            elapsed_ms=total_ms,
            outcome=str(code or "unknown"),
            error_code=str(code or ""),
        )
        self._emit_health(region_id)

    def _pause_region(
        self, region_id: str, generation: int, code: CaptureErrorCode
    ) -> None:
        """窗口最小化 / 桌面锁定：暂停而不是故障，恢复后重新建立基线。"""
        with self._condition:
            if self._generation[region_id] != generation or region_id not in self._regions:
                return
            self._invalidate_locked(region_id)
            runtime = self._runtime[region_id]
            runtime.failed = False
            runtime.status = RegionStatus.PAUSED
            runtime.detail = _PAUSE_DETAILS.get(code, "已暂停：采集条件不满足")
        log_capture_stage(
            self.stage_logger, stage="paused", region_id=region_id, outcome=str(code)
        )
        self._emit_health(region_id)

    def _record_capture_segment(self, region_id: str, started: float) -> None:
        """采集段耗时：优先用采集器自报的 ``CaptureTrace.total_ms``。

        子进程的 popen / 等帧 / kill / 解码分段都在 trace 里，这里**不重复计时**；
        只有假采集器（没有 ``last_trace``）才退回墙钟，保证离线用例也有样本。
        """
        trace = getattr(self.capturer, "last_trace", None)
        total_ms = float(getattr(trace, "total_ms", 0.0) or 0.0)
        if total_ms <= 0.0:
            total_ms = (time.perf_counter() - started) * 1000.0
        self.metrics.record(STAGE_CAPTURE, total_ms, region_id)

    def _capture_budget_ms(self) -> float:
        """当前采集预算（毫秒）：生产路径直接取采集客户端自己的 ``timeout``。

        假采集器（测试、离线探针）没有 ``client.timeout`` 时退回常量，
        保证 cycle 行永远有这个数字位。
        """
        client = getattr(self.capturer, "client", None)
        timeout = getattr(client, "timeout", None)
        if isinstance(timeout, int | float) and not isinstance(timeout, bool) and timeout > 0:
            return float(timeout) * 1000.0
        return DIAGNOSTIC_CAPTURE_TIMEOUT_MS

    def _log_cycle(
        self,
        cycle_started: float,
        interval_ms: int,
        region_count: int,
        schedule_wait_ms: float = 0.0,
        laggard_id: str = "",
    ) -> None:
        """写一轮采集循环的墙钟耗时、采样间隔与预算（P2 / P3 的判据）。

        * ``cycle_elapsed_ms`` 是整轮循环体的实际耗时；
        * ``schedule_wait_ms`` 是“真正开始采集 − 原定 ``_next_due``”的最大值，
          这是 P2 分辨“卡在排队”与“卡在子进程”的唯一直接证据；
        * ``interval_ms`` / ``capture_timeout_ms`` / ``wgc_timeout_ms`` 让这一行
          自己就能证明那次运行用的是不是生产参数（P3），不必再去翻报告；
        * ``in_flight`` = 采集队列 + 正在 OCR 的帧数，反映是否在处理积压。

        只有排队等待超过 ``SCHEDULE_WAIT_REPORT_MS`` 才署名 ``region_id``，
        避免 60 秒采样下每轮都挂一个没意义的区域号。
        """
        finished = time.monotonic()
        elapsed_ms = round((finished - cycle_started) * 1000.0, 1)
        overdue = elapsed_ms > interval_ms or schedule_wait_ms > interval_ms
        with self._condition:
            in_flight = len(self._pending) + self._ocr_active
        # 每个采样周期结束记一次"本轮处理中任务数"：队列为空不等于处理完成。
        self.metrics.record_in_flight(in_flight)
        budget_ms = self._capture_budget_ms()
        laggard = laggard_id if schedule_wait_ms >= SCHEDULE_WAIT_REPORT_MS else ""
        self.logger.info(
            "capture cycle_started_at=%.3f cycle_finished_at=%.3f cycle_elapsed_ms=%.1f "
            "interval_ms=%d regions=%d schedule_wait_ms=%.1f in_flight=%d overdue=%s",
            cycle_started,
            finished,
            elapsed_ms,
            interval_ms,
            region_count,
            schedule_wait_ms,
            in_flight,
            "yes" if overdue else "no",
        )
        log_capture_stage(
            self.stage_logger,
            stage="cycle",
            region_id=laggard,
            elapsed_ms=elapsed_ms,
            interval_ms=interval_ms,
            capture_timeout_ms=budget_ms,
            wgc_timeout_ms=DIAGNOSTIC_WGC_TIMEOUT_MS,
            schedule_wait_ms=round(schedule_wait_ms, 1),
            in_flight=in_flight,
            outcome="overdue" if overdue else "ok",
        )

    def _capture_loop(self) -> None:
        wait_ready = getattr(self.capturer, "wait_ready", None)
        if callable(wait_ready) and self._regions and not self._stop.is_set():
            with self._condition:
                warming_ids = list(self._regions)
                for region_id in warming_ids:
                    runtime = self._runtime[region_id]
                    runtime.status = RegionStatus.BASELINING
                    runtime.detail = "采集组件正在预热，尚未开始采样"
            for region_id in warming_ids:
                self._emit_health(region_id)
            warmup_started = time.monotonic()
            warmup_error = None
            try:
                ready = bool(wait_ready(5.0))
            except Exception as error:  # noqa: BLE001 - 预热失败转健康状态，不结束调度线程
                warmup_error = error
                self.logger.exception("capture worker warmup failed")
                ready = False
            warmup_ms = (time.monotonic() - warmup_started) * 1000.0
            failure_code = getattr(warmup_error, "code", CaptureErrorCode.CAPTURE_TIMEOUT)
            failure_trace = getattr(warmup_error, "trace", None) or SimpleNamespace(
                stage_failed="worker_ready", total_ms=warmup_ms
            )
            failure_detail = (str(warmup_error) if getattr(warmup_error, "trace", None) is not None
                              else "采集组件预热超时，尚未开始采样；按采样周期重试")
            if self._stop.is_set():
                return
            with self._condition:
                current_ids = list(self._regions)
                now = time.monotonic()
                for region_id in current_ids:
                    # 预热独立于采样：就绪时才开始计首轮到期，不把预热算成调度延迟。
                    self._next_due[region_id] = now if ready else now + self._task_interval(region_id) / 1000
                    if ready:
                        self._runtime[region_id].detail = "组件已就绪，等待新帧建立基线"
            log_capture_stage(
                self.stage_logger, stage="worker_warmup", elapsed_ms=warmup_ms,
                outcome="ready" if ready else "failed",
                error_code="" if ready else str(failure_code),
            )
            for region_id in current_ids:
                if self._stop.is_set():
                    return
                if ready:
                    self._emit_health(region_id)
                else:
                    with self._condition:
                        generation = self._generation[region_id]
                    self._fail_region(
                        region_id, generation, failure_detail,
                        code=failure_code, trace=failure_trace,
                    )
        while not self._stop.is_set():
            with self._condition:
                selected = None
                while not self._stop.is_set():
                    now = time.monotonic()
                    slots_available = len(self._pending) < self._queue_capacity
                    eligible = [
                        region for region in self._regions.values()
                        if region.id not in self._busy_generation
                        and self._next_due.get(region.id, now) <= now
                    ] if slots_available else []
                    if eligible:
                        earliest = min(self._next_due[region.id] for region in eligible)
                        tied = [r for r in eligible if self._next_due[r.id] == earliest]
                        tied.sort(key=lambda r: self._fair_rank_locked(r.id))
                        region = tied[0]
                        region_id = region.id
                        generation = self._generation[region_id]
                        runtime = self._runtime[region_id]
                        scheduled = self._next_due.get(region_id, now)
                        started = now
                        interval_ms = self._task_interval(region_id)
                        delay_ms = max(0.0, (started - scheduled) * 1000)
                        runtime.previous_capture_start = runtime.last_capture_start
                        if runtime.last_capture_start:
                            runtime.actual_interval_ms = (started - runtime.last_capture_start) * 1000
                        runtime.last_capture_start = started
                        runtime.schedule_delay_ms = delay_ms
                        runtime.overloaded = delay_ms >= max(
                            SCHEDULE_WAIT_REPORT_MS, interval_ms * 0.1
                        )
                        self._next_due[region_id] = started + interval_ms / 1000
                        self._busy_generation[region_id] = generation
                        self._advance_fair_cursor_locked(region_id)
                        force = runtime.failed or time.time() - runtime.last_submit_at >= self.config.force_ocr_seconds
                        selected = (region, region_id, generation, scheduled, started, interval_ms, delay_ms, force)
                        break
                    # 只等待有资格变为到期的区域；被 busy 阻挡的区域靠 OCR/capture
                    # finally 通知唤醒，避免到期时间已过后无意义地忙循环。
                    future_due = [
                        due for rid, due in self._next_due.items()
                        if rid in self._regions and rid not in self._busy_generation and due > now
                    ] if slots_available else []
                    timeout = max(0.0, min(future_due) - now) if future_due else None
                    self._condition.wait(timeout)
                if self._stop.is_set():
                    return
            region, region_id, generation, scheduled, started, interval_ms, delay_ms, force = selected
            self.metrics.record(STAGE_SCHEDULE_WAIT, delay_ms, region_id)
            self.logger.info(
                "capture attempt region=%s due=%.3f started=%.3f schedule_delay_ms=%.1f "
                "configured=%d enabled=%d due_count=1 sampled=1 queue=%d capacity=%d",
                region_id, scheduled, started, delay_ms, len(self.config.regions),
                len(self._regions), len(self._pending), self._queue_capacity,
            )
            try:
                if self._capture_generation.get(region_id) != generation:
                    self.capturer.reset(region_id)
                    self._capture_generation[region_id] = generation
                capture_started = time.perf_counter()
                frame = self.capturer.capture(region, force=force)
                self._record_capture_segment(region_id, capture_started)
                recovered = False
                with self._condition:
                    if generation != self._generation[region_id] or region_id not in self._regions:
                        continue
                    if self._runtime[region_id].failed or self._runtime[region_id].status is RegionStatus.PAUSED:
                        self._invalidate_locked(region_id)
                        generation = self._generation[region_id]
                        self._busy_generation[region_id] = generation
                        self._runtime[region_id].status = RegionStatus.BASELINING
                        self._runtime[region_id].detail = "恢复后重新建立基线"
                        self._runtime[region_id].failed = False
                        recovered = True
                if recovered:
                    log_capture_stage(self.stage_logger, stage="baseline_started", region_id=region_id, outcome="baseline")
                    self._emit_health(region_id)
                if frame:
                    self._submit(frame, generation)
                    # _submit may reject stale/removed frames. Only accepted work keeps busy.
                    with self._condition:
                        queued = region_id in self._pending
                    if not queued:
                        with self._condition:
                            # OCR may dequeue immediately after _submit returns. In that
                            # case pending is false but the region remains owned by OCR.
                            if (
                                self._busy_generation.get(region_id) == generation
                                and region_id not in self._ocr_busy
                            ):
                                self._busy_generation.pop(region_id, None)
                                self._condition.notify_all()
                else:
                    with self._condition:
                        if self._busy_generation.get(region_id) == generation:
                            self._busy_generation.pop(region_id, None)
                            self._condition.notify_all()
            except CaptureError as error:
                trace = getattr(self.capturer, "last_trace", None)
                if error.code in _PAUSE_DETAILS:
                    self._pause_region(region_id, generation, error.code)
                else:
                    self._fail_region(region_id, generation, str(error), code=error.code, trace=trace)
            except Exception as error:
                self.logger.exception("capture failed for region %s", region_id)
                self._fail_region(region_id, generation, f"未预期采集错误：{type(error).__name__}")
            finally:
                with self._condition:
                    if self._busy_generation.get(region_id) == generation:
                        # A queued frame owns the busy marker until OCR finally completes.
                        if region_id not in self._pending and region_id not in self._ocr_busy:
                            self._busy_generation.pop(region_id, None)
                    self._condition.notify_all()
                self._log_cycle(started, interval_ms, 1, delay_ms, region_id)

    def _fair_rank_locked(self, region_id: str) -> int:
        try:
            position = self._region_order.index(region_id)
        except ValueError:
            return len(self._region_order)
        return (position - self._fair_cursor - 1) % max(1, len(self._region_order))

    def _advance_fair_cursor_locked(self, region_id: str) -> None:
        try:
            self._fair_cursor = self._region_order.index(region_id)
        except ValueError:
            self._fair_cursor = -1

    def _submit(self, frame: FrameSample, generation: int | None = None) -> None:
        with self._condition:
            expected = self._generation[frame.region_id]
            if self._stop.is_set() or frame.region_id not in self._regions:
                return
            if generation is not None and generation != expected:
                return
            runtime = self._runtime[frame.region_id]
            runtime.last_frame_at = frame.captured_at
            runtime.last_submit_at = time.time()
            if frame.region_id in self._pending:
                runtime.dropped_frames += 1
            else:
                while len(self._pending) >= self._queue_capacity and self._order:
                    dropped = self._order.popleft()
                    self._pending.pop(dropped, None)
                    self._pending_generation.pop(dropped, None)
                    # 被挤掉的帧不会再被 OCR 取出，入队时刻要一并清掉，
                    # 否则 _queued_at 会留下没有对应 _pending 的孤儿条目。
                    self._queued_at.pop(dropped, None)
                    self._runtime[dropped].dropped_frames += 1
                self._order.append(frame.region_id)
            self._pending[frame.region_id] = frame
            self._pending_generation[frame.region_id] = expected
            # 入队时刻 = ocr_queue 段起点；与 _pending 同锁，避免与取出竞态。
            self._queued_at[frame.region_id] = time.perf_counter()
            in_flight = len(self._pending) + self._ocr_active
            self._condition.notify_all()
        self.metrics.set_in_flight(in_flight)

    def _ocr_loop(self) -> None:
        while not self._stop.is_set():
            with self._condition:
                while not self._order and not self._stop.is_set():
                    self._condition.wait()
                if self._stop.is_set():
                    return
                region_id = self._order.popleft()
                frame = self._pending.pop(region_id)
                generation = self._pending_generation.pop(region_id)
                queued_at = self._queued_at.pop(region_id, 0.0)
                # 复用上下文需要区域配置（后端 / 参考帧尺寸 / ROI），在锁内取一次，
                # 避免 recognize 时再取锁（那会与采集线程的失效路径互相等）。
                region = self._regions.get(region_id)
                self._ocr_busy[region_id] = generation
                self._busy_generation.setdefault(region_id, generation)
                # 与队列长度一起构成 cycle 行的 in_flight（诊断用，不参与调度）。
                self._ocr_active += 1
                in_flight = len(self._pending) + self._ocr_active
            self.metrics.set_in_flight(in_flight)
            if queued_at > 0.0:
                self._runtime[region_id].ocr_queue_ms = (time.perf_counter() - queued_at) * 1000.0
                self.metrics.record(
                    STAGE_OCR_QUEUE, self._runtime[region_id].ocr_queue_ms, region_id
                )
            runtime = self._runtime[region_id]
            pipeline_stage = "ocr"
            try:
                # 静止画面复用只跳过"像素→文字"：下面的跟踪、规则、写库、通知
                # 一律用本次帧走完整流程。
                context = self._ocr_context(region, region_id, generation)
                frame.content_mode = region.content_mode
                observation = self._recognize(frame, context)
                observation.content_mode = region.content_mode
                observation.strict_text = bool(region.rule_group_id) or region.content_mode != "wechat_chat"
                # 复用轮**不写** STAGE_OCR_INFER 样本：写 0.0 会把 P50/P95 稀释成一个
                # 假象（"OCR 只要 0ms"）。口径窄但诚实优于数字漂亮但含义模糊——
                # 复用省了多少次推理由 ocr_reused 计数器体现，不由耗时分布体现。
                reused = bool(getattr(observation, "reused", False))
                if not reused:
                    self.metrics.record(STAGE_OCR_INFER, observation.inference_ms, region_id)
                # storage_write 段：事件落库 + 识别候选 + 命中图写盘，合并成一个样本。
                storage_ms = 0.0
                with self._condition:
                    region = self._regions.get(region_id)
                    if (
                        self._stop.is_set() or not region
                        or generation != self._generation[region_id]
                        or runtime.failed
                    ):
                        continue
                    if self._tracker_generation.get(region_id) != generation:
                        self.tracker.reset(region_id)
                        self._tracker_generation[region_id] = generation
                    # 复用轮的 inference_ms 是 0.0，**不能**盖掉上一次真实推理的耗时：
                    # 界面会直接渲染成 "OCR 0ms"，用户看到 0ms/队列 0/覆盖 0 会以为
                    # 识别挂了。复用轮保留上一次的真实读数不动。
                    if not reused:
                        runtime.last_ocr_ms = observation.inference_ms
                    postprocess_started = time.perf_counter()
                    pipeline_stage = "tracking"
                    all_messages = blocks_to_messages(observation)
                    postprocess_ms = (time.perf_counter() - postprocess_started) * 1000.0
                    tracking_started = time.perf_counter()
                    messages = self.tracker.process(
                        observation, image=frame.image, current_messages=all_messages
                    )
                    # ⚠️ 复用轮这里**也必须**逐条跑 decide()。静止画面复用只跳过
                    # "像素 → 文字"那一次 ONNX，规则判定一次都不能省：去重键
                    # （tracking_id + 归一化文本）、冷却期、关键字命中全都在 decide()
                    # 里算。将来若有人看到"复用轮还在跑模型打分"并把它当性能缺陷
                    # 优化掉，静止消息就会要么反复通知、要么永远不再被检查。
                    decisions = {
                        message.tracking_id: self.rules.decide(message)
                        for message in all_messages
                    }
                    tracking_ms = (time.perf_counter() - tracking_started) * 1000.0
                    # Keep generation validation and durable enqueue atomic w.r.t.
                    # pause/error. The expensive OCR and image IO stay outside.
                    storage_started = time.perf_counter()
                    events = self.rules.process(
                        messages, region.alias, decisions=decisions
                    )
                    storage_ms += (time.perf_counter() - storage_started) * 1000.0
                # 埋点在条件锁**外**写：临界区只留状态变更，避免采集线程等锁时
                # 连带等 metrics 的锁（两把锁固定按 condition → metrics 单向获取，
                # 这里退到锁外是进一步收紧，不是改变顺序）。
                self.metrics.record(STAGE_POSTPROCESS, postprocess_ms, region_id)
                self.metrics.record(STAGE_TRACKING_RULES, tracking_ms, region_id)
                storage_io_started = time.perf_counter()
                if self.recognition_store is not None:
                    pipeline_stage = "record_write"
                    normal_messages = [
                        message for message in all_messages if message.kind == "normal_text"
                    ]
                    matched_by_tracking = {
                        message.tracking_id: decisions[message.tracking_id].matched_keywords
                        for message in normal_messages
                    }
                    # 判断已经在上面算好（decide() 一次都不能省），这里只是把它
                    # 和候选放进**同一个事务**：0.2.2 之前是写候选 + 逐条
                    # mark_decisions 两次 BEGIN/COMMIT，现在一帧只有一次。
                    # 只对 normal_text 传；缺失键由存储层保持 observed，不猜。
                    decisions_by_tracking = {
                        message.tracking_id: decision_state(decisions[message.tracking_id])
                        for message in normal_messages
                    }
                    self.recognition_store.record_candidates(
                        region_id,
                        region.alias,
                        observation.sampled_at,
                        normal_messages,
                        matched_by_tracking,
                        decisions_by_tracking=decisions_by_tracking,
                    )
                if self.config.save_hit_images:
                    pipeline_stage = "hit_image_write"
                    by_tracking = {message.tracking_id: message for message in messages}
                    for event in events:
                        message = by_tracking.get(event.tracking_id)
                        if message:
                            self.hit_images.save(event, message, frame.image)
                storage_ms += (time.perf_counter() - storage_io_started) * 1000.0
                self.metrics.record(STAGE_STORAGE_WRITE, storage_ms, region_id)
                # 恢复必须等本次新帧的识别、规则和落盘都完成；后台落盘期间
                # 暂停/重校准可能改变代数，旧结果不能把暂停状态盖成 RUNNING。
                with self._condition:
                    if (
                        self._stop.is_set() or region_id not in self._regions
                        or generation != self._generation[region_id] or runtime.failed
                    ):
                        continue
                    recovered = bool(runtime.fault_started_at)
                    if recovered:
                        runtime.capture_recovery_ms = max(
                            0.0, (time.monotonic() - runtime.fault_started_at) * 1000
                        )
                        runtime.fault_started_at = 0.0
                    runtime.status = RegionStatus.RUNNING
                    runtime.last_success_at = time.time()
                    runtime.capture_error_stage = ""
                    runtime.capture_error_summary = ""
                    runtime.detail = (
                        f"监控中；调度延迟 {runtime.schedule_delay_ms:.0f} ms，"
                        f"OCR 排队 {runtime.ocr_queue_ms:.0f} ms"
                        if runtime.overloaded or runtime.ocr_queue_ms >= self._task_interval(region_id)
                        else "监控中；无新消息时也保持此状态"
                    )
                if recovered:
                    log_capture_stage(
                        self.stage_logger, stage="recovered", region_id=region_id,
                        generation=generation, outcome="baseline_complete",
                        recovery_ms=runtime.capture_recovery_ms,
                    )
                self._emit_health(region_id)
                if events:
                    self.on_events(events)
            except Exception as error:
                self.logger.exception("frame processing failed for region %s stage=%s", region_id, pipeline_stage)
                self._fail_region(
                    region_id, generation,
                    f"{failure_summary(pipeline_stage)}：{type(error).__name__}: {error}"[:300],
                    stage=pipeline_stage,
                )
            finally:
                # 处理完成标记必须在 finally 里打：无论真推理、复用命中还是识别
                # 失败，这一帧都已被"处理"（失败路径会进故障状态，不会被当成
                # 已完成的成功帧）。回调异常不允许影响监控线程。
                try:
                    self.on_frame_processed(region_id, int(frame.sequence))
                except Exception:  # noqa: BLE001 - 验收回调不能拖垮 OCR 线程
                    self.logger.exception("on_frame_processed callback failed")
                with self._condition:
                    self._ocr_active = max(0, self._ocr_active - 1)
                    if self._ocr_busy.get(region_id) == generation:
                        self._ocr_busy.pop(region_id, None)
                    if self._busy_generation.get(region_id) == generation:
                        self._busy_generation.pop(region_id, None)
                    in_flight = len(self._pending) + self._ocr_active
                    self._condition.notify_all()
                self.metrics.set_in_flight(in_flight)

    def _emit_health(self, region_id: str) -> None:
        runtime = self._runtime[region_id]
        reuse = self.ocr_reuse_stats(region_id)
        process_stats = {}
        provider = getattr(getattr(self.capturer, "client", None), "worker_stats", None)
        if callable(provider):
            try:
                snapshot = provider()
                if isinstance(snapshot, dict):
                    process_stats = snapshot
            except Exception:  # noqa: BLE001 - 诊断旁路不能影响监控
                pass
        with self._condition:
            depth = len(self._pending)
            in_flight = depth + self._ocr_active
            next_retry_at = (
                time.time() + max(0.0, self._next_due.get(region_id, time.monotonic()) - time.monotonic())
                if runtime.status is RegionStatus.ERROR and region_id in self._regions
                else 0.0
            )
        self.on_health(
            HealthSnapshot(
                region_id=region_id,
                status=runtime.status,
                detail=runtime.detail,
                last_frame_at=runtime.last_frame_at,
                last_ocr_ms=runtime.last_ocr_ms,
                queue_depth=depth,
                dropped_frames=runtime.dropped_frames,
                # 分段耗时随快照送出：界面与探针读同一份数据，不必各自再算。
                stages=self.metrics.region_payload(region_id),
                stages_summary=self.metrics.compact_summary(region_id),
                in_flight=in_flight,
                # 0.2.2 T2.3 填真值：复用次数 / 缓存命中次数 / 复用命中率。
                # 两者刻意不同口径——命中但已过期时 cache_hits 涨、ocr_reused 不涨，
                # 报告里因此能区分"没有缓存"与"缓存都过期了"。
                ocr_reused=reuse.reused,
                ocr_cache_hits=reuse.hits,
                ocr_hit_rate=round(reuse.hit_rate, 4),
                target_interval_ms=self._task_interval(region_id),
                actual_interval_ms=runtime.actual_interval_ms,
                schedule_delay_ms=runtime.schedule_delay_ms,
                ocr_queue_ms=runtime.ocr_queue_ms,
                last_success_at=runtime.last_success_at,
                overloaded=runtime.overloaded or runtime.ocr_queue_ms >= self._task_interval(region_id),
                capture_fault_count=runtime.capture_fault_count,
                capture_recovery_ms=runtime.capture_recovery_ms,
                next_retry_at=next_retry_at,
                capture_worker_state=str(process_stats.get("state", "")),
                capture_process_stats=process_stats,
                capture_error_stage=runtime.capture_error_stage,
                capture_error_summary=runtime.capture_error_summary,
            )
        )
