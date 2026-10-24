"""t21: the delivery evidence doc exists, names every claim and keeps c31 pending."""

from __future__ import annotations

from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "evidence" / "t21-delivery-evidence.md"


def test_doc_names_every_claim() -> None:
    text = DOC.read_text()
    for claim in ("c1", "c26", "c27", "c29", "c30", "c31"):
        assert f"{claim} " in text or f"{claim}:" in text, claim


def test_c31_is_pending_not_passed() -> None:
    rows = [ln for ln in DOC.read_text().splitlines() if ln.startswith("| c31")]
    assert rows, "no c31 summary row"
    assert "pending" in rows[0]
    assert "| passed" not in rows[0]
