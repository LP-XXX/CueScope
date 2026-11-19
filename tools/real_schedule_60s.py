"""真实 60 秒调度的稳定性记录（只读，不发送任何微信消息）。

**默认跑用户自己的配置**：从 ``%LOCALAPPDATA%\\DealWatch\\config.json`` **只读**加载
区域，只接受已经「已启用 + 已预览确认 + 通过就绪门禁（HWND/进程身份/参考帧/ROI）」
的区域。工具**不枚举**其他 Weixin 窗口、**不自动生成 ROI**、**不视作用户确认**、
**不写**用户配置。

``--source probe --allow-probe-roi`` 是显式的诊断旁路（给还没有配置的机器量真实
WGC/OCR 耗时）：它会按标题枚举 DISPLAY1 内的微信窗口并生成探针 ROI，报告中
``roi_source="probe_generated"``、``user_confirmed_roi=False``，
``acceptance_eligible=False``——这种数据只能看耗时，不能当作用户验收证据。

通知只发到 ``http://127.0.0.1:<随机端口>`` 模拟飞书；不点击/滚动/输入、
不移动或缩放被监控窗口、不写用户 feedback.db、不保存聊天截图。

分段观测与应用**共用同一套实现**：``LocalOcr(observer=OcrStageRecorder(...))``
与 ``NotificationDispatcher(metrics=NotifyMetrics(...))``，这里不再包一层计时壳，
避免 ``inference_ms``（识别全程）与"纯引擎"两段重叠计时。

退出码：全部断言通过 → 0；有断言失败 → 1；参数不合规/没有可用区域 → 2。
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _entry in (str(ROOT / "src"), str(ROOT), str(ROOT / "tools")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import real_env  # noqa: E402  （先补齐 sys.path 才能导入共享层）

from deal_watch import metrics  # noqa: E402
from deal_watch.diagnostics import (  # noqa: E402
    configure_capture_stage_log,
    format_stage_timings,
    reuse_rate_line,
    source_fingerprint,
    stage_timing_rows,
)
from deal_watch.metrics import (  # noqa: E402
    COLD_BUCKET_CAPACITY,
    STAGE_PENDING_REASONS,
    OcrStageRecorder,
    RoundCompletion,
    TimingRecorder,
)
from deal_watch.notify import NotifyMetrics  # noqa: E402

# RecordingCapturer 用 WindowFrameCapturer → WindowCaptureClient 起采集子进程；子进程只
# 继承 os.environ，必须把 src/ROOT 放进 PYTHONPATH，否则开发态采集全部超时/失败。
real_env.ensure_subprocess_pythonpath()

MIN_ROUNDS = 10
ROUND_GRACE_SECONDS = 20.0
SETTLE_SECONDS = 30.0
SCHEDULE_JITTER_MS = 1000
PAUSE_CODES = {"window_minimized", "desktop_locked"}

SOURCE_CONFIG = "config"
SOURCE_PROBE = "probe"
CAPTURE_MODE_CONFIG = "user_configured"
CAPTURE_MODE_PROBE = "probe_roi"

#: 报告里的"源代码指纹"：产生这份报告的那几个文件，任何一个变动指纹都会变。
#: 除了采集/OCR/通知这条链路，还必须覆盖服务层与采集子进程协议——"完成标记"修复、
#: 队列/跟踪语义和 WGC 帧协议都在这些文件里，漏掉就会让旧报告的指纹对不上新代码。
FINGERPRINT_FILES: tuple[str, ...] = (
    "tools/real_schedule_60s.py",
    "src/deal_watch/ocr.py",
    "src/deal_watch/metrics.py",
    "src/deal_watch/ocr_cache.py",
    "src/deal_watch/notify.py",
    "src/deal_watch/diagnostics.py",
    "src/deal_watch/service.py",
    "src/deal_watch/window_capture.py",
    "src/deal_watch/capture_pool.py",
    "src/deal_watch/capture_worker.py",
)

GIT_TIMEOUT_SECONDS = 10.0


def _run_git(args: list[str], root: Path) -> str | None:
    """只读执行一条 git 命令；Git 缺失/非仓库/超时都返回 ``None``，绝不抛异常。

    只用 ``rev-parse`` 与 ``status --porcelain``：不打印配置、不写仓库，输出里也
    不会带用户名目录或凭据。
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def git_provenance(
    root: Path,
    *,
    runner: Callable[[list[str], Path], str | None] | None = None,
) -> dict[str, object]:
    """报告用的 Git 溯源：``source_commit`` 全值 + ``source_dirty``（tracked 是否被改）。

    Git 不可用时对应字段写 ``None``（JSON null），报告照常产出——缺 Git 是环境限制，
    不是证据无效；但绝不能因此让整份报告写不出来。
    """
    run = runner or _run_git
    try:
        commit = run(["rev-parse", "HEAD"], root)
    except Exception:  # noqa: BLE001 - 溯源失败不得中断验收
        commit = None
    if commit is None or not commit.strip():
        return {"source_commit": None, "source_dirty": None}
    try:
        status = run(["status", "--porcelain", "--untracked-files=no"], root)
    except Exception:  # noqa: BLE001 - 同上
        status = None
    return {
        "source_commit": commit.strip(),
        "source_dirty": None if status is None else bool(status.strip()),
    }


