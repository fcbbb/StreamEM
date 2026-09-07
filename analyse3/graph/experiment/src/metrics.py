"""Community and graph evaluation metrics."""

from __future__ import annotations

from collections import Counter
from typing import Any

import networkx as nx

from .graph_builder import Edge


def graph_stats(node_count: int, edges: list[Edge]) -> dict[str, Any]:
    graph = nx.Graph()
    graph.add_nodes_from(range(node_count))
    graph.add_weighted_edges_from(edges, weight="weight")
    degrees = [degree for _, degree in graph.degree()]
    components = list(nx.connected_components(graph))
    largest = max((len(component) for component in components), default=0)
    return {
        "node_count": node_count,
        "edge_count": graph.number_of_edges(),
        "edge_density": (2 * graph.number_of_edges() / (node_count * (node_count - 1))) if node_count > 1 else 0.0,
        "mean_degree": sum(degrees) / node_count if node_count else 0.0,
        "median_degree": sorted(degrees)[len(degrees) // 2] if degrees else 0.0,
        "max_degree": max(degrees, default=0),
        "isolated_node_count": sum(degree == 0 for degree in degrees),
        "isolated_node_fraction": sum(degree == 0 for degree in degrees) / node_count if node_count else 0.0,
        "connected_component_count": len(components),
        "largest_component_size": largest,
        "largest_component_fraction": largest / node_count if node_count else 0.0,
    }


def _binary_metrics(y_true: list[bool], y_pred: list[bool]) -> dict[str, Any]:
    tp = sum(true and pred for true, pred in zip(y_true, y_pred))
    fp = sum((not true) and pred for true, pred in zip(y_true, y_pred))
    fn = sum(true and (not pred) for true, pred in zip(y_true, y_pred))
    tn = sum((not true) and (not pred) for true, pred in zip(y_true, y_pred))
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall and precision + recall else 0.0
    return {
        "pair_count": len(y_true),
        "positive_count": sum(y_true),
        "negative_count": sum(not value for value in y_true),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "overmerge_rate": fp / (tp + fp) if tp + fp else None,
        "oversplit_rate": fn / (tp + fn) if tp + fn else None,
    }


def community_pair_metrics(
    pair_rows: list[dict[str, Any]],
    node_index: dict[str, int],
    community_labels: list[int],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    predictions: list[dict[str, Any]] = []
    definite_true: list[bool] = []
    definite_pred: list[bool] = []
    relation_counts: Counter[str] = Counter()
    relation_same_community: Counter[str] = Counter()

    for row in pair_rows:
        left = node_index[row["left_segment_id"]]
        right = node_index[row["right_segment_id"]]
        pred = community_labels[left] == community_labels[right]
        relation = row.get("relation") or "unknown"
        relation_counts[relation] += 1
        relation_same_community[relation] += int(pred)
        predictions.append(
            {
                **row,
                "pred_same_community": pred,
                "left_community_id": community_labels[left],
                "right_community_id": community_labels[right],
            }
        )
        if relation != "uncertain":
            definite_true.append(bool(row["same_specific_topic"]))
            definite_pred.append(pred)

    metrics = _binary_metrics(definite_true, definite_pred)
    metrics["uncertain_pair_count"] = relation_counts.get("uncertain", 0)
    metrics["relation_counts"] = dict(sorted(relation_counts.items()))
    metrics["same_community_by_relation"] = {
        relation: {
            "count": relation_counts[relation],
            "same_community_count": relation_same_community[relation],
            "same_community_rate": relation_same_community[relation] / relation_counts[relation],
        }
        for relation in sorted(relation_counts)
    }
    return metrics, predictions


def edge_pair_metrics(
    pair_rows: list[dict[str, Any]],
    node_index: dict[str, int],
    edges: list[Edge],
) -> dict[str, Any]:
    edge_set = {(min(left, right), max(left, right)) for left, right, _ in edges}
    y_true: list[bool] = []
    y_pred: list[bool] = []
    for row in pair_rows:
        left = node_index[row["left_segment_id"]]
        right = node_index[row["right_segment_id"]]
        if row.get("relation") == "uncertain":
            continue
        y_true.append(bool(row["same_specific_topic"]))
        y_pred.append((min(left, right), max(left, right)) in edge_set)
    return _binary_metrics(y_true, y_pred)
