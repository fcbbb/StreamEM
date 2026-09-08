"""Run equal-weight semantic/keyword/entity graph ablations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.community import community_stats, detect_communities  # noqa: E402
from src.data import load_anchor_rows, load_pair_labels, load_segment_metadata  # noqa: E402
from src.embeddings import embed_texts  # noqa: E402
from src.features import (
    DEFAULT_ENTITY_LABELS,
    entity_surface,
    extract_entities_batch,
    extract_keywords_batch,
    feature_stats,
)  # noqa: E402
from src.graph_builder import GRAPH_MODES, cosine_matrix, multichannel_edges  # noqa: E402
from src.metrics import community_pair_metrics, edge_pair_metrics, graph_stats  # noqa: E402

FEATURE_VERSION = 7


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/multichannel.json"))
    parser.add_argument("--out-dir", type=Path, default=None)
    return parser.parse_args()


def resolve_config_path(config_path: Path, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def select_sessions(
    rows: list[dict[str, Any]],
    session_filter: dict[str, Any] | None,
    session_id_by_segment: dict[str, int],
) -> tuple[list[dict[str, Any]], list[int]]:
    available = sorted({session_id_by_segment[row["segment_id"]] for row in rows})
    if not session_filter:
        return rows, available
    if session_filter.get("mode") != "first_n":
        raise ValueError(f"Unsupported session filter: {session_filter}")
    count = int(session_filter["count"])
    selected_ids = available[:count]
    if len(selected_ids) != count:
        raise ValueError(f"Requested {count} sessions, only {len(available)} available")
    selected = [
        row for row in rows
        if session_id_by_segment[row["segment_id"]] in selected_ids
    ]
    return selected, selected_ids


def _feature_vectors(
    values_by_node: list[list[str]],
    *,
    model: str,
    cache_path: Path,
    device: str | None,
    semantic_dimension: int,
    entity_values: bool = False,
) -> tuple[list[np.ndarray], dict[str, int]]:
    texts = [
        entity_surface(value) if entity_values else value
        for values in values_by_node
        for value in values
    ]
    unique_texts = list(dict.fromkeys(texts))
    if not unique_texts:
        empty = np.empty((0, semantic_dimension), dtype=np.float64)
        return [empty.copy() for _ in values_by_node], {"unique_texts": 0, "cache_misses": 0}
    matrix, stats = embed_texts(
        unique_texts,
        model=model,
        cache_path=cache_path,
        device=device,
    )
    index = {text: row for text, row in zip(unique_texts, matrix)}
    result: list[np.ndarray] = []
    for values in values_by_node:
        result.append(
            np.vstack([
                index[entity_surface(value) if entity_values else value]
                for value in values
            ]) if values else np.empty((0, semantic_dimension), dtype=np.float64)
        )
    return result, stats


def _summary_lines(rows: list[dict[str, Any]], input_summary: dict[str, Any]) -> str:
    fields = [
        "run_name", "mode", "algorithm", "resolution", "edge_count",
        "community_count", "community_precision", "community_recall",
        "community_f1", "same_topic_recall", "cross_topic_community_rate",
        "edge_precision", "edge_recall", "cross_topic_edge_rate",
        "isolated_node_fraction", "candidate_pair_count",
    ]
    lines = [
        "# Multichannel graph experiment summary", "", "## Input", "", "```json",
        json.dumps(input_summary, ensure_ascii=False, indent=2), "```", "", "## Runs", "",
        "| " + " | ".join(fields) + " |",
        "| " + " | ".join("---" for _ in fields) + " |",
    ]
    for row in sorted(rows, key=lambda item: (-(item.get("community_f1") or 0.0), item["run_name"])):
        lines.append("| " + " | ".join(str(row.get(field, "")) for field in fields) + " |")
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    config_path = args.config.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    paths = config["paths"]
    input_path = resolve_config_path(config_path, paths["anchor_output"])
    segments_path = resolve_config_path(config_path, paths["segments"])
    candidates_path = resolve_config_path(config_path, paths["pair_candidates"])
    gold_path = resolve_config_path(config_path, paths["pair_gold"])
    out_dir = (args.out_dir or resolve_config_path(config_path, config["output_dir"])).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    anchor_field = config.get("anchor_field", "selected_anchor")
    all_anchor_rows = load_anchor_rows(input_path, anchor_field)
    metadata = load_segment_metadata(segments_path)
    session_by_segment = {
        segment_id: int(row["session_id"]) for segment_id, row in metadata.items()
    }
    if config.get("expected_input_count") and len(all_anchor_rows) != config["expected_input_count"]:
        raise ValueError(f"Expected {config['expected_input_count']} input rows, found {len(all_anchor_rows)}")
    anchor_rows, selected_session_ids = select_sessions(
        all_anchor_rows, config.get("session_filter"), session_by_segment
    )
    if config.get("expected_node_count") and len(anchor_rows) != config["expected_node_count"]:
        raise ValueError(f"Expected {config['expected_node_count']} nodes, found {len(anchor_rows)}")

    node_ids = [row["segment_id"] for row in anchor_rows]
    node_index = {segment_id: index for index, segment_id in enumerate(node_ids)}
    pair_rows = [
        row for row in load_pair_labels(
            candidates_path, gold_path, {row["segment_id"] for row in all_anchor_rows}
        )
        if row["left_segment_id"] in node_index and row["right_segment_id"] in node_index
    ]
    anchor_texts = [str(row.get(anchor_field) or "") for row in anchor_rows]
    segment_texts = [str(metadata[segment_id].get("text") or "") for segment_id in node_ids]
    valid_mask = np.asarray([bool(text.strip()) for text in anchor_texts], dtype=bool)

    feature_config = config.get("features", {})
    max_keywords = int(feature_config.get("max_keywords", 5))
    max_entities = int(feature_config.get("max_entities", 5))
    entity_labels = frozenset(
        feature_config.get("entity_labels", DEFAULT_ENTITY_LABELS)
    )
    feature_cache_path = out_dir / "segment_features.jsonl"
    feature_rows: list[dict[str, Any]] = []
    if feature_cache_path.exists():
        cached_rows = [
            json.loads(line)
            for line in feature_cache_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        cached_by_id = {str(row["segment_id"]): row for row in cached_rows}
        if (
            set(cached_by_id) == set(node_ids)
            and all(row.get("feature_version") == FEATURE_VERSION for row in cached_rows)
            and all(row.get("keyword_method") == "keybert" for row in cached_rows)
            and all(row.get("entity_method") == "spacy" for row in cached_rows)
        ):
            feature_rows = [cached_by_id[segment_id] for segment_id in node_ids]
    if not feature_rows:
        keyword_rows, keyword_method = extract_keywords_batch(
            segment_texts,
            model_name=config["embedding"]["model"],
            max_keywords=max_keywords,
        )
        entity_rows, entity_method = extract_entities_batch(
            segment_texts,
            max_entities=max_entities,
            allowed_labels=entity_labels,
        )
        if keyword_method != "keybert" or entity_method != "spacy":
            raise RuntimeError(
                "Formal multichannel experiments require KeyBERT and a spaCy NER model. "
                f"Got keyword_method={keyword_method!r}, entity_method={entity_method!r}. "
                "Run `uv sync --extra graph --extra nlp-trf` and retry."
            )
        for segment_id, keywords, entities in zip(node_ids, keyword_rows, entity_rows):
            feature_rows.append({
                "segment_id": segment_id,
                "feature_version": FEATURE_VERSION,
                "keywords": keywords[:max_keywords],
                "entities": entities[:max_entities],
                "keyword_method": keyword_method,
                "entity_method": entity_method,
            })
        write_jsonl(feature_cache_path, feature_rows)

    semantic_embeddings, embedding_stats = embed_texts(
        anchor_texts,
        model=config["embedding"]["model"],
        cache_path=out_dir / "embedding_cache.json",
        batch_size=int(config["embedding"].get("batch_size", 64)),
        device=config["embedding"].get("device"),
    )
    keyword_values = [row["keywords"] for row in feature_rows]
    entity_values = [row["entities"] for row in feature_rows]
    keyword_vectors, keyword_embedding_stats = _feature_vectors(
        keyword_values,
        model=config["embedding"]["model"],
        cache_path=out_dir / "feature_embedding_cache.json",
        device=config["embedding"].get("device"),
        semantic_dimension=semantic_embeddings.shape[1],
    )
    entity_vectors, entity_embedding_stats = _feature_vectors(
        entity_values,
        model=config["embedding"]["model"],
        cache_path=out_dir / "feature_embedding_cache.json",
        device=config["embedding"].get("device"),
        semantic_dimension=semantic_embeddings.shape[1],
        entity_values=True,
    )
    semantic_scores = cosine_matrix(semantic_embeddings, valid_mask)
    np.save(out_dir / "semantic_similarity_matrix.npy", semantic_scores)

    input_summary = {
        "anchor_output": str(input_path),
        "segments": str(segments_path),
        "anchor_field": anchor_field,
        "input_row_count": len(all_anchor_rows),
        "node_count": len(node_ids),
        "selected_session_ids": selected_session_ids,
        "selected_session_count": len(selected_session_ids),
        "pair_count": len(pair_rows),
        "definite_pair_count": sum(row.get("relation") != "uncertain" for row in pair_rows),
        "embedding_model": config["embedding"]["model"],
        "embedding_stats": embedding_stats,
        "feature_embedding_stats": {
            "keywords": keyword_embedding_stats,
            "entities": entity_embedding_stats,
        },
        "features": feature_stats(feature_rows),
        "graph": config["graph"],
    }
    write_json(out_dir / "input_summary.json", input_summary)
    write_json(out_dir / "run_config.json", config)

    run_rows: list[dict[str, Any]] = []
    for mode in config["graph"]["modes"]:
        edges, graph_diagnostics = multichannel_edges(
            semantic_scores,
            keyword_values,
            keyword_vectors,
            entity_values,
            entity_vectors,
            mode=mode,
            top_k=int(config["graph"]["top_k"]),
            threshold=float(config["graph"]["threshold"]),
        )
        for algorithm in config["community_detection"]["algorithms"]:
            for resolution in config["community_detection"]["resolutions"]:
                run_name = f"multichannel_{mode}__{algorithm}__r{str(resolution).replace('.', 'p')}"
                labels = detect_communities(
                    len(node_ids), edges, algorithm, float(resolution), int(config["seed"])
                )
                community_metrics, pair_predictions = community_pair_metrics(
                    pair_rows, node_index, labels
                )
                edge_metrics = edge_pair_metrics(pair_rows, node_index, edges)
                graph_metrics = graph_stats(len(node_ids), edges)
                run_dir = out_dir / "runs" / run_name
                write_jsonl(
                    run_dir / "communities.jsonl",
                    [
                        {
                            "segment_id": segment_id,
                            "session_id": metadata.get(segment_id, {}).get("session_id"),
                            "anchor": anchor_texts[index],
                            "keywords": keyword_values[index],
                            "entities": entity_values[index],
                            "community_id": labels[index],
                        }
                        for index, segment_id in enumerate(node_ids)
                    ],
                )
                write_jsonl(run_dir / "pair_predictions.jsonl", pair_predictions)
                write_jsonl(
                    run_dir / "edges.jsonl",
                    [
                        {
                            "left_segment_id": node_ids[left],
                            "right_segment_id": node_ids[right],
                            "similarity": weight,
                        }
                        for left, right, weight in edges
                    ],
                )
                metrics = {
                    "run_name": run_name,
                    "mode": mode,
                    "algorithm": algorithm,
                    "resolution": resolution,
                    "graph": config["graph"],
                    "community_metrics": community_metrics,
                    "edge_metrics": edge_metrics,
                    "graph_metrics": graph_metrics,
                    "graph_diagnostics": graph_diagnostics,
                    "community_stats": community_stats(labels),
                }
                write_json(run_dir / "metrics.json", metrics)
                run_rows.append({
                    "run_name": run_name,
                    "mode": mode,
                    "algorithm": algorithm,
                    "resolution": resolution,
                    "edge_count": graph_metrics["edge_count"],
                    "community_count": metrics["community_stats"]["community_count"],
                    "community_precision": community_metrics["precision"],
                    "community_recall": community_metrics["recall"],
                    "community_f1": community_metrics["f1"],
                    "same_topic_recall": community_metrics["same_topic_recall"],
                    "cross_topic_community_rate": community_metrics["cross_topic_community_rate"],
                    "edge_precision": edge_metrics["precision"],
                    "edge_recall": edge_metrics["recall"],
                    "cross_topic_edge_rate": edge_metrics["cross_topic_edge_rate"],
                    "isolated_node_fraction": graph_metrics["isolated_node_fraction"],
                    "candidate_pair_count": graph_diagnostics["candidate_pair_count"],
                })

    write_json(out_dir / "summary.json", {"input": input_summary, "runs": run_rows})
    (out_dir / "summary.md").write_text(
        _summary_lines(run_rows, input_summary), encoding="utf-8"
    )
    print(json.dumps({"output_dir": str(out_dir), "run_count": len(run_rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
