"""Book and newspaper eval-set generation, against the real chunks and sources routes."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from culture_library.core.indexer import Indexer, enqueue  # noqa: E402
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


def _para(i: int, lead: str = "Paragraph") -> str:
    # ~600 chars so the chunker (max 1000) keeps one paragraph per chunk
    return f"{lead} number {i} opens here. " + ("filler words fill the line. " * 20).strip()


def _embed(texts):
    return [[1.0, 0.0] for _ in texts]


def _index_item(library, paragraphs, *, kind="book", **kw):
    body = "\n\n".join(paragraphs).encode()
    item = library.add_item(body, kind=kind, **kw)
    with library.store.connect() as conn:
        enqueue(conn, item.id)
    Indexer(library.store, _embed, model="fake-embed").run_once()
    return item


def _chunks(library, item):
    return library.client.get_chunks(item.id)


def test_book_samples_reference_item_chunks(library):
    item = _index_item(library, [_para(i) for i in range(30)], title="Big Book")
    all_chunks = _chunks(library, item)
    assert len(all_chunks) > 20
    shas = {c.chunk_sha for c in all_chunks if not c.is_header}
    texts = {c.text for c in all_chunks if not c.is_header}
    header = [c.chunk_sha for c in all_chunks if c.is_header]
    s = generate_book_set(
        library.client, FakeAdapter(), FakeJudge(), LOCAL, item.id, samples=5, seed=3
    )
    assert len(s.samples) == 5
    for smp in s.samples:
        assert set(smp.reference_chunk_shas) <= shas
        assert not set(smp.reference_chunk_shas) & set(header)
        assert all(c in texts for c in smp.reference_contexts)
    assert s.item_sha256 == item.sha256
    assert s.generator == {
        "samples": 5,
        "seed": 3,
        "adapter": "fake",
        "judge_model": "m",
        "dropped": 0,
        "skipped_boilerplate": 0,
    }


def test_book_deterministic_by_seed_and_default_20(library):
    item = _index_item(library, [_para(i) for i in range(30)])

    def run(seed):
        return [
            x.reference_chunk_shas
            for x in generate_book_set(
                library.client, FakeAdapter(), FakeJudge(), LOCAL, item.id, seed=seed
            ).samples
        ]

    first, again, other = run(1), run(1), run(2)
    assert first == again
    assert first != other
    assert len(first) == 20


def test_bad_adapter_refs_are_dropped_and_contexts_made_verbatim(library):
    from culture_library.eval.models import Sample

    item = _index_item(library, [_para(i) for i in range(6)])
    texts = {c.text for c in _chunks(library, item)}

    class Bad(FakeAdapter):
        def generate_samples(self, chunks, n, llm):
            good = chunks[0]
            return [
                Sample("q1", reference_chunk_shas=["nope"], reference_contexts=["x"]),
                Sample("q2", reference_chunk_shas=[good.chunk_sha], reference_contexts=["para"]),
            ]

    s = generate_book_set(library.client, Bad(), FakeJudge(), LOCAL, item.id, samples=3)
    assert [x.question for x in s.samples] == ["q2"]
    assert s.samples[0].reference_contexts[0] in texts
    assert s.generator["dropped"] == 1


def test_gutenberg_licence_chunks_are_never_sampled(library):
    licence = "The Project Gutenberg License applies to this eBook. " + "Terms of use. " * 30
    item = _index_item(library, [_para(i) for i in range(4)] + [licence.strip()])
    s = generate_book_set(library.client, FakeAdapter(), FakeJudge(), LOCAL, item.id, samples=10)
    assert len(s.samples) == 4
    assert not any("Gutenberg" in c for x in s.samples for c in x.reference_contexts)
    assert s.generator["skipped_boilerplate"] == 1


def test_all_boilerplate_book_still_generates(library):
    item = _index_item(library, ["Project Gutenberg " + _para(i) for i in range(3)])
    s = generate_book_set(library.client, FakeAdapter(), FakeJudge(), LOCAL, item.id, samples=3)
    assert len(s.samples) == 3
    assert s.generator["skipped_boilerplate"] == 0


def test_egress_guard_runs_before_chunks_fetched(library, monkeypatch):
    item = _index_item(library, [_para(i) for i in range(3)], visibility="private")
    fetched: list = []
    real = library.client.get_chunks
    monkeypatch.setattr(library.client, "get_chunks", lambda i: fetched.append(i) or real(i))
    adapter, judge = FakeAdapter(), FakeJudge()
    with pytest.raises(EvalPolicyError):
        generate_book_set(library.client, adapter, judge, REMOTE, item.id)
    assert fetched == []


def test_chunker_recorded_from_status(library, monkeypatch):
    item = _index_item(library, [_para(i) for i in range(4)])
    s = generate_book_set(library.client, FakeAdapter(), FakeJudge(), LOCAL, item.id, samples=2)
    assert s.chunker == library.client.status().get("retrieval", {})
    monkeypatch.setattr(library.client, "status", lambda: {})
    s = generate_book_set(library.client, FakeAdapter(), FakeJudge(), LOCAL, item.id, samples=2)
    assert s.chunker == {}


ARTICLES = [_para(i, lead=w) for i, w in enumerate(["Mayor", "Storm", "Team"])]


def _manifest(*pairs):
    return [{"article_title": "a", "excerpt": e, "source_urls": [u]} for e, u in pairs]


def test_newspaper_requires_sources(library, monkeypatch):
    item = _index_item(library, ARTICLES, kind="newspaper")
    fetched: list = []
    monkeypatch.setattr(library.client, "get_chunks", lambda i: fetched.append(i))
    adapter, judge = FakeAdapter(), FakeJudge()
    with pytest.raises(GenerateError) as e:
        generate_newspaper_set(library.client, adapter, judge, LOCAL, item.id)
    assert e.value.hint
    assert fetched == []


def test_newspaper_maps_articles_to_chunks_with_urls(library):
    item = _index_item(library, ARTICLES, kind="newspaper")
    by_text = {c.text: c for c in _chunks(library, item)}
    library.client.put_sources(
        item.id,
        _manifest(
            (ARTICLES[0][:60].upper().replace(" ", "   "), "https://a.example/1"),
            (ARTICLES[1][:80], "https://a.example/2"),
            ("Not in the paper at all", "https://a.example/3"),
        ),
    )
    s = generate_newspaper_set(library.client, FakeAdapter(), FakeJudge(), LOCAL, item.id)
    assert s.generator["unmatched"] == 1
    assert len(s.samples) == 2
    by_url = {x.source_urls[0]: x for x in s.samples}
    assert by_url["https://a.example/1"].reference_chunk_shas == [by_text[ARTICLES[0]].chunk_sha]
    assert by_url["https://a.example/2"].reference_contexts == [ARTICLES[1]]
    assert all(x.source_urls for x in s.samples)


def test_newspaper_guard_before_chunks(library, monkeypatch):
    item = _index_item(library, ARTICLES, kind="newspaper", visibility="private")
    library.client.put_sources(item.id, _manifest((ARTICLES[0][:60], "https://a.example/u")))
    fetched: list = []
    monkeypatch.setattr(library.client, "get_chunks", lambda i: fetched.append(i))
    adapter, judge = FakeAdapter(), FakeJudge()
    with pytest.raises(EvalPolicyError):
        generate_newspaper_set(library.client, adapter, judge, REMOTE, item.id)
    assert fetched == []
