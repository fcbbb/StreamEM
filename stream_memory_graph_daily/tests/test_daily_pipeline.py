from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import numpy as np

from stream_memory_graph_daily.config import DailyGraphConfig
from stream_memory_graph_daily.community import CommunityPlan, PlannedCommunity
from stream_memory_graph_daily.models import MemoryRecord, SegmentRecord
from stream_memory_graph_daily.pipeline import DailyMemoryGraph


class VectorEncoder:
    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors

    def encode(self, texts: Any, **_: Any) -> np.ndarray:
        scalar = isinstance(texts, str)
        values = [texts] if scalar else list(texts)
        rows = np.asarray([self.vectors[str(value)] for value in values], dtype=np.float32)
        return rows[0] if scalar else rows


class RetrievalEncoder:
    def encode(self, texts: Any, **_: Any) -> np.ndarray:
        scalar = isinstance(texts, str)
        values = [texts] if scalar else list(texts)
        rows = []
        for value in values:
            text = str(value).casefold()
            rows.append([
                1.0 if "bridge" in text else 0.0,
                1.0 if "raja ampat" in text else 0.0,
                1.0 if "research paper draft" in text else 0.0,
                1.0 if "journal submissions" in text else 0.0,
            ])
        output = np.asarray(rows, dtype=np.float32)
        return output[0] if scalar else output


