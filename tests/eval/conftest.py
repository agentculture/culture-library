"""Shared eval fixtures. Wave-1 tasks add test files; they never edit this conftest.

Provides: ``FakeAdapter``, ``FakeJudge`` (in-process ChatLLM that records payloads),
``fake_judge_server`` (a real 127.0.0.1 OpenAI-compatible HTTP server that records payloads),
and ``library`` (real ``create_app()`` over a tmp store, reached through a ``LibraryClient``
whose transport is a TestClient).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Sequence

import pytest

from culture_library.eval.client import LibraryClient
from culture_library.eval.config import JudgeConfig
from culture_library.eval.models import Chunk, Sample, ScoreResult


class FakeAdapter:
    """Deterministic EvalAdapter: one sample per chunk; hit-rate style scoring."""

    name = "fake"
    version = "0"

    def __init__(self) -> None:
        self.generate_calls: list[tuple[int, int]] = []
        self.score_calls: int = 0

    def generate_samples(self, chunks: Sequence[Chunk], n: int, llm) -> list[Sample]:
        self.generate_calls.append((len(chunks), n))
        out = []
        for c in list(chunks)[:n]:
            if llm is not None:
                llm.complete([{"role": "user", "content": c.text}])
            out.append(
                Sample(
                    question=f"What does chunk {c.chunk_no} say?",
                    reference_answer=c.text,
                    reference_chunk_shas=[c.chunk_sha],
                    reference_contexts=[c.text],
                )
            )
        return out

    def score(self, samples, retrieved, llm) -> ScoreResult:
        self.score_calls += 1
        per = []
        for s, passages in zip(samples, retrieved):
            got = {p.chunk_sha for p in passages}
            hit = any(sha in got for sha in s.reference_chunk_shas)
            per.append({"hit": 1.0 if hit else 0.0})
        agg = {"hit": (sum(p["hit"] for p in per) / len(per)) if per else None}
        return ScoreResult(per_sample=per, aggregate=agg)


class FakeJudge:
    """In-process ChatLLM; every call's messages are appended to ``payloads``."""

    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply
        self.payloads: list[list[dict]] = []

    def complete(self, messages: list[dict]) -> str:
        self.payloads.append(messages)
        return self.reply


@dataclass
class JudgeServer:
    url: str
    payloads: list[dict] = field(default_factory=list)
    auth_headers: list[str] = field(default_factory=list)
    reply: str = "ok"

    def config(self, model: str = "cortex", api_key: str = "k") -> JudgeConfig:
        return JudgeConfig(url=self.url, model=model, api_key=api_key)


@pytest.fixture
def fake_judge_server():
    state = JudgeServer(url="")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            state.payloads.append(body)
            state.auth_headers.append(self.headers.get("Authorization", ""))
            out = json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": state.reply}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):  # silence
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield state
    srv.shutdown()
    srv.server_close()


@dataclass
class Library:
    """A real app over a tmp store plus a LibraryClient wired to it."""

    app: object
    store: object
    client: LibraryClient
    http: object  # the fastapi TestClient

    def add_item(self, body: bytes, *, title: str = "t", kind: str = "book", **kw):
        kw.setdefault("format", "txt")
        kw.setdefault("visibility", "public")
        return self.store.add_item(body, kind=kind, title=title, **kw).item


@pytest.fixture
def library(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from culture_library.core import Store
    from culture_library.server import app as server_app
    from culture_library.server.auth import Principal, current_principal

    app = server_app.create_app()
    store = app.state.store = Store(tmp_path / "data")
    app.state.embedder = None
    principal = Principal(
        key_id="eval-test",
        scopes=frozenset({"read", "write", "admin"}),
        kinds=None,
        private=True,
    )
    app.dependency_overrides[current_principal] = lambda: principal
    http = TestClient(app)

    def transport(method, url, headers, body):
        path = url[len("http://testserver") :] if url.startswith("http://testserver") else url
        r = http.request(method, path, content=body, headers=headers)
        return r.status_code, r.content

    client = LibraryClient("http://testserver", "test-key", transport=transport)
    return Library(app=app, store=store, client=client, http=http)
