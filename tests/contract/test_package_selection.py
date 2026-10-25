"""T034：条件优先选择契约（硬条件、预算、歧义与示例上限）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from sana_bot.enums import PrimaryMood, RelationshipStatus, Scene
from sana_bot.retrieval.package_selector import (
    DECISION_AMBIGUOUS_REFERENCE,
    DECISION_READY_WITHOUT_REFERENCES,
    DECISION_READY_WITH_REFERENCES,
    DECISION_RELATIONSHIP_MISMATCH,
    PackageSelector,
)
from tests.support.package_harness import PackageHarness, build_package_harness, snapshot_for

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "character_package"


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> PackageHarness:
    built = build_package_harness(tmp_path_factory.mktemp("selection"))
    try:
        yield built
    finally:
        built.close()


def _snapshot(state: str = "", tags=(), scene=Scene.UNKNOWN, mood=PrimaryMood.NEUTRAL):
    return snapshot_for(
        mood=mood,
        relationship=RelationshipStatus.ROMANTIC,
        scene=scene,
        dominant_state=state,
        tags=tags,
    )


def test_unknown_scene_does_not_satisfy_private_condition(harness):
    selection = harness.select(
        query="被你这么陪着，我安心多了",
        snapshot=_snapshot("依赖软萌", ("依赖软萌",), Scene.UNKNOWN),
    )
    selected = {card.card_id for card in selection.supplements} | {
        card.card_id for card in selection.rule_cards
    }
    assert "sana_emotion_secure" not in selected
    evidence = {item.card_id: item for item in selection.evidence}
    assert evidence["sana_emotion_secure"].condition_ok is False
    assert evidence["sana_emotion_secure"].reason == "condition_rejected"


def test_private_scene_allows_secure_card(harness):
    selection = harness.select(
        query="被你这么陪着，我安心多了",
        snapshot=_snapshot("依赖软萌", ("依赖软萌",), Scene.PRIVATE),
    )
    assert "sana_emotion_secure" in {card.card_id for card in selection.rule_cards}


def test_public_scene_excludes_private_only_references(harness):
    selection = harness.select(
        query="在教室里你突然想抱我一下",
        snapshot=_snapshot(scene=Scene.PUBLIC),
        medium="co_present",
    )
    selected = {card.card_id for card in selection.supplements}
    assert not selected & {
        "sana_example_shy",
        "sana_example_jealous",
        "sana_example_hurt",
        "sana_example_gift",
    }


def test_medium_condition_is_enforced(harness):
    selection = harness.select(
        query="今天放学一起走吗，社团收拾完就走",
        snapshot=_snapshot(scene=Scene.PUBLIC),
        medium="text",
    )
    assert "sana_example_calm" not in {card.card_id for card in selection.supplements}


def test_state_card_is_selected_independent_of_vector_score(harness):
    # 查询与状态卡无关，但有效状态卡仍必须载入
    selection = harness.select(
        query="今天天气不错",
        snapshot=_snapshot("害羞局促", ("害羞局促",), Scene.PRIVATE),
    )
    ids = {card.card_id for card in selection.rule_cards}
    assert "sana_emotion_shy" in ids
    evidence = {item.card_id: item for item in selection.evidence}
    assert evidence["sana_emotion_shy"].state_match is True


def test_examples_are_capped_at_two(harness):
    selection = harness.select(
        query="社团的小猫、点心、小饰品、礼物都想跟你说",
        snapshot=_snapshot(scene=Scene.PRIVATE),
        threshold=0.10,
        medium="co_present",
    )
    assert len(selection.example_cards) <= 2


def test_supplements_are_capped_at_eight(harness):
    selection = harness.select(
        query="社团的小猫、点心、小饰品、礼物、料理、夸奖、累、弟弟都想说",
        snapshot=_snapshot(scene=Scene.PRIVATE),
        threshold=0.10,
        medium="co_present",
    )
    assert len(selection.supplements) <= 8


def test_relationship_mismatch_returns_controlled_failure(harness):
    selection = harness.select(
        query="今天放学一起走吗",
        snapshot=snapshot_for(mood=PrimaryMood.NEUTRAL, relationship=RelationshipStatus.NON_ROMANTIC),
    )
    assert selection.decision == DECISION_RELATIONSHIP_MISMATCH
    assert selection.reason_code == "package_relationship_mismatch"
    assert selection.generatable is False


def test_no_match_is_generatable_without_references(harness):
    selection = harness.select(
        query="嗯，今天天气不错", snapshot=_snapshot(scene=Scene.UNKNOWN)
    )
    assert selection.decision in (
        DECISION_READY_WITHOUT_REFERENCES,
        DECISION_READY_WITH_REFERENCES,
    )
    assert selection.generatable is True


def test_ambiguous_state_condition_is_recorded(harness):
    selection = harness.select(
        query="刚才那句话说得很冲，我听着有点难受",
        snapshot=_snapshot("委屈脆弱", ("委屈脆弱",), Scene.UNKNOWN),
    )
    assert selection.decision == DECISION_AMBIGUOUS_REFERENCE
    assert selection.ambiguity is not None
    assert "sana_emotion_hurt" in selection.ambiguity["card_ids"]
    assert selection.generatable is True


def test_budget_exclusion_is_recorded(harness):
    index = harness.index()
    tight = dict(index.policy.get("budget") or {})
    tight["max_prompt_tokens"] = 3150
    selector = PackageSelector(
        threshold=0.10, budgets=tight, count_tokens=harness.backend.count_tokens
    )
    selection = selector.select(
        index=index,
        snapshot=_snapshot(scene=Scene.PRIVATE),
        query_vector=harness.backend.embed_query("社团的小猫、点心、小饰品、礼物都说给你听"),
        medium="co_present",
        required_relationship="romantic",
        query_tokens=20,
    )
    excluded = [item for item in selection.evidence if item.budget_excluded]
    assert excluded, "预算不足时必须记录整卡排除而不是裁剪正文"


def test_over_limit_context_fails_controlled(harness):
    index = harness.index()
    tight = dict(index.policy.get("budget") or {})
    tight["max_prompt_tokens"] = 1200
    selector = PackageSelector(threshold=0.10, budgets=tight, count_tokens=harness.backend.count_tokens)
    selection = selector.select(
        index=index,
        snapshot=_snapshot(scene=Scene.PRIVATE),
        query_vector=harness.backend.embed_query("社团的小猫"),
        medium="text",
        required_relationship="romantic",
        query_tokens=10,
    )
    assert selection.decision == "context_over_limit"
    assert selection.generatable is False


def test_evidence_covers_every_candidate(harness):
    selection = harness.select(
        query="社团的小猫今天很粘人", snapshot=_snapshot(scene=Scene.PRIVATE)
    )
    ids = {item.card_id for item in selection.evidence}
    assert "sana_example_share" in ids
    assert "sana_emotion_shy" in ids
    payload = selection.to_payload()
    assert payload["decision"] == selection.decision
    assert isinstance(payload["evidence"], list)


def test_selection_never_uses_inadmissible_card(harness):
    for scene in (Scene.PUBLIC, Scene.UNKNOWN):
        selection = harness.select(
            query="被你这么陪着，我安心多了，想靠着你",
            snapshot=_snapshot("依赖软萌", ("依赖软萌",), scene),
            medium="co_present",
        )
        selected = {
            card.card_id for card in (*selection.rule_cards, *selection.supplements)
        }
        assert "sana_emotion_secure" not in selected
        assert "sana_emotion_hurt" not in selected