class MemoryFakeLLM:
    def __init__(self) -> None:
        self.extractions = 0
        self.fusions = 0

    @staticmethod
    def _payload(user_prompt: str) -> dict[str, Any]:
        return json.loads(user_prompt.split("INPUT DATA\n", 1)[1])

    def complete(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        if system_prompt.startswith("Conservatively split the segments"):
            payload = self._payload(user_prompt)
            return {
                "groups": [
                    {
                        "group_id": "g1",
                        "segment_ids": [
                            segment["segment_id"] for segment in payload["segments"]
                        ],
                    }
                ]
            }
        if system_prompt.startswith("You are segmenting a conversation"):
            units = json.loads(user_prompt.split("conversation:\n\n", 1)[1])
            return {
                "segments": [
                    {
                        "segment_id": "seg001",
                        "start_unit_id": units[0]["unit_id"],
                        "end_unit_id": units[-1]["unit_id"],
                    }
                ]
            }
        if system_prompt.startswith("You need to extract one semantic anchor for each"):
            segments = json.loads(user_prompt.split("INPUT SEGMENTS\n", 1)[1])
            return {"anchors": [
                {
                    "segment_id": segment["segment_id"],
                    "coarse_candidate": "coffee",
                    "selected_anchor": "coffee purchase",
                    "fine_candidate": "single coffee purchase price",
                    "reason": "The exchange records a coffee purchase.",
                }
                for segment in segments
            ]}
        if system_prompt.startswith("Extract one structured topic-memory"):
            self.extractions += 1
            return {
                "topic": "Coffee spending",
                "summary": "The user recorded coffee purchases.",
                "topic_context": [],
                "user_memories": [
                    {"type": "purchase", "content": "The user spent $3.66 on coffee."}
                ],
            }
        if system_prompt.startswith("You maintain structured memory"):
            self.fusions += 1
            payload = self._payload(user_prompt)
            existing = payload["existing_memory"]
            segments = payload["new_group"]["segments"]
            return {
                "topic": existing["topic"],
                "summary": "The user recorded two coffee purchases.",
                "operations": [
                    {
                        "operation": "add",
                        "field": "user_memories",
                        "value": {
                            "type": "purchase",
                            "content": "The user spent $4.20 on coffee.",
                        },
                        "source_segment_ids": [segments[0]["segment_id"]],
                    }
                ],
            }
        raise AssertionError(f"unexpected prompt: {system_prompt[:60]}")


class DailyPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vectors = {
            "coffee purchase": [1.0, 0.0, 0.0],
            "Coffee spending": [1.0, 0.0, 0.0],
            "coffee price": [0.98, 0.05, 0.0],
            "topic a": [1.0, 0.0, 0.0],
            "topic b": [0.0, 1.0, 0.0],
            "ambiguous bridge": [0.71, 0.70, 0.0],
            "a supporting context": [0.92, 0.39, 0.0],
        }
        self.config = DailyGraphConfig(
            knn_k=10,
            new_new_threshold=0.2,
            new_memory_threshold=0.2,
            assignment_min_support=0.3,
            assignment_margin=0.08,
        )

    def test_date_change_checkpoints_and_topic_replaces_raw_community(self) -> None:
        llm = MemoryFakeLLM()
        pipeline = DailyMemoryGraph(
            llm=llm, encoder=VectorEncoder(self.vectors), config=self.config
        )
        pipeline.ingest_segment(
            SegmentRecord(
                "s1", "I spent $3.66 on coffee.", "coffee purchase", "2025-06-01"
            )
        )
        result = pipeline.ingest_segment(
            SegmentRecord(
                "s2", "Today coffee cost $4.20.", "coffee price", "2025-06-02"
            )
        )

        checkpoint = result["checkpoint"]
        self.assertEqual(checkpoint["checkpoint_date"], "2025-06-01")
        self.assertEqual(llm.extractions, 1)
        self.assertEqual(len(pipeline.memories), 1)
        memory_id = next(iter(pipeline.memories))
        self.assertIn("item_id", pipeline.memories[memory_id].user_memories[0])
        self.assertNotIn("s1", pipeline.active_graph.nodes)
        self.assertEqual(pipeline.active_graph.nodes[memory_id].representation, "Coffee spending")
        self.assertEqual(pipeline.segments["s1"].status, "compressed")

        final = pipeline.finalize()
        self.assertEqual(final["checkpoint_date"], "2025-06-02")
        self.assertEqual(llm.fusions, 1)
        self.assertEqual(pipeline.memories[memory_id].source_segments, ["s1", "s2"])
        self.assertEqual(set(pipeline.active_graph.nodes), {memory_id})
        self.assertEqual(pipeline.memories[memory_id].version, 2)
        fusion_change = final["changes"][0]
        self.assertEqual(fusion_change["operations"][0]["source_segment_ids"], ["s2"])
        self.assertTrue(fusion_change["operations"][0]["item_id"].startswith("item:"))
        self.assertNotEqual(fusion_change["operations"][0]["item_id"], "s2")
        self.assertEqual(fusion_change["topic_source_segment_ids"], [])
        self.assertEqual(fusion_change["summary_source_segment_ids"], ["s2"])

    def test_complete_conversation_path_cuts_anchors_and_extracts(self) -> None:
        llm = MemoryFakeLLM()
        pipeline = DailyMemoryGraph(
            llm=llm, encoder=VectorEncoder(self.vectors), config=self.config
        )
        result = pipeline.ingest_conversation(
            {
                "session_id": 9,
                "date": "2025-06-04",
                "conversation": [
                    {"turn": 1, "speaker": "user", "message": "I spent $3.66 on coffee."},
                    {"turn": 2, "speaker": "assistant", "message": "I recorded that."},
                ],
            }
        )

        self.assertEqual(result["cut_segments"], 1)
        self.assertEqual(result["results"][0]["status"], "added")
        self.assertEqual(pipeline.segments["9_seg001"].anchor, "coffee purchase")
        pipeline.finalize()
        self.assertEqual(llm.extractions, 1)
        self.assertEqual(len(pipeline.memories), 1)

    def test_community_purification_can_split_before_memory_extraction(self) -> None:
        class SplittingLLM(MemoryFakeLLM):
            def complete(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
                if system_prompt.startswith("Conservatively split the segments"):
                    payload = self._payload(user_prompt)
                    return {
                        "groups": [
                            {"group_id": "g1", "segment_ids": [payload["segments"][0]["segment_id"]]},
                            {"group_id": "g2", "segment_ids": [payload["segments"][1]["segment_id"]]},
                        ]
                    }
                return super().complete(system_prompt, user_prompt)

        llm = SplittingLLM()
        pipeline = DailyMemoryGraph(
            llm=llm, encoder=VectorEncoder(self.vectors), config=self.config
        )

        class FixedPlanner:
            def plan(self, active: Any, focus_node_ids: set[str] | None = None) -> CommunityPlan:
                segment_ids = active.segment_ids()
                return CommunityPlan(
                    communities=[
                        PlannedCommunity(
                            "community:fixed",
                            set(),
                            set(segment_ids),
                            "fixed_test",
                        )
                    ],
                    boundaries={},
                    cannot_link_memory_pairs=set(),
                    detected_communities=[set(segment_ids)],
                    affected_node_ids=set(segment_ids),
                )

        pipeline.planner = FixedPlanner()
        pipeline.ingest_segment(
            SegmentRecord("s1", "I spent $3.66 on coffee.", "coffee purchase", "2025-06-01")
        )
        pipeline.ingest_segment(
            SegmentRecord("s2", "Today coffee cost $4.20.", "coffee price", "2025-06-01")
        )

        result = pipeline.finalize()

        self.assertEqual(llm.extractions, 2)
        self.assertEqual(len(pipeline.memories), 2)
        self.assertEqual(
            {pipeline.segments[segment_id].status for segment_id in ("s1", "s2")},
            {"compressed"},
        )
        self.assertEqual(
            [row["action"] for row in result["changes"]],
            ["memory_created", "memory_created"],
        )
        purification_actions = {
            row["action"]
            for row in pipeline.stage_audit
            if row["stage"] == "community_purification"
        }
        self.assertIn("llm_call", purification_actions)
        self.assertIn("normalized_result", purification_actions)
        self.assertIn("applied", purification_actions)

    def test_ambiguous_segment_becomes_boundary_without_fusion(self) -> None:
        llm = MemoryFakeLLM()
        pipeline = DailyMemoryGraph(
            llm=llm, encoder=VectorEncoder(self.vectors), config=self.config
        )
        memory_a = MemoryRecord("m-a", "topic a", "A summary")
        memory_b = MemoryRecord("m-b", "topic b", "B summary")
        pipeline.memories = {"m-a": memory_a, "m-b": memory_b}
        pipeline.active_graph.add_memory("m-a", "topic a")
        pipeline.active_graph.add_memory("m-b", "topic b")
        pipeline.ingest_segment(
            SegmentRecord("bridge", "It connects A and B.", "ambiguous bridge", "2025-06-03")
        )

        result = pipeline.finalize()

        self.assertEqual(result["boundary_segment_ids"], ["bridge"])
        self.assertEqual(pipeline.segments["bridge"].status, "boundary")
        self.assertIn("bridge", pipeline.active_graph.nodes)
        self.assertIn(("m-a", "m-b"), pipeline.cannot_link_memory_pairs)
        self.assertEqual(llm.extractions, 0)
        self.assertEqual(llm.fusions, 0)

    def test_fixed_seed_path_support_splits_a_multi_memory_component(self) -> None:
        pipeline = DailyMemoryGraph(
            llm=MemoryFakeLLM(), encoder=VectorEncoder(self.vectors), config=self.config
        )
        pipeline.active_graph.add_memory("m-a", "topic a")
        pipeline.active_graph.add_memory("m-b", "topic b")
        pipeline.active_graph.add_segment("s-a", "coffee purchase")
        pipeline.active_graph.add_segment("s-b", "coffee price")
        graph = pipeline.active_graph.graph
        # Isolate the hand-built path below from the automatic incremental
        # edges created while the nodes were added.
        graph.remove_edges_from(list(graph.edges()))
        graph.add_edge("m-a", "s-a", weight=0.95)
        graph.add_edge("s-a", "s-b", weight=0.55)
        graph.add_edge("s-b", "m-b", weight=0.95)

        groups, boundaries = pipeline.planner.repair_multi_memory(
            pipeline.active_graph, {"m-a", "m-b", "s-a", "s-b"}, set()
        )

        assignments = {
            next(iter(group.memory_ids)): group.segment_ids
            for group in groups
            if group.memory_ids
        }
        self.assertEqual(assignments, {"m-a": {"s-a"}, "m-b": {"s-b"}})
        self.assertEqual(boundaries, {})

    def test_later_structural_support_can_resolve_a_boundary(self) -> None:
        pipeline = DailyMemoryGraph(
            llm=MemoryFakeLLM(), encoder=VectorEncoder(self.vectors), config=self.config
        )
        pipeline.active_graph.add_memory("m-a", "topic a")
        pipeline.active_graph.add_memory("m-b", "topic b")
        pipeline.active_graph.add_segment("bridge", "ambiguous bridge")
        pipeline.active_graph.add_segment("support-a", "a supporting context")
        graph = pipeline.active_graph.graph
        graph.add_edge("m-a", "bridge", weight=0.71)
        graph.add_edge("m-b", "bridge", weight=0.70)
        graph.add_edge("m-a", "support-a", weight=0.95)
        graph.add_edge("support-a", "bridge", weight=0.95)

        groups, boundaries = pipeline.planner.repair_multi_memory(
            pipeline.active_graph,
            {"m-a", "m-b", "bridge", "support-a"},
            set(),
        )

        assigned_to_a = next(group for group in groups if group.memory_ids == {"m-a"})
        self.assertEqual(assigned_to_a.segment_ids, {"bridge", "support-a"})
        self.assertEqual(boundaries, {})

    def test_state_round_trip_rebuilds_topic_graph(self) -> None:
        llm = MemoryFakeLLM()
        pipeline = DailyMemoryGraph(
            llm=llm, encoder=VectorEncoder(self.vectors), config=self.config
        )
        pipeline.ingest_segment(
            SegmentRecord("s1", "I spent $3.66 on coffee.", "coffee purchase", "2025-06-01")
        )
        pipeline.finalize()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            pipeline.save(path)
            restored = DailyMemoryGraph.load(
                path, llm=llm, encoder=VectorEncoder(self.vectors)
            )

        self.assertEqual(restored.stats(), pipeline.stats())
        self.assertEqual(restored.to_dict()["memories"], pipeline.to_dict()["memories"])
        memory_id = next(iter(restored.memories))
        self.assertEqual(restored.active_graph.nodes[memory_id].representation, "Coffee spending")
        self.assertEqual(
            restored.active_graph.nodes[memory_id].member_representations,
            ["coffee purchase"],
        )

    def test_retrieval_fuses_structured_fields_and_entity_channel(self) -> None:
        pipeline = DailyMemoryGraph(
            encoder=RetrievalEncoder(),
            config=self.config,
        )
        bridge = MemoryRecord(
            "m-bridge",
            "Classic films",
            "The user enjoys classic cinema.",
            user_memories=[
                {"type": "movie", "content": "The Bridge on the River Kwai"}
            ],
        )
        paper = MemoryRecord(
            "m-paper",
            "Current tasks",
            "The user has an active research paper draft.",
            user_memories=[
                {"type": "task", "content": "Write research paper draft"}
            ],
        )
        pipeline.memories = {bridge.memory_id: bridge, paper.memory_id: paper}
        pipeline.active_graph.add_memory(bridge.memory_id, bridge.topic)
        pipeline.active_graph.add_memory(paper.memory_id, paper.topic)

        bridge_results = pipeline.retrieve("What do I think of The Bridge on the River Kwai?", 1)
        paper_results = pipeline.retrieve("Write research paper draft", 1)
        task_results = pipeline.retrieve("What tasks remain on my todo list?", 1)

        self.assertEqual(bridge_results[0]["memory_id"], "m-bridge")
        self.assertEqual(paper_results[0]["memory_id"], "m-paper")
        self.assertEqual(task_results[0]["memory_id"], "m-paper")
        self.assertGreater(bridge_results[0]["entity_score"], 0.0)
        self.assertGreater(paper_results[0]["similarity"], 0.0)


if __name__ == "__main__":
    unittest.main()
