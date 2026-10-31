"""Contract: ``docs/api/eval.md`` is true of the live eval API, for code clients.

culture-rules (agentculture/culture-rules#121) calls the eval routes from code, so the doc
``docs/api/eval.md`` is a contract. This test makes it machine-checked. It parses the doc:
under each ``### METHOD /path`` heading in "Routes", the labelled fenced ``json`` blocks
(``Query parameters:``, ``Request body:`` and ``Response NNN:``) are the documented request and
response; under "Errors", every ``### NNN code`` heading has one block. Then it **replays the
doc** in order against a real app over a seeded temp store, using a real minted key over the
real auth path, and asserts:

* every route replies with the documented status;
* every documented response key is present in the live reply (recursively for objects, the
  first element for lists) with a compatible JSON type, so renaming or removing a field in
  the code fails here;
* every documented request key (body and query) is one the route's OpenAPI schema accepts,
  so renaming an input fails here too;
* every ``/api/eval`` route in the app has a heading in the doc, so a new route without docs
  fails.

Ids are deterministic in a fresh store (item 1, set 1, runs 1 and 2), so the doc's literal
requests replay verbatim. When a shape legitimately changes, update the doc from a live call.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import keys  # noqa: E402
from culture_library.core import Store, eval_store  # noqa: E402
from culture_library.core import indexer as ix  # noqa: E402
from culture_library.core import search as sr  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402


def _pad(v):  # vectors have the current strategy's 768 dimensions (decision c42)
    return [*v, *[0.0] * (768 - len(v))]


DOC = Path(__file__).resolve().parents[2] / "docs" / "api" / "eval.md"

WORDS = ("whale", "ship")
# One ~700-char paragraph per word, so the 1000-char chunker gives each its own chunk.
TEXT = "\n\n".join(f"The {w} story. " + "and then " * 80 for w in WORDS).encode()
TITLE = "Daily newspaper 2026-10-30"

# Placeholder -> the deterministic id a fresh seeded store assigns.
PATH_IDS = {"item_id": 1, "set_id": 1, "run_id": 1}


def _embed(texts):
    return [_pad([1.0 if w in t.lower() else 0.0 for w in WORDS] + [0.1]) for t in texts]


def seeded(tmp_path):
    """(store, TestClient, raw_key): an indexed newspaper item and a read+write key.

    The item is id 1; nothing else exists, so the first set and run get id 1.
    """
    app = server_app.create_app()
    store = app.state.store = Store(tmp_path / "data")
    app.state.query_embedder = _embed
    app.state.embedder = None
    app.state.reranker = None
    item = store.add_item(
        TEXT, kind="newspaper", title=TITLE, format="txt", visibility="public"
    ).item
    with store.connect() as conn:
        sr.index_title(conn, item)
    ix.enqueue_missing(store)
    ix.Indexer(store, _embed).run_once()
    _key, raw = keys.mint(store, "culture-rules eval", ["read", "write"])
    return store, TestClient(app), raw


# ---------------------------------------------------------------- doc parsing

_ROUTE = re.compile(r"^### `?(GET|POST|PUT|DELETE) (/\S+?)`?$")
_ERROR = re.compile(r"^### `?(\d{3}) (\w+)`?$")
_LABEL = re.compile(r"^(Query parameters|Request body|Response (\d{3})):\s*$")


def _blocks(lines):
    """Yield (label, parsed json) for each labelled ```json block in ``lines``."""
    label = None
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _LABEL.match(line.strip())
        if m:
            label = m.group(1)
        elif line.startswith("```json") and label:
            j = i + 1
            while not lines[j].startswith("```"):
                j += 1
            yield label, json.loads("\n".join(lines[i + 1 : j]))
            label, i = None, j
        i += 1


def _key(label):
    return "query" if label == "Query parameters" else "body"


def _used(call):
    return {k for k in ("query", "body") if call[k] is not None}


def parse_doc(text: str):
    """(routes, errors): routes is an ordered list of dicts; errors maps 'NNN code' to a body."""
    routes, errors = [], {}
    section = None
    current = None
    for chunk in re.split(r"(?m)^(?=#{2,3} )", text):
        lines = chunk.splitlines()
        if not lines:
            continue
        head = lines[0]
        if head.startswith("## "):
            section = head[3:].strip()
            continue
        route, err = _ROUTE.match(head), _ERROR.match(head)
        if route and section == "Routes":
            current = {"method": route.group(1), "path": route.group(2), "calls": []}
            for label, data in _blocks(lines[1:]):
                calls = current["calls"]
                new = label in ("Query parameters", "Request body")
                if not calls or (
                    new and (_key(label) in _used(calls[-1]) or calls[-1]["responses"])
                ):
                    calls.append({"query": None, "body": None, "responses": {}})
                if label == "Query parameters":
                    calls[-1]["query"] = data
                elif label == "Request body":
                    calls[-1]["body"] = data
                else:
                    calls[-1]["responses"][int(label.split()[1])] = data
            routes.append(current)
        elif err and section == "Errors":
            blocks = [d for _l, d in _blocks([f"Response {err.group(1)}:", *lines[1:]])]
            assert len(blocks) == 1, f"error heading {head!r} needs one json block"
            errors[f"{err.group(1)} {err.group(2)}"] = blocks[0]
    return routes, errors


