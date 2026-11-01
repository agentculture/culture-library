"""Regenerate docs/api/eval.md's JSON examples from a seeded app (see tests/contract/test_eval_contract.py).

Run from the repo root: `uv run python scripts/gen-eval-api-doc.py`. Re-run it when the chunker
or the eval responses change and the contract replay test reports an example mismatch.
"""

import copy
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.contract import test_eval_contract as t

OUT = ROOT / "docs" / "api" / "eval.md"
d = pathlib.Path(tempfile.mkdtemp())
store, c, raw = t.seeded(d)
H = {"Authorization": "Bearer " + raw}
chunks = [
    x for x in c.get("/api/rag/items/1/chunks", headers=H).json()["chunks"] if not x["is_header"]
]
sha = c.get("/api/library/1", headers=H).json()["sha256"]


def J(o):
    return "```json\n" + json.dumps(o, indent=2) + "\n```\n"


class Route:
    def __init__(self, method, path, concrete=None):
        self.method, self.path, self.concrete = method, path, concrete or path
        self.calls = []

    def call(self, query=None, body=None, trim=None, intro=None):
        r = c.request(self.method, self.concrete, params=query, json=body, headers=H)
        assert r.status_code < 300, (self.path, r.status_code, r.text)
        resp = r.json()
        if self.path == "/api/eval/runs/start":
            t._wait_done(c, H, resp["run_id"])
        shown = copy.deepcopy(resp)
        if trim:
            trim(shown)
        self.calls.append((query, body, r.status_code, shown, intro))
        return resp

    def render(self, title, prose, params="", statuses=""):
        out = [f"### {self.method} {self.path}\n", prose.strip() + "\n"]
        if params:
            out.append(params.strip() + "\n")
        for query, body, status, shown, intro in self.calls:
            if intro:
                out.append(intro.strip() + "\n")
            if query is not None:
                out.append("Query parameters:\n\n" + J(query))
            if body is not None:
                out.append("Request body:\n\n" + J(body))
            out.append(f"Response {status}:\n\n" + J(shown))
        if statuses:
            out.append(statuses.strip() + "\n")
        return "\n".join(out)


secs = {}

# 1. sources
r = Route("PUT", "/api/library/{item_id}/sources", "/api/library/1/sources")
r.call(
    body=[
        {
            "article_title": "Whales return to the bay",
            "source_urls": ["https://example.org/whale"],
            "excerpt": "Whales were seen in the bay this week.",
        },
        {
            "article_title": "Harbour opens a new ship berth",
            "source_urls": ["https://example.org/ship", "https://example.org/harbour"],
            "excerpt": "The new berth takes ships up to 200 m.",
        },
    ]
)
secs["sources"] = r.render(
    "",
    """
Not an eval route, but the step before every newspaper eval: culture-rules pushes the issue,
then this sidecar. The sidecar lists, per article, the source URLs behind it. Eval sets can
carry the same URLs on their questions, and `weakest` groups its answer by those URLs.

The body is a bare JSON list of at most 500 entries. Each entry needs a non-empty
`article_title`, a non-empty `source_urls` list of `http(s)` URLs, and an `excerpt` string of
at most 2000 characters. The call replaces the whole manifest. Needs the `write` scope, and
the key's kind list must allow the item's kind. `GET` on the same path (scope `read`) returns
the same shape, or 404 `no_sources` when no sidecar exists. Sidecars append no feed event.
""",
    statuses="Status codes: 200; 401, 403 (scope or kind), 404 `not_found` (also for a private item the key cannot see), 422 `invalid_request` (a bad manifest).",
)

# 2. suites
r = Route("GET", "/api/eval/suites")


def trim_suites(s):
    s["suites"] = s["suites"][:1]
    s["suites"][0]["metrics"] = s["suites"][0]["metrics"][:1]


