from __future__ import annotations

import logging
import math
import re
from collections import Counter
from typing import Any

_STOP_WORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
        "has", "have", "he", "her", "hers", "him", "his", "i", "if", "in",
        "into", "is", "it", "its", "me", "my", "of", "on", "or", "our",
        "she", "that", "the", "their", "them", "there", "they", "this", "to",
        "was", "we", "were", "what", "when", "where", "which", "who", "will",
        "with", "you", "your", "user", "assistant", "just",
    }
)

_TITLE_ENTITY_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9'’.-]*)(?:\s+(?:[A-Z][A-Za-z0-9'’.-]*|"
    r"the|on|of|and|for|in|to|at|from)){1,8}\b"
)
_NUMBER_ENTITY_RE = re.compile(r"(?:[$€£]\s*)?\d[\d,]*(?:\.\d+)?%?")
_QUOTED_ENTITY_RE = re.compile(r"[\"']([^\"']{2,})[\"']")
_ENTITY_MODEL: Any = None
_ENTITY_LOAD_ATTEMPTED = False


def lex_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for token in re.findall(r"[a-z0-9]{2,}", str(text).casefold()):
        if token in _STOP_WORDS:
            continue
        # The evaluation questions frequently use plural forms (tasks,
        # expenses, papers), while structured memory types/content use the
        # singular form. Keep this deliberately conservative and dependency-free.
        if len(token) > 4 and token.endswith("ies"):
            token = token[:-3] + "y"
        elif len(token) > 4 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
            token = token[:-1]
        tokens.append(token)
    return tokens


class BM25:
    """Small dependency-free BM25 implementation for memory records."""

    def __init__(self, documents: list[tuple[str, str]]) -> None:
        self.ids = [doc_id for doc_id, _ in documents]
        self.tokens = [lex_tokens(text) for _, text in documents]
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


def _fallback_entities(text: str) -> set[str]:
    entities: set[str] = set()
    for value in _NUMBER_ENTITY_RE.findall(str(text)):
        normalized = re.sub(r"\s+", "", value).casefold()
        entities.add(normalized)
        entities.add(normalized.lstrip("$€£"))
    for value in _QUOTED_ENTITY_RE.findall(str(text)):
        entities.add(re.sub(r"\s+", " ", value).strip().casefold())
    for value in _TITLE_ENTITY_RE.findall(str(text)):
        normalized = re.sub(r"\s+", " ", value).strip().casefold()
        if normalized not in {"the user", "the assistant"}:
            entities.add(normalized)
    for value in re.findall(r"\b[A-Z]{2,}[A-Z0-9-]*\b", str(text)):
        entities.add(value.casefold())
    for value in re.findall(r"[\u3400-\u9fff]{2,}", str(text)):
        entities.add(value)
    return {value for value in entities if value}


def extract_entities(text: str) -> set[str]:
    """Extract stable entities, using spaCy when available and regex fallback otherwise."""

    global _ENTITY_MODEL, _ENTITY_LOAD_ATTEMPTED
    if not _ENTITY_LOAD_ATTEMPTED:
        _ENTITY_LOAD_ATTEMPTED = True
        try:
            import spacy

            for model_name in ("en_core_web_trf", "en_core_web_sm"):
                try:
                    _ENTITY_MODEL = spacy.load(model_name)
                    break
                except Exception:
                    continue
        except Exception as exc:
            logging.getLogger(__name__).debug("spaCy entity channel unavailable: %s", exc)
            _ENTITY_MODEL = False
    values = _fallback_entities(text)
    if _ENTITY_MODEL and str(text).strip():
        values.update(
            {
            f"{entity.text.strip().casefold()}|{entity.label_}"
            for entity in _ENTITY_MODEL(str(text)).ents
            if entity.text.strip()
            }
        )
    return values


def entity_overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def rrf_fuse(rankings: list[list[str]], k_rrf: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k_rrf + rank + 1)
    return scores
