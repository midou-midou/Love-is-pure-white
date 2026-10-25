"""T011：资料包构建、来源追溯、容量与封存完整性。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from sana_bot.enums import IndexStatus, MonologueStatus
from sana_bot.errors import NotReadyError
from sana_bot.ingest.markdown_package import parse_markdown_package
from sana_bot.ingest.package_service import (
    build_package,
    parse_package,
    sealed_file_hashes,
    verify_sealed_bundle,
)
from sana_bot.ingest.package_models import PackageError
from sana_bot.retrieval.embeddings import EmbeddingBackend, EmbeddingSpec, HeuristicTokenCounter
from tests.support.package_harness import (
    DEFAULT_CONFIG,
    PackageHarness,
    build_package_harness,
    package_backend,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MINIMAL_CONFIG = (
    Path(__file__).resolve().parents[1] / "fixtures" / "character_package" / "minimal" / "config.json"
)


@pytest.fixture
def harness(tmp_path) -> PackageHarness:
    built = build_package_harness(tmp_path / "harness")
    try:
        yield built
    finally:
        built.close()


def _fresh_harness(tmp_path, config_path: Path) -> PackageHarness:
    return build_package_harness(
        tmp_path / "minimal", config_path=config_path, with_calibration=False
    )


def test_build_encodes_every_reference_card_once(harness):
    snapshot = harness.structures.snapshot(harness.index_id)
    matrix = np.load(Path(snapshot.artifact_path) / "vectors.npy")
    encoded = harness.package.encoded_cards
    assert matrix.shape == (25, harness.backend.spec.dimension)
    assert snapshot.dimension == harness.backend.spec.dimension
    assert len({card.card_id for card in encoded}) == 25
    cards = json.loads((Path(snapshot.artifact_path) / "cards.json").read_text(encoding="utf-8"))
    assert len(cards) == 26
    assert all(card["token_count"] > 0 for card in cards if not card["resident_only"])


def test_build_records_three_residents_and_monologue_disabled(harness):
    snapshot = harness.structures.snapshot(harness.index_id)
    # 夹具为便于对话测试会激活；构建本身只产出 staged
    assert snapshot.status in (IndexStatus.STAGED, IndexStatus.ACTIVE)
    assert snapshot.monologue_status is MonologueStatus.DISABLED_NO_MATERIAL
    residents = json.loads(
        (Path(snapshot.artifact_path) / "residents.json").read_text(encoding="utf-8")
    )
    assert [item["resident_id"] for item in residents] == [
        "sana_core",
        "sana_output",
        "sana_emotion_general",
    ]
    assert snapshot.manifest["monologue_status"] == "disabled_no_material"


def test_build_keeps_source_copies_and_hashes(harness):
    snapshot = harness.structures.snapshot(harness.index_id)
    directory = Path(snapshot.artifact_path)
    copies = sorted(path.name for path in (directory / "sources").iterdir())
    assert copies == [
        "01_核心设定.md",
        "02_输出规范.md",
        "03_情绪状态.md",
        "04_场景行为.md",
        "05_背景资料.md",
        "06_对话示例.md",
    ]
    original = REPO_ROOT / "doc" / "乾纱凪角色设定整理版" / "03_情绪状态.md"
    assert (directory / "sources" / "03_情绪状态.md").read_text(encoding="utf-8") == original.read_text(
        encoding="utf-8"
    )


def test_build_does_not_copy_excluded_materials(harness):
    directory = Path(harness.structures.snapshot(harness.index_id).artifact_path)
    names = {path.name for path in (directory / "sources").iterdir()}
    assert "00_加载说明.md" not in names
    assert "07_整理记录与待配置.md" not in names


def test_repeated_build_is_logically_identical(tmp_path):
    first = build_package_harness(tmp_path / "a", with_calibration=False)
    second = build_package_harness(tmp_path / "b", with_calibration=False)
    try:
        assert first.package.structure_hash == second.package.structure_hash
        assert first.snapshot_hash == second.snapshot_hash
        assert first.bundle_hash == second.bundle_hash
        assert first.index_id != second.index_id
    finally:
        first.close()
        second.close()


def test_build_requires_approval(tmp_path):
    from sana_bot.ingest.markdown_package import parse_markdown_package
    from sana_bot.storage.database import open_runtime_database
    from sana_bot.storage.index_repository import StructureRepository
    from sana_bot.storage.package_repository import PackageRepository

    database = open_runtime_database(tmp_path / "db.sqlite3")
    structures = StructureRepository(database)
    packages = PackageRepository(database)
    package = parse_markdown_package(DEFAULT_CONFIG, project_root=REPO_ROOT)
    packages.save_package(package, structure_id="s1")
    with pytest.raises(NotReadyError) as excinfo:
        build_package(
            database=database,
            structures=structures,
            packages=packages,
            structure_id="s1",
            backend=package_backend(),
            output_dir=tmp_path / "idx",
            project_root=REPO_ROOT,
        )
    assert excinfo.value.reason_code == "struct_not_approved"
    database.close()


def test_missing_resident_blocks_parse(tmp_path):
    from tests.contract.test_markdown_package import _copy_minimal

    root = _copy_minimal(tmp_path)
    (root / "docs" / "02_output.md").unlink()
    with pytest.raises(PackageError) as excinfo:
        parse_markdown_package(root / "config.json", project_root=root)
    assert excinfo.value.reason_code == "package_file_missing"


def test_unverified_tokenizer_is_rejected(tmp_path):
    from sana_bot.storage.database import open_runtime_database
    from sana_bot.storage.index_repository import StructureRepository
    from sana_bot.storage.package_repository import PackageRepository
    from tests.support import FakeEmbeddingsProvider

    config = MINIMAL_CONFIG.resolve()
    root = config.parent
    package = parse_markdown_package(config, project_root=root)
    database = open_runtime_database(tmp_path / "db.sqlite3")
    structures = StructureRepository(database)
    packages = PackageRepository(database)
    packages.save_package(package, structure_id="s1")
    policy = package.config.policy()
    packages.record_approval(
        structure_id="s1",
        structure_hash=package.structure_hash,
        catalog_hash=policy.catalog_hash,
        policy_hash=policy.policy_hash,
        profile_hash=package.compute_profile_hash(),
        catalog_path=str(config),
        reviewer="tester",
    )
    backend = EmbeddingBackend(
        EmbeddingSpec(model_id="fake/model", revision="r1", dimension=64, max_tokens=512),
        embeddings_factory=lambda spec: FakeEmbeddingsProvider(dimension=64),
        token_counter=HeuristicTokenCounter(),
    )
    with pytest.raises(NotReadyError) as excinfo:
        build_package(
            database=database,
            structures=structures,
            packages=packages,
            structure_id="s1",
            backend=backend,
            output_dir=tmp_path / "idx",
            project_root=root,
        )
    assert excinfo.value.reason_code == "tokenizer_unverified"
    database.close()


def test_card_over_limit_reports_card_and_limit(tmp_path):
    from sana_bot.storage.database import open_runtime_database
    from sana_bot.storage.index_repository import StructureRepository
    from sana_bot.storage.package_repository import PackageRepository

    config = MINIMAL_CONFIG.resolve()
    root = config.parent
    package = parse_markdown_package(config, project_root=root)
    database = open_runtime_database(tmp_path / "db.sqlite3")
    structures = StructureRepository(database)
    packages = PackageRepository(database)
    packages.save_package(package, structure_id="s1")
    policy = package.config.policy()
    packages.record_approval(
        structure_id="s1",
        structure_hash=package.structure_hash,
        catalog_hash=policy.catalog_hash,
        policy_hash=policy.policy_hash,
        profile_hash=package.compute_profile_hash(),
        catalog_path=str(config),
        reviewer="tester",
    )
    backend = package_backend(max_tokens=40)
    with pytest.raises(NotReadyError) as excinfo:
        build_package(
            database=database,
            structures=structures,
            packages=packages,
            structure_id="s1",
            backend=backend,
            output_dir=tmp_path / "idx",
            project_root=root,
        )
    assert excinfo.value.reason_code == "card_input_over_limit"
    assert excinfo.value.details["limit"] == 40
    assert excinfo.value.details["card_id"].startswith("sana_")
    database.close()


class _BrokenBackend:
    """最小替身：绕过常规校验，直接暴露矩阵形状/数值问题。"""

    def __init__(self, *, dimension: int, mode: str) -> None:
        self.spec = EmbeddingSpec(
            model_id="broken/model", revision="r1", dimension=dimension, max_tokens=4096
        )
        self.token_counter = _Counter()
        self.tokenizer_verified = True
        self.mode = mode
        self.calls = 0

    def ensure_available(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return len(text)

    def embed_documents(self, texts):
        if self.mode == "wrong_dimension":
            return [[0.1] * (self.spec.dimension - 1) for _ in texts]
        if self.mode == "non_finite":
            vectors = [[0.1] * self.spec.dimension for _ in texts]
            vectors[0][0] = float("nan")
            return vectors
        return [[0.1] * self.spec.dimension for _ in texts]


class _Counter:
    def count(self, text: str) -> int:
        return len(text)


def _prepared_package(tmp_path):
    from sana_bot.storage.database import open_runtime_database
    from sana_bot.storage.index_repository import StructureRepository
    from sana_bot.storage.package_repository import PackageRepository

    config = MINIMAL_CONFIG.resolve()
    root = config.parent
    package = parse_markdown_package(config, project_root=root)
    database = open_runtime_database(tmp_path / "db.sqlite3")
    structures = StructureRepository(database)
    packages = PackageRepository(database)
    packages.save_package(package, structure_id="s1")
    policy = package.config.policy()
    packages.record_approval(
        structure_id="s1",
        structure_hash=package.structure_hash,
        catalog_hash=policy.catalog_hash,
        policy_hash=policy.policy_hash,
        profile_hash=package.compute_profile_hash(),
        catalog_path=str(config),
        reviewer="tester",
    )
    return database, structures, packages, root


@pytest.mark.parametrize(
    ("mode", "reason"),
    (("wrong_dimension", "embedding_dimension_mismatch"), ("non_finite", "embedding_non_finite")),
)
def test_broken_vectors_are_rejected(tmp_path, mode, reason):
    database, structures, packages, root = _prepared_package(tmp_path)
    with pytest.raises(NotReadyError) as excinfo:
        build_package(
            database=database,
            structures=structures,
            packages=packages,
            structure_id="s1",
            backend=_BrokenBackend(dimension=32, mode=mode),
            output_dir=tmp_path / "idx",
            project_root=root,
        )
    assert excinfo.value.reason_code == reason
    database.close()


def test_sealed_bundle_detects_tampering(harness):
    snapshot = harness.structures.snapshot(harness.index_id)
    directory = Path(snapshot.artifact_path)
    declared = sealed_file_hashes(directory)
    assert declared["vectors.npy"] == snapshot.manifest["bundle_files"]["vectors.npy"]
    matrix = np.load(directory / "vectors.npy")
    matrix[0][0] = 99.0
    np.save(directory / "vectors.npy", matrix)
    with pytest.raises(NotReadyError) as excinfo:
        verify_sealed_bundle(snapshot)
    assert excinfo.value.reason_code == "bundle_hash_mismatch"


def test_parse_command_writes_review_materials(tmp_path):
    outcome = parse_package(DEFAULT_CONFIG, output_dir=tmp_path / "out", project_root=REPO_ROOT)
    report = json.loads(outcome.report_path.read_text(encoding="utf-8"))
    assert report["approval_status"] == "pending"
    assert report["card_counts"]["dialogue_example"] == 7
    assert report["resident_ids"] == [
        "sana_core",
        "sana_output",
        "sana_emotion_general",
    ]
    assert report["excluded_files"] == ["00_加载说明.md", "07_整理记录与待配置.md"]
    structure = json.loads(outcome.structure_path.read_text(encoding="utf-8"))
    assert len(structure["sources"]) == 8
    assert len(structure["cards"]) == 26


def test_excluded_materials_never_enter_generation_payload(harness):
    snapshot = harness.structures.snapshot(harness.index_id)
    directory = Path(snapshot.artifact_path)
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix == ".npy":
            continue
        text = path.read_text(encoding="utf-8")
        assert "本套资料用于 AI 扮演" not in text
        assert "原作事实与创作扩展的对应清单" not in text
