"""兜底回复目录：疑惑、失败与查询三类，全部经过同一输出规范预审。

要求：第一人称、句末无句号、自然文字动作，禁止把草稿直接流给 QQ。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from sana_bot.enums import PrimaryMood
from sana_bot.ports import DialogueDraft

__all__ = [
    "FallbackEntry",
    "FallbackCatalog",
    "DEFAULT_FALLBACK_CATALOG",
    "FALLBACK_KINDS",
]

FALLBACK_KINDS = ("confused", "failure", "blocked", "query", "unsupported")


@dataclass(frozen=True)
class FallbackEntry:
    """一条预审通过的兜底表达。"""

    kind: str
    mood: PrimaryMood
    text: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class FallbackCatalog:
    """固定目录；``text_for`` 同时给出心情与语境一致性。"""

    entries: tuple[FallbackEntry, ...] = ()
    version: str = "fallback-1"

    def text_for(self, kind: str, mood: PrimaryMood) -> str:
        exact = [entry for entry in self.entries if entry.kind == kind and entry.mood is mood]
        if exact:
            return exact[0].text
        any_mood = [entry for entry in self.entries if entry.kind == kind]
        if any_mood:
            return any_mood[0].text
        return "（稍稍偏过头）……我有点没明白？"

    def draft_for(self, kind: str, *, mood: PrimaryMood, reason_code: str) -> DialogueDraft:
        return DialogueDraft(
            kind="fallback" if kind != "query" else "status",
            text=self.text_for(kind, mood),
            reason_code=reason_code,
            details={"fallback_kind": kind, "catalog_version": self.version},
        )

    def entries_for(self, kind: str) -> tuple[FallbackEntry, ...]:
        return tuple(entry for entry in self.entries if entry.kind == kind)

    def to_payload(self) -> dict[str, object]:
        return {
            "version": self.version,
            "entries": [
                {"kind": entry.kind, "mood": entry.mood.value, "text": entry.text, "tags": list(entry.tags)}
                for entry in self.entries
            ],
        }


DEFAULT_FALLBACK_CATALOG = FallbackCatalog(
    entries=(
        FallbackEntry(
            kind="confused",
            mood=PrimaryMood.NEUTRAL,
            text="（稍稍偏过头）……我有点没明白？",
            tags=("未理解", "请求补充"),
        ),
        FallbackEntry(
            kind="confused",
            mood=PrimaryMood.HAPPY,
            text="（歪着头）诶？你在说什么呀，我没跟上",
            tags=("轻快", "未理解"),
        ),
        FallbackEntry(
            kind="confused",
            mood=PrimaryMood.SAD,
            text="（垂下眼）……我没听懂",
            tags=("低落", "简短"),
        ),
        FallbackEntry(
            kind="confused",
            mood=PrimaryMood.ANGRY,
            text="（皱起眉）你到底在说什么，说清楚一点",
            tags=("不耐", "未理解"),
        ),
        FallbackEntry(
            kind="failure",
            mood=PrimaryMood.NEUTRAL,
            text="（低头摆弄手指）……刚才没出口，你再说一次？",
            tags=("失败提示",),
        ),
        # 输出审核未通过时使用：不暗示「没听懂」，与低匹配兜底区分开
        FallbackEntry(
            kind="blocked",
            mood=PrimaryMood.NEUTRAL,
            text="（我顿了顿）……刚才那句不算，我重新说",
            tags=("输出未通过", "重来"),
        ),
        FallbackEntry(
            kind="query",
            mood=PrimaryMood.NEUTRAL,
            text="（抱臂扭过头）……这种事自己看就好了",
            tags=("只读查询",),
        ),
        FallbackEntry(
            kind="unsupported",
            mood=PrimaryMood.NEUTRAL,
            text="（皱了皱眉）这个我看不了，发文字给我",
            tags=("不支持输入",),
        ),
    ),
)


def validate_catalog(catalog: FallbackCatalog, checker: object) -> Mapping[str, Sequence[str]]:
    """用同一输出规范预审整份目录；返回每条文本的命中项。"""
    from sana_bot.dialogue.output_policy import OutputConstraints, check_text

    constraints = getattr(checker, "constraints", None)
    if not isinstance(constraints, OutputConstraints):
        constraints = OutputConstraints()
    report: dict[str, list[str]] = {}
    for entry in catalog.entries:
        violations = check_text(entry.text, constraints)
        if violations:
            report[entry.text] = [violation.code for violation in violations]
    return report


@dataclass
class FallbackUsage:
    """记录兜底使用情况，便于维护者核对。"""

    kind: str
    mood: PrimaryMood
    text: str
    reason_code: str
    details: Mapping[str, object] = field(default_factory=dict)
