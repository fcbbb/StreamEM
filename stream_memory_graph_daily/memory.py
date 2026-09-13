from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from .level_policy import get_level_policy
from .llm import JsonLLM, LLMUnavailable
from .models import MemoryRecord, SegmentRecord
from .prompts import (
    MEMORY_EXTRACTION_PROMPT,
    MEMORY_FUSION_PROMPT,
    MEMORY_FUSION_FROM_L1_PROMPT,
    render_memory_level_policy,
)


AuditSink = Callable[[str, str, dict[str, Any]], None]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_memory_id(segment_ids: list[str]) -> str:
    payload = "\x1f".join(sorted(segment_ids)).encode("utf-8")
    return "memory:" + hashlib.sha1(payload).hexdigest()[:16]


def stable_item_id(
    memory_id: str,
    field_name: str,
    value: dict[str, Any],
) -> str:
    """Create an item identity without conflating it with evidence identity."""

    payload = "\x1f".join(
        [
            memory_id,
            field_name,
            str(value.get("type", "")).strip(),
            str(value.get("content", "")).strip(),
        ]
    ).encode("utf-8")
    return "item:" + hashlib.sha1(payload).hexdigest()[:16]


def segment_prompt_row(segment: SegmentRecord) -> dict[str, Any]:
    return {
        "segment_id": segment.segment_id,
        "event_date": segment.event_date,
        "anchor": segment.anchor,
        "text": segment.text,
    }


def direct_segment_member(segment: SegmentRecord) -> dict[str, Any]:
    return {
        "node_id": segment.segment_id,
        "level": 0,
        "kind": "segment",
        "representation": segment.anchor,
    }


def direct_memory_member(memory: MemoryRecord) -> dict[str, Any]:
    return {
        "node_id": memory.memory_id,
        "level": memory.level,
        "kind": "memory",
        "representation": memory.topic,
    }


def merge_direct_segment_members(
    existing: MemoryRecord, segments: list[SegmentRecord]
) -> list[dict[str, Any]]:
    """Keep one-hop member snapshots without expanding raw provenance."""

    output = [dict(member) for member in existing.direct_members]
    seen = {str(member["node_id"]) for member in output}
    for segment in segments:
        member = direct_segment_member(segment)
        if member["node_id"] not in seen:
            output.append(member)
            seen.add(member["node_id"])
    return output


def merge_direct_memory_members(
    existing: MemoryRecord, memory: MemoryRecord
) -> list[dict[str, Any]]:
    """Add one-hop memory provenance when a higher-level owner is fused."""

    output = [dict(member) for member in existing.direct_members]
    seen = {str(member["node_id"]) for member in output}
    member = direct_memory_member(memory)
    if member["node_id"] not in seen:
        output.append(member)
    return output


