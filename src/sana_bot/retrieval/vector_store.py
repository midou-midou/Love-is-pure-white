"""不可变向量索引的加载与精确检索。

校验 manifest / hash / revision / 维度 / 有限实数；检索时不混索引版本，
异常分数不会被伪装成低分。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from sana_bot.enums import RetrievalStage
from sana_bot.errors import NotReadyError
from sana_bot.ingest.models import SemanticUnit
from sana_bot.ingest.package_models import (
    CardCondition,
    ReferenceCard,
    ResidentMaterial,
    canonical_hash,
)
from sana_bot.ingest.package_service import sealed_file_hashes
from sana_bot.storage.index_repository import SnapshotRecord, StructureRepository

__all__ = ["VectorIndex", "StructuralVectorIndex", "VectorStore", "cosine_scores"]


@dataclass(frozen=True)
class VectorIndex:
    """一次编排好的检索视图；同一时刻只对应一个 index_id。"""

    index_id: str
    snapshot_hash: str
    model_id: str
    revision: str
    dimension: int
    unit_ids: tuple[str, ...]
    matrix: np.ndarray
    units: tuple[SemanticUnit, ...]

    def unit(self, unit_id: str) -> SemanticUnit | None:
        for unit in self.units:
            if unit.id == unit_id:
                return unit
        return None


@dataclass(frozen=True)
class StructuralVectorIndex:
    """一张卡一行向量的不可变视图；与包内常驻、条件与策略同版本。"""

    index_id: str
    snapshot_hash: str
    bundle_hash: str
    model_id: str
    revision: str
    dimension: int
    card_ids: tuple[str, ...]
    matrix: np.ndarray
    cards: tuple[ReferenceCard, ...]
    residents: tuple[ResidentMaterial, ...]
    policy: Mapping[str, Any]
    catalog: Mapping[str, Any]
    encoder_config_hash: str

    def card(self, card_id: str) -> ReferenceCard | None:
        for card in self.cards:
            if card.card_id == card_id:
                return card
        return None

    def position_of(self, card_id: str) -> int | None:
        try:
            return self.card_ids.index(card_id)
        except ValueError:
            return None


def cosine_scores(query: Sequence[float], matrix: np.ndarray) -> np.ndarray:
    """固定定义的余弦相似度；零向量返回 0，不伪装成低分。"""
    query_vector = np.asarray(query, dtype=np.float32)
    if query_vector.shape[0] != matrix.shape[1]:
        raise NotReadyError(
            "查询向量维度与索引不一致", reason_code="embedding_dimension_mismatch"
        )
    norms = np.linalg.norm(matrix, axis=1) * float(np.linalg.norm(query_vector))
    with np.errstate(divide="ignore", invalid="ignore"):
        scores = np.where(norms > 0, matrix @ query_vector / norms, 0.0)
    return np.asarray(scores, dtype=np.float32)


class VectorStore:
    """按 index_id 加载向量；禁止静默换模型或混用版本。"""

    def __init__(self, *, structures: StructureRepository, cache_size: int = 2) -> None:
        self.structures = structures
        self._cache: dict[str, VectorIndex] = {}
        self._structural_cache: dict[tuple[str, str], StructuralVectorIndex] = {}
        self._cache_size = cache_size

    def load_structural(self, snapshot: SnapshotRecord) -> StructuralVectorIndex:
        """装载 structural_v2：重算封存文件、来源、向量与 DB 绑定哈希。

        缓存按 (index_id, bundle_hash) 定位；驱逐只影响后续装载，
        已持有的旧引用在本轮内保持有效。
        """
        key = (snapshot.id, snapshot.bundle_hash or "")
        cached = self._structural_cache.get(key)
        if cached is not None:
            return cached
        directory = Path(snapshot.artifact_path)
        manifest_path = directory / "manifest.json"
        if not manifest_path.exists():
            raise NotReadyError(
                f"索引产物缺少 manifest：{directory}", reason_code="index_artifact_missing"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("snapshot_hash") != snapshot.snapshot_hash:
            raise NotReadyError(
                "manifest 与数据库中的 snapshot_hash 不一致",
                reason_code="snapshot_hash_mismatch",
            )
        if manifest.get("bundle_hash") != snapshot.bundle_hash:
            raise NotReadyError(
                "manifest 与数据库中的 bundle_hash 不一致",
                reason_code="bundle_hash_mismatch",
            )
        if manifest.get("structure_hash") != snapshot.structure_hash:
            raise NotReadyError(
                "manifest 的 structure_hash 与快照记录不一致",
                reason_code="bundle_hash_mismatch",
            )
        if manifest.get("profile_hash") != snapshot.structural_profile_hash:
            raise NotReadyError(
                "manifest 的 profile_hash 与快照记录不一致",
                reason_code="bundle_hash_mismatch",
            )
        if manifest.get("policy_hash") != snapshot.retrieval_policy_hash:
            raise NotReadyError(
                "manifest 的 policy_hash 与快照记录不一致",
                reason_code="bundle_hash_mismatch",
            )
        if (
            manifest.get("embedding_model_id") != snapshot.embedding_model_id
            or manifest.get("embedding_revision") != snapshot.embedding_revision
        ):
            raise NotReadyError(
                "索引模型 revision 与快照记录不一致",
                reason_code="embedding_revision_mismatch",
            )
        actual_files = sealed_file_hashes(directory)
        recomputed_bundle = canonical_hash(
            {"snapshot_hash": manifest.get("snapshot_hash"), "files": actual_files}
        )
        if recomputed_bundle != snapshot.bundle_hash:
            raise NotReadyError(
                "重算的 bundle_hash 与数据库记录不一致",
                reason_code="bundle_hash_mismatch",
                details={"expected": snapshot.bundle_hash, "actual": recomputed_bundle},
            )
        matrix = np.load(directory / "vectors.npy")
        cards = tuple(
            ReferenceCard.from_payload(item)
            for item in json.loads((directory / "cards.json").read_text(encoding="utf-8"))
        )
        residents = tuple(
            ResidentMaterial(
                resident_id=str(item["resident_id"]),
                ordinal=int(item["ordinal"]),
                title=str(item["title"]),
                raw_text=str(item["raw_text"]),
                source_id=str(item["source_id"]),
                required=bool(item.get("required", True)),
                content_hash=str(item.get("content_hash", "")),
                token_count=int(item.get("token_count", 0)),
            )
            for item in json.loads((directory / "residents.json").read_text(encoding="utf-8"))
        )
        policy = json.loads((directory / "policy.json").read_text(encoding="utf-8"))
        policy["resident_ids"] = list(manifest.get("resident_ids") or ())
        policy["card_conditions"] = {
            card_id: CardCondition(
                card_id=card_id,
                relationship=str(item.get("relationship", "any")),
                scene=str(item.get("scene", "any")),
                medium=str(item.get("medium", "any")),
                state_tags=tuple(str(tag) for tag in item.get("state_tags", ())),
                card_states=tuple(str(state) for state in item.get("card_states", ())),
                resident=bool(item.get("resident", False)),
            )
            for card_id, item in (policy.get("card_conditions") or {}).items()
        }
        catalog = json.loads((directory / "catalog.json").read_text(encoding="utf-8"))
        encoded = tuple(card for card in cards if card.encoded)
        if matrix.ndim != 2 or matrix.shape[0] != len(encoded):
            raise NotReadyError(
                f"向量矩阵与完整卡数量不一致：{matrix.shape} vs {len(encoded)}",
                reason_code="index_matrix_shape_mismatch",
            )
        if matrix.shape[1] != snapshot.dimension:
            raise NotReadyError(
                "向量维度与快照记录不一致", reason_code="embedding_dimension_mismatch"
            )
        if not np.all(np.isfinite(matrix)):
            raise NotReadyError("向量包含非有限实数", reason_code="embedding_non_finite")
        for card in encoded:
            if card.token_count <= 0:
                raise NotReadyError(
                    f"卡 {card.card_id} 缺少精确 token 计数",
                    reason_code="tokenizer_unverified",
                )
        index = StructuralVectorIndex(
            index_id=snapshot.id,
            snapshot_hash=snapshot.snapshot_hash,
            bundle_hash=snapshot.bundle_hash or "",
            model_id=snapshot.embedding_model_id,
            revision=snapshot.embedding_revision,
            dimension=snapshot.dimension,
            card_ids=tuple(card.card_id for card in encoded),
            matrix=np.asarray(matrix, dtype=np.float32),
            cards=cards,
            residents=residents,
            policy=policy,
            catalog=catalog,
            encoder_config_hash=str(manifest.get("encoder_config_hash", "")),
        )
        self._structural_cache[key] = index
        while len(self._structural_cache) > self._cache_size:
            oldest = next(iter(self._structural_cache))
            self._structural_cache.pop(oldest, None)
        return index

    def forget(self, index_id: str) -> None:
        """仅清理活动缓存条目；在途轮次持有的旧引用不受影响。"""
        self._cache.pop(index_id, None)
        for key in [key for key in self._structural_cache if key[0] == index_id]:
            self._structural_cache.pop(key, None)

    def index_ids(self) -> list[str]:
        return sorted(self._cache)

    def load(self, snapshot: SnapshotRecord) -> VectorIndex:
        cached = self._cache.get(snapshot.id)
        if cached is not None:
            return cached
        directory = Path(snapshot.artifact_path)
        manifest_path = directory / "manifest.json"
        vectors_path = directory / "vectors.npy"
        if not manifest_path.exists() or not vectors_path.exists():
            raise NotReadyError(
                f"索引产物不完整：{snapshot.id}", reason_code="index_artifact_missing"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("snapshot_hash") != snapshot.snapshot_hash:
            raise NotReadyError(
                "manifest 与数据库中的 snapshot_hash 不一致",
                reason_code="snapshot_hash_mismatch",
            )
        embedding = manifest.get("embedding_model_id")
        revision = manifest.get("embedding_revision")
        if embedding != snapshot.embedding_model_id or revision != snapshot.embedding_revision:
            raise NotReadyError(
                "索引模型 revision 与快照记录不一致",
                reason_code="embedding_revision_mismatch",
            )
        matrix = np.load(vectors_path)
        units = self.structures.load_semantic_units(snapshot.id)
        records = self.structures.load_embedding_records(snapshot.id)
        if matrix.ndim != 2 or matrix.shape[0] != len(units):
            raise NotReadyError(
                f"向量矩阵与片段数量不一致：{matrix.shape} vs {len(units)}",
                reason_code="index_matrix_shape_mismatch",
            )
        if matrix.shape[1] != snapshot.dimension:
            raise NotReadyError(
                "向量维度与快照记录不一致", reason_code="embedding_dimension_mismatch"
            )
        if not np.all(np.isfinite(matrix)):
            raise NotReadyError("向量包含非有限实数", reason_code="embedding_non_finite")
        ordered = sorted(units, key=lambda unit: _offset_for(unit.id, records))
        index = VectorIndex(
            index_id=snapshot.id,
            snapshot_hash=snapshot.snapshot_hash,
            model_id=snapshot.embedding_model_id,
            revision=snapshot.embedding_revision,
            dimension=snapshot.dimension,
            unit_ids=tuple(unit.id for unit in ordered),
            matrix=np.asarray(matrix, dtype=np.float32),
            units=tuple(ordered),
        )
        self._remember(index)
        return index

    def _remember(self, index: VectorIndex) -> None:
        self._cache[index.index_id] = index
        while len(self._cache) > self._cache_size:
            oldest = next(iter(self._cache))
            self._cache.pop(oldest, None)

    def stage_for(self, stage: RetrievalStage) -> str:
        return stage.value


def _offset_for(unit_id: str, records: Sequence[object]) -> int:
    for record in records:
        if getattr(record, "unit_id", None) == unit_id:
            return int(getattr(record, "vector_offset", 0))
    return math.inf
