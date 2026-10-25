"""一个用户轮次的完整处理链路。

入站 → 关系/场景读取 → 事件提取与批准规则转换 → 冻结快照 → 检索/低匹配判断 →
生成或疑惑兜底 → 输出规范校验 → 发送资格检查 → 发送 → 确认后结算与独白机会。

任何环节失败都留下可诊断结果，不冒充成功回复；所有待发文本都经过同一输出规范。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sana_bot.config import RuntimeConfig
from sana_bot.diagnostics import DiagnosticCategory, DiagnosticRecord
from sana_bot.dialogue.fallback_catalog import DEFAULT_FALLBACK_CATALOG, FallbackCatalog
from sana_bot.dialogue.history import HistoryService, MaterialAvailability
from sana_bot.dialogue.match_decision import decide_match
from sana_bot.dialogue.output_policy import ConstrainedOutputPolicy
from sana_bot.dialogue.prompts import (
    build_generation_messages,
    package_extra_rules,
    package_reference_units,
    to_reference_units,
)
from sana_bot.enums import (
    DeliveryStatus,
    InboundStatus,
    MatchDecision,
    OutboundKind,
    PrimaryMood,
    RelationshipStatus,
    RetrievalStage,
    Scene,
)
from sana_bot.ports import (
    DialogueDraft,
    ModelBudget,
    ModelPort,
    ModelRequest,
    QQTransport,
    RetrievalResult,
    Retriever,
    StateEventExtractor,
)
from sana_bot.scheduling.serial_queue import QueueItem
from sana_bot.retrieval.package_retrieval import build_query
from sana_bot.retrieval.package_selector import (
    DECISION_AMBIGUOUS_REFERENCE,
    DECISION_CONTEXT_OVER_LIMIT,
    DECISION_READY_WITHOUT_REFERENCES,
    GENERATABLE_DECISIONS,
)
from sana_bot.state.catalog import RuleCatalog, catalog_from_package
from sana_bot.state.models import StateSnapshot, empty_snapshot
from sana_bot.state.reducer import DeterministicStateReducer
from sana_bot.storage.database import Database
from sana_bot.storage.delivery_repository import DeliveryRepository, sequence_for
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.storage.index_repository import StructureRepository
from sana_bot.timeutil import to_utc_iso, utc_now

__all__ = ["TurnOutcome", "DialogueTurnService", "RelationshipProvider", "SnapshotSink"]

RelationshipProvider = Callable[[str], tuple[RelationshipStatus, float]]
SnapshotSink = Callable[[StateSnapshot], None]
PostDeliveryHook = Callable[[str, str, OutboundKind], None]


@dataclass(frozen=True)
class TurnOutcome:
    ok: bool
    turn_id: str = ""
    status: str = "failed"
    text: str = ""
    kind: str = "normal"
    delivery_id: str | None = None
    delivery_status: str | None = None
    reason_code: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


class DialogueTurnService:
    """组合各端口完成一轮处理；不直接访问 QQ 之外的任何外部系统。"""

    def __init__(
        self,
        *,
        database: Database,
        config: RuntimeConfig,
        structures: StructureRepository,
        inbound: InboundRepository,
        deliveries: DeliveryRepository,
        history: HistoryService,
        policy: ConstrainedOutputPolicy,
        reducer: DeterministicStateReducer,
        catalog: RuleCatalog,
        extractor: StateEventExtractor | None = None,
        retriever: Retriever | None = None,
        model: ModelPort | None = None,
        transport: QQTransport | None = None,
        fallbacks: FallbackCatalog = DEFAULT_FALLBACK_CATALOG,
        diagnostics: Any | None = None,
        relationship_provider: RelationshipProvider | None = None,
        snapshot_sink: SnapshotSink | None = None,
        command_handler: Callable[[str, str], Awaitable[DialogueDraft | None]] | None = None,
        post_delivery_hooks: Sequence[PostDeliveryHook] = (),
        clock: Any | None = None,
        previous_state_provider: Callable[[str, str], StateSnapshot] | None = None,
        packages: Any | None = None,
        package_retriever: Any | None = None,
        package_policy: Any | None = None,
        embeddings: Any | None = None,
    ) -> None:
        self.database = database
        self.config = config
        self.structures = structures
        self.inbound = inbound
        self.deliveries = deliveries
        self.history = history
        self.policy = policy
        self.reducer = reducer
        self.catalog = catalog
        self.extractor = extractor
        self.retriever = retriever
        self.model = model
        self.transport = transport
        self.fallbacks = fallbacks
        self.diagnostics = diagnostics
        self.relationship_provider = relationship_provider
        self.snapshot_sink = snapshot_sink
        self.command_handler = command_handler
        self.post_delivery_hooks = tuple(post_delivery_hooks)
        self.clock = clock
        self.previous_state_provider = previous_state_provider
        self.packages = packages
        self.package_retriever = package_retriever
        self.package_policy = package_policy
        self.embeddings = embeddings

    # ------------------------------------------------------------------ 入口

    async def handle(self, item: QueueItem) -> TurnOutcome:
        existing = self.inbound.turn_for_inbound(item.inbound_id)
        if existing is not None:
            # 未支持或重复事件不能生成重复轮次
            return TurnOutcome(
                ok=True,
                turn_id=existing.id,
                status="duplicate",
                reason_code="duplicate_inbound",
            )
        turn_id = self._start_turn(item)
        try:
            return await self._process(item, turn_id)
        except Exception as exc:  # noqa: BLE001 - 单轮失败不能拖垮用户通道
            self._record(
                DiagnosticCategory.STATE_TRANSITION,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                turn_id=turn_id,
                reason="turn_exception",
                payload={"error": type(exc).__name__},
            )
            with self.database.write():
                self.inbound.set_turn_status(turn_id, "failed")
                self.inbound.set_inbound_status(item.inbound_id, InboundStatus.FAILED)
            return TurnOutcome(
                ok=False,
                turn_id=turn_id,
                status="failed",
                reason_code="turn_exception",
                details={"error": type(exc).__name__},
            )

    def _start_turn(self, item: QueueItem) -> str:
        with self.database.write():
            self.inbound.set_inbound_status(item.inbound_id, InboundStatus.PROCESSING)
            return self.inbound.create_turn(
                conversation_id=item.conversation_id,
                inbound_id=item.inbound_id,
                input_text=item.text,
                chat_day_value=item.chat_day,
                correlation_id=item.correlation_id,
            )

    # ------------------------------------------------------------------ 主流程

    def _restart_model_budget(self) -> None:
        """每轮复位共享模型预算：提取/生成/审核/改写共用本轮 30 秒，不跨轮累计。

        生产装配里三者共用同一个 :class:`ModelBudget`；它若不复位，进程启动 30 秒后的
        每一轮都会直接判定预算耗尽，状态提取与生成全部降级。
        """
        seen: set[int] = set()
        for owner in (self.model, self.extractor, self.policy):
            budget = getattr(owner, "budget", None)
            reset = getattr(budget, "reset", None)
            if reset is None or id(budget) in seen:
                continue
            seen.add(id(budget))
            reset()

    async def _process(self, item: QueueItem, turn_id: str) -> TurnOutcome:
        self._restart_model_budget()
        relationship, affection_before = self._relationship(item.user_id)
        scene = self._scene(item.conversation_id)
        material = self.history.material(allow_degraded=True)
        conversation = self.history.history_for(
            item.conversation_id, generation=item.generation
        )

        if (
            material.snapshot is not None
            and material.snapshot.is_structural
            and not material.available
        ):
            # 新格式缺常驻或报告时必须受控失败，不能退回旧的无约束生成
            self._record(
                DiagnosticCategory.INDEX_FAILURE,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                turn_id=turn_id,
                reason=material.reason_code or "package_unavailable",
                payload={"notes": list(material.notes)},
            )
            return self._finish_without_send(
                item,
                turn_id,
                material.reason_code or "package_unavailable",
                {"notes": list(material.notes)},
            )

        if material.available and material.format_version == "structural_v2":
            return await self._process_package(
                item=item,
                turn_id=turn_id,
                material=material,
                relationship=relationship,
                affection_before=affection_before,
                scene=scene,
            )

        snapshot = await self._snapshot(item, turn_id, relationship, affection_before, scene)

        if self.command_handler is not None:
            command = await self.command_handler(item.text, item.user_id)
            if command is not None:
                return await self._deliver(
                    item,
                    turn_id,
                    command,
                    snapshot=snapshot,
                    kind=OutboundKind.STATUS,
                    material=material,
                )

        retrieval = await self._retrieve(item, snapshot, material)
        if retrieval is not None and retrieval.details.get("ambiguous"):
            self._record(
                DiagnosticCategory.MATCH_ERROR,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                turn_id=turn_id,
                reason="ambiguous_rules",
                payload=dict(retrieval.details.get("ambiguity") or {}),
            )
        references = to_reference_units(retrieval.candidates) if retrieval else ()
        draft, degraded_reason = await self._compose(
            item=item,
            snapshot=snapshot,
            conversation=conversation,
            references=references,
            retrieval=retrieval,
            material=material,
        )

        decision = await self.policy.review(
            draft=draft,
            snapshot=snapshot,
            stage=RetrievalStage.REPLY,
            correlation_id=item.correlation_id,
        )
        if not decision.approved:
            self._record(
                DiagnosticCategory.OUTPUT_BLOCKED,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                turn_id=turn_id,
                reason=decision.reason_code or "output_blocked",
                payload={
                    "violations": list(decision.violations),
                    "draft": draft.text,
                    # 审核对象可能是改写后的文本；同时保留审核器原始返回便于排查
                    "reviewed_text": decision.text or draft.text,
                    "rewritten": bool(decision.rewritten),
                    "review_attempts": decision.details.get("semantic_review_attempts"),
                    "semantic_review_raw": decision.details.get("semantic_review_raw"),
                    "kind": draft.kind,
                },
            )
            fallback = self.fallbacks.draft_for(
                "blocked",
                mood=snapshot.primary_mood,
                reason_code=decision.reason_code or "output_blocked",
            )
            retry = await self.policy.review(
                draft=fallback,
                snapshot=snapshot,
                stage=RetrievalStage.REPLY,
                correlation_id=item.correlation_id,
            )
            if not retry.approved:
                return self._finish_without_send(
                    item,
                    turn_id,
                    "output_blocked",
                    {"violations": list(retry.violations)},
                )
            draft = DialogueDraft(
                kind="fallback",
                text=retry.text,
                reason_code=decision.reason_code,
                correlation_id=item.correlation_id,
            )
        elif decision.rewritten:
            draft = DialogueDraft(
                kind=draft.kind,
                text=decision.text,
                source_candidates=draft.source_candidates,
                reason_code=decision.reason_code,
                correlation_id=item.correlation_id,
            )

        kind = OutboundKind.FALLBACK if draft.kind == "fallback" else OutboundKind.NORMAL
        outcome = await self._deliver(
            item,
            turn_id,
            draft,
            snapshot=snapshot,
            kind=kind,
            material=material,
            retrieval=retrieval,
            degraded_reason=degraded_reason,
        )
        return outcome

    # ------------------------------------------------------------------ 资料包路径

    async def _process_package(
        self,
        *,
        item: QueueItem,
        turn_id: str,
        material: MaterialAvailability,
        relationship: RelationshipStatus,
        affection_before: float,
        scene: Scene,
    ) -> TurnOutcome:
        """新格式一轮：固定包 → 关系守卫 → 状态归约 → 条件选卡 → 生成 → 审核 → 发送。"""
        from sana_bot.retrieval.package_retrieval import PackageRetriever
        from sana_bot.retrieval.vector_store import VectorStore

        view = material.view
        index = material.index
        if view is None or index is None:
            self._record(
                DiagnosticCategory.INDEX_FAILURE,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                turn_id=turn_id,
                reason="package_unavailable",
                payload={"notes": list(material.notes)},
            )
            return self._finish_without_send(
                item, turn_id, "package_unavailable", {"notes": list(material.notes)}
            )

        required = view.config.required_relationship
        if required not in ("", "any") and required != relationship.value:
            self._record(
                DiagnosticCategory.MATCH_ERROR,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                turn_id=turn_id,
                reason="package_relationship_mismatch",
                payload={"required": required, "actual": relationship.value},
            )
            return self._finish_without_send(
                item,
                turn_id,
                "package_relationship_mismatch",
                {"required": required, "actual": relationship.value},
            )

        package_catalog = catalog_from_package(
            view.config.state_catalog, version=view.config.catalog_version
        )
        reducer = DeterministicStateReducer(package_catalog)
        conversation = self.history.history_for(
            item.conversation_id, generation=item.generation
        )
        previous = self._previous_state(item, relationship, affection_before, scene)
        events: Sequence[Any] = ()
        if self.extractor is not None:
            extraction = await self.extractor.extract(
                message=item.text,
                source_message_id=item.message_id or item.inbound_id,
                catalog=package_catalog.event_catalog,
                correlation_id=item.correlation_id,
                history=tuple(
                    {
                        "message_id": entry.turn_id,
                        "role": entry.role,
                        "content": entry.text,
                    }
                    for entry in conversation.entries
                ),
                previous_state=previous.dominant_state,
            )
            if extraction.ok:
                events = extraction.events
            else:
                self._record(
                    DiagnosticCategory.STATE_TRANSITION,
                    correlation_id=item.correlation_id,
                    user_id=item.user_id,
                    conversation_id=item.conversation_id,
                    turn_id=turn_id,
                    reason=extraction.reason_code or "no_event",
                    payload={"details": dict(extraction.details)},
                )
        reduced = reducer.reduce(
            current=previous,
            events=events,
            relationship=relationship,
            scene=scene,
            correlation_id=item.correlation_id,
        )
        snapshot = StateSnapshot(
            id=uuid.uuid4().hex,
            turn_id=turn_id,
            conversation_id=item.conversation_id,
            relationship_status=relationship,
            affection_before=affection_before,
            primary_mood=reduced.primary_mood,
            secondary_tags=reduced.secondary_tags,
            dominant_state=reduced.dominant_state,
            scene=scene,
            catalog_version=package_catalog.version,
            transition_evidence=reduced.transition_evidence,
            created_at=to_utc_iso(utc_now()),
            source="turn",
        )
        with self.database.write():
            self.inbound.attach_snapshot(turn_id, snapshot.id)
        if self.snapshot_sink is not None:
            try:
                self.snapshot_sink(snapshot)
            except Exception as exc:  # noqa: BLE001 - 持久化失败需可诊断
                self._record(
                    DiagnosticCategory.STATE_TRANSITION,
                    correlation_id=item.correlation_id,
                    user_id=item.user_id,
                    turn_id=turn_id,
                    reason="snapshot_persist_failed",
                    payload={"error": type(exc).__name__},
                )

        retriever = self.package_retriever
        if retriever is None:
            if self.packages is None:
                return self._finish_without_send(
                    item, turn_id, "package_unavailable", {"error": "retriever_unconfigured"}
                )
            retriever = PackageRetriever(
                structures=self.structures,
                reports=self.packages,
                store=VectorStore(structures=self.structures),
                embeddings=self.embeddings,
            )
        query = build_query(
            message=item.text,
            history=conversation.as_messages(),
            snapshot=snapshot,
        )
        history_tokens = sum(len(entry.text) for entry in conversation.entries)
        dropped_turns = 0
        try:
            while True:
                retrieval = await retriever.retrieve(
                    query=query,
                    snapshot=snapshot,
                    index_id=material.index_id or "",
                    stage=RetrievalStage.REPLY,
                    correlation_id=item.correlation_id,
                    medium="text",
                    history_tokens=history_tokens,
                )
                if (
                    retrieval.details.get("package_decision") != DECISION_CONTEXT_OVER_LIMIT
                    or len(conversation.entries) <= 2
                ):
                    break
                # 只按完整旧轮次移除，保留本轮消息与状态证据
                conversation = _drop_oldest_entries(conversation, 2)
                dropped_turns += 2
                history_tokens = sum(len(entry.text) for entry in conversation.entries)
                query = build_query(
                    message=item.text,
                    history=conversation.as_messages(),
                    snapshot=snapshot,
                )
        except Exception as exc:  # noqa: BLE001 - 检索异常必须与低分区分
            self._record(
                DiagnosticCategory.MATCH_ERROR,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                turn_id=turn_id,
                reason="retrieval_exception",
                payload={"error": type(exc).__name__},
            )
            return self._finish_without_send(
                item, turn_id, "processing_error", {"error": type(exc).__name__}
            )

        if dropped_turns:
            self._record(
                DiagnosticCategory.STATE_TRANSITION,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                turn_id=turn_id,
                reason="history_trimmed",
                payload={"dropped_entries": dropped_turns},
            )

        package_decision = str(retrieval.details.get("package_decision") or "")
        selection = retrieval.details.get("selection")
        self._persist_package_evidence(
            item=item,
            turn_id=turn_id,
            material=material,
            snapshot=snapshot,
            selection=selection,
            decision=package_decision,
        )
        if package_decision not in GENERATABLE_DECISIONS:
            reason = retrieval.reason_code or package_decision or "package_unavailable"
            self._record(
                DiagnosticCategory.MATCH_ERROR if package_decision != DECISION_CONTEXT_OVER_LIMIT else DiagnosticCategory.INDEX_FAILURE,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                turn_id=turn_id,
                reason=reason,
                payload={
                    "package_decision": package_decision,
                    "details": dict(retrieval.details.get("package") or {}).get("details", {}),
                },
            )
            return self._finish_without_send(
                item, turn_id, reason, {"package_decision": package_decision}
            )

        if package_decision == DECISION_AMBIGUOUS_REFERENCE:
            self._record(
                DiagnosticCategory.MATCH_ERROR,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                turn_id=turn_id,
                reason="ambiguous_rules",
                payload=dict(retrieval.details.get("ambiguity") or {}),
            )

        references = package_reference_units(selection) if selection is not None else ()
        extra_rules = package_extra_rules(
            residents=index.residents,
            rule_cards=getattr(selection, "rule_cards", ()),
        )
        policy = self._package_policy()
        draft = await self._generate_package(
            item=item,
            snapshot=snapshot,
            conversation=conversation,
            references=references,
            extra_rules=extra_rules,
            policy=policy,
            selection=selection,
        )
        if draft.kind != "normal":
            # 模型失败的受控提示不伪装成正常生成
            return self._finish_without_send(
                item, turn_id, draft.reason_code or "model_failure", {"kind": draft.kind}
            )

        decision = await policy.review(
            draft=draft,
            snapshot=snapshot,
            stage=RetrievalStage.REPLY,
            correlation_id=item.correlation_id,
        )
        if not decision.approved:
            self._record(
                DiagnosticCategory.OUTPUT_BLOCKED,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                turn_id=turn_id,
                reason=decision.reason_code or "output_blocked",
                payload={
                    "violations": list(decision.violations),
                    "kind": draft.kind,
                    "package_decision": package_decision,
                },
            )
            return self._finish_without_send(
                item,
                turn_id,
                decision.reason_code or "output_blocked",
                {"violations": list(decision.violations)},
            )
        if decision.rewritten:
            draft = DialogueDraft(
                kind=draft.kind,
                text=decision.text,
                source_candidates=draft.source_candidates,
                reason_code=decision.reason_code,
                correlation_id=item.correlation_id,
            )
        if package_decision == DECISION_READY_WITHOUT_REFERENCES:
            self._record(
                DiagnosticCategory.LOW_MATCH,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                turn_id=turn_id,
                reason="ready_without_references",
                payload={
                    "decision": package_decision,
                    "candidate_count": len(retrieval.candidates),
                    "threshold": retrieval.threshold,
                    "resident_ids": list(material.details.get("resident_ids") or ()),
                },
            )
        return await self._deliver(
            item,
            turn_id,
            draft,
            snapshot=snapshot,
            kind=OutboundKind.NORMAL,
            material=material,
            retrieval=retrieval,
        )

    def _package_policy(self) -> Any:
        if self.package_policy is not None:
            return self.package_policy
        from dataclasses import replace as _replace

        return ConstrainedOutputPolicy(
            constraints=_replace(self.policy.constraints, strict_package_rules=True),
            model=getattr(self.policy, "model", None) or self.model,
            budget=getattr(self.policy, "budget", None),
        )

    def _previous_state(
        self,
        item: QueueItem,
        relationship: RelationshipStatus,
        affection_before: float,
        scene: Scene,
    ) -> StateSnapshot:
        if self.previous_state_provider is not None:
            return self.previous_state_provider(item.user_id, item.conversation_id)
        return empty_snapshot(
            snapshot_id=f"prev-{item.inbound_id}",
            turn_id=item.inbound_id,
            conversation_id=item.conversation_id,
            relationship=relationship,
            affection_before=affection_before,
            scene=scene,
            catalog_version="",
        )

    async def _generate_package(
        self,
        *,
        item: QueueItem,
        snapshot: StateSnapshot,
        conversation: Any,
        references: Sequence[Any],
        extra_rules: Sequence[str],
        policy: Any,
        selection: Any,
    ) -> DialogueDraft:
        if self.model is None:
            return self.fallbacks.draft_for(
                "failure", mood=snapshot.primary_mood, reason_code="model_unavailable"
            )
        messages = build_generation_messages(
            message=item.text,
            history=conversation.as_messages(),
            snapshot=snapshot,
            constraints=policy.constraints,
            references=references,
            extra_rules=extra_rules,
        )
        budget = getattr(policy, "budget", None) or ModelBudget(self.config.max_reply_chars)
        result = await self.model.complete_text(
            ModelRequest(messages=messages, correlation_id=item.correlation_id),
            budget,
        )
        if not result.ok or not result.text.strip():
            self._record(
                DiagnosticCategory.DELIVERY_ERROR,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                reason=result.reason_code or "model_failure",
                payload={"model": result.model},
            )
            return self.fallbacks.draft_for(
                "failure",
                mood=snapshot.primary_mood,
                reason_code=result.reason_code or "model_failure",
            )
        return DialogueDraft(
            kind="normal",
            text=result.text.strip(),
            source_candidates=tuple(
                card.card_id for card in getattr(selection, "supplements", ())
            ),
            correlation_id=item.correlation_id,
        )

    def _persist_package_evidence(
        self,
        *,
        item: QueueItem,
        turn_id: str,
        material: MaterialAvailability,
        snapshot: StateSnapshot,
        selection: Any,
        decision: str,
    ) -> None:
        if self.packages is None or material.snapshot is None:
            return
        try:
            payloads = (
                [item.to_payload() for item in selection.evidence]
                if selection is not None
                else []
            )
            self.packages.record_evidence(
                index_id=material.snapshot.id,
                snapshot_hash=material.snapshot.snapshot_hash,
                policy_hash=material.policy_hash,
                bundle_hash=material.bundle_hash,
                catalog_hash=material.catalog_hash,
                resident_ids=tuple(material.details.get("resident_ids") or ()),
                turn_id=turn_id,
                correlation_id=item.correlation_id,
                query_ref=(item.message_id or item.inbound_id),
                state_snapshot=snapshot.to_payload(),
                selections=payloads,
                final_decision=decision,
                omissions=[
                    payload
                    for payload in payloads
                    if not payload.get("selected")
                ],
            )
        except Exception:  # noqa: BLE001 - 证据写入失败不得影响用户可见回复
            self._record(
                DiagnosticCategory.LOGGING_ERROR,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                turn_id=turn_id,
                reason="evidence_persist_failed",
            )

    # ------------------------------------------------------------------ 各步骤

    def _relationship(self, user_id: str) -> tuple[RelationshipStatus, float]:
        if self.relationship_provider is None:
            return RelationshipStatus.NON_ROMANTIC, 0.0
        return self.relationship_provider(user_id)

    def _scene(self, conversation_id: str) -> Scene:
        row = self.inbound.conversation_row(conversation_id)
        if row is None:
            return Scene.UNKNOWN
        return Scene(row["scene"])

    async def _snapshot(
        self,
        item: QueueItem,
        turn_id: str,
        relationship: RelationshipStatus,
        affection_before: float,
        scene: Scene,
    ) -> StateSnapshot:
        if self.previous_state_provider is not None:
            # 管理设置与上一轮冻结状态共同构成起始状态
            previous = self.previous_state_provider(item.user_id, item.conversation_id)
        else:
            previous = empty_snapshot(
                snapshot_id=f"prev-{turn_id}",
                turn_id=turn_id,
                conversation_id=item.conversation_id,
                relationship=relationship,
                affection_before=affection_before,
                scene=scene,
                catalog_version=self.catalog.version,
            )
        events: Sequence[Any] = ()
        if self.extractor is not None:
            extraction = await self.extractor.extract(
                message=item.text,
                source_message_id=item.message_id or item.inbound_id,
                catalog=self.catalog.event_catalog,
                correlation_id=item.correlation_id,
            )
            if extraction.ok:
                events = extraction.events
            else:
                self._record(
                    DiagnosticCategory.STATE_TRANSITION,
                    correlation_id=item.correlation_id,
                    user_id=item.user_id,
                    conversation_id=item.conversation_id,
                    turn_id=turn_id,
                    reason=extraction.reason_code or "no_event",
                    payload={"details": dict(extraction.details)},
                )
        reduced = self.reducer.reduce(
            current=previous,
            events=events,
            relationship=relationship,
            scene=scene,
            correlation_id=item.correlation_id,
        )
        snapshot = StateSnapshot(
            id=uuid.uuid4().hex,
            turn_id=turn_id,
            conversation_id=item.conversation_id,
            relationship_status=relationship,
            affection_before=affection_before,
            primary_mood=reduced.primary_mood,
            secondary_tags=reduced.secondary_tags,
            dominant_state=reduced.dominant_state,
            scene=scene,
            catalog_version=self.catalog.version,
            transition_evidence=reduced.transition_evidence,
            created_at=to_utc_iso(utc_now()),
            source="turn",
        )
        with self.database.write():
            self.inbound.attach_snapshot(turn_id, snapshot.id)
        if self.snapshot_sink is not None:
            try:
                self.snapshot_sink(snapshot)
            except Exception as exc:  # noqa: BLE001 - 快照持久化失败需可诊断
                self._record(
                    DiagnosticCategory.STATE_TRANSITION,
                    correlation_id=item.correlation_id,
                    user_id=item.user_id,
                    turn_id=turn_id,
                    reason="snapshot_persist_failed",
                    payload={"error": type(exc).__name__},
                )
        if reduced.conflict:
            self._record(
                DiagnosticCategory.STATE_TRANSITION,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                turn_id=turn_id,
                reason="conflict",
                payload={"conflict": reduced.conflict},
            )
        return snapshot

    async def _retrieve(
        self,
        item: QueueItem,
        snapshot: StateSnapshot,
        material: MaterialAvailability,
    ) -> RetrievalResult | None:
        if not material.available or self.retriever is None or not material.index_id:
            self._record(
                DiagnosticCategory.INDEX_FAILURE,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                reason=material.reason_code or "retriever_unavailable",
                payload={"degraded": True, "notes": list(material.notes)},
            )
            return None
        try:
            return await self.retriever.retrieve(
                query=item.text,
                snapshot=snapshot,
                index_id=material.index_id,
                stage=RetrievalStage.REPLY,
                correlation_id=item.correlation_id,
            )
        except Exception as exc:  # noqa: BLE001 - 检索异常必须与低分区分
            self._record(
                DiagnosticCategory.MATCH_ERROR,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                reason="retrieval_exception",
                payload={"error": type(exc).__name__},
            )
            return RetrievalResult(
                stage=RetrievalStage.REPLY,
                decision=MatchDecision.PROCESSING_ERROR,
                reason_code="retrieval_exception",
                correlation_id=item.correlation_id,
                index_id=material.index_id,
            )

    async def _compose(
        self,
        *,
        item: QueueItem,
        snapshot: StateSnapshot,
        conversation: Any,
        references: Sequence[Any],
        retrieval: RetrievalResult | None,
        material: MaterialAvailability,
    ) -> tuple[DialogueDraft, str | None]:
        if retrieval is None:
            # 资料不可用只是受控降级：仍按人物资料与当前心情自然对话，不算低匹配
            degraded = material.reason_code or "no_material"
            draft = await self._generate(
                item=item,
                snapshot=snapshot,
                conversation=conversation,
                references=references,
            )
            return draft, degraded
        low_match = decide_match(retrieval, stage=RetrievalStage.REPLY)
        if not low_match.usable:
            if low_match.decision is MatchDecision.PROCESSING_ERROR:
                self._record(
                    DiagnosticCategory.MATCH_ERROR,
                    correlation_id=item.correlation_id,
                    user_id=item.user_id,
                    conversation_id=item.conversation_id,
                    reason=low_match.reason_code,
                    payload=low_match.to_payload(),
                )
                degraded = self.fallbacks.draft_for(
                    "failure", mood=snapshot.primary_mood, reason_code=low_match.reason_code
                )
                return degraded, low_match.reason_code
            if low_match.decision in (
                MatchDecision.NO_MATERIAL,
                MatchDecision.EMPTY_CANDIDATES,
                MatchDecision.BELOW_THRESHOLD,
            ):
                self._record(
                    DiagnosticCategory.LOW_MATCH,
                    correlation_id=item.correlation_id,
                    user_id=item.user_id,
                    conversation_id=item.conversation_id,
                    turn_id=None,
                    reason=low_match.reason_code,
                    payload={
                        "stage": RetrievalStage.REPLY.value,
                        "input": item.text,
                        "primary_mood": snapshot.primary_mood.value,
                        "threshold": retrieval.threshold if retrieval else None,
                        "candidates": [
                            {
                                "unit_id": candidate.unit_id,
                                "group_key": candidate.group_key,
                                "score": candidate.score,
                                "specificity": candidate.conditions.get("specificity"),
                                "context_status": candidate.context_status,
                                "matched_field_keys": list(candidate.matched_field_keys),
                                "omitted_optional_fields": list(
                                    candidate.omitted_optional_fields
                                ),
                                "source_node_ids": list(candidate.source_node_ids),
                            }
                            for candidate in (retrieval.candidates if retrieval else ())
                        ],
                        "candidate_count": len(retrieval.candidates) if retrieval else 0,
                        "omissions": list(
                            (retrieval.details.get("evidence") or {}).get("omissions") or []
                        )
                        if retrieval
                        else [],
                        "index_version": retrieval.index_id if retrieval else None,
                        "model_version": retrieval.details.get("model_revision") if retrieval else None,
                        "config_version": self.config.version,
                        "decision": low_match.to_payload(),
                        "fallback_kind": low_match.fallback_kind,
                    },
                )
                if low_match.fallback_kind:
                    return (
                        self.fallbacks.draft_for(
                            low_match.fallback_kind,
                            mood=snapshot.primary_mood,
                            reason_code=low_match.reason_code,
                        ),
                        low_match.reason_code,
                    )

        draft = await self._generate(
            item=item,
            snapshot=snapshot,
            conversation=conversation,
            references=references,
        )
        return draft, None

    async def _generate(
        self,
        *,
        item: QueueItem,
        snapshot: StateSnapshot,
        conversation: Any,
        references: Sequence[Any],
    ) -> DialogueDraft:
        if self.model is None:
            return self.fallbacks.draft_for(
                "failure", mood=snapshot.primary_mood, reason_code="model_unavailable"
            )
        messages = build_generation_messages(
            message=item.text,
            history=conversation.as_messages(),
            snapshot=snapshot,
            constraints=self.policy.constraints,
            references=references,
        )
        budget = getattr(self.policy, "budget", None) or ModelBudget(self.config.max_reply_chars)
        result = await self.model.complete_text(
            ModelRequest(
                messages=messages,
                correlation_id=item.correlation_id,
            ),
            budget,
        )
        if not result.ok or not result.text.strip():
            self._record(
                DiagnosticCategory.DELIVERY_ERROR,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                reason=result.reason_code or "model_failure",
                payload={"model": result.model},
            )
            return self.fallbacks.draft_for(
                "failure", mood=snapshot.primary_mood, reason_code=result.reason_code or "model_failure"
            )
        return DialogueDraft(
            kind="normal",
            text=result.text.strip(),
            source_candidates=tuple(reference.unit_id for reference in references),
            correlation_id=item.correlation_id,
        )

    # ------------------------------------------------------------------ 发送

    async def _deliver(
        self,
        item: QueueItem,
        turn_id: str,
        draft: DialogueDraft,
        *,
        snapshot: StateSnapshot,
        kind: OutboundKind,
        material: MaterialAvailability | None = None,
        retrieval: RetrievalResult | None = None,
        degraded_reason: str | None = None,
    ) -> TurnOutcome:
        if self.transport is None:
            self._record(
                DiagnosticCategory.DELIVERY_ERROR,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                turn_id=turn_id,
                reason="transport_unavailable",
                payload={"kind": kind.value},
            )
            return self._finish_without_send(item, turn_id, "transport_unavailable", {})

        delivery_id = uuid.uuid4().hex
        event_time = item.event_time
        budget = self.deliveries.eligible(
            app_id=_app_id(item),
            source_msg_id=item.inbound_id,
            event_time=event_time,
            now=self._now(),
        )
        if not budget.allowed:
            self._record(
                DiagnosticCategory.DELIVERY_ERROR,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                turn_id=turn_id,
                reason=budget.reason_code or "budget_exhausted",
                payload={"deadline": to_utc_iso(budget.deadline)},
            )
            return self._finish_without_send(
                item, turn_id, budget.reason_code or "budget_exhausted", {}
            )

        msg_seq = sequence_for(kind)
        with self.database.write():
            self.deliveries.prepare(
                delivery_id=delivery_id,
                app_id=_app_id(item),
                user_key=_user_key(item),
                kind=kind,
                source_msg_id=item.inbound_id,
                content=draft.text,
                msg_seq=msg_seq,
                turn_id=turn_id,
                correlation_id=item.correlation_id,
            )
            self.deliveries.reserve(app_id=_app_id(item), source_msg_id=item.inbound_id)
            self.deliveries.mark_sending(delivery_id)

        try:
            result = await self.transport.send(
                content=draft.text,
                source_msg_id=item.inbound_id,
                msg_seq=msg_seq,
                user_key=_user_key(item),
                # 平台只认 ROBOT1.0_... 形式的消息 id；inbound_id 是内部 uuid
                platform_msg_id=item.message_id or None,
                correlation_id=item.correlation_id,
            )
        except Exception as exc:  # noqa: BLE001 - 发送异常一律记 uncertain
            with self.database.write():
                self.deliveries.settle(
                    delivery_id,
                    status=DeliveryStatus.UNCERTAIN,
                    error=type(exc).__name__,
                )
            self._record(
                DiagnosticCategory.DELIVERY_ERROR,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                turn_id=turn_id,
                reason="delivery_exception",
                payload={"error": type(exc).__name__},
            )
            return self._finalise(
                item,
                turn_id,
                delivery_id=delivery_id,
                status=DeliveryStatus.UNCERTAIN,
                text=draft.text,
                kind=kind,
                reason_code="delivery_exception",
            )

        with self.database.write():
            self.deliveries.settle(
                delivery_id,
                status=result.status,
                platform_message_id=result.platform_message_id,
                error=result.reason_code,
            )
        if result.status is not DeliveryStatus.CONFIRMED:
            self._record(
                DiagnosticCategory.DELIVERY_ERROR,
                correlation_id=item.correlation_id,
                user_id=item.user_id,
                turn_id=turn_id,
                reason=result.reason_code or result.status.value,
                payload={"status": result.status.value, "msg_seq": msg_seq},
            )
        else:
            self._after_confirmed(turn_id, delivery_id, kind)
        return self._finalise(
            item,
            turn_id,
            delivery_id=delivery_id,
            status=result.status,
            text=draft.text,
            kind=kind,
            reason_code=result.reason_code,
        )

    def _after_confirmed(self, turn_id: str, delivery_id: str, kind: OutboundKind) -> None:
        for hook in self.post_delivery_hooks:
            try:
                hook(turn_id, delivery_id, kind)
            except Exception as exc:  # noqa: BLE001 - 结算失败需可诊断且不重复发送
                self._record(
                    DiagnosticCategory.LOGGING_ERROR,
                    correlation_id=delivery_id,
                    turn_id=turn_id,
                    reason="post_delivery_hook_failed",
                    payload={"error": type(exc).__name__},
                )

    def _finalise(
        self,
        item: QueueItem,
        turn_id: str,
        *,
        delivery_id: str | None,
        status: DeliveryStatus,
        text: str,
        kind: OutboundKind,
        reason_code: str | None,
    ) -> TurnOutcome:
        with self.database.write():
            self.inbound.set_turn_status(turn_id, "completed")
            self.inbound.set_inbound_status(item.inbound_id, InboundStatus.COMPLETED)
        return TurnOutcome(
            ok=status is not DeliveryStatus.REJECTED,
            turn_id=turn_id,
            status=status.value,
            text=text,
            kind=kind.value,
            delivery_id=delivery_id,
            delivery_status=status.value,
            reason_code=reason_code,
        )

    def _finish_without_send(
        self,
        item: QueueItem,
        turn_id: str,
        reason_code: str,
        details: Mapping[str, Any],
    ) -> TurnOutcome:
        with self.database.write():
            self.inbound.set_turn_status(turn_id, "failed")
            self.inbound.set_inbound_status(item.inbound_id, InboundStatus.FAILED)
        return TurnOutcome(
            ok=False,
            turn_id=turn_id,
            status="failed",
            reason_code=reason_code,
            details=dict(details),
        )

    # ------------------------------------------------------------------ 诊断

    def _record(
        self,
        category: DiagnosticCategory,
        *,
        correlation_id: str,
        reason: str,
        user_id: str | None = None,
        conversation_id: str | None = None,
        turn_id: str | None = None,
        snapshot_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        if self.diagnostics is None:
            return
        try:
            self.diagnostics.record(
                DiagnosticRecord(
                    category=category,
                    correlation_id=correlation_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    snapshot_id=snapshot_id,
                    reason=reason,
                    payload=payload or {},
                    occurred_at=to_utc_iso(utc_now()),
                )
            )
        except Exception:  # noqa: BLE001 - 记录失败不得影响用户可见回复
            pass


    def _now(self) -> Any:
        """当前时间；注入时钟后可与事件时间一致地判定窗口。"""
        if self.clock is None:
            return None
        from sana_bot.timeutil import from_epoch

        return from_epoch(self.clock.wall())


def _drop_oldest_entries(conversation: Any, count: int) -> Any:
    """按完整轮次（用户+角色各一条）移除最早的上下文。"""
    from dataclasses import replace as _replace

    return _replace(conversation, entries=tuple(conversation.entries[count:]))


def _app_id(item: QueueItem) -> str:
    return item.app_id


def _user_key(item: QueueItem) -> str:
    return item.user_key or item.user_id


def mood_from_snapshot(snapshot: StateSnapshot) -> PrimaryMood:
    return snapshot.primary_mood
