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


class MemoryExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.segment = SegmentRecord(
            "new-segment",
            "The conversation only explores a general concept.",
            "general concept",
            "2025-06-02",
        )

    def test_topic_summary_only_output_is_treated_as_no_memory(self) -> None:
        llm = OperationLLM({
            "topic": "General concept",
            "summary": "The conversation explored a general concept.",
            "topic_context": [],
            "user_memories": [],
        })

        memory = MemoryService(llm).extract("c1", [self.segment])

        self.assertIsNone(memory)

    def test_output_with_a_durable_item_still_creates_memory(self) -> None:
        llm = OperationLLM({
            "topic": "Research plan",
            "summary": "The user plans to review a draft.",
            "topic_context": [],
            "user_memories": [
                {"type": "plan", "content": "The user plans to review a draft."}
            ],
        })

        memory = MemoryService(llm).extract("c1", [self.segment])

        self.assertIsNotNone(memory)
        assert memory is not None
        self.assertEqual(len(memory.user_memories), 1)
        self.assertEqual(memory.level, 1)
        self.assertEqual(
            memory.direct_members,
            [{
                "node_id": "new-segment",
                "level": 0,
                "kind": "segment",
                "representation": "general concept",
            }],
        )
        self.assertTrue(memory.last_mentioned_at)


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
            "no_op_reason": None,
        })
        updated, result = MemoryService(llm).fuse("c1", self.existing, [self.segment])

        self.assertEqual(updated.topic_context[0]["item_id"], "ctx1")
        self.assertEqual(updated.topic_context[0]["content"], "The analysis is complete.")
        self.assertTrue(updated.user_memories[0]["item_id"].startswith("item:"))
        self.assertNotEqual(updated.user_memories[0]["item_id"], "new-segment")
        self.assertEqual(updated.source_segments, ["old-segment", "new-segment"])
        self.assertEqual(
            [member["node_id"] for member in updated.direct_members],
            ["new-segment"],
        )
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
            "no_op_reason": None,
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
            "no_op_reason": None,
        })
        updated, result = MemoryService(llm).fuse("c1", self.existing, [self.segment])

        added_ids = [operation["item_id"] for operation in result["operations"]]
        self.assertEqual(len(set(added_ids)), 2)
        self.assertTrue(all(item_id.startswith("item:") for item_id in added_ids))
        self.assertEqual(
            [item["item_id"] for item in updated.user_memories[-2:]], added_ids
        )

    def test_empty_operations_report_why_nothing_was_stored(self) -> None:
        for reason in ("already_present", "no_storable_content"):
            with self.subTest(reason=reason):
                llm = OperationLLM({
                    "topic": self.existing.topic,
                    "summary": self.existing.summary,
                    "operations": [],
                    "no_op_reason": reason,
                })

                updated, result = MemoryService(llm).fuse(
                    "c1", self.existing, [self.segment]
                )

                self.assertEqual(result["decision"], "no_material_change")
                self.assertEqual(result["no_op_reason"], reason)
                self.assertEqual(updated.version, self.existing.version)
                self.assertEqual(updated.updated_at, self.existing.updated_at)
                self.assertTrue(updated.last_mentioned_at)

    def test_provisional_fusion_uses_only_l1_content_and_keeps_one_hop_member(self) -> None:
        owner = MemoryRecord(
            memory_id="l2-owner",
            topic="Research project",
            summary="The project has an open analysis task.",
            topic_context=self.existing.topic_context,
            user_memories=self.existing.user_memories,
            source_anchors=self.existing.source_anchors,
            source_segments=self.existing.source_segments,
            level=2,
            direct_members=[{
                "node_id": "old-l1",
                "level": 1,
                "kind": "memory",
                "representation": "Research project details",
            }],
        )
        provisional = MemoryRecord(
            memory_id="new-l1",
            topic="Research project",
            summary="The analysis is complete.",
            topic_context=[],
            user_memories=[{
                "item_id": "new-fact",
                "type": "outcome",
                "content": "The user completed the analysis.",
            }],
            source_anchors=["research analysis completion"],
            source_segments=["new-segment"],
            level=1,
            direct_members=[{
                "node_id": "new-segment",
                "level": 0,
                "kind": "segment",
                "representation": "research analysis completion",
            }],
        )
        llm = OperationLLM({
            "topic": owner.topic,
            "summary": "The analysis is complete.",
            "operations": [{
                "operation": "add",
                "field": "user_memories",
                "value": {"type": "outcome", "content": "The user completed the analysis."},
                "source_segment_ids": ["new-segment"],
            }],
            "no_op_reason": None,
        })

        updated, _ = MemoryService(llm).fuse_provisional("owner:l2-owner", owner, provisional)

        self.assertEqual(updated.level, 2)
        self.assertEqual(updated.direct_members[-1]["node_id"], "new-l1")
        self.assertEqual(updated.direct_members[-1]["level"], 1)
        self.assertEqual(updated.source_segments, ["old-segment", "new-segment"])
        self.assertIn("provisional_l1", llm.payload["new_group"])
        self.assertNotIn("segments", llm.payload["new_group"])

    def test_legacy_memory_state_gets_compatible_metadata_defaults(self) -> None:
        payload = self.existing.to_dict()
        payload.pop("level")
        payload.pop("direct_members")
        payload.pop("last_mentioned_at")

        restored = MemoryRecord.from_dict(payload)

        self.assertEqual(restored.level, 1)
        self.assertEqual(restored.direct_members, [])
        self.assertEqual(restored.last_mentioned_at, restored.updated_at)


if __name__ == "__main__":
    unittest.main()
