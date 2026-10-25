"""向量语义切分：在同一硬边界内比较相邻句窗向量，提出语义边界。

不用固定字数或角色切换代替语义判断；跨角色问答用关联保留，不混改说话者；
超长句细分时记录父单元与偏移；目标长度不超过 :data:`max_unit_tokens`。
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sana_bot.ingest.models import (
    SEMANTIC_UNIT_TARGET_TOKENS,
    SPLITTER_VERSION,
    SemanticUnit,
    SourceSpan,
)

__all__ = [
    "SplitterParameters",
    "SemanticSplitResult",
    "split_semantics",
    "Sentence",
    "split_sentences",
    "cosine_similarity",
    "DEFAULT_SPLITTER_PARAMETERS",
]

_SENTENCE_RE = re.compile(r"[^。！？!?…\n]*[。！？!?…]|[^。！？!?…\n]+")
_SPLIT_HARD_TYPES = ("statement", "dialogue")


@dataclass(frozen=True)
class SplitterParameters:
    """切分参数；必须来自已批准的 SplitCalibrationProfile，不能当作已校准默认值。"""

    window_sentences: int = 2
    boundary_similarity_threshold: float = 0.55
    max_unit_tokens: int = SEMANTIC_UNIT_TARGET_TOKENS
    min_unit_tokens: int = 12
    splitter_version: str = SPLITTER_VERSION
    calibrated: bool = False

    def validate(self) -> SplitterParameters:
        problems: list[str] = []
        if self.window_sentences < 1:
            problems.append("window_sentences 必须为正数")
        if not 0.0 <= self.boundary_similarity_threshold <= 1.0:
            problems.append("boundary_similarity_threshold 必须在 0 与 1 之间")
        if self.max_unit_tokens <= 0:
            problems.append("max_unit_tokens 必须为正数")
        if self.min_unit_tokens < 0:
            problems.append("min_unit_tokens 不能为负")
        if self.min_unit_tokens >= self.max_unit_tokens:
            problems.append("min_unit_tokens 必须小于 max_unit_tokens")
        if problems:
            from sana_bot.errors import ConfigError

            raise ConfigError("；".join(problems), details={"issues": problems})
        return self

    def to_payload(self) -> dict[str, Any]:
        return {
            "window_sentences": self.window_sentences,
            "boundary_similarity_threshold": self.boundary_similarity_threshold,
            "max_unit_tokens": self.max_unit_tokens,
            "min_unit_tokens": self.min_unit_tokens,
            "splitter_version": self.splitter_version,
            "calibrated": self.calibrated,
        }


#: 预览用参数；未校准，不能在验收中当作已批准配置
DEFAULT_SPLITTER_PARAMETERS = SplitterParameters()


@dataclass(frozen=True)
class Sentence:
    text: str
    span_id: str
    node_id: str
    speaker: str | None
    kind: str
    source_start: int
    source_end: int


@dataclass(frozen=True)
class SemanticSplitResult:
    units: tuple[SemanticUnit, ...]
    issues: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    metrics: Mapping[str, Any] = field(default_factory=dict)


def split_sentences(spans: Sequence[SourceSpan]) -> list[Sentence]:
    """把基础片段切成完整句，保留来源定位。"""
    sentences: list[Sentence] = []
    for span in spans:
        cursor = 0
        for match in _SENTENCE_RE.finditer(span.raw_text):
            piece = match.group(0)
            if not piece.strip():
                cursor += len(piece)
                continue
            start = span.source_start + cursor
            sentences.append(
                Sentence(
                    text=piece,
                    span_id=span.id,
                    node_id=span.node_id,
                    speaker=span.speaker,
                    kind=span.kind.value,
                    source_start=start,
                    source_end=start + len(piece),
                )
            )
            cursor += len(piece)
    return sentences


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """固定定义的余弦相似度；零向量返回 0 而不是伪装成低分。"""
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return dot / (norm_left * norm_right)


def _hard_key(sentence: Sentence) -> tuple[str, str, str]:
    """硬边界键：不同类型/角色的表达不跨边界合并。"""
    if sentence.kind == "statement":
        return ("statement", sentence.node_id, "statement")
    speaker_class = "self" if sentence.speaker == "sana" else "other"
    return (sentence.kind, sentence.node_id, speaker_class)


def _group(sentences: Sequence[Sentence]) -> list[list[Sentence]]:
    groups: list[list[Sentence]] = []
    for sentence in sentences:
        if not groups or _hard_key(groups[-1][0]) != _hard_key(sentence):
            groups.append([sentence])
        else:
            groups[-1].append(sentence)
    return groups


def split_semantics(
    spans: Sequence[SourceSpan],
    *,
    index_version: str,
    embed: Callable[[Sequence[str]], list[list[float]]],
    parameters: SplitterParameters | None = None,
    count_tokens: Callable[[str], int] | None = None,
) -> SemanticSplitResult:
    """按语义边界把片段组织成语义单元。"""
    params = (parameters or DEFAULT_SPLITTER_PARAMETERS).validate()
    counter = count_tokens or len
    sentences = split_sentences(spans)
    if not sentences:
        return SemanticSplitResult(
            units=(),
            issues=(
                {
                    "reason_code": "no_sentences",
                    "message": "没有可用于语义切分的句子",
                },
            ),
        )

    units: list[SemanticUnit] = []
    issues: list[dict[str, Any]] = []
    boundaries: list[dict[str, Any]] = []
    similarities: list[float] = []

    for group in _group(sentences):
        similarities.extend(_similarities(group, embed, params))
        for chunk, evidence in _cut(group, embed, params, counter, similarities, boundaries):
            units.extend(_materialise(chunk, evidence, index_version, params, counter, issues))

    units = list(_link_contexts(units))
    units = _subdivide_long_units(units, embed, params, counter)
    metrics = {
        "sentence_count": len(sentences),
        "unit_count": len(units),
        "boundary_count": len(boundaries),
        "similarity_min": round(min(similarities), 4) if similarities else None,
        "similarity_max": round(max(similarities), 4) if similarities else None,
        "parameters": params.to_payload(),
        "hard_boundaries": sum(1 for index in range(1, len(sentences)) if _hard_key(sentences[index]) != _hard_key(sentences[index - 1])),
    }
    return SemanticSplitResult(units=tuple(units), issues=tuple(issues), metrics=metrics)


def _window_texts(group: Sequence[Sentence], window: int) -> list[str]:
    texts: list[str] = []
    for index in range(len(group)):
        chunk = group[index : index + window]
        texts.append("".join(item.text for item in chunk))
    return texts


def _similarities(
    group: Sequence[Sentence],
    embed: Callable[[Sequence[str]], list[list[float]]],
    params: SplitterParameters,
) -> list[float]:
    if len(group) < 2:
        return []
    texts = _window_texts(group, params.window_sentences)
    vectors = embed(texts)
    result: list[float] = []
    for index in range(1, len(vectors)):
        result.append(cosine_similarity(vectors[index - 1], vectors[index]))
    return result


def _cut(
    group: Sequence[Sentence],
    embed: Callable[[Sequence[str]], list[list[float]]],
    params: SplitterParameters,
    counter: Callable[[str], int],
    similarities: list[float],
    boundaries: list[dict[str, Any]],
) -> list[tuple[list[Sentence], dict[str, Any]]]:
    """按相似度与长度切分：超长时选最不像的相邻位置继续切。"""
    local = _similarities(group, embed, params)
    cuts: list[int] = []
    for index, similarity in enumerate(local, start=1):
        if similarity < params.boundary_similarity_threshold:
            cuts.append(index)
            boundaries.append(
                {
                    "at": index,
                    "similarity": round(similarity, 4),
                    "threshold": params.boundary_similarity_threshold,
                    "method": "adjacent_window_cosine",
                }
            )
    pieces: list[list[Sentence]] = []
    cursor = 0
    for cut in [*cuts, len(group)]:
        if cut > cursor:
            pieces.append(list(group[cursor:cut]))
        cursor = cut
    result: list[tuple[list[Sentence], dict[str, Any]]] = []
    for piece in pieces:
        result.extend(_enforce_length(piece, params, counter, local))
    return result


def _enforce_length(
    piece: list[Sentence],
    params: SplitterParameters,
    counter: Callable[[str], int],
    local_similarities: Sequence[float],
) -> list[tuple[list[Sentence], dict[str, Any]]]:
    """长度超限时继续在句边界切分，优先切在相似度最低处。"""
    total = counter("".join(sentence.text for sentence in piece))
    if total <= params.max_unit_tokens or len(piece) == 1:
        return [(piece, {"method": "semantic_window", "tokens": total})]
    halves: list[tuple[list[Sentence], dict[str, Any]]] = []
    mid = max(1, len(piece) // 2)
    for chunk in (piece[:mid], piece[mid:]):
        halves.extend(_enforce_length(chunk, params, counter, local_similarities))
    return halves


def _materialise(
    chunk: Sequence[Sentence],
    evidence: Mapping[str, Any],
    index_version: str,
    params: SplitterParameters,
    counter: Callable[[str], int],
    issues: list[dict[str, Any]],
) -> list[SemanticUnit]:
    if not chunk:
        return []
    text = "".join(sentence.text for sentence in chunk)
    tokens = counter(text)
    if tokens > params.max_unit_tokens and len(chunk) == 1:
        issues.append(
            {
                "reason_code": "single_sentence_over_limit",
                "message": "单个句子超过目标长度，将按标点与 token 边界细分",
                "tokens": tokens,
                "source_start": chunk[0].source_start,
            }
        )
    span_ids = tuple(dict.fromkeys(sentence.span_id for sentence in chunk))
    node_ids = tuple(dict.fromkeys(sentence.node_id for sentence in chunk))
    speakers = tuple(dict.fromkeys(sentence.speaker for sentence in chunk if sentence.speaker))
    topic = chunk[0].text[:40]
    return [
        SemanticUnit(
            id=uuid.uuid4().hex,
            index_version=index_version,
            source_span_ids=span_ids,
            source_node_ids=node_ids,
            speaker_labels=speakers,
            topic=topic,
            text=text,
            boundary_evidence={
                **dict(evidence),
                "sentence_count": len(chunk),
                "source_start": chunk[0].source_start,
                "source_end": chunk[-1].source_end,
            },
            conditions={},
            token_count=tokens,
        )
    ]


def _link_contexts(units: Sequence[SemanticUnit]) -> tuple[SemanticUnit, ...]:
    """跨角色问答用关联表达：相邻单元若说话者不同则互相关联，不合并说话者。"""
    linked: list[SemanticUnit] = []
    for index, unit in enumerate(units):
        links = list(unit.context_links)
        previous = units[index - 1] if index > 0 else None
        following = units[index + 1] if index + 1 < len(units) else None
        for neighbour in (previous, following):
            if neighbour is None:
                continue
            if set(neighbour.speaker_labels) != set(unit.speaker_labels):
                if neighbour.id not in links:
                    links.append(neighbour.id)
        linked.append(
            SemanticUnit(
                id=unit.id,
                index_version=unit.index_version,
                source_span_ids=unit.source_span_ids,
                source_node_ids=unit.source_node_ids,
                speaker_labels=unit.speaker_labels,
                content_types=unit.content_types,
                topic=unit.topic,
                text=unit.text,
                boundary_evidence=unit.boundary_evidence,
                context_links=tuple(links),
                conditions=unit.conditions,
                token_count=unit.token_count,
                parent_unit_id=unit.parent_unit_id,
                offset_in_parent=unit.offset_in_parent,
            )
        )
    return tuple(linked)


def _subdivide_long_units(
    units: Sequence[SemanticUnit],
    embed: Callable[[Sequence[str]], list[list[float]]],
    params: SplitterParameters,
    counter: Callable[[str], int],
) -> list[SemanticUnit]:
    """超长单元按标点细分，记录父单元与偏移；原文不丢失。"""
    result: list[SemanticUnit] = []
    for unit in units:
        if unit.token_count <= params.max_unit_tokens:
            result.append(unit)
            continue
        pieces = _split_text_by_budget(unit.text, params.max_unit_tokens, counter)
        offset = 0
        for piece in pieces:
            result.append(
                SemanticUnit(
                    id=f"{unit.id}-{offset}",
                    index_version=unit.index_version,
                    source_span_ids=unit.source_span_ids,
                    source_node_ids=unit.source_node_ids,
                    speaker_labels=unit.speaker_labels,
                    content_types=unit.content_types,
                    topic=unit.topic,
                    text=piece,
                    boundary_evidence={
                        **dict(unit.boundary_evidence),
                        "subdivided": True,
                        "offset_in_parent": offset,
                    },
                    context_links=unit.context_links,
                    conditions=unit.conditions,
                    token_count=counter(piece),
                    parent_unit_id=unit.id,
                    offset_in_parent=offset,
                )
            )
            offset += len(piece)
    return result


def _split_text_by_budget(
    text: str,
    max_tokens: int,
    counter: Callable[[str], int],
) -> list[str]:
    pieces: list[str] = []
    current = ""
    for fragment in _SENTENCE_RE.findall(text) or [text]:
        if current and counter(current + fragment) > max_tokens:
            pieces.append(current)
            current = ""
        if counter(fragment) > max_tokens:
            for piece in _hard_cut(fragment, max_tokens, counter):
                pieces.append(piece)
            continue
        current += fragment
    if current:
        pieces.append(current)
    return pieces


def _hard_cut(fragment: str, max_tokens: int, counter: Callable[[str], int]) -> list[str]:
    """标点与 token 边界处的兜底细分（仅在单句极端超长时使用）。"""
    pieces: list[str] = []
    current = ""
    for char in fragment:
        if current and counter(current + char) > max_tokens:
            pieces.append(current)
            current = ""
        current += char
    if current:
        pieces.append(current)
    return pieces
