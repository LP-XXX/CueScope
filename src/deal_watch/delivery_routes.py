"""Runtime for independently gated, destination-bound notification workers."""

from __future__ import annotations

import threading
import time
from dataclasses import replace

from .diagnostics import HitImageStore
from .health import DeliveryHealthSnapshot
from .notify import FeishuClient, NotificationDispatcher
from .qq_notify import PushPlusClient, QQClient, QQTarget
from .security import destination_id


class DeliveryRoutes:
    def __init__(self, storage, data_dir, ttl, metrics):
        self.storage = storage
        self.data_dir = data_dir
        self.ttl = ttl
        self.metrics = metrics
        self._active = {}
        self._workers = {}
        self._retiring = {}
        self._labels = {}
        self._overrides = {}
        self._stopping = False
        self._maintenance = None
        self._next_cleanup = 0
        self._event_owners = {}
        self.storage.set_notification_routes(self.routes)

    def configure(self, mode, feishu, settings):
        targets = [QQTarget(**item) for item in settings.get("targets", [])]
        active = {}
        if feishu[0] and mode != "local":
            key = destination_id("feishu", feishu[0])
            active[key] = tuple(feishu)
            self._labels[key] = "飞书"
        for target in targets:
            self._labels[target.id] = target.alias
            if mode == "auto" and target.enabled:
                active[target.id] = target
        self._overrides = {key: tuple(values) for key, values in settings.get("tasks", {}).items()}
        self._active = active
        self.reconcile()

    def routes(self, region_id="system"):
        active = self._active
        return tuple(key for key in self._overrides.get(region_id, active) if key in active)

    def label(self, key):
        return self._labels.get(key, "历史接收端 " + key[:12])

    def reconcile(self):
        if self._stopping:
            return
        for key, (definition, worker) in list(self._workers.items()):
            if self._active.get(key) != definition:
                worker.request_stop()
                self._retiring[key] = worker
                del self._workers[key]
        self._retiring = {key: worker for key, worker in self._retiring.items() if worker.is_alive()}
        for key, definition in self._active.items():
            if key in self._workers or key in self._retiring:
                continue
            if isinstance(definition, tuple):
                client = FeishuClient(*definition)
            elif definition.kind == "qq":
                client = QQClient(definition)
            else:
                client = PushPlusClient(definition)
            worker = NotificationDispatcher(
                self.storage.for_destination(key), lambda item=client: item, ttl_seconds=self.ttl,
                metrics=self.metrics,
                delivery_enabled=lambda k=key, d=definition: not self._stopping and self._active.get(k) == d,
            )
            self._workers[key] = (definition, worker)
            worker.start()
        if time.monotonic() >= self._next_cleanup and not (self._maintenance and self._maintenance.is_alive()):
            self._next_cleanup = time.monotonic() + 3600
            self._maintenance = threading.Thread(target=self._cleanup, name="retention-cleanup", daemon=True)
            self._maintenance.start()

    def _cleanup(self):
        HitImageStore(self.data_dir).cleanup()
        self.storage.expire_unavailable_routes(tuple(self._active), self.ttl)

    def poll_delivery_health_events(self):
        self.reconcile()
        events = []
        for key, (_, worker) in list(self._workers.items()):
            for item in worker.poll_delivery_health_events():
                self._event_owners[item.event_id] = worker
                events.append(replace(item, detail=f"{self.label(key)} · {item.detail}"))
        return events

    def ack_delivery_health_event(self, event_id):
        worker = self._event_owners.pop(event_id, None)
        if worker:
            worker.ack_delivery_health_event(event_id)

    @property
    def delivery_health(self):
        snapshots = [worker.delivery_health for _, worker in self._workers.values()]
        return next((item for item in snapshots if item.state == "failed"), DeliveryHealthSnapshot())

    def request_stop(self):
        self._stopping = True
        for _, worker in self._workers.values():
            worker.request_stop()
        for worker in self._retiring.values():
            worker.request_stop()

    def is_alive(self):
        return (any(worker.is_alive() for _, worker in self._workers.values())
                or any(worker.is_alive() for worker in self._retiring.values())
                or bool(self._maintenance and self._maintenance.is_alive()))

    def stop(self):
        self.request_stop()
        for _, worker in self._workers.values():
            worker.stop()
        for worker in self._retiring.values():
            worker.stop()
        if self._maintenance:
            self._maintenance.join(timeout=2)
