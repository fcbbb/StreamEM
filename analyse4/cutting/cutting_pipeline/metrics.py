from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

from .prompt import unit_index


def positions_from_segments(row: dict[str, Any]) -> list[int]:
    if isinstance(row.get("predicted_boundary_positions"), list):
        return sorted({int(value) for value in row["predicted_boundary_positions"]})
    units, segments = row.get("units") or [], row.get("segments") or []
    index = unit_index(units)
    return sorted(index[segment["end_unit_id"]] + 1 for segment in segments[:-1])


def gold_positions(row: dict[str, Any]) -> list[int]:
    units = row.get("units") or []
    index = unit_index(units)
    if isinstance(row.get("boundaries"), list):
        positions = []
        for boundary in row["boundaries"]:
            if boundary.get("boundary", True) is not True:
                continue
            after, before = boundary.get("after_unit_id"), boundary.get("before_unit_id")
            if after not in index or before not in index or index[before] != index[after] + 1:
                raise ValueError(f"gold session {row.get('session_id')}: invalid boundary {boundary}")
            positions.append(index[after] + 1)
        return sorted(set(positions))
    return positions_from_segments({"units": units, "segments": row.get("segments") or []})


def boundary_prf(predicted: list[int], gold: list[int], tolerance: int = 0) -> dict[str, Any]:
    predicted, gold = sorted(set(predicted)), sorted(set(gold))
    pairs = []
    pred_index = gold_index = 0
    while pred_index < len(predicted) and gold_index < len(gold):
        distance = predicted[pred_index] - gold[gold_index]
        if abs(distance) <= tolerance:
            pairs.append({"predicted": predicted[pred_index], "gold": gold[gold_index]})
            pred_index += 1
            gold_index += 1
        elif predicted[pred_index] < gold[gold_index] - tolerance:
            pred_index += 1
        else:
            gold_index += 1
    tp = len(pairs)
    precision = tp / len(predicted) if predicted else (1.0 if not gold else 0.0)
    recall = tp / len(gold) if gold else (1.0 if not predicted else 0.0)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision, "recall": recall, "f1": f1, "tp": tp,
        "predicted": len(predicted), "gold": len(gold), "missed": len(gold) - tp,
        "oversegmented": len(predicted) - tp, "matches": pairs,
    }


def _same_segment(boundaries: set[int], left: int, right: int) -> bool:
    if left > right:
        left, right = right, left
    return not any(left < boundary <= right for boundary in boundaries)


def segmentation_metrics(predicted: list[int], gold: list[int], n_units: int) -> dict[str, Any]:
    if n_units <= 1:
        return {"pk": None, "window_diff": None, "window_size": None}
    window_size = max(1, int(round(n_units / (2 * (len(gold) + 1)))))
    pair_count = max(0, n_units - window_size)
    if not pair_count:
        return {"pk": None, "window_diff": None, "window_size": window_size}
    predicted_set, gold_set = set(predicted), set(gold)
    pk_errors = sum(
        _same_segment(predicted_set, i, i + window_size)
        != _same_segment(gold_set, i, i + window_size)
        for i in range(pair_count)
    )
    wd_errors = sum(
        sum(i + 1 <= boundary <= i + window_size for boundary in predicted_set)
        != sum(i + 1 <= boundary <= i + window_size for boundary in gold_set)
        for i in range(pair_count)
    )
    return {"pk": pk_errors / pair_count, "window_diff": wd_errors / pair_count, "window_size": window_size}


def _finish(values: dict[str, int]) -> dict[str, Any]:
    precision = values["tp"] / values["predicted"] if values["predicted"] else (1.0 if not values["gold"] else 0.0)
    recall = values["tp"] / values["gold"] if values["gold"] else (1.0 if not values["predicted"] else 0.0)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {**values, "precision": precision, "recall": recall, "f1": f1}


