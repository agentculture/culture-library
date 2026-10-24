"""Contract: culture-rules' ``library.push`` upload works unchanged against culture-library.

culture-rules (agentculture/culture-rules @ 1edfd8c, ``culture_rules/node/actions/library.py``)
was written against xteink feat/issue-1 (ea2f643). Its upload is one call::

    POST <base_url>/api/library
    Authorization: Bearer <key>
    Content-Type: multipart/form-data; boundary=culture-rules-<hex>
    fields: title, author, kind=article; file=<slug>.epub (application/epub+zip)

with redirects never followed. It parses a 2xx reply as ``{"item": {"id": int}, "created":
bool}`` and a non-2xx reply's top-level ``code``. These tests send those exact bytes (built by
the cited :mod:`culture_rules_wire`) with a real minted key over the real auth path, and, when
a culture-rules checkout is available, drive the real ``LibraryPushPort`` itself. Its second
call (queueing on an xteink reader device) is out of scope for the library and is stubbed.
"""

from __future__ import annotations

import base64
import inspect
import io
import json
import os
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store, keys  # noqa: E402
from culture_library.core.feed import read_since  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server import routes_library  # noqa: E402

from . import culture_rules_wire as wire  # noqa: E402

TITLE = "Daily newspaper 2026-10-11"


def make_epub(text: str = "Today's news") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("OEBPS/c1.xhtml", f"<html><body><p>{text}</p></body></html>")
    return buf.getvalue()


@pytest.fixture
def server(tmp_path):
    """A real app (no auth override) and a write-only key like culture-rules' grant."""
    app = server_app.create_app()
    store = app.state.store = Store(tmp_path / "data")
    _key, raw = keys.mint(store, "culture-rules newspaper", ["write"])
    return store, TestClient(app), raw


def _push(client, raw_key, data, title=TITLE, author=wire.DEFAULT_AUTHOR):
    """Exactly the request ``LibraryPushPort._push`` sends for the upload."""
    content_type, body = wire.encode_multipart(
        [("title", title), ("author", author), ("kind", wire.KIND)],
        [("file", wire.epub_filename(title), wire.EPUB_TYPE, data)],
    )
    headers = {"Authorization": "Bearer " + raw_key, "Content-Type": content_type}
    return client.post("/api/library", content=body, headers=headers, follow_redirects=False)


def _parse_like_culture_rules(status: int, raw: bytes) -> dict:
    """The checks ``LibraryPushPort`` applies to the upload reply."""
    assert 200 <= status < 300, (status, raw)
    reply = json.loads(raw)
    assert isinstance(reply, dict)
    item = reply.get("item")
    assert isinstance(item, dict)
    item_id = item.get("id")
    assert isinstance(item_id, int) and not isinstance(item_id, bool)
    return {"item_id": item_id, "created": bool(reply.get("created"))}


def test_push_creates_then_dedupes(server):
    store, client, raw = server
    data = make_epub()
    first = _push(client, raw, data)
    assert first.status_code == 201
    out = _parse_like_culture_rules(first.status_code, first.content)
    assert out["created"] is True
    item = first.json()["item"]
    assert item["kind"] == "article" and item["title"] == TITLE
    assert item["author"] == "culture-rules" and item["format"] == "epub"
    assert item["visibility"] == "private"  # pushed without visibility (c43)

    retry = _push(client, raw, data)  # idempotent retry: same bytes, same item
    assert retry.status_code == 200
    assert _parse_like_culture_rules(retry.status_code, retry.content) == {
        "item_id": out["item_id"],
        "created": False,
    }
    with store.reader() as conn:
        events = read_since(conn, 0)["events"]
    assert [(e["type"], e["item_id"]) for e in events] == [("created", out["item_id"])]


def test_push_is_never_redirected(server):
    _, client, raw = server
    r = _push(client, raw, make_epub("other"))
    assert r.status_code == 201 and not r.is_redirect and r.history == []


