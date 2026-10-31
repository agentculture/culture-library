"""Shared helpers for the search tests: fake embeddings and direct index writes.

The indexer is not ported to Postgres yet (plan task t13), so these tests fill
``item_text``/``repodoc_text`` and the vector tables through :mod:`culture_library.core.search`'s
write helpers, chunked the way the indexer chunks (chunk 0 is the title/author header).
The fake vectors have the current strategy's 768 dimensions, so the migration's HNSW index
serves them; no gateway is ever contacted.
"""

from __future__ import annotations

from culture_library.core import embedding_strategy as es
from culture_library.core import search as sr
from culture_library.core import uploaders

DIM = es.CURRENT_STRATEGY.dimension
PAIR = {"model_id": es.CURRENT_MODEL_ID, "strategy": es.CURRENT_STRATEGY}


def vec(*head: float) -> list[float]:
    """A 768-d vector starting with ``head``, zero-padded."""
    return [float(x) for x in head] + [0.0] * (DIM - len(head))


def fake_embed(texts):
    """Deterministic vectors: cats / dogs / other."""
    out = []
    for t in texts:
        low = t.lower()
        out.append(vec(float("cat" in low or "feline" in low), float("dog" in low), 0.1))
    return out


class DownEmbed:
    def __init__(self):
        self.up = False
        self.calls = 0

    def __call__(self, texts):
        self.calls += 1
        if not self.up:
            raise ConnectionRefusedError("gateway down")
        return fake_embed(texts)


def add(store, title, body="", *, vis="public", kind="document", fmt="md", owner=None, author=""):
    """Add an item and make its title searchable in the add transaction (as library.add does)."""
    data = body if isinstance(body, bytes) and body else (body or title)
    data = data.encode() if isinstance(data, str) else data

    def on_insert(conn, item):
        sr.index_title(conn, item)
        if owner:
            uploaders.record(conn, item.id, owner)

    res = store.add_item(
        data, kind=kind, title=title, format=fmt, visibility=vis, author=author, on_insert=on_insert
    )
    return res.item


def index(store, item, body=None, embed=fake_embed, **pair):
    """Write an item's body and (with ``embed``) its chunk vectors, as the indexer would."""
    text = store.read_bytes(item.id).decode() if body is None else body
    with store.connect() as conn:
        sr.set_body(conn, item, text)
    if embed is None:
        return
    chunks = [f"{item.title}\n{item.author}".strip()] + sr.chunk_text(text)
    with store.connect() as conn:
        sr.set_embeddings(conn, item.id, chunks, embed(chunks), **(pair or PAIR))


def index_doc(store, doc, title, body, *, visibility=None, embed=fake_embed, **pair):
    """Write a repo doc's full text and (with ``embed``) its chunk vectors."""
    vis = visibility or doc.visibility
    with store.connect() as conn:
        sr.set_repodoc_text(conn, doc.id, title, doc.path, body, vis)
    if embed is None:
        return
    chunks = [f"{title}\n{doc.path}".strip()] + sr.chunk_text(body)
    with store.connect() as conn:
        sr.set_repodoc_embeddings(conn, doc.id, chunks, embed(chunks), **(pair or PAIR))


def count(store, sql, params=()):
    with store.reader() as conn:
        return conn.execute(sql, params).fetchone()[0]


_SHIPPED_HNSW = {
    es.hnsw_index_name(t, es.CURRENT_MODEL_ID, es.CURRENT_STRATEGY)
    for t in ("embeddings", "repodoc_embeddings")
}


def drop_created_hnsw_indexes(store):
    """Drop the HNSW indexes a test's new (model id, strategy) pair created.

    Indexes outlive the per-test truncate, so a test that indexes under another pair calls
    this afterwards and the worker database keeps only the migration's
    (tests/server/test_status_database.py counts them).
    """
    with store.connect() as conn:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
                " AND indexname LIKE %s",
                ("%\\_hnsw\\_%",),
            )
        ]
        for name in sorted(set(names) - _SHIPPED_HNSW):
            conn.execute(f'DROP INDEX IF EXISTS "{name}"')
