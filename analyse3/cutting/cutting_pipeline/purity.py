from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .data import read_jsonl, write_jsonl
from .prompt import unit_index


def purity_metrics(purity_path: Optional[Path]) -> dict[str, Any]:
    if purity_path is None or not purity_path.exists():
        return {"status": "not_provided", "pure": None, "sampled_segments": 0}
    rows = read_jsonl(purity_path)
    answered = [row for row in rows if isinstance(row.get("pure"), bool)]
    pure_count = sum(row["pure"] for row in answered)
    return {
        "status": "provided", "pure": pure_count / len(answered) if answered else None,
        "pure_segments": pure_count, "sampled_segments": len(rows),
        "answered_segments": len(answered), "unanswered_segments": len(rows) - len(answered),
    }


def make_purity_template(predictions: list[dict[str, Any]], output_path: Path, sample_size: int) -> None:
    candidates = []
    for prediction in predictions:
        if prediction.get("status") != "ok":
            continue
        units = prediction.get("units") or []
        index = unit_index(units)
        for segment in prediction.get("segments") or []:
            start, end = index[segment["start_unit_id"]], index[segment["end_unit_id"]]
            candidates.append({
                "session_id": prediction["session_id"], "segment_id": segment["segment_id"],
                "start_unit_id": segment["start_unit_id"], "end_unit_id": segment["end_unit_id"],
                "segment_text": " ".join(unit["text"] for unit in units[start : end + 1]),
                "pure": None, "notes": "",
            })
    if sample_size > 0 and len(candidates) > sample_size:
        step = len(candidates) / sample_size
        candidates = [candidates[min(len(candidates) - 1, int(i * step))] for i in range(sample_size)]
    write_jsonl(output_path, candidates)
    print(f"wrote purity template: {output_path} segments={len(candidates)}")
