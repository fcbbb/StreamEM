"""Run the static graph construction and community detection experiment."""

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
from src.graph_builder import cosine_matrix, knn_edges, threshold_edges  # noqa: E402
from src.metrics import community_pair_metrics, edge_pair_metrics, graph_stats  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/default.json"))
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
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value).replace("|", "\\|")


def write_summary(path: Path, rows: list[dict[str, Any]], input_summary: dict[str, Any]) -> None:
    fields = [
        "run_name", "graph_rule", "algorithm", "resolution", "edge_count",
        "community_count", "community_precision", "community_recall",
        "community_f1", "edge_precision", "edge_recall", "isolated_node_fraction",
        "largest_community_fraction",
    ]
    lines = ["# Graph experiment summary", "", "## Input", "", "```json", json.dumps(input_summary, ensure_ascii=False, indent=2), "```", "", "## Runs", "", "| " + " | ".join(fields) + " |", "| " + " | ".join("---" for _ in fields) + " |"]
    for row in sorted(rows, key=lambda item: (-(item.get("community_f1") or 0.0), item["run_name"])):
        lines.append("| " + " | ".join(fmt(row.get(field)) for field in fields) + " |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_edges(scores: np.ndarray, rule: dict[str, Any]) -> list[tuple[int, int, float]]:
    rule_type = rule["type"]
    if rule_type == "threshold":
        return threshold_edges(scores, float(rule["threshold"]))
    if rule_type == "knn":
        return knn_edges(scores, int(rule["k"]), "union", rule.get("min_similarity"))
    if rule_type == "mutual_knn":
        return knn_edges(scores, int(rule["k"]), "mutual", rule.get("min_similarity"))
    raise ValueError(f"Unsupported graph rule: {rule_type}")


def select_sessions(
    rows: list[dict[str, Any]],
    session_filter: dict[str, Any] | None,
    session_id_by_segment: dict[str, int],
) -> tuple[list[dict[str, Any]], list[int]]:
    """Select a deterministic session window for the graph experiment."""

    available_session_ids = sorted(
        {session_id_by_segment[row["segment_id"]] for row in rows}
    )
    if not session_filter:
        return rows, available_session_ids

    mode = session_filter.get("mode")
    if mode != "first_n":
        raise ValueError(f"Unsupported session filter mode: {mode!r}")
    count = int(session_filter.get("count", 0))
    if count <= 0:
        raise ValueError("session_filter.count must be a positive integer")
    selected_session_ids = available_session_ids[:count]
    if len(selected_session_ids) < count:
        raise ValueError(
            f"Requested first {count} sessions, but only "
            f"{len(selected_session_ids)} are available"
        )
    selected = [
        row
        for row in rows
        if session_id_by_segment[row["segment_id"]] in selected_session_ids
    ]
    return selected, selected_session_ids


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
    session_id_by_segment = {
        segment_id: int(row["session_id"])
        for segment_id, row in metadata.items()
    }
    if config.get("expected_input_count") and len(all_anchor_rows) != config["expected_input_count"]:
        raise ValueError(
            f"Expected {config['expected_input_count']} input rows, found {len(all_anchor_rows)}"
        )
    # Keep empty-anchor segments as nodes. They receive no semantic edges and
    # let the end-to-end evaluation expose missing anchor extraction.
    anchor_rows, selected_session_ids = select_sessions(
        all_anchor_rows, config.get("session_filter"), session_id_by_segment
    )
    if config.get("expected_node_count") and len(anchor_rows) != config["expected_node_count"]:
        raise ValueError(
            f"Expected {config['expected_node_count']} nodes, found {len(anchor_rows)}"
        )
    node_ids = [row["segment_id"] for row in anchor_rows]
    node_index = {segment_id: index for index, segment_id in enumerate(node_ids)}
    all_pair_rows = load_pair_labels(
        candidates_path, gold_path, {row["segment_id"] for row in all_anchor_rows}
    )
    selected_node_ids = set(node_ids)
    pair_rows = [
        row
        for row in all_pair_rows
        if row["left_segment_id"] in selected_node_ids
        and row["right_segment_id"] in selected_node_ids
    ]
    texts = [row.get(anchor_field) or "" for row in anchor_rows]
    valid_anchor_mask = np.asarray([bool(text.strip()) for text in texts], dtype=bool)
    pair_mask = None
    if config.get("target_type_gate", False):
        target_types = [row.get("target_type") for row in anchor_rows]
        # Missing types remain mutually compatible for backward compatibility;
        # typed targeted-v2 nodes may only connect to the same target type.
        pair_mask = np.asarray(
            [
                [
                    left_type is None
                    or right_type is None
                    or left_type == right_type
                    for right_type in target_types
                ]
                for left_type in target_types
            ],
            dtype=bool,
        )

    cache_path = out_dir / "embedding_cache.json"
    embeddings, embedding_stats = embed_texts(
        texts,
        model=config["embedding"]["model"],
        cache_path=cache_path,
        batch_size=int(config["embedding"].get("batch_size", 64)),
        device=config["embedding"].get("device"),
    )
    scores = cosine_matrix(embeddings, valid_anchor_mask, pair_mask)
    np.save(out_dir / "similarity_matrix.npy", scores)

    algorithm_configs = config["community_detection"]
    run_rows: list[dict[str, Any]] = []
    input_summary = {
        "anchor_output": str(input_path),
        "anchor_field": anchor_field,
        "target_type_gate": bool(config.get("target_type_gate", False)),
        "input_row_count": len(all_anchor_rows),
        "node_count": len(node_ids),
        "available_session_count": len(
            {session_id_by_segment[row["segment_id"]] for row in all_anchor_rows}
        ),
        "selected_session_ids": selected_session_ids,
        "selected_session_count": len(selected_session_ids),
        "pair_count": len(pair_rows),
        "non_empty_anchor_count": int(valid_anchor_mask.sum()),
        "empty_anchor_count": int((~valid_anchor_mask).sum()),
        "definite_pair_count": sum(row.get("relation") != "uncertain" for row in pair_rows),
        "embedding_model": config["embedding"]["model"],
        "embedding_stats": embedding_stats,
    }
    write_json(out_dir / "input_summary.json", input_summary)
    write_json(out_dir / "run_config.json", config)

    for rule in config["graph_rules"]:
        edges = build_edges(scores, rule)
        for algorithm in algorithm_configs["algorithms"]:
            for resolution in algorithm_configs["resolutions"]:
                run_name = rule["name"] + "__" + algorithm + "__r" + str(resolution).replace(".", "p")
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
                            "anchor": texts[index],
                            "core_target": anchor_rows[index].get("core_target"),
                            "aspect": anchor_rows[index].get("aspect"),
                            "target_type": anchor_rows[index].get("target_type"),
                            "target_name": anchor_rows[index].get("target_name"),
                            "facet": anchor_rows[index].get("facet"),
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
                combined = {
                    "run_name": run_name,
                    "graph_rule": rule,
                    "algorithm": algorithm,
                    "resolution": resolution,
                    "embedding_model": config["embedding"]["model"],
                    "community_metrics": community_metrics,
                    "edge_metrics": edge_metrics,
                    "graph_metrics": graph_metrics,
                    "community_stats": community_stats(labels),
                }
                write_json(run_dir / "metrics.json", combined)
                run_rows.append(
                    {
                        "run_name": run_name,
                        "graph_rule": rule["name"],
                        "algorithm": algorithm,
                        "resolution": resolution,
                        "edge_count": graph_metrics["edge_count"],
                        "community_count": combined["community_stats"]["community_count"],
                        "community_precision": community_metrics["precision"],
                        "community_recall": community_metrics["recall"],
                        "community_f1": community_metrics["f1"],
                        "edge_precision": edge_metrics["precision"],
                        "edge_recall": edge_metrics["recall"],
                        "isolated_node_fraction": graph_metrics["isolated_node_fraction"],
                        "largest_community_fraction": combined["community_stats"]["largest_community_fraction"],
                    }
                )

    write_json(out_dir / "summary.json", {"input": input_summary, "runs": run_rows})
    write_summary(out_dir / "summary.md", run_rows, input_summary)
    print(json.dumps({"output_dir": str(out_dir), "run_count": len(run_rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
