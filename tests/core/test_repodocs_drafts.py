"""Repo documents: working drafts, tombstones and binary content (devague-work plan task t1)."""

from __future__ import annotations

import sqlite3

import pytest

from culture_library.core import Store, ValidationError
from culture_library.core.repodocs import NotFoundError, RepoDocs

A = "a" * 40
B = "b" * 40
REMOTE = "https://github.com/agentculture/lobes-cli"
PATH = ".devague/frames/x.json"


@pytest.fixture
def docs(store: Store) -> RepoDocs:
    return RepoDocs(store)


def _commit(docs: RepoDocs, content="v-a\n", commit=A, **kw):
    args = dict(
        remote=REMOTE,
        path=PATH,
        commit=commit,
        committed_at="2026-10-01T00:00:00Z",
        default_branch_head=commit,
        visibility="public",
        content=content,
    )
    args.update(kw)
    return docs.push(**args)


def _working(docs: RepoDocs, content="w-1\n", **kw):
    args = dict(remote=REMOTE, path=PATH, commit="working", visibility="public", content=content)
    args.update(kw)
    return docs.push(**args)


def _count(store: Store, table: str) -> int:
    conn = sqlite3.connect(store.db_path)
    try:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # nosec B608
    finally:
        conn.close()


# --- working drafts -----------------------------------------------------------------


def test_working_push_upserts_one_draft_and_no_version(docs: RepoDocs, store: Store) -> None:
    first = _working(docs, "w-1\n")
    assert first.created
    assert first.version is None
    assert first.draft is not None
    assert not first.current_changed
    second = _working(docs, "w-2\n")
    assert second.created
    assert second.doc.id == first.doc.id
    assert _count(store, "repodoc_drafts") == 1
    assert _count(store, "repodoc_versions") == 0
    assert docs.content(first.doc.id, "working", include_private=True) == b"w-2\n"
    draft = docs.draft(first.doc.id, include_private=True)
    assert draft is not None
    assert draft.size == 4
    assert draft.ahead


def test_working_push_needs_no_commit_metadata(docs: RepoDocs) -> None:
    r = docs.push(remote=REMOTE, path=PATH, commit="working", content="w\n")
    assert r.doc.visibility == "private"  # fails closed like a commit push
    assert r.doc.current_commit is None


def test_identical_working_repush_is_not_created(docs: RepoDocs) -> None:
    _working(docs, "same\n")
    again = _working(docs, "same\n")
    assert not again.created


def test_drafts_keyed_by_doc_not_checkout(docs: RepoDocs, store: Store) -> None:
    # a clone and a worktree push the same repo/path with different remote spellings
    _working(docs, "from clone\n", remote="git@github.com:agentculture/lobes-cli.git")
    _working(docs, "from worktree\n", remote="https://github.com/agentculture/lobes-cli")
    assert _count(store, "repodoc_drafts") == 1
    assert _count(store, "repodocs") == 1


