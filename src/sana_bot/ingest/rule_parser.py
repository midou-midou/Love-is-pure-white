"""结构解析：标题作用域 → 列表 → 冒号字段。

顺序不可颠倒：先得到带类型节点，再角色切分，最后语义切分。
一段可有多个字段，各自保留原文字符区间；父作用域不得跨同级标题。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sana_bot.enums import ContentType, FieldType, Provenance
from sana_bot.ingest.docx_reader import RawBlock
from sana_bot.ingest.models import PARSER_VERSION, RuleNode, compute_structure_hash

__all__ = [
    "FieldMarker",
    "build_nodes",
    "FIELD_ALIASES",
    "CHAPTER_CONTENT_TYPES",
    "SUBSECTION_CONTENT_TYPES",
    "RuleParseResult",
    "annotate_conditions",
    "register_aliases",
    "aliases_registered",
]

#: 冒号字段白名单；别名必须映射到九类之一
FIELD_ALIASES: dict[str, FieldType] = {
    "触发条件": FieldType.TRIGGER,
    "触发": FieldType.TRIGGER,
    "升级条件": FieldType.ESCALATION,
    "升级": FieldType.ESCALATION,
    "整体表现": FieldType.OVERALL,
    "表现": FieldType.OVERALL,
    "常态": FieldType.OVERALL,
    "肢体细节": FieldType.ACTION,
    "动作": FieldType.ACTION,
    "神态": FieldType.ACTION,
    "肢体": FieldType.ACTION,
    "行为表现": FieldType.BEHAVIOR,
    "行为": FieldType.BEHAVIOR,
    "维持": FieldType.BEHAVIOR,
    "说话风格": FieldType.SPEECH,
    "台词": FieldType.SPEECH,
    "语言": FieldType.SPEECH,
    # 状态组里的「典型反应台词」是表达类字段，不是新的字段枚举
    "典型反应台词": FieldType.SPEECH,
    "反应台词": FieldType.SPEECH,
    # 「维持 / 消退」同时描述维持与转换；解析时拆成两个连续字段
    "维持 / 消退": FieldType.MAINTENANCE,
    "维持/消退": FieldType.MAINTENANCE,
    "维持、消退": FieldType.MAINTENANCE,
    "消退逻辑": FieldType.RECOVERY,
    "恢复": FieldType.RECOVERY,
    "消退": FieldType.RECOVERY,
    "属性": FieldType.ATTRIBUTE,
    "喜欢": FieldType.ATTRIBUTE,
    "讨厌": FieldType.ATTRIBUTE,
    "擅长": FieldType.ATTRIBUTE,
    "不擅长": FieldType.ATTRIBUTE,
    "称呼": FieldType.ATTRIBUTE,
    "示例": FieldType.EXAMPLE,
    "例子": FieldType.EXAMPLE,
    "禁区": FieldType.NEGATIVE,
    "禁止": FieldType.NEGATIVE,
    "不会": FieldType.NEGATIVE,
}

#: 章节 → 内容类型；章节序号来自可见中文编号
CHAPTER_CONTENT_TYPES: dict[str, ContentType] = {
    "一": ContentType.PROFILE,
    "二": ContentType.GENERAL_RULE,
    "三": ContentType.PROFILE,
    "四": ContentType.STATE,
    "五": ContentType.STATE,
    "六": ContentType.GENERAL_RULE,
    "七": ContentType.NEGATIVE,
    "八": ContentType.OUTPUT_RULE,
}

#: 未编号章节标题 → 内容类型
NAMED_SECTION_CONTENT_TYPES: dict[str, ContentType] = {
    "补充约束": ContentType.GENERAL_RULE,
}

#: 子标题覆盖（按标题文字包含关系匹配，长者在先）
SUBSECTION_CONTENT_TYPES: tuple[tuple[str, ContentType], ...] = (
    ("符合乾纱凪人设的主动示例", ContentType.EXAMPLE),
    ("主动行为的边界", ContentType.NEGATIVE),
    ("强度切换原则", ContentType.GENERAL_RULE),
    ("可以主动做的行为", ContentType.GENERAL_RULE),
    ("外貌细节", ContentType.PROFILE),
    ("背景故事", ContentType.PROFILE),
    ("称呼习惯", ContentType.PROFILE),
    ("居住", ContentType.PROFILE),
    ("喜好", ContentType.PROFILE),
    ("吃醋", ContentType.STATE),
    ("状态", ContentType.STATE),
)

#: 标注型文字（说明框、核心原则）不当作可执行规则
ANNOTATION_PREFIXES = ("说明：", "核心原则：", "备注：")

#: 「对所有关系状态恒定生效」的用语：命中即不收紧关系，避免把通用性格锁死。
UNIVERSAL_MARKERS: tuple[str, ...] = ("恒定", "通用", "未恋爱也", "无论关系")

_STATE_MOOD_MAP: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("平稳常态", ("neutral",), ("平稳常态",)),
    ("害羞局促", ("happy",), ("害羞局促",)),
    ("傲娇炸毛", ("angry",), ("傲娇炸毛", "not_genuine_anger")),
    ("轻度吃醋", ("angry",), ("轻度吃醋",)),
    ("中度吃醋", ("angry",), ("中度吃醋",)),
    ("重度吃醋", ("sad",), ("重度吃醋",)),
    ("委屈脆弱", ("sad",), ("委屈脆弱",)),
    ("依赖软萌", ("happy",), ("依赖软萌",)),
)


@dataclass(frozen=True)
class FieldMarker:
    alias: str
    field_type: FieldType
    start: int
    end: int


@dataclass(frozen=True)
class RuleParseResult:
    nodes: tuple[RuleNode, ...]
    heading_tree: tuple[Mapping[str, Any], ...]
    issues: tuple[Mapping[str, Any], ...]
    structure_hash: str
    parser_version: str = PARSER_VERSION
    coverage: Mapping[str, Any] = field(default_factory=dict)


def _marker_regex() -> re.Pattern[str]:
    aliases = sorted(FIELD_ALIASES, key=len, reverse=True)
    joined = "|".join(re.escape(alias) for alias in aliases)
    # 标记出现在行首或空白之后，避免正文里偶然出现的同名词
    return re.compile(rf"(?:(?<=^)|(?<=\s))({joined})\s*[：:]")


_MARKER_RE = _marker_regex()


@contextmanager
def aliases_registered(aliases: Mapping[str, Any]) -> Iterator[dict[str, FieldType]]:
    """在上下文内登记 profile 别名，退出时恢复全局表。

    解析必须可重入：同一进程先后解析不同 profile 时，别名不得互相污染，
    否则同一份文档会因解析顺序不同得到不同字段边界。
    """
    global _MARKER_RE
    saved_aliases = dict(FIELD_ALIASES)
    saved_regex = _MARKER_RE
    try:
        yield register_aliases(aliases)
    finally:
        FIELD_ALIASES.clear()
        FIELD_ALIASES.update(saved_aliases)
        _MARKER_RE = saved_regex


def register_aliases(aliases: Mapping[str, Any]) -> dict[str, FieldType]:
    """把已批准 profile 里的别名并入白名单并重建标记正则。

    profile 只允许补充别名映射，不允许改写既有结构边界；返回值便于调用方
    记录本次实际生效的别名集合。
    """
    applied: dict[str, FieldType] = {}
    for alias, field_type in aliases.items():
        text = str(alias).strip()
        if not text:
            continue
        try:
            resolved = field_type if isinstance(field_type, FieldType) else FieldType(str(field_type))
        except ValueError as exc:
            raise ValueError(f"未知字段类型：{field_type!r}") from exc
        FIELD_ALIASES[text] = resolved
        applied[text] = resolved
    if applied:
        global _MARKER_RE
        _MARKER_RE = _marker_regex()
    return applied


def find_markers(line: str) -> list[FieldMarker]:
    """返回该行所有字段标记（同一行可有多个字段）。"""
    return [
        FieldMarker(
            alias=match.group(1),
            field_type=FIELD_ALIASES[match.group(1)],
            start=match.start(),
            end=match.end(),
        )
        for match in _MARKER_RE.finditer(line)
    ]


def detect_standalone_marker(line: str) -> FieldMarker | None:
    """整行仅有别名（例如单独一行「示例」）时作为字段起点。"""
    stripped = line.strip()
    if stripped in FIELD_ALIASES:
        offset = line.index(stripped)
        return FieldMarker(
            alias=stripped,
            field_type=FIELD_ALIASES[stripped],
            start=offset,
            end=offset + len(stripped),
        )
    return None


@dataclass(frozen=True)
class _Segment:
    text: str
    char_start: int
    char_end: int
    field_type: FieldType | None = None
    alias: str | None = None


#: 「维持 / 消退」这类复合标记的后半段关键词：出现即视为恢复/升级说明
RECOVERY_CLAUSE_KEYWORDS = ("切换", "恢复", "升级", "消退", "回归", "解除")


def _split_composite(
    raw: str, *, field_type: FieldType, alias: str, start: int
) -> tuple[_Segment, ...]:
    """把维持/消退复合字段拆成两个连续段，offset 仍可直接拼接还原。

    只在同一行内按分号切分，且后半段必须含有恢复类关键词；否则保持单段，
    避免把普通说明拆成两条规则。
    """
    if field_type is not FieldType.MAINTENANCE:
        return ()
    for position, char in enumerate(raw):
        if char != "；":
            continue
        tail = raw[position + 1 :].strip()
        if not tail or not any(word in tail for word in RECOVERY_CLAUSE_KEYWORDS):
            continue
        head_text = raw[: position + 1].strip()
        tail_text = raw[position + 1 :].strip()
        head_start = start + (len(raw) - len(raw.lstrip()))
        return (
            _Segment(
                text=head_text,
                char_start=head_start,
                char_end=head_start + len(head_text),
                field_type=FieldType.MAINTENANCE,
                alias=alias,
            ),
            _Segment(
                text=tail_text,
                char_start=start + position + 1 + (len(raw[position + 1 :]) - len(raw[position + 1 :].lstrip())),
                char_end=start + position + 1 + len(raw[position + 1 :].rstrip()),
                field_type=FieldType.RECOVERY,
                alias=alias,
            ),
        )
    return ()


#: 行首「有名字段：」但不在 FIELD_ALIASES 时的候选标签
LABEL_PATTERN = re.compile(r"^([^：:\s，。！？；、]{2,14})[：:]")


def detect_label(text: str) -> str | None:
    """返回行首的未知字段标签；已知别名与说明前缀不算未知。"""
    first = text.strip().split("\n", 1)[0].strip()
    if not first or text.strip().startswith(ANNOTATION_PREFIXES):
        return None
    matched = LABEL_PATTERN.match(first)
    if not matched:
        return None
    label = matched.group(1)
    if label in FIELD_ALIASES:
        return None
    return label

#: 示例问答里的说话者前缀（“你：”“纱凪：”）。它们不是字段标签，也不是标题。
SPEAKER_NAMES = frozenset({"你", "纱凪", "乾纱凪", "他", "她", "对方"})
_SPEAKER_RE = re.compile(r"^([^\s：:，。！？；]{1,6})[：:]\s*")


def detect_speaker(text: str) -> str | None:
    """识别行首说话者；只认已知说话者，避免把普通冒号段落当问答。"""
    match = _SPEAKER_RE.match(text.strip())
    if not match:
        return None
    name = match.group(1).strip()
    return name if name in SPEAKER_NAMES else None



def split_fields(block: RawBlock) -> tuple[_Segment, ...]:
    """按行与行内标记切分，保留每个字段的原文字符区间。"""
    text = block.text
    if not text.strip():
        return ()
    segments: list[_Segment] = []
    base = block.char_start
    offset = 0
    current: list[str] = []
    current_start = 0
    current_field: FieldType | None = None
    current_alias: str | None = None
    current_end = 0

    def flush() -> None:
        nonlocal current, current_start, current_end
        if not current:
            return
        joined = "\n".join(current)
        leading = len(joined) - len(joined.lstrip())
        trailing = len(joined) - len(joined.rstrip())
        segments.append(
            _Segment(
                text=joined.strip(),
                char_start=current_start + leading,
                char_end=current_end - trailing,
                field_type=current_field,
                alias=current_alias,
            )
        )
        current = []

    for line in text.split("\n"):
        matches = find_markers(line)
        standalone = detect_standalone_marker(line) if not matches else None
        if matches:
            for index, marker in enumerate(matches):
                end = matches[index + 1].start if index + 1 < len(matches) else len(line)
                if index == 0:
                    flush()
                raw = line[marker.start : end]
                pieces = _split_composite(
                    raw,
                    field_type=marker.field_type,
                    alias=marker.alias,
                    start=base + offset + marker.start,
                )
                if len(pieces) > 1:
                    segments.extend(pieces)
                else:
                    trailing = len(raw) - len(raw.rstrip())
                    segments.append(
                        _Segment(
                            text=raw.strip(),
                            char_start=base + offset + marker.start,
                            char_end=base + offset + end - trailing,
                            field_type=marker.field_type,
                            alias=marker.alias,
                        )
                    )
            current_field = None
            current_alias = None
        elif standalone is not None:
            flush()
            current = [line]
            current_start = base + offset
            current_end = base + offset + len(line)
            current_field = standalone.field_type
            current_alias = standalone.alias
        else:
            if not current:
                current_start = base + offset
            current.append(line)
            current_field = None if not current_field else current_field
            current_end = base + offset + len(line)
        offset += len(line) + 1
    flush()
    return tuple(segment for segment in segments if segment.text)


def _chapter_type(heading_text: str) -> ContentType | None:
    match = re.match(r"^([一二三四五六七八九十]+)\s*[、.．]", heading_text.strip())
    if match:
        return CHAPTER_CONTENT_TYPES.get(match.group(1))
    return NAMED_SECTION_CONTENT_TYPES.get(heading_text.strip())


def _subsection_type(heading_text: str) -> ContentType | None:
    for needle, content_type in SUBSECTION_CONTENT_TYPES:
        if needle in heading_text:
            return content_type
    return None


def _scope_conditions(heading_path: Sequence[str], text: str) -> dict[str, Any]:
    """由标题路径与原文推导适用条件；推导结果标记为 derived。"""
    joined = " ".join(heading_path)
    combined = f"{joined} {text}"
    conditions: dict[str, Any] = {
        "relationship": "any",
        "scene": "any",
        "primary_moods": [],
        "secondary_tags": [],
        "derived": False,
    }
    # 本层标题自己写的条件优先于祖先路径：否则「未恋爱章节」下的「恋人专属」
    # 子标题会被祖先改写，冲突就永远发现不了。
    relationship_text = text or joined
    universal = any(
        marker in relationship_text for marker in UNIVERSAL_MARKERS
    )
    if not universal:
        if "未恋爱" in relationship_text:
            conditions["relationship"] = "non_romantic"
            conditions["derived"] = True
        elif "恋人" in relationship_text or "确立恋人关系后" in relationship_text:
            conditions["relationship"] = "romantic"
            conditions["derived"] = True
        elif "未恋爱" in joined and not any(
            marker in joined for marker in UNIVERSAL_MARKERS
        ):
            conditions["relationship"] = "non_romantic"
            conditions["derived"] = True
        elif "恋人" in joined or "确立恋人关系后" in joined:
            conditions["relationship"] = "romantic"
            conditions["derived"] = True
    if "独处" in text:
        conditions["scene"] = "private"
        conditions["derived"] = True
    elif "独处" in joined:
        conditions["scene"] = "private"
        conditions["derived"] = True
    for needle, moods, tags in _STATE_MOOD_MAP:
        if needle in text or needle in joined:
            conditions["primary_moods"] = list(moods)
            conditions["secondary_tags"] = list(tags)
            conditions["derived"] = True
            break
    if "not_genuine_anger" in conditions["secondary_tags"]:
        conditions["not_genuine_anger"] = True
    if "scene" in conditions and conditions["scene"] == "private" and "独处" in text:
        conditions["private_only"] = True
    if "吃醋" in combined:
        conditions["jealousy"] = True
    return conditions


def annotate_conditions(nodes: Sequence[RuleNode]) -> tuple[RuleNode, ...]:
    """按章节补充触发器/消退文本与恋爱硬限制标记，供核对与 reducer 使用。"""
    annotated: list[RuleNode] = []
    for node in nodes:
        conditions = dict(node.conditions)
        if node.field_type is FieldType.TRIGGER and node.raw_text:
            conditions["trigger_texts"] = [node.raw_text]
        if node.field_type is FieldType.RECOVERY and node.raw_text:
            conditions["exit_texts"] = [node.raw_text]
        annotated.append(
            RuleNode(
                id=node.id,
                document_id=node.document_id,
                parent_id=node.parent_id,
                heading_path=node.heading_path,
                paragraph_index=node.paragraph_index,
                char_start=node.char_start,
                char_end=node.char_end,
                raw_text=node.raw_text,
                content_type=node.content_type,
                field_type=node.field_type,
                list_level=node.list_level,
                scope=node.scope,
                conditions=conditions,
                speaker=node.speaker,
                provenance=node.provenance,
                review_status=node.review_status,
            )
        )
    return tuple(annotated)


def build_nodes(
    blocks: Sequence[RawBlock],
    *,
    document_id: str,
) -> RuleParseResult:
    """按标题作用域生成带类型节点，并输出标题树与核对问题。"""
    nodes: list[RuleNode] = []
    heading_tree: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    stack: list[tuple[int, str, ContentType, str]] = []  # level, node_id, content_type, title
    unknown_labels: list[str] = []
    # 独立标签行（如「典型反应台词：」）之后的无标签段落属于同一字段，
    # 直到遇到新字段、同级组或章节边界。
    continuation: tuple[str, FieldType] | None = None

    def current_scope() -> tuple[tuple[str, ...], ContentType, str | None]:
        if not stack:
            return (), ContentType.GENERAL_RULE, None
        return (
            tuple(entry[3] for entry in stack),
            stack[-1][2],
            stack[-1][1],
        )

    def resolve_type(heading_text: str) -> ContentType:
        return (
            _chapter_type(heading_text)
            or _subsection_type(heading_text)
            or (stack[-1][2] if stack else ContentType.GENERAL_RULE)
        )

    for block in blocks:
        if not block.text.strip():
            continue
        if block.heading is not None:
            level = block.heading.level
            # 同级或更高级标题终止当前作用域
            while stack and stack[-1][0] >= level:
                stack.pop()
            content_type = _chapter_type(block.heading.text) or _subsection_type(block.heading.text)
            if content_type is None:
                content_type = stack[-1][2] if stack else ContentType.GENERAL_RULE
            heading_path = tuple(entry[3] for entry in stack) + (block.heading.text,)
            parent_id = stack[-1][1] if stack else None
            node_id = uuid.uuid4().hex
            conditions = _scope_conditions(heading_path, block.heading.text)
            nodes.append(
                RuleNode(
                    id=node_id,
                    document_id=document_id,
                    parent_id=parent_id,
                    heading_path=heading_path,
                    paragraph_index=block.paragraph_index,
                    char_start=block.char_start,
                    char_end=block.char_end,
                    raw_text=block.text,
                    content_type=content_type,
                    field_type=None,
                    list_level=block.list_level,
                    scope=" | ".join(heading_path),
                    conditions=conditions,
                    provenance=Provenance.SOURCE,
                )
            )
            stack.append((level, node_id, content_type, block.heading.text))
            continuation = None
            heading_tree.append(
                {
                    "node_id": node_id,
                    "level": level,
                    "title": block.heading.text,
                    "basis": block.heading.basis,
                    "content_type": content_type.value,
                    "parent_id": parent_id,
                }
            )
            continue

        heading_path, scope_type, parent_id = current_scope()
        scope_text = block.text.strip()
        is_annotation = scope_text.startswith(ANNOTATION_PREFIXES)
        segments = split_fields(block) or (
            _Segment(text=scope_text, char_start=block.char_start, char_end=block.char_end),
        )
        speaker = detect_speaker(scope_text)
        standalone = detect_standalone_marker(scope_text)
        for segment in segments:
            field_type = segment.field_type
            is_continuation = False
            if field_type is None and continuation is not None and not is_annotation:
                field_type = continuation[1]
                is_continuation = True
            if scope_type is ContentType.NEGATIVE:
                field_type = FieldType.NEGATIVE
            content_type = scope_type
            if is_annotation:
                content_type = ContentType.GENERAL_RULE
            elif field_type is FieldType.EXAMPLE:
                content_type = ContentType.EXAMPLE
            elif field_type is FieldType.NEGATIVE:
                content_type = ContentType.NEGATIVE
            conditions = _scope_conditions(heading_path, segment.text)
            if is_annotation:
                conditions["annotation"] = True
            elif speaker is not None:
                # 说话者只是示例里的角色归属，不是新字段
                conditions["speaker"] = speaker
            elif field_type is None or is_continuation:
                # 未识别字段必须进问题清单，不能静默并入正文（FR-003）
                label = detect_label(segment.text)
                if label:
                    conditions["original_label"] = label
                    unknown_labels.append(label)
            if segment.alias and segment.field_type is not FIELD_ALIASES.get(segment.alias):
                conditions["field_alias"] = segment.alias
            elif segment.alias:
                conditions["field_alias"] = segment.alias
            elif is_continuation and continuation is not None:
                conditions["field_alias"] = continuation[0]
                conditions["continuation"] = True
            if block.kind == "table_cell":
                conditions.setdefault("table_path", list(block.table_path or ()))
            nodes.append(
                RuleNode(
                    id=uuid.uuid4().hex,
                    document_id=document_id,
                    parent_id=parent_id,
                    heading_path=heading_path,
                    paragraph_index=block.paragraph_index,
                    char_start=segment.char_start,
                    char_end=segment.char_end,
                    raw_text=segment.text,
                    content_type=content_type,
                    field_type=field_type,
                    list_level=block.list_level,
                    scope=" | ".join(heading_path),
                    conditions=conditions,
                    provenance=Provenance.DERIVED if conditions.get("derived") else Provenance.SOURCE,
                )
            )
        if standalone is not None:
            continuation = (standalone.alias, standalone.field_type)
        else:
            labelled = [segment for segment in segments if segment.alias]
            if labelled:
                last = labelled[-1]
                continuation = (str(last.alias), last.field_type)  # type: ignore[arg-type]

    mapped = annotate_conditions(nodes)
    body = "\n".join(block.text for block in blocks)
    coverage = _coverage(mapped, body)
    if unknown_labels:
        issues.append(
            {
                "reason_code": "unrecognized_field_label",
                "message": "存在未登记的字段标签，已保留原标签并作为说明段落处理",
                "labels": sorted(set(unknown_labels)),
                "count": len(unknown_labels),
            }
        )
    if coverage["unmapped_char_count"]:
        issues.append(
            {
                "reason_code": "unmapped_source_text",
                "message": "存在未映射到任何节点的正文区间",
                "char_count": coverage["unmapped_char_count"],
            }
        )
    return RuleParseResult(
        nodes=mapped,
        heading_tree=tuple(heading_tree),
        issues=tuple(issues),
        structure_hash=compute_structure_hash(mapped, body),
        coverage=coverage,
    )


def _coverage(nodes: Iterable[RuleNode], body_text: str) -> dict[str, Any]:
    """原文覆盖统计：按字符区间并集，避免重复关联掩盖漏字。"""
    spans = sorted((node.char_start, node.char_end) for node in nodes)
    covered = 0
    cursor = -1
    for start, end in spans:
        if end <= start:
            continue
        start = max(start, 0)
        end = min(end, len(body_text))
        if start > cursor:
            covered += end - start
            cursor = end
        elif end > cursor:
            covered += end - cursor
            cursor = end
    blank = sum(1 for char in body_text if char.isspace())
    return {
        "body_length": len(body_text),
        "covered_char_count": covered,
        "unmapped_char_count": max(0, len(body_text) - blank - covered),
        "node_count": len(list(nodes)) if not isinstance(nodes, Sequence) else len(nodes),
    }
