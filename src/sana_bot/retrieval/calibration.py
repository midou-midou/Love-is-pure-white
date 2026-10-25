"""切分与检索门槛的校准。

顺序固定：切分参数在确认样本上校准并固定 → 按固定参数重建快照 → 在该快照上校准
reply / monologue 门槛。相等视为命中；不使用未经校准的通用默认门槛。
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sana_bot.enums import RetrievalStage
from sana_bot.errors import ConfigError, NotReadyError
from sana_bot.ingest.models import canonical_json
from sana_bot.ingest.semantic_splitter import SplitterParameters, cosine_similarity

__all__ = [
    "SplitSample",
    "RetrievalSample",
    "load_samples",
    "sample_set_hash",
    "SplitCalibrationOutcome",
    "ThresholdOutcome",
    "calibrate_split_parameters",
    "calibrate_threshold",
    "SPLIT_CANDIDATES",
]


@dataclass(frozen=True)
class SplitSample:
    """相邻文本对与人工确认的语义关系。"""

    left: str
    right: str
    same_topic: bool
    note: str = ""


@dataclass(frozen=True)
class RetrievalSample:
    """一条检索门槛样本；positive 表示应命中。"""

    query: str
    stage: RetrievalStage
    expected_unit_ids: tuple[str, ...] = ()
    positive: bool = True
    scores: tuple[float, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class SplitCalibrationOutcome:
    parameters: SplitterParameters
    metrics: Mapping[str, Any] = field(default_factory=dict)
    sample_set_hash: str = ""


@dataclass(frozen=True)
class ThresholdOutcome:
    threshold: float
    metrics: Mapping[str, Any] = field(default_factory=dict)
    sample_set_hash: str = ""


#: 切分参数候选；不含「已校准默认值」的暗示
SPLIT_CANDIDATES: tuple[SplitterParameters, ...] = tuple(
    SplitterParameters(
        window_sentences=window,
        boundary_similarity_threshold=threshold,
        max_unit_tokens=384,
        min_unit_tokens=12,
        calibrated=True,
    )
    for window, threshold in itertools.product((1, 2, 3), (0.35, 0.45, 0.55, 0.65, 0.75))
)

_THRESHOLD_CANDIDATES: tuple[float, ...] = tuple(
    round(0.30 + 0.01 * step, 2) for step in range(0, 51)
)


def load_samples(path: Path) -> tuple[list[SplitSample], list[RetrievalSample]]:
    """读取 JSONL 样本；格式错误必须显式失败，不静默跳过。"""
    source = Path(path)
    if not source.exists():
        raise ConfigError(f"样本文件不存在：{source}")
    split: list[SplitSample] = []
    retrieval: list[RetrievalSample] = []
    for line_number, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"样本第 {line_number} 行不是合法 JSON：{exc}") from exc
        if not isinstance(payload, Mapping):
            raise ConfigError(f"样本第 {line_number} 行必须是对象")
        kind = str(payload.get("kind", "retrieval"))
        if kind == "split":
            left = payload.get("left")
            right = payload.get("right")
            if not isinstance(left, str) or not isinstance(right, str):
                raise ConfigError(f"样本第 {line_number} 行缺少 left/right")
            split.append(
                SplitSample(
                    left=left,
                    right=right,
                    same_topic=bool(payload.get("same_topic", True)),
                    note=str(payload.get("note", "")),
                )
            )
        elif kind == "retrieval":
            query = payload.get("query")
            if not isinstance(query, str) or not query:
                raise ConfigError(f"样本第 {line_number} 行缺少 query")
            stage = RetrievalStage(str(payload.get("stage", RetrievalStage.REPLY.value)))
            retrieval.append(
                RetrievalSample(
                    query=query,
                    stage=stage,
                    expected_unit_ids=tuple(payload.get("expected", ())),
                    positive=bool(payload.get("positive", True)),
                    scores=tuple(float(item) for item in payload.get("scores", ())),
                    note=str(payload.get("note", "")),
                )
            )
        else:
            raise ConfigError(f"样本第 {line_number} 行 kind 不受支持：{kind}")
    if not split and not retrieval:
        raise ConfigError("样本文件没有任何可用样本")
    return split, retrieval


def sample_set_hash(samples: Sequence[object]) -> str:
    payload: list[dict[str, Any]] = []
    for sample in samples:
        if isinstance(sample, SplitSample):
            payload.append(
                {"left": sample.left, "right": sample.right, "same_topic": sample.same_topic}
            )
        elif isinstance(sample, RetrievalSample):
            payload.append(
                {
                    "query": sample.query,
                    "stage": sample.stage.value,
                    "expected": list(sample.expected_unit_ids),
                    "positive": sample.positive,
                }
            )
        else:  # pragma: no cover - 防御性分支
            raise TypeError(f"未知样本类型：{type(sample)!r}")
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def calibrate_split_parameters(
    samples: Sequence[SplitSample],
    *,
    embed: Callable[[Sequence[str]], list[list[float]]],
    candidates: Sequence[SplitterParameters] = SPLIT_CANDIDATES,
) -> SplitCalibrationOutcome:
    """在确认样本上选择切分参数；样本不足时明确失败而不是给默认值。"""
    if not samples:
        raise NotReadyError(
            "切分校准至少需要一条已确认样本",
            reason_code="split_samples_insufficient",
        )
    windows = max(parameter.window_sentences for parameter in candidates)
    texts: list[str] = []
    for sample in samples:
        texts.append(sample.left)
        texts.append(sample.right)
    vectors = embed(texts)
    pair_vectors = [
        (vectors[index * 2], vectors[index * 2 + 1]) for index in range(len(samples))
    ]

    best: tuple[float, SplitterParameters, list[float]] | None = None
    for parameter in candidates:
        correct = 0
        similarities: list[float] = []
        for index, sample in enumerate(samples):
            left, right = pair_vectors[index]
            # 窗口参数影响边界判定的保守程度：窗口越大越容易判断为同话题
            similarity = cosine_similarity(left, right)
            similarities.append(similarity)
            adjusted = min(1.0, similarity * (1.0 + 0.05 * (parameter.window_sentences - 1)))
            predicted_same = adjusted >= parameter.boundary_similarity_threshold
            if predicted_same == sample.same_topic:
                correct += 1
        accuracy = correct / len(samples)
        if best is None or accuracy > best[0]:
            best = (accuracy, parameter, similarities)
    assert best is not None
    accuracy, parameter, similarities = best
    positives = [similarity for similarity, sample in zip(similarities, samples, strict=True) if sample.same_topic]
    negatives = [similarity for similarity, sample in zip(similarities, samples, strict=True) if not sample.same_topic]
    metrics = {
        "sample_count": len(samples),
        "accuracy": round(accuracy, 4),
        "window_sentences": parameter.window_sentences,
        "boundary_similarity_threshold": parameter.boundary_similarity_threshold,
        "same_topic_similarity_min": round(min(positives), 4) if positives else None,
        "different_topic_similarity_max": round(max(negatives), 4) if negatives else None,
        "candidate_count": len(candidates),
        "windows_evaluated": windows,
    }
    return SplitCalibrationOutcome(
        parameters=parameter,
        metrics=metrics,
        sample_set_hash=sample_set_hash(samples),
    )


def calibrate_threshold(
    samples: Sequence[RetrievalSample],
    *,
    stage: RetrievalStage,
    scored: Callable[[RetrievalSample], tuple[float, ...]] | None = None,
) -> ThresholdOutcome:
    """在给定快照的分数上选择门槛：相等视为命中，最大化准确率后取最低门槛。"""
    relevant = [sample for sample in samples if sample.stage is stage]
    if not relevant:
        raise NotReadyError(
            f"{stage.value} 阶段没有可用样本",
            reason_code="calibration_samples_missing",
        )
    # 真实索引优先：只要能现场计算就用真实分数，样本内记录的分数只作兜底说明，
    # 避免报告绑定在样本里写死的数字上而掩盖实际检索表现。
    resolved: list[tuple[RetrievalSample, tuple[float, ...]]] = []
    for sample in relevant:
        if scored is not None:
            values = tuple(scored(sample))
        else:
            values = tuple(sample.scores)
        if not values:
            raise NotReadyError(
                f"样本缺少分数且无法现场计算：{sample.query}",
                reason_code="calibration_scores_missing",
            )
        resolved.append((sample, values))

    best: tuple[float, float] | None = None
    confusion: dict[str, Any] = {}
    for threshold in _THRESHOLD_CANDIDATES:
        true_positive = false_positive = true_negative = false_negative = 0
        for sample, values in resolved:
            hit = max(values) >= threshold  # 相等视为命中
            if sample.positive and hit:
                true_positive += 1
            elif sample.positive and not hit:
                false_negative += 1
            elif not sample.positive and hit:
                false_positive += 1
            else:
                true_negative += 1
        total = len(resolved)
        accuracy = (true_positive + true_negative) / total
        if best is None or accuracy > best[0] + 1e-9:
            best = (accuracy, threshold)
            confusion = {
                "threshold": threshold,
                "accuracy": round(accuracy, 4),
                "true_positive": true_positive,
                "false_positive": false_positive,
                "true_negative": true_negative,
                "false_negative": false_negative,
                "sample_count": total,
            }
    assert best is not None
    with_expected = sum(1 for sample, _ in resolved if sample.expected_unit_ids)
    return ThresholdOutcome(
        threshold=best[1],
        metrics={
            **confusion,
            "score_metric": "cosine",
            "stage": stage.value,
            # 未指定 expected 的正样本按“全部候选中最高分”度量，属较弱证据
            "samples_with_expected": with_expected,
            "score_source": "live_index" if scored is not None else "sample_recorded",
        },
        sample_set_hash=sample_set_hash(relevant),
    )
