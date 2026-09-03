#!/usr/bin/env python3
"""Build the first semantic-anchor dataset from cutting predictions.

This script deliberately creates candidate data, not human gold labels.  The
question file supplies topic seeds and memory evidence; the cutting output
supplies the segment boundaries; raw conversations supply the share_memory
unit mapping.
"""

from __future__ import annotations

import itertools
import json
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CUTTING_FILE = (
    ROOT.parent
    / "cutting"
    / "artifacts"
    / "cutting_opencode_go_v5_all"
    / "predictions.jsonl"
)
SUBSTANTIVE_FILE = ROOT / "data" / "substantive_segments.jsonl"
SOURCE_DIR = ROOT.parent / "cutting" / "data" / "conversations"
QUESTION_FILE = ROOT / "evaluation_questions_academic_researcher.json"
OUT_DIR = ROOT / "artifacts" / "anchor_dataset_v1"


TAG_PATTERNS = {
    "ai": r"\b(ai|artificial intelligence)\b",
    "machine_learning": r"\b(machine learning|deep learning|neural network)\b",
    "consciousness": r"\b(consciousness|free will|soul|sentien)\w*",
    "quantum": r"\b(quantum computing|quantum machine learning|superposition)\b",
    "ai_assistant": r"\b(ai assistant|assistant capabilities|using your features)\b",
    "ai_alignment": r"\b(ai alignment|alignment)\b",
    "semantic_search": r"\bsemantic search\b",
    "ai_generated_content": r"\b(ai-generated|generated content)\b",
    "technology": r"\b(technology|technological|smart home|programming language)\w*",
    "food_coffee": r"\b(coffee)\b",
    "food_lunch": r"\b(lunch)\b",
    "food": r"\b(breakfast|dinner|grocer\w*|food|meal)\b",
    "steps": r"\b(steps|step tracker|walked)\b",
    "project": r"\b(project proposal|project|framework|deliverables|budget)\b",
    "meeting": r"\b(meeting|agenda|attendees)\b",
    "email": r"\b(email|recipient|call.to.action)\b",
    "books": r"\b(book|books|reading|author|novel|tragedy|modernism)\w*",
    "music": r"\b(music|symphon\w*|bebop|opera|chamber|spiritual music)\w*",
    "travel": r"\b(travel|Australia|Raja Ampat|savanna|botanical garden)\w*",
    "todo": r"\b(todo|to-do|task|schedule|prepare|review|update)\w*",
}

# Broad tags such as ``technology``, ``todo`` and ``food`` are useful for
# stratified sampling but create too many pair combinations.  Pair retrieval
# uses only more discriminative tags; human review can still add pairs outside
# this automatically retrieved pool.
PAIR_TAGS = {
    "ai",
    "machine_learning",
    "consciousness",
    "quantum",
    "ai_assistant",
    "ai_alignment",
    "semantic_search",
    "ai_generated_content",
    "food_coffee",
    "food_lunch",
    "steps",
    "project",
    "meeting",
    "email",
    "books",
    "music",
    "travel",
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=False) + "\n")


def unit_number(unit_id: str) -> int:
    return int(unit_id[1:])


def topic_tags(text: str) -> list[str]:
    lowered = text.lower()
    return [tag for tag, pattern in TAG_PATTERNS.items() if re.search(pattern, lowered)]


def operation_terms(value: Any, key: str = "") -> list[str]:
    """Extract searchable content terms from operation metadata."""
    terms: list[str] = []
    if isinstance(value, dict):
        for child_key, child in value.items():
            terms.extend(operation_terms(child, child_key))
    elif isinstance(value, list):
        for child in value:
            terms.extend(operation_terms(child, key))
    elif isinstance(value, str):
        # Dates, generic operation labels and very short values are not useful
        # for locating the memory-bearing sentence.
        if key not in {"created_at", "updated_at", "category", "update_type"} and len(value.strip()) >= 4:
            terms.append(value.strip().casefold())
    elif isinstance(value, (int, float)) and key not in {"created_at", "updated_at"}:
        terms.append(str(value).casefold())
    return terms


