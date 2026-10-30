# Delivery Summary — eval suites and radar

plan: `eval-suites-and-radar` · run: `complete` · date: `2026-10-29`
baseline: `devague summary skeleton`

## Intent

> culture-library's evals become suites with an honest reflection: each run scores a registry of suites (ID retrieval at more depths, judged retrieval, result quality, speed and errors, and more), stores error rates and partial scores instead of dropping them, and is shown on an Evals tab with a radar per run (one document or a set) and a radar per comparison, so an operator can see at a glance whether a change improved the RAG

After: Every new eval run scores five suites and keeps its scores even when some retrievals fail; the Evals tab shows each run's shape (one axis per result, a perfect run fills the outline), lays the after shape over the before shape for any two comparable runs after showing the comparability checks, and lists the questions that changed; agents get the same data from read-only MCP tools

## Planned Work

- `t1` — Eval storage: additive columns, run snapshots and a backfill function
- `t2` — Core ID metrics from stored ranks, with item-aware matching
- `t3` — Core shapes, comparability, compare and per-source breakdown
- `t4` — Suite registry with ID retrieval and judged retrieval suites
- `t5` — Result quality and speed-and-errors suites
- `t6` — Freshness suite: three-valued staleness per question
- `t7` — Runner on the registry: partial scores, error rate, latency, snapshots
- `t8` — Run routes: new fields, partial storage and the suite catalog
- `t9` — Radar, compare and weakest-sources routes with group visibility
- `t10` — Server-side judge-free runs: start, poll, close out on restart
- `t11` — Re-anchor references after a re-index
- `t12` — CLI verbs, explain entries and the migration runbook
- `t13` — MCP tools for every eval capability, with an API parity test
- `t14` — Eval API contract docs and contract tests for code clients
- `t15` — Registry end to end: a fake suite reaches runner, catalog and radar
- `t16` — Evals tab: routing, overview and shared styles
- `t17` — Shape radar view: set, small multiples and table
- `t18` — Compare view: checks first, shape of the change, changed questions
- `t19` — Reference screenshots and the design contract update
- `t20` — Live: deploy, backfill runs 1-22 and verify the signals
- `t21` — Docs: CLAUDE.md and harness files describe eval suites, shapes and the API

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | additive eval_runs/eval_run_samples columns via ALTER TABLE ADD COLUMN, partial aggregates, snapshots, `backfill_snapshots` (`037e1f4`, merge `cae0133`); store.py migration hook (d1) |
| `t2` | delivered | `core/eval_metrics.py` with item-aware matching; reproduces runs 19/11 from stored ranks (`9f8a5e8`, `60b8d61` per d2) |
| `t3` | delivered | `core/eval_radar.py` axes, group, compare, breakdown (`18610f3`), item-aware radar matching (`077f8cd`), no_duplicates/fresh axes (`2b2ad6b`) |
| `t4` | delivered | `eval/suites/` registry with auto-discovery, id_retrieval and judged suites (`68d1e7f`) |
| `t5` | delivered | result_quality and speed suites (`27b9cea`) |
| `t6` | delivered | freshness suite, fresh/stale/unknown/not applicable (`52690fb`) |
| `t7` | delivered | runner on the registry: partial/failed status, error_rate, latency, snapshots, item ids, relaxed sha gate, opt-in warmup (`ca37831`) |
| `t8` | delivered | run routes take the new fields; `GET /api/eval/suites` (`ddeea9a`) |
| `t9` | delivered | `routes_eval_views.py`: radar, compare, weakest with group visibility (`c8d04d2`) |
| `t10` | delivered | `POST /api/eval/runs/start`, in-process judge-free runs, 409, startup close-out (`818dfd8`) |
| `t11` | delivered | `core/eval_reanchor.py` and `POST /api/eval/sets/{id}/reanchor` (`a525817`) |
| `t12` | delivered | CLI eval suites/radar/compare/reanchor, run --suites/--warmup, explain entries, deploy.md eval columns and backfill (`2b6ac55`) |
| `t13` | delivered | 12 `eval_*` MCP tools with an API parity test (`fb7a347`) |
| `t14` | delivered | `docs/api/eval.md` and `tests/contract/test_eval_contract.py` (`705c575`), example generator script (`2ff614d`) |
| `t15` | delivered | `tests/eval/test_registry_e2e.py` (`f93a212`); its radar assertion needed d3 (`42c783b`) to pass |
| `t16` | delivered | Evals tab, hash routing, overview (`f9a17f1`) |
| `t17` | delivered | shape radar view, overlay, small multiples, table (`424e939`) |
| `t18` | delivered | compare view, checks first (`2db4c5e`) |
| `t19` | delivered | 12 Evals visual views and a deterministic eval seed (`f28a060`); the operator approved all 42 candidates on 2026-10-30 and they are the references (`244681a`; Evals renders refreshed for d4, `3fd1d2c`); design contract Evals section |
| `t20` | delivered | with the user's go-ahead on 2026-10-30: snapshot `20261030T003841Z` and image `culture-library:0.17.0` kept; live restarted on 0.18.0 (`fb52f03`); every row count unchanged; backfill filled 344 of 344 results; live compare 19 -> 11 gives found at 1 0.20 -> 0.45 and MRR 0.435 -> 0.618; server run 23 (set 6) complete; the Evals tab renders live data |
| `t21` | delivered | CLAUDE.md, README.md, QWEN.md, AGENTS.override.md, AGENTS.colleague.md, docs test (`f02dbad`, `fcdebcd`, `f34099a`) |

