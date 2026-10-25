"""语义切分与向量编码的集成测试（离线 fake 向量）。

真实本地模型的质量验证单列 ``embedding`` 标记，不与 fake 结果混为一谈。
"""

from __future__ import annotations

import pytest

from sana_bot.enums import SpanKind
from sana_bot.errors import ConfigError, NotReadyError
from sana_bot.ingest.models import EMBEDDING_DIMENSION, EMBEDDING_MAX_TOKENS, SourceSpan
from sana_bot.ingest.semantic_splitter import (
    SplitterParameters,
    cosine_similarity,
    split_semantics,
)
from sana_bot.retrieval.embeddings import (
    EmbeddingBackend,
    EmbeddingInputTooLong,
    EmbeddingSpec,
    EmbeddingUnavailable,
)
from tests.support import CharTokenizer, FakeEmbeddingsProvider, topic_vectors


def _span(text: str, *, node: str = "n1", speaker: str | None = "sana", kind: SpanKind = SpanKind.DIALOGUE, start: int = 0) -> SourceSpan:
    return SourceSpan(
        id=f"span-{node}-{start}-{hash(text) % 9999}",
        node_id=node,
        kind=kind,
        raw_text=text,
        source_start=start,
        source_end=start + len(text),
        speaker=speaker,
    )


def test_topic_change_creates_semantic_boundary() -> None:
    spans = [
        _span("今天社团来了一只流浪猫。它毛软软的，很亲人。", start=0),
        _span("说到点心，附近新开了和果子店。料理课上老师夸了我。", start=25),
    ]
    result = split_semantics(
        spans, index_version="v1", embed=topic_vectors(), parameters=SplitterParameters()
    )
    assert len(result.units) == 2
    assert result.metrics["boundary_count"] >= 1
    boundary = next(unit for unit in result.units[1].boundary_evidence.items() if unit[0] == "method")
    assert boundary[1] == "semantic_window"


def test_same_topic_stays_in_one_unit() -> None:
    spans = [
        _span("社团今天收养了一只小猫。猫咪很粘人。", start=0),
        _span("流浪猫咪还会在社团门口等着。", start=18),
    ]
    result = split_semantics(
        spans, index_version="v1", embed=topic_vectors(), parameters=SplitterParameters()
    )
    assert len(result.units) == 1
    assert result.units[0].token_count == sum(len(span.raw_text) for span in spans)


def test_cross_role_question_and_answer_are_linked_not_merged() -> None:
    spans = [
        _span("你：今天社团怎么样？", speaker="user", start=0),
        _span("纱凪：猫咪来了，闹得不行。", speaker="sana", start=12),
    ]
    result = split_semantics(
        spans,
        index_version="v1",
        embed=topic_vectors(),
        parameters=SplitterParameters(boundary_similarity_threshold=0.1),
    )
    assert len(result.units) == 2
    first, second = result.units
    assert first.speaker_labels == ("user",)
    assert second.speaker_labels == ("sana",)
    assert second.id in first.context_links
    assert first.id in second.context_links


def test_long_passage_stays_within_token_budget() -> None:
    long_text = "这是一段很长的发言。" * 40
    spans = [_span(long_text, start=0)]
    parameters = SplitterParameters(max_unit_tokens=100, min_unit_tokens=10)
    result = split_semantics(
        spans, index_version="v1", embed=topic_vectors(), parameters=parameters
    )
    assert len(result.units) > 1
    for unit in result.units:
        assert unit.token_count <= parameters.max_unit_tokens
    assert "".join(unit.text for unit in result.units) == long_text


def test_single_over_long_sentence_is_subdivided_with_parent_and_offset() -> None:
    single = "很长的句子" * 50 + "。"
    spans = [_span(single, start=0)]
    parameters = SplitterParameters(max_unit_tokens=100, min_unit_tokens=10)
    result = split_semantics(
        spans, index_version="v1", embed=topic_vectors(), parameters=parameters
    )
    assert any(unit.parent_unit_id for unit in result.units), "超长单句必须细分并保留父单元"
    for unit in result.units:
        if unit.parent_unit_id:
            assert unit.offset_in_parent is not None
            assert unit.token_count <= parameters.max_unit_tokens
    assert any(issue["reason_code"] == "single_sentence_over_limit" for issue in result.issues)
    assert "".join(unit.text for unit in result.units) == single


