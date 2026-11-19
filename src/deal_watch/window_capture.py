"""父进程窗口采集客户端：一个逻辑请求一个截止时间。

0.2.2 的关键变化是**预算可见且不可重置**：

* 从 :meth:`WindowCaptureClient.capture` 入口建立唯一 deadline（生产锁定 5.0 秒），
  启动、等退出、读结果、解码、清理全部共用它；任何分支都不得重新计时；
* 组件启动交给有界监督任务：监督线程与调用方共用绝对 deadline，锁保护的
  ``claim`` / ``abandon`` 状态确保迟到结果不能重新交接；
* 启动中、运行中与待回收进程共享固定额度，未确认退出的进程由后台 reaper
  有界重试并持续 poll，因此连续超时不会堆积进程；
* kill 之后最多再等 0.5 秒（且不超过剩余预算），仍未退出就记 ``orphan_suspected``
  并立刻返回；
* 分段记录 ``popen_ms / wait_ms / kill_ms / decode_ms / total_ms``；
* 子进程的 stderr 是独立的阶段通道，被强杀时 stdout 的完整 payload 不存在，
  但已经 flush 进管道的阶段行还在，因此父进程仍能说出“卡在哪个阶段”；
* 每个请求都有 ``request_id``，父进程与子进程的阶段日志靠它对齐。

ROI 是**一次编码**协议：子进程在 PNG 编码前裁剪，只回传 ROI 像素；父进程不再
二次裁剪，也不接受“少了字段就偷偷按整帧走”的旧组件降级。字段缺失、类型错误、
ROI 回显不一致、尺寸矛盾一律按协议失败上报。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import numpy as np

from .capture import CaptureError, WindowsWindows
from .diagnostics import CAPTURE_STAGE_LOGGER_NAME, log_capture_stage, sanitize_stage_fields
from .models import CaptureErrorCode, FrameSample, Rect, RegionConfig

# 模块级别名：唯一的进程启动入口，也是测试的补丁点。
# 改成 ``Popen`` 之后 ``subprocess.run`` 不再是补丁点，因此测试改 patch
# ``deal_watch.window_capture.spawn``（断言内容与错误码完全不变）。
spawn = subprocess.Popen

# kill 之后的宽限期：0.2.1 的 onedir 组件 kill 即刻退出（实测 <50ms），
# 0.5 秒足够覆盖管道 EOF 抖动；超时即视为“可能残留”。
CAPTURE_KILL_GRACE_SECONDS = 0.5
# 进程退出后读管道线程的收尾上限（正常 EOF 只要几毫秒）。
CAPTURE_STREAM_JOIN_SECONDS = 0.3
# 单次请求每路最多保留多少行输出：异常组件刷屏时不能无限吃内存。
CAPTURE_STREAM_LINE_LIMIT = 64
# 子进程阶段行上限（与 capture_worker 的 MAX_STAGE_LINES 对齐，有界通道）。
CHILD_STAGE_LIMIT = 16
# 同时存活的读管道线程上限：被孙进程占着写端时，读线程会一直阻塞在 read 上。
# 这里给一个固定上限，宁可丢一次诊断输出，也不让线程数随故障轮次增长。
MAX_LIVE_READERS = 8
# 同时在收尾的管道句柄上限（单个后台清洁线程按队列处理）。
MAX_STREAM_RETIREMENTS = 8
# 同时未决的组件启动任务上限。CreateProcess 一旦卡在杀软/过滤驱动就不可中断，
# 只允许固定数量的监督线程存在；槽位满时直接失败，不再新建线程。
MAX_PENDING_STARTS = 2
# 进程的全部生命周期（启动、运行、待回收）共享此上限，防止 orphan 堆积。
MAX_CAPTURE_PROCESSES = 2
CAPTURE_REAPER_POLL_SECONDS = 0.25
CAPTURE_REAPER_MAX_KILLS = 4
# 阶段名只接受「小写字母开头、最长 32 位标识符」这一种形状。
# 阶段通道是我们自己的封闭协议，这条形状约束是防止有人把用户内容当阶段名传进来
# 的第二道闸（第一道在 diagnostics 的字段白名单里）：中文、空格、标点、纯数字、
# 长得像正文的字符串全部直接丢弃，不落盘。
_STAGE_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,31}")
# ``--session-exit-timeout-ms``：默认 500，硬上限 1000（不得放宽）。
DEFAULT_SESSION_EXIT_TIMEOUT_MS = 500
MAX_SESSION_EXIT_TIMEOUT_MS = 1000

# 子进程错误标记 → 父进程错误码。顺序敏感：先判最具体的。
# 这些标记全部来自 capture_worker 的 ``RuntimeError(<TOKEN>)``，只描述失败阶段，
# 不含任何用户内容。
_CHILD_ERROR_MAP: tuple[tuple[str, CaptureErrorCode], ...] = (
    ("BLACK_FRAME", CaptureErrorCode.BLACK_FRAME),
    ("WGC_SESSION_EXIT_TIMEOUT", CaptureErrorCode.CAPTURE_SESSION_HUNG),
    ("WGC_SESSION_CLOSED", CaptureErrorCode.CAPTURE_FAILED),
    ("WGC_FRAME_TIMEOUT", CaptureErrorCode.CAPTURE_TIMEOUT),
    ("WGC_START_TIMEOUT", CaptureErrorCode.CAPTURE_TIMEOUT),
    ("WGC_CALLBACK_FAILED", CaptureErrorCode.CAPTURE_SESSION_HUNG),
    ("WGC_SESSION_EXIT_FAILED", CaptureErrorCode.CAPTURE_SESSION_HUNG),
    ("FRAME_SIZE_CHANGED", CaptureErrorCode.WINDOW_RESIZED),
    ("COMPONENT_START_FAILED", CaptureErrorCode.CAPTURE_FAILED),
    ("PNG_ENCODE_FAILED", CaptureErrorCode.CAPTURE_FAILED),
    ("INVALID_HWND", CaptureErrorCode.CAPTURE_FAILED),
    ("PRINTWINDOW_BLANK_FRAME", CaptureErrorCode.CAPTURE_FAILED),
    ("PRINTWINDOW_FAILED", CaptureErrorCode.CAPTURE_FAILED),
    # 子进程裁剪 ROI 时的越界/非法：必须与父进程本地裁剪映射成同一个错误码，
    # 否则同一种配置错误会因为"这次谁裁的"而显示成两种提示。
    ("INVALID_REGION", CaptureErrorCode.INVALID_REGION),
)


def new_request_id() -> str:
    """8 位十六进制请求编号：父进程与子进程阶段日志的对齐键。"""
    return uuid4().hex[:8]


def clamp_session_exit_timeout(value: int | float) -> int:
    """把会话退出上限夹到 ``[0, 1000]`` 毫秒；非法值退回默认 500。"""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_SESSION_EXIT_TIMEOUT_MS
    if number <= 0:
        return DEFAULT_SESSION_EXIT_TIMEOUT_MS
    return min(number, MAX_SESSION_EXIT_TIMEOUT_MS)


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000.0, 1)


def _cleanup_reserve(timeout: float) -> float:
    """从总预算里给收尾（kill + 等退出 + 关管道）预留的时间。

    生产 5.0 秒预算预留 0.5 秒（正好等于强杀宽限）；短预算测试按 10% 预留，
    保证“总墙钟 ≤ timeout”这条不变式在小超时下也成立。
    """
    return min(CAPTURE_KILL_GRACE_SECONDS, max(0.0, timeout * 0.1))


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


@dataclass(slots=True)
class CaptureTrace:
    """一次采集请求的分段耗时、失败阶段与孤儿判定。

    字段全部是标识符或数字，可以直接进日志：不含图像、OCR 文本、聊天正文、
    窗口标题原文或凭据。
    """

    request_id: str = ""
    region_id: str = ""
    popen_ms: float = 0.0
    wait_ms: float = 0.0
    kill_ms: float = 0.0
    decode_ms: float = 0.0
    total_ms: float = 0.0
    stage_failed: str = ""
    stages: dict[str, float] = field(default_factory=dict)
    orphan_suspected: bool = False
    exit_code: int | None = None
    generation: int = 0
    worker_id: int = 0
    queue_wait_ms: float = 0.0
    deadline_overrun_ms: float = 0.0
    # 子进程原始错误标记（如 WGC_SESSION_EXIT_TIMEOUT），用于区分失败种类。
    child_error: str = ""

    def detail_suffix(self) -> str:
        """给 ``HealthSnapshot.detail`` 追加的一段诊断（不含任何用户内容）。"""
        parts: list[str] = []
        if self.request_id:
            parts.append(f"请求 {self.request_id}")
        if self.stage_failed:
            parts.append(f"卡在 {self.stage_failed}")
        if self.child_error:
            parts.append(f"子进程 {self.child_error}")
        parts.append(
            f"耗时 总{self.total_ms:.0f}ms/启动{self.popen_ms:.0f}/等待{self.wait_ms:.0f}/强杀{self.kill_ms:.0f}/解码{self.decode_ms:.0f}"
        )
        if self.orphan_suspected:
            parts.append("采集组件正在后台回收；后续采样自动重试")
        return "｜".join(parts)

    def as_fields(self) -> dict[str, object]:
        """扁平化成一个字典，便于写进报告或断言（只含标识符与数字）。"""
        return {
            "request_id": self.request_id,
            "region_id": self.region_id,
            "popen_ms": self.popen_ms,
            "wait_ms": self.wait_ms,
            "kill_ms": self.kill_ms,
            "decode_ms": self.decode_ms,
            "total_ms": self.total_ms,
            "stage_failed": self.stage_failed,
            "orphan_suspected": self.orphan_suspected,
            "exit_code": self.exit_code,
            "generation": self.generation,
            "worker_id": self.worker_id,
            "queue_wait_ms": self.queue_wait_ms,
            "deadline_overrun_ms": self.deadline_overrun_ms,
            "child_error": self.child_error,
            "stages": dict(self.stages),
        }


@dataclass(slots=True, frozen=True)
class CapturedWindowFrame:
    image: np.ndarray
    width: int
    height: int
    captured_at: float
    backend: str
    # 追加字段全部带默认值：0.2.1 的位置参数构造继续有效。
    frame_width: int = 0
    frame_height: int = 0
    roi: Rect | None = None
    trace: CaptureTrace | None = None


class WindowCaptureError(RuntimeError):
    def __init__(
        self,
        code: CaptureErrorCode,
        message: str,
        trace: CaptureTrace | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.trace = trace


class _ProtocolError(RuntimeError):
    """子进程回传的字段不满足采集协议（缺失/类型错误/尺寸矛盾/回显不符）。

    ``version_mismatch=True`` 表示这是“组件太旧、根本不认识 ROI 协议”的同一种表现：
    对用户要说清楚是版本问题，不能伪装成一次普通采集失败，更不能自动重采。
    """

    def __init__(self, message: str, *, version_mismatch: bool = False) -> None:
        super().__init__(message)
        self.version_mismatch = version_mismatch


def _argv_rejects_roi(stderr_lines: list[str]) -> bool:
    """子进程是否因为不认识 ``--roi`` 而直接退出（老采集组件的 argparse 报错）。"""
    text = "".join(stderr_lines).lower()
    if "--roi" not in text:
        return False
    return "unrecognized argument" in text or "invalid choice" in text or "error:" in text


# 读线程槽位：daemon 线程 + 固定上限，异常组件刷屏或孙进程握住写端都不会无限增长。
_READER_SLOTS = threading.BoundedSemaphore(MAX_LIVE_READERS)


def _drain_stream(stream, sink: list[str]) -> None:
    """把管道逐行读进有界列表；关闭/解码异常一律吞掉，读线程不能抛。"""
    try:
        for line in stream:
            if len(sink) < CAPTURE_STREAM_LINE_LIMIT:
                sink.append(line)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (AttributeError, OSError, ValueError):
            pass


def _reader_main(stream, sink: list[str]) -> None:
    """读线程主体：无论怎么退出都要归还槽位。"""
    try:
        _drain_stream(stream, sink)
    finally:
        _READER_SLOTS.release()


class _StreamJanitor:
    """单线程、有界队列的管道清洁工。

    ``BufferedReader.close()`` 可能与另一个线程里阻塞的 ``read`` 抢同一把锁，
    在“孙进程握着写端”时会把调用方一起拖住 3 秒。这里把关闭动作移出采集线程：
    调用方立刻返回，清洁线程按队列逐个关闭，队列满了就丢弃（读线程仍是 daemon，
    最坏情况是留下几个有上限的空转线程，不会拖慢采集）。
    """

    def __init__(self, limit: int = MAX_STREAM_RETIREMENTS) -> None:
        import queue

        self._queue: queue.Queue = queue.Queue(maxsize=max(1, limit))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def retire(self, process) -> None:
        if process is None:
            return
        self._ensure_thread()
        try:
            self._queue.put_nowait(process)
        except Exception:  # noqa: BLE001 - 队列满就放弃；读线程本身是 daemon
            return

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="capture-stream-janitor", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        while True:
            process = self._queue.get()
            try:
                _close_streams(process)
            finally:
                self._queue.task_done()


STREAM_JANITOR = _StreamJanitor()


def _start_readers(process, stdout_lines: list[str], stderr_lines: list[str]) -> list[threading.Thread]:
    """起两条 daemon 读线程。

    必须是 daemon 且不能 join 无界：子进程被强杀后若还有孙进程握着写端，
    ``communicate()`` 会一直等 EOF（0.2.1 的 W2）。读线程总数有固定上限。
    """
    streams = (
        (getattr(process, "stdout", None), stdout_lines, "capture-stdout"),
        (getattr(process, "stderr", None), stderr_lines, "capture-stderr"),
    )
    if any(stream is None for stream, _, _ in streams):
        return []
    acquired = 0
    for _ in streams:
        if not _READER_SLOTS.acquire(blocking=False):
            for _ in range(acquired):
                _READER_SLOTS.release()
            return []
        acquired += 1
    threads: list[threading.Thread] = []
    try:
        for stream, sink, name in streams:
            thread = threading.Thread(target=_reader_main, args=(stream, sink), name=name, daemon=True)
            thread.start()
            threads.append(thread)
    except RuntimeError:
        for _ in range(acquired - len(threads)):
            _READER_SLOTS.release()
        raise
    return threads


def _join_readers(threads: list[threading.Thread], deadline: float | None = None) -> None:
    """在同一个总上限内收尾全部读线程（不去等卡住的管道）。"""
    limit = time.monotonic() + CAPTURE_STREAM_JOIN_SECONDS
    if deadline is not None:
        limit = min(limit, deadline)
    for thread in threads:
        thread.join(max(0.0, limit - time.monotonic()))


def _close_streams(process) -> None:
    """关闭父进程侧的管道句柄。

    kill 之后如果孙进程还握着写端，读线程会一直阻塞在 ``for line in stream``；
    关闭本端后读线程立刻收到 ``ValueError`` 退出，不会一轮一轮堆积。
    """
    for name in ("stdout", "stderr", "stdin"):
        stream = getattr(process, name, None)
        if stream is None:
            continue
        try:
            stream.close()
        except (AttributeError, OSError, ValueError):
            pass


class _StartJob:
    """启动请求的唯一所有权状态机；调用方 deadline 决定接手或放弃。"""

    __slots__ = ("event", "released", "process", "error", "state", "_lock", "token", "deadline")

    def __init__(self, token, deadline: float) -> None:
        self.event = threading.Event()
        self.released = threading.Event()
        self.process = None
        self.error: BaseException | None = None
        self.state = "starting"
        self._lock = threading.Lock()
        self.token = token
        self.deadline = deadline

    def ready(self, process, error) -> None:
        with self._lock:
            self.process = process
            self.error = error
            if self.state == "starting":
                self.state = "ready"
            self.event.set()

    def claim(self):
        """仅 ready 状态可原子交接；其他状态返回 None。"""
        with self._lock:
            if self.state != "ready":
                return None
            if time.monotonic() >= self.deadline:
                self.state = "abandoned"
                self.released.set()
                return None
            self.state = "claimed"
            process = self.process
            self.released.set()
            return process

    def abandon(self) -> None:
        """deadline到点放弃；claimed 后不能撤销或二次交接。"""
        with self._lock:
            if self.state in ("starting", "ready"):
                self.state = "abandoned"
            self.released.set()


def _log_process_registry_event(stage: str, *, process, active: int, orphans: int) -> None:
    """Log only lifecycle state and PID; never command lines or window data."""
    try:
        pid = getattr(process, "pid", None) if process is not None else None
    except Exception:
        pid = None
    log_capture_stage(
        logging.getLogger(CAPTURE_STAGE_LOGGER_NAME),
        stage=stage,
        outcome="orphan" if "orphan" in stage else "ok",
        pid=pid,
        active_processes=active,
        orphan_processes=orphans,
    )


class _ProcessRegistry:
    """有界进程生命周期额度及后台孤儿回收器。"""

    def __init__(self, limit: int = MAX_CAPTURE_PROCESSES) -> None:
        self._limit = max(1, limit)
        self._lock = threading.Lock()
        self._tokens: set[object] = set()
        self._orphans: dict[object, tuple[object, int]] = {}
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> int:
        with self._lock:
            return len(self._tokens)

    @property
    def orphans(self) -> int:
        with self._lock:
            return len(self._orphans)

    @property
    def needs_attention(self) -> bool:
        with self._lock:
            return any(attempts >= CAPTURE_REAPER_MAX_KILLS for _, attempts in self._orphans.values())

    def reserve(self):
        with self._lock:
            if len(self._tokens) >= self._limit:
                return None
            token = object()
            self._tokens.add(token)
            return token

    def release(self, token) -> None:
        if token is None:
            return
        with self._lock:
            existed = token in self._tokens
            orphan = self._orphans.get(token)
            self._tokens.discard(token)
            self._orphans.pop(token, None)
            active = len(self._tokens)
            orphans = len(self._orphans)
        if existed:
            _log_process_registry_event(
                "process_capacity_released",
                process=orphan[0] if orphan else None,
                active=active,
                orphans=orphans,
            )

    def owns(self, token) -> bool:
        with self._lock:
            return token in self._tokens

    def is_orphan(self, token) -> bool:
        with self._lock:
            return token in self._orphans

    def defer_reap(self, process, token) -> None:
        """Nonblocking fallback for a claimed process whose caller failed unexpectedly."""
        if process is None:
            self.release(token)
            return
        with self._lock:
            if token not in self._tokens or token in self._orphans:
                return
            self._orphans[token] = (process, 0)
            self._ensure_reaper_locked()
            active = len(self._tokens)
            orphans = len(self._orphans)
        _log_process_registry_event(
            "process_orphan_registered", process=process, active=active, orphans=orphans
        )

    def _ensure_reaper_locked(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._run, name="capture-process-reaper", daemon=True
            )
            self._thread.start()

    def track_orphan(self, process, token) -> bool:
        if process is None:
            self.release(token)
            return True
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=CAPTURE_KILL_GRACE_SECONDS)
            self.release(token)
            return True
        except subprocess.TimeoutExpired:
            pass
        except Exception:  # wait/poll APIs can fail independently; never lose the token.
            pass
        self._register_orphan(process, token, attempts=1)
        return False

    def _register_orphan(self, process, token, *, attempts: int) -> None:
        registered = False
        with self._lock:
            if token in self._tokens and token not in self._orphans:
                self._orphans[token] = (process, attempts)
                registered = True
                self._ensure_reaper_locked()
            active = len(self._tokens)
            orphans = len(self._orphans)
        if registered:
            _log_process_registry_event(
                "process_orphan_registered", process=process, active=active, orphans=orphans
            )

    def adopt_orphan(self, process, token) -> bool:
        """接纳调用方已执行一次 kill/wait 的进程，后续只后台poll/重试。"""
        try:
            exited = process.poll() is not None
        except Exception:
            exited = False
        if exited:
            self.release(token)
            return True
        self._register_orphan(process, token, attempts=0)
        return False

    def _run(self) -> None:
        while True:
            time.sleep(CAPTURE_REAPER_POLL_SECONDS)
            with self._lock:
                entries = list(self._orphans.items())
            if not entries:
                with self._lock:
                    if not self._orphans:
                        self._thread = None
                        return
                continue
            for token, (process, attempts) in entries:
                try:
                    exited = process.poll() is not None
                except Exception:
                    exited = False
                if exited:
                    self.release(token)
                    continue
                if attempts < CAPTURE_REAPER_MAX_KILLS:
                    try:
                        process.kill()
                    except Exception:
                        pass
                    with self._lock:
                        if token in self._orphans:
                            self._orphans[token] = (process, attempts + 1)


PROCESS_REGISTRY = _ProcessRegistry()


class _StartSupervisor:
    """有界组件启动监督：固定槽位 + 一个请求一个（短命）监督线程。

    ``spawn``（Windows 上是 ``CreateProcess``）无法被超时打断，只能在别的线程里
    执行。这里把“调用方等进程句柄”和“进程创建本身”分开：

    * 调用方最多等到自己的 deadline；到点即返回，不阻塞采集线程；
    * 监督线程等调用方的原子 ``claim`` 或 ``abandon``；到统一 deadline 无响应时
      放弃并将进程交给生命周期注册表回收；
    * 同时未决的启动任务不超过 ``limit``，槽位满时新请求直接失败，不新建线程。
    """

    def __init__(self, limit: int = MAX_PENDING_STARTS) -> None:
        self._limit = max(1, int(limit))
        self._slots = threading.BoundedSemaphore(self._limit)
        self._lock = threading.Lock()
        self._pending = 0
        self._workers = 0

    @property
    def pending(self) -> int:
        """尚未收尾的启动任务数（含已放弃、仍在回收的）。"""
        with self._lock:
            return self._pending

    @property
    def workers(self) -> int:
        """存活的监督线程数。"""
        with self._lock:
            return self._workers

    def start(self, command: list[str], kwargs: dict, deadline: float) -> _StartJob | None:
        """占用一个槽位并启动监督线程；槽位满返回 ``None``。"""
        if not self._slots.acquire(blocking=False):
            return None
        token = PROCESS_REGISTRY.reserve()
        if token is None:
            self._slots.release()
            return None
        job = _StartJob(token, deadline)
        with self._lock:
            self._pending += 1
            self._workers += 1
        thread = threading.Thread(
            target=self._run,
            args=(job, command, kwargs),
            name="capture-start",
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError:
            PROCESS_REGISTRY.release(token)
            with self._lock:
                self._pending -= 1
                self._workers -= 1
            self._slots.release()
            return None
        return job

    def wait_idle(self, timeout: float = 2.0) -> bool:
        """等所有启动任务收尾（测试与退出路径用）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.pending == 0:
                return True
            time.sleep(0.005)
        return self.pending == 0

    def _run(self, job: _StartJob, command: list[str], kwargs: dict) -> None:
        process = None
        try:
            process = spawn(command, **kwargs)
        except BaseException as error:  # noqa: BLE001 - 启动失败要原样交给调用方
            job.error = error
        finally:
            job.ready(process, job.error)
            with self._lock:
                self._workers -= 1
        try:
            job.released.wait(max(0.0, job.deadline - time.monotonic()))
            with job._lock:
                state = job.state
                if state == "ready":
                    job.state = "abandoned"
                    state = "abandoned"
            if state == "abandoned":
                self._reap(process, job.token)
            elif process is None:
                PROCESS_REGISTRY.release(job.token)
        finally:
            with self._lock:
                self._pending -= 1
            self._slots.release()

    @staticmethod
    def _reap(process, token=None) -> None:
        """kill一次、有限等待，未退出则交给有界后台reaper继续持有额度。"""
        if process is None:
            PROCESS_REGISTRY.release(token)
            return
        PROCESS_REGISTRY.track_orphan(process, token)
        STREAM_JANITOR.retire(process)


