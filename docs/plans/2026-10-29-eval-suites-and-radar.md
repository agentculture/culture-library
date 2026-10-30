# Build Plan — eval suites and radar

slug: `eval-suites-and-radar` · status: `exported` · from frame: `eval-suites-and-radar`

> culture-library's evals become suites with an honest reflection: each run scores a registry of suites (ID retrieval at more depths, judged retrieval, result quality, speed and errors, and more), stores error rates and partial scores instead of dropping them, and is shown on an Evals tab with a radar per run (one document or a set) and a radar per comparison, so an operator can see at a glance whether a change improved the RAG

## Tasks

### t1 — Eval storage: additive columns, run snapshots and a backfill function

- instruction: Only `culture_library`/core/`eval_store.py` and its tests. Add an ensure-columns step run at schema registration/open (PRAGMA `table_info`, then ALTER TABLE ADD COLUMN for any missing column); do not touch store.py's items rebuild. Keep core stdlib-only.
- covers: c2, h2, c5, h5, c41, h32
- acceptance:
  - tests/core/`test_eval_store_schema.py`: opening an existing DB made by 0.17.2's schema adds the new nullable columns with ALTER TABLE ADD COLUMN, keeps every row, id and `sqlite_sequence`, and never rebuilds a table
  - `eval_runs` gains `partial_aggregates_json`, `error_rate`, `suites_json` and running (default 0); `aggregates_json` still only on complete; a partial run stores `partial_aggregates_json` and `error_rate`
  - `eval_run_samples` gains question, `reference_chunk_shas`, `set_version`, `retrieved_item_ids`, `latency_ms` and freshness; `add_run_sample` accepts them and old callers without them still work
  - `backfill_snapshots`(conn) copies question and reference shas from `eval_samples` into every run result that lacks them, is idempotent, and reports how many it filled

### t2 — Core ID metrics from stored ranks, with item-aware matching

- instruction: Pure functions, no DB or network. Copy the per-question `retrieved_chunk_shas` and reference shas of runs 11 and 19 from the live DB into a test fixture (questions are public Gutenberg text).
- covers: c42, h33, c10, h8, c33, h25
- acceptance:
  - `culture_library`/core/`eval_metrics.py`: `id_metrics`(retrieved, references, `k_run`) returns found at 1, 3, 5, 10, precision at k, MRR@10, nDCG@10; a depth above `k_run` is None (not measured), never 0
  - a passage counts as found only when item id and `chunk_sha` both match when item ids are present; with sha-only data it matches on sha and the result says `sha_only` true
  - tests/core/`test_eval_metrics.py` reproduces run 19 and run 11's found at 1 (0.20, 0.45) and MRR (0.435, 0.618) from a fixture copied from their stored ranks
  - tests/`test_zero_deps.py` passes; the module imports only the stdlib

### t3 — Core shapes, comparability, compare and per-source breakdown

- instruction: Pure functions over the dicts `eval_store` returns; import `eval_metrics` for the ID axes. Axis order follows the design canvas: found at 1, 3, 5, MRR, judge precision, judge recall, no errors, then new suites.
- depends on: t2
- covers: c4, h4, c6, h6, c40, h31, c38, h30
- acceptance:
  - `culture_library`/core/`eval_radar.py`: axes(run) returns 0-1 higher-better values; lower-is-better metrics are turned around, latency is the share within a budget, precision at k is excluded, unscored metrics are None
  - group(runs) is the mean over documents with each document's question count; compare(a, b) returns checks first: different suites or judge gives not comparable and no deltas; different suite version, set, set version or a partial run gives warnings
  - compare lists changed questions worse first, and breakdown(run) gives per-item and, for newspaper questions, per-source-URL aggregates
  - tests/core/`test_eval_radar.py` covers each rule above with fixtures

### t4 — Suite registry with ID retrieval and judged retrieval suites

