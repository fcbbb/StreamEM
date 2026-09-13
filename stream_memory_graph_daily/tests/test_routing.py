from __future__ import annotations

import unittest

import numpy as np

from stream_memory_graph_daily.models import MemoryRecord
from stream_memory_graph_daily.routing import TopicOwnerRouter


class VectorEncoder:
    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors

    def encode(self, texts, **_):
        scalar = isinstance(texts, str)
        values = [texts] if scalar else list(texts)
        rows = np.asarray([self.vectors[str(value)] for value in values], dtype=np.float32)
        return rows[0] if scalar else rows


class RoutingLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        return self.response


class TopicOwnerRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.encoder = VectorEncoder({
            "deployment": [1.0, 0.0],
            "research": [0.0, 1.0],
        })
        self.router = TopicOwnerRouter(self.encoder)

    def test_indexes_memories_across_levels_without_graph_edges(self) -> None:
        lower = MemoryRecord("l1", "deployment", "Deployment incidents", level=1)
        upper = MemoryRecord("l2", "deployment", "Deployment workflow", level=2)

        self.router.register(lower)
        self.router.register(upper)

        candidates = self.router.candidates("deployment")

        self.assertEqual(self.router.memory_ids(), {"l1", "l2"})
        self.assertEqual({row["memory_id"] for row in candidates}, {"l1", "l2"})
        self.assertFalse(hasattr(self.router, "graph"))

    def test_replace_removes_old_active_owners(self) -> None:
        old = MemoryRecord("l1", "deployment", "Old deployment memory")
        new = MemoryRecord("l2", "deployment", "Higher deployment memory", level=2)

        self.router.register(old)
        self.router.replace({"l1"}, new)

        self.assertEqual(self.router.memory_ids(), {"l2"})
        self.assertEqual([row["memory_id"] for row in self.router.candidates("deployment")], ["l2"])

    def test_rebuild_restores_only_supplied_active_memories(self) -> None:
        first = MemoryRecord("m1", "deployment", "One")
        second = MemoryRecord("m2", "research", "Two")
        self.router.register(first)
        self.router.rebuild([second])

        self.assertEqual(self.router.memory_ids(), {"m2"})

    def test_decide_compares_only_recalled_l2_plus_candidates(self) -> None:
        l1 = MemoryRecord("l1", "deployment", "L1 should not be recalled", level=1)
        owner = MemoryRecord("l2", "deployment", "Deployment workflow", level=2)
        provisional = MemoryRecord("p", "deployment", "New deployment event", level=1)
        self.router.register(l1)
        self.router.register(owner)
        llm = RoutingLLM({"owner_memory_id": "l2", "reason": "same_topic"})

        decision = self.router.decide(
            provisional,
            {"l1": l1, "l2": owner},
            llm=llm,
            top_k=5,
        )

        self.assertEqual(decision.owner_memory_id, "l2")
        self.assertEqual(decision.reason, "same_topic")
        self.assertEqual([row["memory_id"] for row in decision.candidates], ["l2"])
        self.assertEqual(len(llm.calls), 1)

    def test_invalid_owner_output_falls_back_to_no_owner(self) -> None:
        owner = MemoryRecord("l2", "deployment", "Deployment workflow", level=2)
        provisional = MemoryRecord("p", "deployment", "New deployment event", level=1)
        self.router.register(owner)
        llm = RoutingLLM({"owner_memory_id": "invented", "reason": "same_topic"})

        decision = self.router.decide(
            provisional,
            {"l2": owner},
            llm=llm,
        )

        self.assertIsNone(decision.owner_memory_id)
        self.assertEqual(decision.reason, "uncertain")


if __name__ == "__main__":
    unittest.main()
