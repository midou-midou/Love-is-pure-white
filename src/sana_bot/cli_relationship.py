"""``relationship show`` 命令：分数、状态、三项累计与配置版本。"""

from __future__ import annotations

import argparse
from typing import Any

from sana_bot.errors import ConfigError
from sana_bot.runtime import open_context
from sana_bot.state.relationship import RelationshipService
from sana_bot.storage.relationship_repository import RelationshipRepository

__all__ = ["run_show"]


def run_show(args: argparse.Namespace) -> dict[str, Any]:
    user = getattr(args, "user", None)
    if not user:
        raise ConfigError("必须提供 --user（内部 UserIdentity.id）")
    context = open_context(args)
    try:
        repository = RelationshipRepository(context.database)
        service = RelationshipService(repository, config=context.config)
        payload = service.snapshot_payload(str(user))
        payload["affection_events"] = repository.events_for(str(user), limit=10)
        payload["dialogue_days"] = repository.days_for(str(user))
        payload["config_version_stored"] = repository.latest_config()
        return payload
    finally:
        context.close()
