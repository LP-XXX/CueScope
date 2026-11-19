"""分段耗时指标：只观察，不改调度。冷启动与稳定态分开统计。

**为什么先定名**：段名一旦散落在各调用点，报告里就会出现 ``capture`` /
``capture_ms`` / ``ocr`` 三种写法，半年后没人能对齐。所以段名、起止点、
以及"哪些段本期没有样本"都写在这一个文件里，调用点只引用常量。

**为什么冷启动要显式标记，而不是"前 N 个样本"**：60 秒采样下第一轮包含模型
加载、ORT 会话初始化、磁盘缓存全冷，把它混进 steady 会让 P95 永久偏高。但"前
3 个样本算冷启动"是按**区域**各自数的：第 4 个区域第一次识别（真实冷启动）
会被算成 steady，而第 1 个区域在切换窗口后的第一次识别明明是热的，却仍被算成
cold。因此哪些样本属于冷启动，只能由真正知道"这次是否做了初始化 / 是否首次
调用"的调用方用 ``cold=True`` 显式声明；``record()`` 默认进 steady。

**为什么环形缓冲**：60 秒采样下 240 个样本 ≈ 4 小时。固定容量保证内存不随时间
增长；超出的旧样本自然淘汰。cold 桶单独一个小缓冲，容量只约束内存，不决定
"哪些样本算冷启动"。

**为什么热路径不做格式化**：``record()`` 只做两次 ``append`` 与计数自增，
字符串组装留给 ``snapshot()`` / ``compact_summary()``（每轮最多一次）。
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# 段名（唯一真值）
# ---------------------------------------------------------------------------

STAGE_SCHEDULE_WAIT = "schedule_wait"
STAGE_CAPTURE = "capture"
STAGE_OCR_QUEUE = "ocr_queue"
STAGE_PREPROCESS = "preprocess"
STAGE_MODEL_INIT = "model_init"
#: 纯引擎调用（``LocalOcr._engine(prepared)`` 前后），**不含**预处理与消息后处理。
STAGE_OCR_ENGINE = "ocr_engine"
#: 识别流程总耗时（``OcrObservation.inference_ms``）：预处理 + 初始化 + 引擎 + 后处理。
#: 与 :data:`STAGE_OCR_ENGINE` 并存是为了不改变旧字段含义：前者是"一次识别多久"，
#: 后者是"纯 ONNX 多久"，两者相减就是可优化的非引擎开销。
STAGE_OCR_INFER = "ocr_infer"
STAGE_POSTPROCESS = "postprocess"
STAGE_TRACKING_RULES = "tracking_rules"
STAGE_STORAGE_WRITE = "storage_write"
STAGE_NOTIFY_QUEUE = "notify_queue"
STAGE_NOTIFY_RATE_LIMIT = "notify_rate_limit"
STAGE_NOTIFY_SEND = "notify_send"

#: 报告与健康快照里的固定顺序；调用点不得自行新增段名。
STAGES: tuple[str, ...] = (
    STAGE_SCHEDULE_WAIT,
    STAGE_CAPTURE,
    STAGE_OCR_QUEUE,
    STAGE_PREPROCESS,
    STAGE_MODEL_INIT,
    STAGE_OCR_ENGINE,
    STAGE_OCR_INFER,
    STAGE_POSTPROCESS,
    STAGE_TRACKING_RULES,
    STAGE_STORAGE_WRITE,
    STAGE_NOTIFY_QUEUE,
    STAGE_NOTIFY_RATE_LIMIT,
    STAGE_NOTIFY_SEND,
)

#: 每段的**起止点**。报告必须带上它，否则 P95 是没法解释的数字。
STAGE_WINDOWS: dict[str, tuple[str, str]] = {
    STAGE_SCHEDULE_WAIT: ("区域原定采样时刻 _next_due", "该区域真正开始调用采集"),
    STAGE_CAPTURE: ("capturer.capture() 进入", "返回帧或抛错（优先取 CaptureTrace.total_ms）"),
    STAGE_OCR_QUEUE: ("帧成功入队 _submit", "OCR 线程取出该帧"),
    STAGE_PREPROCESS: (
        "LocalOcr.recognize 进入",
        "页面背景/图片块过滤/遮罩完成，即将调用引擎",
    ),
    STAGE_MODEL_INIT: (
        "首次真正创建引擎（verify_models + RapidOCR 构造）进入",
        "构造返回（含模型加载与 ORT 会话初始化）",
    ),
    STAGE_OCR_ENGINE: ("LocalOcr._engine(prepared) 进入", "引擎返回原始结果"),
    STAGE_OCR_INFER: (
        "LocalOcr.recognize 进入",
        "返回 OcrObservation（含预处理、初始化与消息组装，取 inference_ms）",
    ),
    STAGE_POSTPROCESS: ("引擎返回原始结果", "文字块/气泡组装完成（assign_messages 返回）"),
    STAGE_TRACKING_RULES: ("FrameTracker.process 进入", "关键词判定完成"),
    STAGE_STORAGE_WRITE: ("事件落库开始", "反馈候选/命中图写盘完成"),
    STAGE_NOTIFY_QUEUE: ("事件写进 outbox（job.created_at）", "分发线程开始处理该 job"),
    STAGE_NOTIFY_RATE_LIMIT: ("进入 RateLimiter.acquire", "拿到发送令牌"),
    STAGE_NOTIFY_SEND: ("FeishuClient 发起 HTTP 请求", "收到响应或抛错"),
}

#: 段**没有样本**时的说明。报告原样输出，避免"没样本"被读成"0 毫秒"；
#: 也避免把"这次没发送"写成"发送 0ms"。文字按段给，不写通用的一句话。
STAGE_PENDING_REASONS: dict[str, str] = {
    STAGE_SCHEDULE_WAIT: "本次运行没有发生采样调度（服务未启动或没有就绪区域）",
    STAGE_CAPTURE: "没有采集样本（区域未就绪或已暂停）",
    STAGE_OCR_QUEUE: "没有帧进入 OCR 队列",
    STAGE_PREPROCESS: "没有识别样本（OCR 未运行，或全部轮次命中静止画面复用）",
    STAGE_MODEL_INIT: "本次运行没有真正初始化引擎（引擎已加载或没有识别）",
    STAGE_OCR_ENGINE: "没有真实引擎调用（未识别，或全部轮次命中静止画面复用）",
    STAGE_OCR_INFER: "没有识别样本（未识别，或全部轮次命中静止画面复用）",
    STAGE_POSTPROCESS: "没有识别样本（未识别，或全部轮次命中静止画面复用）",
    STAGE_TRACKING_RULES: "没有识别结果进入跟踪",
    STAGE_STORAGE_WRITE: "没有事件/候选写入",
    STAGE_NOTIFY_QUEUE: "本次运行没有通知进入 outbox（无命中或全部被去重/冷却）",
    STAGE_NOTIFY_RATE_LIMIT: "没有请求通过限速（没有待发送通知）",
    STAGE_NOTIFY_SEND: "没有实际 HTTP 请求（无命中、全部去重，或并发队列为空）",
}

#: cold 桶容量上限。**不决定哪些样本算冷启动**（那由 ``cold=True`` 显式声明），
#: 只约束内存：一个区域故障→恢复→重新初始化会再产生冷启动样本，保留最近几个即可。
COLD_BUCKET_CAPACITY = 3
#: 兼容旧导入名；语义已是"cold 桶容量"，不再是"前 N 个样本"。
COLD_SAMPLE_COUNT = COLD_BUCKET_CAPACITY
#: 每（区域, 段）环形缓冲容量：60 秒采样下约 4 小时。
SAMPLE_CAPACITY = 240


def percentile(values: list[float], ratio: float) -> float:
    """线性插值百分位（毫秒，保留 1 位小数）；空样本返回 0.0。

    与 ``tools/real_schedule_60s._percentile`` 同一口径（那边再取整到毫秒），
    避免报告里两套算法互相打脸。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    position = (len(ordered) - 1) * ratio
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 1)