r.call(trim=trim_suites)
secs["suites"] = r.render(
    "",
    """
The suite registry: every suite the library knows, whether it needs a judge, and each metric's
definition, range and direction. Use it to know which names `suites` accepts and what a score
in a run means. The example is cut to one suite and one metric; the real reply lists all of
them (`freshness`, `id_retrieval`, `judged`, `result_quality`, `speed`).
""",
    statuses="Status codes: 200; 401, 403.",
)

# 3. sets
r = Route("POST", "/api/eval/sets")
samples = [
    {
        "question": "Where were whales seen this week?",
        "reference_answer": "In the bay.",
        "reference_chunk_shas": [chunks[0]["chunk_sha"]],
        "reference_contexts": ["The whale story. and then and then and then"],
        "source_urls": ["https://example.org/whale"],
    },
    {
        "question": "What size of vessel can the new berth take?",
        "reference_answer": "Up to 200 m.",
        "reference_chunk_shas": [chunks[1]["chunk_sha"]],
        "reference_contexts": ["The ship story. and then and then and then"],
        "source_urls": ["https://example.org/ship"],
    },
]
r.call(
    body={
        "item_id": 1,
        "item_sha256": sha,
        "adapter": "culture-rules",
        "adapter_version": "1",
        "chunker": {},
        "generator": {"by": "culture-rules", "questions": "newspaper sources"},
        "samples": samples,
    }
)
secs["set_post"] = r.render(
    "",
    """
Create an eval set: your own questions for one item, each anchored to the chunk(s) that
answer it. The set starts at `version` 1.

| Field | Type | Notes |
|-------|------|-------|
| `item_id` | int | the item the questions are about |
| `item_sha256` | string | the item's current sha256; a mismatch is 409 `item_changed` |
| `adapter` | string | who made the set, for example `culture-rules` |
| `adapter_version` | string | optional |
| `chunker` | object | optional, free-form |
| `generator` | object | optional, free-form provenance |
| `samples` | list | the questions |

Each sample has `question` (required), `reference_answer`, `reference_chunk_shas` (the
`chunk_sha` values from `GET /api/rag/items/{id}/chunks` that answer it; this is what runs
score against), `reference_contexts` (the reference text itself, which lets `reanchor` find
the passage again after a re-index) and `source_urls`. Anchor by `chunk_sha`, never by chunk
number: re-indexing renumbers chunks.

Needs the `write` scope for the item's kind. The item must be visible to the key.
""",
    statuses="Status codes: 201; 401, 403, 404 `not_found` (item missing, archived or hidden), 409 `item_changed`, 422 `invalid`, and 422 for a malformed body (FastAPI's own shape, see Errors).",
)

r = Route("GET", "/api/eval/sets")
r.call(query={"item_id": "1"})
secs["sets"] = r.render(
    "",
    """
List sets, oldest first (by id), without their samples. `item_id` is optional and
filters to one item. Sets of items the key cannot see are left out. Needs `read`.
""",
    statuses="Status codes: 200; 401, 403.",
)

r = Route("GET", "/api/eval/sets/{set_id}", "/api/eval/sets/1")
r.call()
secs["set_get"] = r.render(
    "",
    """
One set with its samples. The sample `id` is what runs refer to as `sample_id`. Needs `read`.
""",
    statuses="Status codes: 200; 401, 403, 404 `not_found` (also for a set whose item is hidden).",
)

