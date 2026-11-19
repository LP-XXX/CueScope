"""飞书自定义机器人签名、业务响应检查、持久重试和发送限速。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .config import validate_webhook_url
from .health import DeliveryHealth, DeliveryHealthEvent, DeliveryHealthSnapshot
from .metrics import (
    STAGE_NOTIFY_QUEUE,
    STAGE_NOTIFY_RATE_LIMIT,
    STAGE_NOTIFY_SEND,
    TimingRecorder,
)
from .models import NotificationJob
from .storage import Storage

# 单次 HTTP 请求的结果（只用于观测，不参与重试判定）。
REQUEST_OK = "ok"
REQUEST_RETRYABLE = "retryable"
REQUEST_UNKNOWN = "unknown"
REQUEST_REJECTED = "rejected"
# 一个 outbox job 的最终结果。与 storage 的状态名保持一致，便于对照。
OUTCOME_SENT = "sent"
OUTCOME_RETRY = "retry"
OUTCOME_FAILED = "failed"
OUTCOME_UNKNOWN = "unknown"
OUTCOME_EXPIRED = "expired"
OUTCOME_STOPPED = "stopped"
NOTIFY_OUTCOME_CATEGORY = "notify"


class NotifyMetrics:
    """通知分发路径的四个观测点：排队等待、限速等待、请求耗时、业务结果。

    只记录，**不改**限速、重试或投递语义：任何一个观测点出问题都不应该让通知
    发不出去，因此写失败一律吞掉（``_record`` 内 try/except）。

    ``recorder`` 可为 ``None``（离线用例只关心结果计数）；此时仍然累计
    ``outcomes`` / ``requests``，报告不会因为没接 recorder 就变成空白。
    """

    def __init__(self, recorder: TimingRecorder | None = None) -> None:
        self.recorder = recorder
        self._lock = threading.Lock()
        self.requests = 0
        self.outcomes: dict[str, int] = {}
        self.request_outcomes: dict[str, int] = {}

    def _record(self, stage: str, elapsed_ms: float) -> None:
        recorder = self.recorder
        if recorder is None:
            return
        try:
            recorder.record(stage, elapsed_ms)
        except Exception:  # noqa: BLE001 - 观测失败不得影响投递
            pass

    def queue_wait(self, elapsed_ms: float) -> None:
        """从事件写进 outbox 到分发线程开始处理它的等待时间。"""
        self._record(STAGE_NOTIFY_QUEUE, max(0.0, float(elapsed_ms)))

    def rate_limit_wait(self, elapsed_ms: float) -> None:
        """等在 ``RateLimiter.acquire`` 里的时间（0 表示没有触发限速）。"""
        self._record(STAGE_NOTIFY_RATE_LIMIT, max(0.0, float(elapsed_ms)))

    def request(self, elapsed_ms: float, outcome: str) -> None:
        """单次 HTTP 请求耗时与结果。"""
        self._record(STAGE_NOTIFY_SEND, max(0.0, float(elapsed_ms)))
        with self._lock:
            self.requests += 1
            self.request_outcomes[outcome] = self.request_outcomes.get(outcome, 0) + 1

    def result(self, outcome: str) -> None:
        """一个 outbox job 的最终业务结果（sent/retry/failed/unknown/expired/stopped）。"""
        with self._lock:
            self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
        recorder = self.recorder
        if recorder is None:
            return
        try:
            recorder.record_outcome(NOTIFY_OUTCOME_CATEGORY, outcome)
        except Exception:  # noqa: BLE001
            pass

    def as_dict(self) -> dict[str, object]:
        with self._lock:
            return {
                "requests": self.requests,
                "request_outcomes": dict(self.request_outcomes),
                "job_outcomes": dict(self.outcomes),
            }



class NotifyError(Exception):
    retryable = False
    unknown = False


class RetryableNotifyError(NotifyError):
    retryable = True


class UnknownNotifyError(RetryableNotifyError):
    unknown = True


class _DeliveryStopped(Exception):
    pass


def _request_outcome(error: NotifyError) -> str:
    """把异常映射成单次请求的结果名（只用于观测，不参与重试判定）。"""
    if error.unknown:
        return REQUEST_UNKNOWN
    if error.retryable:
        return REQUEST_RETRYABLE
    return REQUEST_REJECTED


class _DeliveryExpired(Exception):
    pass


def feishu_sign(timestamp: int, secret: str) -> str:
    """飞书自定义机器人签名：HMAC key 为 timestamp\\nsecret，消息为空。"""
    if not secret:
        return ""
    digest = hmac.new(f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _chunks(text: str, size: int = 1200) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)] or [""]


def build_messages(payload: dict[str, Any], timestamp: int, secret: str) -> list[dict[str, Any]]:
    parts = _chunks(str(payload.get("text", "")))
    messages: list[dict[str, Any]] = []
    observed = datetime.fromtimestamp(float(payload["observed_at"])).strftime("%Y-%m-%d %H:%M:%S")
    for index, part in enumerate(parts, 1):
        suffix = f"（{index}/{len(parts)}）" if len(parts) > 1 else ""
        rows = [
            [{"tag": "text", "text": f"来源：{payload['region_alias']}\n"}],
            [{"tag": "text", "text": f"命中词：{payload['keyword']}\n"}],
            [{"tag": "text", "text": f"识别时间：{observed}\n"}],
            [{"tag": "text", "text": f"内容{suffix}：{part}\n"}],
            [{"tag": "text", "text": f"事件编号：{payload['event_id']}"}],
        ]
        if payload.get("boundary_uncertain"):
            rows.insert(3, [{"tag": "text", "text": "注意：消息边界不确定\n"}])
        message: dict[str, Any] = {
            "timestamp": str(timestamp),
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": f"线报提醒｜{payload['region_alias']}",
                        "content": rows,
                    }
                }
            },
        }
        if secret:
            message["sign"] = feishu_sign(timestamp, secret)
        messages.append(message)
    return messages


class FeishuClient:
    def __init__(
        self,
        webhook: str,
        secret: str = "",
        timeout: float = 5.0,
        metrics: NotifyMetrics | None = None,
    ) -> None:
        self.webhook = webhook
        self.secret = secret
        self.timeout = timeout
        # dispatcher 会在拿到实例后注入自己的观测器；单独构造时可以为 None。
        self.metrics = metrics

    def send(
        self,
        payload: dict[str, Any],
        *,
        now: float | None = None,
        start_part: int = 0,
        before_request: Callable[[], None] | None = None,
        on_part_sent: Callable[[int], None] | None = None,
    ) -> None:
        try:
            validate_webhook_url(self.webhook)
        except ValueError as error:
            raise NotifyError("Webhook 地址无效，请检查设置") from error
        timestamp = int(time.time() if now is None else now)
        for index, message in enumerate(build_messages(payload, timestamp, self.secret)):
            if index < start_part:
                continue
            if before_request:
                before_request()
            started = time.perf_counter()
            try:
                self._send_message(message)
            except NotifyError as error:
                self._report_request(started, _request_outcome(error))
                raise
            except Exception:
                self._report_request(started, REQUEST_UNKNOWN)
                raise
            else:
                self._report_request(started, REQUEST_OK)
            if on_part_sent:
                on_part_sent(index + 1)

    def _report_request(self, started: float, outcome: str) -> None:
        metrics = self.metrics
        if metrics is None:
            return
        metrics.request((time.perf_counter() - started) * 1000.0, outcome)

    def _send_message(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(message, ensure_ascii=False).encode("utf-8")
        if len(encoded) > 20 * 1024:
            raise NotifyError("通知内容超过飞书请求大小限制，请缩短来源别名或关键词")
        request = urllib.request.Request(
            self.webhook,
            data=encoded,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(65536).decode("utf-8", errors="replace")
        except TimeoutError as error:
            raise UnknownNotifyError("请求超时，投递结果未知") from error
        except urllib.error.HTTPError as error:
            if error.code == 429 or error.code >= 500:
                raise RetryableNotifyError(f"飞书 HTTP {error.code}") from error
            raise NotifyError(f"飞书 HTTP {error.code}") from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise UnknownNotifyError("连接超时，投递结果未知") from error
            raise RetryableNotifyError("网络连接失败") from error
        except (OSError, http.client.HTTPException) as error:
            raise UnknownNotifyError("连接中断，投递结果未知") from error
        try:
            result = json.loads(body)
        except json.JSONDecodeError as error:
            raise UnknownNotifyError("飞书返回非 JSON 响应，投递结果未知") from error
        if not isinstance(result, dict):
            raise UnknownNotifyError("飞书返回无效业务响应，投递结果未知")
        code = result.get("code", result.get("StatusCode", -1))
        if not isinstance(code, int) or isinstance(code, bool):
            raise UnknownNotifyError("飞书返回无效业务状态，投递结果未知")
        if code != 0:
            # 服务端 msg 可能回显 URL、签名或正文，不向日志/队列/UI 转发。
            if code in {99991663, 99991664, 19021}:
                raise RetryableNotifyError(f"飞书限流或临时错误（代码 {code}）")
            raise NotifyError(f"飞书业务错误（代码 {code}），请检查机器人设置")


class RateLimiter:
    def __init__(
        self,
        per_second: int = 1,
        per_minute: int = 60,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if per_second < 1 or per_minute < 1:
            raise ValueError("发送限速必须为正数")
        self.per_second = per_second
        self.per_minute = per_minute
        self.clock = clock
        self.sleeper = sleeper
        self._second = deque[float]()
        self._minute = deque[float]()
        self._lock = threading.Lock()

    def acquire(self, cancel: threading.Event | None = None) -> bool:
        while True:
            if cancel is not None and cancel.is_set():
                return False
            with self._lock:
                now = self.clock()
                while self._second and now - self._second[0] >= 1:
                    self._second.popleft()
                while self._minute and now - self._minute[0] >= 60:
                    self._minute.popleft()
                if len(self._second) < self.per_second and len(self._minute) < self.per_minute:
                    self._second.append(now)
                    self._minute.append(now)
                    return True
                delays = []
                if len(self._second) >= self.per_second:
                    delays.append(1 - (now - self._second[0]))
                if len(self._minute) >= self.per_minute:
                    delays.append(60 - (now - self._minute[0]))
                delay = max(0.01, *delays)
            if cancel is not None:
                if cancel.wait(delay):
                    return False
            else:
                self.sleeper(delay)


class NotificationDispatcher:
    """唯一 outbox 消费线程；超时保留 unknown 并使用相同事件编号重试。"""

    def __init__(
        self,
        storage: Storage,
        client_factory: Callable[[], FeishuClient],
        *,
        ttl_seconds: int = 300,
        max_attempts: int = 5,
        limiter: RateLimiter | None = None,
        metrics: NotifyMetrics | None = None,
        recorder: TimingRecorder | None = None,
        delivery_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.storage = storage
        self.client_factory = client_factory
        self.delivery_enabled = delivery_enabled if delivery_enabled is not None else lambda: True
        self.ttl_seconds = ttl_seconds
        self.max_attempts = max_attempts
        self.limiter = limiter or RateLimiter()
        # 观测器可选：传 ``recorder`` 等价于 ``NotifyMetrics(recorder)``。
        self.metrics = metrics if metrics is not None else NotifyMetrics(recorder)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._health = DeliveryHealth(storage)
        self.last_error = self._health.snapshot.error

    @property
    def delivery_health(self) -> DeliveryHealthSnapshot:
        return self._health.snapshot

    def poll_delivery_health_events(self) -> list[DeliveryHealthEvent]:
        return self._health.poll()

    def ack_delivery_health_event(self, event_id: str) -> None:
        self._health.acknowledge(event_id)

    @staticmethod
    def _is_health_job(job: NotificationJob) -> bool:
        return job.payload.get("region_id") == "system"

    def _delivery_failed(self, detail: str) -> None:
        self.last_error = detail
        self._health.fail(detail)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="feishu-outbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.request_stop()
        if self._thread:
            self._thread.join(timeout=8)

    def request_stop(self) -> None:
        self._stop.set()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.process_once()
            except Exception:
                # 包括存储异常；不把异常正文中的敏感内容写入健康消息。
                self._delivery_failed("通知队列处理异常，请检查本地存储与运行环境")
                self._stop.wait(1)
                continue
            if not worked:
                self._stop.wait(0.25)

    def _before_request(self, job: NotificationJob) -> None:
        if self._stop.is_set() or not self.delivery_enabled():
            raise _DeliveryStopped
        if time.time() - job.created_at > self.ttl_seconds:
            raise _DeliveryExpired
        # 限速等待单独计时：它与采集/网络无关，是"通知排队慢"与"飞书慢"的分界。
        waited_from = time.perf_counter()
        if isinstance(self.limiter, RateLimiter):
            if not self.limiter.acquire(self._stop):
                self.metrics.rate_limit_wait((time.perf_counter() - waited_from) * 1000.0)
                raise _DeliveryStopped
        else:
            self.limiter.acquire()
        self.metrics.rate_limit_wait((time.perf_counter() - waited_from) * 1000.0)
        if self._stop.is_set() or not self.delivery_enabled():
            raise _DeliveryStopped
        if time.time() - job.created_at > self.ttl_seconds:
            raise _DeliveryExpired

    def _record_error(self, job: NotificationJob, error: NotifyError) -> None:
        if not self._is_health_job(job):
            self._delivery_failed(str(error))
        delivery_unknown = error.unknown or job.state == "unknown"
        if job.attempts + 1 >= self.max_attempts or not error.retryable:
            if delivery_unknown:
                self.storage.mark_unknown_terminal(job.event_id, str(error))
                self.metrics.result(OUTCOME_UNKNOWN)
            else:
                self.storage.mark_failed(job.event_id, str(error))
                self.metrics.result(OUTCOME_FAILED)
        else:
            self.storage.schedule_retry(
                job.event_id,
                min(30.0, 2 ** max(0, job.attempts)),
                str(error),
                state="unknown" if delivery_unknown else "pending",
            )
            self.metrics.result(OUTCOME_RETRY)

    def process_once(self) -> bool:
        # Local-only mode and missing credentials pause the persistent outbox.
        # They are user configuration, not failed delivery attempts or recovery.
        if not self.delivery_enabled():
            return False
        if not self._health.flush():
            # Keep the pending transition bounded if its own persistence is broken.
            # No outbound request may outrun the durable incident history.
            self.last_error = "通知健康记录尚未保存，请检查本地存储"
            return False
        jobs = self.storage.due_jobs(limit=1)
        if not jobs:
            return False
        job = jobs[0]
        # 排队等待：从事件写进 outbox（created_at）到分发线程真正开始处理它。
        # 只观察，不影响 due_jobs 的挑选与顺序。
        self.metrics.queue_wait(max(0.0, time.time() - job.created_at) * 1000.0)
        if self.storage.next_part(job.event_id) >= len(_chunks(str(job.payload.get("text", "")))):
            self.storage.mark_sent(job.event_id)
            self.metrics.result(OUTCOME_SENT)
            return True
        if job.attempts >= self.max_attempts:
            error = "达到自动发送次数上限，投递结果需人工核对"
            if not self._is_health_job(job):
                self._delivery_failed(error)
            # 重启时可能停在请求发出后、结果落库前，不能推断为确定失败。
            self.storage.mark_unknown_terminal(job.event_id, error)
            self.metrics.result(OUTCOME_UNKNOWN)
            return True
        now = time.time()
        if now - job.created_at > self.ttl_seconds:
            self.storage.mark_expired(job.event_id, now)
            self.metrics.result(OUTCOME_EXPIRED)
            return True
        attempt_started = False

        def before_request() -> None:
            nonlocal attempt_started
            self._before_request(job)
            if not attempt_started:
                self.storage.mark_attempt(job.event_id)
                attempt_started = True

        try:
            client = self.client_factory()
            if isinstance(client, FeishuClient):
                # 观测器只补空缺：调用方自己配了 metrics 时尊重它，不覆盖。
                if client.metrics is None:
                    client.metrics = self.metrics
                client.send(
                    job.payload,
                    now=now,
                    start_part=self.storage.next_part(job.event_id),
                    before_request=before_request,
                    on_part_sent=lambda part: self.storage.mark_part_sent(job.event_id, part),
                )
            else:
                # 兼容离线模拟接收器和原有注入接口。
                before_request()
                started = time.perf_counter()
                try:
                    client.send(job.payload, now=now)
                except NotifyError as error:
                    self.metrics.request(
                        (time.perf_counter() - started) * 1000.0, _request_outcome(error)
                    )
                    raise
                except Exception:
                    self.metrics.request((time.perf_counter() - started) * 1000.0, REQUEST_UNKNOWN)
                    raise
                else:
                    self.metrics.request((time.perf_counter() - started) * 1000.0, REQUEST_OK)
        except _DeliveryStopped:
            self.metrics.result(OUTCOME_STOPPED)
            return False
        except _DeliveryExpired:
            self.storage.mark_expired(job.event_id)
            self.metrics.result(OUTCOME_EXPIRED)
        except NotifyError as error:
            self._record_error(job, error)
        except Exception:
            self._record_error(job, UnknownNotifyError("发送处理异常，投递结果未知"))
        else:
            self.storage.mark_sent(job.event_id)
            if attempt_started and not self._is_health_job(job):
                self._health.succeeded(job.event_id)
                self.last_error = ""
            self.metrics.result(OUTCOME_SENT)
        return True
