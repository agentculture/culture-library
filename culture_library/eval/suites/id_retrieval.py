"""ID retrieval suite: rank-based hit metrics over ``chunk_sha``, no judge."""

from __future__ import annotations

from culture_library.core.eval_metrics import id_metrics

from .base import HIGHER, Metric, SuiteContext, SuiteScore, mean


def _found(depth: int) -> Metric:
    return Metric(
        f"found_at_{depth}",
        f"1 if a reference passage is in the top {depth} ranks, else 0; not measured "
        f"(undefined) when the run's k is below {depth}.",
        (0.0, 1.0),
        HIGHER,
    )


class IdRetrievalSuite:
    name = "id_retrieval"
    version = "1"
    needs_judge = False
    metrics = (
        _found(1),
        _found(3),
        _found(5),
        _found(10),
        Metric(
            "precision_at_k",
            "Distinct reference passages among the top k retrieved, divided by k.",
            (0.0, 1.0),
            HIGHER,
        ),
        Metric(
            "mrr",
            "Reciprocal rank of the first reference passage over the top min(10, k) ranks "
            "(0 when none).",
            (0.0, 1.0),
            HIGHER,
        ),
        Metric(
            "ndcg",
            "Binary-relevance nDCG over the top min(10, k) ranks.",
            (0.0, 1.0),
            HIGHER,
        ),
    )

    def score(self, ctx: SuiteContext) -> SuiteScore:
        keys = [m.name for m in self.metrics]
        per: list[dict[str, float | None]] = []
        for sample, passages in zip(ctx.samples, ctx.retrieved):
            got = id_metrics(
                [p.chunk_sha for p in passages], list(sample.reference_chunk_shas), ctx.k
            )
            per.append({key: got.get(key) for key in keys})
        return SuiteScore(per, {key: mean([row[key] for row in per]) for key in keys})


SUITE = IdRetrievalSuite()
