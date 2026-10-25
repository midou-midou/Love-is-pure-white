"""远程向量后端契约（OpenAI 兼容，例如硅基流动）。

用 respx 固定接口形状；结论不依赖真实密钥，离线可复现。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from sana_bot.errors import ConfigError, NotReadyError
from sana_bot.retrieval.embeddings import (
    EmbeddingBackend,
    EmbeddingInputTooLong,
    EmbeddingKind,
    EmbeddingSpec,
    EmbeddingUnavailable,
    HeuristicTokenCounter,
    RemoteEmbeddingBackend,
    build_backend,
)

BASE = "https://api.siliconflow.cn/v1"
URL = BASE + "/embeddings"
KEY = "sk-test-key-value-0123456789"
DIMENSION = 1024


def _spec(**overrides: object) -> EmbeddingSpec:
    payload: dict[str, object] = {
        "kind": EmbeddingKind.OPENAI_COMPATIBLE,
        "model_id": "BAAI/bge-m3",
        "base_url": BASE,
        "api_key": KEY,
        "dimension": DIMENSION,
        "max_tokens": 8192,
        "query_instruction": "",
    }
    payload.update(overrides)
    return EmbeddingSpec(**payload)  # type: ignore[arg-type]


def _vector(seed: float = 0.0) -> list[float]:
    values = [seed] * DIMENSION
    values[0] = 1.0
    return values


def _response(count: int = 1, *, model: str = "BAAI/bge-m3", tokens: int = 12) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "data": [
                {"index": index, "embedding": _vector(float(index))} for index in range(count)
            ],
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
        },
    )


# --------------------------------------------------------------------------- 规格校验


def test_api_kind_requires_key_and_base_url() -> None:
    with pytest.raises(NotReadyError):
        _spec(api_key="").validate()
    with pytest.raises(ConfigError):
        _spec(base_url="").validate()


def test_local_kind_requires_revision() -> None:
    spec = EmbeddingSpec(kind=EmbeddingKind.LOCAL_HF, model_id="BAAI/bge-small-zh-v1.5")
    with pytest.raises(NotReadyError):
        spec.validate()


def test_build_backend_selects_by_kind() -> None:
    remote = build_backend(_spec())
    assert isinstance(remote, RemoteEmbeddingBackend)
    local = build_backend(
        EmbeddingSpec(kind=EmbeddingKind.LOCAL_HF, model_id="m", revision="deadbeef")
    )
    assert isinstance(local, EmbeddingBackend)


def test_unknown_kind_is_rejected_via_environment() -> None:
    from sana_bot.config import Environment
    from sana_bot.runtime import embedding_spec_from

    with pytest.raises(ConfigError):
        embedding_spec_from(Environment(embedding_kind="openai"))


# --------------------------------------------------------------------------- 正常路径


@respx.mock
def test_batch_encoding_returns_vectors_in_index_order() -> None:
    route = respx.post(URL).mock(return_value=_response(3, tokens=42))
    backend = RemoteEmbeddingBackend(_spec())
    vectors = backend.embed_documents(["第一段", "第二段", "第三段"])
    assert len(vectors) == 3
    assert all(len(vector) == DIMENSION for vector in vectors)
    assert route.call_count == 1
    assert backend.actual_token_counts == [42], "必须记录提供方返回的真实 token 数"
    assert backend.reported_model == "BAAI/bge-m3"


@respx.mock
def test_query_encoding_uses_same_endpoint() -> None:
    respx.post(URL).mock(return_value=_response(1))
    backend = RemoteEmbeddingBackend(_spec())
    vector = backend.embed_query("查询文本")
    assert len(vector) == DIMENSION


@respx.mock
def test_empty_input_makes_no_request() -> None:
    route = respx.post(URL).mock(return_value=_response(1))
    backend = RemoteEmbeddingBackend(_spec())
    assert backend.embed_documents([]) == []
    assert route.call_count == 0


def test_probe_reports_dimension_and_model() -> None:
    with respx.mock:
        respx.post(URL).mock(return_value=_response(1, tokens=7))
        payload = RemoteEmbeddingBackend(_spec()).probe()
    assert payload["ok"] is True
    assert payload["dimension"] == DIMENSION
    assert payload["prompt_tokens"] == 7
    assert payload["reported_model"] == "BAAI/bge-m3"
    assert payload["base_url_host"] == "api.siliconflow.cn"


# --------------------------------------------------------------------------- 失败分类


@respx.mock
def test_auth_failure_is_classified() -> None:
    respx.post(URL).mock(return_value=httpx.Response(401, json={"message": "Unauthorized"}))
    with pytest.raises(EmbeddingUnavailable) as info:
        RemoteEmbeddingBackend(_spec()).embed_documents(["x"])
    assert info.value.reason_code == "embedding_auth_failed"
    assert info.value.details["http_status"] == 401
    assert KEY not in str(info.value.details)


@respx.mock
def test_rate_limit_is_classified() -> None:
    respx.post(URL).mock(return_value=httpx.Response(429, json={"message": "rate limit"}))
    with pytest.raises(EmbeddingUnavailable) as info:
        RemoteEmbeddingBackend(_spec()).embed_documents(["x"])
    assert info.value.reason_code == "embedding_rate_limited"


@respx.mock
def test_over_length_rejection_is_surfaced_not_truncated() -> None:
    """提供方拒绝超长输入时必须显式失败，绝不用截断绕过。"""
    respx.post(URL).mock(
        return_value=httpx.Response(400, json={"code": 20015, "message": "The parameter is invalid"})
    )
    with pytest.raises(EmbeddingUnavailable) as info:
        RemoteEmbeddingBackend(_spec(max_tokens=100)).embed_documents(["字" * 50])
    assert info.value.reason_code in {"embedding_input_rejected", "embedding_bad_request"}
    assert info.value.details["provider_code"] == 20015


@respx.mock
def test_server_error_and_network_error_are_distinct() -> None:
    respx.post(URL).mock(return_value=httpx.Response(503, text="upstream down"))
    with pytest.raises(EmbeddingUnavailable) as info:
        RemoteEmbeddingBackend(_spec()).embed_documents(["x"])
    assert info.value.reason_code == "embedding_service_error"

    respx.post(URL).mock(side_effect=httpx.ConnectError("dns"))
    with pytest.raises(EmbeddingUnavailable) as info2:
        RemoteEmbeddingBackend(_spec()).embed_documents(["x"])
    assert info2.value.reason_code == "embedding_network_error"


@respx.mock
def test_non_json_and_shape_errors_are_reported() -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(EmbeddingUnavailable) as info:
        RemoteEmbeddingBackend(_spec()).embed_documents(["x"])
    assert info.value.reason_code == "embedding_bad_response"

    respx.post(URL).mock(return_value=httpx.Response(200, json={"unexpected": True}))
    with pytest.raises(EmbeddingUnavailable):
        RemoteEmbeddingBackend(_spec()).embed_documents(["x"])


@respx.mock
def test_dimension_mismatch_is_rejected() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [0.1] * 512}],
                "usage": {"prompt_tokens": 3},
            },
        )
    )
    with pytest.raises(EmbeddingUnavailable) as info:
        RemoteEmbeddingBackend(_spec(dimension=1024)).embed_documents(["x"])
    assert info.value.reason_code == "embedding_dimension_mismatch"


@respx.mock
def test_non_finite_values_are_rejected() -> None:
    import json

    vector = _vector()
    vector[5] = float("nan")
    # httpx 无法序列化 NaN，这里手工构造允许 NaN 的原始 JSON 文本
    body = json.dumps(
        {"data": [{"index": 0, "embedding": vector}], "usage": {"prompt_tokens": 3}},
        allow_nan=True,
    )
    respx.post(URL).mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "application/json"})
    )
    with pytest.raises(EmbeddingUnavailable) as info:
        RemoteEmbeddingBackend(_spec()).embed_documents(["x"])
    assert info.value.reason_code == "embedding_non_finite"


@respx.mock
def test_count_mismatch_is_rejected() -> None:
    respx.post(URL).mock(return_value=_response(2))
    with pytest.raises(EmbeddingUnavailable) as info:
        RemoteEmbeddingBackend(_spec()).embed_documents(["only-one"])
    assert info.value.reason_code == "embedding_count_mismatch"


# --------------------------------------------------------------------------- 上限与密钥


@respx.mock
def test_local_precheck_rejects_over_limit_without_calling_provider() -> None:
    route = respx.post(URL).mock(return_value=_response(1))
    backend = RemoteEmbeddingBackend(_spec(max_tokens=100))
    with pytest.raises(EmbeddingInputTooLong) as info:
        backend.embed_documents(["字" * 200])
    assert info.value.details["estimated"] is True
    assert route.call_count == 0, "超长输入不应发出请求"


@respx.mock
def test_key_never_appears_in_manifest_or_errors() -> None:
    respx.post(URL).mock(return_value=_response(1))
    backend = RemoteEmbeddingBackend(_spec())
    backend.embed_documents(["文本"])
    payload = str(backend.manifest()) + str(backend.spec.to_manifest())
    assert KEY not in payload
    manifest = backend.spec.to_manifest()
    assert manifest["kind"] == "openai_compatible"
    assert manifest["dimension"] == DIMENSION
    assert manifest["revision"] == "BAAI/bge-m3@api.siliconflow.cn"
    assert "api_key" not in manifest


def test_heuristic_counter_bounds() -> None:
    counter = HeuristicTokenCounter()
    assert counter.count("") == 0
    assert counter.count("中") == 1 + counter.SPECIAL_TOKENS
    assert counter.count("中文测试") == 4 + counter.SPECIAL_TOKENS
    # 纯 ASCII 按约 3.5 字符/token 上取整，仍显著小于字符数
    assert counter.count("a" * 35) == 10 + counter.SPECIAL_TOKENS


@respx.mock
def test_documents_are_sent_in_batches() -> None:
    """batch_size=16 时 40 条输入应拆成 3 次请求，且顺序保持。"""
    route = respx.post(URL).mock(
        side_effect=lambda request: _response(len(request.content and __import__("json").loads(request.content)["input"]))
    )
    backend = RemoteEmbeddingBackend(_spec(batch_size=16))
    vectors = backend.embed_documents([f"第{i}段" for i in range(40)])
    assert len(vectors) == 40
    assert route.call_count == 3
    assert backend.batches == 3
    assert len(backend.actual_token_counts) == 3


def _provider_error(code: int, message: str = "The parameter is invalid.") -> httpx.Response:
    return httpx.Response(
        400,
        json={"code": code, "message": message, "data": None},
    )


@respx.mock
def test_transient_provider_rejection_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """提供方偶发的瞬时拒绝（400 + 20015）重试后仍能成功，不把可用服务判成坏输入。"""
    from sana_bot.retrieval import embeddings as module

    monkeypatch.setattr(module, "RETRY_BACKOFF_SECONDS", (0, 0))
    route = respx.post(URL).mock(
        side_effect=[_provider_error(20015), _response(1)]
    )
    backend = RemoteEmbeddingBackend(_spec())
    vectors = backend.embed_documents(["第一段"])
    assert len(vectors) == 1
    assert route.call_count == 2


@respx.mock
def test_deterministic_rejection_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """确定性的坏输入不重试：只调用一次就报错，避免把真正的问题拖成偶发。"""
    from sana_bot.retrieval import embeddings as module

    monkeypatch.setattr(module, "RETRY_BACKOFF_SECONDS", (0, 0))
    route = respx.post(URL).mock(return_value=_provider_error(20041, "too long"))
    backend = RemoteEmbeddingBackend(_spec())
    with pytest.raises(EmbeddingUnavailable) as rejected:
        backend.embed_documents(["第一段"])
    assert rejected.value.reason_code == "embedding_input_rejected"
    assert route.call_count == 1


@respx.mock
def test_server_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """5xx 属瞬时故障：有限次重试后成功。"""
    from sana_bot.retrieval import embeddings as module

    monkeypatch.setattr(module, "RETRY_BACKOFF_SECONDS", (0, 0))
    route = respx.post(URL).mock(
        side_effect=[httpx.Response(503, json={"message": "busy"}), _response(2)]
    )
    backend = RemoteEmbeddingBackend(_spec())
    vectors = backend.embed_documents(["第一段", "第二段"])
    assert len(vectors) == 2
    assert route.call_count == 2
