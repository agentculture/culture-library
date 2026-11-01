"""The indexer's model id and strategy come from the gateway's ``GET /capabilities`` (plan task
t13, decision c42). The GET is mocked at the gateway's opener: no socket is ever opened (the
core suite's guard would fail the test) and the real gateway is never contacted.
"""

from __future__ import annotations

import io
import json
import threading
import time

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
        assert req.get_method() == "GET"
        assert req.full_url.endswith("/capabilities")
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
    first = emb.vector_space()
    second = emb.vector_space()
    assert first == second
    assert len(opener.requests) == 1


def test_unreachable_capabilities_raise_and_are_retried_after_the_failure_ttl(opener):
    clock = [100.0]
    opener.reply = OSError("connection refused")
    emb = _embedder(failure_ttl=30.0, clock=lambda: clock[0])
    with pytest.raises(sr.EmbeddingError, match="capabilities"):
        emb.vector_space()
    clock[0] += 29.0  # within the TTL: the failure is answered from the cache, no request
    opener.reply = CAPS
    with pytest.raises(sr.EmbeddingError, match="connection refused"):
        emb.vector_space()
    assert len(opener.requests) == 1
    clock[0] += 1.5  # past the TTL: asked again, and the success replaces the failure
    assert emb.vector_space()[0] == "google/embeddinggemma-2"
    opener.reply = OSError("down again")
    clock[0] += 1000.0
    assert emb.vector_space()[0] == "google/embeddinggemma-2"  # a success stays cached
    assert len(opener.requests) == 2


def test_a_zero_failure_ttl_retries_every_call(opener):
    opener.reply = OSError("connection refused")
    emb = _embedder(failure_ttl=0)
    for _ in range(2):
        with pytest.raises(sr.EmbeddingError):
            emb.vector_space()
    assert len(opener.requests) == 2


class SlowOpener(FakeOpener):
    """A /capabilities that hangs ``delay`` seconds, then answers (or fails)."""

    def __init__(self, delay, reply=CAPS):
        super().__init__(reply)
        self.delay = delay
        self._lock = threading.Lock()

    def open(self, req, timeout=None):
        time.sleep(self.delay)
        with self._lock:
            return super().open(req, timeout)


def _concurrently(fn, n=5):
    results = [None] * n
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait()
        try:
            results[i] = fn()
        except Exception as exc:  # collected, asserted by the caller
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results, time.monotonic() - started


@pytest.mark.parametrize("reply", [OSError("timed out"), CAPS])
def test_concurrent_callers_share_one_fetch_of_a_hanging_gateway(monkeypatch, reply):
    slow = SlowOpener(0.5, reply)
    monkeypatch.setattr(gw, "_OPENER", slow)
    emb = _embedder()
    results, elapsed = _concurrently(emb.vector_space)
    assert len(slow.requests) == 1  # single flight: one fetch, not one per caller
    assert elapsed < 0.5 * 1.8  # about one timeout in all, not five in a row
    if isinstance(reply, Exception):
        assert all(isinstance(r, sr.EmbeddingError) for r in results), results
        started = time.monotonic()
        with pytest.raises(sr.EmbeddingError, match="timed out"):
            emb.vector_space()  # a later caller fails fast from the negative cache
        assert time.monotonic() - started < 0.1
    else:
        assert {r[0] for r in results} == {"google/embeddinggemma-2"}
    assert len(slow.requests) == 1


def test_the_lock_is_not_held_while_fetching(monkeypatch):
    # a caller arriving mid-fetch waits on the flight, never on a lock held across the I/O
    slow = SlowOpener(0.3)
    monkeypatch.setattr(gw, "_OPENER", slow)
    emb = _embedder()
    leader = threading.Thread(target=emb.vector_space)
    leader.start()
    time.sleep(0.1)
    assert emb._space_lock.acquire(timeout=0.05)  # free while the fetch is in flight
    emb._space_lock.release()
    leader.join(5)
    assert emb.vector_space()[0] == "google/embeddinggemma-2"


def test_from_env_reads_the_failure_ttl(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "sekret")
    assert gw.GatewayEmbedder.from_env("document").failure_ttl == gw.CAPABILITIES_FAILURE_TTL
    monkeypatch.setenv("LOBES_CAPABILITIES_FAILURE_TTL", "5")
    assert gw.GatewayEmbedder.from_env("document").failure_ttl == 5.0
    for bad in ("soon", "-1", "nan", "inf"):
        monkeypatch.setenv("LOBES_CAPABILITIES_FAILURE_TTL", bad)
        assert gw.GatewayEmbedder.from_env().failure_ttl == gw.CAPABILITIES_FAILURE_TTL


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
    emb = _embedder()
    with pytest.raises(sr.EmbeddingError):
        emb.vector_space()


def test_redirects_are_refused(opener):
    opener.reply = gw.RedirectRefusedError("refused redirect: status=302")
    emb = _embedder()
    with pytest.raises(sr.EmbeddingError, match="redirect"):
        emb.vector_space()


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
    emb = _embedder(clock=lambda: clock[0])  # one clock for the backoff and the failure TTL
    idx = ix.Indexer(store, embed=emb, now=lambda: clock[0], backoff_base=10)
    assert idx.run_once() == 0
    st = ix.status(store, now=clock[0])
    assert st["pending"] == 1
    assert "capabilities" in st["last_error"]
    with store.reader() as conn:
        assert conn.execute("SELECT count(*) FROM embeddings").fetchone()[0] == 0
    opener.reply = CAPS
    clock[0] += 11  # the job is due again, but the failure is still remembered: no request
    n_requests = len(opener.requests)
    assert idx.run_once() == 0
    assert len(opener.requests) == n_requests
    clock[0] += gw.CAPABILITIES_FAILURE_TTL  # past the TTL (and the doubled backoff)
    assert idx.run_once() == 1
