"""eval_store + sources: schema, cascade, visibility, comparability, and the kinds rebuild."""

from __future__ import annotations

import psycopg
import pytest

from culture_library.core import Store, eval_store
from culture_library.core.errors import NotFoundError, ValidationError

NEW_TABLES = (
    "eval_sets",
    "eval_samples",
    "eval_runs",
    "eval_run_samples",
)


def _seed(conn, item_id: int, sha: str) -> tuple[int, int]:
    """One set with two samples, one run with two run-samples."""
    set_id = eval_store.create_set(
        conn, item_id, sha, chunker={"size": 5}, adapter="md", adapter_version="1", generator={}
    )
    eval_store.add_samples(
        conn,
        set_id,
        [
            {"question": "q1", "reference_answer": "a1", "reference_chunk_shas": ["c1"]},
            {"question": "q2", "reference_answer": "a2"},
        ],
    )
    run_id = eval_store.create_run(
        conn, set_id, status="complete", config={"k": 3}, total=2, completed=2, aggregates={"m": 1}
    )
    for sample in eval_store.list_samples(conn, set_id):
        eval_store.add_run_sample(
            conn, run_id, sample["id"], retrieved_chunk_shas=["c1"], scores={"s": 0.5}
        )
    return set_id, run_id


def _counts(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in NEW_TABLES}


def _item(store: Store, n: int = 1, visibility: str = "public"):
    return store.add_item(
        f"body {n}".encode(), kind="document", title=f"T{n}", format="md", visibility=visibility
    ).item


def test_roundtrip_and_json_decoding(tmp_path):
    store = Store(tmp_path)
    item = _item(store)
    with store.connect() as conn:
        set_id, run_id = _seed(conn, item.id, item.sha256)
        s = eval_store.get_set(conn, set_id)
        assert s["chunker_json"] == {"size": 5}
        assert s["version"] == 1
        assert eval_store.list_samples(conn, set_id)[0]["reference_chunk_shas"] == ["c1"]
        assert eval_store.get_run(conn, run_id)["aggregates_json"] == {"m": 1}
        assert len(eval_store.list_run_samples(conn, run_id)) == 2
        with pytest.raises(NotFoundError):
            eval_store.get_set(conn, 999)


def test_aggregates_only_for_complete_runs(tmp_path):
    store = Store(tmp_path)
    item = _item(store)
    with store.connect() as conn:
        set_id, _ = _seed(conn, item.id, item.sha256)
        with pytest.raises(ValidationError):
            eval_store.create_run(
                conn, set_id, status="partial", config={}, total=2, completed=1, aggregates={"m": 1}
            )
        with pytest.raises(ValidationError):
            eval_store.create_run(conn, set_id, status="bogus", config={}, total=0, completed=0)
        run = eval_store.create_run(conn, set_id, status="partial", config={}, total=2, completed=1)
        assert eval_store.get_run(conn, run)["aggregates_json"] is None
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO eval_runs (set_id, set_version, status, config_json, aggregates_json,"
                " created_at) VALUES (%s, 1, 'failed', '{}', '{}', 'x')",
                (set_id,),
            )


def test_comparable_needs_same_set_and_version(tmp_path):
    store = Store(tmp_path)
    item, other = _item(store), _item(store, 2)
    with store.connect() as conn:
        set_id, run_a = _seed(conn, item.id, item.sha256)
        run_b = eval_store.create_run(
            conn, set_id, status="complete", config={}, total=0, completed=0
        )
        a, b = eval_store.get_run(conn, run_a), eval_store.get_run(conn, run_b)
        assert eval_store.comparable(a, b)
        assert (
            eval_store.replace_samples(conn, set_id, [{"question": "n", "reference_answer": "n"}])
            == 2
        )
        run_c = eval_store.create_run(
            conn, set_id, status="complete", config={}, total=0, completed=0
        )
        c = eval_store.get_run(conn, run_c)
        assert not eval_store.comparable(a, c)
        other_set, other_run = _seed(conn, other.id, other.sha256)
        assert not eval_store.comparable(a, eval_store.get_run(conn, other_run))


def test_visibility_filtering(tmp_path):
    store = Store(tmp_path)
    pub, priv = _item(store, 1), _item(store, 2, "private")
    with store.connect() as conn:
        _seed(conn, pub.id, pub.sha256)
        _seed(conn, priv.id, priv.sha256)
        conn.execute(
            "INSERT INTO item_uploaders (item_id, key_id) VALUES (%s, %s)", (priv.id, "key-a")
        )
    with store.connect() as conn:
        assert [s["item_id"] for s in eval_store.list_sets(conn)] == [pub.id]
        assert len(eval_store.list_sets(conn, include_private=True)) == 2
        assert [s["item_id"] for s in eval_store.list_sets(conn, owner="key-a")] == [
            pub.id,
            priv.id,
        ]
        assert len(eval_store.list_runs(conn)) == 1
        assert len(eval_store.list_runs(conn, include_private=True)) == 2
        assert len(eval_store.list_runs(conn, owner="key-a")) == 2
        assert len(eval_store.list_sets(conn, item_id=priv.id, include_private=True)) == 1


def test_item_delete_cascades_everything(tmp_path):
    store = Store(tmp_path)
    item = _item(store)
    with store.connect() as conn:
        _seed(conn, item.id, item.sha256)
        assert all(n > 0 for n in _counts(conn).values())
        conn.execute("DELETE FROM items WHERE id = %s", (item.id,))
        assert _counts(conn) == dict.fromkeys(NEW_TABLES, 0)


def test_purge_leaves_no_rows(tmp_path):
    from datetime import UTC, datetime, timedelta

    from culture_library.core import library

    store = Store(tmp_path)
    item = _item(store)
    with store.connect() as conn:
        _seed(conn, item.id, item.sha256)
    library.archive(store, item.id, include_private=True)
    assert library.purge_expired(store, datetime.now(UTC) + timedelta(days=365)) == [item.id]
    with store.connect() as conn:
        assert _counts(conn) == dict.fromkeys(NEW_TABLES, 0)


def test_sources_sidecar_roundtrip(tmp_path):
    from culture_library.core import sources  # owned by the library port, not this module

    store = Store(tmp_path)
    item = _item(store)
    with store.connect() as conn:
        sources.put(conn, item.id, {"urls": ["https://example.test/a"]})
        assert sources.get(conn, item.id) == {"urls": ["https://example.test/a"]}
        assert sources.get(conn, 999) is None
