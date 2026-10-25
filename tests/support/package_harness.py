"""资料包测试夹具：独立临时库、确定性编码器/分词器、可控时钟与替代依赖。

默认完全离线：不读取真实凭据、不联网、不共享业务库。向量用字符 n-gram 哈希
生成，词面相近的文本确实更相似，便于断言真实的排序与条件筛选行为。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from sana_bot.enums import RetrievalStage
from sana_bot.ingest.markdown_package import parse_markdown_package
from sana_bot.ingest.package_service import build_package
from sana_bot.retrieval.embeddings import (
    EmbeddingBackend,
    EmbeddingSpec,
    TokenizerCounter,
)
from sana_bot.retrieval.package_calibration import load_calibration_samples
from sana_bot.retrieval.package_selector import PackageSelection, PackageSelector
from sana_bot.retrieval.vector_store import StructuralVectorIndex, VectorStore
from sana_bot.storage.database import Database, open_runtime_database
from sana_bot.storage.index_repository import StructureRepository
from sana_bot.storage.package_repository import (
    PackageCalibrationReport,
    PackageRegressionReport,
    PackageRepository,
    PackageReportRepository,
)
from sana_bot.timeutil import to_utc_iso, utc_now
from tests.support import CharTokenizer

__all__ = [
    "TopicEmbeddingsProvider",
    "TOPIC_LEXICON",
    "PackageHarness",
    "build_package_harness",
    "stable_report_artifacts",
]

DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "character_package"
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "character-package.json"


#: 每个完整卡一个话题维度；关键词只用于离线替身，不代表真实语义模型行为
TOPIC_LEXICON: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sana_emotion_calm", ("安静", "平稳", "待着", "日常")),
    ("sana_emotion_shy", ("害羞", "脸热", "靠这么近", "脸红")),
    ("sana_emotion_flustered", ("捉弄", "不正经", "炸毛", "羞恼")),
    ("sana_emotion_jealous", ("吃醋", "冷落", "理我")),
    ("sana_emotion_hurt", ("委屈", "受伤", "难受")),
    ("sana_emotion_secure", ("安心", "依赖", "撒娇")),
    ("sana_scene_share", ("邀约", "散步", "庙会", "宠物店", "分享", "社团事务")),
    ("sana_scene_care", ("料理", "忌口", "热饮", "照顾")),
    ("sana_scene_praise", ("夸奖", "好看", "直白")),
    ("sana_scene_comfort", ("疲惫", "受挫", "安慰")),
    ("sana_scene_gift", ("礼物", "纪念日", "手工")),
    ("sana_scene_conflict", ("拌嘴", "抬杠", "矛盾", "台阶")),
    ("sana_lore_family", ("弟弟", "理央", "父亲", "母亲")),
    ("sana_lore_club", ("美羽", "学姐", "喵喵社", "社长")),
    ("sana_lore_relationship", ("交换男生", "敌视", "告白", "恋人关系")),
    ("sana_lore_appearance", ("酒红色", "马尾", "猫耳", "水手服", "呆毛")),
    ("sana_lore_preferences", ("喜好", "擅长", "编织", "蟑螂")),
    ("sana_lore_places_names", ("公寓", "房间", "称呼", "同居")),
    ("sana_example_calm", ("放学", "一起走", "收拾")),
    ("sana_example_care", ("忙了一整天", "聊")),
    ("sana_example_shy", ("特意", "留着", "发辫")),
    ("sana_example_jealous", ("别的女生", "聊得久", "站了")),
    ("sana_example_hurt", ("误会", "对不起")),
    ("sana_example_share", ("小猫", "纸箱", "猫粮")),
    ("sana_example_gift", ("小饰品", "拆了")),
)


class TopicEmbeddingsProvider:
    """话题维度替身编码器：按关键词命中生成单位向量，确定性且可离线。"""

    def __init__(self) -> None:
        self.dimension = len(TOPIC_LEXICON)
        self.calls: list[list[str]] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        vector = np.zeros(self.dimension, dtype=np.float64)
        for index, (_card_id, keywords) in enumerate(TOPIC_LEXICON):
            hits = sum(1 for keyword in keywords if keyword in text)
            if hits:
                vector[index] = float(hits)
        if not vector.any():
            vector[-1] = 0.5
        norm = float(np.linalg.norm(vector))
        return [float(value / norm) for value in vector]


def package_backend(*, dimension: int | None = None, max_tokens: int = 512) -> EmbeddingBackend:
    tokenizer = CharTokenizer()
    provider = TopicEmbeddingsProvider()
    dimension = provider.dimension
    return EmbeddingBackend(
        EmbeddingSpec(
            model_id="fixture/package-hash",
            revision="fixture-rev-1",
            dimension=dimension,
            max_tokens=max_tokens,
            query_instruction="",
        ),
        embeddings_factory=lambda _spec: provider,
        token_counter=TokenizerCounter(tokenizer),
    )


@dataclass
class PackageHarness:
    """一套可复用的临时资料包与索引。"""

    directory: Path
    database: Database
    structures: StructureRepository
    packages: PackageRepository
    reports: PackageReportRepository
    store: VectorStore
    backend: EmbeddingBackend
    package: Any
    structure_id: str
    index_id: str
    artifact_dir: Path
    snapshot_hash: str
    bundle_hash: str
    threshold: float = 0.25
    calibration_id: str | None = None
    report_id: str | None = None
    budget: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ 便捷方法

    def index(self) -> StructuralVectorIndex:
        return self.store.load_structural(self.structures.snapshot(self.index_id))

    def select(
        self,
        *,
        query: str,
        snapshot: Any,
        threshold: float | None = None,
        medium: str = "text",
    ) -> PackageSelection:
        index = self.index()
        selector = PackageSelector(
            threshold=self.threshold if threshold is None else threshold,
            budgets=dict(index.policy.get("budget") or {}),
            count_tokens=self.backend.count_tokens,
        )
        return selector.select(
            index=index,
            snapshot=snapshot,
            query_vector=self.backend.embed_query(query),
            medium=medium,
            required_relationship="romantic",
        )

    def close(self) -> None:
        self.database.close()


def build_package_harness(
    directory: Path,
    *,
    config_path: Path | None = None,
    dimension: int | None = None,
    threshold: float = 0.25,
    with_calibration: bool = True,
    with_regression: bool = False,
    regression_mode: str = "live",
    require_live_regression: bool = True,
    activate: bool = True,
) -> PackageHarness:
    """解析、登记、批准并构建一个隔离的资料包；可选写入校准/行为报告。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    database = open_runtime_database(directory / "package.sqlite3")
    structures = StructureRepository(database)
    packages = PackageRepository(database)
    reports = PackageReportRepository(database)
    config = Path(config_path or DEFAULT_CONFIG)

    package = parse_markdown_package(config, project_root=_project_root(config))
    structure_id = "fixture-structure"
    packages.save_package(package, structure_id=structure_id)
    policy = package.config.policy()
    packages.record_approval(
        structure_id=structure_id,
        structure_hash=package.structure_hash,
        catalog_hash=policy.catalog_hash,
        policy_hash=policy.policy_hash,
        profile_hash=package.compute_profile_hash(),
        catalog_path=str(config),
        reviewer="fixture-reviewer",
    )
    backend = package_backend(dimension=dimension)
    outcome = build_package(
        database=database,
        structures=structures,
        packages=packages,
        structure_id=structure_id,
        backend=backend,
        output_dir=directory / "indexes",
        project_root=_project_root(config),
    )
    store = VectorStore(structures=structures)
    harness = PackageHarness(
        directory=directory,
        database=database,
        structures=structures,
        packages=packages,
        reports=reports,
        store=store,
        backend=backend,
        package=package,
        structure_id=structure_id,
        index_id=outcome.index_id,
        artifact_dir=outcome.artifact_path,
        snapshot_hash=outcome.snapshot_hash,
        bundle_hash=outcome.bundle_hash,
        threshold=threshold,
        budget=dict(package.config.budgets),
    )
    if with_calibration:
        insert_calibration_report(
            harness,
            threshold=threshold,
            approved=True,
            execution_mode="live",
        )
    if activate:
        from sana_bot.enums import IndexStatus

        structures.set_snapshot_status(outcome.index_id, IndexStatus.ACTIVE)
    if with_regression:
        insert_regression_report(
            harness,
            approved=True,
            execution_mode=regression_mode,
            live_required=require_live_regression,
        )
    return harness


