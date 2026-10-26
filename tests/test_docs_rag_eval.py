"""CLAUDE.md, README.md and the harness files describe the RAG + eval surface."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_claude_md_documents_rag_and_eval() -> None:
    text = _read("CLAUDE.md")
    for needle in (
        "/api/rag/retrieve",
        "/api/rag/items/{id}/chunks",
        "/api/eval/sets",
        "/api/eval/runs",
        "/sources",
        "culture-library eval",
        "[eval]",
        "ragas==0.4.3",
        "rag_retrieve",
        "Hit",
        "docs/evidence/t7-embedding-cost.md",
        "#7",
        "LOBES_RERANK_MODEL",
    ):
        assert needle in text, needle


def test_claude_md_states_live_rag_status_and_open_baseline() -> None:
    text = _read("CLAUDE.md").lower()
    assert "embeddings are not yet enabled" not in text
    assert "embeddings were enabled on" in text
    assert "newspaper half of the eval baseline" in text


def test_readme_has_rag_eval_section() -> None:
    text = _read("README.md")
    assert "## RAG and evals" in text
    for needle in ("/api/rag/retrieve", "culture-library eval", "culture-library[eval]"):
        assert needle in text, needle


def test_harness_files_stay_in_step() -> None:
    for name in ("QWEN.md", "AGENTS.colleague.md", "AGENTS.override.md"):
        text = _read(name)
        assert "/api/rag" in text, name
        assert "eval" in text, name
        assert len(text.splitlines()) < 145, name