# 进程级监督器：所有客户端共用同一组槽位，避免多区域并发时线程数失控。
START_SUPERVISOR = _StartSupervisor()


def parse_stage_lines(lines: list[str]) -> dict[str, float]:
    """解析子进程 stderr 的阶段行：``{"stage": ..., "t_ms": ..., "request_id": ...}``。

    只接受单行 JSON 且 ``stage`` 非空；解析失败的行直接跳过——阶段通道是
    诊断用的旁路，不能因为一行垃圾让它影响采集结果。
    """
    stages: dict[str, float] = {}
    for line in lines:
        if len(stages) >= CHILD_STAGE_LIMIT:
            break
        text = (line or "").strip()
        if not text.startswith("{"):
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        name = str(data.get("stage", "") or "")
        if not _STAGE_NAME_PATTERN.fullmatch(name):
            continue
        try:
            stages[name] = round(float(data.get("t_ms", 0.0)), 1)
        except (TypeError, ValueError):
            stages[name] = 0.0
    return stages


def last_stage(stages: dict[str, float]) -> str:
    """子进程最后完成的阶段名；空字典返回空串。"""
    return next(reversed(stages), "") if stages else ""


def _last_json_object(text: str) -> dict:
    """取 stdout 最后一个 JSON 对象行；一个都没有就按“无效数据”处理。"""
    for line in reversed([item.strip() for item in text.splitlines()]):
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise WindowCaptureError(CaptureErrorCode.CAPTURE_FAILED, "窗口采集组件返回无效数据")


