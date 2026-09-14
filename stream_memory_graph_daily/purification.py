from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .llm import JsonLLM, LLMUnavailable
from .models import MemoryRecord, SegmentRecord
from .prompts import COMMUNITY_TOPIC_PARTITION_PROMPT


AuditSink = Callable[[str, str, dict[str, Any]], None]


@dataclass(frozen=True)
class PurifiedGroup:
    """A validated partition of one planned community's memory/segment nodes."""

    group_id: str
    node_ids: tuple[str, ...]
    memory_ids: tuple[str, ...] = ()
    segment_ids: tuple[str, ...] = ()


class CommunityPurifier:
    """Conservatively split a planned community before memory application."""

    def __init__(self, llm: JsonLLM | None, audit_sink: AuditSink | None = None) -> None:
        self.llm = llm
        self.audit_sink = audit_sink

    def _audit(self, action: str, values: dict[str, Any]) -> None:
        if self.audit_sink is not None:
            self.audit_sink("community_purification", action, values)

    @staticmethod
    def _segment_row(segment: SegmentRecord) -> dict[str, str]:
        return {
            "segment_id": segment.segment_id,
            "anchor": segment.anchor,
            "text": segment.text,
        }

    def purify(
        self,
        community_id: str,
        segments: list[SegmentRecord],
        memories: list[MemoryRecord] | None = None,
        *,
        allow_memory_only: bool = False,
        allow_multiple_memories: bool = False,
    ) -> list[PurifiedGroup]:
        memories = list(memories or [])
        segment_ids = [segment.segment_id for segment in segments]
        memory_ids = [memory.memory_id for memory in memories]
        input_ids = [*memory_ids, *segment_ids]
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("community purification input contains duplicate node_id")
        if not segment_ids and not allow_memory_only:
            raise ValueError("community purification requires at least one segment")

        # A genuinely new-topic singleton needs no partitioning. A singleton
        # *segment attached to an existing memory* is different: the memory
        # and segment form a two-node community and must be checked together.
        if not memory_ids and len(segment_ids) == 1:
            self._audit(
                "skipped_singleton",
                {
                    "community_id": community_id,
                    "input_memory_ids": [],
                    "input_segment_ids": segment_ids,
                    "reason": "singleton_community_does_not_need_purification",
                    "output_groups": [
                        {
                            "group_id": "g1",
                            "node_ids": segment_ids,
                            "memory_ids": [],
                            "segment_ids": segment_ids,
                        }
                    ],
                },
            )
            return [PurifiedGroup("g1", tuple(segment_ids), (), tuple(segment_ids))]

        request = {
            "community_id": community_id,
            "memory_nodes": [
                {
                    "node_id": memory.memory_id,
                    "kind": "memory",
                    "topic": memory.topic,
                    "summary": memory.summary,
                    "topic_context": memory.topic_context,
                    "user_memories": memory.user_memories,
                    "member_representations": memory.source_anchors,
                }
                for memory in memories
            ],
            "segment_nodes": [
                {
                    "node_id": segment.segment_id,
                    "kind": "segment",
                    **self._segment_row(segment),
                }
                for segment in segments
            ],
        }
        if self.llm is None:
            error = LLMUnavailable("community purification requires an LLM")
            self._audit(
                "llm_call",
                {
                    "request": request,
                    "system_prompt": COMMUNITY_TOPIC_PARTITION_PROMPT,
                    "response": None,
                    "error_type": type(error).__name__,
                    "error": str(error),
                },
            )
            raise error

        response: dict[str, Any] | None = None
        try:
            response = self.llm.complete(
                COMMUNITY_TOPIC_PARTITION_PROMPT,
                "INPUT DATA\n" + json.dumps(request, ensure_ascii=False, indent=2),
            )
            if set(response) != {"groups"} or not isinstance(response["groups"], list):
                raise ValueError("community purification output must contain exactly a groups list")
            if not response["groups"]:
                raise ValueError("community purification groups list must be non-empty")

            expected = set(input_ids)
            seen: list[str] = []
            normalized: list[PurifiedGroup] = []
            group_ids: set[str] = set()
            for index, row in enumerate(response["groups"], start=1):
                if not isinstance(row, dict):
                    raise ValueError(f"purification group {index} must be an object")
                if set(row) != {"group_id", "node_ids"}:
                    raise ValueError(
                        f"purification group {index} fields must be exactly "
                        "['group_id', 'node_ids']"
                    )
                group_id = str(row["group_id"]).strip()
                node_ids = row["node_ids"]
                if not group_id:
                    raise ValueError(f"purification group {index} has an empty group_id")
                if group_id in group_ids:
                    raise ValueError(f"purification output duplicates group_id: {group_id}")
                group_ids.add(group_id)
                if (
                    not isinstance(node_ids, list)
                    or not node_ids
                    or any(not isinstance(node_id, str) for node_id in node_ids)
                ):
                    raise ValueError(
                        f"purification group {index} must have a non-empty node_ids list"
                    )
                clean_ids = [node_id.strip() for node_id in node_ids]
                if any(not node_id for node_id in clean_ids):
                    raise ValueError(f"purification group {index} contains an empty node_id")
                seen.extend(clean_ids)
                group_memory_ids = tuple(node_id for node_id in clean_ids if node_id in memory_ids)
                group_segment_ids = tuple(node_id for node_id in clean_ids if node_id in segment_ids)
                if len(group_memory_ids) > 1 and not allow_multiple_memories:
                    raise ValueError(
                        "a purified group cannot contain multiple existing memory nodes"
                    )
                normalized.append(
                    PurifiedGroup(
                        group_id,
                        tuple(clean_ids),
                        group_memory_ids,
                        group_segment_ids,
                    )
                )

            if len(seen) != len(set(seen)):
                raise ValueError("purification output repeats a node_id")
            if set(seen) != expected:
                missing = sorted(expected - set(seen))
                extra = sorted(set(seen) - expected)
                raise ValueError(
                    "purification output must cover exactly the input node IDs; "
                    f"missing={missing}, extra={extra}"
                )
        except Exception as exc:
            self._audit(
                "llm_call",
                {
                    "request": request,
                    "system_prompt": COMMUNITY_TOPIC_PARTITION_PROMPT,
                    "response": response,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            raise

        self._audit(
            "llm_call",
            {
                "request": request,
                "system_prompt": COMMUNITY_TOPIC_PARTITION_PROMPT,
                "response": response,
            },
        )
        self._audit(
            "normalized_result",
            {
                "community_id": community_id,
                "input_memory_ids": memory_ids,
                "input_segment_ids": segment_ids,
                "output_groups": [
                    {
                        "group_id": group.group_id,
                        "node_ids": list(group.node_ids),
                        "memory_ids": list(group.memory_ids),
                        "segment_ids": list(group.segment_ids),
                    }
                    for group in normalized
                ],
            },
        )
        return normalized