def _project_root(config_path: Path) -> Path:
    """source_root 相对配置上一级或上两级解析；两者都试不到时用配置目录。"""
    config_path = Path(config_path).resolve()
    try:
        import json

        source_root = str(json.loads(config_path.read_text(encoding="utf-8"))["source_root"])
    except Exception:  # noqa: BLE001 - 交给解析器报告具体错误
        return config_path.parent
    for candidate in (config_path.parent, config_path.parent.parent):
        if (candidate / source_root).is_dir():
            return candidate
    return config_path.parent


def insert_calibration_report(
    harness: PackageHarness,
    *,
    threshold: float | None,
    approved: bool = True,
    execution_mode: str = "live",
) -> PackageCalibrationReport:
    report = PackageCalibrationReport(
        id=f"cal-{execution_mode}-{threshold}",
        index_id=harness.index_id,
        stage=RetrievalStage.REPLY.value,
        snapshot_hash=harness.snapshot_hash,
        structural_profile_hash=harness.structures.snapshot(harness.index_id).structural_profile_hash
        or "",
        retrieval_policy_hash=harness.structures.snapshot(harness.index_id).retrieval_policy_hash,
        sample_set_hash="fixture-samples",
        model_id=harness.structures.snapshot(harness.index_id).embedding_model_id,
        encoder_config_hash=str(
            harness.structures.snapshot(harness.index_id).manifest.get("encoder_config_hash", "")
        ),
        threshold=threshold,
        sample_counts={"positive": 36, "negative": 12, "total": 48},
        rank_metrics={"recall": 1.0, "adopted_excluded": 0},
        approved_by="fixture-reviewer" if approved else "",
        approved_at=to_utc_iso(utc_now()) if approved else None,
        created_at=to_utc_iso(utc_now()),
        execution_mode=execution_mode,
        artifacts={"thresholds": {}},
        status="approved" if approved else "pending_review",
    )
    harness.reports.insert_calibration(report)
    harness.calibration_id = report.id
    with harness.database.write():
        harness.database.execute(
            """
            UPDATE index_snapshot
            SET reply_calibration_id = ?, active_report_hash = ?, updated_at = ?
            WHERE id = ?
            """,
            (report.id, report.compute_hash(), to_utc_iso(utc_now()), harness.index_id),
        )
    return report


