"""QQ 发送契约：被动回复必须用平台消息 id，400 要留下平台的 code/message。

回归的是线上真实故障：把内部 inbound uuid 当 ``msg_id`` 发出去，平台返回
400 Bad Request，用户完全收不到回复。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from sana_bot.adapters.qq_auth import QQAuthClient, QQCredentials
from sana_bot.adapters.qq_sender import QQTextTransport
from sana_bot.config import RuntimeConfig
from sana_bot.enums import DeliveryStatus
from sana_bot.storage.database import Database
from sana_bot.storage.delivery_repository import DeliveryRepository

PLATFORM_MSG_ID = "ROBOT1.0_z36fZJndx7WDYBY8QVF7a1.PGqWxtQSm1gMGTSBcWUOVemeVTLuOumFwCWz2hebh6vo8dXv"
OPENID = "5ED2D75A050D74503ED7BB722A17DEDB"


def _transport(database: Database, handler) -> tuple[QQTextTransport, list[dict[str, Any]]]:
    captured: list[dict[str, Any]] = []

    def recording(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/app/getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
        captured.append(
            {
                "url": str(request.url),
                "body": json.loads(request.content.decode("utf-8")),
                "auth": request.headers.get("authorization"),
            }
        )
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    auth = QQAuthClient(
        credentials=QQCredentials(app_id="123456", app_secret="secret"), client=client
    )
    transport = QQTextTransport(
        auth=auth,
        deliveries=DeliveryRepository(database, config=RuntimeConfig()),
        client=client,
        app_id="123456",
    )
    return transport, captured


async def test_passive_reply_sends_platform_msg_id(database: Database) -> None:
    transport, captured = _transport(
        database, lambda _request: httpx.Response(200, json={"id": "sent-id"})
    )

    result = await transport.send(
        content="在",
        source_msg_id="ec442581be6d4f6c8c3f02ba97738c7a",
        msg_seq=1,
        user_key=OPENID,
        platform_msg_id=PLATFORM_MSG_ID,
    )

    assert result.status is DeliveryStatus.CONFIRMED
    assert captured[0]["url"].endswith(f"/v2/users/{OPENID}/messages")
    body = captured[0]["body"]
    assert body["msg_id"] == PLATFORM_MSG_ID
    assert body["msg_id"] != "ec442581be6d4f6c8c3f02ba97738c7a"
    assert body["msg_seq"] == 1
    assert body["msg_type"] == 0
    assert body["content"] == "在"
    assert captured[0]["auth"] == "QQBot tok"


async def test_active_message_omits_msg_id(database: Database) -> None:
    transport, captured = _transport(
        database, lambda _request: httpx.Response(200, json={"id": "sent-id"})
    )

    await transport.send(
        content="（突然开口）在吗",
        source_msg_id="internal-id",
        msg_seq=1,
        user_key=OPENID,
        platform_msg_id=None,
    )

    assert "msg_id" not in captured[0]["body"], "没有平台消息 id 时不能编造 msg_id"


async def test_bad_request_records_platform_error(database: Database) -> None:
    transport, _captured = _transport(
        database,
        lambda _request: httpx.Response(
            400, json={"code": 40054005, "message": "msg_id 不合法"}
        ),
    )

    result = await transport.send(
        content="在",
        source_msg_id="internal-id",
        msg_seq=1,
        user_key=OPENID,
        platform_msg_id="not-a-platform-id",
    )

    assert result.status is DeliveryStatus.REJECTED
    assert result.reason_code == "http_rejected"
    details = result.details or {}
    assert details["http_status"] == 400
    assert details["platform_code"] == 40054005
    assert "msg_id" in details["platform_message"]
    assert details["sent_msg_id_kind"] == "unexpected", "非平台 id 必须能被识别出来"


@pytest.mark.parametrize(("msg_id", "kind"), [(None, "absent"), ("ROBOT1.0_x", "platform")])
async def test_msg_id_kind_is_classified(
    database: Database, msg_id: str | None, kind: str
) -> None:
    transport, _captured = _transport(database, lambda _request: httpx.Response(400, json={}))
    result = await transport.send(
        content="在",
        source_msg_id="internal-id",
        msg_seq=1,
        user_key=OPENID,
        platform_msg_id=msg_id,
    )
    assert (result.details or {})["sent_msg_id_kind"] == kind
