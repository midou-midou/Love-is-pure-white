"""时间工具：统一以 UTC 存储，聊天日与展示日期按 Asia/Shanghai 转换。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

__all__ = ["SHANGHAI", "utc_now", "to_utc_iso", "parse_utc", "to_shanghai", "chat_day", "from_epoch"]

SHANGHAI = ZoneInfo("Asia/Shanghai")


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_utc_iso(moment: datetime) -> str:
    """存储用格式；naive 时间按 UTC 解释。"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def to_shanghai(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(SHANGHAI)


def chat_day(received_at: datetime) -> date:
    """聊天日：按 Asia/Shanghai 的消息接收日期，导入时即固定。"""
    return to_shanghai(received_at).date()


def from_epoch(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)
