"""资料导入服务：正式 DOCX → 不可变结构 → 角色片段 → 核对报告。

顺序固定：结构解析（标题/字段/类型）→ 角色切分 → 人工核对；语义切分与向量在
切分参数被批准后由构建步骤执行（见 :meth:`IngestService.build_snapshot`）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sana_bot.config import RuntimeConfig, RuntimeLayout
from sana_bot.enums import ContentType, IndexStatus, ReviewStatus, SpanKind
from sana_bot.errors import ConfigError, NotReadyError
from sana_bot.ingest.docx_reader import read_docx
from sana_bot.ingest.models import (
    PARSER_VERSION,
    RuleNode,
    SourceDocument,
    SourceSpan,
    StructuredDocument,
)
from sana_bot.ingest.role_splitter import RoleSplitResult, split_roles
from sana_bot.ingest.semantic_splitter import SplitterParameters
from sana_bot.ingest.rule_parser import (
    RuleParseResult,
    build_nodes,
    aliases_registered,
)
from sana_bot.ports import IngestResult, ServiceIssue
from sana_bot.storage.database import Database
from sana_bot.storage.index_repository import SnapshotRecord, StructureRepository, new_id
from sana_bot.timeutil import to_utc_iso, utc_now

__all__ = [
    "IngestService",
    "ReviewReport",
]

#: 正式资料的八个主章节（用于覆盖核对）
EXPECTED_CHAPTERS = ("一", "二", "三", "四", "五", "六", "七", "八")


@dataclass(frozen=True)
class ReviewReport:
    """可核对的核对材料。"""

    payload: dict[str, Any]
    issues: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def to_json(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, indent=2, sort_keys=True)


class IngestService:
    """结构与角色的导入；不产生可启用快照。"""

    def __init__(
        self,
        database: Database,
        *,
        layout: RuntimeLayout,
        config: RuntimeConfig | None = None,
        repository: StructureRepository | None = None,
    ) -> None:
        self.database = database
        self.layout = layout
        self.config = config or RuntimeConfig()
        self.repository = repository or StructureRepository(database)

    # ------------------------------------------------------------------ 解析

    def parse(self, source: Path, output_dir: Path) -> IngestResult:
        """解析正式资料并落盘不可变结构；失败时给出可诊断结果。"""
        source = Path(source)
        if not source.exists():
            raise ConfigError(f"资料文件不存在：{source}")
        read = read_docx(source)
        structure_id = new_id()
        parsed: RuleParseResult = build_nodes(read.blocks, document_id=structure_id)
        roles: RoleSplitResult = split_roles(parsed.nodes)

        issues: list[ServiceIssue] = [
            ServiceIssue(reason_code=str(item.get("reason_code", "ingest_issue")), message=str(item.get("message", "")))
            for item in (*read.issues, *parsed.issues, *roles.issues)
        ]
        document = StructuredDocument(
            id=structure_id,
            source=SourceDocument(
                id=new_id(),
                relative_path=_relative_to_repo(source),
                file_hash=read.file_hash,
                imported_at=to_utc_iso(utc_now()),
                parser_version=read.parser_version,
            ),
            structure_hash=parsed.structure_hash,
            parser_version=PARSER_VERSION,
            typed_nodes=parsed.nodes,
            heading_tree=parsed.heading_tree,
            review_status=ReviewStatus.PENDING,
            issues=tuple(parsed.issues),
            body_text=read.body_text,
            coverage={**dict(parsed.coverage), "role_span_ratio": roles.coverage.get("ratio", 0.0)},
        )
        directory = self.repository.save_structure(document, output_dir=Path(output_dir))
        self.repository.save_spans(structure_id, roles.spans)

        report = self.build_report(document, roles.spans)
        report_path = Path(directory) / "review-report.json"
        report_path.write_text(report.to_json() + "\n", encoding="utf-8")
        spans_path = Path(directory) / "spans.json"
        spans_path.write_text(
            json.dumps([span.to_payload() for span in roles.spans], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        return IngestResult(
            ok=True,
            structure_id=structure_id,
            structure_path=Path(directory) / "structure.json",
            report_path=report_path,
            node_count=len(document.typed_nodes),
            source_hash=document.source.file_hash,
            issues=tuple(issues),
            correlation_id=structure_id,
            details={
                "span_count": len(roles.spans),
                "speaker_counts": dict(roles.speaker_counts),
                "review_status": document.review_status.value,
                "structure_hash": document.structure_hash,
            },
        )

    # ------------------------------------------------------------------ 核对

    def load(self, structure_id: str) -> StructuredDocument:
        return self.repository.load_structure(structure_id)

    def review(self, structure_id: str) -> ReviewReport:
        document = self.repository.load_structure(structure_id)
        spans = self.repository.load_spans(structure_id)
        return self.build_report(document, spans)

    def build_report(
        self,
        document: StructuredDocument,
        spans: Sequence[SourceSpan],
    ) -> ReviewReport:
        """标题树、字段归属、缺失项、状态与负例清单、原文往返定位。"""
        nodes = document.typed_nodes
        heading_ids = {item["node_id"] for item in document.heading_tree}
        body_nodes = [node for node in nodes if node.id not in heading_ids]
        chapter_coverage: dict[str, int] = {chapter: 0 for chapter in EXPECTED_CHAPTERS}
        chapter_coverage["补充约束"] = 0
        for node in body_nodes:
            chapter = _chapter_of(node)
            if chapter:
                chapter_coverage[chapter] = chapter_coverage.get(chapter, 0) + 1

        field_distribution: dict[str, int] = {}
        content_distribution: dict[str, int] = {}
        for node in body_nodes:
            key = node.field_type.value if node.field_type else "unclassified"
            field_distribution[key] = field_distribution.get(key, 0) + 1
            content_distribution[node.content_type.value] = (
                content_distribution.get(node.content_type.value, 0) + 1
            )

        state_nodes = [node for node in body_nodes if node.content_type is ContentType.STATE]
        negative_nodes = [node for node in body_nodes if node.content_type is ContentType.NEGATIVE]
        example_nodes = [node for node in body_nodes if node.content_type is ContentType.EXAMPLE]
        mood_branches = _mood_branches(state_nodes, body_nodes)
        roundtrip = _roundtrip(document, nodes)
        missing = [
            name
            for name, count in chapter_coverage.items()
            if name in EXPECTED_CHAPTERS and count == 0
        ]
        ambiguous = [
            {
                "node_id": node.id,
                "paragraph_index": node.paragraph_index,
                "field_alias": node.conditions.get("field_alias"),
                "content_type": node.content_type.value,
                "raw_text": node.raw_text[:80],
            }
            for node in body_nodes
            if node.field_type is None
            and node.heading_path
            and node.content_type
            in (ContentType.STATE, ContentType.GENERAL_RULE, ContentType.OUTPUT_RULE)
            and "field_alias" not in node.conditions
        ]

        payload: dict[str, Any] = {
            "structure_id": document.id,
            "structure_hash": document.structure_hash,
            "source": document.source.to_payload(),
            "review_status": document.review_status.value,
            "coverage": dict(document.coverage),
            "heading_tree": [dict(item) for item in document.heading_tree],
            "chapter_coverage": chapter_coverage,
            "missing_chapters": missing,
            "field_distribution": field_distribution,
            "content_type_distribution": content_distribution,
            "state_rules": [
                {
                    "node_id": node.id,
                    "scope": node.scope,
                    "field_type": node.field_type.value if node.field_type else None,
                    "relationship": node.conditions.get("relationship"),
                    "scene": node.conditions.get("scene"),
                    "primary_moods": node.conditions.get("primary_moods", []),
                    "secondary_tags": node.conditions.get("secondary_tags", []),
                    "provenance": node.provenance.value,
                    "raw_text": node.raw_text[:120],
                }
                for node in state_nodes
            ],
            "negative_examples": [
                {"node_id": node.id, "scope": node.scope, "raw_text": node.raw_text[:120]}
                for node in negative_nodes
            ],
            "examples": [
                {
                    "node_id": node.id,
                    "field_type": node.field_type.value if node.field_type else None,
                    "raw_text": node.raw_text[:120],
                }
                for node in example_nodes
            ],
            "mood_branches": mood_branches,
            "unclassified_nodes": ambiguous,
            "unconfirmed_rules": [
                node.id for node in body_nodes if node.provenance.value == "derived"
            ],
            "roundtrip": roundtrip,
            "issues": [dict(item) for item in document.issues],
            "speaker_summary": _speaker_summary(spans),
            "monologue_spans": [
                {"node_id": span.node_id, "raw_text": span.raw_text[:120]}
                for span in spans
                if span.kind is SpanKind.MONOLOGUE
            ],
        }
        return ReviewReport(payload=payload, issues=tuple(document.issues))


def _chapter_of(node: RuleNode) -> str | None:
    for part in node.heading_path:
        if part.startswith("补充约束"):
            return "补充约束"
        for chapter in EXPECTED_CHAPTERS:
            if part.startswith(f"{chapter}、") or part.startswith(f"{chapter}."):
                return chapter
    return None


def _mood_branches(state_nodes: Sequence[RuleNode], all_nodes: Sequence[RuleNode]) -> dict[str, Any]:
    """六类分支与恋爱/非恋爱覆盖情况，供人工核对。"""
    branches: dict[str, Any] = {}
    for node in state_nodes:
        title = node.heading_path[-1] if node.heading_path else node.scope
        key = title or "未命名状态"
        entry = branches.setdefault(
            key,
            {
                "relationship": node.conditions.get("relationship"),
                "scene": node.conditions.get("scene"),
                "primary_moods": node.conditions.get("primary_moods", []),
                "secondary_tags": node.conditions.get("secondary_tags", []),
                "field_types": [],
                "derived": bool(node.conditions.get("derived")),
            },
        )
        if node.field_type is not None:
            entry["field_types"].append(node.field_type.value)
    romance_markers = sum(
        1 for node in all_nodes if node.conditions.get("relationship") == "romantic"
    )
    return {
        "branches": branches,
        "branch_count": len(branches),
        "romantic_condition_nodes": romance_markers,
        "non_romantic_condition_nodes": sum(
            1 for node in all_nodes if node.conditions.get("relationship") == "non_romantic"
        ),
        "note": "非恋爱二级状态与主情绪映射为推导值，需提供者确认；未批准分支不得执行",
    }


def _roundtrip(document: StructuredDocument, nodes: Sequence[RuleNode]) -> dict[str, Any]:
    """原文往返定位：每个节点区间必须能在正文中原样取回。"""
    mismatches: list[dict[str, Any]] = []
    for node in nodes:
        excerpt = document.body_text[node.char_start : node.char_end]
        if excerpt != node.raw_text:
            mismatches.append(
                {
                    "node_id": node.id,
                    "paragraph_index": node.paragraph_index,
                    "expected": node.raw_text[:60],
                    "actual": excerpt[:60],
                }
            )
    return {
        "checked": len(nodes),
        "mismatched": len(mismatches),
        "ok": not mismatches,
        "samples": mismatches[:5],
    }


def _speaker_summary(spans: Sequence[SourceSpan]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for span in spans:
        key = span.speaker or f"<{span.kind.value}>"
        summary[key] = summary.get(key, 0) + 1
    return summary


def _relative_to_repo(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def ensure_review_ready(report: ReviewReport) -> None:
    """结构与原文往返失败时不得继续启用。"""
    if not report.payload.get("roundtrip", {}).get("ok", False):
        raise NotReadyError(
            "原文往返定位失败，结构不可用",
            reason_code="structure_roundtrip_failed",
        )


# --------------------------------------------------------------------------- 构建


@dataclass(frozen=True)
class BuildOutcome:
    """一次索引构建的结果；不自动启用。"""

    index_id: str
    snapshot_hash: str
    manifest_hash: str
    artifact_path: Path
    unit_count: int
    monologue_status: str
    monologue_material_count: int
    metrics: Mapping[str, Any] = field(default_factory=dict)


def build_snapshot(
    *,
    database: Database,
    repository: StructureRepository,
    layout: RuntimeLayout,
    structure_id: str,
    split_profile,
    embedding_backend,
    output_dir: Path,
    config: RuntimeConfig | None = None,
) -> BuildOutcome:
    """按固定切分 profile 重新生成片段与向量，返回不可变快照。

    不复用试切分结果；参数变化必须新建版本，旧检索校准报告随之失效。
    """
    import hashlib

    import numpy as np

    from sana_bot.enums import MonologueStatus
    from sana_bot.ingest.models import (
        SPLITTER_VERSION,
        EmbeddingRecord,
        SemanticUnit,
        canonical_json,
    )
    from sana_bot.ingest.semantic_splitter import split_semantics
    from sana_bot.storage.index_repository import SnapshotRecord

    document = repository.load_structure(structure_id)
    approval = repository.approval(structure_id)
    if approval is None:
        raise NotReadyError(
            "结构尚未批准，不能构建可启用快照",
            reason_code="structure_not_approved",
        )
    if document.catalog_hash != approval["catalog_hash"]:
        raise NotReadyError(
            "结构的 catalog hash 与批准记录不一致，请重新批准后再构建",
            reason_code="catalog_hash_mismatch",
        )
    # 快照记录已批准 catalog 的 hash：与 snapshot_hash 的输入同源，便于回溯映射版本
    catalog_version = document.catalog_hash
    profile = split_profile
    if profile.structure_hash != document.structure_hash:
        raise ConfigError("切分 profile 与当前结构不一致，请重新校准切分参数")
    parameters = SplitterParameters(
        window_sentences=int(profile.parameters.get("window_sentences", 2)),
        boundary_similarity_threshold=float(
            profile.parameters.get("boundary_similarity_threshold", 0.55)
        ),
        max_unit_tokens=int(profile.parameters.get("max_unit_tokens", 384)),
        min_unit_tokens=int(profile.parameters.get("min_unit_tokens", 12)),
        splitter_version=str(profile.parameters.get("splitter_version", SPLITTER_VERSION)),
        calibrated=bool(profile.parameters.get("calibrated", False)),
    ).validate()

    embedding_backend.ensure_available()
    spec = embedding_backend.spec
    index_id = new_id()
    spans = repository.load_spans(structure_id)
    split_result = split_semantics(
        spans,
        index_version=index_id,
        embed=embedding_backend.embed_documents,
        parameters=parameters,
        count_tokens=embedding_backend.count_tokens,
    )
    units = split_result.units
    if not units:
        raise NotReadyError(
            "没有可用的语义单元，构建中止",
            reason_code="no_semantic_units",
        )

    keyed = _attach_metadata(units, document, spans=spans)
    texts = [unit.text for unit in keyed]
    vectors = embedding_backend.embed_documents(texts)
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.shape != (len(keyed), spec.dimension):
        raise NotReadyError(
            f"向量矩阵形状异常：{matrix.shape}",
            reason_code="embedding_matrix_shape_mismatch",
        )

    directory = Path(output_dir) / index_id
    directory.mkdir(parents=True, exist_ok=True)
    vectors_path = directory / "vectors.npy"
    np.save(vectors_path, matrix)
    units_path = directory / "units.json"
    units_path.write_text(
        json.dumps([unit.to_payload() for unit in keyed], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    records = tuple(
        EmbeddingRecord(
            unit_id=unit.id,
            model_id=spec.model_id,
            revision=spec.revision,
            dimension=spec.dimension,
            input_hash=hashlib.sha256(unit.text.encode("utf-8")).hexdigest(),
            token_count=unit.token_count,
            vector_offset=offset,
        )
        for offset, unit in enumerate(keyed)
    )
    material = [unit for unit in keyed if "monologue" in unit.content_types]
    monologue_status = (
        MonologueStatus.DISABLED_UNCALIBRATED if material else MonologueStatus.DISABLED_NO_MATERIAL
    )

    file_hashes = {
        "vectors.npy": _file_hash(vectors_path),
        "units.json": _file_hash(units_path),
    }
    snapshot_hash = _snapshot_hash(
        document=document,
        profile_hash=profile.profile_hash,
        spec_manifest=spec.to_manifest(),
        records=records,
        units=keyed,
    )
    manifest = {
        "index_id": index_id,
        "source_hash": document.source.file_hash,
        "structure_hash": document.structure_hash,
        "parser_version": document.parser_version,
        "split_profile_hash": profile.profile_hash,
        "splitter_parameters": parameters.to_payload(),
        "embedding_model_id": spec.model_id,
        "embedding_revision": spec.effective_revision,
        "provider_reported_model": getattr(embedding_backend, "reported_model", None),
        "dimension": spec.dimension,
        "catalog_version": catalog_version,
        "unit_count": len(keyed),
        "monologue_material_count": len(material),
        "monologue_status": monologue_status.value,
        "snapshot_hash": snapshot_hash,
        "file_hashes": file_hashes,
        "build_status": "staged",
        "split_metrics": dict(profile.metrics),
        "split_issues": [dict(item) for item in split_result.issues],
    }
    manifest_hash = hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
    manifest["manifest_hash"] = manifest_hash
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    now = to_utc_iso(utc_now())
    with database.write():
        # 先写快照再写依赖它的片段与向量记录，满足外键顺序
        repository.insert_snapshot(
            SnapshotRecord(
                id=index_id,
                source_hash=document.source.file_hash,
                structure_hash=document.structure_hash,
                split_profile_hash=profile.profile_hash,
                snapshot_hash=snapshot_hash,
                manifest_hash=manifest_hash,
                parser_version=document.parser_version,
                splitter_version=parameters.splitter_version,
                embedding_model_id=spec.model_id,
                embedding_revision=spec.effective_revision,
                dimension=spec.dimension,
                rule_catalog_version=catalog_version,
                reply_calibration_id=None,
                monologue_calibration_id=None,
                monologue_status=monologue_status,
                status=IndexStatus.STAGED,
                artifact_path=str(directory),
                manifest=manifest,
                created_at=now,
                updated_at=now,
            )
        )
        repository.save_semantic_units(index_id, keyed)
        repository.save_embedding_records(records, index_id=index_id)
    return BuildOutcome(
        index_id=index_id,
        snapshot_hash=snapshot_hash,
        manifest_hash=manifest_hash,
        artifact_path=directory,
        unit_count=len(keyed),
        monologue_status=monologue_status.value,
        monologue_material_count=len(material),
        metrics={
            **dict(split_result.metrics),
            "vector_shape": list(matrix.shape),
        },
    )


def _attach_metadata(units, document: StructuredDocument, *, spans=()) -> tuple[Any, ...]:
    """补上内容类型等元数据；语义单元本身不改写来源片段身份。"""
    node_types = {node.id: node.content_type.value for node in document.typed_nodes}
    span_kinds = {span.id: span.kind.value for span in spans}
    enriched = []
    for unit in units:
        content_types = tuple(
            dict.fromkeys(node_types.get(node_id, "") for node_id in unit.source_node_ids if node_id)
        )
        kinds = tuple(
            dict.fromkeys(
                span_kinds[span_id]
                for span_id in unit.source_span_ids
                if span_kinds.get(span_id) in ("dialogue", "monologue")
            )
        )
        content_types = tuple(content_types) + kinds
        enriched.append(
            type(unit)(
                id=unit.id,
                index_version=unit.index_version,
                source_span_ids=unit.source_span_ids,
                source_node_ids=unit.source_node_ids,
                speaker_labels=unit.speaker_labels,
                content_types=tuple(item for item in content_types if item),
                topic=unit.topic,
                text=unit.text,
                boundary_evidence=unit.boundary_evidence,
                context_links=unit.context_links,
                conditions=unit.conditions,
                token_count=unit.token_count,
                parent_unit_id=unit.parent_unit_id,
                offset_in_parent=unit.offset_in_parent,
            )
        )
    return tuple(enriched)


def _file_hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_hash(*, document, profile_hash, spec_manifest, records, units) -> str:
    """快照 hash 覆盖结构/目录/切分参数/模型 revision/片段与向量记录，不含校准报告。"""
    import hashlib

    from sana_bot.ingest.models import canonical_json

    return hashlib.sha256(
        canonical_json(
            {
                "source_hash": document.source.file_hash,
                "structure_hash": document.structure_hash,
                "catalog_hash": document.catalog_hash,
                "split_profile_hash": profile_hash,
                "embedding": dict(spec_manifest),
                # 不含随机主键，保证同一资料与参数下的快照 hash 可复现
                "units": [
                    {
                        "ordinal": ordinal,
                        "text_hash": _text_hash(unit.text),
                        "token_count": unit.token_count,
                        "source_span_ids": list(unit.source_span_ids),
                    }
                    for ordinal, unit in enumerate(units)
                ],
                "embeddings": [
                    {
                        "ordinal": record.vector_offset,
                        "model_id": record.model_id,
                        "revision": record.revision,
                        "dimension": record.dimension,
                        "input_hash": record.input_hash,
                        "token_count": record.token_count,
                    }
                    for record in records
                ],
            }
        ).encode("utf-8")
    ).hexdigest()


def _text_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 结构化路径


#: 解析期 token 估算：CJK 下按「一字一 token」保守上界，宁可多切不可超限。
def estimate_tokens(text: str) -> int:
    return max(1, len(text))


def _ensure_formal_source(source: Path) -> Path:
    source = Path(source)
    if source.name.startswith("~$"):
        raise ConfigError(
            f"Word 锁文件不是正式资料：{source.name}",
            reason_code="word_lock_file",
        )
    if not source.exists():
        raise ConfigError(f"资料文件不存在：{source}")
    return source


