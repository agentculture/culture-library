"""HTTP embedder for the lobes gateway (OpenAI-compatible ``/v1/embeddings``).

Lives outside ``culture_library.core`` because core is guarded to be network-free
(``tests/core/test_store.py::test_core_source_has_no_network_imports``). Stdlib urllib only.
Configuration comes from the environment (``LOBES_GATEWAY_URL``, ``GATEWAY_API_KEY``,
``LOBES_EMBED_MODEL``); the key is never written to disk or shown in ``repr``.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Sequence

from .core.search import EmbeddingError

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8001"
DEFAULT_EMBED_MODEL = "gemma2-embed"


class GatewayEmbedder:
    """Callable ``texts -> vectors`` against an OpenAI-compatible ``/v1/embeddings``."""

    def __init__(self, url: str, key: str, model: str = DEFAULT_EMBED_MODEL, timeout: float = 60):
        self.url = url.rstrip("/")
        self._key = key
        self.model = model
        self.timeout = timeout

    def __repr__(self) -> str:  # never leak the key
        return f"GatewayEmbedder(url={self.url!r}, model={self.model!r})"

    @classmethod
    def from_env(cls) -> GatewayEmbedder | None:
        key = os.environ.get("GATEWAY_API_KEY", "")
        if not key:
            return None
        return cls(
            os.environ.get("LOBES_GATEWAY_URL", DEFAULT_GATEWAY_URL),
            key,
            os.environ.get("LOBES_EMBED_MODEL", DEFAULT_EMBED_MODEL),
        )

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        body = json.dumps({"model": self.model, "input": list(texts)}).encode()
        req = urllib.request.Request(  # noqa: S310 - operator-configured gateway URL
            f"{self.url}/v1/embeddings",
            data=body,
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosec B310
                payload = json.loads(resp.read())
            rows = sorted(payload["data"], key=lambda r: r.get("index", 0))
            vecs = [[float(x) for x in r["embedding"]] for r in rows]
        except (OSError, ValueError, KeyError, TypeError) as exc:  # URLError is an OSError
            raise EmbeddingError(f"embedding gateway failed: {exc}") from exc
        if len(vecs) != len(texts):
            raise EmbeddingError("embedding gateway returned the wrong number of vectors")
        return vecs
