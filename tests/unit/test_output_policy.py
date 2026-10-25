"""输出规范测试：禁句、句末标点、第一人称、代用户言行、长度与句式。

包含正常反例，避免出现「一律拒发」的过度拦截。
"""

from __future__ import annotations

import pytest

from sana_bot.dialogue.output_policy import (
    ConstrainedOutputPolicy,
    OutputConstraints,
    check_text,
)
from sana_bot.enums import PrimaryMood, RetrievalStage
from sana_bot.ports import DialogueDraft, ModelBudget
from sana_bot.state.models import empty_snapshot
from tests.support import FakeModel


def _constraints(**overrides: object) -> OutputConstraints:
    defaults = {
        "forbidden_phrases": ("我讨厌你", "分手"),
        "verbatim_reference_texts": ("好呀，刚好可以和你多待一会",),
    }
    defaults.update(overrides)
    return OutputConstraints(**defaults)  # type: ignore[arg-type]


def _codes(text: str, constraints: OutputConstraints | None = None) -> set[str]:
    return {violation.code for violation in check_text(text, constraints or _constraints())}


def _snapshot():
    return empty_snapshot(
        snapshot_id="s1",
        turn_id="t1",
        conversation_id="c1",
        mood=PrimaryMood.HAPPY,
    )


# --------------------------------------------------------------------------- 规则化检查


@pytest.mark.parametrize(
    "text",
    ["（歪头）我才没有这么说呢。", "……好吧，我知道了.", "（抱臂）随便你吧。"],
)
def test_trailing_periods_are_rejected(text: str) -> None:
    assert "trailing_full_stop" in _codes(text)


@pytest.mark.parametrize(
    "text",
    [
        "（歪头）我才没有这么说呢",
        "（别开视线）……只是顺手而已",
        "版本 1.0 已经更新了，别想太多",
        "社团的猫咪今天打了 3.5 次滚，挺热闹的",
        "这里写着 abc.def 之类的东西，我也看不懂",
    ],
)
def test_normal_phrases_are_allowed(text: str) -> None:
    assert "trailing_full_stop" not in _codes(text)


def test_forbidden_phrase_is_rejected() -> None:
    assert "forbidden_phrase" in _codes("哼，我讨厌你")


def test_guidance_and_user_mind_are_rejected() -> None:
    assert "guidance_phrase" in _codes("你应该早点休息")
    assert "guidance_phrase" in _codes("你可以去问问别人")
    assert "user_mind_guess" in _codes("你一定觉得我很麻烦")
    assert "user_mind_guess" in _codes("你心里在想着别的事情")


def test_third_person_self_is_rejected() -> None:
    assert "third_person_self" in _codes("纱凪才不会承认这种事")


def test_over_boundary_restraint_is_rejected() -> None:
    import re

    constraints = _constraints(
        forbidden_patterns=(re.compile(r"(不许|不能|不准)(和|跟).{0,4}(异性|其他人|别人)"),)
    )
    assert "guidance_phrase" in _codes("不许和异性说话", constraints)


def test_length_and_sentence_limits() -> None:
    assert "too_long" in _codes("哼" * 200)
    assert "too_many_sentences" in _codes("嗯！好啊？当然……也行！", _constraints(max_sentences=2))


def test_empty_text_is_rejected() -> None:
    assert _codes("   ") == {"empty_text"}


def test_verbatim_copy_of_reference_is_rejected() -> None:
    assert "verbatim_copy" in _codes("好呀，刚好可以和你多待一会")


def test_normal_reply_passes_all_rules() -> None:
    text = "（歪着头）诶？你今天怎么这么安静……我只是随口问问"
    assert _codes(text) == set()


# --------------------------------------------------------------------------- 策略


async def test_rule_violation_without_model_is_blocked() -> None:
    policy = ConstrainedOutputPolicy(constraints=_constraints())
    decision = await policy.review(
        draft=DialogueDraft(kind="normal", text="哼，我讨厌你。"),
        snapshot=_snapshot(),
    )
    assert not decision.approved
    assert decision.reason_code == "output_blocked"
    assert "forbidden_phrase" in decision.violations


