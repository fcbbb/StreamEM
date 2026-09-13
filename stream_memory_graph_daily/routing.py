from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .encoder import Encoder, unit_vector
from .models import MemoryRecord
from .retrieval import entity_overlap, extract_entities, lex_tokens


@dataclass(frozen=True)
class TopicWakeSignature:
    """Derived, in-memory routing data for one active memory."""

    memory_id: str
    level: int
    topic: str
    summary: str
    vector: np.ndarray
    keywords: frozenset[str]
    entities: frozenset[str]
    direct_member_representations: tuple[str, ...]


class TopicOwnerRouter:
    """Long-lived index of active memories for cross-layer candidate lookup.

    The router is deliberately separate from the community graph.  It stores
    no graph edges and does not run community detection.  Signatures are
    derived from ``MemoryRecord`` objects and can be rebuilt after loading a
    persisted pipeline state.
    """

    def __init__(self, encoder: Encoder) -> None:
        self.encoder = encoder
        self.signatures: dict[str, TopicWakeSignature] = {}

    @staticmethod
    def _direct_member_representations(memory: MemoryRecord) -> tuple[str, ...]:
        return tuple(
            str(member["representation"]).strip()
            for member in memory.direct_members
            if str(member.get("representation", "")).strip()
        )

    @staticmethod
    def _memory_text(memory: MemoryRecord, direct_members: tuple[str, ...]) -> str:
        values = [memory.topic, memory.summary, *direct_members]
        values.extend(
            f"{item.get('type', '')} {item.get('content', '')}"
            for item in [*memory.topic_context, *memory.user_memories]
        )
        return " ".join(value for value in values if str(value).strip())

    def register(self, memory: MemoryRecord) -> None:
        direct_members = self._direct_member_representations(memory)
        text = self._memory_text(memory, direct_members)
        vector = unit_vector(self.encoder.encode([memory.topic])[0])
        self.signatures[memory.memory_id] = TopicWakeSignature(
            memory_id=memory.memory_id,
            level=memory.level,
            topic=memory.topic,
            summary=memory.summary,
            vector=vector,
            keywords=frozenset(lex_tokens(text)),
            entities=frozenset(extract_entities(text)),
            direct_member_representations=direct_members,
        )

    def remove(self, memory_id: str) -> None:
        self.signatures.pop(str(memory_id), None)

    def replace(
        self,
        old_memory_ids: Iterable[str],
        new_memory: MemoryRecord,
    ) -> None:
        for memory_id in old_memory_ids:
            self.remove(memory_id)
        self.register(new_memory)

    def rebuild(self, memories: Iterable[MemoryRecord]) -> None:
        self.signatures.clear()
        for memory in memories:
            self.register(memory)

    def candidates(
        self,
        topic: str,
        *,
        summary: str = "",
        direct_members: Iterable[str] | None = None,
        k: int = 10,
    ) -> list[dict[str, Any]]:
        if k < 1:
            return []
        member_values = tuple(
            str(value).strip()
            for value in (direct_members or [])
            if str(value).strip()
        )
        query_text = " ".join([str(topic), str(summary), *member_values]).strip()
        query_vector = unit_vector(self.encoder.encode([str(topic)])[0])
        query_keywords = set(lex_tokens(query_text))
        query_entities = extract_entities(query_text)
        rows: list[dict[str, Any]] = []
        for signature in self.signatures.values():
            semantic_score = float(np.dot(query_vector, signature.vector))
            keyword_score = (
                len(query_keywords & signature.keywords) / len(query_keywords)
                if query_keywords
                else 0.0
            )
            entity_score = entity_overlap(query_entities, set(signature.entities))
            score = (
                0.7 * semantic_score
                + 0.2 * keyword_score
                + 0.1 * entity_score
            )
            rows.append(
                {
                    "memory_id": signature.memory_id,
                    "level": signature.level,
                    "topic": signature.topic,
                    "summary": signature.summary,
                    "score": float(score),
                    "semantic_score": semantic_score,
                    "keyword_score": float(keyword_score),
                    "entity_score": float(entity_score),
                }
            )
        rows.sort(key=lambda row: (-row["score"], row["memory_id"]))
        return rows[:k]

    def memory_ids(self) -> set[str]:
        return set(self.signatures)

    def __len__(self) -> int:
        return len(self.signatures)
