"""Book and newspaper eval-set generation (test-only server routes for chunks and sources)."""

from __future__ import annotations

import hashlib

import pytest

pytest.importorskip("fastapi")
from fastapi import APIRouter  # noqa: E402

from culture_library.eval.config import JudgeConfig  # noqa: E402
from culture_library.eval.errors import EvalPolicyError  # noqa: E402
from culture_library.eval.generate import (  # noqa: E402
    GenerateError,
    generate_book_set,
    generate_newspaper_set,
)

from .conftest import FakeAdapter, FakeJudge  # noqa: E402

LOCAL = JudgeConfig(url="http://127.0.0.1:9", model="m")
REMOTE = JudgeConfig(url="https://api.example.com", model="m")


def _sha(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def _mount(library, chunks_by_item: dict, sources_by_item: dict, log: list):
    router = APIRouter()

    @router.get("/api/rag/items/{item_id}/chunks")
    def chunks(item_id: int):
        log.append(("chunks", item_id))
        rows = [
            {"chunk_no": i, "chunk_sha": _sha(t), "text": t, "is_header": i == 0}
            for i, t in enumerate(chunks_by_item[item_id])
        ]
        return {"item_id": item_id, "chunks": rows}

    library.app.include_router(router)
    for item_id, manifest in sources_by_item.items():  # seed through the REAL sources route
        library.client.put_sources(item_id, manifest)


TEXTS = ["HEADER"] + [
    f"Paragraph number {i} with some words.  Extra   spacing." for i in range(1, 31)
]


def test_book_samples_reference_item_chunks(library):
    item = library.add_item(b"book body", kind="book")
    _mount(library, {item.id: TEXTS}, {}, [])
    adapter = FakeAdapter()
    s = generate_book_set(library.client, adapter, FakeJudge(), LOCAL, item.id, samples=5, seed=3)
    shas = {_sha(t) for t in TEXTS[1:]}
    texts = set(TEXTS[1:])
    assert len(s.samples) == 5
    for smp in s.samples:
        assert set(smp.reference_chunk_shas) <= shas
        assert _sha(TEXTS[0]) not in smp.reference_chunk_shas
        assert all(c in texts for c in smp.reference_contexts)
    assert s.item_sha256 == item.sha256
    assert s.generator == {
        "samples": 5,
        "seed": 3,
        "adapter": "fake",
        "judge_model": "m",
        "dropped": 0,
    }


def test_book_deterministic_by_seed_and_default_20(library):
    item = library.add_item(b"b", kind="book")
    _mount(library, {item.id: TEXTS}, {}, [])
    run = lambda seed: [  # noqa: E731
        x.reference_chunk_shas
        for x in generate_book_set(
            library.client, FakeAdapter(), FakeJudge(), LOCAL, item.id, seed=seed
        ).samples
    ]
    assert run(1) == run(1)
    assert run(1) != run(2)
    assert len(run(1)) == 20


def test_bad_adapter_refs_are_dropped_and_contexts_made_verbatim(library):
    from culture_library.eval.models import Sample

    item = library.add_item(b"b", kind="book")
    _mount(library, {item.id: TEXTS}, {}, [])

    class Bad(FakeAdapter):
        def generate_samples(self, chunks, n, llm):
            good = chunks[0]
            return [
                Sample("q1", reference_chunk_shas=["nope"], reference_contexts=["x"]),
                Sample("q2", reference_chunk_shas=[good.chunk_sha], reference_contexts=["para"]),
            ]

    s = generate_book_set(library.client, Bad(), FakeJudge(), LOCAL, item.id, samples=3)
    assert [x.question for x in s.samples] == ["q2"]
    assert s.samples[0].reference_contexts[0] in TEXTS
    assert s.generator["dropped"] == 1


def test_egress_guard_runs_before_chunks_fetched(library):
    item = library.add_item(b"secret", kind="book", visibility="private")
    log: list = []
    _mount(library, {item.id: TEXTS}, {}, log)
    with pytest.raises(EvalPolicyError):
        generate_book_set(library.client, FakeAdapter(), FakeJudge(), REMOTE, item.id)
    assert log == []


def test_chunker_recorded_from_status(library, monkeypatch):
    item = library.add_item(b"b", kind="book")
    _mount(library, {item.id: TEXTS}, {}, [])
    s = generate_book_set(library.client, FakeAdapter(), FakeJudge(), LOCAL, item.id, samples=2)
    assert s.chunker == library.client.status().get("retrieval", {})
    monkeypatch.setattr(library.client, "status", lambda: {})
    s = generate_book_set(library.client, FakeAdapter(), FakeJudge(), LOCAL, item.id, samples=2)
    assert s.chunker == {}
    monkeypatch.setattr(library.client, "status", lambda: {"retrieval": {"chunk_chars": 800}})
    s = generate_book_set(library.client, FakeAdapter(), FakeJudge(), LOCAL, item.id, samples=2)
    assert s.chunker == {"chunk_chars": 800}


NEWS = [
    "HEADER",
    "Mayor opens   the NEW bridge today after years of delay.",
    "Storm hits the coast; thousands lose power overnight.",
    "Local team wins the final in extra time.",
]


def test_newspaper_requires_sources(library):
    item = library.add_item(b"paper", kind="newspaper")
    log: list = []
    _mount(library, {item.id: NEWS}, {}, log)
    with pytest.raises(GenerateError) as e:
        generate_newspaper_set(library.client, FakeAdapter(), FakeJudge(), LOCAL, item.id)
    assert e.value.hint
    assert log == []


def test_newspaper_maps_articles_to_chunks_with_urls(library):
    item = library.add_item(b"paper", kind="newspaper")
    sources = [
        {
            "article_title": "a",
            "excerpt": "mayor OPENS the new bridge",
            "source_urls": ["https://a.example/1"],
        },
        {
            "article_title": "a",
            "excerpt": "Storm hits the coast;  thousands",
            "source_urls": ["https://a.example/2"],
        },
        {
            "article_title": "a",
            "excerpt": "Not in the paper at all",
            "source_urls": ["https://a.example/3"],
        },
    ]
    _mount(library, {item.id: NEWS}, {item.id: sources}, [])
    s = generate_newspaper_set(library.client, FakeAdapter(), FakeJudge(), LOCAL, item.id)
    assert s.generator["unmatched"] == 1
    assert len(s.samples) == 2
    by_url = {x.source_urls[0]: x for x in s.samples}
    assert by_url["https://a.example/1"].reference_chunk_shas == [_sha(NEWS[1])]
    assert by_url["https://a.example/2"].reference_contexts == [NEWS[2]]
    assert all(x.source_urls for x in s.samples)


def test_newspaper_guard_before_chunks(library):
    item = library.add_item(b"paper", kind="newspaper", visibility="private")
    log: list = []
    _mount(
        library,
        {item.id: NEWS},
        {item.id: [{"article_title": "a", "excerpt": "x", "source_urls": ["https://a.example/u"]}]},
        log,
    )
    with pytest.raises(EvalPolicyError):
        generate_newspaper_set(library.client, FakeAdapter(), FakeJudge(), REMOTE, item.id)
    assert log == []
