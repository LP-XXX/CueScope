"""执行离线回归与静态检查，保存可核验的结果和源码指纹。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "build" / "validation"


def main(argv=None) -> int:
    global OUTPUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--release", action="store_true", help="Reject unexecuted release checks")
    args = parser.parse_args(argv)
    OUTPUT = args.output.resolve()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
           "PYTHONIOENCODING": "utf-8", "QT_QPA_PLATFORM": "offscreen"}
    git = shutil.which("git")
    if git and not shutil.which("sha256sum"):
        for parent in Path(git).resolve().parents:
            directory = parent / "usr/bin"
            if (directory / "sha256sum.exe").is_file():
                env["PATH"] += os.pathsep + str(directory)
                break
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
    skipped = [case.get("name") for case in root.iter("testcase") if case.find("skipped") is not None]
    display_only = {"test_display1_rect_comes_from_win32_not_from_qt_primary",
                    "test_dialog_uses_display1_rect_even_when_qt_primary_is_elsewhere"}
    permitted = display_only if os.environ.get("CUESCOPE_HEADLESS_CI") == "1" else set()
    complete = counts["tests"] > 0 and (not args.release or not set(skipped) - permitted)
    report = {
        "kind": "offline_automated_regression_and_lint", "finished_at": time.time(),
        "passed": all(check["passed"] for check in checks) and complete, "checks": checks,
        "test_counts": counts, "skipped_tests": skipped, "real_feishu_or_user_profile_accessed": False,
    }
    (OUTPUT / "automated-tests.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    snapshot_sources()
    return 0 if report["passed"] else 1


def source_hashes() -> dict[str, str]:
    """Hash the public build, documentation and test inputs using repository-relative paths."""
    paths = [path for directory in ("src", "tests", "tools", "packaging", ".github", "docs")
             for path in (ROOT / directory).rglob("*")
             if path.is_file() and path.suffix.lower() in {
                 ".py", ".spec", ".txt", ".json", ".lock", ".svg", ".png", ".ico", ".otf", ".md", ".yml", ".yaml",
             }]
    paths += [ROOT / name for name in (
        "README.md", "QUICK_START.md",
        "VALIDATION.md", "THIRD_PARTY_NOTICES.md",
        "pyproject.toml", "requirements.lock", "models/manifest.json", "LICENSE", ".gitignore",
    )]
    paths += [path for name in ("SECURITY.md", "CONTRIBUTING.md", "CHANGELOG.md", ".gitattributes")
              if (path := ROOT / name).is_file()]
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def snapshot_sources() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "source-snapshot.json").write_text(
        json.dumps(source_hashes(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    raise SystemExit(main())
