from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Mapping

from .config import DailyGraphConfig
from .graph import ActiveGraph, LayeredActiveGraph
from .models import MemoryRecord


def logical_date(value: str) -> date | None:
    """Parse a stored mention timestamp using the session calendar date."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        if "T" in text:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.date()
        return date.fromisoformat(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class PromotionCandidate:
    source_level: int
    target_level: int
    node_ids: tuple[str, ...]
    due_ids: tuple[str, ...]
    latest_mentioned_at: str


class LevelPromotionScheduler:
    """Select time-inactive lower-level memories for one-level promotion."""

    def __init__(self, config: DailyGraphConfig, *, max_level: int = 3) -> None:
        self.config = config
        self.max_level = max_level

    def inactivity_days(self, source_level: int) -> int | None:
        if source_level < 1 or source_level >= self.max_level:
            return None
        index = source_level - 1
        if index >= len(self.config.promotion_inactivity_days):
            return None
        return self.config.promotion_inactivity_days[index]

    def is_due(self, memory: MemoryRecord, as_of: str) -> bool:
        days = self.inactivity_days(memory.level)
        mentioned = logical_date(memory.last_mentioned_at)
        current = logical_date(as_of)
        if days is None or mentioned is None or current is None:
            return False
        return (current - mentioned).days >= days

    def due_ids(
        self,
        memories: Mapping[str, MemoryRecord],
        as_of: str,
        *,
        source_level: int | None = None,
        active_ids: set[str] | None = None,
    ) -> set[str]:
        return {
            memory_id
            for memory_id, memory in memories.items()
            if (active_ids is None or memory_id in active_ids)
            and (source_level is None or memory.level == source_level)
            and self.is_due(memory, as_of)
        }

    @staticmethod
    def latest_mention_at(memories: list[MemoryRecord]) -> str:
        dated = [
            (logical_date(memory.last_mentioned_at), memory.last_mentioned_at)
            for memory in memories
        ]
        dated = [(when, value) for when, value in dated if when is not None]
        if not dated:
            return ""
        return max(dated, key=lambda row: row[0])[0].isoformat()

    def candidates(
        self,
        layered_graph: LayeredActiveGraph,
        memories: Mapping[str, MemoryRecord],
        as_of: str,
        *,
        source_level: int,
        detect: Callable[[ActiveGraph, set[str]], list[set[str]]],
    ) -> list[PromotionCandidate]:
        """Return due components plus same-level neighbours as activity context.

        A graph edge is only context here. The caller still uses topic
        partitioning before promoting, so a false edge does not permanently
        protect an inactive memory.
        """

        active = layered_graph.graph(source_level)
        active_source_ids = {
            memory_id
            for memory_id in active.memory_ids()
            if memory_id in memories and memories[memory_id].level == source_level
        }
        due = self.due_ids(
            memories,
            as_of,
            source_level=source_level,
            active_ids=active_source_ids,
        )
        if not due:
            return []
        context = {memory_id for memory_id in due if memory_id in active.nodes}
        for memory_id in sorted(context):
            for neighbour in active.graph.neighbors(memory_id):
                memory = memories.get(neighbour)
                if memory is not None and memory.level == source_level:
                    context.add(neighbour)

        groups: list[set[str]] = []
        missing = due - context
        groups.extend({memory_id} for memory_id in sorted(missing))
        if context:
            detected_groups = [
                set(group) for group in detect(active, context) if set(group) & due
            ]
            # Preserve adjacent active nodes as purification context even if
            # the graph community detector separates a weak boundary edge.
            # The topic partitioner, not the raw edge, makes the final split.
            expanded_groups = []
            source_ids = set(
                memory_id
                for memory_id in context
                if memory_id in memories and memories[memory_id].level == source_level
            )
            for group in detected_groups:
                expanded = set(group)
                for memory_id in list(group):
                    expanded.update(
                        neighbour
                        for neighbour in active.graph.neighbors(memory_id)
                        if neighbour in source_ids
                    )
                expanded_groups.append(expanded)
            while expanded_groups:
                current = expanded_groups.pop(0)
                changed = True
                while changed:
                    changed = False
                    remaining = []
                    for other in expanded_groups:
                        if current & other:
                            current |= other
                            changed = True
                        else:
                            remaining.append(other)
                    expanded_groups = remaining
                groups.append(current)

        candidates: list[PromotionCandidate] = []
        for group in groups:
            source_ids = tuple(
                sorted(
                    memory_id
                    for memory_id in group
                    if memory_id in memories
                    and memories[memory_id].level == source_level
                )
            )
            due_ids = tuple(sorted(set(source_ids) & due))
            if not source_ids or not due_ids:
                continue
            source_memories = [memories[memory_id] for memory_id in source_ids]
            candidates.append(
                PromotionCandidate(
                    source_level=source_level,
                    target_level=source_level + 1,
                    node_ids=source_ids,
                    due_ids=due_ids,
                    latest_mentioned_at=self.latest_mention_at(source_memories),
                )
            )
        candidates.sort(key=lambda item: (item.source_level, item.node_ids))
        return candidates
