"""使用合成文字图验证真实 PP-OCRv6 推理；不读取聊天或桌面截图。"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from deal_watch.config import ModelConfig
from deal_watch.models import FrameSample, Rect
from deal_watch.ocr import LocalOcr

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    image = Image.new("RGB", (1000, 480), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 30)
    lines = ["普通聊天消息", "苹果手机 15 Pro 256GB", "限时优惠 4999 元", "型号 ABC-123"]
    for index, text in enumerate(lines):
        draw.text((35, 35 + index * 100), text, font=font, fill="black")
    frame = FrameSample("synthetic", 1, time.time(), np.asarray(image), Rect(0, 0, 1000, 480))
    engine = LocalOcr(ModelConfig(), ROOT)
    for iteration in range(3):
        result = engine.recognize(frame)
        texts = [block.text for block in result.blocks]
        joined = "\n".join(texts)
        print(f"OCR iteration {iteration + 1}: {result.inference_ms:.1f} ms")
        print(f"Uncovered image blocks: {len(result.uncovered_boxes)}")
        if iteration == 0:
            print(joined)
        if "4999" not in joined or "手机" not in joined:
            raise RuntimeError("合成图推理缺少预期文字")
    print("Synthetic OCR smoke test: OK (仅合成图，不代表四窗现场 P95)")


if __name__ == "__main__":
    main()
