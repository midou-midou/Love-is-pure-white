"""``config validate`` / ``config apply``：原子应用，非法配置保留旧版本。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sana_bot.errors import ConfigError
from sana_bot.runtime import open_context
from sana_bot.storage.relationship_repository import RelationshipRepository

__all__ = ["run_validate", "run_apply"]


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("配置文件必须是对象")
    return payload


def run_validate(args: argparse.Namespace) -> dict[str, Any]:
    payload = _load(Path(args.file))
    context = open_context(args)
    try:
        candidate = context.config_store.validate_payload(payload)
        return {
            "valid": True,
            "file": str(args.file),
            "effective": candidate.to_payload(),
            "current_version": context.config.version,
        }
    finally:
        context.close()


def run_apply(args: argparse.Namespace) -> dict[str, Any]:
    payload = _load(Path(args.file))
    context = open_context(args)
    try:
        applied = context.config_store.apply_payload(payload, source=str(args.file))
        version = None
        with context.database.write():
            version = RelationshipRepository(context.database).store_config(
                applied, effective_at=applied.effective_at.isoformat()
            )
        return {
            "applied": applied.to_payload(),
            "relationship_config_version": version,
            "note": "配置只影响后续事件；历史分数不重算",
        }
    finally:
        context.close()
