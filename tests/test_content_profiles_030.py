"""Synthetic content-profile contracts; no OCR model load or desktop access."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from deal_watch.config import ModelConfig
from deal_watch.content import generic_content
from deal_watch.models import FrameSample, OcrObservation, Rect, TextBlock
from deal_watch.ocr import LocalOcr
from deal_watch.ocr_cache import OcrCacheContext, ReusableOcr
from deal_watch.rule_config import RuleCondition, RuleGroup
from deal_watch.rule_evaluation import evaluate_groups
from deal_watch.tracking import blocks_to_messages


def block(text, x=10, y=10, *, confidence=0.99, kind="text", height=24):
    return TextBlock(text, confidence, Rect(x, y, 160, height), kind=kind)


def candidates(blocks, mode="generic_line"):
    processed, uncovered = generic_content(blocks, mode)
    observation = OcrObservation("synthetic", 1, 1, processed, content_mode=mode)
    return blocks_to_messages(observation), uncovered


def test_generic_lines_do_not_merge_columns_or_neighboring_records():
    units, uncovered = candidates([
        block("连接", x=10, y=10), block("失败", x=240, y=10),
        block("另一条记录", x=10, y=50),
    ])
    assert [unit.text for unit in units] == ["连接", "失败", "另一条记录"]
    rule = RuleGroup("组合", [RuleCondition("连接失败", mode="char_multiset")])
    assert not any(evaluate_groups(unit.text, [rule]).should_notify for unit in units)
    assert uncovered == []


def test_embedded_newlines_are_independent_units_in_generic_line_mode():
    units, _ = candidates([block("连接\n失败", height=48)])
    assert [unit.text for unit in units] == ["连接", "失败"]
    assert units[0].box.y < units[1].box.y
    assert units[0].box.y + units[0].box.height <= units[1].box.y


def test_text_block_is_explicit_and_preserves_multiline_order():
    blocks = [block("第二行", y=50), block("第一行", y=10)]
    lines, _ = candidates(blocks, "generic_line")
    merged, uncovered = candidates(blocks, "text_block")
    assert len(lines) == 2
    assert len(merged) == 1
    assert merged[0].text == "第一行\n第二行"
    assert uncovered == []


@pytest.mark.parametrize("bad", [
    block("无法确认", y=50, confidence=0.1),
    block("图片文字", y=50, kind="image_card"),
    block("转发摘要", y=50, kind="forwarded_card"),
])
def test_selected_text_block_never_bridges_unreadable_or_excluded_content(bad):
    source = [block("连接", y=10), bad, block("失败", y=90)]
    units, uncovered = candidates(source, "text_block")
    assert units == []
    assert set(uncovered) == {item.box for item in source}
    separate, uncovered_lines = candidates(source, "generic_line")
    assert [item.text for item in separate] == ["连接", "失败"]
    assert uncovered_lines == [bad.box]


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.1, 1.1])
def test_invalid_confidence_is_uncovered_not_trusted(score):
    source = block("ERROR", confidence=score)
    units, uncovered = candidates([source])
    assert units == []
    assert uncovered == [source.box]


def test_generic_processing_does_not_mutate_shared_ocr_blocks():
    source = block("one\ntwo", height=48)
    original = replace(source)
    generic_content([source], "generic_line")
    generic_content([source], "text_block")
    assert source == original
    assert source.message_id == ""


def fake_ocr_output():
    return SimpleNamespace(
        boxes=[[[10, 10], [120, 10], [120, 34], [10, 34]],
               [[10, 54], [120, 54], [120, 78], [10, 78]]],
        txts=["first line", "second line"], scores=[0.99, 0.99],
    )


def frame(mode="wechat_chat", sequence=1):
    return FrameSample("synthetic", sequence, float(sequence), np.full((120, 180, 3), 245, np.uint8),
                       Rect(0, 0, 180, 120), content_mode=mode)


@pytest.mark.parametrize("mode", ["generic_line", "text_block"])
def test_generic_local_ocr_routes_without_chat_masks_or_bubble_assumptions(mode, monkeypatch):
    import deal_watch.ocr as module

    engine = LocalOcr(ModelConfig())
    engine._engine = lambda _image: fake_ocr_output()
    engine.filter.find = Mock(side_effect=AssertionError("chat mask used for generic input"))
    engine.filter.mask = Mock(side_effect=AssertionError("chat mask used for generic input"))
    monkeypatch.setattr(module, "assign_messages", Mock(side_effect=AssertionError("chat assembler used")))
    observation = engine.recognize(frame(mode))
    assert observation.content_mode == mode
    assert observation.strict_text
    units = blocks_to_messages(observation)
    assert len(units) == (2 if mode == "generic_line" else 1)


def test_default_local_ocr_keeps_chat_pipeline(monkeypatch):
    import deal_watch.ocr as module

    engine = LocalOcr(ModelConfig())
    engine._engine = lambda _image: fake_ocr_output()
    engine.filter.find = Mock(return_value=[])
    engine.filter.mask = Mock(side_effect=lambda image, _boxes: image.copy())
    original = module.assign_messages
    assembler = Mock(wraps=original)
    monkeypatch.setattr(module, "assign_messages", assembler)
    observation = engine.recognize(frame())
    assert observation.content_mode == "wechat_chat"
    assert not observation.strict_text
    engine.filter.find.assert_called_once()
    engine.filter.mask.assert_called_once()
    assembler.assert_called_once()


def test_content_mode_switch_invalidates_identical_pixel_cache():
    inner = SimpleNamespace(recognize=Mock(side_effect=lambda incoming: OcrObservation(
        incoming.region_id, incoming.captured_at, incoming.sequence,
        [block(incoming.content_mode)], content_mode=incoming.content_mode,
    )))
    reusable = ReusableOcr(inner, clock=lambda: 10.0)
    chat_context = OcrCacheContext("synthetic", 1, content_mode="wechat_chat")
    generic_context = replace(chat_context, content_mode="generic_line")
    first = reusable.recognize(frame(), chat_context)
    reused = reusable.recognize(frame(sequence=2), chat_context)
    changed = reusable.recognize(frame("generic_line", 3), generic_context)
    again = reusable.recognize(frame("generic_line", 4), generic_context)
    assert not first.reused and reused.reused
    assert not changed.reused and again.reused
    assert changed.blocks[0].text == "generic_line"
    assert again.content_mode == "generic_line"
    assert again.frame_sequence == 4 and again.sampled_at == 4
    assert inner.recognize.call_count == 2


def test_unrecognized_content_mode_is_not_silently_reinterpreted():
    engine = LocalOcr(ModelConfig())
    engine._engine = Mock(side_effect=AssertionError("invalid mode reached OCR"))
    with pytest.raises(ValueError):
        engine.recognize(frame("unknown"))
    with pytest.raises(ValueError):
        generic_content([block("text")], "unknown")
