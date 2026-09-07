from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            rows.append(value)
    return rows


def read_records(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if source.is_dir():
        rows = []
        for child in sorted(source.glob("*.json")):
            value = json.loads(child.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError(f"{child}: expected a JSON object")
            rows.append(value)
        return rows
    if source.suffix.casefold() == ".jsonl":
        return read_jsonl(source)
    value = json.loads(source.read_text(encoding="utf-8"))
    if isinstance(value, list):
        if not all(isinstance(row, dict) for row in value):
            raise ValueError(f"{source}: expected a list of objects")
        return value
    if isinstance(value, dict):
        return [value]
    raise ValueError(f"{source}: expected an object or list of objects")