def stable_report_artifacts(*, mode: str = "offline", turns: int = 24) -> dict[str, Any]:
    return {
        "turns": [
            {
                "case_id": f"case-{index}",
                "turn_index": 0,
                "input": "今天社团怎么样",
                "reply": "社团那边还好",
                "violations": [],
                "execution_mode": mode,
            }
            for index in range(turns)
        ],
        "samples": [],
        "integrity": {"cards": 26, "encoded": 25},
    }


def required_results(*, mode: str) -> dict[str, Any]:
    return {
        "required": {
            "integrity": "passed",
            "selection": "passed",
            "empty_retrieval": "passed",
            "behaviour": "passed",
            "output": "passed",
            "operations": "not_run",
        },
        "execution_mode": mode,
        "live_required": True,
        "categories": {},
    }


def insert_regression_report(
    harness: PackageHarness,
    *,
    approved: bool = True,
    execution_mode: str = "live",
    live_required: bool = True,
    results: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    kind: str = "behaviour",
) -> PackageRegressionReport:
    snapshot = harness.structures.snapshot(harness.index_id)
    payload = results or required_results(mode=execution_mode)
    report = PackageRegressionReport(
        report_id=f"reg-{execution_mode}-{harness.index_id[:6]}",
        index_id=harness.index_id,
        snapshot_hash=harness.snapshot_hash,
        bundle_hash=harness.bundle_hash,
        structural_profile_hash=snapshot.structural_profile_hash or "",
        retrieval_policy_hash=snapshot.retrieval_policy_hash,
        sample_set_hash="fixture-evaluation",
        results=payload,
        business_data_checks={},
        report_path="",
        approved_by="fixture-reviewer" if approved else "",
        approved_at=to_utc_iso(utc_now()) if approved else None,
        created_at=to_utc_iso(utc_now()),
        kind=kind,
        execution_mode=execution_mode,
        artifacts=artifacts if artifacts is not None else stable_report_artifacts(mode=execution_mode),
        catalog_hash=harness.package.config.policy().catalog_hash,
        encoder_config_hash=str(snapshot.manifest.get("encoder_config_hash", "")),
    )
    harness.reports.insert_regression(report)
    harness.report_id = report.report_id
    return report


def load_fixture_samples(name: str = "retrieval.jsonl"):
    return load_calibration_samples(DEFAULT_FIXTURE_DIR / name)


def snapshot_for(**kwargs):
    from sana_bot.state.models import empty_snapshot

    return empty_snapshot(
        snapshot_id=kwargs.pop("snapshot_id", "fixture"),
        turn_id=kwargs.pop("turn_id", "fixture"),
        conversation_id=kwargs.pop("conversation_id", "conv-fixture"),
        **kwargs,
    )


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return float(np.dot(np.asarray(a), np.asarray(b)))


# --------------------------------------------------------------------------- 对话链路


