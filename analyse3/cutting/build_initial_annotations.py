#!/usr/bin/env python3
"""Build the first-pass 60-session event-boundary annotations.

The source conversations are kept unchanged.  This script only derives stable
semantic-unit IDs and applies the manually reviewed first-pass boundary map.
"""

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "conversations"
OUT_DIR = ROOT / "annotations"
OUT_FILE = OUT_DIR / "initial_60.jsonl"


# A boundary is represented by the first sentence of the new event:
#   (turn_number, sentence_number_within_turn)
# sentence_number is 1-based.  Omitting a session means one event for the
# whole session.  This map intentionally covers sessions 1..60, which form a
# continuous development subset and contain all metadata categories present
# in the 158-session source set.
BOUNDARIES = {
    2: [(17, 1)],
    4: [(17, 1)],
    5: [(18, 1)],
    6: [(15, 1)],
    9: [(11, 1)],
    10: [(10, 2)],
    11: [(20, 1)],
    12: [(13, 1)],
    16: [(15, 2)],  # assistant turn: answer acknowledgement -> new question
    17: [(12, 1)],
    18: [(19, 1)],
    19: [(19, 1)],
    20: [(14, 2)],
    21: [(14, 1)],
    23: [(12, 2)],
    26: [(10, 1)],
    27: [(18, 1)],
    28: [(12, 1)],
    29: [(16, 1)],
    31: [(15, 1)],
    34: [(14, 1)],
    35: [(13, 1)],
    36: [(12, 1)],
    37: [(10, 2)],
    38: [(14, 1)],
    40: [(16, 1)],
    41: [(12, 1)],
    42: [(17, 1)],
    44: [(11, 1)],
    45: [(12, 1)],
    46: [(12, 1)],
    47: [(17, 1)],
    50: [(11, 1)],
    51: [(15, 2)],  # assistant turn: privacy answer -> book-preference question
    52: [(13, 1)],
    53: [(12, 1)],
    57: [(15, 2)],
    59: [(11, 1)],
    60: [(13, 1)],
}


# First substantive unit after the opening pleasantries.  The greeting itself
# remains in segment 1, while this unit starts the first useful interaction.
MAIN_STARTS = {
    1: (3, 2), 2: (2, 3), 3: (3, 2), 4: (3, 2), 5: (3, 1),
    6: (1, 2), 7: (3, 2), 8: (3, 2), 9: (3, 1), 10: (3, 2),
    11: (3, 2), 12: (3, 1), 13: (3, 1), 14: (3, 2), 15: (3, 2),
    16: (3, 2), 17: (3, 2), 18: (3, 2), 19: (2, 3), 20: (3, 1),
    21: (1, 2), 22: (3, 2), 23: (3, 2), 24: (3, 1), 25: (3, 2),
    26: (3, 2), 27: (3, 2), 28: (2, 3), 29: (3, 2), 30: (3, 1),
    31: (3, 2), 32: (3, 2), 33: (3, 2), 34: (2, 3), 35: (4, 1),
    36: (3, 2), 37: (3, 2), 38: (3, 2), 39: (3, 2), 40: (2, 1),
    41: (5, 2), 42: (3, 1), 43: (3, 2), 44: (3, 2), 45: (4, 1),
    46: (4, 2), 47: (3, 2), 48: (3, 1), 49: (3, 2), 50: (3, 2),
    51: (3, 1), 52: (3, 1), 53: (5, 1), 54: (3, 1), 55: (3, 1),
    56: (3, 1), 57: (3, 2), 58: (3, 1), 59: (3, 2), 60: (6, 2),
}


# First closing/farewell unit.  The last event is deliberately kept separate
# so it can be filtered without removing the final substantive response.
GOODBYE_STARTS = {
    1: (21, 1), 2: (20, 1), 3: (10, 1), 4: (19, 1), 5: (22, 2),
    6: (17, 1), 7: (13, 1), 8: (26, 1), 9: (13, 1), 10: (14, 1),
    11: (22, 1), 12: (15, 1), 13: (13, 1), 14: (10, 1), 15: (14, 1),
    16: (17, 2), 17: (14, 1), 18: (22, 1), 19: (21, 1), 20: (16, 1),
    21: (16, 1), 22: (12, 1), 23: (15, 2), 24: (14, 1), 25: (12, 1),
    26: (13, 2), 27: (20, 2), 28: (16, 1), 29: (21, 2), 30: (14, 1),
    31: (17, 1), 32: (12, 2), 33: (18, 1), 34: (18, 1), 35: (15, 1),
    36: (14, 1), 37: (12, 1), 38: (16, 1), 39: (11, 1), 40: (18, 1),
    41: (14, 1), 42: (19, 1), 43: (17, 1), 44: (13, 1), 45: (14, 1),
    46: (15, 1), 47: (19, 1), 48: (19, 1), 49: (26, 1), 50: (15, 1),
    51: (18, 1), 52: (17, 1), 53: (14, 1), 54: (11, 2), 55: (18, 1),
    56: (18, 1), 57: (17, 2), 58: (9, 1), 59: (13, 2), 60: (15, 1),
}


_ABBREVIATIONS = {
    "Mr.", "Mrs.", "Ms.", "Dr.", "Prof.", "e.g.", "i.e.", "etc.",
    "U.S.", "AI.", "GPT-3.",
}


