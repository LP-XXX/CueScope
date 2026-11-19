"""Verify the ZIP sidecar, every archived file and the portable documentation."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlsplit


def hash_stream(stream) -> str:
    return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_archive(archive: Path, *, smoke=False, commit=None) -> dict:
    match = re.fullmatch(r"CueScope-(\d+\.\d+\.\d+)-win-x64\.zip", archive.name)
    if not match:
        raise ValueError("Unexpected portable ZIP name")
    version = match[1]
    root = f"CueScope-{version}/"
    with archive.open("rb") as handle:
        expected_sidecar = f"{hash_stream(handle)}  {archive.name}\n"
    if archive.with_suffix(".zip.sha256").read_bytes() != expected_sidecar.encode():
        raise ValueError("ZIP digest/filename mismatch")
    with zipfile.ZipFile(archive) as bundle:
        entries = {}
        seen = set()
        for item in bundle.infolist():
            name = item.filename.removeprefix(root)
            parts = name.split("/")
            if (not item.filename.startswith(root) or any(part in {"", ".", ".."} for part in parts)
                    or "\\" in name or ":" in name or name.casefold() in seen
                    or any(PureWindowsPath(part).is_reserved() or part.rstrip(" .") != part for part in parts)):
                raise ValueError("Unexpected or duplicate archive path")
            seen.add(name.casefold())
            entries[name] = item
        manifest = bundle.read(entries["SHA256SUMS.txt"]).decode("utf-8")
        expected = {}
        for line in manifest.split("\n"):
            if not line:
                continue
            digest, separator, name = line.partition("  ")
            if not separator or not re.fullmatch("[0-9a-f]{64}", digest) or name in expected:
                raise ValueError("Invalid SHA256SUMS.txt entry")
            expected[name] = digest
        if expected.keys() != entries.keys() - {"SHA256SUMS.txt"}:
            raise ValueError("SHA256SUMS.txt does not list exactly the archived payload")
        for name, digest in expected.items():
            with bundle.open(entries[name]) as stream:
                if hash_stream(stream) != digest:
                    raise ValueError(f"Archived file failed SHA-256 check: {name}")
        info = json.loads(bundle.read(entries["validation/build-info.json"]))
        if commit and (not re.fullmatch(r"[0-9a-f]{40}", commit) or info.get("source_commit") != commit):
            raise ValueError("Archive was built from a different source commit")
        if (info.get("version") != version or info.get("main_sha256") != expected.get("CueScope.exe")
                or info.get("capture_sha256") != expected.get("capture-component/DealWatchCapture.exe")
                or info.get("source_snapshot_sha256") != expected.get("validation/source-snapshot.json")):
            raise ValueError("Build identity differs from the archived files")
        source_commit = info.get("source_commit")
        tag = info.get("release_tag")
        if source_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", str(source_commit)):
            raise ValueError("Invalid recorded source commit")
        if tag is not None and (not source_commit or not re.fullmatch(rf"v{re.escape(version)}(?:-rc\.[1-9]\d*)?", str(tag))):
            raise ValueError("Invalid recorded release tag")
        def evidence(stem, versioned=True):
            name = stem + (f"-{version}" if versioned else "") + ".json"
            return json.loads(bundle.read(entries["validation/" + name]))
        regression = evidence("automated-tests", False)
        counts = {key: regression["test_counts"][key] for key in ("tests", "failures", "errors", "skipped")}
        checks = {item.get("name") for item in regression.get("checks", []) if item.get("passed") is True}
        frozen = evidence("package-smoke")
        capture = evidence("capture-component-self-test")
        pool = evidence("capture-pool-self-test")
        if (not all(type(n) is int and n >= 0 for n in counts.values()) or not counts["tests"]
                or counts["failures"] or counts["errors"] or counts["skipped"] >= counts["tests"]
                or not {"pytest", "ruff"} <= checks or regression.get("passed") is not True or frozen.get("passed") is not True
                or frozen.get("frozen") is not True or frozen.get("qq_pairing_module") is not True
                or capture.get("payload", {}).get("ok") is not True or pool.get("passed") is not True
                or pool.get("captured_frames") != 0 or pool.get("remaining_processes") != 0
                or pool.get("ready_workers") != 2):
            raise ValueError("Required offline release checks are missing or failed")
        for name in ("README.md", "QUICK_START.md", "VALIDATION.md", "THIRD_PARTY_NOTICES.md"):
            markdown = bundle.read(entries[name]).decode("utf-8")
            for target in re.findall(r"\]\(([^)\s]+)\)", markdown):
                url = urlsplit(target.strip("<>"))
                if url.scheme or url.netloc or not url.path:
                    continue
                path = PurePosixPath(name).parent / unquote(url.path)
                if path.as_posix() not in entries:
                    raise ValueError(f"Broken portable document link: {name} -> {path}")
        if smoke:
            # Only execute the just-built candidate on an explicitly requested check.
            with tempfile.TemporaryDirectory(prefix="cuescope-extracted-") as tmp:
                bundle.extractall(tmp)
                package = Path(tmp) / root
                report = Path(tmp) / "self-check.json"
                subprocess.run([package / "CueScope.exe", "--self-check", "--report", report],
                               cwd=tmp, timeout=120, check=True)
                data = json.loads(report.read_text(encoding="utf-8"))
                if data.get("passed") is not True or data.get("frozen") is not True or data.get("version") != version:
                    raise ValueError("Extracted program failed its offline self-check")
    return {
        "version": version, "tag": tag, "source_commit": source_commit,
        "archive_filename": archive.name, "archive_sha256": expected_sidecar.split(" ", 1)[0],
        "source_snapshot_sha256": info["source_snapshot_sha256"], "offline_test_counts": counts,
        "ruff_passed": True,
        "frozen_ocr_and_ui_self_check_passed": True, "qq_binding_offline_check_passed": True,
        "capture_dependency_and_two_worker_shutdown_passed": True,
        "archive_hashes_verified": True,
        "field_validation_pending": ["target application compatibility and occlusion", "five-window scheduling",
                                     "QQ/Feishu platform acceptance and phone visibility", "long-running stability"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--commit", help="Require this exact source commit in the build identity")
    parser.add_argument("--write-summary", action="store_true", help="Write a redacted verification summary beside the ZIP")
    parser.add_argument("--smoke", action="store_true", help="Run the extracted build's isolated self-check")
    args = parser.parse_args()
    archive = args.archive.resolve()
    summary = verify_archive(archive, smoke=args.smoke, commit=args.commit)
    sidecar = archive.with_suffix(".validation.json")
    if args.write_summary:
        sidecar.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")
    elif sidecar.exists() and json.loads(sidecar.read_text(encoding="utf-8")) != summary:
        raise ValueError("External validation summary differs from the archived evidence")
    print(f"Verified {args.archive.name}")


if __name__ == "__main__":
    main()
