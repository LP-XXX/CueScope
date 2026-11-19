"""真实飞书试发的显式入口（由用户亲自执行，工具自身绝不自动发送）。

用途：从 Windows DPAPI 凭据存储读取飞书机器人 webhook/签名密钥，校验地址后
发送**一条**构造的试发消息，只回传业务结果、耗时与事件编号。

约束：必须显式给出 ``--confirm-send`` 才会发送（缺失时打印说明并以退出码 2 退出）。
不修改用户 config.json，不写 feedback.db，不发送任何微信消息。绝不打印 webhook、
签名密钥或响应正文；报告里 webhook 只记录 host 与路径末 4 位掩码。
``phone_delivery_confirmed_by_user`` 固定写 ``null``，由用户自行确认手机是否收到，
工具不做推断。

退出码：发送成功 0；业务错误/凭据问题 1；缺少 ``--confirm-send`` 2。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
for _entry in (str(ROOT / "src"), str(ROOT), str(ROOT / "tools")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import real_env  # noqa: E402  （先补齐 sys.path 才能导入共享层）


def default_text() -> str:
    """试发正文：默认自带实际版本号，便于在飞书端确认是哪一版发出的。"""
    return f"DealWatch {real_env.app_version()} 真实飞书试发"


def _masked_webhook(webhook: str) -> dict[str, str]:
    """只保留 host 与路径末 4 位掩码，不泄露 token。"""
    parsed = urlsplit(webhook)
    path = parsed.path or ""
    tail = path[-4:] if len(path) >= 4 else ""
    return {
        "host": parsed.hostname or "",
        "scheme": parsed.scheme,
        "path_masked": f"…{tail}" if tail else "",
    }


def _build_payload(text: str) -> dict:
    """构造一条试发 payload：不包含任何聊天原文。"""
    from uuid import uuid4

    return {
        "event_id": uuid4().hex,
        "region_alias": "试发",
        "keyword": "试发",
        "observed_at": time.time(),
        "text": text,
        "boundary_uncertain": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "真实飞书试发的显式入口：必须带 --confirm-send 才会发送。"
            "不会发送微信消息、不会修改用户配置；不打印 webhook / 密钥 / 响应正文。"
        )
    )
    parser.add_argument(
        "--confirm-send",
        action="store_true",
        help="确认真的向飞书机器人发送一条试发消息（必需）",
    )
    parser.add_argument("--text", default=default_text(), help="试发正文（默认含版本号）")
    parser.add_argument(
        "--output",
        type=Path,
        default=real_env.VALIDATION_DIR / f"feishu-real-send-{real_env.app_version()}.json",
    )
    args = parser.parse_args()

    if not args.confirm_send:
        print("这是真实飞书试发工具的显式入口，未发送任何消息。")
        print("")
        print("本工具会：")
        print("  1. 从 Windows DPAPI 凭据存储读取飞书 webhook 与签名密钥；")
        print("  2. 校验 webhook 必须是飞书 HTTPS 机器人地址（或本机模拟地址）；")
        print("  3. 向该机器人发送**一条**试发消息，并只回报业务结果、耗时与事件编号。")
        print("它不会发送微信消息、不会修改用户配置，也不会打印 webhook 或密钥。")
        print("")
        print("确认要真的发送时，请显式加上 --confirm-send 重新运行：")
        print(f"  python {Path(__file__).name} --confirm-send")
        return 2

    real_env.require_windows()
    from deal_watch.config import default_data_dir, validate_webhook_url
    from deal_watch.notify import FeishuClient, NotifyError
    from deal_watch.security import CredentialStore

    report: dict = {
        "kind": "feishu_real_send",
        "version": real_env.app_version(),
        "started_at": time.time(),
        "confirmed_by_flag": True,
        "sent": False,
        "business_result": "not_attempted",
        "error": "",
        "elapsed_ms": None,
        "event_id": "",
        "webhook": {},
        "text_length": len(args.text),
        "text_sha256": real_env.text_digest(args.text),
        "phone_delivery_confirmed_by_user": None,
        "read_only_notes": ["未发送任何微信消息", "未修改用户 config.json / feedback.db"],
    }
    try:
        webhook, secret = CredentialStore(default_data_dir()).load()
    except Exception as error:
        report["error"] = f"读取凭据失败：{type(error).__name__}"
        print("读取 DPAPI 凭据失败，未发送任何消息。")
        real_env.write_report(args.output, report)
        return 1
    if not webhook:
        report["error"] = "未配置飞书 webhook"
        print("未在 DPAPI 凭据存储中找到飞书 webhook，未发送任何消息。")
        real_env.write_report(args.output, report)
        return 1
    report["webhook"] = _masked_webhook(webhook)
    try:
        validate_webhook_url(webhook)
    except ValueError:
        report["error"] = "webhook 地址不合法"
        print("已保存的 webhook 不是合法飞书机器人地址，未发送任何消息。")
        real_env.write_report(args.output, report)
        return 1

    payload = _build_payload(args.text)
    event_id = str(payload["event_id"])
    client = FeishuClient(webhook, secret, timeout=8.0)
    started = time.perf_counter()
    try:
        client.send(payload, now=time.time())
    except NotifyError as error:
        elapsed_ms = (time.perf_counter() - started) * 1000
        # NotifyError 的 message 已由业务层脱敏，不含 webhook/密钥/正文。
        report.update(
            {
                "sent": False,
                "business_result": "error",
                "error": str(error)[:200],
                "elapsed_ms": round(elapsed_ms, 1),
                "event_id": event_id,
            }
        )
        print(f"发送失败：{error}")
        print(f"耗时：{report['elapsed_ms']}ms；事件编号：{event_id}")
        report["finished_at"] = time.time()
        real_env.write_report(args.output, report)
        return 1
    except Exception as error:
        elapsed_ms = (time.perf_counter() - started) * 1000
        report.update(
            {
                "sent": False,
                "business_result": "unknown",
                "error": f"未预期异常：{type(error).__name__}",
                "elapsed_ms": round(elapsed_ms, 1),
                "event_id": event_id,
            }
        )
        print(f"发送结果未知：{type(error).__name__}（不打印正文，请到手机/飞书核对）")
        print(f"耗时：{report['elapsed_ms']}ms；事件编号：{event_id}")
        report["finished_at"] = time.time()
        real_env.write_report(args.output, report)
        return 1

    elapsed_ms = (time.perf_counter() - started) * 1000
    report.update(
        {
            "sent": True,
            "business_result": "success",
            "error": "",
            "elapsed_ms": round(elapsed_ms, 1),
            "event_id": event_id,
        }
    )
    print("飞书业务结果：成功（code=0）")
    print(f"耗时：{report['elapsed_ms']}ms")
    print(f"事件编号：{event_id}")
    print("HTTP 成功与平台接受都不等于手机送达；请自行在手机确认，并手工填写")
    print("报告中的 phone_delivery_confirmed_by_user（工具不会自动推断）。")
    report["finished_at"] = time.time()
    real_env.write_report(args.output, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
