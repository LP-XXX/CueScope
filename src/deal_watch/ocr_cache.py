"""静止画面 OCR 复用：像素完全相同时复用上一次的**文字结果**。

**只复用"像素到文字"这一步。** 每次采样仍然要：取得新帧 → 验证 → 走本次的跟踪
（``FrameTracker``）→ 规则判定（``RuleEngine``）→ 写识别记录 → 该通知就通知。
复用跳过的只有 ONNX 推理，绝不跳过"文字到判断"。

三条设计约束，任一条破了都会变成难查的回归：

1. **只缓存像素摘要，绝不缓存图像。** 键里存 16 字节 blake2b，值里存
   ``OcrObservation``（文字块与坐标）。整窗原帧一律不落内存、不落磁盘。
2. **``sampled_at`` / ``frame_sequence`` 必须换成本次帧的值。** 直接复用上一次的
   时间戳会让 ``FrameTracker`` 收到"比上一帧还旧"的观测，静止消息的 ``last_seen``
   停在原地，界面上看起来像卡住；frame_sequence 重复还会让跟踪层的判重失效。
3. **键要能区分"长得一样但不是同一块屏幕"。** 因此除了像素摘要，还要带上
   区域、generation、参考帧尺寸、采集后端与 ROI —— 换窗口、换后端、缩放、
   重新校准之后，即使某一帧像素恰好相同，也不能算同一画面。

失效策略：``generation`` 变化（故障、暂停、恢复、重新校准、停止都会 bump）、
后端 / 参考尺寸 / ROI 变化，都在下一次 ``recognize`` 或 ``invalidate`` 时清空该
区域的条目。清空用 ``_epoch`` 兜底：正在推理的帧在失效之后才返回，也不会把旧
画面重新塞回缓存。

**``FrameSample.forced`` 与本模块完全无关，别把它接进来。** 采集层的 ``forced``
语义是"跳过 ``_changed`` 去重、强制把帧交出来"，生产 ``poll_interval_ms=60000``
下它**每一轮都是 True**（``capture.py`` 的 10 秒与 ``force_ocr_seconds`` 的 10 秒
都远小于采样间隔）。若让它绕过复用，命中率恒为 0，整个复用就是一段永不执行的
死代码。有效期只由 ``max_age_seconds`` 决定。

**``stored_at`` 只在写入时设置，命中时绝不刷新。** 这是去掉 forced 兜底后唯一
的"陈旧上限"保证：同一区域每 ``max_age_seconds`` 秒至少真跑一次 ``recognize``。
若在命中时刷新时间戳，一个永远静止的画面会被无限期复用下去。
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from .models import FrameSample, OcrObservation

# 生产默认：静止画面最多复用 300 秒（5 分钟）。0 = 关闭复用。
DEFAULT_MAX_AGE_SECONDS = 300.0
# 每个区域最多保留几个不同画面的结果（LRU）。静止画面只需要 1 个；留几个是
# 为了"两帧交替"（光标闪烁、时间戳逐秒跳动）时不会每次都 miss。
DEFAULT_MAX_ENTRIES = 4

_PIXEL_DIGEST_SIZE = 16


@dataclass(frozen=True, slots=True)
class OcrCacheContext:
    """缓存键的上下文。缺一项都可能张冠李戴。"""

    region_id: str
    generation: int
    reference_width: int = 0
    reference_height: int = 0
    backend: str = ""
    roi: tuple[int, int, int, int] = (0, 0, 0, 0)
    content_mode: str = "wechat_chat"

    def as_key_prefix(self) -> tuple[Any, ...]:
        return (
            self.region_id,
            self.generation,
            self.reference_width,
            self.reference_height,
            self.backend,
            self.roi,
            self.content_mode,
        )


@dataclass(slots=True)
class ReuseStats:
    """一个区域的复用计数。

    ``hits`` 与 ``reused`` 故意分开，两者含义不同且都有用：

    * ``hits``（对外叫 ``ocr_cache_hits``）：**内容判定命中**次数 —— 摘要、shape、
      dtype、generation 全部匹配，**包含已超龄的情况**。它反映"画面有多静止"。
    * ``reused``（对外叫 ``ocr_reused``）：**实际跳过推理**的次数。它反映"省了
      多少次 ONNX"。

    典型场景：画面 10 轮完全没变，但 ``max_age_seconds`` 只有 30 秒、一轮 60 秒，
    于是每一轮都命中却都超龄 —— ``hits`` 涨 9、``reused`` 一个不涨。这个差值就是
    运维把 ``ocr_reuse_max_age_seconds`` 调大的唯一依据；两者若接成同一个计数，
    这条信号就没了。

    **口径（单位：次查询 lookups）**：

    * ``content_hit_rate = hits / lookups``：内容命中率
    * ``reuse_rate = reused / lookups``：实际复用率（真正省掉的推理占比）
    * ``hit_rate`` 是旧字段名，**等同** ``content_hit_rate``，保留是为了不改报告
      与 ``HealthSnapshot.ocr_hit_rate`` 的历史含义；它**不是**复用率。

    没有查询样本时两个比率都没有意义，``observed`` 为 False，报告必须写"未观测"
    而不是 0%——0% 会被读成"画面一直在变"。
    """

    lookups: int = 0
    hits: int = 0
    reused: int = 0
    infer: int = 0

    @property
    def observed(self) -> bool:
        return self.lookups > 0

    @property
    def content_hit_rate(self) -> float:
        """内容命中率：命中次数 / 查询次数。"""
        return self.hits / self.lookups if self.lookups else 0.0

    @property
    def reuse_rate(self) -> float:
        """实际复用率：真正跳过推理的次数 / 查询次数。"""
        return self.reused / self.lookups if self.lookups else 0.0

    @property
    def hit_rate(self) -> float:
        """兼容旧字段：等同内容命中率（不是复用率）。"""
        return self.content_hit_rate

    def as_dict(self) -> dict[str, Any]:
        return {
            "lookups": self.lookups,
            "hits": self.hits,
            "reused": self.reused,
            "infer": self.infer,
            "observed": self.observed,
            "unit": "lookups",
            "content_hit_rate": round(self.content_hit_rate, 4),
            "reuse_rate": round(self.reuse_rate, 4),
            "hit_rate": round(self.hit_rate, 4),
            "hit_rate_semantics": "content_hit_rate（内容命中率）；旧字段名兼容，不是复用率",
        }


@dataclass(slots=True)
class _Entry:
    observation: OcrObservation
    stored_at: float
    epoch: int = 0


def pixel_signature(frame: FrameSample) -> tuple[Any, ...] | None:
    """帧的像素指纹：``(blake2b 摘要, shape, dtype)``。

    非图像帧（离线用例里的 ``FrameSample(image=["文本"])`` 之类）返回 ``None``，
    调用方据此**不复用**——宁可多跑一次 OCR，也不能对着一堆字符串算哈希崩掉。
    """
    image = getattr(frame, "image", None)
    shape = getattr(image, "shape", None)
    dtype = getattr(image, "dtype", None)
    if shape is None or dtype is None:
        return None
    try:
        array = np.ascontiguousarray(image)
    except (TypeError, ValueError):
        return None
    if not array.size:
        return None
    digest = hashlib.blake2b(array.tobytes(), digest_size=_PIXEL_DIGEST_SIZE).hexdigest()
    return (digest, tuple(int(value) for value in array.shape), str(array.dtype))


def rebind(cached: OcrObservation, frame: FrameSample) -> OcrObservation:
    """把缓存里的观测换成"本次帧"的观测：时间戳与序号用本次的。"""
    return replace(
        cached,
        region_id=frame.region_id,
        sampled_at=frame.captured_at,
        frame_sequence=frame.sequence,
        blocks=list(cached.blocks),
        uncovered_boxes=list(cached.uncovered_boxes),
        inference_ms=0.0,
        reused=True,
    )


class ReusableOcr:
    """``LocalOcr`` 的透明包装：``recognize`` 签名不变，多一个可选上下文。

    不传 ``context`` 时**完全直通**内部实现——离线用例、预览、手动补录这些
    单次调用没有区域上下文，本来也不该被缓存。

    ``max_age_seconds <= 0`` 时同样直通且不写缓存：这就是"关闭复用"的语义，
    配置为 0 的部署与 0.2.1 逐字节一致（连一次哈希都不算）。
    """

    def __init__(
        self,
        inner: Any,
        *,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.inner = inner
        self.max_age_seconds = max(0.0, float(max_age_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, OrderedDict[tuple[Any, ...], _Entry]] = {}
        self._prefix: dict[str, tuple[Any, ...]] = {}
        self._epoch: dict[str, int] = {}
        self._stats: dict[str, ReuseStats] = {}
        self.invalidations = 0

    # -- 读取 ---------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.max_age_seconds > 0.0

    def stats_for(self, region_id: str) -> ReuseStats:
        """该区域的复用计数（没有样本时返回零值对象，不是 None）。"""
        with self._lock:
            return replace(self._stats.get(region_id, ReuseStats()))

    def cached_regions(self) -> list[str]:
        with self._lock:
            return sorted(region for region, bucket in self._entries.items() if bucket)

    def entry_count(self, region_id: str) -> int:
        with self._lock:
            return len(self._entries.get(region_id) or ())

    def inspect_entries(self, region_id: str) -> list[tuple[OcrObservation, float, int]]:
        """诊断用只读快照：``[(observation, stored_at, epoch), …]``。

        存在的意义是让"缓存里到底有什么"可以被断言——尤其是那条红线：缓存条目
        里不允许出现任何带 ``.shape`` / ``.nbytes`` 的对象（图像帧）。
        """
        with self._lock:
            bucket = self._entries.get(region_id)
            if not bucket:
                return []
            return [(entry.observation, entry.stored_at, entry.epoch) for entry in bucket.values()]

    def summary(self) -> dict[str, Any]:
        """跨区域汇总，供健康快照与探针报告使用。

        比率的单位统一是**查询次数（lookups）**：``content_hit_rate = hits/lookups``、
        ``reuse_rate = reused/lookups``；``hit_rate`` 只是内容命中率的旧名。
        没有查询样本时 ``observed=False`` 且 ``sample_note`` 写"未观测"，
        调用方不得把 0.0 当成"画面一直在变"。
        """
        with self._lock:
            regions = {region: stats for region, stats in self._stats.items()}
        total = ReuseStats()
        for stats in regions.values():
            total.lookups += stats.lookups
            total.hits += stats.hits
            total.reused += stats.reused
            total.infer += stats.infer
        payload = total.as_dict()
        payload.update(
            {
                "reused": total.reused,
                "cache_hits": total.hits,
                "hit_rate": round(total.hit_rate, 4),
                "content_hit_rate": round(total.content_hit_rate, 4),
                "reuse_rate": round(total.reuse_rate, 4),
                "observed": total.observed,
                "sample_note": (
                    f"{total.lookups} 次查询"
                    if total.observed
                    else "未观测（没有查询样本）"
                ),
                "invalidations": self.invalidations,
                "max_age_seconds": self.max_age_seconds,
                "per_region": {region: stats.as_dict() for region, stats in regions.items()},
            }
        )
        return payload

    # -- 失效 ---------------------------------------------------------------

    def invalidate(self, region_id: str) -> bool:
        """丢弃该区域的全部缓存；返回是否真的丢了东西。

        ``_epoch`` 同时前进：正在推理的帧返回时（``recognize`` 的后半段）会发现
        epoch 变了，于是**不写回**缓存。否则"故障时清了缓存、结果却在清完之后
        写回来"会让下一轮复用一张故障前的画面。
        """
        with self._lock:
            bucket = self._entries.get(region_id)
            dropped = bool(bucket)
            if bucket:
                bucket.clear()
            self._prefix.pop(region_id, None)
            self._epoch[region_id] = self._epoch.get(region_id, 0) + 1
            if dropped:
                self.invalidations += 1
            return dropped

    def clear(self) -> None:
        with self._lock:
            for region_id in list(self._entries):
                self._entries[region_id].clear()
                self._epoch[region_id] = self._epoch.get(region_id, 0) + 1
            self._prefix.clear()
            self.invalidations += 1

    # -- 主流程 -------------------------------------------------------------

    def recognize(
        self, frame: FrameSample, context: OcrCacheContext | None = None
    ) -> OcrObservation:
        """命中且未过期 → 复用；否则真跑 OCR 并把结果记进缓存。

        ``frame.forced`` **不参与判定**（原因见模块 docstring）：生产采样间隔 60 秒
        下它恒为 True，接进来等于关掉复用。
        """
        if context is None or not self.enabled:
            return self.inner.recognize(frame)
        signature = pixel_signature(frame)
        if signature is None:
            return self.inner.recognize(frame)

        region_id = context.region_id
        prefix = context.as_key_prefix()
        key = (*prefix, *signature)
        now = self._clock()

        served: OcrObservation | None = None
        epoch = 0
        with self._lock:
            stats = self._stats.setdefault(region_id, ReuseStats())
            stats.lookups += 1
            bucket = self._entries.setdefault(region_id, OrderedDict())
            # 上下文变了（generation / 后端 / 参考尺寸 / ROI）→ 该区域的旧条目一律作废。
            if self._prefix.get(region_id) != prefix:
                bucket.clear()
                self._prefix[region_id] = prefix
                self._epoch[region_id] = self._epoch.get(region_id, 0) + 1
            epoch = self._epoch.get(region_id, 0)
            entry = bucket.get(key)
            if entry is not None:
                stats.hits += 1
                # 注意：命中分支只 move_to_end，**不刷新 stored_at**。
                if now - entry.stored_at <= self.max_age_seconds:
                    stats.reused += 1
                    served = entry.observation
                    bucket.move_to_end(key)
            if served is None:
                stats.infer += 1
        if served is not None:
            return rebind(served, frame)

        observation = self.inner.recognize(frame)
        stored_at = self._clock()
        with self._lock:
            # 推理期间发生过失效（故障/暂停/恢复/校准/上下文变化）→ 不写回。
            if self._epoch.get(region_id, 0) != epoch:
                return observation
            bucket = self._entries.setdefault(region_id, OrderedDict())
            # blocks / uncovered_boxes 是可变 list（TextBlock 本身 frozen 但容器不是），
            # 必须拷一份进缓存：下游任何一个 ``blocks.append()`` 都能污染缓存，
            # 让下一轮复用拿到"上次被改过的"结果。
            bucket[key] = _Entry(
                observation=replace(
                    observation,
                    blocks=list(observation.blocks),
                    uncovered_boxes=list(observation.uncovered_boxes),
                ),
                stored_at=stored_at,
                epoch=epoch,
            )
            bucket.move_to_end(key)
            while len(bucket) > self.max_entries:
                bucket.popitem(last=False)
        return observation

    # 其余接口（verify_models 等）照常透传，服务与工具不必知道这里多了一层。
    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)
