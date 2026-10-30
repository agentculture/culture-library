"""t16: ``culture-library eval`` driven end to end against the real routes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from culture_library.cli import main  # noqa: E402
from culture_library.cli._commands import eval as eval_cmd  # noqa: E402
from culture_library.core.indexer import Indexer, enqueue  # noqa: E402
from culture_library.eval.config import JudgeConfig  # noqa: E402

from .conftest import FakeAdapter, FakeJudge  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
LOCAL = JudgeConfig(url="http://127.0.0.1:9", model="m")


def _para(i: int) -> str:
    return f"Paragraph number {i} opens here. " + ("filler words fill the line. " * 20).strip()


def _embed(texts):
    return [[1.0, 0.0] for _ in texts]


@pytest.fixture
def cli(library, monkeypatch):
    library.app.state.query_embedder = _embed
    library.app.state.reranker = None
    adapter = FakeAdapter()
    monkeypatch.setattr(eval_cmd, "_make_client", lambda: library.client)
    monkeypatch.setattr(eval_cmd, "_make_judge", lambda args=None: LOCAL)
    monkeypatch.setattr(eval_cmd, "_make_llm", lambda judge: FakeJudge())
    monkeypatch.setattr(eval_cmd, "_make_adapter", lambda judge: adapter)
    library.adapter = adapter
    return library


def _item(library, n=12, kind="book"):
    item = library.add_item("\n\n".join(_para(i) for i in range(n)).encode(), kind=kind)
    with library.store.connect() as conn:
        enqueue(conn, item.id)
    Indexer(library.store, _embed, model="fake-embed").run_once()
    return item


def _run(capsys, *argv):
    rc = main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_generate_run_show_round_trip(cli, capsys):
    item = _item(cli)
    rc, out, _ = _run(capsys, "eval", "generate", str(item.id), "--samples", "4", "--json")
    assert rc == 0
    gen = json.loads(out)
    assert gen["samples"] == 4
    assert gen["kind"] == "book"
    assert gen["item_id"] == item.id
    set_id = gen["set_id"]

    rc, out, _ = _run(capsys, "eval", "run", str(set_id), "--json")
    assert rc == 0
    run = json.loads(out)
    assert run["status"] == "complete"
    assert run["completed"] == run["total"] == 4
    assert run["set_id"] == set_id
    assert run["config"]["mode"] == "hybrid"
    assert run["config"]["k"] == 5
    assert {"hit"} <= set(run["aggregates"]) or run["aggregates"]

    rc, out, _ = _run(capsys, "eval", "show", "--run", str(run["run_id"]), "--json")
    assert rc == 0
    assert json.loads(out)["id"] == run["run_id"]
    rc, out, _ = _run(capsys, "eval", "show", "--set", str(set_id), "--json")
    assert rc == 0
    assert len(json.loads(out)["samples"]) == 4
    rc, out, _ = _run(capsys, "eval", "show", "--item", str(item.id), "--json")
    listing = json.loads(out)
    assert [s["id"] for s in listing["sets"]] == [set_id]
    assert [r["id"] for r in listing["runs"]] == [run["run_id"]]

    rc, out, _ = _run(capsys, "eval", "show")
    assert f"set {set_id}" in out
    assert f"run {run['run_id']}" in out


def test_text_output(cli, capsys):
    item = _item(cli)
    rc, out, err = _run(capsys, "eval", "generate", str(item.id), "--samples", "3")
    assert rc == 0
    assert "3 samples" in out
    assert err == ""
    rc, out, _ = _run(capsys, "eval", "run", "1")
    assert rc == 0
    assert "complete" in out


def test_two_configs_over_one_set(cli, capsys):
    item = _item(cli)
    _, out, _ = _run(capsys, "eval", "generate", str(item.id), "--samples", "3", "--json")
    set_id = json.loads(out)["set_id"]
    _, a, _ = _run(capsys, "eval", "run", str(set_id), "--mode", "semantic", "--k", "2", "--json")
    _, b, _ = _run(
        capsys, "eval", "run", str(set_id), "--mode", "hybrid", "--k", "7", "--no-rerank", "--json"
    )
    a, b = json.loads(a), json.loads(b)
    assert a["run_id"] != b["run_id"]
    assert a["set_id"] == b["set_id"] == set_id
    assert (a["config"]["mode"], a["config"]["k"], a["config"]["rerank"]) == ("semantic", 2, None)
    assert (b["config"]["mode"], b["config"]["k"], b["config"]["rerank"]) == ("hybrid", 7, False)
    _, out, _ = _run(capsys, "eval", "show", "--json")
    assert len(json.loads(out)["runs"]) == 2


def test_run_no_llm_skips_llm(cli, capsys):
    item = _item(cli)
    _, out, _ = _run(capsys, "eval", "generate", str(item.id), "--samples", "2", "--json")
    rc, out, _ = _run(capsys, "eval", "run", str(json.loads(out)["set_id"]), "--no-llm", "--json")
    assert rc == 0
    assert json.loads(out)["config"]["judge_model"] is None


def test_overview_default_and_json(capsys):
    rc, out, _ = _run(capsys, "eval")
    assert rc == 0
    assert "generate" in out
    rc, out, _ = _run(capsys, "eval", "overview", "--json")
    data = json.loads(out)
    assert rc == 0
    assert data["subject"] == "culture-library eval"
    assert data["sections"]


def test_unknown_set_is_environment_error(cli, capsys):
    rc, _, err = _run(capsys, "eval", "run", "999", "--json")
    assert rc == 2
    e = json.loads(err)
    assert "404" in e["message"]
    assert e["remediation"]


def test_wrong_kind_and_no_samples_are_user_errors(cli, capsys):
    doc = cli.add_item(b"text", kind="article")
    rc, _, err = _run(capsys, "eval", "generate", str(doc.id))
    assert rc == 1
    assert "hint:" in err
    # a book that was never indexed has no chunks: GenerateError -> exit 1 with its hint
    book = cli.add_item(b"hello", kind="book")
    rc, _, err = _run(capsys, "eval", "generate", str(book.id), "--json")
    assert rc == 1
    assert json.loads(err)["remediation"]


def test_non_public_item_to_remote_judge_is_policy_error(cli, capsys, monkeypatch):
    monkeypatch.setattr(
        eval_cmd,
        "_make_judge",
        lambda args=None: JudgeConfig(url="https://api.example.com", model="m"),
    )
    item = cli.add_item(b"secret", kind="book", visibility="private")
    rc, _, err = _run(capsys, "eval", "generate", str(item.id))
    assert rc == 1
    assert "not public" in err


def test_show_rejects_two_selectors(cli, capsys):
    rc, _, err = _run(capsys, "eval", "show", "--set", "1", "--run", "1")
    assert rc == 1
    assert "hint:" in err


def test_missing_key_is_user_error(monkeypatch, capsys):
    monkeypatch.delenv(eval_cmd.ENV_KEY, raising=False)
    monkeypatch.setattr(eval_cmd, "_make_adapter", lambda judge: FakeAdapter())
    rc, _, err = _run(capsys, "eval", "run", "1", "--json")
    assert rc == 1
    assert eval_cmd.ENV_KEY in json.loads(err)["message"]


def test_client_defaults_from_env(monkeypatch):
    monkeypatch.setenv(eval_cmd.ENV_KEY, "k")
    monkeypatch.delenv(eval_cmd.ENV_URL, raising=False)
    assert eval_cmd._make_client().base_url == "http://127.0.0.1:8796"
    monkeypatch.setenv(eval_cmd.ENV_URL, "http://x:1/")
    assert eval_cmd._make_client().base_url == "http://x:1"


_NO_EXTRA = """
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        top = name.split('.')[0]
        if top in {'ragas', 'openai', 'datasets', 'instructor'} or top.startswith('langchain'):
            raise ImportError('blocked: ' + name)
