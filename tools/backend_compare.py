"""WGC 与 PrintWindow 的同窗高 DPI 对照（只读验收工具）。

用途：对同一个真实微信窗口分别用 WGC 与 PrintWindow 抓帧，给出“PrintWindow 是否
能替代 WGC”的可核验证据：帧尺寸、DPI 感知级别、客户区/窗口矩形差值、空白帧判据、
两侧 OCR 的字符集合 Jaccard、底部 20% 区域字符是否都在。

约束（只读）：不发送微信消息、不点击/滚动/输入、不移动或缩放被监控窗口、
不写用户 config.json、不写 feedback.db、不保存聊天截图。报告只放计数、耗时、
长度、sha256 摘要与状态；控制台默认把中文替换为 ●，只有显式 ``--show-text``
才打印识别原文（报告仍不含原文）。

退出码：两侧都拿不到帧 → 1；其余情况（含“PrintWindow 空白帧”这个预期结论）→ 0。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _entry in (str(ROOT / "src"), str(ROOT), str(ROOT / "tools")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import real_env  # noqa: E402  （先补齐 sys.path 才能导入共享层）

BOTTOM_BAND_RATIO = 0.8
FRAME_SIZE_TOLERANCE = 2
COMPATIBLE_JACCARD = 0.5
COMPATIBLE_BOTTOM_RATIO = 0.5


def _ocr_metrics(image, ocr, show_text: bool) -> dict:
    """对一帧跑一次真实 OCR，只回传计数、摘要与长度（原文不进报告）。"""
    from deal_watch.models import FrameSample, Rect
    from deal_watch.normalize import char_rule_normalize

    frame = FrameSample(
        "backend-compare",
        1,
        time.time(),
        image,
        Rect(0, 0, image.shape[1], image.shape[0]),
    )
    started = time.perf_counter()
    observation = ocr.recognize(frame)
    elapsed_ms = (time.perf_counter() - started) * 1000
    texts = [block.text for block in observation.blocks]
    normalized = char_rule_normalize("".join(texts))
    band_top = int(round(image.shape[0] * BOTTOM_BAND_RATIO))
    bottom_texts = [
        block.text for block in observation.blocks if block.box.center[1] >= band_top
    ]
    bottom = char_rule_normalize("".join(bottom_texts))
    if show_text:
        print("  识别原文（--show-text）：")
        for text in texts:
            print("    " + text)
    else:
        print("  脱敏预览：" + real_env.redact("\n".join(texts)[:160]))
    return {
        "blocks": len(observation.blocks),
        "chars": len(normalized),
        "char_sha256": real_env.text_digest(normalized),
        "bottom_band_top": band_top,
        "bottom_blocks": len(bottom_texts),
        "bottom_chars": len(bottom),
        "bottom_char_sha256": real_env.text_digest(bottom),
        "uncovered_boxes": len(observation.uncovered_boxes),
        "ocr_ms": round(elapsed_ms, 1),
        "char_set": set(normalized),
        "bottom_char_set": set(bottom),
    }


def _public(metrics: dict) -> dict:
    """去掉集合，只留可 JSON 序列化的计数与摘要。"""
    return {key: value for key, value in metrics.items() if not key.endswith("_set")}


def _pixel_digest(image) -> str:
    """整帧像素 sha256，用于确认两次采集看到的是同一画面附近。"""
    import hashlib

    return hashlib.sha256(image.tobytes()).hexdigest()


def _build_ocr():
    """构建一次真实 OCR 并做一次热身，避免把模型加载时间算进单侧耗时。"""
    import numpy as np

    from deal_watch.models import FrameSample, Rect
    from deal_watch.ocr import LocalOcr

    ocr = LocalOcr(real_env.model_config(), ROOT)
    warmup = np.full((160, 240, 3), 250, dtype=np.uint8)
    warmup[70:90, 40:200] = 30
    started = time.perf_counter()
    ocr.recognize(FrameSample("warmup", 1, time.time(), warmup, Rect(0, 0, 240, 160)))
    print(f"OCR 热身完成（模型加载与首帧）：{(time.perf_counter() - started) * 1000:.0f}ms")
    return ocr


def _capture_side(
    hwnd: int,
    backend: str,
    ocr,
    show_text: bool,
    capture_timeout: float = real_env.PRODUCTION_CAPTURE_TIMEOUT,
) -> tuple[dict, dict]:
    """抓一侧证据；返回（报告用字段，内部 OCR 指标）。"""
    from deal_watch.window_capture import WindowCaptureClient, WindowCaptureError

    side: dict = {
        "backend": backend,
        "ok": False,
        "error": "",
        "error_code": "",
        "frame": None,
        "frame_valid": False,
        "blank": None,
        "dpi_awareness": "",
        "dpi_request": None,
        "dpi_matches_expected": False,
        "window_rect": None,
        "client_rect": None,
        "uniform_border": None,
        "frame_pixels_sha256": "",
        "ocr_ms": None,
    }
    payload = real_env.worker_payload(hwnd, backend, timeout=capture_timeout)
    side["dpi_awareness"] = str(payload.get("dpi_awareness", ""))
    side["dpi_request"] = payload.get("dpi_request")
    side["dpi_matches_expected"] = side["dpi_awareness"] == real_env.DPI_EXPECTED
    side["worker_ok"] = bool(payload.get("ok"))
    side["worker_error"] = str(payload.get("error", ""))[:300]
    # worker 新版 payload 自带边框宽度与内部墨水占比，作为“空白帧”判定的旁证。
    side["worker_frame_stats"] = payload.get("frame_stats")
    geometry = real_env.window_geometry(hwnd)
    side["window_rect"] = geometry.get("window_rect")
    side["client_rect"] = geometry.get("client_rect")
    side["worker_window_rect"] = payload.get("window_rect")
    side["worker_client_rect"] = payload.get("client_rect")

    metrics: dict = {}
    client = WindowCaptureClient(timeout=float(capture_timeout))
    image = None
    try:
        frame = client.capture(hwnd, backend)
        image = frame.image
        side["ok"] = True
        side["frame"] = {"width": int(frame.width), "height": int(frame.height)}
    except WindowCaptureError as error:
        side["error"] = f"{error.code}: {error}"
        side["error_code"] = str(error.code)
    except Exception as error:  # 采集组件返回异常内容时按失败记录，不让工具崩
        side["error"] = f"{type(error).__name__}: {error}"[:300]
    if side["error"] and side["worker_error"]:
        side["error"] = side["worker_error"][:300]

    if image is not None:
        side["blank"] = real_env.is_blank_frame(image)
        side["uniform_border"] = real_env.uniform_border(image)
        side["frame_pixels_sha256"] = _pixel_digest(image)
        metrics = _ocr_metrics(image, ocr, show_text)
        side["ocr_ms"] = metrics["ocr_ms"]
        side["frame_valid"] = bool(side["ok"] and not side["blank"])
    return side, metrics


def _size_matches(frame: dict | None, rect: dict | None) -> bool:
    if not frame or not rect:
        return False
    return (
        abs(int(frame["width"]) - int(rect["width"])) <= FRAME_SIZE_TOLERANCE
        and abs(int(frame["height"]) - int(rect["height"])) <= FRAME_SIZE_TOLERANCE
    )


def _client_plus_border(side: dict) -> dict | None:
    """客户区 + 实测同色边框宽度；WGC 帧通常正好等于“客户区 + 2px 描边”。"""
    client = side.get("client_rect")
    border = side.get("uniform_border")
    if not client or not border:
        return None
    return {
        "width": int(client["width"]) + int(border["left"]) + int(border["right"]),
        "height": int(client["height"]) + int(border["top"]) + int(border["bottom"]),
    }


def _delta(frame: dict | None, rect: dict | None) -> dict | None:
    if not frame or not rect:
        return None
    return {
        "width": int(frame["width"]) - int(rect["width"]),
        "height": int(frame["height"]) - int(rect["height"]),
    }


def build_parser() -> argparse.ArgumentParser:
    """命令行参数；抽成独立函数便于测试默认值而不触碰真实桌面。"""
    parser = argparse.ArgumentParser(
        description=(
            "WGC 与 PrintWindow 的同窗高 DPI 对照。只读：不发送微信消息、"
            "不点击/滚动/输入、不写用户配置、不保存聊天截图。"
        )
    )
    parser.add_argument(
        "--hwnd", type=lambda value: int(value, 0), help="目标窗口句柄，例如 0x36e07da"
    )
    parser.add_argument(
        "--title-contains", help="按标题子串在 DISPLAY1 内选唯一微信窗口"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=real_env.VALIDATION_DIR / f"backend-compare-{real_env.app_version()}.json",
    )
    parser.add_argument(
        "--capture-timeout",
        type=float,
        default=real_env.PRODUCTION_CAPTURE_TIMEOUT,
        help=(
            "单次窗口采集超时秒数（默认 5.0，与生产 WindowCaptureClient 一致）；"
            "放宽后报告会标记 diagnostic_params"
        ),
    )
    parser.add_argument(
        "--show-text",
        action="store_true",
        help="在控制台打印识别原文（报告仍只写计数与 sha256 摘要）",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    real_env.require_windows()
    real_env.enable_dpi_awareness()
    target = real_env.select_target(args.hwnd, args.title_contains)
    real_env.require_weixin(target)
    real_env.assert_display1(target.hwnd)

    report: dict = {
        "kind": "backend_compare_wgc_vs_printwindow",
        "version": real_env.app_version(),
        "started_at": time.time(),
        "hwnd": f"0x{target.hwnd:x}",
        "title_sha256": real_env.text_digest(target.title),
        "monitor": real_env.monitor_device(target.hwnd),
        "visible_rect": {
            "x": target.rect.x,
            "y": target.rect.y,
            "width": target.rect.width,
            "height": target.rect.height,
        },
        "dpi_expected": real_env.DPI_EXPECTED,
        "read_only": True,
        "sends_wechat_message": False,
        "writes_user_config": False,
        "sides": {},
        "conclusion": {},
    }
    # 采集参数写进报告：报告自己说明这是不是生产参数。
    report.update(real_env.production_param_flags(args.capture_timeout))
    print(f"目标窗口 {report['hwnd']} 显示器 {report['monitor']} 可见 {target.rect}")
    print(f"标题摘要 {report['title_sha256'][:16]}…（不打印标题明文）")

    raw: dict[str, dict] = {}
    ocr = _build_ocr()
    for backend in ("wgc", "print-window"):
        print(f"[{backend}] 抓帧 …")
        side, metrics = _capture_side(
            target.hwnd, backend, ocr, bool(args.show_text), float(args.capture_timeout)
        )
        report["sides"][backend] = side
        raw[backend] = metrics
        if side["error"]:
            print(f"  error: {side['error']}")
        if side["frame"]:
            print(f"  frame: {side['frame']['width']}x{side['frame']['height']}")
            print(f"  uniform_border: {side['uniform_border']}")
        print(f"  dpi_awareness: {side['dpi_awareness']}（期望 {real_env.DPI_EXPECTED}）")
        if side["blank"] is not None:
            print(f"  blank: {side['blank']}")
        if metrics:
            print(
                f"  ocr: blocks={metrics['blocks']} chars={metrics['chars']} "
                f"bottom_chars={metrics['bottom_chars']} "
                f"uncovered={metrics['uncovered_boxes']} ms={metrics['ocr_ms']}"
            )

    wgc = report["sides"]["wgc"]
    printwindow = report["sides"]["print-window"]
    wgc_metrics = raw.get("wgc", {})
    pw_metrics = raw.get("print-window", {})
    text_jaccard = (
        real_env.jaccard(wgc_metrics.get("char_set", set()), pw_metrics.get("char_set", set()))
        if wgc_metrics and pw_metrics
        else 0.0
    )
    bottom_jaccard = (
        real_env.jaccard(
            wgc_metrics.get("bottom_char_set", set()),
            pw_metrics.get("bottom_char_set", set()),
        )
        if wgc_metrics and pw_metrics
        else 0.0
    )
    wgc_bottom_chars = wgc_metrics.get("bottom_chars", 0)
    pw_bottom_chars = pw_metrics.get("bottom_chars", 0)
    if not wgc_bottom_chars:
        # WGC 底部也没有文字时不做“底部缺失”判定，避免把空聊天误判为失败。
        bottom_ok = True
    else:
        bottom_ok = (
            bottom_jaccard >= COMPATIBLE_JACCARD
            and pw_bottom_chars >= wgc_bottom_chars * COMPATIBLE_BOTTOM_RATIO
        )
    printwindow_compatible = bool(
        printwindow["frame_valid"]
        and printwindow["blank"] is False
        and pw_metrics
        and text_jaccard >= COMPATIBLE_JACCARD
        and bottom_ok
    )
    report["conclusion"] = {
        "printwindow_compatible": printwindow_compatible,
        "printwindow_ok": bool(printwindow["ok"]),
        "printwindow_error": str(printwindow["error"])[:300],
        "printwindow_blank_frame": printwindow["blank"],
        "wgc_ok": bool(wgc["ok"]),
        "wgc_error": str(wgc["error"])[:300],
        "text_jaccard": round(text_jaccard, 4),
        "bottom_jaccard": round(bottom_jaccard, 4),
        "text_chars": {
            "wgc": wgc_metrics.get("chars", 0),
            "printwindow": pw_metrics.get("chars", 0),
        },
        "text_blocks": {
            "wgc": wgc_metrics.get("blocks", 0),
            "printwindow": pw_metrics.get("blocks", 0),
        },
        "bottom_chars": {"wgc": wgc_bottom_chars, "printwindow": pw_bottom_chars},
        "wgc_frame_matches_client_area": _size_matches(
            wgc["frame"], wgc["client_rect"]
        ),
        "wgc_client_plus_border": _client_plus_border(wgc),
        "wgc_frame_matches_client_plus_border": _size_matches(
            wgc["frame"], _client_plus_border(wgc)
        ),
        "printwindow_frame_matches_window_rect": _size_matches(
            printwindow["frame"], printwindow["window_rect"]
        ),
        "dpi_awareness": {
            "wgc": wgc["dpi_awareness"],
            "printwindow": printwindow["dpi_awareness"],
        },
        "dpi_expected": real_env.DPI_EXPECTED,
        "size_delta": {
            "wgc_vs_client": _delta(wgc["frame"], wgc["client_rect"]),
            "printwindow_vs_window": _delta(printwindow["frame"], printwindow["window_rect"]),
        },
    }

    conclusion = report["conclusion"]
    print("")
    print("结论：")
    print(f"  printwindow_compatible = {conclusion['printwindow_compatible']}")
    if conclusion["printwindow_error"]:
        print(f"  printwindow_error      = {conclusion['printwindow_error']}")
    print(f"  text_jaccard           = {conclusion['text_jaccard']}")
    print(f"  bottom_jaccard         = {conclusion['bottom_jaccard']}")
    print(
        "  wgc_frame_matches_client_area         = "
        f"{conclusion['wgc_frame_matches_client_area']} "
        f"（差值 {conclusion['size_delta']['wgc_vs_client']}；"
        f"客户区+边框 {conclusion['wgc_client_plus_border']} → "
        f"{conclusion['wgc_frame_matches_client_plus_border']}）"
    )
    print(
        "  printwindow_frame_matches_window_rect = "
        f"{conclusion['printwindow_frame_matches_window_rect']} "
        f"（差值 {conclusion['size_delta']['printwindow_vs_window']}）"
    )
    print(
        f"  dpi_awareness          = {conclusion['dpi_awareness']}"
        f"（期望 {conclusion['dpi_expected']}）"
    )

    report["finished_at"] = time.time()
    report["read_only_notes"] = [
        "未发送任何微信消息",
        "未点击、滚动或输入",
        "未移动或缩放被监控窗口",
        "未写入用户 config.json / feedback.db",
        "未保存聊天截图，报告只含计数与 sha256 摘要",
    ]
    real_env.write_report(args.output, report)
    if not wgc["frame"] and not printwindow["frame"]:
        print("两侧都未取到采集帧，判定为采集失败（退出码 1）。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
