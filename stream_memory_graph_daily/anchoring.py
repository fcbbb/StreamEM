from __future__ import annotations

import json
from typing import Any, Callable

from .llm import JsonLLM, LLMUnavailable
from .prompts import ANCHOR_PROMPT


AuditSink = Callable[[str, str, dict[str, Any]], None]


class AnchorExtractor:
    def __init__(self, llm: JsonLLM | None, audit_sink: AuditSink | None = None) -> None:
        self.llm = llm
        self.audit_sink = audit_sink

    @staticmethod
    def _selected_anchor(row: dict[str, Any]) -> str | None:
        required = {
            "segment_id",
            "coarse_candidate",
            "selected_anchor",
            "fine_candidate",
            "reason",
        }
        if set(row) != required:
            raise ValueError(f"anchor item fields must be exactly {sorted(required)}")
        for key in ("coarse_candidate", "selected_anchor", "fine_candidate"):
            if row[key] is not None and not isinstance(row[key], str):
                raise ValueError(f"{key} must be a string or null")
        if not isinstance(row["reason"], str):
            raise ValueError("anchor reason must be a string")
        selected = row["selected_anchor"]
        if selected is None:
            if row["coarse_candidate"] is not None or row["fine_candidate"] is not None:
                raise ValueError("null selected_anchor requires null coarse/fine candidates")
            return None
        selected = selected.strip()
        return selected or None

    def extract_many(self, segments: list[dict[str, str]]) -> dict[str, str | None]:
        if self.llm is None:
            raise LLMUnavailable("semantic-anchor extraction requires an LLM")
        normalized: list[dict[str, str]] = []
        expected_ids: list[str] = []
        for index, segment in enumerate(segments, start=1):
            if not isinstance(segment, dict):
                raise ValueError(f"anchor input segment {index} must be an object")
            segment_id = str(segment.get("segment_id", "")).strip()
            text = str(segment.get("text", "")).strip()
            if not segment_id or not text:
                raise ValueError("anchor input segments need non-empty segment_id and text")
            if segment_id in expected_ids:
                raise ValueError(f"duplicate anchor input segment_id: {segment_id}")
            expected_ids.append(segment_id)
            normalized.append({"segment_id": segment_id, "text": text})
        if not normalized:
            return {}
        request = {"segments": normalized}
        value: dict[str, Any] | None = None
        try:
            value = self.llm.complete(
                ANCHOR_PROMPT,
                "INPUT SEGMENTS\n" + json.dumps(normalized, ensure_ascii=False, indent=2),
            )
            if set(value) != {"anchors"} or not isinstance(value["anchors"], list):
                raise ValueError("batch anchor output must contain exactly an anchors list")
            output: dict[str, str | None] = {}
            for row in value["anchors"]:
                if not isinstance(row, dict):
                    raise ValueError("each anchor output item must be an object")
                segment_id = str(row.get("segment_id", "")).strip()
                if segment_id not in expected_ids:
                    raise ValueError(f"anchor output contains unknown segment_id: {segment_id}")
                if segment_id in output:
                    raise ValueError(f"anchor output duplicates segment_id: {segment_id}")
                output[segment_id] = self._selected_anchor(row)
            if list(output) != expected_ids:
                missing = [segment_id for segment_id in expected_ids if segment_id not in output]
                raise ValueError(
                    "anchor output must preserve every input segment in order; "
                    f"missing={missing}"
                )
        except Exception as exc:
            if self.audit_sink is not None:
                self.audit_sink(
                    "anchoring",
                    "llm_call",
                    {
                        "request": request,
                        "system_prompt": ANCHOR_PROMPT,
                        "response": value,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
            raise
        if self.audit_sink is not None:
            self.audit_sink(
                "anchoring",
                "llm_call",
                {
                    "request": request,
                    "system_prompt": ANCHOR_PROMPT,
                    "response": value,
                    "selected_anchors": output,
                },
            )
        return output

    def extract(self, segment_text: str) -> str | None:
        return self.extract_many(
            [{"segment_id": "single_segment", "text": segment_text}]
        )["single_segment"]
