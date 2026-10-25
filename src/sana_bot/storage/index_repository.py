"""索引仓储：不可变结构、片段、快照与校准报告的读写。

结构一旦落盘即不可变；批准记录单独保存结构 hash 与 catalog hash，便于核对
「批准的是哪一版」。快照与校准部分在后续阶段补齐。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sana_bot.enums import (
    IndexStatus,
    MonologueStatus,
    RetrievalStage,
    ReviewStatus,
    SpanKind,
)
from sana_bot.errors import ConfigError, NotReadyError
from sana_bot.ingest.models import (
    CHUNKING_STRATEGY_MARKDOWN,
    CHUNKING_STRATEGY_SEMANTIC,
    INDEX_FORMAT_SEMANTIC_V1,
    INDEX_FORMAT_STRUCTURAL_V2,
    EmbeddingRecord,
    SemanticUnit,
    SourceDocument,
    SourceSpan,
    StructuredDocument,
)
from sana_bot.storage.database import Database
from sana_bot.timeutil import to_utc_iso, utc_now

__all__ = [
    "StructureRepository",
    "SnapshotRecord",
    "SplitCalibrationProfileRecord",
    "CalibrationProfileRecord",
    "new_id",
    "validate_snapshot_format",
]


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class SplitCalibrationProfileRecord:
    id: str
    structure_hash: str
    model_revision: str
    sample_set_hash: str
    splitter_version: str
    parameters: dict[str, Any]
    profile_hash: str
    metrics: dict[str, Any]
    approved_at: str | None
    created_at: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "structure_hash": self.structure_hash,
            "model_revision": self.model_revision,
            "sample_set_hash": self.sample_set_hash,
            "splitter_version": self.splitter_version,
            "parameters": self.parameters,
            "profile_hash": self.profile_hash,
            "metrics": self.metrics,
            "approved_at": self.approved_at,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class CalibrationProfileRecord:
    id: str
    index_id: str
    snapshot_hash: str
    split_profile_hash: str
    stage: RetrievalStage
    sample_set_hash: str
    threshold: float | None
    score_metric: str
    metrics: dict[str, Any]
    approved_at: str | None
    created_at: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "index_id": self.index_id,
            "snapshot_hash": self.snapshot_hash,
            "split_profile_hash": self.split_profile_hash,
            "stage": self.stage.value,
            "sample_set_hash": self.sample_set_hash,
            "threshold": self.threshold,
            "score_metric": self.score_metric,
            "metrics": self.metrics,
            "approved_at": self.approved_at,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class SnapshotRecord:
    id: str
    source_hash: str
    structure_hash: str
    split_profile_hash: str | None
    snapshot_hash: str
    manifest_hash: str
    parser_version: str
    splitter_version: str
    embedding_model_id: str
    embedding_revision: str
    dimension: int
    rule_catalog_version: str
    reply_calibration_id: str | None
    monologue_calibration_id: str | None
    monologue_status: MonologueStatus
    status: IndexStatus
    artifact_path: str
    manifest: dict[str, Any]
    created_at: str
    updated_at: str
    #: 双格式标识；缺字段的旧快照按 semantic_v1 解释
    index_format_version: str = INDEX_FORMAT_SEMANTIC_V1
    chunking_strategy: str = CHUNKING_STRATEGY_SEMANTIC
    structural_profile_hash: str | None = None
    retrieval_policy_hash: str = ""
    bundle_hash: str | None = None
    sealed_at: str | None = None
    active_report_hash: str = ""
    approved_report_id: str | None = None

    @property
    def is_structural(self) -> bool:
        return (self.index_format_version or INDEX_FORMAT_SEMANTIC_V1) == INDEX_FORMAT_STRUCTURAL_V2

    @property
    def monologue_threshold_available(self) -> bool:
        return self.monologue_status is MonologueStatus.ENABLED

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_hash": self.source_hash,
            "structure_hash": self.structure_hash,
            "split_profile_hash": self.split_profile_hash,
            "snapshot_hash": self.snapshot_hash,
            "manifest_hash": self.manifest_hash,
            "parser_version": self.parser_version,
            "splitter_version": self.splitter_version,
            "embedding_model_id": self.embedding_model_id,
            "embedding_revision": self.embedding_revision,
            "dimension": self.dimension,
            "rule_catalog_version": self.rule_catalog_version,
            "reply_calibration_id": self.reply_calibration_id,
            "monologue_calibration_id": self.monologue_calibration_id,
            "monologue_status": self.monologue_status.value,
            "status": self.status.value,
            "artifact_path": self.artifact_path,
            "manifest": self.manifest,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "index_format_version": self.index_format_version,
            "chunking_strategy": self.chunking_strategy,
            "structural_profile_hash": self.structural_profile_hash,
            "retrieval_policy_hash": self.retrieval_policy_hash,
            "bundle_hash": self.bundle_hash,
            "sealed_at": self.sealed_at,
            "active_report_hash": self.active_report_hash,
            "approved_report_id": self.approved_report_id,
        }


def validate_snapshot_format(record: SnapshotRecord) -> None:
    """双格式校验：语义快照必须带切分配置，结构快照必须带包级哈希。

    缺格式字段的旧库按 ``semantic_v1`` 解释；未知格式明确拒绝。
    """
    version = record.index_format_version or INDEX_FORMAT_SEMANTIC_V1
    if version == INDEX_FORMAT_SEMANTIC_V1:
        if not record.split_profile_hash:
            raise NotReadyError(
                f"语义快照缺少 split_profile_hash：{record.id}",
                reason_code="snapshot_format_inconsistent",
            )
        if record.chunking_strategy not in ("", CHUNKING_STRATEGY_SEMANTIC):
            raise NotReadyError(
                f"语义快照的切分策略不合法：{record.chunking_strategy}",
                reason_code="snapshot_format_inconsistent",
            )
        return
    if version != INDEX_FORMAT_STRUCTURAL_V2:
        raise NotReadyError(
            f"未知索引格式：{version}",
            reason_code="unknown_index_format",
        )
    if record.split_profile_hash:
        raise NotReadyError(
            f"结构快照不应带 split_profile_hash：{record.id}",
            reason_code="snapshot_format_inconsistent",
        )
    if not record.structural_profile_hash:
        raise NotReadyError(
            f"结构快照缺少 structural_profile_hash：{record.id}",
            reason_code="snapshot_format_inconsistent",
        )
    if not record.retrieval_policy_hash:
        raise NotReadyError(
            f"结构快照缺少 retrieval_policy_hash：{record.id}",
            reason_code="snapshot_format_inconsistent",
        )
    if record.chunking_strategy not in ("", CHUNKING_STRATEGY_MARKDOWN):
        raise NotReadyError(
            f"结构快照的切分策略不合法：{record.chunking_strategy}",
            reason_code="snapshot_format_inconsistent",
        )


class StructureRepository:
    """结构、节点、片段与批准记录的持久化。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    # ------------------------------------------------------------------ 写入

    def save_structure(self, document: StructuredDocument, *, output_dir: Path) -> Path:
        """落盘结构（数据库 + 不可变 JSON），返回结构目录。"""
        directory = Path(output_dir) / document.id
        directory.mkdir(parents=True, exist_ok=True)
        now = to_utc_iso(utc_now())
        with self.database.write():
            self.database.execute(
                """
                INSERT INTO source_document (id, relative_path, file_hash, imported_at, parser_version)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    document.source.id,
                    document.source.relative_path,
                    document.source.file_hash,
                    document.source.imported_at,
                    document.source.parser_version,
                ),
            )
            self.database.execute(
                """
                INSERT INTO structured_document
                    (id, source_id, structure_hash, parser_version, catalog_hash, review_status,
                     body_text, coverage, issues, heading_tree, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document.id,
                    document.source.id,
                    document.structure_hash,
                    document.parser_version,
                    document.catalog_hash,
                    document.review_status.value,
                    document.body_text,
                    json.dumps(document.coverage, ensure_ascii=False),
                    json.dumps(list(document.issues), ensure_ascii=False),
                    json.dumps(list(document.heading_tree), ensure_ascii=False),
                    now,
                ),
            )
            for node in document.typed_nodes:
                self.database.execute(
                    """
                    INSERT INTO rule_node
                        (id, document_id, parent_id, heading_path, paragraph_index, char_start,
                         char_end, list_level, field_type, content_type, raw_text, scope,
                         conditions, speaker, provenance, review_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node.id,
                        node.document_id,
                        node.parent_id,
                        json.dumps(list(node.heading_path), ensure_ascii=False),
                        node.paragraph_index,
                        node.char_start,
                        node.char_end,
                        node.list_level,
                        node.field_type.value if node.field_type else None,
                        node.content_type.value,
                        node.raw_text,
                        node.scope,
                        json.dumps(dict(node.conditions), ensure_ascii=False),
                        node.speaker,
                        node.provenance.value,
                        node.review_status.value,
                    ),
                )
        artifact = directory / "structure.json"
        _write_json(artifact, document.to_payload())
        return directory

    def save_spans(self, structure_id: str, spans: Sequence[SourceSpan]) -> Path:
        with self.database.write():
            self.database.execute("DELETE FROM source_span WHERE structure_id = ?", (structure_id,))
            for span in spans:
                self.database.execute(
                    """
                    INSERT INTO source_span
                        (id, structure_id, node_id, kind, raw_text, source_start, source_end,
                         speaker, speaker_label, segment_index, previous_id, next_id, parent_span_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        span.id,
                        structure_id,
                        span.node_id,
                        span.kind.value,
                        span.raw_text,
                        span.source_start,
                        span.source_end,
                        span.speaker,
                        span.speaker_label,
                        span.segment_index,
                        span.previous_id,
                        span.next_id,
                        span.parent_span_id,
                    ),
                )
        return Path(structure_id)

    def record_approval(
        self,
        *,
        structure_id: str,
        structure_hash: str,
        catalog_hash: str,
        catalog_path: str,
        reviewer: str,
    ) -> None:
        with self.database.write():
            self.database.execute(
                """
                INSERT OR REPLACE INTO structure_approval
                    (structure_id, structure_hash, catalog_hash, catalog_path, reviewer, approved_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    structure_id,
                    structure_hash,
                    catalog_hash,
                    catalog_path,
                    reviewer,
                    to_utc_iso(utc_now()),
                ),
            )

    def approval(self, structure_id: str) -> dict[str, Any] | None:
        row = self.database.query_one(
            "SELECT * FROM structure_approval WHERE structure_id = ? ORDER BY approved_at DESC LIMIT 1",
            (structure_id,),
        )
        return dict(row) if row else None

    def list_structures(self) -> list[dict[str, Any]]:
        rows = self.database.query(
            "SELECT id, structure_hash, parser_version, review_status, created_at FROM structured_document ORDER BY created_at DESC"
        )
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ 读取

    def load_structure(self, structure_id: str) -> StructuredDocument:
        row = self.database.query_one("SELECT * FROM structured_document WHERE id = ?", (structure_id,))
        if row is None:
            raise NotReadyError(f"结构不存在：{structure_id}", reason_code="structure_missing")
        source_row = self.database.query_one(
            "SELECT * FROM source_document WHERE id = ?", (row["source_id"],)
        )
        if source_row is None:  # pragma: no cover - 外键保证存在
            raise NotReadyError(f"来源文档缺失：{row['source_id']}", reason_code="source_missing")
        node_rows = self.database.query(
            "SELECT * FROM rule_node WHERE document_id = ? ORDER BY paragraph_index, char_start",
            (structure_id,),
        )
        from sana_bot.ingest.models import RuleNode

        nodes = tuple(
            RuleNode.from_payload(
                {
                    "id": item["id"],
                    "document_id": item["document_id"],
                    "parent_id": item["parent_id"],
                    "heading_path": json.loads(item["heading_path"]),
                    "paragraph_index": item["paragraph_index"],
                    "char_start": item["char_start"],
                    "char_end": item["char_end"],
                    "raw_text": item["raw_text"],
                    "content_type": item["content_type"],
                    "field_type": item["field_type"],
                    "list_level": item["list_level"],
                    "scope": item["scope"],
                    "conditions": json.loads(item["conditions"]),
                    "speaker": item["speaker"],
                    "provenance": item["provenance"],
                    "review_status": item["review_status"],
                }
            )
            for item in node_rows
        )
        return StructuredDocument(
            id=row["id"],
            source=SourceDocument(
                id=source_row["id"],
                relative_path=source_row["relative_path"],
                file_hash=source_row["file_hash"],
                imported_at=source_row["imported_at"],
                parser_version=source_row["parser_version"],
            ),
            structure_hash=row["structure_hash"],
            parser_version=row["parser_version"],
            typed_nodes=nodes,
            heading_tree=tuple(json.loads(row["heading_tree"])),
            catalog_hash=row["catalog_hash"],
            review_status=ReviewStatus(row["review_status"]),
            issues=tuple(json.loads(row["issues"])),
            body_text=row["body_text"],
            coverage=json.loads(row["coverage"]),
        )

    def load_spans(self, structure_id: str) -> tuple[SourceSpan, ...]:
        rows = self.database.query(
            """
            SELECT * FROM source_span WHERE structure_id = ?
            ORDER BY source_start, segment_index
            """,
            (structure_id,),
        )
        return tuple(
            SourceSpan(
                id=row["id"],
                node_id=row["node_id"],
                kind=SpanKind(row["kind"]),
                raw_text=row["raw_text"],
                source_start=row["source_start"],
                source_end=row["source_end"],
                speaker=row["speaker"],
                previous_id=row["previous_id"],
                next_id=row["next_id"],
                parent_span_id=row["parent_span_id"],
                segment_index=row["segment_index"],
                speaker_label=row["speaker_label"],
            )
            for row in rows
        )

    # ------------------------------------------------------------------ 快照

    def insert_snapshot(self, record: SnapshotRecord) -> None:
        validate_snapshot_format(record)
        with self.database.write():
            self.database.execute(
                """
                INSERT INTO index_snapshot
                    (id, source_hash, structure_hash, split_profile_hash, snapshot_hash,
                     manifest_hash, parser_version, splitter_version, embedding_model_id,
                     embedding_revision, dimension, rule_catalog_version,
                     reply_calibration_id, monologue_calibration_id, monologue_status,
                     status, artifact_path, manifest, created_at, updated_at,
                     index_format_version, chunking_strategy, structural_profile_hash,
                     retrieval_policy_hash, bundle_hash, sealed_at, active_report_hash,
                     approved_report_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.source_hash,
                    record.structure_hash,
                    record.split_profile_hash,
                    record.snapshot_hash,
                    record.manifest_hash,
                    record.parser_version,
                    record.splitter_version,
                    record.embedding_model_id,
                    record.embedding_revision,
                    record.dimension,
                    record.rule_catalog_version,
                    record.reply_calibration_id,
                    record.monologue_calibration_id,
                    record.monologue_status.value,
                    record.status.value,
                    record.artifact_path,
                    json.dumps(record.manifest, ensure_ascii=False),
                    record.created_at,
                    record.updated_at,
                    record.index_format_version,
                    record.chunking_strategy,
                    record.structural_profile_hash,
                    record.retrieval_policy_hash,
                    record.bundle_hash,
                    record.sealed_at,
                    record.active_report_hash,
                    record.approved_report_id,
                ),
            )

    def snapshot(self, index_id: str) -> SnapshotRecord:
        row = self.database.query_one("SELECT * FROM index_snapshot WHERE id = ?", (index_id,))
        if row is None:
            raise NotReadyError(f"索引不存在：{index_id}", reason_code="index_missing")
        return self._snapshot_from_row(row)

    def active_snapshot(self) -> SnapshotRecord | None:
        row = self.database.query_one(
            "SELECT * FROM index_snapshot WHERE status = 'active' LIMIT 1"
        )
        return self._snapshot_from_row(row) if row else None

    def snapshots_for_structure(self, structure_hash: str) -> list[SnapshotRecord]:
        rows = self.database.query(
            "SELECT * FROM index_snapshot WHERE structure_hash = ? ORDER BY created_at DESC",
            (structure_hash,),
        )
        return [self._snapshot_from_row(row) for row in rows]

    def set_snapshot_status(self, index_id: str, status: IndexStatus) -> None:
        self.database.execute(
            "UPDATE index_snapshot SET status = ?, updated_at = ? WHERE id = ?",
            (status.value, to_utc_iso(utc_now()), index_id),
        )

    def save_semantic_units(self, index_id: str, units: Sequence[SemanticUnit]) -> None:
        for unit in units:
            self.database.execute(
                """
                INSERT OR REPLACE INTO semantic_unit
                    (id, index_id, source_span_ids, source_node_ids, speaker_labels, content_types,
                     topic, text, boundary_evidence, context_links, conditions, token_count,
                     parent_unit_id, offset_in_parent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    unit.id,
                    index_id,
                    json.dumps(list(unit.source_span_ids), ensure_ascii=False),
                    json.dumps(list(unit.source_node_ids), ensure_ascii=False),
                    json.dumps(list(unit.speaker_labels), ensure_ascii=False),
                    json.dumps(list(unit.content_types), ensure_ascii=False),
                    unit.topic,
                    unit.text,
                    json.dumps(dict(unit.boundary_evidence), ensure_ascii=False),
                    json.dumps(list(unit.context_links), ensure_ascii=False),
                    json.dumps(dict(unit.conditions), ensure_ascii=False),
                    unit.token_count,
                    unit.parent_unit_id,
                    unit.offset_in_parent,
                ),
            )

    def save_embedding_records(self, records: Sequence[EmbeddingRecord], *, index_id: str) -> None:
        for record in records:
            self.database.execute(
                """
                INSERT OR REPLACE INTO embedding_record
                    (unit_id, index_id, model_id, revision, dimension, normalized, input_hash,
                     token_count, vector_offset)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.unit_id,
                    index_id,
                    record.model_id,
                    record.revision,
                    record.dimension,
                    1 if record.normalized else 0,
                    record.input_hash,
                    record.token_count,
                    record.vector_offset,
                ),
            )

    def load_semantic_units(self, index_id: str) -> tuple[SemanticUnit, ...]:
        rows = self.database.query("SELECT * FROM semantic_unit WHERE index_id = ?", (index_id,))
        return tuple(
            SemanticUnit(
                id=row["id"],
                index_version=index_id,
                source_span_ids=tuple(json.loads(row["source_span_ids"])),
                source_node_ids=tuple(json.loads(row["source_node_ids"])),
                speaker_labels=tuple(json.loads(row["speaker_labels"])),
                content_types=tuple(json.loads(row["content_types"])),
                topic=row["topic"],
                text=row["text"],
                boundary_evidence=json.loads(row["boundary_evidence"]),
                context_links=tuple(json.loads(row["context_links"])),
                conditions=json.loads(row["conditions"]),
                token_count=row["token_count"],
                parent_unit_id=row["parent_unit_id"],
                offset_in_parent=row["offset_in_parent"],
            )
            for row in rows
        )

    def load_embedding_records(self, index_id: str) -> tuple[EmbeddingRecord, ...]:
        rows = self.database.query(
            "SELECT * FROM embedding_record WHERE index_id = ? ORDER BY vector_offset",
            (index_id,),
        )
        return tuple(
            EmbeddingRecord(
                unit_id=row["unit_id"],
                model_id=row["model_id"],
                revision=row["revision"],
                dimension=row["dimension"],
                normalized=bool(row["normalized"]),
                input_hash=row["input_hash"],
                token_count=row["token_count"],
                vector_offset=row["vector_offset"],
            )
            for row in rows
        )

    # ------------------------------------------------------------------ 校准

    def insert_split_profile(self, record: SplitCalibrationProfileRecord) -> None:
        with self.database.write():
            self.database.execute(
                """
                INSERT INTO split_calibration_profile
                    (id, structure_hash, model_revision, sample_set_hash, splitter_version,
                     parameters, profile_hash, metrics, approved_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.structure_hash,
                    record.model_revision,
                    record.sample_set_hash,
                    record.splitter_version,
                    json.dumps(record.parameters, ensure_ascii=False),
                    record.profile_hash,
                    json.dumps(record.metrics, ensure_ascii=False),
                    record.approved_at,
                    record.created_at,
                ),
            )

    def split_profile(self, profile_id: str) -> SplitCalibrationProfileRecord:
        row = self.database.query_one(
            "SELECT * FROM split_calibration_profile WHERE id = ?", (profile_id,)
        )
        if row is None:
            raise NotReadyError(f"切分 profile 不存在：{profile_id}", reason_code="split_profile_missing")
        return SplitCalibrationProfileRecord(
            id=row["id"],
            structure_hash=row["structure_hash"],
            model_revision=row["model_revision"],
            sample_set_hash=row["sample_set_hash"],
            splitter_version=row["splitter_version"],
            parameters=json.loads(row["parameters"]),
            profile_hash=row["profile_hash"],
            metrics=json.loads(row["metrics"]),
            approved_at=row["approved_at"],
            created_at=row["created_at"],
        )

    def split_profile_by_hash(self, profile_hash: str) -> SplitCalibrationProfileRecord | None:
        row = self.database.query_one(
            "SELECT * FROM split_calibration_profile WHERE profile_hash = ? LIMIT 1",
            (profile_hash,),
        )
        if row is None:
            return None
        return self.split_profile(row["id"])

    def insert_calibration(self, record: CalibrationProfileRecord) -> None:
        with self.database.write():
            self.database.execute(
                """
                INSERT OR REPLACE INTO calibration_profile
                    (id, index_id, snapshot_hash, split_profile_hash, stage, sample_set_hash,
                     threshold, score_metric, metrics, approved_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.index_id,
                    record.snapshot_hash,
                    record.split_profile_hash,
                    record.stage.value,
                    record.sample_set_hash,
                    record.threshold,
                    record.score_metric,
                    json.dumps(record.metrics, ensure_ascii=False),
                    record.approved_at,
                    record.created_at,
                ),
            )

    def calibration(self, index_id: str, stage: RetrievalStage) -> CalibrationProfileRecord | None:
        row = self.database.query_one(
            "SELECT * FROM calibration_profile WHERE index_id = ? AND stage = ? LIMIT 1",
            (index_id, stage.value),
        )
        if row is None:
            return None
        return CalibrationProfileRecord(
            id=row["id"],
            index_id=row["index_id"],
            snapshot_hash=row["snapshot_hash"],
            split_profile_hash=row["split_profile_hash"],
            stage=RetrievalStage(row["stage"]),
            sample_set_hash=row["sample_set_hash"],
            threshold=row["threshold"],
            score_metric=row["score_metric"],
            metrics=json.loads(row["metrics"]),
            approved_at=row["approved_at"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _snapshot_from_row(row: Any) -> SnapshotRecord:
        return SnapshotRecord(
            id=row["id"],
            source_hash=row["source_hash"],
            structure_hash=row["structure_hash"],
            split_profile_hash=row["split_profile_hash"],
            snapshot_hash=row["snapshot_hash"],
            manifest_hash=row["manifest_hash"],
            parser_version=row["parser_version"],
            splitter_version=row["splitter_version"],
            embedding_model_id=row["embedding_model_id"],
            embedding_revision=row["embedding_revision"],
            dimension=row["dimension"],
            rule_catalog_version=row["rule_catalog_version"],
            reply_calibration_id=row["reply_calibration_id"],
            monologue_calibration_id=row["monologue_calibration_id"],
            monologue_status=MonologueStatus(row["monologue_status"]),
            status=IndexStatus(row["status"]),
            artifact_path=row["artifact_path"],
            manifest=json.loads(row["manifest"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            index_format_version=_row_value(
                row, "index_format_version", INDEX_FORMAT_SEMANTIC_V1
            ),
            chunking_strategy=_row_value(
                row, "chunking_strategy", CHUNKING_STRATEGY_SEMANTIC
            ),
            structural_profile_hash=_row_value(row, "structural_profile_hash", None),
            retrieval_policy_hash=_row_value(row, "retrieval_policy_hash", "") or "",
            bundle_hash=_row_value(row, "bundle_hash", None),
            sealed_at=_row_value(row, "sealed_at", None),
            active_report_hash=_row_value(row, "active_report_hash", "") or "",
            approved_report_id=_row_value(row, "approved_report_id", None),
        )


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    """从 sqlite3.Row/映射里取值；旧库可能还没有新列。"""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None and default is not None else value


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
