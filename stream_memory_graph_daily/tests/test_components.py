from __future__ import annotations

import unittest

import numpy as np

from stream_memory_graph_daily.config import DailyGraphConfig
from stream_memory_graph_daily.anchoring import AnchorExtractor
from stream_memory_graph_daily.cutting import ConversationCutter
from stream_memory_graph_daily.graph import ActiveGraph, LayeredActiveGraph
from stream_memory_graph_daily.relations import MemoryRelation, MemoryRelationStore


class CuttingFakeLLM:
    def complete(self, system_prompt: str, user_prompt: str):
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return {
            "segments": [
                {"segment_id": "ignored", "start_unit_id": "u001", "end_unit_id": "u001"},
                {"segment_id": "ignored", "start_unit_id": "u002", "end_unit_id": "u002"},
            ]
        }


class VectorEncoder:
    def __init__(self, vectors):
        self.vectors = vectors

    def encode(self, texts, **_):
        scalar = isinstance(texts, str)
        values = [texts] if scalar else list(texts)
        rows = np.asarray([self.vectors[value] for value in values], dtype=np.float32)
        return rows[0] if scalar else rows


class BatchAnchorFakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system_prompt, user_prompt):
        import json

        self.calls += 1
        segments = json.loads(user_prompt.split("INPUT SEGMENTS\n", 1)[1])
        return {"anchors": [
            {
                "segment_id": segment["segment_id"],
                "coarse_candidate": "topic",
                "selected_anchor": f"anchor for {segment['segment_id']}",
                "fine_candidate": "fine topic",
                "reason": "The anchor represents this segment.",
            }
            for segment in segments
        ]}