- instruction: New package only (eval/suites/`__init__.py`, base.py, `id_retrieval.py`, judged.py) plus tests/eval/`test_suites.py`. The registry discovers suite modules in eval/suites/ automatically (like `create_app` auto-includes `routes_`\*.py), so later suites are one new file with no edit to `__init__.py`. `id_retrieval` calls core.`eval_metrics`. Do not edit runner.py here.
- depends on: t2
- covers: c3, h3, c11, h9
- acceptance:
  - `culture_library`/eval/suites/: a Suite protocol (name, version, metrics with definition, range, direction, `needs_judge`) and a registry; `id_retrieval` and judged (wrapping RagasAdapter) are registered
  - the judged suite keeps `guard_text_egress` and the runner.`_llm_scores` privacy behaviour: a non-public item with a non-local judge is skipped and recorded as skipped
  - importing `culture_library`.eval.suites without the \[eval\] extra works; only the judged suite needs ragas, imported lazily

### t5 — Result quality and speed-and-errors suites

- instruction: Two new modules, eval/suites/`result_quality.py` and eval/suites/speed.py, found by the registry's auto-discovery; do not edit eval/suites/`__init__.py`.
- depends on: t4
- covers: c37, h29
- acceptance:
  - eval/suites/`result_quality.py`: duplicate rate by exact chunk text and distinct sources in the top k, with definitions, range and direction in the registry
  - eval/suites/speed.py: p50 and p95 latency, share within the latency budget (default 1.0 s, configurable), timeouts and error rate from per-question `latency_ms` and errors
  - tests/eval/`test_suites_quality_speed.py` covers both suites with fixture results

### t6 — Freshness suite: three-valued staleness per question

- instruction: New module eval/suites/freshness.py, found by auto-discovery; do not edit eval/suites/`__init__.py`. Use GET /api/rag/items/{id}/chunks through LibraryClient. The runner's set-level `item_sha256` hard gate is relaxed in t7, not here.
- depends on: t4
- covers: c20, h18
- acceptance:
  - eval/suites/freshness.py marks each question fresh, stale, unknown or not applicable by its reference `chunk_sha` against the item's current chunks, and counts each
  - a gone item is stale, an unreachable chunk listing is unknown, a question with no references is not applicable; tests/eval/`test_suite_freshness.py` covers all four

### t7 — Runner on the registry: partial scores, error rate, latency, snapshots

- instruction: Edit eval/runner.py and eval/client.py (latency timing, run posting with the new fields) only. The CLI keeps working via the same `run_set` entry point.
- depends on: t1, t4, t6
- covers: c34, h26, c30, h22, c15, h13
- acceptance:
  - eval/runner.py runs the requested suites from the registry, times each retrieve call, records item ids and snapshots, and keeps scoring after a failed retrieval
  - tests/eval/`test_runner.py`: with 1 of 20 retrievals failing, the run is partial with completed 19, partial aggregates over 19 and `error_rate` 0.05; a run that scores nothing is failed
  - a changed item no longer refuses the run: freshness reports it; archived or not-indexed items still refuse
  - POST /api/rag/retrieve is untouched and the existing RAG route tests pass unmodified

### t8 — Run routes: new fields, partial storage and the suite catalog

- instruction: Only server/`routes_eval.py` and its tests. The catalog reads `culture_library`.eval.suites (no ragas import).
- depends on: t1, t4
- covers: c49, h36
- acceptance:
  - `routes_eval.py`: POST /api/eval/runs accepts suites, partial aggregates, `error_rate` and per-question snapshots, item ids, latency and freshness, and still accepts the old body
  - GET /api/eval/runs/{id} returns them, with status running while a server run is in progress; GET /api/eval/suites returns the registry with metric definitions
  - tests/eval/`test_routes_eval.py` covers the new fields, the old body and the catalog

### t9 — Radar, compare and weakest-sources routes with group visibility

- instruction: New route module, auto-included by `create_app`; compute with core.`eval_radar` from `eval_store` rows.
- depends on: t3, t8
- covers: c14, h12, c43, h34
- acceptance:
  - server/`routes_eval_views.py`: GET radar for a run, a group of runs and a pair; GET compare for two runs or groups; GET weakest sources; all need read scope
  - a run whose item the key cannot see is 404 alone, and in a group it is left out of every mean with a count of runs left out and no value from it
  - tests/eval/`test_routes_eval_views.py` covers scope, visibility, group exclusion and the comparability checks

