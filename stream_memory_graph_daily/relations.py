from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class MemoryRelation:
    source_memory_id: str
    target_memory_id: str
    relation_type: str
    weight: float = 1.0
    metadata: dict[str, Any] | None = None


class MemoryRelationStore:
    """Reserved upper-layer relation API; disabled in the one-layer version."""

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self._relations: list[MemoryRelation] = []

    def add(self, relation: MemoryRelation) -> None:
        if not self.enabled:
            raise RuntimeError("memory-memory relations are disabled in the one-layer graph")
        self._relations.append(relation)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "relations": [asdict(relation) for relation in self._relations],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryRelationStore":
        store = cls(enabled=bool(value.get("enabled", False)))
        store._relations = [MemoryRelation(**row) for row in value.get("relations", [])]
        return store

