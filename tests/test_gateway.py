"""Gateway embedder + reranker tests against a local fake HTTP server (127.0.0.1, no real
network)."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from culture_library import gateway as gw
from culture_library.core.search import EmbeddingError


@contextmanager
def fake_gateway(respond):
    """Serve ``respond(path, body) -> (status, headers, payload)``; yield (url, seen requests)."""
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            n = int(self.headers.get("Content-Length") or 0)
            raw_body = self.rfile.read(n)
            body = json.loads(raw_body) if raw_body else None
            seen.append(
                {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
            )
            status, headers, payload = respond(self.path, body)
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST  # a followed 303 would arrive as GET

        def log_message(self, *a):  # keep test output quiet
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()


def _embeddings(path, body):
    data = [{"index": i, "embedding": [float(i), 1.0]} for i in range(len(body["input"]))]
    return 200, {"Content-Type": "application/json"}, {"data": list(reversed(data))}


# ------------------------------------------------------------------ embedder


def test_embedder_request_and_parse_without_prompt():
    with fake_gateway(_embeddings) as (url, seen):
        emb = gw.GatewayEmbedder(url + "/", "k", "m")
        assert emb(["a", "b"]) == [[0.0, 1.0], [1.0, 1.0]]
    assert seen[0]["path"] == "/v1/embeddings"
    assert seen[0]["auth"] == "Bearer k"
    assert seen[0]["body"] == {"model": "m", "input": ["a", "b"]}  # no prompt_name key
    assert emb.prompt_name is None
    assert emb.label == "m"


def test_embedder_sends_prompt_name_when_set():
    with fake_gateway(_embeddings) as (url, seen):
        emb = gw.GatewayEmbedder(url, "k", "gemma2-embed", prompt_name="Retrieval-document")
        emb(["a"])
    assert seen[0]["body"] == {
        "model": "gemma2-embed",
        "input": ["a"],
        "prompt_name": "Retrieval-document",
    }
    assert emb.model == "gemma2-embed"  # the wire model is unchanged
    assert emb.label == "gemma2-embed+Retrieval-document"  # what embeddings.model records


def test_embedder_errors_wrapped():
    with fake_gateway(lambda p, b: (500, {}, b"boom")) as (url, _):
        with pytest.raises(EmbeddingError):
            gw.GatewayEmbedder(url, "k", "m")(["a"])
    with pytest.raises(EmbeddingError):  # nothing listening
        gw.GatewayEmbedder("http://127.0.0.1:9", "k", "m", timeout=2)(["a"])
    with fake_gateway(lambda p, b: (200, {}, {"data": []})) as (url, _):
        with pytest.raises(EmbeddingError):
            gw.GatewayEmbedder(url, "k", "m")(["a"])


def test_from_env_document_and_query(monkeypatch):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("LOBES_EMBED_PROMPTS", raising=False)
    monkeypatch.delenv("LOBES_EMBED_MODEL", raising=False)
    assert gw.GatewayEmbedder.from_env() is None
    assert gw.GatewayEmbedder.from_env("document") is None
    assert gw.GatewayEmbedder.from_env("query") is None
    monkeypatch.setenv("GATEWAY_API_KEY", "sekret")
    monkeypatch.setenv("LOBES_GATEWAY_URL", "http://h:9")
    plain = gw.GatewayEmbedder.from_env()
    assert plain.url == "http://h:9"
    assert plain.model == "gemma2-embed"
    assert plain.prompt_name is None
    assert "sekret" not in repr(plain)
    doc = gw.GatewayEmbedder.from_env("document")
    query = gw.GatewayEmbedder.from_env("query")
    assert doc.prompt_name == "Retrieval-document"
    assert query.prompt_name == "Retrieval-query"
    assert doc.label == "gemma2-embed+Retrieval-document"
    assert "sekret" not in repr(doc) and "sekret" not in repr(query)
    with pytest.raises(ValueError):
        gw.GatewayEmbedder.from_env("bogus")


@pytest.mark.parametrize("off", ["off", "OFF", " off "])
def test_embed_prompts_off_disables_both(monkeypatch, off):
    monkeypatch.setenv("GATEWAY_API_KEY", "sekret")
    monkeypatch.setenv("LOBES_EMBED_PROMPTS", off)
    doc = gw.GatewayEmbedder.from_env("document")
    query = gw.GatewayEmbedder.from_env("query")
    assert doc.prompt_name is None and query.prompt_name is None
    assert doc.label == query.label == "gemma2-embed"


# ------------------------------------------------------------------ reranker


def test_reranker_from_env(monkeypatch):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("LOBES_RERANK_MODEL", raising=False)
    assert gw.GatewayReranker.from_env() is None
    monkeypatch.setenv("GATEWAY_API_KEY", "sekret")
    monkeypatch.setenv("LOBES_GATEWAY_URL", "http://h:9/")
    r = gw.GatewayReranker.from_env()
    assert r.model == "reranker"
    assert r.url == "http://h:9"
    assert "sekret" not in repr(r)
    monkeypatch.setenv("LOBES_RERANK_MODEL", "bge-rr")
    assert gw.GatewayReranker.from_env().model == "bge-rr"


def test_reranker_request_and_parse():
    def respond(path, body):
        results = [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]
        return 200, {}, {"id": "x", "model": body["model"], "results": results}

    with fake_gateway(respond) as (url, seen):
        r = gw.GatewayReranker(url, "k")
        assert r("whale?", ["cat", "whale"]) == [(1, 0.9), (0, 0.1)]
    assert seen[0]["path"] == "/v1/rerank"
    assert seen[0]["auth"] == "Bearer k"
    assert seen[0]["body"] == {
        "model": "reranker",
        "query": "whale?",
        "documents": ["cat", "whale"],
    }


def test_reranker_empty_documents_makes_no_request():
    with fake_gateway(lambda p, b: (500, {}, b"")) as (url, seen):
        assert gw.GatewayReranker(url, "k")("q", []) == []
    assert seen == []


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        {"results": [{"index": 5, "relevance_score": 0.5}]},  # index out of range
        {"results": [{"index": 0}]},
        {"nope": []},
    ],
)
def test_reranker_errors_wrapped(payload):
    with fake_gateway(lambda p, b: (200, {}, payload)) as (url, _):
        with pytest.raises(gw.RerankError):
            gw.GatewayReranker(url, "k")("q", ["a"])
    with fake_gateway(lambda p, b: (503, {}, b"down")) as (url, _):
        with pytest.raises(gw.RerankError):
            gw.GatewayReranker(url, "k")("q", ["a"])


# ------------------------------------------------------------------ redirects refused


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_both_clients_refuse_redirects(code):
    with fake_gateway(_embeddings) as (elsewhere, elsewhere_seen):

        def redirect(path, body):
            return code, {"Location": f"{elsewhere}{path}"}, b""

        with fake_gateway(redirect) as (url, seen):
            with pytest.raises(EmbeddingError, match="redirect"):
                gw.GatewayEmbedder(url, "sekret", "m")(["a"])
            with pytest.raises(gw.RerankError, match="redirect"):
                gw.GatewayReranker(url, "sekret")("q", ["a"])
        assert len(seen) == 2  # one request per client, no follow-up
    assert elsewhere_seen == []  # the key never reached the other origin
