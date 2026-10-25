"""Plain dataclasses that serialize to the JSON shapes ``eval_store`` stores."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Chunk:
    """One chunk of an item, as returned by ``GET /api/rag/items/{id}/chunks``."""

    chunk_no: int
    chunk_sha: str
    text: str
    is_header: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> Chunk:
        return cls(
            chunk_no=int(d["chunk_no"]),
            chunk_sha=d["chunk_sha"],
            text=d["text"],
            is_header=bool(d.get("is_header", False)),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Passage:
    """One retrieved passage from ``POST /api/rag/retrieve``."""

    item_id: int
    chunk_sha: str
    text: str = ""
    title: str = ""
    kind: str = ""
    chunk_no: int = 0
    score: float | None = None
    rerank_score: float | None = None
    source: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> Passage:
        return cls(
            item_id=int(d["item_id"]),
            chunk_sha=d["chunk_sha"],
            text=d.get("text", ""),
            title=d.get("title", ""),
            kind=d.get("kind", ""),
            chunk_no=int(d.get("chunk_no", 0)),
            score=d.get("score"),
            rerank_score=d.get("rerank_score"),
            source=d.get("source", ""),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Sample:
    """One dataset row (``eval_samples``)."""

    question: str
    reference_answer: str = ""
    reference_chunk_shas: list[str] = field(default_factory=list)
    reference_contexts: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    id: int | None = None

    @classmethod
    def from_dict(cls, d: dict) -> Sample:
        return cls(
            question=d["question"],
            reference_answer=d.get("reference_answer", ""),
            reference_chunk_shas=list(d.get("reference_chunk_shas") or []),
            reference_contexts=list(d.get("reference_contexts") or []),
            source_urls=list(d.get("source_urls") or []),
            id=d.get("id"),
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["id"] is None:
            del d["id"]
        return d


@dataclass
class EvalSet:
    """A dataset (``eval_sets``) with its samples."""

    item_id: int
    item_sha256: str
    adapter: str
    adapter_version: str
    chunker: dict[str, Any] = field(default_factory=dict)
    generator: dict[str, Any] = field(default_factory=dict)
    samples: list[Sample] = field(default_factory=list)
    id: int | None = None
    created_at: str | None = None
    version: int | None = None

    @classmethod
    def from_dict(cls, d: dict) -> EvalSet:
        return cls(
            item_id=int(d["item_id"]),
            item_sha256=d["item_sha256"],
            adapter=d["adapter"],
            adapter_version=d.get("adapter_version", ""),
            chunker=d.get("chunker") or d.get("chunker_json") or {},
            generator=d.get("generator") or d.get("generator_json") or {},
            samples=[Sample.from_dict(s) for s in d.get("samples", [])],
            id=d.get("id"),
            created_at=d.get("created_at"),
            version=d.get("version"),
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("id", "created_at", "version"):
            if d[k] is None:
                del d[k]
        return d


@dataclass
class SampleResult:
    """One scored sample (``eval_run_samples``)."""

    sample_id: int | None
    retrieved_chunk_shas: list[str] = field(default_factory=list)
    scores: dict[str, float | None] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScoreResult:
    """What ``EvalAdapter.score`` returns: per-sample scores plus aggregates."""

    per_sample: list[dict[str, float | None]] = field(default_factory=list)
    aggregate: dict[str, float | None] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvalRun:
    """A run (``eval_runs``)."""

    set_id: int
    status: str = "complete"  # complete | partial | failed
    config: dict[str, Any] = field(default_factory=dict)
    total: int = 0
    completed: int = 0
    aggregates: dict[str, float | None] = field(default_factory=dict)
    samples: list[SampleResult] = field(default_factory=list)
    id: int | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["id"] is None:
            del d["id"]
        return d
