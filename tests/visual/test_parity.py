"""Visual parity: each of the 30 reference views, rendered from fixture data, within 1%.

Obligation o1 on claim c32 (plan task t16). All 30 views compare with ``docs/design/reference/``;
the 28 in ``views.DEVIATED`` were re-approved by the operator: 18 for deviations d3 and d5 on
2026-10-24, and 10 more (the newspaper reader and the views over it, #18 source links) on
2026-10-25. ``CULTURE_LIBRARY_VISUAL_CANDIDATES=1`` writes renders to
``docs/design/reference-candidates/`` for a future re-approval; it never touches the references.
Each run prints a per-view diff table; a failing view leaves its actual and diff images in
the output directory (``CULTURE_LIBRARY_VISUAL_OUT`` or a temp dir) and names the paths.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("playwright")
pytest.importorskip("PIL")
from playwright.sync_api import sync_playwright  # noqa: E402

from . import compare, harness, views  # noqa: E402

pytestmark = pytest.mark.visual
RESULTS: dict[str, str] = {}


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = harness.launch(pw)
        yield b
        b.close()


@pytest.mark.parametrize("name,form,scheme,view", views.VIEWS, ids=[v[0] for v in views.VIEWS])
def test_view_matches_reference(browser, site, out_dir, name, form, scheme, view) -> None:
    page = harness.new_page(browser, site, form, scheme)
    try:
        views.go(page, site, view)
        shot = page.screenshot()
    finally:
        page.context.close()
    ref = views.reference_path(name)
    if os.environ.get("CULTURE_LIBRARY_VISUAL_CANDIDATES") == "1":
        cand = views.candidate_path(name)
        cand.parent.mkdir(parents=True, exist_ok=True)
        cand.write_bytes(shot)
    fraction, marked, note = compare.diff(shot, ref)
    RESULTS[name] = f"{fraction * 100:6.2f}%" + (" (re-approved)" if name in views.DEVIATED else "")
    if fraction > compare.THRESHOLD or note:
        actual = out_dir / f"{name}.actual.png"
        actual.write_bytes(shot)
        paths = [str(actual)]
        if marked is not None:
            diff_png = out_dir / f"{name}.diff.png"
            marked.save(diff_png)
            paths.append(str(diff_png))
        pytest.fail(
            f"{name}: {fraction * 100:.2f}% of pixels differ (limit "
            f"{compare.THRESHOLD * 100:.0f}%) {note}; images: {', '.join(paths)}"
        )


def test_zz_report(capsys) -> None:
    """Print the per-view table (runs last in this module)."""
    with capsys.disabled():
        print("\nvisual parity, share of pixels differing (channel tolerance 16):")
        for name, _, _, _ in views.VIEWS:
            print(f"  {name:34s} {RESULTS.get(name, 'not run')}")
