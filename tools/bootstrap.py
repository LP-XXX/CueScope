"""Prepare the documented Windows build/runtime dependencies without personal paths."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import struct
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def exact_pins(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, version = line.partition("==")
        if not separator or not name or not version or any(c in version for c in "*; "):
            raise ValueError(f"Expected an exact dependency pin in {path.name}")
        pins[name.lower().replace("_", "-")] = version
    return pins


def check_dependencies(root: Path = ROOT) -> None:
    for name, expected in exact_pins(root / "requirements.lock").items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as error:
            raise RuntimeError(f"缺少主环境依赖：{name}") from error
        if actual != expected:
            raise RuntimeError(f"主环境依赖不匹配：{name}，期望 {expected}，实际 {actual}")
    runtime = root / ".capture-runtime"
    expected = exact_pins(root / "packaging/capture-requirements.lock")
    found = {}
    for item in importlib.metadata.distributions(path=[str(runtime)]):
        name = item.metadata["Name"].lower().replace("_", "-")
        if name in found:
            raise RuntimeError(f"采集环境依赖重复：{name}")
        found[name] = item.version
    for name, version in expected.items():
        if found.get(name) != version:
            raise RuntimeError(f"采集环境缺少锁定依赖：{name}=={version}；请运行 tools/bootstrap.py")


def prepare_capture(root: Path = ROOT) -> None:
    """Install into a fresh directory, then replace locally; retain previous bytes for rollback."""
    workspace = root.resolve()
    build = workspace / "build" / "bootstrap"
    build.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix="capture-", dir=build)).resolve()
    runtime = (workspace / ".capture-runtime").resolve()
    if not staged.is_relative_to(build.resolve()) or runtime.parent != workspace:
        raise ValueError("Capture dependency target escapes the workspace")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--only-binary=:all:", "--no-deps",
         "--target", str(staged), "-r", str(workspace / "packaging/capture-requirements.lock")],
        check=True, cwd=workspace,
    )
    # Verify native imports before promoting the stage. No capture or network call occurs here.
    subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0,sys.argv[1]); "
         "import numpy,cv2,windows_capture; print('Capture imports OK')", str(staged)],
        check=True, cwd=workspace,
    )
    if runtime.exists():
        backup = (build / f"previous-capture-{uuid.uuid4().hex}").resolve()
        if not backup.is_relative_to(build.resolve()):
            raise ValueError("Capture backup escapes the workspace")
        runtime.replace(backup)
        try:
            staged.replace(runtime)
        except OSError:
            backup.replace(runtime)
            raise
    else:
        staged.replace(runtime)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="初始化 CueScope 的主环境、隔离采集环境和固定模型")
    parser.add_argument("--check", action="store_true", help="只检查已安装依赖，不安装或下载")
    parser.add_argument("--skip-models", action="store_true", help="安装依赖后不下载模型")
    args = parser.parse_args(argv)
    if os.name != "nt" or struct.calcsize("P") != 8 or sys.version_info[:2] != (3, 11):
        raise RuntimeError("本锁定构建流程需要 Windows x64 和 Python 3.11")
    if args.check:
        check_dependencies()
        print("Locked main and capture dependencies are present")
        return
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.lock")],
                   check=True, cwd=ROOT)
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "-e", str(ROOT)],
                   check=True, cwd=ROOT)
    prepare_capture()
    check_dependencies()
    if not args.skip_models:
        subprocess.run([sys.executable, str(ROOT / "tools/fetch_models.py")], check=True, cwd=ROOT)


if __name__ == "__main__":
    main()
