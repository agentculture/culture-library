"""End to end (plan task t22): one file of each kind through API, MCP and the mirror.

A fixture repo pushes a spec, a plan, a devague frame and a design PNG. Each is read back
byte-identical over ``GET /content``, the MCP ``repodoc_read`` tool and the mirror backend
(after a deterministic outbox drain). The spec is found by a body phrase through
``/api/search?corpora=repodoc`` and the feed carries exactly one repodoc event per push.
Deviation d8: ``source`` keeps its fulltext|semantic|hybrid meaning; the corpus is ``corpora``.
"""

from __future__ import annotations

import base64
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store  # noqa: E402
from culture_library.core import mirror  # noqa: E402
from culture_library.core import indexer as ix  # noqa: E402
from culture_library.core import keys as core_keys  # noqa: E402
from culture_library.mcp import client as api_client  # noqa: E402
from culture_library.mcp import tools  # noqa: E402
from culture_library.server import ServerConfig  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402

REMOTE = "https://github.com/agentculture/fixture-repo"
COMMIT = "d" * 40
PHRASE = "quokka lighthouse reconciliation"

# A minimal valid 1x1 PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4"
    "DwABBAEAHnOcQAAAAABJRU5ErkJggg=="
)
assert PNG.startswith(b"\x89PNG\r\n\x1a\n")

SPEC = f"# Demo spec\n\nThe {PHRASE} must hold.\n".encode()
PLAN = b"# Demo plan\n\n- t1: build it\n"
FRAME = (
    json.dumps(
        {
            "slug": "demo",
            "title": "Demo frame",
            "schema_version": 1,
            "claims": [{"id": "c1", "text": "It works.", "kind": "outcome", "status": "confirmed"}],
        },
        indent=2,
    )
    + "\n"
).encode()

FILES = [
    ("docs/specs/2026-10-30-demo.md", SPEC),
    ("docs/plans/2026-10-30-demo.md", PLAN),
    (".devague/frames/demo.json", FRAME),
    ("docs/design/mock.png", PNG),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CULTURE_LIBRARY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CULTURE_LIBRARY_PURGE_INTERVAL_S", "0")
    monkeypatch.delenv("CULTURE_LIBRARY_S3_BUCKET", raising=False)
    app = server_app.create_app(ServerConfig())
    app.state.store = store = Store(tmp_path / "data")
    raw = core_keys.mint(store, "flow", ["read", "write"], private=True)[1]
    return app, store, raw


def test_repodoc_flow(env):
    app, store, raw = env
    http = TestClient(app)
    auth = {"Authorization": f"Bearer {raw}"}

    def send(method, url, headers, body):
        r = http.request(method, url, headers=headers, content=body)
        return r.status_code, r.content

    def mcp_read(doc_id):
        client = lambda: api_client.Client("http://testserver", raw, transport=send)  # noqa: E731
        return tools.call_tool("repodoc_read", {"doc_id": doc_id}, client)

    # -- push one of each kind -------------------------------------------------------
    doc_ids = {}
    repos = set()
    for path, data in FILES:
        body = dict(
            remote=REMOTE,
            path=path,
            commit=COMMIT,
            committed_at="2026-10-30T10:00:00Z",
            default_branch_head=COMMIT,
        )
        if path.endswith(".png"):
            body["content_b64"] = base64.b64encode(data).decode("ascii")
        else:
            body["content"] = data.decode("utf-8")
        r = http.post("/api/repodocs", json=body, headers=auth)
        assert r.status_code in (200, 201), r.text
        doc_ids[path] = r.json()["doc"]["id"]
        repos.add(r.json()["doc"]["repo"])
    assert len(set(doc_ids.values())) == 4
    assert len(repos) == 1  # one fixture repo; the server stores its normalised name
    (repo,) = repos

    # -- read back via GET /content (byte-identical) ----------------------------------
    for path, data in FILES:
        r = http.get(f"/api/repodocs/{doc_ids[path]}/content", headers=auth)
        assert r.status_code == 200, r.text
        assert r.content == data, path

    # -- read back via the MCP repodoc_read tool ----------------------------------------
    for path, data in FILES:
        out = mcp_read(doc_ids[path])
        if path.endswith(".png"):
            assert "text" not in out
            assert base64.b64decode(out["content_base64"]) == data
        else:
            assert out["text"].encode("utf-8") == data, path

    # -- read back via the mirror backend after a drain ---------------------------------
    backend = mirror.FakeBackend()
    assert mirror.pending_stats(store)["count"] == 4
    mirror.drain(store, backend)
    assert mirror.pending_stats(store)["count"] == 0
    assert len(backend.objects) == 4
    for path, data in FILES:
        stored, _ctype = backend.objects[mirror.key_for(repo, path, COMMIT)]
        assert stored == data, path

    # -- index (fulltext path) and search by a body phrase ------------------------------
    ix.Indexer(store).run_once()
    r = http.get("/api/search", params={"q": PHRASE, "corpora": "repodoc"}, headers=auth)
    assert r.status_code == 200, r.text
    hits = r.json()["hits"]
    spec_path = "docs/specs/2026-10-30-demo.md"
    assert [h["doc_id"] for h in hits] == [doc_ids[spec_path]]
    assert hits[0]["corpus"] == "repodoc"
    assert hits[0]["path"] == spec_path
    assert hits[0]["repo"]

    # -- feed: exactly one repodoc event per push ---------------------------------------
    r = http.get("/api/feed", params={"subject": "repodoc"}, headers=auth)
    assert r.status_code == 200, r.text
    events = r.json()["events"]
    assert len(events) == 4
    assert {e["subject"] for e in events} == {"repodoc"}
    assert sorted(e["data"]["path"] for e in events) == sorted(p for p, _ in FILES)
    assert {e["data"]["commit"] for e in events} == {COMMIT}
