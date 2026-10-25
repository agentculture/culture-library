"""``culture-library eval`` — generate, run and inspect RAG retrieval evals.

Talks to a running library over HTTP (``CULTURE_LIBRARY_URL``, default
``http://127.0.0.1:8796``; ``CULTURE_LIBRARY_API_KEY``). Generating and scoring use the
optional ``[eval]`` extra (ragas); it is imported lazily inside the handlers, so the rest of
the CLI works without it.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from culture_library.cli._commands.overview import emit_overview
from culture_library.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from culture_library.cli._output import emit_result

DEFAULT_URL = "http://127.0.0.1:8796"
ENV_URL = "CULTURE_LIBRARY_URL"
ENV_KEY = "CULTURE_LIBRARY_API_KEY"
_JSON_HELP = "Emit structured JSON."
_INSTALL = "install culture-library[eval]"


def _json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


# ---- factories (tests monkeypatch these) ---------------------------------------------------


def _make_client():
    """A LibraryClient from the environment; a missing key is a user error."""
    from culture_library.eval.client import LibraryClient

    key = os.environ.get(ENV_KEY, "")
    if not key:
        raise CliError(
            EXIT_USER_ERROR,
            f"{ENV_KEY} is not set",
            f"export {ENV_KEY}=<an API key>; mint one with 'culture-library keys mint'",
        )
    return LibraryClient(os.environ.get(ENV_URL) or DEFAULT_URL, key)


def _make_judge():
    from culture_library.eval.config import JudgeConfig

    return JudgeConfig.from_env()


def _make_llm(judge):
    from culture_library.eval.config import OpenAIChat

    return OpenAIChat(judge)


def _server_detail(body: Any) -> str | None:
    """The server's own explanation: flat ``{detail, code}`` or nested ``{detail: {...}}``."""
    if not isinstance(body, dict):
        return None
    detail = body.get("detail", body.get("error"))
    if isinstance(detail, dict):
        detail = detail.get("detail")
    return detail if isinstance(detail, str) and detail else None


def _make_adapter(judge):
    """The ragas adapter; without the [eval] extra this raises CliError (exit 2)."""
    try:
        from culture_library.eval.ragas_adapter import RagasAdapter
    except ImportError as exc:
        raise CliError(
            EXIT_ENV_ERROR, f"the eval dependencies are not installed ({exc})", _INSTALL
        ) from None
    return RagasAdapter(judge)


# ---- error mapping -------------------------------------------------------------------------


def _map_errors(fn):
    """Run ``fn``; translate eval errors into CliError (api -> 2, others -> 1)."""
    from culture_library.eval.errors import EvalApiError, EvalError

    try:
        return fn()
    except CliError:
        raise
    except EvalApiError as exc:
        body = _server_detail(exc.body)
        detail = f": {body}" if body else ""
        raise CliError(
            EXIT_ENV_ERROR,
            f"{exc}{detail}",
            f"check that the library is running at ${ENV_URL} and ${ENV_KEY} is valid",
        ) from None
    except EvalError as exc:
        text = str(exc)
        if _INSTALL in text or _INSTALL in getattr(exc, "hint", ""):
            code = EXIT_ENV_ERROR
        else:
            code = EXIT_USER_ERROR
        raise CliError(code, text, getattr(exc, "hint", "") or _default_hint(exc, text)) from None


def _default_hint(exc: Exception, text: str) -> str:
    if _INSTALL in text:
        return _INSTALL
    return "run 'culture-library explain eval' for usage"


