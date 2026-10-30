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


def _make_judge(args: argparse.Namespace | None = None):
    """The judge: --judge-* flags, else CULTURE_LIBRARY_EVAL_LLM_* env, else defaults."""
    from culture_library.eval.config import JudgeConfig

    return JudgeConfig.from_env(
        url=getattr(args, "judge_url", None),
        model=getattr(args, "judge_model", None),
        max_tokens=getattr(args, "judge_max_tokens", None),
    )


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
        raise CliError(code, text, getattr(exc, "hint", "") or _default_hint(text)) from None


def _default_hint(text: str) -> str:
    if _INSTALL in text:
        return _INSTALL
    return "run 'culture-library explain eval' for usage"


# ---- verbs ---------------------------------------------------------------------------------


def _judge_items() -> list[str]:
    from culture_library.eval.config import judge_settings

    return [f"{name}: {value} ({source})" for name, value, source in judge_settings()] + [
        "flags: --judge-url URL, --judge-model NAME, --judge-max-tokens N on generate and run",
        "env: CULTURE_LIBRARY_EVAL_LLM_URL / _MODEL / _MAX_TOKENS / _KEY (non-gateway judge)",
    ]


def _judge_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--judge-url", help="Judge base URL (OpenAI-compatible; default the gateway).")
    p.add_argument("--judge-model", help="Judge model or gateway role (default cortex).")
    p.add_argument(
        "--judge-max-tokens", type=int, help="Judge output limit in tokens (default 8192)."
    )


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
                    "suites — list the metric suites a run can score (run --suites a,b)",
                    "radar --run ID | --runs IDS [--vs IDS] — ten-axis shape of a run or group",
                    "compare --before IDS --after IDS — before/after deltas (checks first)",
                    "reanchor SET_ID — re-anchor a set's samples after the item changed",
                    "overview — this description",
                ],
            },
            {
                "title": "Configuration",
                "items": [
                    f"{ENV_URL} (default {DEFAULT_URL})",
                    f"{ENV_KEY} (required)",
                    "install: pip install 'culture-library[eval]'",
                ],
            },
            {
                "title": "Judge (effective; --judge-url / --judge-model / --judge-max-tokens "
                "override it per command)",
                "items": _judge_items(),
            },
        ],
        json_mode=_json(args),
    )
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    def go() -> dict[str, Any]:
        from culture_library.eval import generate as gen

        judge = _make_judge(args)
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


