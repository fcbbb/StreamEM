from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

import numpy as np

from stream_memory_graph_daily.config import DailyGraphConfig
from stream_memory_graph_daily.community import CommunityPlan, PlannedCommunity
from stream_memory_graph_daily.models import BoundaryRecord, MemoryRecord, SegmentRecord
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
        if system_prompt.startswith("Conservatively partition one graph community"):
            payload = self._payload(user_prompt)
            return {
                "groups": [
                    {
                        "group_id": "g1",
                        "node_ids": [
                            *[memory["node_id"] for memory in payload["memory_nodes"]],
                            *[segment["node_id"] for segment in payload["segment_nodes"]],
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
        if "extract one structured topic-memory" in system_prompt.lower():
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
                "no_op_reason": None,
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
                "operation": "add",
                "operation_details": {"hidden_evaluation_value": 3.66},
                "conversation": [
                    {
                        "turn": 1,
                        "speaker": "user",
                        "message": "I spent $3.66 on coffee.",
                        "share_memory": True,
                    },
                    {"turn": 2, "speaker": "assistant", "message": "I recorded that."},
                ],
            }
        )

        self.assertEqual(result["cut_segments"], 1)
        self.assertEqual(result["results"][0]["status"], "added")
        self.assertEqual(pipeline.segments["9_seg001"].anchor, "coffee purchase")
        conversation_audit = next(
            row for row in pipeline.stage_audit if row["stage"] == "conversation_input"
        )
        audited_input = json.dumps(conversation_audit["input"])
        self.assertNotIn("share_memory", audited_input)
        self.assertNotIn("operation_details", audited_input)
        self.assertNotIn('"operation"', audited_input)
        self.assertEqual(
            result["segment_mappings"][0]["message_unit_ids"],
            {"m001": ["u001"], "m002": ["u002"]},
        )
        pipeline.finalize()
        self.assertEqual(llm.extractions, 1)
        self.assertEqual(len(pipeline.memories), 1)

    def test_community_purification_can_split_before_memory_extraction(self) -> None:
        class SplittingLLM(MemoryFakeLLM):
            def complete(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
                if system_prompt.startswith("Conservatively partition one graph community"):
                    payload = self._payload(user_prompt)
                    return {
                        "groups": [
                            {"group_id": "g1", "node_ids": [payload["segment_nodes"][0]["node_id"]]},
                            {"group_id": "g2", "node_ids": [payload["segment_nodes"][1]["node_id"]]},
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

        deferred = pipeline.checkpoint(reason="manual", checkpoint_date="2025-06-01")

        self.assertEqual(llm.extractions, 0)
        self.assertEqual(len(pipeline.memories), 0)
        self.assertEqual(
            {pipeline.segments[segment_id].status for segment_id in ("s1", "s2")},
            {"active"},
        )
        self.assertEqual(
            [row["action"] for row in deferred["changes"]],
            ["singleton_kept_active", "singleton_kept_active"],
        )
        self.assertEqual(pipeline.pending_segment_ids, {"s1", "s2"})

        finalized = pipeline.finalize()
        self.assertEqual(llm.extractions, 2)
        self.assertEqual(len(pipeline.memories), 2)
        self.assertEqual(
            {pipeline.segments[segment_id].status for segment_id in ("s1", "s2")},
            {"compressed"},
        )
        self.assertEqual(
            [row["action"] for row in finalized["changes"]],
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

    def test_memory_and_single_segment_are_purified_as_two_nodes(self) -> None:
        class DetachingLLM(MemoryFakeLLM):
            def complete(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
                if system_prompt.startswith("Conservatively partition one graph community"):
                    payload = self._payload(user_prompt)
                    self.asserted_memory_nodes = payload["memory_nodes"]
                    return {
                        "groups": [
                            {"group_id": "keep-memory", "node_ids": [
                                payload["memory_nodes"][0]["node_id"]
                            ]},
                            {"group_id": "new-topic", "node_ids": [
                                payload["segment_nodes"][0]["node_id"]
                            ]},
                        ]
                    }
                return super().complete(system_prompt, user_prompt)

        llm = DetachingLLM()
        pipeline = DailyMemoryGraph(
            llm=llm, encoder=VectorEncoder(self.vectors), config=self.config
        )
        existing = MemoryRecord("m-existing", "Coffee spending", "Existing coffee memory")
        pipeline.memories = {existing.memory_id: existing}
        pipeline.active_graph.add_memory(existing.memory_id, existing.topic)

        class FixedPlanner:
            def plan(self, active: Any, focus_node_ids: set[str] | None = None) -> CommunityPlan:
                return CommunityPlan(
                    communities=[PlannedCommunity(
                        "community:existing-plus-new",
                        {"m-existing"},
                        {"new"},
                        "fixed_test",
                    )],
                    boundaries={},
                    cannot_link_memory_pairs=set(),
                    detected_communities=[{"m-existing", "new"}],
                    affected_node_ids={"m-existing", "new"},
                )

        pipeline.planner = FixedPlanner()
        pipeline.ingest_segment(
            SegmentRecord("new", "A new topic.", "coffee price", "2025-06-01")
        )
        result = pipeline.finalize()

        self.assertEqual(llm.asserted_memory_nodes[0]["node_id"], "m-existing")
        self.assertEqual(llm.fusions, 0)
        self.assertEqual(llm.extractions, 1)
        self.assertEqual(pipeline.segments["new"].status, "compressed")
        self.assertIsNotNone(pipeline.segments["new"].memory_id)
        self.assertNotIn("new", pipeline.active_graph.nodes)
        self.assertEqual(len(pipeline.memories), 2)
        self.assertTrue(any(
            row.get("action") == "llm_call"
            and row.get("stage") == "community_purification"
            for row in pipeline.stage_audit
        ))
        self.assertTrue(any(
            row.get("action") == "edges_cut"
            and row.get("stage") == "community_purification"
            for row in pipeline.stage_audit
        ))

    def test_bridge_segment_is_assigned_to_most_similar_memory(self) -> None:
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

        self.assertEqual(result["boundary_segment_ids"], [])
        self.assertEqual(pipeline.segments["bridge"].status, "compressed")
        self.assertNotIn("bridge", pipeline.active_graph.nodes)
        self.assertEqual(pipeline.cannot_link_memory_pairs, set())
        self.assertEqual(pipeline.memories["m-b"].topic, "topic b")
        self.assertEqual(llm.extractions, 0)
        self.assertEqual(llm.fusions, 1)

    def test_multi_memory_component_assigns_each_segment_by_direct_similarity(self) -> None:
        encoder = VectorEncoder({
            "topic a": [1.0, 0.0],
            "topic b": [0.0, 1.0],
            "segment a": [0.99, 0.10],
            "segment b": [0.10, 0.99],
        })
        pipeline = DailyMemoryGraph(
            llm=MemoryFakeLLM(), encoder=encoder, config=self.config
        )
        pipeline.active_graph.add_memory("m-a", "topic a")
        pipeline.active_graph.add_memory("m-b", "topic b")
        pipeline.active_graph.add_segment("s-a", "segment a")
        pipeline.active_graph.add_segment("s-b", "segment b")
        graph = pipeline.active_graph.graph
        graph.remove_edges_from(list(graph.edges()))

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

    def test_multi_memory_assignment_ignores_bridge_path_support(self) -> None:
        pipeline = DailyMemoryGraph(
            llm=MemoryFakeLLM(), encoder=VectorEncoder({
                "topic a": [1.0, 0.0],
                "topic b": [0.0, 1.0],
                "bridge": [0.8, 0.6],
                "support b": [0.0, 1.0],
            }), config=self.config
        )
        pipeline.active_graph.add_memory("m-a", "topic a")
        pipeline.active_graph.add_memory("m-b", "topic b")
        pipeline.active_graph.add_segment("bridge", "bridge")
        pipeline.active_graph.add_segment("support-b", "support b")
        graph = pipeline.active_graph.graph
        graph.remove_edges_from(list(graph.edges()))
        # The path through support-b strongly favors m-b, but direct topic
        # similarity still assigns bridge to m-a.
        graph.add_edge("m-b", "support-b", weight=0.99)
        graph.add_edge("support-b", "bridge", weight=0.99)

        groups, boundaries = pipeline.planner.repair_multi_memory(
            pipeline.active_graph,
            {"m-a", "m-b", "bridge", "support-b"},
            set(),
        )

        assignments = {
            next(iter(group.memory_ids)): group.segment_ids
            for group in groups
            if group.memory_ids
        }
        self.assertEqual(assignments, {"m-a": {"bridge"}, "m-b": {"support-b"}})
        self.assertEqual(boundaries, {})

    def test_postprocess_stages_run_concurrently_and_commit_in_order(self) -> None:
        class ConcurrentLLM(MemoryFakeLLM):
            def __init__(self) -> None:
                super().__init__()
                self.lock = threading.Lock()
                self.active: dict[str, int] = {}
                self.maximum: dict[str, int] = {}

            def _enter(self, stage: str) -> None:
                with self.lock:
                    active = self.active.get(stage, 0) + 1
                    self.active[stage] = active
                    self.maximum[stage] = max(self.maximum.get(stage, 0), active)

            def _leave(self, stage: str) -> None:
                with self.lock:
                    self.active[stage] -= 1

            def complete(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
                if system_prompt.startswith("Conservatively partition one graph community"):
                    stage = "purification"
                    self._enter(stage)
                    try:
                        time.sleep(0.03)
                        payload = self._payload(user_prompt)
                        return {
                            "groups": [{
                                "group_id": payload["community_id"],
                                "node_ids": [
                                    *[row["node_id"] for row in payload["memory_nodes"]],
                                    *[row["node_id"] for row in payload["segment_nodes"]],
                                ],
                            }]
                        }
                    finally:
                        self._leave(stage)
                if system_prompt.startswith("You maintain structured memory"):
                    stage = "fusion"
                    self._enter(stage)
                    try:
                        time.sleep(0.03)
                        with self.lock:
                            self.fusions += 1
                        payload = self._payload(user_prompt)
                        existing = payload["existing_memory"]
                        return {
                            "topic": existing["topic"],
                            "summary": existing["summary"],
                            "operations": [],
                            "no_op_reason": "already_present",
                        }
                    finally:
                        self._leave(stage)
                raise AssertionError(f"unexpected prompt: {system_prompt[:60]}")

        llm = ConcurrentLLM()
        config = DailyGraphConfig(postprocess_workers=2)
        pipeline = DailyMemoryGraph(llm=llm, config=config)
        memories = {
            "m-a": MemoryRecord("m-a", "topic a", "summary a"),
            "m-b": MemoryRecord("m-b", "topic b", "summary b"),
        }
        pipeline.memories = memories
        pipeline.active_graph.add_memory("m-a", "topic a")
        pipeline.active_graph.add_memory("m-b", "topic b")
        pipeline.ingest_segment(SegmentRecord("s-a", "A", "topic a", "2025-06-01"))
        pipeline.ingest_segment(SegmentRecord("s-b", "B", "topic b", "2025-06-01"))

        class FixedPlanner:
            def plan(self, active: Any, focus_node_ids: set[str] | None = None) -> CommunityPlan:
                return CommunityPlan(
                    communities=[
                        PlannedCommunity("community:a", {"m-a"}, {"s-a"}, "test"),
                        PlannedCommunity("community:b", {"m-b"}, {"s-b"}, "test"),
                    ],
                    boundaries={},
                    cannot_link_memory_pairs=set(),
                    detected_communities=[{"m-a", "s-a"}, {"m-b", "s-b"}],
                    affected_node_ids={"m-a", "m-b", "s-a", "s-b"},
                )

        pipeline.planner = FixedPlanner()
        result = pipeline.checkpoint(reason="manual", checkpoint_date="2025-06-01")

        self.assertEqual(llm.maximum["purification"], 2)
        self.assertEqual(llm.maximum["fusion"], 2)
        self.assertEqual(llm.fusions, 2)
        self.assertEqual(
            [row["memory_id"] for row in result["changes"]],
            ["m-a", "m-b"],
        )
        self.assertEqual(
            [row["stage_id"] for row in pipeline.stage_audit],
            [f"stage:{index:08d}" for index in range(1, len(pipeline.stage_audit) + 1)],
        )

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

    def test_graph_uses_direct_members_before_full_raw_provenance(self) -> None:
        pipeline = DailyMemoryGraph(
            encoder=VectorEncoder(self.vectors), config=self.config
        )
        raw_segment = SegmentRecord(
            "raw-segment",
            "Raw historical evidence.",
            "raw historical anchor",
            "2025-06-01",
        )
        memory = MemoryRecord(
            memory_id="l2-memory",
            topic="Deployment workflow",
            summary="A higher-level deployment topic.",
            source_segments=[raw_segment.segment_id],
            source_anchors=[raw_segment.anchor],
            level=2,
            direct_members=[
                {
                    "node_id": "l1-memory",
                    "level": 1,
                    "kind": "memory",
                    "representation": "deployment rollback",
                }
            ],
        )
        pipeline.segments[raw_segment.segment_id] = raw_segment

        self.assertEqual(
            pipeline._memory_direct_representations(memory),
            ["deployment rollback"],
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

    def test_retrieval_includes_all_active_memory_and_segment_nodes(self) -> None:
        pipeline = DailyMemoryGraph(encoder=RetrievalEncoder(), config=self.config)
        memory = MemoryRecord(
            "m-existing",
            "Existing topic",
            "An existing compressed memory.",
        )
        active = SegmentRecord(
            "s-active",
            "The user is preparing journal submissions.",
            "journal submissions",
            "2025-06-05",
        )
        boundary = SegmentRecord(
            "s-boundary",
            "The user is considering journal submissions.",
            "journal submissions",
            "2025-06-05",
            status="boundary",
        )
        pipeline.memories[memory.memory_id] = memory
        pipeline.segments[active.segment_id] = active
        pipeline.segments[boundary.segment_id] = boundary
        pipeline.boundaries[boundary.segment_id] = BoundaryRecord(
            segment_id=boundary.segment_id,
            candidate_memories={memory.memory_id: 0.6},
            reason="ambiguous",
            first_seen_date=boundary.event_date,
            last_checked_date=boundary.event_date,
        )
        pipeline.active_graph.add_memory(memory.memory_id, memory.topic)
        pipeline.active_graph.add_segment(active.segment_id, active.anchor)
        pipeline.active_graph.add_segment(boundary.segment_id, boundary.anchor)

        results = pipeline.retrieve("journal submissions", k=10)
        by_id = {row["id"]: row for row in results}

        self.assertEqual(set(by_id), {"m-existing", "segment:s-active", "segment:s-boundary"})
        self.assertEqual(by_id["m-existing"]["kind"], "memory")
        self.assertEqual(by_id["segment:s-active"]["segment_id"], "s-active")
        self.assertEqual(by_id["segment:s-boundary"]["status"], "boundary")


if __name__ == "__main__":
    unittest.main()
