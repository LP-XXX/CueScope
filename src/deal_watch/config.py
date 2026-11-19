"""版本化配置与原子保存；敏感凭据不进入 JSON。"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .model_catalog import load_model_defaults
from .models import RegionConfig
from .rule_config import RuleGroup

APP_NAME = "DealWatch"
CONFIG_VERSION = 4

# 保存失败后的一次重试间隔（0.2.2 设计决策 Q2：按“可能被占用”设计，
# 300ms 后重试一次，仍失败再提示把数据目录移出同步盘/加入杀软白名单）。
SAVE_RETRY_DELAY_SECONDS = 0.3

# 失败阶段到中文原因的映射；日志与界面文案都从这里取，避免两套口径。
# ``verify`` 校验的是**临时文件**（0.2.2 D3 修复：校验前移到 ``os.replace`` 之前），
# 因此文案必须写明“正式配置未被替换”，否则用户会以为文件已经被改过。
SAVE_STAGE_LABELS: dict[str, str] = {
    "mkdir": "创建数据目录失败",
    "write": "写入临时配置文件失败",
    "replace": "替换正式配置文件失败",
    "verify": "临时配置文件回读校验失败（正式配置未被替换）",
}
SAVE_RETRY_HINT = "请把程序数据目录移出同步盘（OneDrive/坚果云等）或加入杀毒软件白名单后重试"


class ConfigSaveError(OSError):
    """配置文件保存失败；``stage`` 指出失败发生在哪一步。

    继承 ``OSError`` 以保持 0.2.1 的向后兼容：现有的 ``except OSError``
    调用点无需修改即可继续工作。
    """

    def __init__(
        self,
        stage: str,
        path: Path,
        original: BaseException | None = None,
        attempts: int = 1,
    ) -> None:
        self.stage = stage
        self.path = Path(path)
        self.original = original
        self.attempts = attempts
        reason = SAVE_STAGE_LABELS.get(stage, stage)
        detail = f"{type(original).__name__}: {original}" if original is not None else "未知原因"
        super().__init__(f"配置保存失败（{reason}；{detail}）。{SAVE_RETRY_HINT}。")


def validate_webhook_url(webhook: str) -> None:
    """仅接受飞书 HTTPS Webhook 或显式本机模拟地址；错误不包含凭据。"""
    try:
        parsed = urlsplit(webhook)
        port = parsed.port
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError
        if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
            if parsed.scheme not in {"http", "https"}:
                raise ValueError
            return
        if (
            parsed.scheme != "https"
            or parsed.hostname != "open.feishu.cn"
            or port not in {None, 443}
            or not re.fullmatch(r"/open-apis/bot/v2/hook/[A-Za-z0-9_-]+", parsed.path)
        ):
            raise ValueError
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("Webhook 必须是飞书 HTTPS 机器人地址或本机模拟地址") from error


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / APP_NAME if base else Path.home() / f".{APP_NAME.lower()}"


_MODEL_DEFAULTS = load_model_defaults()


@dataclass(slots=True)
class ModelConfig:
    det_path: str = _MODEL_DEFAULTS["det_path"]
    rec_path: str = _MODEL_DEFAULTS["rec_path"]
    keys_path: str = _MODEL_DEFAULTS["keys_path"]
    det_sha256: str = _MODEL_DEFAULTS["det_sha256"]
    rec_sha256: str = _MODEL_DEFAULTS["rec_sha256"]
    keys_sha256: str = _MODEL_DEFAULTS["keys_sha256"]
    intra_op_threads: int = 4
    inter_op_threads: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.intra_op_threads <= 16 or self.inter_op_threads != 1:
            raise ValueError("OCR CPU 线程须为 1–16，跨算子线程固定为 1")


@dataclass(slots=True)
class AppConfig:
    version: int = CONFIG_VERSION
    regions: list[RegionConfig] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    webhook: str = ""
    save_hit_images: bool = True
    auto_start: bool = False
    poll_interval_ms: int = 30000
    recognition_record_limit: int = 20000
    force_ocr_seconds: float = 10.0
    # 0.2.2 T2.3：静止画面 OCR 复用的最长有效期（秒）。像素完全相同时复用上一次
    # 的文字结果，跳过 ONNX 推理。**0 = 关闭复用**，行为与 0.2.1 完全一致。
    # 注意：与 ``force_ocr_seconds`` 是两套语义——后者是"多久强制重算一次"，
    # 不要用其中一个去判断另一个，也不要在生产 WGC 路径上混用。
    ocr_reuse_max_age_seconds: float = 300.0
    # 0.2.2 T2.5：把消息区域（ROI）下推给采集组件——子进程收帧即裁、只编码传输
    # ROI（整帧先照常采、黑帧/空白帧判定口径不变）。False 时完全回到 0.2.1：
    # 请求整帧 + 父进程本地裁。圈选/校准预览永远取完整帧，不受这个开关影响。
    capture_roi_mode: bool = True
    dedupe_seconds: int = 300
    event_ttl_seconds: int = 300
    max_queue_size: int = 4
    match_mode: str = "char_multiset"
    model: ModelConfig = field(default_factory=ModelConfig)
    rule_groups: list[RuleGroup] = field(default_factory=list)
    notification_mode: str = "auto"

    def __post_init__(self) -> None:
        if self.version < 3:
            raise ValueError("配置版本过旧，必须经过 v3 迁移加载")
        if self.poll_interval_ms < 100:
            raise ValueError("采集间隔不能小于 100ms")
        if self.match_mode not in {"char_multiset", "literal"}:
            raise ValueError("匹配模式必须是 char_multiset 或 literal")
        if self.max_queue_size < 1:
            raise ValueError("OCR 队列容量必须为正数")
        if self.notification_mode not in {"auto", "local", "feishu"}:
            raise ValueError("未知通知方式")
        group_ids = [group.id for group in self.rule_groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("规则组标识重复")
        if self.ocr_reuse_max_age_seconds < 0:
            raise ValueError("静止画面 OCR 复用上限不能为负数（0 表示关闭复用）")
        if (
            isinstance(self.recognition_record_limit, bool)
            or not isinstance(self.recognition_record_limit, int)
            or self.recognition_record_limit < 1
        ):
            raise ValueError("识别记录上限必须为正整数")


class ConfigStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or default_data_dir()
        self.path = self.data_dir / "config.json"

    def load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        loaded_version = raw.get("version")
        if loaded_version not in {1, 2, 3, CONFIG_VERSION}:
            raise ValueError(f"不支持的配置版本：{loaded_version}")
        raw["version"] = CONFIG_VERSION
        if loaded_version == 1:
            # v1 桌面绝对坐标不能安全转换到 WGC 帧坐标；保留别名并要求重新圈选。
            if raw.get("poll_interval_ms", 60000) == 60000:
                raw["poll_interval_ms"] = 30000
            raw["match_mode"] = "char_multiset"
        if loaded_version == 2 and raw.get("poll_interval_ms") == 60000:
            raw["poll_interval_ms"] = 30000
        raw.pop("learning_enabled", None)
        raw["regions"] = [RegionConfig.from_dict(item) for item in raw.get("regions", [])]
        if loaded_version == 1:
            for region in raw["regions"]:
                region.coordinate_space = "desktop"
                region.needs_calibration = True
                region.window_hwnd = 0
                region.process_id = 0
                region.capture_backend = "wgc"
        raw["model"] = ModelConfig(**raw.get("model", {}))
        raw["rule_groups"] = [RuleGroup.from_dict(item) for item in raw.get("rule_groups", [])]
        config = AppConfig(**raw)
        # Webhook 只从 DPAPI 凭据存储恢复；旧配置中的明文凭据必须移除。
        config.webhook = ""
        return config

    def _backup_before_migration(self) -> None:
        if not self.path.exists():
            return
        original = self.path.read_bytes()
        version = json.loads(original).get("version", 0)
        if version >= CONFIG_VERSION:
            return
        backup = self.path.with_name(f"config.pre-v{CONFIG_VERSION}.json")
        # Exclusive creation retains the first recoverable configuration.
        try:
            with backup.open("xb") as handle:
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            previous = json.loads(backup.read_bytes())
            if not isinstance(previous, dict) or previous.get("version") not in {1, 2, 3}:
                raise ValueError("升级备份无效，原配置未覆盖，请先检查备份文件") from None

    def save(self, config: AppConfig, *, verify: bool = False) -> None:
        """原子保存配置；失败抛 :class:`ConfigSaveError`（``OSError`` 子类）。

        顺序固定：序列化 → 写临时文件（名字带 ``pid`` 与 ``uuid``）→（可选）回读
        校验**临时文件** → ``os.replace`` 原子替换正式文件。``verify`` 只在“保存并
        确认”流程打开，热路径默认关闭以避免额外 IO。

        **不变量 W-1**：正式文件 ``config.json`` 只会被 ``os.replace`` 一次性替换，
        而 ``os.replace`` 之前的所有步骤（写临时文件、回读校验）失败都只丢弃临时
        文件。因此“要么全成功、要么原文件一个字节都不动”，调用方不需要为
        “已写入但校验失败”写任何特殊文案（0.2.2 D3）。

        失败后 300ms 重试一次（针对杀软/同步盘瞬时占用），仍失败才向上抛。
        """
        raw = asdict(config)
        raw["version"] = CONFIG_VERSION
        raw["webhook"] = ""
        raw.pop("learning_enabled", None)
        payload = json.dumps(raw, ensure_ascii=False, indent=2)
        try:
            self._backup_before_migration()
        except (OSError, ValueError) as error:
            raise ConfigSaveError("verify", self.path, error) from error
        attempts = 2
        last_error: ConfigSaveError | None = None
        for attempt in range(1, attempts + 1):
            try:
                self._write_payload(payload, raw, verify=verify, attempt=attempt)
            except ConfigSaveError as error:
                last_error = error
                if attempt < attempts:
                    time.sleep(SAVE_RETRY_DELAY_SECONDS)
                continue
            return
        assert last_error is not None  # 循环要么 return 要么记下最后一次错误
        raise last_error

    def _write_payload(
        self, payload: str, raw: dict, *, verify: bool, attempt: int
    ) -> None:
        """单次写入尝试；任何一步失败都转成带阶段标记的 ``ConfigSaveError``。

        正式文件只被 ``os.replace`` 触碰一次，其余步骤失败都只是丢弃临时文件，
        所以任何 ``ConfigSaveError`` 都意味着“``config.json`` 没有被改动”。
        """
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ConfigSaveError("mkdir", self.data_dir, error, attempt) from error
        # 临时文件名必须唯一：固定名 ``config.tmp`` 在上一轮崩溃后会残留，
        # 且可能被同步盘/杀软持有句柄（0.2.2 假设 H2）。
        temporary = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        )
        try:
            temporary.write_text(payload, encoding="utf-8")
        except OSError as error:
            self._discard(temporary)
            raise ConfigSaveError("write", temporary, error, attempt) from error
        # D3：回读校验必须发生在替换之前。校验不过就丢弃临时文件后重试，
        # 正式配置保持原样 —— 不再存在“磁盘是新的、内存是旧的”这种例外状态。
        try:
            if verify:
                self._verify(raw, attempt, temporary)
        except Exception:
            self._discard(temporary)
            raise
        try:
            os.replace(temporary, self.path)
        except OSError as error:
            # 替换失败必须清掉临时文件，否则下次保存会看到一堆残留 tmp。
            self._discard(temporary)
            raise ConfigSaveError("replace", temporary, error, attempt) from error

    def _verify(self, raw: dict, attempt: int, path: Path | None = None) -> None:
        """回读刚写好的文件并与内存快照逐字段比较。

        ``path`` 缺省为正式配置文件，实际保存流程传入的是**临时文件**
        （``os.replace`` 之前校验）。任何异常都收敛成 ``verify`` 阶段错误，
        便于界面给出可操作的原因。
        """
        target = self.path if path is None else Path(path)
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ConfigSaveError("verify", target, error, attempt) from error
        except Exception as error:  # noqa: BLE001 - 回读是防御性检查，异常统一归到 verify
            raise ConfigSaveError("verify", target, error, attempt) from error
        if loaded != raw:
            raise ConfigSaveError(
                "verify", target, OSError("回读内容与写入内容不一致"), attempt
            )

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # 残留文件本身不是致命错误；下一次保存用新的唯一名字。
            pass
