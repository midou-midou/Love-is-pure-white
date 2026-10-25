"""本地向量后端依赖是可选组：缺依赖时必须给出可操作的 reason_code，而不是误报缓存缺失。"""

from __future__ import annotations

import importlib.util

import pytest

from sana_bot.retrieval import embeddings as embeddings_module
from sana_bot.retrieval.embeddings import EmbeddingBackend, EmbeddingSpec, EmbeddingUnavailable


def _local_spec() -> EmbeddingSpec:
    return EmbeddingSpec(
        model_id="BAAI/bge-small-zh-v1.5",
        revision="0123456789abcdef0123456789abcdef01234567",
    )


def test_missing_extra_reports_dedicated_reason_code(monkeypatch: pytest.MonkeyPatch) -> None:
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args: object, **kwargs: object):
        if name == "langchain_huggingface":
            return None
        return real_find_spec(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(embeddings_module.importlib.util, "find_spec", fake_find_spec)

    backend = EmbeddingBackend(_local_spec())
    with pytest.raises(EmbeddingUnavailable) as info:
        backend.ensure_available()

    assert info.value.reason_code == "local_embeddings_extra_missing"
    assert "local-embeddings" in str(info.value)


def test_extra_present_but_model_uncached_reports_cache_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embeddings_module.importlib.util, "find_spec", lambda *a, **k: object())
    monkeypatch.setattr(
        "sana_bot.retrieval.local_model.model_is_cached", lambda *a, **k: False, raising=True
    )

    backend = EmbeddingBackend(_local_spec())
    with pytest.raises(EmbeddingUnavailable) as info:
        backend.ensure_available()

    assert info.value.reason_code != "local_embeddings_extra_missing"


def test_injected_factory_never_requires_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings_module.importlib.util, "find_spec", lambda *a, **k: None)

    class _Stub:
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 8 for _ in texts]

        def embed_query(self, text: str) -> list[float]:
            return [0.0] * 8

    spec = EmbeddingSpec(dimension=8, revision="deadbeef")
    backend = EmbeddingBackend(spec, embeddings_factory=lambda _spec: _Stub())
    backend.ensure_available()
    assert backend.manifest()["revision"] == "deadbeef"
