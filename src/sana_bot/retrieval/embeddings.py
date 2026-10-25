"""向量编码：本地 HuggingFace 模型或 OpenAI 兼容的远程向量 API。

两种形态共用 :class:`EmbeddingSpec` 与同一套校验，因此上层（切分、构建、检索）
无需知道后端差异。共同约束：
- 维度与配置一致，且必须为有限实数；不一致直接失败，不静默降级；
- 输入超过上限时**拒绝**而不是截断（远程由提供方报错，本地由计数预先拒绝）；
- 实际 token 数优先采用提供方返回值，其次使用保守上界估算；
- 绝不记录密钥、向量原文或提供方返回的敏感内容。
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from sana_bot.errors import ConfigError, NotReadyError
from sana_bot.ingest.models import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MAX_TOKENS,
)

__all__ = [
    "EmbeddingKind",
    "EmbeddingSpec",
    "TokenCounter",
    "TokenizerCounter",
    "HeuristicTokenCounter",
    "EmbeddingBackend",
    "RemoteEmbeddingBackend",
    "build_backend",
    "EmbeddingInputTooLong",
    "EmbeddingUnavailable",
    "DEFAULT_QUERY_INSTRUCTION",
    "EMBEDDING_DEFAULT_DIMENSION",
    "EMBEDDING_DEFAULT_MAX_TOKENS",
    "RETRY_ATTEMPTS",
    "RETRY_BACKOFF_SECONDS",
    "RETRY_STATUSES",
    "RETRY_PROVIDER_CODES",
]

#: 默认维度与输入上限来自领域常量，避免两处定义漂移
EMBEDDING_DEFAULT_DIMENSION = EMBEDDING_DIMENSION
EMBEDDING_DEFAULT_MAX_TOKENS = EMBEDDING_MAX_TOKENS

#: 提供方偶发的瞬时拒绝/限流：有限次重试；确定性失败必须立刻报错，不能被重试掩盖
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (0.5, 1.5)
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_PROVIDER_CODES = frozenset({20015})

#: bge 系列中文检索建议的查询指令（本地小模型用；bge-m3/Qwen 通常不需要）
DEFAULT_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

#: 远程提供方错误响应中的常见标记
_AUTH_MARKERS = ("unauthorized", "invalid api key", "authentication", "401")
_RATE_MARKERS = ("rate limit", "too many requests", "429")


class EmbeddingKind(StrEnum):
    LOCAL_HF = "local"
    OPENAI_COMPATIBLE = "openai_compatible"


class EmbeddingInputTooLong(NotReadyError):
    """输入超过上限；必须继续分段，禁止截断。"""

    reason_code = "embedding_input_too_long"


class EmbeddingUnavailable(NotReadyError):
    """向量后端不可用（模型缺失、鉴权失败、网络或服务异常）。"""

    reason_code = "embedding_unavailable"


@runtime_checkable
class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


class TokenizerCounter:
    """使用真实 tokenizer 精确计数（本地模型可用时）。"""

    def __init__(self, tokenizer: Any) -> None:
        self._tokenizer = tokenizer

    def count(self, text: str) -> int:
        if not text:
            return 0
        encoded = self._tokenizer(text, add_special_tokens=True)
        return len(encoded["input_ids"])


class HeuristicTokenCounter:
    """无 tokenizer 时的保守上界估算：中日韩字符按 1 token、其余按约 3.5 字符/token。

    这是**上界**而不是精确值：宁可多分一段，也不冒静默截断的风险。
    真实计数以提供方返回的 usage 为准。
    """

    CJK_RATIO = 1.0
    OTHER_CHARS_PER_TOKEN = 3.5
    SPECIAL_TOKENS = 2

    def count(self, text: str) -> int:
        if not text:
            return 0
        cjk = 0
        other = 0
        for char in text:
            if _is_cjk(char):
                cjk += 1
            else:
                other += 1
        estimate = cjk * self.CJK_RATIO + math.ceil(other / self.OTHER_CHARS_PER_TOKEN)
        return int(estimate) + self.SPECIAL_TOKENS


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF      # CJK 统一表意文字
        or 0x3400 <= code <= 0x4DBF   # 扩展 A
        or 0x3040 <= code <= 0x30FF   # 日文假名
        or 0xAC00 <= code <= 0xD7AF   # 韩文音节
        or 0xF900 <= code <= 0xFAFF   # 兼容表意文字
        or 0x3000 <= code <= 0x303F   # CJK 标点
        or 0xFF00 <= code <= 0xFFEF   # 全角形式
    )


@dataclass(frozen=True)
class EmbeddingSpec:
    """编码规格；本地要求精确 revision，远程要求 base_url 与密钥。"""

    model_id: str = "BAAI/bge-small-zh-v1.5"
    kind: EmbeddingKind = EmbeddingKind.LOCAL_HF
    revision: str = ""
    dimension: int = EMBEDDING_DEFAULT_DIMENSION
    max_tokens: int = EMBEDDING_DEFAULT_MAX_TOKENS
    normalize: bool = True
    batch_size: int = 16
    device: str | None = None
    cache_dir: Path | None = None
    query_instruction: str = DEFAULT_QUERY_INSTRUCTION
    base_url: str = ""
    api_key: str = ""
    timeout_seconds: float = 30.0
    api_key_env: str = ""

    def validate(self) -> EmbeddingSpec:
        if not self.model_id:
            raise ConfigError("EMBEDDING_MODEL 不能为空")
        if self.dimension <= 0:
            raise ConfigError(f"EMBEDDING_DIMENSION 必须为正数，当前 {self.dimension}")
        if self.max_tokens <= 0:
            raise ConfigError(f"EMBEDDING_MAX_TOKENS 必须为正数，当前 {self.max_tokens}")
        if self.kind is EmbeddingKind.LOCAL_HF:
            if not self.revision:
                raise NotReadyError(
                    "本地向量模型必须提供精确 EMBEDDING_REVISION（commit）",
                    reason_code="embedding_revision_missing",
                )
        elif self.kind is EmbeddingKind.OPENAI_COMPATIBLE:
            if not self.base_url:
                raise ConfigError("远程向量后端必须配置 EMBEDDING_BASE_URL")
            if not self.api_key:
                raise NotReadyError(
                    "远程向量后端缺少 EMBEDDING_API_KEY",
                    reason_code="embedding_credentials_missing",
                )
        return self

    @property
    def effective_revision(self) -> str:
        """可写入记录与 hash 的版本标识；远程用「模型@host」，因为 API 无 commit 概念。"""
        if self.kind is EmbeddingKind.LOCAL_HF:
            return self.revision
        return self.revision or f"{self.model_id}@{self.host}"

    @property
    def host(self) -> str:
        try:
            from urllib.parse import urlparse

            return urlparse(self.base_url).netloc or self.base_url
        except Exception:  # noqa: BLE001 - 仅用于诊断
            return "unknown"

    def to_manifest(self) -> dict[str, Any]:
        """入库信息：不含密钥；远程额外记录 host 便于追溯。"""
        payload: dict[str, Any] = {
            "kind": self.kind.value,
            "model_id": self.model_id,
            "dimension": self.dimension,
            "max_tokens": self.max_tokens,
            "normalize": self.normalize,
            "query_instruction_hash": hashlib.sha256(
                self.query_instruction.encode("utf-8")
            ).hexdigest(),
        }
        if self.kind is EmbeddingKind.LOCAL_HF:
            payload["revision"] = self.revision
        else:
            payload["base_url_host"] = self.host
            payload["revision"] = self.effective_revision
        return payload


class _BackendBase:
    """两种后端共用的校验逻辑。"""

    def __init__(self, spec: EmbeddingSpec) -> None:
        self.spec = spec.validate()
        self.actual_token_counts: list[int] = []
        self.reported_model: str | None = None
        self.calls = 0
        self.batches = 0

    # ------------------------------------------------------------------ 计数

    def count_tokens(self, text: str) -> int:
        return self.token_counter.count(text)

    @property
    def tokenizer_verified(self) -> bool:
        """只有真实 tokenizer 计数才算容量证据；启发式估算不能用于启用。"""
        return isinstance(self.token_counter, TokenizerCounter)

    def count_query_tokens(self, text: str) -> int:
        return self.count_tokens(self.spec.query_instruction + text)

    def _assert_within_limit(self, texts: Sequence[str], *, query: bool) -> None:
        limit = self.spec.max_tokens
        for text in texts:
            counted = self.count_query_tokens(text) if query else self.count_tokens(text)
            if counted > limit:
                raise EmbeddingInputTooLong(
                    f"输入估计 {counted} token 超过上限 {limit}；必须继续分段，禁止截断",
                    details={"token_count": counted, "limit": limit, "estimated": True},
                )

    def _validated(self, vector: Sequence[float]) -> list[float]:
        values = [float(item) for item in vector]
        if len(values) != self.spec.dimension:
            raise EmbeddingUnavailable(
                f"向量维度 {len(values)} 与配置 {self.spec.dimension} 不一致",
                reason_code="embedding_dimension_mismatch",
            )
        if not all(math.isfinite(value) for value in values):
            raise EmbeddingUnavailable("向量包含非有限实数", reason_code="embedding_non_finite")
        return values

    def manifest(self) -> dict[str, Any]:
        payload = self.spec.to_manifest()
        payload["calls"] = self.calls
        payload["batches"] = self.batches
        payload["reported_model"] = self.reported_model
        payload["token_counter"] = type(self.token_counter).__name__
        return payload

    @property
    def token_counter(self) -> TokenCounter:  # pragma: no cover - 子类覆写
        raise NotImplementedError


class EmbeddingBackend(_BackendBase):
    """本地 HuggingFace 后端；``embeddings_factory`` 可注入替身以便离线测试。"""

    def __init__(
        self,
        spec: EmbeddingSpec,
        *,
        embeddings_factory: Any | None = None,
        token_counter: TokenCounter | None = None,
        allow_download: bool = False,
    ) -> None:
        super().__init__(spec)
        self.allow_download = allow_download
        self._embeddings_factory = embeddings_factory
        self._embeddings: Any | None = None
        self._token_counter = token_counter
        self._loaded_via = "unloaded"

    # ------------------------------------------------------------------ 生命周期

    def ensure_available(self) -> None:
        if self._embeddings is not None:
            return
        if self._embeddings_factory is None:
            self._require_local_extra()
        if self._embeddings_factory is None and not self.allow_download:
            from sana_bot.retrieval.local_model import model_is_cached

            if not model_is_cached(self.spec.model_id, self.spec.revision, self.spec.cache_dir):
                raise EmbeddingUnavailable(
                    "本地未缓存该精确 revision 的向量模型；请先执行显式准备步骤",
                    details={
                        "revision": self.spec.revision,
                        "model": self.spec.model_id,
                        "hint": "或改用 EMBEDDING_KIND=openai_compatible",
                    },
                )
        try:
            self._embeddings = self._build_embeddings()
        except EmbeddingUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - 加载失败必须显式报告
            raise EmbeddingUnavailable(
                f"向量模型加载失败：{type(exc).__name__}: {exc}",
                details={"revision": self.spec.revision, "model": self.spec.model_id},
            ) from exc
        self._loaded_via = "factory" if self._embeddings_factory else "huggingface"

    def _require_local_extra(self) -> None:
        """本地向量依赖是可选组；缺依赖要先于缓存缺失报出来，否则提示会误导。"""
        if importlib.util.find_spec("langchain_huggingface") is not None:
            return
        raise EmbeddingUnavailable(
            "本地向量后端需要可选依赖 local-embeddings；请安装 "
            "`uv sync --extra local-embeddings`，或改用 EMBEDDING_KIND=openai_compatible",
            reason_code="local_embeddings_extra_missing",
        )

    def _build_embeddings(self) -> Any:
        if self._embeddings_factory is not None:
            return self._embeddings_factory(self.spec)
        from langchain_huggingface import HuggingFaceEmbeddings

        model_kwargs: dict[str, Any] = {"revision": self.spec.revision}
        if self.spec.device:
            model_kwargs["device"] = self.spec.device
        if not self.allow_download:
            model_kwargs["local_files_only"] = True
        return HuggingFaceEmbeddings(
            model_name=self.spec.model_id,
            cache_folder=str(self.spec.cache_dir) if self.spec.cache_dir else None,
            model_kwargs=model_kwargs,
            encode_kwargs={"normalize_embeddings": self.spec.normalize},
        )

    @property
    def loaded(self) -> bool:
        return self._embeddings is not None

    @property
    def token_counter(self) -> TokenCounter:
        if self._token_counter is not None:
            return self._token_counter
        if self._embeddings is not None:
            tokenizer = _extract_tokenizer(self._embeddings)
            if tokenizer is not None:
                self._token_counter = TokenizerCounter(tokenizer)
                return self._token_counter
        self._token_counter = HeuristicTokenCounter()
        return self._token_counter

    # ------------------------------------------------------------------ 编码

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.ensure_available()
        if not texts:
            return []
        self._assert_within_limit(texts, query=False)
        vectors = self._encode(list(texts))
        self.calls += len(texts)
        self.actual_token_counts.extend(self.count_tokens(text) for text in texts)
        return [self._validated(vector) for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        self.ensure_available()
        self._assert_within_limit([text], query=True)
        vectors = self._encode([self.spec.query_instruction + text])
        if not vectors:
            raise EmbeddingUnavailable("查询编码未返回向量", reason_code="embedding_empty_result")
        self.calls += 1
        return self._validated(vectors[0])

    def _encode(self, texts: list[str]) -> list[list[float]]:
        assert self._embeddings is not None
        try:
            vectors = self._embeddings.embed_documents(texts)
        except Exception as exc:  # noqa: BLE001 - 外部失败必须显式报告
            raise EmbeddingUnavailable(
                f"向量编码失败：{type(exc).__name__}: {exc}",
                reason_code="embedding_encode_failed",
            ) from exc
        return [list(map(float, vector)) for vector in vectors]


class RemoteEmbeddingBackend(_BackendBase):
    """OpenAI 兼容的远程向量后端（例如硅基流动）。

    只用 ``POST {base_url}/embeddings``；拒绝静默截断（超长交由提供方报错并显式失败），
    并把提供方返回的 ``usage.prompt_tokens`` 作为真实 token 数记录。
    """

    def __init__(
        self,
        spec: EmbeddingSpec,
        *,
        client: Any | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        super().__init__(spec)
        self._client = client
        self._token_counter = token_counter or HeuristicTokenCounter()

    @property
    def token_counter(self) -> TokenCounter:
        assert self._token_counter is not None
        return self._token_counter

    def ensure_available(self) -> None:
        self.spec.validate()

    @property
    def loaded(self) -> bool:
        return True

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """按 batch_size 分批请求：单请求体积过大时提供方会直接拒绝。"""
        if not texts:
            return []
        self._assert_within_limit(texts, query=False)
        batch = max(1, self.spec.batch_size)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch):
            chunk = list(texts[start : start + batch])
            chunk_vectors, usage = self._request(chunk)
            vectors.extend(chunk_vectors)
            self.actual_token_counts.append(usage)
            self.batches += 1
        if len(vectors) != len(texts):
            raise EmbeddingUnavailable(
                f"分批返回向量数量 {len(vectors)} 与请求 {len(texts)} 不一致",
                reason_code="embedding_count_mismatch",
            )
        self.calls += len(texts)
        return [self._validated(vector) for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        self._assert_within_limit([text], query=True)
        vectors, usage = self._request([self.spec.query_instruction + text])
        if not vectors:
            raise EmbeddingUnavailable("查询编码未返回向量", reason_code="embedding_empty_result")
        self.calls += 1
        self.actual_token_counts.append(usage)
        return self._validated(vectors[0])

    def probe(self) -> dict[str, Any]:
        """一次最小调用，用于 doctor 的连通性与维度核对。"""
        vectors, usage = self._request(["连通性检查"])
        vector = self._validated(vectors[0]) if vectors else []
        return {
            "ok": bool(vector),
            "dimension": len(vector),
            "prompt_tokens": usage,
            "reported_model": self.reported_model,
            "base_url_host": self.spec.host,
        }

    # ------------------------------------------------------------------ HTTP

    def _request(self, texts: list[str]) -> tuple[list[list[float]], int]:
        import httpx

        url = self.spec.base_url.rstrip("/") + "/embeddings"
        payload = {"model": self.spec.model_id, "input": texts}
        headers = {
            "Authorization": f"Bearer {self.spec.api_key}",
            "Content-Type": "application/json",
        }
        owns = self._client is None
        client = self._client or httpx.Client(timeout=self.spec.timeout_seconds)
        try:
            response = self._post_with_retry(client, url, payload, headers)
        finally:
            if owns:
                client.close()

        status = response.status_code
        if status >= 400:
            raise self._error_for(response, status)
        try:
            body = response.json()
        except ValueError as exc:
            raise EmbeddingUnavailable(
                "向量服务返回的不是 JSON",
                reason_code="embedding_bad_response",
                details={"http_status": status, "base_url_host": self.spec.host},
            ) from exc
        data = body.get("data")
        if not isinstance(data, list):
            raise EmbeddingUnavailable(
                "向量服务响应缺少 data 数组",
                reason_code="embedding_bad_response",
                details={
                    "http_status": status,
                    "response_keys": sorted(str(key) for key in body),
                },
            )
        model = body.get("model")
        if isinstance(model, str) and model:
            self.reported_model = model
        usage_block = body.get("usage") or {}
        usage = usage_block.get("prompt_tokens")
        usage_tokens = int(usage) if isinstance(usage, (int, float)) else sum(
            self.count_tokens(text) for text in texts
        )
        ordered = sorted(
            (item for item in data if isinstance(item, dict)),
            key=lambda item: int(item.get("index", 0)),
        )
        vectors: list[list[float]] = []
        for item in ordered:
            embedding = item.get("embedding")
            if not isinstance(embedding, list):
                raise EmbeddingUnavailable(
                    "向量条目缺少 embedding 数组", reason_code="embedding_bad_response"
                )
            vectors.append(embedding)
        if len(vectors) != len(texts):
            raise EmbeddingUnavailable(
                f"返回向量数量 {len(vectors)} 与请求 {len(texts)} 不一致",
                reason_code="embedding_count_mismatch",
            )
        return vectors, usage_tokens

    def _post_with_retry(self, client: Any, url: str, payload: dict, headers: dict) -> Any:
        """发送编码请求；只对明确可重试的失败重试有限次数。

        提供方偶尔用 400 + provider_code 20015 表示瞬时拒绝（同样的输入稍后即可
        通过）。只对这类瞬时码与 429/5xx/网络错误重试，其余失败立即报错，避免把
        真正的超长或非法输入掩盖成可重试。
        """
        import time

        import httpx

        last: EmbeddingUnavailable | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                response = client.post(url, json=payload, headers=headers)
            except Exception as exc:  # noqa: BLE001 - 网络失败必须显式分类
                last = EmbeddingUnavailable(
                    f"向量服务网络失败：{type(exc).__name__}",
                    reason_code="embedding_network_error",
                    details={"base_url_host": self.spec.host},
                )
                if attempt + 1 >= RETRY_ATTEMPTS:
                    raise last from exc
                time.sleep(RETRY_BACKOFF_SECONDS[attempt])
                continue
            if response.status_code < 400:
                return response
            error = self._error_for(response, response.status_code)
            retryable = response.status_code in RETRY_STATUSES or (
                error.details.get("provider_code") in RETRY_PROVIDER_CODES
            )
            if not retryable or attempt + 1 >= RETRY_ATTEMPTS:
                raise error
            last = error
            time.sleep(RETRY_BACKOFF_SECONDS[attempt])
        assert last is not None
        raise last

    def _error_for(self, response: Any, status: int) -> EmbeddingUnavailable:
        message = _safe_message(response)
        lowered = message.lower()
        base = {
            "http_status": status,
            "base_url_host": self.spec.host,
            "provider_code": _provider_code(response),
        }
        if status in (401, 403):
            return EmbeddingUnavailable(
                "向量服务鉴权失败，请检查 EMBEDDING_API_KEY",
                reason_code="embedding_auth_failed",
                details=base,
            )
        if status == 429:
            return EmbeddingUnavailable(
                "向量服务限流，请降低并发或稍后重试",
                reason_code="embedding_rate_limited",
                details=base,
            )
        if status == 400:
            # 提供方拒绝超长输入时给出可操作的原因，绝不改用截断绕过
            too_long = any(
                marker in lowered
                for marker in ("too long", "length", "token", "maximum context", "invalid")
            )
            return EmbeddingUnavailable(
                "向量服务拒绝该输入（可能是超出长度上限）；请调整分段而不是截断",
                reason_code="embedding_input_rejected" if too_long else "embedding_bad_request",
                details={**base, "message": message[:160]},
            )
        if any(marker in lowered for marker in _AUTH_MARKERS):
            return EmbeddingUnavailable(
                "向量服务鉴权失败", reason_code="embedding_auth_failed", details=base
            )
        if any(marker in lowered for marker in _RATE_MARKERS):
            return EmbeddingUnavailable(
                "向量服务限流", reason_code="embedding_rate_limited", details=base
            )
        return EmbeddingUnavailable(
            f"向量服务失败：HTTP {status}",
            reason_code="embedding_service_error",
            details={**base, "message": message[:160]},
        )


def _safe_message(response: Any) -> str:
    """只取提供方错误消息的前段用于诊断，不回传请求内容。"""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(body, dict):
        for key in ("message", "error", "msg"):
            value = body.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict) and isinstance(value.get("message"), str):
                return value["message"]
    return ""


def _provider_code(response: Any) -> Any:
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return None
    return body.get("code") if isinstance(body, dict) else None


def _extract_tokenizer(embeddings: Any) -> Any | None:
    client = getattr(embeddings, "_client", None) or getattr(embeddings, "client", None)
    if client is None:
        return None
    return getattr(client, "tokenizer", None)


def build_backend(
    spec: EmbeddingSpec,
    *,
    embeddings_factory: Any | None = None,
    token_counter: TokenCounter | None = None,
    allow_download: bool = False,
    client: Any | None = None,
) -> EmbeddingBackend | RemoteEmbeddingBackend:
    """按 ``spec.kind`` 选择后端；两种后端对外接口一致。"""
    if spec.kind is EmbeddingKind.OPENAI_COMPATIBLE:
        return RemoteEmbeddingBackend(spec, client=client, token_counter=token_counter)
    return EmbeddingBackend(
        spec,
        embeddings_factory=embeddings_factory,
        token_counter=token_counter,
        allow_download=allow_download,
    )


#: 便于类型标注与既有导入的别名
HuggingFaceEmbeddingBackend = EmbeddingBackend
