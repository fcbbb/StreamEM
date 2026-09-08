from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import networkx as nx
import numpy as np

from .config import DailyGraphConfig
from .encoder import Encoder, unit_vector


NodeKind = Literal["segment", "memory"]


@dataclass
class ActiveNode:
    node_id: str
    kind: NodeKind
    representation: str
    member_representations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ActiveGraph:
    """One-layer graph with topic-anchored memory supernodes."""

    def __init__(self, encoder: Encoder, config: DailyGraphConfig) -> None:
        self.encoder = encoder
        self.config = config
        self.nodes: dict[str, ActiveNode] = {}
        self.vectors: dict[str, np.ndarray] = {}
        self.member_vectors: dict[str, np.ndarray] = {}
        self.graph = nx.Graph()
        # Pairs separated by community purification must not be recreated by
        # incremental updates or state restoration.
        self.blocked_edges: set[tuple[str, str]] = set()

    @staticmethod
    def _edge_key(left: str, right: str) -> tuple[str, str]:
        return tuple(sorted((left, right)))

    def add_node(
        self,
        node_id: str,
        kind: NodeKind,
        representation: str,
        member_representations: list[str] | None = None,
    ) -> None:
        node_id = str(node_id).strip()
        representation = str(representation).strip()
        if not node_id or not representation:
            raise ValueError("active graph nodes need a non-empty ID and representation")
        existing = self.nodes.get(node_id)
        if existing is not None and existing.kind != kind:
            raise ValueError(f"cannot change node kind for {node_id}")
        # Preserve one representation per compressed segment. Repeated anchors
        # are separate member votes when calculating supernode consensus.
        members = [
            str(value).strip()
            for value in (member_representations or [])
            if str(value).strip()
        ]
        if kind == "segment" and members:
            raise ValueError("segment nodes cannot have member representations")
        vector = unit_vector(self.encoder.encode([representation])[0])
        self.nodes[node_id] = ActiveNode(node_id, kind, representation, members)
        self.vectors[node_id] = vector
        if members:
            self.member_vectors[node_id] = np.asarray(
                [unit_vector(row) for row in self.encoder.encode(members)],
                dtype=np.float32,
            )
        else:
            self.member_vectors.pop(node_id, None)
        self.graph.add_node(node_id, kind=kind)

    def add_segment(self, segment_id: str, anchor: str) -> None:
        self.add_node(segment_id, "segment", anchor)
        self._refresh_incremental_edges(segment_id)

    def add_memory(
        self, memory_id: str, topic: str, member_representations: list[str] | None = None
    ) -> None:
        self.add_node(memory_id, "memory", topic, member_representations)
        self._refresh_incremental_edges(memory_id)

    def update_memory_topic(
        self, memory_id: str, topic: str, member_representations: list[str] | None = None
    ) -> None:
        node = self.nodes.get(memory_id)
        if node is None or node.kind != "memory":
            raise KeyError(f"unknown memory graph node: {memory_id}")
        members = (
            node.member_representations
            if member_representations is None
            else member_representations
        )
        self.add_memory(memory_id, topic, members)

    def remove_nodes(self, node_ids: set[str] | list[str]) -> None:
        removed = set(node_ids)
        for node_id in node_ids:
            self.nodes.pop(node_id, None)
            self.vectors.pop(node_id, None)
            self.member_vectors.pop(node_id, None)
            if self.graph.has_node(node_id):
                self.graph.remove_node(node_id)
        self.blocked_edges = {
            edge for edge in self.blocked_edges if not (set(edge) & removed)
        }

    def cut_cross_group_edges(self, groups: list[set[str]]) -> list[dict[str, Any]]:
        """Remove stale edges between nodes the purifier placed in different groups.

        Purification is authoritative for the current community.  Keeping an
        old memory-segment or segment-segment bridge would allow the next
        incremental planning pass to immediately reassemble the split groups.
        Nodes themselves remain active; only those stale cross-group edges are
        removed.
        """

        removed: list[dict[str, Any]] = []
        normalized = [set(group) for group in groups if group]
        for index, left_group in enumerate(normalized):
            for right_group in normalized[index + 1 :]:
                for left in sorted(left_group):
                    for right in sorted(right_group):
                        if not self.graph.has_edge(left, right):
                            self.blocked_edges.add(self._edge_key(left, right))
                            continue
                        data = self.graph.get_edge_data(left, right) or {}
                        removed.append(
                            {
                                "left": min(left, right),
                                "right": max(left, right),
                                "weight": float(data.get("weight", 0.0)),
                            }
                        )
                        self.graph.remove_edge(left, right)
                        self.blocked_edges.add(self._edge_key(left, right))
        return removed

    def node_kind(self, node_id: str) -> NodeKind:
        return self.nodes[node_id].kind

    def memory_ids(self) -> set[str]:
        return {node_id for node_id, node in self.nodes.items() if node.kind == "memory"}

    def segment_ids(self) -> set[str]:
        return {node_id for node_id, node in self.nodes.items() if node.kind == "segment"}

    def similarity(self, left: str, right: str) -> float:
        if left == right:
            return 1.0
        topic_score = float(np.dot(self.vectors[left], self.vectors[right]))
        left_kind = self.node_kind(left)
        right_kind = self.node_kind(right)
        if left_kind == right_kind:
            return topic_score
        memory_id = left if left_kind == "memory" else right
        segment_id = right if left_kind == "memory" else left
        members = self.member_vectors.get(memory_id)
        if members is None or len(members) == 0:
            return topic_score
        member_scores = members @ self.vectors[segment_id]
        # The memory topic is the stable semantic identity. Historical member
        # anchors can provide supporting votes, but one accidentally broad or
        # contaminated member must not replace the topic as the base score.
        base_score = topic_score
        supporting_scores = member_scores[
            member_scores >= self.config.new_memory_threshold
        ]
        if len(supporting_scores) == 0:
            return base_score
        coverage = len(supporting_scores) / len(member_scores)
        support_strength = float(np.mean(supporting_scores))
        consensus_score = base_score + (1.0 - base_score) * coverage * support_strength
        return min(1.0, max(-1.0, consensus_score))

    def _threshold(self, left: str, right: str) -> float | None:
        left_kind = self.node_kind(left)
        right_kind = self.node_kind(right)
        if left_kind == right_kind == "memory":
            return None
        if left_kind == right_kind == "segment":
            return self.config.new_new_threshold
        return self.config.new_memory_threshold

    def _incremental_candidates(self, node_id: str) -> list[tuple[float, str]]:
        """Return this node's current thresholded top-k eligible neighbours.

        New segment nodes consider all currently active segments and memories.
        New or updated memory nodes consider only currently active segments;
        memory-memory edges are never candidates.
        """

        node_kind = self.node_kind(node_id)
        candidates: list[tuple[float, str]] = []
        for other_id in sorted(self.nodes):
            if other_id == node_id:
                continue
            if self._edge_key(node_id, other_id) in self.blocked_edges:
                continue
            if node_kind == "memory" and self.node_kind(other_id) != "segment":
                continue
            threshold = self._threshold(node_id, other_id)
            if threshold is None:
                continue
            score = self.similarity(node_id, other_id)
            if score >= threshold:
                candidates.append((score, other_id))
        candidates.sort(key=lambda row: (-row[0], row[1]))
        return candidates[: self.config.knn_k]

    def _refresh_incremental_edges(self, node_id: str) -> None:
        """Refresh only one node's outgoing top-k choices.

        The stored graph is undirected, so each selected choice becomes an
        undirected edge. Existing nodes are not rescanned when a new node
        arrives; this intentionally implements online incremental kNN rather
        than the global top-k graph produced by ``rebuild_edges``.
        """

        if node_id not in self.nodes:
            raise KeyError(f"unknown graph node: {node_id}")
        if not self.graph.has_node(node_id):
            self.graph.add_node(node_id, kind=self.node_kind(node_id))
        for other_id in list(self.graph.neighbors(node_id)):
            self.graph.remove_edge(node_id, other_id)
        for score, other_id in self._incremental_candidates(node_id):
            self.graph.add_edge(node_id, other_id, weight=score)

    def rebuild_edges(self) -> None:
        """Rebuild the global symmetric kNN graph for state restoration."""

        graph = nx.Graph()
        for node_id, node in sorted(self.nodes.items()):
            graph.add_node(node_id, kind=node.kind)
        candidates: dict[str, list[tuple[float, str]]] = {
            node_id: [] for node_id in self.nodes
        }
        ids = sorted(self.nodes)
        pair_scores: dict[tuple[str, str], float] = {}
        for index, left in enumerate(ids):
            for right in ids[index + 1 :]:
                if self._edge_key(left, right) in self.blocked_edges:
                    continue
                threshold = self._threshold(left, right)
                if threshold is None:
                    continue
                score = self.similarity(left, right)
                if score < threshold:
                    continue
                pair_scores[(left, right)] = score
                candidates[left].append((score, right))
                candidates[right].append((score, left))
        selected: set[tuple[str, str]] = set()
        for node_id, rows in candidates.items():
            rows.sort(key=lambda row: (-row[0], row[1]))
            for _, other in rows[: self.config.knn_k]:
                selected.add(tuple(sorted((node_id, other))))
        for left, right in sorted(selected):
            graph.add_edge(left, right, weight=pair_scores[(left, right)])
        self.graph = graph

    def edge_rows(self) -> list[dict[str, Any]]:
        return [
            {"left": left, "right": right, "weight": float(data["weight"])}
            for left, right, data in sorted(self.graph.edges(data=True))
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in sorted(self.nodes.values(), key=lambda n: n.node_id)],
            "edges": self.edge_rows(),
            "blocked_edges": [list(edge) for edge in sorted(self.blocked_edges)],
        }

    def restore_nodes(
        self,
        rows: list[dict[str, Any]],
        blocked_edges: list[list[str]] | None = None,
    ) -> None:
        self.nodes.clear()
        self.vectors.clear()
        self.member_vectors.clear()
        self.graph.clear()
        self.blocked_edges = {
            self._edge_key(str(row[0]), str(row[1]))
            for row in (blocked_edges or [])
            if isinstance(row, list)
            and len(row) == 2
            and str(row[0]).strip()
            and str(row[1]).strip()
            and str(row[0]) != str(row[1])
        }
        for row in rows:
            self.add_node(
                str(row["node_id"]),
                row["kind"],
                str(row["representation"]),
                list(row.get("member_representations", [])),
            )
        self.rebuild_edges()
