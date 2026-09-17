from __future__ import annotations

import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .anchoring import AnchorExtractor
from .community import ConstrainedCommunityPlanner, PlannedCommunity, stable_group_id
from .config import DailyGraphConfig
from .cutting import ConversationCutter, CutSegment
from .encoder import Encoder, HashEncoder
from .graph import LayeredActiveGraph
from .llm import JsonLLM
from .memory import MemoryService, utc_now
from .models import BoundaryRecord, MemoryRecord, SegmentRecord
from .purification import CommunityPurifier, PurifiedGroup
from .promotion import LevelPromotionScheduler, PromotionCandidate
from .relations import MemoryRelationStore
from .retrieval import BM25, entity_overlap, extract_entities, lex_tokens, rrf_fuse
from .routing import OwnerDecision, TopicOwnerRouter


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
        graph_encoder = encoder or HashEncoder()
        self.layered_graph = LayeredActiveGraph(graph_encoder, self.config, max_level=3)
        # Keep graph 0 as the compatibility-facing active graph. It is the L0
        # boundary graph (segments plus active L1 memories).
        self.active_graph = self.layered_graph.graph(0)
        self.planner = ConstrainedCommunityPlanner(self.config)
        self.promotion_scheduler = LevelPromotionScheduler(
            self.config,
            max_level=self.layered_graph.max_level,
        )
        self.stage_audit: list[dict[str, Any]] = []
        self.cutter = ConversationCutter(llm, audit_sink=self._audit_stage)
        self.anchor_extractor = AnchorExtractor(llm, audit_sink=self._audit_stage)
        self.community_purifier = CommunityPurifier(llm, audit_sink=self._audit_stage)
        self.memory_service = MemoryService(llm, audit_sink=self._audit_stage)
        self.relations = MemoryRelationStore(enabled=False)
        self.topic_owner_router = TopicOwnerRouter(self.active_graph.encoder)

        self.segments: dict[str, SegmentRecord] = {}
        self.memories: dict[str, MemoryRecord] = {}
        # Active ownership is durable state, not an accidental consequence of
        # a memory being present in one of the community graphs.  A memory can
        # remain in ``memories`` as historical provenance after it is replaced
        # by a higher-level owner.
        self.active_memory_ids: set[str] = set()
        self.boundaries: dict[str, BoundaryRecord] = {}
        self.skipped_segments: list[dict[str, Any]] = []
        self.pending_segment_ids: set[str] = set()
        self.cannot_link_memory_pairs: set[tuple[str, str]] = set()
        self.current_date: str | None = None
        self.last_checkpoint_date: str | None = None
        self.checkpoint_count = 0
        self.trace: list[dict[str, Any]] = []
        self.llm_errors: list[dict[str, Any]] = []

    @staticmethod
    def _set_logical_mention_time(
        memory: MemoryRecord, mentioned_at: str
    ) -> MemoryRecord:
        """Use session/checkpoint time for lifecycle decisions, not wall time."""

        return replace(memory, last_mentioned_at=normalize_date(mentioned_at))

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
            "active_memory_ids": sorted(self.active_memory_ids),
            "boundaries": {
                key: value.to_dict() for key, value in sorted(self.boundaries.items())
            },
            "pending_segment_ids": sorted(self.pending_segment_ids),
            "active_graph": self.active_graph.to_dict(),
            "layered_active_graph": self.layered_graph.to_dict(),
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
        prepared = self.prepare_conversation(value, event_date=event_date)
        return self.ingest_prepared_conversation(prepared)

    def prepare_conversation(
        self,
        value: dict[str, Any],
        *,
        event_date: str | None = None,
    ) -> dict[str, Any]:
        """Run the stateless LLM preprocessing stages for one conversation.

        This method deliberately does not touch graph state. It can therefore
        be run concurrently for multiple conversations; the returned payload
        must still be ingested in stream order.
        """
        conversation_id = str(
            value.get("conversation_id", value.get("session_id", value.get("id", "conversation")))
        )
        resolved_date = normalize_date(event_date or value.get("event_date") or value.get("date"))
        messages = value.get("conversation", value.get("messages"))
        if not isinstance(messages, list):
            raise ValueError(f"conversation {conversation_id} has no message list")
        inference_messages = [
            {
                "turn": int(message.get("turn", index)),
                "speaker": str(message.get("speaker", message.get("role", "unknown"))),
                "message": str(
                    message.get("message", message.get("content", message.get("text", "")))
                ),
            }
            for index, message in enumerate(messages, start=1)
        ]
        # Persist only the actual inference view. Evaluation-only annotations
        # such as share_memory and operation_details must never enter pipeline
        # prompts, graph state, or replay-oriented stage inputs.
        audit_events: list[dict[str, Any]] = []

        def collect_audit(stage: str, action: str, values: dict[str, Any]) -> None:
            audit_events.append({"stage": stage, "action": action, "values": values})

        audit_events.append(
            {
                "stage": "conversation_input",
                "action": "received",
                "values": {
                    "conversation_id": conversation_id,
                    "input": {
                        "conversation_id": conversation_id,
                        "event_date": resolved_date,
                        "messages": inference_messages,
                    },
                },
            }
        )
        cutter = ConversationCutter(self.llm, audit_sink=collect_audit)
        anchor_extractor = AnchorExtractor(self.llm, audit_sink=collect_audit)
        cut_segments = cutter.cut(conversation_id, inference_messages)
        audit_events.append(
            {
                "stage": "cutting",
                "action": "normalized_result",
                "values": {
                    "conversation_id": conversation_id,
                    "output": [asdict(cut) for cut in cut_segments],
                },
            }
        )
        anchors = anchor_extractor.extract_many(
            [
                {"segment_id": cut.segment_id, "text": cut.text}
                for cut in cut_segments
            ]
        )
        return {
            "conversation_id": conversation_id,
            "event_date": resolved_date,
            "cut_segments": [asdict(cut) for cut in cut_segments],
            "anchors": anchors,
            "audit_events": audit_events,
        }

    def ingest_prepared_conversation(self, prepared: dict[str, Any]) -> dict[str, Any]:
        """Apply a stateless preprocessing result to the mutable graph state."""

        conversation_id = str(prepared.get("conversation_id", "conversation"))
        resolved_date = normalize_date(prepared.get("event_date"))
        raw_cut_segments = prepared.get("cut_segments")
        if not isinstance(raw_cut_segments, list):
            raise ValueError("prepared conversation has no cut_segments list")
        raw_anchors = prepared.get("anchors")
        if not isinstance(raw_anchors, dict):
            raise ValueError("prepared conversation has no anchors object")
        cut_segments = [CutSegment(**dict(row)) for row in raw_cut_segments]
        anchors = {str(key): value for key, value in raw_anchors.items()}
        for event in prepared.get("audit_events", []):
            if not isinstance(event, dict):
                raise ValueError("prepared audit event must be an object")
            self._audit_stage(
                str(event["stage"]),
                str(event["action"]),
                dict(event.get("values") or {}),
            )

        results = []
        for cut in cut_segments:
            anchor = anchors[cut.segment_id]
            if anchor is None:
                skipped = {
                    "segment_id": cut.segment_id,
                    "conversation_id": conversation_id,
                    "segment_index": cut.segment_index,
                    "event_date": resolved_date,
                    "text": cut.text,
                    "start_unit_id": cut.start_unit_id,
                    "end_unit_id": cut.end_unit_id,
                    "unit_ids": list(cut.unit_ids),
                    "message_unit_ids": dict(cut.message_unit_ids),
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
                        metadata={
                            "unit_ids": cut.unit_ids,
                            "message_unit_ids": cut.message_unit_ids,
                        },
                    )
                )
            )
        result_by_segment = {
            str(result["segment_id"]): result for result in results
        }
        return {
            "conversation_id": conversation_id,
            "event_date": resolved_date,
            "cut_segments": len(cut_segments),
            "results": results,
            "segment_mappings": [
                {
                    "segment_id": cut.segment_id,
                    "segment_index": cut.segment_index,
                    "unit_ids": list(cut.unit_ids),
                    "message_unit_ids": dict(cut.message_unit_ids),
                    "ingest_status": result_by_segment[cut.segment_id]["status"],
                }
                for cut in cut_segments
            ],
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

    def _memory_direct_representations(self, memory: MemoryRecord) -> list[str]:
        direct_members = [
            str(member["representation"]).strip()
            for member in memory.direct_members
            if str(member.get("representation", "")).strip()
        ]
        if direct_members:
            return direct_members

        # Legacy states predate direct_members.  Keep their old behavior as
        # a migration fallback; new multi-layer memories do not use this path.
        anchors = [
            self.segments[segment_id].anchor
            for segment_id in memory.source_segments
            if segment_id in self.segments
        ]
        return anchors or list(memory.source_anchors)

    def _memory_member_anchors(self, memory: MemoryRecord) -> list[str]:
        """Backward-compatible alias for saved workflow code and callers."""

        return self._memory_direct_representations(memory)

    def _memory_graph_levels(self, memory: MemoryRecord) -> tuple[int, ...]:
        """Return graphs used by an active memory at its current level.

        G0 is the short-term boundary graph: active L1 memories are present
        there so new L0 segments can attach to an existing local topic.  The
        higher graphs are same-level peer graphs used for time-driven
        promotion, so an Lk memory is stored only in Gk for k >= 2.
        """

        if memory.level == 1:
            return (0, 1)
        if 1 < memory.level <= self.layered_graph.max_level:
            return (memory.level,)
        return ()

    def _add_memory_to_layered_graphs(self, memory: MemoryRecord) -> None:
        self.active_memory_ids.add(memory.memory_id)
        for level in self._memory_graph_levels(memory):
            self.layered_graph.add_memory(
                level,
                memory.memory_id,
                memory.topic,
                self._memory_direct_representations(memory),
                memory.level,
            )

    def _update_memory_in_layered_graphs(self, memory: MemoryRecord) -> None:
        self.active_memory_ids.add(memory.memory_id)
        for level in self._memory_graph_levels(memory):
            graph = self.layered_graph.graph(level)
            if memory.memory_id in graph.nodes:
                self.layered_graph.update_memory_topic(
                    level,
                    memory.memory_id,
                    memory.topic,
                    self._memory_direct_representations(memory),
                    memory.level,
                )
            else:
                self.layered_graph.add_memory(
                    level,
                    memory.memory_id,
                    memory.topic,
                    self._memory_direct_representations(memory),
                    memory.level,
                )

    def _remove_memory_from_layered_graphs(
        self, memory_ids: str | Iterable[str]
    ) -> None:
        if isinstance(memory_ids, str):
            memory_ids = {memory_ids}
        memory_ids = set(memory_ids)
        self.active_memory_ids.difference_update(memory_ids)
        self.layered_graph.remove_nodes(memory_ids)

    def _run_purification_task(
        self,
        task: tuple[PlannedCommunity, list[SegmentRecord], list[MemoryRecord]],
    ) -> tuple[list[PurifiedGroup], list[dict[str, Any]], Exception | None]:
        """Run one purification request without mutating pipeline state."""

        group, segments, existing_memories = task
        audit_events: list[dict[str, Any]] = []

        def collect_audit(stage: str, action: str, values: dict[str, Any]) -> None:
            audit_events.append({"stage": stage, "action": action, "values": values})

        purifier = CommunityPurifier(self.llm, audit_sink=collect_audit)
        try:
            purified_groups = purifier.purify(
                group.community_id,
                segments,
                existing_memories,
            )
            return purified_groups, audit_events, None
        except Exception as exc:
            return [], audit_events, exc

    def _run_memory_task(
        self,
        task: tuple[PlannedCommunity, list[SegmentRecord]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Run one extraction/fusion request without mutating pipeline state."""

        group, segments = task
        audit_events: list[dict[str, Any]] = []

        def collect_audit(stage: str, action: str, values: dict[str, Any]) -> None:
            audit_events.append({"stage": stage, "action": action, "values": values})

        service = MemoryService(self.llm, audit_sink=collect_audit)
        try:
            if group.memory_ids:
                if len(group.memory_ids) != 1:
                    raise AssertionError("planner emitted a multi-memory community")
                memory_id = next(iter(group.memory_ids))
                existing = self.memories[memory_id]
                updated, decision = service.fuse(
                    group.community_id,
                    existing,
                    segments,
                    target_level=existing.level,
                )
                return {
                    "kind": "fused",
                    "existing_before": existing.to_dict(),
                    "updated": updated,
                    "decision": decision,
                }, audit_events

            return {
                "kind": "extracted",
                "memory": service.extract(
                    group.community_id, segments, target_level=1
                ),
            }, audit_events
        except Exception as exc:
            return {"kind": "error", "error": exc}, audit_events

    def _run_provisional_task(
        self,
        task: tuple[PlannedCommunity, list[SegmentRecord]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Extract one temporary L1 without changing pipeline state."""

        group, segments = task
        audit_events: list[dict[str, Any]] = []

        def collect_audit(stage: str, action: str, values: dict[str, Any]) -> None:
            audit_events.append({"stage": stage, "action": action, "values": values})

        service = MemoryService(self.llm, audit_sink=collect_audit)
        try:
            return {
                "kind": "provisional",
                "memory": service.extract(
                    group.community_id, segments, target_level=1
                ),
            }, audit_events
        except Exception as exc:
            return {"kind": "error", "error": exc}, audit_events

    def _run_owner_fusion_task(
        self,
        task: tuple[str, MemoryRecord, MemoryRecord],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        owner_id, owner, provisional = task
        audit_events: list[dict[str, Any]] = []

        def collect_audit(stage: str, action: str, values: dict[str, Any]) -> None:
            audit_events.append({"stage": stage, "action": action, "values": values})

        service = MemoryService(self.llm, audit_sink=collect_audit)
        try:
            updated, decision = service.fuse_provisional(
                f"owner:{owner_id}:{provisional.memory_id}",
                owner,
                provisional,
                target_level=owner.level,
            )
            return {
                "kind": "fused_owner",
                "existing_before": owner.to_dict(),
                "updated": updated,
                "decision": decision,
            }, audit_events
        except Exception as exc:
            return {"kind": "error", "error": exc}, audit_events

    def _run_owner_fusion_chain_task(
        self,
        task: tuple[str, MemoryRecord, list[MemoryRecord]],
    ) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        """Fuse several provisionals into one owner in order.

        The worker owns only local snapshots.  This lets different owners run
        concurrently while preserving the dependency between multiple
        provisionals targeting the same owner.  The caller still applies all
        returned results in the main state-owning thread.
        """

        owner_id, owner, provisionals = task
        current_owner = owner
        results: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for provisional in provisionals:
            result, audit_events = self._run_owner_fusion_task(
                (owner_id, current_owner, provisional)
            )
            results.append((result, audit_events))
            if result.get("kind") == "fused_owner":
                updated = result.get("updated")
                if isinstance(updated, MemoryRecord):
                    current_owner = updated
        return results

    def _run_promotion_task(
        self,
        task: tuple[PromotionCandidate, dict[str, MemoryRecord]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Partition one inactive region and compress eligible groups one level."""

        candidate, memory_snapshot = task
        audit_events: list[dict[str, Any]] = []

        def collect_audit(stage: str, action: str, values: dict[str, Any]) -> None:
            audit_events.append({"stage": stage, "action": action, "values": values})

        try:
            source_memories = [
                memory_snapshot[memory_id]
                for memory_id in candidate.node_ids
                if memory_id in memory_snapshot
            ]
            if not source_memories:
                return {"kind": "skipped", "reason": "no_source_memories"}, audit_events

            if len(source_memories) == 1:
                groups = [
                    PurifiedGroup(
                        "singleton",
                        (source_memories[0].memory_id,),
                        (source_memories[0].memory_id,),
                        (),
                    )
                ]
            else:
                purifier = CommunityPurifier(self.llm, audit_sink=collect_audit)
                groups = purifier.purify(
                    f"promotion:{candidate.source_level}:{':'.join(candidate.node_ids)}",
                    [],
                    source_memories,
                    allow_memory_only=True,
                    allow_multiple_memories=True,
                )

            service = MemoryService(self.llm, audit_sink=collect_audit)
            promoted: list[dict[str, Any]] = []
            due_ids = set(candidate.due_ids)
            for group in groups:
                group_ids = tuple(sorted(group.memory_ids))
                if not group_ids or not set(group_ids).issubset(due_ids):
                    # An active lower-level member protects this group. A
                    # false graph edge can still be split by purification.
                    continue
                group_memories = [memory_snapshot[memory_id] for memory_id in group_ids]
                latest = self.promotion_scheduler.latest_mention_at(group_memories)
                memory = service.extract_from_memories(
                    f"promotion:{candidate.source_level}:{group.group_id}",
                    group_memories,
                    target_level=candidate.target_level,
                    last_mentioned_at=latest,
                )
                promoted.append(
                    {
                        "source_memory_ids": list(group_ids),
                        "group_id": group.group_id,
                        "memory": memory,
                    }
                )
            return {
                "kind": "promotions",
                "source_level": candidate.source_level,
                "target_level": candidate.target_level,
                "candidate_node_ids": list(candidate.node_ids),
                "due_ids": list(candidate.due_ids),
                "promoted": promoted,
            }, audit_events
        except Exception as exc:
            return {
                "kind": "error",
                "source_level": candidate.source_level,
                "target_level": candidate.target_level,
                "candidate_node_ids": list(candidate.node_ids),
                "due_ids": list(candidate.due_ids),
                "error": exc,
            }, audit_events

    def _append_task_audits(self, audit_events: list[dict[str, Any]]) -> None:
        for event in audit_events:
            self._audit_stage(
                str(event["stage"]),
                str(event["action"]),
                dict(event.get("values") or {}),
            )

    def _apply_owner_fusion_result(
        self,
        provisional: MemoryRecord,
        owner_id: str,
        checkpoint_date: str,
        changes: list[dict[str, Any]],
        retry_ids: set[str],
        *,
        parent_community_id: str,
        purification_group_id: str,
        decision: OwnerDecision,
        result: dict[str, Any],
        source_memory_ids: Iterable[str] = (),
    ) -> bool:
        """Apply one-level owner fusion; return false for lower-level fallback."""

        segment_ids = set(provisional.source_segments)
        source_memory_ids = {str(memory_id) for memory_id in source_memory_ids}
        try:
            if result.get("kind") == "error":
                raise result["error"]
            owner = self.memories[owner_id]
            if owner.level != provisional.level + 1:
                raise ValueError(
                    "owner fusion must target exactly one level above the provisional memory"
                )
            updated = result["updated"]
            updated = self._set_logical_mention_time(updated, checkpoint_date)
            fusion_decision = result["decision"]
            self.memories[owner_id] = updated
            self.topic_owner_router.register(updated)
            self._update_memory_in_layered_graphs(updated)
            self._archive_segments(segment_ids, "compressed", owner_id)
            if source_memory_ids:
                self._remove_memory_from_layered_graphs(source_memory_ids)
                for memory_id in source_memory_ids:
                    self.topic_owner_router.remove(memory_id)
            self._audit_stage(
                "memory_apply",
                "owner_fused",
                {
                    "checkpoint_date": checkpoint_date,
                    "community_id": f"owner:{owner_id}:{provisional.memory_id}",
                    "parent_community_id": parent_community_id,
                    "purification_group_id": purification_group_id,
                    "owner_decision": decision.to_dict(),
                    "source_level": provisional.level,
                    "target_level": owner.level,
                    "source_memory_ids": sorted(source_memory_ids),
                    "input": {
                        "owner_memory": owner.to_dict(),
                        "provisional_l1": provisional.to_dict(),
                    },
                    "output": {
                        "memory": updated.to_dict(),
                        "decision": fusion_decision,
                    },
                },
            )
            changes.append(
                {
                    "community_id": f"owner:{owner_id}:{provisional.memory_id}",
                    "parent_community_id": parent_community_id,
                    "purification_group_id": purification_group_id,
                    "source": "owner_routing",
                    "action": "memory_owner_fused",
                    "memory_id": owner_id,
                    "owner_decision": decision.to_dict(),
                    "source_level": provisional.level,
                    "target_level": updated.level,
                    "source_memory_ids": sorted(source_memory_ids),
                    "decision": fusion_decision["decision"],
                    "operations": fusion_decision["operations"],
                    "segment_ids": sorted(segment_ids),
                }
            )
            return True
        except Exception as exc:
            error = {
                "at": utc_now(),
                "checkpoint_date": checkpoint_date,
                "community_id": f"owner:{owner_id}:{provisional.memory_id}",
                "parent_community_id": parent_community_id,
                "purification_group_id": purification_group_id,
                "segment_ids": sorted(segment_ids),
                "owner_memory_id": owner_id,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            self.llm_errors.append(error)
            self._audit_stage("memory_apply", "owner_fusion_error", error)
            changes.append({**error, "action": "owner_fusion_failed_fallback_to_lower_level"})
            return False

    def _apply_memory_result(
        self,
        group: PlannedCommunity,
        segments: list[SegmentRecord],
        checkpoint_date: str,
        changes: list[dict[str, Any]],
        retry_ids: set[str],
        *,
        parent_community_id: str,
        purification_group_id: str,
        result: dict[str, Any],
    ) -> None:
        """Apply one completed LLM result in the main state-owning thread."""

        segment_ids = {segment.segment_id for segment in segments}
        try:
            if result.get("kind") == "error":
                raise result["error"]
            if result.get("kind") == "fused":
                memory_id = next(iter(group.memory_ids))
                existing_before = result["existing_before"]
                updated = result["updated"]
                decision = result["decision"]
                updated = self._set_logical_mention_time(updated, checkpoint_date)
                self.memories[memory_id] = updated
                self.topic_owner_router.register(updated)
                self._update_memory_in_layered_graphs(updated)
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
                        "no_op_reason": decision["no_op_reason"],
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

            memory = result.get("memory")
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
            memory = self._set_logical_mention_time(memory, checkpoint_date)
            self.memories[memory.memory_id] = memory
            self.topic_owner_router.register(memory)
            self._archive_segments(segment_ids, "compressed", memory.memory_id)
            self._add_memory_to_layered_graphs(memory)
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
        """Run and apply one memory task synchronously for compatibility."""

        result, audit_events = self._run_memory_task((group, segments))
        self._append_task_audits(audit_events)
        self._apply_memory_result(
            group,
            segments,
            checkpoint_date,
            changes,
            retry_ids,
            parent_community_id=parent_community_id,
            purification_group_id=purification_group_id,
            result=result,
        )

    def _run_promotions(
        self,
        checkpoint_date: str,
        changes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Promote time-inactive memories by exactly one level."""

        memory_snapshot = dict(self.memories)
        candidates: list[PromotionCandidate] = []
        candidate_errors: list[dict[str, Any]] = []
        for source_level in range(1, self.layered_graph.max_level):
            try:
                candidates.extend(
                    self.promotion_scheduler.candidates(
                        self.layered_graph,
                        memory_snapshot,
                        checkpoint_date,
                        source_level=source_level,
                        detect=self.planner.detect_nodes,
                    )
                )
            except Exception as exc:
                error = {
                    "at": utc_now(),
                    "checkpoint_date": checkpoint_date,
                    "source_level": source_level,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                candidate_errors.append(error)
                self.llm_errors.append(error)
                self._audit_stage("memory_promotion", "candidate_error", error)

        promotion_tasks = [(candidate, memory_snapshot) for candidate in candidates]
        if self.config.postprocess_workers > 1 and len(promotion_tasks) > 1:
            # Promotion tasks only read the checkpoint snapshot.  Keep all
            # graph/state mutations in the commit loop below, but overlap the
            # expensive high-level purification/extraction requests just like
            # the L0/L1 post-processing stages above.
            with ThreadPoolExecutor(
                max_workers=self.config.postprocess_workers
            ) as executor:
                promotion_results_raw = list(
                    executor.map(self._run_promotion_task, promotion_tasks)
                )
        else:
            promotion_results_raw = [
                self._run_promotion_task(task) for task in promotion_tasks
            ]

        promotion_results: list[dict[str, Any]] = []
        for candidate, (result, audit_events) in zip(candidates, promotion_results_raw):
            self._append_task_audits(audit_events)
            promotion_results.append(
                {
                    "kind": result.get("kind"),
                    "source_level": result.get("source_level"),
                    "target_level": result.get("target_level"),
                    "candidate_node_ids": result.get("candidate_node_ids", []),
                    "due_ids": result.get("due_ids", []),
                    "promoted": [
                        {
                            "group_id": row.get("group_id"),
                            "source_memory_ids": row.get("source_memory_ids", []),
                            "memory_id": (
                                row["memory"].memory_id
                                if row.get("memory") is not None
                                else None
                            ),
                        }
                        for row in result.get("promoted", [])
                    ],
                }
            )
            if result.get("kind") == "error":
                error = {
                    "at": utc_now(),
                    "checkpoint_date": checkpoint_date,
                    "source_level": candidate.source_level,
                    "target_level": candidate.target_level,
                    "candidate_node_ids": list(candidate.node_ids),
                    "error_type": type(result.get("error")).__name__,
                    "error": str(result.get("error")),
                }
                self.llm_errors.append(error)
                self._audit_stage("memory_promotion", "error", error)
                changes.append({**error, "action": "promotion_failed"})
                continue

            for promoted in result.get("promoted", []):
                promoted_memory = promoted.get("memory")
                source_ids = {
                    str(memory_id) for memory_id in promoted.get("source_memory_ids", [])
                }
                if promoted_memory is None:
                    continue
                if not source_ids or not source_ids.issubset(self.memories):
                    continue
                if any(
                    self.memories[memory_id].level != promoted_memory.level - 1
                    for memory_id in source_ids
                ):
                    continue
                if promoted_memory.memory_id in self.memories:
                    error = {
                        "at": utc_now(),
                        "checkpoint_date": checkpoint_date,
                        "source_memory_ids": sorted(source_ids),
                        "memory_id": promoted_memory.memory_id,
                        "error_type": "ValueError",
                        "error": "generated duplicate promoted memory_id",
                    }
                    self.llm_errors.append(error)
                    self._audit_stage("memory_promotion", "error", error)
                    changes.append({**error, "action": "promotion_failed"})
                    continue

                # A newly materialized lower-level memory can wake only the
                # immediately higher level.  If that owner exists, fuse into
                # it and retire the lower-level source memories; otherwise
                # keep the ordinary one-level promotion result.
                bypassed = False
                if (
                    self.config.enable_owner_bypass
                    and candidate.source_level == 2
                    and len(source_ids) == 1
                ):
                    # For an existing L2 promotion candidate, the L2 itself
                    # is the lower-level input.  The generated L3 below is
                    # only the ordinary fallback and must not be routed as a
                    # same-level provisional memory.
                    routing_provisional = self.memories[next(iter(source_ids))]
                    target_level = routing_provisional.level + 1
                    recall_rows = self.topic_owner_router.candidates(
                        routing_provisional.topic,
                        summary=routing_provisional.summary,
                        direct_members=(
                            str(member["representation"])
                            for member in routing_provisional.direct_members
                        ),
                        k=self.config.owner_candidate_top_k,
                        min_level=target_level,
                        max_level=target_level,
                    )
                    if recall_rows:
                        routing_audit: list[dict[str, Any]] = []
                        owner_decision = self.topic_owner_router.decide(
                            routing_provisional,
                            self.memories,
                            llm=self.llm,
                            top_k=self.config.owner_candidate_top_k,
                            target_level=target_level,
                            audit_sink=routing_audit.append,
                        )
                        for audit in routing_audit:
                            self._audit_stage(
                                "owner_routing",
                                "llm_call" if "request" in audit else "decided",
                                {
                                    "checkpoint_date": checkpoint_date,
                                    "parent_community_id": (
                                        f"promotion:{candidate.source_level}"
                                    ),
                                    "purification_group_id": promoted.get("group_id"),
                                    **audit,
                                },
                            )
                        self._trace(
                            "owner_routing",
                            provisional_memory_id=routing_provisional.memory_id,
                            owner_memory_id=owner_decision.owner_memory_id,
                            reason=owner_decision.reason,
                            source_level=routing_provisional.level,
                            target_level=target_level,
                            candidates=[row["memory_id"] for row in recall_rows],
                        )
                        owner_id = owner_decision.owner_memory_id
                        owner = self.memories.get(owner_id) if owner_id else None
                        if owner is not None:
                            owner_result, owner_audits = self._run_owner_fusion_task(
                                (owner_id, owner, routing_provisional)
                            )
                            self._append_task_audits(owner_audits)
                            bypassed = self._apply_owner_fusion_result(
                                routing_provisional,
                                owner_id,
                                checkpoint_date,
                                changes,
                                set(),
                                parent_community_id=(
                                    f"promotion:{candidate.source_level}"
                                ),
                                purification_group_id=str(promoted.get("group_id", "")),
                                decision=owner_decision,
                                result=owner_result,
                                source_memory_ids=source_ids,
                            )
                if bypassed:
                    continue

                self.memories[promoted_memory.memory_id] = promoted_memory
                self._add_memory_to_layered_graphs(promoted_memory)
                self._remove_memory_from_layered_graphs(source_ids)
                self.topic_owner_router.replace(source_ids, promoted_memory)
                self._audit_stage(
                    "memory_promotion",
                    "applied",
                    {
                        "checkpoint_date": checkpoint_date,
                        "source_memory_ids": sorted(source_ids),
                        "output_memory": promoted_memory.to_dict(),
                    },
                )
                changes.append(
                    {
                        "source": "time_based_promotion",
                        "action": "memory_promoted",
                        "source_memory_ids": sorted(source_ids),
                        "memory_id": promoted_memory.memory_id,
                        "source_level": promoted_memory.level - 1,
                        "target_level": promoted_memory.level,
                        "last_mentioned_at": promoted_memory.last_mentioned_at,
                    }
                )

        return {
            "candidate_count": len(candidates),
            "applied_count": sum(
                1
                for change in changes
                if change.get("action") == "memory_promoted"
            ),
            "candidate_errors": candidate_errors,
            "results": promotion_results,
        }

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
        promotion_due = bool(
            self.promotion_scheduler.due_ids(
                self.memories,
                checkpoint_date,
                active_ids=self.active_memory_ids,
            )
        )
        if not self.pending_segment_ids and not force and not promotion_due:
            return {
                "status": "no_pending",
                "checkpoint": self.checkpoint_count,
                "checkpoint_date": checkpoint_date,
            }

        focus_node_ids = (
            self.active_graph.segment_ids()
            if force
            else (
                set(self.pending_segment_ids)
                if self.pending_segment_ids
                else self.active_graph.segment_ids()
            )
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

        purification_tasks: list[
            tuple[PlannedCommunity, list[SegmentRecord], list[MemoryRecord]]
        ] = []
        for group in plan.communities:
            segment_ids = set(group.segment_ids) - set(plan.boundaries)
            if not segment_ids:
                continue
            segments = [self.segments[segment_id] for segment_id in sorted(segment_ids)]
            existing_memories = [
                self.memories[memory_id]
                for memory_id in sorted(group.memory_ids)
            ]
            purification_tasks.append((group, segments, existing_memories))

        if self.config.postprocess_workers > 1 and len(purification_tasks) > 1:
            with ThreadPoolExecutor(
                max_workers=self.config.postprocess_workers
            ) as executor:
                purification_results = list(
                    executor.map(self._run_purification_task, purification_tasks)
                )
        else:
            purification_results = [
                self._run_purification_task(task) for task in purification_tasks
            ]

        purified_communities: list[
            tuple[PlannedCommunity, list[SegmentRecord], list[PurifiedGroup]]
        ] = []
        for task, task_result in zip(purification_tasks, purification_results):
            group, segments, _ = task
            purified_groups, audit_events, error = task_result
            self._append_task_audits(audit_events)
            if error is not None:
                segment_ids = {segment.segment_id for segment in segments}
                retry_ids.update(segment_ids)
                error_row = {
                    "at": utc_now(),
                    "checkpoint_date": checkpoint_date,
                    "community_id": group.community_id,
                    "segment_ids": sorted(segment_ids),
                    "stage": "community_purification",
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
                self.llm_errors.append(error_row)
                self._audit_stage(
                    "community_purification",
                    "error",
                    error_row,
                )
                changes.append(
                    {
                        **error_row,
                        "action": "kept_active_after_purification_error",
                    }
                )
                continue

            purification_output = [
                {
                    "group_id": purified.group_id,
                    "node_ids": list(purified.node_ids),
                    "memory_ids": list(purified.memory_ids),
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
                    "input_segment_ids": sorted(segment.segment_id for segment in segments),
                    "input_memory_ids": sorted(group.memory_ids),
                    "output_groups": purification_output,
                },
            )
            self._trace(
                "community_purified",
                community_id=group.community_id,
                input_segment_ids=sorted(segment.segment_id for segment in segments),
                output_groups=purification_output,
            )

            if len(purified_groups) > 1:
                removed_edges = self.active_graph.cut_cross_group_edges(
                    [set(purified.node_ids) for purified in purified_groups]
                )
                if removed_edges:
                    self._audit_stage(
                        "community_purification",
                        "edges_cut",
                        {
                            "checkpoint_date": checkpoint_date,
                            "parent_community_id": group.community_id,
                            "removed_edges": removed_edges,
                        },
                    )
                    self._trace(
                        "community_purification_edges_cut",
                        community_id=group.community_id,
                        removed_edges=removed_edges,
                    )
            purified_communities.append((group, segments, purified_groups))

        memory_tasks: list[tuple[PlannedCommunity, list[SegmentRecord]]] = []
        memory_contexts: list[tuple[str, str]] = []
        owner_bypass_tasks: list[
            tuple[PlannedCommunity, list[SegmentRecord], tuple[str, str]]
        ] = []
        for group, _, purified_groups in purified_communities:
            purification_was_split = len(purified_groups) > 1
            for purified in purified_groups:
                purified_segment_ids = set(purified.segment_ids)
                purified_memory_ids = set(purified.memory_ids)
                if not purified_segment_ids:
                    # A memory-only purification group is the explicit
                    # "keep existing memory" outcome.
                    continue
                if (
                    reason != "finalize"
                    and purification_was_split
                    and not purified_memory_ids
                    and len(purified_segment_ids) == 1
                ):
                    # A singleton produced by purification has no remaining
                    # community evidence. Keep it active for a future
                    # community, but do not call extraction/fusion on it.
                    changes.append(
                        {
                            "community_id": stable_group_id(purified_segment_ids),
                            "parent_community_id": group.community_id,
                            "purification_group_id": purified.group_id,
                            "source": f"{group.source}:purified",
                            "action": "singleton_kept_active",
                            "segment_ids": sorted(purified_segment_ids),
                        }
                    )
                    self._audit_stage(
                        "memory_apply",
                        "skipped_singleton",
                        {
                            "checkpoint_date": checkpoint_date,
                            "community_id": stable_group_id(purified_segment_ids),
                            "parent_community_id": group.community_id,
                            "purification_group_id": purified.group_id,
                            "reason": "purification_split_singleton_kept_active",
                            "input": {
                                "segment_ids": sorted(purified_segment_ids),
                                "memory_ids": [],
                            },
                        },
                    )
                    # Keep the detached singleton eligible for a later
                    # checkpoint. It may gain same-topic neighbours before
                    # the stream ends; finalize processes it as a standalone
                    # purified community so it cannot remain stranded.
                    retry_ids.update(purified_segment_ids)
                    continue
                purified_group = PlannedCommunity(
                    community_id=stable_group_id(
                        purified_memory_ids | purified_segment_ids
                    ),
                    memory_ids=purified_memory_ids,
                    segment_ids=purified_segment_ids,
                    source=f"{group.source}:purified",
                )
                purified_segments = [
                    self.segments[segment_id]
                    for segment_id in sorted(purified_segment_ids)
                ]
                parent_context = (group.community_id, purified.group_id)

                # Existing L1 communities use the ordinary fusion path. An
                # owner lookup must not first extract a duplicate provisional
                # L1 for a memory that already exists.
                normal_memory_ids = {
                    memory_id
                    for memory_id in purified_memory_ids
                    if memory_id in self.memories
                    and self.memories[memory_id].level == 1
                }

                # Only a genuinely new L1 candidate needs owner routing. The
                # provisional extraction is also the final L1 result when no
                # adjacent L2 owner is found.
                if self.config.enable_owner_bypass and not normal_memory_ids:
                    owner_bypass_tasks.append(
                        (purified_group, purified_segments, parent_context)
                    )
                    continue

                # No adjacent L2 owner, an invalid owner decision, or a
                # routing/fusion failure all use the existing ordinary L1 path.
                normal_memory_ids = {
                    memory_id
                    for memory_id in purified_group.memory_ids
                    if memory_id in self.memories and self.memories[memory_id].level == 1
                }
                purified_group = PlannedCommunity(
                    community_id=purified_group.community_id,
                    memory_ids=normal_memory_ids,
                    segment_ids=purified_segment_ids,
                    source=purified_group.source,
                )
                memory_tasks.append((purified_group, purified_segments))
                memory_contexts.append(parent_context)

        # Run ordinary memory work and provisional owner-bypass extraction in
        # the same bounded pool.  These workers only read checkpoint state and
        # return values; all state mutations remain below in the main thread.
        provisional_results: list[Any] = [None] * len(owner_bypass_tasks)
        memory_results: list[Any] = [None] * len(memory_tasks)
        total_initial_tasks = len(owner_bypass_tasks) + len(memory_tasks)
        if self.config.postprocess_workers > 1 and total_initial_tasks > 1:
            with ThreadPoolExecutor(
                max_workers=self.config.postprocess_workers
            ) as executor:
                futures: list[tuple[str, int, Any]] = []
                for index, (group, segments, _) in enumerate(owner_bypass_tasks):
                    futures.append(
                        (
                            "provisional",
                            index,
                            executor.submit(
                                self._run_provisional_task, (group, segments)
                            ),
                        )
                    )
                for index, task in enumerate(memory_tasks):
                    futures.append(
                        ("memory", index, executor.submit(self._run_memory_task, task))
                    )
                for kind, index, future in futures:
                    if kind == "provisional":
                        provisional_results[index] = future.result()
                    else:
                        memory_results[index] = future.result()
        else:
            for index, (group, segments, _) in enumerate(owner_bypass_tasks):
                provisional_results[index] = self._run_provisional_task(
                    (group, segments)
                )
            memory_results = [self._run_memory_task(task) for task in memory_tasks]

        owner_entries: list[dict[str, Any]] = []
        fallback_memory_tasks: list[tuple[PlannedCommunity, list[SegmentRecord]]] = []
        fallback_memory_contexts: list[tuple[str, str]] = []
        for task, task_result in zip(owner_bypass_tasks, provisional_results):
            purified_group, purified_segments, parent_context = task
            provisional_result, provisional_audits = task_result
            self._append_task_audits(provisional_audits)
            provisional = provisional_result.get("memory")
            if provisional_result.get("kind") != "provisional" or provisional is None:
                # Preserve the existing retry/fallback behavior if the
                # provisional extraction failed.
                fallback_memory_tasks.append((purified_group, purified_segments))
                fallback_memory_contexts.append(parent_context)
                continue

            parent_community_id, purification_group_id = parent_context
            routing_audit: list[dict[str, Any]] = []
            owner_decision = self.topic_owner_router.decide(
                provisional,
                self.memories,
                llm=self.llm,
                top_k=self.config.owner_candidate_top_k,
                target_level=2,
                audit_sink=routing_audit.append,
            )
            for audit in routing_audit:
                self._audit_stage(
                    "owner_routing",
                    "llm_call" if "request" in audit else "decided",
                    {
                        "checkpoint_date": checkpoint_date,
                        "parent_community_id": parent_community_id,
                        "purification_group_id": purification_group_id,
                        **audit,
                    },
                )
            self._trace(
                "owner_routing",
                provisional_memory_id=provisional.memory_id,
                owner_memory_id=owner_decision.owner_memory_id,
                reason=owner_decision.reason,
                candidates=[row["memory_id"] for row in owner_decision.candidates],
            )
            owner_id = owner_decision.owner_memory_id
            if owner_id not in self.memories:
                owner_id = None
            owner_entries.append(
                {
                    "group": purified_group,
                    "segments": purified_segments,
                    "parent_community_id": parent_community_id,
                    "purification_group_id": purification_group_id,
                    "provisional": provisional,
                    "owner_id": owner_id,
                    "decision": owner_decision,
                }
            )

        if fallback_memory_tasks:
            if self.config.postprocess_workers > 1 and len(fallback_memory_tasks) > 1:
                with ThreadPoolExecutor(
                    max_workers=self.config.postprocess_workers
                ) as executor:
                    fallback_results = list(
                        executor.map(self._run_memory_task, fallback_memory_tasks)
                    )
            else:
                fallback_results = [
                    self._run_memory_task(task) for task in fallback_memory_tasks
                ]
            memory_tasks.extend(fallback_memory_tasks)
            memory_contexts.extend(fallback_memory_contexts)
            memory_results.extend(fallback_results)

        owner_groups: dict[str, list[dict[str, Any]]] = {}
        for entry in owner_entries:
            owner_id = entry["owner_id"]
            if owner_id is not None:
                owner_groups.setdefault(owner_id, []).append(entry)

        owner_chain_tasks = [
            (
                owner_id,
                self.memories[owner_id],
                [entry["provisional"] for entry in entries],
            )
            for owner_id, entries in owner_groups.items()
        ]
        if self.config.postprocess_workers > 1 and len(owner_chain_tasks) > 1:
            with ThreadPoolExecutor(
                max_workers=self.config.postprocess_workers
            ) as executor:
                owner_chain_results = dict(
                    zip(
                        owner_groups,
                        executor.map(
                            self._run_owner_fusion_chain_task, owner_chain_tasks
                        ),
                    )
                )
        else:
            owner_chain_results = {
                owner_id: self._run_owner_fusion_chain_task(task)
                for owner_id, task in zip(owner_groups, owner_chain_tasks)
            }

        owner_chain_offsets = {owner_id: 0 for owner_id in owner_groups}
        for entry in owner_entries:
            provisional = entry["provisional"]
            owner_id = entry["owner_id"]
            if owner_id is not None:
                offset = owner_chain_offsets[owner_id]
                owner_result, owner_audits = owner_chain_results[owner_id][offset]
                owner_chain_offsets[owner_id] = offset + 1
                self._append_task_audits(owner_audits)
                if self._apply_owner_fusion_result(
                    provisional,
                    owner_id,
                    checkpoint_date,
                    changes,
                    retry_ids,
                    parent_community_id=entry["parent_community_id"],
                    purification_group_id=entry["purification_group_id"],
                    decision=entry["decision"],
                    result=owner_result,
                ):
                    continue

            # No owner, invalid owner, or owner-fusion failure: keep the
            # provisional extraction as an ordinary active L1 memory.
            self._apply_memory_result(
                PlannedCommunity(
                    community_id=entry["group"].community_id,
                    memory_ids=set(),
                    segment_ids=set(entry["group"].segment_ids),
                    source=entry["group"].source,
                ),
                entry["segments"],
                checkpoint_date,
                changes,
                retry_ids,
                parent_community_id=entry["parent_community_id"],
                purification_group_id=entry["purification_group_id"],
                result={"kind": "extracted", "memory": provisional},
            )

        for (group, segments), (parent_community_id, purification_group_id), (
            memory_result,
            audit_events,
        ) in zip(memory_tasks, memory_contexts, memory_results):
            self._append_task_audits(audit_events)
            self._apply_memory_result(
                group,
                segments,
                checkpoint_date,
                changes,
                retry_ids,
                parent_community_id=parent_community_id,
                purification_group_id=purification_group_id,
                result=memory_result,
            )

        self.pending_segment_ids = retry_ids
        promotion_result = self._run_promotions(checkpoint_date, changes)
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
            "promotion": promotion_result,
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
        return self.checkpoint(
            reason="finalize",
            checkpoint_date=self.current_date,
            force=bool(self.active_graph.segment_ids()),
        )

    def retrieve(self, query: str, k: int = 10) -> list[dict[str, Any]]:
        if k < 1:
            return []
        if not self.memories and not self.active_graph.segment_ids():
            return []

        def memory_fields(memory: MemoryRecord) -> list[str]:
            fields = [memory.topic, memory.summary]
            fields.extend(str(value) for value in memory.source_anchors)
            fields.extend(
                f"{item.get('type', '')} {item.get('content', '')}"
                for item in [*memory.topic_context, *memory.user_memories]
            )
            return [value for value in fields if value.strip()]

        def segment_fields(node_id: str) -> list[str]:
            segment = self.segments.get(node_id)
            if segment is None:
                return []
            return [value for value in (segment.text, segment.anchor) if value.strip()]

        node_rows = []
        semantic_texts: list[str] = []
        semantic_owner_ids: list[str] = []
        lexical_documents: list[tuple[str, str]] = []
        entity_documents: dict[str, str] = {}
        active_memory_ids = self.active_memory_ids
        retrieval_nodes: list[tuple[str, str, list[str]]] = [
            (memory_id, "memory", memory_fields(memory))
            for memory_id, memory in sorted(self.memories.items())
            if memory_id in active_memory_ids
        ]
        retrieval_nodes.extend(
            (segment_id, "segment", segment_fields(segment_id))
            for segment_id in sorted(self.active_graph.segment_ids())
        )
        for node_id, kind, fields in retrieval_nodes:
            lexical_document = " ".join(fields)
            row = {
                "id": node_id if kind == "memory" else f"segment:{node_id}",
                "node_id": node_id,
                "kind": kind,
            }
            if kind == "memory":
                memory = self.memories.get(node_id)
                if memory is None:
                    continue
                row.update({"memory_id": node_id, "memory": memory})
            else:
                segment = self.segments.get(node_id)
                if segment is None:
                    continue
                row.update({
                    "segment_id": node_id,
                    "segment": segment,
                    "status": segment.status,
                })
            node_rows.append(row)
            lexical_documents.append((row["id"], lexical_document))
            entity_documents[row["id"]] = lexical_document
            for field in fields:
                semantic_texts.append(field)
                semantic_owner_ids.append(row["id"])

        if not node_rows:
            return []

        query_vector = np.asarray(
            self.active_graph.encoder.encode([query])[0], dtype=np.float32
        )
        query_norm = float(np.linalg.norm(query_vector))
        if query_norm > 0:
            query_vector /= query_norm
        encoded_fields = np.asarray(
            self.active_graph.encoder.encode(semantic_texts), dtype=np.float32
        )
        semantic_scores = {row["id"]: 0.0 for row in node_rows}
        for owner_id, vector in zip(semantic_owner_ids, encoded_fields):
            norm = float(np.linalg.norm(vector))
            if norm > 0:
                vector = vector / norm
            semantic_scores[owner_id] = max(
                semantic_scores[owner_id], float(np.dot(query_vector, vector))
            )
        semantic_rank = [
            item_id
            for item_id, _ in sorted(
                semantic_scores.items(), key=lambda item: (-item[1], item[0])
            )[:k]
        ]

        bm25 = BM25(lexical_documents)
        lexical_scores = bm25.scores(set(lex_tokens(query)))
        lexical_rank = [
            item_id
            for item_id, _ in sorted(
                lexical_scores.items(), key=lambda item: (-item[1], item[0])
            )[:k]
        ]

        query_entities = extract_entities(query)
        entity_values = {
            item_id: extract_entities(document)
            for item_id, document in entity_documents.items()
        }
        entity_scores = {
            item_id: entity_overlap(query_entities, values)
            for item_id, values in entity_values.items()
        }
        entity_rank = [
            item_id
            for item_id, score in sorted(
                entity_scores.items(), key=lambda item: (-item[1], item[0])
            )
            if score > 0.0
        ][:k]

        fused_scores = rrf_fuse(
            [semantic_rank, lexical_rank, entity_rank], self.config.retrieval_rrf_k
        )
        rows = []
        for row in node_rows:
            item_id = row["id"]
            if item_id not in fused_scores:
                continue
            result = {
                key: value
                for key, value in row.items()
                if key not in {"id", "node_id", "kind", "memory", "segment"}
            }
            result.update({
                "id": item_id,
                "node_id": row["node_id"],
                "kind": row["kind"],
                "source": row["kind"],
                "score": float(fused_scores[item_id]),
                "similarity": float(semantic_scores[item_id]),
                "lexical_score": float(lexical_scores.get(item_id, 0.0)),
                "entity_score": float(entity_scores[item_id]),
            })
            if row["kind"] == "memory":
                result["memory"] = row["memory"].to_dict()
            else:
                result["segment"] = row["segment"].to_dict()
            rows.append(result)
        rows.sort(
            key=lambda row: (
                -row["score"],
                -row["entity_score"],
                -row["lexical_score"],
                -row["similarity"],
                row["id"],
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
            "active_memories": len(self.active_memory_ids),
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
            "active_memory_ids": sorted(self.active_memory_ids),
            "boundaries": {key: value.to_dict() for key, value in sorted(self.boundaries.items())},
            "skipped_segments": self.skipped_segments,
            "active_graph": self.active_graph.to_dict(),
            "layered_active_graph": self.layered_graph.to_dict(),
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
        raw_active_memory_ids = payload.get("active_memory_ids")
        has_explicit_active_memory_ids = isinstance(raw_active_memory_ids, list)
        if has_explicit_active_memory_ids:
            instance.active_memory_ids = {
                str(memory_id).strip()
                for memory_id in raw_active_memory_ids
                if str(memory_id).strip()
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
        layered_graph_payload = payload.get("layered_active_graph")
        has_layered_graph = (
            isinstance(layered_graph_payload, dict)
            and "graphs" in layered_graph_payload
        )
        if has_layered_graph:
            instance.layered_graph.restore(layered_graph_payload)
            instance.layered_graph.annotate_memory_levels(
                {memory_id: memory.level for memory_id, memory in instance.memories.items()}
            )
            instance.active_graph = instance.layered_graph.graph(0)
        else:
            active_graph_payload = payload.get("active_graph", {})
            instance.layered_graph.restore(
                {"graphs": {"0": active_graph_payload}}
            )
            instance.active_graph = instance.layered_graph.graph(0)
        # Pre-active-memory snapshots used graph membership as the active
        # marker. Infer that marker once, then normalize all graph memberships
        # to the current per-level layout before rebuilding derived indexes.
        if not has_explicit_active_memory_ids:
            instance.active_memory_ids = instance.layered_graph.memory_ids()

        unknown_active_memories = instance.active_memory_ids - set(instance.memories)
        if unknown_active_memories:
            raise ValueError(
                "active graph has unknown memories: "
                f"{sorted(unknown_active_memories)}"
            )
        for level in instance.layered_graph.levels():
            graph = instance.layered_graph.graph(level)
            stale_ids = graph.memory_ids() - instance.active_memory_ids
            if stale_ids:
                graph.remove_nodes(stale_ids)
            misplaced_ids = {
                memory_id
                for memory_id in graph.memory_ids()
                if level not in instance._memory_graph_levels(
                    instance.memories[memory_id]
                )
            }
            if misplaced_ids:
                graph.remove_nodes(misplaced_ids)
        for memory_id in sorted(instance.active_memory_ids):
            instance._update_memory_in_layered_graphs(instance.memories[memory_id])

        graph_active_memory_ids = instance.layered_graph.memory_ids()
        missing_active_memories = instance.active_memory_ids - graph_active_memory_ids
        if missing_active_memories:
            raise ValueError(
                "active memories are missing from the normalized graph: "
                f"{sorted(missing_active_memories)}"
            )
        instance.topic_owner_router.rebuild(
            instance.memories[memory_id]
            for memory_id in sorted(instance.active_memory_ids)
        )
        unknown_active_segments = instance.active_graph.segment_ids() - set(instance.segments)
        if unknown_active_segments:
            raise ValueError(f"active graph has unknown segments: {sorted(unknown_active_segments)}")
        return instance