def memory_units_for_session(
    source: dict[str, Any], units: list[dict[str, Any]]
) -> set[str]:
    """Locate memory-bearing sentences, including turns split at a boundary.

    ``share_memory`` is attached to a whole source message, while cutting can
    split that message into multiple semantic units.  Operation metadata gives
    us a safer sentence-level match than marking every unit in the shared
    message.  If no term matches, the final unit of that shared turn is used as
    a conservative fallback.
    """
    shared_turns = {
        message["turn"]
        for message in source["conversation"]
        if message.get("share_memory") is True
    }
    terms = operation_terms(source.get("operation_details", {}))
    result: set[str] = set()
    for turn in shared_turns:
        turn_units = [unit for unit in units if unit["turn"] == turn]
        matched = [
            unit
            for unit in turn_units
            if any(term in unit["text"].casefold() for term in terms)
        ]
        if matched:
            result.update(unit["unit_id"] for unit in matched)
        elif turn_units:
            result.add(turn_units[-1]["unit_id"])
    return result


def question_scope(question_id: str) -> str:
    if question_id.startswith("content_"):
        return "same_content_item"
    if question_id in {
        "activity_food_coffee_158",
        "goal_food_expenses_lunch_158_1",
        "goal_step_tracker_daily_steps_158_0",
    }:
        return "same_specific_topic"
    if question_id.startswith("pref_"):
        return "same_broad_preference_category"
    if question_id.startswith("activity_") or question_id.startswith("goal_"):
        return "same_broad_activity_category"
    return "needs_review"


