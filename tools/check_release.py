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


def verify_archive(archive: Path, *, smoke=False, commit=None) -> None:
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--commit", help="Require this exact source commit in the build identity")
    parser.add_argument("--smoke", action="store_true", help="Run the extracted build's isolated self-check")
    args = parser.parse_args()
    verify_archive(args.archive.resolve(), smoke=args.smoke, commit=args.commit)
    print(f"Verified {args.archive.name}")


if __name__ == "__main__":
    main()