# ---- verbs ---------------------------------------------------------------------------------


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "culture-library eval",
        [
            {
                "title": "Verbs",
                "items": [
                    "generate ITEM_ID — build an eval set from a book or newspaper and store it",
                    "run SET_ID — retrieve + score every sample and store the run",
                    "show [--set ID | --run ID | --item ID] — list or show sets and runs",
                    "overview — this description",
                ],
            },
            {
                "title": "Configuration",
                "items": [
                    f"{ENV_URL} (default {DEFAULT_URL})",
                    f"{ENV_KEY} (required)",
                    "judge: CULTURE_LIBRARY_EVAL_LLM_URL / _MODEL, GATEWAY_API_KEY",
                    "install: pip install 'culture-library[eval]'",
                ],
            },
        ],
        json_mode=_json(args),
    )
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    def go() -> dict[str, Any]:
        from culture_library.eval import generate as gen

        judge = _make_judge()
        adapter = _make_adapter(judge)
        client = _make_client()
        kind = args.kind or (client.get_item(args.item_id) or {}).get("kind")
        builders = {"book": gen.generate_book_set, "newspaper": gen.generate_newspaper_set}
        if kind not in builders:
            raise CliError(
                EXIT_USER_ERROR,
                f"item {args.item_id} is kind {kind!r}; eval sets support book and newspaper",
                "pass --kind book|newspaper to override",
            )
        eval_set = builders[kind](
            client,
            adapter,
            _make_llm(judge),
            judge,
            args.item_id,
            samples=args.samples,
            seed=args.seed,
        )
        stored = client.create_set(eval_set)
        return {
            "set_id": stored.get("id"),
            "item_id": args.item_id,
            "kind": kind,
            "samples": len(eval_set.samples),
            "version": stored.get("version"),
        }

    result = _map_errors(go)
    _emit(args, result, f"set {result['set_id']}: {result['samples']} samples ({result['kind']})")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    def go() -> dict[str, Any]:
        from culture_library.eval import runner

        judge = None if args.no_llm else _make_judge()
        adapter = runner.IdOnlyAdapter() if judge is None else _make_adapter(judge)
        client = _make_client()
        eval_set = client.get_set(args.set_id)
        run = runner.run_set(
            client,
            eval_set,
            adapter,
            llm=None if judge is None else _make_llm(judge),
            judge=judge,
            mode=args.mode,
            k=args.k,
            rerank=args.rerank,
            candidates=args.candidates,
        )
        stored = client.create_run(run)
        return {
            "run_id": stored.get("id"),
            "set_id": args.set_id,
            "status": run.status,
            "completed": run.completed,
            "total": run.total,
            "config": run.config,
            "aggregates": run.aggregates,
        }

    result = _map_errors(go)
    agg = ", ".join(f"{k}={_fmt(v)}" for k, v in sorted((result["aggregates"] or {}).items()))
    _emit(
        args,
        result,
        f"run {result['run_id']} on set {result['set_id']}: {result['status']} "
        f"({result['completed']}/{result['total']})" + (f"\n{agg}" if agg else ""),
    )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    chosen = [n for n in ("set", "run", "item") if getattr(args, n) is not None]
    if len(chosen) > 1:
        raise CliError(EXIT_USER_ERROR, "pass at most one of --set, --run, --item", "pick one")

    def go() -> tuple[dict[str, Any], str]:
        client = _make_client()
        if args.set is not None:
            data = client.request("GET", f"/api/eval/sets/{int(args.set)}")
            return data, (
                f"set {data['id']}: item {data['item_id']}, adapter {data['adapter']} "
                f"{data.get('adapter_version', '')}, {len(data.get('samples', []))} samples"
            )
        if args.run is not None:
            data = client.get_run(args.run)
            agg = ", ".join(f"{k}={_fmt(v)}" for k, v in sorted((data["aggregates"]).items()))
            return data, (
                f"run {data['id']} on set {data['set_id']}: {data['status']} "
                f"({data['completed']}/{data['total']})" + (f"\n{agg}" if agg else "")
            )
        q = f"?item_id={int(args.item)}" if args.item is not None else ""
        sets = client.request("GET", f"/api/eval/sets{q}")["sets"]
        runs = client.request("GET", "/api/eval/runs")["runs"]
        if args.item is not None:
            ids = {s["id"] for s in sets}
            runs = [r for r in runs if r["set_id"] in ids]
        lines = [f"set {s['id']}: item {s['item_id']}, {s['adapter']}" for s in sets]
        lines += [f"run {r['id']}: set {r['set_id']}, {r['status']}" for r in runs]
        return {"sets": sets, "runs": runs}, "\n".join(lines) or "no eval sets or runs"

    data, text = _map_errors(go)
    _emit(args, data, text)
    return 0


def _fmt(v: Any) -> str:
    return "n/a" if v is None else f"{v:.3f}" if isinstance(v, float) else str(v)


def _emit(args: argparse.Namespace, data: dict, text: str) -> None:
    emit_result(data if _json(args) else text, json_mode=_json(args))


# ---- registration --------------------------------------------------------------------------


def _bool_opt(p: argparse.ArgumentParser, name: str, help_: str) -> None:
    dest = name.replace("-", "_")
    p.add_argument(f"--{name}", dest=dest, action="store_true", default=None, help=help_)
    p.add_argument(f"--no-{name}", dest=dest, action="store_false", help=f"Disable {name}.")


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("eval", help="RAG evals (see 'culture-library eval overview').")
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=cmd_overview, json=False)
    nsub = p.add_subparsers(dest="eval_command", parser_class=type(p))

    g = nsub.add_parser("generate", help="Build and store an eval set for a book or newspaper.")
    g.add_argument("item_id", type=int, help="Library item id.")
    g.add_argument("--samples", type=int, default=20, help="Samples to generate (default 20).")
    g.add_argument("--seed", type=int, default=0, help="Selection seed (default 0).")
    g.add_argument("--kind", choices=["book", "newspaper"], help="Override the item's kind.")
    g.add_argument("--json", action="store_true", help=_JSON_HELP)
    g.set_defaults(func=cmd_generate)

    r = nsub.add_parser("run", help="Run an eval set against retrieval and store the run.")
    r.add_argument("set_id", type=int, help="Eval set id.")
    r.add_argument("--mode", choices=["semantic", "hybrid"], default="hybrid")
    r.add_argument("--k", type=int, default=5, help="Passages per query (default 5).")
    _bool_opt(r, "rerank", "Force reranking on (default: server decides).")
    r.add_argument("--candidates", type=int, default=None, help="Rerank candidate pool size.")
    r.add_argument(
        "--no-llm", action="store_true", help="Skip LLM-judged metrics (ID metrics only)."
    )
    r.add_argument("--json", action="store_true", help=_JSON_HELP)
    r.set_defaults(func=cmd_run)

    s = nsub.add_parser("show", help="List or show eval sets and runs.")
    s.add_argument("--set", type=int, default=None, help="Show one set.")
    s.add_argument("--run", type=int, default=None, help="Show one run.")
    s.add_argument("--item", type=int, default=None, help="List sets and runs for an item.")
    s.add_argument("--json", action="store_true", help=_JSON_HELP)
    s.set_defaults(func=cmd_show)

    ov = nsub.add_parser("overview", help="Describe the eval noun.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_overview)
