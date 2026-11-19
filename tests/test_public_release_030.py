"""Public packaging boundaries and clean-bootstrap contracts; no desktop or network access."""

from __future__ import annotations

import importlib.util
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from deal_watch import __version__, brand, model_catalog

ROOT = Path(__file__).resolve().parents[1]


def tool(name):
    spec = importlib.util.spec_from_file_location(f"release_030_{name}", ROOT / "tools" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_brand_assets_and_all_windows_icon_sizes_are_present():
    assert brand.DISPLAY_NAME == "CueScope"
    assert brand.VERSION == __version__ == tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert brand.brand_svg_path().is_file()
    with Image.open(brand.brand_svg_path().with_suffix(".ico")) as icon:
        assert {(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)} <= icon.ico.sizes()


def test_brand_icon_renders_at_requested_size_and_custom_color():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    icon = brand.brand_icon(32, monochrome=True, color="#FFFFFF")
    assert not icon.isNull()
    assert not icon.pixmap(32, 32).toImage().isNull()
    with pytest.raises(ValueError):
        brand.brand_icon(color="not a color")
    assert app is not None


def test_manifest_is_the_only_model_catalog_and_wheel_contains_it():
    defaults = model_catalog.load_model_defaults()
    manifest = model_catalog.load_model_manifest()
    for role, filename in manifest["roles"].items():
        assert defaults[f"{role}_path"] == f"models/{filename}"
        assert defaults[f"{role}_sha256"] == manifest["files"][filename]["sha256"]
    wheel = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["force-include"]["models/manifest.json"] == "deal_watch/assets/model-manifest.json"


def test_catalog_falls_back_to_wheel_resource_without_checkout(tmp_path, monkeypatch):
    module_file = tmp_path / "site-packages" / "deal_watch" / "model_catalog.py"
    module_file.parent.mkdir(parents=True)
    monkeypatch.setattr(model_catalog, "__file__", str(module_file))
    assert model_catalog.manifest_path() == module_file.parent / "assets/model-manifest.json"


@pytest.mark.parametrize("change", ["hash", "traversal", "role", "http"])
def test_catalog_rejects_invalid_inputs(tmp_path, change):
    manifest = model_catalog.load_model_manifest()
    name = manifest["roles"]["det"]
    if change == "hash":
        manifest["files"][name]["sha256"] = "wrong"
    elif change == "traversal":
        manifest["files"]["../outside"] = manifest["files"].pop(name)
    elif change == "role":
        manifest["roles"]["det"] = "absent.onnx"
    else:
        manifest["files"][name]["url"] = "http://example.invalid/model"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="模型清单"):
        model_catalog.load_model_manifest(path)


def test_model_downloader_uses_manifest_and_does_not_rewrite_it(tmp_path, monkeypatch):
    fetch = tool("fetch_models")
    models = tmp_path / "models"
    models.mkdir()
    payload = b"synthetic model"
    import hashlib

    metadata = {"sha256": hashlib.sha256(payload).hexdigest(), "url": "https://example.invalid/model"}
    manifest = {"files": {"test.onnx": metadata}}
    sentinel = models / "manifest.json"
    sentinel.write_bytes(b"unchanged manifest")
    (models / "test.onnx").write_bytes(payload)
    monkeypatch.setattr(fetch, "ROOT", tmp_path)
    monkeypatch.setattr(fetch, "DESTINATION", models)
    monkeypatch.setattr(fetch, "load_model_manifest", lambda path: manifest)
    monkeypatch.setattr(fetch.urllib.request, "urlopen", lambda *a, **k: pytest.fail("unexpected download"))
    fetch.main()
    assert sentinel.read_bytes() == b"unchanged manifest"


@pytest.mark.parametrize("private", ["AGENTS.md", "PLAN_STATUS.md", "config.json",
                                      "_internal/credentials.dpapi", "capture-component/user.db",
                                      "validation/private.json", "private.png"])
def test_public_package_rejects_private_or_undeclared_files(tmp_path, private):
    package = tool("finalize_package")
    package.DIST = tmp_path
    path = tmp_path / private
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture")
    with pytest.raises(ValueError, match="私有文件"):
        package.verify_public_contents()


def test_public_package_accepts_runtime_brand_and_approved_documents(tmp_path):
    package = tool("finalize_package")
    package.DIST = tmp_path
    for relative in ["CueScope.exe", "LICENSE", "README.md", "_internal/deal_watch/assets/brand/cuescope.svg",
                     "capture-component/DealWatchCapture.exe", "validation/automated-tests.json"]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    package.verify_public_contents()


def test_bootstrap_checks_both_runtime_versions(tmp_path, monkeypatch):
    bootstrap = tool("bootstrap")
    (tmp_path / "packaging").mkdir()
    (tmp_path / "requirements.lock").write_text("numpy==2.2.6\n", encoding="utf-8")
    (tmp_path / "packaging/capture-requirements.lock").write_text("windows-capture==2.0.1\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap.importlib.metadata, "version", lambda name: "2.2.6")
    dist = SimpleNamespace(metadata={"Name": "windows-capture"}, version="2.0.1")
    monkeypatch.setattr(bootstrap.importlib.metadata, "distributions", lambda **kwargs: [dist])
    bootstrap.check_dependencies(tmp_path)
    dist.version = "2.0.0"
    with pytest.raises(RuntimeError, match="采集环境"):
        bootstrap.check_dependencies(tmp_path)


def test_shared_ignore_rules_cover_isolated_dependencies_and_local_agent_records():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {".capture-runtime/", ".workbuddy/", "credentials.tmp", "build/", "dist/"} <= set(lines)


def test_source_snapshot_covers_all_modules_and_brand_assets(tmp_path, monkeypatch):
    verify = tool("verify")
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    monkeypatch.setattr(verify, "OUTPUT", tmp_path / "build/validation")
    for relative in ["AGENTS.md", "README.md", "QUICK_START.md", "PLAN_STATUS.md", "VALIDATION.md",
                     "THIRD_PARTY_NOTICES.md", "pyproject.toml", "requirements.lock", "models/manifest.json",
                     "LICENSE", ".gitignore", "src/deal_watch/new_feature.py",
                     "src/deal_watch/assets/brand/cuescope.svg", "src/deal_watch/assets/brand/cuescope.ico",
                     "src/deal_watch/assets/brand/cuescope.png"]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    verify.snapshot_sources()
    result = json.loads((verify.OUTPUT / "source-snapshot.json").read_text(encoding="utf-8"))
    assert {"LICENSE", ".gitignore", "src/deal_watch/new_feature.py",
            "src/deal_watch/assets/brand/cuescope.svg", "src/deal_watch/assets/brand/cuescope.ico",
            "src/deal_watch/assets/brand/cuescope.png"} <= result.keys()
