"""Core suite fixtures. Cited from agentculture/xteink @ ea2f643, path tests/core/conftest.py.

Modified: no-network guard kept; service fixtures replaced by a bare Store.
"""

import socket

import pytest

from culture_library.core import Store


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail the test if anything in the core suite opens a socket."""
    opened = []

    def _boom(*args, **kwargs):
        opened.append(args)
        raise AssertionError("network I/O attempted in culture_library.core suite")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    yield opened
    assert opened == []


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")