@dataclass(frozen=True, slots=True)
class StageStats:
    """一组样本的 count / p50 / p95 / max（毫秒）。"""

    count: int = 0
    p50: float = 0.0
    p95: float = 0.0
    maximum: float = 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "count": self.count,
            "p50_ms": self.p50,
            "p95_ms": self.p95,
            "max_ms": self.maximum,
        }

    @classmethod
    def of(cls, values: list[float]) -> StageStats:
        if not values:
            return cls()
        return cls(
            count=len(values),
            p50=percentile(values, 0.5),
            p95=percentile(values, 0.95),
            maximum=round(max(values), 1),
        )


@dataclass(frozen=True, slots=True)
class StageTiming:
    """一段的 cold / steady 两桶统计。"""

    cold: StageStats = StageStats()
    steady: StageStats = StageStats()

    @property
    def total(self) -> int:
        return self.cold.count + self.steady.count

    def as_dict(self) -> dict[str, Any]:
        return {"cold": self.cold.as_dict(), "steady": self.steady.as_dict()}


class _Series:
    """单（区域, 段）样本序列：``recent`` 是环形窗，``cold`` 单列。

    ``recent`` 里连冷启动标记一起存，steady 因此在同一个环形缓冲里按标记过滤，
    不需要第三份副本；冷启动样本被环形窗淘汰后仍留在 ``cold`` 里。
    """

    __slots__ = ("cold", "recent", "total")

    def __init__(self, capacity: int, cold_capacity: int) -> None:
        self.cold: deque[float] = deque(maxlen=max(1, cold_capacity))
        self.recent: deque[tuple[int, float, bool]] = deque(maxlen=capacity)
        self.total = 0

    def add(self, value: float, cold: bool = False) -> None:
        self.total += 1
        if cold:
            self.cold.append(value)
        self.recent.append((self.total, value, cold))

    def cold_values(self) -> list[float]:
        return list(self.cold)

    def steady_values(self) -> list[float]:
        return [value for _index, value, cold in self.recent if not cold]

    def recent_values(self) -> list[float]:
        return [value for _index, value, _cold in self.recent]


