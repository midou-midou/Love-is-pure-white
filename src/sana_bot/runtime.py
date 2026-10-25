"""运行期上下文：把命令行参数、环境变量、配置与存储装配到一起。

所有 ``cli_*`` 命令共用这里的装配路径，避免各自拼装运行目录或读取密钥。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sana_bot.config import ConfigStore, Environment, RuntimeConfig, RuntimeLayout
from sana_bot.errors import ConfigError
from sana_bot.diagnostics import LoggingRecorder, ResilientRecorder
from sana_bot.retrieval.embeddings import (
    DEFAULT_QUERY_INSTRUCTION,
    EmbeddingBackend,
    EmbeddingKind,
    EmbeddingSpec,
    RemoteEmbeddingBackend,
    build_backend,
)
from sana_bot.storage.database import Database, open_runtime_database
from sana_bot.storage.delivery_repository import DeliveryRepository
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.storage.index_repository import StructureRepository

__all__ = ["RuntimeContext", "open_context", "build_embedding_backend"]


@dataclass
class RuntimeContext:
    """一次命令执行所需的全部依赖。"""

    environment: Environment
    layout: RuntimeLayout
    config: RuntimeConfig
    config_store: ConfigStore
    database: Database
    structures: StructureRepository
    inbound: InboundRepository
    deliveries: DeliveryRepository
    diagnostics: Any

    def close(self) -> None:
        self.database.close()


def open_context(args: argparse.Namespace | None = None, *, migrate: bool = True) -> RuntimeContext:
    """打开运行目录；环境变量只读，密钥不落盘、不出现在返回值里。"""
    environment = Environment.from_process()
    runtime_dir = getattr(args, "runtime_dir", None) if args is not None else None
    if runtime_dir:
        environment = _with_runtime_dir(environment, Path(runtime_dir))
    layout = environment.layout.ensure()
    config_store = ConfigStore(layout.config_path)
    config = config_store.load()
    database = open_runtime_database(layout.database_path) if migrate else Database(layout.database_path)
    secrets = (environment.deepseek_api_key, environment.qq_app_secret, environment.qq_app_id)
    return RuntimeContext(
        environment=environment,
        layout=layout,
        config=config,
        config_store=config_store,
        database=database,
        structures=StructureRepository(database),
        inbound=InboundRepository(database),
        deliveries=DeliveryRepository(database, config=config),
        diagnostics=_build_diagnostics(database, secrets),
    )


def _with_runtime_dir(environment: Environment, path: Path) -> Environment:
    from dataclasses import replace

    return replace(environment, runtime_dir=path.expanduser())


def embedding_spec_from(environment: Environment) -> EmbeddingSpec:
    """由环境配置生成向量规格；kind 非法时明确失败。"""
    raw_kind = (environment.embedding_kind or "local").strip().lower()
    try:
        kind = EmbeddingKind(raw_kind)
    except ValueError as exc:
        raise ConfigError(
            f"EMBEDDING_KIND 不支持：{raw_kind}（可选 local / openai_compatible）",
            reason_code="config_invalid",
        ) from exc
    # 本地默认沿用 bge 中文检索指令；远程模型（bge-m3/Qwen）通常不需要
    instruction = (
        DEFAULT_QUERY_INSTRUCTION if kind is EmbeddingKind.LOCAL_HF else ""
    )
    return EmbeddingSpec(
        model_id=environment.embedding_model,
        kind=kind,
        revision=environment.embedding_revision or "",
        dimension=environment.embedding_dimension,
        max_tokens=environment.embedding_max_tokens,
        cache_dir=environment.embedding_cache_dir,
        base_url=environment.embedding_base_url,
        api_key=environment.embedding_api_key or "",
        query_instruction=instruction,
    )


def build_embedding_backend(
    context: RuntimeContext,
    *,
    allow_download: bool = False,
    client: Any | None = None,
) -> EmbeddingBackend | RemoteEmbeddingBackend:
    """按环境配置构造向量后端；缺少必需项时在校验阶段明确失败。"""
    spec = embedding_spec_from(context.environment)
    return build_backend(spec, allow_download=allow_download, client=client)


class _NullRecorder:
    """命令上下文不落库的诊断占位：由 ResilientRecorder 决定降级信号。"""

    def record(self, record: Any) -> Any:
        from sana_bot.diagnostics import DiagnosticOutcome

        return DiagnosticOutcome(recorded=False, correlation_id=record.correlation_id)


def _build_diagnostics(database: Database, secrets: tuple[str | None, ...]) -> Any:
    """诊断记录器：优先落库，失败时发出备用信号且如实报告未记录。"""
    fallback = LoggingRecorder(secrets=secrets)
    try:
        from sana_bot.storage.diagnostic_repository import DiagnosticRepository
    except ImportError:  # pragma: no cover - 诊断表在其所属阶段建立
        return fallback
    return ResilientRecorder(DiagnosticRepository(database), fallback=fallback, secrets=secrets)
