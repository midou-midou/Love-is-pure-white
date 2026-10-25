"""角色切分：把对话正文按说话者拆成基础片段。

规则：标题、字段与人物规则说明不算独白；只有实际对话正文里的无名字内容才归为
乾纱凪的心理独白；其他角色具名台词不得归给纱凪。长片段拆子片段时保留父片段。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sana_bot.enums import ContentType, SpanKind
from sana_bot.ingest.models import RuleNode, SourceSpan
from sana_bot.ingest.rule_parser import FIELD_ALIASES

__all__ = [
    "SPEAKER_ALIASES",
    "SELF_SPEAKER",
    "RoleSplitResult",
    "split_roles",
    "is_heading_node",
    "LONG_SPAN_CHARS",
]

SELF_SPEAKER = "sana"

#: 已确认别名 → 规范说话者；未列出的标签保留原样，不归给纱凪
SPEAKER_ALIASES: Mapping[str, str] = {
    "纱凪": SELF_SPEAKER,
    "乾纱凪": SELF_SPEAKER,
    "sana": SELF_SPEAKER,
    "纱凪酱": SELF_SPEAKER,
    "我": SELF_SPEAKER,
    "你": "user",
    "用户": "user",
    "user": "user",
    "理央": "rio",
    "乾理央": "rio",
    "美羽": "mio",
    "天羽美羽": "mio",
    "学姐": "mio",
    "爱理": "airi",
}

_LABEL_RE = re.compile(r"^\s*([\u4e00-\u9fffA-Za-z_]{1,8})\s*[：:]\s*(.*)$")
#: 超过该长度的片段拆成子片段，缓解超长发言（原文不丢失）
LONG_SPAN_CHARS = 200
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?…])")
#: 未确认标签的最大长度：中文人名通常很短，更长的多为子字段名
MAX_UNCONFIRMED_LABEL_CHARS = 4

#: 这些内容类型即使出现无名文本也不作为独白（规则、禁区、输出规范等）
NON_MONOLOGUE_TYPES = frozenset(
    {
        ContentType.GENERAL_RULE,
        ContentType.OUTPUT_RULE,
        ContentType.NEGATIVE,
        ContentType.ACTION,
    }
)

#: 明确属于示例/对话正文的内容类型
DIALOGUE_TYPES = frozenset({ContentType.EXAMPLE, ContentType.DIALOGUE, ContentType.MONOLOGUE})

#: 说明型标签，永远不是说话者
ANNOTATION_LABELS = frozenset({"说明", "核心原则", "备注", "示例", "例子", "注"})


@dataclass(frozen=True)
class RoleSplitResult:
    spans: tuple[SourceSpan, ...]
    issues: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    coverage: Mapping[str, Any] = field(default_factory=dict)
    speaker_counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _Line:
    text: str
    start: int
    end: int
    label: str | None


def _iter_lines(node: RuleNode) -> list[_Line]:
    lines: list[_Line] = []
    offset = 0
    for raw_line in node.raw_text.split("\n"):
        start = node.char_start + offset
        end = start + len(raw_line)
        match = _LABEL_RE.match(raw_line)
        label = None
        if match and raw_line.strip():
            candidate = match.group(1)
            if _is_label_candidate(candidate):
                label = candidate
        lines.append(_Line(text=raw_line, start=start, end=end, label=label))
        offset += len(raw_line) + 1
    return lines


def _is_label_candidate(candidate: str) -> bool:
    """字段别名与说明型标签不是说话者；过长的标签多为子字段名。"""
    if candidate in FIELD_ALIASES or candidate in ANNOTATION_LABELS:
        return False
    return len(candidate) <= MAX_UNCONFIRMED_LABEL_CHARS


def _accepted_labels(
    lines: Sequence[_Line],
    node: RuleNode,
    alias_map: Mapping[str, str],
) -> set[str]:
    """确定该节点中真正算说话者的标签。

    已确认别名直接接受；其他标签必须有多处对话证据（同标签重复、多个不同标签，
    或处于示例/对话正文），避免把子字段名误判为说话者。
    """
    counts: dict[str, int] = {}
    for line in lines:
        if line.label is not None:
            counts[line.label] = counts.get(line.label, 0) + 1
    distinct = len(counts)
    dialogue_context = node.content_type in DIALOGUE_TYPES
    accepted = {
        label
        for label, count in counts.items()
        if label in alias_map or count >= 2 or distinct >= 2 or dialogue_context
    }
    if node.field_type is not None and not dialogue_context and len(accepted) < 2:
        # 单一字段段落里的冒号是字段名而不是说话者
        accepted &= set(alias_map)
    return accepted


def _split_long(node: RuleNode, content: str, start: int, parent_id: str, base_index: int) -> list[SourceSpan]:
    """超长发言按完整句拆分子片段，保留父片段与原文顺序。"""
    pieces: list[SourceSpan] = []
    cursor = 0
    for piece in _SENTENCE_SPLIT.split(content):
        if not piece:
            continue
        piece_start = start + cursor
        cursor += len(piece)
        pieces.append(
            SourceSpan(
                id=uuid.uuid4().hex,
                node_id=node.id,
                kind=SpanKind.DIALOGUE,
                raw_text=piece,
                source_start=piece_start,
                source_end=piece_start + len(piece),
                speaker=node.speaker,
                parent_span_id=parent_id,
                segment_index=base_index + len(pieces),
            )
        )
    return pieces


def split_roles(
    nodes: Sequence[RuleNode],
    *,
    aliases: Mapping[str, str] | None = None,
) -> RoleSplitResult:
    """把所有带类型节点切成基础片段；每个片段只有一个说话者与内容类型。"""
    alias_map = dict(SPEAKER_ALIASES)
    if aliases:
        alias_map.update(aliases)

    spans: list[SourceSpan] = []
    issues: list[dict[str, Any]] = []
    speaker_counts: dict[str, int] = {}
    covered_by_node: dict[str, int] = {}
    # 对话区域：同一作用域内紧邻前一片段是对话时，无名正文才可能是独白
    region_dialogue: dict[str, bool] = {}

    for node in nodes:
        if is_heading_node(node):
            continue
        lines = [line for line in _iter_lines(node) if line.text.strip()]
        if not lines:
            continue
        accepted = _accepted_labels(lines, node, alias_map)
        labelled = [line for line in lines if line.label in accepted]
        in_dialogue_region = node.content_type in DIALOGUE_TYPES or region_dialogue.get(node.scope, False)
        allow_monologue = node.content_type not in NON_MONOLOGUE_TYPES or in_dialogue_region
        node_spans: list[SourceSpan] = []
        for line in lines:
            if line.label in accepted:
                speaker = alias_map.get(line.label, line.label)
                text = line.text.strip()
                leading = len(line.text) - len(line.text.lstrip())
                start = line.start + leading
                content_start = start + len(line.label) + 1
                span = SourceSpan(
                    id=uuid.uuid4().hex,
                    node_id=node.id,
                    kind=SpanKind.DIALOGUE,
                    raw_text=text,
                    source_start=start,
                    source_end=start + len(text),
                    speaker=speaker,
                    speaker_label=line.label,
                    segment_index=len(node_spans),
                )
                node_spans.append(span)
                if len(text) > LONG_SPAN_CHARS:
                    content = text[len(line.label) + 1 :]
                    node_spans.extend(
                        _split_long(node, content, content_start, span.id, len(node_spans))
                    )
                speaker_counts[speaker] = speaker_counts.get(speaker, 0) + 1
                continue
            text = line.text.strip()
            stripped = text
            is_marker_line = stripped in FIELD_ALIASES or stripped in ANNOTATION_LABELS
            is_unlabelled_dialogue = (
                allow_monologue
                and not is_marker_line
                and not node.list_level
                and _looks_like_dialogue(stripped)
                and (bool(labelled) or in_dialogue_region)
            )
            if is_unlabelled_dialogue:
                start = line.start + (len(line.text) - len(line.text.lstrip()))
                node_spans.append(
                    SourceSpan(
                        id=uuid.uuid4().hex,
                        node_id=node.id,
                        kind=SpanKind.MONOLOGUE,
                        raw_text=stripped,
                        source_start=start,
                        source_end=start + len(stripped),
                        speaker=SELF_SPEAKER,
                        segment_index=len(node_spans),
                    )
                )
                speaker_counts[SELF_SPEAKER] = speaker_counts.get(SELF_SPEAKER, 0) + 1
                continue
            if not stripped:
                continue
            node_spans.append(
                SourceSpan(
                    id=uuid.uuid4().hex,
                    node_id=node.id,
                    kind=SpanKind.STATEMENT,
                    raw_text=stripped,
                    source_start=line.start + (len(line.text) - len(line.text.lstrip())),
                    source_end=line.start + len(line.text.rstrip()),
                    speaker=None,
                    segment_index=len(node_spans),
                )
            )

        if not node_spans:
            continue
        node_spans = _link(node_spans)
        if node_spans:
            region_dialogue[node.scope] = node_spans[-1].kind in (
                SpanKind.DIALOGUE,
                SpanKind.MONOLOGUE,
            )
        spans.extend(node_spans)
        covered_by_node[node.id] = sum(span.source_end - span.source_start for span in node_spans)

    if not spans:
        issues.append(
            {
                "reason_code": "no_source_spans",
                "message": "未产生任何基础片段，语义切分无法继续",
            }
        )
    coverage = _coverage(nodes, covered_by_node)
    return RoleSplitResult(
        spans=tuple(spans),
        issues=tuple(issues),
        coverage=coverage,
        speaker_counts=speaker_counts,
    )


def is_heading_node(node: RuleNode) -> bool:
    """标题节点只提供作用域，不产生片段（标题不是独白也不是台词）。"""
    return bool(node.heading_path) and node.raw_text.strip() == node.heading_path[-1]


#: 规则/约束式开头；这些内容即使无名也不是独白
RULE_PREFIXES = (
    "不会",
    "不要",
    "不得",
    "不需要",
    "禁止",
    "必须",
    "应当",
    "不要忘",
    "杜绝",
    "说明：",
    "核心原则：",
    "备注：",
)


def _looks_like_dialogue(text: str) -> bool:
    """无名内容只有在像对话正文（引号、动作括号或对话标点）时才作为独白。"""
    if not text:
        return False
    if text.startswith(RULE_PREFIXES):
        return False
    if text.endswith("："):
        return False
    if len(text) > 120:
        return False
    # 整体被引号包住的是被引用的台词，不是心理独白
    if text[0] in "“\"「『" and text[-1] in "”\"」』":
        return False
    has_dialogue_marker = bool(set(text) & set("“”「」『』（）()……？！"))
    ends_with_sentence = text.endswith(("。", "！", "？", "…"))
    return has_dialogue_marker or ends_with_sentence


def _link(spans: Sequence[SourceSpan]) -> list[SourceSpan]:
    """补齐前后链接，便于回溯语境。"""
    linked: list[SourceSpan] = []
    for index, span in enumerate(spans):
        previous_id = spans[index - 1].id if index > 0 else None
        next_id = spans[index + 1].id if index + 1 < len(spans) else None
        linked.append(
            SourceSpan(
                id=span.id,
                node_id=span.node_id,
                kind=span.kind,
                raw_text=span.raw_text,
                source_start=span.source_start,
                source_end=span.source_end,
                speaker=span.speaker,
                previous_id=previous_id,
                next_id=next_id,
                parent_span_id=span.parent_span_id,
                segment_index=span.segment_index,
                speaker_label=span.speaker_label,
            )
        )
    return linked


def _coverage(nodes: Sequence[RuleNode], covered_by_node: Mapping[str, int]) -> dict[str, Any]:
    """原文覆盖统计：以非标题节点的原文长度与片段区间为基准，避免重复关联掩盖漏字。"""
    from sana_bot.ingest.role_splitter import is_heading_node

    body_nodes = [node for node in nodes if not is_heading_node(node)]
    total = sum(len(node.raw_text) for node in body_nodes)
    covered = sum(covered_by_node.get(node.id, 0) for node in body_nodes)
    return {
        "node_char_total": total,
        "heading_char_total": sum(len(node.raw_text) for node in nodes) - total,
        "span_char_total": covered,
        "ratio": round(covered / total, 4) if total else 0.0,
    }
