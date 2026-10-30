"""Re-anchor eval references onto an item's current chunks after a re-index.

An eval sample anchors its references by ``chunk_sha`` (sha256 of the chunk text), so a
re-index that changes chunk boundaries or removes text orphans those shas. Each sample also
stores the reference *text* (``reference_contexts``); this module maps that text onto the
item's current chunks and reports the samples whose text is gone.

Matching rule (after :func:`normalise`, which collapses whitespace runs): a reference context
maps to every current non-header chunk that **contains** it, and to every non-header chunk
that is **contained in** it (a context spanning chunks). The header chunk (chunk 0, title and
author) only matches by containing the context. A sample is re-anchored when *every* one of
its contexts maps to at least one chunk; its new shas are the de-duplicated union in chunk
order. Otherwise it is stale and left untouched. Matching is only ever against the chunks of
the set's own item. Stdlib only, no network.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import eval_store
from .search import Chunk
from .store import now

NOT_FOUND = "reference text not found in current chunks"
NO_TEXT = "no stored reference text"


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def chunks_for_context(context: str, chunks: Sequence[Chunk]) -> list[str]:
    """Shas of the current chunks a reference context maps to, in chunk order."""
    ctx = normalise(context)
    if not ctx:
        return []
    out: list[str] = []
    for chunk in chunks:
        body = normalise(chunk.text)
        if ctx in body or (not chunk.is_header and body and body in ctx):
            if chunk.chunk_sha not in out:
                out.append(chunk.chunk_sha)
    return out


@dataclass
class Reanchored:
    updates: dict[int, list[str]] = field(default_factory=dict)  # sample id -> new shas
    stale: list[dict[str, Any]] = field(default_factory=list)  # {sample_id, question, reason}


def reanchor_samples(samples: Sequence[Mapping[str, Any]], chunks: Sequence[Chunk]) -> Reanchored:
    result = Reanchored()
    for sample in samples:
        contexts = [c for c in sample.get("reference_contexts") or [] if normalise(c)]
        reason = None if contexts else NO_TEXT
        shas: list[str] = []
        for ctx in contexts:
            found = chunks_for_context(ctx, chunks)
            if not found:
                reason = NOT_FOUND
                break
            shas.extend(s for s in found if s not in shas)
        if reason is None:
            result.updates[sample["id"]] = shas
        else:
            result.stale.append(
                {"sample_id": sample["id"], "question": sample["question"], "reason": reason}
            )
    return result


def apply_reanchor(
    conn: sqlite3.Connection, set_id: int, chunks: Sequence[Chunk]
) -> dict[str, Any]:
    """Re-anchor a set in place; bump its version once if any sample's shas changed.

    Sample ids are kept (UPDATE, not replace_samples). The summary is stored additively under
    ``generator_json["reanchor"]`` whether or not the version bumped, and returned.
    """
    row = eval_store.get_set(conn, set_id)
    samples = eval_store.list_samples(conn, set_id)
    res = reanchor_samples(samples, chunks)
    changed = [
        sid
        for sid, shas in res.updates.items()
        if shas != next(s["reference_chunk_shas"] for s in samples if s["id"] == sid)
    ]
    for sid in changed:
        conn.execute(
            "UPDATE eval_samples SET reference_chunk_shas = ? WHERE id = ?",
            (eval_store._dumps(res.updates[sid]), sid),
        )
    version = row["version"] + (1 if changed else 0)
    note = {
        "at": now(),
        "version": version,
        "re_anchored": len(res.updates),
        "changed": len(changed),
        "stale": len(res.stale),
        "stale_sample_ids": [s["sample_id"] for s in res.stale],
    }
    generator = dict(row["generator_json"] or {})
    generator["reanchor"] = note
    conn.execute(
        "UPDATE eval_sets SET version = ?, generator_json = ? WHERE id = ?",
        (version, eval_store._dumps(generator), set_id),
    )
    return {
        "previous_version": row["version"],
        "version": version,
        "re_anchored": list(res.updates),
        "could_not_re_anchor": res.stale,
        "note": note,
    }


__all__ = ["Reanchored", "apply_reanchor", "chunks_for_context", "normalise", "reanchor_samples"]
