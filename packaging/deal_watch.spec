# -*- mode: python ; coding: utf-8 -*-
import json
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPEC).resolve().parents[1]
# 复用 packaging/version_metadata.py 渲染版本资源，避免 spec 里再写一份拼接逻辑。
sys.path.insert(0, str(ROOT / "packaging"))
from version_metadata import ensure_version_info  # noqa: E402

# Windows 版本资源由构建脚本按 pyproject.toml 生成到 build/（packaging/version_info.txt
# 只是不含版本数字的模板）：EXE 属性里的版本号不可能再与源码漂移。
VERSION_INFO = ensure_version_info(ROOT)
# 产物目录名取 pyproject.toml 的版本号：0.2.1 时代这里写死了带版本号的目录名，
# 升版后 PyInstaller 产出目录、build.py 复制组件的目标目录与 finalize 打包目录
# 会互相错位，属于同一类“版本字面量”雷。
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
rapid_datas, rapid_binaries, rapid_hidden = collect_all("rapidocr")
ort_datas, ort_binaries, ort_hidden = collect_all("onnxruntime")

model_files = json.loads((ROOT / "models/manifest.json").read_text(encoding="utf-8"))["files"]
if any(Path(name).name != name or "/" in name or "\\" in name for name in model_files):
    raise ValueError("Model manifest contains an unsafe filename")
datas = [(str(ROOT / "models" / name), "models") for name in (*model_files, "manifest.json")] + [
    (str(ROOT / "src/deal_watch/assets/brand"), "deal_watch/assets/brand"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "requirements.lock"), "."),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
] + rapid_datas + ort_datas
binaries = rapid_binaries + ort_binaries
hiddenimports = rapid_hidden + ort_hidden

a = Analysis(
    [str(ROOT / "src" / "deal_watch" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "paddle", "tensorflow", "cuda", "tokenizers", "transformers"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CueScope",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "src/deal_watch/assets/brand/cuescope.ico"),
    version=str(VERSION_INFO),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=f"CueScope-{VERSION}",
)
