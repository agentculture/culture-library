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

Which vectors the embedder makes (decision c42): :meth:`GatewayEmbedder.vector_space` reads the
gateway's ``GET /capabilities`` (no key needed, none is sent), whose entry for the embedder's
model (a role or lane name such as ``gemma2-embed``) names the real model (``model``, e.g.
``google/embeddinggemma-2``), its ``dimension`` and its ``normalization``. With the prompts and
the indexer's chunker that makes the ``(model_id, Strategy)`` pair every stored vector carries.
The answer is cached for the embedder's lifetime (a restart reads it again); an unreachable or
incomplete answer raises :class:`EmbeddingError` and is not cached, so the indexer's job backs
off and asks again instead of guessing.

Both clients refuse every HTTP redirect (the idea is cited from embodiment's
``voice._NoRedirectHandler``): stdlib ``urlopen`` would resend ``Authorization`` to whatever
host a ``Location`` header names, so the key would leave the configured origin. A redirect
surfaces as the client's ordinary error (:class:`EmbeddingError` / :class:`RerankError`).
"""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from typing import Sequence

from .core import indexer
from .core.embedding_strategy import Strategy
from .core.search import EmbeddingError

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8001"
DEFAULT_EMBED_MODEL = "gemma2-embed"
DEFAULT_RERANK_MODEL = "reranker"

#: ``from_env`` purpose -> the gemma2-embed prompt name sent with it.
PROMPT_NAMES = {"document": "Retrieval-document", "query": "Retrieval-query"}

_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError)
CAPABILITIES_TIMEOUT = 10.0


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


def fetch_capabilities(url: str, timeout: float = CAPABILITIES_TIMEOUT) -> dict:
    """``GET {url}/capabilities`` (no key: the endpoint needs none) as a JSON object.

    Raises ``OSError`` (transport, HTTP status, a refused redirect) or ``ValueError`` (a bad
    URL scheme, a reply that is not a JSON object).
    """
    req = urllib.request.Request(  # noqa: S310 - open_no_redirect checks the scheme
        f"{url.rstrip('/')}/capabilities", headers={"Accept": "application/json"}, method="GET"
    )
    with open_no_redirect(req, timeout) as resp:
        return _object(json.loads(resp.read()))


def _space_from_capabilities(caps: dict, role: str) -> tuple[str, int, str]:
    """``(model_id, dimension, normalisation)`` from the ``role`` entry; ValueError if absent."""
    entry = caps.get(role)
    if not isinstance(entry, dict):
        raise ValueError(f"/capabilities has no entry for {role!r}")
    model_id = entry.get("model")
    dimension = entry.get("dimension")
    norm = entry.get("normalization", entry.get("normalisation"))
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError(f"/capabilities entry {role!r} names no model")
    if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension < 1:
        raise ValueError(f"/capabilities entry {role!r} has no positive dimension: {dimension!r}")
    if not isinstance(norm, str) or not norm.strip():
        raise ValueError(f"/capabilities entry {role!r} states no normalization")
    return model_id.strip(), dimension, norm.strip()


def _object(reply: object) -> dict:
    """``reply`` as a JSON object; anything else is a ``ValueError`` (a malformed reply)."""
    if not isinstance(reply, dict):
        raise ValueError(f"expected a JSON object, got {type(reply).__name__}")
    return reply


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
    model (a gateway role or lane name); ``label`` (``model`` or ``model+prompt_name``) is a
    display name (``/api/status``). What a stored vector records is :meth:`vector_space`.

    ``prompts`` is the (document, query) prompt pair of the configuration this embedder
    belongs to, part of the strategy. Without it, a ``Retrieval-*`` ``prompt_name`` implies
    the standard pair, another prompt is taken for both, and no prompt means neither.
    """

    def __init__(
        self,
        url: str,
        key: str,
        model: str = DEFAULT_EMBED_MODEL,
        timeout: float = 60,
        prompt_name: str | None = None,
        prompts: tuple[str | None, str | None] | None = None,
    ):
        self.url = url.rstrip("/")
        self._key = key
        self.model = model
        self.timeout = timeout
        self.prompt_name = prompt_name or None
        if prompts is None:
            standard = (PROMPT_NAMES["document"], PROMPT_NAMES["query"])
            if self.prompt_name in standard:
                prompts = standard
            else:
                prompts = (self.prompt_name, self.prompt_name)
        self.prompts = (prompts[0] or None, prompts[1] or None)
        self._space: tuple[str, Strategy] | None = None
        self._space_lock = threading.Lock()

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
        on = prompts_enabled()
        return cls(
            *gateway,
            os.environ.get("LOBES_EMBED_MODEL", DEFAULT_EMBED_MODEL),
            prompt_name=prompt,
            prompts=(
                PROMPT_NAMES["document"] if on else None,
                PROMPT_NAMES["query"] if on else None,
            ),
        )

    def vector_space(self) -> tuple[str, Strategy]:
        """``(model_id, strategy)`` of the vectors this embedder makes, from ``/capabilities``.

        Cached after the first answer. Raises :class:`EmbeddingError` (retryable: nothing is
        cached) when ``/capabilities`` is unreachable or its entry for :attr:`model` lacks the
        model id, dimension or normalization; the model is never guessed.
        """
        with self._space_lock:
            if self._space is None:
                try:
                    caps = fetch_capabilities(self.url, min(self.timeout, CAPABILITIES_TIMEOUT))
                    model_id, dimension, norm = _space_from_capabilities(caps, self.model)
                except _ERRORS as exc:
                    raise EmbeddingError(
                        f"cannot tell which model {self.model!r} is from "
                        f"{self.url}/capabilities: {exc}"
                    ) from exc
                strategy = indexer.strategy_for(
                    document_prompt=self.prompts[0],
                    query_prompt=self.prompts[1],
                    dimension=dimension,
                    normalisation=norm,
                )
                self._space = (model_id, strategy)
            return self._space

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        payload: dict = {"model": self.model, "input": list(texts)}
        if self.prompt_name:
            payload["prompt_name"] = self.prompt_name
        try:
            reply = _object(
                _post_json(f"{self.url}/v1/embeddings", self._key, payload, self.timeout)
            )
            rows = sorted(reply["data"], key=lambda r: r.get("index", 0))
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
            reply = _object(_post_json(f"{self.url}/v1/rerank", self._key, payload, self.timeout))
            scored = [(int(r["index"]), float(r["relevance_score"])) for r in reply["results"]]
        except _ERRORS as exc:
            raise RerankError(f"rerank gateway failed: {exc}") from exc
        if any(not 0 <= i < len(docs) for i, _ in scored):
            raise RerankError("rerank gateway returned an index out of range")
        return scored
