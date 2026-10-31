"""The prototype's data, loaded into a library data directory.

Everything the 30 reference screenshots show comes from here: the 17 catalog items (titles,
kinds, sizes and upload times as in the prototype), the 23 Oct newspaper, the Gwen spec and
this library's own devague frame as repo documents, and the two send targets. Items go in
through the library's own ingest path (``library.add``, the code ``POST /api/library`` runs)
and repo documents through ``RepoDocs.push`` (``POST /api/repodocs``); only the timestamps are
then set to the prototype's, because the API stamps "now".
"""

from __future__ import annotations

import io
import json
import random
import zipfile
from pathlib import Path

from culture_library.core import Store, eval_store, keys, library
from culture_library.core.repodocs import RepoDocs

FIXTURES = Path(__file__).resolve().parent / "fixtures"
_STAMP = (2026, 10, 23, 0, 0, 0)

_CONTAINER = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
    "</rootfiles></container>"
)
_OPF = (
    '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" '
    'version="3.0" unique-identifier="uid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
    '<dc:identifier id="uid">urn:fixture:{n}</dc:identifier><dc:title>{title}</dc:title>'
    "<dc:language>en</dc:language></metadata><manifest>"
    '<item id="s01" href="s01.xhtml" media-type="application/xhtml+xml"/></manifest>'
    '<spine><itemref idref="s01"/></spine></package>'
)
_CHAPTER = (
    '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
    "<head><title>{title}</title></head><body><p>{text}</p></body></html>"
)


