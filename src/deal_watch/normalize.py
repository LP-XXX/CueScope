"""匹配、稳定更新和跨群去重分别使用不同强度的归一化。"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

_WHITESPACE = re.compile(r"\s+")
_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def _transform_outside_urls(text: str, transform) -> str:
    """URL 路径、查询和值区分大小写；不能随普通文字一起 casefold。"""
    pieces: list[str] = []
    start = 0
    for match in _URL.finditer(text):
        pieces.append(transform(text[start:match.start()]))
        pieces.append(match.group())
        start = match.end()
    pieces.append(transform(text[start:]))
    return "".join(pieces)


def match_normalize(text: str) -> str:
    """字面匹配：统一兼容字符、大小写，并忽略气泡内部空白。"""
    return _WHITESPACE.sub("", unicodedata.normalize("NFKC", text)).casefold()


def char_rule_normalize(text: str) -> str:
    """逐字符规则：保留文字与数字，忽略空白和装饰符号。

    NFKC 统一兼容字符后，仅保留 Unicode 字母、数字和音调标记。
    这样“坦博尔羽绒服”可命中同一消息中的“坦博尔饼干羽绒服”，
    但字符计数仍按出现次数判断，不会跨消息拼字。
    """
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        character
        for character in normalized
        if character.isspace() is False
        and (unicodedata.category(character)[0] in {"L", "N", "M"})
    )


def contains_all_chars(message: str, keyword: str) -> bool:
    """同一条消息中按出现次数包含关键词全部有效字符。"""
    message_counts = Counter(char_rule_normalize(message))
    for character, required in Counter(char_rule_normalize(keyword)).items():
        if message_counts[character] < required:
            return False
    return True


def dedupe_normalize(text: str) -> str:
    """保守去重：保留数字、价格、型号、链接和标点差异。"""
    normalized = _transform_outside_urls(
        unicodedata.normalize("NFKC", text), str.casefold
    )
    return _WHITESPACE.sub(" ", normalized).strip()


def meaningful_normalize(text: str) -> str:
    """仅用于抑制同一已命中消息的 OCR 标点/换行抖动，不用于跨群合并。"""
    # 只忽略普通文字尾部的标点抖动。保留小数点、型号连接符、URL
    # 及其大小写；1.99/199、AB-12/AB12、/A1/ /a1/ 必须产生更新。
    return _transform_outside_urls(
        unicodedata.normalize("NFKC", text),
        lambda part: _WHITESPACE.sub("", part).casefold().rstrip("。，、！？；：.,!?:;"),
    )


def content_digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(dedupe_normalize(text).encode("utf-8")).hexdigest()
