"""t20: the ebooks.culture.dev teardown gate lists its conditions and the dry run."""

from __future__ import annotations

from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "operations" / "teardown-ebooks.md"


def test_gate_lists_three_conditions_with_evidence_slots() -> None:
    text = DOC.read_text()
    for heading in (
        "Devices sync from the library",
        "Seven consecutive newspapers on library.culture.dev",
        "Migration complete",
    ):
        assert heading in text
    assert text.count("- [ ] **Evidence:**") == 3


def test_gate_has_dry_run_before_apply() -> None:
    text = DOC.read_text()
    dry = text.index(
        "cultureflare remote-login teardown --hostname ebooks.culture.dev \\\n"
        "  --tunnel-name ebooks-culture-dev\n"
    )
    apply_ = text.index("--tunnel-name ebooks-culture-dev --apply")
    assert dry < apply_
    assert "(dry-run output, date)" in text
    assert "xteink.culture.dev" in text
