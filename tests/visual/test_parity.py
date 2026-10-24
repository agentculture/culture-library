"""Visual parity: each of the 30 reference views, rendered from fixture data, within 1%.

Obligation o1 on claim c32 (plan task t16). The views in ``views.DEVIATED`` (deviation d3, the
phone send-menu fix) are compared with ``docs/design/reference-candidates/`` when the operator
has approved candidates there; ``CULTURE_LIBRARY_VISUAL_CANDIDATES=1`` (re)writes them.
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
    if name in views.DEVIATED and os.environ.get("CULTURE_LIBRARY_VISUAL_CANDIDATES") == "1":
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_bytes(shot)
    if not ref.is_file():
        RESULTS[name] = "no approved candidate yet (d3)"
        pytest.skip(f"{name}: awaiting operator re-approval of docs/design/reference-candidates")
    fraction, marked, note = compare.diff(shot, ref)
    RESULTS[name] = f"{fraction * 100:6.2f}%" + (" (candidate)" if name in views.DEVIATED else "")
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
