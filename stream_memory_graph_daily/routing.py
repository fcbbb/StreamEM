from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

import numpy as np

from .encoder import Encoder, unit_vector
from .llm import JsonLLM
from .models import MemoryRecord
from .prompts import TOPIC_OWNER_ROUTING_PROMPT
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


@dataclass(frozen=True)
class OwnerDecision:
    """The only decision exposed by the owner-routing stage."""

    owner_memory_id: str | None
    reason: str
    candidates: tuple[dict[str, Any], ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_memory_id": self.owner_memory_id,
            "reason": self.reason,
            "candidates": [dict(row) for row in self.candidates],
            **({"error": self.error} if self.error else {}),
        }


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
        min_level: int | None = None,
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
            if min_level is not None and signature.level < min_level:
                continue
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

    @staticmethod
    def _memory_prompt_dict(memory: MemoryRecord) -> dict[str, Any]:
        value = memory.prompt_dict()
        return {
            "memory_id": memory.memory_id,
            "level": memory.level,
            "topic": value["topic"],
            "summary": value["summary"],
            "topic_context": value["topic_context"],
            "user_memories": value["user_memories"],
        }

    def decide(
        self,
        provisional: MemoryRecord,
        memories: Mapping[str, MemoryRecord],
        *,
        llm: JsonLLM | None,
        top_k: int = 5,
        audit_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> OwnerDecision:
        """Recall active L2+ candidates, then ask one LLM to compare them all."""

        candidates = self.candidates(
            provisional.topic,
            summary=provisional.summary,
            direct_members=(
                str(member["representation"])
                for member in provisional.direct_members
            ),
            k=top_k,
            min_level=2,
        )
        candidate_ids = {str(row["memory_id"]) for row in candidates}
        if not candidates:
            decision = OwnerDecision(None, "new_topic", tuple(candidates))
            if audit_sink:
                audit_sink({"decision": decision.to_dict(), "candidates": []})
            return decision
        if llm is None:
            decision = OwnerDecision(None, "uncertain", tuple(candidates), "llm_unavailable")
            if audit_sink:
                audit_sink({"decision": decision.to_dict(), "candidates": candidates})
            return decision

        candidate_payload = [
            self._memory_prompt_dict(memories[memory_id])
            for memory_id in [str(row["memory_id"]) for row in candidates]
            if memory_id in memories
        ]
        payload = {
            "provisional_l1": self._memory_prompt_dict(provisional),
            "candidates": candidate_payload,
        }
        raw: Any = None
        try:
            raw = llm.complete(
                TOPIC_OWNER_ROUTING_PROMPT,
                "INPUT DATA\n" + json.dumps(payload, ensure_ascii=False, indent=2),
            )
            if not isinstance(raw, dict) or set(raw) != {"owner_memory_id", "reason"}:
                raise ValueError("owner routing output fields must be exactly owner_memory_id and reason")
            owner = raw["owner_memory_id"]
            reason = raw["reason"]
            if owner is not None and (
                not isinstance(owner, str) or owner.strip() not in candidate_ids
            ):
                raise ValueError("owner_memory_id must be null or one recalled candidate")
            if reason not in {"same_topic", "new_topic", "ambiguous", "uncertain"}:
                raise ValueError("owner routing reason is not in the allowed enum")
            if reason == "same_topic" and owner is None:
                raise ValueError("same_topic requires an owner_memory_id")
            if reason != "same_topic" and owner is not None:
                raise ValueError("non same_topic routing must not select an owner")
            decision = OwnerDecision(owner.strip() if isinstance(owner, str) else None, reason, tuple(candidates))
        except Exception as exc:
            decision = OwnerDecision(None, "uncertain", tuple(candidates), str(exc))
        if audit_sink:
            audit_sink(
                {
                    "request": payload,
                    "response": raw,
                    "decision": decision.to_dict(),
                    "candidates": candidates,
                }
            )
        return decision

    def memory_ids(self) -> set[str]:
        return set(self.signatures)

    def __len__(self) -> int:
        return len(self.signatures)
