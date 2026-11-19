"""Public release evidence, ZIP integrity and tag semantics."""

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"tools/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive_at(tmp_path, *, readme="[Guide](QUICK_START.md)", omit=None, stale_identity=False):
    version = load("build_release").VERSION
    folder = f"CueScope-{version}"
    archive = tmp_path / f"{folder}-win-x64.zip"
    files = {"README.md": readme.encode(), "QUICK_START.md": b"offline setup", "VALIDATION.md": b"short test",
             "THIRD_PARTY_NOTICES.md": b"third party", "CueScope.exe": b"stub exe",
             "capture-component/DealWatchCapture.exe": b"stub component", "validation/source-snapshot.json": b"{}"}
    def digest(name):
        return hashlib.sha256(files[name]).hexdigest()
    files["validation/build-info.json"] = json.dumps({
        "version": version, "main_sha256": "stale" if stale_identity else digest("CueScope.exe"),
        "capture_sha256": digest("capture-component/DealWatchCapture.exe"),
        "source_snapshot_sha256": digest("validation/source-snapshot.json"),
    }).encode()
    files["SHA256SUMS.txt"] = "".join(f"{digest(name)}  {name}\n" for name in files if name != omit).encode()
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, data in files.items():
            bundle.writestr(folder + "/" + name, data)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_bytes(f"{checksum}  {archive.name}\n".encode())
    return archive


def test_verify_zip_without_executing(tmp_path):
    load("check_release").verify_archive(archive_at(tmp_path))


@pytest.mark.parametrize("options,reason", [
    ({"readme": "[Broken](src/deal_watch/assets/brand/cuescope.png)"}, "document link"),
    ({"omit": "CueScope.exe"}, "does not list"),
    ({"stale_identity": True}, "Build identity"),
])
def test_inconsistent_zip_is_rejected(tmp_path, options, reason):
    with pytest.raises(ValueError, match=reason):
        load("check_release").verify_archive(archive_at(tmp_path, **options))


def test_tag_must_identify_numeric_application_version():
    module = load("build_release")
    for invalid in ("v99.0.0", f"v{module.VERSION}-rc.0", f"v{module.VERSION}-rc.1.extra"):
        with pytest.raises(ValueError, match="Tag must match"):
            module.check_tag(invalid)


def test_runtime_payload_rejects_undeclared_file_inside_allowed_directory(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    module = load("finalize_package")
    module.DIST = tmp_path / "app"
    module.EVIDENCE_DIR = tmp_path / "reports"
    module.EVIDENCE_DIR.mkdir()
    directory = module.DIST / "_internal"
    directory.mkdir(parents=True)
    (module.DIST / "CueScope.exe").write_bytes(b"app")
    digest = hashlib.sha256(b"app").hexdigest()
    (module.EVIDENCE_DIR / f"frozen-payload-{module.VERSION}.sha256").write_text(
        f"{digest}  CueScope.exe\n", encoding="utf-8")
    module.verify_frozen_payload()
    (directory / "arbitrary-note.txt").write_text("synthetic private note", encoding="utf-8")
    with pytest.raises(ValueError, match="contents changed"):
        module.verify_frozen_payload()
