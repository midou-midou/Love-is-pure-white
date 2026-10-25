"""``state show`` / ``state set`` 命令。"""

from __future__ import annotations

import argparse
from typing import Any

from sana_bot.errors import ConfigError
from sana_bot.runtime import open_context
from sana_bot.state.catalog import RuleCatalog, load_catalog
from sana_bot.state.reducer import DeterministicStateReducer
from sana_bot.state.service import StateService
from sana_bot.storage.state_repository import StateRepository

__all__ = ["run_show", "run_set", "build_state_service"]


def build_state_service(context, *, catalog: RuleCatalog | None = None) -> StateService:
    resolved = catalog or _catalog_for(context)
    return StateService(
        repository=StateRepository(context.database),
        reducer=DeterministicStateReducer(resolved),
        catalog=resolved,
        inbound=context.inbound,
        config=context.config,
    )


def _catalog_for(context) -> RuleCatalog:
    for candidate in (
        context.layout.root / "reviewed-catalog.json",
        context.layout.root.parent / "config" / "state-catalog.draft.json",
    ):
        if candidate.exists():
            return load_catalog(candidate)
    return RuleCatalog(version="empty")


def _require_user(args: argparse.Namespace) -> str:
    user = getattr(args, "user", None)
    if not user:
        raise ConfigError("必须提供 --user（内部 UserIdentity.id）")
    return str(user)


def run_show(args: argparse.Namespace) -> dict[str, Any]:
    user_id = _require_user(args)
    context = open_context(args)
    try:
        service = build_state_service(context)
        conversation_id = context.inbound.active_conversation_id(user_id)
        payload = service.show(user_id, conversation_id)
        payload["transitions"] = (
            service.recent_transitions(conversation_id) if conversation_id else []
        )
        payload["audits"] = service.repository.audits(user_id, limit=5)
        return payload
    finally:
        context.close()


def run_set(args: argparse.Namespace) -> dict[str, Any]:
    user_id = _require_user(args)
    tags = getattr(args, "tag", None) or []
    context = open_context(args)
    try:
        service = build_state_service(context)
        override = service.set_state(
            user_id,
            mood=getattr(args, "mood", None),
            tags=tags,
            scene=getattr(args, "scene", None),
        )
        return {
            "user_id": user_id,
            "applied": override.to_payload(),
            "note": "设置从下一轮生效；不改好感度、不写入长期档案",
        }
    finally:
        context.close()
