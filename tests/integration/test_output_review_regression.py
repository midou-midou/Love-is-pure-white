"""输出审核回归：自报姓名豁免、审核协议不符合的重试与兜底分流。

对应问题：问「你的弟弟是谁」时检索已命中、草稿含「乾理央」，却被语义审核以
自创描述（`缺少待审核的角色输出`）拦下，并替换成「我不明白」兜底。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sana_bot.config import RuntimeConfig
from sana_bot.dialogue.fallback_catalog import DEFAULT_FALLBACK_CATALOG
from sana_bot.dialogue.history import HistoryService
from sana_bot.dialogue.output_policy import (
    SEMANTIC_REVIEW_PROMPT,
    SEMANTIC_REVIEW_PROMPT_STRICT,
    ConstrainedOutputPolicy,
    OutputConstraints,
    check_text,
)
from sana_bot.dialogue.service import DialogueTurnService
from sana_bot.enums import DiagnosticCategory, DeliveryStatus, PrimaryMood, RelationshipStatus
from sana_bot.ports import DialogueDraft, ModelBudget
from sana_bot.retrieval.service import VectorRetriever
from sana_bot.retrieval.vector_store import VectorStore
from sana_bot.state.catalog import RuleCatalog
from sana_bot.state.event_extractor import LangChainStateEventExtractor
from sana_bot.state.models import empty_snapshot
from sana_bot.state.reducer import DeterministicStateReducer
from sana_bot.storage.delivery_repository import DeliveryRepository
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.storage.index_repository import StructureRepository
from tests.support import FakeModel, FakeQQTransport, make_event
from tests.support.index_fixture import build_indexed_fixture

BASE_TIME = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
REPORTED_DRAFT = "我弟弟叫乾理央，老跟我拌嘴的那个\n你问这么多是想干嘛"
NONSENSE_VIOLATION = "缺少待审核的角色输出"


def _snapshot():
    return empty_snapshot(
        snapshot_id="s",
        turn_id="t",
        conversation_id="c",
        mood=PrimaryMood.NEUTRAL,
        relationship=RelationshipStatus.ROMANTIC,
    )


def _codes(text: str) -> set[str]:
    return {item.code for item in check_text(text, OutputConstraints())}


# --------------------------------------------------------------------------- 自报姓名


@pytest.mark.parametrize(
    "text",
    (
        "我叫乾纱凪，你叫我纱凪就好",
        "我是纱凪，怎么了",
        "叫我纱凪就好",
        "我的名字是乾纱凪",
    ),
)
def test_self_introduction_is_first_person(text):
    assert _codes(text) == set()


@pytest.mark.parametrize(
    "text",
    (
        "纱凪才没有在意",
        "乾纱凪觉得这样不好",
        "纱凪我可不吃这一套",  # 名字后接第一人称仍是第三人称叙述
    ),
)
def test_third_person_narration_is_still_blocked(text):
    assert "third_person_self" in _codes(text)


def test_reported_draft_passes_all_rules():
    assert _codes(REPORTED_DRAFT) == set()


# --------------------------------------------------------------------------- 审核协议


@pytest.mark.asyncio
async def test_nonsense_violation_is_retried_and_does_not_block():
    model = FakeModel()
    model.queue_json({"approved": False, "violations": [NONSENSE_VIOLATION]})
    policy = ConstrainedOutputPolicy(
        constraints=OutputConstraints(), model=model, budget=ModelBudget(30.0)
    )
    decision = await policy.review(
        draft=DialogueDraft(kind="normal", text=REPORTED_DRAFT), snapshot=_snapshot()
    )
    assert decision.approved is True
    reviews = [r for r in model.requests if r.json_schema_name == "output_review"]
    assert len(reviews) == 2, "协议不符合时应重试一次"


@pytest.mark.asyncio
async def test_known_violation_code_blocks_immediately():
    model = FakeModel()
    model.queue_json({"approved": False, "violations": ["user_mind_guess"]})
    policy = ConstrainedOutputPolicy(
        constraints=OutputConstraints(), model=model, budget=ModelBudget(30.0)
    )
    decision = await policy.review(
        draft=DialogueDraft(kind="normal", text="你一定是想我了"),
        snapshot=_snapshot(),
    )
    assert decision.approved is False
    assert decision.violations == ("user_mind_guess",)
    assert decision.reason_code == "output_blocked_semantic"
    assert len([r for r in model.requests if r.json_schema_name == "output_review"]) == 1


@pytest.mark.asyncio
async def test_persistent_protocol_violation_fails_closed_with_raw_text():
    model = FakeModel()
    model.queue_json(
        {"approved": False, "violations": [NONSENSE_VIOLATION]},
        {"approved": False, "violations": [NONSENSE_VIOLATION]},
    )
    policy = ConstrainedOutputPolicy(
        constraints=OutputConstraints(), model=model, budget=ModelBudget(30.0)
    )
    decision = await policy.review(
        draft=DialogueDraft(kind="normal", text=REPORTED_DRAFT), snapshot=_snapshot()
    )
    assert decision.approved is False
    assert decision.reason_code == "semantic_review_error"
    assert decision.details["semantic_review_attempts"] == 2
    assert decision.details["semantic_review_raw"]


@pytest.mark.asyncio
async def test_review_requests_wrap_the_draft_in_delimiters():
    model = FakeModel()
    policy = ConstrainedOutputPolicy(
        constraints=OutputConstraints(), model=model, budget=ModelBudget(30.0)
    )
    await policy.review(draft=DialogueDraft(kind="normal", text=REPORTED_DRAFT), snapshot=_snapshot())
    review = [r for r in model.requests if r.json_schema_name == "output_review"][0]
    assert "<待审核输出>" in review.messages[1]["content"]
    assert "</待审核输出>" in review.messages[1]["content"]
    system = str(review.messages[0]["content"])
    assert "user_mind_guess" in system
    assert "user_action_ghostwritten" not in SEMANTIC_REVIEW_PROMPT
    assert "user_action_ghostwritten" in SEMANTIC_REVIEW_PROMPT_STRICT


@pytest.mark.asyncio
async def test_string_true_is_accepted_as_approved():
    model = FakeModel()
    model.queue_json({"approved": "true", "violations": []})
    policy = ConstrainedOutputPolicy(
        constraints=OutputConstraints(), model=model, budget=ModelBudget(30.0)
    )
    decision = await policy.review(
        draft=DialogueDraft(kind="normal", text=REPORTED_DRAFT), snapshot=_snapshot()
    )
    assert decision.approved is True


# --------------------------------------------------------------------------- 兜底分流


def test_blocked_fallback_is_not_the_confused_text():
    blocked = DEFAULT_FALLBACK_CATALOG.text_for("blocked", PrimaryMood.NEUTRAL)
    confused = DEFAULT_FALLBACK_CATALOG.text_for("confused", PrimaryMood.NEUTRAL)
    assert blocked != confused
    assert "没明白" not in blocked
    assert "没听懂" not in blocked
    assert not check_text(blocked, OutputConstraints())


# --------------------------------------------------------------------------- 端到端


class _Harness:
    """最小语义索引对话链路（替代模型与传输），用于复现报告中的一轮。"""

    def __init__(self, database, *, model, transport, fixture) -> None:
        self.database = database
        self.config = RuntimeConfig()
        self.model = model
        self.transport = transport
        self.inbound = InboundRepository(database)
        self.deliveries = DeliveryRepository(database, config=self.config)
        self.structures = StructureRepository(database)
        self.history = HistoryService(
            structures=self.structures, inbound=self.inbound, deliveries=self.deliveries
        )
        retriever = VectorRetriever(
            structures=self.structures,
            embeddings=fixture.embeddings,
            store=VectorStore(structures=self.structures),
        )
        self.service = DialogueTurnService(
            database=database,
            config=self.config,
            structures=self.structures,
            inbound=self.inbound,
            deliveries=self.deliveries,
            history=self.history,
            policy=ConstrainedOutputPolicy(
                constraints=OutputConstraints(), model=model, budget=ModelBudget(30.0)
            ),
            reducer=DeterministicStateReducer(RuleCatalog(version="empty")),
            catalog=RuleCatalog(version="empty"),
            extractor=LangChainStateEventExtractor(model=model, budget=ModelBudget(30.0)),
            retriever=retriever,
            model=model,
            transport=transport,
            diagnostics=_Recorder(database),
        )

    async def say(self, text: str):
        event = make_event(
            message_id="m-1",
            user_key="openid-a",
            text=text,
            event_time=BASE_TIME,
            received_at=BASE_TIME + timedelta(milliseconds=5),
        )
        with self.database.write():
            outcome = self.inbound.persist_inbound(event)
        row = self.inbound.inbound_row(outcome.inbound_id or "")
        from sana_bot.scheduling.serial_queue import QueueItem

        item = QueueItem(
            inbound_id=row.id,
            user_id=row.user_id,
            conversation_id=row.conversation_id,
            generation=outcome.generation,
            text=row.text,
            chat_day=row.chat_day,
            received_at=row.received_at,
            event_time=row.event_time,
            correlation_id=row.id,
            app_id=row.app_id,
            user_key="openid-a",
            message_id=row.message_id,
        )
        return await self.service.handle(item)


class _Recorder:
    def __init__(self, database) -> None:
        self.rows: list = []
        self._database = database

    def record(self, record):
        self.rows.append(record)
        return None


@pytest.fixture
def harness(database, clock, tmp_path):
    fixture = build_indexed_fixture(database, artifact_root=tmp_path / "indexes")
    model = FakeModel()
    transport = FakeQQTransport(clock=clock)
    return _Harness(database, model=model, transport=transport, fixture=fixture)


@pytest.mark.asyncio
async def test_retrieval_hit_is_not_replaced_by_confused_text_after_nonsense_review(harness):
    """检索命中 + 审核器自创违规 → 重试通过，发送正确草稿而不是「我不明白」。"""
    harness.model.queue_json({"events": []})
    harness.model.queue_json({"approved": False, "violations": [NONSENSE_VIOLATION]})
    harness.model.queue_text(REPORTED_DRAFT)
    outcome = await harness.say("今天社团的小猫怎么样")
    assert outcome.ok is True
    assert outcome.status == DeliveryStatus.CONFIRMED.value
    assert "乾理央" in outcome.text
    assert "没明白" not in outcome.text


@pytest.mark.asyncio
async def test_blocked_output_uses_neutral_fallback_and_records_evidence(harness):
    harness.model.queue_json({"events": []})
    harness.model.queue_json({"approved": False, "violations": ["user_mind_guess"]})
    harness.model.queue_text(REPORTED_DRAFT)
    # 兜底句也要重新过一遍审核
    harness.model.queue_json({"approved": True, "violations": []})
    outcome = await harness.say("今天社团的小猫怎么样")

    blocked = DEFAULT_FALLBACK_CATALOG.text_for("blocked", PrimaryMood.NEUTRAL)
    assert outcome.text == blocked
    assert "没明白" not in outcome.text

    blocked_records = [
        row for row in harness.service.diagnostics.rows if row.category is DiagnosticCategory.OUTPUT_BLOCKED
    ]
    assert blocked_records
    payload = blocked_records[0].payload
    assert payload["violations"] == ["user_mind_guess"]
    assert payload["reviewed_text"] == REPORTED_DRAFT
    assert payload["reviewed_text"]
