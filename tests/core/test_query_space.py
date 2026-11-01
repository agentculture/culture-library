"""Plan risk r6: queries ask for the (model id, strategy) the query embedder states.

The indexer writes every vector under its embedder's ``vector_space()``; a query must select
the same pair, or it finds nothing (or fails on a dimension mismatch). The current pair is a
fallback only for embedders that state no space (plain callables).
"""

from __future__ import annotations

import pytest

from culture_library.core import embedding_strategy as es
from culture_library.core import search as sr
from tests.core.search_helpers import add, fake_embed, index

OTHER_MODEL = "acme/tiny-embed"
OTHER = es.Strategy(
    document_prompt="",
    query_prompt="",
    chunker="paragraph-pack",
    max_chars=1000,
    dimension=4,
    normalisation="l2",
)


def _small(text: str) -> list[float]:
    low = text.lower()
    return [float("cat" in low), float("dog" in low), 0.1, 0.0]


class SpacedEmbed:
    """An embedder that states its vector space, like GatewayEmbedder."""

    def __init__(self, space=(OTHER_MODEL, OTHER), fail=False):
        self.space = space
        self.fail = fail
        self.lookups = 0

    def vector_space(self):
        self.lookups += 1
        if self.fail:
            raise sr.EmbeddingError("capabilities unreachable")
        return self.space

    def __call__(self, texts):
        return [_small(t) for t in texts]


def test_embedder_space_reads_the_embedders_space():
    assert sr.embedder_space(SpacedEmbed()) == (OTHER_MODEL, OTHER)


def test_embedder_space_falls_back_for_a_plain_callable():
    assert sr.embedder_space(fake_embed) == (es.CURRENT_MODEL_ID, es.CURRENT_STRATEGY)


def test_embedder_space_failure_is_an_embedding_error():
    failing = SpacedEmbed(fail=True)
    with pytest.raises(sr.EmbeddingError):
        sr.embedder_space(failing)

    class Broken:
        def vector_space(self):
            raise RuntimeError("boom")

        def __call__(self, texts):
            return []

    broken = Broken()
    with pytest.raises(sr.EmbeddingError, match="boom"):
        sr.embedder_space(broken)


def _seed(store):
    cats = add(store, "Cats", "all about cats")
    dogs = add(store, "Dogs", "all about dogs")
    pair = {"model_id": OTHER_MODEL, "strategy": OTHER}
    for item in (cats, dogs):
        index(store, item, embed=SpacedEmbed(), **pair)
    return cats, dogs


def test_semantic_searches_the_embedders_space(store):
    cats, _ = _seed(store)
    hits = sr.semantic(store, "cat", SpacedEmbed(), limit=1)
    assert [h.item_id for h in hits] == [cats.id]


def test_hybrid_searches_the_embedders_space(store):
    cats, _ = _seed(store)
    hits = sr.hybrid(store, "cat", SpacedEmbed(), limit=5)
    assert hits[0].item_id == cats.id
    # no vectors under the current pair: a plain callable's semantic leg finds nothing
    assert sr.semantic(store, "cat", fake_embed, limit=5) == []


def test_semantic_with_an_unresolvable_space_raises_and_hybrid_degrades(store):
    _seed(store)
    failing = SpacedEmbed(fail=True)
    with pytest.raises(sr.EmbeddingError):
        sr.semantic(store, "cat", failing)
    hits = sr.hybrid(store, "cats", SpacedEmbed(fail=True))
    assert {h.source for h in hits} == {"fulltext"}


def test_an_explicit_pair_still_wins(store):
    cats, _ = _seed(store)
    embed = SpacedEmbed(space=(es.CURRENT_MODEL_ID, es.CURRENT_STRATEGY))
    hits = sr.semantic(store, "cat", embed, model_id=OTHER_MODEL, strategy=OTHER)
    assert hits[0].item_id == cats.id
    assert embed.lookups == 0
