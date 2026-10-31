"""Build a SQLite library.db shaped like live 0.19.8, from the frozen schema fixture.

The schema is ``fixtures/sqlite_schema_0.19.8.sql`` minus the FTS5 shadow tables, which
``CREATE VIRTUAL TABLE ... USING fts5`` creates itself. :func:`build_source` then inserts
representative rows into every table (the 0.19.8 SQLite triggers fill ``repodoc_dirty``).
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import struct
from array import array
from pathlib import Path

SCHEMA = Path(__file__).parent / "fixtures" / "sqlite_schema_0.19.8.sql"
LABEL = "gemma2-embed+Retrieval-document"
DIM = 768
FTS_SHADOWS = ("_config", "_content", "_data", "_docsize", "_idx")

_HEADER = re.compile(r"^-- (table|index|trigger) (\S+)$", re.M)


def schema_statements() -> list[str]:
    """The fixture's statements in order, without the FTS5 shadow tables."""
    text = SCHEMA.read_text(encoding="utf-8")
    marks = list(_HEADER.finditer(text))
    out = []
    for n, mark in enumerate(marks):
        end = marks[n + 1].start() if n + 1 < len(marks) else len(text)
        name = mark.group(2)
        if mark.group(1) == "table" and name.endswith(FTS_SHADOWS) and "_fts_" in name:
            continue
        out.append(text[mark.end() : end].strip())
    return out


def vec_blob(seed: int, dim: int = DIM, *, special: bool = False) -> bytes:
    """A float32 blob as the SQLite store wrote it (``array("f", vec).tobytes()``)."""
    rnd = random.Random(seed)
    vals = array("f", (rnd.uniform(-1, 1) for _ in range(dim)))
    if special:  # values a decimal round trip could lose: -0.0, a subnormal, extremes
        vals[0] = -0.0
        vals[1] = struct.unpack("=f", b"\x01\x00\x00\x00")[0]  # a float32 subnormal
        vals[2] = 3.4028234663852886e38
        vals[3] = 1.1754943508222875e-38
    return vals.tobytes()


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = OFF")  # orphans must be insertable for the tests
    return conn


def create_schema(path: Path) -> sqlite3.Connection:
    conn = connect(path)
    for stmt in schema_statements():
        conn.execute(stmt)
    conn.commit()
    return conn


