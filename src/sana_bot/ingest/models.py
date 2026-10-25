"""资料结构与索引的领域模型。

字段与校验对应 data-model.md：类型/角色单一、原文无损、说明无 speaker 不等于
monologue、跨角色可关联但不混改说话者、向量维度与 token 上限由后端决定且禁止静默截断。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sana_bot.enums import ContentType, FieldType, Provenance, ReviewStatus, SpanKind

__all__ = [
    "PARSER_VERSION",
    "SPLITTER_VERSION",
    "EMBEDDING_DIMENSION",
    "EMBEDDING_MAX_TOKENS",
    "SEMANTIC_UNIT_TARGET_TOKENS",
    "INDEX_FORMAT_SEMANTIC_V1",
    "INDEX_FORMAT_STRUCTURAL_V2",
    "CHUNKING_STRATEGY_SEMANTIC",
    "CHUNKING_STRATEGY_MARKDOWN",
    "SourceDocument",
    "RuleNode",
    "SourceSpan",
    "SemanticUnit",
    "EmbeddingRecord",
    "StructuredDocument",
    "canonical_json",
    "compute_structure_hash",
]

PARSER_VERSION = "sana-ingest-1.0.0"
SPLITTER_VERSION = "sana-splitter-1.0.0"
DOCX_PARSER_VERSION = "sana-docx-1.0.0"

INDEX_FORMAT_SEMANTIC_V1 = "semantic_v1"
INDEX_FORMAT_STRUCTURAL_V2 = "structural_v2"
CHUNKING_STRATEGY_SEMANTIC = "semantic"
CHUNKING_STRATEGY_MARKDOWN = "markdown_cards_v1"

#: 默认本地后端 BAAI/bge-small-zh-v1.5 的向量维度与输入上限（模型卡与 config.json）；
#: 实际值由 EMBEDDING_DIMENSION / EMBEDDING_MAX_TOKENS 决定，每条记录保存真实维度。
EMBEDDING_DIMENSION = 512
EMBEDDING_MAX_TOKENS = 512
#: 语义段目标长度（research.md：目标不超过 384 token）
SEMANTIC_UNIT_TARGET_TOKENS = 384


def canonical_json(payload: Any) -> str:
    """确定性序列化，用于所有 hash 计算。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceDocument:
    """正式资料文件；只读，且文档内容不具有执行能力。"""

    id: str
    relative_path: str
    file_hash: str
    imported_at: str
    parser_version: str = DOCX_PARSER_VERSION

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "relative_path": self.relative_path,
            "file_hash": self.file_hash,
            "imported_at": self.imported_at,
            "parser_version": self.parser_version,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SourceDocument:
        return cls(
            id=str(payload["id"]),
            relative_path=str(payload["relative_path"]),
            file_hash=str(payload["file_hash"]),
            imported_at=str(payload["imported_at"]),
            parser_version=str(payload.get("parser_version", DOCX_PARSER_VERSION)),
        )


@dataclass(frozen=True)
class RuleNode:
    """带类型的结构节点；一段可产生多个字段节点，来源 char_range 不丢失。"""

    id: str
    document_id: str
    parent_id: str | None
    heading_path: tuple[str, ...]
    paragraph_index: int
    char_start: int
    char_end: int
    raw_text: str
    content_type: ContentType
    field_type: FieldType | None = None
    list_level: int | None = None
    scope: str = ""
    conditions: Mapping[str, Any] = field(default_factory=dict)
    speaker: str | None = None
    provenance: Provenance = Provenance.SOURCE
    review_status: ReviewStatus = ReviewStatus.PENDING

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "document_id": self.document_id,
            "parent_id": self.parent_id,
            "heading_path": list(self.heading_path),
            "paragraph_index": self.paragraph_index,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "raw_text": self.raw_text,
            "content_type": self.content_type.value,
            "field_type": self.field_type.value if self.field_type else None,
            "list_level": self.list_level,
            "scope": self.scope,
            "conditions": dict(self.conditions),
            "speaker": self.speaker,
            "provenance": self.provenance.value,
            "review_status": self.review_status.value,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> RuleNode:
        field_type = payload.get("field_type")
        return cls(
            id=str(payload["id"]),
            document_id=str(payload["document_id"]),
            parent_id=payload.get("parent_id"),
            heading_path=tuple(payload.get("heading_path", ())),
            paragraph_index=int(payload["paragraph_index"]),
            char_start=int(payload["char_start"]),
            char_end=int(payload["char_end"]),
            raw_text=str(payload["raw_text"]),
            content_type=ContentType(payload["content_type"]),
            field_type=FieldType(field_type) if field_type else None,
            list_level=payload.get("list_level"),
            scope=str(payload.get("scope", "")),
            conditions=dict(payload.get("conditions", {})),
            speaker=payload.get("speaker"),
            provenance=Provenance(payload.get("provenance", Provenance.SOURCE.value)),
            review_status=ReviewStatus(payload.get("review_status", ReviewStatus.PENDING.value)),
        )


