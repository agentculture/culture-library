"""The real app on a free port over seeded data, and a Chromium page that looks like the capture.

The reference screenshots were taken with a 1680x1000 or 400x860 viewport (device scale 1,
classic scrollbars), the OS color scheme set to light or dark, reduced motion, an empty
localStorage per view, a local sign-in key in sessionStorage, and a fixed clock of
24 Oct 2026 03:00 in Jerusalem (the library's own time zone: the "Sent to x3-ori at 03:00"
toast and the day groups depend on it).
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .seed import seed

CLOCK = "2026-10-24T03:00:00+03:00"
TIMEZONE = "Asia/Jerusalem"
VIEWPORTS = {"desktop": (1680, 1000), "phone": (400, 860)}


@dataclass
class Site:
    base: str
    key: str
    ids: dict
    _server: object = field(repr=False, default=None)
    _thread: threading.Thread | None = field(repr=False, default=None)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_site(root: Path) -> Site:
    import os

    import uvicorn

    from culture_library.server import app as server_app
    from culture_library.server import routes_send

    os.environ["CULTURE_LIBRARY_PURGE_INTERVAL_S"] = "0"
    os.environ.pop("CULTURE_LIBRARY_ACCESS_TEAM_DOMAIN", None)
    os.environ.pop("CULTURE_LIBRARY_ACCESS_AUD", None)
    os.environ.pop("CULTURE_LIBRARY_SEND_TARGETS", None)
    routes_send.publish = lambda data: None  # no broker here: the event is "published"
    seeded = seed(root / "data")
    app = server_app.create_app()
    app.state.store = seeded["store"]
    app.state.access = None  # Access off: the key in sessionStorage signs the page in
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the library server did not start")
        time.sleep(0.05)
    return Site(f"http://127.0.0.1:{port}", seeded["key"], seeded["ids"], server, thread)


def new_page(playwright_browser, site: Site, form: str, scheme: str):
    """A fresh context (so an empty localStorage) set up like the capture; returns the page."""
    width, height = VIEWPORTS[form]
    ctx = playwright_browser.new_context(
        viewport={"width": width, "height": height},
        device_scale_factor=1,
        color_scheme=scheme,
        reduced_motion="reduce",
        timezone_id=TIMEZONE,
        locale="en-US",
    )
    ctx.add_init_script("try { sessionStorage.setItem('lib.key', %r); } catch (e) {}" % site.key)
    page = ctx.new_page()
    page.clock.set_fixed_time(CLOCK)
    return page


def launch(playwright):
    """Chromium with classic scrollbars, as in the references (headless hides them by default)."""
    return playwright.chromium.launch(
        ignore_default_args=["--hide-scrollbars"],
        args=["--font-render-hinting=slight"],
    )


def ready(page, docs: bool = True) -> None:
    """Wait until the page has drawn from live data and its fonts are in."""
    page.wait_for_load_state("networkidle")
    page.wait_for_function("() => document.querySelector('#shelf .spine') !== null")
    if docs:  # repo-document titles arrive in the background, after the first draw
        page.wait_for_function("() => document.body.textContent.includes('Gwen is pocketchat')")
    page.wait_for_load_state("networkidle")
    page.evaluate("() => document.fonts.ready.then(() => true)")
    page.wait_for_timeout(150)
