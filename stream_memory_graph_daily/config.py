from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class DailyGraphConfig:
    """Configuration for the one-layer daily activity graph."""

    knn_k: int = 10
    new_new_threshold: float = 0.5
    new_memory_threshold: float = 0.5
    resolution: float = 1.0
    community_seed: int = 42
    assignment_min_support: float = 0.35
    assignment_margin: float = 0.08
    path_decay: float = 1.0
    max_assignment_hops: int = 8
    llm_retries: int = 3
    retrieval_rrf_k: int = 60
    postprocess_workers: int = 1
    owner_candidate_top_k: int = 5
    # Adjacent-level owner fusion is the default lifecycle. This switch is
    # retained as an explicit escape hatch for experiments or compatibility.
    enable_owner_bypass: bool = True
    promotion_inactivity_days: tuple[int, ...] = (7, 30)

    def __post_init__(self) -> None:
        if self.knn_k < 1:
            raise ValueError("knn_k must be positive")
        if self.postprocess_workers < 1:
            raise ValueError("postprocess_workers must be positive")
        for name in (
            "new_new_threshold",
            "new_memory_threshold",
            "assignment_min_support",
            "assignment_margin",
            "path_decay",
        ):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.resolution <= 0:
            raise ValueError("resolution must be positive")
        if self.max_assignment_hops < 1:
            raise ValueError("max_assignment_hops must be positive")
        if self.llm_retries < 1:
            raise ValueError("llm_retries must be positive")
        if self.retrieval_rrf_k < 1:
            raise ValueError("retrieval_rrf_k must be positive")
        if self.owner_candidate_top_k < 1:
            raise ValueError("owner_candidate_top_k must be positive")
        if not self.promotion_inactivity_days:
            raise ValueError("promotion_inactivity_days must not be empty")
        if any(
            isinstance(days, bool) or not isinstance(days, int) or days < 1
            for days in self.promotion_inactivity_days
        ):
            raise ValueError("promotion_inactivity_days must contain positive integers")
        if any(
            right <= left
            for left, right in zip(
                self.promotion_inactivity_days,
                self.promotion_inactivity_days[1:],
            )
        ):
            raise ValueError("promotion_inactivity_days must increase by level")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
