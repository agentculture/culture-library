"""Freshness suite: is each question's reference still in the item's current chunks?

Three-valued per question, plus not applicable:

- fresh: every reference ``chunk_sha`` is among the item's current chunks;
- stale: some reference is missing, or the item is gone;
- unknown: the chunk listing could not be reached (or no lookup was given);
- not applicable: the question has no references.

Per-sample values are numeric (``SuiteScore.per_sample`` is float-typed): the four state
metrics are one-hot (1.0 / 0.0), and ``fresh_share`` is 1.0 fresh, 0.0 stale, None otherwise.
"""

from __future__ import annotations

from typing import Callable, Sequence

from ..errors import EvalApiError
from .base import HIGHER, LOWER, Metric, SuiteContext, SuiteScore

FRESH, STALE, UNKNOWN, NA = "fresh", "stale", "unknown", "not_applicable"
_COUNT_KEYS = {
    FRESH: "fresh_count",
    STALE: "stale_count",
    UNKNOWN: "fresh_unknown_count",
    NA: "fresh_na_count",
}


def chunks_lookup(client) -> Callable[[int], Sequence[str] | None]:
    """Build a ``SuiteContext.current_chunks`` callable from a ``LibraryClient``.

    A 404 (item gone) raises LookupError; any other failure returns None (unknown).
    """

    def lookup(item_id: int) -> Sequence[str] | None:
        try:
            data = client.request("GET", f"/api/rag/items/{int(item_id)}/chunks")
            return [c["chunk_sha"] for c in data["chunks"]]
        except EvalApiError as exc:
            if exc.status == 404:
                raise LookupError(f"item {item_id} is gone") from exc
            return None
        except (TypeError, ValueError):
            return None

    return lookup


def _state(sample, current: Sequence[str] | None, gone: bool) -> str:
    refs = list(sample.reference_chunk_shas)
    if not refs:
        return NA
    if gone:
        return STALE
    if current is None:
        return UNKNOWN
    have = set(current)
    return FRESH if all(r in have for r in refs) else STALE


class FreshnessSuite:
    name = "freshness"
    version = "1"
    needs_judge = False
    metrics = (
        Metric(
            "fresh_count",
            "Questions whose reference chunk shas are all in the item's current chunks.",
            (0.0, None),
            HIGHER,
        ),
        Metric(
            "stale_count",
            "Questions with a reference chunk sha missing from the item's current chunks, "
            "or whose item is gone.",
            (0.0, None),
            LOWER,
        ),
        Metric(
            "fresh_unknown_count",
            "Questions whose freshness could not be decided (chunk listing unreachable).",
            (0.0, None),
            LOWER,
        ),
        Metric(
            "fresh_na_count",
            "Questions with no reference chunk shas, so freshness does not apply.",
            (0.0, None),
            LOWER,
        ),
        Metric(
            "fresh_share",
            "Fresh questions divided by fresh plus stale (unknown and not applicable "
            "excluded); undefined when neither exists.",
            (0.0, 1.0),
            HIGHER,
        ),
    )

    def score(self, ctx: SuiteContext) -> SuiteScore:
        current: Sequence[str] | None = None
        gone = False
        if ctx.current_chunks is not None:
            try:
                current = ctx.current_chunks(int(ctx.item["id"]))
            except LookupError:
                gone = True
            except (KeyError, TypeError, ValueError):
                current = None
        per: list[dict[str, float | None]] = []
        counts = dict.fromkeys(_COUNT_KEYS.values(), 0)
        for sample in ctx.samples:
            state = _state(sample, current, gone)
            row: dict[str, float | None] = {
                key: 1.0 if s == state else 0.0 for s, key in _COUNT_KEYS.items()
            }
            row["fresh_share"] = {FRESH: 1.0, STALE: 0.0}.get(state)
            counts[_COUNT_KEYS[state]] += 1
            per.append(row)
        decided = counts["fresh_count"] + counts["stale_count"]
        agg: dict[str, float | None] = {k: float(v) for k, v in counts.items()}
        agg["fresh_share"] = counts["fresh_count"] / decided if decided else None
        return SuiteScore(per, agg)


SUITE = FreshnessSuite()
