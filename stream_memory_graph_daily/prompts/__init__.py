from __future__ import annotations

from pathlib import Path


PROMPT_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    path = PROMPT_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"prompt not found: {path}")
    return path.read_text(encoding="utf-8").strip()


SHARED_SEMANTICS = load_prompt("shared_semantics.txt")


def load_stage_prompt(name: str) -> str:
    prompt = load_prompt(name)
    marker = "{{SHARED_SEMANTICS}}"
    if prompt.count(marker) != 1:
        raise ValueError(f"stage prompt must contain exactly one {marker}: {name}")
    return prompt.replace(marker, SHARED_SEMANTICS)


CUTTING_PROMPT = load_stage_prompt("cutting.txt")
ANCHOR_PROMPT = load_stage_prompt("anchor_extraction.txt")
MEMORY_EXTRACTION_PROMPT = load_stage_prompt("memory_extraction.txt")
MEMORY_FUSION_PROMPT = load_stage_prompt("memory_fusion.txt")
COMMUNITY_PURIFICATION_PROMPT = load_stage_prompt("community_purification.txt")

__all__ = [
    "ANCHOR_PROMPT",
    "COMMUNITY_PURIFICATION_PROMPT",
    "CUTTING_PROMPT",
    "MEMORY_EXTRACTION_PROMPT",
    "MEMORY_FUSION_PROMPT",
    "SHARED_SEMANTICS",
    "load_prompt",
    "load_stage_prompt",
]
