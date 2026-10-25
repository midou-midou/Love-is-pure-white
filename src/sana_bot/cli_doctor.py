"""``doctor`` 与 ``serve``：装配运行期依赖并给出可信的检查结论。

doctor 区分「未执行」与「通过」：缺少凭据、缺少已激活索引或缺少本地模型时明确列出，
绝不假报成功，也不打印任何凭据。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sana_bot.adapters.qq import QQWebhookHandler
from sana_bot.adapters.qq_auth import QQAuthClient, QQCredentials
from sana_bot.adapters.qq_sender import QQTextTransport
from sana_bot.app import ApplicationServices, build_application
from sana_bot.config import RuntimeConfig
from sana_bot.dialogue.deepseek import ChatDeepSeekModel
from sana_bot.dialogue.history import HistoryService
from dataclasses import replace

from sana_bot.dialogue.output_policy import ConstrainedOutputPolicy, load_output_constraints
from sana_bot.dialogue.service import DialogueTurnService
from sana_bot.enums import RetrievalStage
from sana_bot.errors import ConfigError, NotReadyError
from sana_bot.ingest.service import IngestService
from sana_bot.ports import ModelBudget
from sana_bot.retrieval.local_model import describe_cache, model_is_cached
from sana_bot.retrieval.service import VectorRetriever
from sana_bot.retrieval.vector_store import VectorStore
from sana_bot.runtime import RuntimeContext, build_embedding_backend, open_context
from sana_bot.scheduling.serial_queue import InboundQueueService, UserSerialQueue
from sana_bot.scheduling.worker import TurnWorker
from sana_bot.state.catalog import RuleCatalog, load_catalog
from sana_bot.state.event_extractor import LangChainStateEventExtractor
from sana_bot.state.reducer import DeterministicStateReducer
from sana_bot.state.service import StateService
from sana_bot.storage.delivery_repository import DeliveryRepository
from sana_bot.storage.state_repository import StateRepository

__all__ = ["run_doctor", "run_serve", "build_services", "RunServices"]

MODEL_BUDGET_SECONDS = 30.0
#: 经维护者核对后的 catalog（存在时优先于待审草稿）
REVIEWED_CATALOG = Path("runtime/reviewed-catalog.json")
DRAFT_CATALOG = Path("config/state-catalog.draft.json")


@dataclass
class RuntimeMaintenance:
    """启动与周期维护：清空短期状态、取消待发独白、清理过期诊断正文。"""

    context: RuntimeContext
    state_service: StateService

    def clear_overrides(self) -> int:
        return self.state_service.clear_all_overrides()

    def recover_interrupted_deliveries(self) -> int:
        with self.context.database.write():
            return self.context.deliveries.recover_interrupted()

    def cancel_monologues(self) -> int:
        from sana_bot.storage.monologue_repository import MonologueRepository

        with self.context.database.write():
            repository = MonologueRepository(self.context.database, config=self.context.config)
            repository.mark_interrupted_uncertain()
            return repository.cancel_all_pending(reason="service_restart")

    def prune_diagnostics(self) -> int:
        from sana_bot.storage.diagnostic_repository import DiagnosticRepository

        with self.context.database.write():
            return DiagnosticRepository(
                self.context.database, config=self.context.config, inbound=self.context.inbound
            ).prune()

    def stop(self) -> None:
        return None


@dataclass
class RunServices:
    """一次 ``serve`` 所需的全部服务。"""

    context: RuntimeContext
    catalog: RuleCatalog
    catalog_source: str
    policy: ConstrainedOutputPolicy
    worker: TurnWorker
    services: ApplicationServices
    embeddings: EmbeddingBackend


def _load_catalog_for_runtime(context: RuntimeContext) -> tuple[RuleCatalog, str]:
    for candidate in (context.layout.root / "reviewed-catalog.json", REVIEWED_CATALOG, DRAFT_CATALOG):
        if candidate.exists():
            return load_catalog(candidate), str(candidate)
    return RuleCatalog(version="empty"), "none"


def _constraints_for_runtime(context: RuntimeContext, config: RuntimeConfig):
    snapshot = context.structures.active_snapshot()
    if snapshot is None:
        return load_output_constraints(_blank_document(), max_chars=config.max_reply_chars, max_sentences=config.max_reply_sentences), None
    matches = [
        item
        for item in context.structures.list_structures()
        if item["structure_hash"] == snapshot.structure_hash
    ]
    if not matches:
        return load_output_constraints(_blank_document(), max_chars=config.max_reply_chars, max_sentences=config.max_reply_sentences), None
    service = IngestService(context.database, layout=context.layout, config=config)
    document = service.load(matches[0]["id"])
    constraints = load_output_constraints(
        document,
        max_chars=config.max_reply_chars,
        max_sentences=config.max_reply_sentences,
    )
    return constraints, document


def _blank_document():
    from sana_bot.ingest.models import SourceDocument, StructuredDocument

    return StructuredDocument(
        id="empty",
        source=SourceDocument(id="empty", relative_path="", file_hash="", imported_at=""),
        structure_hash="",
    )


def build_services(
    context: RuntimeContext,
    *,
    allow_live_model: bool = False,
    allow_model_download: bool = False,
) -> RunServices:
    """装配运行期服务；缺少凭据时明确失败，不静默降级为假成功。"""
    from sana_bot.retrieval.package_retrieval import PackageRetriever
    from sana_bot.storage.package_repository import (
        PackageRepository,
        PackageReportRepository,
    )

    config = context.config
    deliveries = DeliveryRepository(context.database, config=config)
    packages = PackageRepository(context.database)
    reports = PackageReportRepository(context.database)
    vector_store = VectorStore(structures=context.structures)
    history = HistoryService(
        structures=context.structures,
        inbound=context.inbound,
        deliveries=deliveries,
        packages=packages,
        store=vector_store,
        reports=reports,
    )
    catalog, catalog_source = _load_catalog_for_runtime(context)
    constraints, _document = _constraints_for_runtime(context, config)
    model = ChatDeepSeekModel(
        api_key=context.environment.deepseek_api_key,
        model=context.environment.deepseek_model,
        base_url=context.environment.deepseek_base_url,
        total_budget_seconds=MODEL_BUDGET_SECONDS,
    )
    policy = ConstrainedOutputPolicy(
        constraints=constraints, model=model, budget=model.budget
    )
    extractor = LangChainStateEventExtractor(model=model, budget=model.budget)
    reducer = DeterministicStateReducer(catalog)
    auth = QQAuthClient(
        credentials=QQCredentials(
            app_id=context.environment.qq_app_id,
            app_secret=context.environment.qq_app_secret,
            base_url=context.environment.qq_api_base_url,
        )
    )
    transport = QQTextTransport(
        auth=auth, deliveries=deliveries, app_id=context.environment.qq_app_id or ""
    )
    # 统一走工厂：本地/远程两种后端由 EMBEDDING_KIND 决定，避免此处各自拼装
    embeddings = build_embedding_backend(
        context, allow_download=allow_model_download
    )
    retriever = VectorRetriever(
        structures=context.structures,
        embeddings=embeddings,
        store=vector_store,
    )
    package_retriever = PackageRetriever(
        structures=context.structures,
        reports=reports,
        store=vector_store,
        embeddings=embeddings,
        count_tokens=embeddings.count_tokens,
    )
    state_service = StateService(
        repository=StateRepository(context.database),
        reducer=reducer,
        catalog=catalog,
        inbound=context.inbound,
        config=config,
    )
    package_policy = ConstrainedOutputPolicy(
        constraints=replace(constraints, strict_package_rules=True),
        model=model,
        budget=model.budget,
    )
    turn_service = DialogueTurnService(
        retriever=retriever,
        package_retriever=package_retriever,
        package_policy=package_policy,
        packages=packages,
        embeddings=embeddings,
        previous_state_provider=state_service.current,
        database=context.database,
        config=config,
        structures=context.structures,
        inbound=context.inbound,
        deliveries=deliveries,
        history=history,
        policy=policy,
        reducer=reducer,
        catalog=catalog,
        extractor=extractor,
        model=model,
        transport=transport,
        diagnostics=context.diagnostics,
    )
    queue = UserSerialQueue(
        capacity_per_user=64,
        item_timeout_seconds=MODEL_BUDGET_SECONDS + 10.0,
    )
    inbound_queue = InboundQueueService(
        database=context.database, repository=context.inbound, queue=queue
    )
    worker = TurnWorker(queue=queue, service=turn_service, inbound_queue=inbound_queue)
    webhook = QQWebhookHandler(
        queue_service=inbound_queue,
        secret=context.environment.qq_app_secret,
        app_id=context.environment.qq_app_id,
    )
    maintenance = RuntimeMaintenance(context=context, state_service=state_service)
    gateway = _build_gateway(context, auth=auth, webhook=webhook)
    services = ApplicationServices(
        context=context,
        webhook=webhook,
        worker=worker,
        maintenance=maintenance,
        gateway=gateway,
    )
    del allow_live_model
    return RunServices(
        context=context,
        catalog=catalog,
        catalog_source=catalog_source,
        policy=policy,
        worker=worker,
        services=services,
        embeddings=embeddings,
    )


def _build_gateway(
    context: RuntimeContext,
    *,
    auth: Any,
    webhook: Any,
) -> Any:
    """按 ``QQ_TRANSPORT`` 决定是否启用 WebSocket 长连接。

    长连接与 webhook 共用同一条受理链路：``webhook.accept_dispatch`` 已包含
    去重落盘与串行入队，因此这里只负责协议与心跳。
    """
    environment = context.environment
    if environment.qq_transport != "websocket":
        return None
    from sana_bot.adapters.qq_gateway import GatewayOptions, QQGatewayClient

    return QQGatewayClient(
        auth=auth,
        accept=webhook.accept_dispatch,
        options=GatewayOptions(
            intents=environment.qq_intents,
            shard=environment.qq_shard,
            gateway_url=environment.qq_gateway_url,
        ),
        diagnostics=context.diagnostics,
    )


# --------------------------------------------------------------------------- doctor


def run_doctor(args: argparse.Namespace) -> dict[str, Any]:
    context = open_context(args)
    checks: dict[str, Any] = {}
    problems: list[str] = []
    try:
        checks["environment"] = context.environment.as_public_payload()
        checks["config"] = {
            "version": context.config.version,
            "issues": context.config.issues(),
        }
        problems.extend(context.config.issues())
        try:
            context.database.query_one("SELECT 1 AS ok")
            checks["storage"] = {"ready": True, "path": str(context.layout.database_path)}
        except Exception as exc:  # noqa: BLE001
            checks["storage"] = {"ready": False, "error": type(exc).__name__}
            problems.append("storage_unavailable")

        checks["embedding"] = _embedding_check(context)
        problems.extend(checks["embedding"]["problems"])

        snapshot = context.structures.active_snapshot()
        reply = (
            context.structures.calibration(snapshot.id, RetrievalStage.REPLY)
            if snapshot
            else None
        )
        structural = bool(snapshot and snapshot.is_structural)
        package_state = package_readiness(context)
        checks["index"] = {
            "active": snapshot.id if snapshot else None,
            "format_version": snapshot.index_format_version if snapshot else None,
            "snapshot_hash": snapshot.snapshot_hash if snapshot else None,
            "monologue_status": snapshot.monologue_status.value if snapshot else None,
            "reply_threshold": reply.threshold if reply else None,
            "bundle_hash": snapshot.bundle_hash if snapshot else None,
        }
        if snapshot is None:
            problems.append("no_active_index")
        elif structural:
            checks["package"] = package_state
            problems.extend(package_state.get("problems") or [])
        elif reply is None:
            problems.append("reply_calibration_missing")

        checks["transport"] = {
            "mode": context.environment.qq_transport,
            "intents": context.environment.qq_intents,
            "shard": list(context.environment.qq_shard),
            "gateway_url_override": bool(context.environment.qq_gateway_url),
        }

        credentials = context.environment.credential_status()
        checks["credentials"] = credentials
        if not all(credentials.values()):
            problems.append("credentials_missing")

        checks["checks_executed"] = ["offline"]
        not_executed: list[str] = []
        if getattr(args, "live_model", False):
            live_model = _live_model_check(context)
            checks["live_model"] = live_model
            checks["checks_executed"].append("live-model")
            if not live_model["executed"]:
                not_executed.append(f"live-model: {live_model.get('reason_code')}")
            elif not live_model["ok"]:
                problems.append(f"live_model_failed:{live_model.get('reason_code')}")
        if getattr(args, "qq", False):
            qq = _qq_check(context)
            checks["qq"] = qq
            checks["checks_executed"].append("qq")
            if not qq["executed"]:
                not_executed.append(f"qq: {qq.get('reason_code')}")
            elif not qq["ok"]:
                problems.append(f"qq_auth_failed:{qq.get('reason_code')}")
            else:
                not_executed.extend(
                    f"qq: {item}" for item in qq["details"].get("not_executed", [])
                )
        if not getattr(args, "live_model", False):
            not_executed.append("live-model 未执行（需 --live-model）")
        if not getattr(args, "qq", False):
            not_executed.append("qq 未执行（需 --qq）")
        checks["not_executed"] = not_executed

        return {
            "ready": not problems,
            "problems": problems,
            "checks": checks,
        }
    finally:
        context.close()


def _embedding_check(context: RuntimeContext) -> dict[str, Any]:
    """按后端类型检查向量就绪度；远程只需凭据与地址，本地需要固定 revision 与缓存。"""
    from sana_bot.retrieval.embeddings import EmbeddingKind
    from sana_bot.runtime import embedding_spec_from

    environment = context.environment
    problems: list[str] = []
    kind = (environment.embedding_kind or "local").strip().lower()
    payload: dict[str, Any] = {
        "kind": kind,
        "model": environment.embedding_model,
        "dimension": environment.embedding_dimension,
        "max_tokens": environment.embedding_max_tokens,
    }
    try:
        spec = embedding_spec_from(environment)
    except Exception as exc:  # noqa: BLE001 - 配置错误必须显式报告
        payload["problems"] = [getattr(exc, "reason_code", "config_invalid")]
        return {**payload, "problems": payload["problems"], "error": type(exc).__name__}

    if spec.kind is EmbeddingKind.LOCAL_HF:
        revision = environment.embedding_revision
        cached = bool(revision) and model_is_cached(
            environment.embedding_model, revision or "", environment.embedding_cache_dir
        )
        payload.update(
            {
                **describe_cache(environment.embedding_model, environment.embedding_cache_dir),
                "revision": revision,
                "revision_pinned": bool(revision),
                "cached": cached,
            }
        )
        if not revision:
            problems.append("embedding_revision_missing")
        elif not cached:
            problems.append("embedding_model_not_cached")
    else:
        payload["base_url_host"] = spec.host
        payload["credentials_present"] = bool(spec.api_key)
        if not environment.embedding_base_url:
            problems.append("embedding_base_url_missing")
        if not spec.api_key:
            problems.append("embedding_credentials_missing")
        if not problems:
            # 真实连通性检查：一次最小调用，核对返回维度
            payload["probe"] = _embedding_probe(context)
            if not payload["probe"].get("ok"):
                problems.append(f"embedding_probe_failed:{payload['probe'].get('reason_code')}")
            elif payload["probe"].get("dimension") != environment.embedding_dimension:
                problems.append("embedding_dimension_mismatch")
    payload["problems"] = problems
    return payload


def _embedding_probe(context: RuntimeContext) -> dict[str, Any]:
    from sana_bot.retrieval.embeddings import EmbeddingUnavailable, RemoteEmbeddingBackend
    from sana_bot.runtime import build_embedding_backend

    backend = build_embedding_backend(context, allow_download=False)
    if not isinstance(backend, RemoteEmbeddingBackend):  # pragma: no cover - 仅远程分支调用
        return {"ok": False, "reason_code": "not_remote"}
    try:
        return {**backend.probe(), "ok": True}
    except EmbeddingUnavailable as exc:
        return {"ok": False, "reason_code": exc.reason_code, **dict(exc.details)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason_code": "probe_failed", "error": type(exc).__name__}


def _live_model_check(context: RuntimeContext) -> dict[str, Any]:
    """真实 DeepSeek smoke；缺少凭据时明确标记未执行。"""
    from sana_bot.live_checks import model_smoke

    check = asyncio.run(model_smoke(context.environment))
    return check.to_payload()


def _qq_check(context: RuntimeContext) -> dict[str, Any]:
    """真实 QQ 检查：取 access_token，并验证能否拿到 WSS 接入点。"""
    from sana_bot.live_checks import qq_auth_smoke, qq_gateway_smoke

    auth = asyncio.run(qq_auth_smoke(context.environment))
    payload = auth.to_payload()
    gateway = asyncio.run(qq_gateway_smoke(context.environment))
    details = dict(payload.get("details") or {})
    details["gateway"] = gateway.to_payload()
    details["transport"] = context.environment.qq_transport
    payload["details"] = details
    if payload.get("ok") and gateway.executed and not gateway.ok:
        payload["ok"] = False
        payload["reason_code"] = gateway.reason_code
    return payload


# --------------------------------------------------------------------------- serve


def run_serve(args: argparse.Namespace) -> dict[str, Any]:
    context = open_context(args)
    if not context.environment.credential_status()["QQ_APP_SECRET"]:
        context.close()
        raise ConfigError("缺少 QQ_APP_SECRET，不能启动服务", reason_code="qq_credentials_missing")
    run = build_services(context)
    application = build_application(run.services)
    import uvicorn

    uvicorn.run(application, host=args.host, port=int(args.port), log_level="info")
    return {"status": "stopped", "host": args.host, "port": int(args.port)}


def describe_run(run: RunServices) -> str:
    return json.dumps(
        {
            "catalog_source": run.catalog_source,
            "approved_rules": len(run.catalog.approved_rules),
            "pending_rules": len(run.catalog.pending_rules),
        },
        ensure_ascii=False,
    )


def package_readiness(context: RuntimeContext) -> dict[str, Any]:
    """新格式就绪检查：常驻与报告必须有效；无独白素材不阻塞 reply。"""
    from sana_bot.retrieval.vector_store import VectorStore
    from sana_bot.storage.package_repository import (
        PackageRepository,
        PackageReportRepository,
    )

    snapshot = context.structures.active_snapshot()
    payload: dict[str, Any] = {"format": "structural_v2" if snapshot and snapshot.is_structural else "semantic_v1"}
    if snapshot is None:
        payload.update({"ready": False, "problems": ["no_active_index"]})
        return payload
    if not snapshot.is_structural:
        payload.update({"ready": True, "problems": []})
        return payload
    problems: list[str] = []
    packages = PackageRepository(context.database)
    reports = PackageReportRepository(context.database)
    try:
        view = packages.view(str(snapshot.manifest.get("structure_id") or ""))
        index = VectorStore(structures=context.structures).load_structural(snapshot)
    except Exception as exc:  # noqa: BLE001 - 绑定或产物损坏要显式报告
        payload.update(
            {
                "ready": False,
                "problems": [getattr(exc, "reason_code", "package_unavailable")],
            }
        )
        return payload
    sealed = {item.resident_id for item in index.residents}
    stored = {item.resident_id for item in view.residents}
    present = sealed & stored
    missing = [item for item in view.config.resident_ids if item not in sealed or item not in stored]
    if missing:
        problems.append("resident_missing")
    calibration = [
        item
        for item in reports.calibrations(snapshot.id, stage="reply")
        if item.snapshot_hash == snapshot.snapshot_hash and item.approved_at
    ]
    if not calibration:
        problems.append("package_calibration_missing")
    payload.update(
        {
            "ready": not problems,
            "problems": problems,
            "resident_ids": sorted(present),
            "encoded_cards": len(index.card_ids),
            "bundle_hash": snapshot.bundle_hash,
            "calibration_id": calibration[-1].id if calibration else None,
            "monologue_status": snapshot.monologue_status.value,
            "monologue_blocks_reply": False,
        }
    )
    return payload


def ensure_ready(run: RunServices) -> None:
    snapshot = run.context.structures.active_snapshot()
    if snapshot is None:
        raise NotReadyError("没有已激活的完整索引", reason_code="no_active_index")
    if snapshot.is_structural:
        readiness = package_readiness(run.context)
        if not readiness.get("ready"):
            raise NotReadyError(
                f"资料包未就绪：{readiness.get('problems')}",
                reason_code=str((readiness.get("problems") or ["package_unavailable"])[0]),
                details={"problems": readiness.get("problems")},
            )


def model_budget(seconds: float = MODEL_BUDGET_SECONDS) -> ModelBudget:
    return ModelBudget(total_seconds=seconds)
