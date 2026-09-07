from __future__ import annotations

import json
import unittest
from typing import Any

from stream_memory_graph_daily.memory import MemoryService
from stream_memory_graph_daily.models import MemoryRecord, SegmentRecord


class OperationLLM:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response

    def complete(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        self.payload = json.loads(user_prompt.split("INPUT DATA\n", 1)[1])
        return self.response


class FusionOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.existing = MemoryRecord(
            memory_id="m1",
            topic="Research project",
            summary="The project has an open analysis task.",
            topic_context=[
                {"item_id": "ctx1", "type": "open issue", "content": "The analysis is pending."}
            ],
            user_memories=[
                {"item_id": "fact1", "type": "deadline", "content": "The deadline is Friday."}
            ],
            source_anchors=["research analysis"],
            source_segments=["old-segment"],
        )
        self.segment = SegmentRecord(
            "new-segment",
            "The analysis is complete and the deadline was cancelled.",
            "research analysis completion",
            "2025-06-02",
        )

    def test_add_update_delete_are_applied_with_validated_sources(self) -> None:
        llm = OperationLLM({
            "topic": "Research project",
            "summary": "The analysis is complete and the former deadline was cancelled.",
            "operations": [
                {
                    "operation": "update",
                    "field": "topic_context",
                    "item_id": "ctx1",
                    "value": {"type": "status", "content": "The analysis is complete."},
                    "source_segment_ids": ["new-segment"],
                },
                {
                    "operation": "delete",
                    "field": "user_memories",
                    "item_id": "fact1",
                    "source_segment_ids": ["new-segment"],
                },
                {
                    "operation": "add",
                    "field": "user_memories",
                    "value": {"type": "outcome", "content": "The user completed the analysis."},
                    "source_segment_ids": ["new-segment"],
                },
            ],
        })
        updated, result = MemoryService(llm).fuse("c1", self.existing, [self.segment])

        self.assertEqual(updated.topic_context[0]["item_id"], "ctx1")
        self.assertEqual(updated.topic_context[0]["content"], "The analysis is complete.")
        self.assertTrue(updated.user_memories[0]["item_id"].startswith("item:"))
        self.assertNotEqual(updated.user_memories[0]["item_id"], "new-segment")
        self.assertEqual(updated.source_segments, ["old-segment", "new-segment"])
        self.assertEqual(result["operations"][1]["source_segment_ids"], ["new-segment"])
        self.assertEqual(result["topic_source_segment_ids"], [])
        self.assertEqual(result["summary_source_segment_ids"], ["new-segment"])

    def test_operation_rejects_invented_source_segment(self) -> None:
        llm = OperationLLM({
            "topic": self.existing.topic,
            "summary": self.existing.summary,
            "operations": [{
                "operation": "delete",
                "field": "topic_context",
                "item_id": "ctx1",
                "source_segment_ids": ["invented"],
            }],
        })
        with self.assertRaisesRegex(ValueError, "must come from new_group"):
            MemoryService(llm).fuse("c1", self.existing, [self.segment])

    def test_one_segment_can_add_multiple_independently_identified_items(self) -> None:
        llm = OperationLLM({
            "topic": self.existing.topic,
            "summary": "The project gained two durable facts.",
            "operations": [
                {
                    "operation": "add",
                    "field": "user_memories",
                    "value": {"type": "result", "content": "The experiment passed."},
                    "source_segment_ids": ["new-segment"],
                },
                {
                    "operation": "add",
                    "field": "user_memories",
                    "value": {"type": "next step", "content": "The user will write the report."},
                    "source_segment_ids": ["new-segment"],
                },
            ],
        })
        updated, result = MemoryService(llm).fuse("c1", self.existing, [self.segment])

        added_ids = [operation["item_id"] for operation in result["operations"]]
        self.assertEqual(len(set(added_ids)), 2)
        self.assertTrue(all(item_id.startswith("item:") for item_id in added_ids))
        self.assertEqual(
            [item["item_id"] for item in updated.user_memories[-2:]], added_ids
        )


if __name__ == "__main__":
    unittest.main()
