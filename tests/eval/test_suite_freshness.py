"""The freshness suite: fresh / stale / unknown / not applicable per question."""

from __future__ import annotations

import pytest

from culture_library.eval import suites
from culture_library.eval.client import LibraryClient
from culture_library.eval.models import Sample
from culture_library.eval.suites.base import SuiteContext
from culture_library.eval.suites.freshness import SUITE, chunks_lookup

STATES = ("fresh_count", "stale_count", "fresh_unknown_count", "fresh_na_count")


def _ctx(samples, current):
    return SuiteContext(
        samples=samples,
        retrieved=[[] for _ in samples],
        k=5,
        item={"id": 7},
        current_chunks=current,
    )


def _state(row):
    on = [k for k in STATES if row[k] == 1.0]
    assert len(on) == 1
    return on[0]


def test_registered_and_metric_names_unique():
    assert "freshness" in suites.names()
    assert SUITE.needs_judge is False
    assert SUITE.version == "1"


def test_fresh_stale_and_not_applicable():
    samples = [
        Sample("a", reference_chunk_shas=["x", "y"]),
        Sample("b", reference_chunk_shas=["x", "gone"]),
        Sample("c", reference_chunk_shas=[]),
    ]
    out = SUITE.score(_ctx(samples, lambda i: ["x", "y", "z"]))
    assert [_state(r) for r in out.per_sample] == [
        "fresh_count",
        "stale_count",
        "fresh_na_count",
    ]
    assert out.per_sample[0]["fresh_share"] == 1.0
    assert out.per_sample[1]["fresh_share"] == 0.0
    assert out.per_sample[2]["fresh_share"] is None
    agg = out.aggregate
    assert (agg["fresh_count"], agg["stale_count"]) == (1, 1)
    assert (agg["fresh_unknown_count"], agg["fresh_na_count"]) == (0, 1)
    assert agg["fresh_share"] == 0.5


def test_gone_item_is_stale():
    def gone(i):
        raise LookupError(i)

    out = SUITE.score(_ctx([Sample("a", reference_chunk_shas=["x"])], gone))
    assert _state(out.per_sample[0]) == "stale_count"
    assert out.aggregate["fresh_share"] == 0.0


def test_unreachable_listing_is_unknown():
    samples = [Sample("a", reference_chunk_shas=["x"]), Sample("b", reference_chunk_shas=[])]
    for lookup in (lambda i: None, None):
        out = SUITE.score(_ctx(samples, lookup))
        assert _state(out.per_sample[0]) == "fresh_unknown_count"
        assert _state(out.per_sample[1]) == "fresh_na_count"
        assert out.aggregate["fresh_unknown_count"] == 1
        assert out.aggregate["fresh_share"] is None


def test_lookup_called_once_per_item():
    calls = []

    def cur(i):
        calls.append(i)
        return ["x"]

    SUITE.score(_ctx([Sample("a", reference_chunk_shas=["x"])] * 3, cur))
    assert calls == [7]


@pytest.mark.parametrize(
    "status,body,expect",
    [
        (200, b'{"chunks":[{"chunk_no":0,"chunk_sha":"s","text":"t"}]}', ["s"]),
        (404, b"{}", LookupError),
        (503, b"{}", None),
        (500, b"{}", None),
    ],
)
def test_chunks_lookup_maps_errors(status, body, expect):
    client = LibraryClient("http://x", transport=lambda m, u, h, b: (status, body))
    look = chunks_lookup(client)
    if expect is LookupError:
        with pytest.raises(LookupError):
            look(1)
    else:
        assert look(1) == expect


def test_chunks_lookup_unreachable_is_none():
    from culture_library.eval.errors import EvalApiError

    def boom(m, u, h, b):
        raise EvalApiError("library unreachable")

    assert chunks_lookup(LibraryClient("http://x", transport=boom))(1) is None
