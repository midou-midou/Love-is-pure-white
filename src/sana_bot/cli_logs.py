"""``logs`` 命令：按内部 user / 时间 / 事件 / 原因检索与清理。

日志只供维护者本地访问；查询结果带有限前后文，不复制无限对话。
"""

from __future__ import annotations

import argparse
from typing import Any

from sana_bot.errors import ConfigError
from sana_bot.runtime import open_context
from sana_bot.storage.diagnostic_repository import DiagnosticRepository, summarise

__all__ = ["run_query", "run_prune"]


def _repository(context) -> DiagnosticRepository:
    return DiagnosticRepository(context.database, config=context.config, inbound=context.inbound)


def run_query(args: argparse.Namespace) -> dict[str, Any]:
    user = getattr(args, "user", None)
    event = getattr(args, "event", None)
    since = getattr(args, "since", None)
    reason = getattr(args, "reason", None)
    if not any((user, event, since, reason)):
        raise ConfigError("logs query 至少需要一个检索条件（--user/--event/--since/--reason）")
    context = open_context(args)
    try:
        repository = _repository(context)
        events = repository.with_context(
            user_id=user, event_id=event, since=since, reason=reason
        )
        return {
            "count": len(events),
            "by_type": summarise(events),
            "events": events,
            "retention_days": context.config.diagnostic_retention_days,
            "note": "结果仅含有限前后文；不含凭据与模型推理",
        }
    finally:
        context.close()


def run_prune(args: argparse.Namespace) -> dict[str, Any]:
    raw = getattr(args, "older_than_days", None)
    days = 30 if raw is None else int(raw)
    if days <= 0:
        raise ConfigError("--older-than-days 必须为正数")
    context = open_context(args)
    try:
        repository = _repository(context)
        pruned = repository.prune(older_than_days=days)
        preserved = {
            "user_identity": _count(context, "user_identity"),
            "relationship": _count(context, "relationship"),
            "affection_event": _count(context, "affection_event"),
            "inbound_event": _count(context, "inbound_event"),
        }
        return {
            "older_than_days": days,
            "pruned_events": pruned,
            "preserved_records": preserved,
            "note": "只清理诊断正文；关系账本、去重凭据与轮次标识保留",
        }
    finally:
        context.close()


def _count(context, table: str) -> int:
    if not _has_table(context, table):
        return 0
    row = context.database.query_one(f"SELECT COUNT(*) AS n FROM {table}")
    return int(row["n"]) if row else 0


def _has_table(context, name: str) -> bool:
    row = context.database.query_one(
        "SELECT 1 AS ok FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    )
    return row is not None
