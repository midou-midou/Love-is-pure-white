"""传输方式相关配置解析：QQ_TRANSPORT / QQ_INTENTS / QQ_SHARD / QQ_GATEWAY_URL。"""

from __future__ import annotations

import pytest

from sana_bot.config import DEFAULT_QQ_INTENTS, Environment
from sana_bot.errors import ConfigError


def test_defaults_to_webhook_with_c2c_intents() -> None:
    environment = Environment.from_mapping({})
    assert environment.qq_transport == "webhook"
    assert environment.qq_intents == DEFAULT_QQ_INTENTS == 1 << 25
    assert environment.qq_shard == (0, 1)
    assert environment.qq_gateway_url is None


def test_websocket_transport_is_accepted_case_insensitively() -> None:
    environment = Environment.from_mapping({"QQ_TRANSPORT": "  WebSocket  "})
    assert environment.qq_transport == "websocket"


def test_unknown_transport_is_rejected() -> None:
    with pytest.raises(ConfigError) as info:
        Environment.from_mapping({"QQ_TRANSPORT": "grpc"})
    assert info.value.reason_code == "qq_transport_invalid"


@pytest.mark.parametrize("raw", ["1,4", " 0 , 1 ", "3,4"])
def test_valid_shard_is_parsed(raw: str) -> None:
    shard, total = Environment.from_mapping({"QQ_SHARD": raw}).qq_shard
    assert 0 <= shard < total


@pytest.mark.parametrize("raw", ["4,4", "-1,2", "0", "a,b", "0,0"])
def test_invalid_shard_is_rejected(raw: str) -> None:
    with pytest.raises(ConfigError) as info:
        Environment.from_mapping({"QQ_SHARD": raw})
    assert info.value.reason_code == "qq_shard_invalid"


def test_intents_override_and_gateway_url() -> None:
    environment = Environment.from_mapping(
        {
            "QQ_INTENTS": str(1 << 25 | 1 << 30),
            "QQ_GATEWAY_URL": "wss://api.bot.qq.com/websocket/",
        }
    )
    assert environment.qq_intents == (1 << 25 | 1 << 30)
    assert environment.qq_gateway_url == "wss://api.bot.qq.com/websocket/"
    assert environment.as_public_payload()["qq_transport"] == "webhook"
    assert environment.as_public_payload()["qq_shard"] == [0, 1]
