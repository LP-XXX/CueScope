"""显式本地 RapidOCR 模型适配；以气泡底色分隔独立面板并保守组装消息。"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np

from .config import ModelConfig
from .metrics import OcrStageSample
from .models import FrameSample, OcrObservation, Rect, TextBlock

# 面板几何阈值。数值来自 0.2.1 计划第三节的可验收要求，改动前必须同步测试。
_MIN_PANEL_WIDTH = 24
_MIN_PANEL_HEIGHT = 18
_MAX_PANEL_AREA_RATIO = 0.95
_FOREGROUND_DELTA = 4
_FRAME_BORDER_MARGIN = 3
# 均匀边框检测上限与判据：PrintWindow 帧可能带 8–11px 纯色边框。
_BORDER_LIMIT = 16
_BORDER_UNIFORM_DELTA = 6.0
_BRIGHT_GRAY = 200.0
_MIN_BRIGHT_RING_RATIO = 0.2
_BUBBLE_CONTRAST = 2.0
_BUBBLE_FILL_RATIO = 0.5
_BACKGROUND_ROW_RATIO = 0.02
_MIN_BACKGROUND_RUN = 2

# 粘连面板二次切分阈值。
_ROW_CLUSTER_GAP = 4
_SPLIT_MIN_GAP = 10
_SPLIT_EDGE_DELTA = 12
_FILL_BAND_OFFSET = 2
_FILL_BAND_HEIGHT = 6
_FILL_DIFF = 6.0
_TALL_PANEL_HEIGHT_TOLERANCE = 1.6
_UNCERTAIN_PANEL_RATIO = 2.2
_UNCERTAIN_CONTENT_RATIO = 0.5
# 单面板帧没有同帧参照时按帧高/行高估算，判据更保守（2.6× 且可疑行距 ≥12px）。
_SINGLE_PANEL_HEIGHT_RATIO = 0.28
_SINGLE_PANEL_LINE_SPAN = 4
_UNCERTAIN_SINGLE_RATIO = 2.6
_SUSPICIOUS_ROW_GAP = 12

# 图片/卡片证据阈值。
_IMAGE_OVERLAP_RATIO = 0.6
_CARD_MIN_ROWS = 3
_CARD_FOOTER_WIDTH_RATIO = 0.55

# 大尺寸彩色纹理种子（0.2.0 起沿用）。
_COLOR_SEED_TEXTURE = 42.0
_COLOR_SEED_CHROMA = 16.0
_COLOR_SEED_MIN_TILES = 6
_COLOR_SEED_MIN_SIDE = 72

# 小尺寸照片种子：商品/笔记卡片的缩略图只有 40–70px，彩色门槛抓不到。
_PHOTO_TILE_MIN_SIZE = 40
_PHOTO_TILE_MIN_TILES = 3
_PHOTO_TILE_MIN_TEXTURE = 6.0
_PHOTO_TILE_MAX_DOMINANT = 0.65
_PHOTO_TILE_MIN_GRAY_STD = 8.0
_PHOTO_TILE_MAX_TOP2 = 0.72
_PHOTO_TILE_MIN_LEVELS = 8
_PHOTO_TILE_MIN_COLOR_RATIO = 0.12
_PHOTO_TILE_MIN_EDGE = 1.5
_PHOTO_TILE_MAX_EDGE = 48.0

# 浅色卡片结构证据：以缩略图为种子，逐行/逐列向外试探卡片边界。
# 真实卡片底色只比页面背景亮 3 级（253 / 250），分隔带甚至只有 2 级，
# 因此“卡片内部”的判据取“与背景差 ≥2 级”，而不是“明显更亮的实心块”。
_CARD_CONTENT_DELTA = 2.0
_CARD_BODY_RATIO = 0.5
_CARD_EXPAND_LIMIT = 320
_CARD_MAX_AREA_RATIO = 0.5
# 卡片内部文字也是卡片的一部分：只按底色判断会在密集文字行停下，把无边框
# 浅色卡片切成两段（真实案例：339x233 的卡片只扩到 225x150，卡内 6 行文字
# 全部漏成普通文字）。用“墨水占比”区分可跨越的行：卡片文字行是“少量深色
# 墨水 + 大量卡片底色”，绿气泡/深色照片接近整行墨水，立即停止，因此不会把
# 相邻气泡并进卡片。
_CARD_INK_DELTA = 24.0
_CARD_INK_RATIO = 0.8

# 预览页“头像型”小块的剔除阈值：小面积、无文字、位于左侧头像栏。
_AVATAR_MAX_WIDTH = 88
_AVATAR_MAX_HEIGHT = 120
_AVATAR_LEFT_RATIO = 0.25

# 文字压在图/照片上的判据。
_PHOTO_RING = 4
_PHOTO_DOMINANT_MAX = 0.65
_PHOTO_LEVEL_RATIO_MIN = 0.12
_PHOTO_GRAY_STD_MIN = 8.0
_PHOTO_CHANNEL_STD_MIN = 6.0
_PHOTO_RING_MIN_PIXELS = 32

_CARD_FOOTER_MARKERS = ("查看全部聊天记录", "点击查看", "聊天记录", "转发")
_CARD_FOOTER_PATTERNS = (re.compile(r"共\s*\d+\s*条"),)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class BubblePanel:
    """一个独立气泡面板；certain=False 表示无法确认边界，其文字不参与组装。"""

    rect: Rect
    certain: bool = True


def _as_color_patch(patch: np.ndarray) -> np.ndarray:
    """统一成三通道视图；灰度或带 alpha 的截图也能参与通道差分。"""
    if patch.ndim == 2:
        return np.repeat(patch[:, :, None], 3, axis=2)
    if patch.shape[2] >= 4:
        return patch[:, :, :3]
    if patch.shape[2] == 1:
        return np.repeat(patch, 3, axis=2)
    return patch


def _edge_ring_pixels(image: np.ndarray, band: int) -> np.ndarray:
    """取图像四周 band 像素宽环带的像素（N×3 float32）。"""
    parts = [
        _as_color_patch(image[:band]).reshape(-1, 3),
        _as_color_patch(image[-band:]).reshape(-1, 3),
        _as_color_patch(image[:, :band]).reshape(-1, 3),
        _as_color_patch(image[:, -band:]).reshape(-1, 3),
    ]
    return np.concatenate(parts, axis=0).astype(np.float32)


def _bright_mode(image: np.ndarray) -> np.ndarray | None:
    """整幅出现频次最高的亮色；用于粗深色边框吃掉整个环带时兜底。"""
    pixels = _as_color_patch(image).reshape(-1, 3)
    gray = pixels.mean(axis=1)
    bright = pixels[gray > _BRIGHT_GRAY]
    if len(bright) < 64:
        return None
    quantized = (bright // 8).astype(np.int32)
    keys = quantized[:, 0] * 1024 + quantized[:, 1] * 32 + quantized[:, 2]
    values, counts = np.unique(keys, return_counts=True)
    best = int(values[int(np.argmax(counts))])
    target = np.array([best // 1024, (best // 32) % 32, best % 32], dtype=np.int32)
    same = np.all(quantized == target, axis=1)
    return np.median(bright[same], axis=0)


def page_background(image: np.ndarray) -> np.ndarray:
    """从图像外缘环带估计页面背景色。

    消息气泡经常占过半画面，取整幅中位数会把气泡灰色当成背景；窗口边框是
    深色时（例如 2px 深灰描边），外缘中位数会变成边框色，导致整幅被判前景。
    因此只取环带里的亮色（灰度>200）中位色，环带全黑时逐级加宽，
    仍然拿不到再用整幅亮色众数兜底；深色主题才回退到环带中位色。
    """
    height, width = image.shape[:2]
    max_band = max(1, min(height, width) // 8)
    fallback: np.ndarray | None = None
    for band in (2, 4, 8, 16):
        if band > max_band:
            break
        pixels = _edge_ring_pixels(image, band)
        if fallback is None:
            fallback = np.median(pixels, axis=0)
        bright = pixels[pixels.mean(axis=1) > _BRIGHT_GRAY]
        needed = max(16, int(len(pixels) * _MIN_BRIGHT_RING_RATIO))
        if len(bright) >= needed:
            return np.median(bright, axis=0)
    mode = _bright_mode(image)
    if mode is not None:
        return mode
    return fallback if fallback is not None else np.median(_edge_ring_pixels(image, 1), axis=0)


def _frame_border(image: np.ndarray) -> tuple[int, int, int, int]:
    """检出“整行/整列与自身中位数偏差 <6”的均匀边框宽度（上限 16px）。

    PrintWindow 帧可能带 8–11px 纯色边框。边框本身就是“与页面背景不同”的
    前景，会形成一圈闭合外轮廓，使 ``RETR_EXTERNAL`` 只返回整幅矩形；
    拿到真实边框宽度才能把它清掉（写死 3px 只能挡住 2–3px 的描边）。
    """
    pixels = _as_color_patch(image).astype(np.float32)
    height, width = pixels.shape[:2]
    limit_v = max(0, min(_BORDER_LIMIT, height // 4))
    limit_h = max(0, min(_BORDER_LIMIT, width // 4))

    def uniform(line: np.ndarray) -> bool:
        flat = line.reshape(-1, line.shape[-1])
        median = np.median(flat, axis=0)
        return bool(float(np.abs(flat - median).max()) < _BORDER_UNIFORM_DELTA)

    top = 0
    while top < limit_v and uniform(pixels[top]):
        top += 1
    bottom = 0
    while bottom < limit_v and uniform(pixels[height - 1 - bottom]):
        bottom += 1
    left = 0
    while left < limit_h and uniform(pixels[:, left]):
        left += 1
    right = 0
    while right < limit_h and uniform(pixels[:, width - 1 - right]):
        right += 1
    return (top, bottom, left, right)


def _foreground_mask(image: np.ndarray, background: np.ndarray) -> np.ndarray:
    """前景=与页面背景最大通道差>4 的像素；只用 3×3 闭运算去掉 1px 噪点。

    真实窗口采集的帧四周常有 2–11px 的均匀描边，它会形成一圈闭合外轮廓，
    使 RETR_EXTERNAL 只返回整幅矩形；先清掉检出的边框（至少 3px 保险）。
    """
    import cv2

    delta = np.abs(_as_color_patch(image).astype(np.float32) - background).max(axis=2)
    mask = (delta > _FOREGROUND_DELTA).astype(np.uint8) * 255
    height, width = mask.shape[:2]
    top, bottom, left, right = _frame_border(image)
    top = max(top, _FRAME_BORDER_MARGIN)
    bottom = max(bottom, _FRAME_BORDER_MARGIN)
    left = max(left, _FRAME_BORDER_MARGIN)
    right = max(right, _FRAME_BORDER_MARGIN)
    if height > top + bottom + 4 and width > left + right + 4:
        mask[:top] = 0
        mask[height - bottom :] = 0
        mask[:, :left] = 0
        mask[:, width - right :] = 0
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), dtype=np.uint8))


def _has_bubble_fill(image: np.ndarray, rect: Rect, background: np.ndarray) -> bool:
    """校验矩形内部确有区别于页面背景的气泡底色。

    分隔线、滚动条、窗口边框这类细长前景没有气泡底色，必须丢弃；
    页面上直接显示的文字（时间戳、系统提示）内部以页面背景为主，同样丢弃。
    """
    inset = 2
    height, width = image.shape[:2]
    left = rect.x + inset
    top = rect.y + inset
    right = min(width, rect.x + rect.width - inset)
    bottom = min(height, rect.y + rect.height - inset)
    if right - left < 2 or bottom - top < 2:
        return False
    patch = _as_color_patch(image[top:bottom, left:right]).astype(np.float32)
    delta = np.abs(patch - background).max(axis=2)
    solid = delta > _FOREGROUND_DELTA
    if float(solid.mean()) < _BUBBLE_FILL_RATIO:
        return False
    median_color = np.median(patch[solid], axis=0)
    return bool(np.abs(median_color - background).max() > _BUBBLE_CONTRAST)


def _split_by_background_rows(mask: np.ndarray, rect: Rect) -> list[Rect]:
    """在同一前景块内部按“页面背景行”切分相邻气泡。

    连续 ≥2 行几乎全为背景像素，说明这里是两个气泡之间的空隙。
    """
    sub = mask[rect.y : rect.y + rect.height, rect.x : rect.x + rect.width]
    if sub.size == 0:
        return [rect]
    counts = (sub > 0).sum(axis=1)
    limit = max(1, round(rect.width * _BACKGROUND_ROW_RATIO))
    is_background = counts <= limit
    segments: list[tuple[int, int]] = []
    cursor = 0
    index = 0
    while index < rect.height:
        if not is_background[index]:
            index += 1
            continue
        start = index
        while index < rect.height and is_background[index]:
            index += 1
        if index - start < _MIN_BACKGROUND_RUN:
            continue
        # 只在面板内部切分，且不产生退化片段。
        if start > cursor and index < rect.height and start - cursor >= _MIN_PANEL_HEIGHT:
            segments.append((cursor, start))
            cursor = index
    segments.append((cursor, rect.height))
    if len(segments) < 2:
        return [rect]
    pieces: list[Rect] = []
    for top, bottom in segments:
        if bottom - top < _MIN_PANEL_HEIGHT:
            return [rect]
        pieces.append(Rect(rect.x, rect.y + top, rect.width, bottom - top))
    return pieces


@dataclass(frozen=True, slots=True)
class _RowCluster:
    top: int
    bottom: int
    left: int
    right: int


def _row_clusters(blocks: Sequence[TextBlock]) -> list[_RowCluster]:
    """把文字块按垂直相邻关系聚成文字行；同气泡多行仍是多个行簇。"""
    ordered = sorted(blocks, key=lambda block: (block.box.y, block.box.x))
    clusters: list[list[int]] = []
    for block in ordered:
        top = block.box.y
        bottom = block.box.y + block.box.height
        left = block.box.x
        right = block.box.x + block.box.width
        if clusters and top <= clusters[-1][1] + _ROW_CLUSTER_GAP:
            current = clusters[-1]
            current[0] = min(current[0], top)
            current[1] = max(current[1], bottom)
            current[2] = min(current[2], left)
            current[3] = max(current[3], right)
        else:
            clusters.append([top, bottom, left, right])
    return [_RowCluster(*values) for values in clusters]


def _contains_point(rect: Rect, point: tuple[float, float]) -> bool:
    x, y = point
    return rect.x <= x < rect.x + rect.width and rect.y <= y < rect.y + rect.height


def _intersection_area(first: Rect, second: Rect) -> int:
    width = min(first.x + first.width, second.x + second.width) - max(first.x, second.x)
    height = min(first.y + first.height, second.y + second.height) - max(first.y, second.y)
    if width <= 0 or height <= 0:
        return 0
    return width * height


def _union_rect(rects: Sequence[Rect]) -> Rect:
    left = min(rect.x for rect in rects)
    top = min(rect.y for rect in rects)
    right = max(rect.x + rect.width for rect in rects)
    bottom = max(rect.y + rect.height for rect in rects)
    return Rect(left, top, max(1, right - left), max(1, bottom - top))


def _merge_boxes(rects: Sequence[Rect]) -> list[Rect]:
    result: list[Rect] = []
    for rect in rects:
        if rect not in result:
            result.append(rect)
    return result


def _drop_contained(rects: Sequence[Rect]) -> list[Rect]:
    """去掉被其它框基本包住的重复框。

    同一张卡片可能被轮廓法和卡片扩展各报一次；重复框会让 UI 画出双层红框，
    也会让“未覆盖区域”数量虚高。
    """
    kept: list[Rect] = []
    for rect in sorted(rects, key=lambda item: item.width * item.height, reverse=True):
        area = rect.width * rect.height
        if any(_intersection_area(rect, other) >= area * 0.9 for other in kept):
            continue
        kept.append(rect)
    return sorted(kept, key=lambda item: (item.y, item.x))


def _band_median(
    image: np.ndarray, rect: Rect, top: int, bottom: int
) -> np.ndarray | None:
    """取面板内某一段横向条带的中位色；文字像素是少数，中位数即底色。"""
    height, width = image.shape[:2]
    clip_top = max(rect.y, min(height, top))
    clip_bottom = max(clip_top + 1, min(height, bottom))
    left = max(0, rect.x)
    right = min(width, rect.x + rect.width)
    if right - left < 2 or clip_bottom - clip_top < 1:
        return None
    patch = _as_color_patch(image[clip_top:clip_bottom, left:right]).astype(np.float32)
    return np.median(patch.reshape(-1, 3), axis=0)


def _split_with_text_rows(
    image: np.ndarray, rect: Rect, blocks: Sequence[TextBlock]
) -> list[Rect]:
    """借助 OCR 行簇在粘连面板里找出已确认的两个气泡边界。

    只有同时满足三个证据才切分：相邻行簇垂直空隙 ≥10px、左右边界差 >12px、
    并且空隙上下两侧的**气泡底色不同**。同一气泡里的分段空隙底色相同，
    因此不会被切断（真实微信长消息常用空行分段）。
    """
    inside = [block for block in blocks if _contains_point(rect, block.box.center)]
    clusters = _row_clusters(inside)
    if len(clusters) < 2:
        return []
    cuts: list[int] = []
    for previous, following in zip(clusters, clusters[1:], strict=False):
        gap = following.top - previous.bottom
        if gap < _SPLIT_MIN_GAP:
            continue
        if (
            abs(previous.left - following.left) <= _SPLIT_EDGE_DELTA
            and abs(previous.right - following.right) <= _SPLIT_EDGE_DELTA
        ):
            continue
        cut = (previous.bottom + following.top) // 2
        above = _band_median(
            image, rect, cut - _FILL_BAND_OFFSET - _FILL_BAND_HEIGHT, cut - _FILL_BAND_OFFSET
        )
        below = _band_median(
            image, rect, cut + _FILL_BAND_OFFSET, cut + _FILL_BAND_OFFSET + _FILL_BAND_HEIGHT
        )
        if above is None or below is None:
            continue
        if float(np.abs(above - below).max()) < _FILL_DIFF:
            continue
        cuts.append(cut)
    if not cuts:
        return []
    relative = sorted({max(1, min(rect.height - 1, cut - rect.y)) for cut in cuts})
    edges = [0, *relative, rect.height]
    pieces: list[Rect] = []
    for top, bottom in zip(edges, edges[1:], strict=False):
        if bottom - top < _MIN_PANEL_HEIGHT:
            return []
        pieces.append(Rect(rect.x, rect.y + top, rect.width, bottom - top))
    return pieces if len(pieces) >= 2 else []


def _reference_height(pieces: Sequence[Rect], rect: Rect) -> float | None:
    others = [piece.height for piece in pieces if piece is not rect]
    if not others:
        return None
    return float(median(others))


def _fallback_reference(frame_height: int, blocks: Sequence[TextBlock]) -> float:
    """帧内只有一个面板时的参照高度。

    没有其它面板可比，只能按“一屏大约放得下几个气泡”和文字行高估算：
    参照 = max(0.28 × 帧高, 4 × 中位文字行高)。
    """
    heights = [block.box.height for block in blocks if block.box.height > 0]
    line = float(median(heights)) if heights else 18.0
    return max(_SINGLE_PANEL_HEIGHT_RATIO * frame_height, _SINGLE_PANEL_LINE_SPAN * line)


def _max_row_gap(rect: Rect, blocks: Sequence[TextBlock]) -> int:
    """面板内相邻文字行簇的最大垂直空隙；用于发现“看起来像两段”的粘连。"""
    inside = [block for block in blocks if _contains_point(rect, block.box.center)]
    clusters = _row_clusters(inside)
    if len(clusters) < 2:
        return 0
    return max(
        following.top - previous.bottom
        for previous, following in zip(clusters, clusters[1:], strict=False)
    )


def _panel_content_height(rect: Rect, blocks: Sequence[TextBlock]) -> int:
    """面板内文字内容占据的垂直高度；远小于面板高度说明里面还有读不到的东西。"""
    inside = [block for block in blocks if _contains_point(rect, block.box.center)]
    if not inside:
        return 0
    top = min(block.box.y for block in inside)
    bottom = max(block.box.y + block.box.height for block in inside)
    return max(0, bottom - top)


def _frame_background(image: np.ndarray, background: np.ndarray | None = None) -> np.ndarray:
    """复用同一帧已算好的页面背景；一帧只扫描一次外缘环带。

    调用方（``LocalOcr.recognize``）先算一次并沿调用链下传，避免
    ``ImageBlockFilter.find`` 与 ``find_bubbles`` 各算一遍（4 窗轮询时每帧
    多花数十毫秒）。``background=None`` 时行为与 ``page_background`` 完全一致。
    """
    return page_background(image) if background is None else background


def _drop_avatar_boxes(
    image: np.ndarray, rects: Sequence[Rect], blocks: Sequence[TextBlock]
) -> list[Rect]:
    """剔除预览页左侧“头像型”小块，避免把头像画成未覆盖红框。

    同时满足三条才剔除：宽 <88 且高 <120、没有任何文字块与之相交、
    块中心落在帧左 25% 区域。真正的商品卡片/图片面板通常更大或带文字，
    因此仍会保留在未覆盖列表里。
    """
    height, width = image.shape[:2]
    if height <= 0 or width <= 0:
        return list(rects)
    left_limit = width * _AVATAR_LEFT_RATIO
    kept: list[Rect] = []
    for rect in rects:
        if rect.width >= _AVATAR_MAX_WIDTH or rect.height >= _AVATAR_MAX_HEIGHT:
            kept.append(rect)
            continue
        if rect.center[0] >= left_limit:
            kept.append(rect)
            continue
        if any(_intersection_area(rect, block.box) > 0 for block in blocks):
            kept.append(rect)
            continue
    return kept


def find_bubbles(
    image: np.ndarray,
    blocks: Sequence[TextBlock] = (),
    *,
    background: np.ndarray | None = None,
) -> list[BubblePanel]:
    """按页面背景与气泡底色分隔独立面板。

    返回顺序按 (y, x) 稳定排列；certain=False 的面板表示“疑似多气泡粘连但
    无法确认边界”，其文字不允许进入消息组装。
    """
    height, width = image.shape[:2]
    if height < 80 or width < 80:
        return []
    background = _frame_background(image, background)
    mask = _foreground_mask(image, background)
    import cv2

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    frame_area = height * width
    base: list[Rect] = []
    for contour in contours:
        x, y, panel_width, panel_height = cv2.boundingRect(contour)
        if panel_width < _MIN_PANEL_WIDTH or panel_height < _MIN_PANEL_HEIGHT:
            continue
        if panel_width * panel_height > frame_area * _MAX_PANEL_AREA_RATIO:
            continue
        rect = Rect(int(x), int(y), int(panel_width), int(panel_height))
        if not _has_bubble_fill(image, rect, background):
            continue
        base.append(rect)
    pieces: list[Rect] = []
    for rect in sorted(base, key=lambda item: (item.y, item.x)):
        pieces.extend(_split_by_background_rows(mask, rect))
    pieces.sort(key=lambda item: (item.y, item.x))

    panels: list[BubblePanel] = []
    for rect in pieces:
        reference = _reference_height(pieces, rect)
        single_panel = reference is None
        if single_panel:
            # 只有一帧一个面板时也要能识别“两段被粘连”：用帧高/行高估参照。
            reference = _fallback_reference(height, blocks)
        assert reference is not None
        # 先试“文字行簇空隙 + 上下底色突变”的切分，不依赖面板高度：
        # 两个 0px 粘连、底色不同的小气泡合成一个面板后，高度可能并不可疑
        # （135×75 远低于参照的 1.6 倍），只靠高度门槛会漏切、把两段拼成一条。
        # 证据仍是三件套：空隙 ≥10px、左右边界差 >12px、空隙上下底色差 >6。
        split = _split_with_text_rows(image, rect, blocks)
        if split:
            panels.extend(BubblePanel(part) for part in split)
            continue
        if rect.height > reference * _TALL_PANEL_HEIGHT_TOLERANCE:
            if single_panel:
                # 单面板帧：又高、文字只占其中一小部分（或出现 ≥12px 的可疑行距）
                # → 可能是 0px 粘连或漏读内容，不拼字。
                uncertain = rect.height > reference * _UNCERTAIN_SINGLE_RATIO and (
                    _panel_content_height(rect, blocks)
                    < rect.height * _UNCERTAIN_CONTENT_RATIO
                    or _max_row_gap(rect, blocks) >= _SUSPICIOUS_ROW_GAP
                )
            else:
                # 面板明显高于同帧其它面板，而文字内容只占其中一小部分：
                # 里面还有读不到的东西（图片/卡片/未知版式），不声称判断正确。
                uncertain = (
                    rect.height > reference * _UNCERTAIN_PANEL_RATIO
                    and _panel_content_height(rect, blocks)
                    < rect.height * _UNCERTAIN_CONTENT_RATIO
                )
            if uncertain:
                panels.append(BubblePanel(rect, certain=False))
                continue
        panels.append(BubblePanel(rect))
    return panels


def _ring_pixels(image: np.ndarray, box: Rect, ring: int) -> np.ndarray | None:
    """取文字框外侧的窄环带像素；聊天气泡内环带均匀，照片环带杂乱。"""
    height, width = image.shape[:2]
    left = max(0, box.x - ring)
    top = max(0, box.y - ring)
    right = min(width, box.x + box.width + ring)
    bottom = min(height, box.y + box.height + ring)
    if right - left <= 2 * ring or bottom - top <= 2 * ring:
        return None
    patch = _as_color_patch(image[top:bottom, left:right]).astype(np.float32)
    keep = np.ones(patch.shape[:2], dtype=bool)
    inner_left = max(0, box.x - left)
    inner_top = max(0, box.y - top)
    inner_right = min(right - left, box.x + box.width - left)
    inner_bottom = min(bottom - top, box.y + box.height - top)
    keep[inner_top:inner_bottom, inner_left:inner_right] = False
    pixels = patch[keep]
    return pixels if pixels.size else None


def text_on_photo(image: np.ndarray, box: Rect) -> bool:
    """判断文字是否压在图/照片上（灰度图里的文字同样适用）。

    气泡内文字四周是均匀底色：环带以单色为主、灰度标准差接近 0。
    照片环带颜色杂乱：主色占比低、灰度或颜色标准差高。
    阈值由合成样本标定，宁可漏判也不要声称识别正确。
    """
    pixels = _ring_pixels(image, box, _PHOTO_RING)
    if pixels is None or len(pixels) < _PHOTO_RING_MIN_PIXELS:
        return False
    gray = pixels.mean(axis=1)
    median_gray = float(np.median(gray))
    dominant = float((np.abs(gray - median_gray) <= 8.0).mean())
    if dominant >= _PHOTO_DOMINANT_MAX:
        return False
    level_ratio = np.unique(gray.astype(np.uint8)).size / min(len(gray), 256)
    if level_ratio < _PHOTO_LEVEL_RATIO_MIN:
        return False
    gray_std = float(gray.std())
    channel_std = float(pixels.std(axis=0).mean())
    return gray_std >= _PHOTO_GRAY_STD_MIN or channel_std >= _PHOTO_CHANNEL_STD_MIN


def _footer_marker(text: str) -> bool:
    folded = text.casefold()
    if any(marker.casefold() in folded for marker in _CARD_FOOTER_MARKERS):
        return True
    return any(pattern.search(text) for pattern in _CARD_FOOTER_PATTERNS)


def _looks_like_forwarded_card(group: Sequence[TextBlock]) -> bool:
    """卡片结构证据：≥3 行，末行是明显更窄的底栏且命中底栏标记。

    仅文本出现“聊天记录”不构成证据；单个文字块永远按普通文字处理。
    """
    if len(group) < _CARD_MIN_ROWS:
        return False
    rows = _row_clusters(group)
    if len(rows) < _CARD_MIN_ROWS:
        return False
    last = rows[-1]
    footer = "".join(
        block.text
        for block in group
        if block.box.y + block.box.height >= last.bottom - 2
    )
    if not _footer_marker(footer):
        return False
    last_width = last.right - last.left
    widest = max(row.right - row.left for row in rows)
    return last_width < widest * _CARD_FOOTER_WIDTH_RATIO


def _group_kind(group: Sequence[TextBlock], image_boxes: Sequence[Rect]) -> str:
    """图片/卡片判定用证据组合，而不是单个词。"""
    union = _union_rect([block.box for block in group])
    if image_boxes:
        area = union.width * union.height
        if area > 0:
            covered = sum(_intersection_area(union, box) for box in image_boxes)
            if min(1.0, covered / area) >= _IMAGE_OVERLAP_RATIO:
                return "image_card"
    if _looks_like_forwarded_card(group):
        return "forwarded_card"
    return "text"


def assign_messages(
    image: np.ndarray,
    blocks: list[TextBlock],
    image_boxes: Sequence[Rect] = (),
    *,
    background: np.ndarray | None = None,
) -> tuple[list[TextBlock], list[Rect]]:
    """给文字块分配 message_id 与 kind，返回（文字块，新增的未覆盖矩形）。

    规则：
    - 同一确定面板内的文字块共享 message_id，多行商品合集保持完整；
    - 不在任何确定面板内的文字块保持独立（不跨块拼字）；
    - 不确定面板内的文字块直接丢弃，面板矩形并入未覆盖；
    - 图片面板重叠 ≥0.6 判为 image_card，卡片结构证据判为 forwarded_card；
    - 文字压在图/照片上的块丢弃并并入未覆盖；
    - 未覆盖矩形里剔除左侧无文字的头像型小块（预览页头像不画红框）。
    """
    panels = list(enumerate(find_bubbles(image, blocks, background=background)))
    image_panels = list(image_boxes)
    assigned: dict[int, str] = {}
    dropped: set[int] = set()
    uncovered: list[Rect] = []

    for index, block in enumerate(blocks):
        center = block.box.center
        certain = [
            (panel_index, panel)
            for panel_index, panel in panels
            if panel.certain
            and _contains_point(panel.rect, center)
            and panel.rect.width * panel.rect.height >= block.box.width * block.box.height * 1.2
        ]
        if certain:
            panel_index, _panel = min(
                certain, key=lambda item: item[1].rect.width * item[1].rect.height
            )
            assigned[index] = f"bubble-{panel_index:04d}"
            continue
        unsure = [
            panel
            for _panel_index, panel in panels
            if not panel.certain and _contains_point(panel.rect, center)
        ]
        if unsure:
            dropped.add(index)
            uncovered.extend(panel.rect for panel in unsure)

    for index, block in enumerate(blocks):
        if index in dropped:
            continue
        if text_on_photo(image, block.box):
            dropped.add(index)
            assigned.pop(index, None)
            uncovered.append(block.box)

    grouped: dict[str, list[int]] = {}
    singles: list[int] = []
    for index in range(len(blocks)):
        if index in dropped:
            continue
        message_id = assigned.get(index, "")
        if message_id:
            grouped.setdefault(message_id, []).append(index)
        else:
            singles.append(index)

    kinds: dict[int, str] = {}
    for members in grouped.values():
        kind = _group_kind([blocks[index] for index in members], image_panels)
        for index in members:
            kinds[index] = kind
    for index in singles:
        kinds[index] = _group_kind([blocks[index]], image_panels)

    result = [
        TextBlock(
            text=blocks[index].text,
            confidence=blocks[index].confidence,
            box=blocks[index].box,
            kind=kinds.get(index, blocks[index].kind),
            message_id=assigned.get(index, ""),
        )
        for index in range(len(blocks))
        if index not in dropped
    ]
    return result, _drop_avatar_boxes(image, _merge_boxes(uncovered), blocks)


def tile_photo_stats(patch: np.ndarray) -> dict[str, float]:
    """一个小图块的色调统计；照片与文字块在这些量上差别明显。

    返回：灰度标准差、灰度直方图最高两档占比（双峰程度）、占有层级数、
    唯一量化色占比、相邻像素平均绝对差（边缘密度）。
    """
    pixels = _as_color_patch(np.asarray(patch)).astype(np.float32)
    gray = pixels.mean(axis=2)
    height, width = gray.shape[:2]
    if height < 4 or width < 4:
        return {
            "gray_std": 0.0, "level_top2": 1.0, "levels": 0.0,
            "color_ratio": 0.0, "edge": 0.0,
        }
    levels = np.clip((gray // 8.0).astype(np.int32), 0, 31)
    histogram = np.bincount(levels.reshape(-1), minlength=32).astype(np.float64)
    histogram /= max(1.0, float(histogram.sum()))
    top2 = float(np.sort(histogram)[-2:].sum())
    occupied = float((histogram >= 0.004).sum())
    quantized = np.clip(pixels // 8.0, 0, 31).astype(np.int32)
    keys = quantized[:, :, 0] * 1024 + quantized[:, :, 1] * 32 + quantized[:, :, 2]
    color_ratio = float(np.unique(keys).size / max(1, keys.size))
    horizontal = float(np.abs(np.diff(gray, axis=1)).mean()) if width > 1 else 0.0
    vertical = float(np.abs(np.diff(gray, axis=0)).mean()) if height > 1 else 0.0
    return {
        "gray_std": float(gray.std()),
        "level_top2": top2,
        "levels": occupied,
        "color_ratio": color_ratio,
        "edge": (horizontal + vertical) / 2.0,
    }


def looks_like_photo(patch: np.ndarray) -> bool:
    """照片图块判据（独立函数，便于单测与阈值标定）。

    文字图块的典型特征是“双峰灰度 + 极少唯一色 + 高边缘密度”；
    照片图块是“灰度层级连续 + 唯一色多 + 亮度方差大 + 中等边缘密度”。
    色度低（灰度照片）同样成立，因此不看色度只看层级连续性。
    """
    stats = tile_photo_stats(patch)
    if stats["gray_std"] < _PHOTO_TILE_MIN_GRAY_STD:
        return False
    if stats["level_top2"] > _PHOTO_TILE_MAX_TOP2:
        return False
    if (
        stats["levels"] < _PHOTO_TILE_MIN_LEVELS
        and stats["color_ratio"] < _PHOTO_TILE_MIN_COLOR_RATIO
    ):
        return False
    return _PHOTO_TILE_MIN_EDGE <= stats["edge"] <= _PHOTO_TILE_MAX_EDGE


def _card_content_mask(delta: np.ndarray) -> np.ndarray:
    """卡片内部候选像素：与页面背景存在可见差异（≥2 级）。整帧只算一次。"""
    return delta >= _CARD_CONTENT_DELTA


def _card_ink_mask(delta: np.ndarray) -> np.ndarray:
    """卡片内部文字/图标的“墨水”掩码；整帧只算一次。"""
    return delta > _CARD_INK_DELTA


def _light_card_region(content: np.ndarray, ink: np.ndarray, seed: Rect) -> Rect:
    """从照片种子向外扩到浅色卡片边界。

    商品/笔记卡片常常没有明显边框（白底卡片直接贴在浅灰页面上），
    轮廓法只能拿到缩略图本身；这里沿四个方向逐行/逐列试探，
    只要相邻行列仍属“卡片内部”就继续扩展，遇到页面背景或成片深色块
    立即停止。扩展距离有上限，不会吞掉整屏。

    判据是“两条证据的并集”：``content``（与页面背景差 ≥2 级）或 ``ink``
    （明显深于背景的墨水）。同一行/列里墨水占比超过 :data:`_CARD_INK_RATIO`
    视为成片深色（绿气泡、深色商品图），不算卡片内部；卡片自己的文字行
    墨水占比通常 0.2–0.65，因此能跨过去。
    """
    height, width = content.shape[:2]
    top = max(0, seed.y)
    bottom = min(height, seed.y + seed.height)
    left = max(0, seed.x)
    right = min(width, seed.x + seed.width)
    if right - left < 2 or bottom - top < 2:
        return seed
    limit = _CARD_EXPAND_LIMIT
    # 每行/每列在试探范围内的占比先整体算好，再逐行推进；
    # 逐行单独求 mean 会让 4 帧各多花 200ms 以上。
    for _ in range(2):
        row_body = content[:, left:right].mean(axis=1)
        row_ink = ink[:, left:right].mean(axis=1)
        open_row = (row_body + row_ink > _CARD_BODY_RATIO) & (row_ink <= _CARD_INK_RATIO)
        while top > 0 and top > seed.y - limit and bool(open_row[top - 1]):
            top -= 1
        while bottom < height and bottom < seed.y + seed.height + limit and bool(open_row[bottom]):
            bottom += 1
        column_body = content[top:bottom, :].mean(axis=0)
        column_ink = ink[top:bottom, :].mean(axis=0)
        open_column = (column_body + column_ink > _CARD_BODY_RATIO) & (
            column_ink <= _CARD_INK_RATIO
        )
        while left > 0 and left > seed.x - limit and bool(open_column[left - 1]):
            left -= 1
        while right < width and right < seed.x + seed.width + limit and bool(open_column[right]):
            right += 1
    if right - left < 2 or bottom - top < 2:
        return seed
    return Rect(left, top, right - left, bottom - top)


class ImageBlockFilter:
    """以纹理与色调特征筛选疑似图片/卡片区；未可靠分类的区域不送入 OCR。

    这是可验收的启发式分类器，不承诺识别所有图片或遮挡；宁可漏遮，
    也不把聊天气泡文字遮掉。判不准的区域保持“文字丢弃 + 面板进未覆盖”。
    """

    def __init__(self, tile: int = 24) -> None:
        self.tile = tile

    def find(self, image: np.ndarray, *, background: np.ndarray | None = None) -> list[Rect]:
        height, width = image.shape[:2]
        if height < self.tile * 3 or width < self.tile * 3:
            return []
        rows, columns = height // self.tile, width // self.tile
        cropped = image[: rows * self.tile, : columns * self.tile].astype(np.float32)
        tiles = cropped.reshape(rows, self.tile, columns, self.tile, 3).transpose(0, 2, 1, 3, 4)
        luminance = tiles.mean(axis=4)
        texture = luminance.std(axis=(2, 3))
        chroma = (tiles.max(axis=4) - tiles.min(axis=4)).mean(axis=(2, 3))
        # 大尺寸彩色纹理种子：0.2.0 起沿用的主路径。
        color_seed = (texture > _COLOR_SEED_TEXTURE) & (chroma > _COLOR_SEED_CHROMA)
        photo_seed = self._photo_tiles(cropped, luminance, texture, color_seed)
        seeds = self._merge_seeds(color_seed, photo_seed, self.tile)
        return _drop_contained(self._expand_image_panels(image, seeds, background))

    def _photo_tiles(
        self,
        cropped: np.ndarray,
        luminance: np.ndarray,
        texture: np.ndarray,
        color_seed: np.ndarray,
    ) -> np.ndarray:
        """小尺寸照片种子：先用整幅向量化统计筛候选，再逐块确认。

        文字图块的灰度以背景为主（主色占比高），整幅算一次中位色占比即可
        淘汰绝大多数区块；只有剩下的少数区块才逐块做完整的照片判据。
        """
        rows, columns = texture.shape
        median = np.median(luminance, axis=(2, 3))
        dominant = (np.abs(luminance - median[:, :, None, None]) <= 8.0).mean(axis=(2, 3))
        candidate = (
            ~color_seed
            & (texture >= _PHOTO_TILE_MIN_TEXTURE)
            & (dominant <= _PHOTO_TILE_MAX_DOMINANT)
        )
        photo = np.zeros((rows, columns), dtype=bool)
        for row, column in zip(*np.nonzero(candidate), strict=True):
            top, left = int(row) * self.tile, int(column) * self.tile
            patch = cropped[top : top + self.tile, left : left + self.tile]
            photo[row, column] = looks_like_photo(patch)
        return photo

    @staticmethod
    def _merge_seeds(color_seed: np.ndarray, photo_seed: np.ndarray, tile: int = 24) -> list[Rect]:
        """把相邻种子格聚成矩形；照片种子允许更小的面积门槛。"""
        rows, columns = color_seed.shape
        visited = np.zeros((rows, columns), dtype=bool)
        seeds: list[Rect] = []
        for start_row in range(rows):
            for start_column in range(columns):
                if visited[start_row, start_column]:
                    continue
                if not (color_seed[start_row, start_column] or photo_seed[start_row, start_column]):
                    continue
                stack = [(start_row, start_column)]
                visited[start_row, start_column] = True
                component: list[tuple[int, int]] = []
                photo_only = True
                while stack:
                    y, x = stack.pop()
                    component.append((y, x))
                    if color_seed[y, x]:
                        photo_only = False
                    for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                        if not (0 <= ny < rows and 0 <= nx < columns):
                            continue
                        if visited[ny, nx]:
                            continue
                        if not (color_seed[ny, nx] or photo_seed[ny, nx]):
                            continue
                        visited[ny, nx] = True
                        stack.append((ny, nx))
                required = _PHOTO_TILE_MIN_TILES if photo_only else _COLOR_SEED_MIN_TILES
                if len(component) < required:
                    continue
                ys, xs = zip(*component, strict=True)
                x, y = min(xs) * tile, min(ys) * tile
                right, bottom = (max(xs) + 1) * tile, (max(ys) + 1) * tile
                min_side = _PHOTO_TILE_MIN_SIZE if photo_only else _COLOR_SEED_MIN_SIDE
                if right - x >= min_side and bottom - y >= min_side:
                    seeds.append(Rect(x, y, right - x, bottom - y))
        return seeds

    @staticmethod
    def _expand_image_panels(
        image: np.ndarray, seeds: list[Rect], background: np.ndarray | None = None
    ) -> list[Rect]:
        """种子只用于定位；连同图片的白底、卡片外框和内部文字一起排除。

        单独遮住种子（例如卡片里的缩略图）会留下同一张卡片上的标题、
        价格和底栏文字，造成商品词误报。背景差分寻找完整面板，
        仅扩展与种子相交的候选矩形；面板可能没有边框，再用
        ``_light_card_region`` 从种子向外扩到浅色卡片边界。
        """
        if not seeds:
            return []
        import cv2

        # 消息气泡可能占过半画面，中位数会落到错误的气泡灰色上。
        # 从外缘估计页面背景，避免把整幅聊天区域误判为图片面板。
        # 同一帧的背景已由调用方算好时直接复用，不重复扫描环带。
        background = _frame_background(image, background)
        pixels = _as_color_patch(image).astype(np.float32)
        delta = np.abs(pixels - background).max(axis=2)
        foreground = (delta > _FOREGROUND_DELTA).astype(np.uint8) * 255
        foreground = cv2.morphologyEx(
            foreground, cv2.MORPH_CLOSE, np.ones((3, 3), dtype=np.uint8)
        )
        contours, _ = cv2.findContours(foreground, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        panels = [Rect(*cv2.boundingRect(contour)) for contour in contours]
        height, width = image.shape[:2]
        panels = [
            panel for panel in panels
            if 40 <= panel.width < width - 2 and 40 <= panel.height < height - 2
        ]
        frame_area = height * width
        card_content = _card_content_mask(delta)
        card_ink = _card_ink_mask(delta)
        result: list[Rect] = []
        for seed in seeds:
            candidates = [
                panel for panel in panels
                if (
                    max(0, min(seed.x + seed.width, panel.x + panel.width) - max(seed.x, panel.x))
                    * max(0, min(seed.y + seed.height, panel.y + panel.height) - max(seed.y, panel.y))
                    >= seed.width * seed.height * 0.75
                )
                and panel.width * panel.height >= seed.width * seed.height
            ]
            panel = min(candidates, key=lambda item: item.width * item.height) if candidates else seed
            card = _light_card_region(card_content, card_ink, seed)
            if card.width * card.height > frame_area * _CARD_MAX_AREA_RATIO:
                card = seed
            if card.width * card.height > panel.width * panel.height:
                panel = _union_rect([panel, card])
            if panel not in result:
                result.append(panel)
        return result

    @staticmethod
    def mask(image: np.ndarray, boxes: list[Rect]) -> np.ndarray:
        """把给定图块涂成整帧背景色，**始终返回拷贝**。

        0.2.2 T2.4：``boxes`` 为空时直接返回拷贝。原来的顺序是先算
        ``np.median(result.reshape(-1, 3), axis=0)`` 再进循环——一张 1200×800 的
        ROI 就是 96 万行 × 3 通道的中位数排序，而 ``boxes`` 为空时这次扫描的产物
        **一次都不会被用到**（循环体不执行）。跳过它不改变任何输出：此时函数
        唯一的效果就是返回拷贝。

        **仍然返回拷贝**（用户明确要求保留输入复制）：下游可能原地写 ``result``，
        返回原数组会让它改到调用方的帧。``boxes`` 非空时中位数照旧整帧计算。
        """
        result = image.copy()
        if not boxes:
            return result
        background = np.median(result.reshape(-1, 3), axis=0).astype(np.uint8)
        for box in boxes:
            result[box.y : box.y + box.height, box.x : box.x + box.width] = background
        return result


class LocalOcr:
    """显式本地 RapidOCR 适配。

    ``observer`` 是**可选的**分段观测回调（``Callable[[OcrStageSample], None]``，
    通常就是 :class:`deal_watch.metrics.OcrStageRecorder`）。应用与探针共用这一套
    观测：先 ``page_background`` / 图片块过滤 / 遮罩，再真正构造引擎，再调引擎，
    最后组装消息，四段各自计时。观测回调抛异常只丢一次样本，不允许影响识别。

    **为什么观测点放在这里而不是包一层**：包一层只能量到"另一次 recognize 的墙钟"，
    与 ``inference_ms`` 完全重叠；这里才是预处理、初始化、纯引擎、后处理的分界。
    """

    def __init__(
        self,
        config: ModelConfig,
        base_dir: Path | None = None,
        observer: Any | None = None,
    ) -> None:
        self.config = config
        self.base_dir = base_dir or Path.cwd()
        self._engine: Any = None
        self.filter = ImageBlockFilter()
        self._observer = observer
        self._calls = 0
        #: 最近一次分段的原始样本；供服务层判断"这一次是不是冷启动"。
        self.last_stage_sample: OcrStageSample | None = None

    @property
    def last_cold_start(self) -> bool:
        sample = self.last_stage_sample
        return bool(sample.cold_start) if sample is not None else False

    def _resolve(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.base_dir / path

    def verify_models(self) -> None:
        for name, expected in (
            ("det_path", self.config.det_sha256),
            ("rec_path", self.config.rec_sha256),
            ("keys_path", self.config.keys_sha256),
        ):
            path = self._resolve(getattr(self.config, name))
            if not path.is_file():
                raise FileNotFoundError(f"缺少显式配置的 OCR 模型：{path}")
            if not expected:
                raise ValueError(f"模型清单缺少 SHA-256：{path.name}")
            actual = sha256_file(path)
            if actual.lower() != expected.lower():
                raise ValueError(f"模型 SHA-256 不匹配：{path.name}")

    def _load(self) -> Any:
        if self._engine is None:
            self.verify_models()
            from rapidocr import RapidOCR

            self._engine = RapidOCR(
                params={
                    "Global.use_cls": False,
                    "Global.model_root_dir": str(self.base_dir / "models"),
                    "Det.model_path": str(self._resolve(self.config.det_path)),
                    # 桌面文字不先把窄聊天区域放大到最短边736；限制最长边
                    # 可减少四窗轮询的CPU和尾延迟，保持常用字号原始像素。
                    "Det.limit_type": "max",
                    "Det.limit_side_len": 960,
                    "Rec.model_path": str(self._resolve(self.config.rec_path)),
                    "Rec.rec_keys_path": str(self._resolve(self.config.keys_path)),
                    "EngineConfig.onnxruntime.use_cuda": False,
                    "EngineConfig.onnxruntime.intra_op_num_threads": self.config.intra_op_threads,
                    "EngineConfig.onnxruntime.inter_op_num_threads": self.config.inter_op_threads,
                }
            )
        return self._engine

    def recognize(self, frame: FrameSample) -> OcrObservation:
        """识别一帧并（可选）上报分段耗时。

        ``inference_ms`` **保持旧含义**：整个 recognize 的墙钟（预处理 + 初始化 +
        引擎 + 消息组装）。纯引擎耗时单独走观测回调，不塞进这个字段，
        否则旧字段会突然变成另一个意思，历史报告无法对照。
        """
        started = time.perf_counter()
        # 只有该引擎实例的**第一次**调用可能包含真正的初始化；之后即使换了区域
        # 也不是冷启动——冷启动归属由这里判断，不由"前 N 个样本"推断。
        cold_start = self._calls == 0
        self._calls += 1

        # 每帧只估计一次页面背景，图片过滤器与气泡分隔共用同一结果。
        preprocess_started = started
        mode = getattr(frame, "content_mode", "wechat_chat")
        if mode not in {"wechat_chat", "generic_line", "text_block"}:
            raise ValueError("未知内容模式")
        background = page_background(frame.image) if mode == "wechat_chat" else None
        image_boxes = self.filter.find(frame.image, background=background) if mode == "wechat_chat" else []
        prepared = self.filter.mask(frame.image, image_boxes) if mode == "wechat_chat" else frame.image
        preprocess_ms = (time.perf_counter() - preprocess_started) * 1000.0

        # 初始化单独计时：模型校验 + RapidOCR 构造 + ORT 会话初始化都在这里。
        # 复用已加载引擎时该值恒为 0，报告据此区分"冷启动"与"热识别"。
        model_init_ms = 0.0
        engine = self._engine
        if engine is None:
            init_started = time.perf_counter()
            engine = self._load()
            # ``_load`` 已把引擎记进 self._engine；这里再写一次，保证可注入的
            # 构造实现（测试替身）也走同一路径，不会每帧都重新初始化。
            self._engine = engine
            model_init_ms = (time.perf_counter() - init_started) * 1000.0

        engine_started = time.perf_counter()
        output = engine(prepared)
        engine_ms = (time.perf_counter() - engine_started) * 1000.0

        postprocess_started = time.perf_counter()
        boxes, texts, scores = self._unpack(output)
        blocks: list[TextBlock] = []
        for box, text, score in zip(boxes, texts, scores, strict=True):
            text = str(text).strip() if mode == "wechat_chat" else str(text)
            if not text.strip():
                continue
            points = np.asarray(box, dtype=float).reshape(-1, 2)
            left, top = points.min(axis=0)
            right, bottom = points.max(axis=0)
            rect = Rect(
                max(0, round(float(left))),
                max(0, round(float(top))),
                max(1, round(float(right - left))),
                max(1, round(float(bottom - top))),
            )
            blocks.append(TextBlock(text=text, confidence=float(score), box=rect))
        # 图片/卡片排除、气泡分隔与未覆盖矩形由纯函数统一处理，
        # 预览、手动发送、回放和自动监控共用同一套判定。
        if mode == "wechat_chat":
            blocks, extra_uncovered = assign_messages(
                frame.image, blocks, image_boxes, background=background
            )
            uncovered = _drop_avatar_boxes(
                frame.image, _merge_boxes([*image_boxes, *extra_uncovered]), blocks
            )
        else:
            from .content import generic_content
            blocks, uncovered = generic_content(blocks, mode)
        postprocess_ms = (time.perf_counter() - postprocess_started) * 1000.0
        total_ms = (time.perf_counter() - started) * 1000.0
        observation = OcrObservation(
            region_id=frame.region_id,
            sampled_at=frame.captured_at,
            frame_sequence=frame.sequence,
            blocks=blocks,
            uncovered_boxes=uncovered,
            inference_ms=total_ms,
            content_mode=mode,
            strict_text=mode != "wechat_chat",
        )
        self._report_stages(
            OcrStageSample(
                region_id=frame.region_id,
                frame_sequence=frame.sequence,
                cold_start=cold_start,
                preprocess_ms=preprocess_ms,
                model_init_ms=model_init_ms,
                engine_ms=engine_ms,
                postprocess_ms=postprocess_ms,
                total_ms=total_ms,
            )
        )
        return observation

    def _report_stages(self, sample: OcrStageSample) -> None:
        """上报分段样本；观测失败只丢样本，绝不把识别结果变成异常。"""
        self.last_stage_sample = sample
        observer = self._observer
        if observer is None:
            return
        try:
            observer(sample)
        except Exception:  # noqa: BLE001 - 埋点是观察者，不能反向影响识别
            pass

    @staticmethod
    def _unpack(output: Any) -> tuple[Any, Any, Any]:
        if all(hasattr(output, name) for name in ("boxes", "txts", "scores")):
            boxes = [] if output.boxes is None else output.boxes
            texts = [] if output.txts is None else output.txts
            scores = [] if output.scores is None else output.scores
            return boxes, texts, scores
        result = output[0] if isinstance(output, tuple) else output
        if not result:
            return [], [], []
        boxes = [item[0] for item in result]
        texts = [item[1] for item in result]
        scores = [item[2] for item in result]
        return boxes, texts, scores
