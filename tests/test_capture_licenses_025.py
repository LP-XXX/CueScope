"""Capture component wheel licenses must be copied, versioned and complete."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def distribution(base, name, version, entries):
    files = [Path(entry) for entry in entries]
    for relative in files:
        target = base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"Fixture license: {name} {version}\n", encoding="utf-8")
    return SimpleNamespace(metadata={"Name": name}, version=version, files=files,
                           requires=[], locate_file=lambda entry: base / entry)


@pytest.fixture
def package(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("capture_license_packager", ROOT / "tools/finalize_package.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_path
    module.DIST = tmp_path / "dist"
    (tmp_path / "packaging").mkdir()
    (tmp_path / "packaging/capture-requirements.lock").write_bytes(
        (ROOT / "packaging/capture-requirements.lock").read_bytes())
    capture = tmp_path / ".capture-runtime"
    windows = distribution(capture, "windows-capture", "2.0.1",
                           ["windows_capture-2.0.1.dist-info/licenses/LICENCE"])
    opencv = distribution(capture, "opencv-python", "5.0.0.93",
                          ["cv2/LICENSE.txt", "cv2/LICENSE-3RD-PARTY.txt"])
    wrong_numpy = distribution(capture, "numpy", "2.4.6", ["numpy-2.4.6.dist-info/LICENSE.txt"])
    build = tmp_path / "build-interpreter"
    main_numpy = distribution(build, "numpy", "2.2.6", ["numpy-2.2.6.dist-info/LICENSE.txt"])
    installed = {"numpy": main_numpy}
    for name in ("mss", "PySide6", "rapidocr", "onnxruntime", "pillow", "requests"):
        installed[name] = distribution(build, name, "1.0", [f"{name}-1.0.dist-info/LICENSE.txt"])
    monkeypatch.setattr(module.importlib.metadata, "distribution", installed.__getitem__)

    def isolated(*, path):
        assert path == [str(capture)]
        return [windows, opencv, wrong_numpy]

    monkeypatch.setattr(module.importlib.metadata, "distributions", isolated)
    return module, windows, opencv, main_numpy


def test_capture_licenses_copy_actual_files_and_exclude_isolated_numpy(package):
    module, _, _, _ = package
    records = module.collect_capture_licenses()
    assert {entry["name"]: entry["version"] for entry in records} == {
        "numpy": "2.2.6", "opencv-python": "5.0.0.93", "windows-capture": "2.0.1",
    }
    numpy = next(entry for entry in records if entry["name"] == "numpy")
    assert numpy["metadata_source"] == "build-interpreter"
    assert all(entry["runtime"] == "capture-component" for entry in records)
    for entry in records:
        for path in entry["license_files"]:
            assert path.startswith("licenses/capture-runtime/")
            content = (module.DIST / path).read_bytes()
            assert content
            assert hashlib.sha256(content).hexdigest() in entry["license_source_sha256"].values()
    opencv = next(entry for entry in records if entry["name"] == "opencv-python")
    assert {Path(path).name for path in opencv["license_files"]} == {
        "LICENSE.txt", "LICENSE-3RD-PARTY.txt",
    }
    assert "2.4.6" not in json.dumps(records)


def test_capture_inventory_is_appended_alongside_main_dependencies(package):
    module, _, _, _ = package
    module.collect_licenses()
    entries = json.loads((module.DIST / "licenses/inventory.json").read_text(encoding="utf-8"))
    assert {entry["runtime"] for entry in entries if entry["name"] == "numpy"} == {
        "application", "capture-component",
    }
    assert len([entry for entry in entries if entry["runtime"] == "capture-component"]) == 3


@pytest.mark.parametrize("dependency", ["windows", "opencv", "numpy"])
def test_capture_version_drift_blocks_delivery(package, dependency):
    module, windows, opencv, numpy = package
    {"windows": windows, "opencv": opencv, "numpy": numpy}[dependency].version = "99.0"
    with pytest.raises(ValueError, match="版本"):
        module.collect_capture_licenses()


@pytest.mark.parametrize("dependency", ["windows", "opencv"])
def test_missing_capture_license_blocks_delivery(package, dependency):
    module, windows, opencv, _ = package
    item = windows if dependency == "windows" else opencv
    item.locate_file(item.files[-1]).unlink()
    with pytest.raises(FileNotFoundError):
        module.collect_capture_licenses()


def test_missing_capture_lock_blocks_delivery(package):
    module, _, _, _ = package
    (module.ROOT / "packaging/capture-requirements.lock").unlink()
    with pytest.raises(FileNotFoundError):
        module.collect_capture_licenses()
