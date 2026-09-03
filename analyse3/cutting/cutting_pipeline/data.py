from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from ..build_initial_annotations import make_units as build_units
from .config import DATA_DIR


def load_dotenv(path: Path, override: bool = True) -> None:
    """Load simple KEY=VALUE entries from the selected project env file."""

    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and (override or key not in os.environ):
            os.environ[key] = value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected an object")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def load_conversations(data_dir: Path = DATA_DIR) -> dict[int, dict[str, Any]]:
    conversations = {}
    for path in sorted(data_dir.glob("session_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        conversations[int(data["session_id"])] = data
    if not conversations:
        raise ValueError(f"no session_*.json files found in {data_dir}")
    return conversations


def load_annotations(path: Path) -> dict[int, dict[str, Any]]:
    return {int(row["session_id"]): row for row in read_jsonl(path)}


def make_units(conversation: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reuse the unitizer used to create the checked-in annotations."""
    return build_units(conversation)


def validate_annotation_units(
    annotation: dict[str, Any], conversation: dict[str, Any]
) -> list[dict[str, Any]]:
    annotated_units = annotation.get("units")
    generated_units = make_units(conversation["conversation"])
    if not isinstance(annotated_units, list) or not annotated_units:
        raise ValueError(f"session {conversation['session_id']}: annotation has no units")
    fields = ("unit_id", "message_id", "turn", "sentence_index", "speaker", "text")
    annotated_projection = [tuple(unit.get(field) for field in fields) for unit in annotated_units]
    generated_projection = [tuple(unit.get(field) for field in fields) for unit in generated_units]
    if annotated_projection != generated_projection:
        raise ValueError(
            f"session {conversation['session_id']}: annotation units do not match source unitization"
        )
    return annotated_units