# 4. start
r = Route("POST", "/api/eval/runs/start")
r.call(body={"set_id": 1}, intro="Start a run with the defaults:")
r.call(
    body={"set_id": 1, "mode": "semantic", "k": 1},
    intro="Start a second run on the same set with a different retriever setting, to compare against the first:",
)
secs["start"] = r.render(
    "",
    """
Start a judge-free run on the server. It scores retrieval only (ID metrics, speed and
freshness); it never calls an LLM. The reply is immediate (202); the run works in a
background thread. Poll `GET /api/eval/runs/{run_id}` until `status` is no longer `running`.

| Field | Type | Notes |
|-------|------|-------|
| `set_id` | int | required |
| `suites` | list of names | optional; default is every judge-free suite |
| `mode` | `semantic` or `hybrid` | default `hybrid` |
| `k` | int 1-50 | default 5 |
| `rerank` | bool | optional; default is the server's setting |
| `candidates` | int 1-200, at least `k` | optional |
| `warmup` | bool | default false; one un-timed call first |

The run retrieves in process with the starting key's visibility, so it never retrieves an
item the key cannot see, and its latency has no HTTP in it (`config.latency_source` is
`in-process`). Needs the `write` scope for the set's item kind. Only one run per set may be
running at a time. A server restart closes a running run as `partial` (or `failed` when no
question was done) and records `config.closed_reason`.

A judged suite (`judged`) is refused with 422 `needs_judge`: use the CLI, see
[What needs the CLI](#what-needs-the-cli-or-the-python-client).
""",
    statuses="Status codes: 202; 401, 403, 404 `not_found`, 409 `running` (names the `run_id`), 409 `stale` (item archived, missing or not fully indexed; has a `hint`), 422 `invalid`, 422 `needs_judge`, 422 body validation.",
)

# 5. get run
r = Route("GET", "/api/eval/runs/{run_id}", "/api/eval/runs/1")
r.call()
secs["run_get"] = r.render(
    "",
    """
One run with its per-question results. While it is working, `status` is `running` and
`completed` climbs towards `total`; the rest of the shape is the same, with `aggregates` still
empty. A finished run is `complete` (all questions done), `partial` or `failed`; `aggregates`
exist only on `complete` and `partial_aggregates` carries what a `partial` run managed. Scores
that could not be measured are `null` (for example `found_at_5` when `k` is below 5).
`error_rate` is the share of questions that errored. Needs `read`.
""",
    statuses="Status codes: 200; 401, 403, 404 `not_found` (absent, or the run's item is hidden).",
)

r = Route("GET", "/api/eval/runs")


def trim_runs(s):
    s["runs"] = s["runs"][:1]


r.call(query={"set_id": "1"}, trim=trim_runs)
secs["runs"] = r.render(
    "",
    """
List runs, oldest first (by id), without their per-question samples. `set_id` is optional and filters to one set.
Runs whose item the key cannot see are left out. The example is cut to the first run.
Needs `read`.
""",
    statuses="Status codes: 200; 401, 403.",
)

# 6. push run
r = Route("POST", "/api/eval/runs")
run1 = c.get("/api/eval/runs/1", headers=H).json()
pushed = {
    "set_id": 1,
    "status": "complete",
    "config": {"mode": "hybrid", "k": 5, "source": "culture-rules"},
    "total": 2,
    "completed": 2,
    "aggregates": {"found_at_5": 1.0, "mrr": 0.75},
    "suites": ["id_retrieval"],
    "samples": [
        {
            "sample_id": x["sample_id"],
            "retrieved_chunk_shas": x["retrieved_chunk_shas"][:2],
            "scores": {"found_at_5": 1.0, "mrr": 1.0 if i else 0.5},
            "question": x["question"],
            "latency_ms": 41.5,
        }
        for i, x in enumerate(run1["samples"])
    ],
}
r.call(body=pushed)
secs["run_post"] = r.render(
    "",
    """
Store a run your own client scored. Use it when you retrieve and score on your side and only
want the library to keep the result and draw its radar. Most clients should prefer
`POST /api/eval/runs/start`, which needs no scoring code.

`status` is `complete` (default), `partial` or `failed`. `aggregates` are accepted only with
`complete` (422 otherwise). Every `sample_id` must be a sample of the set (422 otherwise).
`suites` is a list of suite names; the optional per-sample fields (`question`,
`reference_chunk_shas`, `set_version`, `retrieved_item_ids`, `latency_ms`, `freshness`)
snapshot what the run asked and got, and the radar and `weakest` read them. A run is
compared to another only when both ran the same suites, so push the full default suite list
if you want to `compare` it with a server run. Needs `write` for the set's item kind.
""",
    statuses="Status codes: 201; 401, 403, 404 `not_found`, 422 `invalid`, 422 for a malformed body.",
)

