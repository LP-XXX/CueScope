"""Read one pinned model manifest in a checkout, frozen app, or installed wheel."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


def manifest_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "models" / "manifest.json"
    checkout = Path(__file__).resolve().parents[2] / "models" / "manifest.json"
    if checkout.is_file():
        return checkout
    # Hatch copies the same source manifest here when creating a wheel.
    return Path(__file__).resolve().parent / "assets" / "model-manifest.json"


def load_model_manifest(path: Path | None = None) -> dict:
    selected = path or manifest_path()
    try:
        manifest = json.loads(selected.read_text(encoding="utf-8"))
        files = manifest["files"]
        roles = manifest["roles"]
        if manifest.get("format_version") != 1 or set(roles) != {"det", "rec", "keys"}:
            raise ValueError("unsupported format or missing model roles")
        if not isinstance(files, dict) or not files:
            raise ValueError("empty model files")
        for name, metadata in files.items():
            if not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name:
                raise ValueError("model filename must be a basename")
            if not re.fullmatch(r"[a-f0-9]{64}", metadata["sha256"]):
                raise ValueError("invalid SHA-256")
            url = urlsplit(metadata["url"])
            if url.scheme != "https" or not url.netloc or url.username or url.password:
                raise ValueError("model source must be an HTTPS URL")
        if any(name not in files for name in roles.values()):
            raise ValueError("model role references an unknown file")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise RuntimeError(f"无法读取有效的模型清单：{selected.name}（{type(error).__name__}）") from error
    return manifest


def load_model_defaults() -> dict[str, str]:
    manifest = load_model_manifest()
    result = {}
    for role, filename in manifest["roles"].items():
        result[f"{role}_path"] = f"models/{filename}"
        result[f"{role}_sha256"] = manifest["files"][filename]["sha256"]
    return result