## Mid-work Decisions

Deviations `d1`-`d3` were recorded as proposed during the run and approved by the user on 2026-10-30; `d4` is the user's own request, approved at filing:

- `d1` — t1 adds a generic `register_migration` hook to `core/store.py` so `eval_store.py` can run its ensure-columns step — `register_schema` only executes plain DDL, and a conditional `ALTER TABLE ADD COLUMN` cannot be written in SQL
- `d2` — t2 reports MRR and nDCG as `mrr`/`ndcg` with an explicit `depth` = min(10, k) instead of "@10" names — a k=5 run cannot measure depth 10 (`/assess`: depth field 0.92 against 0.04 and 0.03)
- `d3` — radar axes follow the suite registry (`set_axis_source`, `axis_names`) — t15 found the axes hard-coded, so a new suite never reached the radar (c35)
- `d4` — MRR and nDCG labels on the Evals pages carry an (i) icon whose hover text names the depth they were measured at (MRR@5, MRR@10) — user request extending `d2` (`3fd1d2c`)
- Shared `SuiteContext` fields (latency, errors, current-chunks lookup) were added once by the main agent before wave 3 (`26f9456`) so t5, t6 and t7 would not collide in `base.py`.
- t3's radar matched sha-only; the main agent made it item-aware from the set's item id (`077f8cd`), since every reference belongs to the set's own item (c42).
- t13 and t14 and t21 started before their nominal waves once their dependencies had merged.
- t10 refuses a stale item with 409 before the 202 instead of recording a failed run.
- Plan risks r3 and r4 were resolved from t7's code reading (k=10 top 5 equals k=5 top 5; opt-in warmup).

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t1` (`d1`) | touched `core/store.py` outside "only eval_store.py": a conditional ADD COLUMN needs code, not DDL | acceptable |
| `t2` (`d2`) | MRR/nDCG named with an explicit depth, not "@10": a k=5 run cannot measure depth 10 | acceptable |
| `t15` (`d3`) | radar/suites/views route edited although t15 was test-only: the axes were hard-coded, against c35 | acceptable |
| `t16` | c16/c7 name a review queue and `#evals/review`; not built (moved to #30 by decision q2), delta `b4` | acceptable |
| `t17`/`t18` (`d4`) | the user asked for an (i) icon naming the depth on MRR and nDCG | acceptable |

## Evidence

