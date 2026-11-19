"""构建 Windows x64 onedir 主程序、隔离采集组件并生成逐文件 SHA-256。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: 脚本所在目录（导入同目录的 checksums 用），与 ROOT 分开以便用例把 ROOT 打桩。
TOOLS_DIR = Path(__file__).resolve().parent
CAPTURE_RUNTIME = ROOT / ".capture-runtime"
CAPTURE_STAGE = ROOT / "build" / "capture-stage"

#: 版本号只有一处真值源（``pyproject.toml``）；产物目录名、最终打包目录都必须由它派生，
#: 否则升版时会出现“PyInstaller 产出 A 目录、build.py 复制到 B 目录”的错位。
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
DIST_NAME = f"CueScope-{VERSION}"
#: 当前构建输出根目录；默认 ``dist/``，候选构建由 ``main(--dist-root)`` 改写。
#: 保留模块级变量是为了让 ``verify_capture_component()`` 继续零参数调用。
DIST_ROOT = ROOT / "dist"
VALIDATION_DIR = ROOT / "build" / "validation"


def stage_capture_dependencies() -> Path:
    """准备组件构建环境：保留 cv2/windows_capture，排除它们的 numpy。

    组件构建解释器自己的 site-packages 里也有 numpy（主程序 OCR 用）。
    若同时把 ``.capture-runtime`` 放进 pathex，PyInstaller 会出现
    “纯 Python 取自 pathex、C 扩展取自 site-packages”的混版产物，
    运行时表现为 ``DLL load failed while importing _multiarray_umath``。
    这里只在暂存目录里暴露 cv2 / windows_capture，让 numpy 只有一个来源。
    """
    if not CAPTURE_RUNTIME.is_dir():
        raise FileNotFoundError("缺少隔离采集依赖，请先运行 python tools/bootstrap.py")
    if not CAPTURE_STAGE.resolve().is_relative_to((ROOT / "build").resolve()):
        raise ValueError("采集暂存目录必须位于项目 build 目录内")
    if CAPTURE_STAGE.exists():
        shutil.rmtree(CAPTURE_STAGE)
    CAPTURE_STAGE.mkdir(parents=True)
    skipped = []
    for item in sorted(CAPTURE_RUNTIME.iterdir()):
        if item.name == "numpy" or item.name.startswith("numpy-") or item.name == "numpy.libs":
            skipped.append(item.name)
            continue
        target = CAPTURE_STAGE / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
    print(f"Capture stage: {CAPTURE_STAGE} (excluded {', '.join(skipped) or 'none'})")
    return CAPTURE_STAGE


def verify_capture_component() -> dict:
    """构建后必须真正导入依赖；“组件文件存在”不能证明它能采集。"""
    dist_root = DIST_ROOT
    component = dist_root / "DealWatchCapture" / "DealWatchCapture.exe"
    if not component.is_file():
        raise RuntimeError(f"缺少采集组件：{component}")
    result = subprocess.run(
        [str(component), "--self-test"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"采集组件自检未返回有效 JSON：{result.stdout[-400:]}{result.stderr[-400:]}"
        ) from error
    if not payload.get("ok"):
        raise RuntimeError(
            "采集组件依赖自检失败（便携包将无法圈选）："
            f"{payload.get('error')}\n{payload.get('error_detail', '')}"
        )
    versions = {name: payload.get(name) for name in ("numpy", "cv2") if payload.get(name)}
    bundles = sorted(p.name for p in (component.parent / "_internal").glob("numpy-*.dist-info"))
    if len(bundles) != 1:
        raise RuntimeError(f"采集组件内 numpy 版本数应为 1，实际 {bundles}")
    print(f"Capture component self-test OK: {versions} / {bundles[0]}")
    return {
        "kind": "capture_component_self_test",
        "version": VERSION,
        "finished_at": time.time(),
        "component": str(component.relative_to(ROOT)).replace("\\", "/"),
        "component_sha256": hashlib.sha256(component.read_bytes()).hexdigest(),
        "numpy_bundles": bundles,
        "payload": payload,
    }


def write_capture_self_test(report: dict) -> Path:
    """把采集组件自检写成机器可读证据，随候选包一起交付。"""
    VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
    path = VALIDATION_DIR / f"capture-component-self-test-{VERSION}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="构建当前版本便携目录")
    # 默认仍写 dist/；候选构建必须显式指定独立目录，避免覆盖已交付的 0.2.0/0.2.1/0.2.2。
    parser.add_argument("--dist-root", default=str(ROOT / "dist"))
    parser.add_argument("--work-root", default=str(ROOT / "build" / "pyinstaller"))
    args = parser.parse_args(argv)
    global DIST_ROOT
    dist_root = Path(args.dist_root).resolve()
    DIST_ROOT = dist_root
    dist_root.mkdir(parents=True, exist_ok=True)
    work_root = Path(args.work_root).resolve()
    pyinstaller_paths = ["--distpath", str(dist_root), "--workpath", str(work_root)]

    # 版本资源双保险：两个 spec 内部也会各自渲染，这里先渲染一次，保证“产物名 DIST_NAME”
    # 与“EXE 属性里的版本号”来自同一次读到的 pyproject 版本，中途被改也不会出现半套版本。
    sys.path.insert(0, str(ROOT / "packaging"))
    from version_metadata import ensure_version_info

    version_info = ensure_version_info(ROOT)
    print(f"Version info: {version_info} (version {VERSION})")
    sys.path.insert(0, str(ROOT / "src"))
    from deal_watch.config import ModelConfig
    from deal_watch.ocr import LocalOcr

    LocalOcr(ModelConfig(), ROOT).verify_models()
    stage = stage_capture_dependencies()
    build_env = {**os.environ, "DEALWATCH_CAPTURE_STAGE": str(stage)}
    # 不让 .capture-runtime 的 numpy 参与本次分析的 import 解析。
    build_env.pop("PYTHONPATH", None)
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
            *pyinstaller_paths,
            "packaging/capture_worker.spec",
        ],
        cwd=ROOT,
        env=build_env,
        check=True,
    )
    report = verify_capture_component()
    print(f"Capture self-test report: {write_capture_self_test(report)}")
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
            *pyinstaller_paths,
            "packaging/deal_watch.spec",
        ],
        cwd=ROOT,
        check=True,
    )
    dist = dist_root / DIST_NAME
    capture_source = dist_root / "DealWatchCapture"
    capture_target = dist / "capture-component"
    if not capture_target.resolve().is_relative_to(dist.resolve()):
        raise ValueError("采集组件输出目录越界")
    if capture_target.exists():
        shutil.rmtree(capture_target)
    shutil.copytree(capture_source, capture_target)
    sys.path.insert(0, str(TOOLS_DIR))
    from checksums import write_manifest

    # 清单必须是 LF：Windows 默认写成 CRLF 时，sha256sum -c 会把 \r 当成文件名的一部分。
    print(f"Checksums: {write_manifest(dist)}")
    print(f"Portable directory: {dist}")


if __name__ == "__main__":
    main()
