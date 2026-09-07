"""Similarity matrix and graph construction rules."""

from __future__ import annotations

from typing import Literal

import numpy as np

Edge = tuple[int, int, float]


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
