#!/usr/bin/env python3
"""Create a human-readable view for manual review of the 60-session labels."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ANNOTATION_FILE = ROOT / "annotations" / "initial_60.jsonl"
OUTPUT_FILE = ROOT / "annotations" / "initial_60_audit_view.md"
CONTEXT_OUTPUT_FILE = ROOT / "annotations" / "initial_60_boundary_context.md"


def speaker_label(value: str) -> str:
    return {"ai_agent": "assistant", "user_agent": "user"}.get(value, value)


def unit_number(unit_id: str) -> int:
    return int(unit_id.lstrip("u"))


def main() -> None:
    records = [
        json.loads(line)
        for line in ANNOTATION_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    lines = [
        "# 60 个 session 切割标注人工审计视图",
        "",
        "## 审计口径",
        "",
        "- 同一事实或局部目标下的附和、追问和回答可以留在同一段。",
        "- 出现另一个独立的实质事实或目标时切开。",
        "- 不要在助手问题和用户直接回答之间切开；若该问题属于新事件，应把边界放在问题之前。",
        "- 下方的 `⚠️` 只是优先检查提示，不是自动判错。",
        "",
        "## 优先检查：当前边界两侧为助手→用户的问答衔接",
        "",
        "| session | 边界 | 原因 | 左侧 | 右侧 | 审计结论 |",
        "|---:|---|---|---|---|---|",
    ]

    flagged = []
    for record in records:
        by_id = {unit["unit_id"]: unit for unit in record["units"]}
        for index, boundary in enumerate(record.get("boundaries", []), start=1):
            left = by_id[boundary["after_unit_id"]]
            right = by_id[boundary["before_unit_id"]]
            if left["speaker"] == "ai_agent" and right["speaker"] == "user_agent":
                flagged.append((record, index, boundary, left, right))

    for record, index, boundary, left, right in flagged:
        left_text = f"{left['unit_id']} [{speaker_label(left['speaker'])}] {left['text']}"
        right_text = f"{right['unit_id']} [{speaker_label(right['speaker'])}] {right['text']}"
        lines.append(
            f"| {record['session_id']} | {boundary['after_unit_id']} → "
            f"{boundary['before_unit_id']} | `{boundary['initial_reason']}` | "
            f"⚠️ {left_text} | {right_text} | 待审 |"
                )

    lines.extend(["", "## 全量逐段视图", ""])
    for record in records:
        lines.extend(
            [
                f"## session_{int(record['session_id']):04d} "
                f"({record.get('session_type')}/{record.get('operation') or 'none'})",
                "",
                f"状态：`{record.get('annotation_status')}`；"
                f"单位数：{len(record['units'])}；片段数：{len(record['segments'])}",
                "",
            ]
        )
        by_id = {unit["unit_id"]: unit for unit in record["units"]}
        boundaries = record.get("boundaries", [])
        for segment_index, segment in enumerate(record["segments"]):
            start = unit_number(segment["start_unit_id"])
            end = unit_number(segment["end_unit_id"])
            lines.extend(
                [
                    f"### {segment['segment_id']} `{segment['start_unit_id']}`–`{segment['end_unit_id']}` "
                    f"({segment['segment_type']})",
                    "",
                ]
            )
            for unit in record["units"]:
                number = unit_number(unit["unit_id"])
                if start <= number <= end:
                    lines.append(
                        f"- **{unit['unit_id']}** [{speaker_label(unit['speaker'])}] {unit['text']}"
                    )
            lines.append("")
            if segment_index < len(record["segments"]) - 1:
                boundary = boundaries[segment_index]
                left = by_id[boundary["after_unit_id"]]
                right = by_id[boundary["before_unit_id"]]
                flag = " ⚠️ 助手→用户衔接，优先检查" if (
                    left["speaker"] == "ai_agent" and right["speaker"] == "user_agent"
                ) else ""
                lines.extend(
                    [
                        f"> **当前边界** `{boundary['after_unit_id']}` → "
                        f"`{boundary['before_unit_id']}` "
                        f"（`{boundary['initial_reason']}`）{flag}",
                        "> 审计结论：待审（保留 / 删除 / 前移 / 后移）",
                        "",
                    ]
                )

    OUTPUT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
 
    context_lines = [
        "# 60 个 session 边界上下文审计视图",
        "",
        "每个边界展示边界前 2 个语义单元、边界两侧单元和边界后 2 个语义单元。",
        "边界标记 `✂` 位于两条语义单元之间；请在“审计结论”处填写：保留 / 删除 / 前移 / 后移。",
        "",
    ]
    for record in records:
        units = record["units"]
        by_id = {unit["unit_id"]: index for index, unit in enumerate(units)}
        context_lines.extend(
            [
                f"## session_{int(record['session_id']):04d} "
                f"({record.get('session_type')}/{record.get('operation') or 'none'})",
                "",
            ]
        )
        for boundary_index, boundary in enumerate(record.get("boundaries", []), start=1):
            left_index = by_id[boundary["after_unit_id"]]
            right_index = by_id[boundary["before_unit_id"]]
            context_lines.extend(
                [
                    f"### 边界 {boundary_index}: `{boundary['after_unit_id']}` → "
                    f"`{boundary['before_unit_id']}` (`{boundary['initial_reason']}`)",
                    "",
                ]
            )
            start = max(0, left_index - 2)
            end = min(len(units), right_index + 3)
            for index in range(start, end):
                unit = units[index]
                context_lines.append(
                    f"- **{unit['unit_id']}** [{speaker_label(unit['speaker'])}] {unit['text']}"
                )
                if index == left_index:
                    context_lines.append("- ✂ **当前边界**")
            context_lines.extend(
                [
                    "",
                    "**审计结论：** 待审（保留 / 删除 / 前移 / 后移）",
                    "",
                ]
            )

    CONTEXT_OUTPUT_FILE.write_text("\n".join(context_lines) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT_FILE}")
    print(f"wrote {CONTEXT_OUTPUT_FILE}")
    print(f"sessions={len(records)} flagged_assistant_to_user_boundaries={len(flagged)}")


if __name__ == "__main__":
    main()