def submit_check_detail(region_count: int) -> str:
    """``every_region_submitted_each_round`` 的说明文案，按**实际区域数**生成。

    用户可能只配 2 个或 3 个区域；写死"四区域"会让报告说的是配置里没有的事。
    """
    return f"每轮 {int(region_count)} 个区域提交数均 > 0"


def _percentile(values: list[float], ratio: float) -> int:
    """四舍五入到毫秒的百分位；口径与 ``deal_watch.metrics.percentile`` 完全一致。"""
    if not values:
        return 0
    return round(metrics.percentile(values, ratio))


class RecordingCapturer:
    """记录每次提交与失败的采集适配器；不改变 ``WindowFrameCapturer`` 语义。"""

    def __init__(
        self, capture_timeout: float = real_env.PRODUCTION_CAPTURE_TIMEOUT,
        component_path: Path | None = None,
    ) -> None:
        from deal_watch.window_capture import WindowCaptureClient, WindowFrameCapturer

        self.capture_timeout = float(capture_timeout)
        self._inner = WindowFrameCapturer(
            client=WindowCaptureClient(
                timeout=self.capture_timeout, persistent=True, component_path=component_path,
            )
        )
        self.submitted: dict[str, int] = {}
        self.timestamps: dict[str, list[float]] = {}
        # 每个区域最近一次成功采集的帧序号：本轮的"目标序号"就从这里取，
        # 完成判定因此可以对上具体某一帧，而不是猜"队列空了"。
        self.sequences: dict[str, int] = {}
        self.failures: list[dict] = []
        self._lock = threading.Lock()

    @property
    def last_trace(self):
        """透传子进程 trace：``capture`` 段的耗时因此取真分段，而不是外层墙钟。"""
        return self._inner.last_trace

    @property
    def client(self):
        return self._inner.client

    @property
    def windows(self):
        return self._inner.windows

    def prepare(self) -> None:
        self._inner.prepare()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        return self._inner.wait_ready(timeout)

    def close(self) -> None:
        self._inner.close()

    @staticmethod
    def require_display1(region) -> None:
        from deal_watch.capture import CaptureError
        from deal_watch.models import CaptureErrorCode

        visible = real_env.visible_rect(region.window_hwnd)
        if (real_env.monitor_device(region.window_hwnd).upper() != real_env.DISPLAY1.upper()
                or visible is None or not real_env.fully_inside(visible, real_env.monitor_rect())):
            raise CaptureError(CaptureErrorCode.INVALID_REGION, "窗口不完全位于 DISPLAY1，拒绝采集")

    def validate(self, region):
        self.require_display1(region)
        return self._inner.validate(region)

    def capture(self, region, force: bool = False):
        from deal_watch.capture import CaptureError

        try:
            self._inner.last_trace = None
            self.require_display1(region)
            frame = self._inner.capture(region, force=force)
        except CaptureError as error:
            with self._lock:
                self.failures.append(
                    {"region_id": region.id, "code": str(error.code), "detail": str(error)[:200]}
                )
            raise
        with self._lock:
            self.submitted[region.id] = self.submitted.get(region.id, 0) + 1
            self.timestamps.setdefault(region.id, []).append(time.monotonic())
            if frame is not None:
                self.sequences[region.id] = int(frame.sequence)
        return frame

    def reset(self, region_id: str) -> None:
        self._inner.reset(region_id)

    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self.submitted)

    def sequence_for(self, region_id: str) -> int:
        with self._lock:
            return int(self.sequences.get(region_id, 0))

    def cadence(self) -> dict[str, dict]:
        """每个区域相邻两次提交的间隔（毫秒）：这是“60 秒调度”的真实证据。"""
        with self._lock:
            result: dict[str, dict] = {}
            for region_id, stamps in self.timestamps.items():
                deltas = [
                    round((later - earlier) * 1000, 1)
                    for earlier, later in zip(stamps, stamps[1:], strict=False)
                ]
                result[region_id] = {
                    "submits": len(stamps),
                    "delays_ms": deltas,
                    "max_ms": max(deltas) if deltas else 0.0,
                    "min_ms": min(deltas) if deltas else 0.0,
                }
            return result

    def failure_summary(self) -> dict[str, int]:
        with self._lock:
            summary: dict[str, int] = {}
            for item in self.failures:
                summary[item["code"]] = summary.get(item["code"], 0) + 1
            return summary

    def failure_samples(self, limit: int = 8) -> list[dict]:
        with self._lock:
            return list(self.failures[:limit])


def configured_regions(config, windows=None) -> tuple[list, dict[str, list[str]]]:
    """挑出**用户已配置并确认**的区域；返回（可用区域，被拒绝的 {别名: 原因}）。

    判据全部来自用户配置本身（``previewed`` / ``needs_calibration`` / HWND /
    参考帧 / ROI 越界）与窗口身份核对（HWND 是否存在、PID/路径/类名/标题是否仍
    一致）。这里**不枚举**任何窗口，也不会为了凑数生成 ROI。
    """
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


