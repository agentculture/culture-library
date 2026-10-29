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


def test_claude_md_states_live_rag_status_and_baseline() -> None:
    text = _read("CLAUDE.md").lower()
    assert "embeddings are not yet enabled" not in text
    assert "embeddings were enabled on" in text
    assert "sets 6–8 for newspapers" in text
    assert "docs/deliveries/2026-10-25-rag-endpoints-ragas-evals.md" in text


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


def test_claude_md_documents_eval_suites_shape_and_api() -> None:
    text = _read("CLAUDE.md")
    for needle in (
        "eval/suites/",
        "id_retrieval",
        "result_quality",
        "freshness",
        "fresh_share",
        "within_budget",
        "eval_radar.py",
        "/api/eval/suites",
        "/api/eval/runs/start",
        "/api/eval/radar",
        "/api/eval/weakest",
        "/reanchor",
        "docs/api/eval.md",
        "eval_start_run",
        "EVAL_ROUTES",
        "show,suites,radar,compare,reanchor}",
        "#30",
        "culture-rules#121",
    ):
        assert needle in text, needle


def test_readme_and_harness_files_mention_suites_and_radar() -> None:
    readme = _read("README.md")
    for needle in ("### Eval suites and shape", "/api/eval/radar", "docs/api/eval.md", "eval_*"):
        assert needle in readme, needle
    for name in ("QWEN.md", "AGENTS.override.md"):
        text = _read(name)
        for needle in ("id_retrieval", "radar", "docs/api/eval.md", "eval_*"):
            assert needle in text, (name, needle)
    assert not (ROOT / "AGENTS.md").exists()
