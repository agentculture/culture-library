"""The 30 reference views and how to reach each one.

Names are the reference files' (``<form>-<scheme>-<view>``). ``DEVIATED`` are the views that
cannot match the prototype's screenshots, and are compared with
``docs/design/reference-candidates/`` once the operator has approved candidates there (never
with ``docs/design/reference/``):

- home, site state, and the upload backdrop: deviation d3 (no faded "arriving" spines, no
  "N arriving", live site state) plus the prototype's preview-only upload text;
- spec and frame readers (and the frame with the list hidden): d3 again, the prototype's
  "Arriving" chip makes its head row 2px taller than the built site's;
- the phone send menu: the prototype's menu ran off the right edge, the built site anchors it
  to the viewport so the page never scrolls sideways;
- the newspaper reader, and the send menu and "sent" toast drawn over it (desktop and phone):
  #18, the newspaper's source titles render as links (accent colour, underlined), re-approved
  by the operator on 2026-10-26.
"""

from __future__ import annotations

from pathlib import Path

from .harness import ready

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "docs" / "design" / "reference"
CANDIDATES = ROOT / "docs" / "design" / "reference-candidates"

DESKTOP = ("home", "reader-paper", "reader-spec", "reader-frame", "reader-list-hidden")
DESKTOP += ("send-menu", "sent", "state", "upload")
PHONE = ("home", "reader", "list-drawer", "send-menu", "sent", "upload")
DEVIATED = {
    f"{form}-{scheme}-{view}"
    for scheme in ("light", "dark")
    for form, view in (
        ("desktop", "home"),
        ("phone", "home"),
        ("desktop", "state"),
        ("desktop", "upload"),
        ("phone", "upload"),
        ("desktop", "reader-spec"),
        ("desktop", "reader-frame"),
        ("desktop", "reader-list-hidden"),
        ("phone", "send-menu"),
        ("desktop", "reader-paper"),  # #18 source links
        ("desktop", "send-menu"),
        ("desktop", "sent"),
        ("phone", "reader"),
        ("phone", "sent"),
    )
}

VIEWS: list[tuple[str, str, str, str]] = [  # (name, form, scheme, view)
    (f"{form}-{scheme}-{view}", form, scheme, view)
    for form, views in (("desktop", DESKTOP), ("phone", PHONE))
    for scheme in ("light", "dark")
    for view in views
]
assert len(VIEWS) == 30


def reference_path(name: str) -> Path:
    """Every view compares with the approved reference (the 18 d3/d5 views were re-approved)."""
    return REFERENCE / f"{name}.png"


def candidate_path(name: str) -> Path:
    """Where ``CULTURE_LIBRARY_VISUAL_CANDIDATES=1`` writes a render for a future re-approval."""
    return CANDIDATES / f"{name}.png"


def _open(page, site, fragment: str = "", docs: bool = True) -> None:
    page.goto(f"{site.base}/{fragment}")
    ready(page, docs)


def _paper(page, site) -> None:
    _open(page, site, "#" + site.ids["paper"])
    page.wait_for_selector("#reader h2")


def _send_menu(page) -> None:
    page.click("#send-btn")
    page.wait_for_selector("#send-menu [data-send]")


def go(page, site, view: str) -> None:
    """Drive ``page`` to the named view (the part after ``<form>-<scheme>-``)."""
    if view == "home":
        _open(page, site)
    elif view in ("reader-paper", "reader"):
        _paper(page, site)
    elif view == "reader-spec":
        _open(page, site, "#" + site.ids["spec"])
        page.wait_for_selector("#reader .lede")
    elif view == "reader-frame":
        _open(page, site, "#" + site.ids["frame"])
        page.wait_for_selector("#reader .dv-id, #reader .claim")
    elif view == "reader-list-hidden":  # the frame reader with the list pane hidden
        _open(page, site, "#" + site.ids["frame"])
        page.wait_for_selector("#reader .dv-id, #reader .claim")
        page.click("#pane-btn")
    elif view == "list-drawer":
        _paper(page, site)
        page.click("#pane-btn")
        page.wait_for_selector("#split:not(.closed)")
    elif view == "send-menu":
        _paper(page, site)
        _send_menu(page)
    elif view == "sent":
        _paper(page, site)
        _send_menu(page)
        page.click('#send-menu [data-send="x3-ori"]')
        page.wait_for_selector("#reader .sendline")
        page.wait_for_selector("#toast:not([hidden])")
    elif view == "state":
        _open(page, site, "#state", docs=False)
        page.wait_for_selector("#charts .cc")
    elif view == "upload":
        _open(page, site)
        page.click("#upload-open")
        page.wait_for_selector("#upload[open]")
    else:  # pragma: no cover
        raise ValueError(view)
    page.evaluate("() => document.fonts.ready.then(() => true)")
    page.wait_for_timeout(150)
