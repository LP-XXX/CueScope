# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from pathlib import Path

ROOT = Path(SPEC).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
from version_metadata import ensure_version_info  # noqa: E402

VERSION_INFO = ensure_version_info(ROOT, component=True)
# 构建期依赖目录：默认用 .capture-runtime，tools/build.py 会传入一个
# 只含 cv2 / windows_capture（不含 numpy）的暂存目录，避免组件里混入
# 两份 numpy（纯 Python 从 pathex、C 扩展从 site-packages），
# 那会导致 "DLL load failed while importing _multiarray_umath"。
CAPTURE_RUNTIME = Path(
    os.environ.get("DEALWATCH_CAPTURE_STAGE", ROOT / ".capture-runtime")
)

a = Analysis(
    [str(ROOT / "src" / "deal_watch" / "capture_worker.py")],
    pathex=[str(ROOT / "src"), str(CAPTURE_RUNTIME)],
    binaries=[],
    datas=[],
    hiddenimports=["windows_capture", "windows_capture.windows_capture", "cv2"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "transformers", "rapidocr", "PySide6", "onnxruntime"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DealWatchCapture",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    version=str(VERSION_INFO),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DealWatchCapture",
)
