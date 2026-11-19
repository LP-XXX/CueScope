"""执行离线回归与静态检查，保存可核验的结果和源码指纹。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "build" / "validation"


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    commands = [
        [sys.executable, "-m", "pytest", "tests", "-vv", "-o", "faulthandler_timeout=45",
         f"--junitxml={OUTPUT / 'pytest.xml'}"],
        [sys.executable, "-m", "ruff", "check", "src", "tests", "tools"],
    ]
    checks = []
    for command in commands:
        started = time.perf_counter()
        label = "pytest" if "pytest" in command else "ruff"
        log_path = OUTPUT / f"{label}.log"
        try:
            with log_path.open("w", encoding="utf-8") as output:
                result = subprocess.run(command, cwd=ROOT, env=env, stdout=output,
                                        stderr=subprocess.STDOUT, timeout=900, check=False)
        except subprocess.TimeoutExpired:
            with log_path.open("a", encoding="utf-8") as output:
                output.write("\nValidation exceeded 900 seconds; run aborted.\n")
            print(f"{label}: timeout; see {log_path}")
            return 1
        checks.append({
            "name": label, "passed": result.returncode == 0,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        })
        print("\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]))
    root = ET.parse(OUTPUT / "pytest.xml").getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    counts = {key: sum(int(suite.get(key, "0")) for suite in suites)
              for key in ("tests", "failures", "errors", "skipped")}
    report = {
        "kind": "offline_automated_regression_and_lint", "finished_at": time.time(),
        "passed": all(check["passed"] for check in checks), "checks": checks,
        "test_counts": counts, "real_feishu_or_user_profile_accessed": False,
    }
    (OUTPUT / "automated-tests.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    snapshot_sources()
    return 0 if report["passed"] else 1


def snapshot_sources() -> None:
    """文档与发布工具更新后可单独刷新指纹，不伪造或重复执行回归。"""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    paths = [path for directory in ("src", "tests", "tools", "packaging", "training")
             for path in (ROOT / directory).rglob("*")
             if path.is_file() and path.suffix in {".py", ".spec", ".txt", ".json", ".lock", ".svg", ".png", ".ico"}]
    paths += [ROOT / name for name in (
        "README.md", "QUICK_START.md",
        "VALIDATION.md", "THIRD_PARTY_NOTICES.md",
        "pyproject.toml", "requirements.lock", "models/manifest.json", "LICENSE", ".gitignore",
    )]
    snapshot = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(paths)}
    (OUTPUT / "source-snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    raise SystemExit(main())
