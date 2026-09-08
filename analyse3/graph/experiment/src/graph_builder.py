"""Similarity matrix and graph construction rules."""

from __future__ import annotations

from typing import Literal

import numpy as np

Edge = tuple[int, int, float]

GRAPH_MODES = (
    "semantic_only",
    "semantic_keyword",
    "semantic_entity",
    "semantic_keyword_entity",
)


def cosine_matrix(
    embeddings: np.ndarray,
    valid_mask: np.ndarray | None = None,
    pair_mask: np.ndarray | None = None,
) -> np.ndarray:
    """Build a cosine matrix and optionally suppress invalid node pairs."""
    scores = np.asarray(embeddings @ embeddings.T, dtype=np.float64)
    np.fill_diagonal(scores, -np.inf)
    if valid_mask is not None:
        invalid = ~np.asarray(valid_mask, dtype=bool)
        scores[invalid, :] = -np.inf
        scores[:, invalid] = -np.inf
    if pair_mask is not None:
        mask = np.asarray(pair_mask, dtype=bool)
        if mask.shape != scores.shape:
            raise ValueError(
                f"pair_mask shape {mask.shape} does not match scores {scores.shape}"
            )
        scores[~mask] = -np.inf
    return scores


def threshold_edges(scores: np.ndarray, threshold: float) -> list[Edge]:
    edges: list[Edge] = []
    for left in range(scores.shape[0]):
        for right in range(left + 1, scores.shape[0]):
            score = float(scores[left, right])
            if score >= threshold:
                edges.append((left, right, score))
    return edges


def _knn_sets(scores: np.ndarray, k: int) -> list[set[int]]:
    n = scores.shape[0]
    if k <= 0 or k >= n:
        raise ValueError(f"k must be between 1 and n-1; got k={k}, n={n}")
    result: list[set[int]] = []
    for index in range(n):
        order = np.argsort(-scores[index], kind="mergesort")
        result.append(
            set(
                int(value)
                for value in order
                if value != index and np.isfinite(scores[index, value])
            )
        )
        if len(result[-1]) > k:
            result[-1] = set(sorted(result[-1], key=lambda j: (-scores[index, j], j))[:k])
    return result


def knn_edges(
    scores: np.ndarray,
    k: int,
    mode: Literal["union", "mutual"],
    min_similarity: float | None = None,
) -> list[Edge]:
    neighbours = _knn_sets(scores, k)
    edges: list[Edge] = []
    for left in range(scores.shape[0]):
        for right in range(left + 1, scores.shape[0]):
            selected = right in neighbours[left] and (
                mode == "union" or left in neighbours[right]
            )
            score = float(scores[left, right])
            if selected and (min_similarity is None or score >= min_similarity):
                edges.append((left, right, score))
    return edges


def _symmetric_best_match(left: np.ndarray, right: np.ndarray) -> float:
    """Compare two sets of normalized feature embeddings without exact match."""
    if left.size == 0 or right.size == 0:
        return 0.0
    if left.ndim == 1:
        left = left.reshape(1, -1)
    if right.ndim == 1:
        right = right.reshape(1, -1)
    pairwise = np.asarray(left @ right.T, dtype=np.float64)
    forward = float(np.max(pairwise, axis=1).mean())
    backward = float(np.max(pairwise, axis=0).mean())
    return max(0.0, min(1.0, (forward + backward) / 2.0))


def _entity_type(value: str) -> str:
    return value.rsplit("|", 1)[1] if "|" in value else ""


def multichannel_edges(
    semantic_scores: np.ndarray,
    keyword_values: list[list[str]],
    keyword_vectors: list[np.ndarray],
    entity_values: list[list[str]],
    entity_vectors: list[np.ndarray],
    *,
    mode: str,
    top_k: int,
    threshold: float,
) -> tuple[list[Edge], dict[str, int]]:
    """Build edges from the union of per-channel top-k candidates.

    Each enabled channel contributes one normalized similarity. The final
    edge weight is the arithmetic mean over enabled channels that have feature
    values for both nodes. Empty keyword/entity channels are omitted rather
    than treated as negative evidence.
    """
    if mode not in GRAPH_MODES:
        raise ValueError(f"Unsupported multichannel graph mode: {mode}")
    n = semantic_scores.shape[0]
    if not all(len(values) == n for values in (keyword_values, entity_values)):
        raise ValueError("Feature value lists must have the same length as semantic_scores")
    if not all(len(values) == n for values in (keyword_vectors, entity_vectors)):
        raise ValueError("Feature vector lists must have the same length as semantic_scores")
    channels = ["semantic"]
    if mode in {"semantic_keyword", "semantic_keyword_entity"}:
        channels.append("keyword")
    if mode in {"semantic_entity", "semantic_keyword_entity"}:
        channels.append("entity")

    def channel_score(channel: str, left: int, right: int) -> float | None:
        if channel == "semantic":
            value = float(semantic_scores[left, right])
            return None if not np.isfinite(value) else max(0.0, min(1.0, value))
        if channel == "keyword":
            if not keyword_values[left] or not keyword_values[right]:
                return None
            return _symmetric_best_match(keyword_vectors[left], keyword_vectors[right])
        if not entity_values[left] or not entity_values[right]:
            return None
        left_matrix = entity_vectors[left]
        right_matrix = entity_vectors[right]
        if left_matrix.size == 0 or right_matrix.size == 0:
            return None
        allowed = np.asarray([
            [
                not _entity_type(left_value)
                or not _entity_type(right_value)
                or _entity_type(left_value) == _entity_type(right_value)
                for right_value in entity_values[right]
            ]
            for left_value in entity_values[left]
        ], dtype=bool)
        pairwise = np.where(left_matrix @ right_matrix.T >= -1.0, left_matrix @ right_matrix.T, -1.0)
        pairwise = np.where(allowed, pairwise, -1.0)
        if not np.any(allowed):
            return 0.0
        forward = float(np.max(pairwise, axis=1).mean())
        backward = float(np.max(pairwise, axis=0).mean())
        return max(0.0, min(1.0, (forward + backward) / 2.0))

    candidate_pairs: set[tuple[int, int]] = set()
    candidate_counts = {channel: 0 for channel in channels}
    for left in range(n):
        for channel in channels:
            ranked: list[tuple[float, int]] = []
            for right in range(n):
                if left == right:
                    continue
                value = channel_score(channel, left, right)
                if value is not None and value > 0.0:
                    ranked.append((value, right))
            ranked.sort(key=lambda item: (-item[0], item[1]))
            selected = ranked[:max(1, int(top_k))]
            candidate_counts[channel] += len(selected)
            candidate_pairs.update((min(left, right), max(left, right)) for _, right in selected)

    edges: list[Edge] = []
    for left, right in sorted(candidate_pairs):
        values = [
            value
            for channel in channels
            if (value := channel_score(channel, left, right)) is not None
        ]
        if not values:
            continue
        fused = sum(values) / len(values)
        if fused >= threshold:
            edges.append((left, right, fused))
    diagnostics = {
        "candidate_pair_count": len(candidate_pairs),
        "accepted_edge_count": len(edges),
        **{f"candidate_count_{key}": value for key, value in candidate_counts.items()},
    }
    return edges, diagnostics
