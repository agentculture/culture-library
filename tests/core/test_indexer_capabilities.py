"""The indexer's model id and strategy come from the gateway's ``GET /capabilities`` (plan task
t13, decision c42). The GET is mocked at the gateway's opener: no socket is ever opened (the
core suite's guard would fail the test) and the real gateway is never contacted.
"""

from __future__ import annotations

import io
import json

import pytest

from culture_library import gateway as gw
from culture_library.core import embedding_strategy as es
from culture_library.core import indexer as ix
from culture_library.core import search as sr
from tests.core.search_helpers import fake_embed

# The shape lobes-cli's gateway serves: a flat object keyed by role or lane name.
CAPS = {
    "gemma2-embed": {
        "lane": True,
        "model": "google/embeddinggemma-2",
        "dimension": 768,
        "normalization": "l2",
        "path": "/v1/embeddings",
    },
    "reranker": {"model": "Qwen/Qwen3-Reranker-0.6B"},
}


class FakeOpener:
    """Stands in for ``gateway._OPENER``: answers GETs of /capabilities, refuses anything else."""

    def __init__(self, reply=CAPS):
        self.reply = reply
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        if isinstance(self.reply, Exception):
            raise self.reply
        assert req.get_method() == "GET" and req.full_url.endswith("/capabilities")
        body = self.reply if isinstance(self.reply, bytes) else json.dumps(self.reply).encode()
        return io.BytesIO(body)


@pytest.fixture
def opener(monkeypatch):
    fake = FakeOpener()
    monkeypatch.setattr(gw, "_OPENER", fake)
    return fake


class CapsEmbedder(gw.GatewayEmbedder):
    """A GatewayEmbedder whose /v1/embeddings call is a local fake (the GET stays real code)."""

    def __call__(self, texts):
        return fake_embed(texts)


def _embedder(**kw):
    kw.setdefault("prompt_name", "Retrieval-document")
    return CapsEmbedder("http://gateway.invalid:8001", "sekret", "gemma2-embed", **kw)


def test_vector_space_reads_the_model_from_capabilities(opener):
    emb = _embedder()
    model_id, strategy = emb.vector_space()
    assert model_id == "google/embeddinggemma-2"
    assert strategy == es.CURRENT_STRATEGY  # today's setup gives today's key
    req = opener.requests[0]
    assert req.full_url == "http://gateway.invalid:8001/capabilities"
    assert req.get_header("Authorization") is None  # /capabilities needs no key: none is sent


def test_vector_space_is_cached(opener):
    emb = _embedder()
    assert emb.vector_space() == emb.vector_space()
    assert len(opener.requests) == 1


def test_unreachable_capabilities_raise_and_are_not_cached(opener):
    opener.reply = OSError("connection refused")
    emb = _embedder()
    with pytest.raises(sr.EmbeddingError, match="capabilities"):
        emb.vector_space()
    opener.reply = CAPS
    assert emb.vector_space()[0] == "google/embeddinggemma-2"
    assert len(opener.requests) == 2


@pytest.mark.parametrize(
    "reply",
    [
        {},  # no entry for the role
        {"gemma2-embed": {"dimension": 768, "normalization": "l2"}},  # no model
        {"gemma2-embed": {"model": "m", "normalization": "l2"}},  # no dimension
        {"gemma2-embed": {"model": "m", "dimension": True, "normalization": "l2"}},
        {"gemma2-embed": {"model": "m", "dimension": 768, "normalization": ""}},
        ["not", "an", "object"],
        b"not json",
    ],
)
def test_an_incomplete_answer_is_never_guessed(opener, reply):
    opener.reply = reply
    with pytest.raises(sr.EmbeddingError):
        _embedder().vector_space()


def test_redirects_are_refused(opener):
    opener.reply = gw.RedirectRefusedError("refused redirect: status=302")
    with pytest.raises(sr.EmbeddingError, match="redirect"):
        _embedder().vector_space()


def test_prompts_off_makes_a_strategy_without_prompts(opener, monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "sekret")
    monkeypatch.setenv("LOBES_GATEWAY_URL", "http://gateway.invalid:8001")
    monkeypatch.setenv("LOBES_EMBED_PROMPTS", "off")
    emb = gw.GatewayEmbedder.from_env("document")
    _, strategy = emb.vector_space()
    assert (strategy.document_prompt, strategy.query_prompt) == ("", "")
    assert strategy != es.CURRENT_STRATEGY
    monkeypatch.delenv("LOBES_EMBED_PROMPTS")
    on = gw.GatewayEmbedder.from_env("document").vector_space()[1]
    assert on == es.CURRENT_STRATEGY


def test_strategy_follows_the_reported_dimension_and_normalisation(opener):
    opener.reply = {
        "embedder": {"model": "Qwen/Qwen3-Embedding-0.6B", "dimension": 1024, "normalization": "l2"}
    }
    emb = CapsEmbedder("http://gateway.invalid:8001", "k", "embedder")
    model_id, strategy = emb.vector_space()
    assert model_id == "Qwen/Qwen3-Embedding-0.6B"
    assert (strategy.dimension, strategy.normalisation, strategy.chunker) == (
        1024,
        "l2",
        ix.CHUNKER,
    )
    assert strategy.max_chars == sr.CHUNK_MAX_CHARS
    assert (strategy.document_prompt, strategy.query_prompt) == ("", "")  # no prompt sent


def test_the_indexer_writes_the_capabilities_pair(store, opener):
    item = store.add_item(b"all about cats", kind="document", title="Felines", format="md").item
    with store.connect() as conn:
        ix.index_on_add(conn, item)
    assert ix.Indexer(store, embed=_embedder()).run_once() == 1
    with store.reader() as conn:
        pairs = conn.execute("SELECT DISTINCT model_id, strategy FROM embeddings").fetchall()
    assert [tuple(p) for p in pairs] == [("google/embeddinggemma-2", es.CURRENT_STRATEGY.key())]


def test_the_indexer_backs_off_while_capabilities_are_down(store, opener):
    opener.reply = OSError("connection refused")
    clock = [100.0]
    item = store.add_item(b"all about cats", kind="document", title="Felines", format="md").item
    with store.connect() as conn:
        ix.index_on_add(conn, item)
    idx = ix.Indexer(store, embed=_embedder(), now=lambda: clock[0], backoff_base=10)
    assert idx.run_once() == 0
    st = ix.status(store, now=clock[0])
    assert st["pending"] == 1 and "capabilities" in st["last_error"]
    with store.reader() as conn:
        assert conn.execute("SELECT count(*) FROM embeddings").fetchone()[0] == 0
    opener.reply = CAPS
    clock[0] += 11
    assert idx.run_once() == 1
