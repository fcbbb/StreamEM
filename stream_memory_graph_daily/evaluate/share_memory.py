from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from stream_memory_graph_daily.pipeline import DailyMemoryGraph


SUPERVISION_FIELDS = frozenset({"share_memory", "operation", "operation_details"})


def sanitize_conversation_for_inference(value: dict[str, Any]) -> dict[str, Any]:
    """Return the only conversation view that may enter the memory pipeline."""

    conversation_id = str(
        value.get("conversation_id", value.get("session_id", value.get("id", "conversation")))
    )
    messages = value.get("conversation", value.get("messages"))
    if not isinstance(messages, list):
        raise ValueError(f"conversation {conversation_id} has no message list")
    safe_messages = []
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            raise ValueError(f"conversation {conversation_id} message {index} is not an object")
        safe_messages.append(
            {
                "turn": int(message.get("turn", index)),
                "speaker": str(message.get("speaker", message.get("role", "unknown"))),
                "message": str(
                    message.get("message", message.get("content", message.get("text", "")))
                ),
            }
        )
    return {
        "conversation_id": conversation_id,
        "event_date": value.get("event_date", value.get("date")),
        "messages": safe_messages,
    }


def extract_share_memory_labels(
    value: dict[str, Any], *, source_file: str | Path | None = None
) -> list[dict[str, Any]]:
    """Extract evaluation-only labels without changing the inference view."""

    conversation_id = str(
        value.get("conversation_id", value.get("session_id", value.get("id", "conversation")))
    )
    messages = value.get("conversation", value.get("messages"))
    if not isinstance(messages, list):
        raise ValueError(f"conversation {conversation_id} has no message list")
    labels: list[dict[str, Any]] = []
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict) or message.get("share_memory") is not True:
            continue
        turn = int(message.get("turn", index))
        labels.append(
            {
                "label_id": f"{conversation_id}:m{turn:03d}",
                "conversation_id": conversation_id,
                "session_id": value.get("session_id"),
                "event_date": value.get("event_date", value.get("date")),
                "turn": turn,
                "message_id": f"m{turn:03d}",
                "speaker": str(message.get("speaker", message.get("role", "unknown"))),
                "text": str(
                    message.get("message", message.get("content", message.get("text", "")))
                ),
                "operation": deepcopy(value.get("operation")),
                "operation_details": deepcopy(value.get("operation_details")),
                "source_file": str(Path(source_file).resolve()) if source_file else None,
            }
        )
    return labels


def _cut_mappings(pipeline: DailyMemoryGraph) -> dict[str, list[dict[str, Any]]]:
    mappings: dict[str, list[dict[str, Any]]] = {}
    for event in pipeline.stage_audit:
        if (
            event.get("stage") != "cutting"
            or event.get("action") != "llm_call"
            or not isinstance(event.get("normalized_segments"), list)
        ):
            continue
        request = event.get("request", {})
        conversation_id = str(request.get("conversation_id", ""))
        unit_message_ids = {
            str(unit.get("unit_id")): str(unit.get("message_id"))
            for unit in request.get("units", [])
            if isinstance(unit, dict)
        }
        rows: list[dict[str, Any]] = []
        for segment in event["normalized_segments"]:
            row = dict(segment)
            message_unit_ids = row.get("message_unit_ids")
            if not isinstance(message_unit_ids, dict):
                message_unit_ids = {}
                for unit_id in row.get("unit_ids", []):
                    message_id = unit_message_ids.get(str(unit_id))
                    if message_id:
                        message_unit_ids.setdefault(message_id, []).append(str(unit_id))
            row["message_unit_ids"] = message_unit_ids
            rows.append(row)
        mappings[conversation_id] = rows
    return mappings


def _memory_apply_segment_ids(event: dict[str, Any]) -> set[str]:
    values: list[Any] = []
    payload = event.get("input")
    if isinstance(payload, dict):
        if isinstance(payload.get("segments"), list):
            values.extend(
                row.get("segment_id")
                for row in payload["segments"]
                if isinstance(row, dict)
            )
        if isinstance(payload.get("segment_ids"), list):
            values.extend(payload["segment_ids"])
    if isinstance(event.get("segment_ids"), list):
        values.extend(event["segment_ids"])
    return {str(value) for value in values if value is not None and str(value)}


def _forbidden_key_paths(value: Any, prefix: str = "request") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}"
            if str(key) in SUPERVISION_FIELDS:
                paths.append(path)
            paths.extend(_forbidden_key_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_forbidden_key_paths(item, f"{prefix}[{index}]"))
    return paths


def supervision_leakage_events(pipeline: DailyMemoryGraph) -> list[dict[str, Any]]:
    """Find evaluation-only keys in any persisted pipeline input/request."""

    leaks: list[dict[str, Any]] = []
    for event in pipeline.stage_audit:
        inspected: list[tuple[str, Any]] = []
        if event.get("action") == "llm_call" and "request" in event:
            inspected.append(("request", event["request"]))
        if event.get("stage") == "conversation_input" and "input" in event:
            inspected.append(("input", event["input"]))
        paths = [
            path
            for name, value in inspected
            for path in _forbidden_key_paths(value, name)
        ]
        if paths:
            leaks.append(
                {
                    "stage_id": event.get("stage_id"),
                    "stage": event.get("stage"),
                    "action": event.get("action"),
                    "paths": paths,
                }
            )
    return leaks