def collect_question_refs(question_data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    refs: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def walk(value: Any, path: str, question_id: str, group: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else key
                if key == "session_id" and isinstance(child, int):
                    refs[question_id].append(
                        {
                            "session_id": child,
                            "source_path": child_path,
                            "evidence_kind": (
                                "forgetting"
                                if "forgetting_evidence" in child_path
                                else "memory"
                            ),
                            "question_group": group,
                        }
                    )
                else:
                    walk(child, child_path, question_id, group)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]", question_id, group)

    for group, questions in question_data["questions"].items():
        for question in questions:
            question_id = question["question_id"]
            walk(question, "", question_id, group)
            memory = question.get("memory_evidence", {})
            for index, session_id in enumerate(memory.get("session_history", [])):
                refs[question_id].append(
                    {
                        "session_id": session_id,
                        "source_path": f"memory_evidence.session_history[{index}]",
                        "evidence_kind": "memory_history",
                        "question_group": group,
                    }
                )
    return refs


def build_segments() -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    predictions = read_jsonl(CUTTING_FILE)
    prediction_segment_ids = {
        f"session_{prediction['session_id']:04d}_seg{index:03d}"
        for prediction in predictions
        for index, _ in enumerate(prediction["segments"], start=1)
    }
    curated_segments = read_jsonl(SUBSTANTIVE_FILE)
    source_sessions = {
        int(path.stem.split("_")[-1]): json.loads(path.read_text(encoding="utf-8"))
        for path in SOURCE_DIR.glob("session_*.json")
    }
    segments: list[dict[str, Any]] = []
    by_session: dict[int, dict[str, Any]] = {}

    for curated in curated_segments:
        session_id = curated["session_id"]
        if curated["segment_id"] not in prediction_segment_ids:
            raise ValueError(f"Curated segment is absent from cutting output: {curated['segment_id']}")
        source = source_sessions[session_id]
        units = curated["units"]
        all_memory_units = memory_units_for_session(source, units)
        memory_units = [
            unit["unit_id"] for unit in units if unit["unit_id"] in all_memory_units
        ]
        text = curated.get("text") or "\n".join(
            f"[{unit['speaker']}] {unit['text']}" for unit in units
        )
        row = {
            "segment_id": curated["segment_id"],
            "session_id": session_id,
            "date": source["date"],
            "persona": source["persona"],
            "session_type": source["session_type"],
            "operation": source.get("operation"),
            "segment_index": curated["original_segment_index"],
            "segment_count": curated["original_segment_count"],
            "segment_role": curated["segment_role"],
            "retained_reason": curated.get("retained_reason"),
            "include_in_anchor_eval": True,
            "start_unit_id": curated["start_unit_id"],
            "end_unit_id": curated["end_unit_id"],
            "unit_ids": curated["unit_ids"],
            "memory_unit_ids": memory_units,
            "text": text,
            "heuristic_tags": topic_tags(text),
            "gold": None,
            "notes": ["Substantive membership comes from the curated source file."],
        }
        segments.append(row)
        by_session.setdefault(session_id, {"source": source, "segments": []})["segments"].append(row)
    return segments, by_session


def build_seed_groups(
    question_data: dict[str, Any],
    refs: dict[str, list[dict[str, Any]]],
    by_session: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    groups = []
    for group_name, questions in question_data["questions"].items():
        for question in questions:
            question_id = question["question_id"]
            question_refs = refs.get(question_id, [])
            session_ids = sorted({ref["session_id"] for ref in question_refs})
            member_segment_ids = []
            unmapped = []
            for session_id in session_ids:
                candidates = [
                    segment
                    for segment in by_session[session_id]["segments"]
                    if segment["include_in_anchor_eval"] and segment["memory_unit_ids"]
                ]
                if candidates:
                    member_segment_ids.extend(segment["segment_id"] for segment in candidates)
                else:
                    unmapped.append(session_id)
            groups.append(
                {
                    "topic_seed_id": question_id,
                    "question_group": group_name,
                    "question": question["question"],
                    "relation_scope": question_scope(question_id),
                    "session_ids": session_ids,
                    "member_segment_ids": sorted(set(member_segment_ids)),
                    "unmapped_session_ids": unmapped,
                    "evidence_refs": question_refs,
                    "gold_topic_descriptor": None,
                    "annotation_status": "seed_only_needs_human_review",
                }
            )
    return groups


def build_pair_candidates(
    segments: list[dict[str, Any]], groups: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    included = [segment for segment in segments if segment["include_in_anchor_eval"]]
    by_id = {segment["segment_id"]: segment for segment in included}
    pair_rows: dict[tuple[str, str], dict[str, Any]] = {}

    def add(left: str, right: str, candidate_type: str, reason: str, scope: str | None = None) -> None:
        if left == right:
            return
        pair = tuple(sorted((left, right)))
        row = pair_rows.setdefault(
            pair,
            {
                "pair_id": f"pair_{len(pair_rows) + 1:05d}",
                "left_segment_id": pair[0],
                "right_segment_id": pair[1],
                "same_specific_topic": None,
                "relation": None,
                "annotation_status": "candidate_needs_review",
                "candidate_types": [],
                "candidate_reasons": [],
                "annotator": None,
            },
        )
        if candidate_type not in row["candidate_types"]:
            row["candidate_types"].append(candidate_type)
        if reason not in row["candidate_reasons"]:
            row["candidate_reasons"].append(reason)
        if scope and scope not in row.get("seed_scopes", []):
            row.setdefault("seed_scopes", []).append(scope)

    for group in groups:
        members = [member for member in group["member_segment_ids"] if member in by_id]
        for left, right in itertools.combinations(members, 2):
            add(
                left,
                right,
                "question_seed_group",
                group["topic_seed_id"],
                group["relation_scope"],
            )

    tag_members: dict[str, list[str]] = defaultdict(list)
    for segment in included:
        for tag in segment["heuristic_tags"]:
            tag_members[tag].append(segment["segment_id"])
    for tag, members in tag_members.items():
        if tag not in PAIR_TAGS:
            continue
        for left in members:
            candidates = [
                right
                for right in members
                if right != left
                and by_id[left]["session_id"] != by_id[right]["session_id"]
            ]
            # Keep a small, deterministic hard-negative retrieval neighborhood
            # for each target.  This is a review queue, not a gold label.
            candidates.sort(
                key=lambda right: (
                    -len(
                        set(by_id[left]["heuristic_tags"])
                        .intersection(by_id[right]["heuristic_tags"])
                    ),
                    abs(by_id[left]["session_id"] - by_id[right]["session_id"]),
                    by_id[right]["session_id"],
                )
            )
            for right in candidates[:4]:
                add(left, right, "shared_heuristic_tag", tag)

    # A small deterministic random-negative pool gives the annotator a control
    # set without exploding the number of pair cards.
    rng = random.Random(20260903)
    for segment in included:
        other = [
            candidate
            for candidate in included
            if candidate["session_id"] != segment["session_id"]
            and not set(segment["heuristic_tags"]).intersection(candidate["heuristic_tags"])
        ]
        rng.shuffle(other)
        for candidate in other[:2]:
            add(
                segment["segment_id"],
                candidate["segment_id"],
                "random_negative_control",
                "no_shared_heuristic_tag",
            )

    return list(pair_rows.values())


def write_readme(segments: list[dict[str, Any]], groups: list[dict[str, Any]], pairs: list[dict[str, Any]]) -> None:
    substantive = sum(row["include_in_anchor_eval"] for row in segments)
    referenced_sessions = sorted({session_id for group in groups for session_id in group["session_ids"]})
    ai_segments = sum(
        row["include_in_anchor_eval"]
        and bool(set(row["heuristic_tags"]).intersection({"ai", "machine_learning", "consciousness", "quantum", "ai_assistant", "ai_alignment", "semantic_search", "ai_generated_content"}))
        for row in segments
    )
    text = f"""# Semantic-anchor dataset v1

This directory is generated from the curated `substantive_segments.jsonl`,
with provenance from `cutting_opencode_go_v5_all`.

- Curated substantive segments: {substantive}
- Question seed groups: {len(groups)}
- Sessions referenced by the question file: {len(referenced_sessions)}
- Pair candidates: {len(pairs)}
- AI-related substantive candidates by heuristic tag: {ai_segments}

The files contain candidate data only. `gold` labels and pair labels are
intentionally null until human review. The curated source file is the
authoritative membership filter, including its manually reviewed boundary
exceptions.

`heuristic_tags` are retrieval aids only; they must not be treated as topic
gold labels or used as final anchor strings.
"""
    (OUT_DIR / "README.md").write_text(text, encoding="utf-8")


def write_annotation_templates(
    segments: list[dict[str, Any]], pairs: list[dict[str, Any]]
) -> None:
    segment_labels = []
    for segment in segments:
        if not segment["include_in_anchor_eval"]:
            continue
        segment_labels.append(
            {
                "segment_id": segment["segment_id"],
                "topic_presence": None,
                "null_expected": None,
                "topic_family": None,
                "gold_topic_descriptor": None,
                "memory_granularity": None,
                "is_user_memory_relevant": None,
                "annotation_status": "needs_annotation",
                "annotator": None,
                "notes": None,
            }
        )
    pair_labels = []
    for pair in pairs:
        pair_labels.append(
            {
                "pair_id": pair["pair_id"],
                "same_specific_topic": None,
                "relation": None,
                "annotation_status": "needs_annotation",
                "annotator": None,
                "notes": None,
            }
        )
    write_jsonl(OUT_DIR / "segment_gold_labels.jsonl", segment_labels)
    write_jsonl(OUT_DIR / "pair_gold_labels.jsonl", pair_labels)
    schema = {
        "segment_label_values": {
            "topic_presence": ["clear", "weak", "none"],
            "null_expected": "boolean",
            "memory_granularity": [
                "appropriate",
                "too_coarse",
                "too_fine",
                "not_applicable",
            ],
        },
        "pair_label_values": {
            "same_specific_topic": "boolean",
            "relation": [
                "same_specific_topic",
                "same_memory_item",
                "same_project_different_aspect",
                "same_broad_family",
                "shared_entity_only",
                "unrelated",
                "uncertain",
            ],
        },
        "annotation_rule": "Do not use heuristic_tags or candidate_types as gold labels.",
    }
    (OUT_DIR / "annotation_schema.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    question_data = json.loads(QUESTION_FILE.read_text(encoding="utf-8"))
    refs = collect_question_refs(question_data)
    segments, by_session = build_segments()
    groups = build_seed_groups(question_data, refs, by_session)
    seed_by_segment: dict[str, list[str]] = defaultdict(list)
    for group in groups:
        for segment_id in group["member_segment_ids"]:
            seed_by_segment[segment_id].append(group["topic_seed_id"])
    for segment in segments:
        segment["topic_seed_ids"] = sorted(seed_by_segment[segment["segment_id"]])
    pairs = build_pair_candidates(segments, groups)
    manifest = {
        "dataset_version": "anchor_dataset_v1",
        "source_cutting_file": str(CUTTING_FILE),
        "source_substantive_file": str(SUBSTANTIVE_FILE),
        "source_question_file": str(QUESTION_FILE),
        "source_session_count": len(by_session),
        "segment_count": len(segments),
        "substantive_candidate_count": sum(row["include_in_anchor_eval"] for row in segments),
        "question_seed_group_count": len(groups),
        "question_referenced_session_count": len({ref["session_id"] for refs_for_question in refs.values() for ref in refs_for_question}),
        "pair_candidate_count": len(pairs),
        "label_policy": "candidate_only_no_gold_labels",
        "membership_policy": "use_curated_substantive_segments_file",
        "heuristic_tags_are_not_gold": True,
    }
    write_jsonl(OUT_DIR / "segments.jsonl", segments)
    write_jsonl(OUT_DIR / "topic_seed_groups.jsonl", groups)
    write_jsonl(OUT_DIR / "pair_candidates.jsonl", pairs)
    write_annotation_templates(segments, pairs)
    (OUT_DIR / "experiment_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_readme(segments, groups, pairs)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
