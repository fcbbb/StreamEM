#!/usr/bin/env python3
"""A small end-to-end weekly memory based on graph communities.

This module is intentionally separate from ``stream_memory_multi_signal.py``.
It is an experiment runner, not a replacement for the existing production
implementation.

Design contract
---------------
* Input is already segmented text.  No filtering, sentence splitting, or
  second segmentation is performed here.
* The only online organization mechanism is a weighted semantic graph and a
  deterministic Leiden/Louvain community pass. After a checkpoint, a summary
  node replaces every raw node it covers in the active graph. The summary is
  one graph node; archived input segments are not node ancestry or features.
* L1/L2 memories are not created.  The archive still retains every original
  segment, while retrieval exposes exactly one C_con item per current
  community.  Structured records use ``summary`` for semantic search and
  ``title``/``keywords``/``summary`` for lexical and entity search; legacy
  text records retain the previous retrieval path.
* LLM calls happen only after a graph checkpoint, and only for a changed
  community whose size reaches ``summary_min_members``. The first call only
  removes obvious outliers from the algorithmic community. The second call
  updates an existing memory or initializes one from the retained evidence.
  Rejected raw nodes remain active graph nodes, but are blocked from the
  community that rejected them.
  A no-memory decision is archive-only and is excluded from the next active
  graph round.
  A failed call never deletes the previous summary or the source segments.

The ``WeeklyGraphMemorySystem`` adapter implements the same interface used by
``conversation_to_memory.py`` and ``memory_to_answer.py``.  The adapter uses
the fixed weekly segment artifact when it is available, so the official
conversation pipeline does not accidentally re-segment raw conversations.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import math
import os
import platform
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

try:
    from .graph_keyword_prompt import graph_keyword_rules
except ImportError:  # Direct module execution from evals/agent_eval/streamem.
    try:
        from graph_keyword_prompt import graph_keyword_rules
    except ImportError:
        def graph_keyword_rules(max_keywords: int) -> str:
            """Return the local fallback policy when the evaluator helper is absent."""
            return (
                "- anchored_phrases: return up to "
                f"{max_keywords} concrete phrases grounded in the evidence."
            )

try:
    import numpy as np
except ImportError:  # Keep import and smoke tests working in a minimal image.
    np = None  # type: ignore[assignment]

try:
    import networkx as nx
except ImportError:  # The minimal evaluator image does not ship networkx.
    nx = None  # type: ignore[assignment]

try:
    from base_evaluator import BaseMemorySystem
except ImportError:  # Direct module execution from evals/agent_eval/streamem.
    class BaseMemorySystem:  # pragma: no cover - only a lightweight import shim
        def __init__(self, user_id: str, **_: Any) -> None:
            self.user_id = user_id
            self.system_name = self.get_system_name()


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE
DEFAULT_FIXED_SEGMENTS = (
    HERE / "analyse2" / "graph_weekly" / "local_detect8-24" /
    "inputs" / "weekly_segments_v3.jsonl"
)
DEFAULT_ENCODER = "all-MiniLM-L6-v2"
_LOCAL_ENV_LOADED = False


# The active builder uses inventory classification and structured fusion.

_TWO_STAGE_PRUNE_PROMPT = """Review one graph-detected community and remove only blocks that are clearly unrelated to this community.

Judge each block against the community's shared topic or context. Keep blocks
that fit that topic or might reasonably belong to it. Reject only an obvious
outsider.

Partition every block ID exactly once and return json only in this shape:
{"keep_block_ids":["B0","B2"],"reject_block_ids":["B1"]}
"""

_TWO_STAGE_PRUNE_MAX_ATTEMPTS = 3
_TWO_STAGE_FUSE_MAX_ATTEMPTS = 3

# Structured fusion prompt: keep policy, update rules, field semantics, and
# output contract together so the model receives one unambiguous instruction.
_TWO_STAGE_STRUCTURED_POLICY = """Structured memory rules for one community.

MEMORY
- Keep only explicit, useful future facts whose truth is about the user, the
  user's state, or something the user controls, creates, does, experiences, or
  commits to.
  Preserve all details and qualifiers needed to keep the meaning.
- Exclude external-world content, assistant content, inference, one-off
  inquiries, and interaction mechanics. For an ongoing interest, keep at most
  one compact fact about the user's stable topic and scope, not what was learned
  during the discussion.
- Update only when a later statement explicitly corrects a fact or replaces a
  single-valued state. Keep facts about different dates, periods, or events
  separate. One-time events are valid memory. Resolve relative event times from
  the evidence Date and write the absolute date in the fact text; never invent
  time or history.

FACT TRANSACTION
- Current facts are the baseline and their fact_id values are stable. Each
  COMMUNITY lists valid record-qualified IDs such as R0:F0; if it lists none,
  only add is valid.
- Omitted facts are retained unchanged.
- add: provide a complete fact {text, dimension, time}; the program assigns ID.
- update: reference an existing fact_id, provide a complete replacement fact,
  and give a non-empty reason.
- delete: reference an existing fact_id only when evidence clearly proves
  removal or supersession, and give a non-empty reason. Otherwise, do not
  delete.

FIELDS
- title: concise and broad enough to cover the retained subject and facts.
- summary: concise complete unified state of all input content after applying operations.
{KEYWORD_RULES}
- Each fact is one independently updateable, retractable facet of the title's
  subject: one user-owned assertion with one subject and predicate, interpretable
  from the title alone. Split whenever one part could change while the rest stays
  true; conjunctions and semicolons are split points. Facts together must cover
  every explicit user facet and nothing else. dimension names the facet; time is
  the memory write date, not the event date.
FUSION SCOPE
- If CURRENT RECORDS contains multiple existing records, fuse the retained
  information from all of them into one unified title, summary, and anchored-phrase
  set; do not copy only one record.
- If CURRENT RECORDS contains exactly one existing record, preserve it as the
  baseline and update it only with relevant new evidence.

"""

_TWO_STAGE_FUSE_PROMPT_STRUCTURED = _TWO_STAGE_STRUCTURED_POLICY + """

FUSION OUTPUT
Return json only in this shape:
{"title":"...","summary":"...",
 "anchored_phrases":["specific anchored phrase"],
 "fact_operations":[
   {"op":"add","fact":{"text":"...","dimension":"...","time":"..."}},
   {"op":"update","fact_id":"R0:F0","fact":{"text":"...","dimension":"...","time":"..."},"reason":"..."},
   {"op":"delete","fact_id":"R0:F1","reason":"..."}
 ]}
"""

# This is intentionally a separate prompt. Initialization has no current
# records and therefore has no update/delete transaction to reconcile.
_TWO_STAGE_INITIAL_PROMPT_STRUCTURED = """Extract one structured user-memory record from the retained evidence of one community.

INPUT
- The input consists only of EVIDENCE BLOCKS from this community.

MEMORY FACTS
- Keep only explicit information whose truth is about the user, the user's own
  state, or something the user controls, creates, does, experiences, or commits
  to, and that can stand alone as useful future context. Preserve every explicit
  detail and qualifier needed to retain its meaning.
- Do not store external-world content, assistant content, unsupported inference,
  one-off inquiry content, or interaction mechanics. For an ongoing interest,
  keep at most one compact fact about the user's stable topic and scope, never
  the information learned through the discussion.
- A later statement updates an existing fact only if keeping both would be
  contradictory — i.e., it explicitly corrects the earlier one, or replaces the
  current value of a single-valued state. Statements about different dates,
  periods, or events are separate records, not updates.
- A one-time event is valid memory. Resolve relative event times using the
  evidence Date and write the absolute date in the fact text. Never invent
  time or history.

RECORD
- Create one self-contained record with a concise title and complete summary.
{KEYWORD_RULES}
- A fact is one facet of the subject named by the title — the smallest unit you
  would ever need to update, retract, or retrieve on its own. Its text states
  one user-owned assertion about exactly one facet: one subject, one predicate,
  interpretable given the title alone. Split whenever part of the text could be
  denied or changed while the rest stays true; a conjunction or semicolon
  joining two claims is always a split point. Each fact covers a distinct facet;
  together the facts cover every facet the source states, and nothing more.
  Dimension names that facet; time is the memory write date, not the event date.

OUTPUT
Return exactly one of these two JSON shapes:
{"is_memory":false,"memory":null}
{"is_memory":true,"memory":{"title":"...","summary":"...",
 "anchored_phrases":["..."],"fact_operations":[
   {"op":"add","fact":{"text":"...","dimension":"...","time":"..."}}
 ]}}
