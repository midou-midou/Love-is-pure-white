"""资料包构建：解析 → 逐卡精确计数 → 完整编码 → 封存不可变快照。

外部编码调用一律在写事务之外；只有全部产物写入并复核通过后才登记 ``staged``。
失败不改动已有 active 版本，也不静默截断、摘要或拆分任何卡。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from sana_bot.enums import IndexStatus, MonologueStatus
from sana_bot.errors import NotReadyError
from sana_bot.ingest.markdown_package import (
    ensure_package_complete,
    parse_markdown_package,
)
from sana_bot.ingest.package_models import (
    CHUNKING_STRATEGY_MARKDOWN,
    PACKAGE_FORMAT_VERSION,
    PACKAGE_PARSER_VERSION,
    CharacterPackage,
    ReferenceCard,
    canonical_hash,
)
from sana_bot.storage.database import Database
from sana_bot.storage.index_repository import SnapshotRecord, StructureRepository, new_id
from sana_bot.storage.package_repository import PackageRepository
from sana_bot.timeutil import to_utc_iso, utc_now

__all__ = [
    "PackageParseOutcome",
    "PackageBuildOutcome",
    "ParsedPackage",
    "parse_package",
    "build_package",
    "verify_sealed_bundle",
    "encoder_config_payload",
    "sealed_file_hashes",
]

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ParsedPackage:
    package: CharacterPackage
    structure_id: str
    directory: Path
    profile_hash: str
    policy_hash: str
    catalog_hash: str


@dataclass(frozen=True)
class PackageParseOutcome:
    structure_id: str
    structure_hash: str
    source_set_hash: str
    profile_hash: str
    policy_hash: str
    catalog_hash: str
    resident_count: int
    card_counts: Mapping[str, int]
    excluded_files: tuple[str, ...]
    issues: tuple[Mapping[str, Any], ...]
    structure_path: Path
    report_path: Path
    cards_path: Path


@dataclass(frozen=True)
class PackageBuildOutcome:
    index_id: str
    structure_id: str
    snapshot_hash: str
    bundle_hash: str
    manifest_hash: str
    artifact_path: Path
    resident_count: int
    card_count: int
    encoded_count: int
    dimension: int
    token_counts: Mapping[str, int]
    encoder_config_hash: str
    monologue_status: str
    metrics: Mapping[str, Any] = field(default_factory=dict)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _count_cards(package: CharacterPackage) -> dict[str, int]:
    counts: dict[str, int] = {}
    for card in package.cards:
        counts[card.type.value] = counts.get(card.type.value, 0) + 1
    return counts


def parse_package(
    config_path: Path,
    *,
    output_dir: Path,
    project_root: Path | None = None,
) -> PackageParseOutcome:
    """解析显式清单并落盘结构、卡清单与核对报告；成功不等于批准。"""
    package = parse_markdown_package(
        Path(config_path), project_root=project_root or PROJECT_ROOT
    )
    profile_hash = package.compute_profile_hash()
    policy = package.config.policy()
    structure_id = new_id()
    directory = Path(output_dir) / structure_id
    directory.mkdir(parents=True, exist_ok=True)

    # 解析阶段不写库；结构由 CLI 在核对通过后登记

    structure_payload = {
        "structure_id": structure_id,
        "format_version": package.config.format_version,
        "chunking_strategy": package.config.chunking_strategy,
        "structure_hash": package.structure_hash,
        "source_set_hash": package.source_set_hash,
        "profile_hash": profile_hash,
        "policy_hash": policy.policy_hash,
        "catalog_hash": policy.catalog_hash,
        "sources": [
            {
                "source_id": source.source_id,
                "path": source.relative_path,
                "role": source.role.value,
                "byte_hash": source.byte_hash,
                "included": source.included,
                "excluded_reason": source.excluded_reason,
            }
            for source in package.sources
        ],
        "residents": [
            {
                "resident_id": item.resident_id,
                "ordinal": item.ordinal,
                "title": item.title,
                "content_hash": item.content_hash,
                "char_count": len(item.raw_text),
            }
            for item in package.residents
        ],
        "cards": [
            {
                "card_id": card.card_id,
                "type": card.type.value,
                "provenance": card.provenance.value,
                "resident_only": card.resident_only,
                "heading_path": list(card.heading_path),
                "char_count": len(card.raw_text),
                "turn_count": len(card.speaker_turns),
            }
            for card in package.cards
        ],
    }
    structure_path = directory / "structure.json"
    _write_json(structure_path, structure_payload)
    cards_path = directory / "cards.json"
    _write_json(cards_path, [card.to_payload() for card in package.cards])
    report = {
        "structure_id": structure_id,
        "format_version": package.config.format_version,
        "structure_hash": package.structure_hash,
        "source_set_hash": package.source_set_hash,
        "profile_hash": profile_hash,
        "policy_hash": policy.policy_hash,
        "catalog_hash": policy.catalog_hash,
        "resident_ids": list(package.resident_ids),
        "card_counts": _count_cards(package),
        "encoded_count": len(package.encoded_cards),
        "excluded_files": list(package.config.excluded_files),
        "issues": [issue.to_payload() for issue in package.issues],
        "unlisted_files": [
            issue.file
            for issue in package.issues
            if issue.reason_code == "package_file_unlisted"
        ],
        "approval_status": "pending",
        "note": "解析成功只表示结构可核对；需人工核对并执行 index approve",
    }
    report_path = directory / "review-report.json"
    _write_json(report_path, report)
    return PackageParseOutcome(
        structure_id=structure_id,
        structure_hash=package.structure_hash,
        source_set_hash=package.source_set_hash,
        profile_hash=profile_hash,
        policy_hash=policy.policy_hash,
        catalog_hash=policy.catalog_hash,
        resident_count=len(package.residents),
        card_counts=_count_cards(package),
        excluded_files=tuple(package.config.excluded_files),
        issues=tuple(issue.to_payload() for issue in package.issues),
        structure_path=structure_path,
        report_path=report_path,
        cards_path=cards_path,
    )


def encoder_config_payload(backend: Any) -> dict[str, Any]:
    """编码器配置哈希输入：kind/model/revision/维度/上限/tokenizer/查询前缀。"""
    spec = backend.spec
    counter = backend.token_counter
    tokenizer_name = type(counter).__name__
    revision = ""
    tokenizer = getattr(counter, "_tokenizer", None)
    if tokenizer is not None:
        revision = str(getattr(tokenizer, "name_or_path", "") or getattr(tokenizer, "init_kwargs", "") or "")
    payload = {
        "kind": spec.kind.value,
        "model_id": spec.model_id,
        "revision": spec.effective_revision,
        "dimension": spec.dimension,
        "max_tokens": spec.max_tokens,
        "normalize": spec.normalize,
        "query_instruction_hash": hashlib.sha256(
            spec.query_instruction.encode("utf-8")
        ).hexdigest(),
        "tokenizer": tokenizer_name,
        "tokenizer_revision": revision,
        "input_format": "title+type+scope+body",
        "special_tokens_counted": bool(getattr(backend, "tokenizer_verified", False)),
    }
    return payload


def _seal_files(directory: Path, relative_paths: Sequence[str]) -> dict[str, str]:
    return {relative: _file_sha256(directory / relative) for relative in relative_paths}


def sealed_file_hashes(directory: Path) -> dict[str, str]:
    """按固定顺序重算封存文件哈希（不含 manifest 自身）。"""
    paths = sorted(
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    )
    return {relative: _file_sha256(directory / relative) for relative in paths}


def verify_sealed_bundle(snapshot: SnapshotRecord, *, directory: Path | None = None) -> dict[str, str]:
    """从实际产物重算文件哈希并与 manifest 比较，不只信 manifest 自称值。"""
    target = Path(directory or snapshot.artifact_path)
    manifest_path = target / "manifest.json"
    if not manifest_path.exists():
        raise NotReadyError(
            f"索引产物缺少 manifest：{target}", reason_code="index_artifact_missing"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual = sealed_file_hashes(target)
    declared = manifest.get("bundle_files") or {}
    missing = sorted(set(declared) - set(actual))
    changed = sorted(key for key in actual if key in declared and actual[key] != declared[key])
    if missing or changed:
        raise NotReadyError(
            "封存文件与 manifest 不一致"
            f"（缺失 {missing[:3]}，变更 {changed[:3]}）",
            reason_code="bundle_hash_mismatch",
            details={"missing": missing, "changed": changed},
        )
    recomputed_bundle = canonical_hash(
        {"snapshot_hash": manifest.get("snapshot_hash"), "files": actual}
    )
    if recomputed_bundle != manifest.get("bundle_hash"):
        raise NotReadyError(
            "重算的 bundle_hash 与 manifest 不一致",
            reason_code="bundle_hash_mismatch",
            details={"expected": manifest.get("bundle_hash"), "actual": recomputed_bundle},
        )
    return actual


def build_package(
    *,
    database: Database,
    structures: StructureRepository,
    packages: PackageRepository,
    structure_id: str,
    backend: Any,
    output_dir: Path,
    project_root: Path | None = None,
) -> PackageBuildOutcome:
    """从已批准的资料包构建不可变快照；不自动启用。"""
    import numpy as np

    view = packages.view(structure_id)
    approval = packages.approval(structure_id)
    if approval is None:
        raise NotReadyError(
            "资料包尚未批准，不能构建可启用快照",
            reason_code="struct_not_approved",
        )
    if approval["structure_hash"] != view.structure_hash:
        raise NotReadyError(
            "批准记录的结构 hash 与当前结构不一致",
            reason_code="catalog_structure_mismatch",
        )
    if approval["policy_hash"] != view.policy_hash:
        raise NotReadyError(
            "批准记录的策略 hash 与当前结构不一致",
            reason_code="catalog_structure_mismatch",
        )

    # 重新解析以取得完整正文（数据库只存卡载荷与来源）
    package = _resolve_package(view, project_root=project_root)
    ensure_package_complete(package)
    if package.structure_hash != view.structure_hash:
        raise NotReadyError(
            "重新解析得到的结构 hash 与已登记版本不一致，请重新解析后再批准",
            reason_code="report_version_mismatch",
            details={"expected": view.structure_hash, "actual": package.structure_hash},
        )

    backend.ensure_available()
    spec = backend.spec
    if not getattr(backend, "tokenizer_verified", False):
        raise NotReadyError(
            "缺少与编码模型匹配的可信 tokenizer，无法证明输入未截断",
            reason_code="tokenizer_unverified",
            details={"tokenizer": type(backend.token_counter).__name__, "model": spec.model_id},
        )

    encoded = package.encoded_cards
    inputs = [card.embedding_input for card in encoded]
    token_counts: dict[str, int] = {}
    counted_cards: list[ReferenceCard] = []
    for card in encoded:
        counted = backend.count_tokens(card.embedding_input)
        if counted > spec.max_tokens:
            raise NotReadyError(
                f"卡 {card.card_id} 输入 {counted} token 超过模型上限 {spec.max_tokens}",
                reason_code="card_input_over_limit",
                details={
                    "card_id": card.card_id,
                    "token_count": counted,
                    "limit": spec.max_tokens,
                    "model": spec.model_id,
                },
            )
        token_counts[card.card_id] = counted
        counted_cards.append(replace(card, token_count=counted))

    # 外部编码必须在写事务之外
    vectors = backend.embed_documents(inputs)
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape != (len(counted_cards), spec.dimension):
        raise NotReadyError(
            f"向量矩阵形状异常：{matrix.shape}",
            reason_code="embedding_dimension_mismatch",
            details={"expected": [len(counted_cards), spec.dimension], "actual": list(matrix.shape)},
        )
    if not np.all(np.isfinite(matrix)):
        raise NotReadyError(
            "向量包含非有限实数", reason_code="embedding_non_finite"
        )

    encoder_payload = encoder_config_payload(backend)
    encoder_config_hash = canonical_hash(encoder_payload)
    profile_hash = package.compute_profile_hash()
    policy = package.config.policy()
    index_id = new_id()
    directory = Path(output_dir) / index_id
    (directory / "sources").mkdir(parents=True, exist_ok=True)

    # 六份来源副本（含排除材料的版本记录不复制为生成载荷）
    for source in package.included_sources():
        target = directory / "sources" / Path(source.relative_path).name
        target.write_text(source.raw_text, encoding="utf-8")
    _write_json(directory / "residents.json", [item.to_payload() for item in package.residents])
    # 封存完整卡（26 张）：状态总则只常驻，不进向量矩阵
    token_by_id = {card.card_id: card.token_count for card in counted_cards}
    sealed_cards = [
        replace(card, token_count=token_by_id.get(card.card_id, card.token_count))
        for card in package.cards
    ]
    _write_json(directory / "cards.json", [card.to_payload() for card in sealed_cards])
    _write_json(
        directory / "profile.json",
        {
            "profile_version": package.config.profile_version,
            "profile_hash": profile_hash,
            "parser_version": PACKAGE_PARSER_VERSION,
            "included_files": [
                {"path": path, "role": role.value} for path, role in package.config.included_files
            ],
            "embedding_input_format": "title+type+scope+body",
        },
    )
    _write_json(directory / "policy.json", policy.to_payload())
    _write_json(
        directory / "catalog.json",
        {
            "version": policy.catalog_version,
            "catalog_hash": policy.catalog_hash,
            "state_catalog": dict(package.config.state_catalog),
            "card_conditions": {
                card_id: condition.to_payload()
                for card_id, condition in sorted(package.config.card_conditions.items())
            },
        },
    )
    _write_json(directory / "encoder.json", encoder_payload)
    np.save(directory / "vectors.npy", matrix)

    snapshot_hash = canonical_hash(
        {
            "source_set_hash": package.source_set_hash,
            "structure_hash": package.structure_hash,
            "profile_hash": profile_hash,
            "policy_hash": policy.policy_hash,
            "catalog_hash": policy.catalog_hash,
            "encoder_config_hash": encoder_config_hash,
            "residents": [
                {"resident_id": item.resident_id, "content_hash": item.content_hash}
                for item in package.residents
            ],
            "cards": [
                {
                    "card_id": card.card_id,
                    "type": card.type.value,
                    "input_hash": hashlib.sha256(card.embedding_input.encode("utf-8")).hexdigest(),
                    "token_count": card.token_count,
                }
                for card in counted_cards
            ],
            "vectors": {
                "shape": list(matrix.shape),
                "content_hash": hashlib.sha256(matrix.tobytes()).hexdigest(),
            },
        }
    )
    bundle_files = sealed_file_hashes(directory)
    bundle_hash = canonical_hash({"snapshot_hash": snapshot_hash, "files": bundle_files})
    manifest = {
        "index_id": index_id,
        "structure_id": structure_id,
        "index_format_version": PACKAGE_FORMAT_VERSION,
        "chunking_strategy": CHUNKING_STRATEGY_MARKDOWN,
        "parser_version": PACKAGE_PARSER_VERSION,
        "character": package.config.character,
        "story_stage": package.config.story_stage,
        "required_relationship": package.config.required_relationship,
        "source_set_hash": package.source_set_hash,
        "structure_hash": package.structure_hash,
        "profile_hash": profile_hash,
        "policy_hash": policy.policy_hash,
        "catalog_hash": policy.catalog_hash,
        "encoder_config_hash": encoder_config_hash,
        "snapshot_hash": snapshot_hash,
        "bundle_hash": bundle_hash,
        "bundle_files": bundle_files,
        "embedding_model_id": spec.model_id,
        "embedding_revision": spec.effective_revision,
        "dimension": spec.dimension,
        "resident_ids": [item.resident_id for item in package.residents],
        "resident_count": len(package.residents),
        "card_count": len(package.cards),
        "encoded_count": len(counted_cards),
        "card_counts": _count_cards(package),
        "token_counts": token_counts,
        "monologue_status": MonologueStatus.DISABLED_NO_MATERIAL.value,
        "monologue_material_count": 0,
        "build_status": "staged",
        "package_config": package.config.to_payload(),
        "issues": [issue.to_payload() for issue in package.issues],
    }
    manifest_hash = canonical_hash(manifest)
    manifest["manifest_hash"] = manifest_hash
    _write_json(directory / "manifest.json", manifest)

    now = to_utc_iso(utc_now())
    # 写事务内不再调用任何外部服务
    with database.write():
        structures.insert_snapshot(
            SnapshotRecord(
                id=index_id,
                source_hash=package.source_set_hash,
                structure_hash=package.structure_hash,
                split_profile_hash=None,
                snapshot_hash=snapshot_hash,
                manifest_hash=manifest_hash,
                parser_version=PACKAGE_PARSER_VERSION,
                splitter_version=CHUNKING_STRATEGY_MARKDOWN,
                embedding_model_id=spec.model_id,
                embedding_revision=spec.effective_revision,
                dimension=spec.dimension,
                rule_catalog_version=policy.catalog_hash,
                reply_calibration_id=None,
                monologue_calibration_id=None,
                monologue_status=MonologueStatus.DISABLED_NO_MATERIAL,
                status=IndexStatus.STAGED,
                artifact_path=str(directory),
                manifest=manifest,
                created_at=now,
                updated_at=now,
                index_format_version=PACKAGE_FORMAT_VERSION,
                chunking_strategy=CHUNKING_STRATEGY_MARKDOWN,
                structural_profile_hash=profile_hash,
                retrieval_policy_hash=policy.policy_hash,
                bundle_hash=bundle_hash,
                sealed_at=now,
            )
        )
        packages.save_structural_embeddings(
            index_id=index_id,
            structure_id=structure_id,
            cards=counted_cards,
            model_id=spec.model_id,
            revision=spec.effective_revision,
            dimension=spec.dimension,
            encoder_config_hash=encoder_config_hash,
        )
    return PackageBuildOutcome(
        index_id=index_id,
        structure_id=structure_id,
        snapshot_hash=snapshot_hash,
        bundle_hash=bundle_hash,
        manifest_hash=manifest_hash,
        artifact_path=directory,
        resident_count=len(package.residents),
        card_count=len(package.cards),
        encoded_count=len(counted_cards),
        dimension=spec.dimension,
        token_counts=token_counts,
        encoder_config_hash=encoder_config_hash,
        monologue_status=MonologueStatus.DISABLED_NO_MATERIAL.value,
        metrics={
            "profile_hash": profile_hash,
            "policy_hash": policy.policy_hash,
            "catalog_hash": policy.catalog_hash,
            "model": spec.model_id,
            "revision": spec.effective_revision,
        },
    )


def _resolve_package(view: Any, *, project_root: Path | None) -> CharacterPackage:
    """从已登记配置重新解析来源文件，得到完整正文与卡的完整载荷。"""
    root = Path(project_root or PROJECT_ROOT)
    source_root = (root / view.config.source_root).resolve()
    if not source_root.is_dir():
        raise NotReadyError(
            f"来源目录不可用，无法复核封存内容：{source_root}",
            reason_code="package_file_missing",
        )
    return _rebuild_from_view(view, source_root=source_root)


def _rebuild_from_view(view: Any, *, source_root: Path) -> CharacterPackage:
    """用已登记配置与磁盘来源重建内存包（不信任数据库里的卡载荷正文）。"""
    from sana_bot.ingest.package_models import SourceFileVersion

    sources: list[SourceFileVersion] = []
    for source in view.sources:
        if not source.included:
            sources.append(source)
            continue
        path = source_root / Path(source.relative_path).name
        if not path.is_file():
            raise NotReadyError(
                f"来源文件缺失：{source.relative_path}",
                reason_code="package_file_missing",
                details={"file": str(path)},
            )
        text = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != source.byte_hash:
            raise NotReadyError(
                f"来源文件内容已变更：{source.relative_path}",
                reason_code="bundle_hash_mismatch",
                details={"expected": source.byte_hash, "actual": digest},
            )
        sources.append(replace(source, raw_text=text))
    return CharacterPackage(
        config=view.config,
        sources=tuple(sources),
        residents=view.residents,
        cards=view.cards,
        issues=(),
        structure_hash=view.structure_hash,
        source_set_hash=view.source_set_hash,
    )