class TimingRecorder:
    """线程安全的分段耗时记录器。

    采集线程、OCR 线程、UI 线程、通知线程都会写：``record()`` 与 ``in_flight``
    的读改写共用一把锁，临界区里只有 ``append`` 与整数自增。
    """

    def __init__(
        self, capacity: int = SAMPLE_CAPACITY, cold_samples: int = COLD_BUCKET_CAPACITY
    ) -> None:
        self.capacity = max(1, int(capacity))
        #: cold 桶容量（只约束内存，不决定冷启动归属）。
        self.cold_samples = max(1, int(cold_samples))
        self._lock = threading.Lock()
        self._series: dict[tuple[str, str], _Series] = {}
        self._version = 0
        self._cache: dict[tuple[str, str], tuple[int, StageTiming]] = {}
        self._in_flight = 0
        self._peak_in_flight = 0
        self._in_flight_samples = 0
        self._outcomes: dict[str, dict[str, int]] = {}

    # -- 写入 ---------------------------------------------------------------

    def record(
        self, stage: str, elapsed_ms: float, region_id: str = "", *, cold: bool = False
    ) -> None:
        """记一个样本。

        ``cold=True`` 表示这一次**真的**包含初始化或属于首次调用（由调用方判断）；
        默认进 steady。负数 / NaN / Inf / 非数字直接忽略（诊断不能被坏值污染）。
        """
        try:
            value = float(elapsed_ms)
        except (TypeError, ValueError):
            return
        if not math.isfinite(value) or value < 0:
            return
        key = (region_id, stage)
        with self._lock:
            series = self._series.get(key)
            if series is None:
                series = _Series(self.capacity, self.cold_samples)
                self._series[key] = series
            series.add(value, bool(cold))
            self._version += 1

    def record_outcome(self, category: str, outcome: str) -> None:
        """记一次业务结果（不是耗时）。只统计次数，键由调用点的常量决定。"""
        name = str(category or "").strip()
        result = str(outcome or "").strip()
        if not name or not result:
            return
        with self._lock:
            bucket = self._outcomes.setdefault(name, {})
            bucket[result] = bucket.get(result, 0) + 1

    def outcome_counts(self, category: str | None = None) -> dict[str, Any]:
        with self._lock:
            if category is not None:
                return dict(self._outcomes.get(category, {}))
            return {name: dict(bucket) for name, bucket in self._outcomes.items()}

    def set_in_flight(self, value: int) -> None:
        """当前正在处理的任务数（采集队列长度 + OCR 线程正在处理的帧数）。"""
        number = max(0, int(value))
        with self._lock:
            self._in_flight = number
            if number > self._peak_in_flight:
                self._peak_in_flight = number

    def record_in_flight(self, value: int) -> None:
        """每个采样周期结束记一次"本轮处理中任务数"，同时更新峰值。"""
        number = max(0, int(value))
        with self._lock:
            self._in_flight = number
            self._in_flight_samples += 1
            if number > self._peak_in_flight:
                self._peak_in_flight = number

    # -- 读取 ---------------------------------------------------------------

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def peak_in_flight(self) -> int:
        with self._lock:
            return self._peak_in_flight

    @property
    def in_flight_samples(self) -> int:
        with self._lock:
            return self._in_flight_samples

    def total_samples(self, stage: str) -> int:
        """该段在所有区域上的累计样本数（含已被环形缓冲淘汰的）。"""
        with self._lock:
            return sum(
                series.total
                for (_, series_stage), series in self._series.items()
                if series_stage == stage
            )

    def recent_count(self, stage: str, region_id: str = "") -> int:
        with self._lock:
            series = self._series.get((region_id, stage))
            return 0 if series is None else len(series.recent)

    def recent_values(self, stage: str, region_id: str = "") -> list[float]:
        with self._lock:
            series = self._series.get((region_id, stage))
            return [] if series is None else series.recent_values()

    def stats(self, stage: str, region_id: str = "") -> StageTiming:
        """cold / steady 两桶统计；版本号未变时复用上次结果（同轮 4 区域不重算）。"""
        key = (region_id, stage)
        with self._lock:
            version = self._version
            cached = self._cache.get(key)
            if cached is not None and cached[0] == version:
                return cached[1]
            series = self._series.get(key)
            if series is None:
                timing = StageTiming()
            else:
                timing = StageTiming(
                    cold=StageStats.of(series.cold_values()),
                    steady=StageStats.of(series.steady_values()),
                )
            self._cache[key] = (version, timing)
            return timing

    def region_ids(self) -> list[str]:
        with self._lock:
            return sorted({region for region, _ in self._series})

    def snapshot(self, region_id: str | None = None) -> dict[str, Any]:
        """JSON 安全的分段快照。``region_id`` 为 ``None`` 时按区域分组输出全部。"""
        if region_id is None:
            regions = self.region_ids()
            return {
                "capacity": self.capacity,
                "cold_samples": self.cold_samples,
                "cold_note": "cold_samples 只是 cold 桶容量；哪些样本算冷启动由 cold=True 显式标记",
                "in_flight": self.in_flight,
                "peak_in_flight": self.peak_in_flight,
                "in_flight_samples": self.in_flight_samples,
                "stage_windows": {
                    stage: list(STAGE_WINDOWS.get(stage, ("", ""))) for stage in STAGES
                },
                "pending_stages": dict(STAGE_PENDING_REASONS),
                "outcomes": self.outcome_counts(),
                "regions": {region: self.region_payload(region) for region in regions},
            }
        return self.region_payload(region_id)

    def region_payload(self, region_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for stage in STAGES:
            timing = self.stats(stage, region_id)
            payload[stage] = {
                "cold": timing.cold.as_dict(),
                "steady": timing.steady.as_dict(),
                "window": list(STAGE_WINDOWS.get(stage, ("", ""))),
            }
        return payload

    def compact_summary(self, region_id: str | None = None) -> str:
        """一行可读摘要（每轮最多一次，不参与热路径）。

        只列有样本的段：没有样本的段一律不出现，避免"0ms"被误读成"很快"。
        """
        regions = [region_id] if region_id is not None else (self.region_ids() or [""])
        parts: list[str] = []
        for region in regions:
            for stage in STAGES:
                timing = self.stats(stage, region)
                stats = timing.steady if timing.steady.count else timing.cold
                if not stats.count:
                    continue
                parts.append(
                    f"{stage}:p50={stats.p50:.1f}/p95={stats.p95:.1f}/max={stats.maximum:.1f}"
                )
        return " ".join(parts)


# ---------------------------------------------------------------------------
# OCR 分段观测：LocalOcr 产出样本，应用与探针共用同一个适配器
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OcrStageSample:
    """一次 ``LocalOcr.recognize`` 的分段耗时（毫秒）。

    ``cold_start=True`` 只表示**这一次调用**真的做了引擎初始化或属于该引擎实例的
    首次识别，由 ``LocalOcr`` 自己判断；它不是"该区域前几个样本"的推断。
    """

    region_id: str
    frame_sequence: int
    cold_start: bool
    preprocess_ms: float
    model_init_ms: float
    engine_ms: float
    postprocess_ms: float
    total_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id,
            "frame_sequence": self.frame_sequence,
            "cold_start": self.cold_start,
            "preprocess_ms": round(self.preprocess_ms, 3),
            "model_init_ms": round(self.model_init_ms, 3),
            "engine_ms": round(self.engine_ms, 3),
            "postprocess_ms": round(self.postprocess_ms, 3),
            "total_ms": round(self.total_ms, 3),
        }


