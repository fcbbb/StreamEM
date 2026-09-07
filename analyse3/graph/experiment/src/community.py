"""Community detection adapters."""

from __future__ import annotations

from typing import Any

import networkx as nx

from .graph_builder import Edge


def make_networkx_graph(node_count: int, edges: list[Edge]) -> nx.Graph:
    graph = nx.Graph()
    graph.add_nodes_from(range(node_count))
    graph.add_weighted_edges_from(edges, weight="weight")
    return graph


def _canonicalize(communities: list[set[int]], node_count: int) -> list[int]:
    assigned: dict[int, int] = {}
    ordered = sorted(communities, key=lambda group: min(group) if group else node_count)
    for community_id, group in enumerate(ordered):
        for node in group:
            if node in assigned:
                raise ValueError(f"Node assigned to multiple communities: {node}")
            assigned[node] = community_id
    if set(assigned) != set(range(node_count)):
        raise ValueError("Community detector did not assign every node")
    return [assigned[node] for node in range(node_count)]


def detect_communities(
    node_count: int,
    edges: list[Edge],
    algorithm: str,
    resolution: float,
    seed: int,
) -> list[int]:
    graph = make_networkx_graph(node_count, edges)
    if algorithm == "louvain":
        communities = list(
            nx.community.louvain_communities(
                graph,
                weight="weight",
                resolution=resolution,
                seed=seed,
            )
        )
    elif algorithm == "leiden":
        try:
            import igraph as ig
            import leidenalg
        except ImportError as exc:
            raise RuntimeError(
                "Leiden requires igraph and leidenalg. Run `uv sync --extra graph`."
            ) from exc
        igraph = ig.Graph(n=node_count, edges=[(left, right) for left, right, _ in edges])
        igraph.es["weight"] = [weight for _, _, weight in edges]
        partition = leidenalg.find_partition(
            igraph,
            leidenalg.RBConfigurationVertexPartition,
            weights="weight",
            resolution_parameter=resolution,
            seed=seed,
        )
        communities = [set(group) for group in partition]
    elif algorithm == "connected_components":
        communities = [set(group) for group in nx.connected_components(graph)]
    else:
        raise ValueError(f"Unsupported community algorithm: {algorithm}")
    return _canonicalize(communities, node_count)


def community_stats(labels: list[int]) -> dict[str, Any]:
    counts: dict[int, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    sizes = list(counts.values())
    return {
        "community_count": len(sizes),
        "largest_community_size": max(sizes, default=0),
        "largest_community_fraction": max(sizes, default=0) / len(labels) if labels else None,
        "singleton_community_count": sum(size == 1 for size in sizes),
        "singleton_community_fraction": sum(size == 1 for size in sizes) / len(sizes) if sizes else None,
        "community_sizes": sorted(sizes, reverse=True),
    }
