# eval suites and radar

> culture-library's evals become suites with an honest reflection: each run scores a registry of suites (ID retrieval at more depths, judged retrieval, result quality, speed and errors, and more), stores error rates and partial scores instead of dropping them, and is shown on an Evals tab with a radar per run (one document or a set) and a radar per comparison, so an operator can see at a glance whether a change improved the RAG

## Audience

- The operator deciding whether a change to the library's RAG helped (a re-index such as #27, an embedder swap such as #7, a speed change such as #25, a rerank setting), and the agents (Gwen, culture-rules, Claude) who read the same eval data through MCP to find weak sources and fix them

## Before → After

- Before: Today an eval run prints two ID numbers and, with a judge, two RAGAS numbers; a partial run throws away the scores it collected (runner.`_fail`); comparing two runs means reading two JSON blobs side by side; per-question ranks and found-at-1 exist only as raw rows in `eval_run_samples`, so building the design canvas on 2026-10-29 needed direct SQL against the live DB
- After: Every new eval run scores five suites and keeps its scores even when some retrievals fail; the Evals tab shows each run's shape (one axis per result, a perfect run fills the outline), lays the after shape over the before shape for any two comparable runs after showing the comparability checks, and lists the questions that changed; agents get the same data from read-only MCP tools
  - instruction: Check against the design canvas <https://claude.ai/artifact/JSkBwBrSGJUYCbq7R1C7FP>: the Radar and Compare boards are the target

## Why it matters

- \#27 (strip boilerplate and re-index), #7 (embedder choice) and #25 (faster retrieval) are each a change whose worth is a before-and-after measurement; without suites, a stored comparison and a picture of it, those decisions stay guesses, and a regression on one result hides inside a good average

## Requirements

- A run that hits retrieval errors keeps its scores over the questions that did score and stores `error_rate` in a new nullable column added with ALTER TABLE ADD COLUMN (decision q5), while `aggregates_json` keeps meaning a complete run; today runner.py `_fail`() truncates samples and sets aggregates to None
  - honesty: A run with some failed retrievals stores status partial, the scores of every question that did score, aggregates over those, and `error_rate` = failed / total; a run that scored nothing is failed with no aggregates; existing live rows survive the schema change unchanged
- Suites are plugins in a registry: a run lists the suites it scored with each suite's name and version, and every metric belongs to one suite; today runner.py hard-codes `id_based_context_precision`/recall at k and adapter.py's EvalAdapter only has `generate_samples` and score
  - honesty: Each stored run names every suite it scored with its version, every metric key belongs to exactly one suite, and the RAGAS adapter is one registered suite, not a special case in the runner
- Comparability is checked before any delta is shown: different suite or judge makes runs not comparable; a different suite version, set, set version or a partial run gives a warning; today core/`eval_store`.comparable() only checks `set_id` and `set_version`
  - honesty: The compare endpoint returns its checks before any delta: not comparable when the suite set or judge differs (no deltas returned), a warning when suite version, set, set version or completeness differs; the old comparable() callers keep working
- Each scored result stores what result-quality and speed suites need: the item id of every retrieved passage and the client-side latency of the retrieve call; today `eval_run_samples` keeps only `retrieved_chunk_shas` and `scores_json`
  - honesty: Each new run's per-question record stores the item id of every retrieved passage and the client-measured retrieve latency in ms, and a run posted without them (an old client) is still accepted
- The radar is a shape chart: each benchmark result is one axis from 0 at the centre to 1 at the edge, so a run that scores 1 everywhere fills a regular outline and every dent is a result that falls short; it is drawn per run (one document) and per set of documents (mean shape, plus small multiples per document), and a comparison lays the after shape over the before shape
  - instruction: Behavioral obligation for /validate-delivery: render the built Radar and Compare views from live runs 11 and 19 and the books and newspaper sets, and check them against the Radar and Compare boards of the design canvas <https://claude.ai/artifact/JSkBwBrSGJUYCbq7R1C7FP> (one axis per result, perfect outline, after shape over before shape)
  - honesty: The radar endpoint returns one value in \[0, 1\] per axis, higher better, for a run, for a group of runs (mean over documents) and for two runs or groups (before and after), and the site draws the after shape over the before shape on the same axes as the design canvas's Radar and Compare boards
