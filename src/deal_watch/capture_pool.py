"""Bounded, prewarmed capture processes; idle workers never capture a window.

The parent owns deadlines and process leases, including workers whose native
calls cannot be cancelled. Only a matching, timely response can be consumed.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

from . import window_capture as wc

PROTOCOL_VERSION = 2
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_REQUEST_BYTES = 8192


class PoolFailure(RuntimeError):
    def __init__(self, stage: str, message: str, *, timeout: bool = False):
        super().__init__(message)
        self.stage = stage
        self.timeout = timeout

    @property
    def requires_restart(self):
        return self.stage == "component_version" or (self.stage == "deps_init" and not self.timeout)


def _resources(process) -> dict:
    """Called only by the background manager, never by the UI."""
    result = {"pid": process.pid}
    if sys.platform != "win32":
        return result
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                "PagefileUsage", "PeakPagefileUsage", "PrivateUsage",
            )
        ]

    handle = wintypes.HANDLE(int(process._handle))
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        result.update(private_bytes=counters.PrivateUsage, working_set_bytes=counters.WorkingSetSize)
    count = wintypes.DWORD()
    if ctypes.windll.kernel32.GetProcessHandleCount(handle, ctypes.byref(count)):
        result["handles"] = count.value
    # Toolhelp snapshots are bounded system metadata, not process memory inspection.
    class ThreadEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                    ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD)]

    kernel = ctypes.windll.kernel32
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
    if snapshot and snapshot != ctypes.c_void_p(-1).value:
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            more = kernel.Thread32First(wintypes.HANDLE(snapshot), ctypes.byref(entry))
            threads = 0
            while more:
                threads += entry.th32OwnerProcessID == process.pid
                more = kernel.Thread32Next(wintypes.HANDLE(snapshot), ctypes.byref(entry))
            result["threads"] = threads
        finally:
            kernel.CloseHandle(wintypes.HANDLE(snapshot))
    return result


class _Worker:
    def __init__(self, process, token):
        self.process = process
        self.token = token
        self.responses: queue.Queue = queue.Queue(maxsize=2)
        self.commands: queue.Queue = queue.Queue(maxsize=1)
        self.broken = threading.Event()
        self.lock = threading.Lock()
        self.stages: dict[str, float] = {}
        self.stage_received: dict[str, float] = {}
        self.request_id = ""
        self.busy = False
        self.ready = False
        self.retired = False
        self.readers: list[threading.Thread] = []

    def start_io(self):
        acquired = 0
        for _ in range(2):
            if not wc._READER_SLOTS.acquire(blocking=False):
                for _ in range(acquired):
                    wc._READER_SLOTS.release()
                raise PoolFailure("reader_start", "采集输出读取资源暂不可用")
            acquired += 1
        started = 0
        try:
            for stream, kind in ((self.process.stdout, "result"), (self.process.stderr, "stage")):
                thread = threading.Thread(target=self._read, args=(stream, kind), daemon=True,
                                          name=f"capture-pool-{kind}")
                thread.start()
                self.readers.append(thread)
                started += 1
            threading.Thread(target=self._write, daemon=True, name="capture-pool-input").start()
        finally:
            for _ in range(acquired - started):
                wc._READER_SLOTS.release()

    def _read(self, stream, kind):
        limit = MAX_RESPONSE_BYTES if kind == "result" else MAX_REQUEST_BYTES
        try:
            while not self.broken.is_set():
                line = stream.readline(limit + 1)
                if not line or len(line) > limit or not line.endswith("\n"):
                    break
                value = json.loads(line)
                if not isinstance(value, dict):
                    break
                if kind == "result":
                    self.responses.put_nowait(value)
                else:
                    stage = value.get("stage", "")
                    offset = value.get("t_ms")
                    with self.lock:
                        if (value.get("request_id") == self.request_id
                                and isinstance(stage, str) and wc._STAGE_NAME_PATTERN.fullmatch(stage)
                                and isinstance(offset, int | float) and not isinstance(offset, bool)
                                and 0 <= offset < 1e9 and len(self.stages) < 32):
                            self.stages[stage] = float(offset)
                            self.stage_received.setdefault(stage, time.monotonic())
        except (OSError, ValueError, queue.Full, AttributeError):
            pass
        finally:
            self.broken.set()
            wc._READER_SLOTS.release()

    def _write(self):
        try:
            while not self.broken.is_set():
                try:
                    command = self.commands.get(timeout=0.1)
                except queue.Empty:
                    continue
                self.process.stdin.write(command)
                self.process.stdin.flush()
        except (OSError, ValueError, AttributeError):
            self.broken.set()

    def retire(self):
        with self.lock:
            if self.retired:
                return
            self.retired = True
        self.broken.set()
        # The registered lease remains occupied until poll confirms termination.
        wc.PROCESS_REGISTRY.defer_reap(self.process, self.token)
        wc.STREAM_JANITOR.retire(self.process)

    def shutdown(self):
        if self.busy or self.broken.is_set():
            self.retire()
            return
        with self.lock:
            if self.retired:
                return
            self.retired = True
        try:
            self.commands.put_nowait(json.dumps({"type": "shutdown", "protocol_version": PROTOCOL_VERSION}) + "\n")
        except queue.Full:
            self.broken.set()
        # Give the idle server a chance to exit through its normal EOF protocol.
        # Reaper still owns the lease and enforces cleanup if stdin is stuck.
        wc.PROCESS_REGISTRY.defer_reap(self.process, self.token)
        wc.STREAM_JANITOR.retire(self.process)


class CapturePool:
    """One active request plus one warm spare, shared global two-process quota."""

    def __init__(self, command, *, startup_timeout=5.0):
        self.command = list(command)
        self.startup_timeout = startup_timeout
        self._condition = threading.Condition()
        self._closed = False
        self._workers: list[_Worker] = []
        self._manager = None
        self._request_lock = threading.Lock()
        self._starts = 0
        self._failures = 0
        self._error = ""
        self._startup_error = None
        self._stats: dict = {"state": "waiting", "process_starts": 0, "failures": 0, "processes": []}

    def prepare(self):
        with self._condition:
            if self._closed or self._manager is not None:
                return
            self._manager = threading.Thread(target=self._manage, daemon=True, name="capture-pool-manager")
            self._manager.start()

    @property
    def closed(self):
        return self._closed

    def _launch(self):
        started = time.monotonic()
        deadline = started + self.startup_timeout
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        job = wc.START_SUPERVISOR.start(self.command + ["--serve"], {
            "stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
            "text": True, "encoding": "utf-8", "errors": "strict", "creationflags": flags,
            "env": {**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
                    "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1"},
        }, deadline)
        if job is None:
            return None
        self._starts += 1
        while not job.event.wait(min(0.05, wc._remaining(deadline))):
            if self._closed or wc._remaining(deadline) <= 0:
                job.abandon()
                raise PoolFailure("component_start", "采集组件启动超过预算", timeout=True)
        if self._closed:
            job.abandon()
            return None
        process = job.claim()
        if process is None:
            job.abandon()
            raise PoolFailure("component_start", "采集组件启动失败或迟到", timeout=True)
        worker = _Worker(process, job.token)
        try:
            # Once claimed, every path owns the same lease and must retire it on
            # failure, including diagnostic I/O before the readers have started.
            wc.log_capture_stage(wc.logging.getLogger(wc.CAPTURE_STAGE_LOGGER_NAME),
                                 stage="pool_component_start", pid=process.pid,
                                 elapsed_ms=wc._elapsed_ms(started), outcome="ok")
            if time.monotonic() >= deadline:
                raise PoolFailure("component_start", "采集组件启动超过预算", timeout=True)
            worker.start_io()
            dependency_wait = time.monotonic()
            handshake = self._receive(worker, deadline)
            if (type(handshake.get("protocol_version")) is not int
                    or handshake.get("protocol_version") != PROTOCOL_VERSION or handshake.get("type") != "ready"):
                raise PoolFailure("component_version", "采集组件协议版本不匹配，请使用同版完整便携包")
            if handshake.get("ok") is not True:
                raise PoolFailure("deps_init", "采集组件依赖初始化失败")
            wc.log_capture_stage(wc.logging.getLogger(wc.CAPTURE_STAGE_LOGGER_NAME),
                                 stage="pool_dependencies_ready", pid=process.pid,
                                 elapsed_ms=wc._elapsed_ms(dependency_wait), outcome="ok")
            if time.monotonic() >= deadline:
                raise PoolFailure("deps_init", "采集组件就绪响应迟到，已作废", timeout=True)
            worker.ready = True
            return worker
        except Exception:
            worker.retire()
            raise

    @staticmethod
    def _receive(worker, deadline):
        while time.monotonic() < deadline:
            try:
                response = worker.responses.get(timeout=min(0.02, wc._remaining(deadline)))
                if time.monotonic() >= deadline:
                    break
                return response
            except queue.Empty:
                if worker.broken.is_set():
                    raise PoolFailure("transport", "采集组件已退出或输出协议异常") from None
        raise PoolFailure("deps_init", "等待采集组件就绪超时", timeout=True)

    def _manage(self):
        retry_at = 0.0
        while not self._closed:
            with self._condition:
                dead = [w for w in self._workers if w.broken.is_set()]
                self._workers = [w for w in self._workers if w not in dead]
            for worker in dead:
                worker.retire()
            blocked = self._startup_error is not None and self._startup_error.requires_restart
            if len(self._workers) < 2 and time.monotonic() >= retry_at and not blocked:
                try:
                    worker = self._launch()
                    if worker is not None:
                        with self._condition:
                            if self._closed:
                                worker.retire()
                            else:
                                self._workers.append(worker)
                                self._error = ""
                                self._startup_error = None
                            self._condition.notify_all()
                    else:
                        retry_at = time.monotonic() + 0.25
                except Exception as error:
                    self._failures += 1
                    self._error = error.stage if isinstance(error, PoolFailure) else "component_start"
                    self._startup_error = PoolFailure(
                        self._error, str(error) if isinstance(error, PoolFailure) else "采集组件启动失败",
                        timeout=error.timeout if isinstance(error, PoolFailure) else False,
                    )
                    with self._condition:
                        self._condition.notify_all()
                    retry_at = time.monotonic() + 1.0
            processes = []
            for worker in list(self._workers):
                try:
                    processes.append(_resources(worker.process))
                except (OSError, ValueError, AttributeError):
                    processes.append({"pid": worker.process.pid})
            orphans = wc.PROCESS_REGISTRY.orphans
            with self._condition:
                self._stats = {
                    "state": ("manual_action" if wc.PROCESS_REGISTRY.needs_attention or
                               (self._startup_error is not None and self._startup_error.requires_restart) else
                              "reaping" if orphans else ("ready" if self._workers else "waiting")),
                    "process_starts": self._starts, "failures": self._failures,
                    "active_processes": wc.PROCESS_REGISTRY.active, "orphan_processes": orphans,
                    "processes": processes, "last_error_stage": self._error,
                }
                self._condition.wait(0.1 if len(self._workers) < 2 else 1.0)

    def stats(self):
        with self._condition:
            return {**self._stats, "processes": [dict(p) for p in self._stats["processes"]]}

    def wait_ready(self, timeout=5.0):
        """Startup gate, separate from a capture request's deadline."""
        self.prepare()
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._closed:
                # An OS wake-up or lock acquisition can itself cross the budget.
                # A newly ready worker must not turn an expired startup into success.
                if time.monotonic() >= deadline:
                    return False
                if any(w.ready and not w.broken.is_set() for w in self._workers):
                    return True
                if self._startup_error is not None and self._startup_error.requires_restart:
                    raise PoolFailure(self._startup_error.stage, str(self._startup_error))
                self._condition.wait(min(0.05, wc._remaining(deadline)))
        return False

    def invalidate(self, pid):
        with self._condition:
            workers = [w for w in self._workers if w.process.pid == pid]
        for worker in workers:
            worker.retire()

    def request(self, request, deadline, trace, *, validate_target=None):
        self.prepare()
        started = time.monotonic()
        if not self._request_lock.acquire(timeout=wc._remaining(deadline)):
            raise PoolFailure("queue_wait", "采集组件繁忙，等待超过预算", timeout=True)
        worker = None
        try:
            with self._condition:
                while not self._closed:
                    if not self._workers and self._startup_error is not None and self._startup_error.requires_restart:
                        raise PoolFailure(self._startup_error.stage, str(self._startup_error))
                    if time.monotonic() >= deadline:
                        raise PoolFailure(self._error or "worker_ready", "请求排队已超过预算", timeout=True)
                    available = [w for w in self._workers if w.ready and not w.broken.is_set() and not w.busy]
                    if available:
                        worker = available[0]
                        worker.busy = True
                        break
                    if time.monotonic() >= deadline:
                        raise PoolFailure(self._error or "worker_ready", "等待采集组件就绪超时", timeout=True)
                    self._condition.wait(min(0.05, wc._remaining(deadline)))
            if worker is None:
                raise PoolFailure("cancelled", "采集已取消")
            trace.worker_id = worker.process.pid
            trace.queue_wait_ms += wc._elapsed_ms(started)
            with worker.lock:
                if not worker.responses.empty():
                    raise PoolFailure("protocol", "采集组件存在未消费响应，已隔离")
                worker.request_id = request["request_id"]
                worker.stages.clear()
                worker.stage_received.clear()
            line = json.dumps(request, separators=(",", ":")) + "\n"
            if len(line) > MAX_REQUEST_BYTES:
                raise PoolFailure("protocol", "采集请求过大")
            if validate_target is not None:
                trace.stage_failed = "target_validation"
                validate_target()
                trace.stage_failed = ""
            if time.monotonic() >= deadline:
                raise PoolFailure("queue_wait", "请求排队已超过预算，未发送采集请求", timeout=True)
            worker.commands.put_nowait(line)
            while time.monotonic() < deadline:
                if self._closed:
                    raise PoolFailure("cancelled", "采集已取消")
                with worker.lock:
                    trace.stages = dict(worker.stages)
                    wgc_start = worker.stage_received.get("wgc_construct_started")
                    first_frame = worker.stage_received.get("first_valid_frame")
                if wgc_start is not None and first_frame is None and time.monotonic() - wgc_start >= 3.0:
                    raise PoolFailure(wc.last_stage(trace.stages) or "wgc_start_requested",
                                      "WGC 启动或获取有效帧超过 3 秒", timeout=True)
                try:
                    result = worker.responses.get(timeout=min(0.02, wc._remaining(deadline)))
                except queue.Empty:
                    if worker.broken.is_set():
                        raise PoolFailure(wc.last_stage(trace.stages) or "transport", "采集组件连接中断") from None
                    continue
                with worker.lock:
                    trace.stages = dict(worker.stages)
                for field in ("protocol_version", "request_id", "region_id", "generation", "hwnd", "backend", "roi",
                              "reference_frame_width", "reference_frame_height"):
                    if result.get(field) != request[field] or type(result.get(field)) is not type(request[field]):
                        raise PoolFailure("protocol", "采集响应身份或区域代数不匹配，已隔离")
                if result.get("type") != "result":
                    raise PoolFailure("protocol", "采集响应类型不匹配")
                if type(result.get("ok")) is not bool or type(result.get("worker_reusable")) is not bool:
                    raise PoolFailure("protocol", "采集响应状态类型不匹配")
                if time.monotonic() >= deadline:
                    raise PoolFailure("transport", "采集响应迟到，已作废", timeout=True)
                if result.get("ok") is not True:
                    self._failures += 1
                if result.get("worker_reusable") is not True:
                    worker.retire()
                return result
            raise PoolFailure(wc.last_stage(trace.stages) or "transport", "采集请求超过总预算", timeout=True)
        except wc.CaptureError:
            # A changed target is not a broken capture component.
            raise
        except Exception:
            self._failures += 1
            if worker is not None:
                worker.retire()
            raise
        finally:
            if worker is not None:
                worker.busy = False
            self._request_lock.release()

    def close(self):
        with self._condition:
            self._closed = True
            workers, self._workers = self._workers, []
            self._stats = {**self._stats, "state": "closed", "processes": []}
            self._condition.notify_all()
        for worker in workers:
            worker.shutdown()

    def wait_closed(self, timeout=1.0):
        """Background shutdown callers may await leases; UI close stays nonblocking."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (self._manager is None or not self._manager.is_alive()) and wc.PROCESS_REGISTRY.active == 0:
                return True
            time.sleep(0.01)
        return False
