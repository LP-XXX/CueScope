"""Runtime for independently gated, destination-bound notification workers."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
        self._route_keys = {}
        self._stopping = False
        self._maintenance = None
        self._next_cleanup = 0
        self._event_owners = {}
        self._update_generation = 0
        self._verified_generation = -1
        self._verifier = ThreadPoolExecutor(max_workers=1, thread_name_prefix="route-permissions")
        self.configuration_error = ""
        self.storage.set_notification_routes(self.routes)

    def suspend(self):
        self._update_generation += 1

    def configure(self, mode, feishu, settings, *, unavailable=False):
        if self._stopping:
            return
        self.suspend()
        generation = self._update_generation
        targets = [QQTarget(**item) for item in settings.get("targets", [])]
        active = {}
        all_keys = {}
        if feishu[0]:
            key = destination_id("feishu", feishu[0])
            all_keys[key] = key
            if mode != "local":
                active[key] = tuple(feishu)
            self._labels[key] = "飞书"
        for target in targets:
            self._labels[target.delivery_id] = target.alias
            all_keys[target.id] = target.delivery_id
            if mode == "auto" and target.enabled:
                active[target.delivery_id] = target
        overrides = {key: tuple(values) for key, values in settings.get("tasks", {}).items()}
        self._overrides = overrides
        self._route_keys = all_keys
        self._active = active
        if unavailable:
            self.configuration_error = "通知配置无法读取，外发已暂停。请检查 Windows 用户或恢复备份。"
            return

        def allowed(region):
            return tuple(all_keys[key] for key in overrides.get(region, all_keys) if key in all_keys)

        def check_permissions():
            try:
                self.storage.revoke_unselected_deliveries(allowed)
            except Exception:
                if generation == self._update_generation:
                    self.configuration_error = "投递授权未能写入，外发已暂停。请检查磁盘并重试保存。"
                return
            if generation == self._update_generation:
                self.configuration_error = ""
                self._verified_generation = generation

        self._verifier.submit(check_permissions)
        self.reconcile()

    def routes(self, region_id="system"):
        active = self._active
        keys = self._route_keys
        return tuple(keys[key] for key in self._overrides.get(region_id, keys) if key in keys and keys[key] in active)

    def label(self, key):
        return self._labels.get(key, "历史接收端 " + key[:12])

    def can_send(self, key):
        return (not self._stopping and key in self._active
                and self._verified_generation == self._update_generation)

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
                delivery_enabled=lambda k=key, d=definition: self.can_send(k) and self._active.get(k) == d,
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
        self._verifier.shutdown(wait=True)
