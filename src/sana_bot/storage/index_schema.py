"""索引相关表：结构、类型节点、片段、语义单元、向量元数据、快照与校准报告。

不可变快照的关键约束：结构/切分配置不可变、同一时刻仅一个 active、
``snapshot_hash`` 不含随后追加的校准报告（避免循环 hash）。
"""

from __future__ import annotations

from sana_bot.config import RuntimeConfig
from sana_bot.storage.database import Migration

__all__ = ["migrations"]


def migrations(config: RuntimeConfig | None = None) -> tuple[Migration, ...]:
    return (
        Migration(
            name="0010_index_structure",
            statements=(
                """
                CREATE TABLE IF NOT EXISTS source_document (
                    id             TEXT PRIMARY KEY,
                    relative_path  TEXT NOT NULL,
                    file_hash      TEXT NOT NULL,
                    imported_at    TEXT NOT NULL,
                    parser_version TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS structured_document (
                    id             TEXT PRIMARY KEY,
                    source_id      TEXT NOT NULL REFERENCES source_document(id),
                    structure_hash TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    catalog_hash   TEXT,
                    review_status  TEXT NOT NULL DEFAULT 'pending',
                    body_text      TEXT NOT NULL,
                    coverage       TEXT NOT NULL DEFAULT '{}',
                    issues         TEXT NOT NULL DEFAULT '[]',
                    heading_tree   TEXT NOT NULL DEFAULT '[]',
                    created_at     TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS structure_approval (
                    structure_id   TEXT NOT NULL REFERENCES structured_document(id),
                    structure_hash TEXT NOT NULL,
                    catalog_hash   TEXT NOT NULL,
                    catalog_path   TEXT NOT NULL,
                    reviewer       TEXT NOT NULL,
                    approved_at    TEXT NOT NULL,
                    PRIMARY KEY (structure_id, catalog_hash)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS rule_node (
                    id              TEXT PRIMARY KEY,
                    document_id     TEXT NOT NULL REFERENCES structured_document(id),
                    parent_id       TEXT,
                    heading_path    TEXT NOT NULL DEFAULT '[]',
                    paragraph_index INTEGER NOT NULL,
                    char_start      INTEGER NOT NULL,
                    char_end        INTEGER NOT NULL,
                    list_level      INTEGER,
                    field_type      TEXT,
                    content_type    TEXT NOT NULL,
                    raw_text        TEXT NOT NULL,
                    scope           TEXT NOT NULL DEFAULT '',
                    conditions      TEXT NOT NULL DEFAULT '{}',
                    speaker         TEXT,
                    provenance      TEXT NOT NULL DEFAULT 'source',
                    review_status   TEXT NOT NULL DEFAULT 'pending'
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_rule_node_document
                    ON rule_node(document_id, paragraph_index, char_start)
                """,
                """
                CREATE TABLE IF NOT EXISTS source_span (
                    id             TEXT PRIMARY KEY,
                    structure_id   TEXT NOT NULL REFERENCES structured_document(id),
                    node_id        TEXT NOT NULL REFERENCES rule_node(id),
                    kind           TEXT NOT NULL,
                    raw_text       TEXT NOT NULL,
                    source_start   INTEGER NOT NULL,
                    source_end     INTEGER NOT NULL,
                    speaker        TEXT,
                    speaker_label  TEXT,
                    segment_index  INTEGER NOT NULL DEFAULT 0,
                    previous_id    TEXT,
                    next_id        TEXT,
                    parent_span_id TEXT
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_source_span_node
                    ON source_span(node_id, segment_index)
                """,
                """
                CREATE TABLE IF NOT EXISTS index_snapshot (
                    id                       TEXT PRIMARY KEY,
                    source_hash              TEXT NOT NULL,
                    structure_hash           TEXT NOT NULL,
                    split_profile_hash       TEXT NOT NULL,
                    snapshot_hash            TEXT NOT NULL,
                    manifest_hash            TEXT NOT NULL DEFAULT '',
                    parser_version           TEXT NOT NULL,
                    splitter_version         TEXT NOT NULL,
                    embedding_model_id       TEXT NOT NULL,
                    embedding_revision       TEXT NOT NULL,
                    dimension                INTEGER NOT NULL,
                    rule_catalog_version     TEXT NOT NULL,
                    reply_calibration_id     TEXT,
                    monologue_calibration_id TEXT,
                    monologue_status         TEXT NOT NULL DEFAULT 'disabled_no_material',
                    status                   TEXT NOT NULL DEFAULT 'staged',
                    artifact_path            TEXT NOT NULL,
                    manifest                 TEXT NOT NULL DEFAULT '{}',
                    created_at               TEXT NOT NULL,
                    updated_at               TEXT NOT NULL
                )
                """,
                # 同一时刻仅一个 active 版本
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_index_snapshot_single_active
                    ON index_snapshot(status) WHERE status = 'active'
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_index_snapshot_structure
                    ON index_snapshot(structure_hash, status)
                """,
                """
                CREATE TABLE IF NOT EXISTS split_calibration_profile (
                    id               TEXT PRIMARY KEY,
                    structure_hash   TEXT NOT NULL,
                    model_revision   TEXT NOT NULL,
                    sample_set_hash  TEXT NOT NULL,
                    splitter_version TEXT NOT NULL,
                    parameters       TEXT NOT NULL,
                    profile_hash     TEXT NOT NULL,
                    metrics          TEXT NOT NULL DEFAULT '{}',
                    approved_at      TEXT,
                    created_at       TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS calibration_profile (
                    id                 TEXT PRIMARY KEY,
                    index_id           TEXT NOT NULL REFERENCES index_snapshot(id),
                    snapshot_hash      TEXT NOT NULL,
                    split_profile_hash TEXT NOT NULL,
                    stage              TEXT NOT NULL CHECK (stage IN ('reply', 'monologue')),
                    sample_set_hash    TEXT NOT NULL,
                    threshold          REAL,
                    score_metric       TEXT NOT NULL DEFAULT 'cosine',
                    metrics            TEXT NOT NULL DEFAULT '{}',
                    approved_at        TEXT,
                    created_at         TEXT NOT NULL
                )
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_calibration_stage
                    ON calibration_profile(index_id, stage)
                """,
                """
                CREATE TABLE IF NOT EXISTS semantic_unit (
                    id                TEXT PRIMARY KEY,
                    index_id          TEXT NOT NULL REFERENCES index_snapshot(id),
                    source_span_ids   TEXT NOT NULL,
                    source_node_ids   TEXT NOT NULL DEFAULT '[]',
                    speaker_labels    TEXT NOT NULL DEFAULT '[]',
                    content_types     TEXT NOT NULL DEFAULT '[]',
                    topic             TEXT NOT NULL DEFAULT '',
                    text              TEXT NOT NULL,
                    boundary_evidence TEXT NOT NULL DEFAULT '{}',
                    context_links     TEXT NOT NULL DEFAULT '[]',
                    conditions        TEXT NOT NULL DEFAULT '{}',
                    token_count       INTEGER NOT NULL DEFAULT 0,
                    parent_unit_id    TEXT,
                    offset_in_parent  INTEGER
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS embedding_record (
                    unit_id       TEXT PRIMARY KEY REFERENCES semantic_unit(id),
                    index_id      TEXT NOT NULL REFERENCES index_snapshot(id),
                    model_id      TEXT NOT NULL,
                    revision      TEXT NOT NULL,
                    dimension     INTEGER NOT NULL,
                    normalized    INTEGER NOT NULL DEFAULT 1,
                    input_hash    TEXT NOT NULL,
                    token_count   INTEGER NOT NULL,
                    vector_offset INTEGER NOT NULL
                )
                """,
            ),
        ),
        Migration(
            name="0011_structural_index",
            foreign_keys_off=True,
            statements=(
                # split_profile_hash 必须放宽为可空：structural_v2 没有语义切分配置。
                # SQLite 无 ALTER COLUMN，按官方 12 步表重建配方复制索引表。
                """
                CREATE TABLE index_snapshot_rebuilt (
                    id                       TEXT PRIMARY KEY,
                    source_hash              TEXT NOT NULL,
                    structure_hash           TEXT NOT NULL,
                    split_profile_hash       TEXT,
                    structural_profile_hash  TEXT,
                    retrieval_policy_hash    TEXT NOT NULL DEFAULT '',
                    index_format_version     TEXT NOT NULL DEFAULT 'semantic_v1',
                    chunking_strategy        TEXT NOT NULL DEFAULT 'semantic',
                    bundle_hash              TEXT,
                    sealed_at                TEXT,
                    snapshot_hash            TEXT NOT NULL,
                    manifest_hash            TEXT NOT NULL DEFAULT '',
                    parser_version           TEXT NOT NULL,
                    splitter_version         TEXT NOT NULL,
                    embedding_model_id       TEXT NOT NULL,
                    embedding_revision       TEXT NOT NULL,
                    dimension                INTEGER NOT NULL,
                    rule_catalog_version     TEXT NOT NULL,
                    reply_calibration_id     TEXT,
                    monologue_calibration_id TEXT,
                    monologue_status         TEXT NOT NULL DEFAULT 'disabled_no_material',
                    status                   TEXT NOT NULL DEFAULT 'staged',
                    artifact_path            TEXT NOT NULL,
                    manifest                 TEXT NOT NULL DEFAULT '{}',
                    created_at               TEXT NOT NULL,
                    updated_at               TEXT NOT NULL,
                    CHECK (index_format_version IN ('semantic_v1', 'structural_v2')),
                    CHECK (
                        (index_format_version = 'semantic_v1'
                         AND split_profile_hash IS NOT NULL
                         AND structural_profile_hash IS NULL)
                        OR
                        (index_format_version = 'structural_v2'
                         AND structural_profile_hash IS NOT NULL
                         AND split_profile_hash IS NULL)
                    )
                )
                """,
                """
                INSERT INTO index_snapshot_rebuilt (
                    id, source_hash, structure_hash, split_profile_hash,
                    structural_profile_hash, retrieval_policy_hash, index_format_version,
                    chunking_strategy, bundle_hash, sealed_at, snapshot_hash, manifest_hash,
                    parser_version, splitter_version, embedding_model_id, embedding_revision,
                    dimension, rule_catalog_version, reply_calibration_id,
                    monologue_calibration_id, monologue_status, status, artifact_path,
                    manifest, created_at, updated_at
                )
                SELECT
                    id, source_hash, structure_hash, split_profile_hash,
                    NULL, '', 'semantic_v1',
                    'semantic', NULL, NULL, snapshot_hash, manifest_hash,
                    parser_version, splitter_version, embedding_model_id, embedding_revision,
                    dimension, rule_catalog_version, reply_calibration_id,
                    monologue_calibration_id, monologue_status, status, artifact_path,
                    manifest, created_at, updated_at
                FROM index_snapshot
                """,
                "DROP TABLE index_snapshot",
                "ALTER TABLE index_snapshot_rebuilt RENAME TO index_snapshot",
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_index_snapshot_single_active
                    ON index_snapshot(status) WHERE status = 'active'
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_index_snapshot_structure
                    ON index_snapshot(structure_hash, status)
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_index_snapshot_format
                    ON index_snapshot(index_format_version, status)
                """,
                """
                CREATE TABLE IF NOT EXISTS structural_profile (
                    id                 TEXT PRIMARY KEY,
                    version            TEXT NOT NULL,
                    aliases            TEXT NOT NULL DEFAULT '{}',
                    heading_policy     TEXT NOT NULL DEFAULT '{}',
                    inheritance_policy TEXT NOT NULL DEFAULT '{}',
                    length_policy      TEXT NOT NULL DEFAULT '{}',
                    group_id_policy    TEXT NOT NULL DEFAULT '{}',
                    profile_hash       TEXT NOT NULL UNIQUE,
                    approved_by        TEXT NOT NULL DEFAULT '',
                    approved_at        TEXT,
                    created_at         TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS structural_document (
                    id             TEXT PRIMARY KEY,
                    source_id      TEXT NOT NULL REFERENCES source_document(id),
                    structure_hash TEXT NOT NULL,
                    profile_hash   TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    review_status  TEXT NOT NULL DEFAULT 'pending',
                    catalog_hash   TEXT,
                    body_text      TEXT NOT NULL,
                    coverage       TEXT NOT NULL DEFAULT '{}',
                    issues         TEXT NOT NULL DEFAULT '[]',
                    heading_tree   TEXT NOT NULL DEFAULT '[]',
                    created_at     TEXT NOT NULL
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_structural_document_hash
                    ON structural_document(structure_hash)
                """,
                """
                CREATE TABLE IF NOT EXISTS structural_approval (
                    structure_id   TEXT NOT NULL REFERENCES structural_document(id),
                    structure_hash TEXT NOT NULL,
                    catalog_hash   TEXT NOT NULL,
                    profile_hash   TEXT NOT NULL,
                    catalog_path   TEXT NOT NULL,
                    reviewer       TEXT NOT NULL,
                    approved_at    TEXT NOT NULL,
                    PRIMARY KEY (structure_id, catalog_hash)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS structural_group (
                    structure_id         TEXT NOT NULL REFERENCES structural_document(id),
                    group_key            TEXT NOT NULL,
                    group_id             TEXT NOT NULL DEFAULT '',
                    heading_path         TEXT NOT NULL DEFAULT '[]',
                    occurrence           INTEGER NOT NULL DEFAULT 0,
                    group_kind           TEXT NOT NULL,
                    parent_group_key     TEXT,
                    source_ranges        TEXT NOT NULL DEFAULT '[]',
                    field_keys           TEXT NOT NULL DEFAULT '[]',
                    constraint_refs      TEXT NOT NULL DEFAULT '[]',
                    effective_conditions TEXT NOT NULL DEFAULT '{}',
                    unresolved_fields    TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY (structure_id, group_key)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS structural_field (
                    structure_id     TEXT NOT NULL REFERENCES structural_document(id),
                    field_key        TEXT NOT NULL,
                    group_key        TEXT NOT NULL,
                    field_type       TEXT NOT NULL,
                    original_label   TEXT NOT NULL DEFAULT '',
                    raw_text         TEXT NOT NULL,
                    source_node_ids  TEXT NOT NULL DEFAULT '[]',
                    source_ranges    TEXT NOT NULL DEFAULT '[]',
                    speaker_turns    TEXT NOT NULL DEFAULT '[]',
                    field_conditions TEXT NOT NULL DEFAULT '{}',
                    explicit_links   TEXT NOT NULL DEFAULT '[]',
                    provenance       TEXT NOT NULL DEFAULT 'source',
                    PRIMARY KEY (structure_id, field_key)
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_structural_field_group
                    ON structural_field(structure_id, group_key)
                """,
                """
                CREATE TABLE IF NOT EXISTS structural_link (
                    structure_id     TEXT NOT NULL REFERENCES structural_document(id),
                    ordinal          INTEGER NOT NULL,
                    source_group_key TEXT NOT NULL,
                    target_group_keys TEXT NOT NULL DEFAULT '[]',
                    relation         TEXT NOT NULL,
                    source_ranges    TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY (structure_id, ordinal)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS structural_leaf (
                    structure_id         TEXT NOT NULL REFERENCES structural_document(id),
                    leaf_key             TEXT NOT NULL,
                    group_key            TEXT NOT NULL,
                    field_key            TEXT NOT NULL,
                    ordinal              INTEGER NOT NULL DEFAULT 0,
                    raw_text             TEXT NOT NULL,
                    offset_start         INTEGER NOT NULL,
                    offset_end           INTEGER NOT NULL,
                    content_types        TEXT NOT NULL DEFAULT '[]',
                    speaker_labels       TEXT NOT NULL DEFAULT '[]',
                    effective_conditions TEXT NOT NULL DEFAULT '{}',
                    eligible_stages      TEXT NOT NULL DEFAULT '["reply", "monologue"]',
                    token_split          INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (structure_id, leaf_key)
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_structural_leaf_field
                    ON structural_leaf(structure_id, field_key, ordinal)
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_structural_leaf_group
                    ON structural_leaf(structure_id, group_key)
                """,
                """
                CREATE TABLE IF NOT EXISTS structural_constraint (
                    structure_id    TEXT NOT NULL REFERENCES structural_document(id),
                    clause_key      TEXT NOT NULL,
                    predicate       TEXT NOT NULL,
                    allowed_values  TEXT NOT NULL DEFAULT '[]',
                    expression      TEXT NOT NULL DEFAULT '',
                    source_start    INTEGER NOT NULL DEFAULT 0,
                    source_end      INTEGER NOT NULL DEFAULT 0,
                    source_node_key TEXT NOT NULL DEFAULT '',
                    provenance      TEXT NOT NULL DEFAULT 'source',
                    scope           TEXT NOT NULL DEFAULT '',
                    approved        INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (structure_id, clause_key)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS structural_embedding (
                    index_id            TEXT NOT NULL REFERENCES index_snapshot(id),
                    leaf_key            TEXT NOT NULL,
                    vector_offset       INTEGER NOT NULL,
                    dimension           INTEGER NOT NULL,
                    model_id            TEXT NOT NULL,
                    effective_revision  TEXT NOT NULL,
                    encoder_config_hash TEXT NOT NULL,
                    input_hash          TEXT NOT NULL,
                    input_token_count   INTEGER NOT NULL DEFAULT 0,
                    token_estimate      INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (index_id, leaf_key)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS structural_calibration_report (
                    id                      TEXT PRIMARY KEY,
                    index_id                TEXT NOT NULL REFERENCES index_snapshot(id),
                    stage                   TEXT NOT NULL CHECK (stage IN ('reply', 'monologue')),
                    snapshot_hash           TEXT NOT NULL,
                    structural_profile_hash TEXT NOT NULL,
                    retrieval_policy_hash   TEXT NOT NULL,
                    sample_set_hash         TEXT NOT NULL,
                    model_id                TEXT NOT NULL,
                    encoder_config_hash     TEXT NOT NULL,
                    threshold               REAL,
                    sample_counts           TEXT NOT NULL DEFAULT '{}',
                    rank_metrics            TEXT NOT NULL DEFAULT '{}',
                    approved_by             TEXT NOT NULL,
                    approved_at             TEXT,
                    created_at              TEXT NOT NULL
                )
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_structural_calibration_stage
                    ON structural_calibration_report(index_id, stage)
                """,
                """
                CREATE TABLE IF NOT EXISTS regression_report (
                    report_id               TEXT PRIMARY KEY,
                    index_id                TEXT,
                    snapshot_hash           TEXT NOT NULL,
                    bundle_hash             TEXT NOT NULL,
                    structural_profile_hash TEXT NOT NULL DEFAULT '',
                    retrieval_policy_hash   TEXT NOT NULL DEFAULT '',
                    sample_set_hash         TEXT NOT NULL DEFAULT '',
                    results                 TEXT NOT NULL DEFAULT '{}',
                    business_data_checks    TEXT NOT NULL DEFAULT '{}',
                    report_path             TEXT NOT NULL DEFAULT '',
                    approved_by             TEXT NOT NULL DEFAULT '',
                    approved_at             TEXT,
                    created_at              TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS activation_audit (
                    id                    TEXT PRIMARY KEY,
                    action                TEXT NOT NULL CHECK (action IN ('activate', 'rollback')),
                    previous_index_id     TEXT,
                    target_index_id       TEXT NOT NULL,
                    mode                  TEXT NOT NULL,
                    snapshot_hash         TEXT NOT NULL,
                    bundle_hash           TEXT,
                    validation_report_ids TEXT NOT NULL DEFAULT '[]',
                    actor                 TEXT NOT NULL DEFAULT '',
                    reason                TEXT NOT NULL DEFAULT '',
                    occurred_at           TEXT NOT NULL,
                    result                TEXT NOT NULL DEFAULT 'ok'
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS retrieval_evidence (
                    id             TEXT PRIMARY KEY,
                    index_id       TEXT NOT NULL,
                    snapshot_hash  TEXT NOT NULL,
                    policy_hash    TEXT NOT NULL,
                    stage          TEXT NOT NULL,
                    correlation_id TEXT,
                    query_ref      TEXT NOT NULL,
                    state_snapshot TEXT NOT NULL DEFAULT '{}',
                    group_ranking  TEXT NOT NULL DEFAULT '[]',
                    guard_rejections TEXT NOT NULL DEFAULT '[]',
                    hydrated_fields TEXT NOT NULL DEFAULT '[]',
                    omissions      TEXT NOT NULL DEFAULT '[]',
                    final_decision TEXT NOT NULL DEFAULT '',
                    created_at     TEXT NOT NULL
                )
                """,
            ),
        ),
        Migration(
            name="0012_package_sources_and_reports",
            statements=(
                # 多来源关联：不能用一个合成 source_id 抹掉六份文件的出处
                """
                CREATE TABLE IF NOT EXISTS package_source (
                    structure_id       TEXT NOT NULL REFERENCES structural_document(id),
                    source_key         TEXT NOT NULL,
                    source_document_id TEXT NOT NULL REFERENCES source_document(id),
                    relative_path      TEXT NOT NULL,
                    role               TEXT NOT NULL,
                    byte_hash          TEXT NOT NULL,
                    included           INTEGER NOT NULL DEFAULT 1,
                    excluded_reason    TEXT NOT NULL DEFAULT '',
                    source_note        TEXT NOT NULL DEFAULT '',
                    raw_text           TEXT NOT NULL DEFAULT '',
                    ordinal            INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (structure_id, source_key)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS package_resident (
                    structure_id  TEXT NOT NULL REFERENCES structural_document(id),
                    resident_id   TEXT NOT NULL,
                    ordinal       INTEGER NOT NULL,
                    title         TEXT NOT NULL,
                    raw_text      TEXT NOT NULL,
                    source_key    TEXT NOT NULL,
                    source_ranges TEXT NOT NULL DEFAULT '[]',
                    content_hash  TEXT NOT NULL,
                    required      INTEGER NOT NULL DEFAULT 1,
                    token_count   INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (structure_id, resident_id)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS package_card (
                    structure_id TEXT NOT NULL REFERENCES structural_document(id),
                    card_id      TEXT NOT NULL,
                    card_type    TEXT NOT NULL,
                    ordinal      INTEGER NOT NULL,
                    payload      TEXT NOT NULL,
                    PRIMARY KEY (structure_id, card_id)
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_package_card_type
                    ON package_card(structure_id, card_type, ordinal)
                """,
                # 结构版本记录包级哈希，便于与快照逐项核对
                "ALTER TABLE structural_document ADD COLUMN package_source_set_hash TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE structural_document ADD COLUMN package_profile_hash TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE structural_document ADD COLUMN package_policy_hash TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE structural_document ADD COLUMN package_catalog_hash TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE structural_document ADD COLUMN package_config TEXT NOT NULL DEFAULT '{}'",
                "ALTER TABLE structural_approval ADD COLUMN policy_hash TEXT NOT NULL DEFAULT ''",
                # 校准报告版本化：同一 index/stage 允许多份不可变修订
                "DROP INDEX IF EXISTS idx_structural_calibration_stage",
                """
                CREATE INDEX IF NOT EXISTS idx_structural_calibration_versioned
                    ON structural_calibration_report(index_id, stage, created_at)
                """,
                "ALTER TABLE structural_calibration_report ADD COLUMN report_hash TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE structural_calibration_report ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'offline'",
                "ALTER TABLE structural_calibration_report ADD COLUMN artifacts TEXT NOT NULL DEFAULT '{}'",
                "ALTER TABLE structural_calibration_report ADD COLUMN status TEXT NOT NULL DEFAULT 'pending_review'",
                "ALTER TABLE structural_calibration_report ADD COLUMN report_path TEXT NOT NULL DEFAULT ''",
                # 回归/行为报告：区分种类、执行模式与不可变修订
                "ALTER TABLE regression_report ADD COLUMN report_hash TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE regression_report ADD COLUMN kind TEXT NOT NULL DEFAULT 'behaviour'",
                "ALTER TABLE regression_report ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'offline'",
                "ALTER TABLE regression_report ADD COLUMN artifacts TEXT NOT NULL DEFAULT '{}'",
                "ALTER TABLE regression_report ADD COLUMN status TEXT NOT NULL DEFAULT 'pending_review'",
                "ALTER TABLE regression_report ADD COLUMN catalog_hash TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE regression_report ADD COLUMN encoder_config_hash TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE regression_report ADD COLUMN reviewer_reasons TEXT NOT NULL DEFAULT '[]'",
                # 激活审计：记录期望值与被批准的报告引用
                "ALTER TABLE activation_audit ADD COLUMN expected_active_index_id TEXT",
                "ALTER TABLE activation_audit ADD COLUMN calibration_id TEXT",
                "ALTER TABLE activation_audit ADD COLUMN report_hash TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE activation_audit ADD COLUMN validation_report_hashes TEXT NOT NULL DEFAULT '[]'",
                # 活动包固定所指报告，后续校准另建报告，不就地替换
                "ALTER TABLE index_snapshot ADD COLUMN active_report_hash TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE index_snapshot ADD COLUMN approved_report_id TEXT",
                # 本轮证据：固定包版本、常驻与逐卡选择
                "ALTER TABLE retrieval_evidence ADD COLUMN bundle_hash TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE retrieval_evidence ADD COLUMN catalog_hash TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE retrieval_evidence ADD COLUMN resident_ids TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE retrieval_evidence ADD COLUMN selections TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE retrieval_evidence ADD COLUMN turn_id TEXT",
            ),
        ),
    )
