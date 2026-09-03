"""Create transparent, local pair pre-annotations from direct segment labels.

This deliberately ignores candidate_types, candidate_reasons, seed_scopes, and
heuristic_tags. It is a reproducible pre-annotation pass, not an API call or a
substitute for later double-blind human adjudication.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


BASE = Path(__file__).parent / "artifacts" / "anchor_dataset_v1"
SEGMENT_LABELS = BASE / "segment_gold_labels.jsonl"
PAIR_CANDIDATES = BASE / "pair_candidates.jsonl"
OUT_DIR = BASE / "direct_annotations" / "pair_labels"
PAIR_GOLD = BASE / "pair_gold_labels.jsonl"


def compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def topic_key(row: dict) -> tuple[str | None, str | None, bool]:
    """Return (broad category, specific key, memory-item flag)."""
    family = compact(row.get("topic_family") or "")
    desc = compact(row.get("gold_topic_descriptor") or "")
    text = f"{family} {desc}"
    if row.get("topic_presence") != "clear" or row.get("null_expected"):
        return None, None, False

    # Repeated concrete user-memory objects.
    if "coffee" in text:
        return "food", "food coffee", True
    if "breakfast" in text:
        return "food", "food breakfast", True
    if "lunch" in text:
        return "food", "food lunch", True
    if "dinner" in text:
        return "food", "food dinner", True
    if "groceries" in text:
        return "food", "food groceries", True
    if "step count" in text or "steps" in text:
        return "activity", "daily steps", True

    # Durable preferences.
    if any(word in text for word in ("book", "reading preference", "literary preference")):
        if "social dynamics" in text:
            key = "books social dynamics"
        elif "modernism" in text:
            key = "books modernism"
        elif "gatsby" in text:
            key = "books great gatsby"
        elif "thomas mann" in text:
            key = "books thomas mann"
        elif "tragedy" in text:
            key = "books tragedy"
        else:
            key = "books general"
        return "preference_books", key, True
    if any(word in text for word in ("music", "opera", "bebop", "symphon", "chamber")):
        if "opera" in text:
            key = "music opera"
        elif "bebop" in text:
            key = "music bebop"
        elif "symphon" in text:
            key = "music symphonies"
        elif "chamber" in text:
            key = "music chamber"
        elif "spiritual" in text:
            key = "music spiritual"
        elif "indie" in text or "alternative rock" in text:
            key = "music indie alternative"
        else:
            key = "music general"
        return "preference_music", key, True
    if any(word in text for word in ("actor", "william holden", "grace kelly", "rita hayworth")):
        if "william holden" in text:
            key = "actor william holden"
        elif "grace kelly" in text:
            key = "actor grace kelly"
        elif "rita hayworth" in text:
            key = "actor rita hayworth"
        else:
            key = "actor general"
        return "preference_actor", key, True
    if any(word in text for word in ("travel", "raja ampat", "savanna", "tropical", "ancient history destinations", "botanical gardens")):
        if "raja ampat" in text:
            key = "travel raja ampat"
        elif "botanical" in text:
            key = "travel botanical gardens"
        elif "savanna" in text:
            key = "travel savanna"
        elif "tropical" in text:
            key = "travel tropical climate"
        elif "ancient history" in text:
            key = "travel ancient history"
        else:
            key = "travel general"
        return "preference_travel", key, True

    # Project/document memory objects and tasks.
    if "energy forecasting" in text:
        return "project", "project energy forecasting", True
    if "federated learning" in text:
        return "project", "project secure federated learning", True
    if "drug discovery" in text:
        return "project", "project ai drug discovery", True
    if "target validation" in text:
        return "project", "project deep learning target validation", True
    if "workflow optimization" in text:
        return "project", "project workflow optimization", True
    if "healthcare accessibility" in text:
        return "document", "document healthcare accessibility", True
    if "meeting notes" in text or "meeting note" in text:
        return "document", "document meeting notes", True
    if "lecture material" in text:
        return "task", "task lecture materials", True
    if "conference abstract" in text:
        return "task", "task conference abstract", True
    if "academic conference" in text:
        return "task", "task academic conference", True
    if "university library" in text:
        return "task", "task university library", True
    if "research methodology workshop" in text:
        return "task", "task research methodology workshop", True
    if "health appointment" in text:
        return "task", "task health appointments", True
    if "field work" in text:
        return "task", "task field work scheduling", True
    if "research proposal" in text:
        return "task", "task research proposal", True
    if "research paper" in text:
        return "task", "task research paper", True
    if "journal submission" in text:
        return "task", "task journal submissions", True
    if "cv and publications" in text:
        return "task", "task cv publications", True
    if "exercise scheduling" in text:
        return "task", "task exercise scheduling", True
    if "file organization" in text or "organizing files" in text:
        return "task", "task file organization", True
    if "scholarly lecture" in text and "schedul" in text:
        return "task", "task scholarly lecture scheduling", True

    # Focused technical topics.
    if "semantic search" in text:
        return "ai", "ai semantic search", False
    if "ai alignment" in text:
        return "ai", "ai alignment", False
    if "large language model" in text or "language model" in text:
        return "ai", "ai large language models", False
    if "machine learning" in text and "deep learning" in text:
        return "ai", "ai machine learning deep learning", False
    if "machine learning" in text:
        return "ai", "ai machine learning", False
    if "neural network" in text:
        return "ai", "ai neural networks", False
    if "quantum computing" in text:
        return "quantum", "quantum computing", False
    if "consciousness" in text and "ai" in text:
        return "ai", "ai consciousness", False
    if "ai" in text or family.startswith("ai ") or family == "ai":
        if "ethic" in text or "bias" in text or "regulat" in text:
            key = "ai ethics"
        elif "healthcare" in text or "drug" in text or "scientific discovery" in text:
            key = "ai applied science healthcare"
        elif "communication" in text:
            key = "ai communication"
        else:
            key = "ai general"
        return "ai", key, False

    # Other coherent domains.
    for category, words in {
        "productivity": ("productivity", "burnout", "time management", "focus", "work life balance"),
        "communication": ("communication", "social media", "language change", "nonverbal"),
        "culture": ("cultural", "greeting", "ancestor", "coming of age"),
        "history": ("history", "historical", "revolution", "german unification"),
        "science": ("scientific", "superconduct", "celestial", "biology", "photosynth"),
        "technology": ("technology", "smartphone", "tablet", "smart home", "internet"),
        "language": ("language", "idiom", "ephemeral", "collective noun"),
    }.items():
        if any(word in text for word in words):
            return category, f"{category} broad", False
    return "general", family or "general", False


def classify(left: dict, right: dict) -> tuple[bool, str, str]:
    lc, lk, lm = topic_key(left)
    rc, rk, rm = topic_key(right)
    if not lk or not rk:
        return False, "uncertain", "At least one segment has no stable main topic."
    if lk == rk:
        relation = "same_memory_item" if lm or rm else "same_specific_topic"
        return True, relation, f"Both segments express the same specific topic: {lk}."
    if lc == rc:
        if lc in {"project", "document", "task"}:
            relation = "same_project_different_aspect" if lc == "project" else "same_broad_family"
        elif lc in {"preference_books", "preference_music", "preference_actor", "preference_travel", "food"}:
            relation = "same_broad_family"
        elif lc == "ai":
            relation = "same_broad_family"
        else:
            relation = "shared_entity_only"
        return False, relation, f"Both segments belong to {lc}, but their specific topics differ ({lk} vs {rk})."
    return False, "unrelated", f"The specific topics belong to different broad categories ({lc} vs {rc})."


def main() -> None:
    labels = {}
    with SEGMENT_LABELS.open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            labels[row["segment_id"]] = row
    pairs = [json.loads(line) for line in PAIR_CANDIDATES.open(encoding="utf-8") if line.strip()]
    annotated = []
    for pair in pairs:
        left = labels[pair["left_segment_id"]]
        right = labels[pair["right_segment_id"]]
        same, relation, notes = classify(left, right)
        annotated.append({
            "pair_id": pair["pair_id"],
            "same_specific_topic": same,
            "relation": relation,
            "annotation_status": "direct_rule_assisted_preannotation",
            "annotator": "assistant_direct_pair_v1",
            "notes": notes,
        })
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("pair_labels_batch_*.jsonl"):
        old.unlink()
    for start in range(0, len(annotated), 20):
        path = OUT_DIR / f"pair_labels_batch_{start // 20 + 1:03d}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for row in annotated[start : start + 20]:
                fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    with PAIR_GOLD.open("w", encoding="utf-8") as fh:
        for row in annotated:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"wrote {len(annotated)} pair labels in {(len(annotated) + 19) // 20} batches")


if __name__ == "__main__":
    main()
