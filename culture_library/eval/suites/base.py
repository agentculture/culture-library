"""The suite contract: metric metadata (plain data) and the :class:`Suite` protocol.

Stdlib only. A suite scores one eval set's retrieval results; every metric key belongs to
exactly one suite (the registry enforces it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from ..models import Passage, Sample

HIGHER = "higher"
LOWER = "lower"


@dataclass(frozen=True)
class Metric:
    """One score a suite reports. JSON-serialisable via :meth:`to_dict`."""

    name: str
    definition: str
    range: tuple[float | None, float | None]
    direction: str  # HIGHER or LOWER: which way is better

    def __post_init__(self) -> None:
        if self.direction not in (HIGHER, LOWER):
            raise ValueError(f"metric {self.name!r}: direction must be 'higher' or 'lower'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "definition": self.definition,
            "range": list(self.range),
            "direction": self.direction,
        }


@dataclass
class SuiteContext:
    """Everything a suite may need to score one run's retrieval results."""

    samples: Sequence[Sample]
    retrieved: Sequence[Sequence[Passage]]
    k: int
    item: dict = field(default_factory=dict)
    judge: Any = None  # JudgeConfig | None
    llm: Any = None  # ChatLLM | None
    is_public: Callable[[int], bool] | None = None  # per-item visibility lookup


@dataclass
class SuiteScore:
    """A suite's output: per-sample metric dicts and aggregates (``None`` = undefined).

    ``skipped`` is a reason string when the suite did not score; per_sample/aggregate are then
    empty and the caller records the reason.
    """

    per_sample: list[dict[str, float | None]] = field(default_factory=list)
    aggregate: dict[str, float | None] = field(default_factory=dict)
    skipped: str | None = None


@runtime_checkable
class Suite(Protocol):
    name: str
    version: str
    metrics: tuple[Metric, ...]
    needs_judge: bool

    def score(self, ctx: SuiteContext) -> SuiteScore: ...


def describe(suite: Suite) -> dict[str, Any]:
    """The JSON catalog entry for a suite."""
    return {
        "name": suite.name,
        "version": suite.version,
        "needs_judge": suite.needs_judge,
        "metrics": [m.to_dict() for m in suite.metrics],
    }


def mean(values: Sequence[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None
