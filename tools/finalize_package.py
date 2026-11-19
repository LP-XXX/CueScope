"""补齐交付文档、重建校验清单并压缩完整 onedir 目录。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import shutil
import sys
import tomllib
import zipfile
from pathlib import Path, PureWindowsPath

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
#: 脚本所在目录。导入同目录的 checksums 必须用它，不能用 ROOT —— 验收用例会把
#: ROOT / DIST 打桩到 tmp 目录跑 main()，跟着 ROOT 走就找不到 tools/checksums.py。
TOOLS_DIR = Path(__file__).resolve().parent


def project_version() -> str:
    """``pyproject.toml`` 的 ``project.version``：打包脚本唯一的版本真值源。

    0.2.1 时代 DIST / VERSION / 报告名各写一份字面量，升版必须三处同步；漏一处就
    会打出“目录叫旧版本、报告叫新版本”的错包。现在全部由这一处派生。
    """
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


VERSION = project_version()
EVIDENCE_DIR = None
DIST = ROOT / "dist" / f"CueScope-{VERSION}"
ARCHIVE = ROOT / "dist" / f"CueScope-{VERSION}-win-x64.zip"

#: 旧版本参考件的自证版本，仅用于 synthetic-schedule-0.2.0-reference.json。
REFERENCE_VERSION = "0.2.0"


def _versioned(stem: str) -> str:
    """带版本号的验证报告名；版本取自 :func:`project_version`。"""
    return f"{stem}-{VERSION}.json"


#: 包内 validation 报告清单：(源文件名, 包内文件名)。
#: 带版本号的条目由 :func:`_versioned` 生成；源文件缺失时跳过（真实环境报告可能尚未产出）。
VALIDATION_REPORTS: tuple[tuple[str, str], ...] = (
    (_versioned("package-smoke"), _versioned("package-smoke")),
    (_versioned("source-self-check"), _versioned("source-self-check")),
    (_versioned("performance"), _versioned("performance")),
    (_versioned("runtime-audit"), _versioned("runtime-audit")),
    (_versioned("capture-component-self-test"), _versioned("capture-component-self-test")),
    (_versioned("capture-pool-self-test"), _versioned("capture-pool-self-test")),
    ("automated-tests.json", "automated-tests.json"),
    ("source-snapshot.json", "source-snapshot.json"),
    ("build-info.json", "build-info.json"),
    (_versioned("clean-environment-check"), _versioned("clean-environment-check")),
    # 合成四窗闭环（自建窗口 + 生产 WGC 路径 + 模拟飞书）：必须带版本号入包。
    (_versioned("synthetic-four-window"), _versioned("synthetic-four-window")),
)

#: 不带版本号的旧自检/合成报告名；一旦出现在目标 validation/ 内即视为串版。
#: ``synthetic-four-window.json`` 是 0.2.0 时代的合成闭环，已被带版本号的同名报告
#: 取代，不得随当前版本的包分发。
FORBIDDEN_REPORT_NAMES = frozenset({
    "package-smoke.json", "synthetic-four-window.json", "attachment-token-probe.json",
    "wgc-service-60s.json", f"synthetic-schedule-{REFERENCE_VERSION}-reference.json",
})

#: 明确标注为旧版本参考件、按 REFERENCE_VERSION 校验的包内文件名。
REFERENCE_REPORT_NAMES = frozenset()


def collect_capture_licenses() -> list[dict]:
    """Resolve isolated wheel metadata and fail closed on missing or drifting licenses."""
    requirements = {}
    lock = ROOT / "packaging" / "capture-requirements.lock"
    for line in lock.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        key = re.sub(r"[-_.]+", "-", requirement.name).lower()
        pins = list(requirement.specifier)
        if (key in requirements or len(pins) != 1 or pins[0].operator != "=="
                or "*" in pins[0].version or requirement.marker or requirement.extras):
            raise ValueError("采集组件依赖锁必须逐项固定一个精确版本")
        requirements[key] = requirement
    if set(requirements) != {"numpy", "opencv-python", "windows-capture"}:
        raise ValueError("采集组件依赖锁必须包含 numpy、opencv-python、windows-capture")
    capture_runtime = ROOT / ".capture-runtime"
    isolated = {}
    for distribution in importlib.metadata.distributions(path=[str(capture_runtime)]):
        name = re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower()
        if name not in {"opencv-python", "windows-capture"}:
            continue
        if name in isolated:
            raise ValueError(f"采集运行环境含有重复的 {name} 发行元数据")
        isolated[name] = distribution
    records = []
    for name, requirement in requirements.items():
        # The .capture-runtime NumPy may differ and is deliberately never selected.
        distribution = importlib.metadata.distribution("numpy") if name == "numpy" else isolated.get(name)
        if distribution is None:
            raise FileNotFoundError(f"缺少采集组件依赖元数据：{name}")
        if distribution.version not in requirement.specifier:
            raise ValueError(f"采集组件 {name} 版本 {distribution.version} 与 {requirement} 不符")
        entries = list(distribution.files or [])
        if name == "opencv-python":
            wanted = {"cv2/LICENSE.txt", "cv2/LICENSE-3RD-PARTY.txt"}
            license_entries = [entry for entry in entries if entry.as_posix() in wanted]
            if {entry.as_posix() for entry in license_entries} != wanted:
                raise FileNotFoundError("opencv-python wheel 缺少完整许可证和第三方说明")
        else:
            license_entries = [
                entry for entry in entries
                if any(word in Path(entry).name.lower() for word in ("license", "licence", "copying", "notice"))
                or "/licenses/" in entry.as_posix().lower()
            ]
        if not license_entries:
            raise FileNotFoundError(f"采集组件 {name} wheel 未提供许可证文件")
        copies = []
        digests = {}
        for entry in license_entries:
            relative = Path(entry)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"采集组件 {name} 许可证路径超出发行目录")
            source = Path(distribution.locate_file(entry))
            if not source.is_file() or source.stat().st_size == 0:
                raise FileNotFoundError(f"采集组件 {name} 缺少有效许可证：{entry}")
            destination = DIST / "licenses" / "capture-runtime" / name / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copies.append(destination.relative_to(DIST).as_posix())
            digests[entry.as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
        records.append({
            "name": distribution.metadata["Name"], "version": distribution.version,
            "runtime": "capture-component", "license_files": sorted(copies),
            "metadata_source": "build-interpreter" if name == "numpy" else ".capture-runtime",
            "license_source_sha256": digests,
        })
    return records


def collect_licenses() -> None:
    """将运行依赖 wheel 中的许可证实际随包分发，而非只列网页链接。"""
    pending = ["mss", "numpy", "PySide6", "rapidocr", "onnxruntime", "pillow", "requests"]
    visited: set[str] = set()
    inventory = []
    while pending:
        name = pending.pop()
        key = re.sub(r"[-_.]+", "-", name).lower()
        if key in visited:
            continue
        visited.add(key)
        distribution = importlib.metadata.distribution(name)
        for requirement_text in distribution.requires or []:
            requirement = Requirement(requirement_text)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
        copies = []
        for entry in distribution.files or []:
            lowered = str(entry).lower()
            if not (
                any(word in Path(entry).name.lower() for word in ("license", "copying", "notice"))
                or "/licenses/" in lowered
            ):
                continue
            source = Path(distribution.locate_file(entry))
            if not source.is_file():
                continue
            relative = Path(*[part for part in entry.parts if part not in {"..", "."}])
            destination = DIST / "licenses" / key / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copies.append(destination.relative_to(DIST).as_posix())
        fallback = ROOT / "packaging" / "licenses" / f"{key}-LICENSE.txt"
        if not copies and fallback.is_file():
            destination = DIST / "licenses" / key / "LICENSE.txt"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fallback, destination)
            copies.append(destination.relative_to(DIST).as_posix())
        inventory.append({
            "name": distribution.metadata["Name"], "version": distribution.version,
            "runtime": "application",
            "license_files": sorted(copies),
        })
    inventory.extend(collect_capture_licenses())
    destination = DIST / "licenses" / "inventory.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(sorted(inventory, key=lambda item: item["name"].lower()), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _allowed_versions(target_name: str) -> tuple[str, ...]:
    """包内文件允许自证的版本：旧版本参考件只允许 :data:`REFERENCE_VERSION`。"""
    if target_name in REFERENCE_REPORT_NAMES:
        return (REFERENCE_VERSION,)
    return (VERSION,)


def _version_problem(source_name: str, target_name: str, payload: object) -> str | None:
    """返回版本不符的问题描述；没有 ``version`` 字段的报告不参与校验。"""
    if not isinstance(payload, dict) or "version" not in payload:
        return None
    declared = payload["version"]
    allowed = _allowed_versions(target_name)
    if declared in allowed:
        return None
    return f"{source_name} -> {target_name}: version={declared!r}，只允许 {' / '.join(allowed)}"


def validate_source_reports() -> None:
    """开包前门禁：源报告版本不符时不得进入包内。

    Raises:
        ValueError: 某个将要拷贝的报告自证版本与本包不符，或不是合法 JSON。
    """
    reports = EVIDENCE_DIR or ROOT / "build" / "validation"
    problems: list[str] = []
    for source_name, target_name in VALIDATION_REPORTS:
        source = reports / source_name
        if not source.is_file():
            continue
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            problems.append(f"{source_name}: 不是合法 JSON 报告（{error}）")
            continue
        problem = _version_problem(source_name, target_name, payload)
        if problem is not None:
            problems.append(problem)
    if problems:
        raise ValueError("源报告版本不符，已中止打包：\n  " + "\n  ".join(problems))


def copy_validation_reports() -> list[str]:
    """按显式清单拷贝验证报告，返回包内实际落地的文件名列表。"""
    reports = EVIDENCE_DIR or ROOT / "build" / "validation"
    target_dir = DIST / "validation"
    target_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for source_name, target_name in VALIDATION_REPORTS:
        source = reports / source_name
        if not source.is_file():
            continue
        shutil.copy2(source, target_dir / target_name)
        copied.append(target_name)
    return copied


def verify_validation_reports() -> None:
    """开包后门禁：拒绝旧文件名或版本号不符的报告留在当前版本包内。

    Raises:
        ValueError: 目标 validation/ 内存在无版本号的旧报告名，或有报告的
            ``version`` 字段与 :data:`VERSION` 不一致。
    """
    target_dir = DIST / "validation"
    if not target_dir.is_dir():
        raise FileNotFoundError(f"缺少包内验证报告目录：{target_dir}")
    owner = {target: source for source, target in VALIDATION_REPORTS}
    problems: list[str] = []
    for path in sorted(target_dir.glob("*.json")):
        name = path.name
        if name in FORBIDDEN_REPORT_NAMES:
            problems.append(f"{name}: 未带版本号的旧报告名，禁止随 {VERSION} 包分发")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            problems.append(f"{name}: 不是合法 JSON 报告（{error}）")
            continue
        problem = _version_problem(owner.get(name, name), name, payload)
        if problem is not None:
            problems.append(problem)
    if problems:
        raise ValueError("校验报告串版，已中止打包：\n  " + "\n  ".join(problems))


#: 交付 ZIP 的必需证据（只认这三类离线可复现证据 + 采集组件自检）。
#: 真实环境报告允许缺失，但必须在 VALIDATION.md 里写明“未验收”，不能由这里放行。
REQUIRED_EVIDENCE: tuple[str, ...] = (
    "automated-tests.json",
    "source-snapshot.json",
    _versioned("package-smoke"),
    _versioned("capture-component-self-test"),
    _versioned("capture-pool-self-test"),
)


def verify_required_evidence() -> None:
    """缺任一必需证据就中止；源码指纹还必须与当前工作区逐文件一致。"""
    target_dir = DIST / "validation"
    missing = [name for name in REQUIRED_EVIDENCE if not (target_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(
            "缺少必需交付证据，已中止生成正式 ZIP：\n  " + "\n  ".join(missing)
        )
    for name in REQUIRED_EVIDENCE:
        if name == "source-snapshot.json":
            continue
        payload = json.loads((target_dir / name).read_text(encoding="utf-8"))
        if name == _versioned("capture-component-self-test"):
            passed = isinstance(payload, dict) and payload.get("payload", {}).get("ok") is True
        else:
            passed = isinstance(payload, dict) and payload.get("passed") is True
        if not passed:
            raise ValueError(f"必需验证未通过，已中止打包：{name}")
    snapshot = json.loads((target_dir / "source-snapshot.json").read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict) or not snapshot:
        raise ValueError("source-snapshot.json 为空或格式非法")
    drifted: list[str] = []
    for relative, expected in snapshot.items():
        parts = relative.split("/")
        if (any(part in {"", ".", ".."} for part in parts) or "\\" in relative or ":" in relative
                or PureWindowsPath(relative).drive or not isinstance(expected, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise ValueError("源码指纹含无效路径或摘要")
        path = ROOT / relative
        if not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError("源码指纹路径越界")
        if not path.is_file():
            drifted.append(f"{relative}: 文件已不存在")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            drifted.append(f"{relative}: 指纹不一致")
    if drifted:
        raise ValueError(
            "源码指纹与当前工作区不一致，已中止生成正式 ZIP：\n  " + "\n  ".join(drifted[:20])
        )


PUBLIC_DOCUMENTS = (
    "README.md", "QUICK_START.md", "VALIDATION.md", "LICENSE",
    "requirements.lock", "THIRD_PARTY_NOTICES.md",
)
PUBLIC_DIRECTORIES = frozenset({"_internal", "capture-component", "licenses", "validation"})
PUBLIC_ROOT_FILES = frozenset({
    *PUBLIC_DOCUMENTS, "CueScope.exe", "capture-requirements.lock", "SHA256SUMS.txt",
})


def verify_public_contents() -> None:
    """Fail closed on private files or undeclared top-level payloads; never silently omit bytes."""
    report_names = {target for _, target in VALIDATION_REPORTS}
    private_names = {"agents.md", "plan_status.md", "config.json", "credentials.dpapi",
                     "credentials.tmp", "ui_preferences.json", ".env"}
    problems = []
    resolved = DIST.resolve()
    for path in DIST.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(DIST)
        if not path.resolve().is_relative_to(resolved):
            problems.append(relative.as_posix())
            continue
        if len(relative.parts) == 1:
            allowed = relative.name in PUBLIC_ROOT_FILES
        else:
            allowed = relative.parts[0] in PUBLIC_DIRECTORIES
        if relative.parts[0] == "validation":
            allowed = len(relative.parts) == 2 and relative.name in report_names
        lowered = path.name.lower()
        private = (lowered in private_names or lowered.startswith(".env.")
                   or lowered.endswith((".db", ".db-wal", ".db-shm", ".log", ".dpapi", ".private.png", ".download"))
                   or any(part.lower() in {"hits", "logs", "replay-private", ".workbuddy"}
                          for part in relative.parts))
        if private or not allowed:
            problems.append(relative.as_posix())
    if problems:
        raise ValueError("公开交付目录含未声明或私有文件，请使用干净的候选目录：\n  "
                         + "\n  ".join(problems[:20]))


def verify_frozen_payload() -> None:
    """Reject additions and changed bytes in the runtime since PyInstaller generated its manifest."""
    from checksums import file_hash

    runtime_dirs = {"_internal", "capture-component"}
    expected = {}
    reports = EVIDENCE_DIR or ROOT / "build" / "validation"
    for line in (reports / f"frozen-payload-{VERSION}.sha256").read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest) or relative in expected:
            raise ValueError("Invalid build-time runtime manifest")
        if relative.split("/")[0] in runtime_dirs or relative == "CueScope.exe":
            expected[relative] = digest
    actual = {}
    files = [DIST / "CueScope.exe"]
    files.extend(f for folder in runtime_dirs for f in (DIST / folder).rglob("*") if f.is_file())
    for f in files:
        if not f.resolve().is_relative_to(DIST.resolve()):
            raise ValueError("Runtime file resolves outside the package")
        actual[f.relative_to(DIST).as_posix()] = file_hash(f)
    if not expected or actual != expected:
        raise ValueError("Runtime contents changed after building")


def main(argv: list[str] | None = None) -> None:
    global DIST, ARCHIVE, EVIDENCE_DIR
    parser = argparse.ArgumentParser(description="整理交付目录并生成 ZIP")
    parser.add_argument("--dist-root", default=str(ROOT / "dist"))
    parser.add_argument("--archive", default="")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument(
        "--require-evidence",
        action="store_true", default=True,
        help="兼容参数；现在所有交付ZIP都要求验证证据",
    )
    if argv is None:
        # 被验收用例 import 后 ``main()`` 不应解析 pytest 自己的命令行参数。
        argv = sys.argv[1:] if __name__ == "__main__" else []
    args = parser.parse_args(argv)
    EVIDENCE_DIR = args.evidence_dir.resolve() if args.evidence_dir else None
    DIST = Path(args.dist_root).resolve() / f"CueScope-{VERSION}"
    ARCHIVE = (
        Path(args.archive).resolve()
        if args.archive
        else DIST.parent / f"CueScope-{VERSION}-win-x64.zip"
    )

    # 校验清单与旁挂文件统一走 tools/checksums.py（LF 行尾），两处实现不再各写一份。
    sys.path.insert(0, str(TOOLS_DIR))
    from checksums import file_hash, write_manifest, write_sidecar

    if not (DIST / "CueScope.exe").is_file():
        raise FileNotFoundError("尚未生成 CueScope.exe")
    verify_frozen_payload()
    for name in PUBLIC_DOCUMENTS:
        source = ROOT / ("packaging/PORTABLE_README.md" if name == "README.md" else name)
        shutil.copy2(source, DIST / name)
        internal = DIST / "_internal" / name
        if internal.is_file():
            shutil.copy2(source, internal)
    shutil.copy2(ROOT / "packaging" / "capture-requirements.lock", DIST / "capture-requirements.lock")
    collect_licenses()
    validate_source_reports()
    copied_reports = copy_validation_reports()
    verify_validation_reports()
    verify_required_evidence()
    verify_public_contents()
    write_manifest(DIST)
    with zipfile.ZipFile(ARCHIVE, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(item for item in DIST.rglob("*") if item.is_file()):
            output.write(path, Path(DIST.name) / path.relative_to(DIST))
    archive_hash = file_hash(ARCHIVE)
    write_sidecar(ARCHIVE.with_suffix(".zip.sha256"), archive_hash, ARCHIVE.name)
    print(f"Reports: {len(copied_reports)} -> {DIST / 'validation'}")
    for name in copied_reports:
        print(f"  + {name}")
    print(f"Archive: {ARCHIVE}")
    print(f"SHA-256: {archive_hash}")


if __name__ == "__main__":
    main()
