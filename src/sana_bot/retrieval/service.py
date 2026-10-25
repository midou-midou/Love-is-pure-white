"""检索服务：统一余弦分数、来源返回与歧义说明。

顺序固定：读取本轮固定索引 → 硬过滤 → 余弦打分 → 门槛判断。
等于门槛算命中；异常分数单独分类，不伪装成低分。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sana_bot.enums import MatchDecision, RetrievalStage
from sana_bot.errors import ConfigError, NotReadyError
from sana_bot.ingest.models import SemanticUnit
from sana_bot.ports import RetrievalResult, RetrievedCandidate
from sana_bot.retrieval.embeddings import EmbeddingBackend
from sana_bot.retrieval.filters import MAX_CANDIDATES, eligible_units, to_candidates
from sana_bot.retrieval.vector_store import VectorIndex, VectorStore, cosine_scores
from sana_bot.state.models import StateSnapshot
from sana_bot.storage.index_repository import StructureRepository

__all__ = ["VectorRetriever", "AmbiguityNote"]


@dataclass(frozen=True)
class AmbiguityNote:
    """无法确定唯一更具体规则时给出的说明。"""

    code: str
    message: str
    unit_ids: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "unit_ids": list(self.unit_ids)}


@dataclass
class VectorRetriever:
    """实现 :class:`sana_bot.ports.Retriever`。"""

    structures: StructureRepository
    embeddings: EmbeddingBackend
    store: VectorStore
    max_candidates: int = MAX_CANDIDATES
    _cache: dict[str, VectorIndex] = field(default_factory=dict, init=False)

    async def retrieve(
        self,
        *,
        query: str,
        snapshot: StateSnapshot,
        index_id: str,
        stage: RetrievalStage,
        correlation_id: str | None = None,
    ) -> RetrievalResult:
        record = self.structures.snapshot(index_id)
        calibration = self.structures.calibration(index_id, stage)
        index = self._index(record)
        if index.dimension != self.embeddings.spec.dimension:
            return RetrievalResult(
                stage=stage,
                decision=MatchDecision.PROCESSING_ERROR,
                reason_code="embedding_dimension_mismatch",
                index_id=index_id,
                snapshot_hash=index.snapshot_hash,
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
                snapshot_hash=index.snapshot_hash,
                correlation_id=correlation_id,
                details={"error": type(exc).__name__},
            )
        eligible = eligible_units(index.units, snapshot=snapshot)
        if not eligible:
            return RetrievalResult(
                stage=stage,
                decision=MatchDecision.EMPTY_CANDIDATES,
                threshold=calibration.threshold if calibration else None,
                index_id=index_id,
                snapshot_hash=index.snapshot_hash,
                correlation_id=correlation_id,
                notes=("硬过滤后没有合格候选",),
            )
        scores = cosine_scores(query_vector, index.matrix)
        if not all(_finite(scores[position]) for position, _unit in enumerate(index.units)):
            return RetrievalResult(
                stage=stage,
                decision=MatchDecision.PROCESSING_ERROR,
                reason_code="score_not_finite",
                index_id=index_id,
                snapshot_hash=index.snapshot_hash,
                correlation_id=correlation_id,
            )
        position_of = {unit.id: position for position, unit in enumerate(index.units)}
        scored = [(unit, float(scores[position_of[unit.id]])) for unit in eligible]
        candidates = to_candidates(scored, limit=self.max_candidates)
        threshold = calibration.threshold if calibration else None
        notes, ambiguity = self._ambiguity_notes(candidates)
        if threshold is None:
            return RetrievalResult(
                stage=stage,
                decision=MatchDecision.PROCESSING_ERROR,
                candidates=candidates,
                threshold=None,
                index_id=index_id,
                snapshot_hash=index.snapshot_hash,
                reason_code="threshold_missing",
                correlation_id=correlation_id,
                notes=("该阶段尚未校准门槛",),
            )
        best = max(candidate.score for candidate in candidates)
        decision = MatchDecision.MATCHED if best >= threshold else MatchDecision.BELOW_THRESHOLD
        return RetrievalResult(
            stage=stage,
            decision=decision,
            candidates=candidates,
            threshold=threshold,
            index_id=index_id,
            snapshot_hash=index.snapshot_hash,
            correlation_id=correlation_id,
            notes=notes,
            details={
                "ambiguous": ambiguity is not None,
                "ambiguity": ambiguity.to_payload() if ambiguity else None,
                "eligible_count": len(eligible),
                "model_revision": index.revision,
            },
        )

    def _index(self, record) -> VectorIndex:
        cached = self._cache.get(record.id)
        if cached is not None:
            return cached
        index = self.store.load(record)
        self._cache = {record.id: index}
        return index

    def _ambiguity_notes(
        self, candidates: Sequence[RetrievedCandidate]
    ) -> tuple[tuple[str, ...], AmbiguityNote | None]:
        if len(candidates) < 2:
            return (), None
        top, second = candidates[0], candidates[1]
        if abs(top.score - second.score) > 1e-6:
            return (), None
        top_specificity = int(top.conditions.get("specificity", 0))
        second_specificity = int(second.conditions.get("specificity", 0))
        if top_specificity != second_specificity:
            return (), None
        if _guard_signature(top) == _guard_signature(second):
            # 同守卫条件的多个候选是同一规则的并列表达，不构成需要人工裁决的歧义
            return (), None
        note = AmbiguityNote(
            code="equal_score_different_rules",
            message="同等分数下存在不同更具体规则，改用角色化自由回复并记录歧义",
            unit_ids=(top.unit_id, second.unit_id),
        )
        return (note.message,), note


def _guard_signature(candidate: RetrievedCandidate) -> tuple[object, ...]:
    conditions = candidate.conditions or {}
    return (
        candidate.relationship,
        candidate.scene,
        tuple(candidate.primary_moods),
        tuple(candidate.secondary_tags),
    )


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def describe_candidates(candidates: Sequence[RetrievedCandidate]) -> list[dict[str, Any]]:
    return [
        {
            "unit_id": candidate.unit_id,
            "score": round(candidate.score, 4),
            "specificity": candidate.conditions.get("specificity"),
            "source_node_ids": list(candidate.source_node_ids),
            "types": list(candidate.content_types),
        }
        for candidate in candidates
    ]


def validate_stage(stage: str) -> RetrievalStage:
    try:
        return RetrievalStage(stage)
    except ValueError as exc:
        raise ConfigError(f"未知检索阶段：{stage}") from exc


def ensure_units_present(units: Sequence[SemanticUnit]) -> None:
    if not units:
        raise NotReadyError("索引中没有语义单元", reason_code="no_semantic_units")
