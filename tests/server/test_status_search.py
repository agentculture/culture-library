"""Tests for GET /api/status and GET /api/search. The gateway is never contacted."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store  # noqa: E402
from culture_library.core import indexer as ix  # noqa: E402
from culture_library.core import search as sr  # noqa: E402
from culture_library.core.feed import append  # noqa: E402
from culture_library.core.repodocs import RepoDocs  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server import routes_search  # noqa: E402
from culture_library.server.auth import Principal, current_principal  # noqa: E402

READ = frozenset({"read"})


def _client(tmp_path, principal, embedder=None):
    app = server_app.create_app()
    store = app.state.store = Store(tmp_path / "data")
    app.state.embedder = embedder
    app.dependency_overrides[current_principal] = lambda: principal
    return store, TestClient(app)


def _add(store, title, kind="book", visibility="public", body=b"", created_at=None):
    res = store.add_item(
        body or title.encode(),
        kind=kind,
        title=title,
        format="txt",
        visibility=visibility,
        created_at=created_at,
    )
    with store.connect() as conn:
        sr.index_title(conn, res.item)
    return res.item


def _vec(text):
    return [1.0, 0.0] if "whale" in text else [0.0, 1.0]


def _embed(texts):
    return [_vec(t) for t in texts]


def _down(texts):
    raise sr.EmbeddingError("gateway down")


# ------------------------------------------------------------------ status


def _seed_status(store):
    _add(store, "Moby", "book", "public", created_at="2026-01-01T00:00:00Z")
    _add(store, "Secret diary", "diary", "private", created_at="2026-02-01T00:00:00Z")
    _add(store, "Daily paper", "newspaper", "public", created_at="2026-03-01T00:00:00Z")
    with store.connect() as conn:
        append(
            conn,
            {
                "type": "created",
                "item_id": 1,
                "sha256": "a" * 64,
                "kind": "book",
                "visibility": "public",
            },
        )
        append(
            conn,
            {
                "type": "created",
                "item_id": 2,
                "sha256": "b" * 64,
                "kind": "diary",
                "visibility": "private",
            },
        )


def _push(store, visibility, path="docs/specs/a.md"):
    RepoDocs(store).push(
        remote="https://github.com/x/y.git",
        path=path,
        content=b"hello " + path.encode(),
        commit="c" * 40,
        committed_at="2026-04-01T00:00:00Z",
        default_branch_head="c" * 40,
        visibility=visibility,
    )


def test_status_counts_private_caller(tmp_path):
    store, c = _client(tmp_path, Principal("k", READ, private=True))
    _seed_status(store)
    _push(store, "private")
    body = c.get("/api/status").json()
    assert body["private_included"] is True
    items = body["counts"]["items"]
    assert items["total"] == 3
    assert items["by_kind"] == {"book": 1, "diary": 1, "newspaper": 1}
    assert items["by_visibility"] == {"public": 2, "private": 1}
    assert items["by_kind_visibility"]["diary"] == {"private": 1}
    assert body["counts"]["repodocs"]["by_kind"] == {"spec": 1}
    assert body["counts"]["repodocs"]["versions"] == 1
    assert body["feed"]["cursor"] == 2
    assert body["last_push"]["items"]["by_kind"]["diary"] == "2026-02-01T00:00:00Z"
    assert body["last_push"]["repodocs"]["by_kind"]["spec"]
    assert body["last_push"]["source_note"]
    assert body["last_push"]["feed"]["created"]
    idx = body["index"]
    assert {"pending", "done", "failed", "lag_seconds"} <= set(idx)
    assert idx["pending"] == 0


def test_status_public_caller_sees_public_only(tmp_path):
    store, c = _client(tmp_path, Principal("k", READ))
    _seed_status(store)
    _push(store, "private", "docs/specs/p.md")
    _push(store, "public", "docs/plans/q.md")
    body = c.get("/api/status").json()
    assert body["private_included"] is False
    items = body["counts"]["items"]
    assert items["total"] == 2
    assert "diary" not in items["by_kind"]
    assert items["by_visibility"] == {"public": 2}
    assert body["counts"]["repodocs"]["by_kind"] == {"plan": 1}
    assert "diary" not in body["last_push"]["items"]["by_kind"]
    assert body["feed"]["cursor"] == 2  # the cursor itself reveals nothing about content


def test_status_reflects_live_database_and_index_failures(tmp_path):
    store, c = _client(tmp_path, Principal("k", READ, private=True))
    assert c.get("/api/status").json()["counts"]["items"]["total"] == 0
    _add(store, "One")
    ix.enqueue_missing(store)
    ix.Indexer(store, _down, max_attempts=1).run_once()
    body = c.get("/api/status").json()
    assert body["counts"]["items"]["total"] == 1
    assert body["index"]["failed"] == 1
    assert body["index"]["last_error"]
    _add(store, "Two")
    assert c.get("/api/status").json()["counts"]["items"]["total"] == 2


def test_status_requires_read_scope(tmp_path):
    _, c = _client(tmp_path, Principal("k", frozenset({"write"})))
    assert c.get("/api/status").status_code == 403


# ------------------------------------------------------------------ search


def test_fulltext_hits_with_snippets_and_kind_filter(tmp_path):
    store, c = _client(tmp_path, Principal("k", READ))
    _add(store, "Whale watching", "book")
    _add(store, "Whale news", "newspaper")
    body = c.get("/api/search?q=whale&mode=fulltext").json()
    assert body["mode"] == "fulltext"
    assert {h["title"] for h in body["hits"]} == {"Whale watching", "Whale news"}
    h = body["hits"][0]
    assert {"item_id", "title", "kind", "snippet", "score"} <= set(h)
    assert "[" in h["snippet"]
    only = c.get("/api/search?q=whale&mode=fulltext&kind=newspaper").json()
    assert [h["kind"] for h in only["hits"]] == ["newspaper"]
    assert len(c.get("/api/search?q=whale&mode=fulltext&limit=1").json()["hits"]) == 1


def test_visibility_scope_and_archived(tmp_path):
    store, c = _client(tmp_path, Principal("k", READ))
    _add(store, "Whale open", visibility="public")
    _add(store, "Whale hidden", visibility="private")
    gone = _add(store, "Whale archived", visibility="public")
    with store.connect() as conn:
        conn.execute("UPDATE items SET archived_at='2026-01-01T00:00:00Z' WHERE id=%s", (gone.id,))
    titles = [h["title"] for h in c.get("/api/search?q=whale").json()["hits"]]
    assert titles == ["Whale open"]
    _, c2 = _client(tmp_path / "p", Principal("k", READ, private=True))
    store2 = c2.app.state.store
    _add(store2, "Whale open")
    _add(store2, "Whale hidden", visibility="private")
    titles = {h["title"] for h in c2.get("/api/search?q=whale").json()["hits"]}
    assert titles == {"Whale open", "Whale hidden"}


def test_default_mode_depends_on_embedder(tmp_path):
    store, c = _client(tmp_path, Principal("k", READ), embedder=None)
    _add(store, "Whale song")
    assert c.get("/api/search?q=whale").json()["mode"] == "fulltext"
    store, c = _client(tmp_path / "e", Principal("k", READ), embedder=_embed)
    _add(store, "Whale song")
    assert c.get("/api/search?q=whale").json()["mode"] == "hybrid"


def test_semantic_unavailable_without_embedder(tmp_path):
    _, c = _client(tmp_path, Principal("k", READ), embedder=None)
    r = c.get("/api/search?q=whale&mode=semantic")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "semantic_unavailable"


def test_semantic_unavailable_when_gateway_down_and_hybrid_degrades(tmp_path):
    store, c = _client(tmp_path, Principal("k", READ), embedder=_down)
    _add(store, "Whale song")
    r = c.get("/api/search?q=whale&mode=semantic")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "semantic_unavailable"
    body = c.get("/api/search?q=whale&mode=hybrid").json()
    assert [h["title"] for h in body["hits"]] == ["Whale song"]


def test_semantic_hits(tmp_path):
    store, c = _client(tmp_path, Principal("k", READ), embedder=_embed)
    a = _add(store, "Leviathan", body=b"a whale tale")
    b = _add(store, "Tulips", body=b"flowers")
    ix.enqueue_missing(store)
    ix.Indexer(store, _embed).run_once()
    hits = c.get("/api/search?q=whale&mode=semantic").json()["hits"]
    assert hits[0]["item_id"] == a.id
    assert hits[0]["score"] > hits[1]["score"]
    assert b.id in [h["item_id"] for h in hits]


def test_search_validation_and_scope(tmp_path):
    _, c = _client(tmp_path, Principal("k", READ))
    assert c.get("/api/search").status_code == 422
    assert c.get("/api/search?q=x&mode=bogus").status_code == 422
    assert c.get("/api/search?q=x&kind=bogus").status_code == 422
    _, c = _client(tmp_path / "w", Principal("k", frozenset({"write"})))
    assert c.get("/api/search?q=x").status_code == 403


# ------------------------------------------------------------------ indexer owner


def test_indexer_started_without_embedder_for_body_fulltext(tmp_path, monkeypatch):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    store = Store(tmp_path / "d")
    idx = routes_search.start_indexer(store)
    try:
        assert idx is not None
        assert idx.embed is None
        assert idx._thread.is_alive()
    finally:
        routes_search.stop_indexer()


def test_indexer_started_and_stopped_with_embedder(tmp_path):
    store = Store(tmp_path / "d")
    idx = routes_search.start_indexer(store, embedder=_embed)
    try:
        assert idx is not None
        assert idx._thread.is_alive()
        assert routes_search.start_indexer(store, embedder=_embed) is idx  # idempotent
    finally:
        routes_search.stop_indexer()
    assert not idx._thread.is_alive()


# ------------------------------------------------------------------ body full-text, no gateway


def _wait_done(store, n, timeout=10.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ix.status(store)["done"] >= n:
            return
        time.sleep(0.02)
    raise AssertionError(f"indexer did not finish: {ix.status(store)}")


def test_epub_body_fulltext_without_gateway(tmp_path, monkeypatch):
    from tests.core.test_ingest import make_epub

    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("CULTURE_LIBRARY_PURGE_INTERVAL_S", "0")
    app = server_app.create_app()
    store = app.state.store = Store(tmp_path / "data")
    app.state.embedder = None
    app.dependency_overrides[current_principal] = lambda: Principal(
        "k", frozenset({"read", "write"}), private=True
    )
    epub = make_epub({"c1.xhtml": "<html><body><p>The mayor cut the ribbon</p></body></html>"})
    with TestClient(app) as c:  # lifespan starts the indexer even without a gateway
        r = c.post(
            "/api/library",
            files={"file": ("paper.epub", epub)},
            data={"title": "Morning Post", "kind": "newspaper"},
        )
        assert r.status_code == 201, r.text
        iid = r.json()["item"]["id"]
        _wait_done(store, 1)
        hits = c.get("/api/search?q=ribbon&mode=fulltext").json()["hits"]
        assert [h["item_id"] for h in hits] == [iid]
        assert "[ribbon]" in hits[0]["snippet"]
        r = c.get("/api/search?q=ribbon&mode=semantic")
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "semantic_unavailable"
    assert ix.status(store)["failed"] == 0
    assert ix.status(store)["last_error"] is None


def test_fulltext_over_200_items_under_one_second(tmp_path):
    import random
    import time

    store, c = _client(tmp_path, Principal("k", READ, private=True))
    rnd = random.Random(7)
    words = [f"w{n}" for n in range(3000)]
    items = []
    for n in range(200):
        body = " ".join(rnd.choices(words, k=4000))  # ~20 KB, newspaper-sized
        if n == 137:
            body += " zeppelin"
        res = store.add_item(
            body.encode(), kind="newspaper", title=f"Issue {n}", format="txt", visibility="public"
        )
        items.append((res.item, body))
    # Bodies go in exactly as the indexer writes them (search.set_body), in one transaction
    # to keep the setup quick; the indexer path itself is covered by the EPUB test above.
    with store.connect() as conn:
        for item, body in items:
            sr.set_body(conn, item, body)
    start = time.perf_counter()
    r = c.get("/api/search?q=zeppelin&mode=fulltext")
    elapsed = time.perf_counter() - start
    assert r.status_code == 200
    assert [h["title"] for h in r.json()["hits"]] == ["Issue 137"]
    start = time.perf_counter()
    r = c.get("/api/search?q=w42&mode=fulltext&limit=100")
    broad = time.perf_counter() - start
    print(
        f"fulltext 200 items: rare-term {elapsed * 1000:.1f} ms, common-term {broad * 1000:.1f} ms"
    )
    assert r.status_code == 200
    assert r.json()["hits"]
    assert elapsed < 1.0
    assert broad < 1.0
