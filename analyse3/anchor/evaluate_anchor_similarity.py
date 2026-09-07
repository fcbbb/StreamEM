#!/usr/bin/env python3
"""Evaluate semantic-anchor similarity against pair gold labels.

The input is one or more JSONL files containing ``segment_id`` and
the configured anchor field.  Each method is evaluated independently, so its
similarity threshold is never shared with another method.  Targeted-v2 output
should normally be evaluated with ``--anchor-field cluster_anchor``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parent
DEFAULT_BASE = ROOT / "artifacts" / "anchor_dataset_v1"
DEFAULT_PILOT = DEFAULT_BASE / "pilot_v1"
DEFAULT_PAIRS = DEFAULT_PILOT / "pilot_pair_candidates.jsonl"
DEFAULT_GOLD = DEFAULT_PILOT / "pilot_pair_gold_labels.jsonl"
DEFAULT_SEGMENT_GOLD = DEFAULT_BASE / "segment_gold_labels.jsonl"
DEFAULT_OUT = DEFAULT_PILOT / "anchor_similarity_eval_v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--method",
        action="append",
        required=True,
        metavar="NAME=JSONL",
        help="Method name and anchor output file; repeat for each method",
    )
    parser.add_argument("--pairs", type=Path, default=DEFAULT_PAIRS)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--segment-gold", type=Path, default=DEFAULT_SEGMENT_GOLD)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--anchor-field",
        default="selected_anchor",
        help="Output field to embed and compare (use cluster_anchor for targeted_v2)",
    )
    parser.add_argument(
        "--anchor-part",
        choices=("full", "before_em_dash"),
        default="full",
        help="Optionally evaluate only the text before the first em dash",
    )
    parser.add_argument("--embedding-model", default="all-MiniLM-L6-v2")
    parser.add_argument("--embedding-batch-size", type=int, default=64)
    parser.add_argument("--device", default=None, help="sentence-transformers device, e.g. cpu or cuda")
    parser.add_argument(
        "--precision-target",
        type=float,
        default=0.95,
        help="Target precision used to select the high-precision error-analysis threshold",
    )
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def parse_methods(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"--method must look like NAME=JSONL: {value}")
        name, raw_path = value.split("=", 1)
        name = name.strip()
        path = Path(raw_path.strip())
        if not name or name in result:
            raise SystemExit(f"Duplicate or empty method name: {name!r}")
        if not path.exists():
            raise SystemExit(f"Anchor output does not exist: {path}")
        result[name] = path
    return result


def parse_anchor_outputs(
    path: Path, anchor_field: str, anchor_part: str
) -> dict[str, dict[str, Any]]:
    rows = load_jsonl(path)
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        sid = row.get("segment_id")
        if not sid:
            raise ValueError(f"Missing segment_id in {path}")
        if sid in by_id:
            raise ValueError(f"Duplicate segment_id {sid} in {path}")
        if row.get("status") not in (None, "success"):
            raise ValueError(
                f"{path} contains non-success row for {sid}: {row.get('status')}; "
                "use a complete output file"
            )
        anchor = row.get(anchor_field)
        if anchor is not None and (not isinstance(anchor, str) or not anchor.strip()):
            raise ValueError(f"Invalid {anchor_field} for {sid} in {path}")
        eval_anchor = anchor.strip() if isinstance(anchor, str) else anchor
        if eval_anchor is not None and anchor_part == "before_em_dash":
            eval_anchor = eval_anchor.split("—", 1)[0].strip()
        by_id[sid] = {**row, "_evaluation_anchor": eval_anchor}
    return by_id


def parse_embedding_cache(path: Path, model: str) -> dict[str, list[float]]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("embedding_model") != model:
        raise ValueError(
            f"Embedding cache model mismatch in {path}: "
            f"{raw.get('embedding_model')!r} != {model!r}"
        )
    return {str(k): list(v) for k, v in raw.get("embeddings", {}).items()}


def cache_key(model: str, text: str) -> str:
    return f"{model}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def get_embeddings(
    texts: list[str],
    model: str,
    cache: dict[str, list[float]],
    cache_path: Path,
    batch_size: int,
    device: str | None,
) -> dict[str, np.ndarray]:
    missing = [text for text in texts if cache_key(model, text) not in cache]
    if missing:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise SystemExit(
                "Missing sentence-transformers. Install it with `uv sync --extra anchor-eval`."
            ) from exc
        print(f"Loading local sentence-transformers model: {model}")
        encoder = SentenceTransformer(model, device=device)
        vectors = encoder.encode(
            missing,
            batch_size=batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=True,
        )
        for text, vector in zip(missing, vectors):
            cache[cache_key(model, text)] = vector.astype(np.float64).tolist()
        print(f"Embedded {len(missing)} anchors locally")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"embedding_model": model, "embeddings": cache}, ensure_ascii=False),
            encoding="utf-8",
        )

    vectors: dict[str, np.ndarray] = {}
    for text in texts:
        vector = np.asarray(cache[cache_key(model, text)], dtype=np.float64)
        norm = np.linalg.norm(vector)
        if vector.ndim != 1 or not np.isfinite(norm) or norm == 0:
            raise ValueError(f"Invalid embedding for anchor: {text!r}")
        vectors[text] = vector / norm
    return vectors


def average_precision(labels: np.ndarray, scores: np.ndarray) -> float | None:
    positives = int(labels.sum())
    if positives == 0:
        return None
    order = np.argsort(-scores, kind="stable")
    sorted_labels = labels[order]
    cumulative = np.cumsum(sorted_labels)
    ranks = np.arange(1, len(labels) + 1)
    return float((cumulative[sorted_labels == 1] / ranks[sorted_labels == 1]).sum() / positives)


def pr_trapezoid(labels: np.ndarray, scores: np.ndarray) -> float | None:
    positives = int(labels.sum())
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        return None
    order = np.argsort(-scores, kind="stable")
    y = labels[order]
    tp = np.cumsum(y)
    fp = np.cumsum(1 - y)
    recall = np.r_[0.0, tp / positives]
    precision = np.r_[1.0, tp / np.maximum(tp + fp, 1)]
    return float(np.trapezoid(precision, recall))


def metric_row(labels: np.ndarray, scores: np.ndarray, threshold: float) -> dict[str, Any]:
    predicted = scores >= threshold
    tp = int(np.sum(predicted & (labels == 1)))
    fp = int(np.sum(predicted & (labels == 0)))
    tn = int(np.sum(~predicted & (labels == 0)))
    fn = int(np.sum(~predicted & (labels == 1)))
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall and precision + recall else 0.0
    return {
        "threshold": float(threshold),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_edge_rate": fp / (fp + tn) if fp + tn else None,
        "false_negative_rate": fn / (tp + fn) if tp + fn else None,
        "edge_rate": int(predicted.sum()) / len(labels) if len(labels) else None,
    }


def sweep(labels: np.ndarray, scores: np.ndarray) -> list[dict[str, Any]]:
    valid_scores = sorted({float(x) for x in scores if x > -1.5}, reverse=True)
    # Exact score thresholds are the useful PR operating points. Add familiar
    # grid points so results are easy to compare and plot across methods.
    grid = [round(-1.0 + i * 0.01, 2) for i in range(201)]
    thresholds = sorted(set(valid_scores + grid), reverse=True)
    return [metric_row(labels, scores, threshold) for threshold in thresholds]


def select_high_precision(rows: list[dict[str, Any]], target: float) -> dict[str, Any] | None:
    eligible = [row for row in rows if row["precision"] is not None and row["precision"] >= target]
    if not eligible:
        return None
    return max(eligible, key=lambda row: (row["recall"] or 0.0, row["threshold"]))


def markdown_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value).replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def write_markdown(path: Path, rows: list[dict[str, Any]], title: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    fields = list(rows[0])
    lines = []
    if title:
        lines.append(f"# {title}\n")
    lines.append("| " + " | ".join(fields) + " |")
    lines.append("| " + " | ".join("---" for _ in fields) + " |")
    for row in rows:
        lines.append("| " + " | ".join(markdown_cell(row.get(field)) for field in fields) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    if not 0 < args.precision_target <= 1:
        raise SystemExit("--precision-target must be in (0, 1]")
    if args.embedding_batch_size <= 0:
        raise SystemExit("--embedding-batch-size must be positive")

    methods = parse_methods(args.method)
    if not args.anchor_field.strip():
        raise SystemExit("--anchor-field must not be empty")
    pair_candidates = {row["pair_id"]: row for row in load_jsonl(args.pairs)}
    pair_gold = {row["pair_id"]: row for row in load_jsonl(args.gold)}
    pair_ids = [pair_id for pair_id in pair_gold if pair_id in pair_candidates]
    if len(pair_ids) != len(pair_gold):
        missing = sorted(set(pair_gold) - set(pair_candidates))
        raise SystemExit(f"Gold pairs missing from candidate file, e.g. {missing[:3]}")
    if not pair_ids:
        raise SystemExit("No pair rows found")

    segment_gold = {row["segment_id"]: row for row in load_jsonl(args.segment_gold)}
    outputs = {
        name: parse_anchor_outputs(path, args.anchor_field, args.anchor_part)
        for name, path in methods.items()
    }
    required_ids = {
        sid
        for pair_id in pair_ids
        for sid in (
            pair_candidates[pair_id]["left_segment_id"],
            pair_candidates[pair_id]["right_segment_id"],
        )
    }
    for name, rows in outputs.items():
        missing = sorted(required_ids - set(rows))
        if missing:
            raise SystemExit(f"Method {name} is missing {len(missing)} pair-referenced segments, e.g. {missing[:3]}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = args.out_dir / f"embedding_cache_{args.embedding_model.replace('/', '_')}.json"
    cache = parse_embedding_cache(cache_path, args.embedding_model)
    all_anchors = sorted(
        {
            row["_evaluation_anchor"]
            for rows in outputs.values()
            for row in rows.values()
            if isinstance(row.get("_evaluation_anchor"), str)
            and row["_evaluation_anchor"]
        }
    )
    vectors = get_embeddings(
        all_anchors,
        args.embedding_model,
        cache,
        cache_path,
        args.embedding_batch_size,
        args.device,
    )

    labels_all = np.asarray([bool(pair_gold[pair_id]["same_specific_topic"]) for pair_id in pair_ids], dtype=np.int8)
    definite = np.asarray(
        [pair_gold[pair_id].get("relation") != "uncertain" for pair_id in pair_ids], dtype=bool
    )
    summary: list[dict[str, Any]] = []
    for name, rows in outputs.items():
        scores_list: list[float] = []
        pair_rows: list[dict[str, Any]] = []
        for pair_id in pair_ids:
            pair = pair_candidates[pair_id]
            left = rows[pair["left_segment_id"]].get("_evaluation_anchor")
            right = rows[pair["right_segment_id"]].get("_evaluation_anchor")
            if isinstance(left, str) and isinstance(right, str) and left.strip() and right.strip():
                score = float(np.dot(vectors[left.strip()], vectors[right.strip()]))
                missing_anchor = False
            else:
                # Cosine similarity is in [-1, 1] after normalization. Keep a
                # missing-anchor pair below every valid threshold so it cannot
                # create an edge.
                score = -2.0
                missing_anchor = True
            scores_list.append(score)
            pair_rows.append({
                "method": name,
                "pair_id": pair_id,
                "left_segment_id": pair["left_segment_id"],
                "right_segment_id": pair["right_segment_id"],
                "left_anchor": left,
                "right_anchor": right,
                "similarity": score if not missing_anchor else None,
                "missing_anchor": missing_anchor,
                "same_specific_topic": bool(pair_gold[pair_id]["same_specific_topic"]),
                "relation": pair_gold[pair_id].get("relation"),
            })
        scores = np.asarray(scores_list, dtype=np.float64)
        method_dir = args.out_dir / name
        method_dir.mkdir(parents=True, exist_ok=True)
        write_markdown(method_dir / "pair_scores.md", pair_rows, f"{name}: pair scores")
        with (method_dir / "pair_scores.jsonl").open("w", encoding="utf-8") as fh:
            for row in pair_rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        for group_name, mask in [("all", np.ones(len(pair_ids), dtype=bool)), ("definite", definite)]:
            group_labels = labels_all[mask]
            group_scores = scores[mask]
            pr_rows = sweep(group_labels, group_scores)
            write_markdown(
                method_dir / f"thresholds_{group_name}.md",
                pr_rows,
                f"{name}: threshold sweep ({group_name})",
            )
            ap = average_precision(group_labels, group_scores)
            trap = pr_trapezoid(group_labels, group_scores)
            high = select_high_precision(pr_rows, args.precision_target)
            best_f1 = max(pr_rows, key=lambda row: row["f1"])
            summary_row = {
                "method": name,
                "group": group_name,
                "pair_count": int(mask.sum()),
                "positive_count": int(group_labels.sum()),
                "negative_count": int(len(group_labels) - group_labels.sum()),
                "pr_auc_average_precision": ap,
                "pr_auc_trapezoid": trap,
                "best_f1_threshold": best_f1["threshold"],
                "best_f1": best_f1["f1"],
                "best_f1_precision": best_f1["precision"],
                "best_f1_recall": best_f1["recall"],
                "precision_target": args.precision_target,
                "high_precision_threshold": high["threshold"] if high else None,
                "high_precision_precision": high["precision"] if high else None,
                "high_precision_recall": high["recall"] if high else None,
                "high_precision_f1": high["f1"] if high else None,
                "high_precision_false_edge_rate": high["false_edge_rate"] if high else None,
                "high_precision_false_negative_rate": high["false_negative_rate"] if high else None,
            }
            summary.append(summary_row)
            if high:
                threshold = high["threshold"]
                for error_type, condition in (
                    ("false_positive", (group_scores >= threshold) & (group_labels == 0)),
                    ("false_negative", (group_scores < threshold) & (group_labels == 1)),
                ):
                    errors = []
                    for row, keep in zip(np.asarray(pair_rows, dtype=object)[mask], condition):
                        if not keep:
                            continue
                        left_gold = segment_gold.get(row["left_segment_id"], {})
                        right_gold = segment_gold.get(row["right_segment_id"], {})
                        errors.append({
                            **row,
                            "error_type": error_type,
                            "threshold": threshold,
                            "left_gold_topic_descriptor": left_gold.get("gold_topic_descriptor"),
                            "right_gold_topic_descriptor": right_gold.get("gold_topic_descriptor"),
                            "left_null_expected": left_gold.get("null_expected"),
                            "right_null_expected": right_gold.get("null_expected"),
                            "left_memory_granularity": left_gold.get("memory_granularity"),
                            "right_memory_granularity": right_gold.get("memory_granularity"),
                            "left_topic_family": left_gold.get("topic_family"),
                            "right_topic_family": right_gold.get("topic_family"),
                        })
                    with (method_dir / f"{group_name}_{error_type}.jsonl").open("w", encoding="utf-8") as fh:
                        for row in errors:
                            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    write_markdown(args.out_dir / "summary.md", summary, "Anchor similarity evaluation summary")
    (args.out_dir / "run_config.json").write_text(
        json.dumps({
            "embedding_model": args.embedding_model,
            "anchor_field": args.anchor_field,
            "anchor_part": args.anchor_part,
            "embedding_backend": "sentence-transformers-local",
            "device": args.device,
            "pairs": str(args.pairs),
            "gold": str(args.gold),
            "segment_gold": str(args.segment_gold),
            "precision_target": args.precision_target,
            "methods": {name: str(path) for name, path in methods.items()},
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"summary": str(args.out_dir / 'summary.md'), "methods": list(methods)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
