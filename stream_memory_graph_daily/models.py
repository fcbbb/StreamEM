from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


SegmentStatus = Literal["active", "boundary", "compressed", "no_memory"]


@dataclass
class SegmentRecord:
    segment_id: str
    text: str
    anchor: str
    event_date: str
    conversation_id: str = ""
    segment_index: int = 0
    start_unit_id: str | None = None
    end_unit_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    status: SegmentStatus = "active"
    memory_id: str | None = None

    def __post_init__(self) -> None:
        self.segment_id = str(self.segment_id).strip()
        self.text = str(self.text).strip()
        self.anchor = str(self.anchor).strip()
        self.event_date = str(self.event_date).strip()
        if not self.segment_id:
            raise ValueError("segment_id must be non-empty")
        if not self.text:
            raise ValueError(f"segment {self.segment_id} has empty text")
        if not self.anchor:
            raise ValueError(f"segment {self.segment_id} has empty anchor")
        if not self.event_date:
            raise ValueError(f"segment {self.segment_id} has no event_date")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SegmentRecord":
        return cls(**value)


@dataclass
class MemoryRecord:
    memory_id: str
    topic: str
    summary: str
    topic_context: list[dict[str, str]] = field(default_factory=list)
    user_memories: list[dict[str, str]] = field(default_factory=list)
    source_anchors: list[str] = field(default_factory=list)
    source_segments: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    version: int = 1
    level: int = 1
    direct_members: list[dict[str, Any]] = field(default_factory=list)
    last_mentioned_at: str = ""

    def __post_init__(self) -> None:
        self.memory_id = str(self.memory_id).strip()
        self.topic = str(self.topic).strip()
        self.summary = str(self.summary).strip()
        if not self.memory_id or not self.topic or not self.summary:
            raise ValueError("memory_id, topic, and summary must be non-empty")
        self.topic_context = self._validate_items("topic_context", self.topic_context)
        self.user_memories = self._validate_items("user_memories", self.user_memories)
        self.source_anchors = self._dedupe_strings(self.source_anchors)
        self.source_segments = self._dedupe_strings(self.source_segments)
        if isinstance(self.level, bool) or not isinstance(self.level, int) or self.level < 1:
            raise ValueError("memory level must be a positive integer")
        self.direct_members = self._validate_direct_members(
            self.direct_members, self.level
        )
        self.created_at = str(self.created_at).strip()
        self.updated_at = str(self.updated_at).strip()
        self.last_mentioned_at = str(self.last_mentioned_at).strip()
        if not self.last_mentioned_at:
            # Older states do not have a topic-mention timestamp.  Their
            # content timestamp is the safest backward-compatible estimate.
            self.last_mentioned_at = self.updated_at or self.created_at
        if self.version < 1:
            raise ValueError("memory version must be positive")

    @staticmethod
    def _validate_items(name: str, values: Any) -> list[dict[str, str]]:
        if not isinstance(values, list):
            raise ValueError(f"{name} must be a list")
        output: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for item in values:
            if not isinstance(item, dict) or not {"type", "content"}.issubset(item):
                raise ValueError(f"each {name} item must contain type and content")
            if set(item) - {"item_id", "type", "content"}:
                raise ValueError(f"each {name} item may contain only item_id, type, and content")
            type_name = str(item["type"]).strip()
            content = str(item["content"]).strip()
            if not type_name or not content:
                raise ValueError(f"{name} items must have non-empty type and content")
            item_id = str(item.get("item_id", "")).strip()
            if not item_id:
                # States created before operation-based fusion had no item IDs.
                # Upgrade them deterministically so they are immediately valid
                # targets in the new fusion input contract.
                digest = hashlib.sha1(
                    f"{name}\x1f{type_name}\x1f{content}".encode("utf-8")
                ).hexdigest()[:16]
                item_id = f"item:{digest}"
            if item_id in seen_ids:
                raise ValueError(f"{name} contains duplicate item_id {item_id!r}")
            seen_ids.add(item_id)
            output.append({"item_id": item_id, "type": type_name, "content": content})
        return output

    @staticmethod
    def _dedupe_strings(values: Any) -> list[str]:
        if not isinstance(values, list):
            raise ValueError("source fields must be lists")
        return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))

    @staticmethod
    def _validate_direct_members(
        values: Any, memory_level: int
    ) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            raise ValueError("direct_members must be a list")
        expected_level = memory_level - 1
        output: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in values:
            if not isinstance(item, dict):
                raise ValueError("each direct member must be an object")
            if set(item) != {"node_id", "level", "kind", "representation"}:
                raise ValueError(
                    "each direct member must contain exactly node_id, level, kind, "
                    "and representation"
                )
            node_id = str(item["node_id"]).strip()
            representation = str(item["representation"]).strip()
            kind = str(item["kind"]).strip()
            member_level = item["level"]
            if not node_id or not representation:
                raise ValueError("direct member node_id and representation must be non-empty")
            if kind not in {"segment", "memory"}:
                raise ValueError("direct member kind must be segment or memory")
            if (
                isinstance(member_level, bool)
                or not isinstance(member_level, int)
                or member_level != expected_level
            ):
                raise ValueError(
                    f"direct member level must be exactly {expected_level}"
                )
            if node_id in seen_ids:
                raise ValueError(f"direct_members contains duplicate node_id {node_id!r}")
            seen_ids.add(node_id)
            output.append(
                {
                    "node_id": node_id,
                    "level": member_level,
                    "kind": kind,
                    "representation": representation,
                }
            )
        return output

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def prompt_dict(self) -> dict[str, Any]:
        value = self.to_dict()
        for key in (
            "created_at",
            "updated_at",
            "last_mentioned_at",
            "version",
            "level",
            "direct_members",
        ):
            value.pop(key, None)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryRecord":
        return cls(**value)


@dataclass
class BoundaryRecord:
    segment_id: str
    candidate_memories: dict[str, float]
    reason: str
    first_seen_date: str
    last_checked_date: str
    attempts: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BoundaryRecord":
        return cls(**value)
