"""为已配置的四条区域生成标注预览图（只读，不写用户配置）。

用途：0.2.1 首次使用时用户要逐个确认“当前四窗与 ROI 是否对得上”。本工具先在
命令行把每条区域的实际画面、ROI 边界、气泡面板、未覆盖区域画出来，供人工比对；
真正的放行仍由程序里的“确认此区域”完成（``previewed=true``）。

约束：不发送微信消息、不点击/滚动/输入、不移动或缩放目标窗口、不写
``config.json``、不写 ``feedback.db``、不保存到用户目录以外的位置。

输出：``build/tmp/roi-preview/<序号>-<别名>.png``（标注图，交付前删除）。
控制台默认把识别文字替换为 ●，只有 ``--show-text`` 才打印原文。
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

OUT_DIR = ROOT / "build" / "tmp" / "roi-preview"


def _load_regions():
    real_env.bootstrap()
    from deal_watch.config import ConfigStore, default_data_dir

    return ConfigStore(default_data_dir()).load()


def _annotate(image, region, observation):
    """返回一张标注图：ROI 用绿色，气泡面板用蓝色，未覆盖区域用橙色。"""
    import cv2

    canvas = image.copy()
    cv2.rectangle(canvas, (0, 0), (canvas.shape[1] - 1, canvas.shape[0] - 1), (60, 160, 60), 2)
    for box in getattr(observation, "uncovered_boxes", ()):
        cv2.rectangle(
            canvas,
            (box.x, box.y),
            (box.x + box.width, box.y + box.height),
            (0, 140, 255),
            2,
        )
    for block in observation.blocks:
        box = block.box
        cv2.rectangle(
            canvas,
            (box.x, box.y),
            (box.x + box.width, box.y + box.height),
            (200, 120, 0),
            2,
        )
    return canvas


def build_parser() -> argparse.ArgumentParser:
    """命令行参数；抽成独立函数便于测试默认值而不触碰真实桌面。"""
    parser = argparse.ArgumentParser(description="生成已配置区域的标注预览图（只读）")
    parser.add_argument("--show-text", action="store_true", help="打印识别原文（默认脱敏）")
    parser.add_argument("--backend", default="", help="覆盖采集后端（默认用区域配置）")
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
        "--output",
        type=Path,
        default=real_env.VALIDATION_DIR / f"roi-preview-{real_env.app_version()}.json",
        help="参数与结果计数报告（不含聊天原文）",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    real_env.require_windows()
    real_env.enable_dpi_awareness()

    import cv2
    import numpy as np

    from deal_watch.models import FrameSample, Rect
    from deal_watch.ocr import LocalOcr

    config = _load_regions()
    regions = [r for r in config.regions if r.enabled]
    if not regions:
        print("没有启用的区域，无法生成预览。")
        return 1

    ocr = LocalOcr(config.model, ROOT)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    failures = 0
    for index, region in enumerate(regions, start=1):
        print(f"\n[{index}/{len(regions)}] {region.alias}")
        print(f"  窗口标题: {region.window_title}")
        print(f"  hwnd: 0x{region.window_hwnd:x}  后端: {args.backend or region.capture_backend}")
        try:
            real_env.assert_display1(region.window_hwnd)
        except SystemExit:
            print("  跳过：该窗口不在 DISPLAY1")
            failures += 1
            continue

        backend = args.backend or region.capture_backend
        payload = real_env.worker_payload(
            region.window_hwnd, backend, timeout=float(args.capture_timeout)
        )
        if not payload.get("ok"):
            print(f"  采集失败: {payload.get('error')} {payload.get('message', '')}")
            failures += 1
            continue
        image = real_env.decode_frame(payload)
        if image is None:
            print("  采集失败: 无法解码帧")
            failures += 1
            continue

        print(f"  帧尺寸: {image.shape[1]}x{image.shape[0]}")
        reference_ok = (
            image.shape[1] == (region.reference_frame_width or image.shape[1])
            and image.shape[0] == (region.reference_frame_height or image.shape[0])
        )
        print(
            f"  参考尺寸: {region.reference_frame_width or '-'}x"
            f"{region.reference_frame_height or '-'}  {'一致' if reference_ok else '不一致（需重新校准）'}"
        )

        rect = region.rect
        inside = (
            rect.x >= 0
            and rect.y >= 0
            and rect.x + rect.width <= image.shape[1]
            and rect.y + rect.height <= image.shape[0]
        )
        print(f"  ROI: {rect.x},{rect.y} {rect.width}x{rect.height}  {'在帧内' if inside else '超出帧范围'}")
        if not inside:
            failures += 1
            continue

        patch = real_env.crop(image, rect)
        started = time.perf_counter()
        observation = ocr.recognize(
            FrameSample(
                region_id=region.id,
                sequence=0,
                captured_at=time.time(),
                image=np.ascontiguousarray(patch),
                rect=Rect(0, 0, patch.shape[1], patch.shape[0]),
            )
        )
        elapsed = (time.perf_counter() - started) * 1000
        print(f"  文字块: {len(observation.blocks)}  未覆盖区域: {len(observation.uncovered_boxes)}")
        print(f"  识别耗时: {elapsed:.0f} ms")
        for block in observation.blocks:
            text = block.text if args.show_text else real_env.redact(block.text)
            print(f"    - [{block.kind}] {text}")
        for box in observation.uncovered_boxes:
            print(f"    ! 未覆盖: {box.x},{box.y} {box.width}x{box.height}")

        name = "".join(ch for ch in region.alias if ch.isalnum() or ch in "-_")[:24] or f"r{index}"
        target = OUT_DIR / f"{index}-{name}.png"
        # cv2.imwrite 在 Windows 上对非 ASCII 路径不可靠，改用 imencode + 字节写入。
        ok, buffer = cv2.imencode(".png", _annotate(patch, region, observation))
        if not ok:
            print("  标注图写入失败：编码错误")
            failures += 1
            continue
        target.write_bytes(buffer.tobytes())
        print(f"  标注图: {target}")

        if not region.previewed:
            print("  确认状态: 尚未在程序内确认（previewed=false）")

    report: dict = {
        "kind": "roi_preview",
        "version": real_env.app_version(),
        "finished_at": time.time(),
        "regions": len(regions),
        "failures": failures,
        "previewed_regions": sum(1 for region in regions if region.previewed),
        "read_only": True,
        "sends_wechat_message": False,
        "writes_user_config": False,
    }
    # 采集参数写进报告：报告自己说明这是不是生产参数。
    report.update(real_env.production_param_flags(args.capture_timeout))
    real_env.write_report(Path(args.output), report)

    print(f"\n完成，失败 {failures} 条。确认图在 {OUT_DIR}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
