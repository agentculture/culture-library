"""HTTP clients for the lobes gateway: an embedder (OpenAI-compatible ``/v1/embeddings``) and a
reranker (vLLM-style ``/v1/rerank``).

Lives outside ``culture_library.core`` because core is guarded to be network-free
(``tests/core/test_store.py::test_core_source_has_no_network_imports``). Stdlib urllib only.

Configuration comes from the environment; the key is never written to disk or shown in ``repr``:

- ``GATEWAY_API_KEY``: the bearer key; unset means no gateway (``from_env`` returns ``None``).
- ``LOBES_GATEWAY_URL``: the gateway origin (default ``http://127.0.0.1:8001``).
- ``LOBES_EMBED_MODEL``: the embedding model (default ``gemma2-embed``).
- ``LOBES_EMBED_PROMPTS``: ``off`` disables the ``prompt_name`` field on both the document
  (``Retrieval-document``, used by the indexer) and the query (``Retrieval-query``, used by
  retrieval) embedders; anything else, or unset, enables them.
- ``LOBES_RERANK_MODEL``: the reranker model (default ``reranker``).

Both clients refuse every HTTP redirect (the idea is cited from embodiment's
``voice._NoRedirectHandler``): stdlib ``urlopen`` would resend ``Authorization`` to whatever
host a ``Location`` header names, so the key would leave the configured origin. A redirect
surfaces as the client's ordinary error (:class:`EmbeddingError` / :class:`RerankError`).
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Sequence

from .core.search import EmbeddingError

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8001"
DEFAULT_EMBED_MODEL = "gemma2-embed"
DEFAULT_RERANK_MODEL = "reranker"

#: ``from_env`` purpose -> the gemma2-embed prompt name sent with it.
PROMPT_NAMES = {"document": "Retrieval-document", "query": "Retrieval-query"}

_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError)


class RerankError(Exception):
    """The reranker could not score the documents (transport, status or payload failure)."""


class RedirectRefusedError(OSError):
    """The gateway answered with a redirect; it is never followed."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: every ``http_error_30x`` calls ``redirect_request``, and raising
    here propagates out of ``opener.open()`` before any second request is made."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise RedirectRefusedError(f"refused redirect: status={code}")


#: Stateless, shared by both clients.
_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def open_no_redirect(req: urllib.request.Request, timeout: float):
    """Open ``req`` refusing any redirect (raises :class:`RedirectRefusedError`)."""
    if not req.full_url.startswith(("http://", "https://")):
        raise ValueError("URL must be http(s)")
    return _OPENER.open(req, timeout=timeout)  # nosec B310 - scheme checked, no redirects


