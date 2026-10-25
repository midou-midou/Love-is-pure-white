"""离线测试替身：fake 时钟、随机源、模型与 QQ 传输。

这些替身让计时、概率、外部失败都可重现，且不依赖 API 密钥、网络或真实等待。
"""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sana_bot.diagnostics import DiagnosticOutcome, DiagnosticRecord
from sana_bot.enums import DeliveryStatus, DiagnosticCategory
from sana_bot.ports import (
    DeliveryResult,
    ModelBudget,
    ModelJsonResult,
    ModelRequest,
    ModelTextResult,
    NormalizedEvent,
)

__all__ = [
    "FakeClock",
    "InlineRandom",
    "SeededRandom",
    "FakeModel",
    "FakeQQTransport",
    "RecordingDiagnostics",
    "FailingDiagnostics",
    "make_event",
    "SentMessage",
]


class FakeClock:
    """单调与墙上时间都可推进的时钟。"""

    def __init__(self, *, monotonic: float = 0.0, wall: float | None = None) -> None:
        self.monotonic = monotonic
        self._wall = wall if wall is not None else datetime(2026, 10, 16, 12, 0, tzinfo=UTC).timestamp()

    def now(self) -> float:
        return self.monotonic

    def wall(self) -> float:
        return self._wall

    def dt(self) -> datetime:
        return datetime.fromtimestamp(self._wall, tz=UTC)

    def advance(self, seconds: float) -> float:
        self.monotonic += seconds
        self._wall += seconds
        return self.monotonic

    def sleep(self, seconds: float) -> None:
        """不真正等待，只推进时间。"""
        self.advance(seconds)


class InlineRandom:
    """按给定序列返回随机数；序列耗尽时可选择循环或报错。"""

    def __init__(self, values: Sequence[float], *, cycle: bool = False) -> None:
        if not values:
            raise ValueError("InlineRandom 至少需要一个取值")
        self._values = tuple(values)
        self._cycle = cycle
        self.calls = 0

    def random(self) -> float:
        if self.calls >= len(self._values):
            if not self._cycle:
                raise AssertionError("随机序列已耗尽：不应再抽取")
            index = self.calls % len(self._values)
        else:
            index = self.calls
        self.calls += 1
        return self._values[index]


class SeededRandom:
    """可重现的 seeded 随机源，用于概率统计。"""

    def __init__(self, seed: int = 20261016) -> None:
        self._random = random.Random(seed)

    def random(self) -> float:
        return self._random.random()


@dataclass
class _ModelScript:
    text: str = ""
    payload: Mapping[str, Any] | None = None
    fail_reason: str | None = None