def build_share_memory_attributions(
    labels: Iterable[dict[str, Any]], pipeline: DailyMemoryGraph
) -> list[dict[str, Any]]:
    """Map hidden evaluation labels through the completed pipeline structure."""

    cut_by_conversation = _cut_mappings(pipeline)
    segment_rows = pipeline.segments
    skipped_rows = {
        str(row.get("segment_id")): row for row in pipeline.skipped_segments
    }
    apply_events: dict[str, list[dict[str, Any]]] = {}
    for event in pipeline.stage_audit:
        if event.get("stage") != "memory_apply":
            continue
        for segment_id in _memory_apply_segment_ids(event):
            apply_events.setdefault(segment_id, []).append(event)

    output: list[dict[str, Any]] = []
    for original in labels:
        label = deepcopy(dict(original))
        conversation_id = str(label.get("conversation_id", ""))
        message_id = str(label.get("message_id", ""))
        matching_segments = [
            row
            for row in cut_by_conversation.get(conversation_id, [])
            if message_id in row.get("message_unit_ids", {})
        ]
        segment_ids = [str(row["segment_id"]) for row in matching_segments]
        unit_ids = [
            str(unit_id)
            for row in matching_segments
            for unit_id in row.get("message_unit_ids", {}).get(message_id, [])
        ]
        statuses: list[str] = []
        memory_ids: list[str] = []
        stage_ids: list[str] = []
        apply_actions: list[str] = []
        declared_change_segment_ids: set[str] = set()
        for segment_id in segment_ids:
            segment = segment_rows.get(segment_id)
            if segment is not None:
                statuses.append(segment.status)
                if segment.memory_id:
                    memory_ids.append(segment.memory_id)
            elif segment_id in skipped_rows:
                statuses.append("anchor_null")
            else:
                statuses.append("missing_after_cutting")
            for event in apply_events.get(segment_id, []):
                if event.get("stage_id"):
                    stage_ids.append(str(event["stage_id"]))
                apply_actions.append(str(event.get("action", "unknown")))
                decision = event.get("output", {}).get("decision", {})
                if isinstance(decision, dict):
                    declared_change_segment_ids.update(
                        str(value)
                        for value in decision.get("topic_source_segment_ids", [])
                    )
                    declared_change_segment_ids.update(
                        str(value)
                        for value in decision.get("summary_source_segment_ids", [])
                    )
                    for operation in decision.get("operations", []):
                        if isinstance(operation, dict):
                            declared_change_segment_ids.update(
                                str(value)
                                for value in operation.get("source_segment_ids", [])
                            )

        unique_statuses = sorted(set(statuses))
        if not segment_ids:
            structural_stage = "cutting_unmapped"
        elif "anchor_null" in statuses:
            structural_stage = "anchor_null"
        elif "boundary" in statuses:
            structural_stage = "boundary"
        elif "active" in statuses:
            structural_stage = "active_or_deferred"
        elif "no_memory" in statuses:
            structural_stage = "no_memory"
        elif statuses and all(status == "compressed" for status in statuses):
            structural_stage = "compressed_needs_content_evaluation"
        else:
            structural_stage = "mixed_or_missing"

        label.update(
            {
                "unit_ids": list(dict.fromkeys(unit_ids)),
                "segment_ids": segment_ids,
                "segment_statuses": statuses,
                "unique_segment_statuses": unique_statuses,
                "memory_ids": list(dict.fromkeys(memory_ids)),
                "memory_apply_stage_ids": list(dict.fromkeys(stage_ids)),
                "memory_apply_actions": list(dict.fromkeys(apply_actions)),
                "declared_change_segment_ids": sorted(declared_change_segment_ids),
                "structural_stage": structural_stage,
            }
        )
        output.append(label)
    return output


def generate_share_memory_report(
    attributions: list[dict[str, Any]], pipeline: DailyMemoryGraph
) -> dict[str, Any]:
    stages = Counter(str(row.get("structural_stage", "unknown")) for row in attributions)
    target_segments = {
        str(segment_id)
        for row in attributions
        for segment_id in row.get("segment_ids", [])
    }
    segment_statuses = Counter()
    for segment_id in target_segments:
        segment = pipeline.segments.get(segment_id)
        if segment is not None:
            segment_statuses[segment.status] += 1
        elif any(
            str(row.get("segment_id")) == segment_id
            for row in pipeline.skipped_segments
        ):
            segment_statuses["anchor_null"] += 1
        else:
            segment_statuses["missing"] += 1
    leaks = supervision_leakage_events(pipeline)
    mapped = sum(bool(row.get("segment_ids")) for row in attributions)
    fully_compressed = stages.get("compressed_needs_content_evaluation", 0)
    return {
        "schema_version": "share_memory_structural_report_v1",
        "total_labels": len(attributions),
        "mapped_labels": mapped,
        "mapping_rate": mapped / len(attributions) if attributions else None,
        "fully_compressed_labels": fully_compressed,
        "fully_compressed_rate": (
            fully_compressed / len(attributions) if attributions else None
        ),
        "labels_by_structural_stage": dict(sorted(stages.items())),
        "unique_target_segments": len(target_segments),
        "target_segments_by_status": dict(sorted(segment_statuses.items())),
        "supervision_leakage_event_count": len(leaks),
        "supervision_leakage_events": leaks,
        "content_retention_evaluated": False,
        "content_retention_note": (
            "Compressed is a structural state, not proof of semantic retention. "
            "Run the separate semantic evaluator before comparing retention."
        ),
    }
