from __future__ import annotations

from pathlib import Path

from ..level_policy import get_level_policy


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
MEMORY_FUSION_FROM_L1_PROMPT = load_stage_prompt("memory_fusion_from_l1.txt")
TOPIC_OWNER_ROUTING_PROMPT = load_stage_prompt("topic_owner_routing.txt")
COMMUNITY_PURIFICATION_PROMPT = load_stage_prompt("community_purification.txt")
COMMUNITY_TOPIC_PARTITION_PROMPT = load_stage_prompt(
    "community_topic_partition.txt"
)
MEMORY_LEVEL_POLICY_PROMPT = load_prompt("memory_level_policy.txt")


def render_memory_level_policy(target_level: int) -> str:
    """Render the shared level policy block for an extraction/fusion request."""

    policy = get_level_policy(target_level)

    def bullets(values: tuple[str, ...]) -> str:
        return "\n".join(f"- {value}" for value in values)

    replacements = {
        "{{TARGET_LEVEL}}": str(policy.level),
        "{{LEVEL_NAME}}": policy.name,
        "{{LEVEL_PURPOSE}}": policy.purpose,
        "{{MUST_KEEP}}": bullets(policy.must_keep),
        "{{MAY_COMPRESS}}": bullets(policy.may_compress),
        "{{MAY_DISCARD}}": bullets(policy.may_discard),
        "{{MUST_NOT_INFER}}": bullets(policy.must_not_infer),
    }
    rendered = MEMORY_LEVEL_POLICY_PROMPT
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    return rendered

__all__ = [
    "ANCHOR_PROMPT",
    "COMMUNITY_PURIFICATION_PROMPT",
    "COMMUNITY_TOPIC_PARTITION_PROMPT",
    "CUTTING_PROMPT",
    "MEMORY_EXTRACTION_PROMPT",
    "MEMORY_FUSION_PROMPT",
    "MEMORY_FUSION_FROM_L1_PROMPT",
    "MEMORY_LEVEL_POLICY_PROMPT",
    "render_memory_level_policy",
    "TOPIC_OWNER_ROUTING_PROMPT",
    "SHARED_SEMANTICS",
    "load_prompt",
    "load_stage_prompt",
]