def _probe_region(width: int, height: int):
    """探针 ROI：仅用于 ``--source probe`` 明确要求的诊断，不写入配置。"""
    return real_env.probe_region(width, height)


def _probe_regions(targets, frame_sizes: dict[str, tuple[int, int]]):
    """按探针 ROI 生成内存中的 RegionConfig（仅诊断旁路，需要显式开关）。"""
    from deal_watch.models import Rect, RegionConfig

    regions = []
    for target in targets:
        width, height = frame_sizes[f"0x{target.hwnd:x}"]
        roi = _probe_region(width, height)
        regions.append(
            RegionConfig(
                alias=target.title[:24] or f"窗口{target.hwnd}",
                window_title=target.title,
                rect=Rect(roi.x, roi.y, roi.width, roi.height),
                window_class=target.class_name,
                window_rect=target.rect,
                window_hwnd=target.hwnd,
                process_id=target.process_id,
                process_path=target.process_path,
                capture_backend="wgc",
                coordinate_space="frame",
                reference_frame_width=width,
                reference_frame_height=height,
                needs_calibration=False,
                enabled=True,
                monitor_id=real_env.DISPLAY1,
                # 仅内存标记：这是探针 ROI，不是用户确认过的 ROI；报告里明确标注。
                previewed=True,
            )
        )
    return regions


def _measure_frame_sizes(
    targets, capture_timeout: float = real_env.PRODUCTION_CAPTURE_TIMEOUT
) -> dict[str, tuple[int, int]]:
    """先用 WGC 取一次帧尺寸，作为探针 RegionConfig 的参考帧大小。"""
    sizes: dict[str, tuple[int, int]] = {}
    for target in targets:
        payload = real_env.worker_payload(target.hwnd, "wgc", timeout=capture_timeout)
        if payload.get("ok"):
            sizes[f"0x{target.hwnd:x}"] = (int(payload["width"]), int(payload["height"]))
    return sizes


def format_ocr_reuse(reuse: dict) -> str:
    """把 ``ocr_reuse`` 汇总格式化成一行（与报告读同一份真值）。

    实现放在 ``deal_watch.diagnostics.reuse_rate_line``：探针、界面和报告必须念
    同一句话，尤其"未观测"这条——无样本时绝不能写成 0%。
    """
    return reuse_rate_line(reuse)


def build_parser() -> argparse.ArgumentParser:
    """命令行参数；抽成独立函数便于测试默认值而不触碰真实桌面。"""
    parser = argparse.ArgumentParser(
        description=(
            "真实 60 秒调度稳定性记录。默认只跑用户已配置并确认的区域：不枚举其他"
            "微信窗口、不自动生成 ROI、不写用户配置；通知只发本机模拟飞书。"
        )
    )
    parser.add_argument("--rounds", type=int, default=MIN_ROUNDS, help="采样轮数，不得少于 10")
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=real_env.PRODUCTION_POLL_SECONDS,
        help="每轮间隔秒数（默认 30，与生产 poll_interval_ms 一致）",
    )
    parser.add_argument(
        "--source",
        choices=(SOURCE_CONFIG, SOURCE_PROBE),
        default=SOURCE_CONFIG,
        help=(
            "区域来源：config=用户已配置并确认的区域（默认）；"
            "probe=按标题枚举 DISPLAY1 微信窗口并生成探针 ROI（需 --allow-probe-roi）"
        ),
    )
    parser.add_argument(
        "--allow-probe-roi",
        action="store_true",
        help="显式允许 --source probe 生成探针 ROI；这种数据不能当作用户验收证据",
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
        "--capture-component", type=Path, default=None,
        help="指定同版冻结采集组件 EXE；默认使用当前源码组件，始终启用常驻模式",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=real_env.VALIDATION_DIR / f"real-wechat-schedule-{real_env.app_version()}.json",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _load_user_config():
    """只读加载用户配置；任何异常都返回 (None, 原因)，绝不写回。"""
    from deal_watch.config import ConfigStore

    try:
        store = ConfigStore()
        return store.load(), store.path, ""
    except Exception as error:  # noqa: BLE001 - 配置损坏不能变成"没有区域"
        return None, None, f"{type(error).__name__}: {error}"[:300]


def temporary_storage():
    """Real chat SQLite contents must never be created in the system temp directory."""
    base = ROOT / "build" / "validation025"
    base.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="real-service-", dir=base)


