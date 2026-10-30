"""The one small stdlib HTTP client for the library API (Bearer key, JSON, no redirects)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

from .errors import EvalApiError
from .models import Chunk, EvalRun, EvalSet, Passage

# transport(method, url, headers, body) -> (status, response bytes)
Transport = Callable[[str, str, dict, "bytes | None"], "tuple[int, bytes]"]


# Optional run fields added by the suite runner; omitted from the posted body when unset, so a
# plain EvalRun posts exactly the body it always did.
_OPTIONAL_RUN_KEYS = ("partial_aggregates", "error_rate", "suites")
_OPTIONAL_SAMPLE_KEYS = (
    "question",
    "reference_chunk_shas",
    "set_version",
    "retrieved_item_ids",
    "latency_ms",
    "freshness",
)


def run_body(run: EvalRun) -> dict:
    """The ``POST /api/eval/runs`` body for ``run``, without unset optional fields."""
    body = run.to_dict()
    for key in _OPTIONAL_RUN_KEYS:
        if body.get(key) is None:
            body.pop(key, None)
    for sample in body.get("samples", []):
        for key in _OPTIONAL_SAMPLE_KEYS:
            if sample.get(key) is None:
                sample.pop(key, None)
    return body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # noqa: D401 - never follow (would leak the key)
        return None


def _urllib_transport(timeout: float) -> Transport:
    opener = urllib.request.build_opener(_NoRedirect)

    def send(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)  # noqa: S310
        try:
            with opener.open(req, timeout=timeout) as resp:  # nosec B310
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except OSError as exc:
            raise EvalApiError(f"library unreachable: {exc}") from exc

    return send


class LibraryClient:
    """Thin JSON client. Pass ``transport`` to swap the wire (tests use a TestClient)."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        timeout: float = 60,
        transport: Transport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self._send = transport or _urllib_transport(timeout)

    def __repr__(self) -> str:
        return f"LibraryClient({self.base_url!r})"

    def request(self, method: str, path: str, body: Any = None) -> Any:
        headers = {"Accept": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, raw = self._send(method, self.base_url + path, headers, data)
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw.decode("utf-8", "replace")
        if status >= 400 or 300 <= status < 400:
            raise EvalApiError(f"{method} {path} -> HTTP {status}", status=status, body=payload)
        return payload

    # ---- endpoints
    def status(self) -> dict:
        return self.request("GET", "/api/status")

    def get_item(self, item_id: int) -> dict:
        return self.request("GET", f"/api/library/{int(item_id)}")

    def get_chunks(self, item_id: int) -> list[Chunk]:
        data = self.request("GET", f"/api/rag/items/{int(item_id)}/chunks")
        return [Chunk.from_dict(c) for c in data["chunks"]]

    def retrieve(self, query: str, k: int = 5, mode: str = "hybrid", **opts: Any) -> list[Passage]:
        body = {"query": query, "k": k, "mode": mode}
        body.update({k_: v for k_, v in opts.items() if v is not None})
        data = self.request("POST", "/api/rag/retrieve", body)
        return [Passage.from_dict(p) for p in data["passages"]]

    def get_sources(self, item_id: int) -> list[dict]:
        data = self.request("GET", f"/api/library/{int(item_id)}/sources")
        return data["sources"] if isinstance(data, dict) and "sources" in data else data

    def put_sources(self, item_id: int, manifest: list[dict]) -> Any:
        return self.request("PUT", f"/api/library/{int(item_id)}/sources", manifest)

    def create_set(self, eval_set: EvalSet) -> dict:
        return self.request("POST", "/api/eval/sets", eval_set.to_dict())

    def list_sets(self) -> Any:
        return self.request("GET", "/api/eval/sets")

    def get_set(self, set_id: int) -> EvalSet:
        return EvalSet.from_dict(self.request("GET", f"/api/eval/sets/{int(set_id)}"))

    def create_run(self, run: EvalRun) -> dict:
        return self.request("POST", "/api/eval/runs", run_body(run))

    def list_runs(self) -> Any:
        return self.request("GET", "/api/eval/runs")

    def get_run(self, run_id: int) -> Any:
        return self.request("GET", f"/api/eval/runs/{int(run_id)}")
