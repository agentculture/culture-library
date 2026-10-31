"""A repodoc passage (item_id null, deviation d8) parses instead of crashing the client."""

from culture_library.eval.models import Passage


def test_repodoc_passage_parses_with_null_item_id():
    p = Passage.from_dict(
        {"item_id": None, "chunk_sha": "ab", "corpus": "repodoc", "doc_id": 7, "source": "hybrid"}
    )
    assert p.item_id is None
    assert p.corpus == "repodoc"
    assert p.doc_id == 7
    assert p.source == "hybrid"


def test_item_passage_unchanged():
    p = Passage.from_dict({"item_id": "3", "chunk_sha": "cd"})
    assert p.item_id == 3
    assert p.corpus == "item"
    assert p.doc_id is None