# ------------------------------------------------------------- shape checking

_TYPES = {
    dict: "object",
    list: "array",
    str: "string",
    bool: "boolean",
    int: "number",
    float: "number",
    type(None): "null",
}


def _kind(v) -> str:
    return _TYPES[type(v)]


def check_shape(doc, live, where="$") -> list[str]:
    """Problems where ``live`` lacks something ``doc`` documents (extra live keys are fine)."""
    if doc is None:  # documented as null: the type is not pinned
        return []
    if live is None:  # a nullable field that is null in this reply
        return []
    if _kind(doc) != _kind(live):
        return [f"{where}: documented {_kind(doc)}, live {_kind(live)}"]
    if isinstance(doc, dict):
        out = []
        for key, sub in doc.items():
            if key not in live:
                out.append(f"{where}.{key}: documented but missing from the response")
            else:
                out += check_shape(sub, live[key], f"{where}.{key}")
        return out
    if isinstance(doc, list) and doc:
        if not live:
            return [f"{where}: documented a non-empty list, live is empty"]
        return check_shape(doc[0], live[0], f"{where}[0]")
    return []


# ------------------------------------------------------------ request checking


def _resolve(schema, spec):
    while "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    return schema


def _variants(schema, spec):
    schema = _resolve(schema, spec)
    subs = schema.get("anyOf") or schema.get("oneOf")
    if subs:
        return [v for s in subs for v in _variants(s, spec)]
    return [schema]


def check_request_keys(example, schema, spec, where="body") -> list[str]:
    """Documented request keys the OpenAPI schema does not accept."""
    out = []
    if isinstance(example, list):
        for variant in _variants(schema, spec):
            if variant.get("type") == "array":
                if example:
                    out += check_request_keys(example[0], variant["items"], spec, where + "[0]")
                return out
        return out
    if not isinstance(example, dict):
        return out
    variants = [v for v in _variants(schema, spec) if v.get("properties")]
    if not variants:
        return out  # a free-form object (config, chunker, ...): any keys
    props = {}
    for v in variants:
        props.update(v["properties"])
    for key, sub in example.items():
        if key not in props:
            out.append(f"{where}.{key}: not accepted by the route")
        else:
            out += check_request_keys(sub, props[key], spec, f"{where}.{key}")
    return out


# ----------------------------------------------------------------- the checks


@pytest.fixture(scope="module")
def parsed():
    assert DOC.is_file(), f"{DOC} is missing"
    return parse_doc(DOC.read_text(encoding="utf-8"))


@pytest.fixture
def server(tmp_path):
    store, client, raw = seeded(tmp_path)
    return store, client, {"Authorization": "Bearer " + raw}


def _concrete(path: str) -> str:
    return path.format(**PATH_IDS)


def _wait_done(client, headers, run_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = client.get(f"/api/eval/runs/{run_id}", headers=headers).json()
        if got["status"] != "running":
            return got
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} still running")


def test_every_eval_route_is_documented(parsed):
    routes, _errors = parsed
    documented = {(r["method"], r["path"]) for r in routes}
    paths = server_app.create_app().openapi()["paths"]
    live = {
        (method.upper(), path)
        for path, ops in paths.items()
        if path.startswith("/api/eval")
        for method in ops
    }
    assert live, "no /api/eval routes found"
    assert live - documented == set(), f"undocumented eval routes: {sorted(live - documented)}"
    assert documented - live - {("PUT", "/api/library/{item_id}/sources")} == set(), (
        "documented routes the app does not serve: "
        f"{sorted(documented - live - {('PUT', '/api/library/{item_id}/sources')})}"
    )


def test_doc_requests_match_the_route_schemas(parsed):
    routes, _ = parsed
    spec = server_app.create_app().openapi()
    problems = []
    for r in routes:
        op = spec["paths"].get(r["path"], {}).get(r["method"].lower())
        assert op is not None, f"{r['method']} {r['path']} is not in the OpenAPI schema"
        accepted = {p["name"] for p in op.get("parameters", [])}
        for call in r["calls"]:
            for name in call["query"] or {}:
                if name not in accepted:
                    problems.append(f"{r['method']} {r['path']}: query parameter {name!r} unknown")
            if call["body"] is not None:
                schema = op["requestBody"]["content"]["application/json"]["schema"]
                problems += [
                    f"{r['method']} {r['path']}: {p}"
                    for p in check_request_keys(call["body"], schema, spec)
                ]
    assert problems == []


