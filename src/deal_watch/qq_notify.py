"""QQ notification transports. Binding and sending only use official HTTPS origins."""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field

import requests

from .notify import NotifyError, RelayDeliveryRejected, RetryableNotifyError, UnknownNotifyError

QQ_API = "https://api.bot.qq.com"
PUSHPLUS = "https://www.pushplus.plus"
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _request(method, url, *, headers=None, data=None, params=None, submitting=False):
    try:
        response = requests.request(method, url, headers=headers, json=data, params=params,
                                    timeout=(4, 6), allow_redirects=False)
    except requests.RequestException:
        if submitting:
            raise UnknownNotifyError("连接中断，消息是否已由平台接受未知") from None
        raise RetryableNotifyError("无法连接通知平台") from None
    if response.status_code == 429:
        raise RetryableNotifyError("平台限流，请稍后再试")
    if response.status_code >= 500:
        error = UnknownNotifyError if submitting else RetryableNotifyError
        raise error(f"平台暂时不可用（HTTP {response.status_code}）")
    if 300 <= response.status_code < 400:
        raise NotifyError("平台返回了意外的重定向")
    try:
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError
    except ValueError:
        error = UnknownNotifyError if submitting else NotifyError
        raise error("平台未返回可确认的业务结果") from None
    if response.status_code >= 400 and not result.get("code"):
        raise NotifyError(f"平台拒绝请求（HTTP {response.status_code}）")
    return result


def _check_qq(result):
    code = result.get("code")
    if code is None or type(code) is int and code == 0:
        return
    if type(code) is not int:
        raise UnknownNotifyError("QQ 业务状态无效")
    if code == 40034100:
        raise RetryableNotifyError("QQ 主动消息已达频率限制")
    hints = {40034105: "请检查主动消息权限", 40054013: "用户已拒收",
             40054016: "机器人已下线", 40054007: "消息过长"}
    raise NotifyError(f"QQ 返回 {code}，{hints.get(code, '请核对机器人权限和绑定关系')}")


@dataclass(frozen=True)
class QQTarget:
    kind: str
    scope: str
    alias: str
    target: str = ""
    app_id: str = ""
    secret: str = field(default="", repr=False)
    token: str = field(default="", repr=False)
    query_key: str = field(default="", repr=False)
    full_text: bool = False
    enabled: bool = True

    def __post_init__(self):
        if self.scope not in {"user", "group"} or not self.alias.strip():
            raise ValueError("请填写接收目标名称并选择会话类型")
        if self.kind == "qq":
            if not self.secret or not _ID.fullmatch(self.app_id) or not _ID.fullmatch(self.target):
                raise ValueError("请填写机器人凭据并完成会话绑定")
        elif self.kind == "pushplus":
            if not self.token.strip() or self.scope == "group" and not self.target.strip():
                raise ValueError("请填写推送凭据，发送到群时还需填写群配置编码")
        else:
            raise ValueError("未知通知渠道")

    @property
    def id(self):
        from .security import destination_id
        account = self.app_id if self.kind == "qq" else self.token
        return destination_id(self.kind, account, self.scope, self.target or "self")

    @property
    def delivery_id(self):
        from .security import destination_id
        # A queued summary must never be reformatted as full text after a settings edit.
        return destination_id(self.kind, self.id, "text-v1" if self.full_text else "summary-v1")


def validate_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError("通知接收端配置无效")
    targets, tasks = settings.get("targets", []), settings.get("tasks", {})
    if not isinstance(targets, list) or not isinstance(tasks, dict):
        raise ValueError("通知接收端或任务路由配置无效")
    for item in targets:
        target = QQTarget(**item)
        if type(target.full_text) is not bool or type(target.enabled) is not bool:
            raise ValueError("通知接收端开关无效")
    for task, values in tasks.items():
        if not isinstance(task, str) or not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError("任务路由列表无效")


def message_preview(payload, full_text):
    reference = str(payload.get("event_id", ""))[:12]
    title = "CueScope · 监测任务有新提醒"
    count = payload.get("digest_count")
    if type(count) is int and count > 0:
        title = f"CueScope · 免打扰期间共 {count} 条命中"
        return title, f"{title}\n打开活动记录查看详情。"
    text = f"{title}\n事件 {reference}"
    if full_text:
        text += f"\n来源：{payload.get('region_alias', '')}\n命中：{payload.get('keyword', '')}\n{payload.get('text', '')}"
    return title, text


