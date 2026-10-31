# Eval API for code clients

This is the contract for calling culture-library's RAG evaluation routes from code. It is
written for culture-rules (agentculture/culture-rules#121), which pushes newspapers with a
sources sidecar and wants to measure how well the library retrieves them. Every JSON example
below was produced by calling the real app over a seeded temp store, and
`tests/contract/test_eval_contract.py` replays this page against the app on every test run:
rename or remove a field in the code and that test fails until this page is updated.

Everything here works with a plain HTTP client and an API key. No ragas, no LLM judge and no
Python package is needed for the server-side flow.

## Conventions

**Base URL.** Machine clients use spark's loopback address, `http://127.0.0.1:8796`. People
use `https://library.culture.dev` behind Cloudflare Access; code does not.

**Auth.** Send `Authorization: Bearer <key>`. A key has scopes and, optionally, a list of item
kinds it may write:

- `read` covers every `GET` below.
- `write` covers creating sets, storing and starting runs, re-anchoring and the sources
  sidecar. A write also needs the item's kind to be on the key's kind list, if it has one
  (403 `forbidden_kind` otherwise). A newspaper pipeline's key therefore needs
  `read` and `write` with `newspaper` among its kinds.
- `admin` passes every check.

A request with no header, or a bad key, is 401. Any `Authorization` header decides on its own;
there is no fallback to another credential.

**Visibility.** Eval data follows the item it belongs to. A key that cannot see an item (a
private item, without the `private` flag on the key) gets 404 `not_found` for that item's
sets and runs, exactly as for something that does not exist. Lists leave such rows out, and a
group in `radar`, `compare` or `weakest` leaves such runs out of every mean and only counts
them in `left_out`. Runs started on the server retrieve with the starting key's visibility.

**Ids.** Item, set, run and sample ids are integers. Lists of run ids in query strings are
comma-separated, at most 200.

**Errors.** Route-level errors are flat JSON, `{"detail": "...", "code": "..."}`, with a
`hint` when there is a next step and a `run_id` on 409 `running`. Branch on `code`, not on
`detail`. Two cases differ, and the examples under Errors show both: auth errors (401, 403)
come wrapped as `{"detail": {"detail": "...", "code": "..."}}`, and a malformed request body
is FastAPI's own 422 with `detail` as a list and no `code`.

**Nulls.** A score the run could not measure is `null`, never 0. Treat `null` as "not
measured".

## Typical flow for culture-rules

1. Push the issue with `POST /api/library` as today, then `PUT /api/library/{id}/sources` with
   the issue's sources manifest.
2. Look up the issue's chunks with `GET /api/rag/items/{id}/chunks` (scope `read`), and for each
   question you want to ask, pick the chunk(s) that answer it. Create the set with
   `POST /api/eval/sets`: your questions, each with `reference_chunk_shas`, `reference_contexts`
   and the `source_urls` of the article.
3. Start a run with `POST /api/eval/runs/start` and keep the `run_id`.
4. Poll `GET /api/eval/runs/{run_id}` until `status` is not `running` (a second or two for a
   small set). Check it is `complete`.
5. Read `GET /api/eval/radar?run={run_id}` for the ten-axis picture and
   `GET /api/eval/weakest?run={run_id}` for the source URLs that retrieve worst.
6. After a change (a new embed model, a reindex, a different `k`), start a second run on the
   same set and call `GET /api/eval/compare?before={first}&after={second}`. Read
   `checks.comparable` first, then `deltas` and `changed_questions`.
7. If the item was replaced or re-indexed so that chunk shas moved,
   `POST /api/eval/sets/{set_id}/reanchor` rewrites the references from the stored
   `reference_contexts` before the next run.

Which of this runs where:

| Capability | Server (HTTP only) | Needs the CLI or Python client |
|------------|--------------------|--------------------------------|
| Store sets and runs, read radar, compare, weakest | yes | |
| Start a retrieval run with ID metrics, speed and freshness | yes | |
| Re-anchor a set | yes | |
| Judged suite (LLM-scored context precision and recall) | no | yes |
| Generate questions from a book or a sources sidecar | no | yes |

## Routes

### PUT /api/library/{item_id}/sources

Not an eval route, but the step before every newspaper eval: culture-rules pushes the issue,
then this sidecar. The sidecar lists, per article, the source URLs behind it. Eval sets can
carry the same URLs on their questions, and `weakest` groups its answer by those URLs.

The body is a bare JSON list of at most 500 entries. Each entry needs a non-empty
`article_title`, a non-empty `source_urls` list of `http(s)` URLs, and an `excerpt` string of
at most 2000 characters. The call replaces the whole manifest. Needs the `write` scope, and
the key's kind list must allow the item's kind. `GET` on the same path (scope `read`) returns
the same shape, or 404 `no_sources` when no sidecar exists. Sidecars append no feed event.

Request body:

```json
[
  {
    "article_title": "Whales return to the bay",
    "source_urls": [
      "https://example.org/whale"
    ],
    "excerpt": "Whales were seen in the bay this week."
  },
  {
    "article_title": "Harbour opens a new ship berth",
    "source_urls": [
      "https://example.org/ship",
      "https://example.org/harbour"
    ],
    "excerpt": "The new berth takes ships up to 200 m."
  }
]
```

Response 200:

```json
{
  "item_id": 1,
  "sources": [
    {
      "article_title": "Whales return to the bay",
      "excerpt": "Whales were seen in the bay this week.",
      "source_urls": [
        "https://example.org/whale"
      ]
    },
    {
      "article_title": "Harbour opens a new ship berth",
      "excerpt": "The new berth takes ships up to 200 m.",
      "source_urls": [
        "https://example.org/ship",
        "https://example.org/harbour"
      ]
    }
  ],
  "updated_at": "2026-10-30T12:50:07+00:00"
}
```

Status codes: 200; 401, 403 (scope or kind), 404 `not_found` (also for a private item the key cannot see), 422 `invalid_request` (a bad manifest).

### GET /api/eval/suites

The suite registry: every suite the library knows, whether it needs a judge, and each metric's
definition, range and direction. Use it to know which names `suites` accepts and what a score
in a run means. The example is cut to one suite and one metric; the real reply lists all of
them (`freshness`, `id_retrieval`, `judged`, `result_quality`, `speed`).

Response 200:

```json
{
  "suites": [
    {
      "name": "freshness",
      "version": "1",
      "needs_judge": false,
      "metrics": [
        {
          "name": "fresh_count",
          "definition": "Questions whose reference chunk shas are all in the item's current chunks.",
          "range": [
            0.0,
            null
          ],
          "direction": "higher"
        }
      ]
    }
  ]
}
```

Status codes: 200; 401, 403.

### POST /api/eval/sets

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

Request body:

```json
{
  "item_id": 1,
  "item_sha256": "3be578af166115f7f5f62f60c4d42f91d3da43441a7d5278b2b6b196f5bdf2b6",
  "adapter": "culture-rules",
  "adapter_version": "1",
  "chunker": {},
  "generator": {
    "by": "culture-rules",
    "questions": "newspaper sources"
  },
  "samples": [
    {
      "question": "Where were whales seen this week?",
      "reference_answer": "In the bay.",
      "reference_chunk_shas": [
        "f126e0e6ee4d6777f2f587c76eec7994d8db82c0dc782a8f68066df5c8c9d94b"
      ],
      "reference_contexts": [
        "The whale story. and then and then and then"
      ],
      "source_urls": [
        "https://example.org/whale"
      ]
    },
    {
      "question": "What size of vessel can the new berth take?",
      "reference_answer": "Up to 200 m.",
      "reference_chunk_shas": [
        "e9641289e708f6f913711202cd1f38c8fc3b1415bf9ce28ab1c213989a98e289"
      ],
      "reference_contexts": [
        "The ship story. and then and then and then"
      ],
      "source_urls": [
        "https://example.org/ship"
      ]
    }
  ]
}
```

Response 201:

```json
{
  "id": 1,
  "item_id": 1,
  "item_sha256": "3be578af166115f7f5f62f60c4d42f91d3da43441a7d5278b2b6b196f5bdf2b6",
  "adapter": "culture-rules",
  "adapter_version": "1",
  "chunker": {},
  "generator": {
    "by": "culture-rules",
    "questions": "newspaper sources"
  },
  "version": 1,
  "created_at": "2026-10-30T12:50:07+00:00",
  "samples": [
    {
      "id": 1,
      "question": "Where were whales seen this week?",
      "reference_answer": "In the bay.",
      "reference_chunk_shas": [
        "f126e0e6ee4d6777f2f587c76eec7994d8db82c0dc782a8f68066df5c8c9d94b"
      ],
      "reference_contexts": [
        "The whale story. and then and then and then"
      ],
      "source_urls": [
        "https://example.org/whale"
      ]
    },
    {
      "id": 2,
      "question": "What size of vessel can the new berth take?",
      "reference_answer": "Up to 200 m.",
      "reference_chunk_shas": [
        "e9641289e708f6f913711202cd1f38c8fc3b1415bf9ce28ab1c213989a98e289"
      ],
      "reference_contexts": [
        "The ship story. and then and then and then"
      ],
      "source_urls": [
        "https://example.org/ship"
      ]
    }
  ]
}
```

Status codes: 201; 401, 403, 404 `not_found` (item missing, archived or hidden), 409 `item_changed`, 422 `invalid`, and 422 for a malformed body (FastAPI's own shape, see Errors).

### GET /api/eval/sets

List sets, oldest first (by id), without their samples. `item_id` is optional and
filters to one item. Sets of items the key cannot see are left out. Needs `read`.

Query parameters:

```json
{
  "item_id": "1"
}
```

Response 200:

```json
{
  "sets": [
    {
      "id": 1,
      "item_id": 1,
      "item_sha256": "3be578af166115f7f5f62f60c4d42f91d3da43441a7d5278b2b6b196f5bdf2b6",
      "adapter": "culture-rules",
      "adapter_version": "1",
      "chunker": {},
      "generator": {
        "by": "culture-rules",
        "questions": "newspaper sources"
      },
      "version": 1,
      "created_at": "2026-10-30T12:50:07+00:00"
    }
  ]
}
```

Status codes: 200; 401, 403.

### GET /api/eval/sets/{set_id}

One set with its samples. The sample `id` is what runs refer to as `sample_id`. Needs `read`.

Response 200:

```json
{
  "id": 1,
  "item_id": 1,
  "item_sha256": "3be578af166115f7f5f62f60c4d42f91d3da43441a7d5278b2b6b196f5bdf2b6",
  "adapter": "culture-rules",
  "adapter_version": "1",
  "chunker": {},
  "generator": {
    "by": "culture-rules",
    "questions": "newspaper sources"
  },
  "version": 1,
  "created_at": "2026-10-30T12:50:07+00:00",
  "samples": [
    {
      "id": 1,
      "question": "Where were whales seen this week?",
      "reference_answer": "In the bay.",
      "reference_chunk_shas": [
        "f126e0e6ee4d6777f2f587c76eec7994d8db82c0dc782a8f68066df5c8c9d94b"
      ],
      "reference_contexts": [
        "The whale story. and then and then and then"
      ],
      "source_urls": [
        "https://example.org/whale"
      ]
    },
    {
      "id": 2,
      "question": "What size of vessel can the new berth take?",
      "reference_answer": "Up to 200 m.",
      "reference_chunk_shas": [
        "e9641289e708f6f913711202cd1f38c8fc3b1415bf9ce28ab1c213989a98e289"
      ],
      "reference_contexts": [
        "The ship story. and then and then and then"
      ],
      "source_urls": [
        "https://example.org/ship"
      ]
    }
  ]
}
```

Status codes: 200; 401, 403, 404 `not_found` (also for a set whose item is hidden).

### POST /api/eval/runs/start

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

Every stored run records `config.corpora`, always `["item"]`: eval retrieves from library
items only, so repo documents indexed for search never move a baseline (deviation d16).

A judged suite (`judged`) is refused with 422 `needs_judge`: use the CLI, see
[What needs the CLI](#what-needs-the-cli-or-the-python-client).

Start a run with the defaults:

Request body:

```json
{
  "set_id": 1
}
```

Response 202:

```json
{
  "run_id": 1,
  "set_id": 1,
  "status": "running",
  "total": 2,
  "suites": [
    "freshness",
    "id_retrieval",
    "result_quality",
    "speed"
  ],
  "config": {
    "mode": "hybrid",
    "k": 5,
    "rerank": null,
    "candidates": null,
    "suites": [
      "freshness",
      "id_retrieval",
      "result_quality",
      "speed"
    ],
    "latency_source": "in-process"
  }
}
```

Start a second run on the same set with a different retriever setting, to compare against the first:

Request body:

```json
{
  "set_id": 1,
  "mode": "semantic",
  "k": 1
}
```

Response 202:

```json
{
  "run_id": 2,
  "set_id": 1,
  "status": "running",
  "total": 2,
  "suites": [
    "freshness",
    "id_retrieval",
    "result_quality",
    "speed"
  ],
  "config": {
    "mode": "semantic",
    "k": 1,
    "rerank": null,
    "candidates": null,
    "suites": [
      "freshness",
      "id_retrieval",
      "result_quality",
      "speed"
    ],
    "latency_source": "in-process"
  }
}
```

Status codes: 202; 401, 403, 404 `not_found`, 409 `running` (names the `run_id`), 409 `stale` (item archived, missing or not fully indexed; has a `hint`), 422 `invalid`, 422 `needs_judge`, 422 body validation.

### GET /api/eval/runs/{run_id}

One run with its per-question results. While it is working, `status` is `running` and
`completed` climbs towards `total`; the rest of the shape is the same, with `aggregates` still
empty. A finished run is `complete` (all questions done), `partial` or `failed`; `aggregates`
exist only on `complete` and `partial_aggregates` carries what a `partial` run managed. Scores
that could not be measured are `null` (for example `found_at_5` when `k` is below 5).
`error_rate` is the share of questions that errored. Needs `read`.

Response 200:

```json
{
  "id": 1,
  "set_id": 1,
  "set_version": 1,
  "status": "complete",
  "config": {
    "adapter": {
      "name": "id-only",
      "version": "1"
    },
    "candidates": null,
    "judge_model": null,
    "k": 5,
    "latency_source": "in-process",
    "mode": "hybrid",
    "rerank": null,
    "server": {
      "chunk_count": 3,
      "chunker_max_chars": 1000,
      "embed_model": null,
      "embed_models_stored": [
        ""
      ],
      "embed_prompts": null,
      "reranker_model": null
    }
  },
  "total": 2,
  "completed": 2,
  "aggregates": {
    "distinct_sources": 1.0,
    "duplicate_rate": 0.0,
    "error_rate": 0.0,
    "found_at_1": 0.5,
    "found_at_10": null,
    "found_at_3": 1.0,
    "found_at_5": 1.0,
    "fresh_count": 2.0,
    "fresh_na_count": 0.0,
    "fresh_share": 1.0,
    "fresh_unknown_count": 0.0,
    "id_based_context_precision": 0.3333333333333333,
    "id_based_context_recall": 1.0,
    "latency_p50_ms": 0.3707334981299937,
    "latency_p95_ms": 0.38370114052668214,
    "mrr": 0.6666666666666666,
    "ndcg": 0.75,
    "precision_at_k": 0.2,
    "stale_count": 0.0,
    "timeout_rate": 0.0,
    "within_budget": 1.0
  },
  "partial_aggregates": {},
  "error_rate": 0.0,
  "suites": [
    {
      "name": "freshness",
      "version": "1"
    },
    {
      "name": "id_retrieval",
      "version": "2"
    },
    {
      "name": "result_quality",
      "version": "1"
    },
    {
      "name": "speed",
      "version": "1"
    }
  ],
  "created_at": "2026-10-30T12:50:07+00:00",
  "samples": [
    {
      "sample_id": 1,
      "retrieved_chunk_shas": [
        "f126e0e6ee4d6777f2f587c76eec7994d8db82c0dc782a8f68066df5c8c9d94b",
        "ab8d52a0d10660673cef891ef17f38145c721e8da87a95aaeccb3420b6d7d682",
        "e9641289e708f6f913711202cd1f38c8fc3b1415bf9ce28ab1c213989a98e289"
      ],
      "scores": {
        "distinct_sources": 1,
        "duplicate_rate": 0.0,
        "error_rate": 0.0,
        "found_at_1": 1.0,
        "found_at_10": null,
        "found_at_3": 1.0,
        "found_at_5": 1.0,
        "fresh_count": 1.0,
        "fresh_na_count": 0.0,
        "fresh_share": 1.0,
        "fresh_unknown_count": 0.0,
        "id_based_context_precision": 0.3333333333333333,
        "id_based_context_recall": 1.0,
        "latency_p50_ms": null,
        "latency_p95_ms": null,
        "mrr": 1.0,
        "ndcg": 1.0,
        "precision_at_k": 0.2,
        "stale_count": 0.0,
        "timeout_rate": 0.0,
        "within_budget": 1.0
      },
      "question": "Where were whales seen this week?",
      "reference_chunk_shas": [
        "f126e0e6ee4d6777f2f587c76eec7994d8db82c0dc782a8f68066df5c8c9d94b"
      ],
      "set_version": 1,
      "retrieved_item_ids": [
        1,
        1,
        1
      ],
      "latency_ms": 0.385,
      "freshness": "fresh"
    },
    {
      "sample_id": 2,
      "retrieved_chunk_shas": [
        "ab8d52a0d10660673cef891ef17f38145c721e8da87a95aaeccb3420b6d7d682",
        "f126e0e6ee4d6777f2f587c76eec7994d8db82c0dc782a8f68066df5c8c9d94b",
        "e9641289e708f6f913711202cd1f38c8fc3b1415bf9ce28ab1c213989a98e289"
      ],
      "scores": {
        "distinct_sources": 1,
        "duplicate_rate": 0.0,
        "error_rate": 0.0,
        "found_at_1": 0.0,
        "found_at_10": null,
        "found_at_3": 1.0,
        "found_at_5": 1.0,
        "fresh_count": 1.0,
        "fresh_na_count": 0.0,
        "fresh_share": 1.0,
        "fresh_unknown_count": 0.0,
        "id_based_context_precision": 0.3333333333333333,
        "id_based_context_recall": 1.0,
        "latency_p50_ms": null,
        "latency_p95_ms": null,
        "mrr": 0.3333333333333333,
        "ndcg": 0.5,
        "precision_at_k": 0.2,
        "stale_count": 0.0,
        "timeout_rate": 0.0,
        "within_budget": 1.0
      },
      "question": "What size of vessel can the new berth take?",
      "reference_chunk_shas": [
        "e9641289e708f6f913711202cd1f38c8fc3b1415bf9ce28ab1c213989a98e289"
      ],
      "set_version": 1,
      "retrieved_item_ids": [
        1,
        1,
        1
      ],
      "latency_ms": 0.356,
      "freshness": "fresh"
    }
  ]
}
```

Status codes: 200; 401, 403, 404 `not_found` (absent, or the run's item is hidden).

### GET /api/eval/runs

List runs, oldest first (by id), without their per-question samples. `set_id` is optional and filters to one set.
Runs whose item the key cannot see are left out. The example is cut to the first run.
Needs `read`.

Query parameters:

```json
{
  "set_id": "1"
}
```

Response 200:

```json
{
  "runs": [
    {
      "id": 1,
      "set_id": 1,
      "set_version": 1,
      "status": "complete",
      "config": {
        "adapter": {
          "name": "id-only",
          "version": "1"
        },
        "candidates": null,
        "judge_model": null,
        "k": 5,
        "latency_source": "in-process",
        "mode": "hybrid",
        "rerank": null,
        "server": {
          "chunk_count": 3,
          "chunker_max_chars": 1000,
          "embed_model": null,
          "embed_models_stored": [
            ""
          ],
          "embed_prompts": null,
          "reranker_model": null
        }
      },
      "total": 2,
      "completed": 2,
      "aggregates": {
        "distinct_sources": 1.0,
        "duplicate_rate": 0.0,
        "error_rate": 0.0,
        "found_at_1": 0.5,
        "found_at_10": null,
        "found_at_3": 1.0,
        "found_at_5": 1.0,
        "fresh_count": 2.0,
        "fresh_na_count": 0.0,
        "fresh_share": 1.0,
        "fresh_unknown_count": 0.0,
        "id_based_context_precision": 0.3333333333333333,
        "id_based_context_recall": 1.0,
        "latency_p50_ms": 0.3707334981299937,
        "latency_p95_ms": 0.38370114052668214,
        "mrr": 0.6666666666666666,
        "ndcg": 0.75,
        "precision_at_k": 0.2,
        "stale_count": 0.0,
        "timeout_rate": 0.0,
        "within_budget": 1.0
      },
      "partial_aggregates": {},
      "error_rate": 0.0,
      "suites": [
        {
          "name": "freshness",
          "version": "1"
        },
        {
          "name": "id_retrieval",
          "version": "2"
        },
        {
          "name": "result_quality",
          "version": "1"
        },
        {
          "name": "speed",
          "version": "1"
        }
      ],
      "created_at": "2026-10-30T12:50:07+00:00"
    }
  ]
}
```

Status codes: 200; 401, 403.

### POST /api/eval/runs

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

Request body:

```json
{
  "set_id": 1,
  "status": "complete",
  "config": {
    "mode": "hybrid",
    "k": 5,
    "source": "culture-rules"
  },
  "total": 2,
  "completed": 2,
  "aggregates": {
    "found_at_5": 1.0,
    "mrr": 0.75
  },
  "suites": [
    "id_retrieval"
  ],
  "samples": [
    {
      "sample_id": 1,
      "retrieved_chunk_shas": [
        "f126e0e6ee4d6777f2f587c76eec7994d8db82c0dc782a8f68066df5c8c9d94b",
        "ab8d52a0d10660673cef891ef17f38145c721e8da87a95aaeccb3420b6d7d682"
      ],
      "scores": {
        "found_at_5": 1.0,
        "mrr": 0.5
      },
      "question": "Where were whales seen this week?",
      "latency_ms": 41.5
    },
    {
      "sample_id": 2,
      "retrieved_chunk_shas": [
        "ab8d52a0d10660673cef891ef17f38145c721e8da87a95aaeccb3420b6d7d682",
        "f126e0e6ee4d6777f2f587c76eec7994d8db82c0dc782a8f68066df5c8c9d94b"
      ],
      "scores": {
        "found_at_5": 1.0,
        "mrr": 1.0
      },
      "question": "What size of vessel can the new berth take?",
      "latency_ms": 41.5
    }
  ]
}
```

Response 201:

```json
{
  "id": 3,
  "set_id": 1,
  "set_version": 1,
  "status": "complete",
  "config": {
    "k": 5,
    "mode": "hybrid",
    "source": "culture-rules"
  },
  "total": 2,
  "completed": 2,
  "aggregates": {
    "found_at_5": 1.0,
    "mrr": 0.75
  },
  "partial_aggregates": {},
  "error_rate": null,
  "suites": [
    {
      "name": "id_retrieval",
      "version": null
    }
  ],
  "created_at": "2026-10-30T12:50:07+00:00",
  "samples": [
    {
      "sample_id": 1,
      "retrieved_chunk_shas": [
        "f126e0e6ee4d6777f2f587c76eec7994d8db82c0dc782a8f68066df5c8c9d94b",
        "ab8d52a0d10660673cef891ef17f38145c721e8da87a95aaeccb3420b6d7d682"
      ],
      "scores": {
        "found_at_5": 1.0,
        "mrr": 0.5
      },
      "question": "Where were whales seen this week?",
      "reference_chunk_shas": null,
      "set_version": null,
      "retrieved_item_ids": null,
      "latency_ms": 41.5,
      "freshness": null
    },
    {
      "sample_id": 2,
      "retrieved_chunk_shas": [
        "ab8d52a0d10660673cef891ef17f38145c721e8da87a95aaeccb3420b6d7d682",
        "f126e0e6ee4d6777f2f587c76eec7994d8db82c0dc782a8f68066df5c8c9d94b"
      ],
      "scores": {
        "found_at_5": 1.0,
        "mrr": 1.0
      },
      "question": "What size of vessel can the new berth take?",
      "reference_chunk_shas": null,
      "set_version": null,
      "retrieved_item_ids": null,
      "latency_ms": 41.5,
      "freshness": null
    }
  ]
}
```

Status codes: 201; 401, 403, 404 `not_found`, 422 `invalid`, 422 for a malformed body.

### GET /api/eval/radar

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

One run:

Query parameters:

```json
{
  "run": "1"
}
```

Response 200:

```json
{
  "kind": "run",
  "axes": {
    "found_at_1": 0.5,
    "found_at_3": 1.0,
    "found_at_5": 1.0,
    "mrr": 0.6666666666666666,
    "judge_precision": null,
    "judge_recall": null,
    "no_errors": 1.0,
    "latency_in_budget": 1.0,
    "no_duplicates": 1.0,
    "fresh": 1.0
  },
  "documents": [
    {
      "run_id": 1,
      "set_id": 1,
      "questions": 2,
      "axes": {
        "found_at_1": 0.5,
        "found_at_3": 1.0,
        "found_at_5": 1.0,
        "mrr": 0.6666666666666666,
        "judge_precision": null,
        "judge_recall": null,
        "no_errors": 1.0,
        "latency_in_budget": 1.0,
        "no_duplicates": 1.0,
        "fresh": 1.0
      }
    }
  ],
  "left_out": 0,
  "budget_s": 1.0
}
```

A group (`runs`, mean over its documents) set against another run (`vs`):

Query parameters:

```json
{
  "runs": "1",
  "vs": "2"
}
```

Response 200:

```json
{
  "kind": "pair",
  "a": {
    "kind": "group",
    "axes": {
      "found_at_1": 0.5,
      "found_at_3": 1.0,
      "found_at_5": 1.0,
      "mrr": 0.6666666666666666,
      "judge_precision": null,
      "judge_recall": null,
      "no_errors": 1.0,
      "latency_in_budget": 1.0,
      "no_duplicates": 1.0,
      "fresh": 1.0
    },
    "documents": [
      {
        "run_id": 1,
        "set_id": 1,
        "questions": 2,
        "axes": {
          "found_at_1": 0.5,
          "found_at_3": 1.0,
          "found_at_5": 1.0,
          "mrr": 0.6666666666666666,
          "judge_precision": null,
          "judge_recall": null,
          "no_errors": 1.0,
          "latency_in_budget": 1.0,
          "no_duplicates": 1.0,
          "fresh": 1.0
        }
      }
    ],
    "left_out": 0
  },
  "b": {
    "kind": "run",
    "axes": {
      "found_at_1": 0.5,
      "found_at_3": null,
      "found_at_5": null,
      "mrr": 0.5,
      "judge_precision": null,
      "judge_recall": null,
      "no_errors": 1.0,
      "latency_in_budget": 1.0,
      "no_duplicates": 1.0,
      "fresh": 1.0
    },
    "documents": [
      {
        "run_id": 2,
        "set_id": 1,
        "questions": 2,
        "axes": {
          "found_at_1": 0.5,
          "found_at_3": null,
          "found_at_5": null,
          "mrr": 0.5,
          "judge_precision": null,
          "judge_recall": null,
          "no_errors": 1.0,
          "latency_in_budget": 1.0,
          "no_duplicates": 1.0,
          "fresh": 1.0
        }
      }
    ],
    "left_out": 0
  },
  "budget_s": 1.0
}
```

Status codes: 200; 401, 403, 404 `not_found` (a single run that is absent or hidden), 422 `invalid`.

### GET /api/eval/compare

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

Query parameters:

```json
{
  "before": "1",
  "after": "2"
}
```

Response 200:

```json
{
  "checks": {
    "comparable": true,
    "reasons": [],
    "warnings": [
      "depth differs: k=5 vs k=1; MRR is over min(10, k) ranks"
    ]
  },
  "before": {
    "found_at_1": 0.5,
    "found_at_3": 1.0,
    "found_at_5": 1.0,
    "mrr": 0.6666666666666666,
    "judge_precision": null,
    "judge_recall": null,
    "no_errors": 1.0,
    "latency_in_budget": 1.0,
    "no_duplicates": 1.0,
    "fresh": 1.0
  },
  "after": {
    "found_at_1": 0.5,
    "found_at_3": null,
    "found_at_5": null,
    "mrr": 0.5,
    "judge_precision": null,
    "judge_recall": null,
    "no_errors": 1.0,
    "latency_in_budget": 1.0,
    "no_duplicates": 1.0,
    "fresh": 1.0
  },
  "deltas": {
    "found_at_1": 0.0,
    "found_at_3": null,
    "found_at_5": null,
    "mrr": -0.16666666666666663,
    "judge_precision": null,
    "judge_recall": null,
    "no_errors": 0.0,
    "latency_in_budget": 0.0,
    "no_duplicates": 0.0,
    "fresh": 0.0
  },
  "changed_questions": [
    {
      "question": "What size of vessel can the new berth take?",
      "before": {
        "found_at_1": 0.0,
        "found_at_3": 1.0,
        "found_at_5": 1.0,
        "mrr": 0.3333333333333333
      },
      "after": {
        "found_at_1": 0.0,
        "found_at_3": null,
        "found_at_5": null,
        "mrr": 0.0
      },
      "delta": -0.16666666666666666
    },
    {
      "question": "Where were whales seen this week?",
      "before": {
        "found_at_1": 1.0,
        "found_at_3": 1.0,
        "found_at_5": 1.0,
        "mrr": 1.0
      },
      "after": {
        "found_at_1": 1.0,
        "found_at_3": null,
        "found_at_5": null,
        "mrr": 1.0
      },
      "delta": 0.0
    }
  ],
  "left_out": {
    "before": 0,
    "after": 0
  },
  "budget_s": 1.0
}
```

Status codes: 200 (also when not comparable; read `checks.comparable`); 401, 403, 404 `not_found` (a single run absent or hidden), 422 `invalid`.

### GET /api/eval/weakest

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

Query parameters:

```json
{
  "run": "2",
  "limit": "5"
}
```

Response 200:

```json
{
  "sources": [
    {
      "run_id": 2,
      "set_id": 1,
      "url": "https://example.org/ship",
      "questions": 1,
      "axes": {
        "found_at_1": 0.0,
        "found_at_3": null,
        "found_at_5": null,
        "mrr": 0.0,
        "judge_precision": null,
        "judge_recall": null,
        "no_errors": null,
        "latency_in_budget": 1.0,
        "no_duplicates": 1.0,
        "fresh": 1.0
      }
    },
    {
      "run_id": 2,
      "set_id": 1,
      "url": "https://example.org/whale",
      "questions": 1,
      "axes": {
        "found_at_1": 1.0,
        "found_at_3": null,
        "found_at_5": null,
        "mrr": 1.0,
        "judge_precision": null,
        "judge_recall": null,
        "no_errors": null,
        "latency_in_budget": 1.0,
        "no_duplicates": 1.0,
        "fresh": 1.0
      }
    }
  ],
  "items": [
    {
      "run_id": 2,
      "set_id": 1,
      "item_id": 1,
      "questions": 2,
      "axes": {
        "found_at_1": 0.5,
        "found_at_3": null,
        "found_at_5": null,
        "mrr": 0.5,
        "judge_precision": null,
        "judge_recall": null,
        "no_errors": null,
        "latency_in_budget": 1.0,
        "no_duplicates": 1.0,
        "fresh": 1.0
      }
    }
  ],
  "left_out": 0,
  "limit": 5,
  "budget_s": 1.0
}
```

Status codes: 200; 401, 403, 404 `not_found` (with `run`, when it is absent or hidden), 422 `invalid`.

### POST /api/eval/sets/{set_id}/reanchor

After the item was re-indexed or replaced, map each sample's stored `reference_contexts` onto
the item's current chunks and rewrite its `reference_chunk_shas` in place. Sample ids are kept.
`version` goes up by one only when some sample's shas changed (here nothing moved, so it stays
1). A sample with no stored reference text, or whose text is no longer in the item, is listed
in `could_not_re_anchor` with a `reason` and keeps its old shas. `note` is also stored under
the set's `generator.reanchor`. No body. Needs `write` for the item's kind.

Response 200:

```json
{
  "set_id": 1,
  "item_id": 1,
  "previous_version": 1,
  "version": 1,
  "re_anchored": [
    1,
    2
  ],
  "could_not_re_anchor": [],
  "note": {
    "at": "2026-10-30T12:50:07+00:00",
    "version": 1,
    "re_anchored": 2,
    "changed": 0,
    "stale": 0,
    "stale_sample_ids": [],
    "item_sha256": "3be578af166115f7f5f62f60c4d42f91d3da43441a7d5278b2b6b196f5bdf2b6"
  }
}
```

Status codes: 200; 401, 403, 404 `not_found`, 409 `not_indexed` (the item has no indexed chunks yet), 409 `index_pending` (the item is being re-indexed; re-anchor when that is done).

## Errors

### 401 unauthorized

No `Authorization` header, or a key that does not exist or was revoked. Note the nesting: auth errors come wrapped in a `detail` object.

```json
{
  "detail": {
    "detail": "invalid or missing credentials",
    "code": "unauthorized"
  }
}
```

### 403 forbidden

The key lacks the scope the route needs (`read` for reads, `write` for the others; `admin` passes everything). A key whose kind list excludes the item's kind gets `forbidden_kind` instead. Same nesting as 401.

```json
{
  "detail": {
    "detail": "needs the write scope",
    "code": "forbidden"
  }
}
```

### 404 not_found

The set, run or item does not exist, or its item is private and the key cannot see it. The two cases are not told apart.

```json
{
  "detail": "eval run 999 not found",
  "code": "not_found"
}
```

### 409 running

The set already has a run in progress; the body names it. Poll that run instead.

```json
{
  "detail": "eval set 1 already has a running run 4",
  "code": "running",
  "run_id": 4
}
```

### 409 stale

The set's item is archived, missing or not fully indexed. `hint` says what to do.

```json
{
  "detail": "item 2 index is 'none', not 'done'",
  "code": "stale",
  "hint": "wait for the indexer to finish (see /api/status) and retry"
}
```

### 422 invalid

A query parameter or field is out of range or malformed; `detail` says which.

```json
{
  "detail": "run must be comma-separated integer ids",
  "code": "invalid"
}
```

### 422 needs_judge

The requested suite needs an LLM judge, which the server never runs.

```json
{
  "detail": "suite(s) judged need a judge and cannot run on the server",
  "code": "needs_judge",
  "hint": "judged suites need an LLM judge, which the server does not run; use `culture-library eval run <set_id>` or the Python client (eval.runner.run_set)"
}
```

### 422 validation

The JSON body is malformed or lacks a required field. This is FastAPI's own reply: `detail` is a list, and there is no `code`.

```json
{
  "detail": [
    {
      "type": "missing",
      "loc": [
        "body",
        "set_id"
      ],
      "msg": "Field required",
      "input": {}
    }
  ]
}
```

## What needs the CLI or the Python client

The server never imports ragas and never calls a judge. Two things therefore stay outside
HTTP:

- **Judged suites.** The `judged` suite (LLM-scored context precision and recall) is refused
  on `POST /api/eval/runs/start` with 422 `needs_judge`. Run it with
  `culture-library eval run <set_id>` (needs the `eval` extra and a judge endpoint), or call
  `culture_library.eval.runner.run_set` from Python, then store the result with
  `POST /api/eval/runs`. The radar's `judge_precision` and `judge_recall` stay `null` until a
  judged run exists.
- **Question generation.** `culture-library eval generate <item_id>` builds a set from a
  book's own text or a newspaper's sources sidecar using an LLM. culture-rules that wants
  its own questions skips this and posts its own samples to `POST /api/eval/sets`.

Everything else on this page is plain HTTP.
