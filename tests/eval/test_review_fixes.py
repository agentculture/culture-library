"""PR #17 review findings, one regression test each (numbers match the review)."""

from __future__ import annotations

import http.server
import json
import threading

import pytest

pytest.importorskip("fastapi")

from culture_library.cli._commands import eval as eval_cmd  # noqa: E402
from culture_library.core import eval_store, indexer, sources  # noqa: E402
from culture_library.eval import runner  # noqa: E402
from culture_library.eval.config import JudgeConfig, OpenAIChat  # noqa: E402
from culture_library.eval.errors import EvalApiError  # noqa: E402
from culture_library.eval.models import ScoreResult  # noqa: E402

from .conftest import FakeAdapter  # noqa: E402
from .test_cli_eval import _item, _run, cli  # noqa: E402,F401


def _set_id(cli, capsys, n=3):  # noqa: F811
    item = _item(cli)
    _, out, _ = _run(capsys, "eval", "generate", str(item.id), "--samples", str(n), "--json")
    return item, json.loads(out)["set_id"]


# 1 -------------------------------------------------------------------------------------------


def test_partial_run_prints_status_instead_of_crashing(cli, capsys):  # noqa: F811
    _, set_id = _set_id(cli, capsys)
    real = cli.client.retrieve
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise EvalApiError("library went away", status=503)
        return real(*a, **kw)

    cli.client.retrieve = flaky
    rc, out, err = _run(capsys, "eval", "run", str(set_id), "--no-llm")
    assert "Traceback" not in err
    assert "internal" not in err.lower()
    assert "partial" in out
    rc, out, _ = _run(capsys, "eval", "show", "--set", str(set_id), "--json")
    assert rc == 0


# 2 -------------------------------------------------------------------------------------------


class _Recorder(http.server.BaseHTTPRequestHandler):
    hits: list = []
    location = ""

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        type(self).hits.append((self.headers.get("Authorization"), self.rfile.read(n)))
        if type(self).location:
            self.send_response(302)
            self.send_header("Location", type(self).location)
            self.end_headers()
        else:
            body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - urllib turns a followed 302 into a GET
        type(self).hits.append((self.headers.get("Authorization"), b""))
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


def _serve(handler):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_judge_chat_refuses_redirects():
    target = type("Target", (_Recorder,), {"hits": [], "location": ""})
    tsrv, turl = _serve(target)
    first = type("First", (_Recorder,), {"hits": [], "location": turl + "/v1/chat/completions"})
    fsrv, furl = _serve(first)
    try:
        chat = OpenAIChat(JudgeConfig(url=furl, model="m", api_key="secret"))
        with pytest.raises(EvalApiError):
            chat.complete([{"role": "user", "content": "private passage text"}])
        assert len(first.hits) == 1
        assert target.hits == []
    finally:
        fsrv.shutdown()
        tsrv.shutdown()


# 3 -------------------------------------------------------------------------------------------


def test_gateway_key_never_goes_to_a_non_local_judge():
    env = {"CULTURE_LIBRARY_EVAL_LLM_URL": "https://api.example.com", "GATEWAY_API_KEY": "gw"}
    assert JudgeConfig.from_env(env).api_key == ""
    env["CULTURE_LIBRARY_EVAL_LLM_KEY"] = "provider"
    assert JudgeConfig.from_env(env).api_key == "provider"


def test_lan_gateway_still_gets_its_own_key():
    env = {"LOBES_GATEWAY_URL": "http://gw:1", "GATEWAY_API_KEY": "gw"}
    assert JudgeConfig.from_env(env).api_key == "gw"


def test_local_judge_uses_gateway_key():
    env = {"LOBES_GATEWAY_URL": "http://127.0.0.1:8001", "GATEWAY_API_KEY": "gw"}
    assert JudgeConfig.from_env(env).api_key == "gw"


# 4 -------------------------------------------------------------------------------------------


def test_empty_retrieval_precision_is_undefined():
    assert runner.id_metrics([], ["a"])[runner.PRECISION] is None
    assert runner.id_metrics([], ["a"])[runner.RECALL] == 0.0


