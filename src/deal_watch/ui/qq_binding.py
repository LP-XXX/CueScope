"""Short-lived Tencent pairing session. Only a matching challenge is retained."""

from __future__ import annotations

import json
import re
import secrets
import threading
from urllib.parse import urlsplit

from PySide6.QtCore import QEventLoop, QThread, QTimer, QUrl, Signal
from PySide6.QtWebSockets import QWebSocket

from ..qq_notify import QQ_API, QQAccess, _check_qq, _request

_workers = set()


def start_binding(worker):
    _workers.add(worker)
    worker.finished.connect(lambda: _workers.discard(worker))
    worker.start()


def stop_bindings():
    for worker in tuple(_workers):
        worker.cancelled.set()


def wait_bindings():
    for worker in tuple(_workers):
        worker.wait()


def matching_recipient(packet, scope, code):
    if packet.get("op") != 0:
        return ""
    required = "GROUP_AT_MESSAGE_CREATE" if scope == "group" else "C2C_MESSAGE_CREATE"
    data = packet.get("d")
    if not isinstance(data, dict) or packet.get("t") != required:
        return ""
    content = re.sub(r"^\s*<@!?\w+>\s*", "", str(data.get("content", ""))).strip()
    if content != code:
        return ""
    author = data.get("author") or {}
    target = data.get("group_openid") if scope == "group" else author.get("user_openid")
    return target if isinstance(target, str) and re.fullmatch(r"[\w-]{1,128}", target, re.ASCII) else ""


class BindingThread(QThread):
    ready = Signal(str)
    matched = Signal(str)
    failed = Signal(str)

    def __init__(self, app_id, secret, scope):
        super().__init__()
        self.access = QQAccess(app_id, secret)
        self.scope = scope
        self.cancelled = threading.Event()

    def run(self):
        try:
            def check():
                if self.cancelled.is_set():
                    raise ValueError("绑定已取消")

            headers = self.access.headers(check)
            check()
            gateway = _request("GET", QQ_API + "/gateway", headers=headers)
            _check_qq(gateway)
            url = gateway.get("url", "")
            address = urlsplit(url)
            if (address.scheme != "wss" or address.hostname not in {"api.bot.qq.com", "api.sgroup.qq.com"}
                    or address.username or address.password or address.port not in {None, 443}):
                raise ValueError("QQ 返回了无法验证的网关地址")
            self._listen(url, headers["Authorization"])
        except Exception as error:
            from ..notify import NotifyError
            if not self.cancelled.is_set():
                self.failed.emit(str(error) if isinstance(error, NotifyError | ValueError) else "QQ 绑定连接失败")

    def _listen(self, url, token):
        loop = QEventLoop()
        socket = QWebSocket()
        socket.setMaxAllowedIncomingMessageSize(1 << 20)
        heartbeat = QTimer()
        cancellation = QTimer()
        timeout = QTimer()
        timeout.setSingleShot(True)
        challenge = "绑定 " + secrets.token_hex(5).upper()
        seq = None
        bound = ""
        authenticated = False

        def send(op, data):
            socket.sendTextMessage(json.dumps({"op": op, "d": data}))

        def receive(raw):
            nonlocal seq, bound, authenticated
            try:
                packet = json.loads(raw)
                if not isinstance(packet, dict) or self.cancelled.is_set():
                    return
                if packet.get("s") is not None:
                    seq = packet["s"]
                op = packet.get("op")
                if op == 10:
                    interval = int(packet["d"]["heartbeat_interval"])
                    send(2, {"token": token, "intents": 1 << 25, "shard": [0, 1],
                             "properties": {"$os": "windows", "$browser": "CueScope", "$device": "CueScope"}})
                    heartbeat.start(max(1000, min(interval, 60000)))
                elif op == 0 and packet.get("t") == "READY":
                    authenticated = True
                    self.ready.emit(challenge)
                elif op in {7, 9}:
                    loop.quit()
                elif authenticated:
                    bound = matching_recipient(packet, self.scope, challenge)
                    if bound:
                        loop.quit()
            except (KeyError, TypeError, ValueError):
                loop.quit()

        socket.textMessageReceived.connect(receive)
        socket.disconnected.connect(loop.quit)
        socket.errorOccurred.connect(loop.quit)
        heartbeat.timeout.connect(lambda: send(1, seq))
        cancellation.timeout.connect(lambda: loop.quit() if self.cancelled.is_set() else None)
        timeout.timeout.connect(loop.quit)
        cancellation.start(150)
        timeout.start(120000)
        socket.open(QUrl(url))
        loop.exec()
        heartbeat.stop()
        cancellation.stop()
        timeout.stop()
        socket.abort()
        if self.cancelled.is_set():
            return
        if bound:
            self.matched.emit(bound)
        else:
            self.failed.emit("绑定超时或连接已断开，请检查机器人 WebSocket 订阅和网络后重试")
