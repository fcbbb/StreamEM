from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from .llm import JsonLLM, LLMUnavailable
from .models import MemoryRecord, SegmentRecord
from .prompts import MEMORY_EXTRACTION_PROMPT, MEMORY_FUSION_PROMPT


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

    def extract(
        self, community_id: str, segments: list[SegmentRecord]
    ) -> MemoryRecord | None:
        value = self._complete(
            "memory_extraction",
            MEMORY_EXTRACTION_PROMPT,
            {
                "community_id": community_id,
                "segments": [segment_prompt_row(segment) for segment in segments],
            },
        )
        if value == {}:
            return None
        required = {"topic", "summary", "topic_context", "user_memories"}
        if set(value) != required:
            raise ValueError(f"memory extraction fields must be exactly {sorted(required)}")
        now = utc_now()
        memory_id = stable_memory_id([segment.segment_id for segment in segments])
        return MemoryRecord(
            memory_id=memory_id,
            topic=value["topic"],
            summary=value["summary"],
            topic_context=_initial_items(
                memory_id, "topic_context", value["topic_context"]
            ),
            user_memories=_initial_items(
                memory_id, "user_memories", value["user_memories"]
            ),
            source_anchors=list(dict.fromkeys(segment.anchor for segment in segments)),
            source_segments=[segment.segment_id for segment in segments],
            created_at=now,
            updated_at=now,
        )

    def fuse(
        self,
        community_id: str,
        existing: MemoryRecord,
        segments: list[SegmentRecord],
    ) -> tuple[MemoryRecord, dict[str, Any]]:
        value = self._complete(
            "memory_fusion",
            MEMORY_FUSION_PROMPT,
            {
                "existing_memory": existing.prompt_dict(),
                "new_group": {
                    "community_id": community_id,
                    "segments": [segment_prompt_row(segment) for segment in segments],
                },
            },
        )
        required = {"topic", "summary", "operations"}
        if set(value) != required:
            raise ValueError(f"memory fusion fields must be exactly {sorted(required)}")
        topic = str(value["topic"]).strip()
        summary = str(value["summary"]).strip()
        operations = value["operations"]
        if not topic or not summary:
            raise ValueError("fusion topic and summary must be non-empty strings")
        if not isinstance(operations, list):
            raise ValueError("fusion operations must be a list")

        fields = {
            "topic_context": [dict(item) for item in existing.topic_context],
            "user_memories": [dict(item) for item in existing.user_memories],
        }
        new_segment_ids = {segment.segment_id for segment in segments}
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
        updated = MemoryRecord(
            memory_id=existing.memory_id,
            topic=topic,
            summary=summary,
            topic_context=fields["topic_context"],
            user_memories=fields["user_memories"],
            source_anchors=list(
                dict.fromkeys([*existing.source_anchors, *(segment.anchor for segment in segments)])
            ),
            source_segments=list(
                dict.fromkeys([*existing.source_segments, *(segment.segment_id for segment in segments)])
            ),
            created_at=existing.created_at,
            updated_at=utc_now(),
            version=existing.version + (1 if semantic_change else 0),
        )
        return updated, {
            "decision": "update_existing" if semantic_change else "no_material_change",
            "topic": topic,
            "summary": summary,
            "operations": normalized_operations,
            "topic_source_segment_ids": (
                sorted(new_segment_ids) if topic != existing.topic else []
            ),
            "summary_source_segment_ids": (
                sorted(new_segment_ids) if summary != existing.summary else []
            ),
        }
