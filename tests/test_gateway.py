"""Gateway embedder tests (urlopen stubbed; no real network)."""

import io
import json
import urllib.error

import pytest

from culture_library import gateway as gw
from culture_library.core.search import EmbeddingError


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_gateway_embedder_request_and_parse(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = json.loads(req.data)
        data = [{"index": 1, "embedding": [2.0]}, {"index": 0, "embedding": [1.0]}]
        return _Resp(json.dumps({"data": data}).encode())

    monkeypatch.setattr(gw.urllib.request, "urlopen", fake_urlopen)
    emb = gw.GatewayEmbedder("http://gw:1/", "k", "m")
    assert emb(["a", "b"]) == [[1.0], [2.0]]
    assert seen["url"] == "http://gw:1/v1/embeddings"
    assert seen["auth"] == "Bearer k"
    assert seen["body"] == {"model": "m", "input": ["a", "b"]}


def test_gateway_embedder_errors_wrapped(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(gw.urllib.request, "urlopen", boom)
    embedder = gw.GatewayEmbedder("http://gw:1", "k", "m")
    with pytest.raises(EmbeddingError):
        embedder(["a"])


def test_from_env(monkeypatch):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    assert gw.GatewayEmbedder.from_env() is None
    monkeypatch.setenv("GATEWAY_API_KEY", "sekret")
    monkeypatch.setenv("LOBES_GATEWAY_URL", "http://h:9")
    e = gw.GatewayEmbedder.from_env()
    assert e.url == "http://h:9"
    assert e.model == "gemma2-embed"
    assert "sekret" not in repr(e)
