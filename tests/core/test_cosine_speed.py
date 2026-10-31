"""The cosine scorer is fast and scores exactly as the reference formula did (#25)."""

from __future__ import annotations

import math
import random
import time

from culture_library.core import search as sr


def _reference(a, b):  # the scorer before #25: pure-Python sums
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _vectors(n, dim=768, seed=7):
    rng = random.Random(seed)
    return [sr.pack_vec([rng.uniform(-1, 1) for _ in range(dim)]) for _ in range(n)]


def test_scores_match_the_reference_and_rank_the_same():
    blobs = _vectors(300)
    q = sr.unpack_vec(blobs[0])
    score = sr.cosine_scorer(q)
    new = [score(b) for b in blobs]
    old = [_reference(q, sr.unpack_vec(b)) for b in blobs]
    assert all(math.isclose(a, b, rel_tol=0, abs_tol=1e-12) for a, b in zip(new, old))
    assert sorted(range(300), key=lambda i: -new[i]) == sorted(range(300), key=lambda i: -old[i])


def test_zero_vectors_score_zero():
    zero = sr.pack_vec([0.0] * 4)
    assert sr.cosine_scorer([0.0] * 4)(sr.pack_vec([1.0, 0, 0, 0])) == 0.0
    assert sr.cosine_scorer([1.0, 0, 0, 0])(zero) == 0.0


def test_scoring_8k_vectors_takes_well_under_the_old_cost():
    blobs = _vectors(8000)
    score = sr.cosine_scorer(sr.unpack_vec(blobs[1]))
    start = time.perf_counter()
    for b in blobs:
        score(b)
    took = time.perf_counter() - start
    q = sr.unpack_vec(blobs[1])
    start = time.perf_counter()
    for b in blobs[:2000]:
        _reference(q, sr.unpack_vec(b))
    old = (time.perf_counter() - start) * 4
    assert took < old / 2
