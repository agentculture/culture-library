"""RepoDocs.push/tombstone ``on_write`` hook (deviation d6, plan task t10).

The hook runs on push's own connection, inside its transaction, so whatever it writes (the
mirror outbox row, the repodoc feed event) commits or rolls back with the version/draft.

On Postgres (plan library-on-postgres-and-pgvector, task t11) the forced-error tests write the
side rows with plain SQL, so they hold without the feed and mirror modules; the tests that go
through ``feed.append_repodoc`` and ``mirror.enqueue`` import those modules lazily.
"""

from __future__ import annotations

import pytest

from culture_library.core import Store
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


def _modules():
    from culture_library.core import feed, mirror

    return feed, mirror


def _write_both(conn, result: PushResult) -> None:
    feed, mirror = _modules()
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


def _write_side_rows_sql(conn, result: PushResult) -> None:
    """The outbox row and feed event as plain SQL, for tests independent of feed/mirror."""
    sha = result.version.sha256 if result.version else result.draft.sha256
    commit = result.version.commit if result.version else WORKING
    conn.execute(
        "INSERT INTO mirror_outbox (key, sha256, enqueued_at) VALUES (%s, %s, %s)",
        (f"{result.doc.repo}/{result.doc.path}@{commit}", sha, "2026-10-31T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO feed (type, item_id, sha256, kind, at, data, subject)"
        " VALUES ('updated', %s, %s, %s, %s, '{}'::jsonb, 'repodoc')",
        (result.doc.id, sha, result.doc.kind, "2026-10-31T00:00:00+00:00"),
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

    docs = RepoDocs(store)
    with pytest.raises(Boom):
        _push(docs, commit=commit, on_write=hook)
    assert _counts(store) == {
        "repodocs": 0,
        "repodoc_versions": 0,
        "repodoc_drafts": 0,
        "mirror_outbox": 0,
        "feed": 0,
    }


@pytest.mark.parametrize("commit", [A, WORKING])
def test_forced_error_after_the_version_insert_leaves_nothing(tmp_path, commit) -> None:
    """Criterion 1 (t11): the hook sees the version (or draft) row on its connection, writes
    the outbox row and feed event, then fails: no doc, version, draft, outbox row or feed
    event remains."""
    store = Store(tmp_path / "d")
    seen = {}

    def hook(conn, result):
        table = "repodoc_versions" if commit != WORKING else "repodoc_drafts"
        seen["rows"] = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE doc_id = %s", (result.doc.id,)  # nosec B608
        ).fetchone()[0]
        _write_side_rows_sql(conn, result)
        seen["side"] = conn.execute(
            "SELECT (SELECT COUNT(*) FROM feed) + (SELECT COUNT(*) FROM mirror_outbox)"
        ).fetchone()[0]
        raise Boom("after the version insert")

    with pytest.raises(Boom):
        _push(RepoDocs(store), commit=commit, on_write=hook)
    assert seen == {"rows": 1, "side": 2}  # written inside the transaction, then undone
    assert _counts(store) == dict.fromkeys(
        ("repodocs", "repodoc_versions", "repodoc_drafts", "mirror_outbox", "feed"), 0
    )


def test_forced_error_on_a_second_version_keeps_the_first(tmp_path) -> None:
    store = Store(tmp_path / "d")
    docs = RepoDocs(store)
    first = _push(docs, on_write=_write_side_rows_sql)
    B = "b" * 40

    def hook(conn, result):
        _write_side_rows_sql(conn, result)
        raise Boom()

    with pytest.raises(Boom):
        _push(
            docs,
            commit=B,
            committed_at="2026-10-02T00:00:00Z",
            default_branch_head=B,
            content="two\n",
            on_write=hook,
        )
    assert _counts(store) == {
        "repodocs": 1,
        "repodoc_versions": 1,
        "repodoc_drafts": 0,
        "mirror_outbox": 1,
        "feed": 1,
    }
    after = docs.get(first.doc.id, include_private=True)
    assert after.current_commit == A
    assert after.updated_at == first.doc.updated_at


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
        feed, _ = _modules()
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
    assert calls
    assert calls[0].removed_commit == A


def test_no_hook_is_the_old_behaviour(tmp_path) -> None:
    store = Store(tmp_path / "d")
    result = _push(RepoDocs(store))
    assert result.created
    assert _counts(store)["mirror_outbox"] == 0
    assert _counts(store)["feed"] == 0
