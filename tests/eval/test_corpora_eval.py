"""Eval runs record ``corpora`` and are unaffected by indexing repo docs (task t15)."""

from __future__ import annotations

from culture_library.core import indexer as ix
from culture_library.core import search as sr
from culture_library.core.repodocs import RepoDocs
from culture_library.eval.models import EvalSet, Sample
from culture_library.eval.runner import run_set

from .conftest import FakeAdapter

SHA = "a" * 40


def _embed(texts):
    return [[1.0, 0.0] if "whale" in t.lower() else [0.0, 1.0] for t in texts]


def _run(library, eval_set):
    return run_set(
        library.client,
        eval_set,
        FakeAdapter(),
        llm=None,
        mode="hybrid",
        k=5,
        rerank=False,
        suites=["id_retrieval"],
    )


def test_run_config_records_corpora_and_item_ids_survive_repodoc_indexing(library):
    library.app.state.query_embedder = _embed
    library.app.state.reranker = None
    item = library.add_item(b"The whale surfaced near the harbour at dawn.", title="Moby")
    with library.store.connect() as conn:
        sr.index_title(conn, item)
    ix.enqueue_missing(library.store)
    ix.Indexer(library.store, _embed).run_once()
    sha = library.client.get_chunks(item.id)[-1].chunk_sha
    eval_set = EvalSet(
        item_id=item.id,
        item_sha256=item.sha256,
        adapter="fake",
        adapter_version="0",
        samples=[Sample(question="whale harbour", reference_chunk_shas=[sha])],
    )

    before = _run(library, eval_set)
    assert before.config["corpora"] == ["item"]
    ids_before = [r.retrieved_item_ids for r in before.samples]

    RepoDocs(library.store).push(
        remote="https://github.com/agentculture/x",
        path="docs/specs/w.md",
        commit=SHA,
        committed_at="2026-10-01T00:00:00Z",
        default_branch_head=SHA,
        visibility="public",
        content="# Whale\n\nwhale harbour whale harbour dawn\n",
    )
    ix.Indexer(library.store, _embed).run_once()

    after = _run(library, eval_set)
    ids_after = [r.retrieved_item_ids for r in after.samples]
    assert ids_before[0] and ids_before == ids_after
    assert after.config["corpora"] == ["item"]
