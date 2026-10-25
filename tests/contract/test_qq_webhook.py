"""QQ C2C 入站契约：验签、挑战、去重、事件/消息 id 区分与失败状态码。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sana_bot.adapters.qq import QQWebhookHandler, normalize_c2c_event
from sana_bot.adapters.qq_auth import challenge_response, sign_payload
from sana_bot.storage.database import Database
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.scheduling.serial_queue import InboundQueueService, UserSerialQueue
from tests.support import make_event

SECRET = "test-bot-secret-0123456789abcdef"
APP_ID = "123456"
TIMESTAMP = "1730000000"


def _body(**overrides: object) -> bytes:
    payload = {
        "op": 0,
        "s": 1,
        "t": "C2C_MESSAGE_CREATE",
        "id": "outer-event-1",
        "appid": APP_ID,
        "d": {
            "id": "message-1",
            "content": "你好呀",
            "timestamp": "2026-10-16T12:00:00+00:00",
            "author": {"user_openid": "openid-1"},
        },
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _signed(body: bytes, *, timestamp: str = TIMESTAMP, secret: str = SECRET) -> str:
    return sign_payload(secret, timestamp.encode("utf-8") + body)


@pytest.fixture
async def handler(database: Database):
    queue = UserSerialQueue(capacity_per_user=8, item_timeout_seconds=1.0)
    handled: list[str] = []

    async def consume(item) -> None:
        handled.append(item.inbound_id)

    await queue.start(consume)
    service = InboundQueueService(
        database=database, repository=InboundRepository(database), queue=queue
    )
    instance = QQWebhookHandler(queue_service=service, secret=SECRET, app_id=APP_ID)
    yield instance, database, handled, queue
    await queue.stop()


async def test_valid_signature_is_acked_and_persisted(handler) -> None:
    webhook, database, handled, queue = handler
    body = _body()
    decision = await webhook.handle(
        raw_body=body, signature=_signed(body), timestamp=TIMESTAMP
    )
    assert decision.status_code == 200
    assert decision.payload == {"op": 12}
    assert decision.event is not None
    assert decision.event.event_key == "outer-event-1"
    assert decision.event.message_id == "message-1"
    row = database.query_one("SELECT * FROM inbound_event")
    assert row["platform_event_id"] == "outer-event-1"
    assert row["message_id"] == "message-1"


async def test_bad_signature_is_rejected(handler) -> None:
    webhook, database, _handled, _queue = handler
    body = _body()
    decision = await webhook.handle(
        raw_body=body, signature=_signed(body, secret="x" * 32), timestamp=TIMESTAMP
    )
    assert decision.status_code == 401
    assert decision.reason_code == "bad_signature"
    assert database.query("SELECT * FROM inbound_event") == []


async def test_raw_body_change_invalidates_signature(handler) -> None:
    webhook, _db, _handled, _queue = handler
    body = _body()
    tampered = body.replace(b'"s": 1', b'"s": 2')
    decision = await webhook.handle(
        raw_body=tampered, signature=_signed(body), timestamp=TIMESTAMP
    )
    assert decision.status_code == 401


async def test_challenge_is_answered_without_creating_events(handler) -> None:
    webhook, database, _handled, _queue = handler
    body = json.dumps(
        {"op": 13, "d": {"plain_token": "token-1", "event_ts": "1730000001"}},
        ensure_ascii=False,
    ).encode("utf-8")
    decision = await webhook.handle(raw_body=body, signature=None, timestamp=None)
    assert decision.status_code == 200
    expected = challenge_response(secret=SECRET, plain_token="token-1", event_ts="1730000001")
    assert decision.payload == expected
    assert database.query("SELECT * FROM inbound_event") == []
    assert database.query("SELECT * FROM turn") == []


async def test_duplicate_delivery_does_not_create_second_turn(handler) -> None:
    webhook, database, handled, queue = handler
    body = _body()
    first = await webhook.handle(raw_body=body, signature=_signed(body), timestamp=TIMESTAMP)
    second = await webhook.handle(raw_body=body, signature=_signed(body), timestamp=TIMESTAMP)
    assert first.status_code == second.status_code == 200
    assert second.enqueue is not None and second.enqueue.duplicate
    await queue.join()
    assert database.query("SELECT COUNT(*) AS n FROM inbound_event")[0]["n"] == 1
    assert len(handled) == 1


async def test_same_message_id_with_new_event_id_is_still_duplicate(handler) -> None:
    webhook, database, _handled, _queue = handler
    first_body = _body()
    second_body = _body(id="outer-event-2")
    await webhook.handle(raw_body=first_body, signature=_signed(first_body), timestamp=TIMESTAMP)
    decision = await webhook.handle(
        raw_body=second_body, signature=_signed(second_body), timestamp=TIMESTAMP
    )
    assert decision.enqueue is not None and not decision.enqueue.accepted
    assert database.query("SELECT COUNT(*) AS n FROM inbound_event")[0]["n"] == 1


async def test_invalid_structure_is_400(handler) -> None:
    webhook, _db, _handled, _queue = handler
    body = json.dumps({"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {}}).encode("utf-8")
    decision = await webhook.handle(raw_body=body, signature=_signed(body), timestamp=TIMESTAMP)
    assert decision.status_code == 400


async def test_non_json_body_is_400(handler) -> None:
    webhook, _db, _handled, _queue = handler
    decision = await webhook.handle(raw_body=b"not-json", signature="00" * 32, timestamp=TIMESTAMP)
    assert decision.status_code == 400


async def test_unsupported_event_type_is_ignored(handler) -> None:
    webhook, database, handled, queue = handler
    body = _body(t="GROUP_AT_MESSAGE_CREATE")
    decision = await webhook.handle(raw_body=body, signature=_signed(body), timestamp=TIMESTAMP)
    assert decision.status_code == 200
    assert decision.reason_code == "ignored_unsupported_type"
    assert handled == []
    assert database.query("SELECT COUNT(*) AS n FROM turn")[0]["n"] == 0


async def test_media_message_is_ignored_with_hint(handler) -> None:
    webhook, database, handled, queue = handler
    payload = {
        "op": 0,
        "t": "C2C_MESSAGE_CREATE",
        "id": "outer-event-9",
        "appid": APP_ID,
        "d": {
            "id": "message-9",
            "content": "",
            "attachments": [{"url": "https://example.invalid/a.png"}],
            "author": {"user_openid": "openid-9"},
        },
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    decision = await webhook.handle(raw_body=body, signature=_signed(body), timestamp=TIMESTAMP)
    assert decision.status_code == 200
    assert decision.reason_code == "unsupported_media"
    assert handled == []
    row = database.query_one("SELECT status FROM inbound_event")
    assert row["status"] == "ignored"


async def test_persist_failure_returns_503(database: Database) -> None:
    class BrokenQueueService:
        async def accept(self, event):  # noqa: ANN001
            raise RuntimeError("disk full")

    webhook = QQWebhookHandler(
        queue_service=BrokenQueueService(), secret=SECRET, app_id=APP_ID
    )
    body = _body()
    decision = await webhook.handle(raw_body=body, signature=_signed(body), timestamp=TIMESTAMP)
    assert decision.status_code == 503
    assert database.query("SELECT COUNT(*) AS n FROM turn")[0]["n"] == 0


def test_normalize_uses_received_time_for_chat_day() -> None:
    received = datetime(2026, 10, 16, 16, 30, tzinfo=UTC)
    event = normalize_c2c_event(json.loads(_body()), received_at=received)
    assert event is not None and not hasattr(event, "reason_code")
    assert event.received_at == received
    assert make_event(message_id="x").payload_hash
