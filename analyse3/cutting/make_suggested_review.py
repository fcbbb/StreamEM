#!/usr/bin/env python3
"""Create a suggested full boundary review without changing the annotations."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ANNOTATION_FILE = ROOT / "annotations" / "initial_60.jsonl"
OUTPUT_FILE = ROOT / "annotations" / "initial_60_boundary_context_suggested.md"


# Decisions manually supplied in the user's edited review view.
USER_DECISIONS = {
    (1, 1): "保留", (1, 2): "保留",
    (2, 1): "保留", (2, 2): "保留", (2, 3): "保留", (2, 4): "后移",
    (3, 1): "保留", (3, 2): "后移",
    (4, 1): "保留", (4, 2): "保留", (4, 3): "保留",
    (5, 1): "前移", (5, 2): "保留", (5, 3): "删除",
    (6, 1): "删除", (6, 2): "保留", (6, 3): "保留",
    (7, 1): "保留", (7, 2): "保留",
    (8, 1): "保留", (8, 2): "后移",
}


def number(unit_id: str) -> int:
    return int(unit_id.lstrip("u"))


def label(speaker: str) -> str:
    return {"ai_agent": "assistant", "user_agent": "user"}.get(speaker, speaker)


def suggested_decision(session_id: int, boundary_index: int, boundary: dict, left: dict, right: dict) -> str:
    supplied = USER_DECISIONS.get((session_id, boundary_index))
    if supplied:
        return supplied

    reason = boundary["initial_reason"]
    if reason == "new_local_interaction_goal":
        return "保留"

    # Greeting and goodbye placement is not the core audit criterion.  Unless
    # the user has supplied a decision, leave these boundaries unchanged and
    # focus review effort on fact/goal purity.
    return "保留"


def main() -> None:
    records = [
        json.loads(line)
        for line in ANNOTATION_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    lines = [
        "# 60 个 session 边界上下文审计建议版",
        "",
        "这是基于用户已加粗的前 8 个 session 和归纳规范生成的建议，不会覆盖正式标注。",
        "每个边界显示前后各 2 个语义单元；请重点复核建议的保留 / 删除 / 前移 / 后移。",
        "",
    ]

    for record in records:
        units = record["units"]
        by_id = {unit["unit_id"]: index for index, unit in enumerate(units)}
        lines.extend([
            f"## session_{int(record['session_id']):04d} "
            f"({record.get('session_type')}/{record.get('operation') or 'none'})",
            "",
        ])
        for boundary_index, boundary in enumerate(record.get("boundaries", []), start=1):
            left_index = by_id[boundary["after_unit_id"]]
            right_index = by_id[boundary["before_unit_id"]]
            left = units[left_index]
            right = units[right_index]
            decision = suggested_decision(record["session_id"], boundary_index, boundary, left, right)
            same_message = left["message_id"] == right["message_id"]
            flag = "；同一 message 内" if same_message else ""
            lines.extend([
                f"### 边界 {boundary_index}: `{boundary['after_unit_id']}` → "
                f"`{boundary['before_unit_id']}` (`{boundary['initial_reason']}`{flag})",
                "",
            ])
            start = max(0, left_index - 2)
            end = min(len(units), right_index + 3)
            for index in range(start, end):
                unit = units[index]
                lines.append(
                    f"- **{unit['unit_id']}** [{label(unit['speaker'])}] {unit['text']}"
                )
                if index == left_index:
                    lines.append("- ✂ **当前边界**")
            lines.extend([
                "",
                f"**建议：** **{decision}**",
                "**人工结论：** 待审（可改为：保留 / 删除 / 前移 / 后移）",
                "",
            ])

    OUTPUT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT_FILE}")
    print(f"sessions={len(records)}")


if __name__ == "__main__":
    main()
