"""S3 mirror backend on a stubbed client (no boto3 needed)."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from culture_library.storage.s3 import S3Backend, from_env

ROOT = Path(__file__).resolve().parent.parent.parent


class ClientError(Exception):
    def __init__(self, code: str):
        self.response = {"Error": {"Code": code}}


class StubClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.objects: dict[str, dict] = {}

    def put_object(self, **kw):
        self.calls.append("put_object")
        self.objects[kw["Key"]] = kw

    def head_object(self, Bucket, Key):  # noqa: N803
        self.calls.append("head_object")
        if Key not in self.objects:
            raise ClientError("404")
        return {"Metadata": self.objects[Key]["Metadata"]}

    def __getattr__(self, name):
        raise AssertionError(f"unexpected client operation {name}")


def test_put_sets_sse_and_content_type_and_prefix() -> None:
    c = StubClient()
    b = S3Backend(bucket="bk", prefix="mirror/", client=c)
    b.put("repo/a.md@abc", b"hello", "text/markdown")
    kw = c.objects["mirror/repo/a.md@abc"]
    assert kw["Bucket"] == "bk"
    assert kw["ServerSideEncryption"] == "AES256"
    assert kw["ContentType"] == "text/markdown"
    assert kw["Metadata"] == {"sha256": hashlib.sha256(b"hello").hexdigest()}


def test_head_roundtrip_and_missing() -> None:
    c = StubClient()
    b = S3Backend(bucket="bk", client=c)
    assert b.head("k") is None
    b.put("k", b"x", "text/plain")
    assert b.head("k") == hashlib.sha256(b"x").hexdigest()


def test_head_reraises_other_errors() -> None:
    class Boom(StubClient):
        def head_object(self, Bucket, Key):  # noqa: N803
            raise ClientError("403")

    backend = S3Backend(bucket="bk", client=Boom())
    with pytest.raises(ClientError):
        backend.head("k")


def test_never_calls_delete() -> None:
    c = StubClient()
    b = S3Backend(bucket="bk", client=c)
    b.put("k", b"1", "text/plain")
    b.put("k", b"2", "text/plain")
    b.head("k")
    assert set(c.calls) == {"put_object", "head_object"}
    assert not any("delete" in n.lower() for n in c.calls)
    src = (ROOT / "culture_library/storage/s3.py").read_text()
    assert "delete" not in src.lower()


def test_from_env_requires_bucket(monkeypatch) -> None:
    monkeypatch.delenv("CULTURE_LIBRARY_S3_BUCKET", raising=False)
    client = StubClient()
    with pytest.raises(ValueError, match="CULTURE_LIBRARY_S3_BUCKET"):
        from_env(client=client)
    monkeypatch.setenv("CULTURE_LIBRARY_S3_BUCKET", "b")
    monkeypatch.setenv("CULTURE_LIBRARY_S3_PREFIX", "p/")
    be = from_env(client=client)
    assert be.bucket == "b"
    assert be.prefix == "p/"


def test_boto3_imported_lazily(monkeypatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "boto3", None)  # makes import raise ImportError
    with pytest.raises(ImportError, match="culture-library\\[s3\\]"):
        S3Backend(bucket="b")


def test_core_imports_no_network_modules() -> None:
    banned = {"boto3", "botocore", "urllib.request", "socket"}
    for path in (ROOT / "culture_library/core").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            assert not banned & set(names), f"{path.name} imports {names}"