# 7. radar
r = Route("GET", "/api/eval/radar")
r.call(query={"run": "1"}, intro="One run:")
r.call(
    query={"runs": "1", "vs": "2"},
    intro="A group (`runs`, mean over its documents) set against another run (`vs`):",
)
secs["radar"] = r.render(
    "",
    """
The radar: ten axes, each 0 to 1 with higher better, for one run, a group of runs, or two
of them side by side.

| Axis | Meaning |
|------|---------|
| `found_at_1`, `found_at_3`, `found_at_5` | share of questions with a reference passage in the top 1, 3 or 5 |
| `mrr` | mean reciprocal rank |
| `judge_precision`, `judge_recall` | judged context precision and recall (null on judge-free runs) |
| `no_errors` | one minus the error rate |
| `latency_in_budget` | share of questions answered within `budget_s` |
| `no_duplicates` | one minus the duplicate rate |
| `fresh` | share of questions whose reference chunks still exist |

An axis the run could not measure is `null`.

| Parameter | Notes |
|-----------|-------|
| `run` | one run id |
| `runs` | comma-separated run ids (at most 200), a group; give exactly one of `run` and `runs` |
| `vs` | optional; a run id or a comma-separated group, to get a pair |
| `budget_s` | latency budget in seconds, a float above 0, default 1.0; echoed back |

`kind` is `run`, `group` or `pair`. A pair has `a` and `b`, each shaped like a single reply.
`documents` has one entry per run with its question count. In a group, a run the key cannot
see is left out of every mean and counted in `left_out`. Needs `read`.
""",
    statuses="Status codes: 200; 401, 403, 404 `not_found` (a single run that is absent or hidden), 422 `invalid`.",
)

# 8. compare
r = Route("GET", "/api/eval/compare")
r.call(query={"before": "1", "after": "2"})
secs["compare"] = r.render(
    "",
    """
Compare two runs (or two groups) after a change, such as a new embed model or a reindex.

| Parameter | Notes |
|-----------|-------|
| `before` | run id, or comma-separated ids (at most 200) |
| `after` | run id, or comma-separated ids |
| `budget_s` | as for the radar |

`checks.comparable` says whether the two sides can be compared; `reasons` says why not and
`warnings` notes differences that still allow it (here, a different `k`; others are a
different set or set version, a run that is not `complete`, or a suite version change). Two
runs are not comparable when they ran different suites or used a different judge model.
Groups are paired by `set_id`, and an unpaired set is a warning. `deltas` is after minus before, per axis. `changed_questions` lists the
questions whose scores moved, worst `delta` first. When the runs are not comparable, the reply
has only `checks`, `left_out` and `budget_s`. Needs `read`.
""",
    statuses="Status codes: 200 (also when not comparable; read `checks.comparable`); 401, 403, 404 `not_found` (a single run absent or hidden), 422 `invalid`.",
)

# 9. weakest
r = Route("GET", "/api/eval/weakest")
r.call(query={"run": "2", "limit": "5"})
secs["weakest"] = r.render(
    "",
    """
Where retrieval is weakest. `sources` groups the run's questions by the `source_urls` of their
samples; `items` groups by library item. Each list is sorted weakest first (`found_at_5`, then
`mrr`) and cut to `limit`.

| Parameter | Notes |
|-----------|-------|
| `run` or `runs` | one run id, or comma-separated ids; give exactly one |
| `limit` | default 10, at least 1 |
| `budget_s` | as for the radar |

Each row carries the `run_id` and `set_id` it came from, the number of `questions` behind it
and the ten radar `axes`. This is the list to turn into a fix: a source URL whose articles
the library does not retrieve well. Needs `read`.
""",
    statuses="Status codes: 200; 401, 403, 404 `not_found` (with `run`, when it is absent or hidden), 422 `invalid`.",
)

