from __future__ import annotations

import json
import tempfile
import unittest
from typing import Any

from stream_memory_graph_daily.config import DailyGraphConfig
from stream_memory_graph_daily.models import MemoryRecord
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


class PromotionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
