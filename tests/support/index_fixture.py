"""构造可检索的临时索引夹具，供检索与对话链路测试共用。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from sana_bot.enums import IndexStatus, MonologueStatus, RetrievalStage
from sana_bot.ingest.models import SemanticUnit, canonical_json
from sana_bot.retrieval.embeddings import EmbeddingBackend, EmbeddingSpec
from sana_bot.storage.database import Database
from sana_bot.storage.index_repository import (
    CalibrationProfileRecord,
    SnapshotRecord,
    StructureRepository,
)
from sana_bot.timeutil import to_utc_iso, utc_now
from tests.support import FakeEmbeddingsProvider

__all__ = ["IndexedFixture", "build_indexed_fixture", "UNIT_SPECS"]

#: 片段规格：文本、内容类型、条件、说话者
UNIT_SPECS: tuple[dict[str, object], ...] = (
    {
        "id": "unit-club-cat",
        "text": "今天社团来了一只流浪猫，猫咪还一直跟着我",
        "types": ("example", "dialogue"),
        "conditions": {"relationship": "any", "scene": "any"},
        "speakers": ("sana",),
    },
    {
        "id": "unit-club-quiet",
        "text": "社团里安静得有点不习惯，猫咪跑去晒太阳了",
        "types": ("example",),
        "conditions": {"relationship": "any", "scene": "any"},
        "speakers": ("sana",),
    },
    {
        "id": "unit-foods",
        "text": "点心刚好做多了一份，你拿去吃吧",
        "types": ("example", "dialogue"),
        "conditions": {"relationship": "romantic", "scene": "any"},
        "speakers": ("sana",),
    },
    {
        "id": "unit-private-soft",
        "text": "独处的时候才会小声说一句想你",
        "types": ("example",),
        "conditions": {"relationship": "romantic", "scene": "private"},
        "speakers": ("sana",),
    },
    {
        "id": "unit-sad-chew",
        "text": "被冷落之后只会安静地待着",
        "types": ("state",),
        "conditions": {"relationship": "any", "scene": "any", "primary_moods": ["sad"]},
        "speakers": (),
    },
    {
        "id": "unit-negative-forbidden",
        "text": "不会说出“我讨厌你”这类台词",
        "types": ("negative",),
        "conditions": {"relationship": "any", "scene": "any"},
        "speakers": (),
    },
)


@dataclass
class IndexedFixture:
    index_id: str
    snapshot_hash: str
    repository: StructureRepository
    embeddings: EmbeddingBackend
    artifact_path: Path
    unit_ids: tuple[str, ...]


def _unit(spec: dict[str, object]) -> SemanticUnit:
    return SemanticUnit(
        id=str(spec["id"]),
        index_version="fixture",
        source_span_ids=(f"span-{spec['id']}",),
        source_node_ids=(f"node-{spec['id']}",),
        speaker_labels=tuple(spec.get("speakers") or ()),  # type: ignore[arg-type]
        content_types=tuple(spec.get("types") or ()),  # type: ignore[arg-type]
        topic=str(spec["text"])[:20],
        text=str(spec["text"]),
        boundary_evidence={"method": "fixture"},
        conditions=dict(spec.get("conditions") or {}),  # type: ignore[arg-type]
        token_count=len(str(spec["text"])),
    )


def build_indexed_fixture(
    database: Database,
    *,
    artifact_root: Path,
    threshold: float = 0.55,
    status: IndexStatus = IndexStatus.ACTIVE,
    units: Sequence[dict[str, object]] = UNIT_SPECS,
    monologue_status: MonologueStatus = MonologueStatus.DISABLED_NO_MATERIAL,
) -> IndexedFixture:
    """写入快照、片段、向量与已批准门槛，返回可检索夹具。"""
    repository = StructureRepository(database)
    provider = FakeEmbeddingsProvider()
    embeddings = EmbeddingBackend(
        EmbeddingSpec(model_id="fake/model", revision="fixture-revision"),
        embeddings_factory=lambda _spec: provider,
    )
    materialised = [_unit(spec) for spec in units]
    texts = [unit.text for unit in materialised]
    vectors = embeddings.embed_documents(texts)
    matrix = np.asarray(vectors, dtype=np.float32)

    index_id = "fixture-index"
    directory = artifact_root / index_id
    directory.mkdir(parents=True, exist_ok=True)
    np.save(directory / "vectors.npy", matrix)
    (directory / "units.json").write_text(
        json.dumps([unit.to_payload() for unit in materialised], ensure_ascii=False), encoding="utf-8"
    )
    manifest = {
        "index_id": index_id,
        "snapshot_hash": "",
        "embedding_model_id": "fake/model",
        "embedding_revision": "fixture-revision",
        "dimension": matrix.shape[1],
        "unit_count": len(materialised),
        "monologue_status": monologue_status.value,
    }
    snapshot_hash = hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
    manifest["snapshot_hash"] = snapshot_hash
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )

    now = to_utc_iso(utc_now())
    with database.write():
        repository.insert_snapshot(
            SnapshotRecord(
                id=index_id,
                source_hash="fixture-source",
                structure_hash="fixture-structure",
                split_profile_hash="fixture-split",
                snapshot_hash=snapshot_hash,
                manifest_hash="fixture-manifest",
                parser_version="fixture",
                splitter_version="fixture",
                embedding_model_id="fake/model",
                embedding_revision="fixture-revision",
                dimension=matrix.shape[1],
                rule_catalog_version="fixture",
                reply_calibration_id=None,
                monologue_calibration_id=None,
                monologue_status=monologue_status,
                status=status,
                artifact_path=str(directory),
                manifest=manifest,
                created_at=now,
                updated_at=now,
            )
        )
        repository.save_semantic_units(index_id, materialised)
        repository.save_embedding_records(
            [
                __import__("sana_bot.ingest.models", fromlist=["EmbeddingRecord"]).EmbeddingRecord(
                    unit_id=unit.id,
                    model_id="fake/model",
                    revision="fixture-revision",
                    dimension=matrix.shape[1],
                    input_hash=hashlib.sha256(unit.text.encode("utf-8")).hexdigest(),
                    token_count=unit.token_count,
                    vector_offset=offset,
                )
                for offset, unit in enumerate(materialised)
            ],
            index_id=index_id,
        )
        repository.insert_calibration(
            CalibrationProfileRecord(
                id="calibration-reply",
                index_id=index_id,
                snapshot_hash=snapshot_hash,
                split_profile_hash="fixture-split",
                stage=RetrievalStage.REPLY,
                sample_set_hash="fixture-samples",
                threshold=threshold,
                score_metric="cosine",
                metrics={"fixture": True},
                approved_at=now,
                created_at=now,
            )
        )
        database.execute(
            "UPDATE index_snapshot SET reply_calibration_id = ? WHERE id = ?",
            ("calibration-reply", index_id),
        )
    return IndexedFixture(
        index_id=index_id,
        snapshot_hash=snapshot_hash,
        repository=repository,
        embeddings=embeddings,
        artifact_path=directory,
        unit_ids=tuple(unit.id for unit in materialised),
    )
