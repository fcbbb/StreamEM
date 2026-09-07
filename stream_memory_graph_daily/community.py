from __future__ import annotations

import hashlib
import heapq
from dataclasses import dataclass, field
from typing import Iterable

import networkx as nx

from .config import DailyGraphConfig
from .graph import ActiveGraph


def stable_group_id(node_ids: Iterable[str]) -> str:
    payload = "\x1f".join(sorted(node_ids)).encode("utf-8")
    return "community:" + hashlib.sha1(payload).hexdigest()[:16]


@dataclass
class PlannedCommunity:
    community_id: str
    memory_ids: set[str] = field(default_factory=set)
    segment_ids: set[str] = field(default_factory=set)
    source: str = "detected"

    @property
    def node_ids(self) -> set[str]:
        return self.memory_ids | self.segment_ids


@dataclass
class CommunityPlan:
    communities: list[PlannedCommunity]
    boundaries: dict[str, dict[str, float]]
    cannot_link_memory_pairs: set[tuple[str, str]]
    detected_communities: list[set[str]]
    affected_node_ids: set[str]


class ConstrainedCommunityPlanner:
    """Detect communities, then enforce fixed memory seeds and cannot-link."""

    def __init__(self, config: DailyGraphConfig) -> None:
        self.config = config

    def _detect(self, graph: nx.Graph, nodes: set[str] | None = None) -> list[set[str]]:
        selected = set(graph.nodes) if nodes is None else set(nodes)
        if not selected:
            return []
        induced = graph.subgraph(selected).copy()
        if induced.number_of_edges() == 0:
            return [{node_id} for node_id in sorted(selected)]
        try:
            import igraph as ig  # type: ignore
            import leidenalg  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Leiden requires igraph and leidenalg. Run `uv sync --extra graph`."
            ) from exc

        node_ids = sorted(selected)
        index = {node_id: index for index, node_id in enumerate(node_ids)}
        edge_rows = [
            (index[left], index[right], float(data.get("weight", 1.0)))
            for left, right, data in induced.edges(data=True)
        ]
        leiden_graph = ig.Graph(
            n=len(node_ids), edges=[(left, right) for left, right, _ in edge_rows]
        )
        leiden_graph.es["weight"] = [weight for _, _, weight in edge_rows]
        partition = leidenalg.find_partition(
            leiden_graph,
            leidenalg.RBConfigurationVertexPartition,
            weights="weight",
            resolution_parameter=self.config.resolution,
            seed=self.config.community_seed,
            n_iterations=-1,
        )
        return [{node_ids[index] for index in group} for group in partition]

    def _seed_support(
        self,
        active: ActiveGraph,
        nodes: set[str],
        memory_id: str,
        all_memories: set[str],
    ) -> dict[str, float]:
        """Maximum decayed edge-product path from one fixed memory seed."""

        support = {memory_id: 1.0}
        hops = {memory_id: 0}
        pending: list[tuple[float, int, str]] = [(-1.0, 0, memory_id)]
        while pending:
            negative_score, hop, node_id = heapq.heappop(pending)
            score = -negative_score
            if score + 1e-12 < support.get(node_id, 0.0):
                continue
            if hop >= self.config.max_assignment_hops:
                continue
            for neighbour in active.graph.neighbors(node_id):
                if neighbour not in nodes:
                    continue
                if neighbour in all_memories and neighbour != memory_id:
                    continue
                weight = float(active.graph[node_id][neighbour].get("weight", 0.0))
                candidate = score * weight * self.config.path_decay
                next_hop = hop + 1
                if candidate > support.get(neighbour, 0.0) + 1e-12:
                    support[neighbour] = candidate
                    hops[neighbour] = next_hop
                    heapq.heappush(pending, (-candidate, next_hop, neighbour))
        return support

    def _new_groups(
        self, active: ActiveGraph, segment_ids: set[str], source: str
    ) -> list[PlannedCommunity]:
        return [
            PlannedCommunity(stable_group_id(group), set(), group, source)
            for group in self._detect(active.graph, segment_ids)
        ]

    def repair_multi_memory(
        self,
        active: ActiveGraph,
        nodes: set[str],
        pre_boundary: set[str],
    ) -> tuple[list[PlannedCommunity], dict[str, dict[str, float]]]:
        memory_ids = nodes & active.memory_ids()
        segment_ids = (nodes & active.segment_ids()) - pre_boundary
        supports_by_memory = {
            memory_id: self._seed_support(active, nodes - pre_boundary, memory_id, memory_ids)
            for memory_id in memory_ids
        }
        assigned = {memory_id: set() for memory_id in memory_ids}
        unseeded: set[str] = set()
        boundaries: dict[str, dict[str, float]] = {}
        for segment_id in sorted(segment_ids):
            scores = {
                memory_id: supports_by_memory[memory_id].get(segment_id, 0.0)
                for memory_id in memory_ids
            }
            ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
            best_memory, best_score = ranked[0]
            second_score = ranked[1][1] if len(ranked) > 1 else 0.0
            if best_score < self.config.assignment_min_support:
                unseeded.add(segment_id)
            elif (
                second_score >= self.config.assignment_min_support
                and best_score - second_score < self.config.assignment_margin
            ):
                boundaries[segment_id] = dict(ranked)
            else:
                assigned[best_memory].add(segment_id)

        groups = [
            PlannedCommunity(
                stable_group_id({memory_id, *segments}),
                {memory_id},
                segments,
                "fixed_seed_split",
            )
            for memory_id, segments in sorted(assigned.items())
            if segments
        ]
        groups.extend(self._new_groups(active, unseeded, "unseeded_new_topic"))
        return groups, boundaries

    def plan(
        self, active: ActiveGraph, focus_node_ids: set[str] | None = None
    ) -> CommunityPlan:
        boundaries: dict[str, dict[str, float]] = {}
        cannot_link: set[tuple[str, str]] = set()
        planned: list[PlannedCommunity] = []
        memory_nodes = active.memory_ids()
        segment_nodes = active.segment_ids()

        # Enforce the fixed-seed rule over connected components, not only over
        # raw Leiden output. Leiden is free to place a bridge with one seed
        # even when that bridge is almost equally close to another seed. A
        # connected component exposes every possible memory bridge to the
        # constrained repair pass. Path products let later supporting evidence
        # resolve a segment that was previously boundary.
        all_components = [set(group) for group in nx.connected_components(active.graph)]
        if focus_node_ids is None:
            components = all_components
        else:
            focus = set(focus_node_ids) & set(active.graph.nodes)
            components = [group for group in all_components if group & focus]
        affected_node_ids = set().union(*components) if components else set()
        detected = self._detect(active.graph, affected_node_ids)
        for component in components:
            memories = component & memory_nodes
            if len(memories) > 1:
                for left in sorted(memories):
                    for right in sorted(memories):
                        if left < right:
                            cannot_link.add((left, right))
                repaired, new_boundaries = self.repair_multi_memory(
                    active, component, set()
                )
                planned.extend(repaired)
                boundaries.update(new_boundaries)
                continue
            # With zero or one memory seed, ordinary Leiden can still split a
            # weakly connected component into independent new-topic groups.
            for group in self._detect(active.graph, component):
                group_memories = group & memory_nodes
                segments = group & segment_nodes
                if segments:
                    planned.append(
                        PlannedCommunity(
                            stable_group_id(group_memories | segments),
                            group_memories,
                            segments,
                            "detected",
                        )
                    )

        # An ambiguous node may expose a memory pair even when Leiden placed
        # those memories in separate communities. Persist that cannot-link too.
        for scores in boundaries.values():
            candidates = [
                memory_id
                for memory_id, score in scores.items()
                if score >= self.config.assignment_min_support
            ]
            for index, left in enumerate(sorted(candidates)):
                for right in sorted(candidates)[index + 1 :]:
                    cannot_link.add((left, right))

        planned.sort(key=lambda group: group.community_id)
        return CommunityPlan(
            planned, boundaries, cannot_link, detected, affected_node_ids
        )
