from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from .llm import JsonLLM, LLMUnavailable
from .prompts import CUTTING_PROMPT


AuditSink = Callable[[str, str, dict[str, Any]], None]


_ABBREVIATIONS = {
    "Mr.", "Mrs.", "Ms.", "Dr.", "Prof.", "e.g.", "i.e.", "etc.", "U.S.", "AI.", "GPT-3."
}


@dataclass
class CutSegment:
    segment_id: str
    text: str
    segment_index: int
    start_unit_id: str
    end_unit_id: str
    unit_ids: list[str]


def split_sentences(text: str) -> list[str]:
    protected = str(text).strip()
    placeholders: dict[str, str] = {}
    for index, abbreviation in enumerate(sorted(_ABBREVIATIONS, key=len, reverse=True)):
        token = f"__ABBR_{index}__"
        if abbreviation in protected:
            protected = protected.replace(abbreviation, token)
            placeholders[token] = abbreviation
    parts = re.split(r"(?<=[.!?。！？])\s+(?=[A-Z\"'\u3400-\u9fff])", protected)
    output = []
    for part in parts:
        for token, abbreviation in placeholders.items():
            part = part.replace(token, abbreviation)
        if part.strip():
            output.append(part.strip())
    return output or ([str(text).strip()] if str(text).strip() else [])


def make_units(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for message_index, message in enumerate(messages, start=1):
        turn = int(message.get("turn", message_index))
        speaker = str(message.get("speaker", message.get("role", "unknown")))
        text = str(message.get("message", message.get("content", message.get("text", ""))))
        for sentence_index, sentence in enumerate(split_sentences(text), start=1):
            units.append(
                {
                    "unit_id": f"u{len(units) + 1:03d}",
                    "message_id": f"m{turn:03d}",
                    "turn": turn,
                    "sentence_index": sentence_index,
                    "speaker": speaker,
                    "text": sentence,
                }
            )
    if not units:
        raise ValueError("conversation has no non-empty messages")
    return units


class ConversationCutter:
    def __init__(self, llm: JsonLLM | None, audit_sink: AuditSink | None = None) -> None:
        self.llm = llm
        self.audit_sink = audit_sink

    def cut(self, conversation_id: str, messages: list[dict[str, Any]]) -> list[CutSegment]:
        if self.llm is None:
            raise LLMUnavailable("conversation cutting requires an LLM")
        units = make_units(messages)
        request = {"conversation_id": conversation_id, "units": units}
        payload: dict[str, Any] | None = None
        try:
            payload = self.llm.complete(
                CUTTING_PROMPT,
                "Please segment the following conversation:\n\n"
                + json.dumps(units, ensure_ascii=False, indent=2),
            )
            rows = payload.get("segments")
            if not isinstance(rows, list) or not rows:
                raise ValueError("cutting output must contain a non-empty segments list")
            index = {unit["unit_id"]: position for position, unit in enumerate(units)}
            expected_start = 0
            output: list[CutSegment] = []
            prefix = str(conversation_id).strip() or "conversation"
            for number, row in enumerate(rows, start=1):
                if not isinstance(row, dict):
                    raise ValueError(f"cut segment {number} is not an object")
                start_id, end_id = row.get("start_unit_id"), row.get("end_unit_id")
                if start_id not in index or end_id not in index:
                    raise ValueError(f"cut segment {number} references an unknown unit")
                start, end = index[start_id], index[end_id]
                if start != expected_start or end < start:
                    raise ValueError(f"cut segments are not contiguous at segment {number}")
                selected = units[start : end + 1]
                output.append(
                    CutSegment(
                        segment_id=f"{prefix}_seg{number:03d}",
                        text="\n".join(f"[{unit['speaker']}] {unit['text']}" for unit in selected),
                        segment_index=number,
                        start_unit_id=str(start_id),
                        end_unit_id=str(end_id),
                        unit_ids=[str(unit["unit_id"]) for unit in selected],
                    )
                )
                expected_start = end + 1
            if expected_start != len(units):
                raise ValueError("cut segments do not cover every semantic unit")
        except Exception as exc:
            if self.audit_sink is not None:
                self.audit_sink(
                    "cutting",
                    "llm_call",
                    {
                        "request": request,
                        "system_prompt": CUTTING_PROMPT,
                        "response": payload,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
            raise
        if self.audit_sink is not None:
            self.audit_sink(
                "cutting",
                "llm_call",
                {
                    "request": request,
                    "system_prompt": CUTTING_PROMPT,
                    "response": payload,
                    "normalized_segments": [
                        {
                            "segment_id": segment.segment_id,
                            "text": segment.text,
                            "segment_index": segment.segment_index,
                            "start_unit_id": segment.start_unit_id,
                            "end_unit_id": segment.end_unit_id,
                            "unit_ids": segment.unit_ids,
                        }
                        for segment in output
                    ],
                },
            )
        return output
