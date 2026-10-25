"""Adapter protocol, models, judge config, locality gate, client."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from culture_library.eval import (
    EvalAdapter,
    EvalApiError,
    EvalPolicyError,
    EvalRun,
    EvalSet,
    JudgeConfig,
    LibraryClient,
    OpenAIChat,
    Sample,
    guard_text_egress,
    is_local,
)
from culture_library.eval.models import Chunk, Passage
from tests.eval.conftest import FakeAdapter, FakeJudge

ROOT = Path(__file__).resolve().parents[2]


def test_fake_adapter_satisfies_protocol_and_runs():
    a = FakeAdapter()
    assert isinstance(a, EvalAdapter)
    chunks = [Chunk(0, "sha0", "alpha"), Chunk(1, "sha1", "beta")]
    judge = FakeJudge()
    samples = a.generate_samples(chunks, 2, judge)
    assert len(samples) == 2 and len(judge.payloads) == 2
    res = a.score(samples, [[Passage(1, "sha0")], [Passage(1, "nope")]], None)
    assert res.aggregate == {"hit": 0.5}
    assert res.per_sample == [{"hit": 1.0}, {"hit": 0.0}]


def test_judge_config_env_defaults_and_overrides():
    c = JudgeConfig.from_env({})
    assert c.model == "cortex" and c.is_local
    env = {
        "LOBES_GATEWAY_URL": "http://gw:1",
        "GATEWAY_API_KEY": "sekret",
    }
    c = JudgeConfig.from_env(env)
    assert c.url == "http://gw:1" and c.api_key == "sekret"
    assert "sekret" not in repr(c)
    env["CULTURE_LIBRARY_EVAL_LLM_URL"] = "https://api.example.com"
    env["CULTURE_LIBRARY_EVAL_LLM_MODEL"] = "m"
    c = JudgeConfig.from_env(env)
    assert (c.url, c.model, c.is_local) == ("https://api.example.com", "m", False)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://127.0.0.1:8001", True),
        ("http://localhost/v1", True),
        ("http://[::1]:9", True),
        ("http://host.docker.internal:8001", True),
        ("https://api.example.com", False),
        ("http://127.0.0.1.evil.com", False),
        ("http://localhost.evil.com", False),
        ("http://10.0.0.5", False),
        ("", False),
    ],
)
def test_is_local(url, expected):
    assert is_local(url) is expected


def test_guard_text_egress():
    private = [{"id": 3, "visibility": "private"}]
    with pytest.raises(EvalPolicyError):
        guard_text_egress(private, JudgeConfig(url="https://api.example.com"))
    guard_text_egress(private, JudgeConfig(url="http://127.0.0.1:8001"))
    guard_text_egress([{"id": 1, "visibility": "public"}], JudgeConfig(url="https://x.com"))
    with pytest.raises(EvalPolicyError):  # unknown visibility is non-public
        guard_text_egress([{"id": 2}], JudgeConfig(url="https://x.com"))


def test_guard_runs_before_any_text_read():
    read = []

    def items():
        yield {"id": 1, "visibility": "private"}
        read.append("text")  # would only run if the guard kept iterating

    with pytest.raises(EvalPolicyError):
        guard_text_egress(items(), JudgeConfig(url="https://x.com"))
    assert read == []


def test_models_roundtrip():
    s = EvalSet(
        item_id=1,
        item_sha256="a" * 64,
        adapter="fake",
        adapter_version="0",
        chunker={"size": 5},
        samples=[Sample("q", "a", ["s"], ["ctx"], ["http://u"])],
    )
    d = json.loads(json.dumps(s.to_dict()))
    assert EvalSet.from_dict(d) == s
    assert "id" not in d
    r = EvalRun(set_id=1, total=2, completed=1, status="partial")
    assert r.to_dict()["status"] == "partial"


def test_openai_chat_posts_payload(fake_judge_server):
    fake_judge_server.reply = "hello"
    llm = OpenAIChat(fake_judge_server.config(model="cortex", api_key="k1"))
    assert llm.complete([{"role": "user", "content": "hi"}]) == "hello"
    p = fake_judge_server.payloads[0]
    assert p["model"] == "cortex" and p["messages"][0]["content"] == "hi"
    assert fake_judge_server.auth_headers == ["Bearer k1"]


def test_openai_chat_error_wrapped():
    with pytest.raises(EvalApiError):
        OpenAIChat(JudgeConfig(url="http://127.0.0.1:1"), timeout=1).complete([])


def test_client_error_and_no_redirect():
    def t(method, url, headers, body):
        assert headers["Authorization"] == "Bearer k"
        return 302, b""

    with pytest.raises(EvalApiError) as ei:
        LibraryClient("http://x", "k", transport=t).status()
    assert ei.value.status == 302


def test_client_against_real_app(library):
    item = library.add_item(b"hello world", title="Hi")
    st = library.client.status()
    assert isinstance(st, dict)
    assert library.client.get_item(item.id)["visibility"] == "public"
    with pytest.raises(EvalApiError) as ei:
        library.client.get_item(9999)
    assert ei.value.status == 404


def test_no_ragas_import_outside_adapter():
    pat = re.compile(r"^\s*(import|from)\s+ragas\b", re.M)
    offenders = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "culture_library").rglob("*.py")
        if p.name != "ragas_adapter.py" and pat.search(p.read_text())
    ]
    assert offenders == []
    assert (ROOT / "culture_library/eval/client.py").exists()
    assert (ROOT / "tests/eval/conftest.py").exists()


def test_eval_package_imports_without_third_party():
    import subprocess
    import sys

    code = (
        "import sys\n"
        "class B:\n"
        "    def find_spec(self, n, p=None, t=None):\n"
        "        if n.split('.')[0] in {'fastapi','starlette','httpx','ragas','mcp','uvicorn'}:\n"
        "            raise ImportError(n)\n"
        "sys.meta_path.insert(0, B())\n"
        "import culture_library.eval\n"
    )
    r = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