@dataclass(frozen=True)
class SourceSpan:
    """角色切分后的基础片段；类型/角色单一，原文无损。"""

    id: str
    node_id: str
    kind: SpanKind
    raw_text: str
    source_start: int
    source_end: int
    speaker: str | None = None
    previous_id: str | None = None
    next_id: str | None = None
    parent_span_id: str | None = None
    segment_index: int = 0
    speaker_label: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "node_id": self.node_id,
            "kind": self.kind.value,
            "raw_text": self.raw_text,
            "source_start": self.source_start,
            "source_end": self.source_end,
            "speaker": self.speaker,
            "previous_id": self.previous_id,
            "next_id": self.next_id,
            "parent_span_id": self.parent_span_id,
            "segment_index": self.segment_index,
            "speaker_label": self.speaker_label,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SourceSpan:
        return cls(
            id=str(payload["id"]),
            node_id=str(payload["node_id"]),
            kind=SpanKind(payload["kind"]),
            raw_text=str(payload["raw_text"]),
            source_start=int(payload["source_start"]),
            source_end=int(payload["source_end"]),
            speaker=payload.get("speaker"),
            previous_id=payload.get("previous_id"),
            next_id=payload.get("next_id"),
            parent_span_id=payload.get("parent_span_id"),
            segment_index=int(payload.get("segment_index", 0)),
            speaker_label=payload.get("speaker_label"),
        )


@dataclass(frozen=True)
class SemanticUnit:
    """围绕同一话题或情景的语义单元；保留来源片段、边界证据与关联。"""

    id: str
    index_version: str
    source_span_ids: tuple[str, ...]
    topic: str
    text: str
    boundary_evidence: Mapping[str, Any] = field(default_factory=dict)
    context_links: tuple[str, ...] = ()
    source_node_ids: tuple[str, ...] = ()
    speaker_labels: tuple[str, ...] = ()
    conditions: Mapping[str, Any] = field(default_factory=dict)
    content_types: tuple[str, ...] = ()
    token_count: int = 0
    parent_unit_id: str | None = None
    offset_in_parent: int | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "index_version": self.index_version,
            "source_span_ids": list(self.source_span_ids),
            "topic": self.topic,
            "text": self.text,
            "boundary_evidence": dict(self.boundary_evidence),
            "context_links": list(self.context_links),
            "source_node_ids": list(self.source_node_ids),
            "speaker_labels": list(self.speaker_labels),
            "conditions": dict(self.conditions),
            "content_types": list(self.content_types),
            "token_count": self.token_count,
            "parent_unit_id": self.parent_unit_id,
            "offset_in_parent": self.offset_in_parent,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SemanticUnit:
        return cls(
            id=str(payload["id"]),
            index_version=str(payload["index_version"]),
            source_span_ids=tuple(payload.get("source_span_ids", ())),
            topic=str(payload.get("topic", "")),
            text=str(payload.get("text", "")),
            boundary_evidence=dict(payload.get("boundary_evidence", {})),
            context_links=tuple(payload.get("context_links", ())),
            source_node_ids=tuple(payload.get("source_node_ids", ())),
            speaker_labels=tuple(payload.get("speaker_labels", ())),
            conditions=dict(payload.get("conditions", {})),
            content_types=tuple(payload.get("content_types", ())),
            token_count=int(payload.get("token_count", 0)),
            parent_unit_id=payload.get("parent_unit_id"),
            offset_in_parent=payload.get("offset_in_parent"),
        )