class FakeModel:
    """记录调用并按脚本返回的模型端口。"""

    def __init__(
        self,
        *,
        text_responses: Iterable[str] = (),
        json_responses: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        self.text_scripts: deque[_ModelScript] = deque(_ModelScript(text=item) for item in text_responses)
        self.json_scripts: deque[_ModelScript] = deque(
            _ModelScript(payload=item) for item in json_responses
        )
        self.requests: list[ModelRequest] = []
        self.default_text = "（歪头）嗯？"
        self.default_json: Mapping[str, Any] = {"events": []}
        self.model_name = "fake-model"
        self.fail_text: str | None = None
        self.fail_json: str | None = None

    def queue_text(self, *items: str) -> None:
        self.text_scripts.extend(_ModelScript(text=item) for item in items)

    def queue_json(self, *items: Mapping[str, Any]) -> None:
        self.json_scripts.extend(_ModelScript(payload=item) for item in items)

    def queue_failure(self, *, text: bool = False, json: bool = False, reason: str = "model_failure") -> None:
        if text:
            self.fail_text = reason
        if json:
            self.fail_json = reason

    async def complete_text(self, request: ModelRequest, budget: ModelBudget) -> ModelTextResult:
        self.requests.append(request)
        if self.fail_text:
            return ModelTextResult(
                ok=False,
                reason_code=self.fail_text,
                correlation_id=request.correlation_id,
                model=self.model_name,
            )
        script = self.text_scripts.popleft() if self.text_scripts else _ModelScript(text=self.default_text)
        if script.fail_reason:
            return ModelTextResult(
                ok=False,
                reason_code=script.fail_reason,
                correlation_id=request.correlation_id,
                model=self.model_name,
            )
        return ModelTextResult(
            ok=True,
            text=script.text,
            model=self.model_name,
            correlation_id=request.correlation_id,
        )

    async def complete_json(self, request: ModelRequest, budget: ModelBudget) -> ModelJsonResult:
        self.requests.append(request)
        if self.fail_json:
            return ModelJsonResult(
                ok=False,
                reason_code=self.fail_json,
                correlation_id=request.correlation_id,
                model=self.model_name,
            )
        if self.json_scripts:
            script = self.json_scripts.popleft()
        elif request.json_schema_name == "output_review":
            script = _ModelScript(payload={"approved": True, "violations": []})
        else:
            script = _ModelScript(payload=dict(self.default_json))
        if script.fail_reason:
            return ModelJsonResult(
                ok=False,
                reason_code=script.fail_reason,
                correlation_id=request.correlation_id,
                model=self.model_name,
            )
        return ModelJsonResult(
            ok=True,
            payload=dict(script.payload or {}),
            model=self.model_name,
            correlation_id=request.correlation_id,
        )


@dataclass(frozen=True)
class SentMessage:
    content: str
    source_msg_id: str
    msg_seq: int
    user_key: str
    correlation_id: str | None
    platform_msg_id: str | None = None


class FakeQQTransport:
    """记录发送并按脚本返回结果的 QQ 传输替身。"""

    def __init__(
        self,
        *,
        results: Iterable[DeliveryStatus] = (),
        clock: FakeClock | None = None,
        exc: BaseException | None = None,
    ) -> None:
        self.sent: list[SentMessage] = []
        self._results = deque(results)
        self.clock = clock
        self.exc = exc

    def queue(self, *statuses: DeliveryStatus) -> None:
        self._results.extend(statuses)

    async def send(
        self,
        *,
        content: str,
        source_msg_id: str,
        msg_seq: int,
        user_key: str,
        platform_msg_id: str | None = None,
        correlation_id: str | None = None,
    ) -> DeliveryResult:
        self.sent.append(
            SentMessage(
                content=content,
                source_msg_id=source_msg_id,
                msg_seq=msg_seq,
                user_key=user_key,
                correlation_id=correlation_id,
                platform_msg_id=platform_msg_id,
            )
        )
        if self.exc is not None:
            raise self.exc
        status = self._results.popleft() if self._results else DeliveryStatus.CONFIRMED
        submit_at = self.clock.now() if self.clock else None
        return DeliveryResult(
            status=status,
            platform_message_id="msg-" + str(len(self.sent)) if status is DeliveryStatus.CONFIRMED else None,
            msg_seq=msg_seq,
            submit_at=submit_at,
            correlation_id=correlation_id,
        )


class RecordingDiagnostics:
    """内存诊断记录器；用于断言分类与载荷。"""

    def __init__(self, *, secrets: Sequence[str | None] = ()) -> None:
        self.records: list[DiagnosticRecord] = []
        self.secrets = tuple(secrets)

    def record(self, record: DiagnosticRecord) -> DiagnosticOutcome:
        self.records.append(record)
        return DiagnosticOutcome(recorded=True, correlation_id=record.correlation_id)

    def categories(self) -> list[DiagnosticCategory]:
        return [record.category for record in self.records]

    def payloads(self, category: DiagnosticCategory) -> list[Mapping[str, Any]]:
        return [record.payload for record in self.records if record.category is category]


class FailingDiagnostics:
    """总是抛错的诊断记录器；用于验证备用信号与不伪报成功。"""

    def __init__(self, message: str = "disk full") -> None:
        self.message = message
        self.attempts = 0

    def record(self, record: DiagnosticRecord) -> DiagnosticOutcome:
        self.attempts += 1
        raise RuntimeError(self.message)


def make_event(
    *,
    message_id: str = "m-1",
    user_key: str = "openid-1",
    app_id: str = "app-1",
    text: str = "你好",
    event_time: datetime | None = None,
    received_at: datetime | None = None,
    event_key: str | None = None,
    platform_event_id: str | None = None,
    supported: bool = True,
) -> NormalizedEvent:
    """构造归一化事件；默认时间固定便于断言聊天日。"""
    base = event_time or datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    return NormalizedEvent(
        event_key=event_key or platform_event_id or message_id,
        app_id=app_id,
        user_key=user_key,
        message_id=message_id,
        event_time=base,
        received_at=received_at or base + timedelta(milliseconds=50),
        text=text,
        payload_hash=f"hash-{message_id}-{text}",
        platform_event_id=platform_event_id,
        supported=supported,
    )


class CharTokenizer:
    """字符级 tokenizer 替身：每个字符一个 token，便于精确断言上限。"""

    def __call__(self, text: str, add_special_tokens: bool = True) -> dict[str, list[int]]:
        return {"input_ids": [1] * (len(text) + (1 if add_special_tokens else 0))}

    def count(self, text: str) -> int:
        return len(text)


class FakeEmbeddingsProvider:
    """向量后端替身：按关键词分组给出方向稳定的单位向量。"""

    def __init__(self, *, dimension: int = 512, tokenizer: object | None = None) -> None:
        self.dimension = dimension
        self.tokenizer = tokenizer or CharTokenizer()
        self.calls: list[list[str]] = []
        self.failure: BaseException | None = None
        self.bad_dimension = False
        self.non_finite = False

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.failure is not None:
            raise self.failure
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        groups = {
            "社团": 0,
            "猫咪": 0,
            "情绪": 1,
            "吃醋": 1,
            "点心": 2,
            "料理": 2,
        }
        vector = [0.0] * self.dimension
        for needle, axis in groups.items():
            if needle in text:
                vector[axis] += 1.0
        if not any(vector):
            vector[3] = 1.0
        length = sum(value * value for value in vector) ** 0.5
        vector = [value / length for value in vector]
        if self.bad_dimension:
            return vector[:-1]
        if self.non_finite:
            vector[0] = float("nan")
        return vector


def topic_vectors(dimension: int = 4) -> "object":
    """语义切分测试用的向量函数：不同关键词落在不同轴上。"""

    groups = {"社团": 0, "猫咪": 0, "情绪": 1, "吃醋": 1, "点心": 2, "料理": 2}

    def embed(texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * dimension
            for needle, axis in groups.items():
                if needle in text:
                    vector[axis] += 1.0
            if not any(vector):
                vector[3] = 1.0
            length = sum(value * value for value in vector) ** 0.5
            vectors.append([value / length for value in vector])
        return vectors

    return embed
