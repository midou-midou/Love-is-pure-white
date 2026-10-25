"""启动完整性：就绪判定、重启恢复、周期清理与密钥不泄漏。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sana_bot.app import ApplicationServices, build_application, health_payload, readiness_payload
from sana_bot.config import ConfigStore, RuntimeConfig
from sana_bot.enums import DeliveryStatus, MonologueStatus, OpportunityStatus
from sana_bot.runtime import open_context
from sana_bot.storage.monologue_repository import MonologueRepository
from tests.support import FakeClock
from tests.support.index_fixture import build_indexed_fixture

SECRET = "sk-super-secret-value-123456"
BASE_TIME = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def _context(tmp_path: Path, **env):
    import argparse

    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    ConfigStore(runtime / "config.json").apply_payload(
        {"keywords": ["谢谢"], "romance_threshold": 100.0}, source="test"
    )
    return open_context(argparse.Namespace(runtime_dir=str(runtime)))


def test_readiness_requires_active_index(tmp_path: Path) -> None:
    context = _context(tmp_path)
    try:
        status, payload = readiness_payload(context)
        assert status == 503
        assert "no_active_index" in payload["problems"]
        assert payload["ready"] is False
    finally:
        context.close()


def test_readiness_passes_with_complete_index(tmp_path: Path) -> None:
    context = _context(tmp_path)
    try:
        build_indexed_fixture(context.database, artifact_root=tmp_path / "indexes")
        status, payload = readiness_payload(context)
        assert status == 200
        assert payload["ready"] is True
        assert payload["snapshot_hash"]
        assert payload["monologue_status"] == MonologueStatus.DISABLED_NO_MATERIAL.value
    finally:
        context.close()


def test_readyz_and_healthz_do_not_leak_secrets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    monkeypatch.setenv("QQ_APP_SECRET", "bot-secret-abcdefghijklmnopqrstuvwxyz")
    context = _context(tmp_path)
    try:
        _, payload = readiness_payload(context)
        serialized = json.dumps(payload, ensure_ascii=False) + json.dumps(health_payload())
        assert SECRET not in serialized
        assert "bot-secret" not in serialized
        assert context.environment.credential_status()["DEEPSEEK_API_KEY"] is True
    finally:
        context.close()


def test_startup_recovers_and_cleans_up(tmp_path: Path) -> None:
    """/启动：sending→uncertain、待发独白取消、短期状态覆盖清空。"""
    from sana_bot.cli_doctor import RuntimeMaintenance
    from sana_bot.state.service import StateService
    from sana_bot.state.reducer import DeterministicStateReducer
    from sana_bot.state.catalog import RuleCatalog
    from sana_bot.storage.state_repository import StateRepository

    context = _context(tmp_path)
    try:
        monologues = MonologueRepository(context.database, config=context.config)
        with context.database.write():
            user = context.inbound.ensure_user("app-1", "openid-1")
            conversation_id = context.inbound.ensure_conversation(user)
            opportunity = monologues.create(
                turn_id="turn-1",
                conversation_id=conversation_id,
                generation=1,
                source_msg_id="inbound-1",
                now=BASE_TIME,
            )
            context.deliveries.prepare(
                delivery_id="d-1",
                app_id="app-1",
                user_key="openid-1",
                kind=__import__("sana_bot.enums", fromlist=["OutboundKind"]).OutboundKind.NORMAL,
                source_msg_id="inbound-1",
                content="（别开视线）……随便你",
                msg_seq=1,
            )
            context.deliveries.mark_sending("d-1")
        state_service = StateService(
            repository=StateRepository(context.database),
            reducer=DeterministicStateReducer(RuleCatalog(version="empty")),
            catalog=RuleCatalog(version="empty"),
            inbound=context.inbound,
            config=context.config,
        )
        state_service.set_state(user, mood="happy")
        maintenance = RuntimeMaintenance(context=context, state_service=state_service)

        assert maintenance.recover_interrupted_deliveries() == 1
        assert maintenance.clear_overrides() == 1
        assert maintenance.cancel_monologues() == 1
        assert state_service.current(user, conversation_id).primary_mood.value == "neutral"
        assert monologues.get(opportunity.id).status is OpportunityStatus.CANCELLED
        assert context.deliveries.delivery("d-1").status is DeliveryStatus.UNCERTAIN
        assert maintenance.prune_diagnostics() >= 0
    finally:
        context.close()


def test_periodic_prune_keeps_ledger(tmp_path: Path) -> None:
    context = _context(tmp_path)
    try:
        from sana_bot.storage.diagnostic_repository import DiagnosticRepository

        repository = DiagnosticRepository(context.database, config=context.config)
        repository.record(
            __import__("sana_bot.diagnostics", fromlist=["DiagnosticRecord"]).DiagnosticRecord(
                category=__import__("sana_bot.enums", fromlist=["DiagnosticCategory"]).DiagnosticCategory.LOW_MATCH,
                correlation_id="c-1",
                reason="below_threshold",
                occurred_at="2020-01-01T00:00:00+00:00",
            )
        )
        assert repository.prune() == 1
        assert repository.count() == 1, "清理只删除正文，保留事件骨架"
    finally:
        context.close()


def test_application_wires_health_endpoints(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    context = _context(tmp_path)
    try:
        services = ApplicationServices(context=context, webhook=None, worker=_DummyWorker())
        app = build_application(services)
        with TestClient(app) as client:
            assert client.get("/healthz").json() == {"status": "ok"}
            ready = client.get("/readyz")
            assert ready.status_code == 503
            assert "no_active_index" in ready.json()["problems"]
        assert services.startup_result["cancelled_monologues"] >= 0
    finally:
        context.close()


class _DummyWorker:
    def __init__(self) -> None:
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def recover(self) -> list[str]:
        return []