@dataclass(frozen=True)
class EmbeddingRecord:
    """向量元数据；禁止静默截断，不同 revision 不混检索。"""

    unit_id: str
    model_id: str
    revision: str
    dimension: int
    input_hash: str
    token_count: int
    vector_offset: int
    normalized: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "model_id": self.model_id,
            "revision": self.revision,
            "dimension": self.dimension,
            "input_hash": self.input_hash,
            "token_count": self.token_count,
            "vector_offset": self.vector_offset,
            "normalized": self.normalized,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> EmbeddingRecord:
        return cls(
            unit_id=str(payload["unit_id"]),
            model_id=str(payload["model_id"]),
            revision=str(payload["revision"]),
            dimension=int(payload["dimension"]),
            input_hash=str(payload["input_hash"]),
            token_count=int(payload["token_count"]),
            vector_offset=int(payload["vector_offset"]),
            normalized=bool(payload.get("normalized", True)),
        )


@dataclass(frozen=True)
class StructuredDocument:
    """不可变结构；字段与类型分类先于角色切分与语义切分。"""

    id: str
    source: SourceDocument
    structure_hash: str
    parser_version: str = PARSER_VERSION
    typed_nodes: tuple[RuleNode, ...] = ()
    heading_tree: tuple[Mapping[str, Any], ...] = ()
    catalog_hash: str | None = None
    review_status: ReviewStatus = ReviewStatus.PENDING
    issues: tuple[Mapping[str, Any], ...] = ()
    body_text: str = ""
    coverage: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source.to_payload(),
            "structure_hash": self.structure_hash,
            "parser_version": self.parser_version,
            "typed_nodes": [node.to_payload() for node in self.typed_nodes],
            "heading_tree": [dict(item) for item in self.heading_tree],
            "catalog_hash": self.catalog_hash,
            "review_status": self.review_status.value,
            "issues": [dict(item) for item in self.issues],
            "body_text": self.body_text,
            "coverage": dict(self.coverage),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> StructuredDocument:
        return cls(
            id=str(payload["id"]),
            source=SourceDocument.from_payload(payload["source"]),
            structure_hash=str(payload["structure_hash"]),
            parser_version=str(payload.get("parser_version", PARSER_VERSION)),
            typed_nodes=tuple(RuleNode.from_payload(item) for item in payload.get("typed_nodes", ())),
            heading_tree=tuple(payload.get("heading_tree", ())),
            catalog_hash=payload.get("catalog_hash"),
            review_status=ReviewStatus(payload.get("review_status", ReviewStatus.PENDING.value)),
            issues=tuple(payload.get("issues", ())),
            body_text=str(payload.get("body_text", "")),
            coverage=dict(payload.get("coverage", {})),
        )


def compute_structure_hash(nodes: Sequence[RuleNode], body_text: str) -> str:
    """结构 hash：覆盖类型节点与原文，不含后加的校准报告。

    不含随机主键，保证同一份资料在同一解析版本下 hash 稳定。
    """
    return _sha256(
        canonical_json(
            {
                "nodes": [
                    {
                        "heading_path": list(node.heading_path),
                        "paragraph_index": node.paragraph_index,
                        "char_start": node.char_start,
                        "char_end": node.char_end,
                        "content_type": node.content_type.value,
                        "field_type": node.field_type.value if node.field_type else None,
                        "list_level": node.list_level,
                        "raw_text": node.raw_text,
                        "scope": node.scope,
                        "conditions": dict(node.conditions),
                        "provenance": node.provenance.value,
                    }
                    for node in nodes
                ],
                "body": body_text,
            }
        )
    )


def compute_source_hash(chunks: Iterable[bytes]) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()
