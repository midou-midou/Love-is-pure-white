"""T035：条件优先检索、状态连续性、异常与无命中的区分。"""

from __future__ import annotations

import pytest

from sana_bot.enums import MatchDecision, PrimaryMood, RelationshipStatus, RetrievalStage, Scene
from sana_bot.retrieval.package_retrieval import PackageRetriever, build_query
from sana_bot.retrieval.package_selector import (
    DECISION_READY_WITHOUT_REFERENCES,
    DECISION_RELATIONSHIP_MISMATCH,
)
from sana_bot.state.catalog import catalog_from_package
from sana_bot.state.event_models import StateEvent
from sana_bot.state.models import empty_snapshot
from sana_bot.state.reducer import DeterministicStateReducer
from tests.support.package_harness import PackageHarness, build_package_harness, snapshot_for


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> PackageHarness:
    built = build_package_harness(tmp_path_factory.mktemp("context"))
    try:
        yield built
    finally:
        built.close()


def _retriever(harness: PackageHarness) -> PackageRetriever:
    return PackageRetriever(
        structures=harness.structures,
        reports=harness.reports,
        store=harness.store,
        embeddings=harness.backend,
        count_tokens=harness.backend.count_tokens,
    )


def _event(tag: str, quote: str) -> StateEvent:
    return StateEvent(tag=tag, source_message_id="m1", evidence_start=0, evidence_end=len(quote), quote=quote)


@pytest.mark.asyncio
async def test_inapplicable_high_similarity_card_is_excluded(harness):
    snapshot = snapshot_for(
        mood=PrimaryMood.NEUTRAL, relationship=RelationshipStatus.ROMANTIC, scene=Scene.PUBLIC
    )
    result = await _retriever(harness).retrieve(
        query="被你这么陪着，我安心多了，想靠在你身上",
        snapshot=snapshot,
        index_id=harness.index_id,
        stage=RetrievalStage.REPLY,
        medium="co_present",
    )
    selected = [item.unit_id for item in result.candidates]
    assert "sana_emotion_secure" not in selected
    evidence = {item["card_id"]: item for item in result.details["package"]["evidence"]}
    assert evidence["sana_emotion_secure"]["condition_ok"] is False
    assert evidence["sana_emotion_secure"]["score"] > 0.2


@pytest.mark.asyncio
async def test_retrieval_binds_versions_and_reports(harness):
    snapshot = snapshot_for(mood=PrimaryMood.NEUTRAL, relationship=RelationshipStatus.ROMANTIC)
    result = await _retriever(harness).retrieve(
        query="社团的小猫今天很粘人",
        snapshot=snapshot,
        index_id=harness.index_id,
        stage=RetrievalStage.REPLY,
    )
    details = result.details
    assert details["format"] == "structural_v2"
    assert details["bundle_hash"] == harness.bundle_hash
    assert details["calibration_id"] == harness.calibration_id
    assert details["report_hash"]
    assert result.snapshot_hash == harness.snapshot_hash


@pytest.mark.asyncio
async def test_empty_candidates_are_not_an_error(harness):
    snapshot = snapshot_for(mood=PrimaryMood.NEUTRAL, relationship=RelationshipStatus.ROMANTIC)
    result = await _retriever(harness).retrieve(
        query="嗯，这样待着就好",
        snapshot=snapshot,
        index_id=harness.index_id,
        stage=RetrievalStage.REPLY,
    )
    assert result.decision in (MatchDecision.EMPTY_CANDIDATES, MatchDecision.MATCHED)
    assert result.details["package_decision"] in (
        DECISION_READY_WITHOUT_REFERENCES,
        "ready_with_references",
    )


@pytest.mark.asyncio
async def test_relationship_mismatch_is_controlled(harness):
    snapshot = snapshot_for(
        mood=PrimaryMood.NEUTRAL, relationship=RelationshipStatus.NON_ROMANTIC
    )
    result = await _retriever(harness).retrieve(
        query="今天放学一起走吗",
        snapshot=snapshot,
        index_id=harness.index_id,
        stage=RetrievalStage.REPLY,
    )
    assert result.details["package_decision"] == DECISION_RELATIONSHIP_MISMATCH
    assert result.decision is MatchDecision.NO_MATERIAL


