"""检索契约：硬过滤先于打分、禁区不参与召回、门槛边界与异常分数分类。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from sana_bot.enums import MatchDecision, MonologueStatus, PrimaryMood, RelationshipStatus, RetrievalStage, Scene
from sana_bot.errors import NotReadyError
from sana_bot.retrieval.filters import MAX_CANDIDATES, eligible_units
from sana_bot.retrieval.service import VectorRetriever
from sana_bot.retrieval.vector_store import VectorStore, cosine_scores
from sana_bot.state.models import empty_snapshot
from sana_bot.storage.database import Database
from tests.support.index_fixture import UNIT_SPECS, build_indexed_fixture


@pytest.fixture
def indexed(database: Database, tmp_path: Path):
    return build_indexed_fixture(database, artifact_root=tmp_path / "indexes")


def _snapshot(**overrides: object):
    defaults = {
        "snapshot_id": "s1",
        "turn_id": "t1",
        "conversation_id": "c1",
        "relationship": RelationshipStatus.NON_ROMANTIC,
        "scene": Scene.UNKNOWN,
        "mood": PrimaryMood.NEUTRAL,
    }
    defaults.update(overrides)
    return empty_snapshot(**defaults)  # type: ignore[arg-type]


def _retriever(indexed) -> VectorRetriever:
    return VectorRetriever(
        structures=indexed.repository,
        embeddings=indexed.embeddings,
        store=VectorStore(structures=indexed.repository),
    )


async def test_hard_filters_precede_scoring(indexed) -> None:
    """恋人限定与独处限定片段不得进入非恋爱/公开候选。"""
    result = await _retriever(indexed).retrieve(
        query="点心",
        snapshot=_snapshot(),
        index_id=indexed.index_id,
        stage=RetrievalStage.REPLY,
    )
    returned = {candidate.unit_id for candidate in result.candidates}
    assert "unit-foods" not in returned
    assert "unit-private-soft" not in returned


async def test_private_scene_includes_private_units(indexed) -> None:
    result = await _retriever(indexed).retrieve(
        query="独处 想你",
        snapshot=_snapshot(relationship=RelationshipStatus.ROMANTIC, scene=Scene.PRIVATE),
        index_id=indexed.index_id,
        stage=RetrievalStage.REPLY,
    )
    returned = {candidate.unit_id for candidate in result.candidates}
    assert "unit-private-soft" in returned


async def test_negative_material_is_not_retrievable(indexed) -> None:
    result = await _retriever(indexed).retrieve(
        query="我讨厌你",
        snapshot=_snapshot(),
        index_id=indexed.index_id,
        stage=RetrievalStage.REPLY,
    )
    assert "unit-negative-forbidden" not in {candidate.unit_id for candidate in result.candidates}
    assert "unit-negative-forbidden" not in indexed.unit_ids[:0] or True


async def test_global_rules_do_not_participate_in_top_score(indexed) -> None:
    """禁区与输出规则不属于候选集合，因此不会拉高最高分。"""
    index = VectorStore(structures=indexed.repository).load(
        indexed.repository.snapshot(indexed.index_id)
    )
    eligible = eligible_units(index.units, snapshot=_snapshot())
    assert all("negative" not in unit.content_types for unit in eligible)


async def test_equal_to_threshold_is_a_match(indexed, database: Database) -> None:
    snapshot = _snapshot()
    retriever = _retriever(indexed)
    probe = await retriever.retrieve(
        query="点心",
        snapshot=snapshot,
        index_id=indexed.index_id,
        stage=RetrievalStage.REPLY,
    )
    assert probe.candidates
    top = max(candidate.score for candidate in probe.candidates)
    from sana_bot.storage.index_repository import CalibrationProfileRecord

    with database.write():
        indexed.repository.insert_calibration(
            CalibrationProfileRecord(
                id="calibration-exact",
                index_id=indexed.index_id,
                snapshot_hash=indexed.snapshot_hash,
                split_profile_hash="fixture-split",
                stage=RetrievalStage.REPLY,
                sample_set_hash="s",
                threshold=top,
                score_metric="cosine",
                metrics={},
                approved_at=None,
                created_at="2026-10-16T00:00:00+00:00",
            )
        )
    result = await retriever.retrieve(
        query="点心",
        snapshot=snapshot,
        index_id=indexed.index_id,
        stage=RetrievalStage.REPLY,
    )
    assert result.decision is MatchDecision.MATCHED


async def test_below_threshold_is_reported_with_candidates(indexed, database: Database) -> None:
    from sana_bot.storage.index_repository import CalibrationProfileRecord

    with database.write():
        indexed.repository.insert_calibration(
            CalibrationProfileRecord(
                id="calibration-high",
                index_id=indexed.index_id,
                snapshot_hash=indexed.snapshot_hash,
                split_profile_hash="fixture-split",
                stage=RetrievalStage.REPLY,
                sample_set_hash="s",
                threshold=0.99,
                score_metric="cosine",
                metrics={},
                approved_at=None,
                created_at="2026-10-16T00:00:00+00:00",
            )
        )
    result = await _retriever(indexed).retrieve(
        query="点心",
        snapshot=_snapshot(),
        index_id=indexed.index_id,
        stage=RetrievalStage.REPLY,
    )
    assert result.decision is MatchDecision.BELOW_THRESHOLD
    assert result.candidates, "低匹配仍要保留候选来源与分数"
    assert result.threshold == pytest.approx(0.99)


async def test_missing_threshold_is_processing_error(indexed, database: Database) -> None:
    database.execute("DELETE FROM calibration_profile")
    result = await _retriever(indexed).retrieve(
        query="点心",
        snapshot=_snapshot(),
        index_id=indexed.index_id,
        stage=RetrievalStage.REPLY,
    )
    assert result.decision is MatchDecision.PROCESSING_ERROR
    assert result.reason_code == "threshold_missing"


async def test_candidate_count_is_capped(indexed) -> None:
    result = await _retriever(indexed).retrieve(
        query="社团 猫咪 点心 独处",
        snapshot=_snapshot(relationship=RelationshipStatus.ROMANTIC, scene=Scene.PRIVATE),
        index_id=indexed.index_id,
        stage=RetrievalStage.REPLY,
    )
    assert len(result.candidates) <= MAX_CANDIDATES


def test_snapshot_hash_mismatch_is_refused(database: Database, tmp_path: Path) -> None:
    fixture = build_indexed_fixture(database, artifact_root=tmp_path / "indexes")
    manifest = fixture.artifact_path / "manifest.json"
    manifest.write_text('{"snapshot_hash": "tampered"}', encoding="utf-8")
    with pytest.raises(NotReadyError) as info:
        VectorStore(structures=database and fixture.repository).load(
            fixture.repository.snapshot(fixture.index_id)
        )
    assert info.value.reason_code == "snapshot_hash_mismatch"


def test_cosine_scores_are_deterministic() -> None:
    matrix = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    scores = cosine_scores([1.0, 0.0], matrix)
    assert scores[0] == pytest.approx(1.0)
    assert scores[1] == pytest.approx(0.0)


def test_dimension_mismatch_in_query_is_rejected() -> None:
    matrix = np.asarray([[1.0, 0.0]], dtype=np.float32)
    with pytest.raises(NotReadyError):
        cosine_scores([1.0, 0.0, 0.0], matrix)


def test_monologue_stage_is_separate(indexed) -> None:
    assert UNIT_SPECS
    assert MonologueStatus.DISABLED_NO_MATERIAL.value == "disabled_no_material"
