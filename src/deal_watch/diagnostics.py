"""脱敏日志、全局异常出口、采集阶段日志、命中裁剪保留和运行指标。"""

from __future__ import annotations

import hashlib
import logging
import math
import sys
import threading
import time
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Any

import numpy as np

from .models import MatchEvent, MessageCandidate

# ---------------------------------------------------------------------------
# 主日志
# ---------------------------------------------------------------------------


def configure_logging(data_dir: Path) -> logging.Logger:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("deal_watch")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    handler = RotatingFileHandler(
        log_dir / "deal-watch.log", maxBytes=10 * 1024 * 1024, backupCount=4, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    return logger


def close_logging() -> None:
    """关闭主日志与采集阶段日志的全部句柄；重复调用是安全的。"""
    for name in ("deal_watch", CAPTURE_STAGE_LOGGER_NAME):
        logger = logging.getLogger(name)
        for handler in logger.handlers[:]:
            handler.flush()
            handler.close()
            logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# 全局异常出口：槽函数/线程里的未捕获异常必须留痕，不能静默死在不可见的 stderr
# ---------------------------------------------------------------------------


def _is_exit_exception(exc_type: type[BaseException]) -> bool:
    """KeyboardInterrupt / SystemExit 是正常退出路径，不按异常记录。"""
    return issubclass(exc_type, KeyboardInterrupt | SystemExit)


def install_exception_hooks(logger: logging.Logger) -> None:
    """把主线程与子线程的未捕获异常转成 ``logging.exception`` 记录。

    只记录、不改变进程退出语义：主线程抛错后解释器仍会结束进程，但日志里
    会留下类型、消息与栈，界面程序（stderr 不可见）因此仍能事后取证。
    """
    previous_hook = sys.excepthook

    def main_hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        if _is_exit_exception(exc_type):
            previous_hook(exc_type, exc_value, exc_traceback)
            return
        try:
            logger.error(
                "未捕获异常（主线程）：%s: %s",
                exc_type.__name__,
                exc_value,
                exc_info=(exc_type, exc_value, exc_traceback),
            )
        finally:
            # 终端运行时仍然输出，方便开发期直接观察；打包为窗口程序时 stderr 为空。
            previous_hook(exc_type, exc_value, exc_traceback)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        exc_type = args.exc_type or RuntimeError
        if _is_exit_exception(exc_type):
            return
        thread_name = getattr(args.thread, "name", "") or "unknown"
        logger.error(
            "未捕获异常（线程 %s）：%s: %s",
            thread_name,
            exc_type.__name__,
            args.exc_value,
            exc_info=(exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = main_hook
    threading.excepthook = thread_hook


def qt_message_handler(logger: logging.Logger):
    """返回可交给 ``qInstallMessageHandler`` 的处理函数：只记录 Warning 及以上。

    Qt 的调试/信息级消息（元素树、字体回退等）噪声极大且与用户问题无关，
    记进日志只会淹没真正的运行期告警。
    """

    from PySide6.QtCore import QtMsgType

    levels: dict[Any, int] = {
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def handler(mode: Any, context: Any, message: str) -> None:
        level = levels.get(mode)
        if level is None:
            return
        category = str(context.file) if context is not None and context.file else "qt"
        logger.log(level, "Qt 消息 [%s] %s: %s", category, str(mode).split(".")[-1], message)

    return handler


def install_qt_message_handler(logger: logging.Logger) -> None:
    """安装 Qt 消息处理函数。

    必须在 ``QApplication`` 与 ``MainWindow`` 构造之后调用：窗口构造期产生的
    Qt 警告会走默认处理（offscreen 测试因此不受影响，也不会被本函数改写）。
    """

    from PySide6.QtCore import qInstallMessageHandler

    qInstallMessageHandler(qt_message_handler(logger))


def title_fingerprint(title: str) -> str:
    """窗口标题的脱敏指纹（前 8 位哈希）；日志里禁止出现标题原文。

    保留哈希前缀是为了让现场日志能看出“标题是否变化”（0.2.2 决策 Q1：
    ``title`` 暂时仍是强身份字段，一周后用这些指纹决定是否放宽为前缀匹配）。
    """
    return hashlib.sha256((title or "").encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------------------
# 采集阶段日志：独立文件、256KB 轮转、字段白名单
# ---------------------------------------------------------------------------

CAPTURE_STAGE_LOGGER_NAME = "deal_watch.capture_stages"
CAPTURE_STAGE_LOG_BYTES = 4 * 1024 * 1024
CAPTURE_STAGE_LOG_BACKUPS = 4

# 字段白名单：阶段名、请求编号、区域编号、耗时、结果、错误码。
# 图像、OCR 文本、聊天正文、窗口标题原文与任何凭据都不在此列。
# 数值槽单独列出（见 ``_STAGE_NUMERIC_FIELDS``）：``interval_ms`` /
# ``capture_timeout_ms`` / ``wgc_timeout_ms`` / ``schedule_wait_ms`` 是判断
# “那次跑的是不是生产参数、采集线程是不是被占住”的直接证据，
# ``in_flight`` 是同一时刻在处理的队列长度。
STAGE_FIELD_ALLOWLIST: tuple[str, ...] = (
    "stage",
    "request_id",
    "region_id",
    "elapsed_ms",
    "interval_ms",
    "capture_timeout_ms",
    "wgc_timeout_ms",
    "schedule_wait_ms",
    "in_flight",
    "outcome",
    "error_code",
    "pid",
    "active_processes",
    "orphan_processes",
    "generation",
    "worker_id",
    "deadline_overrun_ms",
    "stage_elapsed_ms",
    "stage_offset_ms",
    "process_starts",
    "failures",
    "recovery_ms",
    "private_bytes",
    "working_set_bytes",
    "handles",
    "threads",
    "cleanup_state",
)
# 数值槽：只收真正的 int/float（有限、非负）。字符串一律拒收。
_STAGE_NUMERIC_FIELDS: frozenset[str] = frozenset(
    {
        "elapsed_ms",
        "interval_ms",
        "capture_timeout_ms",
        "wgc_timeout_ms",
        "schedule_wait_ms",
        "in_flight",
        "pid",
        "active_processes",
        "orphan_processes",
        "generation",
        "worker_id",
        "deadline_overrun_ms",
        "stage_elapsed_ms",
        "stage_offset_ms",
        "process_starts",
        "failures",
        "recovery_ms",
        "private_bytes",
        "working_set_bytes",
        "handles",
        "threads",
    }
)
_STAGE_TEXT_MAX = 48
# 文本字段只保留“标识符形状”的字符：空白、引号、冒号、斜杠、中文一律剔除，
# 这样即使调用方误传了聊天正文或窗口标题，也不会原样落盘。
_STAGE_TEXT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


def _clean_stage_text(value: Any) -> str:
    text = str(value)
    kept = "".join(char for char in text[:_STAGE_TEXT_MAX] if char in _STAGE_TEXT_CHARS)
    return kept[:_STAGE_TEXT_MAX]


def _clean_stage_number(value: Any) -> int | float | None:
    """数值槽只收真正的数字：有限、非负；字符串、布尔与 NaN/Inf 一律拒收。

    ``"4999 元"`` 这种字符串如果能被 ``float()`` 转成数字落盘，字段白名单就
    形同虚设——聊天正文里的任何数字都能伪装成诊断字段。整数形态统一去掉
    小数点，便于人读（``interval_ms=60000`` 而不是 ``60000.0``）。
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return int(number) if number.is_integer() else round(number, 3)


def sanitize_stage_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """按白名单过滤阶段字段；未知键直接丢弃，文本值只留标识符字符。

    数值槽与文本槽分开处理：数值必须是真数字，文本必须是标识符形状。
    """
    clean: dict[str, Any] = {}
    for key in STAGE_FIELD_ALLOWLIST:
        if key not in fields:
            continue
        value = fields[key]
        if key in _STAGE_NUMERIC_FIELDS:
            number = _clean_stage_number(value)
            if number is not None:
                clean[key] = number
        else:
            text = _clean_stage_text(value)
            if text:
                clean[key] = text
    return clean


def log_capture_stage(logger: logging.Logger, **fields: Any) -> dict[str, Any]:
    """写一行阶段日志，返回实际落盘的字段（供测试断言白名单生效）。

    没有 ``stage`` 字段时不写：阶段日志的每一行都必须能被聚合到某个阶段。
    """
    clean = sanitize_stage_fields(fields)
    if "stage" not in clean:
        return {}
    payload = " ".join(f"{key}={clean[key]}" for key in STAGE_FIELD_ALLOWLIST if key in clean)
    logger.info(payload)
    return clean


def configure_capture_stage_log(data_dir: Path) -> logging.Logger:
    """独立的 ``logs/capture-stages.log``：4MiB × 4 轮转，只收 INFO 以上。

    与主日志分开的原因：阶段日志是高频、结构化、可整份交给架构师分析的文件，
    不能和用户可见的运行日志混在一起被主日志轮转冲掉。
    """
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(CAPTURE_STAGE_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    # 阶段日志是独立文件，禁止向上冒泡到主日志，否则两处各写一遍。
    logger.propagate = False
    if logger.handlers:
        return logger
    handler = RotatingFileHandler(
        log_dir / "capture-stages.log",
        maxBytes=CAPTURE_STAGE_LOG_BYTES,
        backupCount=CAPTURE_STAGE_LOG_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    return logger


# ---------------------------------------------------------------------------
# 分段耗时：把观察到的数字摊成报告行（数值本身由 metrics.TimingRecorder 产出）
# ---------------------------------------------------------------------------


def _stats_of(bucket: Any) -> dict[str, Any]:
    if isinstance(bucket, Mapping):
        return {
            "count": int(bucket.get("count", 0) or 0),
            "p50_ms": float(bucket.get("p50_ms", 0.0) or 0.0),
            "p95_ms": float(bucket.get("p95_ms", 0.0) or 0.0),
            "max_ms": float(bucket.get("max_ms", 0.0) or 0.0),
        }
    return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}


def stage_timing_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把单个区域的分段快照摊成报告行：每段一行，带 cold/steady 与**起止点**。

    报告必须写清起止点，否则 P95 是个无法解释的数字：同一段在"含等锁"和
    "纯推理"两种口径下能差一个数量级。
    """
    rows: list[dict[str, Any]] = []
    for stage, entry in (payload or {}).items():
        if not isinstance(entry, Mapping):
            continue
        cold = _stats_of(entry.get("cold"))
        steady = _stats_of(entry.get("steady"))
        window = entry.get("window") or ["", ""]
        rows.append(
            {
                "stage": str(stage),
                "samples": cold["count"] + steady["count"],
                "cold": cold,
                "steady": steady,
                "start": str(window[0]) if len(window) > 0 else "",
                "end": str(window[1]) if len(window) > 1 else "",
            }
        )
    return rows


def format_stage_timings(
    payload: Mapping[str, Any],
    stage_notes: Mapping[str, str] | None = None,
    *,
    include_pending: bool = True,
) -> list[str]:
    """报告里的可读行。没有样本的段写出原因，**不写 0ms**（0ms 会被读成"很快"）。

    ``include_pending=False`` 时只列有样本的段，便于终端紧凑输出。
    """
    notes = stage_notes or {}
    lines: list[str] = []
    for row in stage_timing_rows(payload):
        if not row["samples"]:
            if not include_pending:
                continue
            reason = notes.get(row["stage"], "暂无样本")
            lines.append(f"{row['stage']:<16} 无样本（{reason}）")
            continue
        steady, cold = row["steady"], row["cold"]
        cold_text = (
            f"cold n={cold['count']} p50={cold['p50_ms']:.1f}ms" if cold["count"] else "cold n=0"
        )
        lines.append(
            f"{row['stage']:<16} steady n={steady['count']} "
            f"p50={steady['p50_ms']:.1f} p95={steady['p95_ms']:.1f} "
            f"max={steady['max_ms']:.1f}ms | {cold_text}"
        )
    return lines


# ---------------------------------------------------------------------------
# OCR 复用统计口径（单位：查询次数）
# ---------------------------------------------------------------------------


def reuse_rate_line(payload: Mapping[str, Any] | None) -> str:
    """把 ``ocr_reuse`` 汇总写成一行，**明确单位**，无样本写"未观测"。

    两个比率都除以 ``lookups``（查询次数），不是除以命中次数：

    * ``内容命中率 = hits / lookups``：画面有多静止；
    * ``实际复用率 = reused / lookups``：真正省掉了多少推理。

    没有查询样本时不能写 0%：0% 会被读成"画面一直在变"，而真相是"还没开始看"。
    """
    data = dict(payload or {})
    if not data.get("enabled", True):
        return "OCR 复用：已关闭（ocr_reuse_max_age_seconds=0，行为与 0.2.1 一致）"
    lookups = int(data.get("lookups", 0) or 0)
    if lookups <= 0:
        return (
            "OCR 复用：未观测（本次运行没有查询样本；不显示 0%，"
            "0% 会被误读成画面一直在变）"
        )
    hits = int(data.get("hits", data.get("cache_hits", 0)) or 0)
    reused = int(data.get("reused", 0) or 0)
    # 旧报告只有 hit_rate（= 内容命中率），没有 content_hit_rate；两种都要能读。
    content_rate = float(data.get("content_hit_rate", data.get("hit_rate", 0.0)) or 0.0)
    reuse_rate = float(data.get("reuse_rate", 0.0) or 0.0)
    max_age = float(data.get("max_age_seconds", 0.0) or 0.0)
    return (
        f"OCR 复用：查询 {lookups} 次，复用 {reused} 次，命中 {hits} 次，"
        f"内容命中率 {content_rate:.1%}，实际复用率 {reuse_rate:.1%}，"
        f"上限 {max_age:.0f} 秒"
        "（两个比率的分母都是查询次数；旧字段 hit_rate 等于内容命中率）"
    )


def source_fingerprint(paths: object, *, root: Path | None = None) -> dict[str, Any]:
    """给验收报告用的"源代码指纹"：文件路径 → sha256，并给出合并摘要。

    ``paths`` 是 ``Path`` 或 ``Path`` 序列；读不到的文件记 ``missing`` 而不抛异常，
    报告因此不会因为某个可选文件缺失而整份写不出来（那才是真的丢证据）。
    ``root`` 给定时键用相对路径，报告里不会出现用户名目录。
    """
    if isinstance(paths, str | Path):
        items = [Path(paths)]
    else:
        items = [Path(item) for item in paths]  # type: ignore[union-attr]
    files: dict[str, str] = {}
    digest = hashlib.sha256()
    for path in items:
        key = str(path)
        if root is not None:
            try:
                key = str(path.resolve().relative_to(Path(root).resolve()))
            except (OSError, ValueError):
                key = str(path)
        try:
            content = path.read_bytes()
        except OSError:
            files[key] = "missing"
            continue
        file_hash = hashlib.sha256(content).hexdigest()
        files[key] = file_hash
        digest.update(key.encode("utf-8"))
        digest.update(file_hash.encode("ascii"))
    return {"combined_sha256": digest.hexdigest(), "files": files}


# ---------------------------------------------------------------------------
# 命中气泡裁剪保留
# ---------------------------------------------------------------------------


class HitImageStore:
    def __init__(self, data_dir: Path, enabled: bool = True) -> None:
        self.directory = data_dir / "hits"
        self.enabled = enabled

    def save(self, event: MatchEvent, message: MessageCandidate, image: np.ndarray) -> None:
        if not self.enabled:
            return
        try:
            from PIL import Image
        except ImportError:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        box = message.box
        crop = image[
            max(0, box.y) : min(image.shape[0], box.y + box.height),
            max(0, box.x) : min(image.shape[1], box.x + box.width),
        ]
        if crop.size:
            # MSS / OCR 使用 BGR，Pillow 保存时须转换回 RGB。
            Image.fromarray(crop[:, :, ::-1]).save(self.directory / f"{event.id}.png")
        self.cleanup()

    def cleanup(self, max_age_seconds: int = 3 * 86400, max_bytes: int = 200 * 1024 * 1024) -> None:
        if not self.directory.exists():
            return
        now = time.time()
        files = sorted(
            (item for item in self.directory.glob("*.png") if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        total = 0
        for item in files:
            stat = item.stat()
            total += stat.st_size
            if now - stat.st_mtime > max_age_seconds or total > max_bytes:
                item.unlink(missing_ok=True)