def split_sentences(text: str) -> list[str]:
    """Split ordinary English turns into sentence-level semantic units.

    The data is generated English prose.  This lightweight splitter avoids
    splitting common abbreviations and decimals, while preserving text as
    closely as possible for human review.  A turn with no sentence-ending
    punctuation remains one unit.
    """

    protected = text
    placeholders = {}
    for index, abbreviation in enumerate(sorted(_ABBREVIATIONS, key=len, reverse=True)):
        token = f"__ABBR_{index}__"
        if abbreviation in protected:
            protected = protected.replace(abbreviation, token)
            placeholders[token] = abbreviation

    # Do not split decimal numbers or punctuation that is not followed by a
    # new sentence-like token.
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'])", protected.strip())
    result = []
    for part in parts:
        for token, abbreviation in placeholders.items():
            part = part.replace(token, abbreviation)
        part = part.strip()
        if part:
            result.append(part)
    return result or [text.strip()]


def make_units(conversation: list[dict]) -> list[dict]:
    units = []
    next_unit = 1
    for message in conversation:
        turn = int(message["turn"])
        message_id = f"m{turn:03d}"
        sentences = split_sentences(message["message"])
        for sentence_index, sentence in enumerate(sentences, start=1):
            units.append(
                {
                    "unit_id": f"u{next_unit:03d}",
                    "message_id": message_id,
                    "turn": turn,
                    "sentence_index": sentence_index,
                    "speaker": message["speaker"],
                    "text": sentence,
                }
            )
            next_unit += 1
    return units


def build_segments(
    units: list[dict], boundary_specs: list[tuple[int, int, str]]
) -> tuple[list[dict], list[dict]]:
    starts = []
    for turn, sentence_index, reason in boundary_specs:
        matches = [
            index
            for index, unit in enumerate(units)
            if unit["turn"] == turn and unit["sentence_index"] == sentence_index
        ]
        if len(matches) != 1:
            raise ValueError(f"Cannot resolve boundary ({turn}, {sentence_index}); matches={matches}")
        starts.append((matches[0], reason))

    starts = [(0, "session_start"), *starts]
    starts = sorted({index: reason for index, reason in starts}.items())
    segments = []
    boundaries = []
    for segment_index, (start, reason) in enumerate(starts, start=1):
        end = starts[segment_index][0] - 1 if segment_index < len(starts) else len(units) - 1
        if reason == "session_start":
            segment_type = "greeting"
        elif reason == "substantive_to_goodbye":
            segment_type = "goodbye"
        else:
            segment_type = "substantive"
        segments.append(
            {
                "segment_id": f"seg{segment_index:03d}",
                "start_unit_id": units[start]["unit_id"],
                "end_unit_id": units[end]["unit_id"],
                "segment_type": segment_type,
            }
        )
        if segment_index > 1:
            previous = starts[segment_index - 1][0] - 1
            boundaries.append(
                {
                    "after_unit_id": units[previous]["unit_id"],
                    "before_unit_id": units[start]["unit_id"],
                    "boundary": True,
                    "initial_reason": reason,
                }
            )
    return segments, boundaries


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    records = []
    with OUT_FILE.open("w", encoding="utf-8") as output:
        for session_id in range(1, 61):
            source = DATA_DIR / f"session_{session_id:04d}.json"
            data = json.loads(source.read_text(encoding="utf-8"))
            units = make_units(data["conversation"])
            boundary_specs = [
                (*MAIN_STARTS[session_id], "greeting_to_substantive"),
                *[
                    (*spec, "new_local_interaction_goal")
                    for spec in BOUNDARIES.get(session_id, [])
                ],
                (*GOODBYE_STARTS[session_id], "substantive_to_goodbye"),
            ]
            segments, boundaries = build_segments(units, boundary_specs)
            record = {
                "session_id": data["session_id"],
                "source_file": str(source.relative_to(ROOT)),
                "session_type": data.get("session_type"),
                "operation": data.get("operation"),
                "annotation_status": "initial_for_review",
                "annotator": "assistant_initial",
                "units": units,
                "boundaries": boundaries,
                "segments": segments,
            }
            output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            records.append(record)

    review_index = OUT_DIR / "initial_60_review_index.md"
    with review_index.open("w", encoding="utf-8") as output:
        output.write("# 初版 60 个 session 审核索引\n\n")
        output.write("边界的右侧是新事件起点；`turn/sentence` 用于回到原始对话定位。\n\n")
        for record in records:
            units = record["units"]
            by_id = {unit["unit_id"]: unit for unit in units}
            output.write(
                f"## session_{int(record['session_id']):04d} "
                f"({record['session_type']}/{record['operation'] or 'none'})\n\n"
            )
            output.write(
                f"- units: {len(units)}; segments: {len(record['segments'])}; "
                f"types: {', '.join(segment['segment_type'] for segment in record['segments'])}; "
                f"status: `{record['annotation_status']}`\n"
            )
            if not record["boundaries"]:
                output.write("- 边界：无（整个 session 为一个事件）\n\n")
                continue
            for index, boundary in enumerate(record["boundaries"], start=1):
                left = by_id[boundary["after_unit_id"]]
                right = by_id[boundary["before_unit_id"]]
                output.write(
                    f"- 边界 {index}：after `{left['unit_id']}` "
                    f"(turn {left['turn']}/{left['sentence_index']}) → "
                    f"before `{right['unit_id']}` "
                    f"(turn {right['turn']}/{right['sentence_index']})\n"
                    f"  - 左：{left['text']}\n"
                    f"  - 右：{right['text']}\n"
                )
            output.write("\n")

    print(f"wrote {OUT_FILE}")
    print(f"wrote {review_index}")
    print(f"sessions=60 boundaries={sum(len(BOUNDARIES.get(i, [])) for i in range(1, 61))}")


if __name__ == "__main__":
    main()