@dataclass
class PackageDialogue:
    """带资料包的完整对话链路夹具（替代模型与传输，不联网）。"""

    harness: PackageHarness
    service: Any
    history: Any
    model: Any
    transport: Any
    database: Database
    relationships: dict[str, Any] = field(default_factory=dict)

    def submit(self, *, message_id: str, text: str, user_key: str = "openid-a", offset: int = 0):
        from datetime import UTC, datetime, timedelta

        from sana_bot.scheduling.serial_queue import QueueItem
        from tests.support import make_event

        base = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
        event = make_event(
            message_id=message_id,
            user_key=user_key,
            text=text,
            event_time=base + timedelta(seconds=offset),
            received_at=base + timedelta(seconds=offset, milliseconds=5),
        )
        with self.database.write():
            outcome = self.harness.database and None
            from sana_bot.storage.inbound_repository import InboundRepository

            repository = InboundRepository(self.database)
            persisted = repository.persist_inbound(event)
        row = repository.inbound_row(persisted.inbound_id or "")
        assert row is not None
        return QueueItem(
            inbound_id=row.id,
            user_id=row.user_id,
            conversation_id=row.conversation_id,
            generation=persisted.generation,
            text=row.text,
            chat_day=row.chat_day,
            received_at=row.received_at,
            event_time=row.event_time,
            correlation_id=row.id,
            app_id=row.app_id,
            user_key=user_key,
            message_id=row.message_id,
        )

    def generation_requests(self):
        return [request for request in self.model.requests if request.json_schema_name is None]

    def close(self):
        self.harness.close()


def build_package_dialogue(
    directory: Path,
    *,
    config_path: Path | None = None,
    relationship: Any = None,
    model_texts: Sequence[str] = (),
    with_calibration: bool = True,
    scene: str = "unknown",
) -> PackageDialogue:
    """装配新格式对话链路；默认恋人关系、未知场景。"""
    from sana_bot.config import RuntimeConfig
    from sana_bot.dialogue.history import HistoryService
    from sana_bot.dialogue.output_policy import (
        ConstrainedOutputPolicy,
        OutputConstraints,
    )
    from sana_bot.dialogue.service import DialogueTurnService
    from sana_bot.enums import RelationshipStatus, Scene
    from sana_bot.ports import ModelBudget
    from sana_bot.retrieval.package_retrieval import PackageRetriever
    from sana_bot.state.catalog import RuleCatalog
    from sana_bot.state.event_extractor import LangChainStateEventExtractor
    from sana_bot.state.reducer import DeterministicStateReducer
    from sana_bot.storage.delivery_repository import DeliveryRepository
    from sana_bot.storage.inbound_repository import InboundRepository
    from tests.support import FakeClock, FakeModel, FakeQQTransport

    harness = build_package_harness(
        directory, config_path=config_path, with_calibration=with_calibration
    )
    config = RuntimeConfig()
    inbound = InboundRepository(harness.database)
    deliveries = DeliveryRepository(harness.database, config=config)
    history = HistoryService(
        structures=harness.structures,
        inbound=inbound,
        deliveries=deliveries,
        packages=harness.packages,
        store=harness.store,
        reports=harness.reports,
    )
    model = FakeModel(text_responses=model_texts)
    model.default_text = "（我看了看你）嗯，我在听"
    clock = FakeClock()
    transport = FakeQQTransport(clock=clock)
    rel = relationship or RelationshipStatus.ROMANTIC
    constraints = OutputConstraints()
    policy = ConstrainedOutputPolicy(
        constraints=constraints, model=model, budget=ModelBudget(30.0)
    )
    package_policy = ConstrainedOutputPolicy(
        constraints=__import__("dataclasses").replace(
            constraints, strict_package_rules=True
        ),
        model=model,
        budget=policy.budget,
    )
    retriever = PackageRetriever(
        structures=harness.structures,
        reports=harness.reports,
        store=harness.store,
        embeddings=harness.backend,
        count_tokens=harness.backend.count_tokens,
    )
    service = DialogueTurnService(
        database=harness.database,
        config=config,
        structures=harness.structures,
        inbound=inbound,
        deliveries=deliveries,
        history=history,
        policy=policy,
        reducer=DeterministicStateReducer(RuleCatalog(version="unused")),
        catalog=RuleCatalog(version="unused"),
        extractor=LangChainStateEventExtractor(model=model, budget=ModelBudget(30.0)),
        retriever=retriever,
        model=model,
        transport=transport,
        packages=harness.packages,
        package_retriever=retriever,
        package_policy=package_policy,
        embeddings=harness.backend,
        clock=clock,
        relationship_provider=lambda user_id: (rel, 120.0),
    )
    dialogue = PackageDialogue(
        harness=harness,
        service=service,
        history=history,
        model=model,
        transport=transport,
        database=harness.database,
    )
    _set_conversation_scene(harness.database, scene)
    return dialogue


def _set_conversation_scene(database: Database, scene: str) -> None:
    with database.write():
        database.execute("UPDATE conversation SET scene = ?", (scene,))