def map_child_error(message: str) -> CaptureErrorCode:
    """把子进程错误文本映射成父进程错误码（保持 0.2.1 的 BLACK_FRAME 语义）。"""
    for token, code in _CHILD_ERROR_MAP:
        if token in message:
            return code
    return CaptureErrorCode.CAPTURE_FAILED


def child_error_token(message: str) -> str:
    """命中的子进程错误标记；没命中返回空串。"""
    for token, _ in _CHILD_ERROR_MAP:
        if token in message:
            return token
    return ""


def _require_int(payload: dict, name: str) -> int:
    """严格取整数字段：缺失、布尔、字符串、浮点都算协议错误。"""
    if name not in payload:
        raise _ProtocolError(f"采集组件返回缺少 {name} 字段")
    value = payload[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise _ProtocolError(f"采集组件返回的 {name} 不是整数")
    return int(value)


def _roi_echo(payload: dict) -> tuple[int, int, int, int] | None:
    """读取并校验 ROI 回显；``None``/缺失表示整帧。"""
    value = payload.get("roi")
    if value is None:
        return None
    if not isinstance(value, list | tuple) or len(value) != 4:
        raise _ProtocolError("采集组件返回的 roi 回显格式错误")
    numbers: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise _ProtocolError("采集组件返回的 roi 回显不是整数")
        numbers.append(int(item))
    return numbers[0], numbers[1], numbers[2], numbers[3]


def validate_payload(payload: dict, roi: Rect | None) -> tuple[int, int, int, int]:
    """在解码前校验协议字段；返回 ``(返回宽, 返回高, 整帧宽, 整帧高)``。

    这里的判定是**严格**的：字段缺失、类型错误、ROI 回显与请求不一致、尺寸互相
    矛盾全部抛 :class:`_ProtocolError`，绝不退回“按整帧处理”的静默降级。
    """
    returned_width = _require_int(payload, "width")
    returned_height = _require_int(payload, "height")
    if returned_width <= 0 or returned_height <= 0:
        raise _ProtocolError("采集组件返回的图像尺寸非法")

    has_frame_size = "frame_width" in payload and "frame_height" in payload
    if roi is not None and not has_frame_size:
        # 老组件不认识 --roi：不裁、也不回显 frame 尺寸。必须明确提示版本不匹配，
        # 不能在同一请求里自动改成“整帧再本地裁”（那会让每轮多起一次子进程）。
        raise _ProtocolError(
            "采集组件版本不匹配：该组件不支持消息区域下推，请更新完整便携包"
            "（或显式关闭“只采集消息区域”后重新校准）",
            version_mismatch=True,
        )
    frame_width = _require_int(payload, "frame_width")
    frame_height = _require_int(payload, "frame_height")
    if frame_width <= 0 or frame_height <= 0:
        raise _ProtocolError("采集组件返回的整帧尺寸非法")

    echoed = _roi_echo(payload)
    if roi is None:
        if echoed is not None:
            raise _ProtocolError("未请求消息区域，组件却回传了 roi")
        if (returned_width, returned_height) != (frame_width, frame_height):
            raise _ProtocolError(
                "采集组件返回的整帧尺寸与图像尺寸矛盾"
                f"（图像 {returned_width}x{returned_height}，整帧 {frame_width}x{frame_height}）"
            )
        return returned_width, returned_height, frame_width, frame_height

    expected = (roi.x, roi.y, roi.width, roi.height)
    if echoed is None:
        if "roi" in payload:
            raise _ProtocolError("请求了消息区域，组件却没有回显 roi")
        raise _ProtocolError(
            "采集组件版本不匹配：该组件不支持消息区域下推，请更新完整便携包"
            "（或显式关闭“只采集消息区域”后重新校准）",
            version_mismatch=True,
        )
    if echoed != expected:
        raise _ProtocolError(
            f"采集组件回显的 roi 与请求不一致（请求 {expected}，回显 {echoed}）"
        )
    if (returned_width, returned_height) != (roi.width, roi.height):
        raise _ProtocolError(
            "采集组件返回的图像尺寸与请求的消息区域不一致"
            f"（请求 {roi.width}x{roi.height}，返回 {returned_width}x{returned_height}）"
        )
    if roi.x + roi.width > frame_width or roi.y + roi.height > frame_height:
        raise _ProtocolError("采集组件回传的整帧尺寸容不下请求的消息区域")
    return returned_width, returned_height, frame_width, frame_height


class WindowCaptureClient:
    """有界窗口采集：生产复用预热组件，兼容单次组件；请求全程共用5秒预算。"""

    def __init__(
        self,
        component_path: Path | None = None,
        timeout: float = 5.0,
        *,
        session_exit_timeout_ms: int = DEFAULT_SESSION_EXIT_TIMEOUT_MS,
        persistent: bool = False,
    ) -> None:
        self.persistent = persistent
        self._pool_lock = threading.RLock()
        self._capture_lock = threading.Lock()
        self._pool = None
        self.component_path = component_path
        self.timeout = timeout
        # 子进程侧 WGC 会话退出的上限，不属于生产参数白名单（测试锁定 5.0/3.0/60s）。
        self.session_exit_timeout_ms = clamp_session_exit_timeout(session_exit_timeout_ms)
        self.last_trace = CaptureTrace()

    def capture(
        self,
        hwnd: int,
        backend: str = "wgc",
        *,
        roi: Rect | None = None,
        request_id: str = "",
        region_id: str = "",
        generation: int = 0,
        reference_frame_width: int = 0,
        reference_frame_height: int = 0,
        validate_target=None,
    ) -> CapturedWindowFrame:
        """采集一帧；``roi`` 非空时由子进程裁好、只回传该区域。

        两条路径，对调用方都是"返回请求的像素"：

        1. ``roi is None`` → 整帧（圈选/校准预览走这条，永远是完整帧）；
        2. ``roi`` 非空 → 子进程在编码前裁剪，只传 ROI。父进程不二次裁剪。

        整个调用只有**一个** deadline：启动、等待、读结果、解码、清理都在这一个
        预算里。这里不做任何自动重采——组件不支持 ROI 就是版本不匹配。
        """
        started = time.monotonic()
        if self.persistent:
            # Preview and monitoring share this client. Keep the result's lease
            # exclusive through validation/decoding and possible quarantine.
            if not self._capture_lock.acquire(timeout=max(0.0, self.timeout - _cleanup_reserve(self.timeout))):
                trace = CaptureTrace(request_id=request_id or new_request_id(), region_id=region_id,
                                     generation=generation, stage_failed="queue_wait",
                                     total_ms=_elapsed_ms(started), queue_wait_ms=_elapsed_ms(started))
                self._record(trace, "request_total", "failed", trace.total_ms, CaptureErrorCode.CAPTURE_TIMEOUT)
                raise WindowCaptureError(CaptureErrorCode.CAPTURE_TIMEOUT, "等待前一采集结果处理超过预算", trace)
            try:
                return self._capture_persistent(
                    hwnd, backend, roi, request_id, region_id, generation,
                    reference_frame_width, reference_frame_height, started, validate_target,
                )
            finally:
                self._capture_lock.release()
        return self._capture_once(
            hwnd,
            backend,
            roi=roi,
            request_id=request_id,
            region_id=region_id,
            total_started=started,
            deadline=started + self.timeout,
        )

    def prepare(self) -> None:
        with self._pool_lock:
            if self.persistent and (self._pool is None or self._pool.closed):
                from .capture_pool import CapturePool
                self._pool = CapturePool(self._command(), startup_timeout=self.timeout)
                self._pool.prepare()

    def close(self) -> None:
        with self._pool_lock:
            if self._pool is not None:
                self._pool.close()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        if not self.persistent:
            return True
        with self._pool_lock:
            if self._pool is None:
                self.prepare()
            pool = self._pool
        from .capture_pool import PoolFailure
        started = time.monotonic()
        try:
            return pool.wait_ready(timeout)
        except PoolFailure as error:
            code = CaptureErrorCode.CAPTURE_TIMEOUT if error.timeout else CaptureErrorCode.CAPTURE_FAILED
            trace = CaptureTrace(stage_failed=error.stage, total_ms=_elapsed_ms(started))
            self.last_trace = trace
            raise WindowCaptureError(code, str(error), trace) from error

    def wait_closed(self, timeout: float = 1.0) -> bool:
        with self._pool_lock:
            pool = self._pool
        return pool.wait_closed(timeout) if pool is not None else True

    def worker_stats(self) -> dict:
        with self._pool_lock:
            pool = self._pool
        return pool.stats() if pool is not None else {"state": "waiting"}

    def _capture_persistent(self, hwnd, backend, roi, request_id, region_id, generation,
                            reference_frame_width, reference_frame_height, started, validate_target=None):
        from .capture_pool import PROTOCOL_VERSION, PoolFailure
        trace = CaptureTrace(request_id=request_id or new_request_id(), region_id=region_id,
                             generation=generation, queue_wait_ms=_elapsed_ms(started))
        self.last_trace = trace
        deadline = started + self.timeout
        started_wall = time.time()
        request = {
            "type": "capture", "protocol_version": PROTOCOL_VERSION,
            "request_id": trace.request_id, "region_id": region_id, "generation": generation,
            "hwnd": hwnd, "backend": backend,
            "roi": [roi.x, roi.y, roi.width, roi.height] if roi is not None else None,
            "reference_frame_width": reference_frame_width,
            "reference_frame_height": reference_frame_height,
            "wgc_timeout_ms": 3000, "session_exit_timeout_ms": self.session_exit_timeout_ms,
        }
        try:
            with self._pool_lock:
                if self._pool is None:
                    self.prepare()
                pool = self._pool
            payload = pool.request(request, deadline - _cleanup_reserve(self.timeout), trace,
                                   validate_target=validate_target)
            trace.wait_ms = _elapsed_ms(started)
            self._record(trace, "queue_wait", "ok", trace.queue_wait_ms)
            self._record(trace, "response_wait", "ok", max(0.0, trace.wait_ms - trace.queue_wait_ms))
            if payload.get("ok") is True:
                stamp = payload.get("captured_at")
                if (not isinstance(stamp, int | float) or isinstance(stamp, bool)
                        or not started_wall - 0.1 <= stamp <= time.time() + 0.1):
                    pool.invalidate(trace.worker_id)
                    raise PoolFailure("protocol", "采集帧时间不属于当前请求，已作废")
            if payload.get("worker_reusable") is not True:
                pool.invalidate(trace.worker_id)
            return self._decode_payload(payload, trace, roi, backend, started, deadline)
        except PoolFailure as error:
            trace.stage_failed = error.stage
            code = CaptureErrorCode.CAPTURE_TIMEOUT if error.timeout else CaptureErrorCode.CAPTURE_FAILED
            trace.total_ms = _elapsed_ms(started)
            trace.deadline_overrun_ms = max(0.0, trace.total_ms - self.timeout * 1000)
            trace.orphan_suspected = bool(PROCESS_REGISTRY.orphans)
            self._record_child_stages(trace)
            self._record(trace, "request_total", "failed", trace.total_ms, code)
            raise WindowCaptureError(code, str(error), trace) from error
        except WindowCaptureError:
            if self._pool is not None and trace.stage_failed in {"protocol", "component_version", "decode"}:
                pool.invalidate(trace.worker_id)
            raise

    @property
    def stage_logger(self) -> logging.Logger:
        """``logs/capture-stages.log`` 的 logger；未配置时 INFO 级别不落盘。"""
        return logging.getLogger(CAPTURE_STAGE_LOGGER_NAME)

    def _command(self) -> list[str]:
        """子进程命令行：冻结产物直接跑组件，源码运行走 ``-m``。"""
        if self.component_path is not None or getattr(sys, "frozen", False):
            component = self.component_path
            if component is None:
                internal = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
                component = internal.parent / "capture-component" / "DealWatchCapture.exe"
            if not component.is_file():
                raise WindowCaptureError(
                    CaptureErrorCode.CAPTURE_FAILED, "缺少窗口采集组件，请重新安装完整便携包"
                )
            return [str(component)]
        return [sys.executable, "-m", "deal_watch.capture_worker"]

    def _record(
        self,
        trace: CaptureTrace,
        stage: str,
        outcome: str = "ok",
        elapsed_ms: float = 0.0,
        error_code: object = "",
    ) -> None:
        """写一行父进程阶段日志；字段经过白名单过滤。"""
        if stage == "request_total":
            trace.deadline_overrun_ms = max(trace.deadline_overrun_ms, trace.total_ms - self.timeout * 1000)
        fields = {
            "stage": stage,
            "request_id": trace.request_id,
            "region_id": trace.region_id,
            "elapsed_ms": elapsed_ms,
            "outcome": outcome,
            "error_code": error_code,
            "worker_id": trace.worker_id,
            "generation": trace.generation,
            "deadline_overrun_ms": trace.deadline_overrun_ms,
        }
        log_capture_stage(self.stage_logger, **fields)
        if outcome != "ok":
            summary = sanitize_stage_fields(fields)
            logging.getLogger("deal_watch").warning(
                "capture_failed %s",
                " ".join(f"{key}={value}" for key, value in summary.items()),
            )

    def _record_child_stages(self, trace: CaptureTrace) -> None:
        """把子进程阶段行转写成父进程阶段日志（``child.`` 前缀避免与本地阶段重名）。"""
        previous = 0.0
        for name, t_ms in trace.stages.items():
            self._record(trace, f"child.{name}", "ok", t_ms)
            log_capture_stage(self.stage_logger, stage="stage_timing", request_id=trace.request_id,
                              region_id=trace.region_id, worker_id=trace.worker_id,
                              outcome=name, stage_offset_ms=t_ms,
                              stage_elapsed_ms=max(0.0, t_ms - previous))
            previous = t_ms

    def _capture_once(
        self,
        hwnd: int,
        backend: str = "wgc",
        *,
        roi: Rect | None = None,
        request_id: str = "",
        region_id: str = "",
        total_started: float | None = None,
        deadline: float | None = None,
    ) -> CapturedWindowFrame:
        """一次真实的子进程请求；失败一律抛 ``WindowCaptureError``（带 ``trace``）。

        ``deadline`` 由 :meth:`capture` 在入口建立并传进来；这里任何分支都只**消耗**
        剩余预算，绝不重新计时。``request_roi`` 由 ``roi`` 是否为空决定。
        """
        trace = CaptureTrace(request_id=request_id or new_request_id(), region_id=region_id)
        self.last_trace = trace
        started = time.monotonic() if total_started is None else total_started
        hard_deadline = (started + self.timeout) if deadline is None else deadline
        # 给收尾留出的时间也来自同一个预算，不再额外加 0.5 秒。
        wait_deadline = hard_deadline - _cleanup_reserve(self.timeout)
        request_roi = roi is not None

        try:
            command = self._command() + [
                "--hwnd",
                str(hwnd),
                "--backend",
                backend,
                "--request-id",
                trace.request_id,
                "--session-exit-timeout-ms",
                str(self.session_exit_timeout_ms),
            ]
            if roi is not None:
                command += ["--roi", f"{roi.x},{roi.y},{roi.width},{roi.height}"]
        except WindowCaptureError as error:
            trace.total_ms = _elapsed_ms(started)
            trace.stage_failed = "component_start"
            self._record(trace, "component_start", "failed", trace.total_ms, error.code)
            raise WindowCaptureError(error.code, str(error), trace) from error

        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        popen_started = time.monotonic()
        job = START_SUPERVISOR.start(
            command,
            {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "creationflags": creationflags,
            },
            wait_deadline,
        )
        if job is None:
            trace.popen_ms = _elapsed_ms(popen_started)
            trace.total_ms = _elapsed_ms(started)
            trace.stage_failed = "component_start"
            self._record(
                trace, "component_start", "failed", trace.popen_ms,
                CaptureErrorCode.CAPTURE_POPEN_TIMEOUT,
            )
            self._record(
                trace, "request_total", "failed", trace.total_ms,
                CaptureErrorCode.CAPTURE_POPEN_TIMEOUT,
            )
            raise WindowCaptureError(
                CaptureErrorCode.CAPTURE_POPEN_TIMEOUT,
                "采集组件启动任务已达上限，请稍后重试（上一条请求的组件仍未启动完成）",
                trace,
            )

        # 启动交接和后续采集共用此绝对 deadline。
        start_budget = _remaining(wait_deadline)
        job.event.wait(start_budget)
        trace.popen_ms = _elapsed_ms(popen_started)
        if not job.event.is_set() or time.monotonic() >= wait_deadline:
            job.abandon()
            trace.total_ms = _elapsed_ms(started)
            trace.stage_failed = "component_start"
            message = f"启动窗口采集组件就超过 {self.timeout:.1f} 秒预算"
            self._record(
                trace, "component_start", "failed", trace.popen_ms,
                CaptureErrorCode.CAPTURE_POPEN_TIMEOUT,
            )
            self._record(
                trace, "request_total", "failed", trace.total_ms,
                CaptureErrorCode.CAPTURE_POPEN_TIMEOUT,
            )
            raise WindowCaptureError(
                CaptureErrorCode.CAPTURE_POPEN_TIMEOUT, message, trace
            )

        if job.error is not None:
            job.claim()
            trace.total_ms = _elapsed_ms(started)
            trace.stage_failed = "component_start"
            self._record(
                trace, "component_start", "failed", trace.popen_ms,
                CaptureErrorCode.CAPTURE_FAILED,
            )
            self._record(
                trace, "request_total", "failed", trace.total_ms,
                CaptureErrorCode.CAPTURE_FAILED,
            )
            raise WindowCaptureError(
                CaptureErrorCode.CAPTURE_FAILED, "无法启动窗口采集组件", trace
            ) from job.error

        process = job.claim()
        if process is None:
            job.abandon()
            trace.total_ms = _elapsed_ms(started)
            trace.stage_failed = "component_start"
            if time.monotonic() >= wait_deadline:
                message = f"启动窗口采集组件就超过 {self.timeout:.1f} 秒预算"
                self._record(
                    trace,
                    "component_start",
                    "failed",
                    trace.popen_ms,
                    CaptureErrorCode.CAPTURE_POPEN_TIMEOUT,
                )
                self._record(
                    trace,
                    "request_total",
                    "failed",
                    trace.total_ms,
                    CaptureErrorCode.CAPTURE_POPEN_TIMEOUT,
                )
                raise WindowCaptureError(CaptureErrorCode.CAPTURE_POPEN_TIMEOUT, message, trace)
            raise WindowCaptureError(
                CaptureErrorCode.CAPTURE_FAILED,
                "采集组件交接已失效，启动结果作废",
                trace,
            )
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        readers: list[threading.Thread] = []
        timed_out = False
        failure_stage = "reader_start"
        wait_started = time.monotonic()
        try:
            self._record(trace, "component_start", "ok", trace.popen_ms)
            readers = _start_readers(process, stdout_lines, stderr_lines)
            if len(readers) != 2:
                self._abort(trace, process, stderr_lines, readers, hard_deadline, started, job.token)
                trace.stage_failed = "capture_resources"
                trace.total_ms = _elapsed_ms(started)
                message = "采集资源暂不可用：无法为标准输出和错误输出分配读线程，请稍后重试"
                self._record(
                    trace, "reader_start", "failed", trace.total_ms,
                    CaptureErrorCode.CAPTURE_FAILED,
                )
                self._record(
                    trace, "request_total", "failed", trace.total_ms,
                    CaptureErrorCode.CAPTURE_FAILED,
                )
                raise WindowCaptureError(CaptureErrorCode.CAPTURE_FAILED, message, trace)

            wait_started = time.monotonic()
            failure_stage = "wait_exit"
            remaining = _remaining(wait_deadline)
            if remaining <= 0.0:
                timed_out = True
            else:
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    timed_out = True
            trace.wait_ms = _elapsed_ms(wait_started)
            if not timed_out:
                # Once wait returns, process exit is known and the lifecycle slot is free.
                PROCESS_REGISTRY.release(job.token)
        except WindowCaptureError:
            raise
        except Exception as error:
            trace.stage_failed = failure_stage
            if failure_stage == "wait_exit":
                trace.wait_ms = _elapsed_ms(wait_started)
            trace.total_ms = _elapsed_ms(started)
            self._record(
                trace, failure_stage, "failed", _elapsed_ms(wait_started),
                CaptureErrorCode.CAPTURE_FAILED,
            )
            self._record(
                trace, "request_total", "failed", trace.total_ms,
                CaptureErrorCode.CAPTURE_FAILED,
            )
            message = (
                "无法启动采集输出读取线程"
                if failure_stage == "reader_start"
                else "采集组件等待退出失败"
            )
            raise WindowCaptureError(CaptureErrorCode.CAPTURE_FAILED, message, trace) from error
        finally:
            # Fallback lease: all unexpected exits before confirmed wait are handed to
            # the background reaper without a blocking wait or duplicate orphan entry.
            if not timed_out and PROCESS_REGISTRY.owns(job.token):
                if not PROCESS_REGISTRY.is_orphan(job.token):
                    PROCESS_REGISTRY.defer_reap(process, job.token)
                    try:
                        STREAM_JANITOR.retire(process)
                    except Exception:
                        pass
            if not timed_out:
                try:
                    _join_readers(readers, hard_deadline)
                except Exception:
                    pass

        if timed_out:
            self._abort(trace, process, stderr_lines, readers, hard_deadline, started, job.token)
            message = f"窗口采集组件超过 {self.timeout:.1f} 秒未返回"
            code = (
                CaptureErrorCode.CAPTURE_ORPHAN
                if trace.orphan_suspected
                else CaptureErrorCode.CAPTURE_TIMEOUT
            )
            if trace.orphan_suspected:
                message = f"{message}，强制结束后仍未退出（可能残留子进程）"
            self._record(trace, "request_total", "failed", trace.total_ms, code)
            raise WindowCaptureError(code, message, trace)

        try:
            trace.exit_code = process.returncode
        except Exception as error:
            trace.stage_failed = "wait_exit"
            trace.total_ms = _elapsed_ms(started)
            self._record(
                trace, "wait_exit", "failed", trace.wait_ms, CaptureErrorCode.CAPTURE_FAILED
            )
            self._record(
                trace, "request_total", "failed", trace.total_ms,
                CaptureErrorCode.CAPTURE_FAILED,
            )
            raise WindowCaptureError(
                CaptureErrorCode.CAPTURE_FAILED, "无法读取采集组件退出状态", trace
            ) from error
        trace.stages = parse_stage_lines(stderr_lines)
        self._record(trace, "wait_exit", "ok", trace.wait_ms)

        try:
            payload = _last_json_object("".join(stdout_lines))
        except WindowCaptureError as error:
            trace.total_ms = _elapsed_ms(started)
            trace.stage_failed = last_stage(trace.stages) or "result_parse"
            if trace.stage_failed in {"total", "worker_complete"}:
                trace.stage_failed = "result_parse"
            self._record_child_stages(trace)
            self._record(trace, "parse", "failed", trace.total_ms, error.code)
            self._record(trace, "request_total", "failed", trace.total_ms, error.code)
            if request_roi and _argv_rejects_roi(stderr_lines):
                # 老采集组件的 argparse 不认识 --roi：进程直接以退出码 2 收场，
                # stdout 一个 JSON 都没有。这不是采集故障，是"组件太旧"。
                trace.stage_failed = "component_version"
                raise WindowCaptureError(
                    CaptureErrorCode.CAPTURE_FAILED,
                    "采集组件版本不匹配：该组件不支持消息区域下推，请更新完整便携包"
                    "（或显式关闭“只采集消息区域”后重新校准）",
                    trace,
                ) from error
            raise WindowCaptureError(error.code, str(error), trace) from error

        return self._decode_payload(payload, trace, roi, backend, started, hard_deadline)

    def _decode_payload(self, payload, trace, roi, backend, started, hard_deadline):
        if not payload.get("ok"):
            message = str(payload.get("error", "窗口采集失败"))
            # worker 的 traceback 末行通常是真正的失败原因（例如缺失的 DLL），
            # 只显示 error 会把人引向 numpy 的通用建议文本。
            detail = str(payload.get("error_detail", "")).strip()
            if detail:
                reason = detail.splitlines()[-1].strip()
                if reason and reason not in message:
                    message = f"{message} ｜ 具体原因：{reason}"[:600]
            trace.child_error = child_error_token(message)
            trace.stage_failed = (
                str(payload.get("stage_failed") or "")
                or last_stage(trace.stages)
                or "child_error"
            )
            code = map_child_error(message)
            trace.total_ms = _elapsed_ms(started)
            self._record_child_stages(trace)
            self._record(trace, "request_total", "failed", trace.total_ms, code)
            raise WindowCaptureError(code, message, trace)

        # 协议字段先校验，再解码：明显不合约的 payload 不必浪费一次 PNG 解码。
        try:
            returned_width, returned_height, frame_width, frame_height = validate_payload(
                payload, roi
            )
        except _ProtocolError as error:
            trace.total_ms = _elapsed_ms(started)
            trace.stage_failed = "component_version" if error.version_mismatch else "protocol"
            self._record_child_stages(trace)
            self._record(trace, "protocol", "failed", trace.total_ms, CaptureErrorCode.CAPTURE_FAILED)
            self._record(
                trace, "request_total", "failed", trace.total_ms, CaptureErrorCode.CAPTURE_FAILED
            )
            raise WindowCaptureError(
                CaptureErrorCode.CAPTURE_FAILED, str(error), trace
            ) from error

        decode_started = time.monotonic()
        if _remaining(hard_deadline) <= 0.0:
            trace.total_ms = _elapsed_ms(started)
            trace.decode_ms = _elapsed_ms(decode_started)
            trace.stage_failed = "decode"
            self._record_child_stages(trace)
            self._record(
                trace, "decode", "failed", trace.decode_ms, CaptureErrorCode.CAPTURE_TIMEOUT
            )
            self._record(
                trace, "request_total", "failed", trace.total_ms,
                CaptureErrorCode.CAPTURE_TIMEOUT,
            )
            raise WindowCaptureError(
                CaptureErrorCode.CAPTURE_TIMEOUT, "采集结果解码前已用尽预算", trace
            )
        try:
            from PIL import Image

            image = Image.open(BytesIO(base64.b64decode(payload["image"]))).convert("RGB")
            frame = np.asarray(image)[:, :, ::-1].copy()
            height, width = int(frame.shape[0]), int(frame.shape[1])
            if (width, height) != (returned_width, returned_height):
                raise _ProtocolError(
                    "采集组件声明的尺寸与实际图像不一致"
                    f"（声明 {returned_width}x{returned_height}，实际 {width}x{height}）"
                )
        except _ProtocolError as error:
            trace.decode_ms = _elapsed_ms(decode_started)
            trace.total_ms = _elapsed_ms(started)
            trace.stage_failed = "protocol"
            self._record_child_stages(trace)
            self._record(
                trace, "decode", "failed", trace.decode_ms, CaptureErrorCode.CAPTURE_FAILED
            )
            self._record(
                trace, "request_total", "failed", trace.total_ms, CaptureErrorCode.CAPTURE_FAILED
            )
            raise WindowCaptureError(
                CaptureErrorCode.CAPTURE_FAILED, str(error), trace
            ) from error
        except Exception as error:
            trace.decode_ms = _elapsed_ms(decode_started)
            trace.total_ms = _elapsed_ms(started)
            trace.stage_failed = "decode"
            self._record_child_stages(trace)
            self._record(
                trace, "decode", "failed", trace.decode_ms, CaptureErrorCode.CAPTURE_FAILED
            )
            self._record(
                trace, "request_total", "failed", trace.total_ms, CaptureErrorCode.CAPTURE_FAILED
            )
            raise WindowCaptureError(
                CaptureErrorCode.CAPTURE_FAILED, "窗口采集帧解码失败", trace
            ) from error

        if time.monotonic() > hard_deadline:
            # 解码成功但已经越过总预算：结果作废，绝不把迟到帧当成功。
            trace.decode_ms = _elapsed_ms(decode_started)
            trace.total_ms = _elapsed_ms(started)
            trace.stage_failed = "decode"
            self._record_child_stages(trace)
            self._record(
                trace, "decode", "failed", trace.decode_ms, CaptureErrorCode.CAPTURE_TIMEOUT
            )
            self._record(
                trace, "request_total", "failed", trace.total_ms,
                CaptureErrorCode.CAPTURE_TIMEOUT,
            )
            raise WindowCaptureError(
                CaptureErrorCode.CAPTURE_TIMEOUT, "采集结果解码超过总预算，已作废", trace
            )

        trace.decode_ms = _elapsed_ms(decode_started)
        trace.total_ms = _elapsed_ms(started)
        self._record_child_stages(trace)
        self._record(trace, "decode", "ok", trace.decode_ms)
        self._check_delivery_deadline(trace, started, hard_deadline)
        self._record(trace, "request_total", "ok", trace.total_ms)
        # A logging handler can stall too. Never deliver a late frame even when
        # decoding was timely; the caller's fault event records this final outcome.
        self._check_delivery_deadline(trace, started, hard_deadline)
        return CapturedWindowFrame(
            image=frame,
            width=width,
            height=height,
            captured_at=float(payload.get("captured_at", time.time())),
            backend=str(payload.get("backend", backend)),
            frame_width=frame_width,
            frame_height=frame_height,
            roi=roi,
            trace=trace,
        )

    @staticmethod
    def _check_delivery_deadline(trace, started, deadline):
        trace.total_ms = _elapsed_ms(started)
        trace.deadline_overrun_ms = max(0.0, (time.monotonic() - deadline) * 1000)
        if trace.deadline_overrun_ms > 0:
            trace.stage_failed = "diagnostics"
            raise WindowCaptureError(
                CaptureErrorCode.CAPTURE_TIMEOUT, "采集诊断处理超过总预算，迟到帧已作废", trace
            )

    def _abort(
        self,
        trace: CaptureTrace,
        process,
        stderr_lines: list[str],
        readers: list[threading.Thread],
        deadline: float,
        total_started: float,
        process_token=None,
    ) -> None:
        """超预算收尾：kill → 最多等剩余预算 → 关管道 → 放弃等待。

        绝不在这里调 ``communicate()``：只要有孙进程握着管道写端，等它等到 EOF
        就是 0.2.1 的第二处无界等待（W2）。读线程是 daemon；管道句柄交给后台
        清洁线程关闭（``close()`` 本身可能被阻塞的 ``read`` 拖住），调用方立即返回。
        """
        kill_started = time.monotonic()
        try:
            process.kill()
        except Exception:
            pass
        trace.kill_ms = _elapsed_ms(kill_started)
        # 宽限也来自同一个预算；给一个 10ms 的探测下限，避免刚 kill 就误判孤儿。
        grace = min(CAPTURE_KILL_GRACE_SECONDS, _remaining(deadline))
        confirmed = True
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            confirmed = False
            trace.orphan_suspected = True
        except Exception:
            confirmed = False
        if confirmed:
            PROCESS_REGISTRY.release(process_token)
        else:
            PROCESS_REGISTRY.adopt_orphan(process, process_token)
        # 关闭管道不在采集线程上做：孙进程握着写端时 close() 会一直等 read 解锁。
        try:
            STREAM_JANITOR.retire(process)
        except Exception:
            pass
        try:
            _join_readers(readers, deadline)
        except Exception:
            pass
        trace.exit_code = getattr(process, "returncode", None)
        trace.stages = parse_stage_lines(stderr_lines)
        trace.stage_failed = last_stage(trace.stages) or "wait_exit"
        trace.total_ms = _elapsed_ms(total_started)
        trace.deadline_overrun_ms = max(0.0, (time.monotonic() - deadline) * 1000)
        self._record_child_stages(trace)
        self._record(trace, "kill", "ok" if confirmed else "orphan", trace.kill_ms)


class WindowFrameCapturer:
    """给 MonitorService 使用的有界采样适配器；ROI 相对采集帧物理像素。"""

    def __init__(
        self,
        client: WindowCaptureClient | None = None,
        windows: WindowsWindows | None = None,
        *,
        capture_roi_mode: bool = True,
    ) -> None:
        self.client = client or WindowCaptureClient(persistent=True)
        self.windows = windows or WindowsWindows()
        # 手动兼容开关：False 时完全回到 0.2.1（请求整帧 + 父进程本地裁）。
        # 只有在确认组件不支持 ROI 时，才允许用户显式关掉它；默认 True 时绝不
        # 因为一次失败就自动降级。
        self.capture_roi_mode = bool(capture_roi_mode)
        self._sequences: dict[str, int] = {}
        self._generations: dict[str, int] = {}
        # 最近一次真实采集请求的分段信息；service 用它把 request_id 与耗时写进
        # 故障说明，让界面与阶段日志能对上同一次请求。
        self.last_trace: CaptureTrace | None = None

    def validate(self, region: RegionConfig):
        if region.needs_calibration or region.window_hwnd <= 0:
            raise CaptureError(
                CaptureErrorCode.NEEDS_CALIBRATION, "配置来自旧版本，请重新圈选消息区域"
            )
        if region.coordinate_space != "frame":
            raise CaptureError(
                CaptureErrorCode.NEEDS_CALIBRATION, "坐标模式不是采集帧相对坐标"
            )
        if region.capture_backend not in {"wgc", "print-window"}:
            raise CaptureError(CaptureErrorCode.INVALID_REGION, "未知采集后端")
        if self.windows.desktop_locked():
            raise CaptureError(CaptureErrorCode.DESKTOP_LOCKED, "桌面已锁定，请解锁后重新校准")
        window = self.windows.window_from_hwnd(region.window_hwnd)
        if not window:
            raise CaptureError(CaptureErrorCode.WINDOW_NOT_FOUND, "绑定窗口不存在或已关闭")
        from .window_identity import identity_issues
        issues = identity_issues(region, window)
        if issues:
            code, message = issues[0]
            raise CaptureError(code, message)
        if window.minimized:
            raise CaptureError(CaptureErrorCode.WINDOW_MINIMIZED, "窗口已最小化，暂停采集")
        return window

    def capture(self, region: RegionConfig, force: bool = False) -> FrameSample | None:
        # 先清空上一区域的 trace：校验阶段就失败时不能把别人的 request_id 报上去。
        self.last_trace = None
        self.validate(region)
        request_id = new_request_id()
        requested_roi = region.rect if self.capture_roi_mode else None
        try:
            frame = self.client.capture(
                region.window_hwnd,
                region.capture_backend,
                # 开启 ROI 下推时子进程裁好只回传 ROI；关闭时保持 0.2.1 的整帧请求。
                roi=requested_roi,
                request_id=request_id,
                region_id=region.id,
                **({"generation": self._generations.get(region.id, 0),
                    "reference_frame_width": region.reference_frame_width,
                    "reference_frame_height": region.reference_frame_height,
                    "validate_target": lambda: self.validate(region)}
                   if isinstance(self.client, WindowCaptureClient) and self.client.persistent else {}),
            )
        except WindowCaptureError as error:
            self.last_trace = error.trace or getattr(self.client, "last_trace", None)
            raise CaptureError(error.code, str(error)) from error
        self.last_trace = frame.trace or getattr(self.client, "last_trace", None)
        # HWND echoes alone do not prove identity survived queued capture/decoding.
        self.validate(region)
        # ⚠️ 参考尺寸必须与**原始整帧**比对。frame.width/height 是 ROI 尺寸
        # （下推开启时它们等于 region.rect 的宽高），拿它比 reference_frame_width
        # 会让每一帧都判 WINDOW_RESIZED，于是每轮重置基线、永远不通知。
        # ``frame_width`` 缺失（0.2.1 位置参数构造的替身）时退回返回图尺寸。
        frame_width = frame.frame_width or frame.width
        frame_height = frame.frame_height or frame.height
        if (
            region.reference_frame_width
            and frame_width != region.reference_frame_width
        ) or (
            region.reference_frame_height
            and frame_height != region.reference_frame_height
        ):
            raise CaptureError(CaptureErrorCode.WINDOW_RESIZED, "采集帧尺寸已变化，请重新校准")

        if frame.roi is not None:
            # 子进程已经按请求裁好：**不要再裁一次**（二次裁剪会切掉边缘，
            # 边界情况会得到空图），也不允许把 ROI 尺寸当整帧尺寸。
            if requested_roi is not None and (
                frame.roi.x,
                frame.roi.y,
                frame.roi.width,
                frame.roi.height,
            ) != (
                requested_roi.x,
                requested_roi.y,
                requested_roi.width,
                requested_roi.height,
            ):
                raise CaptureError(
                    CaptureErrorCode.CAPTURE_FAILED,
                    "采集组件回显的消息区域与请求不一致（协议不匹配）",
                )
            image = frame.image
            applied = frame.roi
        else:
            # 整帧路径：手动关闭 ROI 下推、圈选/校准，或调用方给的是不支持 ROI
            # 的客户端替身。这里对**整帧**判边界后本地裁一次。
            roi = region.rect
            if (
                roi.x < 0
                or roi.y < 0
                or roi.x + roi.width > frame_width
                or roi.y + roi.height > frame_height
            ):
                raise CaptureError(CaptureErrorCode.INVALID_REGION, "消息区域超出采集帧")
            image = frame.image[roi.y : roi.y + roi.height, roi.x : roi.x + roi.width].copy()
            applied = Rect(roi.x, roi.y, roi.width, roi.height)
        if image.size == 0:
            raise CaptureError(CaptureErrorCode.CAPTURE_FAILED, "消息区域为空")
        self._sequences[region.id] = self._sequences.get(region.id, 0) + 1
        return FrameSample(
            region_id=region.id,
            sequence=self._sequences[region.id],
            captured_at=frame.captured_at,
            image=image,
            rect=Rect(applied.x, applied.y, applied.width, applied.height),
            forced=True,
        )

    def reset(self, region_id: str) -> None:
        self._generations[region_id] = self._generations.get(region_id, 0) + 1

    def prepare(self) -> None:
        if hasattr(self.client, "prepare"):
            self.client.prepare()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        ready = getattr(self.client, "wait_ready", None)
        return ready(timeout) if callable(ready) else True

    def close(self) -> None:
        if hasattr(self.client, "close"):
            self.client.close()
        if hasattr(self.client, "wait_closed"):
            self.client.wait_closed(1.0)
