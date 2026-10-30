"""RepoDocs.push/tombstone ``on_write`` hook (deviation d6, plan task t10).

The hook runs on push's own connection, inside its transaction, so whatever it writes (the
mirror outbox row, the repodoc feed event) commits or rolls back with the version/draft.
"""

from __future__ import annotations

import pytest

from culture_library.core import Store, feed, mirror
from culture_library.core.repodocs import WORKING, PushResult, RepoDocs

A = "a" * 40
REMOTE = "https://github.com/agentculture/devague"


class Boom(RuntimeError):
    pass


def _counts(store: Store) -> dict[str, int]:
    with store.reader() as conn:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # nosec B608
            for t in ("repodocs", "repodoc_versions", "repodoc_drafts", "mirror_outbox", "feed")
        }


def _write_both(conn, result: PushResult) -> None:
    sha = result.version.sha256 if result.version else result.draft.sha256
    commit = result.version.commit if result.version else WORKING
    mirror.enqueue(conn, mirror.key_for(result.doc.repo, result.doc.path, commit), sha)
    feed.append_repodoc(
        conn,
        result.doc.id,
        result.doc.kind,
        sha,
        {"repo": result.doc.repo, "path": result.doc.path, "commit": commit},
    )


def _push(docs: RepoDocs, **kw):
    args = dict(
        remote=REMOTE,
        path="docs/specs/x.md",
        commit=A,
        committed_at="2026-10-01T00:00:00Z",
        default_branch_head=A,
        content="hello\n",
    )
    args.update(kw)
    return docs.push(**args)


def test_hook_sees_result_and_commits_with_the_version(tmp_path) -> None:
    store = Store(tmp_path / "d")
    seen = []

    def hook(conn, result):
        seen.append(result)
        _write_both(conn, result)

    result = _push(RepoDocs(store), on_write=hook)
    assert seen == [result]
    assert _counts(store) == {
        "repodocs": 1,
        "repodoc_versions": 1,
        "repodoc_drafts": 0,
        "mirror_outbox": 1,
        "feed": 1,
    }


@pytest.mark.parametrize("commit", [A, WORKING])
def test_hook_raising_rolls_back_everything(tmp_path, commit) -> None:
    store = Store(tmp_path / "d")

    def hook(conn, result):
        _write_both(conn, result)
        raise Boom("after the outbox and feed writes")

    with pytest.raises(Boom):
        _push(RepoDocs(store), commit=commit, on_write=hook)
    assert _counts(store) == {
        "repodocs": 0,
        "repodoc_versions": 0,
        "repodoc_drafts": 0,
        "mirror_outbox": 0,
        "feed": 0,
    }


def test_hook_raising_keeps_the_previous_draft(tmp_path) -> None:
    store = Store(tmp_path / "d")
    docs = RepoDocs(store)
    first = _push(docs, commit=WORKING, content="one\n")

    def hook(conn, result):
        raise Boom()

    with pytest.raises(Boom):
        _push(docs, commit=WORKING, content="two\n", on_write=hook)
    assert docs.draft(first.doc.id, include_private=True).sha256 == first.draft.sha256


def test_tombstone_hook_runs_in_the_transaction(tmp_path) -> None:
    store = Store(tmp_path / "d")
    docs = RepoDocs(store)
    _push(docs, commit=WORKING)

    def hook(conn, doc):
        assert doc.removed_commit == A
        feed.append_repodoc(
            conn,
            doc.id,
            doc.kind,
            "0" * 64,
            {"repo": doc.repo, "path": doc.path, "commit": A},
            type="archived",
        )
        raise Boom()

    with pytest.raises(Boom):
        docs.tombstone(REMOTE, "docs/specs/x.md", A, on_write=hook)
    counts = _counts(store)
    assert counts["feed"] == 0
    assert counts["repodoc_drafts"] == 1  # the drop rolled back too
    assert docs.list(include_private=True)[0].removed_at is None

    calls = []
    docs.tombstone(REMOTE, "docs/specs/x.md", A, on_write=lambda c, d: calls.append(d))
    assert calls and calls[0].removed_commit == A


def test_no_hook_is_the_old_behaviour(tmp_path) -> None:
    store = Store(tmp_path / "d")
    result = _push(RepoDocs(store))
    assert result.created
    assert _counts(store)["mirror_outbox"] == 0
    assert _counts(store)["feed"] == 0