def _zip(title: str, n: int, pad: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:

        def put(name: str, data: bytes | str, stored: bool = False) -> None:
            info = zipfile.ZipInfo(name, _STAMP)
            info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
            zf.writestr(info, data)

        put("mimetype", "application/epub+zip", stored=True)
        put("META-INF/container.xml", _CONTAINER)
        put("OEBPS/content.opf", _OPF.format(n=n, title=title))
        # the prototype's card text for an item whose body it did not have
        text = (
            "Daily newspaper from culture-rules. Its full text opens in the reader once the "
            "library indexes EPUB bodies."
        )
        put("OEBPS/s01.xhtml", _CHAPTER.format(title=title, text=text))
        put("OEBPS/pad.bin", b"\0" * pad, stored=True)
    return buf.getvalue()


def padded_epub(title: str, n: int, size: int) -> bytes:
    """A small valid EPUB of exactly ``size`` bytes (a stored, unreferenced member pads it)."""
    base = len(_zip(title, n, 0))
    if size < base:
        raise ValueError(f"{title!r}: {size} bytes is smaller than the {base}-byte minimum")
    data = _zip(title, n, size - base)
    if len(data) != size:
        raise AssertionError((title, len(data), size))
    return data


def seed(data_dir: Path) -> dict:
    """Fill ``data_dir``; return ``{"store", "key", "ids": {"paper", "spec", "frame", ...}}``."""
    catalog = json.loads((FIXTURES / "catalog.json").read_text(encoding="utf-8"))
    store = Store(data_dir)
    ids: dict[str, str] = {}
    for spec in catalog["items"]:
        if "file" in spec:
            data, name = (FIXTURES / spec["file"]).read_bytes(), "paper.epub"
        else:
            data, name = padded_epub(spec["title"], spec["id"], spec["size"]), "item.epub"
        item, created = library.add(
            store,
            data,
            name,
            kind=spec["kind"],
            title=spec["title"],
            author=spec["author"],
            visibility="public",
        )
        assert created and item.size == spec["size"], (spec["title"], item.size)
        with store.connect() as conn:
            conn.execute(
                "UPDATE items SET created_at = %s WHERE id = %s", (spec["created_at"], item.id)
            )
        ids[f"item-{spec['id']}"] = f"item-{item.id}"
    ids["paper"] = ids["item-20"]
    docs = RepoDocs(store)
    for spec in catalog["docs"]:
        pushed = docs.push(
            remote=spec["remote"],
            path=spec["path"],
            commit=spec["commit"],
            committed_at=spec["committed_at"],
            default_branch_head=spec["commit"],
            content=(FIXTURES / spec["file"]).read_text(encoding="utf-8"),
            visibility="public",
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE repodocs SET created_at = %s, updated_at = %s WHERE id = %s",
                (spec["updated_at"], spec["updated_at"], pushed.doc.id),
            )
        ids[spec["key"]] = f"doc-{pushed.doc.id}"
    (Path(data_dir) / "targets.json").write_text(json.dumps(catalog["targets"]), encoding="utf-8")
    ids.update(seed_evals(store))
    _, raw = keys.mint(store, "visual", ["read", "write"], private=True)
    return {"store": store, "key": raw, "ids": ids}


_EVAL_STAMP = "2026-10-23T{:02d}:{:02d}:00+00:00"


def seed_evals(store: Store) -> dict:
    """Eval sets and runs for the Evals views: 3 books (20 questions) and 3 newspapers (4).

    Deterministic (a seeded RNG and fixed ``created_at``), so the Evals screenshots are stable.
    Each set has a rerank-off and a rerank-on run; each book has a later rerank-on run too, so
    the radar lays an earlier comparable run under the latest one. Returns
    ``{"eval-book-runs": "1,2,...", ...}`` for the views.
    """
    rnd = random.Random(7)
    runs: dict[str, list[int]] = {"book": [], "newspaper": []}
    minute = 0
    with store.connect() as conn:
        items = conn.execute(
            "SELECT id, kind, sha256 FROM items WHERE kind IN ('book', 'newspaper') ORDER BY id"
        ).fetchall()
        picks = [i for i in items if i[1] == "book"][:3] + [
            i for i in items if i[1] == "newspaper"
        ][:3]
        for iid, kind, sha in picks:
            sid = eval_store.create_set(
                conn, iid, sha, chunker={}, adapter="ragas", adapter_version="0.4.3", generator={}
            )
            n = 20 if kind == "book" else 4
            eval_store.add_samples(
                conn,
                sid,
                [
                    {
                        "question": f"Question {q} about this {kind}?",
                        "reference_answer": "a",
                        "reference_chunk_shas": [f"s{q}"],
                        "source_urls": (
                            [f"https://example.com/{iid}/{q % 2}"] if kind == "newspaper" else []
                        ),
                    }
                    for q in range(n)
                ],
            )
            samples = eval_store.list_samples(conn, sid)
            plan = [(False, 0.55), (True, 0.7)] + ([(True, 0.88)] if kind == "book" else [])
            for rerank, p in plan:
                ranks = []
                for _ in samples:
                    r = rnd.random()
                    ranks.append(0 if r < p * 0.75 else 1 if r < p * 0.92 else 2 if r < p else None)
                judged = kind == "book"
                agg: dict = {"id_based_context_precision": 0.17}
                if judged:
                    agg["context_precision"] = 0.75 + rnd.random() / 5
                    agg["context_recall"] = 0.8 + rnd.random() / 6
                suites = [{"name": "id_retrieval", "version": "1"}]
                suites += [{"name": "judged", "version": "1"}] if judged else []
                suites += [{"name": "speed", "version": "1"}]
                rid = eval_store.create_run(
                    conn,
                    sid,
                    status="complete",
                    config={
                        "mode": "hybrid",
                        "k": 5,
                        "rerank": rerank,
                        "judge_model": "cortex" if judged else None,
                    },
                    total=n,
                    completed=n,
                    aggregates=agg,
                    error_rate=0.0,
                    suites=suites,
                )
                minute += 7
                conn.execute(
                    "UPDATE eval_runs SET created_at = %s WHERE id = %s",
                    (_EVAL_STAMP.format(9 + minute // 60, minute % 60), rid),
                )
                runs[kind].append(rid)
                for smp, rank in zip(samples, ranks):
                    ret = ["x", "y", "z", "w", "v"]
                    if rank is not None:
                        ret[rank] = smp["reference_chunk_shas"][0]
                    eval_store.add_run_sample(
                        conn,
                        rid,
                        smp["id"],
                        retrieved_chunk_shas=ret,
                        scores={},
                        question=smp["question"],
                        reference_chunk_shas=smp["reference_chunk_shas"],
                        retrieved_item_ids=[iid] * 5,
                        latency_ms=rnd.choice([400, 700, 1300]),
                    )
    return {f"eval-{k}-runs": ",".join(str(r) for r in v) for k, v in runs.items()}
