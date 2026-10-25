"""沉默独白：到期重读最新状态、一次概率抽取、统一输出规范与预算约束。

无候选、歧义、概率未命中或输出违规都保持安静并记录原因；独白不创建新机会、
不计好感度；发送结果不明时不自动重发。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sana_bot.config import RuntimeConfig
from sana_bot.diagnostics import DiagnosticCategory, DiagnosticRecord
from sana_bot.dialogue.history import HistoryService
from sana_bot.dialogue.match_decision import decide_match
from sana_bot.dialogue.output_policy import ConstrainedOutputPolicy
from sana_bot.dialogue.prompts import to_reference_units
from sana_bot.enums import (
    MonologueStatus,
    OpportunityStatus,
    PrimaryMood,
    RelationshipStatus,
    RetrievalStage,
    Scene,
)
from sana_bot.ports import (
    ModelBudget,
    ModelPort,
    ModelRequest,
    MonologueResult,
    RandomSource,
    Retriever,
    RetrievedCandidate,
)
from sana_bot.scheduling.dispatch import MonologueDispatcher
from sana_bot.storage.database import Database
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.storage.index_repository import StructureRepository
from sana_bot.storage.monologue_repository import MonologueRepository
from sana_bot.storage.state_repository import StateRepository
from sana_bot.timeutil import from_epoch, utc_now

__all__ = ["MonologueEvaluator", "MONOLOGUE_MARKER", "build_monologue_prompt"]

MONOLOGUE_MARKER = "【心理独白】"

SYSTEM_PROMPT = (
    "你是乾纱凪的内心独白生成器。只输出一句内心独白，第一人称，"
    "句末不要使用句号，不要描述用户的想法，不要提出新的要求。"
    "只依据给定的资料参考与当前心情，不要编造资料里没有的剧情。"
)


def build_monologue_prompt(
    *,
    reference_texts: Sequence[str],
    mood: PrimaryMood,
    relationship: RelationshipStatus,
    scene: Scene,
    previous_reply: str,
) -> tuple[dict[str, str], ...]:
    references = "\n".join(reference_texts) if reference_texts else "（无）"
    user = (
        "资料中的独白参考（仅供表达参考，不要照抄）：\n"
        f"{references}\n\n"
        f"上一轮我的回复：{previous_reply or '（无）'}\n"
        f"当前心情：{mood.value}；关系：{relationship.value}；场景：{scene.value}\n"
        "请给出一句符合上述语境的内心独白。"
    )
    return (
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    )


@dataclass
class MonologueEvaluator:
    """实现 :class:`sana_bot.ports.MonologueService`。"""

    database: Database
    config: RuntimeConfig
    monologues: MonologueRepository
    structures: StructureRepository
    inbound: InboundRepository
    snapshots: StateRepository
    history: HistoryService
    dispatcher: MonologueDispatcher
    policy: ConstrainedOutputPolicy
    retriever: Retriever | None = None
    model: ModelPort | None = None
    diagnostics: Any | None = None
    relationship_provider: Callable[[str], tuple[RelationshipStatus, float]] | None = None
    clock: Any | None = None
    rng: RandomSource | None = None
    app_id: str = ""
    _last_result: MonologueResult | None = field(default=None, init=False)

    # ------------------------------------------------------------------ 时间与随机

    def _now(self):
        if self.clock is None:
            return utc_now()
        return from_epoch(self.clock.wall())

    def _random(self) -> float:
        if self.rng is None:
            import random

            return random.random()
        return self.rng.random()

    # ------------------------------------------------------------------ 入口

    async def evaluate(
        self,
        *,
        opportunity_id: str,
        clock: Any | None = None,
        rng: RandomSource | None = None,
        correlation_id: str | None = None,
    ) -> MonologueResult:
        if clock is not None:
            self.clock = clock
        if rng is not None:
            self.rng = rng
        opportunity = self.monologues.get(opportunity_id)
        if opportunity is None:
            return self._done(MonologueResult(outcome="skipped", skip_reason="opportunity_missing"))
        now = self._now()
        if opportunity.due_at > now:
            return self._done(
                MonologueResult(outcome="skipped", skip_reason="not_due"),
                opportunity_id=opportunity_id,
            )
        if opportunity.status is not OpportunityStatus.PENDING:
            return self._done(
                MonologueResult(outcome="cancelled", skip_reason=opportunity.status.value),
                opportunity_id=opportunity_id,
            )
        with self.database.write():
            self.monologues.transition(opportunity_id, OpportunityStatus.EVALUATING)

        status, threshold = self._monologue_status()
        if status is not MonologueStatus.ENABLED:
            return self._skip(opportunity_id, status.value)

        conversation = self.inbound.conversation_row(opportunity.conversation_id)
        current_generation = int(conversation["generation"]) if conversation else 0
        if current_generation != opportunity.generation:
            return self._skip(opportunity_id, "generation_advanced")

        turn = self.inbound.turn_row(opportunity.turn_id)
        if turn is None:
            return self._skip(opportunity_id, "turn_missing")
        user_id = self.inbound.inbound_row(turn.inbound_id)
        user_key = self.inbound.user_openid(user_id.user_id) if user_id else ""
        user_id_value = user_id.user_id if user_id else ""
        relationship, _affection = (
            self.relationship_provider(user_id_value)
            if self.relationship_provider is not None
            else (RelationshipStatus.NON_ROMANTIC, 0.0)
        )
        snapshot = self.snapshots.snapshot_for_turn(opportunity.turn_id)
        if snapshot is None:
            return self._skip(opportunity_id, "snapshot_missing")
        latest = self.snapshots.latest_snapshot(opportunity.conversation_id)
        if latest is not None and latest.id == snapshot.id and latest.created_at > snapshot.created_at:
            snapshot = latest
        scene = Scene(conversation["scene"]) if conversation else Scene.UNKNOWN

        retrieval = await self._retrieve(
            snapshot=snapshot,
            index_id=opportunity_conversation_index(self, opportunity),
            query=turn.input_text,
            correlation_id=correlation_id,
        )
        if retrieval is None:
            return self._skip(opportunity_id, "material_unavailable")
        decision = decide_match(retrieval, stage=RetrievalStage.MONOLOGUE)
        if not decision.usable:
            self._record_low_match(
                opportunity, snapshot, retrieval, decision.reason_code, user_id_value
            )
            return self._skip(opportunity_id, decision.reason_code)
        if retrieval.details.get("ambiguous"):
            return self._skip(opportunity_id, "ambiguous_candidates")

        draw = self._random()
        probability = opportunity.probability
        if draw >= probability:
            with self.database.write():
                self.monologues.transition(
                    opportunity_id, OpportunityStatus.SKIPPED, random_draw=draw, skip_reason="probability_miss"
                )
            return self._done(
                MonologueResult(
                    outcome="skipped", skip_reason="probability_miss", correlation_id=correlation_id
                ),
                opportunity_id=opportunity_id,
            )

        candidates = retrieval.candidates
        text = await self._compose(
            candidates=candidates,
            snapshot=snapshot,
            relationship=relationship,
            scene=scene,
            previous_reply=self._previous_reply(opportunity.turn_id),
            correlation_id=correlation_id,
        )
        if text is None:
            return self._skip(opportunity_id, "generation_failed")

        reviewed = await self.policy.review(
            draft=_draft(text, correlation_id),
            snapshot=snapshot,
            stage=RetrievalStage.MONOLOGUE,
            correlation_id=correlation_id,
        )
        if not reviewed.approved:
            self._record(
                DiagnosticCategory.OUTPUT_BLOCKED,
                correlation_id=correlation_id or opportunity.id,
                user_id=user_id_value,
                conversation_id=opportunity.conversation_id,
                turn_id=opportunity.turn_id,
                reason=reviewed.reason_code or "output_blocked",
                payload={"stage": "monologue", "violations": list(reviewed.violations)},
            )
            return self._skip(opportunity_id, "output_blocked")
        approved_text = reviewed.text or text

        with self.database.write():
            self.monologues.transition(
                opportunity_id,
                OpportunityStatus.EVALUATING,
                random_draw=draw,
                candidate_id=candidates[0].unit_id if candidates else None,
                latest_snapshot=snapshot.id,
            )
        outcome = await self.dispatcher.send_monologue(
            opportunity_id=opportunity_id,
            content=MONOLOGUE_MARKER + approved_text,
            user_key=user_key or user_id_value,
            app_id=self.app_id,
            correlation_id=correlation_id,
        )
        if outcome.sent:
            return self._done(
                MonologueResult(
                    outcome="sent",
                    text=MONOLOGUE_MARKER + approved_text,
                    delivery_id=outcome.delivery_id,
                    correlation_id=correlation_id,
                ),
                opportunity_id=opportunity_id,
            )
        return self._done(
            MonologueResult(
                outcome="failed" if outcome.status else "skipped",
                skip_reason=outcome.reason_code,
                delivery_id=outcome.delivery_id,
                correlation_id=correlation_id,
            ),
            opportunity_id=opportunity_id,
        )

    # ------------------------------------------------------------------ 内部

    def _monologue_status(self) -> tuple[MonologueStatus, float | None]:
        snapshot = self.structures.active_snapshot()
        if snapshot is None:
            return MonologueStatus.DISABLED_NO_MATERIAL, None
        if snapshot.monologue_status is not MonologueStatus.ENABLED:
            return snapshot.monologue_status, None
        calibration = self.structures.calibration(snapshot.id, RetrievalStage.MONOLOGUE)
        if calibration is None or calibration.threshold is None:
            return MonologueStatus.DISABLED_UNCALIBRATED, None
        return MonologueStatus.ENABLED, calibration.threshold

    async def _retrieve(self, *, snapshot, index_id, query, correlation_id):
        if self.retriever is None:
            return None
        record = self.structures.active_snapshot()
        if record is None:
            return None
        try:
            return await self.retriever.retrieve(
                query=query or snapshot.dominant_state or "心情",
                snapshot=snapshot,
                index_id=record.id,
                stage=RetrievalStage.MONOLOGUE,
                correlation_id=correlation_id,
            )
        except Exception as exc:  # noqa: BLE001 - 检索异常单独记录
            self._record(
                DiagnosticCategory.MATCH_ERROR,
                correlation_id=correlation_id or "monologue",
                conversation_id=snapshot.conversation_id,
                turn_id=snapshot.turn_id,
                reason="retrieval_exception",
                payload={"error": type(exc).__name__},
            )
            return None

    async def _compose(
        self,
        *,
        candidates: Sequence[RetrievedCandidate],
        snapshot,
        relationship: RelationshipStatus,
        scene: Scene,
        previous_reply: str,
        correlation_id: str | None,
    ) -> str | None:
        if self.model is None:
            return None
        references = to_reference_units(candidates)
        messages = build_monologue_prompt(
            reference_texts=[reference.text for reference in references],
            mood=snapshot.primary_mood,
            relationship=relationship,
            scene=scene,
            previous_reply=previous_reply,
        )
        budget = getattr(self.policy, "budget", None) or ModelBudget(30.0)
        result = await self.model.complete_text(
            ModelRequest(messages=messages, correlation_id=correlation_id), budget
        )
        if not result.ok or not result.text.strip():
            return None
        return result.text.strip()

    def _previous_reply(self, turn_id: str) -> str:
        delivery = self.dispatcher.deliveries.delivery(f"{turn_id}")
        del delivery
        row = self.database.query_one(
            """
            SELECT content FROM outbound_delivery
            WHERE turn_id = ? AND status = 'confirmed' ORDER BY msg_seq ASC LIMIT 1
            """,
            (turn_id,),
        )
        return row["content"] if row else ""

    def _skip(self, opportunity_id: str, reason: str) -> MonologueResult:
        with self.database.write():
            self.monologues.transition(
                opportunity_id, OpportunityStatus.SKIPPED, skip_reason=reason
            )
        return self._done(
            MonologueResult(outcome="skipped", skip_reason=reason), opportunity_id=opportunity_id
        )

    def _done(self, result: MonologueResult, *, opportunity_id: str | None = None) -> MonologueResult:
        self._last_result = result
        if result.outcome == "skipped" and result.skip_reason:
            self._record(
                DiagnosticCategory.LOW_MATCH,
                correlation_id=result.correlation_id or opportunity_id or "monologue",
                conversation_id=None,
                turn_id=None,
                reason=result.skip_reason,
                payload={"stage": "monologue", "decision": result.skip_reason, "candidates": []},
            )
        return result

    def _record_low_match(self, opportunity, snapshot, retrieval, reason: str, user_id: str) -> None:
        self._record(
            DiagnosticCategory.LOW_MATCH,
            correlation_id=opportunity.id,
            user_id=user_id,
            conversation_id=opportunity.conversation_id,
            turn_id=opportunity.turn_id,
            reason=reason,
            payload={
                "stage": "monologue",
                "input": "",
                "primary_mood": snapshot.primary_mood.value,
                "candidates": [
                    {"unit_id": candidate.unit_id, "score": candidate.score}
                    for candidate in retrieval.candidates
                ],
                "candidate_count": len(retrieval.candidates),
                "threshold": retrieval.threshold,
                "index_version": retrieval.index_id,
                "decision": reason,
            },
        )

    def _record(self, category, *, correlation_id, reason, **kwargs) -> None:
        if self.diagnostics is None:
            return
        try:
            self.diagnostics.record(
                DiagnosticRecord(
                    category=category,
                    correlation_id=correlation_id,
                    reason=reason,
                    **kwargs,
                )
            )
        except Exception:  # noqa: BLE001 - 记录失败不影响安静策略
            pass


def _draft(text: str, correlation_id: str | None):
    from sana_bot.ports import DialogueDraft

    return DialogueDraft(kind="monologue", text=text, correlation_id=correlation_id)


def opportunity_conversation_index(evaluator: MonologueEvaluator, opportunity) -> str | None:
    snapshot = evaluator.structures.active_snapshot()
    return snapshot.id if snapshot else None


def new_delivery_id() -> str:
    return uuid.uuid4().hex