- The site gains an Evals tab next to Library and Site state with an overview, a radar page, a compare page and a review queue, following the design canvas <https://claude.ai/artifact/JSkBwBrSGJUYCbq7R1C7FP> built from live sets 1-8 and runs 1-22
  - instruction: Behavioral obligation for /validate-delivery: the Evals views match the canvas boards (overview, radar, compare, phone) except recorded deviations
  - honesty: The Evals tab's overview, radar, compare views read only the eval API, work in light and dark at desktop and phone widths, and match the design canvas boards except where library-site.md records a deviation
- MCP tools cover every eval capability: read (suites, sets, runs, radar, compare, weakest sources) and act (create a set from posted questions, start a judge-free run, post a client-scored run, re-anchor); each tool is a thin wrapper over its API route using the caller's own key, so write tools need the same write scope as the route; mcp/tools.py registers tools in `_SPECS` and `_handlers`, and tests/mcp/`test_tools.py`:17 pins the exact tool-name set
  - honesty: Each eval MCP tool calls exactly one eval API route with the caller's own key and returns what that route returns; no tool has a write path its route lacks, and tests/mcp pins the new tool names
- New eval routes: a compare endpoint and a radar endpoint under /api/eval, with read scope and the same visibility gate (`_vis`, `_visible_set`) as the existing set and run routes; `routes_eval.py` has no compare endpoint today
  - honesty: The compare and radar routes need read scope, and a run whose set's item the key cannot see is 404 to that key, the same as GET /api/eval/runs/{id}
