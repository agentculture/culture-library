"""Search and RAG routes with the repodoc corpus (devague-work task t15, deviation d8).

``source`` keeps meaning fulltext|semantic|hybrid; the corpus is ``corpus`` and the filter is
``corpora``.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store  # noqa: E402
from culture_library.core import indexer as ix  # noqa: E402
from culture_library.core import search as sr  # noqa: E402
from culture_library.core.repodocs import RepoDocs  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server.auth import Principal, current_principal  # noqa: E402
from tests.core.search_helpers import vec  # noqa: E402

REMOTE = "https://github.com/agentculture/lobes-cli"
SHA = "a" * 40


def _embed(texts):
    return [vec(1.0, 0.0) if "whale" in t.lower() else vec(0.0, 1.0) for t in texts]


@pytest.fixture
def env(tmp_path):
    app = server_app.create_app()
    store = app.state.store = Store(tmp_path / "data")
    app.state.embedder = _embed
    app.state.query_embedder = _embed
    app.state.reranker = None
    principal = Principal(key_id="k", scopes=frozenset({"read"}), kinds=None, private=True)
    app.dependency_overrides[current_principal] = lambda: principal
    res = store.add_item(
        b"The whale surfaced near the harbour.",
        kind="book",
        title="Moby",
        format="txt",
        visibility="public",
    )
    with store.connect() as conn:
        sr.index_title(conn, res.item)
    RepoDocs(store).push(
        remote=REMOTE,
        path="docs/specs/s.md",
        commit=SHA,
        committed_at="2026-10-01T00:00:00Z",
        default_branch_head=SHA,
        visibility="public",
        content="# Whale spec\n\nThe whale protocol docks at noon.\n",
    )
    ix.enqueue_missing(store)
    ix.Indexer(store, _embed).run_once()
    return store, TestClient(app)


def test_search_defaults_to_items_and_repodocs(env):
    _store, http = env
    hits = http.get("/api/search", params={"q": "whale", "mode": "fulltext"}).json()["hits"]
    assert {h["corpus"] for h in hits} == {"item", "repodoc"}
    doc = next(h for h in hits if h["corpus"] == "repodoc")
    assert doc["source"] == "fulltext"
    assert doc["item_id"] is None
    assert doc["repo"]
    assert doc["path"] == "docs/specs/s.md"
    assert isinstance(doc["doc_id"], int)
    item = next(h for h in hits if h["corpus"] == "item")
    assert item["item_id"]
    assert item["title"] == "Moby"


@pytest.mark.parametrize("qs", ["corpora=item", "corpora=item&corpora=item"])
def test_search_corpora_narrows_to_items(env, qs):
    _store, http = env
    hits = http.get(f"/api/search?q=whale&mode=fulltext&{qs}").json()["hits"]
    assert hits
    assert {h["corpus"] for h in hits} == {"item"}


def test_search_corpora_comma_list_and_repodoc_only(env):
    _store, http = env
    both = http.get("/api/search?q=whale&mode=fulltext&corpora=item,repodoc").json()["hits"]
    assert {h["corpus"] for h in both} == {"item", "repodoc"}
    only = http.get("/api/search?q=whale&mode=fulltext&corpora=repodoc").json()["hits"]
    assert {h["corpus"] for h in only} == {"repodoc"}


def test_search_unknown_corpus_is_400(env):
    _store, http = env
    assert http.get("/api/search?q=whale&corpora=nope").status_code == 400


def test_rag_defaults_to_items_only_with_corpus_field(env):
    _store, http = env
    data = http.post("/api/rag/retrieve", json={"query": "whale", "k": 10}).json()
    assert data["passages"]
    assert {p["corpus"] for p in data["passages"]} == {"item"}
    assert set(data["passages"][0]) == {
        "item_id",
        "title",
        "kind",
        "chunk_no",
        "chunk_sha",
        "text",
        "score",
        "rerank_score",
        "source",
        "corpus",
    }


def test_rag_accepts_repodoc_corpus(env):
    _store, http = env
    body = {"query": "whale protocol", "k": 10, "corpora": ["item", "repodoc"]}
    passages = http.post("/api/rag/retrieve", json=body).json()["passages"]
    assert {p["corpus"] for p in passages} == {"item", "repodoc"}
    doc = next(p for p in passages if p["corpus"] == "repodoc")
    assert doc["item_id"] is None
    assert doc["repo"]
    assert doc["path"] == "docs/specs/s.md"
    assert isinstance(doc["doc_id"], int)


def test_rag_unknown_corpus_is_rejected(env):
    _store, http = env
    r = http.post("/api/rag/retrieve", json={"query": "whale", "corpora": ["nope"]})
    assert r.status_code in (400, 422)
