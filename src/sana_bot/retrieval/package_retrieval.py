"""新格式检索：固定包 → 条件优先选卡 → 背景/示例余弦补充。

结果语义与旧语义格式分开：成功空候选仍可正常生成；编码或存储异常单独分类，
不伪装成低相关。阈值只来自与快照绑定的已批准校准报告。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sana_bot.enums import MatchDecision, RetrievalStage
from sana_bot.ports import RetrievalResult, RetrievedCandidate
from sana_bot.retrieval.embeddings import EmbeddingBackend
from sana_bot.retrieval.package_selector import (
    DECISION_CONTEXT_OVER_LIMIT,
    DECISION_PACKAGE_UNAVAILABLE,
    DECISION_PROCESSING_ERROR,
    DECISION_READY_WITH_REFERENCES,
    DECISION_RELATIONSHIP_MISMATCH,
    GENERATABLE_DECISIONS,
    PackageSelection,
    PackageSelector,
)
from sana_bot.retrieval.vector_store import StructuralVectorIndex, VectorStore
from sana_bot.state.models import StateSnapshot
from sana_bot.storage.index_repository import StructureRepository
from sana_bot.storage.package_repository import PackageReportRepository

__all__ = ["PackageRetriever", "build_query", "QUERY_RECENT_USER_MESSAGES"]


QUERY_RECENT_USER_MESSAGES = 3


def build_query(
    *,
    message: str,
    history: Sequence[Mapping[str, str]] = (),
    snapshot: StateSnapshot | None = None,
    recent_limit: int = QUERY_RECENT_USER_MESSAGES,
) -> str:
    """查询 = 本轮 + 最近三条用户输入 + 状态摘要；不夹带系统提示。"""
    parts = [message.strip()]
    user_texts = [
        str(item.get("content", "")).strip()
        for item in history
        if str(item.get("role")) == "user" and str(item.get("content", "")).strip()
    ]
    for text in user_texts[-recent_limit:]:
        if text and text != message.strip():
            parts.append(text)
    if snapshot is not None:
        state_bits = [snapshot.dominant_state] if snapshot.dominant_state else []
        state_bits.extend(snapshot.secondary_tags)
        if state_bits:
            parts.append("当前状态：" + "、".join(dict.fromkeys(state_bits)))
        parts.append(f"场景：{snapshot.scene.value}")
    return "\n".join(part for part in parts if part)


@dataclass
class PackageRetriever:
    """实现 :class:`sana_bot.ports.Retriever` 的新格式分支。"""

    structures: StructureRepository
    reports: PackageReportRepository
    store: VectorStore
    embeddings: EmbeddingBackend
    selector: PackageSelector | None = None
    count_tokens: Callable[[str], int] | None = None

    async def retrieve(
        self,
        *,
        query: str,
        snapshot: StateSnapshot,
        index_id: str,
        stage: RetrievalStage,
        correlation_id: str | None = None,
        medium: str = "text",
        history_tokens: int = 0,
    ) -> RetrievalResult:
        record = self.structures.snapshot(index_id)
        try:
            index = self.store.load_structural(record)
        except Exception as exc:  # noqa: BLE001 - 产物损坏必须与空检索区分
            return RetrievalResult(
                stage=stage,
                decision=MatchDecision.PROCESSING_ERROR,
                reason_code=getattr(exc, "reason_code", "index_artifact_missing"),
                index_id=index_id,
                correlation_id=correlation_id,
                details={"error": type(exc).__name__},
            )

        reports = self.reports.calibrations(index_id, stage=stage.value)
        matched = [item for item in reports if item.snapshot_hash == record.snapshot_hash]
        approved = [item for item in matched if item.approved_at]
        calib = approved[-1] if approved else (matched[-1] if matched else None)
        threshold = calib.threshold if calib is not None else None
        if threshold is None:
            return RetrievalResult(
                stage=stage,
                decision=MatchDecision.PROCESSING_ERROR,
                reason_code="package_calibration_missing",
                index_id=index_id,
                snapshot_hash=record.snapshot_hash,
                correlation_id=correlation_id,
                notes=("缺少与本快照绑定的已批准校准报告",),
            )

        if index.dimension != self.embeddings.spec.dimension:
            return RetrievalResult(
                stage=stage,
                decision=MatchDecision.PROCESSING_ERROR,
                reason_code="embedding_dimension_mismatch",
                index_id=index_id,
                snapshot_hash=record.snapshot_hash,
                correlation_id=correlation_id,
            )
        try:
            query_vector = self.embeddings.embed_query(query)
        except Exception as exc:  # noqa: BLE001 - 编码失败不是低匹配
            return RetrievalResult(
                stage=stage,
                decision=MatchDecision.PROCESSING_ERROR,
                reason_code=getattr(exc, "reason_code", "embedding_failed"),
                index_id=index_id,
                snapshot_hash=record.snapshot_hash,
                correlation_id=correlation_id,
                details={"error": type(exc).__name__},
            )

        if self.selector is not None:
            selector = self.selector
        else:
            selector = PackageSelector(
                threshold=threshold,
                budgets=dict(index.policy.get("budget") or {}),
                count_tokens=self.count_tokens,
            )
        selection = selector.select(
            index=index,
            snapshot=snapshot,
            query_vector=query_vector,
            medium=medium,
            required_relationship=str(index.policy.get("required_relationship", "romantic")),
            history_tokens=history_tokens,
            query_tokens=(self.count_tokens or (lambda text: len(text)))(query),
        )
        return self._to_result(
            selection,
            index=index,
            record_snapshot_hash=record.snapshot_hash,
            index_id=index_id,
            stage=stage,
            threshold=threshold,
            calibration_id=calib.id if calib is not None else None,
            report_hash=calib.compute_hash() if calib is not None else "",
            correlation_id=correlation_id,
        )

    # ------------------------------------------------------------------ 转换

    @staticmethod
    def _to_result(
        selection: PackageSelection,
        *,
        index: StructuralVectorIndex,
        record_snapshot_hash: str,
        index_id: str,
        stage: RetrievalStage,
        threshold: float | None,
        calibration_id: str | None,
        report_hash: str,
        correlation_id: str | None,
    ) -> RetrievalResult:
        candidates = tuple(
            _candidate(card, selection.scores.get(card.card_id, 0.0), record_snapshot_hash)
            for card in selection.supplements
        )
        if selection.decision == DECISION_PROCESSING_ERROR:
            decision = MatchDecision.PROCESSING_ERROR
        elif selection.decision == DECISION_CONTEXT_OVER_LIMIT:
            decision = MatchDecision.PROCESSING_ERROR
        elif selection.decision == DECISION_PACKAGE_UNAVAILABLE:
            decision = MatchDecision.NO_MATERIAL
        elif selection.decision == DECISION_RELATIONSHIP_MISMATCH:
            decision = MatchDecision.NO_MATERIAL
        elif selection.decision == DECISION_READY_WITH_REFERENCES:
            decision = MatchDecision.MATCHED
        elif selection.decision in GENERATABLE_DECISIONS:
            decision = MatchDecision.EMPTY_CANDIDATES
        else:  # pragma: no cover - 防御未知决策
            decision = MatchDecision.PROCESSING_ERROR
        return RetrievalResult(
            stage=stage,
            decision=decision,
            candidates=candidates,
            threshold=threshold,
            index_id=index_id,
            snapshot_hash=record_snapshot_hash,
            reason_code=selection.reason_code,
            correlation_id=correlation_id,
            notes=(
                (str((selection.ambiguity or {}).get("message")),)
                if selection.ambiguity
                else ()
            ),
            details={
                "format": "structural_v2",
                "package_decision": selection.decision,
                "package": selection.to_payload(),
                "selection": selection,
                "ambiguous": selection.ambiguity is not None,
                "ambiguity": dict(selection.ambiguity) if selection.ambiguity else None,
                "resident_ids": list(selection.residents),
                "rule_card_ids": [card.card_id for card in selection.rule_cards],
                "calibration_id": calibration_id,
                "report_hash": report_hash,
                "bundle_hash": index.bundle_hash,
                "model_revision": index.revision,
                "threshold": threshold,
            },
        )


def _candidate(card: Any, score: float, snapshot_hash: str) -> RetrievedCandidate:
    return RetrievedCandidate(
        unit_id=card.card_id,
        score=float(score),
        source_span_ids=(card.card_id,),
        topic=card.heading_path[-1] if card.heading_path else card.card_id,
        speaker_labels=tuple(turn.speaker for turn in card.speaker_turns),
        relationship=str(card.effective_conditions.get("relationship", "any")),
        scene=str(card.effective_conditions.get("scene", "any")),
        content_types=(card.type.value,),
        text=_render_card(card),
        source_node_ids=(),
        conditions=dict(card.effective_conditions),
        group_key=f"card::{card.card_id}",
        effective_conditions=dict(card.effective_conditions),
        snapshot_hash=snapshot_hash,
    )


def _render_card(card: Any) -> str:
    if card.speaker_turns:
        lines = []
        for turn in card.speaker_turns:
            label = "用户" if turn.speaker == "user" else "角色"
            lines.append(f"{label}：{turn.text}")
        return "\n".join(lines)
    return card.raw_text
