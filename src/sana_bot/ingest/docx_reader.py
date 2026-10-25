"""按 OOXML 正文顺序读取正式 DOCX。

只读定位：不依赖 Heading 样式，保留段落序号、字符区间、列表层级与表格路径，
识别可见中文章节编号与状态序号。文档内容不具备执行工具或修改系统的权限。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from sana_bot.ingest.models import DOCX_PARSER_VERSION

__all__ = [
    "RawBlock",
    "DocxReadResult",
    "read_docx",
    "iter_body_paragraphs",
    "paragraph_text",
    "detect_heading",
    "HeadingInfo",
    "CHAPTER_PATTERN",
    "SUPPLEMENT_HEADINGS",
    "HEADING_BLOCKING_PUNCTUATION",
    "style_heading_level",
    "heading_title",
    "quoted_speaker_prefix",
]

#: 可见中文章节编号，例如「一、基础档案」
CHAPTER_PATTERN = re.compile(r"^([一二三四五六七八九十]+)\s*[、.．]\s*(.+)$")
#: 手工状态序号，例如「1. 平稳常态」
NUMBERED_PATTERN = re.compile(r"^(\d{1,2})\s*[、.．]\s*(\S.*)$")
#: 标题不得包含句末或字段标记标点（顿号与逗号允许，例如「喜好、擅长」）
HEADING_BLOCKING_PUNCTUATION = set("。！？…；：;:!?~～")
#: 兼容旧名；正文中的顿号/逗号不算句末标点
SENTENCE_PUNCTUATION = HEADING_BLOCKING_PUNCTUATION

#: 未编号但明确的章节标题（可见文字本身即标题）
SUPPLEMENT_HEADINGS = frozenset({"补充约束", "设定说明"})

MAX_HEADING_LENGTH = 26

#: Word 内置标题样式名（含中文版）与 OOXML outlineLvl 的对应基准。
STYLE_HEADING_PATTERN = re.compile(r"(?i)^\s*(?:heading|标题)\s*([1-9])\s*$")
STYLE_TITLE_NAMES = frozenset({"title", "标题", "document title"})
#: 以这些符号开头的行是引用台词，其中的说话者冒号不是字段/标题分隔。
QUOTE_OPENERS = frozenset("\"\'\u201c\u2018\u300c\u300e")
SPEAKER_COLONS = frozenset("\uff1a:")


@dataclass(frozen=True)
class HeadingInfo:
    """识别出的标题；``level`` 越小越靠外。"""

    text: str
    level: int
    basis: str


@dataclass(frozen=True)
class RawBlock:
    """正文中的一个有序块（段落或表格单元格）。"""

    index: int
    kind: str
    text: str
    char_start: int
    char_end: int
    paragraph_index: int = -1
    list_level: int | None = None
    num_id: int | None = None
    style_name: str = "Normal"
    bold: bool = False
    table_path: tuple[int, int, int] | None = None
    heading: HeadingInfo | None = None
    outline_level: int | None = None

    @property
    def is_list_item(self) -> bool:
        return self.num_id is not None

    def to_payload(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "text": self.text,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "paragraph_index": self.paragraph_index,
            "list_level": self.list_level,
            "num_id": self.num_id,
            "style_name": self.style_name,
            "bold": self.bold,
            "table_path": list(self.table_path) if self.table_path else None,
            "outline_level": self.outline_level,
            "heading": (
                {"text": self.heading.text, "level": self.heading.level, "basis": self.heading.basis}
                if self.heading
                else None
            ),
        }


@dataclass(frozen=True)
class DocxReadResult:
    """读取结果；``body_text`` 是字符区间的基准文本。"""

    blocks: tuple[RawBlock, ...]
    body_text: str
    file_hash: str
    parser_version: str = DOCX_PARSER_VERSION
    issues: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    table_count: int = 0
    paragraph_count: int = 0


def iter_body_paragraphs(parent: Any) -> Iterator[Any]:
    """按正文顺序产出段落与表格（含嵌套表格）。"""
    element = getattr(parent, "element", None)
    if element is None:
        element = parent._element
    body = getattr(element, "body", element)
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, parent)
        elif child.tag == qn("w:tbl"):
            yield Table(child, parent)


def paragraph_text(paragraph: Any) -> str:
    """无损提取段落文本：遍历所有 run（含超链接内嵌），保留软换行与制表符。"""
    chunks: list[str] = []
    for node in paragraph._p.iter():
        tag = node.tag
        if tag == qn("w:t"):
            chunks.append(node.text or "")
        elif tag == qn("w:br") or tag == qn("w:cr"):
            chunks.append("\n")
        elif tag == qn("w:tab"):
            chunks.append("\t")
        elif tag == qn("w:noBreakHyphen"):
            chunks.append("-")
    return "".join(chunks)


def _paragraph_bold(paragraph: Any) -> bool:
    runs = [run for run in paragraph.runs if run.text and run.text.strip()]
    if not runs:
        return False
    return all(bool(run.bold) for run in runs)


def style_heading_level(style_name: str) -> int | None:
    """Word 标题样式 → 层级；``Title`` 为文档标题（0），``Heading N`` 为 N。"""
    name = (style_name or "").strip()
    if not name:
        return None
    if name.lower() in STYLE_TITLE_NAMES:
        return 0
    matched = STYLE_HEADING_PATTERN.match(name)
    if matched:
        return int(matched.group(1))
    return None


def heading_title(block_text: str) -> str:
    """标题的可见标题部分：编号标题带说明时取首个冒号之前。"""
    text = block_text.strip()
    for position, char in enumerate(text):
        if char in SPEAKER_COLONS and position:
            return text[:position].strip()
    return text


def quoted_speaker_prefix(block_text: str) -> bool:
    """引用台词开头的说话者写法（``“你：…``）不是字段也不是标题。"""
    text = block_text.lstrip()
    if not text or text[0] not in QUOTE_OPENERS:
        return False
    for char in text[1:]:
        if char in QUOTE_OPENERS or char in "\"\'\u201d\u2019\u300d\u300f":
            return False
        if char in SPEAKER_COLONS:
            return True
    return False


def detect_heading(block: RawBlock) -> HeadingInfo | None:
    """依据样式、OOXML 层级、可见序号与列表层级识别标题。

    顺序固定：样式/大纲层级 → 中文章节号 → 已知章节名 → 手写编号
    （含「编号. 状态：说明」） → 列表标题 → 短行加粗。列表项默认属于
    最近标题的作用域，只有本身像标题时才升格。
    """
    text = block.text.strip()
    if not text or "\n" in text:
        return None
    if quoted_speaker_prefix(text):
        return None

    chapter = CHAPTER_PATTERN.match(text)
    if chapter:
        return HeadingInfo(text=text, level=1, basis="chinese_chapter_number")

    if text in SUPPLEMENT_HEADINGS:
        return HeadingInfo(text=text, level=1, basis="known_section_name")

    style_level = style_heading_level(block.style_name)
    if style_level is not None:
        return HeadingInfo(text=text, level=style_level, basis="ooxml_style")

    numbered = NUMBERED_PATTERN.match(text)
    if numbered:
        # 「1. 害羞局促状态」与「1. 普通日常：只有轻微嘴硬…」都算标题：
        # 判据是冒号前的标题部分长度，而不是整行长度或加粗样式。
        title = heading_title(text)
        if title and len(title) <= MAX_HEADING_LENGTH:
            basis = "manual_state_number"
            if title != text:
                basis = "numbered_colon_heading"
            return HeadingInfo(text=text, level=2, basis=basis)

    if block.outline_level is not None:
        # 大纲层级只决定相对深度：一级标题仍由可见章节号保证，正文小标题
        # 最低落在二级，避免「补充约束」被写成八、输出规则的子节。
        return HeadingInfo(
            text=text, level=max(2, block.outline_level), basis="ooxml_outline_level"
        )

    if block.is_list_item:
        # 列表项的层级来自 Word 编号定义；只有短且无标点的加粗项才是标题。
        title = heading_title(text)
        if (
            block.bold
            and title
            and len(title) <= MAX_HEADING_LENGTH
            and not (set(title) & HEADING_BLOCKING_PUNCTUATION)
        ):
            level = 2 + min(block.list_level or 0, 3)
            return HeadingInfo(text=text, level=level, basis="list_level_heading")
        return None

    if len(text) <= MAX_HEADING_LENGTH and not (set(text) & HEADING_BLOCKING_PUNCTUATION):
        if block.bold:
            return HeadingInfo(text=text, level=2, basis="short_line_no_punctuation")
    return None


def read_docx(source: Path) -> DocxReadResult:
    """读取 DOCX；返回有序块与无损原文。"""
    if not source.exists():
        raise FileNotFoundError(f"资料文件不存在：{source}")
    file_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    document = Document(str(source))

    blocks: list[RawBlock] = []
    issues: list[dict[str, Any]] = []
    offset = 0
    paragraph_index = 0
    table_count = 0

    def emit(
        *,
        kind: str,
        text: str,
        paragraph: Any | None,
        table_path: tuple[int, int, int] | None,
    ) -> None:
        nonlocal offset, paragraph_index
        start = offset
        offset += len(text) + 1  # 以 \n 连接各块
        block = RawBlock(
            index=len(blocks),
            kind=kind,
            text=text,
            char_start=start,
            char_end=start + len(text),
            paragraph_index=paragraph_index if paragraph is not None else -1,
            list_level=_list_level(paragraph),
            num_id=_num_id(paragraph),
            style_name=_style_name(paragraph),
            bold=_paragraph_bold(paragraph) if paragraph is not None else False,
            table_path=table_path,
            outline_level=_outline_level(paragraph),
        )
        blocks.append(block)
        if paragraph is not None:
            paragraph_index += 1

    for child in iter_body_paragraphs(document):
        if isinstance(child, Table):
            table_count += 1
            for row_index, row in enumerate(child.rows):
                for cell_index, cell in enumerate(row.cells):
                    for inner_index, inner in enumerate(iter_body_paragraphs(cell)):
                        if isinstance(inner, Table):
                            issues.append(
                                {
                                    "reason_code": "nested_table_skipped",
                                    "message": "嵌套表格仅记录路径，未展开单元格",
                                    "table_path": [table_count - 1, row_index, cell_index],
                                }
                            )
                            continue
                        emit(
                            kind="table_cell",
                            text=paragraph_text(inner),
                            paragraph=inner,
                            table_path=(table_count - 1, row_index, cell_index),
                        )
                        del inner_index
            continue
        emit(kind="paragraph", text=paragraph_text(child), paragraph=child, table_path=None)

    body_text = "\n".join(block.text for block in blocks)
    with_headings = _attach_headings(blocks)
    empty_blocks = sum(1 for block in with_headings if not block.text.strip())
    if empty_blocks:
        issues.append(
            {
                "reason_code": "blank_blocks_ignored",
                "message": "空白块保留位置但未参与字段分类",
                "count": empty_blocks,
            }
        )
    return DocxReadResult(
        blocks=with_headings,
        body_text=body_text,
        file_hash=file_hash,
        issues=tuple(issues),
        table_count=table_count,
        paragraph_count=sum(1 for block in with_headings if block.kind == "paragraph"),
    )


def _attach_headings(blocks: Sequence[RawBlock]) -> tuple[RawBlock, ...]:
    """标注标题，并把首个无编号标题视为文档标题（level 0）。"""
    result: list[RawBlock] = []
    seen_chapter = False
    title_assigned = False
    for block in blocks:
        heading = detect_heading(block)
        if (
            heading is not None
            and not title_assigned
            and not seen_chapter
            and heading.basis == "short_line_no_punctuation"
        ):
            heading = HeadingInfo(text=heading.text, level=0, basis="document_title")
            title_assigned = True
        if heading is not None and heading.level == 1:
            seen_chapter = True
        result.append(
            RawBlock(
                index=block.index,
                kind=block.kind,
                text=block.text,
                char_start=block.char_start,
                char_end=block.char_end,
                paragraph_index=block.paragraph_index,
                list_level=block.list_level,
                num_id=block.num_id,
                style_name=block.style_name,
                bold=block.bold,
                table_path=block.table_path,
                heading=heading,
            )
        )
    return tuple(result)


def _num_pr(paragraph: Any | None) -> Any | None:
    """段落直接编号优先；否则取样式层编号（同样属于 numPr）。"""
    if paragraph is None:
        return None
    pPr = paragraph._p.pPr
    if pPr is not None and pPr.numPr is not None:
        return pPr.numPr
    style = getattr(paragraph, "style", None)
    element = getattr(style, "element", None)
    if element is None:
        return None
    style_pPr = element.find(qn("w:pPr"))
    if style_pPr is None:
        return None
    return style_pPr.find(qn("w:numPr"))


def _list_level(paragraph: Any | None) -> int | None:
    numPr = _num_pr(paragraph)
    if numPr is None or numPr.ilvl is None:
        return 0 if numPr is not None else None
    return int(numPr.ilvl.val)


def _num_id(paragraph: Any | None) -> int | None:
    numPr = _num_pr(paragraph)
    if numPr is None or numPr.numId is None:
        return None
    return int(numPr.numId.val)


def _outline_level(paragraph: Any | None) -> int | None:
    """段落或样式上的 OOXML 大纲层级（0 基）；与 Word 标题级别同源。"""
    if paragraph is None:
        return None
    candidates = [paragraph._p.pPr]
    style = getattr(paragraph, "style", None)
    element = getattr(style, "element", None)
    if element is not None:
        candidates.append(element.find(qn("w:pPr")))
    for pPr in candidates:
        if pPr is None:
            continue
        outline = pPr.find(qn("w:outlineLvl"))
        if outline is None:
            continue
        try:
            return int(outline.get(qn("w:val")))
        except (TypeError, ValueError):
            continue
    return None


def _style_name(paragraph: Any | None) -> str:
    if paragraph is None:
        return "Normal"
    try:
        return paragraph.style.name
    except (AttributeError, KeyError):  # pragma: no cover - 未知样式名
        return "Normal"
