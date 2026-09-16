from __future__ import annotations

from pathlib import Path
from typing import Literal

from ..level_policy import get_level_policy


PROMPT_DIR = Path(__file__).resolve().parent
MULTI_LAYER_DIR = PROMPT_DIR / "multi-layer"
COMMON_DIR = MULTI_LAYER_DIR / "common"
TEMPLATE_DIR = MULTI_LAYER_DIR / "templates"
LEVEL_DIR = MULTI_LAYER_DIR / "levels"


def _read(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"prompt not found: {path}")
    return path.read_text(encoding="utf-8").strip()


_LEGACY_PATHS = {
    "shared_semantics.txt": COMMON_DIR / "shared_semantics.txt",
    "cutting.txt": COMMON_DIR / "cutting.txt",
    "anchor_extraction.txt": COMMON_DIR / "anchor_extraction.txt",
    "community_topic_partition.txt": COMMON_DIR / "community_topic_partition.txt",
    "topic_owner_routing.txt": COMMON_DIR / "topic_owner_routing.txt",
    "memory_extraction.txt": TEMPLATE_DIR / "memory_extraction.txt",
    "memory_extraction_from_memories.txt": TEMPLATE_DIR / "memory_extraction.txt",
    "memory_fusion.txt": TEMPLATE_DIR / "memory_fusion.txt",
    "memory_fusion_from_l1.txt": TEMPLATE_DIR / "memory_fusion.txt",
}


def load_prompt(name: str) -> str:
    """Load a canonical prompt, accepting old logical names for compatibility."""

    path = MULTI_LAYER_DIR / name
    if path.is_file():
        return _read(path)
    if name in _LEGACY_PATHS:
        return _read(_LEGACY_PATHS[name])
    raise FileNotFoundError(f"prompt not found: {MULTI_LAYER_DIR / name}")


SHARED_SEMANTICS = _read(COMMON_DIR / "shared_semantics.txt")


def _render_shared_prompt(path: Path) -> str:
    prompt = _read(path)
    marker = "{{SHARED_SEMANTICS}}"
    if prompt.count(marker) != 1:
        raise ValueError(f"stage prompt must contain exactly one {marker}: {path.name}")
    return prompt.replace(marker, SHARED_SEMANTICS)


def load_stage_prompt(name: str) -> str:
    """Load one non-layered stage prompt from the canonical common directory."""

    path = _LEGACY_PATHS.get(name)
    if path is None or path.parent != COMMON_DIR:
        raise ValueError(f"{name} is not a common stage prompt")
    return _render_shared_prompt(path)


def _render_memory_template(
    task: Literal["extraction", "fusion"],
    target_level: int,
    input_mode: Literal["segments", "memories", "provisional_l1"],
) -> str:
    get_level_policy(target_level)
    if task == "extraction":
        if input_mode == "segments":
            if target_level != 1:
                raise ValueError("segment extraction supports target level 1 only")
            input_rules = (
                "The input is a purified group of raw L0 conversation segments. "
                "Review every segment, including its anchor and original text."
            )
        elif input_mode == "memories":
            if target_level < 2:
                raise ValueError("lower-level memory extraction needs target level >= 2")
            input_rules = (
                "The input is one or more direct lower-level memories. "
                "The lower-level memories are the only semantic input. "
                "Source memory IDs and source segments are provenance, not content to copy."
            )
        else:
            raise ValueError(f"unsupported extraction input mode: {input_mode}")
        template = _read(TEMPLATE_DIR / "memory_extraction.txt")
    else:
        if input_mode == "segments":
            if target_level != 1:
                raise ValueError("segment fusion supports target level 1 only")
            input_rules = (
                "The new_group contains raw conversation segments for the existing L1 topic. "
                "Use those segments as the new evidence."
            )
        elif input_mode == "provisional_l1":
            if target_level < 2:
                raise ValueError("provisional L1 fusion needs target level >= 2")
            input_rules = (
                "The new_group contains a compressed provisional_l1 memory and its "
                "source_segment_ids. The provisional L1 is the only semantic input; "
                "do not request or infer details from original segments. The source IDs "
                "are provenance only."
            )
        else:
            raise ValueError(f"unsupported fusion input mode: {input_mode}")
        template = _read(TEMPLATE_DIR / "memory_fusion.txt")

    replacements = {
        "{{SHARED_SEMANTICS}}": SHARED_SEMANTICS,
        "{{INPUT_MODE_RULES}}": input_rules,
        "{{LEVEL_RULES}}": _read(LEVEL_DIR / f"L{target_level}.txt"),
    }
    for marker, value in replacements.items():
        if template.count(marker) != 1:
            raise ValueError(
                f"memory template must contain exactly one {marker}: {task}"
            )
        template = template.replace(marker, value)
    if "{{" in template or "}}" in template:
        raise ValueError(f"unresolved marker in memory prompt: {task}")
    return template


def load_memory_prompt(
    task: Literal["extraction", "fusion"],
    target_level: int,
    input_mode: Literal["segments", "memories", "provisional_l1"],
) -> str:
    return _render_memory_template(task, target_level, input_mode)


CUTTING_PROMPT = load_stage_prompt("cutting.txt")
ANCHOR_PROMPT = load_stage_prompt("anchor_extraction.txt")
TOPIC_OWNER_ROUTING_PROMPT = load_stage_prompt("topic_owner_routing.txt")
COMMUNITY_TOPIC_PARTITION_PROMPT = load_stage_prompt("community_topic_partition.txt")
# Compatibility alias: the old specialized prompt was never used at runtime.
COMMUNITY_PURIFICATION_PROMPT = COMMUNITY_TOPIC_PARTITION_PROMPT

# Compatibility constants for callers that need the historical default modes.
MEMORY_EXTRACTION_PROMPT = load_memory_prompt("extraction", 1, "segments")
MEMORY_EXTRACTION_FROM_MEMORIES_PROMPT = load_memory_prompt(
    "extraction", 2, "memories"
)
MEMORY_FUSION_PROMPT = load_memory_prompt("fusion", 1, "segments")
MEMORY_FUSION_FROM_L1_PROMPT = load_memory_prompt("fusion", 2, "provisional_l1")


def render_memory_level_policy(target_level: int) -> str:
    """Return the canonical level-specific rule block."""

    get_level_policy(target_level)
    return _read(LEVEL_DIR / f"L{target_level}.txt")


# Historical name retained as a compatibility view of the L1 rule block.
MEMORY_LEVEL_POLICY_PROMPT = render_memory_level_policy(1)


__all__ = [
    "ANCHOR_PROMPT",
    "COMMUNITY_PURIFICATION_PROMPT",
    "COMMUNITY_TOPIC_PARTITION_PROMPT",
    "CUTTING_PROMPT",
    "MEMORY_EXTRACTION_PROMPT",
    "MEMORY_EXTRACTION_FROM_MEMORIES_PROMPT",
    "MEMORY_FUSION_PROMPT",
    "MEMORY_FUSION_FROM_L1_PROMPT",
    "MEMORY_LEVEL_POLICY_PROMPT",
    "SHARED_SEMANTICS",
    "TOPIC_OWNER_ROUTING_PROMPT",
    "load_memory_prompt",
    "load_prompt",
    "load_stage_prompt",
    "render_memory_level_policy",
]
