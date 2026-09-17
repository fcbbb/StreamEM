from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from typing import Any

from stream_memory_graph_daily.config import DailyGraphConfig
from stream_memory_graph_daily.models import MemoryRecord, SegmentRecord
from stream_memory_graph_daily.pipeline import DailyMemoryGraph


class PromotionLLM:
    def complete(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        payload = json.loads(user_prompt.split("INPUT DATA\n", 1)[1])
        if "lower-level memories" in system_prompt:
            memory = payload["memories"][0]
            return {
                "topic": memory["topic"],
                "summary": "A stable long-term summary.",
                "topic_context": memory["topic_context"],
                "user_memories": memory["user_memories"],
            }
        raise AssertionError(f"unexpected promotion prompt: {system_prompt[:80]}")


class AdjacentOwnerPromotionLLM(PromotionLLM):
    def complete(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        if system_prompt.startswith("You decide whether one provisional L1"):
            return {"owner_memory_id": "owner-l3", "reason": "same_topic"}
        if system_prompt.startswith("You maintain structured memory"):
            payload = json.loads(user_prompt.split("INPUT DATA\n", 1)[1])
            existing = payload["existing_memory"]
            source_ids = payload["new_group"]["source_segment_ids"]
            return {
                "topic": existing["topic"],
                "summary": "The long-term topic was updated.",
                "operations": [{
                    "operation": "add",
                    "field": "user_memories",
                    "value": {
                        "type": "update",
                        "content": "The lower-level topic remains active.",
                    },
                    "source_segment_ids": source_ids,
                }],
                "no_op_reason": None,
            }
        return super().complete(system_prompt, user_prompt)


class PromotionTests(unittest.TestCase):
    def test_high_level_promotion_tasks_run_concurrently(self) -> None:
        class ConcurrentPromotionLLM(PromotionLLM):
            def __init__(self) -> None:
                self.lock = threading.Lock()
                self.active = 0
                self.maximum = 0

            def complete(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                try:
                    time.sleep(0.03)
                    return super().complete(system_prompt, user_prompt)
                finally:
                    with self.lock:
                        self.active -= 1

        llm = ConcurrentPromotionLLM()
        pipeline = DailyMemoryGraph(
            llm=llm,
            config=DailyGraphConfig(
                postprocess_workers=2,
                promotion_inactivity_days=(7, 30),
            ),
        )
        for memory_id in ("old-l1-a", "old-l1-b"):
            memory = MemoryRecord(
                memory_id,
                f"Research project {memory_id}",
                "The project has a stable state.",
                topic_context=[{
                    "type": "state",
                    "content": "The research project is established.",
                }],
                last_mentioned_at="2025-01-01",
            )
            pipeline.memories[memory_id] = memory
            pipeline._add_memory_to_layered_graphs(memory)
        # Keep this unit test independent of the optional Leiden dependency;
        # the production scheduler receives the same detector from the
        # community planner.
        pipeline.planner.detect_nodes = lambda _active, node_ids: [
            {node_id} for node_id in sorted(node_ids)
        ]
        pipeline.layered_graph.graph(1).graph.remove_edges_from(
            list(pipeline.layered_graph.graph(1).graph.edges())
        )

        result = pipeline.checkpoint(
            checkpoint_date="2025-01-10",
            reason="promotion_concurrency_test",
        )

        self.assertEqual(llm.maximum, 2)
        self.assertEqual(
            len([
                change
                for change in result["changes"]
                if change["action"] == "memory_promoted"
            ]),
            2,
        )

    def test_pipeline_uses_same_level_graphs_and_persists_active_owners(self) -> None:
        pipeline = DailyMemoryGraph()
        memories = [
            MemoryRecord("l1", "Local topic", "Local summary", level=1),
            MemoryRecord("l2", "Broader topic", "Broader summary", level=2),
            MemoryRecord("l3", "Long topic", "Long summary", level=3),
        ]
        for memory in memories:
            pipeline.memories[memory.memory_id] = memory
            pipeline._add_memory_to_layered_graphs(memory)

        self.assertEqual(pipeline.active_memory_ids, {"l1", "l2", "l3"})
        self.assertEqual(pipeline.layered_graph.graph(0).memory_ids(), {"l1"})
        self.assertEqual(pipeline.layered_graph.graph(1).memory_ids(), {"l1"})
        self.assertEqual(pipeline.layered_graph.graph(2).memory_ids(), {"l2"})
        self.assertEqual(pipeline.layered_graph.graph(3).memory_ids(), {"l3"})

        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/state.json"
            pipeline.save(path)
            restored = DailyMemoryGraph.load(path)

        self.assertEqual(restored.active_memory_ids, {"l1", "l2", "l3"})
        self.assertEqual(restored.topic_owner_router.memory_ids(), {"l1", "l2", "l3"})
        self.assertEqual(restored.layered_graph.graph(1).memory_ids(), {"l1"})
        self.assertEqual(restored.layered_graph.graph(2).memory_ids(), {"l2"})
        self.assertEqual(restored.layered_graph.graph(3).memory_ids(), {"l3"})

    def test_expired_singleton_promotes_one_level_and_inherits_mention_time(self) -> None:
        pipeline = DailyMemoryGraph(
            llm=PromotionLLM(),
            config=DailyGraphConfig(promotion_inactivity_days=(7, 30)),
        )
        source = MemoryRecord(
            memory_id="old-l1",
            topic="Research project",
            summary="The project has a stable state.",
            topic_context=[{
                "type": "state",
                "content": "The research project is established.",
            }],
            level=1,
            last_mentioned_at="2025-01-01",
        )
        pipeline.memories[source.memory_id] = source
        pipeline._add_memory_to_layered_graphs(source)
        pipeline.topic_owner_router.register(source)

        result = pipeline.checkpoint(
            checkpoint_date="2025-01-10",
            reason="promotion_test",
        )

        promoted_ids = [
            change["memory_id"]
            for change in result["changes"]
            if change["action"] == "memory_promoted"
        ]
        self.assertEqual(len(promoted_ids), 1)
        promoted = pipeline.memories[promoted_ids[0]]
        self.assertEqual(promoted.level, 2)
        self.assertEqual(promoted.last_mentioned_at, "2025-01-01")
        self.assertEqual(
            promoted.direct_members,
            [{
                "node_id": "old-l1",
                "level": 1,
                "kind": "memory",
                "representation": "Research project",
            }],
        )
        self.assertIn("old-l1", pipeline.memories)
        self.assertNotIn("old-l1", pipeline.layered_graph.memory_ids())
        self.assertIn(promoted.memory_id, pipeline.layered_graph.memory_ids())
        self.assertEqual(pipeline.active_memory_ids, {promoted.memory_id})
        self.assertNotIn("old-l1", pipeline.topic_owner_router.memory_ids())
        self.assertIn(promoted.memory_id, pipeline.topic_owner_router.memory_ids())

        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/state.json"
            pipeline.save(path)
            restored = DailyMemoryGraph.load(path, llm=PromotionLLM())
            self.assertIn("old-l1", restored.memories)
            self.assertNotIn("old-l1", restored.layered_graph.memory_ids())
            self.assertIn(promoted.memory_id, restored.layered_graph.memory_ids())
            self.assertEqual(restored.active_memory_ids, {promoted.memory_id})
        self.assertNotIn("old-l1", restored.topic_owner_router.memory_ids())

        next_result = pipeline.checkpoint(
            checkpoint_date="2025-01-11",
            reason="promotion_test_follow_up",
        )
        self.assertEqual(next_result["status"], "no_pending")

    def test_new_l2_wakes_only_l3_owner_and_updates_owner_lifecycle(self) -> None:
        pipeline = DailyMemoryGraph(
            llm=AdjacentOwnerPromotionLLM(),
            config=DailyGraphConfig(
                enable_owner_bypass=True,
                promotion_inactivity_days=(7, 30),
            ),
        )
        source = MemoryRecord(
            "old-l2",
            "Research project",
            "The project has a stable state.",
            user_memories=[{
                "type": "state",
                "content": "The research project is established.",
            }],
            source_segments=["source-segment"],
            level=2,
            last_mentioned_at="2025-01-01",
        )
        owner = MemoryRecord(
            "owner-l3",
            "Research project",
            "The long-term project state.",
            user_memories=[{
                "type": "state",
                "content": "The project is long-running.",
            }],
            level=3,
            last_mentioned_at="2024-01-01",
        )
        pipeline.memories = {source.memory_id: source, owner.memory_id: owner}
        pipeline.segments["source-segment"] = SegmentRecord(
            "source-segment",
            "The project is established.",
            "Research project",
            "2025-01-01",
            status="compressed",
            memory_id=source.memory_id,
        )
        pipeline._add_memory_to_layered_graphs(source)
        pipeline._add_memory_to_layered_graphs(owner)
        pipeline.topic_owner_router.rebuild((source, owner))
        pipeline.planner.detect_nodes = lambda _active, node_ids: [
            {node_id} for node_id in sorted(node_ids)
        ]

        changes: list[dict[str, Any]] = []
        result = pipeline._run_promotions("2025-02-10", changes)

        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(
            [change["action"] for change in changes], ["memory_owner_fused"]
        )
        self.assertEqual(pipeline.active_memory_ids, {"owner-l3"})
        self.assertEqual(pipeline.memories["owner-l3"].last_mentioned_at, "2025-02-10")
        self.assertNotIn("old-l2", pipeline.topic_owner_router.memory_ids())
        self.assertNotIn("old-l2", pipeline.layered_graph.memory_ids())


if __name__ == "__main__":
    unittest.main()
