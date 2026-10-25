"""语境检索接入对话：同义表达、语境不符不触发、示例不是固定剧本、歧义回退。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sana_bot.config import RuntimeConfig
from sana_bot.dialogue.history import HistoryService
from sana_bot.dialogue.output_policy import ConstrainedOutputPolicy, OutputConstraints
from sana_bot.dialogue.prompts import REFERENCE_PREAMBLE
from sana_bot.dialogue.service import DialogueTurnService
from sana_bot.enums import DeliveryStatus, MatchDecision, PrimaryMood, RelationshipStatus, Scene, RetrievalStage
from sana_bot.ports import ModelBudget
from sana_bot.retrieval.service import VectorRetriever
from sana_bot.retrieval.vector_store import VectorStore
from sana_bot.scheduling.serial_queue import QueueItem
from sana_bot.state.catalog import RuleCatalog
from sana_bot.state.event_extractor import LangChainStateEventExtractor
from sana_bot.state.reducer import DeterministicStateReducer
from sana_bot.storage.database import Database
from sana_bot.storage.delivery_repository import DeliveryRepository
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.storage.index_repository import StructureRepository
from tests.support import FakeModel, FakeQQTransport, make_event
from tests.support.index_fixture import UNIT_SPECS, build_indexed_fixture

BASE_TIME = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


class Harness:
    def __init__(
        self, database: Database, *, model: FakeModel, transport: FakeQQTransport, clock=None
    ) -> None:
        self.database = database
        self.model = model
        self.transport = transport
        self.inbound = InboundRepository(database)
        self.deliveries = DeliveryRepository(database, config=RuntimeConfig())
        self.structures = StructureRepository(database)
        self.history = HistoryService(
            structures=self.structures, inbound=self.inbound, deliveries=self.deliveries
        )
        self.diagnostics: list[object] = []
        self.service = DialogueTurnService(
            database=database,
            config=RuntimeConfig(),
            structures=self.structures,
            inbound=self.inbound,
            deliveries=self.deliveries,
            history=self.history,
            policy=ConstrainedOutputPolicy(constraints=OutputConstraints(), model=model),
            reducer=DeterministicStateReducer(RuleCatalog(version="empty")),
            catalog=RuleCatalog(version="empty"),
            extractor=LangChainStateEventExtractor(model=model, budget=ModelBudget(30.0)),
            model=model,
            transport=transport,
            clock=clock,
        )

    def attach(self, *, index_id: str, embeddings, store: VectorStore) -> None:
        self.service.retriever = VectorRetriever(
            structures=self.structures, embeddings=embeddings, store=store
        )
        self.index_id = index_id

    def submit(self, *, message_id: str, text: str, user_key: str = "openid-1") -> QueueItem:
        event = make_event(message_id=message_id, user_key=user_key, text=text, event_time=BASE_TIME)
        with self.database.write():
            outcome = self.inbound.persist_inbound(event)
        row = self.inbound.inbound_row(outcome.inbound_id or "")
        return QueueItem(
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
            user_key=user_key,
            message_id=row.message_id,
        )

    def generation_prompt(self) -> str:
        requests = [item for item in self.model.requests if item.json_schema_name is None]
        assert requests
        return " ".join(str(message["content"]) for message in requests[-1].messages)


@pytest.fixture
def harness(database: Database, tmp_path: Path, clock) -> Harness:
    fixture = build_indexed_fixture(database, artifact_root=tmp_path / "indexes")
    model = FakeModel()
    model.default_text = "（别开视线）……社团的猫咪今天挺黏人的"
    transport = FakeQQTransport(clock=clock)
    instance = Harness(database, model=model, transport=transport, clock=clock)
    instance.attach(
        index_id=fixture.index_id,
        embeddings=fixture.embeddings,
        store=VectorStore(structures=fixture.repository),
    )
    return instance


async def test_synonym_expression_reaches_same_context(harness: Harness) -> None:
    item = harness.submit(message_id="m-1", text="社团那边的猫怎么样了")
    outcome = await harness.service.handle(item)
    assert outcome.ok
    assert outcome.status == DeliveryStatus.CONFIRMED.value
    assert REFERENCE_PREAMBLE in harness.generation_prompt()
    assert "流浪猫" in harness.generation_prompt()


async def test_keyword_without_context_is_not_used(harness: Harness) -> None:
    """含相同关键词但状态条件不符时，不应把该片段当命中。"""
    harness.database.execute(
        "UPDATE semantic_unit SET conditions = ? WHERE id = ?",
        ('{"relationship": "any", "scene": "any", "primary_moods": ["angry"]}', "unit-club-cat"),
    )
    item = harness.submit(message_id="m-1", text="社团那边的猫怎么样了")
    outcome = await harness.service.handle(item)
    assert outcome.ok
    prompt = harness.generation_prompt()
    assert "流浪猫" not in prompt, "状态条件不符的片段不得进入参考"


async def test_examples_are_references_not_scripts(harness: Harness) -> None:
    harness.model.queue_text("（歪头）猫咪啊，今天特别黏人，一直蹭我的裤脚")
    item = harness.submit(message_id="m-1", text="社团的猫怎么样")
    outcome = await harness.service.handle(item)
    assert outcome.ok
    assert REFERENCE_PREAMBLE in harness.generation_prompt()
    assert outcome.text != "今天社团来了一只流浪猫，猫咪还一直跟着我"
    assert "今天社团来了一只流浪猫" in harness.generation_prompt()


async def test_other_character_lines_are_labelled_in_references(database: Database, tmp_path: Path) -> None:
    units = (
        {
            "id": "unit-rio-line",
            "text": "理央：姐姐才没有在意这件事",
            "types": ("example", "dialogue"),
            "conditions": {"relationship": "any", "scene": "any"},
            "speakers": ("rio",),
        },
    )
    fixture = build_indexed_fixture(
        database, artifact_root=tmp_path / "indexes", units=units, threshold=0.1
    )
    retriever = VectorRetriever(
        structures=fixture.repository,
        embeddings=fixture.embeddings,
        store=VectorStore(structures=fixture.repository),
    )
    from sana_bot.state.models import empty_snapshot

    result = await retriever.retrieve(
        query="姐姐才没有在意",
        snapshot=empty_snapshot(snapshot_id="s", turn_id="t", conversation_id="c"),
        index_id=fixture.index_id,
        stage=RetrievalStage.REPLY,
    )
    assert result.candidates
    assert result.candidates[0].conditions.get("specificity") is not None
    assert result.candidates[0].speaker_labels == ("rio",)


async def test_equal_score_different_rules_records_ambiguity(
    database: Database, tmp_path: Path
) -> None:
    units = (
        {
            "id": "unit-a",
            "text": "社团的猫咪今天很黏人",
            "types": ("example",),
            "conditions": {"relationship": "romantic", "scene": "private"},
            "speakers": ("sana",),
        },
        {
            "id": "unit-b",
            "text": "社团的猫咪今天比较安静",
            "types": ("example", "dialogue"),
            "conditions": {"relationship": "any", "scene": "private", "primary_moods": ["neutral"]},
            "speakers": ("sana",),
        },
    )
    fixture = build_indexed_fixture(
        database, artifact_root=tmp_path / "indexes", units=units, threshold=0.1
    )
    retriever = VectorRetriever(
        structures=fixture.repository,
        embeddings=fixture.embeddings,
        store=VectorStore(structures=fixture.repository),
    )
    from sana_bot.state.models import empty_snapshot

    snapshot = empty_snapshot(
        snapshot_id="s",
        turn_id="t",
        conversation_id="c",
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PRIVATE,
    )
    result = await retriever.retrieve(
        query="社团 猫咪",
        snapshot=snapshot,
        index_id=fixture.index_id,
        stage=RetrievalStage.REPLY,
    )
    assert result.decision is MatchDecision.MATCHED
    assert result.details["ambiguous"] is True
    assert result.details["ambiguity"]["code"] == "equal_score_different_rules"


async def test_no_hit_falls_back_to_confused(harness: Harness) -> None:
    harness.database.execute(
        "UPDATE calibration_profile SET threshold = 0.99 WHERE stage = 'reply'"
    )
    item = harness.submit(message_id="m-1", text="帮我写一段快速排序")
    outcome = await harness.service.handle(item)
    assert outcome.ok
    assert outcome.kind == "fallback"
    assert outcome.status == DeliveryStatus.CONFIRMED.value
    assert "没明白" in outcome.text or "没听懂" in outcome.text


async def test_reference_block_marks_types_and_scores(harness: Harness) -> None:
    item = harness.submit(message_id="m-1", text="社团的猫咪")
    await harness.service.handle(item)
    prompt = harness.generation_prompt()
    assert "类型=" in prompt and "相似度=" in prompt
    assert UNIT_SPECS[0]["text"] in prompt


async def test_mood_scope_limits_candidates(database: Database, tmp_path: Path) -> None:
    fixture = build_indexed_fixture(
        database, artifact_root=tmp_path / "indexes", threshold=0.1
    )
    retriever = VectorRetriever(
        structures=fixture.repository,
        embeddings=fixture.embeddings,
        store=VectorStore(structures=fixture.repository),
    )
    from sana_bot.state.models import empty_snapshot

    result = await retriever.retrieve(
        query="被冷落之后",
        snapshot=empty_snapshot(
            snapshot_id="s", turn_id="t", conversation_id="c", mood=PrimaryMood.SAD
        ),
        index_id=fixture.index_id,
        stage=RetrievalStage.REPLY,
    )
    assert "unit-sad-chew" in {candidate.unit_id for candidate in result.candidates}

    empty = await retriever.retrieve(
        query="被冷落之后",
        snapshot=empty_snapshot(
            snapshot_id="s", turn_id="t", conversation_id="c", mood=PrimaryMood.HAPPY
        ),
        index_id=fixture.index_id,
        stage=RetrievalStage.REPLY,
    )
    assert "unit-sad-chew" not in {candidate.unit_id for candidate in empty.candidates}
