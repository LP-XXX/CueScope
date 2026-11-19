"""下载锁定模型并验证 SHA-256；不接触用户配置或聊天数据。"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "models"
sys.path.insert(0, str(ROOT / "src"))
from deal_watch.model_catalog import load_model_manifest  # noqa: E402


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> None:
    DESTINATION.mkdir(parents=True, exist_ok=True)
    manifest = load_model_manifest(ROOT / "models" / "manifest.json")
    for filename, metadata in manifest["files"].items():
        target = DESTINATION / filename
        if target.exists() and digest(target) == metadata["sha256"]:
            print(f"verified {filename}")
            continue
        temporary = target.with_suffix(target.suffix + ".download")
        print(f"downloading {filename}", flush=True)
        with urllib.request.urlopen(metadata["url"], timeout=30) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        actual = digest(temporary)
        if actual != metadata["sha256"]:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"SHA-256 mismatch for {filename}: {actual}")
        os.replace(temporary, target)
        print(f"verified {filename} {actual}")



if __name__ == "__main__":
    sys.exit(main())