def _initial_items(
    memory_id: str, field_name: str, values: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Assign deterministic item IDs to initial extraction output."""

    output = []
    for index, value in enumerate(values, start=1):
        digest = hashlib.sha1(
            (
                f"{memory_id}\x1f{field_name}\x1f{index}\x1f"
                f"{value.get('type', '')}\x1f{value.get('content', '')}"
            ).encode("utf-8")
        ).hexdigest()[:16]
        output.append({"item_id": f"item:{digest}", **value})
    return output


class MemoryService:
    def __init__(self, llm: JsonLLM | None, audit_sink: AuditSink | None = None) -> None:
        self.llm = llm
        self.audit_sink = audit_sink

    def _complete(self, stage: str, system: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.llm is None:
            raise LLMUnavailable("memory extraction/fusion requires an LLM")
        value: dict[str, Any] | None = None
        try:
            value = self.llm.complete(
                system,
                "INPUT DATA\n" + json.dumps(payload, ensure_ascii=False, indent=2),
            )
        except Exception as exc:
            if self.audit_sink is not None:
                self.audit_sink(
                    stage,
                    "llm_call",
                    {
                        "request": payload,
                        "system_prompt": system,
                        "response": value,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
            raise
        if self.audit_sink is not None:
            self.audit_sink(
                stage,
                "llm_call",
                {"request": payload, "system_prompt": system, "response": value},
            )
        return value

    @staticmethod
    def _level_system_prompt(system: str, target_level: int) -> str:
        """Keep the established task prompt and append the shared level policy."""

        return system.rstrip() + "\n\n" + render_memory_level_policy(target_level)

    def extract(
        self,
        community_id: str,
        segments: list[SegmentRecord],
        *,
        target_level: int = 1,
    ) -> MemoryRecord | None:
        # This extraction entry point consumes raw L0 segments and therefore
        # currently creates L1. Higher-level memories are produced by fusing
        # lower-level memory representations in the owner path.
        if target_level != 1:
            raise ValueError(
                "segment extraction currently supports target_level=1 only"
            )
        policy = get_level_policy(target_level)
        value = self._complete(
            "memory_extraction",
            self._level_system_prompt(MEMORY_EXTRACTION_PROMPT, target_level),
            {
                "community_id": community_id,
                "target_level": target_level,
                "level_policy": policy.to_dict(),
                "segments": [segment_prompt_row(segment) for segment in segments],
            },
        )
        if value == {}:
            return None
        required = {"topic", "summary", "topic_context", "user_memories"}
        if set(value) != required:
            raise ValueError(f"memory extraction fields must be exactly {sorted(required)}")
        topic_context = value["topic_context"]
        user_memories = value["user_memories"]
        if not isinstance(topic_context, list) or not isinstance(user_memories, list):
            raise ValueError("memory extraction item fields must be lists")
        if not topic_context and not user_memories:
            return None
        now = utc_now()
        memory_id = stable_memory_id([segment.segment_id for segment in segments])
        return MemoryRecord(
            memory_id=memory_id,
            topic=value["topic"],
            summary=value["summary"],
            topic_context=_initial_items(
                memory_id, "topic_context", topic_context
            ),
            user_memories=_initial_items(
                memory_id, "user_memories", user_memories
            ),
            source_anchors=list(dict.fromkeys(segment.anchor for segment in segments)),
            source_segments=[segment.segment_id for segment in segments],
            created_at=now,
            updated_at=now,
            level=1,
            direct_members=[direct_segment_member(segment) for segment in segments],
            last_mentioned_at=now,
        )

    def fuse(
        self,
        community_id: str,
        existing: MemoryRecord,
        segments: list[SegmentRecord],
        *,
        provisional: MemoryRecord | None = None,
        target_level: int | None = None,
    ) -> tuple[MemoryRecord, dict[str, Any]]:
        if provisional is not None and segments:
            raise ValueError("fusion cannot receive both segments and provisional memory")
        if provisional is None and not segments:
            raise ValueError("fusion needs segments or a provisional memory")
        if target_level is None:
            target_level = existing.level
        policy = get_level_policy(target_level)
        if target_level != existing.level:
            raise ValueError(
                "fusion target_level must match the existing memory level"
            )
        new_source_ids = (
            set(provisional.source_segments)
            if provisional is not None
            else {segment.segment_id for segment in segments}
        )
        new_source_anchors = (
            list(provisional.source_anchors)
            if provisional is not None
            else [segment.anchor for segment in segments]
        )
        value = self._complete(
            "memory_fusion_from_l1" if provisional is not None else "memory_fusion",
            self._level_system_prompt(
                MEMORY_FUSION_FROM_L1_PROMPT
                if provisional is not None
                else MEMORY_FUSION_PROMPT,
                target_level,
            ),
            {
                "existing_memory": existing.prompt_dict(),
                "target_level": target_level,
                "level_policy": policy.to_dict(),
                "new_group": (
                    {
                        "community_id": community_id,
                        "provisional_l1": {
                            "topic": provisional.topic,
                            "summary": provisional.summary,
                            "topic_context": provisional.topic_context,
                            "user_memories": provisional.user_memories,
                        },
                        "source_segment_ids": sorted(new_source_ids),
                    }
                    if provisional is not None
                    else {
                        "community_id": community_id,
                        "segments": [segment_prompt_row(segment) for segment in segments],
                    }
                ),
            },
        )
        required = {"topic", "summary", "operations", "no_op_reason"}
        if set(value) != required:
            raise ValueError(f"memory fusion fields must be exactly {sorted(required)}")
        topic = str(value["topic"]).strip()
        summary = str(value["summary"]).strip()
        operations = value["operations"]
        no_op_reason = value["no_op_reason"]
        if not topic or not summary:
            raise ValueError("fusion topic and summary must be non-empty strings")
        if not isinstance(operations, list):
            raise ValueError("fusion operations must be a list")
        if operations:
            if no_op_reason is not None:
                raise ValueError("fusion no_op_reason must be null when operations are present")
        elif no_op_reason not in {"already_present", "no_storable_content"}:
            raise ValueError(
                "fusion no_op_reason must distinguish already_present from "
                "no_storable_content when operations are empty"
            )

        fields = {
            "topic_context": [dict(item) for item in existing.topic_context],
            "user_memories": [dict(item) for item in existing.user_memories],
        }
        new_segment_ids = new_source_ids
        touched_targets: set[tuple[str, str]] = set()
        normalized_operations: list[dict[str, Any]] = []
        for index, operation in enumerate(operations, start=1):
            if not isinstance(operation, dict):
                raise ValueError(f"fusion operation {index} must be an object")
            operation_name = operation.get("operation")
            field_name = operation.get("field")
            if operation_name not in {"add", "update", "delete"}:
                raise ValueError(f"fusion operation {index} has an invalid operation")
            if field_name not in fields:
                raise ValueError(f"fusion operation {index} has an invalid field")
            if operation_name == "add":
                expected_keys = {"operation", "field", "value", "source_segment_ids"}
            elif operation_name == "update":
                expected_keys = {
                    "operation", "field", "item_id", "value", "source_segment_ids"
                }
            else:
                expected_keys = {"operation", "field", "item_id", "source_segment_ids"}
            if set(operation) != expected_keys:
                raise ValueError(
                    f"fusion operation {index} fields must be exactly {sorted(expected_keys)}"
                )

            source_ids = operation["source_segment_ids"]
            if (
                not isinstance(source_ids, list)
                or not source_ids
                or any(not isinstance(source_id, str) or not source_id.strip() for source_id in source_ids)
            ):
                raise ValueError("source_segment_ids must be a non-empty string list")
            normalized_source_ids = list(dict.fromkeys(source_id.strip() for source_id in source_ids))
            if not set(normalized_source_ids).issubset(new_segment_ids):
                raise ValueError("operation source_segment_ids must come from new_group")

            items = fields[str(field_name)]
            positions = {
                str(item["item_id"]): position for position, item in enumerate(items)
            }
            if operation_name == "add":
                if not isinstance(operation["value"], dict):
                    raise ValueError("add operation value must be an object")
                if set(operation["value"]) != {"type", "content"}:
                    raise ValueError("add operation value must contain exactly type and content")
                target_id = stable_item_id(
                    existing.memory_id,
                    str(field_name),
                    operation["value"],
                )
                target = (str(field_name), target_id)
                if target in touched_targets or target_id in positions:
                    raise ValueError("add operation would duplicate a memory item")
                touched_targets.add(target)
                item = MemoryRecord._validate_items(
                    str(field_name), [{"item_id": target_id, **operation["value"]}]
                )[0]
                items.append(item)
                normalized_operation = {
                    "operation": "add",
                    "field": field_name,
                    "item_id": target_id,
                    "value": dict(operation["value"]),
                    "source_segment_ids": normalized_source_ids,
                }
            else:
                target_id = str(operation.get("item_id", "")).strip()
                target = (str(field_name), target_id)
                if not target_id or target in touched_targets:
                    raise ValueError(
                        "each update/delete must target one unique non-empty field/item_id"
                    )
                touched_targets.add(target)
                if target_id not in positions:
                    raise ValueError(
                        f"{operation_name} operation item_id must identify an existing item"
                    )
            if operation_name == "update":
                if not isinstance(operation["value"], dict):
                    raise ValueError("update operation value must be an object")
                if set(operation["value"]) != {"type", "content"}:
                    raise ValueError("update operation value must contain exactly type and content")
                item = MemoryRecord._validate_items(
                    str(field_name), [{"item_id": target_id, **operation["value"]}]
                )[0]
                items[positions[target_id]] = item
                normalized_operation = {
                    **operation,
                    "item_id": target_id,
                    "source_segment_ids": normalized_source_ids,
                }
            elif operation_name == "delete":
                items.pop(positions[target_id])
                normalized_operation = {
                    **operation,
                    "item_id": target_id,
                    "source_segment_ids": normalized_source_ids,
                }
            normalized_operations.append(normalized_operation)

        semantic_change = bool(normalized_operations) or topic != existing.topic or summary != existing.summary
        now = utc_now()
        updated = MemoryRecord(
            memory_id=existing.memory_id,
            topic=topic,
            summary=summary,
            topic_context=fields["topic_context"],
            user_memories=fields["user_memories"],
            source_anchors=list(
                dict.fromkeys([*existing.source_anchors, *new_source_anchors])
            ),
            source_segments=list(
                dict.fromkeys([*existing.source_segments, *new_source_ids])
            ),
            created_at=existing.created_at,
            updated_at=(now if semantic_change else existing.updated_at),
            version=existing.version + (1 if semantic_change else 0),
            level=existing.level,
            direct_members=(
                merge_direct_memory_members(existing, provisional)
                if provisional is not None
                else merge_direct_segment_members(existing, segments)
            ),
            last_mentioned_at=now,
        )
        return updated, {
            "decision": "update_existing" if semantic_change else "no_material_change",
            "topic": topic,
            "summary": summary,
            "operations": normalized_operations,
            "no_op_reason": no_op_reason,
            "topic_source_segment_ids": (
                sorted(new_segment_ids) if topic != existing.topic else []
            ),
            "summary_source_segment_ids": (
                sorted(new_segment_ids) if summary != existing.summary else []
            ),
        }

    def fuse_provisional(
        self,
        community_id: str,
        existing: MemoryRecord,
        provisional: MemoryRecord,
        *,
        target_level: int | None = None,
    ) -> tuple[MemoryRecord, dict[str, Any]]:
        """Fuse only the semantic contents of a provisional L1 memory."""

        return self.fuse(
            community_id,
            existing,
            [],
            provisional=provisional,
            target_level=target_level,
        )