def _replay(client, headers, routes):
    """Send every documented call in doc order; yield (label, call, response)."""
    for r in routes:
        for call in r["calls"]:
            resp = client.request(
                r["method"],
                _concrete(r["path"]),
                params=call["query"],
                json=call["body"],
                headers=headers,
            )
            yield f"{r['method']} {r['path']}", call, resp
            if r["path"] == "/api/eval/runs/start" and resp.status_code == 202:
                _wait_done(client, headers, resp.json()["run_id"])  # finish before the next read


def test_doc_replays_and_responses_have_every_documented_field(parsed, server):
    routes, _ = parsed
    _store, client, headers = server
    assert routes, "the doc documents no routes"
    problems = []
    for label, call, resp in _replay(client, headers, routes):
        status = min(call["responses"])  # the success reply is the lowest status documented
        assert resp.status_code == status, f"{label}: {resp.status_code} {resp.text}"
        problems += [f"{label}: {p}" for p in check_shape(call["responses"][status], resp.json())]
    assert problems == []


def _code(body: dict) -> str:
    """The ``code`` of an error body: flat, nested under ``detail`` (auth), or FastAPI's list."""
    if "code" in body:
        return body["code"]
    if isinstance(body.get("detail"), dict):
        return body["detail"]["code"]
    return "validation"  # FastAPI's own {"detail": [{loc, msg, type}]} body check


def _scenarios(store, client, headers):
    """One live call per documented error, keyed by the doc's 'NNN code' heading."""
    read_only = keys.mint(store, "ro", ["read"])[1]
    out = {}

    def call(name, method, path, **kw):
        kw.setdefault("headers", headers)
        out[name] = client.request(method, path, **kw)

    call("401 unauthorized", "GET", "/api/eval/runs/1", headers={})
    out["403 forbidden"] = client.post(
        "/api/eval/runs/start",
        json={"set_id": 1},
        headers={"Authorization": "Bearer " + read_only},
    )
    call("404 not_found", "GET", "/api/eval/runs/999")
    call("422 invalid", "GET", "/api/eval/radar", params={"run": "abc"})
    call(
        "422 needs_judge", "POST", "/api/eval/runs/start", json={"set_id": 1, "suites": ["judged"]}
    )
    call("422 validation", "POST", "/api/eval/runs/start", json={})
    with store.connect() as conn:
        running = eval_store.create_run(
            conn, 1, status="partial", config={}, total=5, completed=0, running=True
        )
    call("409 running", "POST", "/api/eval/runs/start", json={"set_id": 1})
    assert out["409 running"].json()["run_id"] == running
    with store.connect() as conn:
        eval_store.close_running_runs(conn, "test over")
    # stale: a set whose item is not indexed (a second, never-indexed item)
    item = store.add_item(
        b"unindexed", kind="newspaper", title="Unindexed", format="txt", visibility="public"
    ).item
    created = client.post(
        "/api/eval/sets",
        json={
            "item_id": item.id,
            "item_sha256": item.sha256,
            "adapter": "culture-rules",
            "samples": [{"question": "q?", "reference_chunk_shas": ["x"]}],
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    call("409 stale", "POST", "/api/eval/runs/start", json={"set_id": created.json()["id"]})
    return out


def test_documented_errors_are_what_the_api_returns(parsed, server):
    routes, errors = parsed
    store, client, headers = server
    assert errors, "the doc documents no errors"
    list(_replay(client, headers, routes))  # so set 1 and its runs exist
    live = _scenarios(store, client, headers)
    assert set(errors) == set(live), f"error headings {sorted(errors)} vs scenarios {sorted(live)}"
    problems = []
    for name, doc in errors.items():
        status, code = name.split()
        got = live[name]
        assert got.status_code == int(status), f"{name}: {got.status_code} {got.text}"
        body = got.json()
        assert _code(body) == code, f"{name}: code {_code(body)!r}"
        problems += [f"{name}: {p}" for p in check_shape(doc, body)]
    assert problems == []


def test_check_shape_catches_a_rename_or_removal():
    doc = {"a": 1, "b": {"c": "x"}, "d": [{"e": True}]}
    assert check_shape(doc, {"a": 2, "b": {"c": "y"}, "d": [{"e": False, "f": 1}]}) == []
    assert check_shape(doc, {"a": 2, "b": {"cc": "y"}, "d": [{"e": False}]})  # renamed
    assert check_shape(doc, {"b": {"c": "y"}, "d": [{"e": False}]})  # removed
    assert check_shape(doc, {"a": "1", "b": {"c": "y"}, "d": [{"e": False}]})  # type changed
    assert check_shape(doc, {"a": 2, "b": {"c": "y"}, "d": []})  # list emptied
