"""Keyword/entity extraction and feature preparation for graph experiments."""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any


_LOGGER = logging.getLogger(__name__)
_ENTITY_MODEL: Any = None
_ENTITY_MODEL_ATTEMPTED = False
_KEYWORD_MODEL: Any = None
_KEYWORD_MODEL_ATTEMPTED = False

# Keep entities that identify a stable subject or named concept. Temporal,
# numeric, and measurement entities mostly describe the utterance context and
# create spurious links between otherwise unrelated segments.
DEFAULT_ENTITY_LABELS = frozenset({
    "PERSON",
    "NORP",
    "FAC",
    "ORG",
    "GPE",
    "LOC",
    "PRODUCT",
    "EVENT",
    "WORK_OF_ART",
    "LAW",
    "LANGUAGE",
})


def _fallback_entities(text: str) -> set[str]:
    """Small dependency-free fallback for names and quoted phrases."""
    values: set[str] = set()
    for value in re.findall(r"[\"']([^\"']{2,})[\"']", text):
        values.add(re.sub(r"\s+", " ", value).strip().casefold())
    for value in re.findall(
        r"\b[A-Z][a-z0-9'’.-]*(?:\s+[A-Z][a-z0-9'’.-]*)+\b",
        text,
    ):
        normalized = re.sub(r"\s+", " ", value).strip().casefold()
        if normalized not in {"the user", "the assistant"}:
            values.add(normalized)
    for value in re.findall(r"\b[A-Z]{2,}[A-Z0-9-]*\b", text):
        values.add(value.casefold())
    for value in re.findall(r"[\u3400-\u9fff]{2,}", text):
        values.add(value)
    return {value for value in values if value}


def _load_entity_model() -> Any:
    global _ENTITY_MODEL, _ENTITY_MODEL_ATTEMPTED
    if not _ENTITY_MODEL_ATTEMPTED:
        _ENTITY_MODEL_ATTEMPTED = True
        try:
            import spacy

            for model_name in ("en_core_web_trf", "en_core_web_sm"):
                try:
                    _ENTITY_MODEL = spacy.load(model_name)
                    break
                except Exception:
                    continue
        except Exception as exc:  # pragma: no cover - environment dependent
            _LOGGER.warning("spaCy unavailable for graph entities: %s", exc)
            _ENTITY_MODEL = False
    return _ENTITY_MODEL


def _limit_entities(values: list[str], max_entities: int = 5) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = re.sub(r"\s+", " ", value).strip()
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
        if len(result) >= max_entities:
            break
    return result


def extract_entities_batch(
    texts: list[str],
    max_entities: int = 5,
    allowed_labels: set[str] | frozenset[str] | None = None,
) -> tuple[list[list[str]], str]:
    """Extract entities in one spaCy pipe call when a model is installed."""
    labels = frozenset(allowed_labels or DEFAULT_ENTITY_LABELS)
    model = _load_entity_model()
    if model:
        rows = [
            _limit_entities(
                [
                    f"{entity.text.strip()}|{entity.label_}"
                    for entity in document.ents
                    if entity.text.strip() and entity.label_ in labels
                ],
                max_entities,
            )
            for document in model.pipe(texts, batch_size=16)
        ]
        return rows, "spacy"
    return [
        _limit_entities(sorted(_fallback_entities(text), key=str.casefold), max_entities)
        for text in texts
    ], "regex_fallback"


def extract_entities(
    text: str,
    max_entities: int = 5,
    allowed_labels: set[str] | frozenset[str] | None = None,
) -> tuple[list[str], str]:
    """Extract at most five typed entities, preferring spaCy when available."""
    rows, method = extract_entities_batch(
        [text], max_entities=max_entities, allowed_labels=allowed_labels
    )
    return rows[0], method


