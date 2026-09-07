"""Input loading and validation for the graph experiment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load_anchor_rows(path: Path, anchor_field: str) -> list[dict[str, Any]]:
    rows = load_jsonl(path)
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        segment_id = row.get("segment_id")
        if not segment_id:
            raise ValueError(f"Missing segment_id in {path}")
        if segment_id in by_id:
            raise ValueError(f"Duplicate segment_id in {path}: {segment_id}")
        if row.get("status") not in (None, "success"):
            raise ValueError(
                f"Anchor row is not successful for {segment_id}: {row.get('status')}"
            )
        anchor = row.get(anchor_field)
        if anchor is not None and (not isinstance(anchor, str) or not anchor.strip()):
            raise ValueError(f"Invalid {anchor_field!r} for {segment_id}")
        by_id[segment_id] = row
    return list(by_id.values())


def load_segment_metadata(path: Path) -> dict[str, dict[str, Any]]:
    rows = load_jsonl(path)
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        segment_id = row.get("segment_id")
        if not segment_id:
            raise ValueError(f"Missing segment_id in {path}")
        if segment_id in result:
            raise ValueError(f"Duplicate segment_id in {path}: {segment_id}")
        result[segment_id] = row
    return result


def load_pair_labels(
    candidates_path: Path,
    gold_path: Path,
    node_ids: set[str],
) -> list[dict[str, Any]]:
    candidates = {row["pair_id"]: row for row in load_jsonl(candidates_path)}
    gold = {row["pair_id"]: row for row in load_jsonl(gold_path)}
    if set(candidates) != set(gold):
        missing_in_gold = sorted(set(candidates) - set(gold))
        missing_in_candidates = sorted(set(gold) - set(candidates))
        raise ValueError(
            "Candidate/gold pair IDs do not match: "
            f"missing_in_gold={missing_in_gold[:3]}, "
            f"missing_in_candidates={missing_in_candidates[:3]}"
        )

    rows: list[dict[str, Any]] = []
    for pair_id, candidate in candidates.items():
        left = candidate.get("left_segment_id")
        right = candidate.get("right_segment_id")
        if left not in node_ids or right not in node_ids:
            raise ValueError(f"Pair {pair_id} references a segment outside anchor input")
        label = gold[pair_id]
        rows.append(
            {
                "pair_id": pair_id,
                "left_segment_id": left,
                "right_segment_id": right,
                "same_specific_topic": bool(label.get("same_specific_topic")),
                "relation": label.get("relation"),
            }
        )
    return rows
