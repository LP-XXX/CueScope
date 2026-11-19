"""Public synthetic contracts for CueScope rules, scope and legacy compatibility."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from deal_watch.models import MessageCandidate, OcrObservation, Rect, TextBlock
from deal_watch.normalize import content_digest
from deal_watch.rule_config import MAX_RULE_INPUT_LENGTH, RuleCondition, RuleGroup
from deal_watch.rule_evaluation import evaluate_groups
from deal_watch.rules import RuleEngine
from deal_watch.storage import Storage
from deal_watch.tracking import FrameTracker


def condition(text, **kwargs):
    return RuleCondition(text=text, **kwargs)


def group(*texts, id="errors", **kwargs):
    return RuleGroup(name="Synthetic rules", id=id, conditions=[
        item if isinstance(item, RuleCondition) else condition(item) for item in texts
    ], **kwargs)


def region(id="a", group_id="errors", scope="task", dedupe_group=""):
    return SimpleNamespace(
        id=id, rule_group_id=group_id, dedupe_scope=scope, dedupe_group=dedupe_group,
    )


def message(text, region_id="a", **kwargs):
    return MessageCandidate(
        region_id=region_id, text=text, box=Rect(0, 0, 180, 32),
        first_seen=1, last_seen=1, **kwargs,
    )


@pytest.fixture
def storage(tmp_path):
    value = Storage(tmp_path / "rules.sqlite")
    yield value
    value.close()


def test_rule_group_roundtrip_is_data_only_and_keeps_ids():
    original = group(condition("ERROR", mode="exact", case_sensitive=True),
                     exclusions=[condition("test")], combination="all")
    parsed = RuleGroup.from_dict(json.loads(json.dumps(original.to_dict())))
    assert parsed == original
    assert parsed.conditions[0] is not original.conditions[0]


@pytest.mark.parametrize("kwargs", [
    {"text": ""}, {"text": "   "}, {"text": "x" * 4097},
    {"text": "x", "mode": "python"}, {"text": "x", "mode": "regex"},
    {"text": "x", "case_sensitive": "false"},
    {"text": "x", "whitespace": "guess"}, {"text": "x", "enabled": 1},
    {"text": "!?", "mode": "char_multiset"},
])
def test_invalid_conditions_fail_during_configuration(kwargs):
    with pytest.raises(ValueError):
        RuleCondition(**kwargs)


def test_unknown_serialized_fields_are_rejected():
    with pytest.raises(ValueError):
        RuleCondition.from_dict({"text": "error", "expression": "arbitrary code"})
    with pytest.raises(ValueError):
        RuleGroup.from_dict({"name": "test", "conditions": [], "script": "x"})


def test_empty_enabled_or_duplicate_condition_groups_are_rejected():
    with pytest.raises(ValueError):
        group()
    item = condition("error")
    with pytest.raises(ValueError):
        group(item, exclusions=[item])
    assert not evaluate_groups("error", [group(enabled=False)]).should_notify


@pytest.mark.parametrize(("mode", "text", "expected"), [
    ("contains", "Service FAILED again", True),
    ("contains", "F A I L E D", False),
    ("exact", "FAILED", True),
    ("exact", "service failed", False),
    ("char_multiset", "d-e-l-i-a-f", True),
])
def test_modes_have_distinct_semantics(mode, text, expected):
    assert evaluate_groups(text, [group(condition("failed", mode=mode))]).should_notify is expected


def test_character_counts_and_case_option_are_honored():
    value = group(condition("AAB", mode="char_multiset", case_sensitive=True))
    assert evaluate_groups("A B A", [value]).should_notify
    assert not evaluate_groups("AB", [value]).should_notify
    assert not evaluate_groups("aab", [value]).should_notify


@pytest.mark.parametrize(("policy", "actual", "expected"), [
    ("collapse", "  build\n  done  ", True),
    ("collapse", "builddone", False),
    ("preserve", "build\ndone", False),
    ("preserve", "build done", True),
    ("remove", "build\n done", True),
    ("remove", "builddone", True),
])
def test_whitespace_policies_are_explicit(policy, actual, expected):
    value = group(condition("build done", mode="exact", whitespace=policy))
    assert evaluate_groups(actual, [value]).should_notify is expected


def test_nfkc_does_not_delete_numeric_model_or_url_punctuation():
    value = group(condition("AB-12 1.99 https://example.invalid/A", mode="exact", case_sensitive=True))
    assert evaluate_groups("ＡＢ-１２ １.９９ https://example.invalid/A", [value]).should_notify
    for text in ("AB12 1.99 https://example.invalid/A", "AB-12 199 https://example.invalid/A",
                 "AB-12 1.99 https://example.invalid/a"):
        assert not evaluate_groups(text, [value]).should_notify


def test_any_all_and_group_or_never_join_different_inputs():
    any_group = group("failed", "retry", id="any")
    all_group = group("failed", "retry", id="all", combination="all")
    assert evaluate_groups("failed", [any_group]).should_notify
    assert not evaluate_groups("failed", [all_group]).should_notify
    assert not evaluate_groups("retry", [all_group]).should_notify
    assert evaluate_groups("failed; retry", [all_group]).should_notify
    assert evaluate_groups("retry", [all_group, any_group]).matched_group_ids == ["any"]


def test_exclusions_veto_all_selected_groups_and_return_explanation():
    first = group("failed", id="first", exclusions=[condition("test", label="Exclude tests")])
    second = group("failed", id="second")
    result = evaluate_groups("test failed", [first, second])
    assert result.reason == "excluded_by_rule"
    assert not result.should_notify
    assert result.excluded_by == ["Exclude tests"]
    assert result.condition_results[0].is_exclusion
    assert result.condition_results[0].normalized_text == "test failed"
    assert evaluate_groups("failed", [first, second]).should_notify


def test_disabled_exclusion_and_condition_do_not_affect_all():
    value = group("failed", condition("never", enabled=False), combination="all",
                  exclusions=[condition("failed", enabled=False)])
    result = evaluate_groups("failed", [value])
    assert result.should_notify
    assert len(result.condition_results) == 1


def test_oversized_input_is_unknown_instead_of_a_truncated_match():
    result = evaluate_groups("error" + " " * MAX_RULE_INPUT_LENGTH, [group("error")])
    assert not result.should_notify
    assert result.reason == "rule_input_too_large"
    assert result.error


def test_preview_and_runtime_use_the_same_rule_evaluation(storage):
    engine = RuleEngine(storage, ["irrelevant"], regions=[region()], rule_groups=[
        group("failed", "retry", combination="all", exclusions=[condition("test")]),
    ])
    for text in ("failed", "failed retry", "test failed retry"):
        item = message(text)
        preview = engine.explain(item)
        actual = engine.decide(item)
        assert (actual.should_notify, actual.reason, actual.matched_keywords) == (
            preview.should_notify, preview.reason, preview.matched_labels,
        )


def test_unknown_group_does_not_fall_back_to_legacy_keywords(storage):
    engine = RuleEngine(storage, ["failed"], regions=[region()], rule_groups=[])
    result = engine.explain(message("failed"))
    assert result.reason == "rule_configuration_error"
    assert not result.should_notify
    assert engine.process([message("failed")], "synthetic") == []
    assert storage.list_events() == []


def test_missing_dedupe_group_blocks_send_instead_of_becoming_global(storage):
    engine = RuleEngine(storage, [], regions=[region(scope="group")], rule_groups=[group("failed")])
    assert engine.decide(message("failed")).reason == "rule_configuration_error"
    assert engine.process([message("failed")], "synthetic") == []


def test_configuration_is_snapshotted_and_reports_only_changed_tasks(storage):
    value = group("failed")
    regions = [region()]
    engine = RuleEngine(storage, [], regions=regions, rule_groups=[value])
    assert engine.configure_regions(regions, [value]) == set()
    value.conditions[0].text = "success"
    assert engine.decide(message("failed")).should_notify
    assert engine.configure_regions(regions, [value]) == {"a"}
    assert not engine.decide(message("failed")).should_notify
    assert engine.decide(message("success")).should_notify


def test_multiple_matching_conditions_create_one_event(storage):
    engine = RuleEngine(storage, [], regions=[region()], rule_groups=[group("failed", "retry")])
    events = engine.process([message("failed retry")], "synthetic", now=10)
    assert len(events) == 1
    assert events[0].matched_keywords == ["failed", "retry"]
    assert storage.queue_counts()["pending"] == 1


@pytest.mark.parametrize(("scope", "same_group", "expected"), [
    ("global", True, 1), ("task", True, 2), ("group", True, 1), ("group", False, 2),
])
def test_dedupe_scope_controls_cross_task_suppression(storage, scope, same_group, expected):
    regions = [region("a", scope=scope, dedupe_group="one"),
               region("b", scope=scope, dedupe_group="one" if same_group else "two")]
    engine = RuleEngine(storage, [], regions=regions, rule_groups=[group("failed")])
    events = engine.process([message("failed", "a"), message("failed", "b")], "synthetic", now=10)
    assert len(events) == expected


def test_rule_ids_do_not_split_one_shared_content_event(storage):
    engine = RuleEngine(storage, [], regions=[
        region("a", "first", "group", "shared"), region("b", "second", "group", "shared"),
    ], rule_groups=[group("failed", id="first"), group("retry", id="second")])
    assert len(engine.process([message("failed retry", "a"), message("failed retry", "b")],
                              "synthetic", now=10)) == 1


def test_case_sensitive_exact_updates_and_dedupe_preserve_case(storage):
    engine = RuleEngine(storage, [], regions=[region()], rule_groups=[
        group(condition("DONE", mode="exact", case_sensitive=True)),
    ])
    events = engine.process([message("DONE", previous_text="done", is_new=False, is_update=True)],
                            "synthetic", now=10)
    assert len(events) == 1
    assert not engine.decide(message("done")).should_notify


def test_new_exact_rules_do_not_suppress_meaningful_punctuation_update(storage):
    engine = RuleEngine(storage, [], regions=[region()], rule_groups=[group(condition("done!", mode="exact"))])
    assert len(engine.process([
        message("done!", previous_text="done", is_new=False, is_update=True),
    ], "synthetic", now=10)) == 1


def test_preserved_whitespace_keeps_distinct_event_keys(storage):
    engine = RuleEngine(storage, [], regions=[region()], rule_groups=[
        group(condition("done", whitespace="preserve")),
    ])
    assert len(engine.process([message("done\none"), message("done one")], "synthetic", now=10)) == 2


def test_legacy_global_key_and_update_jitter_remain_unchanged(storage):
    engine = RuleEngine(storage, ["手机"], regions=[region(group_id="", scope="global")])
    events = engine.process([message("手机 1.99")], "synthetic", now=10)
    assert events[0].dedupe_key == content_digest("手机 1.99")
    assert engine.process([message("手机 1.99。", previous_text="手机 1.99", is_update=True)],
                          "synthetic", now=11) == []


def test_zero_timestamp_is_not_replaced_by_wall_clock(storage):
    engine = RuleEngine(storage, ["failed"])
    assert len(engine.process([message("failed")], "synthetic", now=0)) == 1
    assert engine.process([message("failed")], "synthetic", now=1) == []


def test_nontext_and_uncertain_boundaries_never_notify(storage):
    engine = RuleEngine(storage, [], regions=[region()], rule_groups=[group("failed")])
    for kwargs in ({"kind": "image_card"}, {"boundary_uncertain": True}):
        item = message("failed", **kwargs)
        assert not engine.explain(item).should_notify
        assert engine.process([item], "synthetic") == []


def test_local_event_does_not_require_or_enqueue_remote_delivery(storage):
    engine = RuleEngine(storage, [], regions=[region()], rule_groups=[group("failed")], delivery_mode="local")
    events = engine.process([message("failed")], "synthetic", now=10)
    assert len(events) == 1
    assert events[0].delivery_mode == "local"
    assert engine.last_decisions[events[0].tracking_id].queue_state == "local"
    assert storage.queue_counts().get("pending", 0) == 0
    assert len(storage.list_events()) == 1


def tracked_observation(text, at, *, mode="wechat_chat", strict=False):
    return OcrObservation(
        region_id="a", sampled_at=at, frame_sequence=at,
        blocks=[TextBlock(text=text, confidence=0.99, box=Rect(0, 0, 180, 32))],
        content_mode=mode, strict_text=strict,
    )


@pytest.mark.parametrize(("before", "after"), [
    ("done", "DONE"), ("done once", "done  once"), ("done", "done!"),
])
def test_new_content_modes_track_precise_updates_without_losing_identity(before, after):
    tracker = FrameTracker()
    assert tracker.process(tracked_observation(before, 1, mode="generic_line")) == []
    changed = tracker.process(tracked_observation(after, 2, mode="generic_line"))
    assert len(changed) == 1
    assert changed[0].is_update
    assert changed[0].previous_text == before


def test_explicit_strict_chat_tracking_reaches_case_sensitive_rules(storage):
    tracker = FrameTracker()
    engine = RuleEngine(storage, [], regions=[region()], rule_groups=[
        group(condition("DONE", mode="exact", case_sensitive=True)),
    ])
    assert tracker.process(tracked_observation("done", 1, strict=True)) == []
    changes = tracker.process(tracked_observation("DONE", 2, strict=True))
    assert len(engine.process(changes, "synthetic", now=10)) == 1
    assert tracker.process(tracked_observation("DONE", 3, strict=True)) == []


def test_legacy_chat_tracking_still_ignores_case_and_collapsed_space():
    tracker = FrameTracker()
    assert tracker.process(tracked_observation("done once", 1)) == []
    assert tracker.process(tracked_observation("DONE  ONCE", 2)) == []


def test_switching_content_or_comparison_semantics_requires_new_baseline():
    tracker = FrameTracker()
    tracker.process(tracked_observation("done", 1))
    assert tracker.process(tracked_observation("DONE", 2, strict=True)) == []
    assert tracker.process(tracked_observation("DONE again", 3, mode="generic_line")) == []
    assert len(tracker.process(tracked_observation("DONE again!", 4, mode="generic_line"))) == 1
