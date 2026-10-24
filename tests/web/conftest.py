"""Shared fixtures for the site tests (plan task t15)."""

from __future__ import annotations

import io
import zipfile

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server.auth import Principal, current_principal  # noqa: E402

READER = Principal("r", frozenset({"read"}))
OWNER = Principal("o", frozenset({"read", "write"}), private=True)
A = "a" * 40


def make_epub(chapters: dict[str, str]) -> bytes:
    """A minimal EPUB whose members are ``{name: xhtml}``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, body in chapters.items():
            zf.writestr(name, body)
    return buf.getvalue()


class Site:
    """A TestClient over the full app whose caller can be switched."""

    def __init__(self, tmp_path, principal: Principal = OWNER) -> None:
        self.app = server_app.create_app()
        self.store = self.app.state.store = Store(tmp_path / "data")
        self.as_(principal)
        self.c = TestClient(self.app)

    def as_(self, principal: Principal) -> Site:
        self.app.dependency_overrides[current_principal] = lambda: principal
        return self

    def upload(self, data: bytes, filename: str, **fields) -> dict:
        r = self.c.post("/api/library", files={"file": (filename, data)}, data=fields)
        assert r.status_code in (200, 201), r.text
        return r.json()["item"]

    def push_doc(self, path: str, content: str, visibility: str = "public") -> dict:
        body = {
            "remote": "https://github.com/agentculture/devague",
            "path": path,
            "commit": A,
            "committed_at": "2026-10-23T23:10:00Z",
            "default_branch_head": A,
            "visibility": visibility,
            "content": content,
        }
        r = self.c.post("/api/repodocs", json=body)
        assert r.status_code in (200, 201), r.text
        return r.json()["doc"]


@pytest.fixture
def site(tmp_path) -> Site:
    return Site(tmp_path)
