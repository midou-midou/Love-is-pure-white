"""独白测试夹具：把队列、机会、调度与发送串起来（全离线）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sana_bot.config import RuntimeConfig
from sana_bot.dialogue.history import HistoryService
from sana_bot.dialogue.monologue import MonologueEvaluator
from sana_bot.dialogue.output_policy import ConstrainedOutputPolicy, OutputConstraints
from sana_bot.dialogue.service import DialogueTurnService
from sana_bot.enums import RelationshipStatus
from sana_bot.ports import ModelBudget
from sana_bot.retrieval.service import VectorRetriever
from sana_bot.retrieval.vector_store import VectorStore
from sana_bot.scheduling.dispatch import MonologueDispatcher
from sana_bot.scheduling.monologue import MonologueScheduler
from sana_bot.scheduling.serial_queue import QueueItem
from sana_bot.state.catalog import RuleCatalog
from sana_bot.state.event_extractor import LangChainStateEventExtractor
from sana_bot.state.reducer import DeterministicStateReducer
from sana_bot.storage.database import Database
from sana_bot.storage.delivery_repository import DeliveryRepository
from sana_bot.storage.diagnostic_repository import DiagnosticRepository
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.storage.index_repository import StructureRepository
from sana_bot.storage.monologue_repository import MonologueRepository
from sana_bot.storage.state_repository import StateRepository
from tests.support import FakeModel, FakeQQTransport, InlineRandom, make_event, SeededRandom

BASE_TIME = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


class MonologueHarness:
    """一套完整的离线独白链路。"""

    def __init__(
        self,
        database: Database,
        *,
        model: FakeModel,
        transport: FakeQQTransport,
        clock,
        config: RuntimeConfig | None = None,
        app_id: str = "app-1",
    ) -> None:
        self.database = database
        self.config = config or RuntimeConfig()
        self.model = model
        self.transport = transport
        self.clock = clock
        self.app_id = app_id
        self.inbound = InboundRepository(database)
        self.deliveries = DeliveryRepository(database, config=self.config)
        self.structures = StructureRepository(database)
        self.snapshots = StateRepository(database)
        self.monologues = MonologueRepository(database, config=self.config)
        self.history = HistoryService(
            structures=self.structures, inbound=self.inbound, deliveries=self.deliveries
        )
        self.diagnostics = DiagnosticRepository(database, config=self.config)
        self.scheduler = MonologueScheduler(
            repository=self.monologues,
            structures=self.structures,
            deliveries=self.deliveries,
            inbound=self.inbound,
            config=self.config,
            clock=clock,
        )
        self.dispatcher = MonologueDispatcher(
            database=database,
            deliveries=self.deliveries,
            monologues=self.monologues,
            inbound=self.inbound,
            transport=transport,
            clock=clock,
        )
        self.retriever: VectorRetriever | None = None
        self.turn_service = DialogueTurnService(
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
            model=model,
            transport=transport,
            diagnostics=self.diagnostics,
            post_delivery_hooks=(self._after_delivery,),
            clock=clock,
        )
        self.evaluator: MonologueEvaluator | None = None
        self.rng = SeededRandom()

    # ------------------------------------------------------------------ 装配

    def attach_index(self, fixture) -> None:
        self.retriever = VectorRetriever(
            structures=fixture.repository,
            embeddings=fixture.embeddings,
            store=VectorStore(structures=fixture.repository),
        )
        self.turn_service.retriever = self.retriever
        self.evaluator = MonologueEvaluator(
            database=self.database,
            config=self.config,
            monologues=self.monologues,
            structures=self.structures,
            inbound=self.inbound,
            snapshots=self.snapshots,
            history=self.history,
            dispatcher=self.dispatcher,
            policy=self.turn_service.policy,
            retriever=self.retriever,
            model=self.model,
            diagnostics=self.diagnostics,
            clock=self.clock,
            rng=self.rng,
            app_id=self.app_id,
        )

    def _after_delivery(self, turn_id: str, delivery_id: str, kind) -> None:
        from sana_bot.enums import OutboundKind

        if kind is not OutboundKind.NORMAL:
            return
        self.scheduler.create_for_turn(turn_id, delivery_id)

    # ------------------------------------------------------------------ 交互

    def submit(self, *, message_id: str, text: str, user_key: str = "openid-1", offset: float = 0.0):
        # 事件时间跟随时钟，避免长测试把被动回复窗口耗尽
        moment = self.clock.dt()
        event = make_event(
            message_id=message_id,
            user_key=user_key,
            text=text,
            event_time=moment,
            received_at=moment + timedelta(seconds=offset),
        )
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

    async def reply(self, *, message_id: str, text: str, user_key: str = "openid-1", offset: float = 0.0):
        item = self.submit(message_id=message_id, text=text, user_key=user_key, offset=offset)
        self.turn_service.snapshot_sink = self._persist_snapshot
        return await self.turn_service.handle(item)

    def _persist_snapshot(self, snapshot) -> None:
        with self.database.write():
            self.snapshots.save_snapshot(snapshot)

    async def run_due(self):
        """推进到期机会：逐个评估。"""
        results = []
        for opportunity in self.scheduler.due_now():
            results.append(await self.evaluator.evaluate(opportunity_id=opportunity.id))
        return results

    def pending(self):
        rows = self.database.query(
            "SELECT * FROM monologue_opportunity WHERE status = 'pending'"
        )
        return [self.monologues.get(row["id"]) for row in rows]


def inline_rng(values) -> InlineRandom:
    return InlineRandom(values)


def relationship_non_romantic(_user_id: str):
    return RelationshipStatus.NON_ROMANTIC, 0.0


def make_path(tmp_path: Path, name: str = "indexes") -> Path:
    return tmp_path / name
