"""真实微信窗口的只读采集 + 识别探针（证明当前浅色版式可识别）。

默认**只跑用户已配置并确认的区域**：从 config.json 只读加载，逐个用配置里记录的
HWND 与 ROI（采集帧相对坐标）抓帧 → 真实本地 OCR → 用 ``find_bubbles`` /
``assign_messages`` 统计面板、气泡、文字块、未覆盖区域与关键词命中数量。
不枚举其他微信窗口、不自动生成 ROI、不视作用户确认、不写用户配置。

``--source probe --allow-probe-roi`` 是显式的诊断旁路（给还没配置的机器看版式），
报告会标记 ``roi_source="probe_generated"``、``user_confirmed_roi=False``。

约束（只读）：不发送微信消息、不点击/滚动/输入、不移动或缩放被监控窗口、
不写用户 config.json（关键词只读副本）、不写 feedback.db、不保存聊天截图。
报告只放 ``hwnd``（十六进制）、标题 sha256、帧/ROI 尺寸、耗时与计数；控制台默认
把中文替换为 ●，只有显式 ``--show-text`` 才打印识别原文。

退出码：未发现可采窗口或全部采集失败 → 1；参数不合规 → 2；否则 0。
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

MAX_WINDOWS = 4
SOURCE_CONFIG = "config"
SOURCE_PROBE = "probe"


def probe_region(width: int, height: int):
    """探针 ROI：顶部内缩 8% 高、底部内缩 32% 高、左右各内缩 2% 宽。

    这是探针 ROI，不是用户校准的 ROI，不写入配置、不改用户 config.json；
    仅在显式 ``--source probe`` 时用于真机验收时避开标题栏与底部输入框。
    """
    return real_env.probe_region(width, height)


def configured_regions(config, windows=None) -> tuple[list, dict[str, list[str]]]:
    """挑出用户已配置并确认的区域；不枚举窗口、不生成 ROI。"""
    from deal_watch.readiness import region_readiness_problems

    accepted: list = []
    rejected: dict[str, list[str]] = {}
    for region in config.regions:
        problems: list[str] = []
        if not region.enabled:
            problems.append("已在配置中停用")
        for item in region_readiness_problems(region, windows):
            if item not in problems:
                problems.append(item)
        if problems:
            rejected[region.alias or region.id] = problems
            continue
        accepted.append(region)
    return accepted, rejected


def _warm_up(ocr) -> None:
    """用一张合成图预热模型，避免把模型加载时间算进单窗 ocr_ms。"""
    import numpy as np

    from deal_watch.models import FrameSample, Rect

    warmup = np.full((160, 240, 3), 250, dtype=np.uint8)
    warmup[70:90, 40:200] = 30
    started = time.perf_counter()
    ocr.recognize(FrameSample("warmup", 1, time.time(), warmup, Rect(0, 0, 240, 160)))
    print(f"OCR 热身完成（模型加载与首帧）：{(time.perf_counter() - started) * 1000:.0f}ms")


def _keyword_hits(messages, keywords: list[str]) -> tuple[int, int]:
    """返回（命中消息数，命中的不同关键词数）；只计数，不返回文本。"""
    from deal_watch.normalize import contains_all_chars

    hit_messages = 0
    matched: set[str] = set()
    for message in messages:
        hits = [keyword for keyword in keywords if contains_all_chars(message.text, keyword)]
        if hits:
            hit_messages += 1
            matched.update(hits)
    return hit_messages, len(matched)


def _probe_window(
    target,
    ocr,
    keywords: list[str],
    show_text: bool,
    capture_timeout: float = real_env.PRODUCTION_CAPTURE_TIMEOUT,
    roi_override=None,
) -> dict:
    """对单个窗口做一次只读采集 + 识别，返回计数字典。

    ``roi_override`` 非空时用**用户配置里记录的 ROI**（帧内坐标）；为空才用探针
    ROI。两条路径都在报告里写明来源，避免把探针 ROI 当成用户确认的区域。
    """
    from deal_watch.capture import CaptureError
    from deal_watch.models import FrameSample
    from deal_watch.ocr import assign_messages, find_bubbles
    from deal_watch.tracking import blocks_to_messages
    from deal_watch.window_capture import WindowCaptureClient, WindowCaptureError

    record: dict = {
        "hwnd": f"0x{target.hwnd:x}",
        "title_sha256": real_env.text_digest(target.title),
        "window_rect": {
            "x": target.rect.x,
            "y": target.rect.y,
            "width": target.rect.width,
            "height": target.rect.height,
        },
        "roi_source": "user_config" if roi_override is not None else "probe_generated",
        "capture_ok": False,
        "error": "",
        "frame": None,
        "roi": None,
        "dpi_awareness": "",
        "blank": None,
        "ocr_ms": 0.0,
        "bubbles_ms": 0.0,
        "blocks": 0,
        "chars": 0,
        "char_sha256": "",
        "panels": 0,
        "certain": 0,
        "uncertain": 0,
        "uncovered": 0,
        "messages": 0,
        "keyword_hit_messages": 0,
        "keyword_hit_total": 0,
    }
    payload = real_env.worker_payload(target.hwnd, "wgc", timeout=capture_timeout)
    record["dpi_awareness"] = str(payload.get("dpi_awareness", ""))
    if not payload.get("ok"):
        record["error"] = str(payload.get("error", ""))[:300]

    client = WindowCaptureClient(timeout=float(capture_timeout))
    try:
        frame = client.capture(target.hwnd, "wgc")
    except (WindowCaptureError, CaptureError) as error:
        record["error"] = record["error"] or f"{type(error).__name__}: {error}"[:300]
        return record
    except Exception as error:
        record["error"] = f"{type(error).__name__}: {error}"[:300]
        return record

    record["capture_ok"] = True
    record["frame"] = {"width": int(frame.width), "height": int(frame.height)}
    record["blank"] = real_env.is_blank_frame(frame.image)
    if roi_override is None:
        roi = probe_region(int(frame.width), int(frame.height))
    else:
        # 用户配置的 ROI 是帧内物理像素；帧尺寸变化后必须先重新校准，不能静默夹取
        # 成另一块区域（那会拿错误的区域当证据）。
        roi = roi_override
        if (
            roi.x < 0
            or roi.y < 0
            or roi.x + roi.width > int(frame.width)
            or roi.y + roi.height > int(frame.height)
        ):
            record["error"] = "配置 ROI 超出当前帧尺寸，需要重新校准（未做任何采集替换）"
            return record
    record["roi"] = {"x": roi.x, "y": roi.y, "width": roi.width, "height": roi.height}
    cropped = real_env.crop(frame.image, roi)
    sample = FrameSample(target.hwnd, 1, time.time(), cropped, roi)

    started = time.perf_counter()
    observation = ocr.recognize(sample)
    record["ocr_ms"] = round((time.perf_counter() - started) * 1000, 1)

    from deal_watch.normalize import char_rule_normalize

    normalized = char_rule_normalize("".join(block.text for block in observation.blocks))
    record["blocks"] = len(observation.blocks)
    record["chars"] = len(normalized)
    record["char_sha256"] = real_env.text_digest(normalized)

    started = time.perf_counter()
    panels = find_bubbles(cropped, observation.blocks)
    # 复用同一判定：assign_messages 在这里只用于确认未覆盖矩形的口径一致。
    _blocks, uncovered = assign_messages(cropped, observation.blocks)
    record["bubbles_ms"] = round((time.perf_counter() - started) * 1000, 1)
    record["panels"] = len(panels)
    record["certain"] = sum(1 for panel in panels if panel.certain)
    record["uncertain"] = record["panels"] - record["certain"]
    record["uncovered"] = max(len(observation.uncovered_boxes), len(uncovered))

    messages = blocks_to_messages(observation)
    record["messages"] = len(messages)
    hit_messages, hit_total = _keyword_hits(messages, keywords)
    record["keyword_hit_messages"] = hit_messages
    record["keyword_hit_total"] = hit_total

    if show_text:
        print(f"  {record['hwnd']} 识别原文（--show-text）：")
        for block in observation.blocks:
            print("    " + block.text)
    else:
        preview = "\n".join(block.text for block in observation.blocks)[:160]
        print(f"  {record['hwnd']} 脱敏预览：" + real_env.redact(preview))
    return record


def build_parser() -> argparse.ArgumentParser:
    """命令行参数；抽成独立函数便于测试默认值而不触碰真实桌面。"""
    parser = argparse.ArgumentParser(
        description=(
            "真实微信窗口的只读采集 + 识别探针。默认只跑用户已配置并确认的区域："
            "不枚举其他微信窗口、不自动生成 ROI、不写用户配置、不保存聊天截图。"
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=real_env.VALIDATION_DIR / f"real-window-probe-{real_env.app_version()}.json",
    )
    parser.add_argument(
        "--source",
        choices=(SOURCE_CONFIG, SOURCE_PROBE),
        default=SOURCE_CONFIG,
        help=(
            "区域来源：config=用户已配置并确认的 HWND/ROI（默认）；"
            "probe=枚举 DISPLAY1 微信窗口并用探针 ROI（需 --allow-probe-roi）"
        ),
    )
    parser.add_argument(
        "--allow-probe-roi",
        action="store_true",
        help="显式允许 --source probe 枚举窗口并生成探针 ROI；这种数据不能当验收证据",
    )
    parser.add_argument(
        "--max-windows", type=int, default=MAX_WINDOWS, help="探针模式下最多探测的窗口数（默认 4）"
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


def _load_user_config():
    """只读加载用户配置；异常返回 (None, None, 原因)。"""
    from deal_watch.config import ConfigStore

    try:
        store = ConfigStore()
        return store.load(), store.path, ""
    except Exception as error:  # noqa: BLE001
        return None, None, f"{type(error).__name__}: {error}"[:300]


def _target_from_region(region):
    """把已确认的 RegionConfig 变成探针用的 WindowTarget（只读身份快照）。"""
    from deal_watch.models import Rect

    hwnd = int(region.window_hwnd)
    rect = real_env.visible_rect(hwnd) or region.window_rect or Rect(0, 0, 1, 1)
    return real_env.WindowTarget(
        hwnd=hwnd,
        title=region.window_title,
        rect=rect,
        process_id=int(region.process_id),
        process_path=region.process_path,
        class_name=region.window_class,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.source == SOURCE_PROBE and not args.allow_probe_roi:
        print(
            "已拒绝执行（退出码 2）：--source probe 会枚举微信窗口并生成探针 ROI，"
            "必须同时显式给出 --allow-probe-roi；默认请使用 --source config。"
        )
        return 2

    real_env.require_windows()
    real_env.enable_dpi_awareness()
    probe_mode = args.source == SOURCE_PROBE
    report: dict = {
        "kind": "real_window_probe",
        "version": real_env.app_version(),
        "started_at": time.time(),
        "capture_mode": "probe_roi" if probe_mode else "user_configured",
        "roi_source": "probe_generated" if probe_mode else "user_config",
        "user_confirmed_roi": not probe_mode,
        "acceptance_eligible": not probe_mode,
        "region_count": 0,
        "regions_rejected": {},
        "dpi_expected": real_env.DPI_EXPECTED,
        "keywords_count": 0,
        "read_only": True,
        "sends_wechat_message": False,
        "writes_user_config": False,
        "windows": [],
        "status": "no_window",
    }
    if probe_mode:
        report["probe_roi_rule"] = "top 8% / bottom 32% / sides 2%（探针 ROI，不写入配置）"
        report["display1_rect"] = real_env.monitor_rect(real_env.DISPLAY1)
    # 采集参数写进报告：报告自己说明这是不是生产参数。
    report.update(real_env.production_param_flags(args.capture_timeout))

    from deal_watch.diagnostics import source_fingerprint

    report["source_fingerprint"] = source_fingerprint(
        [
            ROOT / "tools/real_window_probe.py",
            ROOT / "src/deal_watch/ocr.py",
            ROOT / "src/deal_watch/ocr_cache.py",
        ],
        root=ROOT,
    )

    keywords: list[str] = []
    entries: list[tuple[object, object]] = []
    if probe_mode:
        print(f"DISPLAY1 物理矩形：{report['display1_rect']}")
        limit = max(1, min(MAX_WINDOWS, int(args.max_windows)))
        targets = real_env.display1_weixin_windows(limit=limit)
        entries = [(target, None) for target in targets]
        keywords = real_env.read_keywords()
    else:
        loaded, config_path, load_error = _load_user_config()
        if loaded is None:
            print(f"读取用户配置失败（{load_error or '未知原因'}），未做任何采集（退出码 2）。")
            return 2
        from deal_watch.capture import WindowsWindows

        regions, rejected = configured_regions(loaded, WindowsWindows())
        report["regions_rejected"] = rejected
        if not regions:
            print(
                "用户配置里没有「已启用并预览确认」的区域，未做任何采集（退出码 2）。"
                "请在应用里圈选并预览确认；本工具不会自动枚举窗口或生成 ROI。"
            )
            for alias, problems in rejected.items():
                print(f"  {alias}: {'；'.join(problems)}")
            return 2
        print(f"只读加载用户配置：{config_path}（不写入）")
        entries = [(_target_from_region(region), region.rect) for region in regions]
        keywords = list(loaded.keywords)

    if not entries:
        print("未发现可采窗口（未做任何采集），已安全退出。")
        real_env.write_report(args.output, report)
        return 1
    report["region_count"] = len(entries)
    report["keywords_count"] = len(keywords)
    print(f"关键词只读副本：{len(keywords)} 个（不打印内容）")

    from deal_watch.ocr import LocalOcr

    ocr = LocalOcr(real_env.model_config(), ROOT)
    _warm_up(ocr)
    rows: list[str] = []
    for index, (target, roi_override) in enumerate(entries, 1):
        print(f"[{index}/{len(entries)}] {real_env.redact(target.title)[:24]} …")
        record = _probe_window(
            target,
            ocr,
            keywords,
            bool(args.show_text),
            float(args.capture_timeout),
            roi_override,
        )
        report["windows"].append(record)
        frame = record["frame"]
        size = f"{frame['width']}x{frame['height']}" if frame else "-"
        rows.append(
            f"  {index}  {size:>10}  blocks={record['blocks']:>3}  "
            f"panels={record['panels']}/{record['uncertain']}  "
            f"messages={record['messages']:>3}  ocr={record['ocr_ms']}ms"
            f"{'' if record['capture_ok'] else '  [采集失败]'}"
        )

    ok_windows = [record for record in report["windows"] if record["capture_ok"]]
    report["status"] = "ok" if ok_windows else "capture_failed"
    report["summary"] = {
        "windows": len(report["windows"]),
        "captured": len(ok_windows),
        "blocks": sum(record["blocks"] for record in report["windows"]),
        "panels": sum(record["panels"] for record in report["windows"]),
        "uncertain": sum(record["uncertain"] for record in report["windows"]),
        "messages": sum(record["messages"] for record in report["windows"]),
        "uncovered": sum(record["uncovered"] for record in report["windows"]),
        "keyword_hit_messages": sum(
            record["keyword_hit_messages"] for record in report["windows"]
        ),
        "ocr_ms_rounded": [
            round(record["ocr_ms"]) for record in report["windows"]
        ],
    }
    print("")
    print("窗口序号  帧尺寸      blocks  panels/uncertain  messages  ocr_ms")
    for row in rows:
        print(row)
    print(
        "合计：blocks={blocks} panels={panels} uncertain={uncertain} "
        "messages={messages} uncovered={uncovered} 命中消息={keyword_hit_messages}".format(
            **report["summary"]
        )
    )
    report["finished_at"] = time.time()
    report["stats_window"] = {
        "started_at": report["started_at"],
        "finished_at": report["finished_at"],
        "windows": len(report["windows"]),
        "note": "ocr_ms/bubbles_ms 只覆盖本次单帧采集；不是调度节奏证据",
    }
    report["read_only_notes"] = [
        "未发送任何微信消息",
        "未点击、滚动或输入",
        "未移动或缩放被监控窗口",
        "未写入用户 config.json / feedback.db（关键词只读副本）",
        "未保存聊天截图，报告只含计数与 sha256 摘要",
        (
            "ROI 取自用户配置并已确认（只读）"
            if not probe_mode
            else "ROI 由探针生成，不是用户确认；报告已标记不可用于验收"
        ),
    ]
    real_env.write_report(args.output, report)
    return 0 if ok_windows else 1


if __name__ == "__main__":
    raise SystemExit(main())