def extract_keywords(
    text: str,
    *,
    model_name: str,
    max_keywords: int = 5,
) -> tuple[list[str], str]:
    """Extract up to five KeyBERT phrases from one segment."""
    global _KEYWORD_MODEL, _KEYWORD_MODEL_ATTEMPTED
    prepared = re.sub(r"(?m)^\[(?:user|assistant|user_agent|ai_agent)\]\s*", "", text).strip()
    try:
        if not _KEYWORD_MODEL_ATTEMPTED:
            from keybert import KeyBERT
            _KEYWORD_MODEL = KeyBERT(model=model_name)
            _KEYWORD_MODEL_ATTEMPTED = True
        extracted = _KEYWORD_MODEL.extract_keywords(
            prepared,
            keyphrase_ngram_range=(1, 3),
            stop_words=None,
            top_n=max_keywords,
            use_mmr=True,
            diversity=0.3,
        )
        values = []
        seen: set[str] = set()
        for phrase, _score in extracted:
            normalized = re.sub(r"\s+", " ", str(phrase)).strip()
            key = normalized.casefold()
            if normalized and key not in seen:
                seen.add(key)
                values.append(normalized)
        return values[:max_keywords], "keybert"
    except Exception as exc:  # pragma: no cover - dependency/model dependent
        _KEYWORD_MODEL_ATTEMPTED = True
        _LOGGER.warning("KeyBERT unavailable for graph keywords: %s", exc)
        tokens = [
            token for token in re.findall(r"[a-z0-9]{2,}", prepared.casefold())
            if token not in {"the", "and", "for", "with", "that", "this", "from"}
        ]
        return list(dict.fromkeys(tokens))[:max_keywords], "token_fallback"


def extract_keywords_batch(
    texts: list[str],
    *,
    model_name: str,
    max_keywords: int = 5,
) -> tuple[list[list[str]], str]:
    """Extract keywords for all segments in one KeyBERT call."""
    global _KEYWORD_MODEL, _KEYWORD_MODEL_ATTEMPTED
    prepared = [
        re.sub(r"(?m)^\[(?:user|assistant|user_agent|ai_agent)\]\s*", "", text).strip()
        for text in texts
    ]
    try:
        if not _KEYWORD_MODEL_ATTEMPTED:
            from keybert import KeyBERT
            _KEYWORD_MODEL = KeyBERT(model=model_name)
            _KEYWORD_MODEL_ATTEMPTED = True
        extracted = _KEYWORD_MODEL.extract_keywords(
            prepared,
            keyphrase_ngram_range=(1, 3),
            stop_words=None,
            top_n=max_keywords,
            use_mmr=True,
            diversity=0.3,
        )
        if prepared and extracted and isinstance(extracted[0], tuple):
            extracted = [extracted]
        rows: list[list[str]] = []
        for values in extracted:
            rows.append(_limit_entities([str(phrase) for phrase, _score in values], max_keywords))
        return rows, "keybert"
    except Exception as exc:  # pragma: no cover - dependency/model dependent
        _KEYWORD_MODEL_ATTEMPTED = True
        _LOGGER.warning("KeyBERT batch extraction unavailable: %s", exc)
        rows = []
        for text in prepared:
            tokens = [
                token for token in re.findall(r"[a-z0-9]{2,}", text.casefold())
                if token not in {"the", "and", "for", "with", "that", "this", "from"}
            ]
            rows.append(list(dict.fromkeys(tokens))[:max_keywords])
        return rows, "token_fallback"


def entity_surface(value: str) -> str:
    return value.split("|", 1)[0]


def entity_type(value: str) -> str:
    return value.rsplit("|", 1)[1] if "|" in value else ""


def feature_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    keyword_counts = Counter(value for row in rows for value in row["keywords"])
    entity_counts = Counter(value for row in rows for value in row["entities"])
    return {
        "node_count": len(rows),
        "keyword_nonempty_count": sum(bool(row["keywords"]) for row in rows),
        "entity_nonempty_count": sum(bool(row["entities"]) for row in rows),
        "mean_keyword_count": sum(len(row["keywords"]) for row in rows) / max(len(rows), 1),
        "mean_entity_count": sum(len(row["entities"]) for row in rows) / max(len(rows), 1),
        "unique_keyword_count": len(keyword_counts),
        "unique_entity_count": len(entity_counts),
        "top_keywords": keyword_counts.most_common(20),
        "top_entities": entity_counts.most_common(20),
        "keyword_extractor_counts": dict(Counter(row["keyword_method"] for row in rows)),
        "entity_extractor_counts": dict(Counter(row["entity_method"] for row in rows)),
    }