def _post_json(url: str, key: str, payload: dict, timeout: float) -> object:
    """POST ``payload`` as JSON with the bearer key; return the decoded JSON reply.

    Raises ``OSError`` (``URLError``/``HTTPError``/:class:`RedirectRefusedError`) or
    ``ValueError`` (bad URL scheme, undecodable reply).
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError("gateway URL must be http(s)")
    req = urllib.request.Request(  # noqa: S310 - scheme checked above
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with _OPENER.open(req, timeout=timeout) as resp:  # nosec B310 - scheme checked, no redirects
        return json.loads(resp.read())


def _gateway_from_env() -> tuple[str, str] | None:
    key = os.environ.get("GATEWAY_API_KEY", "")
    if not key:
        return None
    return os.environ.get("LOBES_GATEWAY_URL", DEFAULT_GATEWAY_URL), key


def prompts_enabled() -> bool:
    """False only when ``LOBES_EMBED_PROMPTS`` is ``off`` (case and whitespace ignored)."""
    return os.environ.get("LOBES_EMBED_PROMPTS", "").strip().lower() != "off"


class GatewayEmbedder:
    """Callable ``texts -> vectors`` against an OpenAI-compatible ``/v1/embeddings``.

    ``prompt_name`` (e.g. ``Retrieval-document``) is sent only when set. ``model`` is the wire
    model; ``label`` (``model`` or ``model+prompt_name``) is what the indexer records in
    ``embeddings.model`` so vectors made under different prompt configs are detectable.
    """

    def __init__(
        self,
        url: str,
        key: str,
        model: str = DEFAULT_EMBED_MODEL,
        timeout: float = 60,
        prompt_name: str | None = None,
    ):
        self.url = url.rstrip("/")
        self._key = key
        self.model = model
        self.timeout = timeout
        self.prompt_name = prompt_name or None

    @property
    def label(self) -> str:
        return f"{self.model}+{self.prompt_name}" if self.prompt_name else self.model

    def __repr__(self) -> str:  # never leak the key
        return (
            f"GatewayEmbedder(url={self.url!r}, model={self.model!r}, "
            f"prompt_name={self.prompt_name!r})"
        )

    @classmethod
    def from_env(cls, purpose: str | None = None) -> GatewayEmbedder | None:
        """The configured embedder, or ``None`` without ``GATEWAY_API_KEY``.

        ``purpose`` ``"document"`` (indexing) or ``"query"`` (retrieval) selects the matching
        ``Retrieval-*`` prompt unless ``LOBES_EMBED_PROMPTS=off``; ``None`` sends no prompt.
        """
        if purpose is not None and purpose not in PROMPT_NAMES:
            raise ValueError(f"unknown embedder purpose {purpose!r}")
        gateway = _gateway_from_env()
        if gateway is None:
            return None
        prompt = PROMPT_NAMES[purpose] if purpose and prompts_enabled() else None
        return cls(
            *gateway,
            os.environ.get("LOBES_EMBED_MODEL", DEFAULT_EMBED_MODEL),
            prompt_name=prompt,
        )

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        payload: dict = {"model": self.model, "input": list(texts)}
        if self.prompt_name:
            payload["prompt_name"] = self.prompt_name
        try:
            reply = _post_json(f"{self.url}/v1/embeddings", self._key, payload, self.timeout)
            rows = sorted(reply["data"], key=lambda r: r.get("index", 0))  # type: ignore[index]
            vecs = [[float(x) for x in r["embedding"]] for r in rows]
        except _ERRORS as exc:  # URLError/HTTPError/RedirectRefusedError are OSErrors
            raise EmbeddingError(f"embedding gateway failed: {exc}") from exc
        if len(vecs) != len(texts):
            raise EmbeddingError("embedding gateway returned the wrong number of vectors")
        return vecs


class GatewayReranker:
    """Callable ``(query, documents) -> [(index, relevance_score)]`` against ``/v1/rerank``.

    Results keep the gateway's order (vLLM answers best first); every index refers to
    ``documents``. Raises :class:`RerankError` on any failure.
    """

    def __init__(self, url: str, key: str, model: str = DEFAULT_RERANK_MODEL, timeout: float = 60):
        self.url = url.rstrip("/")
        self._key = key
        self.model = model
        self.timeout = timeout

    def __repr__(self) -> str:  # never leak the key
        return f"GatewayReranker(url={self.url!r}, model={self.model!r})"

    @classmethod
    def from_env(cls) -> GatewayReranker | None:
        gateway = _gateway_from_env()
        if gateway is None:
            return None
        return cls(*gateway, os.environ.get("LOBES_RERANK_MODEL", DEFAULT_RERANK_MODEL))

    def __call__(self, query: str, documents: Sequence[str]) -> list[tuple[int, float]]:
        docs = list(documents)
        if not docs:
            return []
        payload = {"model": self.model, "query": query, "documents": docs}
        try:
            reply = _post_json(f"{self.url}/v1/rerank", self._key, payload, self.timeout)
            scored = [
                (int(r["index"]), float(r["relevance_score"]))
                for r in reply["results"]  # type: ignore[index]
            ]
        except _ERRORS as exc:
            raise RerankError(f"rerank gateway failed: {exc}") from exc
        if any(not 0 <= i < len(docs) for i, _ in scored):
            raise RerankError("rerank gateway returned an index out of range")
        return scored