class ComponentTests(unittest.TestCase):
    def test_anchor_extractor_batches_all_cut_segments_in_one_call(self) -> None:
        llm = BatchAnchorFakeLLM()
        anchors = AnchorExtractor(llm).extract_many([
            {"segment_id": "s1", "text": "First topic."},
            {"segment_id": "s2", "text": "Second topic."},
            {"segment_id": "s3", "text": "Third topic."},
        ])

        self.assertEqual(llm.calls, 1)
        self.assertEqual(
            anchors,
            {
                "s1": "anchor for s1",
                "s2": "anchor for s2",
                "s3": "anchor for s3",
            },
        )

    def test_cutter_makes_globally_unique_ids_and_preserves_coverage(self) -> None:
        cutter = ConversationCutter(CuttingFakeLLM())
        segments = cutter.cut(
            "session_7",
            [
                {"turn": 1, "speaker": "user", "message": "First topic."},
                {"turn": 2, "speaker": "assistant", "message": "Second response."},
            ],
        )
        self.assertEqual([row.segment_id for row in segments], ["session_7_seg001", "session_7_seg002"])
        self.assertEqual(segments[0].text, "[user] First topic.")
        self.assertEqual(segments[0].message_unit_ids, {"m001": ["u001"]})
        self.assertEqual(segments[1].message_unit_ids, {"m002": ["u002"]})

    def test_memory_relations_are_reserved_but_disabled(self) -> None:
        store = MemoryRelationStore()
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            store.add(MemoryRelation("m1", "m2", "related"))
        self.assertEqual(store.to_dict(), {"enabled": False, "relations": []})

    def test_memory_topic_is_base_and_member_consensus_is_only_support(self) -> None:
        encoder = VectorEncoder({
            "broad topic": [0.0, 1.0],
            "matching new segment": [1.0, 0.0],
            "match a": [0.8, 0.6],
            "match b": [0.8, 0.6],
            "match c": [0.8, 0.6],
            "unrelated a": [0.0, 1.0],
            "unrelated b": [0.0, 1.0],
            "unrelated c": [0.0, 1.0],
        })
        active = ActiveGraph(
            encoder,
            DailyGraphConfig(new_memory_threshold=0.5),
        )
        active.add_memory(
            "m-minority",
            "broad topic",
            ["match a", "unrelated a", "unrelated b", "unrelated c"],
        )
        active.add_memory(
            "m-majority",
            "broad topic",
            ["match a", "match b", "match c", "unrelated a"],
        )
        active.add_segment("s1", "matching new segment")
        active.rebuild_edges()

        minority_score = active.similarity("m-minority", "s1")
        majority_score = active.similarity("m-majority", "s1")
        self.assertAlmostEqual(minority_score, 0.20, places=6)
        self.assertGreater(majority_score, minority_score)
        self.assertAlmostEqual(majority_score, 0.60, places=6)
        self.assertTrue(active.graph.has_edge("m-majority", "s1"))

    def test_new_segment_uses_incremental_top_k_without_global_rescan(self) -> None:
        encoder = VectorEncoder({
            "s1": [1.0, 0.0],
            "s2": [0.8, 0.6],
            "s3": [0.99, 0.14],
        })
        active = ActiveGraph(
            encoder,
            DailyGraphConfig(knn_k=1, new_new_threshold=0.5),
        )

        active.add_segment("s1", "s1")
        active.add_segment("s2", "s2")
        self.assertTrue(active.graph.has_edge("s1", "s2"))

        # s3 selects s1, but the existing s1-s2 edge is intentionally not
        # rescanned or evicted by an online incremental update.
        active.add_segment("s3", "s3")
        self.assertTrue(active.graph.has_edge("s1", "s3"))
        self.assertTrue(active.graph.has_edge("s1", "s2"))
        self.assertFalse(active.graph.has_edge("s2", "s3"))

    def test_cut_cross_group_edges_keeps_nodes_active(self) -> None:
        encoder = VectorEncoder({
            "segment one": [1.0, 0.0],
            "segment two": [0.99, 0.01],
        })
        active = ActiveGraph(
            encoder,
            DailyGraphConfig(new_new_threshold=0.2, new_memory_threshold=0.2),
        )
        active.add_segment("s1", "segment one")
        active.add_segment("s2", "segment two")
        self.assertTrue(active.graph.has_edge("s1", "s2"))

        removed = active.cut_cross_group_edges([{"s1"}, {"s2"}])

        self.assertEqual([row["left"] for row in removed], ["s1"])
        self.assertFalse(active.graph.has_edge("s1", "s2"))
        self.assertEqual(active.segment_ids(), {"s1", "s2"})

        restored = ActiveGraph(
            encoder,
            DailyGraphConfig(new_new_threshold=0.2, new_memory_threshold=0.2),
        )
        payload = active.to_dict()
        restored.restore_nodes(payload["nodes"], payload["blocked_edges"])
        self.assertFalse(restored.graph.has_edge("s1", "s2"))

    def test_new_memory_only_connects_to_active_segments(self) -> None:
        encoder = VectorEncoder({
            "memory topic": [1.0, 0.0],
            "segment topic": [1.0, 0.0],
        })
        active = ActiveGraph(encoder, DailyGraphConfig(knn_k=10))

        active.add_memory("m1", "memory topic")
        active.add_memory("m2", "memory topic")
        self.assertFalse(active.graph.has_edge("m1", "m2"))

        active.add_segment("s1", "segment topic")
        self.assertTrue(active.graph.has_edge("m1", "s1"))
        self.assertTrue(active.graph.has_edge("m2", "s1"))

    def test_layered_graphs_are_isolated_and_support_global_removal(self) -> None:
        encoder = VectorEncoder({
            "memory topic": [1.0, 0.0],
            "level zero segment": [1.0, 0.0],
            "level one segment": [1.0, 0.0],
        })
        layered = LayeredActiveGraph(
            encoder,
            DailyGraphConfig(new_new_threshold=0.5, new_memory_threshold=0.5),
        )

        # An L1 memory can be represented in both adjacent boundary graphs.
        layered.add_memory(0, "m1", "memory topic")
        layered.add_segment(0, "s0", "level zero segment")
        layered.add_memory(1, "m1", "memory topic")
        layered.add_segment(1, "s1", "level one segment")

        self.assertEqual(layered.levels(), (0, 1))
        self.assertEqual(layered.memory_ids(), {"m1"})
        self.assertEqual(layered.segment_ids(), {"s0", "s1"})
        self.assertTrue(layered.graph(0).graph.has_edge("m1", "s0"))
        self.assertTrue(layered.graph(1).graph.has_edge("m1", "s1"))
        self.assertNotIn("s1", layered.graph(0).graph)
        self.assertNotIn("s0", layered.graph(1).graph)

        restored = LayeredActiveGraph.from_dict(
            layered.to_dict(), encoder, layered.config
        )
        self.assertEqual(restored.levels(), (0, 1))
        self.assertTrue(restored.graph(0).graph.has_edge("m1", "s0"))
        self.assertTrue(restored.graph(1).graph.has_edge("m1", "s1"))

        restored.remove_nodes({"m1"})
        self.assertNotIn("m1", restored.graph(0).nodes)
        self.assertNotIn("m1", restored.graph(1).nodes)

    def test_cross_level_edges_are_retained_but_excluded_from_peer_view(self) -> None:
        encoder = VectorEncoder({
            "topic": [1.0, 0.0],
        })
        layered = LayeredActiveGraph(
            encoder,
            DailyGraphConfig(
                new_memory_threshold=0.5,
                knn_k=10,
            ),
        )
        layered.add_memory(1, "l1-a", "topic", memory_level=1)
        layered.add_memory(1, "l2-owner", "topic", memory_level=2)
        layered.add_memory(1, "l1-b", "topic", memory_level=1)

        boundary = layered.graph(1)
        self.assertEqual(
            boundary.edge_relation("l1-a", "l2-owner"), "cross_level"
        )
        self.assertTrue(boundary.graph.has_edge("l1-a", "l2-owner"))
        self.assertTrue(boundary.graph.has_edge("l1-a", "l1-b"))

        peer_view = layered.community_graph(1)
        self.assertTrue(peer_view.has_edge("l1-a", "l1-b"))
        self.assertFalse(peer_view.has_edge("l1-a", "l2-owner"))
        self.assertNotIn("l2-owner", set(peer_view.neighbors("l1-a")))


if __name__ == "__main__":
    unittest.main()