def test_adapter_cannot_override_runner_id_metrics(cli, capsys):  # noqa: F811
    _, set_id = _set_id(cli, capsys, n=2)

    class Liar(FakeAdapter):
        def score(self, samples, retrieved, llm):
            per = [{runner.PRECISION: 0.123, runner.RECALL: 0.456, "llm": 1.0} for _ in samples]
            return ScoreResult(per_sample=per, aggregate={runner.PRECISION: 0.123, "llm": 1.0})

    eval_set = cli.client.get_set(set_id)
    run = runner.run_set(
        cli.client,
        eval_set,
        Liar(),
        llm=object(),
        judge=JudgeConfig(url="http://127.0.0.1:9", model="m"),
    )
    assert run.status == "complete"
    assert all(s.scores[runner.PRECISION] != 0.123 for s in run.samples)
    assert run.aggregates[runner.PRECISION] != 0.123
    assert run.aggregates["llm"] == 1.0


# 5 -------------------------------------------------------------------------------------------


def test_post_set_rejects_a_stale_item_sha(cli, capsys):  # noqa: F811
    item = _item(cli)
    body = {
        "item_id": item.id,
        "item_sha256": "0" * 64,
        "adapter": "fake",
        "adapter_version": "1",
        "samples": [{"question": "q", "reference_answer": "a"}],
    }
    with pytest.raises(EvalApiError) as exc:
        cli.client.request("POST", "/api/eval/sets", body)
    assert exc.value.status == 409
    assert exc.value.body["code"] == "item_changed"


# 6 -------------------------------------------------------------------------------------------


def test_cli_shows_the_server_detail(cli, capsys):  # noqa: F811
    rc, _, err = _run(capsys, "eval", "run", "999")
    assert rc == 2
    assert "not found" in err


# 7 -------------------------------------------------------------------------------------------


def test_no_llm_run_does_not_need_the_ragas_adapter(cli, capsys, monkeypatch):  # noqa: F811
    _, set_id = _set_id(cli, capsys)

    def boom(judge):
        raise AssertionError("--no-llm must not build the ragas adapter")

    monkeypatch.setattr(eval_cmd, "_make_adapter", boom)
    rc, out, err = _run(capsys, "eval", "run", str(set_id), "--no-llm", "--json")
    assert rc == 0, err
    assert json.loads(out)["status"] == "complete"


# 8 -------------------------------------------------------------------------------------------


def test_requeue_stale_model_reopens_items_embedded_under_another_label(cli):  # noqa: F811
    old = _item(cli)  # embedded as "fake-embed"
    with cli.store.connect() as conn:
        state = conn.execute(
            "SELECT state FROM index_queue WHERE item_id = ?", (old.id,)
        ).fetchone()[0]
    assert state == "done"
    assert indexer.requeue_stale_model(cli.store, "fake-embed") == 0
    assert indexer.requeue_stale_model(cli.store, "gemma2-embed+Retrieval-document") == 1
    with cli.store.connect() as conn:
        row = conn.execute(
            "SELECT state, body_done FROM index_queue WHERE item_id = ?", (old.id,)
        ).fetchone()
    assert tuple(row) == ("pending", 1)


# 9 -------------------------------------------------------------------------------------------


def test_add_samples_appends_without_touching_version(cli):  # noqa: F811
    item = _item(cli)
    with cli.store.connect() as conn:
        set_id = eval_store.create_set(
            conn, item.id, item.sha256, chunker={}, adapter="a", adapter_version="1", generator={}
        )
        eval_store.add_samples(conn, set_id, [{"question": "q1", "reference_answer": "a"}])
        eval_store.add_samples(conn, set_id, [{"question": "q2", "reference_answer": "a"}])
        qs = [s["question"] for s in eval_store.list_samples(conn, set_id)]
        version = eval_store.get_set(conn, set_id)["version"]
    assert qs == ["q1", "q2"]
    assert version == 1


# 10 ------------------------------------------------------------------------------------------


def test_sources_get_with_meta(cli):  # noqa: F811
    item = _item(cli)
    with cli.store.connect() as conn:
        assert sources.get_with_meta(conn, item.id) is None
        sources.put(conn, item.id, [{"x": 1}])
        manifest, updated_at = sources.get_with_meta(conn, item.id)
    assert manifest == [{"x": 1}]
    assert updated_at


# judge output limit (live run: reasoning model hit ragas's 1024-token default) ----------------


def test_judge_max_tokens_default_and_env():
    assert JudgeConfig(url="http://127.0.0.1:1").max_tokens == 8192
    env = {"CULTURE_LIBRARY_EVAL_LLM_MAX_TOKENS": "16000"}
    assert JudgeConfig.from_env(env).max_tokens == 16000


def test_chat_sends_max_tokens(fake_judge_server):
    judge = fake_judge_server.config()
    chat = OpenAIChat(judge)
    chat.complete([{"role": "user", "content": "hi"}])
    assert fake_judge_server.payloads[-1]["max_tokens"] == judge.max_tokens
