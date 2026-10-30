"""ID retrieval metrics from stored ranks: pure arithmetic, stdlib only.

A passage is either a bare ``chunk_sha`` string (sha-only data, as stored by runs
1-22) or an ``(item_id, chunk_sha)`` pair. Identical chunk text shares one sha
across items, so when both sides carry item ids a passage counts as found only
if the item id *and* the sha match; otherwise it matches on sha and the result
says ``sha_only: True``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Passage = str | tuple[int, str]

FOUND_DEPTHS = (1, 3, 5, 10)
MRR_DEPTH = 10


def _has_item(passages: Sequence[Passage]) -> bool:
    return bool(passages) and all(not isinstance(p, str) for p in passages)


def _key(passage: Passage, item_aware: bool):
    if isinstance(passage, str):
        return passage
    item_id, sha = passage
    return (item_id, sha) if item_aware else sha


def id_metrics(
    retrieved: Sequence[Passage], references: Sequence[Passage], k_run: int
) -> dict[str, float | int | bool | None]:
    """Score one ranked retrieval against its reference passages.

    Returns ``found_at_1/3/5/10`` (1.0 or 0.0; ``None`` when the depth exceeds
    ``k_run``, i.e. not measured), ``precision_at_k`` (distinct matched passages
    in the top ``k_run`` / ``k_run``), ``mrr`` and ``ndcg`` (binary
    relevance, over the top ``depth`` = ``min(10, k_run)`` stored ranks, so a
    k=5 run's values are labelled depth 5, never 10), ``depth``, ``k`` and
    ``sha_only``. With no references every metric is ``None``.
    """
    item_aware = _has_item(retrieved) and _has_item(references)
    refs = {_key(r, item_aware) for r in references}
    top = list(retrieved)[: max(k_run, 0)]
    seen: set = set()
    rel: list[int] = []  # 1 when this rank is a first-seen reference match
    for p in top:
        key = _key(p, item_aware)
        hit = key in refs and key not in seen
        seen.add(key)
        rel.append(1 if hit else 0)
    out: dict[str, float | int | bool | None] = {
        "k": k_run,
        "depth": min(MRR_DEPTH, max(k_run, 0)),
        "sha_only": not item_aware,
    }
    if not refs:
        for d in FOUND_DEPTHS:
            out[f"found_at_{d}"] = None
        out.update(precision_at_k=None, mrr=None, ndcg=None)
        return out
    for d in FOUND_DEPTHS:
        out[f"found_at_{d}"] = None if d > k_run else float(any(rel[:d]))
    out["precision_at_k"] = sum(rel) / k_run if k_run > 0 else None
    depth = rel[:MRR_DEPTH]
    first = next((i for i, r in enumerate(depth, 1) if r), None)
    out["mrr"] = 1.0 / first if first else 0.0
    dcg = sum(r / math.log2(i + 1) for i, r in enumerate(depth, 1))
    ideal = sum(1 / math.log2(i + 1) for i in range(1, min(len(refs), len(depth)) + 1))
    out["ndcg"] = dcg / ideal if ideal else 0.0
    return out