sys.meta_path.insert(0, Block())
from culture_library.cli import main
sys.exit(main(['eval', sys.argv[1], '1', '--json']))
"""


@pytest.mark.parametrize("verb", ["generate", "run"])
def test_without_eval_extra_exits_2(verb):
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _NO_EXTRA, verb],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT)},
    )
    assert proc.returncode == 2, proc.stderr
    assert "culture-library[eval]" in proc.stderr
    assert json.loads(proc.stderr)["code"] == 2


# ---- t12: suites, radar, compare, reanchor, run --suites --------------------------------


def _two_runs(cli, capsys):
    item = _item(cli)
    _, out, _ = _run(capsys, "eval", "generate", str(item.id), "--samples", "3", "--json")
    set_id = json.loads(out)["set_id"]
    ids = []
    for _i in range(2):
        _, out, _ = _run(capsys, "eval", "run", str(set_id), "--no-llm", "--json")
        ids.append(json.loads(out)["run_id"])
    return item, set_id, ids


def test_suites_text_and_json(cli, capsys):
    rc, out, err = _run(capsys, "eval", "suites")
    assert rc == 0
    assert err == ""
    assert "found_at_5" in out or "needs judge" in out
    rc, out, _ = _run(capsys, "eval", "suites", "--json")
    names = [s["name"] for s in json.loads(out)["suites"]]
    assert names


def test_run_with_suites_and_warmup(cli, capsys):
    item = _item(cli)
    _, out, _ = _run(capsys, "eval", "generate", str(item.id), "--samples", "2", "--json")
    set_id = json.loads(out)["set_id"]
    _, out, _ = _run(capsys, "eval", "suites", "--json")
    first = json.loads(out)["suites"][0]["name"]
    rc, out, _ = _run(
        capsys, "eval", "run", str(set_id), "--no-llm", "--suites", first, "--warmup", "--json"
    )
    assert rc == 0
    assert json.loads(out)["status"] == "complete"


def test_run_unknown_suite_is_user_error_with_hint(cli, capsys):
    rc, out, err = _run(capsys, "eval", "run", "1", "--suites", "nope", "--json")
    assert rc == 1
    assert "nope" in err
    assert "eval suites" in err


def test_radar_run_group_pair(cli, capsys):
    _, _, (a, b) = _two_runs(cli, capsys)
    rc, out, _ = _run(capsys, "eval", "radar", "--run", str(a), "--json")
    assert rc == 0
    assert "found_at_5" in json.loads(out)["axes"]
    rc, out, _ = _run(capsys, "eval", "radar", "--runs", f"{a},{b}")
    assert rc == 0
    assert "found_at_5" in out
    rc, out, _ = _run(capsys, "eval", "radar", "--run", str(a), "--vs", str(b), "--json")
    assert json.loads(out)["kind"] == "pair"
    rc, out, _ = _run(capsys, "eval", "radar", "--run", str(a), "--vs", str(b))
    assert rc == 0
    assert out.startswith("pair")


def test_radar_bad_args(cli, capsys):
    rc, _, err = _run(capsys, "eval", "radar")
    assert rc == 1
    assert "hint:" in err
    rc, _, err = _run(capsys, "eval", "radar", "--run", "x")
    assert rc == 1
    assert "hint:" in err


def test_radar_unknown_run_is_env_error(cli, capsys):
    rc, _, err = _run(capsys, "eval", "radar", "--run", "999")
    assert rc == 2
    assert "hint:" in err


def test_compare_text_prints_checks_first(cli, capsys):
    _, _, (a, b) = _two_runs(cli, capsys)
    rc, out, _ = _run(capsys, "eval", "compare", "--before", str(a), "--after", str(b))
    assert rc == 0
    assert out.startswith("comparable:")
    assert "axis: before -> after" in out
    rc, out, _ = _run(capsys, "eval", "compare", "--before", str(a), "--after", str(b), "--json")
    data = json.loads(out)
    assert data["checks"]["comparable"] is True
    assert "deltas" in data


def test_compare_not_comparable_prints_checks_only(capsys):
    text = eval_cmd._compare_text(
        {
            "checks": {"comparable": False, "reasons": ["different sets"], "warnings": []},
            "left_out": {"before": [], "after": [1]},
            "budget_s": 1.0,
        }
    )
    assert text.startswith("comparable: NO")
    assert "different sets" in text


def test_compare_text_prints_every_axis_value():
    """The API sends before/after as flat axis dicts; each axis gets its own line."""
    text = eval_cmd._compare_text(
        {
            "checks": {"comparable": True, "reasons": [], "warnings": []},
            "before": {"found_at_1": 0.2, "mrr": 0.435},
            "after": {"found_at_1": 0.45, "mrr": 0.618},
            "deltas": {"found_at_1": 0.25, "mrr": 0.183},
            "changed_questions": [],
        }
    )
    assert "found_at_1:" in text
    assert "mrr:" in text
    assert "0.45" in text


def test_reanchor(cli, capsys):
    item = _item(cli)
    _, out, _ = _run(capsys, "eval", "generate", str(item.id), "--samples", "2", "--json")
    set_id = json.loads(out)["set_id"]
    rc, out, _ = _run(capsys, "eval", "reanchor", str(set_id), "--json")
    assert rc == 0
    data = json.loads(out)
    assert data["set_id"] == set_id
    assert "re_anchored" in data
    rc, out, _ = _run(capsys, "eval", "reanchor", str(set_id))
    assert rc == 0
    assert f"set {set_id}" in out
    rc, _, err = _run(capsys, "eval", "reanchor", "999")
    assert rc == 2
    assert "hint:" in err


def test_overview_and_explain_mention_new_verbs(capsys):
    _, out, _ = _run(capsys, "eval", "overview")
    for verb in ("suites", "radar", "compare", "reanchor"):
        assert verb in out
        rc, text, _ = _run(capsys, "explain", "eval", verb)
        assert rc == 0
        assert verb in text
