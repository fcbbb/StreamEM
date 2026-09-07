"""Anchor embedding generation with a local JSON cache."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np


def _cache_key(model: str, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{model}:{digest}"


def _load_cache(path: Path, model: str) -> dict[str, list[float]]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("embedding_model") != model:
        raise ValueError(
            f"Embedding cache model mismatch: {payload.get('embedding_model')!r} != {model!r}"
        )
    return {str(key): list(value) for key, value in payload.get("embeddings", {}).items()}


def embed_texts(
    texts: list[str],
    model: str,
    cache_path: Path,
    batch_size: int = 64,
    device: str | None = None,
) -> tuple[np.ndarray, dict[str, int]]:
    """Return row-normalized embeddings and cache statistics."""

    non_empty_texts = [text.strip() for text in texts if isinstance(text, str) and text.strip()]
    unique_texts = list(dict.fromkeys(non_empty_texts))
    if not unique_texts:
        raise ValueError("No non-empty anchor text was provided")
    cache = _load_cache(cache_path, model)
    missing = [text for text in unique_texts if _cache_key(model, text) not in cache]
    if missing:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers is required to embed uncached anchors. "
                "Run `uv sync --extra graph` or provide a complete cache."
            ) from exc
        encoder = SentenceTransformer(model, device=device)
        vectors = encoder.encode(
            missing,
            batch_size=batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=True,
        )
        for text, vector in zip(missing, vectors):
            cache[_cache_key(model, text)] = np.asarray(vector, dtype=np.float64).tolist()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {"embedding_model": model, "embeddings": cache},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    vectors: list[np.ndarray] = []
    dimension = len(cache[_cache_key(model, unique_texts[0])])
    for text in texts:
        if not isinstance(text, str) or not text.strip():
            vectors.append(np.zeros(dimension, dtype=np.float64))
            continue
        vector = np.asarray(cache[_cache_key(model, text)], dtype=np.float64)
        if vector.ndim != 1 or not np.all(np.isfinite(vector)):
            raise ValueError(f"Invalid embedding for anchor: {text!r}")
        norm = np.linalg.norm(vector)
        if norm == 0:
            raise ValueError(f"Zero embedding for anchor: {text!r}")
        vectors.append(vector / norm)
    matrix = np.vstack(vectors)
    return matrix, {"unique_texts": len(unique_texts), "cache_misses": len(missing)}
