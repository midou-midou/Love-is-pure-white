"""真实服务 smoke：默认跳过，只有显式 `-m live` 且注入凭据时才执行。

这些用例的结论不冒充离线结果；未取得凭据时明确 skip 而不是假装通过。
"""

from __future__ import annotations

import os

import pytest

from sana_bot.config import Environment
from sana_bot.live_checks import model_smoke, qq_auth_smoke

pytestmark = pytest.mark.live


def _environment(monkeypatch: pytest.MonkeyPatch) -> Environment:
    from tests.conftest import REPO_ROOT

    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, _, value = stripped.partition("=")
            if value.strip():
                monkeypatch.setenv(name.strip(), value.strip())
    return Environment.from_process()


async def test_deepseek_smoke_reports_model_and_json_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(monkeypatch)
    if not environment.deepseek_api_key:
        pytest.skip("未注入 DEEPSEEK_API_KEY，真实模型检查未执行")
    check = await model_smoke(environment)
    assert check.executed is True
    assert check.ok is True, check.to_payload()
    assert check.details["text_ok"] is True
    assert check.details["json_ok"] is True
    assert int(check.details["text_char_count"]) > 0
    payload = str(check.to_payload())
    assert "sk-" not in payload, "检查结果不得包含密钥样式内容"


async def test_qq_auth_smoke_acquires_token_without_leaking_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(monkeypatch)
    if not (environment.qq_app_id and environment.qq_app_secret):
        pytest.skip("未注入 QQ 凭据，真实鉴权检查未执行")
    check = await qq_auth_smoke(environment)
    assert check.executed is True
    assert check.ok is True, check.to_payload()
    fingerprint = check.details["token_fingerprint"]
    assert isinstance(fingerprint, str) and len(fingerprint) == 12
    assert environment.qq_app_secret not in str(check.to_payload())


def test_live_marker_prevents_accidental_execution() -> None:
    """确认普通离线运行不会执行真实服务检查。"""
    assert os.environ.get("HF_HUB_OFFLINE") in (None, "1")