class OcrStageRecorder:
    """把 :class:`OcrStageSample` 写进 ``TimingRecorder``。

    **不写** :data:`STAGE_OCR_INFER`：那一段由服务层用 ``observation.inference_ms``
    记录（识别流程总耗时）。这里只写三个可独立优化的子段 + 真实初始化，
    重复写会让同一帧在报告里出现两个 ocr_infer 样本。

    ``on_complete`` 用于探针的"本轮各区域对应序号处理完成"判定：它在样本落库后
    被调用，参数是原始样本。
    """

    def __init__(
        self,
        recorder: TimingRecorder,
        on_complete: Callable[[OcrStageSample], None] | None = None,
    ) -> None:
        self.recorder = recorder
        self._on_complete = on_complete

    def __call__(self, sample: OcrStageSample) -> None:
        cold = bool(sample.cold_start)
        region = sample.region_id
        recorder = self.recorder
        recorder.record(STAGE_PREPROCESS, sample.preprocess_ms, region, cold=cold)
        recorder.record(STAGE_OCR_ENGINE, sample.engine_ms, region, cold=cold)
        recorder.record(STAGE_POSTPROCESS, sample.postprocess_ms, region, cold=cold)
        # 只有真的构造了引擎的那一次才有样本；0 表示复用已加载的引擎。
        if sample.model_init_ms > 0.0:
            recorder.record(STAGE_MODEL_INIT, sample.model_init_ms, region, cold=True)
        if self._on_complete is not None:
            self._on_complete(sample)


