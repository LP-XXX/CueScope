"""Export a locally generated allowlist of counts. Never read logs or user content."""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

from . import __version__

_DELIVERY_STATES = frozenset({
    "pending", "sent", "failed", "unknown", "expired", "submitting", "forwarded",
    "muted", "digest_counted",
})
_STAGES = frozenset({
    "capture", "worker_ready", "request", "decode", "ocr", "tracking", "record_write",
    "hit_image_write", "rule_evaluation", "storage", "frame_validation",
})


def support_counts(regions, snapshots) -> dict:
    return {
        "tasks": len(regions),
        "enabled_tasks": sum(bool(region.enabled) for region in regions),
        "capture_backends": dict(Counter(
            region.capture_backend if region.capture_backend in ("wgc", "printwindow") else "other"
            for region in regions)),
        "content_modes": dict(Counter(
            region.content_mode if region.content_mode in ("wechat_chat", "text_block", "generic_line") else "other"
            for region in regions)),
        "fault_stages": dict(Counter(
            snapshot.capture_error_stage if snapshot.capture_error_stage in _STAGES else "other"
            for snapshot in snapshots if snapshot.capture_error_stage)),
    }


def write_support_bundle(path: Path, counts: dict, queue: dict[str, int]) -> None:
    windows = sys.getwindowsversion() if sys.platform == "win32" else None
    safe_counts = {key: counts[key] for key in ("tasks", "enabled_tasks")
                   if type(counts.get(key)) is int}
    for key, allowed in (("capture_backends", {"wgc", "printwindow", "other"}),
                         ("content_modes", {"wechat_chat", "text_block", "generic_line", "other"}),
                         ("fault_stages", _STAGES | {"other"})):
        safe_counts[key] = {label: value for label, value in counts.get(key, {}).items()
                            if label in allowed and type(value) is int}
    manifest = {
        "format": 1,
        "application": __version__,
        "windows_build": windows.build if windows else None,
        "python_version": list(sys.version_info[:3]),
        "monitoring": safe_counts,
        "delivery_states": {key: value for key, value in queue.items()
                            if key in _DELIVERY_STATES and type(value) is int},
    }
    with NamedTemporaryFile(dir=path.parent, prefix="cuescope-support-", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("diagnostics.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
