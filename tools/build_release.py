"""Build a Windows candidate with fresh offline evidence, then verify its ZIP."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from checksums import file_hash  # noqa: E402
from verify import source_hashes  # noqa: E402

VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()


def check_tag(tag: str) -> str:
    if not re.fullmatch(rf"v{re.escape(VERSION)}(?:-rc\.[1-9]\d*)?", tag):
        raise ValueError(f"Tag must match v{VERSION} or v{VERSION}-rc.N")
    commit = git("rev-parse", "HEAD")
    if git("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}") != commit or git("status", "--porcelain"):
        raise ValueError("The release tag must point to the clean checkout being built")
    return commit


def clean_commit() -> str | None:
    """Local archives without Git or with in-progress edits use the source snapshot only."""
    try:
        return None if git("status", "--porcelain") else git("rev-parse", "HEAD")
    except (OSError, subprocess.CalledProcessError):
        return None


def check_capture_pool(component: Path, report: Path) -> None:
    from deal_watch.window_capture import PROCESS_REGISTRY, WindowCaptureClient

    client = WindowCaptureClient(component_path=component, persistent=True)
    result = {"kind": "frozen_capture_pool_preload_and_shutdown", "version": VERSION,
              "captured_frames": 0, "real_notifications_sent": False, "passed": False,
              "component_sha256": file_hash(component)}
    try:
        client.prepare()
        if not client.wait_ready(5):
            raise RuntimeError("First frozen capture worker missed its startup deadline")
        deadline = time.monotonic() + 5
        while len((stats := client.worker_stats())["processes"]) != 2:
            if time.monotonic() >= deadline:
                raise RuntimeError("Warm spare failed to start")
            time.sleep(0.05)
        if stats["active_processes"] != 2 or stats["failures"]:
            raise RuntimeError("Capture process quota or startup failed")
        result["ready_workers"] = len(stats["processes"])
    finally:
        client.close()
        result["clean_shutdown"] = client.wait_closed(5)
        result["remaining_processes"] = PROCESS_REGISTRY.active
        result["passed"] = (result.get("ready_workers") == 2 and result["clean_shutdown"]
                            and result["remaining_processes"] == 0)
        report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if not result["passed"]:
        raise RuntimeError("Frozen capture workers did not exit cleanly")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-root", required=True, type=Path)
    parser.add_argument("--tag", help="For GitHub Release builds, require this exact clean tag")
    args = parser.parse_args()
    commit = check_tag(args.tag) if args.tag else clean_commit()
    output = args.dist_root.resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"CueScope-{VERSION}-win-x64.zip"
    if archive.exists() or archive.with_suffix(".zip.sha256").exists():
        raise FileExistsError("Use a new destination directory to preserve earlier candidates")
    # Each attempt uses a fresh directory so stale reports cannot authorize a release.
    run = Path(tempfile.mkdtemp(prefix="run-", dir=output))
    evidence = run / "evidence"
    evidence.mkdir()
    dist = run / f"CueScope-{VERSION}"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "QT_QPA_PLATFORM": "offscreen",
           "CUESCOPE_ARTIFACT_DIR": str(dist), "DEALWATCH_BENCH": "1"}

    def tool(name, *arguments):
        subprocess.run([sys.executable, str(ROOT / "tools" / name), *arguments], cwd=ROOT,
                       env=env, check=True)

    initial_sources = source_hashes()
    tool("build.py", "--dist-root", run, "--work-root", run / "pyinstaller", "--evidence-dir", evidence)
    tool("verify.py", "--output", evidence, "--release")
    if initial_sources != json.loads((evidence / "source-snapshot.json").read_text(encoding="utf-8")):
        raise ValueError("Public source changed during the build or regression run")
    main_exe = dist / "CueScope.exe"
    component = dist / "capture-component/DealWatchCapture.exe"
    subprocess.run([main_exe, "--self-check", "--report", evidence / f"package-smoke-{VERSION}.json"],
                   cwd=run, env=env, check=True, timeout=120)
    check_capture_pool(component, evidence / f"capture-pool-self-test-{VERSION}.json")
    info = {"kind": "candidate_build_identity", "version": VERSION, "release_tag": args.tag,
            "source_commit": commit, "main_sha256": file_hash(main_exe),
            "capture_sha256": file_hash(component), "source_snapshot_sha256": file_hash(evidence / "source-snapshot.json")}
    (evidence / "build-info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    staged_archive = run / archive.name
    tool("finalize_package.py", "--dist-root", run, "--archive", staged_archive, "--evidence-dir", evidence)
    provenance = ["--commit", commit] if commit else []
    tool("check_release.py", "--archive", staged_archive, "--smoke", *provenance)
    staged_archive.with_suffix(".zip.sha256").rename(archive.with_suffix(".zip.sha256"))
    staged_archive.rename(archive)
    print(f"Verified {archive.name}; evidence: {evidence}")


if __name__ == "__main__":
    main()
