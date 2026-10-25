"""T024：资料包模式的输出规则（代写、示例记忆、同地假设与格式）。"""

from __future__ import annotations

import pytest

from sana_bot.dialogue.output_policy import (
    ConstrainedOutputPolicy,
    OutputConstraints,
    check_text,
)
from sana_bot.ports import DialogueDraft, ModelBudget
from sana_bot.enums import PrimaryMood, RelationshipStatus
from sana_bot.state.models import empty_snapshot
from tests.support import FakeModel


def _snapshot():
    return empty_snapshot(
        snapshot_id="s",
        turn_id="t",
        conversation_id="c",
        mood=PrimaryMood.NEUTRAL,
        relationship=RelationshipStatus.ROMANTIC,
    )


def codes(text: str) -> set[str]:
    constraints = OutputConstraints(strict_package_rules=True)
    return {item.code for item in check_text(text, constraints)}


def test_ghostwritten_user_action_is_blocked():
    assert "user_action_ghostwritten" in codes("（你伸手摸了摸我的头）别这样啦")


def test_ghostwritten_user_dialogue_is_blocked():
    assert "user_dialogue_ghostwritten" in codes("你说：我也想你")


def test_example_as_shared_memory_is_blocked():
    assert "example_as_memory" in codes("还记得我们第一次一起逛庙会")


def test_assumed_colocation_is_blocked():
    assert "assumed_colocation" in codes("我伸手抱住你，说别怕")


def test_dialogue_only_reply_is_allowed():
    assert codes("嗯，我知道了你今天很累") == set()


def test_trailing_period_and_third_person_still_blocked():
    assert "trailing_full_stop" in codes("我在听。")
    assert "third_person_self" in codes("纱凪才没有在意")


def test_strict_rules_off_keeps_legacy_behaviour():
    legacy = OutputConstraints()
    assert {item.code for item in check_text("（你伸手摸了摸我的头）别这样啦", legacy)} == set()
    strict = OutputConstraints(strict_package_rules=True)
    assert {item.code for item in check_text("（你伸手摸了摸我的头）别这样啦", strict)}


@pytest.mark.asyncio
async def test_strict_policy_uses_extended_semantic_review_prompt():
    model = FakeModel()
    policy = ConstrainedOutputPolicy(
        constraints=OutputConstraints(strict_package_rules=True),
        model=model,
        budget=ModelBudget(30.0),
        allow_rewrite=False,
    )
    decision = await policy.review(
        draft=DialogueDraft(kind="normal", text="我在听着呢"),
        snapshot=_snapshot(),
    )
    assert decision.approved is True
    semantic = [r for r in model.requests if r.json_schema_name == "output_review"]
    assert semantic
    system = str(semantic[0].messages[0]["content"])
    assert "user_action_ghostwritten" in system
    assert "user_dialogue_ghostwritten" in system
    assert "example_as_memory" in system
    assert "assumed_colocation" in system


@pytest.mark.asyncio
async def test_violation_cannot_be_sent_after_rewrite_failure():
    model = FakeModel(text_responses=("（你伸手摸了摸我的头）别这样啦",))
    policy = ConstrainedOutputPolicy(
        constraints=OutputConstraints(strict_package_rules=True),
        model=model,
        budget=ModelBudget(30.0),
        allow_rewrite=True,
    )
    decision = await policy.review(
        draft=DialogueDraft(kind="normal", text="（你伸手摸了摸我的头）别这样啦"),
        snapshot=_snapshot(),
    )
    assert decision.approved is False
    assert "user_action_ghostwritten" in decision.violations


@pytest.mark.asyncio
async def test_rewrite_is_limited_and_respected():
    model = FakeModel(text_responses=("我知道你今天很累了",))
    policy = ConstrainedOutputPolicy(
        constraints=OutputConstraints(strict_package_rules=True),
        model=model,
        budget=ModelBudget(30.0),
        allow_rewrite=True,
    )
    decision = await policy.review(
        draft=DialogueDraft(kind="normal", text="（你伸手摸了摸我的头）别这样啦"),
        snapshot=_snapshot(),
    )
    assert decision.approved is True
    assert decision.rewritten is True
    assert decision.text == "我知道你今天很累了"
    assert len([r for r in model.requests if r.json_schema_name is None]) == 1
