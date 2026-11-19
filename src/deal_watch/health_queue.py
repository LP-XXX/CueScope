"""Bounded background persistence for UI-originated health notices.

The UI never acquires the storage transaction lock. Failed writes retry a bounded
number of times, then stay retained in the same bounded queue until an explicit
retry or resubmission. Closing never blocks longer than the caller's budget.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .models import MatchEvent
from .storage import Storage


@dataclass(slots=True)
class _WriteJob:
    event: MatchEvent
    on_saved: Callable[[MatchEvent], None] | None
    due_at: float
    attempts: int = 0


class HealthEventWriter:
    def __init__(
        self, storage: Storage, cooldown_seconds: int,
        on_saved: Callable[[MatchEvent], None] | None = None,
        logger: logging.Logger | None = None,
        *, capacity: int = 64, max_attempts: int = 3, retry_delay: float = 0.25,
    ):
        if capacity < 1 or max_attempts < 1 or retry_delay < 0:
            raise ValueError("健康记录队列参数无效")
        self.storage = storage
        self.cooldown_seconds = cooldown_seconds
        self.on_saved = on_saved
        self.logger = logger or logging.getLogger("deal_watch")
        self.capacity = capacity
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay
        self._condition = threading.Condition()
        self._pending: dict[str, _WriteJob] = {}
        self._failed: dict[str, _WriteJob] = {}
        self._accepting = True
        self._stop_deadline: float | None = None
        self._last_error = ""
        self._thread = threading.Thread(target=self._run, name="health-record-writer", daemon=True)
        self._thread.start()

    @property
    def last_error(self) -> str:
        with self._condition:
            return self._last_error

    @property
    def pending_count(self) -> int:
        with self._condition:
            return len(self._pending) + len(self._failed)

    @property
    def failed_count(self) -> int:
        with self._condition:
            return len(self._failed)

    def submit(
        self, event: MatchEvent, on_saved: Callable[[MatchEvent], None] | None = None
    ) -> bool:
        with self._condition:
            if not self._accepting:
                self._last_error = "健康记录队列正在关闭，事件未接收"
                return False
            if event.id in self._pending:
                return True
            if event.id in self._failed:
                job = self._failed.pop(event.id)
                job.attempts = 0
                job.due_at = time.monotonic()
                if on_saved is not None:
                    job.on_saved = on_saved
                self._pending[event.id] = job
                self._condition.notify()
                return True
            if len(self._pending) + len(self._failed) >= self.capacity:
                self._last_error = "健康记录队列已满，事件未接收；请重试"
                return False
            self._pending[event.id] = _WriteJob(copy.deepcopy(event), on_saved, time.monotonic())
            self._condition.notify()
            return True

    def retry_failed(self) -> int:
        with self._condition:
            if not self._accepting:
                return 0
            count = len(self._failed)
            for event_id, job in self._failed.items():
                job.attempts = 0
                job.due_at = time.monotonic()
                self._pending[event_id] = job
            self._failed.clear()
            self._condition.notify()
            return count

    def _saved(self, job: _WriteJob) -> None:
        callbacks = [callback for callback in (self.on_saved, job.on_saved) if callback is not None]
        if len(callbacks) == 2 and callbacks[0] is callbacks[1]:
            callbacks.pop()
        for callback in callbacks:
            try:
                callback(job.event)
            except Exception:  # noqa: BLE001 - a UI callback cannot kill persistence
                self.logger.warning("health record saved callback failed event_id=%s", job.event.id)

    def _run(self) -> None:
        while True:
            with self._condition:
                while True:
                    now = time.monotonic()
                    if self._stop_deadline is not None and (
                        now >= self._stop_deadline or not self._pending
                    ):
                        return
                    if self._pending:
                        job = min(self._pending.values(), key=lambda item: item.due_at)
                        delay = job.due_at - now
                        if delay <= 0:
                            break
                        if self._stop_deadline is not None:
                            delay = min(delay, self._stop_deadline - now)
                        self._condition.wait(delay)
                    else:
                        self._condition.wait()
            try:
                self.storage.record_match(
                    job.event, self.cooldown_seconds, idempotent=True
                )
            except Exception as error:  # noqa: BLE001 - keep evidence pending on any storage failure
                with self._condition:
                    job.attempts += 1
                    exhausted = job.attempts >= self.max_attempts or self._stop_deadline is not None
                    if exhausted:
                        self._pending.pop(job.event.id, None)
                        self._failed[job.event.id] = job
                        self._last_error = "健康记录写入失败，已保留待重试"
                    else:
                        job.due_at = time.monotonic() + self.retry_delay * (2 ** (job.attempts - 1))
                        self._last_error = "健康记录写入失败，正在重试"
                self.logger.warning(
                    "health record write failed event_id=%s attempt=%s error_type=%s",
                    job.event.id, job.attempts, type(error).__name__,
                )
            else:
                with self._condition:
                    self._pending.pop(job.event.id, None)
                    if not self._failed:
                        self._last_error = ""
                self._saved(job)

    def request_stop(self, timeout: float = 1.0) -> None:
        with self._condition:
            self._accepting = False
            deadline = time.monotonic() + max(0.0, timeout)
            self._stop_deadline = (deadline if self._stop_deadline is None
                                   else min(deadline, self._stop_deadline))
            self._condition.notify_all()

    def close(self, timeout: float = 1.0) -> bool:
        self.request_stop(timeout)
        self._thread.join(max(0.0, timeout))
        return not self.is_alive() and self.pending_count == 0

    def is_alive(self) -> bool:
        return self._thread.is_alive()
