from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .anchoring import AnchorExtractor
from .community import ConstrainedCommunityPlanner, PlannedCommunity, stable_group_id
from .config import DailyGraphConfig
from .cutting import ConversationCutter
from .encoder import Encoder, HashEncoder
from .graph import ActiveGraph
from .llm import JsonLLM
from .memory import MemoryService, utc_now
from .models import BoundaryRecord, MemoryRecord, SegmentRecord
from .purification import CommunityPurifier
from .relations import MemoryRelationStore
from .retrieval import BM25, entity_overlap, extract_entities, lex_tokens, rrf_fuse


def normalize_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        raise ValueError("event_date is required")
    if "T" in text:
        text = text.split("T", 1)[0]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid event_date {value!r}; expected YYYY-MM-DD") from exc


class DailyMemoryGraph:
    """End-to-end daily streaming memory graph.

    Date changes checkpoint the completed previous date before any segment from
    the new date enters the graph. Successful communities are compressed into
    one memory node whose complete active representation is ``memory.topic``.
    """

    schema_version = "daily_topic_memory_graph_v1"

    def __init__(
        self,
        *,
        llm: JsonLLM | None = None,
        encoder: Encoder | None = None,
        config: DailyGraphConfig | None = None,
    ) -> None:
        self.config = config or DailyGraphConfig()
        self.llm = llm
        self.active_graph = ActiveGraph(encoder or HashEncoder(), self.config)
        self.planner = ConstrainedCommunityPlanner(self.config)
        self.stage_audit: list[dict[str, Any]] = []
        self.cutter = ConversationCutter(llm, audit_sink=self._audit_stage)
        self.anchor_extractor = AnchorExtractor(llm, audit_sink=self._audit_stage)
        self.community_purifier = CommunityPurifier(llm, audit_sink=self._audit_stage)
        self.memory_service = MemoryService(llm, audit_sink=self._audit_stage)
        self.relations = MemoryRelationStore(enabled=False)

        self.segments: dict[str, SegmentRecord] = {}
        self.memories: dict[str, MemoryRecord] = {}
        self.boundaries: dict[str, BoundaryRecord] = {}
        self.skipped_segments: list[dict[str, Any]] = []
        self.pending_segment_ids: set[str] = set()
        self.cannot_link_memory_pairs: set[tuple[str, str]] = set()
        self.current_date: str | None = None
        self.last_checkpoint_date: str | None = None
        self.checkpoint_count = 0
        self.trace: list[dict[str, Any]] = []
        self.llm_errors: list[dict[str, Any]] = []

    def _trace(self, event: str, **values: Any) -> None:
        self.trace.append({"at": utc_now(), "event": event, **values})

    def _audit_stage(self, stage: str, action: str, values: dict[str, Any]) -> None:
        """Persist replay-oriented inputs and outputs separately from the compact trace."""

        self.stage_audit.append(
            {
                "stage_id": f"stage:{len(self.stage_audit) + 1:08d}",
                "at": utc_now(),
                "stage": stage,
                "action": action,
                **values,
            }
        )

    def _state_snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe state snapshot without recursive audit logs."""

        return {
            "current_date": self.current_date,
            "last_checkpoint_date": self.last_checkpoint_date,
            "checkpoint_count": self.checkpoint_count,
            "segments": {
                key: value.to_dict() for key, value in sorted(self.segments.items())
            },
            "memories": {
                key: value.to_dict() for key, value in sorted(self.memories.items())
            },
            "boundaries": {
                key: value.to_dict() for key, value in sorted(self.boundaries.items())
            },
            "pending_segment_ids": sorted(self.pending_segment_ids),
            "active_graph": self.active_graph.to_dict(),
        }

    def _date_transition(self, event_date: str) -> dict[str, Any] | None:
        if self.current_date is None:
            self.current_date = event_date
            return None
        if event_date < self.current_date:
            raise ValueError(
                f"out-of-order event_date {event_date}; current stream date is {self.current_date}"
            )
        if event_date == self.current_date:
            return None
        previous_date = self.current_date
        result = self.checkpoint(reason="event_date_change", checkpoint_date=previous_date)
        self.current_date = event_date
        return result

    def ingest_segment(self, value: SegmentRecord | dict[str, Any]) -> dict[str, Any]:
        if isinstance(value, SegmentRecord):
            segment = value
        else:
            row = dict(value)
            segment_id = str(row.get("segment_id", row.get("id", ""))).strip()
            text = str(row.get("text", row.get("raw_text", ""))).strip()
            event_date = normalize_date(row.get("event_date", row.get("date")))
            anchor = str(row.get("anchor", row.get("selected_anchor", ""))).strip()
            if not anchor:
                extracted = self.anchor_extractor.extract(text)
                if extracted is None:
                    skipped = {
                        "segment_id": segment_id,
                        "event_date": event_date,
                        "text": text,
                        "reason": "anchor_is_null",
                    }
                    self.skipped_segments.append(skipped)
                    self._trace("segment_skipped", **skipped)
                    self._audit_stage("segment_filter", "skipped", skipped)
                    return {"status": "skipped", **skipped}
                anchor = extracted
            known = {
                "segment_id", "id", "text", "raw_text", "event_date", "date", "anchor",
                "selected_anchor", "conversation_id", "group_id", "session_id", "segment_index",
                "start_unit_id", "end_unit_id", "metadata",
            }
            metadata = dict(row.get("metadata") or {})
            metadata.update({key: item for key, item in row.items() if key not in known})
            segment = SegmentRecord(
                segment_id=segment_id,
                text=text,
                anchor=anchor,
                event_date=event_date,
                conversation_id=str(
                    row.get("conversation_id", row.get("group_id", row.get("session_id", "")))
                ),
                segment_index=int(row.get("segment_index", 0)),
                start_unit_id=row.get("start_unit_id"),
                end_unit_id=row.get("end_unit_id"),
                metadata=metadata,
            )
        segment.event_date = normalize_date(segment.event_date)
        if segment.segment_id in self.segments:
            return {"status": "duplicate", "segment_id": segment.segment_id}
        checkpoint = self._date_transition(segment.event_date)
        self.segments[segment.segment_id] = segment
        self.pending_segment_ids.add(segment.segment_id)
        self.active_graph.add_segment(segment.segment_id, segment.anchor)
        self._trace(
            "segment_added",
            segment_id=segment.segment_id,
            event_date=segment.event_date,
            anchor=segment.anchor,
        )
        return {
            "status": "added",
            "segment_id": segment.segment_id,
            "checkpoint": checkpoint,
        }

    def ingest_segments(self, values: Iterable[SegmentRecord | dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.ingest_segment(value) for value in values]

    def ingest_conversation(
        self,
        value: dict[str, Any],
        *,
        event_date: str | None = None,
    ) -> dict[str, Any]:
        conversation_id = str(
            value.get("conversation_id", value.get("session_id", value.get("id", "conversation")))
        )
        self._audit_stage(
            "conversation_input",
            "received",
            {"conversation_id": conversation_id, "input": dict(value)},
        )
        resolved_date = normalize_date(event_date or value.get("event_date") or value.get("date"))
        messages = value.get("conversation", value.get("messages"))
        if not isinstance(messages, list):
            raise ValueError(f"conversation {conversation_id} has no message list")
        cut_segments = self.cutter.cut(conversation_id, messages)
        self._audit_stage(
            "cutting",
            "normalized_result",
            {
                "conversation_id": conversation_id,
                "output": [asdict(cut) for cut in cut_segments],
            },
        )
        anchors = self.anchor_extractor.extract_many(
            [
                {"segment_id": cut.segment_id, "text": cut.text}
                for cut in cut_segments
            ]
        )
        results = []
        for cut in cut_segments:
            anchor = anchors[cut.segment_id]
            if anchor is None:
                skipped = {
                    "segment_id": cut.segment_id,
                    "event_date": resolved_date,
                    "text": cut.text,
                    "reason": "anchor_is_null",
                }
                self.skipped_segments.append(skipped)
                self._trace("segment_skipped", **skipped)
                self._audit_stage("segment_filter", "skipped", skipped)
                results.append({"status": "skipped", **skipped})
                continue
            results.append(
                self.ingest_segment(
                    SegmentRecord(
                        segment_id=cut.segment_id,
                        text=cut.text,
                        anchor=anchor,
                        event_date=resolved_date,
                        conversation_id=conversation_id,
                        segment_index=cut.segment_index,
                        start_unit_id=cut.start_unit_id,
                        end_unit_id=cut.end_unit_id,
                        metadata={"unit_ids": cut.unit_ids},
                    )
                )
            )
        return {
            "conversation_id": conversation_id,
            "event_date": resolved_date,
            "cut_segments": len(cut_segments),
            "results": results,
        }

    def _mark_boundaries(
        self, boundary_scores: dict[str, dict[str, float]], checkpoint_date: str
    ) -> None:
        active_segment_ids = self.active_graph.segment_ids()
        for segment_id in active_segment_ids:
            if segment_id not in boundary_scores and segment_id in self.boundaries:
                self.boundaries.pop(segment_id, None)
                self.segments[segment_id].status = "active"
        for segment_id, scores in boundary_scores.items():
            if segment_id not in self.segments:
                continue
            previous = self.boundaries.get(segment_id)
            self.boundaries[segment_id] = BoundaryRecord(
                segment_id=segment_id,
                candidate_memories={key: float(score) for key, score in scores.items()},
                reason="multiple_memory_support_with_insufficient_margin",
                first_seen_date=(previous.first_seen_date if previous else checkpoint_date),
                last_checked_date=checkpoint_date,
                attempts=(previous.attempts + 1 if previous else 1),
            )
            self.segments[segment_id].status = "boundary"

    def _archive_segments(self, segment_ids: set[str], status: str, memory_id: str | None) -> None:
        for segment_id in segment_ids:
            segment = self.segments[segment_id]
            segment.status = status  # type: ignore[assignment]
            segment.memory_id = memory_id
            self.boundaries.pop(segment_id, None)
        self.active_graph.remove_nodes(segment_ids)

    def _memory_member_anchors(self, memory: MemoryRecord) -> list[str]:
        anchors = [
            self.segments[segment_id].anchor
            for segment_id in memory.source_segments
            if segment_id in self.segments
        ]
        return anchors or list(memory.source_anchors)

    def _apply_memory_group(
        self,
        group: PlannedCommunity,
        segments: list[SegmentRecord],
        checkpoint_date: str,
        changes: list[dict[str, Any]],
        retry_ids: set[str],
        *,
        parent_community_id: str,
        purification_group_id: str,
    ) -> None:
        """Apply one purified group and keep failures retryable at group granularity."""

        segment_ids = {segment.segment_id for segment in segments}
        try:
            if group.memory_ids:
                if len(group.memory_ids) != 1:
                    raise AssertionError("planner emitted a multi-memory community")
                memory_id = next(iter(group.memory_ids))
                existing = self.memories[memory_id]
                existing_before = existing.to_dict()
                updated, decision = self.memory_service.fuse(
                    group.community_id, existing, segments
                )
                self.memories[memory_id] = updated
                self.active_graph.update_memory_topic(
                    memory_id,
                    updated.topic,
                    self._memory_member_anchors(updated),
                )
                self._archive_segments(segment_ids, "compressed", memory_id)
                self._audit_stage(
                    "memory_apply",
                    "fused",
                    {
                        "checkpoint_date": checkpoint_date,
                        "community_id": group.community_id,
                        "parent_community_id": parent_community_id,
                        "purification_group_id": purification_group_id,
                        "input": {
                            "memory": existing_before,
                            "segments": [segment.to_dict() for segment in segments],
                        },
                        "output": {
                            "memory": updated.to_dict(),
                            "decision": decision,
                        },
                    },
                )
                changes.append(
                    {
                        "community_id": group.community_id,
                        "parent_community_id": parent_community_id,
                        "purification_group_id": purification_group_id,
                        "source": group.source,
                        "action": "memory_fused",
                        "memory_id": memory_id,
                        "decision": decision["decision"],
                        "operations": decision["operations"],
                        "topic_source_segment_ids": decision[
                            "topic_source_segment_ids"
                        ],
                        "summary_source_segment_ids": decision[
                            "summary_source_segment_ids"
                        ],
                        "segment_ids": sorted(segment_ids),
                    }
                )
                return

            memory = self.memory_service.extract(group.community_id, segments)
            if memory is None:
                self._archive_segments(segment_ids, "no_memory", None)
                self._audit_stage(
                    "memory_apply",
                    "no_memory",
                    {
                        "checkpoint_date": checkpoint_date,
                        "community_id": group.community_id,
                        "parent_community_id": parent_community_id,
                        "purification_group_id": purification_group_id,
                        "input": {
                            "segments": [segment.to_dict() for segment in segments]
                        },
                        "output": None,
                    },
                )
                changes.append(
                    {
                        "community_id": group.community_id,
                        "parent_community_id": parent_community_id,
                        "purification_group_id": purification_group_id,
                        "source": group.source,
                        "action": "no_memory_archived",
                        "segment_ids": sorted(segment_ids),
                    }
                )
                return

            if memory.memory_id in self.memories:
                raise ValueError(f"generated duplicate memory_id {memory.memory_id}")
            self.memories[memory.memory_id] = memory
            self._archive_segments(segment_ids, "compressed", memory.memory_id)
            self.active_graph.add_memory(
                memory.memory_id,
                memory.topic,
                self._memory_member_anchors(memory),
            )
            self._audit_stage(
                "memory_apply",
                "created",
                {
                    "checkpoint_date": checkpoint_date,
                    "community_id": group.community_id,
                    "parent_community_id": parent_community_id,
                    "purification_group_id": purification_group_id,
                    "input": {
                        "segments": [segment.to_dict() for segment in segments]
                    },
                    "output": {"memory": memory.to_dict()},
                },
            )
            changes.append(
                {
                    "community_id": group.community_id,
                    "parent_community_id": parent_community_id,
                    "purification_group_id": purification_group_id,
                    "source": group.source,
                    "action": "memory_created",
                    "memory_id": memory.memory_id,
                    "segment_ids": sorted(segment_ids),
                }
            )
        except Exception as exc:
            retry_ids.update(segment_ids)
            error = {
                "at": utc_now(),
                "checkpoint_date": checkpoint_date,
                "community_id": group.community_id,
                "parent_community_id": parent_community_id,
                "purification_group_id": purification_group_id,
                "segment_ids": sorted(segment_ids),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            self.llm_errors.append(error)
            self._audit_stage(
                "memory_apply",
                "error",
                error,
            )
            changes.append({**error, "action": "kept_active_after_error"})

    def checkpoint(
        self,
        *,
        reason: str = "manual",
        checkpoint_date: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        checkpoint_date = normalize_date(checkpoint_date or self.current_date) if (
            checkpoint_date or self.current_date
        ) else None
        if checkpoint_date is None:
            return {"status": "empty", "checkpoint": self.checkpoint_count}
        if not self.pending_segment_ids and not force:
            return {
                "status": "no_pending",
                "checkpoint": self.checkpoint_count,
                "checkpoint_date": checkpoint_date,
            }

        focus_node_ids = (
            set(self.pending_segment_ids)
            if self.pending_segment_ids
            else self.active_graph.segment_ids()
        )
        state_before = self._state_snapshot()
        plan = self.planner.plan(self.active_graph, focus_node_ids=focus_node_ids)
        plan_output = {
            "communities": [
                {
                    "community_id": group.community_id,
                    "memory_ids": sorted(group.memory_ids),
                    "segment_ids": sorted(group.segment_ids),
                    "source": group.source,
                }
                for group in plan.communities
            ],
            "boundaries": {
                key: dict(value) for key, value in sorted(plan.boundaries.items())
            },
            "cannot_link_memory_pairs": [
                list(pair) for pair in sorted(plan.cannot_link_memory_pairs)
            ],
            "detected_communities": [sorted(group) for group in plan.detected_communities],
            "affected_node_ids": sorted(plan.affected_node_ids),
        }
        self._audit_stage(
            "community_planning",
            "planned",
            {
                "checkpoint_date": checkpoint_date,
                "focus_node_ids": sorted(focus_node_ids),
                "input_state": state_before,
                "output": plan_output,
            },
        )
        self.cannot_link_memory_pairs.update(plan.cannot_link_memory_pairs)
        self._mark_boundaries(plan.boundaries, checkpoint_date)
        changes: list[dict[str, Any]] = []
        retry_ids: set[str] = set()

        for group in plan.communities:
            segment_ids = set(group.segment_ids) - set(plan.boundaries)
            if not segment_ids:
                continue
            segments = [self.segments[segment_id] for segment_id in sorted(segment_ids)]
            try:
                purified_groups = self.community_purifier.purify(
                    group.community_id, segments
                )
            except Exception as exc:
                retry_ids.update(segment_ids)
                error = {
                    "at": utc_now(),
                    "checkpoint_date": checkpoint_date,
                    "community_id": group.community_id,
                    "segment_ids": sorted(segment_ids),
                    "stage": "community_purification",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                self.llm_errors.append(error)
                self._audit_stage(
                    "community_purification",
                    "error",
                    error,
                )
                changes.append({**error, "action": "kept_active_after_purification_error"})
                continue

            purification_output = [
                {
                    "group_id": purified.group_id,
                    "segment_ids": list(purified.segment_ids),
                }
                for purified in purified_groups
            ]
            self._audit_stage(
                "community_purification",
                "applied",
                {
                    "checkpoint_date": checkpoint_date,
                    "parent_community_id": group.community_id,
                    "memory_ids": sorted(group.memory_ids),
                    "input_segment_ids": sorted(segment_ids),
                    "output_groups": purification_output,
                },
            )
            self._trace(
                "community_purified",
                community_id=group.community_id,
                input_segment_ids=sorted(segment_ids),
                output_groups=purification_output,
            )

            for purified in purified_groups:
                purified_segment_ids = set(purified.segment_ids)
                purified_group = PlannedCommunity(
                    community_id=stable_group_id(
                        group.memory_ids | purified_segment_ids
                    ),
                    memory_ids=set(group.memory_ids),
                    segment_ids=purified_segment_ids,
                    source=f"{group.source}:purified",
                )
                purified_segments = [
                    self.segments[segment_id]
                    for segment_id in sorted(purified_segment_ids)
                ]
                self._apply_memory_group(
                    purified_group,
                    purified_segments,
                    checkpoint_date,
                    changes,
                    retry_ids,
                    parent_community_id=group.community_id,
                    purification_group_id=purified.group_id,
                )

        self.pending_segment_ids = retry_ids
        self.checkpoint_count += 1
        self.last_checkpoint_date = checkpoint_date
        result = {
            "status": "ok",
            "checkpoint": self.checkpoint_count,
            "checkpoint_date": checkpoint_date,
            "reason": reason,
            "community_detection_mode": (
                "global_initial" if self.checkpoint_count == 1 else "incremental_affected_components"
            ),
            "affected_node_ids": sorted(plan.affected_node_ids),
            "detected_communities": len(plan.detected_communities),
            "processed_communities": len(plan.communities),
            "changes": changes,
            "boundary_segment_ids": sorted(plan.boundaries),
            "cannot_link_memory_pairs": [list(pair) for pair in sorted(plan.cannot_link_memory_pairs)],
            "retry_segment_ids": sorted(retry_ids),
        }
        self._audit_stage(
            "checkpoint",
            "completed",
            {
                "input": {
                    "reason": reason,
                    "checkpoint_date": checkpoint_date,
                    "state_before": state_before,
                    "plan": plan_output,
                },
                "output": {"result": result, "state_after": self._state_snapshot()},
            },
        )
        self._trace("checkpoint", **result)
        return result

    def finalize(self) -> dict[str, Any]:
        return self.checkpoint(reason="finalize", checkpoint_date=self.current_date)

    def retrieve(self, query: str, k: int = 10) -> list[dict[str, Any]]:
        if k < 1:
            return []
        if not self.memories:
            return []

        def memory_fields(memory: MemoryRecord) -> list[str]:
            fields = [memory.topic, memory.summary]
            fields.extend(str(value) for value in memory.source_anchors)
            fields.extend(
                f"{item.get('type', '')} {item.get('content', '')}"
                for item in [*memory.topic_context, *memory.user_memories]
            )
            return [value for value in fields if value.strip()]

        memory_rows = []
        semantic_texts: list[str] = []
        semantic_owner_ids: list[str] = []
        lexical_documents: list[tuple[str, str]] = []
        entity_documents: dict[str, str] = {}
        for memory_id, memory in sorted(self.memories.items()):
            fields = memory_fields(memory)
            lexical_document = " ".join(fields)
            memory_rows.append({"memory_id": memory_id, "memory": memory})
            lexical_documents.append((memory_id, lexical_document))
            entity_documents[memory_id] = lexical_document
            for field in fields:
                semantic_texts.append(field)
                semantic_owner_ids.append(memory_id)

        query_vector = np.asarray(
            self.active_graph.encoder.encode([query])[0], dtype=np.float32
        )
        query_norm = float(np.linalg.norm(query_vector))
        if query_norm > 0:
            query_vector /= query_norm
        encoded_fields = np.asarray(
            self.active_graph.encoder.encode(semantic_texts), dtype=np.float32
        )
        semantic_scores = {row["memory_id"]: 0.0 for row in memory_rows}
        for owner_id, vector in zip(semantic_owner_ids, encoded_fields):
            norm = float(np.linalg.norm(vector))
            if norm > 0:
                vector = vector / norm
            semantic_scores[owner_id] = max(
                semantic_scores[owner_id], float(np.dot(query_vector, vector))
            )
        semantic_rank = [
            memory_id
            for memory_id, _ in sorted(
                semantic_scores.items(), key=lambda item: (-item[1], item[0])
            )[:k]
        ]

        bm25 = BM25(lexical_documents)
        lexical_scores = bm25.scores(set(lex_tokens(query)))
        lexical_rank = [
            memory_id
            for memory_id, _ in sorted(
                lexical_scores.items(), key=lambda item: (-item[1], item[0])
            )[:k]
        ]

        query_entities = extract_entities(query)
        entity_values = {
            memory_id: extract_entities(document)
            for memory_id, document in entity_documents.items()
        }
        entity_scores = {
            memory_id: entity_overlap(query_entities, values)
            for memory_id, values in entity_values.items()
        }
        entity_rank = [
            memory_id
            for memory_id, score in sorted(
                entity_scores.items(), key=lambda item: (-item[1], item[0])
            )
            if score > 0.0
        ][:k]

        fused_scores = rrf_fuse(
            [semantic_rank, lexical_rank, entity_rank], self.config.retrieval_rrf_k
        )
        rows = []
        for row in memory_rows:
            memory_id = row["memory_id"]
            if memory_id not in fused_scores:
                continue
            rows.append(
                {
                    "memory_id": memory_id,
                    "score": float(fused_scores[memory_id]),
                    "similarity": float(semantic_scores[memory_id]),
                    "lexical_score": float(lexical_scores.get(memory_id, 0.0)),
                    "entity_score": float(entity_scores[memory_id]),
                    "memory": row["memory"].to_dict(),
                }
            )
        rows.sort(
            key=lambda row: (
                -row["score"],
                -row["entity_score"],
                -row["lexical_score"],
                -row["similarity"],
                row["memory_id"],
            )
        )
        return rows[:k]

    def stats(self) -> dict[str, Any]:
        statuses: dict[str, int] = {}
        for segment in self.segments.values():
            statuses[segment.status] = statuses.get(segment.status, 0) + 1
        return {
            "schema_version": self.schema_version,
            "segments": len(self.segments),
            "segment_statuses": statuses,
            "memories": len(self.memories),
            "active_nodes": len(self.active_graph.nodes),
            "active_edges": self.active_graph.graph.number_of_edges(),
            "boundaries": len(self.boundaries),
            "pending_segments": len(self.pending_segment_ids),
            "checkpoints": self.checkpoint_count,
            "current_date": self.current_date,
            "last_checkpoint_date": self.last_checkpoint_date,
            "llm_errors": len(self.llm_errors),
            "stage_audit_events": len(self.stage_audit),
            "memory_relations_enabled": self.relations.enabled,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "config": self.config.to_dict(),
            "segments": {key: value.to_dict() for key, value in sorted(self.segments.items())},
            "memories": {key: value.to_dict() for key, value in sorted(self.memories.items())},
            "boundaries": {key: value.to_dict() for key, value in sorted(self.boundaries.items())},
            "skipped_segments": self.skipped_segments,
            "active_graph": self.active_graph.to_dict(),
            "pending_segment_ids": sorted(self.pending_segment_ids),
            "cannot_link_memory_pairs": [list(pair) for pair in sorted(self.cannot_link_memory_pairs)],
            "current_date": self.current_date,
            "last_checkpoint_date": self.last_checkpoint_date,
            "checkpoint_count": self.checkpoint_count,
            "relations": self.relations.to_dict(),
            "trace": self.trace,
            "stage_audit": self.stage_audit,
            "llm_errors": self.llm_errors,
            "stats": self.stats(),
        }

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, delete=False, suffix=".tmp"
        ) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        os.replace(temporary, target)

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        llm: JsonLLM | None = None,
        encoder: Encoder | None = None,
    ) -> "DailyMemoryGraph":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("schema_version") != cls.schema_version:
            raise ValueError(f"unsupported state schema: {payload.get('schema_version')}")
        config_values = dict(payload["config"])
        # A short-lived development build persisted this removed parameter.
        # Ignore it so those state files remain recoverable.
        config_values.pop("supernode_member_decay", None)
        instance = cls(
            llm=llm,
            encoder=encoder,
            config=DailyGraphConfig(**config_values),
        )
        instance.segments = {
            key: SegmentRecord.from_dict(value) for key, value in payload.get("segments", {}).items()
        }
        instance.memories = {
            key: MemoryRecord.from_dict(value) for key, value in payload.get("memories", {}).items()
        }
        instance.boundaries = {
            key: BoundaryRecord.from_dict(value)
            for key, value in payload.get("boundaries", {}).items()
        }
        instance.skipped_segments = list(payload.get("skipped_segments", []))
        instance.pending_segment_ids = set(payload.get("pending_segment_ids", []))
        instance.cannot_link_memory_pairs = {
            tuple(sorted((str(row[0]), str(row[1]))))
            for row in payload.get("cannot_link_memory_pairs", [])
            if isinstance(row, list) and len(row) == 2
        }
        instance.current_date = payload.get("current_date")
        instance.last_checkpoint_date = payload.get("last_checkpoint_date")
        instance.checkpoint_count = int(payload.get("checkpoint_count", 0))
        instance.relations = MemoryRelationStore.from_dict(payload.get("relations", {}))
        instance.trace = list(payload.get("trace", []))
        instance.stage_audit = list(payload.get("stage_audit", []))
        instance.llm_errors = list(payload.get("llm_errors", []))
        nodes = payload.get("active_graph", {}).get("nodes", [])
        instance.active_graph.restore_nodes(nodes)
        for memory_id, memory in instance.memories.items():
            instance.active_graph.update_memory_topic(
                memory_id,
                memory.topic,
                instance._memory_member_anchors(memory),
            )
        instance.active_graph.rebuild_edges()
        if set(instance.active_graph.memory_ids()) != set(instance.memories):
            raise ValueError("state memory records and active memory nodes do not match")
        unknown_active_segments = instance.active_graph.segment_ids() - set(instance.segments)
        if unknown_active_segments:
            raise ValueError(f"active graph has unknown segments: {sorted(unknown_active_segments)}")
        return instance
