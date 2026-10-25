"""资料包仓储：多来源关联、常驻材料、完整卡与不可变验收报告。

结构一旦落盘即不可变；批准单独记录哈希，便于核对「批准的是哪一版」。
报告同样只追加不覆盖：同一 index/stage 允许多份修订，活动包固定引用其中一份。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sana_bot.errors import ConfigError, NotReadyError
from sana_bot.ingest.package_models import (
    CardCondition,
    CharacterPackage,
    PackageConfig,
    ReferenceCard,
    ResidentMaterial,
    SourceFileVersion,
    SourceRange,
    SourceRole,
    canonical_hash,
)
from sana_bot.storage.database import Database
from sana_bot.timeutil import to_utc_iso, utc_now

__all__ = [
    "PackageView",
    "PackageCalibrationReport",
    "PackageRegressionReport",
    "ActivationRecord",
    "PackageRepository",
    "PackageReportRepository",
    "ActivationRepository",
    "load_config_payload",
]

_SAFE = object()


@dataclass(frozen=True)
class PackageView:
    """从数据库重建的资料包视图；只读，供运行时与校验器使用。"""

    structure_id: str
    config: PackageConfig
    sources: tuple[SourceFileVersion, ...]
    residents: tuple[ResidentMaterial, ...]
    cards: tuple[ReferenceCard, ...]
    source_set_hash: str
    structure_hash: str
    profile_hash: str
    policy_hash: str
    catalog_hash: str
    review_status: str
    policy: dict[str, Any]

    def resident(self, resident_id: str) -> ResidentMaterial | None:
        for item in self.residents:
            if item.resident_id == resident_id:
                return item
        return None

    def card(self, card_id: str) -> ReferenceCard | None:
        for item in self.cards:
            if item.card_id == card_id:
                return item
        return None

    @property
    def encoded_cards(self) -> tuple[ReferenceCard, ...]:
        return tuple(card for card in self.cards if card.encoded)

    def condition_for(self, card_id: str) -> CardCondition | None:
        return self.config.card_conditions.get(card_id)


def load_config_payload(payload: dict[str, Any]) -> PackageConfig:
    """把持久化的配置 JSON 还原为 :class:`PackageConfig`（不做源文件校验）。"""
    conditions = {
        card_id: CardCondition(
            card_id=card_id,
            relationship=str(item.get("relationship", "any")),
            scene=str(item.get("scene", "any")),
            medium=str(item.get("medium", "any")),
            state_tags=tuple(str(tag) for tag in item.get("state_tags", ())),
            card_states=tuple(str(state) for state in item.get("card_states", ())),
            resident=bool(item.get("resident", False)),
        )
        for card_id, item in (payload.get("card_conditions") or {}).items()
    }
    included = tuple(
        (str(item["path"]), SourceRole(str(item["role"])))
        for item in payload.get("included_files", ())
    )
    return PackageConfig(
        format_version=str(payload.get("format_version", "structural_v2")),
        chunking_strategy=str(payload.get("chunking_strategy", "markdown_cards_v1")),
        character=str(payload.get("character", "sana")),
        story_stage=str(payload.get("story_stage", "")),
        required_relationship=str(payload.get("required_relationship", "romantic")),
        source_root=str(payload.get("source_root", "")),
        profile_version=str(payload.get("profile_version", "")),
        policy_version=str(payload.get("policy_version", "")),
        catalog_version=str(payload.get("catalog_version", "")),
        included_files=included,
        excluded_files=tuple(str(item) for item in payload.get("excluded_files", ())),
        resident_ids=tuple(str(item) for item in payload.get("resident_ids", ())),
        resident_sources={
            str(key): str(value) for key, value in (payload.get("resident_sources") or {}).items()
        },
        card_conditions=conditions,
        state_catalog=dict(payload.get("state_catalog") or {}),
        budgets=dict(payload.get("budgets") or {}),
        retrieval_policy=dict(payload.get("retrieval_policy") or {}),
        expected_counts=dict(payload.get("expected_counts") or {}),
        config_hash=str(payload.get("config_hash", "")),
    )


class PackageRepository:
    """资料包结构与来源的持久化。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    # ------------------------------------------------------------------ 写入

    def save_package(self, package: CharacterPackage, *, structure_id: str) -> None:
        """保存六份来源、常驻、完整卡与逐卡结构行；一个卡一个 group/field/leaf。"""
        now = to_utc_iso(utc_now())
        profile_hash = package.compute_profile_hash()
        policy = package.config.policy()
        policy_hash = policy.policy_hash
        catalog_hash = policy.catalog_hash
        with self.database.write():
            root_source_id = self._insert_source_document(
                relative_path=package.config.source_root,
                file_hash=package.source_set_hash,
                parser_version="sana-package-1.0.0",
                imported_at=now,
            )
            self.database.execute(
                """
                INSERT INTO structural_document
                    (id, source_id, structure_hash, profile_hash, parser_version,
                     review_status, catalog_hash, body_text, coverage, issues, heading_tree,
                     created_at, package_source_set_hash, package_profile_hash,
                     package_policy_hash, package_catalog_hash, package_config)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    structure_id,
                    root_source_id,
                    package.structure_hash,
                    profile_hash,
                    "sana-package-1.0.0",
                    "pending",
                    catalog_hash,
                    "",
                    json.dumps(
                        {
                            "format_version": package.config.format_version,
                            "chunking_strategy": package.config.chunking_strategy,
                            "resident_count": len(package.residents),
                            "card_count": len(package.cards),
                            "encoded_count": len(package.encoded_cards),
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps([issue.to_payload() for issue in package.issues], ensure_ascii=False),
                    json.dumps(
                        [
                            {"heading": card.heading_path[-1], "card_id": card.card_id}
                            for card in package.cards
                        ],
                        ensure_ascii=False,
                    ),
                    now,
                    package.source_set_hash,
                    profile_hash,
                    policy_hash,
                    catalog_hash,
                    json.dumps(package.config.to_payload(), ensure_ascii=False),
                ),
            )
            for ordinal, source in enumerate(package.sources):
                document_id = self._insert_source_document(
                    relative_path=source.relative_path,
                    file_hash=source.byte_hash,
                    parser_version="sana-package-1.0.0",
                    imported_at=now,
                )
                self.database.execute(
                    """
                    INSERT OR REPLACE INTO package_source
                        (structure_id, source_key, source_document_id, relative_path, role,
                         byte_hash, included, excluded_reason, source_note, raw_text, ordinal)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        structure_id,
                        source.source_id,
                        document_id,
                        source.relative_path,
                        source.role.value,
                        source.byte_hash,
                        1 if source.included else 0,
                        source.excluded_reason,
                        source.source_note,
                        source.raw_text,
                        ordinal,
                    ),
                )
            for resident in package.residents:
                self.database.execute(
                    """
                    INSERT OR REPLACE INTO package_resident
                        (structure_id, resident_id, ordinal, title, raw_text, source_key,
                         source_ranges, content_hash, required, token_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        structure_id,
                        resident.resident_id,
                        resident.ordinal,
                        resident.title,
                        resident.raw_text,
                        resident.source_id,
                        json.dumps([item.to_payload() for item in resident.source_ranges], ensure_ascii=False),
                        resident.content_hash or resident.compute_hash(),
                        1 if resident.required else 0,
                        resident.token_count,
                    ),
                )
            for ordinal, card in enumerate(package.cards):
                self.database.execute(
                    """
                    INSERT OR REPLACE INTO package_card
                        (structure_id, card_id, card_type, ordinal, payload)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        structure_id,
                        card.card_id,
                        card.type.value,
                        ordinal,
                        json.dumps(card.to_payload(), ensure_ascii=False),
                    ),
                )
                if card.encoded:
                    self._insert_card_structure(structure_id, card)

    def _insert_source_document(
        self, *, relative_path: str, file_hash: str, parser_version: str, imported_at: str
    ) -> str:
        from sana_bot.storage.index_repository import new_id

        existing = self.database.query_one(
            """
            SELECT id FROM source_document
            WHERE relative_path = ? AND file_hash = ? AND parser_version = ?
            LIMIT 1
            """,
            (relative_path, file_hash, parser_version),
        )
        if existing is not None:
            return str(existing["id"])
        document_id = new_id()
        self.database.execute(
            """
            INSERT INTO source_document (id, relative_path, file_hash, imported_at, parser_version)
            VALUES (?, ?, ?, ?, ?)
            """,
            (document_id, relative_path, file_hash, imported_at, parser_version),
        )
        return document_id

    def _insert_card_structure(self, structure_id: str, card: ReferenceCard) -> None:
        """一个卡 → 一个 group、一个完整正文 field、一个 leaf；``token_split=0``。"""
        group_key = f"card::{card.card_id}"
        field_key = f"{group_key}::body"
        leaf_key = f"{field_key}::leaf"
        conditions = json.dumps(dict(card.effective_conditions), ensure_ascii=False)
        content_types = json.dumps([card.type.value], ensure_ascii=False)
        speakers = json.dumps(
            [turn.speaker for turn in card.speaker_turns], ensure_ascii=False
        )
        self.database.execute(
            """
            INSERT OR REPLACE INTO structural_group
                (structure_id, group_key, group_id, heading_path, occurrence, group_kind,
                 parent_group_key, source_ranges, field_keys, constraint_refs,
                 effective_conditions, unresolved_fields)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                structure_id,
                group_key,
                card.card_id,
                json.dumps(list(card.heading_path), ensure_ascii=False),
                0,
                card.type.value,
                None,
                json.dumps([item.to_payload() for item in card.source_ranges], ensure_ascii=False),
                json.dumps([field_key], ensure_ascii=False),
                json.dumps(list(card.constraint_refs), ensure_ascii=False),
                conditions,
                json.dumps([], ensure_ascii=False),
            ),
        )
        self.database.execute(
            """
            INSERT OR REPLACE INTO structural_field
                (structure_id, field_key, group_key, field_type, original_label, raw_text,
                 source_node_ids, source_ranges, speaker_turns, field_conditions,
                 explicit_links, provenance)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                structure_id,
                field_key,
                group_key,
                card.type.value,
                card.heading_path[-1] if card.heading_path else card.card_id,
                card.raw_text,
                json.dumps([], ensure_ascii=False),
                json.dumps([item.to_payload() for item in card.source_ranges], ensure_ascii=False),
                json.dumps([turn.to_payload() for turn in card.speaker_turns], ensure_ascii=False),
                conditions,
                json.dumps(list(card.constraint_refs), ensure_ascii=False),
                card.provenance.value,
            ),
        )
        self.database.execute(
            """
            INSERT OR REPLACE INTO structural_leaf
                (structure_id, leaf_key, group_key, field_key, ordinal, raw_text,
                 offset_start, offset_end, content_types, speaker_labels,
                 effective_conditions, eligible_stages, token_split)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                structure_id,
                leaf_key,
                group_key,
                field_key,
                0,
                card.raw_text,
                0,
                len(card.raw_text),
                content_types,
                speakers,
                conditions,
                json.dumps(["reply"]),
                0,
            ),
        )

    def save_structural_embeddings(
        self,
        *,
        index_id: str,
        structure_id: str,
        cards: Sequence[ReferenceCard],
        model_id: str,
        revision: str,
        dimension: int,
        encoder_config_hash: str,
    ) -> None:
        for offset, card in enumerate(cards):
            import hashlib

            self.database.execute(
                """
                INSERT OR REPLACE INTO structural_embedding
                    (index_id, leaf_key, vector_offset, dimension, model_id,
                     effective_revision, encoder_config_hash, input_hash,
                     input_token_count, token_estimate)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    index_id,
                    f"card::{card.card_id}::body::leaf",
                    offset,
                    dimension,
                    model_id,
                    revision,
                    encoder_config_hash,
                    hashlib.sha256(card.embedding_input.encode("utf-8")).hexdigest(),
                    card.token_count,
                    card.token_count,
                ),
            )

    def record_approval(
        self,
        *,
        structure_id: str,
        structure_hash: str,
        catalog_hash: str,
        policy_hash: str,
        profile_hash: str,
        catalog_path: str,
        reviewer: str,
    ) -> None:
        now = to_utc_iso(utc_now())
        with self.database.write():
            self.database.execute(
                """
                INSERT OR REPLACE INTO structural_approval
                    (structure_id, structure_hash, catalog_hash, policy_hash, profile_hash,
                     catalog_path, reviewer, approved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    structure_id,
                    structure_hash,
                    catalog_hash,
                    policy_hash,
                    profile_hash,
                    catalog_path,
                    reviewer,
                    now,
                ),
            )
            self.database.execute(
                "UPDATE structural_document SET review_status = 'approved' WHERE id = ?",
                (structure_id,),
            )

    # ------------------------------------------------------------------ 读取

    def approval(self, structure_id: str) -> dict[str, Any] | None:
        row = self.database.query_one(
            "SELECT * FROM structural_approval WHERE structure_id = ? LIMIT 1",
            (structure_id,),
        )
        return dict(row) if row is not None else None

    def view(self, structure_id: str) -> PackageView:
        row = self.database.query_one(
            "SELECT * FROM structural_document WHERE id = ?", (structure_id,)
        )
        if row is None:
            raise NotReadyError(f"资料包结构不存在：{structure_id}", reason_code="structure_missing")
        payload = json.loads(row["package_config"] or "{}")
        if not payload:
            raise ConfigError(
                f"结构 {structure_id} 不是资料包格式",
                reason_code="unknown_index_format",
            )
        config = load_config_payload(payload)
        sources = tuple(
            SourceFileVersion(
                source_id=str(item["source_key"]),
                relative_path=str(item["relative_path"]),
                role=SourceRole(str(item["role"])),
                byte_hash=str(item["byte_hash"]),
                raw_text=str(item["raw_text"]),
                included=bool(item["included"]),
                excluded_reason=str(item["excluded_reason"]),
                source_note=str(item["source_note"]),
            )
            for item in self.database.query(
                "SELECT * FROM package_source WHERE structure_id = ? ORDER BY ordinal",
                (structure_id,),
            )
        )
        residents = tuple(
            ResidentMaterial(
                resident_id=str(item["resident_id"]),
                ordinal=int(item["ordinal"]),
                title=str(item["title"]),
                raw_text=str(item["raw_text"]),
                source_id=str(item["source_key"]),
                source_ranges=tuple(
                    SourceRange(
                        file=str(range_["file"]),
                        heading_path=tuple(range_.get("heading_path", ())),
                        line_start=int(range_["line_start"]),
                        line_end=int(range_["line_end"]),
                        char_start=int(range_["char_start"]),
                        char_end=int(range_["char_end"]),
                    )
                    for range_ in json.loads(item["source_ranges"] or "[]")
                ),
                required=bool(item["required"]),
                content_hash=str(item["content_hash"]),
                token_count=int(item["token_count"]),
            )
            for item in self.database.query(
                "SELECT * FROM package_resident WHERE structure_id = ? ORDER BY ordinal",
                (structure_id,),
            )
        )
        cards = tuple(
            ReferenceCard.from_payload(json.loads(item["payload"]))
            for item in self.database.query(
                "SELECT * FROM package_card WHERE structure_id = ? ORDER BY ordinal",
                (structure_id,),
            )
        )
        return PackageView(
            structure_id=structure_id,
            config=config,
            sources=sources,
            residents=residents,
            cards=cards,
            source_set_hash=str(row["package_source_set_hash"]),
            structure_hash=str(row["structure_hash"]),
            profile_hash=str(row["package_profile_hash"]),
            policy_hash=str(row["package_policy_hash"]),
            catalog_hash=str(row["package_catalog_hash"]),
            review_status=str(row["review_status"]),
            policy=config.policy().to_payload(),
        )

    def list_structures(self) -> list[dict[str, Any]]:
        rows = self.database.query(
            """
            SELECT id, structure_hash, review_status, created_at, package_source_set_hash,
                   package_profile_hash, package_policy_hash, package_catalog_hash, package_config
            FROM structural_document
            ORDER BY created_at DESC
            """
        )
        return [
            {
                "id": str(row["id"]),
                "structure_hash": str(row["structure_hash"]),
                "review_status": str(row["review_status"]),
                "created_at": str(row["created_at"]),
                "format_version": (
                    json.loads(row["package_config"] or "{}").get("format_version")
                    if row["package_config"]
                    else None
                ),
                "source_set_hash": str(row["package_source_set_hash"]),
                "profile_hash": str(row["package_profile_hash"]),
                "policy_hash": str(row["package_policy_hash"]),
                "catalog_hash": str(row["package_catalog_hash"]),
            }
            for row in rows
            if row["package_config"] and json.loads(row["package_config"] or "{}")
        ]

    def record_evidence(
        self,
        *,
        index_id: str,
        snapshot_hash: str,
        policy_hash: str,
        bundle_hash: str,
        catalog_hash: str,
        resident_ids: Sequence[str],
        turn_id: str | None,
        correlation_id: str | None,
        query_ref: str,
        state_snapshot: dict[str, Any],
        selections: Sequence[dict[str, Any]],
        final_decision: str,
        guard_rejections: Sequence[dict[str, Any]] = (),
        omissions: Sequence[dict[str, Any]] = (),
    ) -> str:
        """记录本轮固定版本与逐卡选择证据；不保存完整系统提示或凭据。"""
        from sana_bot.storage.index_repository import new_id

        evidence_id = new_id()
        with self.database.write():
            self.database.execute(
                """
                INSERT INTO retrieval_evidence
                    (id, index_id, snapshot_hash, policy_hash, stage, correlation_id, query_ref,
                     state_snapshot, group_ranking, guard_rejections, hydrated_fields, omissions,
                     final_decision, created_at, bundle_hash, catalog_hash, resident_ids,
                     selections, turn_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    index_id,
                    snapshot_hash,
                    policy_hash,
                    "reply",
                    correlation_id,
                    query_ref,
                    json.dumps(state_snapshot, ensure_ascii=False),
                    json.dumps(list(selections), ensure_ascii=False),
                    json.dumps(list(guard_rejections), ensure_ascii=False),
                    json.dumps(
                        [item.get("card_id") for item in selections if item.get("selected")],
                        ensure_ascii=False,
                    ),
                    json.dumps(list(omissions), ensure_ascii=False),
                    final_decision,
                    to_utc_iso(utc_now()),
                    bundle_hash,
                    catalog_hash,
                    json.dumps(list(resident_ids), ensure_ascii=False),
                    json.dumps(list(selections), ensure_ascii=False),
                    turn_id,
                ),
            )
        return evidence_id

    def evidence_for_turn(self, turn_id: str) -> list[dict[str, Any]]:
        rows = self.database.query(
            "SELECT * FROM retrieval_evidence WHERE turn_id = ? ORDER BY created_at",
            (turn_id,),
        )
        return [dict(row) for row in rows]

    def unlisted_files(self, structure_id: str, package: CharacterPackage) -> list[str]:
        return [
            issue.file
            for issue in package.issues
            if issue.reason_code == "package_file_unlisted"
        ]


