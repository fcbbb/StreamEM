from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


SYSTEM_PROMPT = """You are segmenting a conversation between a user and an AI assistant into continuous conversation events.

A conversation event is a contiguous range of semantic units that serves one local interaction goal or one coherent topic thread.

Create events at a coarse interaction level. A good event usually contains a complete mini-conversation, not a single question-answer pair.

Start a new event when the next semantic unit begins a different interaction purpose, such as a new task episode, a new decision episode, a new personal fact or activity report, a new preference or plan, or a clearly separate topic thread.

Keep semantic units in the same event when they directly support the current goal, including answers, follow-up questions, clarifications, corrections, examples, constraints, field collection, confirmations, and brief acknowledgements.

For exploratory discussion, keep connected questions and answers in the same event when they develop the same broad topic thread. A new question starts a new event only when it changes the interaction purpose, not when it explores another aspect of the current thread.

Opening or closing social exchanges may form separate events when they have a different interaction function from the substantive conversation. Opening bridges stay with the opening exchange until the first semantic unit whose main function is the substantive request, question, or topic. Closing begins at the first semantic unit whose main function is wrapping up, thanking, declining further help, or ending the exchange.

If a topic is interrupted and later returns, treat the return as a new substantive event.

When the boundary is ambiguous, prefer the larger coherent event.

Assign every semantic unit to exactly one event, and each event must contain a contiguous range of semantic units.

Output format:
{
  \"segments\": [
    {
      \"segment_id\": \"seg001\",
      \"start_unit_id\": \"u001\",
      \"end_unit_id\": \"u008\"
    }
  ]
}"""


@dataclass
class NormalizedSegments:
    segments: list[dict[str, Any]]
    boundary_positions: list[int]


def make_user_prompt(units: list[dict[str, Any]]) -> str:
    return "Please segment the following conversation:\n\n" + json.dumps(
        units, ensure_ascii=False, indent=2
    )


def unit_index(units: list[dict[str, Any]]) -> dict[str, int]:
    ids = [str(unit.get("unit_id")) for unit in units]
    if len(ids) != len(set(ids)) or any(not unit_id for unit_id in ids):
        raise ValueError("units must have unique non-empty unit_id values")
    return {unit_id: index for index, unit_id in enumerate(ids)}


def normalize_segments(payload: Any, units: list[dict[str, Any]]) -> NormalizedSegments:
    """Validate ranges and convert them to boundary positions."""

    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise ValueError("model output must be an object with a segments list")
    if not units:
        raise ValueError("cannot normalize segments for an empty session")
    index = unit_index(units)
    normalized = []
    expected_start = 0
    for segment_number, segment in enumerate(payload["segments"], start=1):
        if not isinstance(segment, dict):
            raise ValueError(f"segment {segment_number} is not an object")
        start_id = segment.get("start_unit_id")
        end_id = segment.get("end_unit_id")
        if start_id not in index or end_id not in index:
            raise ValueError(f"segment {segment_number} references an unknown unit")
        start, end = index[start_id], index[end_id]
        if start != expected_start:
            raise ValueError(
                f"segments are not contiguous at segment {segment_number}: "
                f"expected start index {expected_start}, got {start}"
            )
        if end < start:
            raise ValueError(f"segment {segment_number} has end before start")
        normalized.append({
            "segment_id": segment.get("segment_id") or f"seg{segment_number:03d}",
            "start_unit_id": units[start]["unit_id"],
            "end_unit_id": units[end]["unit_id"],
        })
        expected_start = end + 1
    if expected_start != len(units):
        raise ValueError(
            f"segments do not cover all units: covered through {expected_start - 1}, "
            f"session has {len(units)} units"
        )
    return NormalizedSegments(
        segments=normalized,
        boundary_positions=[index[segment["end_unit_id"]] + 1 for segment in normalized[:-1]],
    )


def parse_model_json(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(cleaned[start : end + 1])
