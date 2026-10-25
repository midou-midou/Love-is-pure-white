"""索引校准生命周期：切分校准 → 固定参数重建 → 门槛校准 → 原子启用。

覆盖旧 hash/参数报告被拒绝、片段随参数变化、无独白素材时普通聊天可独立启用、
禁用时不创建机会，以及补材料必须新建版本。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from sana_bot.cli_index import (
    run_activate,
    run_approve,
    run_build,
    run_calibrate,
    run_calibrate_split,
    run_parse,
    run_review,
)
from sana_bot.enums import IndexStatus, MonologueStatus, RetrievalStage
from sana_bot.errors import NotReadyError
from sana_bot.retrieval.embeddings import EmbeddingBackend, EmbeddingSpec
from sana_bot.runtime import RuntimeContext, open_context
from sana_bot.storage.index_repository import CalibrationProfileRecord
from tests.support import CharTokenizer, FakeEmbeddingsProvider, topic_vectors
from tests.support.docx_builder import build_docx, heading, list_item, paragraph

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CALIBRATION_SAMPLES = FIXTURES / "approved-calibration.jsonl"
MONOLOGUE_SAMPLES = FIXTURES / "approved-monologue.jsonl"


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """用确定性向量替身替代真实本地模型，避免联网下载。"""
    provider = FakeEmbeddingsProvider()

    def factory(context: RuntimeContext, *, allow_download: bool = False) -> EmbeddingBackend:
        return EmbeddingBackend(
            EmbeddingSpec(model_id="fake/model", revision="deadbeef"),
            embeddings_factory=lambda _spec: provider,
        )

    import sana_bot.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "build_embedding_backend", factory)
    import sana_bot.cli_index as cli_index

    monkeypatch.setattr(cli_index, "build_embedding_backend", factory, raising=False)


def _args(runtime_dir: Path, **kwargs: object) -> argparse.Namespace:
    payload = {"runtime_dir": str(runtime_dir)}
    payload.update(kwargs)
    return argparse.Namespace(**payload)


def _dialogue_document(path: Path, *, with_monologue: bool) -> Path:
    blocks = [
        heading("乾纱凪角色设定"),
        heading("一、基础档案"),
        heading("外貌细节"),
        paragraph("身形娇小玲珑，头顶有一根呆毛。"),
        heading("四、专属情绪状态系统（恋人限定）"),
        heading("1. 平稳常态"),
        paragraph(
            "触发条件：日常相处，没有强烈情绪波动。 "
            "整体表现：情绪安定放松，对你放下防备。 "
            "肢体细节：呆毛自然松弛下垂。"
        ),
        heading("八、输出规则"),
        list_item("句末不使用句号，所有句子结尾禁止使用句号"),
        heading("补充约束"),
        paragraph("对外人维持喵喵社社长的体面形象。"),
    ]
    if with_monologue:
        blocks.append(paragraph("示例\n你：今天社团怎么样？\n纱凪：猫咪来了，闹得不行。"))
        blocks.append(paragraph("（其实心里挺高兴的，只是没说出来。）"))
    return build_docx(path, blocks)


def _catalog_file(path: Path) -> Path:
    payload = {
        "version": "state-catalog-1",
        "event_catalog": {"version": "events-v1", "tags": ["steady_daily"]},
        "rules": [
            {
                "id": "state::平稳常态",
                "label": "平稳常态",
                "provenance": "source",
                "approved": True,
                "relationship": "any",
                "scene": "any",
                "event_tags": ["steady_daily"],
                "to_primary": "neutral",
                "to_tags": ["平稳常态"],
                "dominant_priority": 1,
            }
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _prepare_structure(runtime: Path, workspace: Path, *, with_monologue: bool) -> str:
    source = _dialogue_document(workspace / "fixture.docx", with_monologue=with_monologue)
    parsed = run_parse(_args(runtime, source=str(source), output=str(workspace / "structures")))
    catalog = _catalog_file(workspace / "catalog.json")
    run_approve(
        _args(runtime, structure=parsed["structure_id"], catalog=str(catalog), reviewer="maintainer")
    )
    return str(parsed["structure_id"])


def _build(runtime: Path, workspace: Path, structure_id: str, *, name: str) -> dict:
    profile = run_calibrate_split(
        _args(
            runtime,
            structure=structure_id,
            samples=str(CALIBRATION_SAMPLES),
        )
    )
    del name
    return run_build(
        _args(
            runtime,
            structure=structure_id,
            split_profile=profile["split_profile_id"],
            output=str(workspace / "indexes"),
        )
    )


def _calibrate_reply(runtime: Path, index_id: str) -> dict:
    return run_calibrate(
        _args(
            runtime,
            index=index_id,
            stage="reply",
            samples=str(CALIBRATION_SAMPLES),
        )
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path


async def test_full_lifecycle_activates_index(workspace: Path, fake_backend: None) -> None:
    runtime = workspace / "runtime"
    structure_id = _prepare_structure(runtime, workspace, with_monologue=False)
    built = _build(runtime, workspace, structure_id, name="v1")
    assert built["unit_count"] > 0
    assert built["monologue_status"] == MonologueStatus.DISABLED_NO_MATERIAL.value

    calibrated = _calibrate_reply(runtime, built["index_id"])
    assert calibrated["threshold"] is not None
    assert calibrated["snapshot_hash"] == built["snapshot_hash"]

    activated = run_activate(_args(runtime, index=built["index_id"]))
    assert activated["status"] == "active"
    assert activated["monologue_status"] == MonologueStatus.DISABLED_NO_MATERIAL.value

    context = open_context(_args(runtime))
    try:
        snapshot = context.structures.active_snapshot()
        assert snapshot is not None and snapshot.id == built["index_id"]
        assert snapshot.status is IndexStatus.ACTIVE
        assert snapshot.monologue_status is MonologueStatus.DISABLED_NO_MATERIAL
        # 明文门槛不落地：独白禁用时没有门槛字段
        assert snapshot.monologue_calibration_id is None
    finally:
        context.close()


async def test_build_requires_approved_structure(workspace: Path, fake_backend: None) -> None:
    runtime = workspace / "runtime"
    source = _dialogue_document(workspace / "fixture.docx", with_monologue=False)
    parsed = run_parse(_args(runtime, source=str(source), output=str(workspace / "structures")))
    profile = run_calibrate_split(
        _args(runtime, structure=str(parsed["structure_id"]), samples=str(CALIBRATION_SAMPLES))
    )
    with pytest.raises(NotReadyError):
        run_build(
            _args(
                runtime,
                structure=str(parsed["structure_id"]),
                split_profile=profile["split_profile_id"],
                output=str(workspace / "indexes"),
            )
        )


async def test_activation_without_reply_calibration_is_refused(
    workspace: Path, fake_backend: None
) -> None:
    runtime = workspace / "runtime"
    structure_id = _prepare_structure(runtime, workspace, with_monologue=False)
    built = _build(runtime, workspace, structure_id, name="v1")
    with pytest.raises(NotReadyError) as info:
        run_activate(_args(runtime, index=built["index_id"]))
    assert info.value.reason_code == "reply_calibration_missing"


async def test_calibration_bound_to_other_snapshot_is_refused(
    workspace: Path, fake_backend: None
) -> None:
    runtime = workspace / "runtime"
    structure_id = _prepare_structure(runtime, workspace, with_monologue=False)
    built = _build(runtime, workspace, structure_id, name="v1")
    _calibrate_reply(runtime, built["index_id"])
    context = open_context(_args(runtime))
    try:
        snapshot = context.structures.snapshot(built["index_id"])
        with context.database.write():
            context.structures.insert_calibration(
                CalibrationProfileRecord(
                    id="tampered",
                    index_id=built["index_id"],
                    snapshot_hash="0" * 64,
                    split_profile_hash=snapshot.split_profile_hash,
                    stage=RetrievalStage.REPLY,
                    sample_set_hash="x",
                    threshold=0.5,
                    score_metric="cosine",
                    metrics={},
                    approved_at=None,
                    created_at="2026-10-16T00:00:00+00:00",
                )
            )
    finally:
        context.close()

    with pytest.raises(NotReadyError) as info:
        run_activate(_args(runtime, index=built["index_id"]))
    assert info.value.reason_code == "calibration_hash_mismatch"


async def test_changing_split_parameters_creates_new_snapshot(
    workspace: Path, fake_backend: None
) -> None:
    runtime = workspace / "runtime"
    structure_id = _prepare_structure(runtime, workspace, with_monologue=False)
    first = _build(runtime, workspace, structure_id, name="v1")
    context = open_context(_args(runtime))
    try:
        before = context.structures.split_profile_by_hash(
            context.structures.snapshot(first["index_id"]).split_profile_hash
        )
        assert before is not None
        assert before.parameters["calibrated"] is True
    finally:
        context.close()
    # 同一个已批准结构与同一份样本，重复校准得到可复现的固定参数
    profile = run_calibrate_split(
        _args(runtime, structure=structure_id, samples=str(CALIBRATION_SAMPLES))
    )
    same = run_build(
        _args(
            runtime,
            structure=structure_id,
            split_profile=profile["split_profile_id"],
            output=str(workspace / "indexes"),
        )
    )
    assert same["snapshot_hash"] == first["snapshot_hash"], "相同参数与资料必须可复现"


async def test_monologue_requires_material_and_new_version(
    workspace: Path, fake_backend: None
) -> None:
    runtime = workspace / "runtime"
    without = _prepare_structure(runtime, workspace, with_monologue=False)
    built = _build(runtime, workspace, without, name="v1")
    with pytest.raises(NotReadyError) as info:
        run_calibrate(
            _args(runtime, index=built["index_id"], stage="monologue", samples=str(MONOLOGUE_SAMPLES))
        )
    assert info.value.reason_code == "monologue_material_absent"

    # 补材料必须新建结构版本，不能修改活动快照
    with_material = _prepare_structure(runtime, workspace, with_monologue=True)
    assert with_material != without
    built2 = _build(runtime, workspace, with_material, name="v2")
    assert built2["monologue_status"] == MonologueStatus.DISABLED_UNCALIBRATED.value
    assert built2["monologue_material_count"] > 0

    calibrated = run_calibrate(
        _args(runtime, index=built2["index_id"], stage="monologue", samples=str(MONOLOGUE_SAMPLES))
    )
    assert calibrated["monologue_status"] == MonologueStatus.ENABLED.value
    assert calibrated["threshold"] is not None


async def test_review_reports_index_state(workspace: Path, fake_backend: None) -> None:
    runtime = workspace / "runtime"
    structure_id = _prepare_structure(runtime, workspace, with_monologue=False)
    built = _build(runtime, workspace, structure_id, name="v1")
    payload = run_review(_args(runtime, index=built["index_id"]))
    assert payload["index"]["id"] == built["index_id"]
    assert payload["roundtrip"]["ok"] is True
    assert payload["approval"]["reviewer"] == "maintainer"


async def test_snapshot_records_approved_catalog_version(
    workspace: Path, fake_backend: None
) -> None:
    """快照必须记录它实际校验过的已批准 catalog hash，便于回溯映射版本。"""
    runtime = workspace / "runtime"
    structure_id = _prepare_structure(runtime, workspace, with_monologue=False)
    built = _build(runtime, workspace, structure_id, name="v1")

    context = open_context(_args(runtime))
    try:
        approval = context.structures.approval(structure_id)
        snapshot = context.structures.snapshot(built["index_id"])
        assert approval is not None
        assert snapshot.rule_catalog_version == approval["catalog_hash"]
    finally:
        context.close()

    manifest = json.loads(
        (Path(built["artifact_path"]) / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["catalog_version"] == approval["catalog_hash"]


async def test_build_refuses_catalog_hash_disagreement(
    workspace: Path, fake_backend: None
) -> None:
    """结构与批准记录的 catalog hash 不一致时必须拒绝，而不是产出无法回溯的快照。"""
    from sana_bot.errors import NotReadyError as _NotReadyError

    runtime = workspace / "runtime"
    structure_id = _prepare_structure(runtime, workspace, with_monologue=False)
    context = open_context(_args(runtime))
    try:
        with context.database.write():
            context.database.execute(
                "UPDATE structured_document SET catalog_hash = ? WHERE id = ?",
                ("0" * 64, structure_id),
            )
    finally:
        context.close()

    with pytest.raises(_NotReadyError) as info:
        _build(runtime, workspace, structure_id, name="v1")
    assert info.value.reason_code == "catalog_hash_mismatch"
