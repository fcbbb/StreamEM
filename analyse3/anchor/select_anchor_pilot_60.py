"""Select a reproducible 60-segment pilot set for the first anchor run."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


BASE = Path(__file__).parent / "artifacts" / "anchor_dataset_v1"
OUT = BASE / "pilot_v1"


REPEATED_FAMILY_TAKES = {
    "food_coffee": 2,
    "coffee expense": 2,
    "food_breakfast": 2,
    "food_lunch": 2,
    "daily_steps_activity": 2,
    "daily step count": 2,
    "project_proposal_energy_forecasting": 2,
    "project_proposal_secure_federated_learning": 2,
    "todo_prepare_lecture_materials": 2,
    "AI alignment": 2,
    "machine learning fundamentals": 2,
    "quantum_computing": 2,
    "book_topic_preference_social_dynamics": 2,
    "movie_actor_preference": 2,
    "todo_academic_conference_attendance": 2,
}

AI_CONTRAST_FAMILIES = [
    "ai_consciousness",
    "ai_assistant_capabilities",
    "explainable_ai",
    "ai_ethics_and_bias",
    "ai_healthcare_diagnostics_ethics",
    "large_language_models_and_nlp",
    "semantic search",
    "ai_creative_applications_and_art",
    "ai_autonomous_decision_making_ethics",
    "AI in scientific discovery",
    "ai_future_and_societal_impact",
    "ai_and_machine_learning_basics",
]

MEMORY_FAMILIES = [
    "travel_australia_nature",
    "travel_preference_savanna",
    "Raja Ampat travel preference",
    "botanical-garden travel preference",
    "book_topic_preference_modernism",
    "music_preference_chamber_music",
    "research paper drafting",
    "journal-submission review task",
]

GENERAL_FAMILIES = [
    "social media and communication",
    "cultural greetings",
    "car-oil-change instructions",
    "room-temperature superconductivity",
    "work_life_balance",
    "commuting",
]

NULL_SEGMENT_IDS = [
    "session_0028_seg002",  # weak general trivia / transition
    "session_0068_seg004",  # closing exchange
    "session_0108_seg002",  # weak work-time trivia transition
    "session_0148_seg003",  # weak generic hobbies prompt
]


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def main() -> None:
    segments = load_jsonl(BASE / "segments.jsonl")
    labels = {row["segment_id"]: row for row in load_jsonl(BASE / "segment_gold_labels.jsonl")}
    by_family: dict[str, list[dict]] = {}
    for segment in segments:
        by_family.setdefault(labels[segment["segment_id"]]["topic_family"], []).append(segment)

    selected: list[tuple[dict, str, str]] = []
    selected_ids: set[str] = set()

    def add(segment: dict, stratum: str, reason: str) -> None:
        sid = segment["segment_id"]
        if sid not in selected_ids:
            selected.append((segment, stratum, reason))
            selected_ids.add(sid)

    for family, take in REPEATED_FAMILY_TAKES.items():
        candidates = by_family.get(family, [])
        assert len(candidates) >= take, (family, len(candidates), take)
        for segment in candidates[:take]:
            add(segment, "repeated_topic_positive", f"Repeated family: {family}")

    for family in AI_CONTRAST_FAMILIES:
        candidates = by_family.get(family, [])
        assert candidates, f"Missing AI family: {family}"
        add(candidates[0], "ai_topic_contrast", f"AI topic contrast: {family}")

    for family in MEMORY_FAMILIES:
        candidates = by_family.get(family, [])
        assert candidates, f"Missing memory family: {family}"
        add(candidates[0], "user_memory_item", f"User-memory example: {family}")

    for family in GENERAL_FAMILIES:
        candidates = by_family.get(family, [])
        assert candidates, f"Missing general family: {family}"
        add(candidates[0], "clear_general_topic", f"Clear general topic: {family}")

    segment_by_id = {segment["segment_id"]: segment for segment in segments}
    for sid in NULL_SEGMENT_IDS:
        assert sid in segment_by_id, sid
        add(segment_by_id[sid], "weak_or_null_case", "Weak or null-expected boundary case")

    assert len(selected) == 60, len(selected)
    selected.sort(key=lambda item: item[0]["segment_id"])

    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "pilot_sample_60.jsonl").open("w", encoding="utf-8") as fh:
        for segment, stratum, reason in selected:
            fh.write(json.dumps({
                "segment_id": segment["segment_id"],
                "session_id": segment["session_id"],
                "text": segment["text"],
                "pilot_stratum": stratum,
                "pilot_selection_reason": reason,
            }, ensure_ascii=False, separators=(",", ":")) + "\n")

    with (OUT / "pilot_sample_60_gold_reference.jsonl").open("w", encoding="utf-8") as fh:
        for segment, stratum, reason in selected:
            gold = dict(labels[segment["segment_id"]])
            gold["pilot_stratum"] = stratum
            gold["pilot_selection_reason"] = reason
            fh.write(json.dumps(gold, ensure_ascii=False, separators=(",", ":")) + "\n")

    selected_set = {segment["segment_id"] for segment, _, _ in selected}
    pairs = load_jsonl(BASE / "pair_candidates.jsonl")
    pair_gold = {row["pair_id"]: row for row in load_jsonl(BASE / "pair_gold_labels.jsonl")}
    pilot_pairs = [
        pair for pair in pairs
        if pair["left_segment_id"] in selected_set and pair["right_segment_id"] in selected_set
    ]
    with (OUT / "pilot_pair_candidates.jsonl").open("w", encoding="utf-8") as fh:
        for pair in pilot_pairs:
            fh.write(json.dumps(pair, ensure_ascii=False, separators=(",", ":")) + "\n")
    with (OUT / "pilot_pair_gold_labels.jsonl").open("w", encoding="utf-8") as fh:
        for pair in pilot_pairs:
            fh.write(json.dumps(pair_gold[pair["pair_id"]], ensure_ascii=False, separators=(",", ":")) + "\n")

    manifest = {
        "dataset": "anchor_dataset_v1",
        "pilot_name": "pilot_v1",
        "segment_count": len(selected),
        "pair_count": len(pilot_pairs),
        "selection_policy": "deterministic stratified selection with repeated-topic positives, AI contrasts, user-memory items, clear general topics, and weak/null cases",
        "stratum_counts": dict(Counter(stratum for _, stratum, _ in selected)),
        "selected_segment_ids": [segment["segment_id"] for segment, _, _ in selected],
        "source_files": ["segments.jsonl", "segment_gold_labels.jsonl", "pair_candidates.jsonl", "pair_gold_labels.jsonl"],
    }
    (OUT / "pilot_selection_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"segment_count": len(selected), "pair_count": len(pilot_pairs), "stratum_counts": manifest["stratum_counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
