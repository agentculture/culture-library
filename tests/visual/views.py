"""The 30 reference views and how to reach each one.

Names are the reference files' (``<form>-<scheme>-<view>``). ``DEVIATED`` are the six that the
operator-approved deviation d3 makes impossible to match (the prototype showed faded
"arriving" spines, "N arriving" and migration-time site state; the built site shows only live
data): they are compared with ``docs/design/reference-candidates/`` once the operator has
re-approved candidates there, never with ``docs/design/reference/``.
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
    "desktop-light-home",
    "desktop-dark-home",
    "phone-light-home",
    "phone-dark-home",
    "desktop-light-state",
    "desktop-dark-state",
}

VIEWS: list[tuple[str, str, str, str]] = [  # (name, form, scheme, view)
    (f"{form}-{scheme}-{view}", form, scheme, view)
    for form, views in (("desktop", DESKTOP), ("phone", PHONE))
    for scheme in ("light", "dark")
    for view in views
]
assert len(VIEWS) == 30


def reference_path(name: str) -> Path:
    return (CANDIDATES if name in DEVIATED else REFERENCE) / f"{name}.png"


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
    elif view == "reader-list-hidden":
        _paper(page, site)
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
