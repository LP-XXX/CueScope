"""窗口被完全覆盖时 WGC 是否仍能采到新文字（只读验收工具）。

做法：用 PySide6 建一个**无边框、置顶、纯色**遮挡窗口盖住目标微信窗口，先用
``EnumWindows`` 的 Z 序与矩形包含关系证明“遮挡窗口确实在目标之上且完全覆盖”，
再按 5 秒节奏持续用 WGC 抓帧 + 真实 OCR，检查是否出现基线里没有的新文字行。

约束（只读）：不发送微信消息、不点击/滚动/输入、**不移动或缩放目标窗口**、
不写用户 config.json、不写 feedback.db、不保存聊天截图。遮挡窗口在 ``finally``
里必定关闭，退出时桌面恢复原状。报告只放计数、耗时、长度与 sha256 摘要；
控制台默认把中文替换为 ●，只有显式 ``--show-text`` 才打印识别原文。

判定：出现基线外的新行 → ``new_text_detected_while_covered = true``；
到 ``--timeout`` 仍未出现 → ``inconclusive: 用户未在该群发送新消息``
（既不算通过也不算失败）；采集报错记入 ``capture_errors``。

端到端口径（0.2.1 补充）：只凭“出现基线外新行”不足以证明指定消息成功送达，
因此支持 ``--expect``（可重复）指定用户从手机发出的唯一编号/短语。给定时：
**同一次采样**必须既 ``fully_covered`` 成立、又按 ``match_normalize`` 字面命中
全部期望项，才记 ``success_basis = "expected_text"`` 且 ``conclusive = true``；
覆盖在某轮掉下来的采样一律不算成功（记 ``coverage_lost_at_seconds`` 继续观察）。
未给 ``--expect`` 时保留旧口径，但报告会写 ``success_basis = "new_line_only"``
与 ``conclusive = false``，避免把“随便有新行”当成生产验收通过。

退出码：结论已给出（检出或不可判定）→ 0；无法开始采集/遮挡校验失败 → 1。
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

POLL_SECONDS = 5.0
OCCLUDER_COLOR = "#2b2b2b"
MIN_NEW_LINE_CHARS = 2
SUCCESS_BASIS_EXPECTED = "expected_text"
SUCCESS_BASIS_NEW_LINE = "new_line_only"


def normalize_expected_items(items: list[str] | None) -> list[str]:
    """规范化期望项：NFKC + 去空白 + casefold（与生产字面匹配同一套规则）。

    空项（纯空白）丢弃；返回空列表表示“没有期望项”，此时只能用新行口径。
    """
    from deal_watch.normalize import match_normalize

    normalized = [match_normalize(item) for item in (items or [])]
    return [item for item in normalized if item]


def expect_sha256(items: list[str] | None) -> str:
    """期望项的 sha256 摘要；报告只存摘要，不存明文。"""
    return real_env.text_digest("|".join(normalize_expected_items(items)))


def expected_items_matched(texts: list[str], expected: list[str] | None) -> bool:
    """同一次采样里全部期望项都字面出现才算命中。

    用 ``match_normalize`` 而不是 ``char_multiset``：唯一编号必须按顺序字面出现，
    不能被“字符齐全但顺序错”判中。归一化会去掉空白，因此 OCR 把同一编号折成
    两行时仍算命中（这是想要的容忍度，不影响顺序要求）。
    期望项为空时返回 False（不构成端到端证据）。
    """
    from deal_watch.normalize import match_normalize

    targets = normalize_expected_items(expected)
    if not targets:
        return False
    haystack = match_normalize("\n".join(texts))
    return all(item in haystack for item in targets)


def evaluate_sample(
    coverage: dict,
    *,
    expected: list[str] | None,
    texts: list[str],
    new_line_count: int,
) -> dict:
    """把一次采样收敛成判定：覆盖成立 + 文字命中才算成功。

    - 有 ``--expect``：``success = fully_covered and 全部期望项命中``，成功即 conclusive。
    - 无 ``--expect``：退回“覆盖成立 + 出现基线外新行”，但**永不** conclusive
      （``success_basis = "new_line_only"``），避免任意新行冒充生产验收证据。
    """
    covered = bool(coverage.get("fully_covered"))
    matched = expected_items_matched(texts, expected)
    if normalize_expected_items(expected):
        return {
            "coverage_ok": covered,
            "expected_text_matched": matched,
            "success": bool(covered and matched),
            "success_basis": SUCCESS_BASIS_EXPECTED,
            "conclusive": bool(covered and matched),
        }
    return {
        "coverage_ok": covered,
        "expected_text_matched": matched,
        "success": bool(covered and new_line_count > 0),
        "success_basis": SUCCESS_BASIS_NEW_LINE,
        "conclusive": False,
    }


def _logical_rect(app, physical, hwnd):
    """把物理矩形换算成 Qt 逻辑矩形（每监视器 V2 DPI 感知下的 device-independent 坐标）。

    这里有两个必须绕开的坑：

    1. ``QScreen.geometry()`` 是逻辑坐标，Win32 窗口矩形是物理像素，原点**不能**混用。
       本机 DISPLAY1 物理原点 x=2560、Qt 逻辑原点也是 x=2560（左侧 G72 逻辑宽 2560），
       但 DPR=1.5，早期版本直接 ``physical.x - geometry.x()`` 再缩放，遮挡窗口会被推到
       目标右侧约 320 物理像素，"完全覆盖"永远为 False。
    2. ``QScreen.name()`` 在 Windows 上返回的是友好名（``G72`` / ``MNG007DA5-1``），
       不是 ``\\\\.\\DISPLAY1``；必须先用 ``MonitorFromWindow`` 取设备名再取物理矩形。
    """
    from PySide6.QtCore import QRect

    monitor = real_env.monitor_rect(real_env.monitor_device(hwnd))
    if not monitor:
        return None
    for screen in app.screens():
        ratio = screen.devicePixelRatio() or 1.0
        geometry = screen.geometry()
        if (
            round(geometry.width() * ratio) != monitor["width"]
            or round(geometry.height() * ratio) != monitor["height"]
        ):
            continue
        candidate = QRect(
            round(geometry.x() + (physical.x - monitor["left"]) / ratio),
            round(geometry.y() + (physical.y - monitor["top"]) / ratio),
            max(1, round(physical.width / ratio)),
            max(1, round(physical.height / ratio)),
        )
        # 逻辑→物理再取整会带来 ±1 像素误差（实测 y 429 逻辑 → 644 物理，目标在 643），
        # 覆盖校验按整数包含判断会因此失败；向外扩 4 逻辑像素后仍夹在该屏内。
        candidate = candidate.adjusted(-4, -4, 4, 4).intersected(geometry)
        if geometry.contains(candidate):
            return candidate
    return None


def _z_order_state(occluder_hwnd: int, target_hwnd: int, target_rect) -> dict:
    """用 EnumWindows（Z 序自上而下）与矩形包含关系校验覆盖率。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    order: list[int] = []

    def visit(hwnd: int, _param: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            order.append(int(hwnd))
        return True

    user32.EnumWindows(enum_type(visit), 0)
    occluder_index = order.index(occluder_hwnd) if occluder_hwnd in order else -1
    target_index = order.index(target_hwnd) if target_hwnd in order else -1
    above = occluder_index >= 0 and target_index >= 0 and occluder_index < target_index
    occluder_rect = real_env.visible_rect(occluder_hwnd)
    covers = bool(
        occluder_rect
        and occluder_rect.x <= target_rect.x
        and occluder_rect.y <= target_rect.y
        and occluder_rect.x + occluder_rect.width >= target_rect.x + target_rect.width
        and occluder_rect.y + occluder_rect.height >= target_rect.y + target_rect.height
    )
    return {
        "occluder_z_index": occluder_index,
        "target_z_index": target_index,
        "occluder_above_target": bool(above),
        "occluder_rect_covers_target": covers,
        "fully_covered": bool(above and covers),
        "occluder_rect": (
            {
                "x": occluder_rect.x,
                "y": occluder_rect.y,
                "width": occluder_rect.width,
                "height": occluder_rect.height,
            }
            if occluder_rect
            else None
        ),
    }


def _visible_windows_occluded(hwnd: int, rect) -> bool:
    """按“可见窗口”判断是否被遮挡；只做记录，因为 WGC 不受遮挡影响。"""
    from deal_watch.capture import WindowsWindows

    windows = WindowsWindows()
    info = windows.window_from_hwnd(hwnd)
    if info is None:
        return False
    return bool(windows.occluded(info, rect))


def _line_digests(observation) -> set[str]:
    """每行文本的“前 40 字”摘要集合；用于判断是否出现基线外的新行。"""
    return {
        real_env.prefix_digest(block.text)
        for block in observation.blocks
        if len(block.text.strip()) >= MIN_NEW_LINE_CHARS
    }


def timeout_conclusion(sample_count: int, expected: list[str] | None) -> dict:
    """超时仍未成功时的结论（纯函数，便于单测）。

    - 一次采样都没取到 → 采集失败，退出码 1（不能伪装成“用户没发消息”）。
    - 有采样但没构成成功 → inconclusive，退出码 0，原因区分是否给了 ``--expect``。
    """
    if sample_count <= 0:
        return {
            "exit_code": 1,
            "aborted": "遮挡期间没有任何一次成功采样",
            "inconclusive": None,
        }
    return {
        "exit_code": 0,
        "aborted": None,
        "inconclusive": (
            "未在覆盖期间识别到期望文字"
            if normalize_expected_items(expected)
            else "用户未在该群发送新消息"
        ),
    }


def _print_observation(observation, show_text: bool) -> None:
    """控制台输出：默认脱敏，只有 --show-text 才打印原文。"""
    if show_text:
        for block in observation.blocks:
            print("    " + block.text)
        return
    preview = "\n".join(block.text for block in observation.blocks)[:160]
    print("  脱敏预览：" + real_env.redact(preview))


def _run_probe(app, target, args, report: dict) -> int:
    """在遮挡窗口存在期间完成校验、基线与轮询；返回退出码。

    ``finally`` 保证遮挡窗口一定关闭；这里所有 ``return`` 都会先执行 ``finally``。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtWidgets import QWidget

    from deal_watch.models import FrameSample, Rect
    from deal_watch.ocr import LocalOcr
    from deal_watch.window_capture import WindowCaptureClient, WindowCaptureError

    logical = _logical_rect(app, target.rect, target.hwnd)
    if logical is None:
        print("目标窗口不在任何 Qt 屏幕内，无法放置遮挡窗口，已安全退出（未改动桌面）。")
        report["aborted"] = "目标窗口不在任何 Qt 屏幕内"
        return 1
    report["occluder_logical_rect"] = {
        "x": logical.x(),
        "y": logical.y(),
        "width": logical.width(),
        "height": logical.height(),
    }

    class Occluder(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("DealWatch OCCLUDER")
            self.setWindowFlags(
                Qt.WindowType.Window
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
            )
            self.setGeometry(logical)

        def paintEvent(self, _event) -> None:
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor(OCCLUDER_COLOR))

    occluder = Occluder()
    ocr = LocalOcr(real_env.model_config(), ROOT)
    client = WindowCaptureClient(timeout=float(args.capture_timeout))
    try:
        occluder.show()
        occluder.raise_()
        app.processEvents()
        time.sleep(0.6)
        app.processEvents()
        occluder_hwnd = int(occluder.winId())
        coverage = _z_order_state(occluder_hwnd, target.hwnd, target.rect)
        coverage["visible_window_occluded"] = _visible_windows_occluded(
            target.hwnd, target.rect
        )
        coverage["note"] = (
            "visible_window_occluded 只按可见窗口记录；WGC 采集不依赖窗口是否可见，"
            "因此它不影响本工具的判定。"
        )
        report["coverage"] = coverage
        print(f"遮挡窗口 0x{occluder_hwnd:x} 完全覆盖目标：{coverage['fully_covered']}")
        print(f"按可见窗口判断已被遮挡（仅记录）：{coverage['visible_window_occluded']}")
        if not coverage["fully_covered"]:
            print("遮挡窗口未完全覆盖目标或不在其之上，无法验证遮挡场景（退出码 1）。")
            report["aborted"] = "遮挡窗口未完全覆盖目标"
            return 1

        baseline_image = None
        try:
            baseline_image = client.capture(target.hwnd, "wgc").image
            report["frames_captured"] += 1
        except WindowCaptureError as error:
            report["capture_errors"].append(f"BASELINE: {error}"[:200])
        if baseline_image is None:
            print("基线抓帧失败，无法继续（退出码 1）。")
            report["aborted"] = "基线抓帧失败"
            return 1

        sample = FrameSample(
            "occluded",
            1,
            time.time(),
            baseline_image,
            Rect(0, 0, baseline_image.shape[1], baseline_image.shape[0]),
        )
        baseline = ocr.recognize(sample)
        baseline_digests = _line_digests(baseline)
        baseline_chars = sum(len(block.text) for block in baseline.blocks)
        report["baseline_lines"] = len(baseline_digests)
        report["baseline_chars"] = baseline_chars
        report["baseline_sha256"] = real_env.text_digest("|".join(sorted(baseline_digests)))
        print(f"基线：{len(baseline_digests)} 行 / {baseline_chars} 字（只记摘要）")
        _print_observation(baseline, bool(args.show_text))

        started = time.monotonic()
        sequence = 1
        detected = False
        succeeded = False
        expected = list(args.expect or [])
        while time.monotonic() - started < args.timeout:
            remaining = args.timeout - (time.monotonic() - started)
            time.sleep(min(POLL_SECONDS, max(0.1, remaining)))
            app.processEvents()
            sequence += 1
            elapsed = time.monotonic() - started
            # 每轮都重做覆盖校验：只有“此刻仍被完全覆盖”的采样才能作为证据，
            # 否则中途覆盖掉过一次就会把普通可见桌面误当成遮挡期间的成果。
            coverage = _z_order_state(occluder_hwnd, target.hwnd, target.rect)
            report["coverage_rechecks"] += 1
            if not coverage["fully_covered"] and report["coverage_lost_at_seconds"] is None:
                report["coverage_lost_at_seconds"] = round(elapsed, 1)
                print(f"  {elapsed:6.1f}s 覆盖已失效（本轮不计入成功）")
            try:
                frame = client.capture(target.hwnd, "wgc")
                report["frames_captured"] += 1
            except WindowCaptureError as error:
                report["capture_errors"].append(f"{error.code}: {error}"[:200])
                print(f"  {elapsed:6.1f}s 采集失败：{error}")
                continue
            sample = FrameSample(
                "occluded",
                sequence,
                time.time(),
                frame.image,
                Rect(0, 0, frame.image.shape[1], frame.image.shape[0]),
            )
            observation = ocr.recognize(sample)
            digests = _line_digests(observation)
            fresh = digests - baseline_digests
            texts = [block.text for block in observation.blocks]
            verdict = evaluate_sample(
                coverage, expected=expected, texts=texts, new_line_count=len(fresh)
            )
            report["samples"].append(
                {
                    "seconds": round(elapsed, 1),
                    "lines": len(digests),
                    "new_lines": len(fresh),
                    "chars": sum(len(block.text) for block in observation.blocks),
                    # 每条采样自带当轮的覆盖结论与期望项命中情况，便于逐条复核。
                    "coverage_ok": bool(verdict["coverage_ok"]),
                    "expected_text_matched": bool(verdict["expected_text_matched"]),
                }
            )
            print(
                f"  {elapsed:6.1f}s 行 {len(digests)} 新行 {len(fresh)} "
                f"覆盖 {verdict['coverage_ok']} 期望命中 {verdict['expected_text_matched']}"
            )
            if args.show_text and fresh:
                for block in observation.blocks:
                    if real_env.prefix_digest(block.text) in fresh:
                        print("    新行：" + block.text)
            if fresh and verdict["coverage_ok"] and not detected:
                detected = True
                report["new_text_detected_while_covered"] = True
                report["first_detection_seconds"] = round(elapsed, 1)
                report["first_detection_line_count"] = len(fresh)
                report["first_detection_sha256"] = real_env.text_digest(
                    "|".join(sorted(fresh))
                )
                print(f"检出遮挡期间的新文字：{len(fresh)} 行，用时 {elapsed:.1f}s")
            if verdict["success"]:
                succeeded = True
                report["success_basis"] = verdict["success_basis"]
                report["conclusive"] = bool(verdict["conclusive"])
                if verdict["expected_text_matched"]:
                    report["expected_text_matched"] = True
                    report["expected_matched_at_seconds"] = round(elapsed, 1)
                    print(
                        f"覆盖期间命中全部 --expect（第 {sequence} 次采样，"
                        f"{elapsed:.1f}s）；该时刻不是通知延迟"
                    )
                else:
                    print(f"覆盖期间出现基线外新行（口径 {verdict['success_basis']}，非 conclusive）")
                break
        if not succeeded:
            outcome = timeout_conclusion(len(report["samples"]), expected)
            if outcome["aborted"]:
                report["aborted"] = outcome["aborted"]
                print(f"{outcome['aborted']}，判定为采集失败（退出码 1）。")
                return int(outcome["exit_code"])
            report["inconclusive"] = outcome["inconclusive"]
            print(
                f"超时未构成结论：inconclusive（{outcome['inconclusive']}），"
                "不记为失败也不记为通过。"
            )
        return 0
    finally:
        occluder.close()
        app.processEvents()
        app.quit()


def build_parser() -> argparse.ArgumentParser:
    """命令行参数；抽成独立函数便于测试默认值而不触碰真实桌面。"""
    parser = argparse.ArgumentParser(
        description=(
            "窗口被完全覆盖时 WGC 是否仍能采到新文字。只读：不发送微信消息、"
            "不点击/滚动/输入、不移动被监控窗口、不写用户配置、不保存聊天截图。"
        )
    )
    parser.add_argument("--hwnd", type=lambda value: int(value, 0), help="目标窗口句柄")
    parser.add_argument("--title-contains", help="按标题子串在 DISPLAY1 内选唯一微信窗口")
    parser.add_argument("--timeout", type=float, default=180.0, help="最长观察秒数（默认 180）")
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
        "--expect",
        action="append",
        default=[],
        metavar="TEXT",
        help=(
            "用户从手机发出的唯一编号/短语（可重复传入）。给定时只有“同一次采样既完全"
            "覆盖、又字面命中全部期望项”才算验收通过；报告只存 sha256，不存明文"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=real_env.VALIDATION_DIR / f"occluded-new-text-{real_env.app_version()}.json",
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

    from PySide6.QtWidgets import QApplication

    from deal_watch.diagnostics import source_fingerprint

    app = QApplication([])
    report: dict = {
        "kind": "occluded_new_text_check",
        "version": real_env.app_version(),
        "started_at": time.time(),
        # 目标窗口由用户显式给出（--hwnd / --title-contains），不是默认枚举。
        "capture_mode": "explicit_target_window",
        "roi_source": "full_frame",
        "user_confirmed_roi": False,
        "region_count": 1,
        "source_fingerprint": source_fingerprint(
            [
                ROOT / "tools/occluded_new_text_check.py",
                ROOT / "src/deal_watch/ocr.py",
                ROOT / "src/deal_watch/window_capture.py",
            ],
            root=ROOT,
        ),
        "hwnd": f"0x{target.hwnd:x}",
        "title_sha256": real_env.text_digest(target.title),
        "monitor": real_env.monitor_device(target.hwnd),
        "target_rect": {
            "x": target.rect.x,
            "y": target.rect.y,
            "width": target.rect.width,
            "height": target.rect.height,
        },
        "occluder_logical_rect": None,
        "timeout_seconds": float(args.timeout),
        "read_only": True,
        "sends_wechat_message": False,
        "writes_user_config": False,
        "target_window_moved": False,
        "target_rect_after": None,
        "coverage": {},
        "new_text_detected_while_covered": False,
        "first_detection_seconds": None,
        "first_detection_seconds_note": (
            "这是相邻两次采样之间首次检出基线外新行的时刻，不是飞书通知延迟"
        ),
        "detection_seconds_is_not_notification_latency": True,
        "expect_count": len(normalize_expected_items(args.expect)),
        "expect_sha256": expect_sha256(args.expect),
        "expected_text_matched": False,
        "expected_matched_at_seconds": None,
        "success_basis": (
            SUCCESS_BASIS_EXPECTED
            if normalize_expected_items(args.expect)
            else SUCCESS_BASIS_NEW_LINE
        ),
        "conclusive": False,
        "coverage_rechecks": 0,
        "coverage_lost_at_seconds": None,
        "frames_captured": 0,
        "capture_errors": [],
        "samples": [],
    }
    # 采集参数写进报告：报告自己说明这是不是生产参数。
    report.update(real_env.production_param_flags(args.capture_timeout))
    if report["expect_count"]:
        print(f"期望文字：{report['expect_count']} 项，sha256 {report['expect_sha256'][:16]}…")
        if args.show_text:
            print("期望文字明文（--show-text）：" + " | ".join(args.expect))
    else:
        print("未提供 --expect：本次只能给出 new_line_only 的弱证据（conclusive=false）。")
    try:
        exit_code = _run_probe(app, target, args, report)
    finally:
        app.processEvents()
        app.quit()

    # 实测目标窗口几何是否变化：工具从不移动/缩放窗口，这里给出证据而不是声明。
    after = real_env.visible_rect(target.hwnd)
    report["target_rect_after"] = (
        {"x": after.x, "y": after.y, "width": after.width, "height": after.height}
        if after
        else None
    )
    report["target_window_moved"] = bool(
        after is None
        or (after.x, after.y, after.width, after.height)
        != (target.rect.x, target.rect.y, target.rect.width, target.rect.height)
    )

    report["finished_at"] = time.time()
    report["occlusion_restored"] = True
    report["read_only_notes"] = [
        "未发送任何微信消息",
        "未点击、滚动或输入",
        "未移动或缩放被监控窗口",
        "未写入用户 config.json / feedback.db",
        "未保存聊天截图，报告只含计数与 sha256 摘要",
        "遮挡窗口已在 finally 中关闭",
        "每轮采样都重做覆盖校验（coverage_rechecks），覆盖失效的采样不计入成功",
        "first_detection_seconds / expected_matched_at_seconds 是采样检出时刻，不是飞书通知延迟",
    ]
    real_env.write_report(args.output, report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