def test_refusals_carry_a_code_culture_rules_can_name(server, monkeypatch):
    store, client, raw = server
    bad = _push(client, raw, b"not an epub")
    assert bad.status_code == 400 and wire._error_code(bad.content) == "bad_magic"

    monkeypatch.setattr(
        routes_library, "UPLOAD_LIMITS", routes_library.library.IngestLimits(max_bytes=64)
    )
    big = _push(client, raw, make_epub("x" * 500))
    assert big.status_code == 413 and wire._error_code(big.content) == "too_large"

    denied = _push(client, "clk_" + "0" * 40, make_epub())
    assert denied.status_code == 401 and wire._error_code(denied.content) == "unauthorized"

    _k, read_only = keys.mint(store, "reader", ["read"])
    forbidden = _push(client, read_only, make_epub("y"))
    assert forbidden.status_code == 403 and wire._error_code(forbidden.content) == "forbidden"


def test_push_at_the_50mib_limit_is_accepted(server):
    """culture-rules allows exactly MAX_BYTES; the library's limit must be the same."""
    assert routes_library.UPLOAD_LIMITS.max_bytes == wire.MAX_BYTES


# ------------------------------------------------------------------ the real port, if present


def _culture_rules_root() -> Path | None:
    candidates = [os.environ.get("CULTURE_RULES_SRC"), str(Path.home() / "git" / "culture-rules")]
    for cand in candidates:
        if cand and (Path(cand) / "culture_rules" / "node" / "actions" / "library.py").is_file():
            return Path(cand)
    return None


@pytest.fixture
def port_module(monkeypatch):
    root = _culture_rules_root()
    if root is None:
        pytest.skip("no culture-rules checkout (set CULTURE_RULES_SRC)")
    monkeypatch.syspath_prepend(str(root))
    try:
        from culture_rules.node.actions import library as port
    except ImportError as exc:  # pragma: no cover - depends on the sibling checkout
        pytest.skip(f"culture-rules not importable: {exc}")
    yield port
    for name in [m for m in sys.modules if m.split(".")[0] == "culture_rules"]:
        sys.modules.pop(name, None)


def test_cited_copy_matches_upstream(port_module):
    for name in ("encode_multipart", "_boundary", "_header_text", "epub_filename", "_error_code"):
        assert inspect.getsource(getattr(wire, name)) == inspect.getsource(
            getattr(port_module, name)
        ), f"culture-rules {name} drifted from the cited copy"
    assert (port_module.MAX_BYTES, port_module.KIND, port_module.EPUB_TYPE) == (
        wire.MAX_BYTES,
        wire.KIND,
        wire.EPUB_TYPE,
    )


def test_real_library_push_port_against_our_server(server, port_module):
    _, client, raw = server
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append((method, url))
        prefix = "http://library.test"
        assert url.startswith(prefix)
        path = url[len(prefix) :]
        if path == "/api/library":
            r = client.request(
                method, path, content=body, headers=dict(headers), follow_redirects=False
            )
            return r.status_code, r.content
        # the xteink device queue call: not a library concern, answered by a stub
        assert path == "/api/devices/3/queue"
        return 201, json.dumps({"id": 77}).encode()

    push = port_module.LibraryPushPort(None, transport=transport, secret_runner=lambda _n: raw)
    data = make_epub("port")
    params = {"base_url": "http://library.test", "api_key": "grant:LIBRARY_PUSH", "device_id": 3}
    payload = {"content_base64": base64.b64encode(data).decode(), "title": TITLE}
    deadline = datetime.now(UTC) + timedelta(minutes=1)

    first = push._push(payload, params, deadline)
    assert first.outcome == port_module.COMPLETED, first
    assert first.output["created"] is True and isinstance(first.output["item_id"], int)
    again = push._push(payload, params, deadline)
    assert again.output == {**first.output, "created": False}
    assert calls[0] == ("POST", "http://library.test/api/library")