class QQAccess:
    def __init__(self, app_id, secret):
        self.app_id = app_id
        self.secret = secret
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()

    def headers(self, before_request):
        with self._lock:
            if time.monotonic() >= self._expires:
                before_request()
                data = _request("POST", QQ_API + "/app/getAppAccessToken",
                                data={"appId": self.app_id, "clientSecret": self.secret})
                _check_qq(data)
                token = data.get("access_token")
                if not isinstance(token, str) or not token:
                    raise NotifyError("腾讯未签发访问令牌，请检查 AppID 和 AppSecret")
                self._token = token
                self._expires = time.monotonic() + max(0, int(data.get("expires_in", 0)) - 60)
            return {"Authorization": "QQBot " + self._token}


class QQClient:
    def __init__(self, target):
        self.target = target
        self.access = QQAccess(target.app_id, target.secret)

    def _parts(self, payload):
        text = message_preview(payload, self.target.full_text)[1]
        return [text[i:i + 800] for i in range(0, len(text), 800)]

    def part_count(self, payload):
        return len(self._parts(payload))

    def send(self, payload, *, now, start_part, before_request, on_part_sent):
        group = "groups" if self.target.scope == "group" else "users"
        url = f"{QQ_API}/v2/{group}/{self.target.target}/messages"
        for index, part in enumerate(self._parts(payload)[start_part:], start_part + 1):
            headers = self.access.headers(before_request)
            before_request()
            result = _request("POST", url, headers=headers, submitting=True,
                              data={"msg_type": 0, "content": part})
            _check_qq(result)
            if not isinstance(result.get("id"), str) or not result["id"]:
                raise UnknownNotifyError("QQ 未返回消息编号，接受状态未知")
            on_part_sent(index)


class PushPlusClient:
    def __init__(self, target):
        self.target = target
        self._key = ""
        self._expires = 0.0

    @staticmethod
    def _check(data):
        code = data.get("code")
        if type(code) is int and code == 200:
            return
        if code == 500:
            raise RetryableNotifyError("PushPlus 服务暂不可用")
        hints = {900: "账户或频率受限", 903: "令牌无效", 905: "账户未实名",
                 401: "查询权限未开通", 403: "查询 IP 未授权"}
        safe_code = code if type(code) is int else "未知"
        raise NotifyError(f"PushPlus 返回 {safe_code}，{hints.get(safe_code, '请检查渠道设置')}")

    def send_tracked(self, payload, *, now, before_request, receipt, on_receipt):
        if not receipt:
            title, content = message_preview(payload, self.target.full_text)
            reference = f"{payload['event_id']}|{self.target.id}"
            body = {"token": self.target.token, "channel": "qq", "template": "txt",
                    "title": title, "content": content, "pushId": hashlib.sha256(reference.encode()).hexdigest()[:32]}
            if self.target.target:
                body["option"] = self.target.target
            before_request()
            result = _request("POST", PUSHPLUS + "/send", data=body, submitting=True)
            self._check(result)
            receipt = result.get("data")
            if not isinstance(receipt, str) or not _ID.fullmatch(receipt):
                raise UnknownNotifyError("中转未返回有效流水号")
            on_receipt(receipt)
            return "forwarded"
        if not self.target.query_key:
            raise NotifyError("中转已受理；未配置开放接口查询密钥，最终状态未确认")
        if time.monotonic() >= self._expires:
            before_request()
            result = _request("POST", PUSHPLUS + "/api/common/openApi/getAccessKey",
                              data={"token": self.target.token, "secretKey": self.target.query_key})
            self._check(result)
            values = result.get("data")
            if not isinstance(values, dict) or not values.get("accessKey"):
                raise NotifyError("中转查询授权失败")
            self._key = values["accessKey"]
            self._expires = time.monotonic() + max(0, int(values.get("expiresIn", 0)) - 60)
        before_request()
        result = _request("GET", PUSHPLUS + "/api/open/message/sendMessageResult",
                          headers={"access-key": self._key}, params={"shortCode": receipt})
        self._check(result)
        data = result.get("data")
        status = data.get("status") if isinstance(data, dict) else None
        if type(status) is int and status == 2:
            return "sent"
        if type(status) is int and status == 3:
            raise RelayDeliveryRejected("中转报告投递失败")
        if type(status) is int and status in {0, 1}:
            return "forwarded"
        raise UnknownNotifyError("中转返回未知的投递状态")
