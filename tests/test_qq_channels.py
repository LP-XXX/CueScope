import time
from dataclasses import asdict, replace
from unittest.mock import patch

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtNetwork import QHostAddress
from PySide6.QtWebSockets import QWebSocketServer
from PySide6.QtWidgets import QApplication

from deal_watch import qq_notify
from deal_watch.delivery_routes import DeliveryRoutes
from deal_watch.notify import NotifyError, RelayDeliveryRejected, UnknownNotifyError
from deal_watch.qq_notify import PushPlusClient, QQClient, QQTarget, validate_settings
from deal_watch.storage import Storage
from deal_watch.ui.qq_binding import BindingThread, matching_recipient


def test_only_matching_binding_event_reveals_recipient():
    challenge = "绑定 EXAMPLE"
    group = {"op": 0, "t": "GROUP_AT_MESSAGE_CREATE",
             "d": {"group_openid": "sample-team", "content": "<@123> " + challenge}}
    assert matching_recipient(group, "group", challenge) == "sample-team"
    assert not matching_recipient(group, "user", challenge)
    assert not matching_recipient(group, "group", "expired")
    private = {"op": 0, "t": "C2C_MESSAGE_CREATE",
               "d": {"author": {"user_openid": "test-user"}, "content": challenge}}
    assert matching_recipient(private, "user", challenge) == "test-user"


def test_qq_gateway_handshake_and_single_use_binding_on_loopback():
    import json

    app = QApplication.instance() or QApplication([])
    server = QWebSocketServer("synthetic-qq", QWebSocketServer.SslMode.NonSecureMode)
    assert server.listen(QHostAddress.SpecialAddress.LocalHost, 0)
    url = f"ws://127.0.0.1:{server.serverPort()}"

    class LocalBinding(BindingThread):
        def run(self):
            self._listen(url, "QQBot sample")

    peers = []
    identified = []
    def connected():
        peer = server.nextPendingConnection()
        peers.append(peer)

        def respond(raw):
            packet = json.loads(raw)
            if packet["op"] == 2:
                identified.append(packet["d"])
                peer.sendTextMessage(json.dumps({"op": 0, "t": "READY", "s": 1, "d": {}}))
        peer.textMessageReceived.connect(respond)
        peer.sendTextMessage(json.dumps({"op": 10, "d": {"heartbeat_interval": 45000}}))

    server.newConnection.connect(connected)
    loop = QEventLoop()
    worker = LocalBinding("sample", "unused", "user")
    result = []

    def simulate_pairing(challenge):
        for message in ("unrelated sample", challenge):
            peers[0].sendTextMessage(json.dumps({"op": 0, "t": "C2C_MESSAGE_CREATE",
                "d": {"author": {"user_openid": "sample-user"}, "content": message}}))

    worker.ready.connect(simulate_pairing)
    worker.matched.connect(result.append)
    worker.finished.connect(loop.quit)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(5000)
    worker.start()
    try:
        loop.exec()
    finally:
        worker.cancelled.set()
        assert worker.wait(2500)
        server.close()
        timer.stop()
        app.processEvents()
    assert result == ["sample-user"]
    assert identified[0]["token"] == "QQBot sample"


def test_qq_direct_checks_each_network_request_and_reuses_token(monkeypatch):
    target = QQTarget("qq", "group", "示例", "test-group", "example", "dummy")
    client = QQClient(target)
    calls = []
    gates = []

    def request(method, url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/getAppAccessToken"):
            return {"access_token": "example", "expires_in": 600}
        return {"id": "synthesized-id"}

    monkeypatch.setattr(qq_notify, "_request", request)
    payload = {"text": "private-example", "keyword": "private-word", "event_id": "001"}
    parts = []
    for _ in range(2):
        client.send(payload, now=0, start_part=0, before_request=lambda: gates.append(1), on_part_sent=parts.append)
    assert len(calls) == len(gates) == 3
    assert calls[1][0].endswith("/v2/groups/test-group/messages")
    assert all("private" not in str(x) for x in calls)
    assert parts == [1, 1]
    assert replace(target, secret="rotated").id == target.id
    assert replace(target, full_text=True).delivery_id != target.delivery_id
    assert replace(target, target="other").id != target.id


def test_malformed_task_routes_cannot_fall_back_to_global_defaults():
    with pytest.raises(ValueError):
        validate_settings({"tasks": {"private-task": "all"}})


def test_policy_changes_cancel_old_routes_without_disclosing_stored_text(tmp_path):
    from deal_watch.models import MatchEvent

    target = QQTarget("pushplus", "user", "个人", token="synthetic")
    store = Storage(tmp_path / "events.db")
    group = DeliveryRoutes(store, tmp_path, 300, None)
    try:
        with patch("deal_watch.delivery_routes.NotificationDispatcher.start"):
            group.configure("auto", ("", ""), {"targets": [asdict(target)]})
            first = group.routes("task")[0]
            event = MatchEvent("task", "sample", "match", "private-example", "key", time.time())
            store.record_match(event)
            group.configure("auto", ("", ""), {"targets": [asdict(replace(target, full_text=True))]})
            second = group.routes("task")[0]
            deadline = time.monotonic() + 3
            while not group.can_send(second) and time.monotonic() < deadline:
                time.sleep(.01)
            assert second != first and group.can_send(second) and not group.can_send(first)
            assert store.list_deliveries(event.id)[0]["state"] == "revoked"
            assert not store.for_destination(second).due_jobs()
    finally:
        group.stop()
        store.close()


def test_qq_requires_message_receipt_and_reports_user_refusal(monkeypatch):
    target = QQTarget("qq", "user", "个人", "test", "app", "example")
    client = QQClient(target)
    client.access.headers = lambda before_request: {}
    args = dict(now=0, start_part=0, before_request=lambda: None, on_part_sent=lambda _: pytest.fail())
    monkeypatch.setattr(qq_notify, "_request", lambda *a, **k: {})
    with pytest.raises(UnknownNotifyError):
        client.send({}, **args)
    monkeypatch.setattr(qq_notify, "_request", lambda *a, **k: {"code": 40054013, "message": "private"})
    with pytest.raises(NotifyError, match="用户已拒收"):
        client.send({}, **args)


@pytest.mark.parametrize("status,expected", [(0, "forwarded"), (1, "forwarded"), (2, "sent"), (3, "rejected")])
def test_pushplus_queries_receipt_without_resubmitting(monkeypatch, status, expected):
    target = QQTarget("pushplus", "user", "手机", token="dummy", query_key="dummy-key")
    client = PushPlusClient(target)
    calls = []
    receipts = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/send"):
            return {"code": 200, "data": "reference123"}
        if url.endswith("/getAccessKey"):
            return {"code": 200, "data": {"accessKey": "example", "expiresIn": 7200}}
        return {"code": 200, "data": {"status": status}}

    monkeypatch.setattr(qq_notify, "_request", request)
    args = dict(payload={"event_id": "event1", "text": "secret-example"}, now=0,
                before_request=lambda: None, on_receipt=receipts.append)
    assert client.send_tracked(**args, receipt="") == "forwarded"
    assert receipts == ["reference123"] and "secret-example" not in str(calls[0][2]["data"])
    if expected == "rejected":
        with pytest.raises(RelayDeliveryRejected):
            client.send_tracked(**args, receipt=receipts[0])
    else:
        assert client.send_tracked(**args, receipt=receipts[0]) == expected
    assert [m for m, _, _ in calls] == ["POST", "POST", "GET"]
    assert calls[-1][2]["params"] == {"shortCode": "reference123"}
