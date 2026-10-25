"""QQ 鉴权契约：官方响应形状、凭据失败分类、缓存刷新与不泄露 token。

真实平台返回把 expires_in 作为字符串数字，这里固定该回归以避免再次误判为业务失败。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from sana_bot.adapters.qq_auth import (
    TOKEN_PATH,
    AccessToken,
    QQAuthClient,
    QQCredentials,
    _coerce_seconds,
    bot_secret_seed,
    sign_payload,
    verify_dispatch_signature,
)
from sana_bot.errors import ConfigError, ExternalServiceError

BASE = "https://api.bot.qq.com"
URL = BASE + TOKEN_PATH
SECRET = "bot-secret-abcdefghijklmnopqrstuvwxyz"


def _client() -> QQAuthClient:
    return QQAuthClient(
        credentials=QQCredentials(app_id="123456", app_secret=SECRET, base_url=BASE)
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        (7200, 7200.0),
        (7200.5, 7200.5),
        ("4458", 4458.0),
        (" 7200 ", 7200.0),
        ("", None),
        ("abc", None),
        (None, None),
        (True, None),
    ],
)
def test_expires_in_coercion(value: object, expected: float | None) -> None:
    """官方把 expires_in 返回为字符串数字，必须接受。"""
    assert _coerce_seconds(value) == expected


@respx.mock
async def test_string_expires_in_is_accepted_and_cached() -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-abc", "expires_in": "4458"})
    )
    client = _client()
    token = await client.access_token()
    assert token == "tok-abc"
    assert route.call_count == 1
    # 缓存命中：第二次调用不得再发请求
    assert await client.access_token() == "tok-abc"
    assert route.call_count == 1


@respx.mock
async def test_near_expiry_token_is_refreshed() -> None:
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-1", "expires_in": "120"}),
            httpx.Response(200, json={"access_token": "tok-2", "expires_in": "7200"}),
        ]
    )
    client = _client()
    fake_now = {"value": 1000.0}
    client._clock = lambda: fake_now["value"]  # type: ignore[assignment]
    assert await client.access_token() == "tok-1"
    # 进入安全余量内必须刷新
    fake_now["value"] += 100
    assert await client.access_token() == "tok-2"


@respx.mock
async def test_business_error_code_is_reported() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(200, json={"code": 100007, "message": "invalid appid"})
    )
    with pytest.raises(ExternalServiceError) as info:
        await _client().access_token()
    assert info.value.reason_code == "qq_token_business_error"
    assert info.value.details["code"] == 100007
    assert info.value.details["response_keys"] == ["code", "message"]


@respx.mock
async def test_missing_token_is_reported_without_leaking_body() -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json={"expires_in": "7200"}))
    with pytest.raises(ExternalServiceError) as info:
        await _client().access_token()
    assert info.value.reason_code == "qq_token_business_error"
    assert info.value.details["expires_in_type"] == "str"


@respx.mock
async def test_non_positive_expires_in_is_rejected() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": "0"})
    )
    with pytest.raises(ExternalServiceError) as info:
        await _client().access_token()
    assert info.value.reason_code == "qq_token_business_error"
    assert info.value.details["expires_in"] == 0.0


@respx.mock
async def test_http_error_records_status_and_host() -> None:
    respx.post(URL).mock(return_value=httpx.Response(401, json={"error": "unauthorized"}))
    with pytest.raises(ExternalServiceError) as info:
        await _client().access_token()
    assert info.value.reason_code == "qq_token_http_error"
    assert info.value.details["http_status"] == 401
    assert info.value.details["url_host"] == "api.bot.qq.com"


@respx.mock
async def test_network_failure_is_classified() -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("dns failure"))
    with pytest.raises(ExternalServiceError) as info:
        await _client().access_token()
    assert info.value.reason_code == "qq_token_network_error"


def test_missing_credentials_are_rejected_before_network() -> None:
    client = QQAuthClient(credentials=QQCredentials(app_id=None, app_secret=None, base_url=BASE))
    with pytest.raises(ConfigError):
        client.credentials.require()


def test_fingerprint_is_stable_and_not_the_token() -> None:
    token = "tok-secret-value"
    fingerprint = QQAuthClient.fingerprint(token)
    assert fingerprint != token
    assert len(fingerprint) == 12
    assert QQAuthClient.fingerprint(token) == fingerprint


def test_access_token_expiry_uses_safety_margin() -> None:
    """余量为 60 秒：进入余量即视为需要刷新。"""
    token = AccessToken(value="t", expires_at=1000.0)
    assert token.expired(now=940.0) is True   # 恰好进入余量
    assert token.expired(now=950.0) is True
    assert token.expired(now=900.0) is False  # 仍在余量之外
    assert token.expired(now=1000.0) is True


def test_bot_secret_seed_is_32_bytes_and_rejects_empty() -> None:
    assert len(bot_secret_seed("short")) == 32
    assert len(bot_secret_seed(SECRET)) == 32
    with pytest.raises(ConfigError):
        bot_secret_seed("")


def test_signature_roundtrip_and_tamper_detection() -> None:
    body = b'{"op":0}'
    signature = sign_payload(SECRET, b"1730000000" + body)
    assert verify_dispatch_signature(
        secret=SECRET, signature_hex=signature, timestamp="1730000000", raw_body=body
    )
    assert not verify_dispatch_signature(
        secret=SECRET, signature_hex=signature, timestamp="1730000001", raw_body=body
    )
    assert not verify_dispatch_signature(
        secret=SECRET, signature_hex=signature, timestamp="1730000000", raw_body=b'{"op":1}'
    )
    assert not verify_dispatch_signature(
        secret=SECRET, signature_hex="not-hex", timestamp="1730000000", raw_body=body
    )
    assert not verify_dispatch_signature(
        secret=SECRET, signature_hex=signature, timestamp="", raw_body=body
    )
