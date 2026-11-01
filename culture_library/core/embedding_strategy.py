"""Which vectors belong together: the model id and the embedding strategy (decision c42).

Every stored vector carries ``model_id`` (the real model, e.g. ``google/embeddinggemma-2``,
never a gateway role name) and ``strategy``, the canonical key of a :class:`Strategy`. Search,
eval runs and each HNSW index select vectors by that pair, so two strategies never mix.

The key is compact JSON with sorted keys, compared as text: an index predicate
(``WHERE model_id = '...' AND strategy = '...'``) matches a query only when both are written
the same way, and :meth:`Strategy.key` is the one place that writes them. Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields

__all__ = [
    "CURRENT_MODEL_ID",
    "CURRENT_STRATEGY",
    "Strategy",
    "hnsw_index_name",
    "hnsw_index_sql",
]


@dataclass(frozen=True)
class Strategy:
    document_prompt: str
    query_prompt: str
    chunker: str
    max_chars: int
    dimension: int
    normalisation: str

    def key(self) -> str:
        """The stable text stored in the ``strategy`` column."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_key(cls, key: str) -> Strategy:
        data = json.loads(key)
        names = {f.name for f in fields(cls)}
        if not isinstance(data, dict) or set(data) != names:
            raise ValueError(f"not a strategy key (needs exactly {sorted(names)}): {key!r}")
        return cls(**data)


# What live writes since 2026-10-25, per the gateway's /capabilities and core/search.py's
# chunk_text (blank-line paragraphs packed up to CHUNK_MAX_CHARS).
CURRENT_MODEL_ID = "google/embeddinggemma-2"
CURRENT_STRATEGY = Strategy(
    document_prompt="Retrieval-document",
    query_prompt="Retrieval-query",
    chunker="paragraph-pack",
    max_chars=1000,
    dimension=768,
    normalisation="l2",
)

_VECTOR_TABLES = ("embeddings", "repodoc_embeddings")


def _table(table: str) -> str:
    if table not in _VECTOR_TABLES:
        raise ValueError(f"not a vector table: {table!r} (one of {', '.join(_VECTOR_TABLES)})")
    return table


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def hnsw_index_name(table: str, model_id: str, strategy: Strategy) -> str:
    """``<table>_hnsw_<12 hex>``, a digest of the pair; fits Postgres's 63-byte limit."""
    digest = hashlib.sha256(f"{model_id}\n{strategy.key()}".encode()).hexdigest()[:12]
    return f"{_table(table)}_hnsw_{digest}"


def hnsw_index_sql(table: str, model_id: str, strategy: Strategy) -> str:
    """The partial cosine HNSW index over one (model id, strategy)'s vectors in ``table``.

    A query uses it when it filters on the same two literals (or parameters bound to them)
    and orders by ``vec::vector(<dimension>) <=> <query>::vector(<dimension>)``.
    """
    if not isinstance(strategy.dimension, int) or strategy.dimension < 1:
        raise ValueError(f"a strategy's dimension must be a positive int: {strategy!r}")
    return (
        f"CREATE INDEX IF NOT EXISTS {hnsw_index_name(table, model_id, strategy)}"
        f" ON {_table(table)} USING hnsw ((vec::vector({int(strategy.dimension)}))"
        f" vector_cosine_ops) WHERE model_id = {_literal(model_id)}"
        f" AND strategy = {_literal(strategy.key())}"
    )