### t10 — Server-side judge-free runs: start, poll, close out on restart

- instruction: Run eval.runner.`run_set` in a background thread with an in-process retrieve transport (record `latency_source` in-process in config). Close-out runs in the app's startup hook.
- depends on: t7, t8
- covers: c50, h37, c51, h38
- acceptance:
  - server/`routes_eval_jobs.py`: POST /api/eval/runs/start (write scope for the item's kind) starts a background run of judge-free suites and returns the run id at once; a judged suite is refused with a hint
  - a second start for a set with a running run returns 409 with that run's id; on app startup any run still marked running is closed partial or failed with a reason
  - tests/eval/`test_routes_eval_jobs.py`: a server run of a fixture set matches a client `run_set` of the same set and config, and an interrupted run is closed out on restart

### t11 — Re-anchor references after a re-index

- instruction: core module plus server/`routes_eval_reanchor.py` and tests; no CLI edits here.
- depends on: t1
- covers: c47, h35
- acceptance:
  - core/`eval_reanchor.py` maps each question's stored `reference_contexts` onto the item's current chunks by text containment and returns new shas plus the questions it could not re-anchor
  - POST /api/eval/sets/{id}/reanchor (write scope) applies it with one set-version bump and a re-anchor note; tests cover a re-chunked item where story references survive and licence references go stale

### t12 — CLI verbs, explain entries and the migration runbook

- instruction: Edit cli/`_commands`/eval.py, explain/catalog.py and docs/operations/deploy.md only; verbs call the API through LibraryClient.
- depends on: t8, t9, t11
- covers: c19, h17
- acceptance:
  - culture-library eval suites, compare, radar, reanchor and run --suites work with --json, and each has an explain catalog entry and an overview mention
  - docs/operations/deploy.md's migration check lists the new eval columns, and the runbook notes the snapshot backfill step

### t13 — MCP tools for every eval capability, with an API parity test

- instruction: Follow the `_spec`/`_handlers` pattern; no auth logic in the tools.
- depends on: t8, t9, t10, t11
- covers: c13, h40, c29, h21
- acceptance:
  - mcp/client.py and mcp/tools.py add tools for suites, sets, runs, start run, run status, post run, radar, compare, weakest sources and re-anchor, each wrapping one route with the caller's key
  - tests/mcp/`test_eval_tools.py`: write tools need the route's write scope; a parity test fails if an eval route has no tool or a tool has no route; tests/mcp/`test_tools.py`'s pinned set includes the new names

### t14 — Eval API contract docs and contract tests for code clients

- instruction: Follow the existing tests/contract style.
- depends on: t8, t9, t10, t11
- covers: c52, h39
- acceptance:
  - docs/api/eval.md documents every eval route's request and response with JSON examples aimed at culture-rules
  - tests/contract/`test_eval_contract.py` fails when a documented field is renamed or removed

### t15 — Registry end to end: a fake suite reaches runner, catalog and radar

- instruction: Test-only task.
- depends on: t7, t8, t9
- covers: c35, h27
- acceptance:
  - tests/eval/`test_registry_e2e.py` registers a fake suite in the test only; the runner scores it, GET /api/eval/suites lists it and the radar route returns its axes, with no edits to runner, routes or site

### t16 — Evals tab: routing, overview and shared styles

- instruction: Follow the design canvas overview board <https://claude.ai/artifact/JSkBwBrSGJUYCbq7R1C7FP> and state.js patterns (data-tip, Table toggle, Source line).
- depends on: t9
- covers: c16, h14, c17, h15
- acceptance:
  - index.html gains the Evals tab and sections; app.js routes #evals, #evals/radar and #evals/compare without colliding with item keys, and the tab never falls through to home
  - web/static/evals.js renders the overview from the eval API (headline tiles, rerank dumbbell, weakest sources, suites, runs) and is in tests/web MODULES; evals.css holds the Evals styles
  - tests/web/`test_static_rules.py` and `test_site.py` pass: no inline style or script, logical properties, textContent for question text, dir=auto

### t17 — Shape radar view: set, small multiples and table

- instruction: Match the canvas Radar board. Reuse evals.css (append a section).
- depends on: t16
- covers: c28, h20, c18, h16
- acceptance:
  - web/static/evals-shape.js draws the shape chart (one axis per result, dotted perfect outline, after shape over before shape, unscored axes as labelled gaps) as SVG with hover tips and a Table toggle
  - the radar view shows a set's mean shape, an overlay when an earlier comparable run exists, small multiples per document over the mean, and the numbers table; works at 400px without sideways scroll

### t18 — Compare view: checks first, shape of the change, changed questions

- instruction: Match the canvas Compare board; reuse evals-shape.js.
- depends on: t17
- covers: c7, h7
- acceptance:
  - web/static/evals-compare.js shows the comparability checks before any delta, the after-over-before shape with a sorted change list, per-metric changes and changed questions worse first
  - the overview, radar and compare views render in light and dark at desktop and phone widths

### t19 — Reference screenshots and the design contract update

- instruction: Operator-gated: render candidates, send them to the operator, wait for approval before overwriting docs/design/reference/.
- depends on: t18
- covers: c12, h10, c26, h19
- acceptance:
  - tests/visual gains Evals views with eval fixture data, the views count assertion is updated, and candidates for all 30 existing views plus the Evals views are rendered to reference-candidates/
  - docs/design/library-site.md gains an Evals section and the deviation; references are committed only after the operator approves the candidates

### t20 — Live: deploy, backfill runs 1-22 and verify the signals

- instruction: Operator task (main agent). Follow docs/operations/deploy.md; never print secrets; live changes need explicit user go-ahead.
- depends on: t10, t12, t13, t18
- covers: c1, h1, c31, h23, c32, h24, c33, h25, c41, h32
- acceptance:
  - with operator go-ahead: snapshot and rollback image taken, live restarted on the new version, `backfill_snapshots` fills runs 1-22, and the compare of run 19 and run 11 on live shows found at 1 0.20 -> 0.45 and MRR 0.44 -> 0.62
  - a judge-free server run of one set completes on live, and the Evals tab shows the books and newspapers shapes from live data

### t21 — Docs: CLAUDE.md and harness files describe eval suites, shapes and the API

- instruction: Keep the other harness files short; don't paste CLAUDE.md into them.
- depends on: t13, t14
- covers: c29, h21
- acceptance:
  - CLAUDE.md, README.md, QWEN.md and AGENTS.override.md describe the suites, the shape radar, the eval API and MCP tools; tests/`test_docs_rag_eval.py` is updated and passes

## Deferred targets

- `c36` (success_signal): After #27's re-index, re-running sets 1-5 reports their questions as stale (count > 0) through the freshness suite instead of refusing the run, and the radar lays the post-#27 shape over the pre-#27 shape — deferred: measured only after #27 ships, which the user ordered after this work (q4)
- `h28` (honesty): Checked on live after #27 ships: the freshness counts and the overlay are produced by the shipped code; until then this signal is pending, not met — deferred: same as c36: measured only after #27 ships

## Risks

- [unknown_nonblocking] CI's visual job runs on every PR (.github/workflows/tests.yml:91-111): the Evals tab (t16) fails all 30 parity checks until the approved references (t19) land, so t16-t19 must ship in one PR and the operator approves candidates before merge (task t19)
- [unknown_nonblocking] The live deploy adds columns to eval tables: take a pre-deploy snapshot and keep the previous image for rollback, as for 0.16.1 and 0.17.0 (task t20)
- [unknown_nonblocking] Whether a k=10 retrieval cut to the top 5 gives the same top 5 as a k=5 retrieval when rerank is on (frame park v1); if not, depth-10 metrics need their own run (task t7)
- [unknown_nonblocking] Client-side latency is noisy (cold first query, shared gateway; frame park v4): the speed suite may need a warm-up query and to report spread (task t5)
- [follow_up] culture-rules needs to learn the eval API contract (t14): an issue on culture-rules pointing at docs/api/eval.md, posted with the user's go-ahead (task t14)