def test_parameters_are_validated() -> None:
    with pytest.raises(ConfigError):
        SplitterParameters(boundary_similarity_threshold=1.5).validate()
    with pytest.raises(ConfigError):
        SplitterParameters(max_unit_tokens=0).validate()
    with pytest.raises(ConfigError):
        SplitterParameters(min_unit_tokens=500, max_unit_tokens=100).validate()


def test_cosine_similarity_is_deterministic() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0


# --------------------------------------------------------------------------- 向量编码


def _backend(**kwargs: object) -> EmbeddingBackend:
    provider = kwargs.pop("provider", None) or FakeEmbeddingsProvider()
    spec = EmbeddingSpec(
        model_id="fake/model",
        revision="deadbeef",
        **kwargs,  # type: ignore[arg-type]
    )
    return EmbeddingBackend(
        spec,
        embeddings_factory=lambda _spec: provider,
        token_counter=CharTokenizer(),
    )


def test_documents_are_encoded_with_fixed_dimension() -> None:
    backend = _backend()
    vectors = backend.embed_documents(["社团的猫咪", "点心"])
    assert len(vectors) == 2
    assert all(len(vector) == EMBEDDING_DIMENSION for vector in vectors)


def test_over_limit_input_raises_instead_of_truncating() -> None:
    backend = _backend()
    too_long = "很" * (EMBEDDING_MAX_TOKENS + 5)
    with pytest.raises(EmbeddingInputTooLong) as info:
        backend.embed_documents([too_long])
    assert info.value.details["limit"] == 512
    assert info.value.details["token_count"] > 512


def test_query_instruction_counts_toward_limit() -> None:
    backend = _backend()
    # 文档长度恰好 500 字符可以编码，但加检索指令后超过 512 即拒绝
    backend.embed_documents(["字" * 500])
    with pytest.raises(EmbeddingInputTooLong):
        backend.embed_query("字" * 500)


def test_revision_is_required() -> None:
    with pytest.raises(NotReadyError):
        EmbeddingSpec(model_id="fake/model", revision="").validate()


def test_wrong_dimension_and_non_finite_are_rejected() -> None:
    provider = FakeEmbeddingsProvider()
    provider.bad_dimension = True
    backend = EmbeddingBackend(
        EmbeddingSpec(model_id="fake/model", revision="deadbeef"),
        embeddings_factory=lambda _spec: provider,
    )
    with pytest.raises(EmbeddingUnavailable):
        backend.embed_documents(["文本"])

    provider = FakeEmbeddingsProvider()
    provider.non_finite = True
    backend = EmbeddingBackend(
        EmbeddingSpec(model_id="fake/model", revision="deadbeef"),
        embeddings_factory=lambda _spec: provider,
    )
    with pytest.raises(EmbeddingUnavailable):
        backend.embed_documents(["文本"])


def test_offline_without_cache_fails_clearly(tmp_path) -> None:
    backend = EmbeddingBackend(
        EmbeddingSpec(model_id="BAAI/bge-small-zh-v1.5", revision="missing", cache_dir=tmp_path),
        allow_download=False,
    )
    with pytest.raises(EmbeddingUnavailable):
        backend.embed_documents(["文本"])


@pytest.mark.embedding
def test_real_local_model_quality_uses_pinned_revision(tmp_path) -> None:
    """真实本地模型校准：必须使用预下载且固定 revision 的缓存。"""
    from sana_bot.retrieval.local_model import describe_cache

    revision = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
    info = describe_cache("BAAI/bge-small-zh-v1.5", tmp_path)
    if revision not in info["snapshots"]:  # type: ignore[operator]
        pytest.skip("本地未预下载固定 revision 的向量模型，真实质量验证未执行")