# 10. reanchor
r = Route("POST", "/api/eval/sets/{set_id}/reanchor", "/api/eval/sets/1/reanchor")
r.call()
secs["reanchor"] = r.render(
    "",
    """
After the item was re-indexed or replaced, map each sample's stored `reference_contexts` onto
the item's current chunks and rewrite its `reference_chunk_shas` in place. Sample ids are kept.
`version` goes up by one only when some sample's shas changed (here nothing moved, so it stays
1). A sample with no stored reference text, or whose text is no longer in the item, is listed
in `could_not_re_anchor` with a `reason` and keeps its old shas. `note` is also stored under
the set's `generator.reanchor`. No body. Needs `write` for the item's kind.
""",
    statuses="Status codes: 200; 401, 403, 404 `not_found`, 409 `not_indexed` (the item has no indexed chunks yet), 409 `index_pending` (the item is being re-indexed; re-anchor when that is done), 503 `rag_unavailable` (the query embedder cannot state which vectors it reads).",
)

# errors
sc = t._scenarios(store, c, H)
errs = {
    "401 unauthorized": "No `Authorization` header, or a key that does not exist or was revoked. Note the nesting: auth errors come wrapped in a `detail` object.",
    "403 forbidden": "The key lacks the scope the route needs (`read` for reads, `write` for the others; `admin` passes everything). A key whose kind list excludes the item's kind gets `forbidden_kind` instead. Same nesting as 401.",
    "404 not_found": "The set, run or item does not exist, or its item is private and the key cannot see it. The two cases are not told apart.",
    "409 running": "The set already has a run in progress; the body names it. Poll that run instead.",
    "409 stale": "The set's item is archived, missing or not fully indexed. `hint` says what to do.",
    "422 invalid": "A query parameter or field is out of range or malformed; `detail` says which.",
    "422 needs_judge": "The requested suite needs an LLM judge, which the server never runs.",
    "422 validation": "The JSON body is malformed or lacks a required field. This is FastAPI's own reply: `detail` is a list, and there is no `code`.",
}
err_out = []
for k in [
    "401 unauthorized",
    "403 forbidden",
    "404 not_found",
    "409 running",
    "409 stale",
    "422 invalid",
    "422 needs_judge",
    "422 validation",
]:
    body = sc[k].json()
    err_out.append(f"### {k}\n\n{errs[k]}\n\n" + J(body))
# fix 422 needs_judge body (seen live)
open(
    "/tmp/claude-1000/-home-spark-git-culture-library/370383b2-08c9-498d-aa77-1d90975ed9e5/scratchpad/errs.txt",
    "w",
).write("\n".join(err_out))

head = open(
    "/tmp/claude-1000/-home-spark-git-culture-library/370383b2-08c9-498d-aa77-1d90975ed9e5/scratchpad/head.md"
).read()
tail = open(
    "/tmp/claude-1000/-home-spark-git-culture-library/370383b2-08c9-498d-aa77-1d90975ed9e5/scratchpad/tail.md"
).read()
order = [
    "sources",
    "suites",
    "set_post",
    "sets",
    "set_get",
    "start",
    "run_get",
    "runs",
    "run_post",
    "radar",
    "compare",
    "weakest",
    "reanchor",
]
doc = (
    head + "\n## Routes\n\n" + "\n".join(secs[k] for k in order) + "\n## Errors\n\n" + errs_intro
    if False
    else None
)
routes_md = "\n".join(secs[k] for k in order)
OUT.write_text(
    head
    + "\n## Routes\n\n"
    + routes_md
    + "\n## Errors\n\n"
    + tail.replace("@@ERRORS@@", "\n".join(err_out))
)
print("written", OUT)
