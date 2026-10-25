"""新格式检索校准：用与运行完全相同的条件优先选择器实际评分。

校准只调整补充材料的相似度门槛：常驻、状态与场景规则的选择不由向量分数决定。
样本必须先冻结；阈值与 report_hash 绑定 snapshot / policy / encoder / 样本哈希，
不反向改变封存快照。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sana_bot.enums import PrimaryMood, RelationshipStatus, Scene
from sana_bot.errors import ConfigError
from sana_bot.ingest.package_models import canonical_hash
from sana_bot.retrieval.package_selector import (
    DECISION_CONTEXT_OVER_LIMIT,
    DECISION_PACKAGE_UNAVAILABLE,
    DECISION_PROCESSING_ERROR,
    DECISION_RELATIONSHIP_MISMATCH,
    PackageSelection,
    PackageSelector,
)
from sana_bot.retrieval.vector_store import StructuralVectorIndex
from sana_bot.state.models import StateSnapshot, empty_snapshot

__all__ = [
    "CalibrationSample",
    "load_calibration_samples",
    "calibrate_reply_threshold",
    "CalibrationOutcome",
    "THRESHOLD_GRID",
]

#: 可扫描的相似度门槛：只影响背景与示例的补充与否
THRESHOLD_GRID: tuple[float, ...] = (
    0.10,
    0.15,
    0.20,
    0.25,
    0.30,
    0.35,
    0.40,
    0.45,
    0.50,
    0.55,
    0.60,
)

MIN_POSITIVE_SAMPLES = 36
MIN_NEGATIVE_SAMPLES = 12


@dataclass(frozen=True)
class CalibrationSample:
    sample_id: str
    query: str
    history: tuple[str, ...]
    relationship: RelationshipStatus
    scene: Scene
    medium: str
    primary_mood: PrimaryMood
    secondary_tags: tuple[str, ...]
    previous_state: str
    expected_card_ids: tuple[str, ...]
    excluded_card_ids: tuple[str, ...]
    tags: tuple[str, ...] = ()
    kind: str = "positive"

    def snapshot(self) -> StateSnapshot:
        return empty_snapshot(
            snapshot_id=f"sample-{self.sample_id}",
            turn_id=self.sample_id,
            conversation_id=f"conv-{self.sample_id}",
            mood=self.primary_mood,
            tags=self.secondary_tags,
            dominant_state=self.previous_state,
            scene=self.scene,
            relationship=self.relationship,
        )

    def query_text(self) -> str:
        parts = [self.query, *self.history[-3:]]
        if self.previous_state:
            parts.append(f"当前状态：{self.previous_state}")
        parts.append(f"场景：{self.scene.value}")
        return "\n".join(part for part in parts if part)

    def to_payload(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "query": self.query,
            "history": list(self.history),
            "relationship": self.relationship.value,
            "scene": self.scene.value,
            "medium": self.medium,
            "primary_mood": self.primary_mood.value,
            "secondary_tags": list(self.secondary_tags),
            "previous_state": self.previous_state,
            "expected_card_ids": list(self.expected_card_ids),
            "excluded_card_ids": list(self.excluded_card_ids),
            "tags": list(self.tags),
            "kind": self.kind,
        }


def load_calibration_samples(path: Path) -> tuple[CalibrationSample, ...]:
    """读取冻结样本（JSONL）；任何缺字段或未知取值都明确失败。"""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"校准样本不存在：{path}", reason_code="package_file_missing")
    samples: list[CalibrationSample] = []
    seen: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"样本第 {number} 行不是合法 JSON：{exc}",
                reason_code="package_metadata_invalid",
                details={"line": number},
            ) from exc
        if not isinstance(payload, Mapping):
            raise ConfigError(
                f"样本第 {number} 行必须是对象", reason_code="package_metadata_invalid"
            )
        sample_id = str(payload.get("sample_id") or "").strip()
        if not sample_id:
            raise ConfigError(
                f"样本第 {number} 行缺少 sample_id", reason_code="package_metadata_invalid"
            )
        if sample_id in seen:
            raise ConfigError(
                f"样本 sample_id 重复：{sample_id}", reason_code="package_metadata_invalid"
            )
        seen.add(sample_id)
        query = str(payload.get("query") or "").strip()
        if not query:
            raise ConfigError(
                f"样本 {sample_id} 缺少 query", reason_code="package_metadata_invalid"
            )
        try:
            relationship = RelationshipStatus(str(payload.get("relationship", "romantic")))
            scene = Scene(str(payload.get("scene", "unknown")))
            mood = PrimaryMood(str(payload.get("primary_mood", "neutral")))
        except ValueError as exc:
            raise ConfigError(
                f"样本 {sample_id} 条件取值非法：{exc}", reason_code="package_metadata_invalid"
            ) from exc
        samples.append(
            CalibrationSample(
                sample_id=sample_id,
                query=query,
                history=tuple(str(item) for item in payload.get("history", ())),
                relationship=relationship,
                scene=scene,
                medium=str(payload.get("medium", "text")),
                primary_mood=mood,
                secondary_tags=tuple(str(item) for item in payload.get("secondary_tags", ())),
                previous_state=str(payload.get("previous_state", "")),
                expected_card_ids=tuple(
                    str(item) for item in payload.get("expected_card_ids", ())
                ),
                excluded_card_ids=tuple(
                    str(item) for item in payload.get("excluded_card_ids", ())
                ),
                tags=tuple(str(item) for item in payload.get("tags", ())),
                kind=str(payload.get("kind", "positive")),
            )
        )
    if not samples:
        raise ConfigError("校准样本为空", reason_code="package_metadata_invalid")
    return tuple(samples)


def samples_hash(samples: Sequence[CalibrationSample]) -> str:
    return canonical_hash([sample.to_payload() for sample in samples])


@dataclass(frozen=True)
class CalibrationOutcome:
    threshold: float | None
    sample_set_hash: str
    sample_counts: Mapping[str, Any]
    rank_metrics: Mapping[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "sample_set_hash": self.sample_set_hash,
            "sample_counts": dict(self.sample_counts),
            "rank_metrics": dict(self.rank_metrics),
        }


def calibrate_reply_threshold(
    *,
    index: StructuralVectorIndex,
    samples: Sequence[CalibrationSample],
    embed_query,
    count_tokens=None,
) -> CalibrationOutcome:
    """在全部候选上实际评分；不按 expected 反向造分，不只看期望卡。"""
    if not samples:
        raise ConfigError("校准样本为空", reason_code="package_metadata_invalid")
    positives = [item for item in samples if item.kind != "negative"]
    negatives = [item for item in samples if item.kind == "negative"]
    if len(positives) < MIN_POSITIVE_SAMPLES:
        raise ConfigError(
            f"正例样本不足：{len(positives)} < {MIN_POSITIVE_SAMPLES}",
            reason_code="package_calibration_missing",
        )
    if len(negatives) < MIN_NEGATIVE_SAMPLES:
        raise ConfigError(
            f"负例样本不足：{len(negatives)} < {MIN_NEGATIVE_SAMPLES}",
            reason_code="package_calibration_missing",
        )

    budgets = dict(index.policy.get("budget") or {})
    query_vectors: list[list[float]] = []
    selections_by_threshold: dict[float, list[PackageSelection]] = {
        threshold: [] for threshold in THRESHOLD_GRID
    }
    for sample in samples:
        vector = embed_query(sample.query_text())
        query_vectors.append(vector)
        for threshold in THRESHOLD_GRID:
            selector = PackageSelector(
                threshold=threshold, budgets=budgets, count_tokens=count_tokens
            )
            selections_by_threshold[threshold].append(
                selector.select(
                    index=index,
                    snapshot=sample.snapshot(),
                    query_vector=vector,
                    medium=sample.medium,
                    required_relationship="romantic",
                )
            )

    metrics_by_threshold: dict[str, Any] = {}
    best_threshold: float | None = None
    best_key: tuple[float, float, float] | None = None
    for threshold in THRESHOLD_GRID:
        selections = selections_by_threshold[threshold]
        condition_violations = 0
        adopted_excluded = 0
        expected_total = 0
        expected_hit = 0
        negative_violations = 0
        for sample, selection in zip(samples, selections, strict=True):
            if selection.decision in (
                DECISION_PROCESSING_ERROR,
                DECISION_PACKAGE_UNAVAILABLE,
                DECISION_RELATIONSHIP_MISMATCH,
                DECISION_CONTEXT_OVER_LIMIT,
            ):
                continue
            adopted = {
                card.card_id for card in (*selection.rule_cards, *selection.supplements)
            }
            for card_id in sample.excluded_card_ids:
                if card_id in adopted:
                    adopted_excluded += 1
                    if sample.kind == "negative":
                        negative_violations += 1
            if sample.kind == "negative":
                if adopted & set(sample.excluded_card_ids):
                    condition_violations += 1
                continue
            wanted = set(sample.expected_card_ids)
            expected_total += len(wanted)
            expected_hit += len(wanted & adopted)
        recall = expected_hit / expected_total if expected_total else 0.0
        metrics_by_threshold[f"{threshold:.2f}"] = {
            "recall": round(recall, 4),
            "expected_total": expected_total,
            "expected_hit": expected_hit,
            "adopted_excluded": adopted_excluded,
            "negative_violations": negative_violations,
        }
        # 先保证不适用的卡采用数为 0，再最大化召回，最后偏好较高门槛
        key = (-float(adopted_excluded), recall, threshold)
        if best_key is None or key > best_key:
            best_key = key
            best_threshold = threshold

    assert best_threshold is not None
    best = metrics_by_threshold[f"{best_threshold:.2f}"]
    rank_metrics = {
        "thresholds": metrics_by_threshold,
        "selected_threshold": best_threshold,
        "recall": best["recall"],
        "adopted_excluded": best["adopted_excluded"],
        "negative_violations": best["negative_violations"],
        "ranking_basis": "condition_first_then_cosine",
        "candidate_card_count": len(index.card_ids),
    }
    sample_counts = {
        "positive": len(positives),
        "negative": len(negatives),
        "total": len(samples),
    }
    return CalibrationOutcome(
        threshold=best_threshold,
        sample_set_hash=samples_hash(samples),
        sample_counts=sample_counts,
        rank_metrics=rank_metrics,
    )


def report_hash_for(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
