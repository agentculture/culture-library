"""A small stdlib HTTP client for the culture-library API (the MCP server's only I/O).

Provenance: the shape (``Client``, ``ApiError``, ``from_env``, multipart upload) is cited
(cite-don't-import) from agentculture/xteink @ ea2f643, path ``xteink/client.py``; this copy is
owned here. Modified: culture-library routes and error bodies (flat ``{detail, code}`` or
FastAPI's nested ``{detail: {detail, code}}``), an injectable ``transport`` so tests run the real
app in-process, and ``uploaded_by_me`` (the item's ``uploaded_by_you`` field). It lives here,
not in ``core/``, because ``core`` is network-free.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from culture_library.core import keys

URL_ENV = "CULTURE_LIBRARY_URL"
KEY_ENV = "CULTURE_LIBRARY_API_KEY"
DEFAULT_URL = "http://127.0.0.1:8796"

# (method, url, headers, body) -> (status, response bytes); raises OSError when unreachable.
Transport = Callable[[str, str, dict, "bytes | None"], "tuple[int, bytes]"]

__all__ = [
    "ApiError",
    "AuthenticationError",
    "Client",
    "ConfigError",
    "ServerUnreachable",
    "from_env",
]


class ConfigError(Exception):
    """The client is not configured (no API key)."""


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(f"{code} (HTTP {status}): {detail}")
        self.status, self.code, self.detail = status, code, detail


class AuthenticationError(ApiError):
    """HTTP 401: the key is missing, wrong or revoked."""


class ServerUnreachable(ApiError):
    def __init__(self, detail: str) -> None:
        super().__init__(0, "unreachable", detail)


def _multipart(fields: Mapping[str, str], filename: str, data: bytes) -> tuple[bytes, str]:
    boundary = f"----culture-library-{uuid.uuid4().hex}"
    out = bytearray()
    for name, value in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += value.encode("utf-8") + b"\r\n"
    safe = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
    out += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{safe}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    out += data + b"\r\n" + f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _urllib_transport(timeout: float) -> Transport:
    def send(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
        if not url.startswith(("http://", "https://")):
            raise OSError(f"unsupported URL scheme: {url}")
        req = urllib.request.Request(url, data=body, method=method, headers=headers)  # nosec B310
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    return send


class Client:
    def __init__(
        self,
        base_url: str,
        key: str,
        *,
        timeout: float = 60.0,
        transport: Transport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.key = key
        self._send = transport or _urllib_transport(timeout)

    @property
    def key_id(self) -> str:
        """The caller's own key id (the middle part of ``clk_<id>_<secret>``)."""
        try:
            return keys.parse_key(self.key)
        except keys.AuthError:
            raise AuthenticationError(401, "unauthorized", "malformed API key") from None

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, object] | None = None,
        json_body: object | None = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
        raw: bool = False,
    ):
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        headers = {"Authorization": f"Bearer {self.key}", "Accept": "application/json"}
        body = raw_body
        if json_body is not None:
            body, content_type = json.dumps(json_body).encode(), "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        try:
            status, payload = self._send(method, url, headers, body)
        except OSError as exc:
            raise ServerUnreachable(f"cannot reach {self.base_url}: {exc}") from None
        if status >= 400:
            raise self._error(status, payload)
        if raw:
            return payload
        return json.loads(payload) if payload else None

    @staticmethod
    def _error(status: int, raw: bytes) -> ApiError:
        code, detail = "", ""
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                d = data.get("detail", "")
                if isinstance(d, dict):  # FastAPI-nested {"detail": {"detail", "code"}}
                    code, d = str(d.get("code") or ""), d.get("detail", "")
                code = code or str(data.get("code") or "")
                detail = d if isinstance(d, str) else json.dumps(d)
        except ValueError:
            detail = raw.decode("utf-8", "replace")[:200]
        cls = AuthenticationError if status == 401 else ApiError
        return cls(status, code or f"http_{status}", detail or "request failed")

    # -- library -----------------------------------------------------------

    def list_library(
        self, q: str | None = None, kind: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        query = {"q": q, "kind": kind, "limit": limit, "offset": offset}
        return self._request("GET", "/api/library", query=query)["items"]

    def get_item(self, item_id: int) -> dict:
        return self._request("GET", f"/api/library/{int(item_id)}")

    def get_file(self, item_id: int) -> bytes:
        return self._request("GET", f"/api/library/{int(item_id)}/file", raw=True)

    def upload(
        self,
        data: bytes | None = None,
        filename: str = "",
        title: str | None = None,
        author: str = "",
        kind: str | None = None,
        visibility: str | None = None,
        *,
        path: str | os.PathLike | None = None,
    ) -> dict:
        """Upload bytes (``data``) or a local file (``path``); returns ``{item, created}``."""
        if (data is None) == (path is None):
            raise ValueError("give exactly one of data or path")
        if path is not None:
            p = Path(path)
            data = p.read_bytes()
            filename = filename or p.name
        if not filename:
            raise ValueError("filename is required")
        fields = {"author": author}
        for name, value in (("title", title), ("kind", kind), ("visibility", visibility)):
            if value is not None:
                fields[name] = value
        body, ctype = _multipart(fields, filename, data)  # type: ignore[arg-type]
        return self._request("POST", "/api/library", raw_body=body, content_type=ctype)

    def archive(self, item_id: int) -> None:
        """``DELETE``: archive the item (restorable for 30 days)."""
        self._request("DELETE", f"/api/library/{int(item_id)}")

    def search(
        self, q: str, mode: str | None = None, kind: str | None = None, limit: int = 20
    ) -> dict:
        query = {"q": q, "mode": mode, "kind": kind, "limit": limit}
        return self._request("GET", "/api/search", query=query)

    def rag_retrieve(
        self,
        query: str,
        k: int | None = None,
        mode: str | None = None,
        kind: str | None = None,
        item_ids: list[int] | None = None,
        rerank: bool | None = None,
        candidates: int | None = None,
    ) -> dict:
        """``POST /api/rag/retrieve``; unset options are omitted so the server's defaults apply."""
        body = {
            "query": query,
            "k": k,
            "mode": mode,
            "kind": kind,
            "item_ids": item_ids,
            "rerank": rerank,
            "candidates": candidates,
        }
        return self._request(
            "POST",
            "/api/rag/retrieve",
            json_body={k_: v for k_, v in body.items() if v is not None},
        )

    def send(self, item_id: int, target: str) -> dict:
        return self._request(
            "POST", f"/api/library/{int(item_id)}/send", json_body={"target": target}
        )

    def send_targets(self) -> list[dict]:
        return self._request("GET", "/api/send/targets")["targets"]

    def uploaded_by_me(self, item_id: int, item: Mapping | None = None) -> bool:
        """Whether this client's key uploaded ``item_id`` (``GET /api/library/{id}``'s
        ``uploaded_by_you``); pass an already-fetched ``item`` to skip the request."""
        if item is None:
            item = self.get_item(item_id)
        return item.get("uploaded_by_you") is True


def from_env(env: Mapping[str, str] | None = None) -> Client:
    """Build a client from ``CULTURE_LIBRARY_URL`` / ``CULTURE_LIBRARY_API_KEY``."""
    env = os.environ if env is None else env
    key = (env.get(KEY_ENV) or "").strip()
    if not key:
        raise ConfigError(f"{KEY_ENV} is not set; mint a key with 'culture-library keys mint'")
    return Client((env.get(URL_ENV) or DEFAULT_URL).strip() or DEFAULT_URL, key)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