# --------------------------------------------------------------------------- 报告


@dataclass(frozen=True)
class PackageCalibrationReport:
    id: str
    index_id: str
    stage: str
    snapshot_hash: str
    structural_profile_hash: str
    retrieval_policy_hash: str
    sample_set_hash: str
    model_id: str
    encoder_config_hash: str
    threshold: float | None
    sample_counts: dict[str, Any]
    rank_metrics: dict[str, Any]
    approved_by: str
    approved_at: str | None
    created_at: str
    report_hash: str = ""
    execution_mode: str = "offline"
    artifacts: dict[str, Any] | None = None
    status: str = "pending_review"
    report_path: str = ""

    def content_payload(self) -> dict[str, Any]:
        return {
            "index_id": self.index_id,
            "stage": self.stage,
            "snapshot_hash": self.snapshot_hash,
            "structural_profile_hash": self.structural_profile_hash,
            "retrieval_policy_hash": self.retrieval_policy_hash,
            "sample_set_hash": self.sample_set_hash,
            "model_id": self.model_id,
            "encoder_config_hash": self.encoder_config_hash,
            "threshold": self.threshold,
            "sample_counts": self.sample_counts,
            "rank_metrics": self.rank_metrics,
            "execution_mode": self.execution_mode,
            "artifacts": self.artifacts or {},
        }

    def compute_hash(self) -> str:
        return self.report_hash or canonical_hash(self.content_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "calibration_id": self.id,
            **self.content_payload(),
            "report_hash": self.compute_hash(),
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "status": self.status,
            "report_path": self.report_path,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class PackageRegressionReport:
    report_id: str
    index_id: str | None
    snapshot_hash: str
    bundle_hash: str
    structural_profile_hash: str
    retrieval_policy_hash: str
    sample_set_hash: str
    results: dict[str, Any]
    business_data_checks: dict[str, Any]
    report_path: str
    approved_by: str
    approved_at: str | None
    created_at: str
    report_hash: str = ""
    kind: str = "behaviour"
    execution_mode: str = "offline"
    artifacts: dict[str, Any] | None = None
    status: str = "pending_review"
    catalog_hash: str = ""
    encoder_config_hash: str = ""
    reviewer_reasons: tuple[str, ...] = ()

    def content_payload(self) -> dict[str, Any]:
        return {
            "index_id": self.index_id,
            "snapshot_hash": self.snapshot_hash,
            "bundle_hash": self.bundle_hash,
            "structural_profile_hash": self.structural_profile_hash,
            "retrieval_policy_hash": self.retrieval_policy_hash,
            "catalog_hash": self.catalog_hash,
            "encoder_config_hash": self.encoder_config_hash,
            "kind": self.kind,
            "execution_mode": self.execution_mode,
            "sample_set_hash": self.sample_set_hash,
            "results": self.results,
            "business_data_checks": self.business_data_checks,
            "artifacts": self.artifacts or {},
        }

    def compute_hash(self) -> str:
        return self.report_hash or canonical_hash(self.content_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            **self.content_payload(),
            "report_hash": self.compute_hash(),
            "report_path": self.report_path,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "status": self.status,
            "reviewer_reasons": list(self.reviewer_reasons),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ActivationRecord:
    id: str
    action: str
    previous_index_id: str | None
    target_index_id: str
    mode: str
    snapshot_hash: str
    bundle_hash: str | None
    validation_report_ids: tuple[str, ...]
    actor: str
    reason: str
    occurred_at: str
    result: str
    expected_active_index_id: str | None = None
    calibration_id: str | None = None
    report_hash: str = ""
    validation_report_hashes: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "previous_index_id": self.previous_index_id,
            "target_index_id": self.target_index_id,
            "expected_active_index_id": self.expected_active_index_id,
            "mode": self.mode,
            "snapshot_hash": self.snapshot_hash,
            "bundle_hash": self.bundle_hash,
            "validation_report_ids": list(self.validation_report_ids),
            "validation_report_hashes": list(self.validation_report_hashes),
            "calibration_id": self.calibration_id,
            "report_hash": self.report_hash,
            "actor": self.actor,
            "reason": self.reason,
            "occurred_at": self.occurred_at,
            "result": self.result,
        }


class PackageReportRepository:
    """不可变校准/回归报告与人工批准。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    # ------------------------------------------------------------------ 校准

    def insert_calibration(self, report: PackageCalibrationReport) -> None:
        existing = self.database.query_one(
            "SELECT id, report_hash FROM structural_calibration_report WHERE id = ?",
            (report.id,),
        )
        if existing is not None:
            raise ConfigError(
                "校准报告已存在；活动报告不可就地替换",
                reason_code="report_immutable",
            )
        with self.database.write():
            self.database.execute(
                """
                INSERT INTO structural_calibration_report
                    (id, index_id, stage, snapshot_hash, structural_profile_hash,
                     retrieval_policy_hash, sample_set_hash, model_id, encoder_config_hash,
                     threshold, sample_counts, rank_metrics, approved_by, approved_at,
                     created_at, report_hash, execution_mode, artifacts, status, report_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.id,
                    report.index_id,
                    report.stage,
                    report.snapshot_hash,
                    report.structural_profile_hash,
                    report.retrieval_policy_hash,
                    report.sample_set_hash,
                    report.model_id,
                    report.encoder_config_hash,
                    report.threshold,
                    json.dumps(report.sample_counts, ensure_ascii=False),
                    json.dumps(report.rank_metrics, ensure_ascii=False),
                    report.approved_by,
                    report.approved_at,
                    report.created_at,
                    report.compute_hash(),
                    report.execution_mode,
                    json.dumps(report.artifacts or {}, ensure_ascii=False),
                    report.status,
                    report.report_path,
                ),
            )

    def calibration(self, report_id: str) -> PackageCalibrationReport:
        row = self.database.query_one(
            "SELECT * FROM structural_calibration_report WHERE id = ?", (report_id,)
        )
        if row is None:
            raise NotReadyError(
                f"校准报告不存在：{report_id}", reason_code="package_report_missing"
            )
        return self._calibration_from_row(row)

    def calibrations(
        self, index_id: str, *, stage: str | None = None
    ) -> list[PackageCalibrationReport]:
        if stage is None:
            rows = self.database.query(
                "SELECT * FROM structural_calibration_report WHERE index_id = ? ORDER BY created_at",
                (index_id,),
            )
        else:
            rows = self.database.query(
                """
                SELECT * FROM structural_calibration_report
                WHERE index_id = ? AND stage = ? ORDER BY created_at
                """,
                (index_id, stage),
            )
        return [self._calibration_from_row(row) for row in rows]

    def latest_calibration(
        self, index_id: str, *, stage: str = "reply", approved_only: bool = False
    ) -> PackageCalibrationReport | None:
        rows = self.calibrations(index_id, stage=stage)
        if approved_only:
            rows = [row for row in rows if row.approved_at]
        return rows[-1] if rows else None

    def approve_calibration(self, report_id: str, *, reviewer: str) -> PackageCalibrationReport:
        row = self.database.query_one(
            "SELECT * FROM structural_calibration_report WHERE id = ?", (report_id,)
        )
        if row is None:
            raise NotReadyError(
                f"校准报告不存在：{report_id}", reason_code="package_report_missing"
            )
        if row["threshold"] is None:
            raise NotReadyError(
                "校准报告缺少可用阈值，不能批准",
                reason_code="package_calibration_missing",
            )
        with self.database.write():
            self.database.execute(
                """
                UPDATE structural_calibration_report
                SET approved_by = ?, approved_at = ?, status = 'approved'
                WHERE id = ?
                """,
                (reviewer, to_utc_iso(utc_now()), report_id),
            )
        return self.calibration(report_id)

    # ------------------------------------------------------------------ 回归

    def insert_regression(self, report: PackageRegressionReport) -> None:
        existing = self.database.query_one(
            "SELECT report_id FROM regression_report WHERE report_id = ?", (report.report_id,)
        )
        if existing is not None:
            raise ConfigError("回归报告已存在；不可就地替换", reason_code="report_immutable")
        with self.database.write():
            self.database.execute(
                """
                INSERT INTO regression_report
                    (report_id, index_id, snapshot_hash, bundle_hash, structural_profile_hash,
                     retrieval_policy_hash, sample_set_hash, results, business_data_checks,
                     report_path, approved_by, approved_at, created_at, report_hash, kind,
                     execution_mode, artifacts, status, catalog_hash, encoder_config_hash,
                     reviewer_reasons)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.report_id,
                    report.index_id,
                    report.snapshot_hash,
                    report.bundle_hash,
                    report.structural_profile_hash,
                    report.retrieval_policy_hash,
                    report.sample_set_hash,
                    json.dumps(report.results, ensure_ascii=False),
                    json.dumps(report.business_data_checks, ensure_ascii=False),
                    report.report_path,
                    report.approved_by,
                    report.approved_at,
                    report.created_at,
                    report.compute_hash(),
                    report.kind,
                    report.execution_mode,
                    json.dumps(report.artifacts or {}, ensure_ascii=False),
                    report.status,
                    report.catalog_hash,
                    report.encoder_config_hash,
                    json.dumps(list(report.reviewer_reasons), ensure_ascii=False),
                ),
            )

    def regression(self, report_id: str) -> PackageRegressionReport:
        row = self.database.query_one(
            "SELECT * FROM regression_report WHERE report_id = ?", (report_id,)
        )
        if row is None:
            raise NotReadyError(
                f"回归报告不存在：{report_id}", reason_code="package_report_missing"
            )
        return self._regression_from_row(row)

    def regressions(self, index_id: str, *, kind: str | None = None) -> list[PackageRegressionReport]:
        if kind is None:
            rows = self.database.query(
                "SELECT * FROM regression_report WHERE index_id = ? ORDER BY created_at",
                (index_id,),
            )
        else:
            rows = self.database.query(
                "SELECT * FROM regression_report WHERE index_id = ? AND kind = ? ORDER BY created_at",
                (index_id, kind),
            )
        return [self._regression_from_row(row) for row in rows]

    def approve_regression(
        self, report_id: str, *, reviewer: str, reasons: Sequence[str] = ()
    ) -> PackageRegressionReport:
        report = self.regression(report_id)
        if report.status == "approved":
            return report
        gaps = _evaluation_gaps(report.results)
        if gaps:
            raise NotReadyError(
                f"回归报告未满足必要门槛：{gaps[0]}",
                reason_code="evaluation_incomplete",
                details={"gaps": gaps},
            )
        if not (report.artifacts or {}).get("turns"):
            raise NotReadyError(
                "回归报告没有逐轮产物，不能仅凭通过标记批准",
                reason_code="evaluation_incomplete",
            )
        with self.database.write():
            self.database.execute(
                """
                UPDATE regression_report
                SET approved_by = ?, approved_at = ?, status = 'approved', reviewer_reasons = ?
                WHERE report_id = ?
                """,
                (
                    reviewer,
                    to_utc_iso(utc_now()),
                    json.dumps(list(reasons), ensure_ascii=False),
                    report_id,
                ),
            )
        return self.regression(report_id)

    # ------------------------------------------------------------------ 行映射

    @staticmethod
    def _calibration_from_row(row: Any) -> PackageCalibrationReport:
        return PackageCalibrationReport(
            id=str(row["id"]),
            index_id=str(row["index_id"]),
            stage=str(row["stage"]),
            snapshot_hash=str(row["snapshot_hash"]),
            structural_profile_hash=str(row["structural_profile_hash"]),
            retrieval_policy_hash=str(row["retrieval_policy_hash"]),
            sample_set_hash=str(row["sample_set_hash"]),
            model_id=str(row["model_id"]),
            encoder_config_hash=str(row["encoder_config_hash"]),
            threshold=row["threshold"],
            sample_counts=json.loads(row["sample_counts"] or "{}"),
            rank_metrics=json.loads(row["rank_metrics"] or "{}"),
            approved_by=str(row["approved_by"]),
            approved_at=row["approved_at"],
            created_at=str(row["created_at"]),
            report_hash=str(row["report_hash"] or ""),
            execution_mode=str(row["execution_mode"] or "offline"),
            artifacts=json.loads(row["artifacts"] or "{}"),
            status=str(row["status"] or "pending_review"),
            report_path=str(row["report_path"] or ""),
        )

    @staticmethod
    def _regression_from_row(row: Any) -> PackageRegressionReport:
        return PackageRegressionReport(
            report_id=str(row["report_id"]),
            index_id=row["index_id"],
            snapshot_hash=str(row["snapshot_hash"]),
            bundle_hash=str(row["bundle_hash"]),
            structural_profile_hash=str(row["structural_profile_hash"] or ""),
            retrieval_policy_hash=str(row["retrieval_policy_hash"] or ""),
            sample_set_hash=str(row["sample_set_hash"] or ""),
            results=json.loads(row["results"] or "{}"),
            business_data_checks=json.loads(row["business_data_checks"] or "{}"),
            report_path=str(row["report_path"] or ""),
            approved_by=str(row["approved_by"] or ""),
            approved_at=row["approved_at"],
            created_at=str(row["created_at"]),
            report_hash=str(row["report_hash"] or ""),
            kind=str(row["kind"] or "behaviour"),
            execution_mode=str(row["execution_mode"] or "offline"),
            artifacts=json.loads(row["artifacts"] or "{}"),
            status=str(row["status"] or "pending_review"),
            catalog_hash=str(row["catalog_hash"] or ""),
            encoder_config_hash=str(row["encoder_config_hash"] or ""),
            reviewer_reasons=tuple(json.loads(row["reviewer_reasons"] or "[]")),
        )


#: 生产启用必须全部通过的类别；运维演练只在真实（live）报告中作为硬门槛
_REQUIRED_GATES = ("integrity", "selection", "empty_retrieval", "behaviour", "output")


def _evaluation_gaps(results: dict[str, Any]) -> list[str]:
    """必要门槛必须逐项为真；未执行项不算通过。

    切换演练（operations）在隔离库单独执行，因此只对 live 报告作为硬门槛；
    offline 报告仍会记录其状态，但不因此被误当成已通过的运行证据。
    """
    required = results.get("required") or {}
    mode = str(results.get("execution_mode") or "offline")
    gaps: list[str] = []
    for key in _REQUIRED_GATES:
        if key not in required:
            gaps.append(f"{key}: not_run")
            continue
        if required[key] != "passed":
            gaps.append(f"{key}: {required[key]}")
    if mode == "live" and required.get("operations") != "passed":
        gaps.append(f"operations: {required.get('operations')}")
    return gaps


class ActivationRepository:
    """激活审计记录（只追加）。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def record(self, record: ActivationRecord) -> None:
        with self.database.write():
            self.database.execute(
                """
                INSERT INTO activation_audit
                    (id, action, previous_index_id, target_index_id, mode, snapshot_hash,
                     bundle_hash, validation_report_ids, actor, reason, occurred_at, result,
                     expected_active_index_id, calibration_id, report_hash,
                     validation_report_hashes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.action,
                    record.previous_index_id,
                    record.target_index_id,
                    record.mode,
                    record.snapshot_hash,
                    record.bundle_hash,
                    json.dumps(list(record.validation_report_ids), ensure_ascii=False),
                    record.actor,
                    record.reason,
                    record.occurred_at,
                    record.result,
                    record.expected_active_index_id,
                    record.calibration_id,
                    record.report_hash,
                    json.dumps(list(record.validation_report_hashes), ensure_ascii=False),
                ),
            )

    def recent(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.database.query(
            "SELECT * FROM activation_audit ORDER BY occurred_at DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in rows]