- tests: the node ids in Delivery Claims below, run per obligation at `f28a060` and filed as evidence `e1`-`e27` (all pass; filed `--origin llm`, so proposed until adjudicated)
- tests: full suite `uv run pytest -n auto` — 1334 passed, 6 skipped at `f28a060`
- visual: `uv run pytest -m visual` — 70 passed against the 42 approved references
- live (0.18.0, 2026-10-30): compare 19 -> 11, backfill 344/344, server run 23, Evals tab render — evidence `e28`-`e32`; temporary key 14 revoked after
- lint: black, isort, flake8, bandit, markdownlint-cli2, `teken cli doctor --strict` — clean per task merges
- deltas: `b1`-`b4` (proposed)
- not checkable yet: `o15` (deferred until #27)
- commits: `92c88db..d697d27` on `spec/eval-suites`
- PRs / issues: #28, #30, agentculture/culture-rules#121

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| `c2` — A run that hits retrieval errors keeps its scores over the questions that did score and stores `error_rate` in a new nullable column added with ALTER TABLE ADD COLUMN (decision q5), while `aggregates_json` keeps meaning a complete run; today runner.py `_fail`() truncates samples and sets aggregates to None | high | `tests/eval/test_runner.py::test_failure_on_later_sample_is_partial tests/eval/test_runner.py::test_run_that_scores_nothing_is_failed tests/eval/test_routes_eval.py::test_post_run_new_fields_roundtrip tests/eval/test_routes_eval.py::test_partial_aggregates_do_not_trip_aggregate_rule` (run 2026-10-30) |
| `c3` — Suites are plugins in a registry: a run lists the suites it scored with each suite's name and version, and every metric belongs to one suite; today runner.py hard-codes `id_based_context_precision`/recall at k and adapter.py's EvalAdapter only has `generate_samples` and score | high | `tests/eval/test_runner.py::test_complete_run_records_suites_snapshots_and_latency tests/eval/test_suites.py::test_every_metric_belongs_to_one_suite tests/eval/test_runner.py::test_llm_metrics_via_adapter_when_judge_configured` (run 2026-10-30) |
| `c4` — Comparability is checked before any delta is shown: different suite or judge makes runs not comparable; a different suite version, set, set version or a partial run gives a warning; today core/`eval_store`.comparable() only checks `set_id` and `set_version` | high | `tests/core/test_eval_radar.py::test_compare_checks_come_first_and_block_on_suite_or_judge tests/core/test_eval_radar.py::test_compare_warnings_do_not_block tests/eval/test_routes_eval_views.py::test_compare_not_comparable_returns_only_checks tests/eval/test_routes_eval_views.py::test_compare_warns_on_different_set_and_depth` (run 2026-10-30) |
| `c5` — Each scored result stores what result-quality and speed suites need: the item id of every retrieved passage and the client-side latency of the retrieve call; today `eval_run_samples` keeps only `retrieved_chunk_shas` and `scores_json` | high | `tests/eval/test_runner.py::test_complete_run_records_suites_snapshots_and_latency tests/eval/test_routes_eval.py::test_post_run_old_body_still_accepted_with_empty_new_fields` (run 2026-10-30) |
| `c6` — The radar is a shape chart: each benchmark result is one axis from 0 at the centre to 1 at the edge, so a run that scores 1 everywhere fills a regular outline and every dent is a result that falls short; it is drawn per run (one document) and per set of documents (mean shape, plus small multiples per document), and a comparison lays the after shape over the before shape | high | `tests/eval/test_routes_eval_views.py::test_radar_single_run tests/eval/test_routes_eval_views.py::test_radar_group_is_mean_over_documents tests/eval/test_routes_eval_views.py::test_radar_pair tests/web/test_static_rules.py::test_shape_chart_draws_gaps_tips_and_a_table tests/web/test_evals_geom.py::test_polar_points_match_the_canvas_perfect_outline` (run 2026-10-30) |
| `c7` — The site gains an Evals tab next to Library and Site state with an overview, a radar page, a compare page and a review queue, following the design canvas <https://claude.ai/artifact/JSkBwBrSGJUYCbq7R1C7FP> built from live sets 1-8 and runs 1-22 | medium | overview, radar and compare built and operator-approved (evidence `e29`, references `244681a`); live render on 0.18.0; the review queue is NOT built (moved to #30, delta `b4`) |
| `c10` — core/ stays stdlib-only and network-free (CLAUDE.md, tests/`test_zero_deps.py`): suite scoring that calls the retriever or a judge lives in `culture_library`/eval/, and core/`eval_store` only stores and reads; radar ring placement is pure arithmetic and may live in core | high | `tests/test_zero_deps.py` (run 2026-10-30) |
| `c11` — The judge privacy rule stays: eval/config.py refuses to send a non-public item's text to a non-local judge, and new judged suites go through the same check | high | `tests/eval/test_runner.py::test_non_local_judge_never_sees_private_passage_text tests/eval/test_runner.py::test_non_local_judge_skips_llm_metrics_for_private_set_item tests/eval/test_suites.py::test_judged_skips_private_item_with_remote_judge tests/eval/test_suites.py::test_judged_remote_judge_blanks_nonpublic_other_items` (run 2026-10-30) |
| `c13` — MCP tools cover every eval capability: read (suites, sets, runs, radar, compare, weakest sources) and act (create a set from posted questions, start a judge-free run, post a client-scored run, re-anchor); each tool is a thin wrapper over its API route using the caller's own key, so write tools need the same write scope as the route; mcp/tools.py registers tools in `_SPECS` and `_handlers`, and tests/mcp/`test_tools.py`:17 pins the exact tool-name set | high | `tests/mcp/test_eval_tools.py::test_read_key_reads_but_cannot_write tests/mcp/test_eval_tools.py::test_suites_to_radar_with_mcp_alone` (run 2026-10-30) |
| `c14` — New eval routes: a compare endpoint and a radar endpoint under /api/eval, with read scope and the same visibility gate (`_vis`, `_visible_set`) as the existing set and run routes; `routes_eval.py` has no compare endpoint today | high | `tests/eval/test_routes_eval_views.py::test_scope_required tests/eval/test_routes_eval_views.py::test_invisible_run_is_404_alone_everywhere` (run 2026-10-30) |
| `c15` — POST /api/rag/retrieve keeps its request and response shape; latency is measured client-side in the eval runner, and duplicate rate is derived from the `chunk_sha` and `item_id` that every passage already carries (`routes_rag.py`:98, :162) | high | `tests/server/test_routes_rag.py tests/eval/test_rag_routes_client.py` (run 2026-10-30) |
| `c16` — The Evals pages are hash routes that cannot collide with item keys (#evals, #evals/radar, #evals/compare, #evals/review); app.js show() hard-codes \['home','read','state'\] (:302) and the tab handler sends any non-state tab home (:349), so routing is refactored, and a new evals.js joins tests/web/`test_site.py` MODULES | high | `tests/web/test_evals_model.py::test_parse_hash tests/web/test_evals_model.py::test_evals_route_wins_even_if_an_item_key_matched tests/web/test_site.py::test_evals_tab_and_sections` (run 2026-10-30) — ⚠ stale: evidence e10 orphaned (referencing delta 'b4' removed the behavior) |
| `c18` — Charts on the Evals pages follow the Site state pattern (state.js:130-141): every chart has a data-tip hover tooltip, a Table toggle and a source line; radar rings and run status always carry a text label, never colour alone | high | `tests/web/test_static_rules.py::test_shape_chart_draws_gaps_tips_and_a_table tests/web/test_static_rules.py::test_compare_page_shows_checks_first_and_question_text_as_text` (run 2026-10-30) |
| `c20` — Staleness is three-valued per question: whether a question's reference `chunk_sha` still exists in the item's current chunks is true, false or unknown (library unreachable), counted apart in the run; today runner.precheck is a set-level hard gate on `item_sha256` that records no run | high | `tests/eval/test_suite_freshness.py tests/eval/test_runner.py::test_changed_item_runs_and_freshness_reports_it` (run 2026-10-30) |
| `c26` — The Evals tab ships with a single batch of new reference screenshots: all 30 existing views re-rendered for the third tab plus the new Evals views, approved by the operator and recorded as a deviation in docs/design/library-site.md (user decision q3) | high | evidence `e30`; commits `244681a`, `3fd1d2c` |
| `c33` — On live, comparing run 19 (rerank off) with run 11 (rerank on) without re-running either shows found at 1 going 0.20 -> 0.45 and MRR 0.44 -> 0.62, the values the design canvas was built from | high | live compare on 0.18.0 (evidence `e28`); fixture tests `tests/core/test_eval_metrics.py::test_run_19_rerank_off_from_stored_ranks`, `::test_run_11_rerank_on_from_stored_ranks` |
| `c34` — A run in which 1 of 20 retrievals errors is stored with its 19 scored questions, aggregates over those 19, and `error_rate` 0.05, and the compare view marks it partial instead of hiding it | high | `tests/eval/test_runner.py::test_one_of_twenty_failing_is_partial_over_nineteen tests/core/test_eval_radar.py::test_compare_warnings_do_not_block` (run 2026-10-30) |
| `c35` — Adding a suite is one new module plus one registry entry: a test-only fake suite registered in the test is scored by the runner, listed by the suites endpoint and drawn as new axes by the radar endpoint, with no edits to runner.py, `routes_eval.py` or the site | high | `tests/eval/test_registry_e2e.py` (run 2026-10-30) |
| `c36` — After #27's re-index, re-running sets 1-5 reports their questions as stale (count > 0) through the freshness suite instead of refusing the run, and the radar lays the post-#27 shape over the pre-#27 shape | unverified | plan-deferred until #27 ships |
| `c37` — A suite catalog endpoint (GET /api/eval/suites) lists every registered suite with its name, version, whether it needs a judge, and each metric's name, definition, range and direction (higher is better or lower is better); the site's hover tips and the MCP suite tool read definitions from it | high | `tests/eval/test_suites_quality_speed.py::test_registered_with_definitions_range_direction tests/eval/test_routes_eval.py::test_suite_catalog tests/web/test_evals_geom.py::test_axis_list_labels_tips_and_registry_extras` (run 2026-10-30) |
| `c38` — Every aggregate is also stored per source: per item for a set of documents, and per source URL for newspaper questions built from the sources sidecar, so one weak source cannot hide inside a good average (#28 section 3) | high | `tests/core/test_eval_radar.py::test_breakdown_per_item_and_per_source_url tests/eval/test_routes_eval_views.py::test_weakest_sources_and_items` (run 2026-10-30) |
| `c39` — ID retrieval metrics (found at 1, 3, 5 and 10, precision at k, MRR@10, nDCG@10) are computed in core from the stored ranked `retrieved_chunk_shas` and the reference chunk shas, by pure arithmetic, so the runner and a backfill of runs 1-22 use the same function; runs stored at k=5 report the depth-10 metrics as not measured, never as zero | high | `tests/core/test_eval_metrics.py::test_run_19_rerank_off_from_stored_ranks tests/core/test_eval_metrics.py::test_run_11_rerank_on_from_stored_ranks tests/core/test_eval_metrics.py::test_depth_beyond_k_run_is_none_not_zero` (run 2026-10-30) |
| `c40` — Radar axes are 0 to 1 with higher always better: lower-is-better metrics are turned around (1 - error rate, 1 - duplicate rate), latency becomes the share of retrievals within a time budget, and precision at k (ceiling 1/k for single-reference questions) stays in the table but off the radar; a metric a run did not score is drawn as a gap labelled not measured, never as 0 | high | `tests/core/test_eval_radar.py::test_lower_is_better_turned_around_and_partial_aggregates_used tests/core/test_eval_radar.py::test_latency_is_share_within_budget_and_budget_is_a_parameter tests/core/test_eval_radar.py::test_precision_at_k_is_excluded_and_values_in_unit_range tests/core/test_eval_radar.py::test_unscored_metrics_are_none_not_zero tests/web/test_evals_geom.py::test_unscored_axes_are_gaps_never_zero` (run 2026-10-30) |
| `c41` — Every scored result snapshots what it was scored against: the question text, the reference chunk shas and the set version, copied into the run record at scoring time; runs 1-22 are backfilled from `eval_samples` now, while every live set is still at version 1. Without it, `replace_samples` deletes `eval_samples` rows and old runs lose the references the radar and compare recompute from (`eval_run_samples`.`sample_id` has no foreign key, core/`eval_store.py`:64-68) | high | `tests/core/test_eval_store_schema.py::test_backfill_snapshots_fills_missing_and_is_idempotent tests/eval/test_runner.py::test_complete_run_records_suites_snapshots_and_latency` (run 2026-10-30) |
| `c42` — New runs credit a retrieved passage as a match only when both its item id and its `chunk_sha` match a reference, because identical chunk text shares one `chunk_sha` across items (search.`chunk_sha` is sha256 of the text; probe on live 2026-10-29: 29 chunk texts are shared by 168 chunks across items); runs 1-22, which stored only shas, are marked as sha-only matches | high | `tests/core/test_eval_metrics.py::test_item_aware_same_sha_in_other_item_is_not_found tests/core/test_eval_metrics.py::test_sha_only_when_either_side_lacks_item_ids tests/core/test_eval_radar.py::test_axes_item_aware_when_retrieved_item_ids_snapshotted tests/eval/test_routes_eval_views.py::test_item_aware_matching_uses_set_item` (run 2026-10-30) |
| `c43` — Group radars and comparisons that span several sets apply item visibility per set: a run whose item the key cannot see is left out of the group and out of every mean, and the response says how many runs were left out, without naming them | high | `tests/eval/test_routes_eval_views.py::test_group_leaves_out_invisible_runs tests/eval/test_routes_eval_views.py::test_group_all_hidden_is_empty_not_404` (run 2026-10-30) |
| `c47` — A re-anchor step (CLI eval reanchor `SET_ID`, write scope) maps each question's stored `reference_contexts` onto the item's current chunks by text containment, records the new chunk shas with a set-version bump and a re-anchor note, and reports which questions it could not re-anchor | high | `tests/eval/test_reanchor.py` (run 2026-10-30) |
| `c49` — Every eval capability is both an API route and an MCP tool, so agents and code (culture-rules, Gwen, Claude) integrate without the CLI: list suites, list and get sets, create a set from posted questions, start a run, get run status and results, post a client-scored run, radar, compare, weakest sources and re-anchor; a test pins the MCP tool list to the API routes so neither side can grow alone | high | `tests/mcp/test_eval_tools.py::test_every_eval_route_has_a_tool_and_back` (run 2026-10-30) |
| `c50` — POST /api/eval/runs/start (write scope for the set item's kind) starts a server-side run of the requested judge-free suites in a background task and returns the run id at once with status running; GET /api/eval/runs/{id} reports progress (completed of total); asking for a judged suite there is refused with a hint to run it from the CLI or Python client | high | `tests/eval/test_routes_eval_jobs.py::test_server_run_matches_client_run_set tests/eval/test_routes_eval_jobs.py::test_judged_suite_is_refused_with_a_hint tests/eval/test_routes_eval_jobs.py::test_second_start_while_running_is_409_and_progress_is_reported` (run 2026-10-30) |
| `c51` — A server-side run interrupted by a restart or crash is never left running: on startup any run still marked running is closed as partial (with what it scored) or failed, with the reason recorded; at most one server-side run per set runs at a time, and a second start for the same set is refused with the running run's id | high | `tests/eval/test_routes_eval_jobs.py::test_interrupted_runs_are_closed_out_on_restart tests/eval/test_routes_eval_jobs.py::test_second_start_while_running_is_409_and_progress_is_reported` (run 2026-10-30) |
| `c52` — The eval API is a documented contract for code clients: request and response shapes for every eval route are written down in docs (with the JSON examples culture-rules needs), and contract tests in tests/contract pin them so a change that would break culture-rules fails CI | high | `tests/contract/test_eval_contract.py` (run 2026-10-30) |

## Remaining Work / Follow-up

- evidence `e1`-`e32` and deltas `b1`-`b4` — user adjudication (`devague evidence --confirm`, `devague delta --confirm`)
- runs 1-22 carry no `suites_json`, so the overview's suite cards count only run 23 ("Judged retrieval: not run yet" although 7 older runs were judged); a backfill of suites for old runs is a follow-up
- live item titles are file names ("pg31547 images 3"); a metadata fix is outside this run
- `c36`/`o15` — after #27 ships, re-run sets 1-5 and lay the after-#27 shape over the before-#27 shape
- the review queue — #30
- r5 — comment on agentculture/culture-rules#121 with the `docs/api/eval.md` link once the PR is open, then resolve the risk
- follow-up from r3: the runner does not record the per-response `reranked` flag, so a silent reranker fallback is invisible in a run
- rollback: snapshot `~/backups/culture-library-pre-0.18.0/snapshots/20261030T003841Z`, image `culture-library:0.17.0`