"""


def _render_two_stage_prompt(template: str, max_keywords: int) -> str:
    """Inject the one canonical graph-keyword policy into a memory prompt."""
    return template.replace(
        "{KEYWORD_RULES}", graph_keyword_rules(max_keywords)
    ).replace("{MAX_KEYWORDS}", str(max_keywords))

# Retrieval dependencies are optional because this experiment is also used in
# dependency-light smoke-test environments.  The semantic channel always
# works; BM25 has a small local implementation and the entity channel becomes
# empty when spaCy/model data are unavailable.
_GRAPH_ENTITY_MODEL: Any = None
_GRAPH_ENTITY_LOAD_ATTEMPTED = False
_SHARED_BM25: Any = None
_SHARED_LEX_TOKENS: Any = None
_SHARED_EXTRACT_ENTITIES: Any = None
try:
    from .multichannel import (
        TopicBM25 as _SHARED_BM25,
        lex_tokens as _SHARED_LEX_TOKENS,
        extract_entities as _SHARED_EXTRACT_ENTITIES,
    )
except ImportError:
    try:
        from multichannel import (
            TopicBM25 as _SHARED_BM25,
            lex_tokens as _SHARED_LEX_TOKENS,
            extract_entities as _SHARED_EXTRACT_ENTITIES,
        )
    except ImportError:
        pass
_GRAPH_LEX_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "he", "her", "hers", "him", "his", "i", "if", "in",
    "into", "is", "it", "its", "me", "my", "of", "on", "or", "our",
    "she", "that", "the", "their", "them", "there", "they", "this", "to",
    "was", "we", "were", "what", "when", "where", "which", "who", "will",
    "with", "you", "your", "user", "assistant", "just",
})


def _graph_lex_tokens(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]{2,}", str(text).lower())
        if token not in _GRAPH_LEX_STOP_WORDS
    }


def _graph_lex_token_list(text: str) -> list[str]:
    return [
        token for token in re.findall(r"[a-z0-9]{2,}", str(text).lower())
        if token not in _GRAPH_LEX_STOP_WORDS
    ]


class _GraphBM25:
    """Small BM25Okapi implementation for the community retrieval corpus."""

    def __init__(self, documents: list[tuple[str, str]]) -> None:
        self.ids = [doc_id for doc_id, _ in documents]
        self.tokens = [_graph_lex_token_list(text) for _, text in documents]
        self.lengths = [len(tokens) for tokens in self.tokens]
        self.avg_length = sum(self.lengths) / max(len(self.lengths), 1)
        document_frequency: Counter[str] = Counter()
        for tokens in self.tokens:
            document_frequency.update(set(tokens))
        self.idf = {
            token: math.log(
                1.0 + (len(self.tokens) - frequency + 0.5) / (frequency + 0.5)
            )
            for token, frequency in document_frequency.items()
        }

    def scores(self, query_tokens: set[str]) -> dict[str, float]:
        if not query_tokens or not self.tokens:
            return {}
        k1, b = 1.5, 0.75
        scores: dict[str, float] = {}
        for doc_id, tokens, length in zip(self.ids, self.tokens, self.lengths):
            term_frequency = Counter(tokens)
            score = 0.0
            for token in query_tokens:
                tf = term_frequency.get(token, 0)
                if not tf:
                    continue
                denominator = tf + k1 * (
                    1.0 - b + b * length / max(self.avg_length, 1e-12)
                )
                score += self.idf.get(token, 0.0) * (tf * (k1 + 1.0)) / denominator
            if score > 0.0:
                scores[doc_id] = float(score)
        return scores


def _graph_extract_entities(text: str) -> set[str]:
    global _GRAPH_ENTITY_MODEL, _GRAPH_ENTITY_LOAD_ATTEMPTED
    if not _GRAPH_ENTITY_LOAD_ATTEMPTED:
        _GRAPH_ENTITY_LOAD_ATTEMPTED = True
        try:
            import spacy
            _GRAPH_ENTITY_MODEL = spacy.load("en_core_web_trf")
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "spaCy NER unavailable, graph entity channel disabled: %s", exc
            )
            _GRAPH_ENTITY_MODEL = False
    if not _GRAPH_ENTITY_MODEL or not str(text).strip():
        return set()
    return {
        f"{entity.text.strip()}|{entity.label_}"
        for entity in _GRAPH_ENTITY_MODEL(str(text)).ents
        if entity.text.strip()
    }


# In the project .venv this selects the exact shared multi-signal primitives.
# The local implementations above remain the direct-script/minimal-env
# fallback, so importing this experiment does not become dependency-hard.
if _SHARED_BM25 is not None:
    _GraphBM25 = _SHARED_BM25
    _graph_lex_tokens = _SHARED_LEX_TOKENS

    def _graph_extract_entities(text: str) -> set[str]:
        return set(_SHARED_EXTRACT_ENTITIES(text))


def _graph_entity_overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _graph_rrf_fuse(rankings: list[list[str]], k_rrf: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k_rrf + rank + 1)
    return scores


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _load_local_env() -> None:
    """Load the project-local .env without requiring python-dotenv."""
    global _LOCAL_ENV_LOADED
    if _LOCAL_ENV_LOADED:
        return
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except ImportError:
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    _LOCAL_ENV_LOADED = True


def _unit(vector: Any) -> np.ndarray:
    if np is not None:
        result = np.asarray(vector, dtype=np.float32)
        return result / max(float(np.linalg.norm(result)), 1e-12)
    result = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in result))
    divisor = max(norm, 1e-12)
    return [value / divisor for value in result]


def _dot(left: Any, right: Any) -> float:
    if np is not None:
        return float(left @ right)
    return float(sum(float(a) * float(b) for a, b in zip(left, right)))


def _symmetric_best_match(left: Any, right: Any) -> float:
    """Compare two keyword-vector sets using the experiment's pair score.

    This deliberately does not average keywords into one document vector.
    Every keyword on either side is matched to its closest keyword on the
    other side, and the two directional means are averaged.
    """
    if left is None or right is None:
        return 0.0
    if np is not None:
        left_matrix = np.asarray(left, dtype=np.float32)
        right_matrix = np.asarray(right, dtype=np.float32)
        if left_matrix.size == 0 or right_matrix.size == 0:
            return 0.0
        if left_matrix.ndim == 1:
            left_matrix = left_matrix.reshape(1, -1)
        if right_matrix.ndim == 1:
            right_matrix = right_matrix.reshape(1, -1)
        pairwise = left_matrix @ right_matrix.T
        forward = float(np.max(pairwise, axis=1).mean())
        backward = float(np.max(pairwise, axis=0).mean())
        return (forward + backward) / 2.0
    left_rows = list(left)
    right_rows = list(right)
    if not left_rows or not right_rows:
        return 0.0
    forward = sum(max(_dot(a, b) for b in right_rows) for a in left_rows) / len(left_rows)
    backward = sum(max(_dot(a, b) for a in left_rows) for b in right_rows) / len(right_rows)
    return float((forward + backward) / 2.0)


def _vector_to_list(vector: Any) -> list[float]:
    return [float(value) for value in vector]


def _stable_id(prefix: str, members: Iterable[str]) -> str:
    digest = hashlib.sha1("\0".join(sorted(members)).encode("utf-8")).hexdigest()[:16]
    return f"graph-weekly:{prefix}:{digest}"


def _session_key(value: Any) -> str:
    text = str(value)
    if text.startswith("session_"):
        return text
    try:
        return f"session_{int(text):04d}"
    except (TypeError, ValueError):
        return text


def _json_date(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    text = str(value)
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    return match.group(0) if match else text[:10]


@dataclass
class Segment:
    id: str
    text: str
    group_id: str = ""
    segment_index: int = 0
    start_turn: Optional[int] = None
    end_turn: Optional[int] = None
    event_date: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "group_id": self.group_id,
            "segment_index": self.segment_index,
            "start_turn": self.start_turn,
            "end_turn": self.end_turn,
            "event_date": self.event_date,
            "metadata": self.metadata,
        }


@dataclass
class _TwoStagePruning:
    """Validated Stage-A keep/reject decision for one graph community."""

    kept_ids: set[str]
    rejected_ids: set[str]


class HashEncoder:
    """Tiny deterministic fallback used only when sentence-transformers is absent.

    It keeps smoke tests and the official adapter importable in a minimal
    environment.  Real runs should use a sentence-transformers encoder.
    """

    dimension = 384

    def encode(self, texts: Any, **_: Any) -> np.ndarray:
        values = [texts] if isinstance(texts, str) else list(texts)
        if np is None:
            output = [[0.0] * self.dimension for _ in values]
        else:
            output = np.zeros((len(values), self.dimension), dtype=np.float32)
        for row, text in enumerate(values):
            tokens = re.findall(r"[\w']+", str(text).lower())
            for token in tokens:
                digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "little") % self.dimension
                sign = 1.0 if digest[4] & 1 else -1.0
                if np is None:
                    output[row][index] += sign
                else:
                    output[row, index] += sign
        return output[0] if isinstance(texts, str) else output


class _SimpleGraph:
    """Small weighted undirected graph fallback for dependency-light runs."""

    def __init__(self) -> None:
        self._adj: dict[str, dict[str, dict[str, float]]] = {}

    def add_node(self, node: str) -> None:
        self._adj.setdefault(node, {})

    def add_nodes_from(self, nodes: Iterable[str]) -> None:
        for node in nodes:
            self.add_node(node)

    def add_edge(self, a: str, b: str, weight: float = 1.0) -> None:
        self.add_node(a)
        self.add_node(b)
        self._adj[a][b] = {"weight": float(weight)}
        self._adj[b][a] = {"weight": float(weight)}

    def has_edge(self, a: str, b: str) -> bool:
        return b in self._adj.get(a, {})

    def __getitem__(self, node: str) -> dict[str, dict[str, float]]:
        return self._adj[node]

    def edges(self) -> list[tuple[str, str]]:
        return [
            (a, b) for a in sorted(self._adj)
            for b in sorted(self._adj[a]) if a < b
        ]

    def number_of_edges(self) -> int:
        return len(self.edges())

    def connected_components(self) -> list[set[str]]:
        pending = set(self._adj)
        components: list[set[str]] = []
        while pending:
            root = min(pending)
            pending.remove(root)
            component = {root}
            stack = [root]
            while stack:
                node = stack.pop()
                for other in sorted(self._adj[node]):
                    if other in pending:
                        pending.remove(other)
                        component.add(other)
                        stack.append(other)
            components.append(component)
        return components


def load_encoder(model_name: str, device: str = "cpu") -> Any:
    try:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(model_name, device=device)
    except (ImportError, OSError, RuntimeError) as exc:
        logging.getLogger(__name__).warning(
            "sentence-transformers unavailable (%s); using deterministic hash encoder", exc
        )
        return HashEncoder()


class GraphWeeklyMemory:
    """Streaming fixed-segment graph memory.

    ``add_segment`` is the hot path.  It only stores the node and its edges.
    ``checkpoint`` is the cold path: community detection, summary generation,
    and active-index replacement all happen there.
    """

    schema_version = "graph_weekly_memory_v1"

    def __init__(
        self,
        encoder_model: str = DEFAULT_ENCODER,
        encoder_device: str = "cpu",
        tau_edge: float = 0.5,
        resolution: float = 1.0,
        # 2026-8-24修改为2，因为对于增量社区检测来说2也有意义了，可以用于判断社区归属
        summary_min_members: int = 2,
        llm_summarize_fn: Optional[Callable[[list[str]], str]] = None,
        two_stage_classify_prompt: str = "inventory",
        two_stage_fuse_prompt: str = "structured",
        max_display_keywords: int = 5,
        rrf_k: int = 60,
        encoder: Any = None,
        seed: int = 17,
        local_detection_community_hops: int = 0,
        community_prune_enabled: bool = True,
    ) -> None:
        self.encoder_model = encoder_model
        self.encoder_device = encoder_device
        self.tau_edge = float(tau_edge)
        self.resolution = float(resolution)
        self.summary_min_members = max(2, int(summary_min_members))
        self.llm_summarize_fn = llm_summarize_fn
        self.community_prune_enabled = bool(community_prune_enabled)
        if two_stage_classify_prompt != "inventory":
            raise ValueError("only the inventory classifier is supported")
        if two_stage_fuse_prompt != "structured":
            raise ValueError("only the structured fuse is supported")
        self.two_stage_classify_prompt = "inventory"
        self.two_stage_fuse_prompt = "structured"
        self.max_display_keywords = max(1, int(max_display_keywords))
        self.rrf_k = max(1, int(rrf_k))
        self.seed = int(seed)
        # 0 means unlimited expansion (the original Step 1 behavior). A
        # positive value limits expansion from seed communities to this many
        # hops in the community-adjacency graph.
        self.local_detection_community_hops = max(0, int(local_detection_community_hops))
        self.encoder = encoder
        # These are the parameters selected by the keyword best-match scan.
        # They are intentionally kept separate from the document embedding.
        self.keyword_ngram_range = (1, 3)
        self.keyword_top_n = 5
        self.keyword_diversity = 0.3
        # Keep stopwords during keyphrase extraction. Removing them before
        # building n-grams can splice words across sentence/turn boundaries
        # (for example, "complex topics research"), creating phrases that do
        # not occur in the source text.
        self.keyword_stop_words = None
        self._keyword_model: Any = None
        self.keyword_extraction_fallback = False
        self.segments: dict[str, Segment] = {}
        # ``vectors`` is the immutable archive representation of raw segments.
        # The current graph uses ``node_vectors`` instead: after a checkpoint a
        # summary node replaces all raw nodes covered by that summary.
        self.vectors: dict[str, np.ndarray] = {}
        self.node_vectors: dict[str, np.ndarray] = {}
        self.node_keywords: dict[str, list[str]] = {}
        # Keywords selected by the LLM for display/prompt context are kept
        # separate from deterministic graph keywords.
        self.node_display_keywords: dict[str, list[str]] = {}
        self.node_keyword_vectors: dict[str, np.ndarray] = {}
        # Identity only: every active node maps to itself. In particular, a
        # summary node never stores the raw segments that produced it here.
        self.node_members: dict[str, set[str]] = {}
        self.node_text: dict[str, str] = {}
        self.graph = nx.Graph() if nx is not None else _SimpleGraph()
        self.communities: dict[str, set[str]] = {}
        self.community_summaries: dict[str, list[dict[str, Any]]] = {}
        # A successfully judged no-memory community is archive-only.
        self.no_memory_communities: set[str] = set()
        # Raw nodes rejected by the community-pruning call are tracked for
        # audit/export. They remain active graph nodes and may connect to
        # other communities.
        self.pruned_node_ids: set[str] = set()
        # Community-pruning decisions create permanent cross-partition barriers.
        # Store raw member sets rather than transient graph-node IDs so the
        # constraint survives summary replacement and state reloads.
        self.separation_barriers: list[tuple[frozenset[str], frozenset[str]]] = []
        # Barrier provenance is separate from ``node_members``: summary nodes
        # remain atomic for community detection, while their source IDs let a
        # prune barrier follow the summary across later replacements.
        self.node_barrier_members: dict[str, set[str]] = {}
        self.active_items: dict[str, dict[str, Any]] = {}
        self.trace: list[dict[str, Any]] = []
        self.last_event_date: Optional[str] = None
        self.checkpoint_count = 0
        self.summary_calls = 0
        self.summary_failures = 0
        self.llm_errors: list[dict[str, Any]] = []
        self.llm_io_trace: list[dict[str, Any]] = []
        self._last_summary_no_memory = False
        self._last_rejected_node_ids: set[str] = set()
        # Kept only for compatibility with older state/report consumers. The
        # active pruning path never creates a multi-group inventory partition.
        self._last_inventory_groups: Optional[list[dict[str, Any]]] = None
        self._encoder_loaded = encoder is not None
        # Node IDs after the previous checkpoint.  Raw nodes added after a
        # summary replacement are therefore the only nodes considered new in
        # the next shadow-local comparison.
        self._shadow_previous_node_ids: set[str] = set()
        # The selected local partition is installed as the formal partition
        # for every checkpoint. It is reset for every checkpoint.
        self._last_shadow_local_partition: Optional[list[set[str]]] = None

    def initialize(self) -> None:
        if not self._encoder_loaded:
            self.encoder = load_encoder(self.encoder_model, self.encoder_device)
            self._encoder_loaded = True

    def _encode(self, texts: list[str]) -> list[np.ndarray]:
        self.initialize()
        if not texts:
            return []
        encoded = self.encoder.encode(texts, batch_size=64, show_progress_bar=False)
        if np is not None:
            matrix = np.asarray(encoded)
            if matrix.ndim == 1:
                matrix = matrix.reshape(1, -1)
            return [_unit(row) for row in matrix]
        return [_unit(row) for row in encoded]

    @staticmethod
    def _prepare_keyword_text(text: str) -> str:
        """Match the experiment's ``no_role_tags`` document preparation."""
        return re.sub(r"(?m)^\[(?:user|assistant)\]\s*", "", str(text)).strip()

    def _get_keyword_model(self) -> Any:
        self.initialize()
        if self._keyword_model is not None or self.keyword_extraction_fallback:
            return self._keyword_model
        try:
            from keybert import KeyBERT
            self._keyword_model = KeyBERT(model=self.encoder)
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            self.keyword_extraction_fallback = True
            logging.getLogger(__name__).warning(
                "KeyBERT unavailable (%s); using one text-vector fallback per node", exc
            )
        return self._keyword_model

    def _keyword_features(
        self,
        text: str,
        keywords: Optional[list[str]] = None,
    ) -> tuple[list[str], np.ndarray]:
        """Return independent keyword vectors for one graph node.

        ``keywords=[]`` is an explicit decision that this valid node has no
        reliable graph anchor. It therefore returns an empty vector set and
        never falls back to KeyBERT or the full text. ``keywords=None`` retains
        the legacy text-derived fallback path. The graph never uses input-node
        membership as a feature.
        """
        if keywords is not None:
            supplied = [
                str(value).strip() for value in keywords if str(value).strip()
            ]
            if not supplied:
                return [], (
                    np.empty((0, 0), dtype=np.float32) if np is not None else []
                )
            vectors = self._encode(supplied)
            return supplied, (
                np.asarray(vectors, dtype=np.float32) if np is not None else vectors
            )

        prepared = self._prepare_keyword_text(text)
        model = self._get_keyword_model()
        if model is not None and prepared:
            try:
                extracted_result = model.extract_keywords(
                    [prepared],
                    keyphrase_ngram_range=self.keyword_ngram_range,
                    stop_words=self.keyword_stop_words,
                    top_n=self.keyword_top_n,
                    use_mmr=True,
                    diversity=self.keyword_diversity,
                )
                # KeyBERT returns ``[(phrase, score), ...]`` for one
                # document, but ``[[(phrase, score), ...], ...]`` for some
                # multi-document versions. Accept both forms.
                if extracted_result and isinstance(extracted_result[0], tuple):
                    extracted = extracted_result
                else:
                    extracted = extracted_result[0] if extracted_result else []
                phrases = [str(phrase).strip() for phrase, _ in extracted if str(phrase).strip()]
                if phrases:
                    vectors = self._encode(phrases)
                    return phrases, (
                        np.asarray(vectors, dtype=np.float32) if np is not None else vectors
                    )
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                logging.getLogger(__name__).warning(
                    "KeyBERT extraction failed; using one text-vector fallback: %s", exc
                )
                self.keyword_extraction_fallback = True

        # A singleton text vector keeps a node comparable in a dependency-
        # light environment, but it is explicitly not a summary-keyword
        # concatenation and is reported as a fallback in stats.
        fallback = self._encode([prepared or text])[0]
        return [], (
            np.asarray([fallback], dtype=np.float32) if np is not None else [fallback]
        )

    def _node_similarity(self, left_id: str, right_id: str) -> float:
        left = self.node_keyword_vectors.get(left_id)
        right = self.node_keyword_vectors.get(right_id)
        if left is not None and right is not None:
            return _symmetric_best_match(left, right)
        return _dot(self.node_vectors[left_id], self.node_vectors[right_id])

    def _nodes_must_be_separated(self, left_id: str, right_id: str) -> bool:
        """Return whether an edge would cross an LLM-declared split."""
        left_members = self.node_barrier_members.get(left_id, {left_id})
        right_members = self.node_barrier_members.get(right_id, {right_id})
        for first, second in self.separation_barriers:
            if (
                (left_members & first and right_members & second)
                or (left_members & second and right_members & first)
            ):
                return True
        return False

    def _add_separation_barriers(
        self, groups: Iterable[set[str] | frozenset[str]]
    ) -> None:
        """Persist pairwise no-edge constraints for a split partition."""
        partitions = [frozenset(group) for group in groups if group]
        for index, first in enumerate(partitions):
            for second in partitions[index + 1:]:
                if not first or not second:
                    continue
                barrier = (first, second)
                reverse = (second, first)
                if barrier not in self.separation_barriers and reverse not in self.separation_barriers:
                    self.separation_barriers.append(barrier)

    def _candidate_keyword_rows(
        self,
        node_ids: set[str],
    ) -> list[dict[str, Any]]:
        """Build candidates from the nodes currently active in this community."""
        sources: dict[str, set[str]] = {}
        for node_id in sorted(node_ids):
            node_members = self.node_members.get(node_id, {node_id})
            keywords = list(self.node_keywords.get(node_id, []))
            keywords.extend(self.node_display_keywords.get(node_id, []))
            for keyword in dict.fromkeys(keywords):
                phrase = str(keyword).strip()
                if phrase:
                    sources.setdefault(phrase, set()).update(node_members)
        return [
            {
                "keyword": keyword,
                "frequency": len(source_ids),
                "source_node_ids": sorted(source_ids),
            }
            for keyword, source_ids in sorted(
                sources.items(), key=lambda item: (-len(item[1]), item[0].casefold(), item[0])
            )
        ]

    def _validate_selected_keywords(
        self,
        keywords: Any,
        candidates: set[str],
        *,
        dedupe: bool = False,
        max_keywords: Optional[int] = None,
    ) -> list[str]:
        """Validate LLM retrieval labels without forcing a closed vocabulary.

        ``candidates`` are extracted from the input and are useful hints, but
        they are not authoritative. A model may produce a more useful grounded
        phrase such as ``spent 55 coffee`` even when that exact phrase was not
        emitted by KeyBERT.

        With ``dedupe=True`` duplicate phrases are collapsed instead of failing
        the whole call.
        """
        del candidates  # Kept in the signature for persisted/older callers.
        if not isinstance(keywords, list):
            raise ValueError("anchored_phrases must be a list")
        selected: list[str] = []
        for value in keywords:
            if not isinstance(value, str):
                raise ValueError("anchored_phrases must contain only strings")
            phrase = re.sub(r"\s+", " ", value).strip()
            if not phrase:
                raise ValueError("anchored_phrases must not contain empty phrases")
            if len(phrase) > 120:
                raise ValueError("anchored_phrases must contain short phrases")
            if len(phrase.split()) < 2:
                logging.getLogger(__name__).warning(
                    "dropping anchored phrase below the 2-word minimum: %r",
                    phrase,
                )
                continue
            selected.append(phrase)
        limit = self.max_display_keywords if max_keywords is None else max(0, int(max_keywords))
        if len(selected) > limit:
            logging.getLogger(__name__).warning(
                "LLM returned %d anchored phrases; keeping the first %d",
                len(selected), limit,
            )
            selected = selected[:limit]
        if len({value.casefold() for value in selected}) != len(selected):
            if not dedupe:
                raise ValueError("anchored_phrases must not contain duplicates")
            seen: set[str] = set()
            deduped: list[str] = []
            for phrase in selected:
                key = phrase.casefold()
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(phrase)
            selected = deduped
        return selected

    @staticmethod
    def _row_to_segment(row: dict[str, Any], event_date: Optional[str] = None) -> Segment:
        segment_id = str(row.get("segment_id") or row.get("id") or "")
        if not segment_id:
            raise ValueError("fixed segment is missing segment_id")
        text = str(row.get("text") or row.get("raw_text") or "").strip()
        if not text:
            raise ValueError(f"fixed segment {segment_id} has empty text")
        return Segment(
            id=segment_id,
            text=text,
            group_id=str(row.get("group_id") or ""),
            segment_index=int(row.get("segment_index", 0)),
            start_turn=row.get("start_turn"),
            end_turn=row.get("end_turn"),
            event_date=_json_date(row.get("event_date") or event_date),
            metadata={
                key: row[key] for key in ("dataset", "unit_mode", "unit_count", "keywords") if key in row
            },
        )

    def add_segment(
        self,
        row: dict[str, Any] | Segment,
        event_date: Optional[str] = None,
        *,
        vector: Optional[np.ndarray] = None,
    ) -> str:
        segment = row if isinstance(row, Segment) else self._row_to_segment(row, event_date)
        if segment.id in self.segments:
            return segment.id
        if vector is None:
            vector = self._encode([segment.text])[0]
        else:
            vector = _unit(vector)

        self.segments[segment.id] = segment
        self.vectors[segment.id] = vector
        self.node_vectors[segment.id] = vector
        keywords, keyword_vectors = self._keyword_features(
            segment.text, segment.metadata.get("keywords")
        )
        self.node_keywords[segment.id] = keywords
        self.node_display_keywords[segment.id] = []
        self.node_keyword_vectors[segment.id] = keyword_vectors
        self.node_members[segment.id] = {segment.id}
        self.node_barrier_members[segment.id] = {segment.id}
        self.node_text[segment.id] = segment.text
        self.graph.add_node(segment.id)
        for other_id in self.node_vectors:
            if other_id == segment.id:
                continue
            if self._nodes_must_be_separated(segment.id, other_id):
                continue
            score = self._node_similarity(segment.id, other_id)
            if score > 0.0 and score >= self.tau_edge:
                self.graph.add_edge(segment.id, other_id, weight=max(score, 0.0))
        if segment.event_date:
            self.last_event_date = segment.event_date
        return segment.id

    def _rebuild_graph(self) -> None:
        """Rebuild edges after a dynamic summary representation is installed."""
        self.graph = nx.Graph() if nx is not None else _SimpleGraph()
        self.graph.add_nodes_from(self.node_vectors)
        ids = sorted(self.node_vectors)
        for index, left in enumerate(ids):
            for right in ids[index + 1:]:
                if self._nodes_must_be_separated(left, right):
                    continue
                score = self._node_similarity(left, right)
                if score > 0.0 and score >= self.tau_edge:
                    self.graph.add_edge(left, right, weight=max(score, 0.0))

    def _update_graph_incremental(
        self,
        old_graph: Any,
        old_node_ids: set[str],
    ) -> None:
        """Keep old edges and calculate edges only for newly created nodes."""
        next_ids = set(self.node_vectors)
        preserved_ids = old_node_ids & next_ids
        new_ids = next_ids - preserved_ids
        graph = nx.Graph() if nx is not None else _SimpleGraph()
        graph.add_nodes_from(sorted(next_ids))

        # Edges between unchanged nodes remain valid because their vectors and
        # representations were not changed.
        for left, right in old_graph.edges():
            if (
                left in preserved_ids
                and right in preserved_ids
                and not self._nodes_must_be_separated(left, right)
            ):
                graph.add_edge(left, right, weight=old_graph[left][right]["weight"])

        # A new summary node must be compared with every current node. New raw
        # segments were already connected on ingestion, but recomputing these
        # pairs is harmless and keeps this method correct for restored states.
        for left in sorted(new_ids):
            for right in sorted(next_ids):
                if left >= right:
                    continue
                if self._nodes_must_be_separated(left, right):
                    continue
                score = self._node_similarity(left, right)
                if score > 0.0 and score >= self.tau_edge:
                    graph.add_edge(left, right, weight=max(score, 0.0))
        self.graph = graph

    def add_segments(
        self,
        rows: Iterable[dict[str, Any]],
        event_date: Optional[str] = None,
        *,
        checkpoint_on_date_change: bool = False,
    ) -> dict[str, Any]:
        added: list[str] = []
        for row in rows:
            row_date = _json_date(row.get("event_date") or event_date)
            if (
                checkpoint_on_date_change and row_date and self.last_event_date
                and row_date != self.last_event_date and self.segments
            ):
                self.checkpoint(reason="event_date_change")
            before = len(self.segments)
            added_id = self.add_segment(row, event_date=row_date)
            if len(self.segments) > before:
                added.append(added_id)
        return {"added": len(added), "segment_ids": added}

    def _detect_communities(
        self,
        graph: Any = None,
        node_ids: Optional[set[str]] = None,
    ) -> list[set[str]]:
        """Run the configured community backend on the requested graph area."""
        graph = self.graph if graph is None else graph
        nodes = sorted(self.node_vectors if node_ids is None else node_ids)
        if not nodes:
            return []
        node_set = set(nodes)
        edges = [
            (left, right)
            for left, right in graph.edges()
            if left in node_set and right in node_set
        ]
        if not edges:
            return [{node} for node in nodes]
        try:
            import igraph as ig  # type: ignore
            import leidenalg  # type: ignore

            index = {node: i for i, node in enumerate(nodes)}
            igraph_edges = [(index[a], index[b]) for a, b in edges]
            weights = [graph[a][b].get("weight", 1.0) for a, b in edges]
            igraph_graph = ig.Graph(n=len(nodes), edges=igraph_edges)
            partition = leidenalg.find_partition(
                igraph_graph,
                leidenalg.RBConfigurationVertexPartition,
                weights=weights,
                resolution_parameter=self.resolution,
                seed=self.seed,
                n_iterations=-1,
            )
            return [{nodes[index] for index in block} for block in partition]
        except ImportError:
            if nx is None:
                if node_ids is None:
                    return graph.connected_components()
                return [
                    component & node_set
                    for component in graph.connected_components()
                    if component & node_set
                ]
            induced = nx.Graph()
            induced.add_nodes_from(nodes)
            induced.add_edges_from(
                (left, right, {"weight": graph[left][right].get("weight", 1.0)})
                for left, right in edges
            )
            return [
                set(block)
                for block in nx.algorithms.community.louvain_communities(
                    induced, weight="weight", resolution=self.resolution, seed=self.seed
                )
            ]

    @staticmethod
    def _partition_labels(
        node_ids: set[str], communities: list[set[str]]
    ) -> list[int]:
        labels: dict[str, int] = {}
        ordered = sorted(
            (set(group) & node_ids for group in communities),
            key=lambda group: (min(group) if group else "", len(group)),
        )
        for label, group in enumerate(ordered):
            for node_id in group:
                labels[node_id] = label
        next_label = len(ordered)
        for node_id in sorted(node_ids - set(labels)):
            labels[node_id] = next_label
            next_label += 1
        return [labels[node_id] for node_id in sorted(node_ids)]

    @staticmethod
    def _weighted_modularity(graph: Any, communities: list[set[str]]) -> float:
        edges = list(graph.edges())
        total_weight = sum(
            max(float(graph[left][right].get("weight", 1.0)), 0.0)
            for left, right in edges
        )
        if total_weight <= 1e-12:
            return 0.0
        degrees: Counter[str] = Counter()
        for left, right in edges:
            weight = max(float(graph[left][right].get("weight", 1.0)), 0.0)
            degrees[left] += weight
            degrees[right] += weight
        modularity = 0.0
        for community in communities:
            members = set(community)
            internal_weight = sum(
                max(float(graph[left][right].get("weight", 1.0)), 0.0)
                for left, right in edges
                if left in members and right in members
            )
            degree_weight = sum(degrees[node_id] for node_id in members)
            modularity += internal_weight / total_weight - (
                degree_weight / (2.0 * total_weight)
            ) ** 2
        return float(modularity)

    def _shadow_node_communities(
        self, node_ids: set[str]
    ) -> dict[str, str]:
        """Map current active nodes to their previous graph communities."""
        labels: dict[str, str] = {}
        for node_id in sorted(node_ids):
            raw_members = self.node_members.get(node_id, {node_id})
            candidates = [
                (len(raw_members & members), community_id)
                for community_id, members in self.communities.items()
                if raw_members & members
            ]
            if candidates:
                labels[node_id] = max(candidates, key=lambda item: (item[0], item[1]))[1]
        return labels

    def _shadow_local_report(
        self,
        global_partition: Optional[list[set[str]]],
        new_node_ids: set[str],
        previous_node_ids: set[str],
        global_elapsed_ms: float = 0.0,
    ) -> dict[str, Any]:
        """Run local detection and expose its partition.

        ``global_partition`` is accepted only for backwards-compatible unit
        tests/reports.  The streaming checkpoint path passes ``None`` after
        the first checkpoint, so no global detection is repeated.
        """
        self._last_shadow_local_partition = None
        current_node_ids = set(self.node_vectors)
        global_node_ids = (
            set().union(*global_partition) if global_partition else current_node_ids
        )
        node_ids = current_node_ids & global_node_ids
        empty_comparison = {
            "community_count_delta": 0,
            "changed_node_count": 0,
            "changed_node_ratio": 0.0,
            "global_elapsed_ms": float(global_elapsed_ms),
            "local_elapsed_ms": 0.0,
        }
        global_stats = None
        if global_partition is not None:
            global_stats = {
                "n_communities": len(global_partition),
                "modularity": self._weighted_modularity(self.graph, global_partition),
            }

        previous_node_ids &= current_node_ids
        previous_labels = self._shadow_node_communities(previous_node_ids)
        old_community_ids = set(previous_labels.values())
        neighbours: dict[str, set[str]] = {node_id: set() for node_id in current_node_ids}
        for left, right in self.graph.edges():
            if left in neighbours and right in neighbours:
                neighbours[left].add(right)
                neighbours[right].add(left)

        seed_communities: set[str] = set()
        for node_id in sorted(new_node_ids):
            seed_communities.update(
                previous_labels[other]
                for other in neighbours.get(node_id, set())
                if other in previous_labels
            )

        community_adjacency: dict[str, set[str]] = {
            community_id: set() for community_id in old_community_ids
        }
        for left, right in self.graph.edges():
            left_community = previous_labels.get(left)
            right_community = previous_labels.get(right)
            if (
                left_community is not None
                and right_community is not None
                and left_community != right_community
            ):
                community_adjacency[left_community].add(right_community)
                community_adjacency[right_community].add(left_community)

        affected_communities = set(seed_communities)
        pending = [(community_id, 0) for community_id in sorted(seed_communities)]
        while pending:
            community_id, hop = pending.pop()
            if (
                self.local_detection_community_hops > 0
                and hop >= self.local_detection_community_hops
            ):
                continue
            for neighbour in sorted(community_adjacency.get(community_id, set())):
                if neighbour not in affected_communities:
                    affected_communities.add(neighbour)
                    pending.append((neighbour, hop + 1))

        affected_nodes = set(new_node_ids)
        affected_nodes.update(
            node_id
            for node_id, community_id in previous_labels.items()
            if community_id in affected_communities
        )
        # Nodes that are not part of the previous formal partition are still
        # ordinary graph nodes. Include them in the normal local pass and let
        # their existing edges expand the affected region to neighboring
        # communities.
        unassigned_node_ids = node_ids - set(previous_labels)
        affected_nodes.update(unassigned_node_ids)
        for node_id in sorted(unassigned_node_ids):
            seed_communities.update(
                previous_labels[other]
                for other in neighbours.get(node_id, set())
                if other in previous_labels
            )
        if seed_communities:
            affected_communities.update(seed_communities)
            affected_nodes.update(
                node_id
                for node_id, community_id in previous_labels.items()
                if community_id in affected_communities
            )
        affected_nodes &= node_ids
        total_communities = len(old_community_ids)
        affected_node_ratio = len(affected_nodes) / max(len(node_ids), 1)
        affected_community_ratio = len(affected_communities) / max(total_communities, 1)
        base = {
            "mode": "shadow_local",
            "new_node_ids": sorted(new_node_ids),
            "affected_community_ids": sorted(affected_communities),
            "affected_node_count": len(affected_nodes),
            "total_node_count": len(node_ids),
            "affected_node_ratio": affected_node_ratio,
            "affected_community_count": len(affected_communities),
            "total_community_count": total_communities,
            "global": global_stats,
            # Kept as an audit field for old report consumers.  There is no
            # 30% limit and this flag never causes a global fallback.
            "region_limit_exceeded": False,
        }

        local_nodes = set(affected_nodes)
        local_started = time.perf_counter()
        local_subgraph = nx.Graph() if nx is not None else _SimpleGraph()
        local_subgraph.add_nodes_from(sorted(local_nodes))
        for left, right in self.graph.edges():
            if left in local_nodes and right in local_nodes:
                local_subgraph.add_edge(
                    left, right, weight=self.graph[left][right].get("weight", 1.0)
                )
        local_partition = self._detect_communities(local_subgraph, local_nodes)

        # Freeze every old community outside the affected region. New nodes
        # without an old community are deliberately left to the local pass.
        frozen = [
            {
                node_id for node_id, community_id in previous_labels.items()
                if community_id == old_community_id and node_id not in local_nodes
            }
            for old_community_id in sorted(old_community_ids)
        ]
        local_partition = [group for group in local_partition if group]
        local_partition.extend(group for group in frozen if group)
        covered = set().union(*local_partition) if local_partition else set()
        local_partition.extend({node_id} for node_id in sorted(node_ids - covered))
        local_elapsed_ms = (time.perf_counter() - local_started) * 1000
        self._last_shadow_local_partition = local_partition

        local_labels = self._partition_labels(node_ids, local_partition)
        comparison = dict(empty_comparison)
        comparison["local_elapsed_ms"] = float(local_elapsed_ms)
        if global_partition is not None:
            sorted_nodes = sorted(node_ids)
            global_labels = self._partition_labels(node_ids, global_partition)
            global_by_node = dict(zip(sorted_nodes, global_labels))
            local_by_node = dict(zip(sorted_nodes, local_labels))
            pair_overlaps = sorted(
                (
                    sum(
                        1
                        for node_id in node_ids
                        if local_by_node[node_id] == local_label
                        and global_by_node[node_id] == global_label
                    ),
                    local_label,
                    global_label,
                )
                for local_label in sorted(set(local_labels))
                for global_label in sorted(set(global_labels))
            )
            local_to_global: dict[int, int] = {}
            used_global: set[int] = set()
            for _, local_label, global_label in reversed(pair_overlaps):
                if local_label not in local_to_global and global_label not in used_global:
                    local_to_global[local_label] = global_label
                    used_global.add(global_label)
            changed_node_count = sum(
                local_to_global.get(local_by_node[node_id], -1) != global_by_node[node_id]
                for node_id in node_ids
            )
            comparison.update({
                "community_count_delta": len(local_partition) - len(global_partition),
                "changed_node_count": changed_node_count,
                "changed_node_ratio": changed_node_count / max(len(node_ids), 1),
            })
        return {
            **base,
            "fallback_to_global": False,
            "local": {
                "n_communities": len(local_partition),
                "modularity": self._weighted_modularity(self.graph, local_partition),
            },
            "comparison": comparison,
        }

    @staticmethod
    def community_backend() -> str:
        try:
            import igraph  # noqa: F401
            import leidenalg  # noqa: F401
            return "leidenalg"
        except ImportError:
            return "networkx_louvain" if nx is not None else "connected_components"


    def _summary_part(
        self,
        text: str,
        members: set[str],
        source: str,
        keywords: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        return {
            "text": text,
            "keywords": [str(value).strip() for value in (keywords or []) if str(value).strip()],
            "source": source,
            # Audit-only provenance. This is deliberately not copied to
            # node_members: a summary is one independent graph node.
            "n_input_nodes": len(members),
            "input_node_ids": sorted(members),
        }

    @staticmethod
    def _summary_text(value: Any) -> str:
        """Keep structured LLM summaries detailed when storing them as text."""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, indent=2)
        return str(value or "").strip()


    @staticmethod
    def _validate_summary_value(value: Any) -> None:
        """Reject a transcript or per-session log masquerading as a summary."""
        if not isinstance(value, (dict, list, str)):
            raise ValueError("summary must be an object, list, or text")
        transcript_metadata = {
            "sessions", "session_id", "dialogue", "speaker", "turns", "messages",
            "conversations",
        }

        def walk(item: Any) -> None:
            if isinstance(item, dict):
                if transcript_metadata & set(item):
                    raise ValueError("summary must extract facts, not copy a transcript")
                for child in item.values():
                    walk(child)
            elif isinstance(item, list):
                for child in item:
                    walk(child)

        walk(value)



    @staticmethod
    def _parse_json_response(raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("LLM response is not JSON")
            parsed = json.loads(text[start:end + 1])
        if not isinstance(parsed, dict):
            raise ValueError("LLM response must be a JSON object")
        return parsed

    def _record_llm_error(
        self,
        *,
        phase: str,
        community_id: str,
        members: set[str],
        exc: Exception,
    ) -> None:
        error = {
            "call": self.summary_calls,
            "phase": phase,
            "community_id": community_id,
            "n_members": len(members),
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000] or "<empty exception message>",
        }
        self.llm_errors.append(error)
        logging.getLogger(__name__).warning(
            "LLM %s failed for community=%s members=%d: %s: %s",
            phase,
            community_id,
            len(members),
            error["error_type"],
            error["error"],
        )

    def _record_llm_io(
        self,
        *,
        call: int,
        phase: str,
        community_id: str,
        members: set[str],
        prompt: str,
        response: str = "",
        error: Optional[Exception] = None,
        attempt: Optional[int] = None,
    ) -> None:
        """Persist the exact LLM input/output for per-round debugging."""
        row: dict[str, Any] = {
            "call": call,
            "phase": phase,
            "community_id": community_id,
            "input_node_ids": sorted(members),
            "prompt": prompt,
            "response": response,
            "status": "error" if error is not None else "ok",
        }
        if attempt is not None:
            row["attempt"] = attempt
        if error is not None:
            row["error_type"] = type(error).__name__
            row["error"] = str(error)[:1000] or "<empty exception message>"
        self.llm_io_trace.append(row)
    def _summarize_two_stage(
        self,
        community_id: str,
        members: set[str],
        node_ids: Optional[set[str]] = None,
    ) -> list[dict[str, Any]]:
        """Optionally prune a community, then update or initialize memory."""
        self._last_summary_no_memory = False
        self._last_rejected_node_ids = set()
        if self.llm_summarize_fn is None:
            return []
        node_ids = node_ids or members
        stored_ids = sorted(
            node_id for node_id in node_ids if node_id.startswith("summary:")
        )
        evidence_ids = sorted(
            node_id for node_id in node_ids if node_id in self.segments
        )
        if not evidence_ids:
            return []
        if self.community_prune_enabled:
            pruning = self._two_stage_prune(
                community_id, members, evidence_ids, stored_ids
            )
        else:
            # Keep the full detected community for the structured memory
            # stage.  In particular, do not create pruning barriers or mark
            # any node as pruned when Stage A is disabled.
            pruning = _TwoStagePruning(
                set(evidence_ids) | set(stored_ids), set()
            )
        if pruning is None:
            return []
        self._last_rejected_node_ids = set(pruning.rejected_ids)
        kept_evidence_ids = [
            node_id for node_id in evidence_ids if node_id in pruning.kept_ids
        ]
        kept_stored_ids = [
            node_id for node_id in stored_ids if node_id in pruning.kept_ids
        ]
        kept_node_ids = set(kept_stored_ids) | set(kept_evidence_ids)
        if not kept_node_ids:
            self._last_summary_no_memory = True
            return []
        # A single retained block is not enough evidence to justify the
        # second memory-extraction call. Keep it as raw evidence instead.
        # ``checkpoint`` still consumes ``_last_rejected_node_ids`` below and
        # installs the keep/reject separation barrier before rebuilding edges.
        if len(kept_node_ids) < 2:
            return []
        parts = self._two_stage_fuse_retained(
            community_id, kept_node_ids, kept_stored_ids, kept_evidence_ids
        )
        if parts is None:
            return []
        if not stored_ids and not parts:
            self._last_summary_no_memory = True
        return parts

    def _two_stage_prune(
        self,
        community_id: str,
        members: set[str],
        evidence_ids: list[str],
        stored_ids: list[str],
    ) -> Optional[_TwoStagePruning]:
        """Stage A: remove only obvious outliers from an algorithmic community."""
        model_to_node: dict[str, str] = {}
        blocks: list[str] = []
        for index, node_id in enumerate(evidence_ids):
            block_id = f"B{len(blocks)}"
            model_to_node[block_id] = node_id
            segment = self.segments[node_id]
            blocks.append(f"- block: {block_id}\n{segment.text}")
        for node_id in stored_ids:
            block_id = f"B{len(blocks)}"
            model_to_node[block_id] = node_id
            blocks.append(f"- block: {block_id}\n{self.node_text.get(node_id, '')}")
        if not blocks:
            return _TwoStagePruning(set(), set())

        system = _TWO_STAGE_PRUNE_PROMPT
        base_user = "Return a valid json object only.\n\nINPUT DATA\n\nBLOCKS:\n" + "\n\n".join(blocks)
        required_ids = set(model_to_node)
        repair_instruction = ""
        last_exc: Optional[Exception] = None
        for attempt in range(1, _TWO_STAGE_PRUNE_MAX_ATTEMPTS + 1):
            self.summary_calls += 1
            call = self.summary_calls
            attempt_user = base_user + repair_instruction
            prompt = f"SYSTEM:\n{system}\n\nUSER:\n{attempt_user}"
            raw = ""
            try:
                raw = str(self.llm_summarize_fn([system, attempt_user]) or "").strip()
                if not raw:
                    raise ValueError("empty community pruning response")
                payload = self._parse_json_response(raw)
                keep = payload.get("keep_block_ids")
                reject = payload.get("reject_block_ids", [])
                if not isinstance(keep, list) or not isinstance(reject, list):
                    raise ValueError("keep_block_ids and reject_block_ids must be lists")
                keep_ids = set(keep)
                reject_ids = set(reject)
                if any(not isinstance(value, str) for value in keep + reject):
                    raise ValueError("block IDs must be strings")
                if keep_ids & reject_ids:
                    raise ValueError("a block cannot be both kept and rejected")
                if keep_ids | reject_ids != required_ids:
                    missing = required_ids - keep_ids - reject_ids
                    extra = (keep_ids | reject_ids) - required_ids
                    detail = []
                    if missing:
                        detail.append("missing=" + ",".join(sorted(missing)))
                    if extra:
                        detail.append("unknown=" + ",".join(sorted(extra)))
                    raise ValueError("invalid block coverage: " + "; ".join(detail))
                kept_nodes = {model_to_node[value] for value in keep_ids}
                rejected_nodes = {model_to_node[value] for value in reject_ids}
                self._record_llm_io(
                    call=call,
                    phase="community_prune",
                    community_id=community_id,
                    members=members,
                    prompt=prompt,
                    response=raw,
                    attempt=attempt,
                )
                return _TwoStagePruning(kept_nodes, rejected_nodes)
            except Exception as exc:
                last_exc = exc
                self._record_llm_io(
                    call=call,
                    phase="community_prune",
                    community_id=community_id,
                    members=members,
                    prompt=prompt,
                    response=raw,
                    error=exc,
                    attempt=attempt,
                )
                repair_instruction = (
                    "\n\nREPAIR THE PREVIOUS RESPONSE\n"
                    f"It was invalid because: {exc}\n"
                    "Return a fresh complete json object. Every required block ID "
                    "must appear exactly once. Required IDs: "
                    + ", ".join(sorted(required_ids))
                )
        self.summary_failures += 1
        self._record_llm_error(
            phase="community_prune",
            community_id=community_id,
            members=members,
            exc=last_exc or ValueError("community pruning failed"),
        )
        return None

    def _two_stage_fuse_retained(
        self,
        community_id: str,
        members: set[str],
        stored_ids: list[str],
        evidence_ids: list[str],
    ) -> Optional[list[dict[str, Any]]]:
        """Stage B: merge retained evidence or initialize one record."""
        if not evidence_ids:
            return self._summary_parts_for_nodes(stored_ids)

        structured_records_by_node: dict[str, dict[str, Any]] = {}
        structured_prompt_records: dict[str, str] = {}
        record_fact_refs: dict[str, str] = {}
        record_blocks: list[str] = []
        # Normalize all records together so their internal fact IDs are unique
        # before facts are combined by the transaction validator.
        loaded_records = dict(self._structured_records_for_nodes(stored_ids))
        for index, node_id in enumerate(stored_ids):
            record = loaded_records.get(node_id)
            if record is not None:
                structured_records_by_node[node_id] = record
                prompt_record = copy.deepcopy(record)
                prompt_record["anchored_phrases"] = prompt_record.pop("keywords", [])
                for fact_index, fact in enumerate(prompt_record.get("facts", [])):
                    fact_ref = f"R{index}:F{fact_index}"
                    record_fact_refs[fact_ref] = str(fact["fact_id"])
                    fact["fact_id"] = fact_ref
                structured_prompt_records[node_id] = json.dumps(
                    prompt_record, ensure_ascii=False, indent=2
                )
            record_blocks.append(
                f"- record: R{index}\n"
                f"{structured_prompt_records.get(node_id, self.node_text.get(node_id, ''))}"
            )

        evidence_blocks: list[str] = []
        for index, node_id in enumerate(evidence_ids):
            segment = self.segments[node_id]
            date_line = f"Date: {segment.event_date}\n" if segment.event_date else ""
            evidence_blocks.append(f"- evidence: E{index}\n{date_line}{segment.text}")

        group_fact_refs = {"G0": dict(record_fact_refs)}
        group_node_ids = {"G0": set(members)}
        community_line = "- community"
        if evidence_ids:
            community_line += " | evidence: " + ", ".join(
                f"E{index}" for index in range(len(evidence_ids))
            )
        if stored_ids:
            community_line += " | records: " + ", ".join(
                f"R{index}" for index in range(len(stored_ids))
            )
        community_line += " | fact_ids: " + (
            ", ".join(record_fact_refs) if record_fact_refs else "none"
        )
        sections: list[str] = []
        if record_blocks:
            sections.append("CURRENT RECORDS:\n" + "\n\n".join(record_blocks))
        sections.append("COMMUNITY:\n" + community_line)
        sections.append("EVIDENCE BLOCKS:\n" + "\n\n".join(evidence_blocks))
        community_data = "\n\n".join(sections)
        has_existing_memory = bool(stored_ids)
        system = (
            _TWO_STAGE_FUSE_PROMPT_STRUCTURED
            if has_existing_memory
            else _TWO_STAGE_INITIAL_PROMPT_STRUCTURED
        )
        system = _render_two_stage_prompt(system, self.max_display_keywords)
        base_user = "Return a valid json object only.\n\nINPUT DATA\n\n" + community_data
        repair_instruction = ""
        last_exc: Optional[Exception] = None
        for attempt in range(1, _TWO_STAGE_FUSE_MAX_ATTEMPTS + 1):
            self.summary_calls += 1
            call = self.summary_calls
            attempt_user = base_user + repair_instruction
            prompt = f"SYSTEM:\n{system}\n\nUSER:\n{attempt_user}"
            raw = ""
            try:
                raw = str(self.llm_summarize_fn([system, attempt_user]) or "").strip()
                if not raw:
                    raise ValueError("empty memory state response")
                payload = self._parse_json_response(raw)
                if not has_existing_memory:
                    is_memory = payload.get("is_memory")
                    if not isinstance(is_memory, bool):
                        raise ValueError("initial response needs a boolean is_memory")
                    if not is_memory:
                        self._record_llm_io(
                            call=call,
                            phase="two_stage_initial",
                            community_id=community_id,
                            members=members,
                            prompt=prompt,
                            response=raw,
                            attempt=attempt,
                        )
                        return []
                    decision = payload.get("memory")
                    if not isinstance(decision, dict):
                        raise ValueError("memory initialization needs a memory object")
                else:
                    decision = payload.get("memory", payload)
                    if not isinstance(decision, dict):
                        raise ValueError("memory update needs a memory object")
                # The legacy validator supports transactions for multiple
                # groups. The active protocol has one community, so add its
                # internal compatibility ID only after parsing the model's
                # group-free response.
                payload = {"decisions": [{**decision, "group_id": "G0"}]}
                parts = self._validate_structured_fuse(
                    payload,
                    members,
                    1,
                    group_node_ids,
                    structured_records_by_node,
                    group_fact_refs,
                )
                self._record_llm_io(
                    call=call,
                    phase="two_stage_fuse" if has_existing_memory else "two_stage_initial",
                    community_id=community_id,
                    members=members,
                    prompt=prompt,
                    response=raw,
                    attempt=attempt,
                )
                return parts
            except Exception as exc:
                last_exc = exc
                self._record_llm_io(
                    call=call,
                    phase="two_stage_fuse" if has_existing_memory else "two_stage_initial",
                    community_id=community_id,
                    members=members,
                    prompt=prompt,
                    response=raw,
                    error=exc,
                    attempt=attempt,
                )
                repair_instruction = (
                    "\n\nREPAIR THE PREVIOUS RESPONSE\n"
                    f"It was invalid because: {exc}\n"
                    + (
                        "Return exactly {\"is_memory\":false,\"memory\":null} "
                        "for no memory, or set is_memory=true and provide one "
                        "valid memory object."
                        if not has_existing_memory
                        else "Return exactly one valid memory object."
                    )
                )
        self.summary_failures += 1
        self._record_llm_error(
            phase="two_stage_fuse" if has_existing_memory else "two_stage_initial",
            community_id=community_id,
            members=members,
            exc=last_exc or ValueError("memory update failed"),
        )
        return None

    def _two_stage_classify(
        self,
        community_id: str,
        members: set[str],
        evidence_ids: list[str],
        stored_ids: list[str],
    ) -> Optional[_TwoStagePruning]:
        """Compatibility entry point for callers using the old method name."""
        return self._two_stage_prune(community_id, members, evidence_ids, stored_ids)

        # Kept below only as source compatibility for old experiment traces;
        # the active path above never performs inventory grouping anymore.
        variant = "inventory"
        if variant == "inventory":
            # Stage A sees one origin-neutral namespace. The E/R distinction is
            # retained only in this private map for Stage B and graph updates.
            model_to_internal: dict[str, str] = {}
            uniform_blocks: list[str] = []
            for index, node_id in enumerate(evidence_ids):
                block_id = f"B{len(uniform_blocks)}"
                model_to_internal[block_id] = f"E{index}"
                segment = self.segments[node_id]
                date_line = (
                    f"Date: {segment.event_date}\n" if segment.event_date else ""
                )
                uniform_blocks.append(
                    f"- block: {block_id}\n{date_line}{segment.text}"
                )
            for index, node_id in enumerate(stored_ids):
                block_id = f"B{len(uniform_blocks)}"
                model_to_internal[block_id] = f"R{index}"
                uniform_blocks.append(
                    f"- block: {block_id}\n{self.node_text.get(node_id, '')}"
                )
            if not uniform_blocks:
                return {}
            community_data = "BLOCKS:\n" + "\n\n".join(uniform_blocks)
            system = _TWO_STAGE_CLASSIFY_PROMPT_INVENTORY
            user = "Return a valid json object only.\n\nINPUT DATA\n\n" + community_data
            required_ids = list(model_to_internal)
        else:
            raise ValueError(
                "graph weekly memory construction supports inventory classification only"
            )
        base_user = user
        required_id_set = set(required_ids)
        last_exc: Optional[Exception] = None
        repair_instruction = ""
        for attempt in range(1, _TWO_STAGE_CLASSIFY_MAX_ATTEMPTS + 1):
            self.summary_calls += 1
            call = self.summary_calls
            raw = ""
            attempt_user = base_user + repair_instruction
            prompt = f"SYSTEM:\n{system}\n\nUSER:\n{attempt_user}"
            try:
                raw = str(self.llm_summarize_fn([system, attempt_user]) or "").strip()
                if not raw:
                    raise ValueError("empty classification response")
                payload = self._parse_json_response(raw)
                by_id: dict[str, str] = {}
                if variant == "inventory":
                    groups = payload.get("groups")
                    if not isinstance(groups, list) or not groups:
                        raise ValueError("groups must be a non-empty list")
                    admitted_subjects: set[str] = set()
                    seen_model_ids: set[str] = set()
                    existing_memory_block_ids = {
                        model_id
                        for model_id, internal_id in model_to_internal.items()
                        if internal_id.startswith("R")
                    }
                    for group_index, group in enumerate(groups):
                        if not isinstance(group, dict):
                            raise ValueError("each group must be an object")
                        title = group.get("title")
                        is_memory = group.get("is_memory")
                        group_ids = group.get("block_ids")
                        if not isinstance(title, str) or not title.strip():
                            raise ValueError("each group needs a non-empty title")
                        if not isinstance(is_memory, bool):
                            raise ValueError("each group needs an is_memory verdict")
                        if not isinstance(group_ids, list) or not group_ids:
                            raise ValueError("each group needs a non-empty block_ids list")
                        title = re.sub(r"\s+", " ", title).strip()
                        internal_group_id = f"A{group_index}"
                        contains_existing_memory = any(
                            block_id in existing_memory_block_ids
                            for block_id in group_ids
                        )
                        if is_memory or contains_existing_memory:
                            admitted_subjects.add(internal_group_id)
                        for block_id in group_ids:
                            if not isinstance(block_id, str) or block_id not in required_id_set:
                                raise ValueError(
                                    "block_ids must contain only required block IDs"
                                )
                            if block_id in seen_model_ids:
                                raise ValueError("duplicate group membership for " + block_id)
                            seen_model_ids.add(block_id)
                            by_id[model_to_internal[block_id]] = internal_group_id
                returned_ids = seen_model_ids
                missing_ids = required_id_set - returned_ids
                if missing_ids:
                    raise ValueError(
                        "missing required block IDs: " + ", ".join(sorted(missing_ids))
                    )
                self._record_llm_io(
                    call=call,
                    phase="two_stage_classify",
                    community_id=community_id,
                    members=members,
                    prompt=prompt,
                    response=raw,
                    attempt=attempt,
                )
                return _TwoStageClassification(
                    by_id, admitted_subjects
                )
            except Exception as exc:
                last_exc = exc
                self._record_llm_io(
                    call=call,
                    phase="two_stage_classify",
                    community_id=community_id,
                    members=members,
                    prompt=prompt,
                    response=raw,
                    error=exc,
                    attempt=attempt,
                )
                repair_instruction = (
                    "\n\nREPAIR THE PREVIOUS RESPONSE\n"
                    f"It was invalid because: {exc}\n"
                    "Return a fresh complete json object. Required block IDs are: "
                    + ", ".join(required_ids)
                    + ". Every required ID must appear exactly once; do not add IDs."
                )
        self.summary_failures += 1
        self._record_llm_error(
            phase="two_stage_classify",
            community_id=community_id,
            members=members,
            exc=last_exc or ValueError("classification failed"),
        )
        return None


    def _validate_structured_fuse(
        self,
        payload: dict[str, Any],
        members: set[str],
        n_groups: int,
        group_node_ids: Optional[dict[str, set[str]]] = None,
        structured_records_by_node: Optional[dict[str, dict[str, Any]]] = None,
        group_fact_refs: Optional[dict[str, dict[str, str]]] = None,
    ) -> list[dict[str, Any]]:
        """Validate structured decisions and apply fact operations atomically."""
        decisions = payload.get("decisions")
        if not isinstance(decisions, list):
            raise ValueError("decisions must be a list")
        seen: set[str] = set()
        parts: list[dict[str, Any]] = []
        for decision in decisions:
            if not isinstance(decision, dict):
                raise ValueError("each decision must be an object")
            gid = decision.get("group_id")
            if not isinstance(gid, str) or not re.fullmatch(r"G\d+", gid):
                raise ValueError("group_id must be a group id like G0")
            if int(gid[1:]) >= n_groups:
                raise ValueError("decision references an unknown group")
            if gid in seen:
                raise ValueError("duplicate decision for " + gid)
            seen.add(gid)
            keywords = self._validate_selected_keywords(
                decision.get("anchored_phrases", decision.get("keywords", [])),
                set(),
                dedupe=True,
                max_keywords=self.max_display_keywords,
            )
            part_members = (
                set(group_node_ids.get(gid, set()))
                if group_node_ids is not None
                else set(members)
            )
            if not part_members:
                raise ValueError("memory decision has no input nodes for " + gid)
            title = decision.get("title")
            summary = decision.get("summary")
            if not isinstance(title, str) or not title.strip():
                raise ValueError("structured memory title must be non-empty text")
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError("structured memory summary must be non-empty text")
            title = re.sub(r"\s+", " ", title).strip()
            summary = re.sub(r"\s+", " ", summary).strip()
            if len(title) > 160:
                raise ValueError("structured memory title must be short")
            self._validate_summary_value(summary)

            stored_node_ids = sorted(
                node_id for node_id in part_members
                if node_id.startswith("summary:")
            )
            if not keywords and stored_node_ids:
                # An empty list during an incremental refresh means the model
                # found no better anchors. Preserve the current summary's
                # anchors by default; clearing them requires a separate,
                # explicit protocol rather than overloading [].
                preserved_keywords: list[str] = []
                for node_id in stored_node_ids:
                    node_keywords = list(
                        self.node_display_keywords.get(node_id, [])
                    )
                    record = (structured_records_by_node or {}).get(node_id)
                    if not node_keywords and record is not None:
                        node_keywords = [
                            str(value).strip()
                            for value in record.get("keywords", [])
                            if str(value).strip()
                        ]
                    if not node_keywords:
                        node_keywords = list(self.node_keywords.get(node_id, []))
                    for phrase in node_keywords:
                        if phrase and phrase.casefold() not in {
                            value.casefold() for value in preserved_keywords
                        }:
                            preserved_keywords.append(phrase)
                keywords = preserved_keywords[:self.max_display_keywords]
            facts: list[dict[str, Any]] = []
            if structured_records_by_node is not None:
                for node_id in stored_node_ids:
                    record = structured_records_by_node.get(node_id)
                    if record is not None:
                        facts.extend(copy.deepcopy(record.get("facts", [])))
            else:
                _, facts = self._structured_baseline(stored_node_ids)
            fact_by_id = {fact["fact_id"]: fact for fact in facts}
            operations = decision.get("fact_operations")
            if not isinstance(operations, list):
                raise ValueError("structured fact_operations must be a list")
            valid_fact_refs = dict((group_fact_refs or {}).get(gid, {}))
            if not valid_fact_refs:
                valid_fact_refs = {
                    fact_id: fact_id for fact_id in fact_by_id
                }
            seen_targets: set[str] = set()
            for operation in operations:
                if not isinstance(operation, dict):
                    raise ValueError("each fact operation must be an object")
                op = operation.get("op")
                if op not in {"add", "update", "delete"}:
                    raise ValueError("fact operation op must be add, update, or delete")
                if op == "add":
                    raw_fact = operation.get("fact")
                    fact_id = self._next_fact_id(set(fact_by_id))
                    new_fact = self._normalize_fact(raw_fact, fact_id)
                    if self._fact_signature(new_fact) not in {
                        self._fact_signature(existing) for existing in fact_by_id.values()
                    }:
                        fact_by_id[fact_id] = new_fact
                    continue

                fact_ref = operation.get("fact_id")
                if not isinstance(fact_ref, str):
                    raise ValueError(f"{op} must reference a record-qualified fact_id")
                fact_id = valid_fact_refs.get(fact_ref)
                if fact_id is None or fact_id not in fact_by_id:
                    raise ValueError(f"{op} references unknown fact_id {fact_ref}")
                if fact_id in seen_targets:
                    raise ValueError(f"fact_id {fact_ref} has multiple operations")
                seen_targets.add(fact_id)
                reason = operation.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    raise ValueError(f"{op} requires a non-empty reason")
                if op == "delete":
                    del fact_by_id[fact_id]
                else:
                    updated_fact = self._normalize_fact(
                        operation.get("fact"), fact_id
                    )
                    fact_by_id[fact_id] = updated_fact

            facts = list(fact_by_id.values())
            if not facts:
                raise ValueError("structured memory must retain or add at least one fact")
            memory_value = {
                "title": title,
                "summary": summary,
                "keywords": keywords,
                "facts": facts,
            }
            parts.append(
                self._summary_part(
                    self._summary_text(memory_value),
                    part_members,
                    "llm_two_stage",
                    keywords,
                )
            )
            parts[-1]["structured_memory"] = memory_value
        if len(seen) != n_groups:
            raise ValueError(
                f"every group needs a decision, got {len(seen)} of {n_groups}"
            )
        return parts

    def _existing_summary_passthrough(self, node_id: str) -> dict[str, Any]:
        """Carry one stored record forward without asking the LLM to rewrite it."""
        for old_parts in self.community_summaries.values():
            candidates = old_parts if isinstance(old_parts, list) else [old_parts]
            for old_part in candidates:
                if old_part.get("node_id") != node_id:
                    continue
                part = copy.deepcopy(old_part)
                part["node_id"] = node_id
                part["input_node_ids"] = [node_id]
                part["n_input_nodes"] = 1
                return part
        text = self.node_text.get(node_id, "")
        part = self._summary_part(
            text,
            {node_id},
            "existing_memory_passthrough",
            list(self.node_display_keywords.get(node_id, [])),
        )
        part["node_id"] = node_id
        try:
            structured_memory = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            structured_memory = None
        if isinstance(structured_memory, dict):
            part["structured_memory"] = structured_memory
        return part

    def _summary_parts_for_nodes(self, node_ids: Iterable[str]) -> list[dict[str, Any]]:
        """Recover summary metadata for active summary nodes.

        A summary node can move to a new graph community after a local
        detection pass.  Its node identity (and therefore its text) remains
        valid, even when the old community's summary mapping no longer does.
        Keep the summary attached to the current community in that case.
        """
        parts: list[dict[str, Any]] = []
        for node_id in sorted(set(node_ids)):
            if not str(node_id).startswith("summary:"):
                continue
            part = self._existing_summary_passthrough(str(node_id))
            if str(part.get("text", "")).strip():
                parts.append(part)
        return parts

    @staticmethod
    def _fact_signature(fact: dict[str, Any]) -> tuple[str, str, Optional[str]]:
        return (
            str(fact.get("text", "")).casefold(),
            str(fact.get("dimension", "")).casefold(),
            fact.get("time"),
        )

    def _normalize_fact(
        self,
        raw_fact: Any,
        fact_id: str,
    ) -> dict[str, Any]:
        """Validate one fact and attach the deterministic transaction ID."""
        if not isinstance(raw_fact, dict):
            raise ValueError("each structured fact must be an object")
        fact_text = raw_fact.get("text")
        dimension = raw_fact.get("dimension")
        fact_time = raw_fact.get("time")
        if not isinstance(fact_text, str) or not fact_text.strip():
            raise ValueError("each fact needs non-empty text")
        if not isinstance(dimension, str) or not dimension.strip():
            raise ValueError("each fact needs a non-empty dimension")
        if fact_time is not None and (
            not isinstance(fact_time, str) or not fact_time.strip()
        ):
            raise ValueError("fact time must be non-empty text or null")
        return {
            "fact_id": fact_id,
            "text": re.sub(r"\s+", " ", fact_text).strip(),
            "dimension": re.sub(r"\s+", " ", dimension).strip(),
            "time": (
                re.sub(r"\s+", " ", fact_time).strip()
                if isinstance(fact_time, str)
                else None
            ),
        }

    @staticmethod
    def _next_fact_id(used_ids: set[str]) -> str:
        index = 0
        while f"F{index}" in used_ids:
            index += 1
        return f"F{index}"

    def _structured_records_for_nodes(
        self,
        node_ids: Iterable[str],
    ) -> list[tuple[str, dict[str, Any]]]:
        """Load current records and give legacy facts stable per-group IDs."""
        records: list[tuple[str, dict[str, Any]]] = []
        used_ids: set[str] = set()
        for node_id in sorted(set(node_ids)):
            try:
                record = json.loads(self.node_text.get(node_id, ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            raw_facts = record.get("facts", [])
            if raw_facts is None:
                raw_facts = []
            if not isinstance(raw_facts, list):
                raise ValueError("structured record facts must be a list")
            normalized = copy.deepcopy(record)
            normalized_facts: list[dict[str, Any]] = []
            for raw_fact in raw_facts:
                requested_id = raw_fact.get("fact_id") if isinstance(raw_fact, dict) else None
                fact_id = (
                    str(requested_id)
                    if isinstance(requested_id, str)
                    and re.fullmatch(r"F\d+", requested_id)
                    and requested_id not in used_ids
                    else self._next_fact_id(used_ids)
                )
                normalized_facts.append(self._normalize_fact(raw_fact, fact_id))
                used_ids.add(fact_id)
            normalized["facts"] = normalized_facts
            records.append((node_id, normalized))
        return records

    def _structured_baseline(
        self,
        node_ids: Iterable[str],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        records = self._structured_records_for_nodes(node_ids)
        baseline: dict[str, Any] = {}
        facts: list[dict[str, Any]] = []
        for _, record in records:
            if not baseline:
                for key in ("title", "summary", "keywords"):
                    if key in record:
                        baseline[key] = copy.deepcopy(record[key])
            facts.extend(copy.deepcopy(record.get("facts", [])))
        return baseline, facts

    def _two_stage_fuse(
        self,
        community_id: str,
        members: set[str],
        stored_ids: list[str],
        evidence_ids: list[str],
        classifications: _TwoStageClassification,
    ) -> Optional[list[dict[str, Any]]]:
        """Stage B: reconcile classified groups into the next record state.

        Inventory classifications filter out declined subjects before the
        structured prompt is built.
        """
        assignments = classifications.assignments
        admitted_subjects = classifications.admitted_subjects
        passthrough_parts: list[dict[str, Any]] = []
        group_order: list[str] = []
        group_members: dict[str, list[str]] = {}
        # Reconstruct origin only after Stage A has finished. Rejected new-only
        # groups disappear; rejected groups containing records preserve those
        # records unchanged. A lone admitted record bypasses fusion because
        # there is nothing to update or merge.
        if admitted_subjects:
            routed_order: list[str] = []
            routed_members: dict[str, list[str]] = {}
            for prefix, count in (("E", len(evidence_ids)), ("R", len(stored_ids))):
                for index in range(count):
                    short_id = f"{prefix}{index}"
                    group_id = assignments[short_id]
                    if group_id not in routed_members:
                        routed_members[group_id] = []
                        routed_order.append(group_id)
                    routed_members[group_id].append(short_id)
            for group_id in routed_order:
                subject_members = routed_members[group_id]
                evs = [value for value in subject_members if value.startswith("E")]
                recs = [value for value in subject_members if value.startswith("R")]
                if group_id not in admitted_subjects:
                    passthrough_parts.extend(
                        self._existing_summary_passthrough(
                            stored_ids[int(value[1:])]
                        )
                        for value in recs
                    )
                    continue
                if not evs and len(recs) == 1:
                    passthrough_parts.append(
                        self._existing_summary_passthrough(
                            stored_ids[int(recs[0][1:])]
                        )
                    )
                    continue
                group_order.append(group_id)
                group_members[group_id] = subject_members
        n_groups = len(group_order)
        if n_groups == 0:
            return passthrough_parts
        llm_member_ids = {
            value for values in group_members.values() for value in values
        }
        structured_records_by_node: dict[str, dict[str, Any]] = {}
        structured_prompt_records: dict[str, str] = {}
        record_fact_refs: dict[str, dict[str, str]] = {}
        prompt_record_nodes = [
            stored_ids[index]
            for index in range(len(stored_ids))
            if f"R{index}" in llm_member_ids
        ]
        record_aliases = {
            node_id: f"R{index}"
            for index, node_id in enumerate(stored_ids)
        }
        for node_id, record in self._structured_records_for_nodes(
            prompt_record_nodes
        ):
            structured_records_by_node[node_id] = record
            prompt_record = copy.deepcopy(record)
            prompt_record["anchored_phrases"] = prompt_record.pop("keywords", [])
            refs: dict[str, str] = {}
            record_alias = record_aliases[node_id]
            for fact_index, fact in enumerate(prompt_record.get("facts", [])):
                canonical_id = str(fact["fact_id"])
                fact_ref = f"{record_alias}:F{fact_index}"
                fact["fact_id"] = fact_ref
                refs[fact_ref] = canonical_id
            record_fact_refs[node_id] = refs
            structured_prompt_records[node_id] = json.dumps(
                prompt_record, ensure_ascii=False, indent=2
            )
        group_node_ids: dict[str, set[str]] = {}
        group_fact_refs: dict[str, dict[str, str]] = {}
        group_blocks = []
        for g, group_id in enumerate(group_order):
            members_list = group_members[group_id]
            gid = f"G{g}"
            evs = [x for x in members_list if x.startswith("E")]
            recs = [x for x in members_list if x.startswith("R")]
            group_node_ids[gid] = {
                evidence_ids[int(value[1:])]
                if value.startswith("E")
                else stored_ids[int(value[1:])]
                for value in members_list
            }
            # Do not pass Stage A's generated title into Stage B as a subject.
            # It can over-specify one facet of the evidence (for example,
            # turning a step record into a goal-only record). Stage B still
            # receives the group boundary and must generate the output title.
            line = f"- group: {gid}"
            if evs:
                line += " | evidence: " + ", ".join(evs)
            if recs:
                line += " | records: " + ", ".join(recs)
            refs: dict[str, str] = {}
            for value in recs:
                refs.update(record_fact_refs.get(stored_ids[int(value[1:])], {}))
            group_fact_refs[gid] = refs
            line += " | fact_ids: " + (", ".join(refs) if refs else "none")
            group_blocks.append(line)
        evidence_blocks = []
        for index, node_id in enumerate(evidence_ids):
            if f"E{index}" not in llm_member_ids:
                continue
            segment = self.segments[node_id]
            date_line = f"Date: {segment.event_date}\n" if segment.event_date else ""
            evidence_blocks.append(f"- evidence: E{index}\n{date_line}{segment.text}")
        record_blocks = [
            f"- record: R{index}\n{structured_prompt_records.get(node_id, self.node_text.get(node_id, ''))}"
            for index, node_id in enumerate(stored_ids)
            if f"R{index}" in llm_member_ids
        ]
        sections: list[str] = []
        if record_blocks:
            sections.append("CURRENT RECORDS:\n" + "\n\n".join(record_blocks))
        if group_blocks:
            sections.append("GROUPS:\n" + "\n".join(group_blocks))
        if evidence_blocks:
            sections.append("EVIDENCE BLOCKS:\n" + "\n\n".join(evidence_blocks))
        if not sections:
            return []
        community_data = "\n\n".join(sections)
        system = _render_two_stage_prompt(
            _TWO_STAGE_FUSE_PROMPT_STRUCTURED, self.max_display_keywords
        )
        # Some OpenAI-compatible providers enforce response_format by scanning
        # user-visible input for the lowercase token "json".
        user = "Return a valid json object only.\n\nINPUT DATA\n\n" + community_data
        base_user = user
        required_group_ids = [f"G{index}" for index in range(n_groups)]
        fact_ref_summary = "; ".join(
            f"{gid}: {', '.join(group_fact_refs.get(gid, {})) or 'none'}"
            for gid in required_group_ids
        )
        repair_instruction = ""
        last_exc: Optional[Exception] = None
        for attempt in range(1, _TWO_STAGE_FUSE_MAX_ATTEMPTS + 1):
            attempt_user = base_user + repair_instruction
            prompt = f"SYSTEM:\n{system}\n\nUSER:\n{attempt_user}"
            self.summary_calls += 1
            call = self.summary_calls
            raw = ""
            try:
                raw = str(
                    self.llm_summarize_fn([system, attempt_user]) or ""
                ).strip()
                if not raw:
                    raise ValueError("empty memory state response")
                payload = self._parse_json_response(raw)
                parts = self._validate_structured_fuse(
                    payload,
                    members,
                    n_groups,
                    group_node_ids,
                    structured_records_by_node,
                    group_fact_refs,
                )
                self._record_llm_io(
                    call=call,
                    phase="two_stage_fuse",
                    community_id=community_id,
                    members=members,
                    prompt=prompt,
                    response=raw,
                    attempt=attempt,
                )
                return passthrough_parts + parts
            except Exception as exc:
                last_exc = exc
                self._record_llm_io(
                    call=call,
                    phase="two_stage_fuse",
                    community_id=community_id,
                    members=members,
                    prompt=prompt,
                    response=raw,
                    error=exc,
                    attempt=attempt,
                )
                repair_instruction = (
                    "\n\nREPAIR THE PREVIOUS RESPONSE\n"
                    f"It was invalid because: {exc}\n"
                    "Return a fresh complete json object with exactly one "
                    "decision for each required group: "
                    + ", ".join(required_group_ids)
                    + ". Do not add group IDs. Available fact_ids by group: "
                    + fact_ref_summary
                    + ". Use add when a group has no fact_ids."
                )
        self.summary_failures += 1
        self._record_llm_error(
            phase="two_stage_fuse",
            community_id=community_id,
            members=members,
            exc=last_exc or ValueError("two-stage fuse failed"),
        )
        return None

    def _summarize(
        self,
        community_id: str,
        members: set[str],
        node_ids: Optional[set[str]] = None,
    ) -> list[dict[str, Any]]:
        self._last_inventory_groups = None
        if self.llm_summarize_fn is None:
            return []
        return self._summarize_two_stage(community_id, members, node_ids)

    def _mean_internal_weight(self, node_ids: set[str]) -> float:
        values = [
            self.graph[a][b]["weight"]
            for a in node_ids
            for b in node_ids
            if a < b and self.graph.has_edge(a, b)
        ]
        return float(sum(values) / len(values)) if values else 0.0

    def _install_next_graph(
        self,
        detected: list[set[str]],
        summaries: dict[str, list[dict[str, Any]]],
        community_groups: Optional[list[dict[str, Any]]] = None,
    ) -> int:
        """Install the post-checkpoint graph representation.

        ``community_groups`` is the normalized checkpoint output. Inventory
        groups can produce several independent community records and graph
        nodes from one candidate graph community.
        The fallback construction keeps the old one-community behavior for
        callers that load/use this method with older state.
        """
        next_vectors: dict[str, np.ndarray] = {}
        next_keywords: dict[str, list[str]] = {}
        next_display_keywords: dict[str, list[str]] = {}
        next_keyword_vectors: dict[str, np.ndarray] = {}
        next_members: dict[str, set[str]] = {}
        next_barrier_members: dict[str, set[str]] = {}
        next_text: dict[str, str] = {}
        replaced = 0

        if community_groups is None:
            community_groups = []
            for node_group in detected:
                community_id = _stable_id("community", node_group)
                community_groups.append({
                    "community_id": community_id,
                    "members": set(node_group),
                    "node_ids": set(node_group),
                    "summaries": summaries.get(community_id, []),
                })

        def install_node(node_id: str) -> None:
            """Carry one current graph node into the next graph round."""
            if node_id not in self.node_vectors:
                return
            next_vectors[node_id] = self.node_vectors[node_id]
            next_keywords[node_id] = list(self.node_keywords.get(node_id, []))
            next_display_keywords[node_id] = list(
                self.node_display_keywords.get(node_id, [])
            )
            next_keyword_vectors[node_id] = self.node_keyword_vectors[node_id]
            next_members[node_id] = {node_id}
            next_barrier_members[node_id] = set(
                self.node_barrier_members.get(node_id, {node_id})
            )
            next_text[node_id] = self.node_text.get(node_id, "")

        for group in community_groups:
            node_group = set(group.get("node_ids", set()))
            raw_members = set(group.get("members", set()))
            community_id = str(group["community_id"])
            # A successful no-memory decision is archive-only. Do not carry
            # these nodes into the next graph round, otherwise a later graph
            # change could send the same discarded content back to the LLM.
            if community_id in self.no_memory_communities:
                continue
            parts = group.get("summaries", summaries.get(community_id, []))
            if isinstance(parts, dict):
                parts = [parts]
            covered: set[str] = set()

            for part_index, part in enumerate(parts):
                existing_node_id = str(part.get("node_id", ""))
                input_nodes = set(
                    part.get("input_node_ids", part.get("member_ids", []))
                )
                # An already-installed summary is itself the only node to
                # carry forward. Its old input IDs are audit metadata only.
                if existing_node_id in node_group:
                    part_nodes = {existing_node_id}
                else:
                    part_nodes = input_nodes & raw_members
                if not part_nodes:
                    continue
                node_id = existing_node_id or f"summary:{community_id}:part{part_index:02d}"
                keywords = [str(value) for value in part.get("keywords", [])]
                representation = str(part.get("text", ""))
                if not representation.strip():
                    continue
                part_node_ids = set(part_nodes)
                if node_id in self.node_vectors and existing_node_id:
                    next_vectors[node_id] = self.node_vectors[node_id]
                    if keywords:
                        selected_keywords, selected_keyword_vectors = (
                            self._keyword_features(representation, keywords)
                        )
                        next_keywords[node_id] = selected_keywords
                        next_keyword_vectors[node_id] = selected_keyword_vectors
                        next_display_keywords[node_id] = list(keywords)
                    else:
                        # Passthrough/refresh with [] keeps the existing graph
                        # anchors. [] means "no replacement", not "delete".
                        old_keywords = list(self.node_keywords.get(node_id, []))
                        old_display_keywords = list(
                            self.node_display_keywords.get(node_id, [])
                        )
                        old_keyword_vectors = self.node_keyword_vectors.get(node_id)
                        if old_keyword_vectors is None:
                            old_keywords, old_keyword_vectors = self._keyword_features(
                                representation,
                                old_display_keywords or old_keywords,
                            )
                        next_keywords[node_id] = old_keywords
                        next_keyword_vectors[node_id] = old_keyword_vectors
                        next_display_keywords[node_id] = old_display_keywords
                else:
                    # A summary is a normal graph node. Its document vector
                    # comes from the returned text, while its graph keyword
                    # features use the LLM-selected keywords when available.
                    next_vectors[node_id] = self._encode([representation])[0]
                    summary_keywords, summary_keyword_vectors = self._keyword_features(
                        representation, keywords
                    )
                    next_keywords[node_id] = summary_keywords
                    next_display_keywords[node_id] = list(keywords)
                    next_keyword_vectors[node_id] = summary_keyword_vectors
                    replaced += 1
                # A summary node has exactly one graph identity, regardless
                # of how many input nodes produced it.
                next_members[node_id] = {node_id}
                barrier_members: set[str] = set()
                for input_node_id in part_nodes:
                    barrier_members.update(
                        self.node_barrier_members.get(input_node_id, {input_node_id})
                    )
                next_barrier_members[node_id] = barrier_members or {node_id}
                next_text[node_id] = representation
                part["node_id"] = node_id
                part["representation_text"] = representation
                part["representation_replaced"] = True
                covered |= part_node_ids

            # Keep current graph nodes only when they were not covered by a
            # newly-created summary. There is no expansion back to archived
            # raw segments: a summary node is atomic in the active graph.
            for node_id in sorted(node_group):
                if node_id in covered:
                    continue
                install_node(node_id)

        # Pruned nodes are intentionally not part of the rejecting community
        # or any synthetic community. Carry them as unassigned graph nodes so
        # their permitted edges survive this checkpoint. The regular detector
        # handles unassigned nodes on the next checkpoint.
        for node_id in sorted(self.pruned_node_ids):
            if node_id in self.node_vectors and node_id not in next_vectors:
                install_node(node_id)

        old_graph = self.graph
        old_node_ids = set(self.node_vectors)
        self.node_vectors = next_vectors
        self.node_keywords = next_keywords
        self.node_display_keywords = next_display_keywords
        self.node_keyword_vectors = next_keyword_vectors
        self.node_members = next_members
        self.node_barrier_members = next_barrier_members
        self.node_text = next_text
        self._update_graph_incremental(old_graph, old_node_ids)
        return replaced

    def _rebuild_active_index(self) -> None:
        active: dict[str, dict[str, Any]] = {}
        for community_id, members in sorted(self.communities.items()):
            if community_id in self.no_memory_communities:
                continue
            # The active index mirrors the current graph exactly. A summary
            # node is one item and never expands into its archived inputs.
            for node_id in sorted(members):
                if node_id not in self.node_vectors:
                    continue
                is_summary = node_id.startswith("summary:")
                item_id = node_id if is_summary else f"segment:{node_id}"
                text = self.node_text.get(node_id, "")
                segment = self.segments.get(node_id)
                active[item_id] = {
                    "id": item_id,
                    "text": text,
                    "keywords": list(self.node_display_keywords.get(node_id, [])),
                    "representation_text": text,
                    "source": "community_summary" if is_summary else "segment",
                    "community_id": community_id,
                    "member_ids": [node_id],
                    "n_members": 1,
                    "event_dates": (
                        [segment.event_date]
                        if segment is not None and segment.event_date
                        else []
                    ),
                }
        texts = [item.get("representation_text", item["text"]) for item in active.values()]
        vectors = self._encode(texts)
        for item, vector in zip(active.values(), vectors):
            item["vector"] = vector
        self.active_items = active

    def _migrate_legacy_split_communities(self) -> None:
        """Promote legacy multi-part communities to independent communities.

        Older states stored an LLM split as several summary parts under one
        community ID.  New checkpoints store one LLM group per community.  A
        load-time migration keeps those states consistent with the new
        retrieval identity without touching the archived raw segments.
        """
        migrated_communities: dict[str, set[str]] = {}
        migrated_summaries: dict[str, list[dict[str, Any]]] = {}
        migrated_no_memory: set[str] = set()
        migrated_any = False

        for community_id, members in self.communities.items():
            summaries = self.community_summaries.get(community_id, [])
            if isinstance(summaries, dict):
                summaries = [summaries]
            if len(summaries) <= 1:
                migrated_communities[community_id] = set(members)
                migrated_summaries[community_id] = summaries
                if community_id in self.no_memory_communities:
                    migrated_no_memory.add(community_id)
                continue

            part_memberships: list[tuple[set[str], dict[str, Any]]] = []
            seen: set[str] = set()
            valid = True
            for part in summaries:
                part_members = set(
                    part.get("input_node_ids", part.get("member_ids", []))
                ) & set(members)
                if not part_members or seen & part_members:
                    valid = False
                    break
                seen |= part_members
                part_memberships.append((part_members, part))
            if not valid:
                migrated_communities[community_id] = set(members)
                migrated_summaries[community_id] = summaries
                continue

            migrated_any = True
            self._add_separation_barriers(
                [part_members for part_members, _ in part_memberships]
            )
            for part_members, part in part_memberships:
                split_id = _stable_id("community", part_members)
                migrated_communities[split_id] = part_members
                migrated_summaries[split_id] = [part]
            remaining = set(members) - seen
            if remaining:
                remaining_id = _stable_id("community", remaining)
                migrated_communities[remaining_id] = remaining
                migrated_summaries[remaining_id] = []

        if migrated_any:
            self.communities = migrated_communities
            self.community_summaries = migrated_summaries
            self.no_memory_communities = migrated_no_memory

    def _state_snapshot(
        self,
        communities: dict[str, set[str]],
        summaries: dict[str, Any],
        active_items: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Return a JSON-safe audit snapshot without embedding vectors."""
        assigned = {sid for members in communities.values() for sid in members}
        community_rows = []
        for community_id, members in sorted(communities.items()):
            parts = summaries.get(community_id, []) if summaries else []
            if isinstance(parts, dict):
                parts = [parts]
            community_rows.append({
                "community_id": community_id,
                "member_ids": sorted(members),
                "n_members": len(members),
                "summary_parts": [
                    {
                        "text": str(part.get("text", "")),
                        "keywords": list(part.get("keywords", [])),
                        "source": part.get("source", ""),
                        "input_node_ids": sorted(
                            part.get("input_node_ids", part.get("member_ids", []))
                        ),
                    }
                    for part in parts
                ],
            })
        active_rows = []
        for item_id, item in sorted(active_items.items()):
            active_rows.append({
                key: value for key, value in item.items()
                if key != "vector"
            })
        return {
            "n_segments": len(self.segments),
            "n_edges": self.graph.number_of_edges(),
            "n_communities": len(communities),
            "pending_node_ids": sorted(set(self.node_vectors) - assigned),
            "communities": community_rows,
            "active_items": active_rows,
        }

    def checkpoint(self, *, reason: str = "manual") -> dict[str, Any]:
        if not self.segments:
            return {"status": "empty", "checkpoint": self.checkpoint_count}
        started = time.perf_counter()
        old_communities = self.communities
        before_snapshot = self._state_snapshot(
            self.communities, self.community_summaries, self.active_items
        )
        new_node_ids = set(self.node_vectors) - self._shadow_previous_node_ids
        previous_node_ids = set(self._shadow_previous_node_ids)
        if self.checkpoint_count == 0:
            # The first checkpoint has no previous partition to seed from.
            # Establish it once with the full-graph detector; every later
            # checkpoint uses only the incremental local detector.
            global_started = time.perf_counter()
            detected = self._detect_communities()
            global_elapsed_ms = (time.perf_counter() - global_started) * 1000
            global_stats = {
                "n_communities": len(detected),
                "modularity": self._weighted_modularity(self.graph, detected),
            }
            community_detection = {
                "mode": "global_initial",
                "formal_detection_mode": "global",
                "new_node_ids": sorted(new_node_ids),
                "affected_community_ids": [],
                "affected_node_count": len(self.node_vectors),
                "total_node_count": len(self.node_vectors),
                "affected_node_ratio": 1.0 if self.node_vectors else 0.0,
                "affected_community_count": 0,
                "total_community_count": 0,
                "fallback_to_global": False,
                "global": global_stats,
                "local": None,
                "comparison": {
                    "community_count_delta": 0,
                    "changed_node_count": 0,
                    "changed_node_ratio": 0.0,
                    "global_elapsed_ms": float(global_elapsed_ms),
                    "local_elapsed_ms": 0.0,
                },
            }
            formal_detection_mode = "global"
        else:
            # No global reference pass here: local is the formal detector and
            # the previous formal partition is its only starting point.
            community_detection = self._shadow_local_report(
                None,
                new_node_ids,
                previous_node_ids,
            )
            detected = self._last_shadow_local_partition or []
            formal_detection_mode = "local"
        community_detection["formal_detection_mode"] = formal_detection_mode
        candidate_communities: dict[str, set[str]] = {}
        nodes_by_community: dict[str, set[str]] = {}

        def community_id_for(node_group: set[str]) -> str:
            # Keep the community identity stable while its current graph
            # representation is unchanged. Once a summary replaces raw
            # nodes, the summary node itself—not its old inputs—becomes the
            # identity used by subsequent rounds.
            for old_id, old_members in old_communities.items():
                if old_members == node_group:
                    return old_id
            return _stable_id("community", node_group)

        for node_group in detected:
            # Communities are defined over the current graph representation.
            # A summary node is one member, just like a raw segment node.
            members = set(node_group)
            community_id = community_id_for(members)
            candidate_communities[community_id] = members
            nodes_by_community[community_id] = set(node_group)

        changed: list[dict[str, Any]] = []
        new_communities: dict[str, set[str]] = {}
        new_summaries: dict[str, list[dict[str, Any]]] = {}
        new_no_memory_communities: set[str] = set()
        install_groups: list[dict[str, Any]] = []

        def node_ids_for_members(
            node_group: set[str], member_ids: set[str]
        ) -> set[str]:
            return set(node_group) & member_ids

        def add_community_group(
            community_id: str,
            members: set[str],
            node_group: set[str],
            summaries: list[dict[str, Any]],
            *,
            no_memory: bool = False,
        ) -> None:
            if not members:
                return
            new_communities[community_id] = set(members)
            new_summaries[community_id] = summaries
            if no_memory:
                new_no_memory_communities.add(community_id)
            install_groups.append({
                "community_id": community_id,
                "members": set(members),
                "node_ids": node_ids_for_members(node_group, members),
                "summaries": summaries,
            })

        for community_id, members in sorted(candidate_communities.items()):
            node_group = nodes_by_community[community_id]
            unchanged = (
                community_id in old_communities
                and old_communities[community_id] == members
                and community_id in self.community_summaries
            )
            # Reuse an unchanged summary. This preserves the previous detailed
            # representation; adding keywords must not trigger repeated lossy
            # compression of the same community.
            if unchanged:
                existing_summaries = self.community_summaries[community_id]
                if isinstance(existing_summaries, dict):
                    existing_summaries = [existing_summaries]
                add_community_group(
                    community_id,
                    members,
                    node_group,
                    existing_summaries,
                    no_memory=community_id in self.no_memory_communities,
                )
                change_type = "unchanged"
            else:
                change_type = "created_or_changed"
                split_community_ids: list[str] = []
                if len(members) >= self.summary_min_members:
                    refreshed = self._summarize(
                        community_id, members, node_group
                    )
                    no_memory = self._last_summary_no_memory
                    rejected_node_ids = self._last_rejected_node_ids & members
                    retained_members = members - rejected_node_ids
                    if rejected_node_ids:
                        # Keep rejected nodes active, but prevent them from
                        # reconnecting to this community. Do not include
                        # unrelated graph nodes in the barrier: rejected
                        # nodes must still be able to join another community.
                        self.pruned_node_ids.update(rejected_node_ids)
                        retained_barrier_members: set[str] = set()
                        for retained_node_id in members - rejected_node_ids:
                            retained_barrier_members.update(
                                self.node_barrier_members.get(
                                    retained_node_id, {retained_node_id}
                                )
                            )
                        self._add_separation_barriers([
                            rejected_node_ids,
                            retained_barrier_members,
                        ])
                    split_groups = self._last_inventory_groups
                    failed_refresh = any(
                        str(part.get("source", "")).startswith("extractive_fallback_after_")
                        for part in refreshed
                    ) or not refreshed

                    # A valid split is a partition of the candidate community:
                    # each returned group gets its own stable community ID.
                    # This is deliberately done before graph installation so
                    # communities, summaries, and retrieval all share the
                    # same identity model.
                    if split_groups is not None:
                        split_member_groups: list[set[str]] = []
                        for part in split_groups:
                            part_members = set(part.get("input_node_ids", [])) & members
                            if not part_members:
                                continue
                            split_member_groups.append(part_members)
                            split_id = _stable_id("community", part_members)
                            add_community_group(
                                split_id,
                                part_members,
                                node_group,
                                [part] if str(part.get("text", "")).strip() else [],
                            )
                            split_community_ids.append(split_id)
                        self._add_separation_barriers(split_member_groups)
                        change_type = (
                            "split_into_communities"
                            if len(split_community_ids) > 1
                            else "created_or_changed"
                        )
                        changed_row = {
                            "community_id": community_id,
                            "change_type": change_type,
                            "n_members": len(members),
                            "n_graph_nodes": len(node_group),
                            "mean_internal_edge_weight": self._mean_internal_weight(node_group),
                            "split_community_ids": split_community_ids,
                        }
                        changed.append(changed_row)
                        continue

                    previous_parts: list[dict[str, Any]] = []
                    if failed_refresh:
                        for node_id in node_group:
                            if not node_id.startswith("summary:"):
                                continue
                            for old_parts in self.community_summaries.values():
                                old_parts = old_parts if isinstance(old_parts, list) else [old_parts]
                                previous_parts.extend(
                                    part for part in old_parts
                                    if part.get("node_id") == node_id
                                )
                    if no_memory and not previous_parts:
                        add_community_group(
                            community_id, retained_members, node_group, [], no_memory=True
                        )
                        change_type = "no_user_memory"
                    elif previous_parts:
                        # Preserve previously independent split communities if
                        # a later refresh fails and the detector temporarily
                        # merges their graph nodes.
                        preserved_members: set[str] = set()
                        for previous_part in previous_parts:
                            previous_node_id = str(previous_part.get("node_id", ""))
                            if previous_node_id in members:
                                part_members = {previous_node_id}
                            else:
                                part_members = set(
                                    previous_part.get(
                                        "input_node_ids",
                                        previous_part.get("member_ids", []),
                                    )
                                ) & retained_members
                            if not part_members:
                                continue
                            preserved_id = _stable_id("community", part_members)
                            add_community_group(
                                preserved_id,
                                part_members,
                                node_group,
                                [previous_part],
                            )
                            preserved_members |= part_members
                        remaining = retained_members - preserved_members
                        if remaining:
                            remaining_id = _stable_id("community", remaining)
                            add_community_group(
                                remaining_id, remaining, node_group, []
                            )
                        change_type = "llm_failed_kept_previous"
                    elif refreshed:
                        # Successful LLM output must become the community's
                        # persisted summary. Without this assignment, calls
                        # can succeed while every community remains raw.
                        add_community_group(
                            community_id, retained_members, node_group, refreshed
                        )
                        change_type = "created_or_changed"
                    else:
                        add_community_group(community_id, retained_members, node_group, [])
                        change_type = (
                            "llm_unavailable_kept_raw"
                            if self.llm_summarize_fn is None
                            else "llm_failed_kept_raw"
                        ) if failed_refresh else "created_or_changed"
                else:
                    # Communities below the summary threshold still remain
                    # valid communities. Keep them as raw-member fallbacks;
                    # never silently drop them from the partition.
                    carried_summaries = self._summary_parts_for_nodes(node_group)
                    add_community_group(
                        community_id, members, node_group, carried_summaries
                    )
                    change_type = "below_summary_min_kept_raw"
            changed.append({
                "community_id": community_id,
                "change_type": change_type,
                "n_members": len(members),
                "n_graph_nodes": len(node_group),
                "mean_internal_edge_weight": self._mean_internal_weight(node_group),
            })

        self.community_summaries = new_summaries
        self.no_memory_communities = new_no_memory_communities
        pre_install_nodes = set(self.node_vectors)
        dynamic_replacements = self._install_next_graph(
            detected, new_summaries, install_groups
        )
        newly_installed_summaries = {
            node_id
            for node_id in set(self.node_vectors) - pre_install_nodes
            if node_id.startswith("summary:")
        }
        # Installation may replace several input nodes with one summary node.
        # Rebuild the formal partition from the nodes that actually survived
        # installation; never leave the replaced input IDs in communities.
        installed_communities: dict[str, set[str]] = {}
        for group in install_groups:
            community_id = str(group["community_id"])
            if community_id in self.no_memory_communities:
                continue
            installed = {
                node_id for node_id in group.get("node_ids", set())
                if node_id in self.node_vectors
            }
            parts = group.get("summaries", [])
            if isinstance(parts, dict):
                parts = [parts]
            installed.update(
                str(part["node_id"])
                for part in parts
                if part.get("node_id") in self.node_vectors
            )
            if installed:
                installed_communities[community_id] = installed
        self.communities = installed_communities
        # Newly installed summary nodes stay outside the previous-node set
        # for one round, so the next checkpoint seeds incremental detection
        # from them: independent memories produced from one community must
        # re-join the graph partition by their own keyword edges instead of
        # staying frozen under the pre-extraction community label.  The
        # pre-install snapshot keeps this one-round deferral exact: a node is
        # excluded exactly once, then consumed by the next checkpoint.
        self._shadow_previous_node_ids = (
            set(self.node_vectors) - newly_installed_summaries
        )
        self._rebuild_active_index()
        self.checkpoint_count += 1
        after_snapshot = self._state_snapshot(
            self.communities, self.community_summaries, self.active_items
        )
        event = {
            "event": "checkpoint",
            "checkpoint": self.checkpoint_count,
            "reason": reason,
            "n_segments": len(self.segments),
            "n_communities": len(self.communities),
            "n_active_items": len(self.active_items),
            "n_graph_nodes": len(self.node_vectors),
            "changed_communities": sum(
                row["change_type"] != "unchanged" for row in changed
            ),
            "summary_calls_total": self.summary_calls,
            "dynamic_replacements": dynamic_replacements,
            "formal_detection_mode": formal_detection_mode,
            "community_detection": community_detection,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
            "before": before_snapshot,
            "after": after_snapshot,
            "communities": changed,
        }
        self.trace.append(event)
        return event

    # Compatibility aliases for small scripts that used the old store shape.
    def consolidate(self, **_: Any) -> dict[str, Any]:
        return self.checkpoint(reason="consolidate")

    def should_consolidate(self) -> bool:
        return False

    @staticmethod
    def _structured_memory_from_part(
        part: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Read a structured record, including records from older states."""
        value = part.get("structured_memory")
        if not isinstance(value, dict):
            try:
                value = json.loads(str(part.get("text", "")))
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
        if not isinstance(value, dict):
            return None
        if not all(
            isinstance(value.get(field), expected)
            for field, expected in (
                ("title", str),
                ("summary", str),
                ("keywords", list),
                ("facts", list),
            )
        ):
            return None
        return value

    def _build_ccon_items(self) -> list[dict[str, Any]]:
        """Represent the complete current community set as C_con.

        There is exactly one candidate item per community.  The retrieval
        channels below rank this complete list of community items; no channel
        performs a second search inside an individual community.
        """
        active_by_community: dict[str, list[dict[str, Any]]] = {}
        for item in self.active_items.values():
            active_by_community.setdefault(str(item["community_id"]), []).append(item)

        ccon: list[dict[str, Any]] = []
        for community_id, members in sorted(self.communities.items()):
            if community_id in self.no_memory_communities:
                continue

            summary_parts = self.community_summaries.get(community_id, [])
            if isinstance(summary_parts, dict):
                summary_parts = [summary_parts]
            known_summary_nodes = {
                str(part.get("node_id", ""))
                for part in summary_parts
                if str(part.get("node_id", ""))
            }
            # Be defensive when loading an older state in which a summary
            # node survived a community move but its mapping did not.
            summary_parts = list(summary_parts) + self._summary_parts_for_nodes(
                set(members) - known_summary_nodes
            )
            summary_texts = [
                str(part.get("text", "")).strip()
                for part in summary_parts
                if str(part.get("text", "")).strip()
            ]
            structured_memories = [
                record
                for part in summary_parts
                for record in [self._structured_memory_from_part(part)]
                if record is not None
            ]
            structured_mode = bool(summary_parts) and (
                len(structured_memories) == len(summary_parts)
            )
            active_items = sorted(
                active_by_community.get(community_id, []),
                key=lambda item: str(item.get("id", "")),
            )
            member_ids = sorted(members)
            member_texts = [
                self.node_text[node_id]
                for node_id in member_ids[:200]
                if node_id in self.node_text
            ]

            # A summarized community is one C_con item whose answer text is
            # the joined summary. If no summary exists, the same C_con item
            # falls back to the community's active raw segments.
            detail_text = "\n\n".join(summary_texts)
            if not detail_text:
                detail_text = "\n\n".join(
                    str(item.get("text", "")).strip()
                    for item in active_items
                    if str(item.get("text", "")).strip()
                )
            if not detail_text:
                detail_text = "\n\n".join(member_texts)
            if not detail_text:
                continue

            keywords: list[str] = []
            for part in summary_parts:
                keywords.extend(str(value).strip() for value in part.get("keywords", []))
            for item in active_items:
                keywords.extend(str(value).strip() for value in item.get("keywords", []))
            for node_id, node_members in self.node_members.items():
                if node_members & members:
                    keywords.extend(self.node_keywords.get(node_id, []))
                    keywords.extend(self.node_display_keywords.get(node_id, []))
            keywords = list(dict.fromkeys(value for value in keywords if value))

            if structured_mode:
                structured_titles = " ".join(
                    str(record.get("title", ""))
                    for record in structured_memories
                )
                structured_keywords = " ".join(
                    str(keyword)
                    for record in structured_memories
                    for keyword in record.get("keywords", [])
                )
                structured_summaries = " ".join(
                    str(record.get("summary", ""))
                    for record in structured_memories
                )
                # Structured retrieval deliberately separates the semantic
                # field from the lexical/entity fields.
                semantic_text = structured_summaries
                lexical_document = " ".join([
                    structured_titles,
                    structured_keywords,
                    structured_summaries,
                ])
                entity_document = lexical_document
                retrieval_summary = structured_summaries
            else:
                # Keep the legacy text-based retrieval path unchanged for
                # communities that do not contain structured records.
                retrieval_summary = detail_text
                semantic_text = retrieval_summary
                lexical_document = " ".join([
                    retrieval_summary,
                    detail_text,
                    " ".join(keywords),
                    " ".join(member_texts),
                ])
                entity_document = " ".join([
                    retrieval_summary,
                    " ".join(member_texts),
                ])
            ccon.append({
                "id": f"community:{community_id}",
                "community_id": community_id,
                "text": detail_text,
                "retrieval_summary": retrieval_summary,
                "semantic_text": semantic_text,
                "lexical_document": lexical_document,
                "entity_values": _graph_extract_entities(entity_document),
                "keywords": keywords,
                "structured_memory": (
                    structured_memories[0]
                    if structured_mode and len(structured_memories) == 1
                    else None
                ),
                "structured_memories": (
                    structured_memories if structured_mode else []
                ),
                "source": "community_summary" if summary_texts else "community",
                "member_ids": member_ids,
                "n_members": len(member_ids),
                "event_dates": sorted({
                    self.segments[node_id].event_date for node_id in member_ids
                    if node_id in self.segments and self.segments[node_id].event_date
                }),
            })
        return ccon

    def _retrieve_con(self, query: str, k: int) -> list[dict[str, Any]]:
        """Rank all community C_con items with sem/lex/entity RRF fusion."""
        items = self._build_ccon_items()
        if not items or k <= 0:
            return []
        k = min(int(k), len(items))
        query_vector = self._encode([query])[0]
        item_vectors = self._encode([item["semantic_text"] for item in items])
        semantic_scores = {
            item["id"]: _dot(query_vector, vector)
            for item, vector in zip(items, item_vectors)
        }
        semantic_rank = [
            item["id"] for item in sorted(
                items,
                key=lambda item: (-semantic_scores[item["id"]], item["id"]),
            )[:k]
        ]

        bm25 = _GraphBM25([
            (item["id"], item["lexical_document"]) for item in items
        ])
        lexical_scores = bm25.scores(_graph_lex_tokens(query))
        lexical_rank = [
            item_id for item_id, _ in sorted(
                lexical_scores.items(), key=lambda pair: (-pair[1], pair[0])
            )[:k]
        ]

        query_entities = _graph_extract_entities(query)
        entity_scores = {
            item["id"]: _graph_entity_overlap(query_entities, item["entity_values"])
            for item in items
        }
        entity_rank = [
            item["id"] for item in sorted(
                items,
                key=lambda item: (-entity_scores[item["id"]], item["id"]),
            )
            if entity_scores[item["id"]] > 0.0
        ][:k]

        # Each channel contributes only its own top-k ranking. The returned
        # score is RRF; raw semantic/BM25/entity magnitudes are retained for
        # diagnostics but are not mixed directly.
        rrf_scores = _graph_rrf_fuse(
            [semantic_rank, lexical_rank, entity_rank], self.rrf_k
        )
        item_by_id = {item["id"]: item for item in items}
        query_terms = _graph_lex_tokens(query)
        results: list[dict[str, Any]] = []
        for item_id, score in rrf_scores.items():
            item = item_by_id[item_id]
            keyword_terms = set().union(*(
                _graph_lex_tokens(keyword) for keyword in item["keywords"]
            )) if item["keywords"] else set()
            result = {
                key: value for key, value in item.items()
                if key not in {
                    "retrieval_summary",
                    "semantic_text",
                    "lexical_document",
                    "entity_values",
                }
            }
            result.update({
                "score": float(score),
                "similarity": float(semantic_scores[item_id]),
                "final_score": float(score),
                "semantic_score": float(semantic_scores[item_id]),
                "lexical_score": float(lexical_scores.get(item_id, 0.0)),
                "entity_score": float(entity_scores[item_id]),
                "n_keyword_matches": len(query_terms & keyword_terms),
            })
            results.append(result)
        results.sort(key=lambda row: (-row["score"], row["id"]))
        return results[:k]

    def retrieve(self, query: str, k: int = 10, include_archived: bool = False) -> list[dict[str, Any]]:
        if not self.active_items and self.segments:
            self.checkpoint(reason="lazy_retrieval")
        if not self.active_items and not include_archived:
            return []
        query_vector = self._encode([query])[0]
        candidates = self._retrieve_con(query, k)
        if include_archived:
            active_member_ids = {
                sid for item in self.active_items.values() for sid in item["member_ids"]
                if item["source"] == "segment"
            }
            for sid, segment in self.segments.items():
                if sid in active_member_ids:
                    continue
                community_id = next(
                    (cid for cid, members in self.communities.items() if sid in members), ""
                )
                candidates.append({
                    "id": f"archived:{sid}",
                    "text": segment.text,
                    "source": "archived_segment",
                    "community_id": community_id,
                    "member_ids": [sid],
                    "n_members": 1,
                    "event_dates": [segment.event_date] if segment.event_date else [],
                    "vector": self.vectors[sid],
                })
        scored: list[dict[str, Any]] = []
        for item in candidates:
            if "vector" not in item:
                # Community items already carry the cross-channel RRF score.
                scored.append(item)
                continue
            score = _dot(query_vector, item["vector"])
            result = {key: value for key, value in item.items() if key != "vector"}
            result.update({"score": score, "similarity": score, "final_score": score})
            scored.append(result)
        scored.sort(key=lambda row: (-row["score"], row["id"]))
        return scored[: max(0, int(k))]

    def get_member_segments(self, community_id: str) -> list[dict[str, Any]]:
        members = self.communities.get(community_id, set())
        return [self.segments[node_id].to_dict() for node_id in sorted(members)
                if node_id in self.segments]

    def stats(self) -> dict[str, Any]:
        summarized = sum(
            1 for cid in self.communities
            if any(
                node_id.startswith("summary:")
                for node_id in self.communities[cid]
            )
        )
        return {
            "schema_version": self.schema_version,
            "n_segments": len(self.segments),
            "n_communities": len(self.communities),
            "n_active_items": len(self.active_items),
            "n_no_memory_communities": len(self.no_memory_communities),
            "n_pruned_nodes": len(self.pruned_node_ids),
            "n_summarized_communities": summarized,
            "n_edges": self.graph.number_of_edges(),
            "checkpoint_count": self.checkpoint_count,
            "summary_calls": self.summary_calls,
            "summary_failures": self.summary_failures,
            "llm_errors": self.llm_errors,
            "llm_trace_count": len(self.llm_io_trace),
            "trace_count": len(self.trace),
            "tau_edge": self.tau_edge,
            "resolution": self.resolution,
            "summary_min_members": self.summary_min_members,
            "community_prune_enabled": self.community_prune_enabled,
            "max_display_keywords": self.max_display_keywords,
            "edge_similarity": "symmetric_keyword_best_match",
            "keyword_extractor": "LLM_for_summaries_input_keywords_for_raw_with_fallback",
            "keyword_ngram_range": list(self.keyword_ngram_range),
            "keyword_stop_words": self.keyword_stop_words,
            "keyword_top_n": self.keyword_top_n,
            "keyword_diversity": self.keyword_diversity,
            "keyword_extraction_fallback": self.keyword_extraction_fallback,
            "retrieval_method": "community_ccon_structured_fields_rrf_legacy_fallback",
            "retrieval_rrf_k": self.rrf_k,
            "retrieval_semantic": "structured_summary_only; legacy_text_fallback",
            "retrieval_lexical": "structured_title_keywords_summary; legacy_text_fallback",
            "retrieval_entity": "structured_title_keywords_summary; legacy_text_fallback",
            "retrieval_entity_extractor": "spacy_en_core_web_trf_optional",
            "community_backend": self.community_backend(),
        }

    def export_communities(self, path: str | Path) -> None:
        """Write one human-readable file per current community.

        The export deliberately contains both the current summary and every
        original member segment, so it can be used to audit over-merged
        communities without decoding state.json manually.
        """
        export_dir = Path(path) / "communities"
        export_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = export_dir / "manifest.json"

        # Remove only files listed by our previous manifest; do not touch any
        # unrelated user files in the output directory.
        if manifest_path.exists():
            try:
                previous = json.loads(manifest_path.read_text(encoding="utf-8"))
                for name in previous.get("generated_files", []):
                    target = export_dir / str(name)
                    if target.parent == export_dir and target.exists():
                        target.unlink()
            except (OSError, ValueError, TypeError):
                pass

        ordered = sorted(
            self.communities.items(),
            key=lambda item: (-len(item[1]), item[0]),
        )
        segments_by_id = self.segments
        manifest_rows: list[dict[str, Any]] = []
        generated_files: list[str] = []

        for index, (community_id, members) in enumerate(ordered, 1):
            summaries = self.community_summaries.get(community_id, [])
            if isinstance(summaries, dict):
                summaries = [summaries]
            known_summary_nodes = {
                str(part.get("node_id", ""))
                for part in summaries
                if str(part.get("node_id", ""))
            }
            # A summary node is authoritative active content.  Recover its
            # serialized summary when an older checkpoint lost only the
            # community-to-summary index during a community ID change.
            summaries = list(summaries) + self._summary_parts_for_nodes(
                set(members) - known_summary_nodes
            )
            suffix = community_id.rsplit(":", 1)[-1]
            filename = f"community_{index:03d}_{len(members):03d}_{suffix}.txt"
            target = export_dir / filename
            generated_files.append(filename)

            lines = [
                f"community_id: {community_id}",
                f"n_members: {len(members)}",
                f"summary_parts: {len(summaries)}",
                "",
                "=== SUMMARIES ===",
            ]
            for part_index, summary in enumerate(summaries):
                lines.extend([
                    f"\n--- summary {part_index} [{summary.get('source', '')}] ---",
                    str(summary.get("text", "")),
                    f"input_node_ids: {', '.join(sorted(summary.get('input_node_ids', summary.get('member_ids', []))))}",
                    f"keywords: {', '.join(summary.get('keywords', []))}",
                ])

            lines.append("\n=== RAW SEGMENTS ===")
            member_rows = sorted(
                (segments_by_id[sid] for sid in members if sid in segments_by_id),
                key=lambda segment: (segment.group_id, segment.segment_index, segment.id),
            )
            for segment in member_rows:
                lines.extend([
                    f"\n--- {segment.id} | {segment.group_id} "
                    f"seg{segment.segment_index:03d} | date={segment.event_date or ''} ---",
                    segment.text,
                ])
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
            manifest_rows.append({
                "community_id": community_id,
                "n_members": len(members),
                "file": filename,
                "summary_sources": [part.get("source", "") for part in summaries],
                "member_ids": sorted(members),
            })

        manifest_path.write_text(
            json.dumps({
                "schema_version": "graph_weekly_community_export_v1",
                "n_communities": len(ordered),
                "generated_files": generated_files,
                "communities": manifest_rows,
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.schema_version,
            "config": {
                "encoder_model": self.encoder_model,
                "encoder_device": self.encoder_device,
                "tau_edge": self.tau_edge,
                "resolution": self.resolution,
                "summary_min_members": self.summary_min_members,
                "community_prune_enabled": self.community_prune_enabled,
                "two_stage_classify_prompt": self.two_stage_classify_prompt,
                "two_stage_fuse_prompt": self.two_stage_fuse_prompt,
                "max_display_keywords": self.max_display_keywords,
                "retrieval": {
                    "method": "community_ccon_structured_fields_rrf_legacy_fallback",
                    "rrf_k": self.rrf_k,
                    "structured_fields": ["title", "keywords", "summary"],
                    "legacy_bm25_member_limit": 200,
                },
                "keyword_graph": {
                    "extractor": "LLM_for_summaries_input_keywords_for_raw_with_fallback",
                    "ngram_range": list(self.keyword_ngram_range),
                    "stop_words": self.keyword_stop_words,
                    "top_n": self.keyword_top_n,
                    "diversity": self.keyword_diversity,
                    "pair_score": "symmetric mean of per-keyword best matches",
                },
                "seed": self.seed,
                "local_detection_community_hops": self.local_detection_community_hops,
            },
            "segments": [segment.to_dict() for segment in self.segments.values()],
            "vectors": {sid: _vector_to_list(vector) for sid, vector in self.vectors.items()},
            "graph_nodes": {
                node_id: {
                    "vector": _vector_to_list(vector),
                    "keywords": list(self.node_keywords.get(node_id, [])),
                    "display_keywords": list(self.node_display_keywords.get(node_id, [])),
                    "keyword_vectors": [
                        _vector_to_list(row)
                        for row in self.node_keyword_vectors.get(node_id, [])
                    ],
                    "member_ids": sorted(self.node_members.get(node_id, [])),
                    "barrier_members": sorted(
                        self.node_barrier_members.get(node_id, [node_id])
                    ),
                    "text": self.node_text.get(node_id, ""),
                }
                for node_id, vector in self.node_vectors.items()
            },
            "communities": {cid: sorted(members) for cid, members in self.communities.items()},
            "community_summaries": self.community_summaries,
            "no_memory_communities": sorted(self.no_memory_communities),
            "pruned_node_ids": sorted(self.pruned_node_ids),
            "separation_barriers": [
                {
                    "left_member_ids": sorted(left),
                    "right_member_ids": sorted(right),
                }
                for left, right in self.separation_barriers
            ],
            "last_event_date": self.last_event_date,
            "checkpoint_count": self.checkpoint_count,
            "summary_calls": self.summary_calls,
            "summary_failures": self.summary_failures,
            "llm_errors": self.llm_errors,
            "trace_count": len(self.trace),
        }
        (target / "state.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (target / "trace.jsonl").write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in self.trace),
            encoding="utf-8",
        )
        (target / "llm_trace.jsonl").write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in self.llm_io_trace),
            encoding="utf-8",
        )
        self.export_communities(target)

    def load_into(self, path: str | Path) -> None:
        state_path = Path(path) / "state.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != self.schema_version:
            raise ValueError(f"unsupported graph memory schema: {payload.get('schema_version')}")
        config = payload.get("config", {})
        if "local_detection_community_hops" in config:
            self.local_detection_community_hops = max(
                0, int(config["local_detection_community_hops"])
            )
        if "community_prune_enabled" in config:
            self.community_prune_enabled = bool(config["community_prune_enabled"])
        self.segments = {
            row["id"]: Segment(**row) for row in payload.get("segments", [])
        }
        self.vectors = {
            sid: _unit(vector) for sid, vector in payload.get("vectors", {}).items()
        }
        graph_nodes = payload.get("graph_nodes", {})
        if graph_nodes:
            self.node_vectors = {
                node_id: _unit(row.get("vector", []))
                for node_id, row in graph_nodes.items()
            }
            self.node_keywords = {
                node_id: [str(value) for value in row.get("keywords", [])]
                for node_id, row in graph_nodes.items()
            }
            self.node_display_keywords = {
                node_id: [str(value) for value in row.get("display_keywords", [])]
                for node_id, row in graph_nodes.items()
            }
            self.node_keyword_vectors = {
                node_id: (
                    np.asarray(row.get("keyword_vectors", []), dtype=np.float32)
                    if np is not None
                    else row.get("keyword_vectors", [])
                )
                for node_id, row in graph_nodes.items()
                if row.get("keyword_vectors")
            }
            self.node_members = {
                # A graph node has one identity. Older states may contain
                # lineage IDs here; ignore them when loading so they cannot
                # affect graph similarity, community size, or prompts.
                node_id: {node_id}
                for node_id in graph_nodes
            }
            self.node_barrier_members = {
                node_id: {
                    str(value) for value in row.get("barrier_members", [node_id])
                } or {node_id}
                for node_id, row in graph_nodes.items()
            }
            self.node_text = {
                node_id: str(row.get("text", ""))
                for node_id, row in graph_nodes.items()
            }
            # States written before keyword vectors were persisted can still
            # be loaded, but their active graph features must be upgraded.
            for node_id, row in graph_nodes.items():
                if node_id in self.node_keyword_vectors:
                    continue
                node_keywords, keyword_vectors = self._keyword_features(
                    self.node_text[node_id], self.node_keywords.get(node_id, [])
                )
                self.node_keywords[node_id] = node_keywords
                self.node_keyword_vectors[node_id] = keyword_vectors
        else:
            # Compatibility with v1 states: reconstruct summary nodes from the
            # persisted community summaries, otherwise retain raw segment nodes.
            self.node_vectors = dict(self.vectors)
            self.node_keywords = {}
            self.node_display_keywords = {}
            self.node_keyword_vectors = {}
            self.node_members = {sid: {sid} for sid in self.segments}
            self.node_barrier_members = {sid: {sid} for sid in self.segments}
            self.node_text = {sid: segment.text for sid, segment in self.segments.items()}
            for sid, text in self.node_text.items():
                node_keywords, keyword_vectors = self._keyword_features(text)
                self.node_keywords[sid] = node_keywords
                self.node_keyword_vectors[sid] = keyword_vectors
        self.communities = {
            cid: set(members) for cid, members in payload.get("communities", {}).items()
        }
        self.no_memory_communities = {
            str(cid) for cid in payload.get("no_memory_communities", [])
        }
        self.pruned_node_ids = {
            str(node_id) for node_id in payload.get(
                "pruned_node_ids", payload.get("excluded_node_ids", [])
            )
        }
        self.separation_barriers = []
        for row in payload.get("separation_barriers", []):
            if not isinstance(row, dict):
                continue
            left = frozenset(str(value) for value in row.get("left_member_ids", []))
            right = frozenset(str(value) for value in row.get("right_member_ids", []))
            if left and right:
                self._add_separation_barriers([left, right])
        self.community_summaries = {}
        for cid, summaries in payload.get("community_summaries", {}).items():
            # Older states stored one summary object per community.
            self.community_summaries[cid] = summaries if isinstance(summaries, list) else [summaries]
        self._migrate_legacy_split_communities()
        for parts in self.community_summaries.values():
            parts = parts if isinstance(parts, list) else [parts]
            for part in parts:
                node_id = str(part.get("node_id", ""))
                if node_id and node_id not in self.node_display_keywords:
                    self.node_display_keywords[node_id] = [
                        str(value) for value in part.get("keywords", [])
                    ]
        # Summary graph features are the LLM-selected keywords. Upgrade older
        # persisted states whose graph_keywords still came from KeyBERT.
        for node_id, keywords in self.node_display_keywords.items():
            if not node_id.startswith("summary:") or not keywords:
                continue
            self.node_keywords[node_id], self.node_keyword_vectors[node_id] = (
                self._keyword_features(self.node_text.get(node_id, ""), keywords)
            )
        if not graph_nodes and self.community_summaries:
            covered: set[str] = set()
            for community_id, parts in self.community_summaries.items():
                if isinstance(parts, dict):
                    parts = [parts]
                for part_index, part in enumerate(parts):
                    members = set(
                        part.get("input_node_ids", part.get("member_ids", []))
                    ) & set(self.segments)
                    if len(members) < self.summary_min_members:
                        continue
                    node_id = str(part.get("node_id") or f"summary:{community_id}:part{part_index:02d}")
                    representation = str(part.get("text", ""))
                    keywords = [str(value) for value in part.get("keywords", [])]
                    self.node_vectors[node_id] = self._encode([representation])[0]
                    node_keywords, keyword_vectors = self._keyword_features(
                        representation, keywords
                    )
                    self.node_keywords[node_id] = node_keywords
                    self.node_display_keywords[node_id] = list(keywords)
                    self.node_keyword_vectors[node_id] = keyword_vectors
                    self.node_members[node_id] = {node_id}
                    self.node_barrier_members[node_id] = set(members) or {node_id}
                    self.node_text[node_id] = representation
                    part["node_id"] = node_id
                    covered |= members
            for sid in covered:
                self.node_vectors.pop(sid, None)
                self.node_keywords.pop(sid, None)
                self.node_keyword_vectors.pop(sid, None)
                self.node_members.pop(sid, None)
                self.node_text.pop(sid, None)
        self._rebuild_graph()
        # The loaded graph is the post-checkpoint active representation.  New
        # raw nodes arriving after load are therefore the next shadow batch.
        # The local detector separately handles nodes that have no prior
        # formal community assignment.
        self._shadow_previous_node_ids = set(self.node_vectors)
        self.last_event_date = payload.get("last_event_date")
        self.checkpoint_count = int(payload.get("checkpoint_count", 0))
        self.summary_calls = int(payload.get("summary_calls", 0))
        self.summary_failures = int(payload.get("summary_failures", 0))
        self.llm_errors = list(payload.get("llm_errors", []))
        trace_path = Path(path) / "trace.jsonl"
        if trace_path.exists():
            self.trace = read_jsonl(trace_path)
        else:
            # Compatibility with an intermediate state format, if present.
            self.trace = payload.get("trace", [])
        llm_trace_path = Path(path) / "llm_trace.jsonl"
        if llm_trace_path.exists():
            self.llm_io_trace = read_jsonl(llm_trace_path)
        else:
            self.llm_io_trace = []
        self._rebuild_active_index()


def build_llm_fn(
    api_key: Optional[str] = None,
    model: str = "gpt-4o-mini",
    base_url: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> Optional[Callable[[list[str]], str]]:
    """Build the same ``List[str] -> str`` callback shape as StreamEM."""
    _load_local_env()
    if max_tokens is None:
        try:
            max_tokens = int(os.getenv("GRAPH_WEEKLY_LLM_MAX_TOKENS", "3000"))
        except ValueError:
            max_tokens = 3000
    api_key = (
        api_key
        or os.getenv("GRAPH_WEEKLY_OPENAI_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
    )
    if not api_key:
        return None
    try:
        import openai
    except ImportError:
        return None
    client = openai.OpenAI(
        api_key=api_key,
        base_url=(
            base_url
            or os.getenv("GRAPH_WEEKLY_OPENAI_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://openrouter.ai/api/v1"
        ),
        max_retries=0,
    )

    def llm_fn(prompts: list[str]) -> str:
        if len(prompts) == 2:
            messages = [
                {"role": "system", "content": prompts[0]},
                {"role": "user", "content": prompts[1]},
            ]
        else:
            messages = [
                {"role": "system", "content": (
                    "Follow the requested output format exactly. Return only the "
                    "requested factual summary or JSON object, with no commentary."
                )},
                {"role": "user", "content": "\n---\n".join(prompts)},
            ]
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=max_tokens,
        )
        return str(response.choices[0].message.content or "").strip()

    return llm_fn


class WeeklyGraphMemorySystem(BaseMemorySystem):
    """Official evaluation adapter for the graph-weekly experiment."""

    def __init__(self, user_id: str, **kwargs: Any) -> None:
        super().__init__(user_id)
        self.user_id = user_id
        self.logger = logging.getLogger(f"GraphWeekly.{user_id}")
        self._storage_path = Path(kwargs.get("storage_path") or HERE / "data" / user_id)
        self._fixed_segments_path = Path(
            kwargs.get("fixed_segments_path") or DEFAULT_FIXED_SEGMENTS
        )
        self._encoder_model = kwargs.get("encoder_model", os.getenv("ENCODER_MODEL_NAME", DEFAULT_ENCODER))
        self._encoder_device = kwargs.get("encoder_device", "cpu")
        self._llm_model = kwargs.get("llm_model", "gpt-4o-mini")
        self._api_key = kwargs.get("api_key")
        self._base_url = kwargs.get("base_url")
        self._tau_edge = float(kwargs.get("tau_edge", 0.5))
        self._resolution = float(kwargs.get("resolution", 1.0))
        self._summary_min_members = int(kwargs.get("summary_min_members", 2))
        self._community_prune_enabled = bool(
            kwargs.get("community_prune_enabled", True)
        )
        self._auto_checkpoint = bool(kwargs.get("auto_checkpoint", True))
        self._llm_enabled = bool(kwargs.get("llm_enabled", True))
        self._llm_fn: Optional[Callable[[list[str]], str]] = None
        self._store: Optional[GraphWeeklyMemory] = None
        self._fixed_by_session: dict[str, list[dict[str, Any]]] = {}
        self._session_dates: dict[str, str] = {}
        self._session_count = 0
        self._total_messages = 0

    def get_system_name(self) -> str:
        return "streamem_graph"

    def get_required_env_vars(self) -> list[str]:
        return []

    def initialize_client(self) -> bool:
        if self._store is not None:
            return True
        if self._llm_enabled:
            self._llm_fn = build_llm_fn(
                api_key=self._api_key, model=self._llm_model, base_url=self._base_url
            )
        self._store = GraphWeeklyMemory(
            encoder_model=self._encoder_model,
            encoder_device=self._encoder_device,
            tau_edge=self._tau_edge,
            resolution=self._resolution,
            summary_min_members=self._summary_min_members,
            llm_summarize_fn=self._llm_fn,
            community_prune_enabled=self._community_prune_enabled,
            two_stage_classify_prompt="inventory",
            two_stage_fuse_prompt="structured",
        )
        self._load_fixed_segments()
        state_path = self._storage_path / "state.json"
        if state_path.exists():
            self._store.load_into(self._storage_path)
        else:
            self._store.initialize()
        return True

    def _load_fixed_segments(self) -> None:
        if not self._fixed_segments_path.exists():
            self.logger.warning("fixed segment file not found: %s", self._fixed_segments_path)
            return
        for row in read_jsonl(self._fixed_segments_path):
            group_id = str(row.get("group_id") or "")
            self._fixed_by_session.setdefault(_session_key(group_id), []).append(row)
        for rows in self._fixed_by_session.values():
            rows.sort(key=lambda row: int(row.get("segment_index", 0)))
        # Dates are optional in the artifact.  Session JSON is the authoritative
        # source and is only used as an event-time anchor, never as a cluster label.
        weekly_root = REPO_ROOT / "data" / "weekly"
        if weekly_root.exists():
            for session_path in weekly_root.glob("*/conversations/session_*.json"):
                try:
                    row = json.loads(session_path.read_text(encoding="utf-8"))
                    if row.get("date"):
                        self._session_dates[session_path.stem] = _json_date(row["date"]) or ""
                except (OSError, ValueError, TypeError):
                    continue

    @staticmethod
    def _extract_messages(conversation_data: dict[str, Any]) -> list[dict[str, str]]:
        messages = []
        for turn in conversation_data.get("conversation", []) or []:
            content = str(turn.get("message") or "").strip()
            if content:
                messages.append({"role": str(turn.get("speaker") or ""), "content": content})
        return messages

    def add_conversation_to_memory(self, conversation_data: dict[str, Any]) -> dict[str, Any]:
        if self._store is None:
            raise RuntimeError("initialize_client() must be called first")
        session_id = _session_key(conversation_data.get("session_id", ""))
        event_date = _json_date(conversation_data.get("date")) or self._session_dates.get(session_id)
        rows = self._fixed_by_session.get(session_id)
        if rows:
            result = self._store.add_segments(
                rows, event_date=event_date, checkpoint_on_date_change=self._auto_checkpoint
            )
        else:
            # Compatibility fallback for a session outside the frozen weekly
            # artifact: treat the supplied conversation as one already-cut unit.
            # No sentence splitting or filtering is performed.
            messages = self._extract_messages(conversation_data)
            text = "\n\n".join(f"[{m['role']}] {m['content']}" for m in messages)
            result = self._store.add_segments(
                [{
                    "segment_id": f"{session_id}:seg000",
                    "group_id": session_id,
                    "segment_index": 0,
                    "text": text,
                    "event_date": event_date,
                }],
                event_date=event_date,
                checkpoint_on_date_change=self._auto_checkpoint,
            )
        self._session_count += 1
        self._total_messages += len(conversation_data.get("conversation", []) or [])
        self._store.save(self._storage_path)
        return {
            "entry_ids": result["segment_ids"],
            "session_id": conversation_data.get("session_id", ""),
            "success": True,
            "message_count": self._total_messages,
            "segments_added": result["added"],
        }

    def process_conversation_file(self, file_path: str) -> dict[str, Any]:
        with open(file_path, encoding="utf-8") as handle:
            return self.add_conversation_to_memory(json.load(handle))

    def finalize(self) -> dict[str, Any]:
        if self._store is None:
            return {"error": "not initialized"}
        result = self._store.checkpoint(reason="finalize")
        self._store.save(self._storage_path)
        return result

    def force_consolidate(self) -> dict[str, Any]:
        return self.finalize()

    def search_memories(
        self,
        query: str,
        limit: int = 5,
        session_date: Optional[str] = None,
        date_range: Optional[tuple] = None,
    ) -> list[dict[str, Any]]:
        del session_date, date_range  # Kept for the official API contract.
        if self._store is None:
            return []
        include_archived = os.getenv("GRAPH_WEEKLY_INCLUDE_RAW") == "1"
        results = self._store.retrieve(query, k=limit, include_archived=include_archived)
        formatted = []
        for row in results:
            source = row["source"]
            is_community = source in {"community", "community_summary"}
            label = "[community]" if is_community else "[segment]"
            structured_memory = row.get("structured_memory")
            structured_memories = row.get("structured_memories") or []
            if structured_memory is not None:
                # Structured communities return the complete record instead
                # of flattening it into a display string.
                memory_value: Any = copy.deepcopy(structured_memory)
                content_value: Any = copy.deepcopy(structured_memory)
            elif structured_memories:
                memory_value = copy.deepcopy(structured_memories)
                content_value = copy.deepcopy(structured_memories)
            else:
                text = f"{label} {row['text']}"
                memory_value = text
                content_value = text
            formatted_row = {
                "id": row["id"],
                "memory": memory_value,
                "content": content_value,
                "score": round(float(row["score"]), 4),
                "source": source,
                "layer": "community" if is_community else "segment",
                "similarity": row["similarity"],
                "n_members": row["n_members"],
                "member_segment_ids": row["member_ids"],
                "community_id": row["community_id"],
                "session_date": (row.get("event_dates") or [""])[0],
            }
            if structured_memory is not None:
                formatted_row["structured_memory"] = copy.deepcopy(structured_memory)
            elif structured_memories:
                formatted_row["structured_memories"] = copy.deepcopy(
                    structured_memories
                )
            formatted.append(formatted_row)
        return formatted

    def get_stats(self) -> dict[str, Any]:
        if self._store is None:
            return {"status": "not initialized"}
        stats = self._store.stats()
        stats.update({"sessions_processed": self._session_count, "total_messages": self._total_messages})
        return stats

    def get_config_info(self) -> dict[str, Any]:
        return {
            "system_name": self.get_system_name(),
            "user_id": self.user_id,
            "encoder_model": self._encoder_model,
            "fixed_segments_path": str(self._fixed_segments_path),
            "tau_edge": self._tau_edge,
            "resolution": self._resolution,
            "summary_min_members": self._summary_min_members,
            "community_prune_enabled": self._community_prune_enabled,
            "two_stage_classify_prompt": "inventory",
            "two_stage_fuse_prompt": "structured",
            "retrieval_method": "community_ccon_structured_fields_rrf_legacy_fallback",
            "retrieval_rrf_k": 60,
            "llm_model": self._llm_model if self._llm_fn else "none (raw nodes retained)",
            "required_env_vars": [],
            "env_vars_set": {},
        }


def ingest_fixed_weekly(
    fixed_segments_path: Path,
    out: Path,
    *,
    encoder_model: str = DEFAULT_ENCODER,
    tau_edge: float = 0.5,
    resolution: float = 1.0,
    summary_min_members: int = 2,
    llm_enabled: bool = False,
    community_prune_enabled: bool = True,
    two_stage_classify_prompt: str = "inventory",
    two_stage_fuse_prompt: str = "structured",
    llm_model: str = "gpt-4o-mini",
    max_segments: int = 0,
    progress: bool = True,
    local_detection_community_hops: int = 0,
) -> dict[str, Any]:
    """Run the same stream directly from the frozen weekly segment JSONL."""
    rows = read_jsonl(fixed_segments_path)
    if max_segments:
        rows = rows[:max_segments]
    rows.sort(key=lambda row: (
        int(re.search(r"\d+", str(row.get("group_id", "0"))).group())
        if re.search(r"\d+", str(row.get("group_id", "0"))) else 0,
        int(row.get("segment_index", 0)),
    ))
    llm_fn = build_llm_fn(model=llm_model) if llm_enabled else None
    memory = GraphWeeklyMemory(
        encoder_model=encoder_model,
        tau_edge=tau_edge,
        resolution=resolution,
        summary_min_members=summary_min_members,
        llm_summarize_fn=llm_fn,
        community_prune_enabled=community_prune_enabled,
        two_stage_classify_prompt=two_stage_classify_prompt,
        two_stage_fuse_prompt=two_stage_fuse_prompt,
        local_detection_community_hops=local_detection_community_hops,
    )
    session_dates: dict[str, str] = {}
    weekly_root = REPO_ROOT / "data" / "weekly"
    if weekly_root.exists():
        for session_path in weekly_root.glob("*/conversations/session_*.json"):
            try:
                session = json.loads(session_path.read_text(encoding="utf-8"))
                if session.get("date"):
                    session_dates[session_path.stem] = _json_date(session["date"]) or ""
            except (OSError, ValueError, TypeError):
                continue
    total_rows = len(rows)
    for index, row in enumerate(rows, 1):
        group_id = str(row.get("group_id") or "")
        memory.add_segments(
            [row],
            event_date=_json_date(row.get("event_date")) or session_dates.get(_session_key(group_id)),
            checkpoint_on_date_change=True,
        )
        if progress:
            width = 32
            filled = int(width * index / max(total_rows, 1))
            bar = "=" * filled + ">" + " " * max(width - filled - 1, 0)
            print(
                f"\r[{bar}] {index}/{total_rows} "
                f"checkpoints={memory.checkpoint_count} "
                f"communities={len(memory.communities)} "
                f"active={len(memory.active_items)} "
                f"llm={memory.summary_calls}",
                end="",
                flush=True,
            )
    if progress:
        print()
    checkpoint = memory.checkpoint(reason="end_of_fixed_weekly_stream")
    if progress:
        print(
            f"checkpoint complete: communities={len(memory.communities)} "
            f"active={len(memory.active_items)} llm={memory.summary_calls}",
            flush=True,
        )
    memory.save(out)
    result = {
        "schema_version": "graph_weekly_e2e_v1",
        "input": str(fixed_segments_path),
        "output": str(out),
        "runtime": {"python": platform.python_version(), "platform": platform.platform()},
        "checkpoint": checkpoint,
        "stats": memory.stats(),
    }
    (out / "run_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixed-segments", type=Path, default=DEFAULT_FIXED_SEGMENTS)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--encoder-model", default=DEFAULT_ENCODER)
    parser.add_argument("--tau-edge", type=float, default=0.5)
    parser.add_argument("--resolution", type=float, default=1.0)
    parser.add_argument("--summary-min-members", type=int, default=2)
    parser.add_argument(
        "--community-prune",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="enable community pruning before structured memory extraction (default: enabled)",
    )
    parser.add_argument("--llm", action="store_true", help="enable the two-stage structured memory builder")
    parser.add_argument(
        "--llm-two-stage", action="store_true",
        help="select the two-stage builder (the only supported builder)",
    )
    parser.add_argument(
        "--two-stage-classify-prompt", choices=("inventory",), default="inventory",
        help="classify accumulated blocks by subject and memory eligibility",
    )
    parser.add_argument(
        "--two-stage-fuse-prompt",
        choices=("structured",),
        default="structured",
        help="reconcile admitted groups into structured memory records",
    )
    parser.add_argument("--llm-model", default="gpt-4o-mini")
    parser.add_argument("--max-segments", type=int, default=0, help="smoke test limit; 0 means all")
    parser.add_argument(
        "--local-community-hops", type=int, default=0,
        help="community-adjacency expansion hops; 0 means unlimited, 2 limits to two hops",
    )
    parser.add_argument("--no-progress", action="store_true", help="disable the live ingest progress bar")
    args = parser.parse_args()
    if not args.llm or not args.llm_two_stage:
        parser.error("the runner requires --llm --llm-two-stage")
    result = ingest_fixed_weekly(
        args.fixed_segments,
        args.out,
        encoder_model=args.encoder_model,
        tau_edge=args.tau_edge,
        resolution=args.resolution,
        summary_min_members=args.summary_min_members,
        llm_enabled=args.llm,
        community_prune_enabled=args.community_prune,
        two_stage_classify_prompt=args.two_stage_classify_prompt,
        two_stage_fuse_prompt=args.two_stage_fuse_prompt,
        llm_model=args.llm_model,
        max_segments=args.max_segments,
        progress=not args.no_progress,
        local_detection_community_hops=args.local_community_hops,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
