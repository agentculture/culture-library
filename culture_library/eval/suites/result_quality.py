"""Result quality suite: duplicates and source diversity in the top k, no judge.

Derived from the ``item_id`` and text/``chunk_sha`` every retrieved passage carries.
"""

from __future__ import annotations

from .base import HIGHER, LOWER, Metric, SuiteContext, SuiteScore, mean


class ResultQualitySuite:
    name = "result_quality"
    version = "1"
    needs_judge = False
    metrics = (
        Metric(
            "duplicate_rate",
            "Share of the top k passages whose exact text repeats an earlier passage in the "
            "same result list (identical text across items counts; near-duplicates do not). "
            "Falls back to chunk_sha, the hash of the text, when a passage carries no text. "
            "Not measured for a question with no retrieved passages. 0 means no repeats.",
            (0.0, 1.0),
            LOWER,
        ),
        Metric(
            "distinct_sources",
            "Number of distinct items (distinct item ids) among the top k passages; at most "
            "k. Not measured for a question with no retrieved passages.",
            (0.0, None),
            HIGHER,
        ),
    )

    def score(self, ctx: SuiteContext) -> SuiteScore:
        per: list[dict[str, float | None]] = []
        for passages in ctx.retrieved:
            top = list(passages)[: ctx.k]
            if not top:
                per.append({"duplicate_rate": None, "distinct_sources": None})
                continue
            seen: set[str] = set()
            dups = 0
            for p in top:
                key = p.text if p.text else p.chunk_sha
                if key in seen:
                    dups += 1
                seen.add(key)
            per.append(
                {
                    "duplicate_rate": dups / len(top),
                    "distinct_sources": len({p.item_id for p in top}),
                }
            )
        keys = [m.name for m in self.metrics]
        return SuiteScore(per, {key: mean([row[key] for row in per]) for key in keys})


SUITE = ResultQualitySuite()
