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


if __name__ == "__main__":
    unittest.main()