def aggregate_boundary_metrics(rows: list[dict[str, Any]], tolerance: Optional[int]) -> dict[str, Any]:
    exact = {key: 0 for key in ("tp", "predicted", "gold", "missed", "oversegmented")}
    tolerant = {key: 0 for key in exact}
    pk_values, wd_values, windows = [], [], []
    failed = 0
    for row in rows:
        gold = gold_positions(row["gold"])
        prediction = row["prediction"]
        predicted = [] if prediction.get("status") != "ok" else positions_from_segments(prediction)
        failed += prediction.get("status") != "ok"
        for key, value in boundary_prf(predicted, gold).items():
            if key in exact:
                exact[key] += value
        segmentation = segmentation_metrics(predicted, gold, len(row["gold"].get("units") or []))
        if segmentation["pk"] is not None:
            pk_values.append(segmentation["pk"])
            wd_values.append(segmentation["window_diff"])
            windows.append(segmentation["window_size"])
        if tolerance is not None:
            for key, value in boundary_prf(predicted, gold, tolerance).items():
                if key in tolerant:
                    tolerant[key] += value
    result = {
        "sessions": len(rows), "failed_sessions": failed, "boundary": _finish(exact),
        "pk": sum(pk_values) / len(pk_values) if pk_values else None,
        "window_diff": sum(wd_values) / len(wd_values) if wd_values else None,
        "window_size_mean": sum(windows) / len(windows) if windows else None,
    }
    if tolerance is not None:
        result["boundary_f1_tolerance"] = _finish(tolerant)
        result["tolerance"] = tolerance
    return result


def scenario_summary(rows: list[dict[str, Any]], tolerance: Optional[int]) -> dict[str, Any]:
    strata: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        gold = row["gold"]
        strata["all"].append(row)
        if gold.get("session_type"):
            strata[f"session_type={gold['session_type']}"].append(row)
        if gold.get("operation"):
            strata[f"operation={gold['operation']}"].append(row)
        labels = gold.get("scenario_types") or []
        if isinstance(labels, str):
            labels = [labels]
        for label in labels:
            strata[f"scenario={label}"].append(row)
    return {name: aggregate_boundary_metrics(group, tolerance) for name, group in sorted(strata.items())}


def boundary_error_breakdown(rows: list[dict[str, Any]], tolerance: Optional[int]) -> dict[str, Any]:
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"sessions_with_boundary": 0, "gold": 0, "matched": 0, "missed": 0,
                 "matched_tolerance": 0, "missed_tolerance": 0}
    )
    for row in rows:
        gold_row, prediction = row["gold"], row["prediction"]
        predicted = [] if prediction.get("status") != "ok" else positions_from_segments(prediction)
        predicted_set, units = set(predicted), gold_row.get("units") or []
        by_id = unit_index(units)
        gold_positions_list = gold_positions(gold_row)
        tolerant_matches = {
            pair["gold"] for pair in boundary_prf(predicted, gold_positions_list, tolerance)["matches"]
        } if tolerance is not None else set()
        seen: set[str] = set()
        for boundary in gold_row.get("boundaries") or []:
            if boundary.get("boundary", True) is not True:
                continue
            after, before = boundary["after_unit_id"], boundary["before_unit_id"]
            position = by_id[after] + 1
            labels = {f"boundary_reason={boundary.get('initial_reason', 'unlabeled')}"}
            same_message = units[by_id[after]].get("message_id") == units[by_id[before]].get("message_id")
            labels.add("same_turn_internal_transition" if same_message else "cross_turn_transition")
            for label in labels:
                bucket = buckets[label]
                if label not in seen:
                    bucket["sessions_with_boundary"] += 1
                bucket["gold"] += 1
                bucket["matched"] += position in predicted_set
                bucket["missed"] += position not in predicted_set
                if tolerance is not None:
                    bucket["matched_tolerance"] += position in tolerant_matches
                    bucket["missed_tolerance"] += position not in tolerant_matches
            seen.update(labels)
    for bucket in buckets.values():
        bucket["recall"] = bucket["matched"] / bucket["gold"] if bucket["gold"] else None
        if tolerance is not None:
            bucket["recall_tolerance"] = bucket["matched_tolerance"] / bucket["gold"] if bucket["gold"] else None
    return {label: buckets[label] for label in sorted(buckets)}
