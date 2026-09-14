from __future__ import annotations

import hashlib
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
    """Detect communities and assign multi-memory segments before purification."""

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

    def detect_nodes(
        self, active: ActiveGraph, nodes: set[str] | None = None
    ) -> list[set[str]]:
        """Detect communities using only edges valid for this graph level."""

        selected = set(active.graph.nodes) if nodes is None else set(nodes)
        return self._detect(active.community_graph(selected), selected)

    def _new_groups(
        self, active: ActiveGraph, segment_ids: set[str], source: str
    ) -> list[PlannedCommunity]:
        community_graph = active.community_graph(segment_ids)
        return [
            PlannedCommunity(stable_group_id(group), set(), group, source)
            for group in self._detect(community_graph, segment_ids)
        ]

    def repair_multi_memory(
        self,
        active: ActiveGraph,
        nodes: set[str],
        pre_boundary: set[str],
    ) -> tuple[list[PlannedCommunity], dict[str, dict[str, float]]]:
        """Assign every segment to its most similar memory.

        The method name is retained for compatibility with callers and saved
        workflow code.  Multi-memory communities no longer use path support,
        minimum-support gates, or boundary margins.  Each memory that receives
        at least one segment becomes its own purification input; memories with
        no assigned segment are omitted.
        """

        memory_ids = nodes & active.memory_ids()
        segment_ids = (nodes & active.segment_ids()) - pre_boundary
        assigned = {memory_id: set() for memory_id in memory_ids}

        if not memory_ids:
            return self._new_groups(active, segment_ids, "unseeded_new_topic"), {}

        for segment_id in sorted(segment_ids):
            scores = {
                memory_id: active.similarity(memory_id, segment_id)
                for memory_id in memory_ids
            }
            ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
            assigned[ranked[0][0]].add(segment_id)

        groups = [
            PlannedCommunity(
                stable_group_id({memory_id, *segments}),
                {memory_id},
                segments,
                "most_similar_memory",
            )
            for memory_id, segments in sorted(assigned.items())
            if segments
        ]
        return groups, {}

    def plan(
        self, active: ActiveGraph, focus_node_ids: set[str] | None = None
    ) -> CommunityPlan:
        community_graph = active.community_graph()
        boundaries: dict[str, dict[str, float]] = {}
        cannot_link: set[tuple[str, str]] = set()
        planned: list[PlannedCommunity] = []
        memory_nodes = active.memory_ids()
        segment_nodes = active.segment_ids()

        # Handle multi-memory connected components before purification. A
        # connected component exposes every segment that could be associated
        # with more than one memory; repair_multi_memory assigns each one to
        # the most similar memory and returns one purification group per
        # memory that received a segment.
        all_components = [set(group) for group in nx.connected_components(community_graph)]
        if focus_node_ids is None:
            components = all_components
        else:
            focus = set(focus_node_ids) & set(active.graph.nodes)
            components = [group for group in all_components if group & focus]
        affected_node_ids = set().union(*components) if components else set()
        detected = self._detect(community_graph, affected_node_ids)
        for component in components:
            memories = component & memory_nodes
            if len(memories) > 1:
                repaired, new_boundaries = self.repair_multi_memory(
                    active, component, set()
                )
                planned.extend(repaired)
                boundaries.update(new_boundaries)
                continue
            # With zero or one memory seed, ordinary Leiden can still split a
            # weakly connected component into independent new-topic groups.
            for group in self._detect(community_graph, component):
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

        planned.sort(key=lambda group: group.community_id)
        return CommunityPlan(
            planned, boundaries, cannot_link, detected, affected_node_ids
        )
