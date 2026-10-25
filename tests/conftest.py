"""共享测试夹具。

默认完全离线：不联网、不隐式下载向量模型、不调用 DeepSeek/QQ、不真实等待。
真实服务测试必须显式加 ``live`` 或 ``embedding`` 标记。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from sana_bot.config import ConfigStore, Environment, RuntimeConfig, RuntimeLayout
from sana_bot.storage.database import Database, open_runtime_database
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.storage.schema import base_migrations
from tests.support import (
    FakeClock,
    FakeModel,
    FakeQQTransport,
    InlineRandom,
    RecordingDiagnostics,
    SeededRandom,
)


TESTS_ROOT = Path(__file__).resolve().parent
FIXTURES_DIR = TESTS_ROOT / "fixtures"
REPO_ROOT = TESTS_ROOT.parent


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """绝对路径的 fixture 目录（测试工作目录被隔离到临时目录）。"""
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(autouse=True)
def offline_environment(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """除非标记 embedding/live，否则禁止任何隐式联网下载。"""
    if "embedding" in request.keywords or "live" in request.keywords:
        return
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("QQ_APP_ID", raising=False)
    monkeypatch.delenv("QQ_APP_SECRET", raising=False)
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def rng() -> SeededRandom:
    return SeededRandom()


@pytest.fixture
def inline_rng() -> InlineRandom:
    return InlineRandom([0.1, 0.9], cycle=True)


@pytest.fixture
def runtime_layout(tmp_path: Path) -> RuntimeLayout:
    return RuntimeLayout(tmp_path / "runtime").ensure()


@pytest.fixture
def environment(tmp_path: Path) -> Environment:
    return Environment(
        runtime_dir=tmp_path / "runtime",
        embedding_model="BAAI/bge-small-zh-v1.5",
        embedding_revision="test-revision",
        embedding_cache_dir=tmp_path / "models",
    )


@pytest.fixture
def database(runtime_layout: RuntimeLayout) -> Iterator[Database]:
    """全部迁移后的临时数据库；每个测试独立。"""
    handle = open_runtime_database(runtime_layout.database_path)
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
def base_database(tmp_path: Path) -> Iterator[Database]:
    """仅基础表的数据库；用于验证基础约束与回滚。"""
    handle = Database(tmp_path / "base.sqlite3", migrations=base_migrations())
    handle.migrate()
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
def repository(database: Database) -> InboundRepository:
    return InboundRepository(database)


@pytest.fixture
def config_store(runtime_layout: RuntimeLayout) -> ConfigStore:
    return ConfigStore(runtime_layout.config_path)


@pytest.fixture
def config() -> RuntimeConfig:
    return RuntimeConfig()


@pytest.fixture
def fake_model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def fake_transport(clock: FakeClock) -> FakeQQTransport:
    return FakeQQTransport(clock=clock)


@pytest.fixture
def diagnostics() -> RecordingDiagnostics:
    return RecordingDiagnostics()


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """避免测试在仓库内写入 runtime/ 产物。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("SANABOT_TEST_TMP", str(workdir))
    for name in ("LANGCHAIN_API_KEY",):
        monkeypatch.delenv(name, raising=False)
    os.environ.setdefault("TZ", "UTC")