def _suite_names(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        raise CliError(EXIT_USER_ERROR, "--suites needs at least one suite name", _SUITES_HINT)
    return names


def cmd_run(args: argparse.Namespace) -> int:
    suites = _suite_names(args.suites)

    def go() -> dict[str, Any]:
        from culture_library.eval import runner

        if suites is not None:
            _check_suites(suites)
        judge = None if args.no_llm else _make_judge(args)
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
            suites=suites,
            warmup=bool(args.warmup),
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


_SUITES_HINT = "run 'culture-library eval suites' to list the available suite names"


def _check_suites(names: list[str]) -> None:
    """Refuse an unknown suite before any retrieval, with a hint listing the real ones."""
    from culture_library.eval import suites as registry

    known = [n for n in names if _is_known(registry, n)]
    unknown = [n for n in names if n not in known]
    if unknown:
        raise CliError(EXIT_USER_ERROR, f"unknown suite: {', '.join(unknown)}", _SUITES_HINT)


def _is_known(registry, name: str) -> bool:
    try:
        registry.get(name)
    except KeyError:
        return False
    return True


def _ids(raw: str | None, flag: str) -> str | None:
    """Validate a comma-separated id list; returns it normalised or None when absent."""
    if raw is None:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts or not all(p.isdigit() for p in parts):
        raise CliError(
            EXIT_USER_ERROR, f"{flag} takes run ids like 3 or 3,4 (got {raw!r})", "pass integers"
        )
    return ",".join(parts)


def _axes_lines(axes: dict[str, Any], indent: str = "  ") -> list[str]:
    return [f"{indent}{k}: {_fmt(v)}" for k, v in axes.items()]


def cmd_suites(args: argparse.Namespace) -> int:
    def go() -> dict[str, Any]:
        return _make_client().request("GET", "/api/eval/suites")

    data = _map_errors(go)
    lines: list[str] = []
    for s in data.get("suites", []):
        judge = " (needs judge)" if s.get("needs_judge") else ""
        lines.append(f"{s['name']} v{s.get('version', '?')}{judge}")
        for m in s.get("metrics", []):
            lines.append(
                f"  {m['name']} [{m.get('range', '')}, {m.get('direction', '')}]: "
                f"{m.get('definition', '')}"
            )
    _emit(args, data, "\n".join(lines) or "no suites")
    return 0


def _radar_query(args: argparse.Namespace) -> str:
    run, runs = _ids(args.run, "--run"), _ids(args.runs, "--runs")
    vs = _ids(args.vs, "--vs")
    if (run is None) == (runs is None):
        raise CliError(EXIT_USER_ERROR, "pass exactly one of --run or --runs", "e.g. --run 3")
    q = f"run={run}" if run is not None else f"runs={runs}"
    if vs is not None:
        q += f"&vs={vs}"
    if args.budget_s is not None:
        q += f"&budget_s={args.budget_s}"
    return q


def _radar_text(data: dict[str, Any]) -> str:
    if data.get("kind") == "pair":
        lines = [f"pair (budget {data.get('budget_s')}s)"]
        for side in ("a", "b"):
            lines.append(f"{side}:")
            part = data.get(side) or {}
            lines += _axes_lines(part.get("axes", part))
        return "\n".join(lines)
    lines = [f"{data.get('kind', 'run')} (budget {data.get('budget_s')}s)"]
    lines += _axes_lines(data.get("axes", {}), "")
    for d in data.get("documents", []):
        lines.append(f"run {d.get('run_id')} set {d.get('set_id')}: {d.get('questions')} questions")
    if data.get("left_out"):
        lines.append(f"left out: {data['left_out']}")
    return "\n".join(lines)


def cmd_radar(args: argparse.Namespace) -> int:
    query = _radar_query(args)
    data = _map_errors(lambda: _make_client().request("GET", f"/api/eval/radar?{query}"))
    _emit(args, data, _radar_text(data))
    return 0


def _compare_text(data: dict[str, Any]) -> str:
    checks = data.get("checks") or {}
    lines = []
    if checks.get("comparable"):
        lines.append("comparable: yes")
    else:
        lines.append("comparable: NO")
    lines += [f"  reason: {r}" for r in checks.get("reasons", [])]
    lines += [f"  warning: {w}" for w in checks.get("warnings", [])]
    if "deltas" not in data:
        if data.get("left_out"):
            lines.append(f"left out: {data['left_out']}")
        return "\n".join(lines)
    before, after = (data.get(k) or {} for k in ("before", "after"))
    deltas = data.get("deltas", {})
    lines.append("axis: before -> after (delta)")
    for axis in after or before:
        lines.append(
            f"  {axis}: {_fmt(before.get(axis))} -> {_fmt(after.get(axis))} "
            f"({_fmt(deltas.get(axis))})"
        )
    for c in data.get("changed_questions", []):
        lines.append(
            f"  q: {c.get('question')} ({_fmt(c.get('delta'))}): "
            + ", ".join(
                f"{a} {_fmt(v)} -> {_fmt((c.get('after') or {}).get(a))}"
                for a, v in (c.get("before") or {}).items()
                if _fmt(v) != _fmt((c.get("after") or {}).get(a))
            )
        )
    if data.get("left_out"):
        lines.append(f"left out: {data['left_out']}")
    return "\n".join(lines)


def cmd_compare(args: argparse.Namespace) -> int:
    before, after = _ids(args.before, "--before"), _ids(args.after, "--after")
    path = f"/api/eval/compare?before={before}&after={after}"
    data = _map_errors(lambda: _make_client().request("GET", path))
    _emit(args, data, _compare_text(data))
    return 0


def cmd_reanchor(args: argparse.Namespace) -> int:
    path = f"/api/eval/sets/{int(args.set_id)}/reanchor"
    data = _map_errors(lambda: _make_client().request("POST", path))
    lost = data.get("could_not_re_anchor", [])
    lines = [
        f"set {data.get('set_id')}: version {data.get('previous_version')} -> "
        f"{data.get('version')}, {len(data.get('re_anchored', []))} re-anchored, "
        f"{len(lost)} could not be"
    ]
    lines += [f"  sample {x.get('sample_id')}: {x.get('reason')}" for x in lost]
    if data.get("note"):
        lines.append(str(data["note"]))
    _emit(args, data, "\n".join(lines))
    return 0


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


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
    _judge_opts(g)
    g.add_argument("--json", action="store_true", help=_JSON_HELP)
    g.set_defaults(func=cmd_generate)

    r = nsub.add_parser("run", help="Run an eval set against retrieval and store the run.")
    r.add_argument("set_id", type=int, help="Eval set id.")
    r.add_argument("--mode", choices=["semantic", "hybrid"], default="hybrid")
    r.add_argument("--k", type=int, default=5, help="Passages per query (default 5).")
    _bool_opt(r, "rerank", "Force reranking on (default: server decides).")
    r.add_argument("--candidates", type=int, default=None, help="Rerank candidate pool size.")
    _judge_opts(r)
    r.add_argument(
        "--no-llm", action="store_true", help="Skip LLM-judged metrics (ID metrics only)."
    )
    r.add_argument(
        "--suites",
        help="Comma-separated metric suites (see 'eval suites'; default the standard set).",
    )
    r.add_argument(
        "--warmup", action="store_true", help="Run one untimed retrieval first (warm caches)."
    )
    r.add_argument("--json", action="store_true", help=_JSON_HELP)
    r.set_defaults(func=cmd_run)

    s = nsub.add_parser("show", help="List or show eval sets and runs.")
    s.add_argument("--set", type=int, default=None, help="Show one set.")
    s.add_argument("--run", type=int, default=None, help="Show one run.")
    s.add_argument("--item", type=int, default=None, help="List sets and runs for an item.")
    s.add_argument("--json", action="store_true", help=_JSON_HELP)
    s.set_defaults(func=cmd_show)

    su = nsub.add_parser("suites", help="List metric suites and their metrics.")
    su.add_argument("--json", action="store_true", help=_JSON_HELP)
    su.set_defaults(func=cmd_suites)

    ra = nsub.add_parser("radar", help="Ten-axis shape of a run or a group of runs.")
    ra.add_argument("--run", help="One run id.")
    ra.add_argument("--runs", help="Comma-separated run ids (a group).")
    ra.add_argument("--vs", help="Run id(s) to pair against.")
    ra.add_argument("--budget-s", dest="budget_s", type=float, help="Latency budget in seconds.")
    ra.add_argument("--json", action="store_true", help=_JSON_HELP)
    ra.set_defaults(func=cmd_radar)

    co = nsub.add_parser("compare", help="Compare run groups before and after a change.")
    co.add_argument("--before", required=True, help="Comma-separated run ids.")
    co.add_argument("--after", required=True, help="Comma-separated run ids.")
    co.add_argument("--json", action="store_true", help=_JSON_HELP)
    co.set_defaults(func=cmd_compare)

    rn = nsub.add_parser("reanchor", help="Re-anchor a set's samples to the item's current text.")
    rn.add_argument("set_id", type=int, help="Eval set id.")
    rn.add_argument("--json", action="store_true", help=_JSON_HELP)
    rn.set_defaults(func=cmd_reanchor)

    ov = nsub.add_parser("overview", help="Describe the eval noun.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_overview)
