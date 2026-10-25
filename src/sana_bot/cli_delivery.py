"""``delivery review``：仅处理 uncertain 发送，凭官方证据核对。

只有 confirmed/rejected 才结算或撤销；confirmed 后按 delivery_id 幂等结算。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from sana_bot.dialogue.history import HistoryService
from sana_bot.enums import DeliveryStatus
from sana_bot.errors import ConfigError
from sana_bot.runtime import open_context
from sana_bot.state.affection import AffectionService
from sana_bot.state.relationship import RelationshipService
from sana_bot.storage.relationship_repository import RelationshipRepository
from sana_bot.storage.state_repository import StateRepository

__all__ = ["run_review"]


def run_review(args: argparse.Namespace) -> dict[str, Any]:
    result = str(getattr(args, "result", "") or "")
    if result not in {"confirmed", "rejected"}:
        raise ConfigError("--result 只能是 confirmed 或 rejected")
    evidence = getattr(args, "evidence", None)
    if not evidence:
        raise ConfigError("必须提供 --evidence（官方核对证据路径）")
    evidence_path = Path(evidence)
    if not evidence_path.exists():
        raise ConfigError(f"证据文件不存在：{evidence_path}")

    context = open_context(args)
    try:
        delivery = context.deliveries.delivery(str(args.id))
        if delivery is None:
            raise ConfigError(f"发送记录不存在：{args.id}")
        if delivery.status is not DeliveryStatus.UNCERTAIN:
            return {
                "delivery_id": delivery.id,
                "status": delivery.status.value,
                "updated": False,
                "note": "只有 uncertain 记录可以核对",
            }
        evidence_text = evidence_path.read_text(encoding="utf-8").strip()
        if not evidence_text:
            raise ConfigError("证据文件为空，不能作为官方核对依据")
        target = DeliveryStatus.CONFIRMED if result == "confirmed" else DeliveryStatus.REJECTED
        with context.database.write():
            context.deliveries.settle(
                delivery.id,
                status=target,
                platform_message_id=delivery.platform_message_id,
                error=None,
            )
        settlement: dict[str, Any] | None = None
        if target is DeliveryStatus.CONFIRMED and delivery.turn_id:
            relationships = RelationshipRepository(context.database)
            service = AffectionService(
                database=context.database,
                config=context.config,
                deliveries=context.deliveries,
                inbound=context.inbound,
                relationships=relationships,
                snapshots=StateRepository(context.database),
                relationship_service=RelationshipService(relationships, config=context.config),
            )
            settled = service.settle_delivery(delivery_id=delivery.id)
            settlement = {
                "applied": settled.applied,
                "delta": settled.delta,
                "score_after": settled.score_after,
                "reason_code": settled.reason_code,
            }
        return {
            "delivery_id": delivery.id,
            "status": target.value,
            "updated": True,
            "evidence": str(evidence_path),
            "settlement": settlement,
            "note": "不重新发送；仅按官方证据更新本地记录",
        }
    finally:
        context.close()


def history_for(context, conversation_id: str) -> HistoryService:
    return HistoryService(
        structures=context.structures, inbound=context.inbound, deliveries=context.deliveries
    )
