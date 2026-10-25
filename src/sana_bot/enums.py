"""跨层共享的枚举。

放在单独模块是为了让 ``storage``、``state``、``ingest`` 等层不必互相导入模型
（data-model.md 的取值即为这里的字面量，不得随意扩展）。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "Scene",
    "PrimaryMood",
    "RelationshipStatus",
    "ConversationStatus",
    "InboundStatus",
    "OutboundKind",
    "DeliveryStatus",
    "MonologueStatus",
    "OpportunityStatus",
    "RetrievalStage",
    "MatchDecision",
    "DiagnosticCategory",
    "IndexStatus",
    "ContentType",
    "FieldType",
    "Provenance",
    "SpanKind",
    "ReviewStatus",
]


class Scene(StrEnum):
    """场景；unknown 与 public 采用同样的保守表达。"""

    UNKNOWN = "unknown"
    PUBLIC = "public"
    PRIVATE = "private"


class PrimaryMood(StrEnum):
    """主情绪；开心与高兴统一为 happy。"""

    NEUTRAL = "neutral"
    HAPPY = "happy"
    ANGRY = "angry"
    SAD = "sad"


class RelationshipStatus(StrEnum):
    NON_ROMANTIC = "non_romantic"
    ROMANTIC = "romantic"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class InboundStatus(StrEnum):
    RECEIVED = "received"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    IGNORED = "ignored"


class OutboundKind(StrEnum):
    NORMAL = "normal"
    FALLBACK = "fallback"
    ERROR = "error"
    MONOLOGUE = "monologue"
    STATUS = "status"


class DeliveryStatus(StrEnum):
    PREPARED = "prepared"
    SENDING = "sending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


class MonologueStatus(StrEnum):
    ENABLED = "enabled"
    DISABLED_NO_MATERIAL = "disabled_no_material"
    DISABLED_UNCALIBRATED = "disabled_uncalibrated"


class OpportunityStatus(StrEnum):
    PENDING = "pending"
    EVALUATING = "evaluating"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class RetrievalStage(StrEnum):
    REPLY = "reply"
    MONOLOGUE = "monologue"


class MatchDecision(StrEnum):
    MATCHED = "matched"
    BELOW_THRESHOLD = "below_threshold"
    EMPTY_CANDIDATES = "empty_candidates"
    NO_MATERIAL = "no_material"
    PROCESSING_ERROR = "processing_error"


class DiagnosticCategory(StrEnum):
    LOW_MATCH = "low_match"
    INDEX_FAILURE = "index_failure"
    MATCH_ERROR = "match_error"
    STATE_TRANSITION = "state_transition"
    OUTPUT_BLOCKED = "output_blocked"
    DELIVERY_ERROR = "delivery_error"
    LOGGING_ERROR = "logging_error"


class IndexStatus(StrEnum):
    STAGED = "staged"
    REVIEWED = "reviewed"
    ACTIVE = "active"
    FAILED = "failed"


class ContentType(StrEnum):
    PROFILE = "profile"
    GENERAL_RULE = "general_rule"
    STATE = "state"
    ACTION = "action"
    EXAMPLE = "example"
    NEGATIVE = "negative"
    OUTPUT_RULE = "output_rule"
    DIALOGUE = "dialogue"
    MONOLOGUE = "monologue"


class FieldType(StrEnum):
    """冒号字段白名单；无匹配时为 None。"""

    TRIGGER = "trigger"
    OVERALL = "overall"
    ACTION = "action"
    BEHAVIOR = "behavior"
    SPEECH = "speech"
    MAINTENANCE = "maintenance"
    RECOVERY = "recovery"
    ESCALATION = "escalation"
    ATTRIBUTE = "attribute"
    EXAMPLE = "example"
    NEGATIVE = "negative"
    #: 不属于任何具名字段的原文段落（例如共同恢复机制说明）
    STATEMENT = "statement"


class Provenance(StrEnum):
    SOURCE = "source"
    DERIVED = "derived"
    #: 由祖先章节正文或标题推导出的限制，与原文显式写出区分开
    INHERITED = "inherited"


class SpanKind(StrEnum):
    DIALOGUE = "dialogue"
    MONOLOGUE = "monologue"
    STATEMENT = "statement"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
