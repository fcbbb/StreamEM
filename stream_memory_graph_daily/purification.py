from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .llm import JsonLLM, LLMUnavailable
from .models import SegmentRecord
from .prompts import COMMUNITY_PURIFICATION_PROMPT


AuditSink = Callable[[str, str, dict[str, Any]], None]


@dataclass(frozen=True)
class PurifiedGroup:
    """A validated partition of one planned community."""

    group_id: str
    segment_ids: tuple[str, ...]


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
        self, community_id: str, segments: list[SegmentRecord]
    ) -> list[PurifiedGroup]:
        input_ids = [segment.segment_id for segment in segments]
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("community purification input contains duplicate segment_id")
        if not input_ids:
            raise ValueError("community purification requires at least one segment")

        if len(input_ids) == 1:
            self._audit(
                "skipped_singleton",
                {
                    "community_id": community_id,
                    "input_segment_ids": input_ids,
                    "reason": "singleton_community_does_not_need_purification",
                    "output_groups": [
                        {"group_id": "g1", "segment_ids": input_ids}
                    ],
                },
            )
            return [PurifiedGroup("g1", (input_ids[0],))]

        request = {
            "community_id": community_id,
            "segments": [self._segment_row(segment) for segment in segments],
        }
        if self.llm is None:
            error = LLMUnavailable("community purification requires an LLM")
            self._audit(
                "llm_call",
                {
                    "request": request,
                    "system_prompt": COMMUNITY_PURIFICATION_PROMPT,
                    "response": None,
                    "error_type": type(error).__name__,
                    "error": str(error),
                },
            )
            raise error

        response: dict[str, Any] | None = None
        try:
            response = self.llm.complete(
                COMMUNITY_PURIFICATION_PROMPT,
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
                if set(row) != {"group_id", "segment_ids"}:
                    raise ValueError(
                        f"purification group {index} fields must be exactly "
                        "['group_id', 'segment_ids']"
                    )
                group_id = str(row["group_id"]).strip()
                segment_ids = row["segment_ids"]
                if not group_id:
                    raise ValueError(f"purification group {index} has an empty group_id")
                if group_id in group_ids:
                    raise ValueError(f"purification output duplicates group_id: {group_id}")
                group_ids.add(group_id)
                if (
                    not isinstance(segment_ids, list)
                    or not segment_ids
                    or any(not isinstance(segment_id, str) for segment_id in segment_ids)
                ):
                    raise ValueError(
                        f"purification group {index} must have a non-empty segment_ids list"
                    )
                clean_ids = [segment_id.strip() for segment_id in segment_ids]
                if any(not segment_id for segment_id in clean_ids):
                    raise ValueError(f"purification group {index} contains an empty segment_id")
                seen.extend(clean_ids)
                normalized.append(PurifiedGroup(group_id, tuple(clean_ids)))

            if len(seen) != len(set(seen)):
                raise ValueError("purification output repeats a segment_id")
            if set(seen) != expected:
                missing = sorted(expected - set(seen))
                extra = sorted(set(seen) - expected)
                raise ValueError(
                    "purification output must cover exactly the input segment IDs; "
                    f"missing={missing}, extra={extra}"
                )
        except Exception as exc:
            self._audit(
                "llm_call",
                {
                    "request": request,
                    "system_prompt": COMMUNITY_PURIFICATION_PROMPT,
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
                "system_prompt": COMMUNITY_PURIFICATION_PROMPT,
                "response": response,
            },
        )
        self._audit(
            "normalized_result",
            {
                "community_id": community_id,
                "input_segment_ids": input_ids,
                "output_groups": [
                    {
                        "group_id": group.group_id,
                        "segment_ids": list(group.segment_ids),
                    }
                    for group in normalized
                ],
            },
        )
        return normalized