async def test_rule_only_approval_records_skipped_semantic_review() -> None:
    policy = ConstrainedOutputPolicy(constraints=_constraints())
    decision = await policy.review(
        draft=DialogueDraft(kind="normal", text="（别开视线）……只是顺手而已"),
        snapshot=_snapshot(),
    )
    assert decision.approved
    assert decision.details["semantic_review"] == "skipped_no_model"


async def test_one_rewrite_can_fix_violation() -> None:
    model = FakeModel()
    model.default_json = {"approved": True, "violations": []}
    model.queue_text("（皱起眉）你到底在说什么")
    policy = ConstrainedOutputPolicy(
        constraints=_constraints(), model=model, budget=ModelBudget(30.0)
    )
    decision = await policy.review(
        draft=DialogueDraft(kind="normal", text="（皱眉）你到底在说什么。"),
        snapshot=_snapshot(),
    )
    assert decision.approved
    assert decision.rewritten
    assert decision.reason_code == "rewritten"
    assert len(model.requests) == 2, "改写后仍需一次受约束语义审核"


async def test_rewrite_still_violating_is_blocked() -> None:
    model = FakeModel()
    model.queue_text("你到底在说什么。")
    policy = ConstrainedOutputPolicy(
        constraints=_constraints(), model=model, budget=ModelBudget(30.0)
    )
    decision = await policy.review(
        draft=DialogueDraft(kind="normal", text="（皱眉）你到底在说什么。"),
        snapshot=_snapshot(),
    )
    assert not decision.approved
    assert decision.reason_code == "output_blocked_after_rewrite"


async def test_semantic_review_can_reject() -> None:
    model = FakeModel()
    model.queue_json({"approved": False, "violations": ["user_mind_guess"]})
    policy = ConstrainedOutputPolicy(
        constraints=_constraints(), model=model, budget=ModelBudget(30.0)
    )
    decision = await policy.review(
        draft=DialogueDraft(kind="normal", text="（别开视线）……只是顺手而已"),
        snapshot=_snapshot(),
    )
    assert not decision.approved
    assert decision.reason_code == "output_blocked_semantic"


async def test_semantic_review_failure_rejects_draft() -> None:
    model = FakeModel()
    model.queue_failure(json=True, reason="model_timeout")
    policy = ConstrainedOutputPolicy(
        constraints=_constraints(), model=model, budget=ModelBudget(30.0)
    )
    decision = await policy.review(
        draft=DialogueDraft(kind="normal", text="（别开视线）……只是顺手而已"),
        snapshot=_snapshot(),
    )
    assert not decision.approved
    assert decision.reason_code == "semantic_review_error"


async def test_semantic_review_approval_passes() -> None:
    model = FakeModel()
    model.queue_json({"approved": True, "violations": []})
    policy = ConstrainedOutputPolicy(
        constraints=_constraints(), model=model, budget=ModelBudget(30.0)
    )
    decision = await policy.review(
        draft=DialogueDraft(kind="normal", text="（别开视线）……只是顺手而已"),
        snapshot=_snapshot(),
        stage=RetrievalStage.MONOLOGUE,
    )
    assert decision.approved
    assert decision.details["stage"] == "monologue"


async def test_monologue_and_reply_use_same_policy() -> None:
    policy = ConstrainedOutputPolicy(constraints=_constraints())
    for stage in (RetrievalStage.REPLY, RetrievalStage.MONOLOGUE, None):
        decision = await policy.review(
            draft=DialogueDraft(kind="normal", text="【心理独白】（低头）……其实有点高兴"),
            snapshot=_snapshot(),
            stage=stage,
        )
        assert decision.approved


def test_default_fallback_catalog_passes_output_constraints() -> None:
    from sana_bot.dialogue.fallback_catalog import (
        DEFAULT_FALLBACK_CATALOG,
        validate_catalog,
    )

    report = validate_catalog(DEFAULT_FALLBACK_CATALOG, _constraints())
    assert report == {}, f"兜底目录必须通过同一规范：{report}"