def _region_report_entry(index: int, region, persisted: bool) -> dict:
    return {
        "label": f"region-{index}",
        # region_id 是 target_sequences / submitted / processed_sequences 的键：
        # 报告内部各表因此能直接对齐，不需要靠顺序猜。
        "region_id": region.id,
        "hwnd": f"0x{region.window_hwnd:x}",
        # 报告里不放窗口标题明文：用序号 + sha256 标识区域。
        "title_sha256": real_env.text_digest(region.window_title),
        "alias_sha256": real_env.text_digest(region.alias),
        "roi": {
            "x": region.rect.x,
            "y": region.rect.y,
            "width": region.rect.width,
            "height": region.rect.height,
        },
        "reference_frame": [region.reference_frame_width, region.reference_frame_height],
        "capture_backend": region.capture_backend,
        "monitor": real_env.monitor_device(region.window_hwnd),
        "previewed": bool(region.previewed),
        "persisted": persisted,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.output.resolve().is_relative_to((ROOT / "build").resolve()):
        print("真实验收报告必须放在 build/ 内（退出码 2）。")
        return 2
    if args.capture_component is not None and not args.capture_component.is_file():
        print("指定的采集组件不存在，未启动调度（退出码 2）。")
        return 2
    configure_capture_stage_log(args.output.parent / "capture-diagnostics")

    if args.rounds < MIN_ROUNDS:
        print(
            f"真实 60 秒调度至少需要 {MIN_ROUNDS} 轮证据，收到 --rounds {args.rounds}；"
            "已拒绝执行（退出码 2）。"
        )
        return 2
    if args.interval_seconds <= 0:
        print("--interval-seconds 必须为正数（退出码 2）。")
        return 2
    if args.source == SOURCE_PROBE and not args.allow_probe_roi:
        print(
            "已拒绝执行（退出码 2）：--source probe 会枚举微信窗口并生成探针 ROI，"
            "必须同时显式给出 --allow-probe-roi；默认请使用 --source config。"
        )
        return 2

    # 溯源要在开跑前采集：这份报告对应的是"此刻的代码 + 此刻的工作区状态"。
    git_info = git_provenance(ROOT)

    real_env.require_windows()
    real_env.enable_dpi_awareness()

    from deal_watch.config import AppConfig

    rejected: dict[str, list[str]] = {}
    keywords: list[str] = []
    ocr_reuse_age = 300.0
    started_at = time.time()
    if args.source == SOURCE_CONFIG:
        loaded, config_path, load_error = _load_user_config()
        if loaded is None:
            print(f"读取用户配置失败（{load_error or '未知原因'}），未启动调度（退出码 2）。")
            return 2
        from deal_watch.capture import WindowsWindows

        regions, rejected = configured_regions(loaded, WindowsWindows())
        keywords = list(loaded.keywords)
        ocr_reuse_age = float(loaded.ocr_reuse_max_age_seconds)
        if not regions:
            print(
                "用户配置里没有「已启用并预览确认」的区域：未启动调度（退出码 2）。"
                "请在应用里圈选并预览确认后再跑本工具；本工具不会自动枚举窗口或生成 ROI。"
            )
            for alias, problems in rejected.items():
                print(f"  {alias}: {'；'.join(problems)}")
            return 2
        capture_mode = CAPTURE_MODE_CONFIG
        roi_source = "user_config"
        user_confirmed = True
        acceptance_eligible = True
        persisted = True
        print(f"只读加载用户配置：{config_path}（不写入）")
    else:
        targets = real_env.display1_weixin_windows(limit=4)
        if not targets:
            print("未发现完全位于 DISPLAY1 内的 Weixin.exe 窗口，未启动调度，已安全退出。")
            return 2
        frame_sizes = _measure_frame_sizes(targets, args.capture_timeout)
        missing = [
            f"0x{target.hwnd:x}" for target in targets if f"0x{target.hwnd:x}" not in frame_sizes
        ]
        if missing:
            print(f"以下窗口取不到 WGC 帧尺寸，未启动调度：{', '.join(missing)}")
            return 2
        regions = _probe_regions(targets, frame_sizes)
        keywords = real_env.read_keywords()
        capture_mode = CAPTURE_MODE_PROBE
        roi_source = "probe_generated"
        user_confirmed = False
        acceptance_eligible = False
        persisted = False
        print(
            "[诊断旁路] 使用探针 ROI，不是用户确认的区域；"
            "这份报告不能作为用户验收证据。"
        )

    from deal_watch.notify import FeishuClient, NotificationDispatcher
    from deal_watch.ocr import LocalOcr
    from deal_watch.recognition import RecognitionStore
    from deal_watch.service import MonitorService
    from deal_watch.storage import Storage

    # Fail before starting any components if a configured window is on G72 or
    # straddles DISPLAY1; RecordingCapturer repeats this check for every sample.
    for region in regions:
        RecordingCapturer.require_display1(region)
    temp = temporary_storage()
    server, port = real_env.start_mock_feishu()
    webhook = f"http://127.0.0.1:{port}/ok"
    app_config = AppConfig(
        regions=regions,
        keywords=keywords,
        poll_interval_ms=max(100, int(args.interval_seconds * 1000)),
        save_hit_images=False,
        max_queue_size=4,
        ocr_reuse_max_age_seconds=ocr_reuse_age,
    )
    storage = Storage(Path(temp.name) / "events.db")
    recognition = RecognitionStore(Path(temp.name))
    recognition.prepare()
    capturer = RecordingCapturer(args.capture_timeout, component_path=args.capture_component)
    # 一个 recorder 同时给 service（采集/队列/跟踪/写库）与 OCR/通知观测：
    # 报告里的分段因此是同一时间轴上的同一批样本。
    recorder = TimingRecorder()
    completion = RoundCompletion()
    # 完成标记的权威来源是**服务层**回调：复用命中轮不进 LocalOcr（观测回调不会
    # 触发），但帧照样走完跟踪/规则/写库。2026-11-05 十轮真机首跑暴露过这个坑：
    # 标记挂在 OcrStageRecorder.on_complete 上时，73% 复用率的运行会让
    # round_settled_by_sequence 永远等不到序号。观测回调只留分段耗时。
    ocr_observer = OcrStageRecorder(recorder)
    ocr = LocalOcr(real_env.model_config(), ROOT, observer=ocr_observer)
    notify_metrics = NotifyMetrics(recorder)
    latest_health: dict[str, object] = {}

    def on_health(snapshot) -> None:
        latest_health[snapshot.region_id] = snapshot

    service = MonitorService(
        app_config,
        storage,
        ocr,
        capturer=capturer,
        data_dir=Path(temp.name),
        on_health=on_health,
        metrics=recorder,
        on_frame_processed=lambda region_id, seq: completion.mark(region_id, seq),
        recognition_store=recognition,
    )
    dispatcher = NotificationDispatcher(
        storage,
        lambda: FeishuClient(webhook, "", timeout=3.0),
        metrics=notify_metrics,
    )

    region_ids = [region.id for region in regions]
    hwnd_by_region = {region.id: f"0x{region.window_hwnd:x}" for region in regions}
    report: dict = {
        "kind": "real_wechat_schedule_stability",
        "version": real_env.app_version(),
        "service_runtime": "source",
        "capture_runtime": "frozen_component" if args.capture_component else "source",
        "capture_component_sha256": (
            hashlib.sha256(args.capture_component.read_bytes()).hexdigest()
            if args.capture_component else None
        ),
        "persistent_capture": True,
        # 代码来源：source_commit 是完整 HEAD；source_dirty=True 表示跑这份报告时
        # 工作区有 tracked 修改（此时指纹也对不上任何提交）。Git 不可用时两者为 null。
        "source_commit": git_info["source_commit"],
        "source_dirty": git_info["source_dirty"],
        "started_at": started_at,
        "interval_seconds": float(args.interval_seconds),
        "rounds_requested": int(args.rounds),
        "mock_webhook_port": port,
        "dpi_expected": real_env.DPI_EXPECTED,
        "capture_mode": capture_mode,
        "roi_source": roi_source,
        "user_confirmed_roi": user_confirmed,
        "acceptance_eligible": acceptance_eligible,
        "region_count": len(regions),
        "regions_rejected": rejected,
        "regions": [
            _region_report_entry(index, region, persisted)
            for index, region in enumerate(regions, 1)
        ],
        "read_only": True,
        "sends_wechat_message": False,
        "writes_user_config": False,
        "rounds": [],
        "checks": [],
    }
    # 采集参数必须写进报告：报告自己说明这是不是生产参数。
    report.update(
        real_env.production_param_flags(args.capture_timeout, poll_interval=args.interval_seconds)
    )
    report["source_fingerprint"] = source_fingerprint(
        [ROOT / name for name in FINGERPRINT_FILES], root=ROOT
    )
    # 只做记录：AGENTS 要求占用屏幕的测试只在 DISPLAY1；本工具不占屏，但报告要能
    # 说明这批数据到底来自哪块屏，不能靠人猜。
    report["all_regions_on_display1"] = all(
        str(entry["monitor"]).upper() == real_env.DISPLAY1.upper()
        for entry in report["regions"]
    )
    print(
        f"启动调度：{len(regions)} 个区域（{capture_mode}），"
        f"间隔 {args.interval_seconds}s，共 {args.rounds} 轮"
    )
    print(f"模拟飞书：http://127.0.0.1:{port}/ok（仅本机）")
    print(f"临时存储：{storage.path}")

    errors: list[str] = []
    previous_dropped = 0
    previous_round_start: float | None = None
    last_targets: dict[str, int] = {}
    unsettled_rounds: list[int] = []
    try:
        service.start()
        dispatcher.start()
        for round_index in range(1, args.rounds + 1):
            round_started = time.monotonic()
            before = capturer.counts()
            deadline = round_started + args.interval_seconds + ROUND_GRACE_SECONDS
            while time.monotonic() < deadline:
                current = capturer.counts()
                if all(current.get(rid, 0) > before.get(rid, 0) for rid in region_ids):
                    break
                time.sleep(0.2)
            submitted_elapsed = time.monotonic() - round_started
            # 本轮的目标序号 = 每个区域这一轮真正交出去的那一帧。
            targets = {rid: capturer.sequence_for(rid) for rid in region_ids}
            last_targets = dict(targets)
            # 完成条件：**各区域都处理到本轮序号** 且没有在途任务。
            # 只看队列深度会把"OCR 线程刚取走最后一帧"当成处理完了；
            # 只看旧健康快照更糟：那是上一拍的值。
            settle_budget = max(5.0, min(SETTLE_SECONDS, args.interval_seconds))
            settle = min(deadline, time.monotonic() + settle_budget)
            settlement_started = time.monotonic()
            while time.monotonic() < settle:
                if completion.is_settled(targets, service.in_flight()):
                    break
                time.sleep(0.1)
            pending_sequences = completion.pending(targets)
            settled = completion.is_settled(targets, service.in_flight())
            if not settled:
                unsettled_rounds.append(round_index)
            settled_after_ms = round((time.monotonic() - settlement_started) * 1000, 1)
            elapsed = time.monotonic() - round_started
            # 轮次节奏 = 与上一轮开始时刻的间隔（含等待处理），这是 60 秒调度的真实节拍。
            paced_ms = (
                None
                if previous_round_start is None
                else round((round_started - previous_round_start) * 1000, 1)
            )
            previous_round_start = round_started
            current = capturer.counts()
            submitted = {
                hwnd_by_region[rid]: current.get(rid, 0) - before.get(rid, 0)
                for rid in region_ids
            }
            dropped_total = sum(
                int(getattr(snapshot, "dropped_frames", 0))
                for snapshot in latest_health.values()
            )
            round_record = {
                "round": round_index,
                "started_at": time.time(),
                # elapsed_ms = 各区域都提交完毕所需时间（调度口径）；
                # settled_ms 含等待"处理到本轮序号"，单独记录便于核对。
                "elapsed_ms": round(submitted_elapsed * 1000, 1),
                "settled_ms": round(elapsed * 1000, 1),
                "settle_wait_ms": settled_after_ms,
                "paced_ms": paced_ms,
                "submitted": submitted,
                # 本轮每区域的目标序号与实际处理完成情况：验收以这张表为准。
                "target_sequences": targets,
                "pending_sequences": pending_sequences,
                "settled_by_sequence": bool(settled),
                "regions": {},
                "queue_counts": storage.queue_counts(),
                "service_alive": bool(service.is_alive()),
                "dispatcher_alive": bool(dispatcher.is_alive()),
                # 本轮结束时刻的处理中任务数：队列为空不等于处理完成。
                "in_flight": int(recorder.in_flight),
                "dropped_frames_total": dropped_total,
                "dropped_frames_round": max(0, dropped_total - previous_dropped),
                "capture_processes": capturer.client.worker_stats(),
                "recognition_record_count": recognition.count(),
            }
            previous_dropped = dropped_total
            for rid in region_ids:
                snapshot = latest_health.get(rid)
                round_record["regions"][hwnd_by_region[rid]] = {
                    "status": str(getattr(getattr(snapshot, "status", ""), "value", "")),
                    "detail": str(getattr(snapshot, "detail", ""))[:80],
                    "last_ocr_ms": round(float(getattr(snapshot, "last_ocr_ms", 0.0)), 1),
                    "queue_depth": int(getattr(snapshot, "queue_depth", 0)),
                }
            report["rounds"].append(round_record)
            print(
                f"第 {round_index} 轮：{round_record['elapsed_ms']:.0f}ms "
                f"提交 {list(submitted.values())} "
                f"序号 {list(targets.values())} 处理完={settled} "
                f"队列 {round_record['queue_counts']} "
                f"丢弃帧 本轮{round_record['dropped_frames_round']}/累计{dropped_total} "
                f"service={round_record['service_alive']} "
                f"dispatcher={round_record['dispatcher_alive']}"
            )
    except Exception as error:  # 任何未预期异常都要落盘，便于复查
        errors.append(f"{type(error).__name__}: {error}"[:300])
    finally:
        dispatcher.request_stop()
        service.request_stop()
        try:
            dispatcher.stop()
        except Exception:
            pass
        try:
            service.stop()
        except Exception:
            pass
        recognition.close()
        storage.close()
        server.shutdown()
        server.server_close()
        temp.cleanup()

    rounds = report["rounds"]
    elapsed_values = [record["elapsed_ms"] for record in rounds]
    settled_values = [record["settled_ms"] for record in rounds]
    paced_values = [record["paced_ms"] for record in rounds if record["paced_ms"] is not None]
    ocr_values = [
        region["last_ocr_ms"]
        for record in rounds
        for region in record["regions"].values()
        if region["last_ocr_ms"] > 0
    ]
    interval_ms = args.interval_seconds * 1000
    failures = capturer.failure_summary()
    hard_failures = {
        code: count for code, count in failures.items() if code not in PAUSE_CODES
    }
    paused = sum(count for code, count in failures.items() if code in PAUSE_CODES)
    all_rounds = len(rounds) == args.rounds
    every_region_each_round = all(
        all(count > 0 for count in record["submitted"].values()) for record in rounds
    )
    all_settled = not unsettled_rounds
    capture_failures = sum(hard_failures.values())
    within_interval = all(
        value <= interval_ms + SCHEDULE_JITTER_MS for value in elapsed_values
    )
    cadence = capturer.cadence()
    cadence_by_hwnd = {
        hwnd_by_region[region_id]: cadence.get(region_id, {}) for region_id in region_ids
    }
    cadence_max = max(
        (item.get("max_ms", 0.0) for item in cadence_by_hwnd.values()), default=0.0
    )
    cadence_ok = all(
        item.get("max_ms", 0.0) <= interval_ms + SCHEDULE_JITTER_MS
        for item in cadence_by_hwnd.values()
    )
    queue_depth_ok = all(
        region["queue_depth"] <= 4 for record in rounds for region in record["regions"].values()
    )
    threads_alive = all(
        record["service_alive"] and record["dispatcher_alive"] for record in rounds
    )
    checks = [
        {"name": "rounds_completed", "passed": all_rounds,
         "detail": f"{len(rounds)}/{args.rounds}"},
        {"name": "every_region_submitted_each_round", "passed": every_region_each_round,
         "detail": submit_check_detail(len(region_ids))},
        # 验收以"本轮各区域对应序号处理完成"为准；队列空 / 旧健康快照都不算。
        {"name": "round_settled_by_sequence", "passed": all_settled,
         "detail": (
             "每轮都等到各区域处理到本轮序号"
             if all_settled
             else f"未完成的轮次：{unsettled_rounds}"
         )},
        {"name": "capture_failures_zero", "passed": capture_failures == 0,
         "detail": f"硬失败 {capture_failures} 次；暂停类 {paused} 次 {sorted(hard_failures)}"},
        {"name": "round_within_interval", "passed": within_interval,
         "detail": f"max={max(elapsed_values) if elapsed_values else 0:.0f}ms "
                   f"interval={interval_ms:.0f}ms + 抖动 {SCHEDULE_JITTER_MS}ms"},
        {"name": "region_cadence_within_interval", "passed": cadence_ok,
         "detail": f"同一区域相邻提交最大间隔 max={cadence_max:.0f}ms "
                   f"interval={interval_ms:.0f}ms + 抖动 {SCHEDULE_JITTER_MS}ms"
                   "（轮次节奏含本工具等待处理的时间，仅作参考不参与断言）"},
        {"name": "queue_depth_at_most_4", "passed": queue_depth_ok,
         "detail": f"max={max((r['queue_depth'] for record in rounds for r in record['regions'].values()), default=0)}"},
        {"name": "no_thread_death", "passed": threads_alive, "detail": "service/dispatcher 全程存活"},
    ]
    if not acceptance_eligible:
        checks.append({
            "name": "user_confirmed_regions",
            "passed": False,
            "detail": "诊断旁路使用探针 ROI，不是用户确认的区域；不得记为验收通过",
        })
    if errors:
        checks.append({"name": "no_unexpected_exception", "passed": False, "detail": errors[0]})
    report["checks"] = checks
    report["summary"] = {
        "rounds_reported": len(rounds),
        "round_elapsed_ms_p50": _percentile(elapsed_values, 0.5),
        "round_elapsed_ms_p95": _percentile(elapsed_values, 0.95),
        "round_settled_ms_p50": _percentile(settled_values, 0.5),
        "round_settled_ms_p95": _percentile(settled_values, 0.95),
        "round_paced_ms_p50": _percentile(paced_values, 0.5),
        "round_paced_ms_p95": _percentile(paced_values, 0.95),
        "ocr_ms_p50": _percentile(ocr_values, 0.5),
        "ocr_ms_p95": _percentile(ocr_values, 0.95),
        "dropped_frames_total": max(
            (record["dropped_frames_total"] for record in rounds), default=0
        ),
        "dropped_frames_rounds_sum": sum(
            record["dropped_frames_round"] for record in rounds
        ),
        "capture_failures": capture_failures,
        "paused_captures": paused,
        "failure_codes": failures,
        "capture_failure_samples": capturer.failure_samples(),
        "region_cadence": cadence_by_hwnd,
        "final_queue_counts": rounds[-1]["queue_counts"] if rounds else {},
        "unsettled_rounds": unsettled_rounds,
    }
    # 0.2.2 T2.1：分段耗时。字段名与 models.HealthSnapshot.stages 一致，便于对照。
    report["stage_timings"] = recorder.snapshot()
    report["stage_notes"] = dict(STAGE_PENDING_REASONS)
    report["stage_rows"] = {
        region_id: stage_timing_rows(recorder.region_payload(region_id))
        for region_id in region_ids
    }
    # 分段观测的样本量：证明确实有"纯引擎/预处理/后处理/真实初始化"四类样本，
    # 而不是把 inference_ms 换个名字重复计数。
    report["stage_sample_counts"] = {
        stage: sum(
            recorder.stats(stage, region_id).total for region_id in region_ids
        )
        for stage in (
            metrics.STAGE_PREPROCESS,
            metrics.STAGE_MODEL_INIT,
            metrics.STAGE_OCR_ENGINE,
            metrics.STAGE_OCR_INFER,
            metrics.STAGE_POSTPROCESS,
            metrics.STAGE_NOTIFY_QUEUE,
            metrics.STAGE_NOTIFY_RATE_LIMIT,
            metrics.STAGE_NOTIFY_SEND,
        )
    }
    report["ocr_reuse"] = service.ocr_reuse_summary()
    report["notify"] = notify_metrics.as_dict()
    # 处理完成序号的最终快照：targets 是最后一轮的目标，completed 是实际完成到的序号。
    report["processed_sequences"] = completion.snapshot(last_targets)
    report["finished_at"] = time.time()
    # 统计起止点：分段样本只覆盖这个区间，区间外的样本不在这份报告里。
    report["stats_window"] = {
        "started_at": started_at,
        "finished_at": report["finished_at"],
        "first_round_started_at": rounds[0]["started_at"] if rounds else None,
        "last_round_started_at": rounds[-1]["started_at"] if rounds else None,
        "rounds": len(rounds),
        "note": "分段耗时/复用/通知统计只覆盖 started_at–finished_at 之间的样本",
    }
    # 验收 6：埋点开销。120 个周期 × 每周期的写入次数，离线微基准；只提醒不设门限。
    report["metrics_overhead"] = metrics.measure_overhead(120)
    report["read_only_notes"] = [
        "未发送任何微信消息；通知只发到 http://127.0.0.1:<随机端口>/ok 模拟飞书",
        "未点击、滚动或输入",
        "未移动或缩放被监控窗口",
        "未写入用户 config.json 或数据库（隔离事件与识别库只在 build/validation025/ 临时目录）",
        (
            "区域来自用户已配置并确认的 config.json（只读）"
            if user_confirmed
            else "区域来自探针 ROI（诊断旁路），不是用户确认；报告已标记不可用于验收"
        ),
    ]

    print("")
    print(
        "轮次 开始时间   调度耗时   本轮耗时  轮次节奏  区域提交             队列            丢弃帧(本轮/累计)"
    )
    for record in rounds:
        paced = "-" if record["paced_ms"] is None else f"{record['paced_ms']:.0f}ms"
        print(
            f"{record['round']:>3}  {time.strftime('%H:%M:%S', time.localtime(record['started_at']))}"
            f"  {record['elapsed_ms']:>8.0f}ms  {record['settled_ms']:>8.0f}ms  {paced:>8}"
            f"  {list(record['submitted'].values())}"
            f"  {record['queue_counts']}  "
            f"{record['dropped_frames_round']}/{record['dropped_frames_total']}"
        )
    print("")
    for check in checks:
        print(("[PASS] " if check["passed"] else "[FAIL] ") + check["name"] + f"：{check['detail']}")
    print(
        f"P50/P95：调度 {report['summary']['round_elapsed_ms_p50']}/"
        f"{report['summary']['round_elapsed_ms_p95']}ms；"
        f"含处理 {report['summary']['round_settled_ms_p50']}/"
        f"{report['summary']['round_settled_ms_p95']}ms；"
        f"轮次节奏 {report['summary']['round_paced_ms_p50']}/"
        f"{report['summary']['round_paced_ms_p95']}ms；"
        f"OCR {report['summary']['ocr_ms_p50']}/{report['summary']['ocr_ms_p95']}ms"
    )
    for region_id in region_ids:
        payload = recorder.region_payload(region_id)
        print("")
        print(
            f"分段耗时 region={hwnd_by_region[region_id]}"
            f"（cold 由真实初始化/首次调用显式标记，cold 桶容量 {COLD_BUCKET_CAPACITY}）"
        )
        for line in format_stage_timings(payload, STAGE_PENDING_REASONS, include_pending=False):
            print("  " + line)
        for row in stage_timing_rows(payload):
            if row["samples"]:
                print(f"    起止点 {row['stage']}：{row['start']} → {row['end']}")
    print("")
    print(
        f"in_flight 峰值 {recorder.peak_in_flight}；"
        f"周期末采样 {recorder.in_flight_samples} 次"
        "（in_flight = 采集队列长度 + OCR 正在处理的帧数，用于区分'队列空了'与'处理完了'）"
    )
    print(
        f"处理完成序号：{report['processed_sequences']['completed']}"
        f"（本轮目标 {report['processed_sequences']['targets']}）"
    )
    for stage, reason in STAGE_PENDING_REASONS.items():
        if not report["stage_sample_counts"].get(stage, 0):
            print(f"  本期无样本 {stage}：{reason}")
    reuse = report["ocr_reuse"]
    print("")
    print(format_ocr_reuse(reuse))
    notify = report["notify"]
    print(
        f"通知：请求 {notify['requests']} 次 {notify['request_outcomes']}；"
        f"结果 {notify['job_outcomes']}"
        "（排队/限速/请求耗时分别为 notify_queue / notify_rate_limit / notify_send 段）"
    )
    overhead = report["metrics_overhead"]
    print("")
    print(
        f"埋点开销：{overhead['cycles']} 周期 × {overhead['calls_per_cycle']} 次写入，"
        f"合计 {overhead['total_ms']:.1f}ms，单周期 {overhead['per_cycle_ms']:.3f}ms"
    )
    if overhead["per_cycle_ms"] > overhead["budget_ms"]:
        print(
            f"  [注意] 单周期埋点 > {overhead['budget_ms']:.0f}ms，"
            "已超过提醒门槛（不是硬门限）：需在交付说明里写明并核对是否挤占调度预算"
        )
    real_env.write_report(args.output, report)
    return 0 if all(check["passed"] for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