def build_source(path: Path, *, label: str = LABEL) -> Path:
    """A library.db with rows in every table; returns ``path``."""
    conn = create_schema(path)
    x = conn.execute
    ts = "2026-10-31T12:00:00+00:00"
    # items: a gap (3, 4 deleted) and an AUTOINCREMENT high-water mark above the max (7).
    for i, kind, vis in ((1, "book", "public"), (2, "newspaper", "private"), (5, "spec", "public")):
        x(
            "INSERT INTO items (id, sha256, kind, title, author, format, size, visibility,"
            " created_at, archived_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (i, f"{i:064x}", kind, f"Title {i}", "Ann", "epub", 100 + i, vis, ts, None),
        )
    x("UPDATE sqlite_sequence SET seq = 7 WHERE name = 'items'")
    x(
        "INSERT INTO api_keys (id, name, key_id, key_hash, scopes, kinds, private, created_at)"
        " VALUES (1, 'rules', 'k1', 'h1', 'read,write', NULL, 1, ?)",
        (ts,),
    )
    x(
        "INSERT INTO api_keys (id, name, key_id, key_hash, scopes, kinds, private, created_at,"
        " revoked_at, last_used, replaces) VALUES (2, 'rules2', 'k2', 'h2', 'read', 'book,spec',"
        " 0, ?, NULL, ?, 1)",
        (ts, ts),
    )
    for c in range(1, 5):
        x(
            "INSERT INTO feed (cursor, type, item_id, sha256, kind, visibility, at, data, subject)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                c,
                "added",
                1 if c < 4 else 99,
                f"{1:064x}",
                "book",
                "public",
                ts,
                json.dumps({"n": c, "path": "a/b"}) if c != 2 else None,
                "item" if c != 3 else "repodoc",
            ),
        )
    x("UPDATE sqlite_sequence SET seq = 9 WHERE name = 'feed'")
    x("INSERT INTO item_uploaders VALUES (1, 'k1'), (99, 'k2')")
    x(
        "INSERT INTO item_sources VALUES (2, ?, ?)",
        (json.dumps([{"article_title": "A", "source_urls": ["https://x"], "excerpt": "é"}]), ts),
    )
    for d in (1, 2):
        x(
            "INSERT INTO repodocs (id, repo, path, kind, visibility, current_version_id,"
            " created_at, updated_at, removed_at, removed_commit) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (d, "agentculture/x", f"docs/specs/s{d}.md", "spec", "private", d, ts, ts, None, None),
        )
        x(
            "INSERT INTO repodoc_versions (id, doc_id, commit_sha, committed_at,"
            " on_default_branch, visibility, sha256, size, pushed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (d, d, f"c{d}", ts, d % 2, "private", f"{d + 50:064x}", 10, ts),
        )
    x(
        "INSERT INTO repodoc_drafts (id, doc_id, visibility, sha256, size, pushed_at)"
        " VALUES (1, 2, 'private', ?, 11, ?)",
        (f"{77:064x}", ts),
    )
    x("DELETE FROM repodoc_dirty WHERE doc_id = 1")  # discovery claimed doc 1's mark
    x("INSERT INTO repodoc_dirty VALUES (40)")  # a mark for a hard-deleted doc
    x(
        "INSERT INTO projects VALUES (3, 'agentculture/x', ?, ?, ?)",
        (json.dumps(["library"]), json.dumps(["agentculture/y"]), ts),
    )
    x(
        "INSERT INTO spec_links VALUES (1, 'https://g/pr/1', 1, 'abc', ?, 'c1', ?, ?, ?)",
        (json.dumps(["culture_library/core/x.py"]), ts, json.dumps([]), ts),
    )
    x(
        "INSERT INTO mirror_outbox (id, key, sha256, state, attempts, last_error, enqueued_at,"
        " done_at) VALUES (1, 'agentculture/x/docs/specs/s1.md@c1', ?, 'done', 1, '', ?, ?)",
        (f"{51:064x}", ts, ts),
    )
    x(
        "INSERT INTO index_queue (item_id, state, body_done, attempts, next_attempt_at,"
        " enqueued_at, last_error, priority) VALUES (1, 'done', 1, 1, 0, 1761900000.5, NULL, 0),"
        " (2, 'failed', 0, 5, 1761900100.25, 1761900000.0, 'boom', 0)"
    )
    x("INSERT INTO index_meta VALUES ('last_success_at', '1761900000.5'), ('last_error', NULL)")
    x(
        "INSERT INTO repodoc_index_queue (doc_id, shown_sha256, shown_visibility, state,"
        " attempts, next_attempt_at, enqueued_at, last_error) VALUES"
        " (1, ?, 'private', 'done', 1, 0, 1761900000.0, NULL)",
        (f"{51:064x}",),
    )
    x(
        "INSERT INTO search_fts (rowid, title, body) VALUES (1, 'Title 1 Ann', ?),"
        " (2, 'Title 2 Ann', 'short')",
        ("Café prose.\n\nIt names culture_library/core/search.py and\ttabs.",),
    )
    x(
        "INSERT INTO repodoc_fts (rowid, title, body, label, visibility) VALUES"
        " (1, 'Spec one docs/specs/s1.md', '# Spec one\n\nbody', 'Spec one', 'private')"
    )
    for item, chunk in ((1, 0), (1, 1), (2, 0)):
        x(
            "INSERT INTO embeddings (item_id, chunk_no, text, vec, model) VALUES (?,?,?,?,?)",
            (
                item,
                chunk,
                f"chunk {item}/{chunk}",
                vec_blob(item * 10 + chunk, special=True),
                label,
            ),
        )
    x(
        "INSERT INTO repodoc_embeddings (doc_id, chunk_no, text, vec, model) VALUES (1, 0, ?,?,?)",
        ("# Spec one", vec_blob(1000), label),
    )
    x(
        "INSERT INTO eval_sets (id, item_id, item_sha256, chunker_json, adapter,"
        " adapter_version, generator_json, version, created_at) VALUES (1, 1, ?, ?, 'ragas',"
        " '0.4.3', ?, 2, ?)",
        (f"{1:064x}", json.dumps({"max_chars": 1000}), json.dumps({"model": "cortex"}), ts),
    )
    for s in (1, 2):
        x(
            "INSERT INTO eval_samples (id, set_id, question, reference_answer,"
            " reference_chunk_shas, reference_contexts, source_urls) VALUES (?,1,?,?,?,?,?)",
            (s, f"q{s}?", f"a{s}", json.dumps([f"sha{s}"]), json.dumps(["ctx"]), "[]"),
        )
    x(
        "INSERT INTO eval_runs (id, set_id, set_version, status, config_json, total, completed,"
        " aggregates_json, created_at, partial_aggregates_json, error_rate, suites_json, running)"
        " VALUES (1, 1, 2, 'complete', ?, 2, 2, ?, ?, NULL, 0.0, ?, 0)",
        (json.dumps({"k": 5, "mode": "hybrid"}), json.dumps({"mrr": 0.75}), ts, '["speed"]'),
    )
    for s in (1, 2):
        x(
            "INSERT INTO eval_run_samples (run_id, sample_id, retrieved_chunk_shas, scores_json,"
            " question, reference_chunk_shas, set_version, retrieved_item_ids, latency_ms,"
            " freshness) VALUES (1, ?, ?, ?, ?, ?, 2, ?, ?, ?)",
            (
                s,
                json.dumps([f"sha{s}"]),
                json.dumps({"found_at_1": 1.0}),
                f"q{s}?",
                json.dumps([f"sha{s}"]),
                json.dumps([1]),
                12.5,
                json.dumps({"state": "fresh"}),
            ),
        )
    conn.commit()
    conn.close()
    return path


def add_vectors(path: Path, n: int, *, per_item: int = 10, label: str = LABEL) -> None:
    """Add ``n`` synthetic vectors (items from 1000 up, ``per_item`` chunks each)."""
    conn = connect(path)
    items = (n + per_item - 1) // per_item
    conn.executemany(
        "INSERT INTO items (id, sha256, kind, title, author, format, size, created_at)"
        " VALUES (?, ?, 'book', ?, '', 'txt', 1, '2026-10-31T00:00:00+00:00')",
        ((1000 + i, f"{1000 + i:064x}", f"b{i}") for i in range(items)),
    )
    rnd = random.Random(7)
    base = [array("f", (rnd.uniform(-1, 1) for _ in range(DIM))).tobytes() for _ in range(64)]

    def rows():
        for k in range(n):
            yield (1000 + k // per_item, k % per_item, f"text {k} " * 40, base[k % 64], label)

    conn.executemany(
        "INSERT INTO embeddings (item_id, chunk_no, text, vec, model) VALUES (?,?,?,?,?)", rows()
    )
    conn.commit()
    conn.close()