def test_draft_table_has_sha256_column(store: Store) -> None:
    conn = sqlite3.connect(store.db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(repodoc_drafts)")}
    finally:
        conn.close()
    assert {"doc_id", "sha256", "size", "pushed_at"} <= cols


def test_commit_push_after_draft_leaves_draft_untouched(docs: RepoDocs, store: Store) -> None:
    w = _working(docs, "w-1\n")
    before = docs.draft(w.doc.id, include_private=True)
    c = _commit(docs, "v-a\n")
    assert c.created
    assert c.version is not None
    assert c.doc.id == w.doc.id
    assert _count(store, "repodoc_versions") == 1
    assert docs.draft(w.doc.id, include_private=True).sha256 == before.sha256
    assert docs.content(w.doc.id, "working", include_private=True) == b"w-1\n"
    assert docs.content(w.doc.id, include_private=True) == b"v-a\n"


def test_spec_scenario_two_working_one_commit(docs: RepoDocs, store: Store) -> None:
    _working(docs, "w-1\n")
    _working(docs, "w-2\n")
    _commit(docs, "v-a\n")
    assert _count(store, "repodoc_drafts") == 1
    assert _count(store, "repodoc_versions") == 1
    doc = docs.find(REMOTE, PATH, include_private=True)
    assert docs.content(doc.id, "working", include_private=True) == b"w-2\n"


def test_draft_equal_to_newest_version_is_not_ahead(docs: RepoDocs) -> None:
    w = _working(docs, "v-a\n")
    assert docs.draft(w.doc.id, include_private=True).ahead
    _commit(docs, "v-a\n")
    assert not docs.draft(w.doc.id, include_private=True).ahead
    _working(docs, "v-b\n")
    assert docs.draft(w.doc.id, include_private=True).ahead


def test_doc_without_draft(docs: RepoDocs) -> None:
    c = _commit(docs)
    assert docs.draft(c.doc.id, include_private=True) is None
    with pytest.raises(NotFoundError):
        docs.content(c.doc.id, "working", include_private=True)


def test_private_draft_hidden_from_public_reader(docs: RepoDocs) -> None:
    c = _commit(docs, "v-a\n")  # public doc
    _working(docs, "secret wip\n", visibility="private")
    assert docs.draft(c.doc.id, include_private=False) is None
    with pytest.raises(NotFoundError):
        docs.content(c.doc.id, "working", include_private=False)
    assert docs.draft(c.doc.id, include_private=True) is not None
    assert docs.content(c.doc.id, include_private=False) == b"v-a\n"


def test_draft_only_doc_takes_draft_visibility(docs: RepoDocs) -> None:
    w = _working(docs, "w\n", visibility="private")
    assert docs.get(w.doc.id, include_private=False) is None
    assert docs.list(include_private=False) == []


def test_working_value_is_validated(docs: RepoDocs) -> None:
    for bad in ("work", "WORKING-tree", ""):
        with pytest.raises(ValidationError):
            docs.push(remote=REMOTE, path=PATH, commit=bad, content="x")
    with pytest.raises(ValidationError):  # a commit push still needs its metadata
        docs.push(remote=REMOTE, path=PATH, commit=A, content="x")


# --- tombstones -----------------------------------------------------------------------


def test_tombstone_marks_removed_drops_draft_keeps_versions(docs: RepoDocs, store: Store) -> None:
    c = _commit(docs, "v-a\n")
    _working(docs, "w\n")
    doc = docs.tombstone(REMOTE, PATH, B)
    assert doc.id == c.doc.id
    assert doc.removed_at is not None
    assert doc.removed_commit == B
    assert _count(store, "repodoc_drafts") == 0
    assert docs.draft(doc.id, include_private=True) is None
    assert [v.commit for v in docs.versions(doc.id, include_private=True)] == [A]
    assert docs.content(doc.id, A, include_private=True) == b"v-a\n"
    assert docs.get(doc.id, include_private=True).removed_commit == B


def test_removed_docs_excluded_from_list_unless_asked(docs: RepoDocs) -> None:
    _commit(docs)
    keep = _commit(docs, path="docs/specs/keep.md")
    docs.tombstone(REMOTE, PATH, B)
    assert [d.id for d in docs.list(include_private=True)] == [keep.doc.id]
    assert docs.find(REMOTE, PATH, include_private=True) is None
    every = docs.list(include_private=True, include_removed=True)
    assert len(every) == 2
    assert {d.path for d in every if d.removed_at} == {PATH}


def test_tombstone_of_never_committed_draft(docs: RepoDocs, store: Store) -> None:
    w = _working(docs, "w\n")
    doc = docs.tombstone(REMOTE, PATH, "working")
    assert doc.id == w.doc.id
    assert doc.removed_commit == "working"
    assert _count(store, "repodoc_drafts") == 0


def test_tombstone_validation(docs: RepoDocs) -> None:
    with pytest.raises(NotFoundError):
        docs.tombstone(REMOTE, "docs/specs/never.md", A)
    _commit(docs)
    with pytest.raises(ValidationError):
        docs.tombstone(REMOTE, PATH, "abc")


def test_new_push_after_tombstone_restores_doc(docs: RepoDocs) -> None:
    _commit(docs, "v-a\n")
    docs.tombstone(REMOTE, PATH, B)
    # a replayed push of an already-stored version does not resurrect the doc
    _commit(docs, "v-a\n")
    assert docs.find(REMOTE, PATH, include_private=True) is None
    # a genuinely new version (the file came back) does
    _commit(docs, "v-c\n", commit="c" * 40, committed_at="2026-10-03T00:00:00Z")
    back = docs.find(REMOTE, PATH, include_private=True)
    assert back is not None
    assert back.removed_at is None
    assert back.removed_commit is None


def test_working_push_after_tombstone_restores_doc(docs: RepoDocs) -> None:
    _commit(docs)
    docs.tombstone(REMOTE, PATH, B)
    _working(docs, "new wip\n")
    assert docs.find(REMOTE, PATH, include_private=True).removed_at is None


# --- binary content -------------------------------------------------------------------


def test_binary_content_round_trips_byte_identical(docs: RepoDocs) -> None:
    png = bytes(range(256)) + b"\x89PNG\r\n\x1a\n\xff\xfe\x00"
    c = _commit(docs, png, path="docs/design/reference/x.png")
    assert c.version.size == len(png)
    assert docs.content(c.doc.id, include_private=True) == png
    w = _working(docs, png[::-1], path="docs/design/reference/x.png")
    assert docs.content(w.doc.id, "working", include_private=True) == png[::-1]


def test_existing_db_gains_removed_columns(tmp_path) -> None:
    data = tmp_path / "data"
    Store(data)
    conn = sqlite3.connect(data / "library.db", isolation_level=None)
    conn.execute("DROP TABLE repodoc_drafts")
    conn.execute("ALTER TABLE repodocs DROP COLUMN removed_at")
    conn.execute("ALTER TABLE repodocs DROP COLUMN removed_commit")
    conn.close()
    store = Store(data)
    r = RepoDocs(store)
    _commit(r)
    assert r.tombstone(REMOTE, PATH, B).removed_commit == B