@pytest.mark.asyncio
async def test_encoding_failure_is_processing_error_not_low_match(harness):
    from tests.support import FakeEmbeddingsProvider

    class _Broken(FakeEmbeddingsProvider):
        @property
        def spec(self):  # type: ignore[override]
            return harness.backend.spec

        def embed_query(self, text):  # type: ignore[override]
            raise RuntimeError("boom")

    retriever = PackageRetriever(
        structures=harness.structures,
        reports=harness.reports,
        store=harness.store,
        embeddings=harness.backend,
        count_tokens=harness.backend.count_tokens,
    )
    retriever.embeddings = _Broken()  # type: ignore[assignment]
    snapshot = snapshot_for(mood=PrimaryMood.NEUTRAL, relationship=RelationshipStatus.ROMANTIC)
    result = await retriever.retrieve(
        query="社团的小猫",
        snapshot=snapshot,
        index_id=harness.index_id,
        stage=RetrievalStage.REPLY,
    )
    assert result.decision is MatchDecision.PROCESSING_ERROR
    assert result.reason_code in {"embedding_failed", "AttributeError"}


@pytest.mark.asyncio
async def test_missing_calibration_is_processing_error(tmp_path):
    built = build_package_harness(tmp_path / "no-cal", with_calibration=False)
    try:
        snapshot = snapshot_for(mood=PrimaryMood.NEUTRAL, relationship=RelationshipStatus.ROMANTIC)
        result = await _retriever(built).retrieve(
            query="社团的小猫",
            snapshot=snapshot,
            index_id=built.index_id,
            stage=RetrievalStage.REPLY,
        )
        assert result.decision is MatchDecision.PROCESSING_ERROR
        assert result.reason_code == "package_calibration_missing"
    finally:
        built.close()


@pytest.mark.asyncio
async def test_comforting_recovers_across_turns_and_keeps_same_card(harness):
    catalog = catalog_from_package(
        harness.package.config.state_catalog, version="catalog-v2"
    )
    reducer = DeterministicStateReducer(catalog)
    current = empty_snapshot(
        snapshot_id="s0",
        turn_id="t0",
        conversation_id="c",
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PRIVATE,
        dominant_state="平稳常态",
    )
    first = reducer.reduce(
        current=current,
        events=[_event("user_with_other", "刚才和别的女生聊得久了点")],
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PRIVATE,
    )
    assert first.dominant_state == "轻度吃醋"
    jealous_state = empty_snapshot(
        snapshot_id="s1",
        turn_id="t1",
        conversation_id="c",
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PRIVATE,
        dominant_state=first.dominant_state,
        tags=first.secondary_tags,
        mood=first.primary_mood,
    )
    selection = harness.select(
        query="刚才和别的女生聊得久了点", snapshot=jealous_state, medium="co_present"
    )
    assert "sana_emotion_jealous" in {card.card_id for card in selection.rule_cards}

    second = reducer.reduce(
        current=jealous_state,
        events=[_event("being_comforted", "抱歉，刚刚没顾上你")],
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PRIVATE,
    )
    assert second.dominant_state == "平稳常态"
    assert second.reason_code == "recovery_applied"


@pytest.mark.asyncio
async def test_query_includes_recent_context_and_state(harness):
    snapshot = snapshot_for(
        mood=PrimaryMood.ANGRY,
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PRIVATE,
        tags=("轻度吃醋",),
        dominant_state="轻度吃醋",
    )
    query = build_query(
        message="那你说说今天的事",
        history=(
            {"role": "user", "content": "刚才和别的女生聊得久了点"},
            {"role": "assistant", "content": "是挺久的"},
            {"role": "user", "content": "抱歉没顾上你"},
        ),
        snapshot=snapshot,
    )
    assert "刚才和别的女生聊得久了点" in query
    assert "当前状态：轻度吃醋" in query
    assert "场景：private" in query


def test_selector_does_not_treat_card_eligibility_as_action_permission(harness):
    """整卡在公共场合可用，但亲近动作仍由常驻规则限制。"""
    snapshot = snapshot_for(
        mood=PrimaryMood.ANGRY,
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PUBLIC,
        tags=("轻度吃醋",),
        dominant_state="轻度吃醋",
    )
    selection = harness.select(query="你和别人聊得挺开心", snapshot=snapshot)
    assert "sana_emotion_jealous" in {card.card_id for card in selection.rule_cards}
    assert selection.rule_cards[0].effective_conditions["scene"] == "any"