# ---------------------------------------------------------------------------
# 采样完成判定：按（区域, 帧序号）处理完成，而不是"队列空了"
# ---------------------------------------------------------------------------


class RoundCompletion:
    """按（区域, 帧序号）判断本轮是否真的处理完。

    **为什么不能用队列深度**：单 OCR 线程取走最后一帧后队列就空了，但那一帧还在
    识别、跟踪、写库里；``in_flight`` 归零又可能是下一轮开始前的瞬间。健康快照
    更不可靠：它是"上一次上报"的值，可能比本轮还旧。

    因此验收与探针必须带上"本轮每区域的目标序号"，等每个区域**处理完成到该序号**
    才算结束。``mark()`` 由处理链末端调用（服务的 OCR 收尾或探针的 OCR 观测回调），
    只增不减，重复/乱序上报都不会把序号倒退。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._completed: dict[str, int] = {}

    def mark(self, region_id: str, sequence: int) -> None:
        try:
            number = int(sequence)
        except (TypeError, ValueError):
            return
        if number <= 0:
            return
        with self._lock:
            if number > self._completed.get(region_id, 0):
                self._completed[region_id] = number

    def completed_sequence(self, region_id: str) -> int:
        with self._lock:
            return self._completed.get(region_id, 0)

    def pending(self, targets: Mapping[str, int]) -> dict[str, int]:
        """``{区域: 还差到第几号}``；已达标的区域不出现。"""
        missing: dict[str, int] = {}
        with self._lock:
            for region_id, target in targets.items():
                wanted = int(target)
                done = self._completed.get(region_id, 0)
                if done < wanted:
                    missing[region_id] = wanted
        return missing

    def is_complete(self, targets: Mapping[str, int]) -> bool:
        return not self.pending(targets)

    def is_settled(self, targets: Mapping[str, int], in_flight: int) -> bool:
        """本轮真的结束了吗：序号都到了 **且** 没有在途任务。

        ``is_complete`` 只回答"OCR 是否处理到该序号"。OCR 返回后还有跟踪、规则、
        写库与健康上报，这些都在 ``in_flight`` 里；因此验收要两个条件同时成立，
        单看队列深度或单看序号都会提前结束。
        """
        try:
            active = int(in_flight)
        except (TypeError, ValueError):
            return False
        return self.is_complete(targets) and active <= 0

    def snapshot(self, targets: Mapping[str, int] | None = None) -> dict[str, Any]:
        wanted = {str(key): int(value) for key, value in (targets or {}).items()}
        with self._lock:
            completed = dict(self._completed)
        return {
            "targets": wanted,
            "completed": completed,
            "pending": {
                region_id: target
                for region_id, target in wanted.items()
                if completed.get(region_id, 0) < target
            },
        }


# ---------------------------------------------------------------------------
# 埋点自身开销（离线微基准，不参与任何断言门限）
# ---------------------------------------------------------------------------


def measure_overhead(cycles: int = 120, calls_per_cycle: int = 32) -> dict[str, Any]:
    """离线量一次埋点开销：``cycles`` 个周期、每周期 ``calls_per_cycle`` 次写入。

    一周期里实际发生的是"4 区域 × 若干段 + 若干次 in_flight"，约 30 次写入，
    所以默认 32。返回值里的 ``per_cycle_ms`` 就是"多花在打点上的时间"。

    只用于报告里说明"埋点贵不贵"：门槛是提醒级（单周期 > 2ms 要在报告里写明），
    不是硬门限——不同机器、不同负载下这个数会差几倍，写死会变成脆测试。
    """
    total_cycles = max(1, int(cycles))
    per_cycle_calls = max(1, int(calls_per_cycle))
    recorder = TimingRecorder()
    stages = list(STAGES)
    # 预热：跳过首次建 dict / 建 deque 的一次性开销，量的是稳态成本。
    for slot in range(per_cycle_calls):
        recorder.record(stages[slot % len(stages)], 1.0, "warmup")
    started = time.perf_counter()
    for cycle in range(total_cycles):
        region_id = f"r{cycle % 4}"
        for slot in range(per_cycle_calls):
            recorder.record(stages[slot % len(stages)], float(slot), region_id)
        recorder.record_in_flight(slot % 5)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return {
        "cycles": total_cycles,
        "calls_per_cycle": per_cycle_calls + 1,
        "total_ms": round(elapsed_ms, 3),
        "per_cycle_ms": round(elapsed_ms / total_cycles, 4),
        "budget_ms": 2.0,
        # 口径写清楚：这是离线微基准，不是"在真实采样里量到的增量"。真实增量要在
        # 每个打点前后各取一次 perf_counter 才能测，那本身就把测量误差加进去了。
        "method": "离线微基准：独立 recorder 上连跑 N 个周期，不叠加在真实采样上",
    }
