"""Explicit content units for desktop text; no application/process-name detection."""
from __future__ import annotations

import math
from dataclasses import replace

from .models import Rect, TextBlock

CONTENT_MODES = {
    "generic_line": "通用文字 · 逐行",
    "text_block": "通用文字 · 指定文本块",
    "wechat_chat": "微信聊天预设",
}
MIN_GENERIC_CONFIDENCE = 0.5


def generic_content(blocks: list[TextBlock], mode: str) -> tuple[list[TextBlock], list[Rect]]:
    """Keep OCR detections separate by default; only explicitly selected blocks join.

    Never bridge low-confidence text, images, or different records. A selected text
    block is a user-defined unit, not automatic inference of arbitrary paragraphs.
    """
    if mode not in {"generic_line", "text_block"}:
        raise ValueError("未知通用文字模式")
    readable = []
    uncovered = []
    for block in sorted(blocks, key=lambda item: (item.box.y, item.box.x)):
        if block.kind != "text" or not math.isfinite(block.confidence) or not MIN_GENERIC_CONFIDENCE <= block.confidence <= 1.0:
            uncovered.append(block.box)
            continue
        lines = [line for line in block.text.splitlines() if line.strip()]
        for i, line in enumerate(lines):
            if not line:
                continue
            height = max(1, block.box.height // max(1, len(lines)))
            rect = Rect(block.box.x, block.box.y + i * height, block.box.width, height)
            readable.append(replace(block, text=line, box=rect, message_id=f"line:{len(readable)}"))
    if mode == "generic_line" or not readable:
        return readable, uncovered
    if uncovered:
        return [], [*uncovered, *(item.box for item in readable)]
    left = min(item.box.x for item in readable)
    top = min(item.box.y for item in readable)
    right = max(item.box.x + item.box.width for item in readable)
    bottom = max(item.box.y + item.box.height for item in readable)
    return [TextBlock(
        text="\n".join(item.text for item in readable),
        confidence=min(item.confidence for item in readable),
        box=Rect(left, top, right - left, bottom - top),
        message_id="selected-block",
    )], []
