from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class LevelPolicy:
    """Semantic contract for the representation stored at one memory level."""

    level: int
    name: str
    purpose: str
    must_keep: tuple[str, ...]
    may_compress: tuple[str, ...]
    may_discard: tuple[str, ...]
    must_not_infer: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.level, bool) or not isinstance(self.level, int) or self.level < 1:
            raise ValueError("memory level must be positive")
        for field_name in (
            "name",
            "purpose",
            "must_keep",
            "may_compress",
            "may_discard",
            "must_not_infer",
        ):
            value = getattr(self, field_name)
            if isinstance(value, str):
                if not value.strip():
                    raise ValueError(f"{field_name} must be non-empty")
            elif not value:
                raise ValueError(f"{field_name} must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# The first implementation is deliberately conservative.  Higher levels
# compress event narration and repetition, while the existing extraction and
# fusion rules continue to protect explicit facts and established context.
LEVEL_POLICIES: dict[int, LevelPolicy] = {
    1: LevelPolicy(
        level=1,
        name="local_topic_memory",
        purpose="Represent the complete storable content of one purified local topic group.",
        must_keep=(
            "every explicit user fact, including one-time, temporary, and completed events",
            "established topic context: active state, decisions, constraints, unresolved issues, and next steps",
            "topic identity, concrete values, dates, negation, conditions, and meaningful state changes",
        ),
        may_compress=(
            "repeated wording and strongly overlapping context items",
            "conversation narration, while keeping its supported semantic information",
        ),
        may_discard=(
            "pure social content without new information",
            "unsupported implications and assistant-only claims",
        ),
        must_not_infer=(
            "long-term preferences, traits, or knowledge from a single event",
            "facts not supported by the input evidence",
        ),
    ),
    2: LevelPolicy(
        level=2,
        name="stable_topic_memory",
        purpose="Represent a stable, reusable topic state for long-running memory operation.",
        must_keep=(
            "stable topic identity and the aspect that makes the topic coherent",
            "durable explicit user information and important user state changes",
            "decisions, constraints, unresolved issues, active state, and next steps needed for continuity",
            "concrete values, dates, conditions, negation, and meaningful uncertainty",
        ),
        may_compress=(
            "local event narration and dialogue order",
            "repeated examples, repeated wording, and obsolete intermediate wording",
            "details that do not change the stable topic state or future continuity",
        ),
        may_discard=(
            "pure social content without new information",
            "redundant details already represented by the final stable state",
            "resolved intermediate narration when no valid fact or context remains",
        ),
        must_not_infer=(
            "long-term preferences, traits, or abilities from one event",
            "a durable fact from an implication, assistant statement, or unsupported generalization",
        ),
    ),
    3: LevelPolicy(
        level=3,
        name="long_term_topic_memory",
        purpose="Represent only the most stable topic-level knowledge needed across long time spans.",
        must_keep=(
            "stable topic identity",
            "durable user facts, lasting decisions, constraints, and unresolved long-term state",
            "specific qualifiers that change the meaning of retained information",
        ),
        may_compress=(
            "local event details, examples, chronology, and repeated explanations",
            "short-lived intermediate states after the durable outcome is established",
        ),
        may_discard=(
            "pure social content without new information",
            "one-off narration that does not alter durable topic or user state",
            "redundant details fully represented by a more stable final statement",
        ),
        must_not_infer=(
            "personality, preference, ability, or permanence from isolated evidence",
            "durability merely because information has been mentioned repeatedly",
        ),
    ),
}


def get_level_policy(level: int) -> LevelPolicy:
    if isinstance(level, bool) or not isinstance(level, int):
        raise ValueError(f"unsupported memory level: {level!r}")
    try:
        return LEVEL_POLICIES[level]
    except KeyError as exc:
        raise ValueError(f"unsupported memory level: {level!r}") from exc