- The Evals pages are hash routes that cannot collide with item keys (#evals, #evals/radar, #evals/compare, #evals/review); app.js show() hard-codes \['home','read','state'\] (:302) and the tab handler sends any non-state tab home (:349), so routing is refactored, and a new evals.js joins tests/web/`test_site.py` MODULES
  - honesty: Opening #evals, #evals/radar, #evals/compare and an item key each lands on the right view, the Evals tab no longer falls through to home, and evals.js is in MODULES
- Charts on the Evals pages follow the Site state pattern (state.js:130-141): every chart has a data-tip hover tooltip, a Table toggle and a source line; radar rings and run status always carry a text label, never colour alone
  - honesty: Every Evals chart has a hover tip, a Table toggle and a source line, and every status or ring is readable with colour removed
- Every new eval CLI verb (compare, radar, review) gets an explain catalog entry next to `_EVAL` (explain/catalog.py:131-158) and an overview mention, and new eval tables join the migration child-table check in docs/operations/deploy.md:375
  - honesty: devague-style catalog check: culture-library explain eval compare (and radar) prints an entry, and deploy.md's migration check lists the new tables
- Staleness is three-valued per question: whether a question's reference `chunk_sha` still exists in the item's current chunks is true, false or unknown (library unreachable), counted apart in the run; today runner.precheck is a set-level hard gate on `item_sha256` that records no run
  - honesty: For each question, freshness is fresh when its reference `chunk_sha` is in the item's current chunks, stale when not (or the item is gone), unknown when the chunks cannot be fetched, and not applicable with no reference; the run stores the three counts and a changed item no longer blocks the run
- The Evals tab ships with a single batch of new reference screenshots: all 30 existing views re-rendered for the third tab plus the new Evals views, approved by the operator and recorded as a deviation in docs/design/library-site.md (user decision q3)
  - honesty: The batch of new references (30 existing views re-rendered plus the Evals views) is committed only after the operator approves it, and library-site.md records the deviation
- The radar page shows the shape for a set of documents with small multiples per document over the set mean, and a Table view of every value; found at 1 and 3 and MRR are computed from the ranked `retrieved_chunk_shas` already stored, so runs 1-22 get them without a re-run
  - honesty: The set view's small multiples share one scale with the big shape and the Table view lists every plotted value
- A suite catalog endpoint (GET /api/eval/suites) lists every registered suite with its name, version, whether it needs a judge, and each metric's name, definition, range and direction (higher is better or lower is better); the site's hover tips and the MCP suite tool read definitions from it
  - honesty: Every metric drawn or shown on the site has a definition served by the catalog, and the hover tips render it
- Every aggregate is also stored per source: per item for a set of documents, and per source URL for newspaper questions built from the sources sidecar, so one weak source cannot hide inside a good average (#28 section 3)
  - honesty: A group radar or compare for several documents can be broken down per item, and a newspaper set's per-source-URL breakdown matches its sidecar entries
- Radar axes are 0 to 1 with higher always better: lower-is-better metrics are turned around (1 - error rate, 1 - duplicate rate), latency becomes the share of retrievals within a time budget, and precision at k (ceiling 1/k for single-reference questions) stays in the table but off the radar; a metric a run did not score is drawn as a gap labelled not measured, never as 0
  - honesty: No radar axis can be read as worse-when-larger: every axis is higher-better, and an unscored metric shows as a labelled gap
- Every scored result snapshots what it was scored against: the question text, the reference chunk shas and the set version, copied into the run record at scoring time; runs 1-22 are backfilled from `eval_samples` now, while every live set is still at version 1. Without it, `replace_samples` deletes `eval_samples` rows and old runs lose the references the radar and compare recompute from (`eval_run_samples`.`sample_id` has no foreign key, core/`eval_store.py`:64-68)
  - honesty: After `replace_samples` bumps a set's version, every earlier run of that set still returns its question texts and reference shas, and its radar and compare values are unchanged; runs 1-22 carry snapshots after the backfill
- New runs credit a retrieved passage as a match only when both its item id and its `chunk_sha` match a reference, because identical chunk text shares one `chunk_sha` across items (search.`chunk_sha` is sha256 of the text; probe on live 2026-10-29: 29 chunk texts are shared by 168 chunks across items); runs 1-22, which stored only shas, are marked as sha-only matches
  - honesty: A test where another item holds the same chunk text as the reference does not count that passage as found for a new run; runs 1-22 are labelled sha-only matches in the API and on the compare view
- Group radars and comparisons that span several sets apply item visibility per set: a run whose item the key cannot see is left out of the group and out of every mean, and the response says how many runs were left out, without naming them
  - honesty: A key that cannot see one of the sets in a group gets a radar and mean computed only from the visible runs, plus a count of runs left out, and no value derived from the hidden run
- A re-anchor step (CLI eval reanchor `SET_ID`, write scope) maps each question's stored `reference_contexts` onto the item's current chunks by text containment, records the new chunk shas with a set-version bump and a re-anchor note, and reports which questions it could not re-anchor
  - honesty: On a copy of the live DB re-indexed with #27's stripping, re-anchoring set 1 keeps its story questions with new shas that point at chunks containing their reference text, marks its 3 licence questions stale, and bumps the set version once
- Every eval capability is both an API route and an MCP tool, so agents and code (culture-rules, Gwen, Claude) integrate without the CLI: list suites, list and get sets, create a set from posted questions, start a run, get run status and results, post a client-scored run, radar, compare, weakest sources and re-anchor; a test pins the MCP tool list to the API routes so neither side can grow alone
  - honesty: For each eval route there is an MCP tool of the same capability and vice versa, checked by a test that compares the two lists; an agent can go from listing suites to reading a radar using MCP alone
- POST /api/eval/runs/start (write scope for the set item's kind) starts a server-side run of the requested judge-free suites in a background task and returns the run id at once with status running; GET /api/eval/runs/{id} reports progress (completed of total); asking for a judged suite there is refused with a hint to run it from the CLI or Python client
  - honesty: Starting a judge-free run over the API returns a run id immediately, the run reaches complete with the same per-question scores the CLI would produce for the same set and config, and a judged-suite request is refused with a hint
- A server-side run interrupted by a restart or crash is never left running: on startup any run still marked running is closed as partial (with what it scored) or failed, with the reason recorded; at most one server-side run per set runs at a time, and a second start for the same set is refused with the running run's id
  - honesty: A test that kills a server-side run mid-way and restarts the app finds the run closed as partial or failed with a reason, never running; a second start for a set with a running run returns 409 with that run's id
- The eval API is a documented contract for code clients: request and response shapes for every eval route are written down in docs (with the JSON examples culture-rules needs), and contract tests in tests/contract pin them so a change that would break culture-rules fails CI
  - honesty: docs carry request and response examples for every eval route, and tests/contract fails when a documented field is renamed or removed

## Honesty conditions

- Every part of the announcement is backed by a requirement below: suites (c3), kept partial scores (c2), the Evals tab (c7), the shape radar per run, per set and per comparison (c6)
- core/ gains no third-party or network import: tests/`test_zero_deps.py` still passes, and every retriever or judge call stays in `culture_library`/eval/
- A judged suite on a non-public item with a non-local judge is skipped and recorded as skipped, exactly as runner.`_llm_scores` does today, and no new code path sends item text to a judge without `guard_text_egress`
- No visual test gates an Evals view until its reference PNG exists in docs/design/reference/ and the operator approved it
- POST /api/rag/retrieve's request and response are byte-for-byte the same shape after this work; the existing RAG route tests pass unchanged
- tests/web/`test_static_rules.py` passes with the Evals markup and scripts: no inline style or script, logical properties only, no innerHTML for question text
- No worker process, lease table or queue exists; server-side runs live in the library process and are closed out on restart
- The operator and at least one agent path (MCP) are served: both can answer did this change help without SQL
- The before state is what is on disk at 1918be9: runner.`_fail` drops aggregates, no compare route exists, and no view reads eval data
- \#27, #7 and #25 are open issues whose acceptance depends on a measured before and after
- Each part of the after state maps to a requirement: suites c3, partial scores c2, tab c7, shape c6, compare c4, MCP c13
- The numbers come from live `eval_run_samples` for runs 19 and 11 as they stand on 2026-10-29 and are recomputed by the shipped code, not typed into a fixture
- The test injects exactly one failing retrieval among 20 and asserts the stored status, completed count, aggregates and `error_rate`
- The fake suite is registered only inside the test and the assertion checks the runner output, GET /api/eval/suites and the radar axes
- Checked on live after #27 ships: the freshness counts and the overlay are produced by the shipped code; until then this signal is pending, not met

## Success signals

- On live, comparing run 19 (rerank off) with run 11 (rerank on) without re-running either shows found at 1 going 0.20 -> 0.45 and MRR 0.44 -> 0.62, the values the design canvas was built from
  - instruction: GET the compare endpoint for runs 19 and 11 on live and check those four numbers
- A run in which 1 of 20 retrievals errors is stored with its 19 scored questions, aggregates over those 19, and `error_rate` 0.05, and the compare view marks it partial instead of hiding it
  - instruction: tests/eval/`test_runner.py` with a FakeLibrary that raises on one question
- Adding a suite is one new module plus one registry entry: a test-only fake suite registered in the test is scored by the runner, listed by the suites endpoint and drawn as new axes by the radar endpoint, with no edits to runner.py, `routes_eval.py` or the site
- After #27's re-index, re-running sets 1-5 reports their questions as stale (count > 0) through the freshness suite instead of refusing the run, and the radar lays the post-#27 shape over the pre-#27 shape

## Scope / boundaries

- core/ stays stdlib-only and network-free (CLAUDE.md, tests/`test_zero_deps.py`): suite scoring that calls the retriever or a judge lives in `culture_library`/eval/, and core/`eval_store` only stores and reads; radar ring placement is pure arithmetic and may live in core
- The judge privacy rule stays: eval/config.py refuses to send a non-public item's text to a non-local judge, and new judged suites go through the same check
- New site views follow docs/design/library-site.md and need operator-approved reference screenshots before the visual tests may gate them
- POST /api/rag/retrieve keeps its request and response shape; latency is measured client-side in the eval runner, and duplicate rate is derived from the `chunk_sha` and `item_id` that every passage already carries (`routes_rag.py`:98, :162)
- The Evals markup obeys the site's CSP and static rules: no inline script or style= attributes, no chart library or CDN, logical CSS properties only, textContent for untrusted text, dir=auto on question text (`routes_web.py`:11-41, tests/web/`test_static_rules.py`); the design canvas's inline styles are mockup-only

## Non-goals

- No job queue with leases or worker tiers: server-side runs are in-process background tasks in the library container, closed out on restart (see the restart requirement), and judged runs stay synchronous CLI or client runs posted to the API; issue #28's worker-provenance lesson does not apply without separate workers
- No Markdown pages or llms.txt for eval data in this round; agents read eval data through the read-only MCP tools (no text/markdown endpoint exists today beyond file serving, `routes_library.py`:70)
- No answer-generation suites (faithfulness, answer relevancy): retrieval is evaluated directly against references, as the previous spec's non-goals and issue #28's 'compare retrieval, not agent answers' both say
- The question review queue (approve/edit/reject, history, roles, derived approvals) is not in this round; it is filed as its own issue (user decision q2, 2026-10-29), so c9 moves out
- The no-answer, privacy-filter and whole-answer suites are not in this round (user decision q1, 2026-10-29); the registry must let them be added as modules later

## Assumptions

- The radar and comparison need no re-run of the baseline: per-question scores for runs 1-22 are already stored in `eval_run_samples` on live (checked 2026-10-29: run 11 vs run 19 per-question diffs read back from the DB)
- \#27 (strip Gutenberg boilerplate, re-index) lands after this work, so its before and after are measured with the new suites and a compare radar; its re-index stales every set, which the freshness suite must report rather than crash on (user decision q4)

## Scope exploration

- `s1` — `culture_library/eval/runner.py`: `run_set` retrieves every sample and on the first exception calls `_fail`(), which sets status partial/failed, truncates samples to those before i and nulls aggregates; ID metrics are fixed precision/recall at k; the LLM path keeps ID aggregates when the judge fails
  - seeds: `c2`, `c3`
- `s2` — `culture_library/core/eval_store.py`: tables `eval_sets`/`eval_samples`/`eval_runs`/`eval_run_samples`; CHECK `aggregates_json` IS NULL OR status='complete'; version bumps only in `replace_samples`; comparable() = same `set_id` and `set_version`; `eval_run_samples` stores `retrieved_chunk_shas` and `scores_json` only
  - seeds: `c2`, `c4`, `c5`, `c9` (rejected)
- `s3` — `culture_library/eval/adapter.py`: EvalAdapter Protocol has name, version, `generate_samples` and score; RAGAS is the only shipped adapter; IdOnlyAdapter in runner.py stands in for --no-llm
  - seeds: `c3`
- `s4` — `live DB /data/library.db (read 2026-10-29)`: 8 sets, 22 runs (20 complete, 2 partial from the judge `max_tokens` bug); per-question scores stored for every run; set 5 has 12 of 20 questions pointing at their source or a citation; set 1 has 3 licence questions
  - seeds: `c8`, `c9` (rejected)
- `s5` — `design canvas JSkBwBrSGJUYCbq7R1C7FP`: five boards: overview, radar (set and one document), compare with change radars, review queue (dark), phone; all numbers from live
  - seeds: `c6`, `c7`
- `s6` — `CLAUDE.md conventions`: core/ stdlib-only and network-free; runtime has zero third-party deps; eval extra holds ragas/openai; visual references are operator-approved
  - seeds: `c10`, `c12`
- `s7` — `culture_library/eval/config.py`: JudgeConfig.`is_local` gates sending text from non-public items to a non-local judge; runner.`_llm_scores` blanks non-public passage text for a non-local judge
  - seeds: `c11`
- `s8` — `culture_library/server/routes_eval.py`: POST/GET sets and runs with write/read scope and kind checks; aggregates refused unless complete (:210); every read gated by `_vis`/`_visible_set` (:67-:169); no compare, radar, `error_rate`, latency, per-source or review fields
  - seeds: `c2`, `c4`, `c14`
- `s9` — `culture_library/server/routes_rag.py`: retrieve (:120) reports no timing; passages carry `item_id`, kind, `chunk_sha`, text, score, `rerank_score`, source (:98) but no visibility; reranked flag and 503 `rag_unavailable` are the server error signals
  - seeds: `c5`, `c15`
- `s10` — `culture_library/mcp/ (server.py, tools.py, client.py)`: nine tools registered statically in `_SPECS` (tools.py:37-130) and `_handlers` (:265); per-call Client uses the caller's Bearer key so the API enforces scope and visibility; read-only eval tools fit the pattern
  - seeds: `c13`
- `s11` — `culture_library/eval/client.py`: LibraryClient has retrieve and set/run CRUD but no compare and no request timing; transport is injectable for tests
  - seeds: `c5`
- `s12` — `tests/eval and tests/mcp`: conftest has FakeAdapter, FakeJudge, a real-app library fixture and a FakeLibrary in `test_runner.py`; MCP tests use an Env fixture with real keys; route tests go in `test_routes_eval.py`
  - seeds: `c13`, `c14`
- `s13` — `culture_library/web/static (index.html, app.js, state.js, styles.css)`: two tabs (index.html:21-24); show() and route() hard-code home/read/state (app.js:302-330); Site state charts use .cc cards with data-tip, Table toggle and Source line (state.js:130-141); api() sends the session key or relies on Cloudflare Access (app.js:117-128); the site calls no /api/eval route
  - seeds: `c6`, `c16`, `c18`
- `s14` — `culture_library/server/routes_web.py + tests/web`: strict CSP (script-src/connect-src self), no inline style= or handlers, logical properties only, MODULES list must include every module (`test_site.py`:83-175)
  - seeds: `c17`
- `s15` — `docs/design/library-site.md + tests/visual`: top bar order is fixed by the contract (79-86); 30 reference PNGs, views.py asserts == 30 and DEVIATED holds 28; a third tab moves the search box in every screenshot, so all references would need re-approval
  - seeds: `c7`, `c12`
- `s16` — `culture_library/cli/_commands/eval.py + explain/catalog.py`: verbs overview/generate/run/show; `cmd_run` hard-wires IdOnlyAdapter or RagasAdapter (:213); no compare, radar or review verb; `_EVAL` catalog entries at :131-158
  - seeds: `c3`, `c19`
- `s17` — `culture_library/eval/generate.py + ragas_adapter.py`: seeded --samples (default 20), Gutenberg markers skipped, self-reference retry; samples replaced wholesale with a set version bump, no per-question history
  - seeds: `c9` (rejected)
- `s18` — `runner.precheck + core/indexer.py + core/search.py`: staleness today = set-level `item_sha256` match, archived, `index_state` done, raising EvalStaleError; no per-item indexed-at; `chunk_sha` and GET /api/rag/items/{id}/chunks are the finer anchors
  - seeds: `c20`
- `s19` — `open issues #27, #25, #7`: \#27 licence chunks are what a result-quality suite measures and a re-index renumbers chunks, staling every set; #25 latency is what a speed suite measures; #7 is a run comparison across embed configs
  - seeds: `c2`, `c5`
- `s20` — `GitHub issue #30`: the question review queue split out of #28 into #30 (user decision q2): history, roles, derived approvals, review page from the design canvas's Review board
  - seeds: `c24`
- `s21` — `challenge pass / migration lens: culture_library/core/store.py`: only the items kind CHECK has a rebuild (`_rebuild_items`, foreign keys off, counts and `sqlite_sequence` verified); there is no general migration step, and relaxing `eval_runs`' CHECK needs its own rebuild or an additive column
- `s22` — `challenge pass / data-lifecycle lens: core/eval_store.py eval_run_samples + replace_samples`: `sample_id` has no foreign key and `replace_samples` deletes samples, so past runs can lose their references; seeded the snapshot requirement
  - seeds: `c41`
- `s23` — `challenge pass / adjacent-systems lens: core/search.py chunk_sha + live embeddings probe`: `chunk_sha` is sha256 of the text; 29 texts are shared by 168 chunks across items on live, so sha-only matching can credit another book's chunk
  - seeds: `c42`
- `s24` — `challenge pass / security lens: group radar and compare across sets`: per-set visibility must filter groups and means; seeded the group visibility requirement
  - seeds: `c43`
- `s25` — `challenge pass / counter-evidence lens: c36 vs c4 and #27's re-index`: re-chunking changes reference shas, so a before/after of #27 on the same set reads as all-stale unless references are re-anchored; routed to a user question
- `s26` — `challenge pass / operations lens: .github/workflows/tests.yml visual job`: visual parity runs on every PR, so the reference batch must land with the tab; parked for the plan
- `s27` — `challenge pass / aggregation lens: design canvas group shapes`: the canvas averaged over documents; recorded as a decision with question counts shown
  - seeds: `c44`
- `s28` — `challenge pass / clean lenses`: MCP read-only tools (mcp/tools.py pattern), judge privacy (config.`guard_text_egress`), phone layout and CSP rules examined: no new finding beyond claims c10-c17; residual: the culture-rules sidecar shape and OPDS/WebDAV paths were not examined, as they are untouched

## Decisions

- ID retrieval metrics (found at 1, 3, 5 and 10, precision at k, MRR@10, nDCG@10) are computed in core from the stored ranked `retrieved_chunk_shas` and the reference chunk shas, by pure arithmetic, so the runner and a backfill of runs 1-22 use the same function; runs stored at k=5 report the depth-10 metrics as not measured, never as zero
- A group's shape is the mean over documents, each document weighted equally, and the view shows each document's question count, so a 4-question newspaper and a 20-question book are visible as different evidence
- Partial-run aggregates and `error_rate` go in a new nullable column added with ALTER TABLE ADD COLUMN; `aggregates_json` keeps meaning a complete run; `eval_runs` is not rebuilt (user decision q5, 2026-10-29)
- After a re-index (#27), references are re-anchored by their stored reference text onto the new chunks so the same questions stay comparable; questions whose text is gone are stale, and only sets with stale questions are regenerated (user decision q6, 2026-10-29)
- Evals are a full API and MCP surface with split execution: the server runs judge-free suites (ID retrieval, result quality, speed and errors, freshness) as an in-process background run; judged suites and question generation run in the CLI or Python client and post through the same API; the production image stays .\[server\] (user decision q7, 2026-10-29)

## Open parks

- [unknown_nonblocking] Whether retrieving k=10 and cutting to the top 5 gives the same top 5 as a k=5 retrieval when rerank is on (the reranker reorders a candidate pool); if not, depth-10 metrics need their own run
- [unknown_nonblocking] The default latency budget for the speed axis (proposed 1.0 s, between today's ~0.7 s without rerank and 1.5-1.9 s with it, and #25's 250 ms target for Gwen)
- [unknown_nonblocking] Duplicate rate by exact chunk text undercounts near-duplicates (per-book Gutenberg licence chunks differ by title and ebook number); whether result quality also needs a normalized or fuzzy match is open
- [unknown_nonblocking] Client-side latency is noisy: the first query is cold and the gateway is shared with the indexer and colleague (open park from the previous frame); the speed suite may need a warm-up query and to report the spread, to be settled when built
- [follow_up] Plan-side once the plan exists: CI's visual job runs on every PR (.github/workflows/tests.yml:91-111), so the third tab fails all 30 parity checks until the approved references land in the same PR; and the eval schema change ships with a pre-deploy snapshot and rollback image like 0.16.1 and 0.17.0
