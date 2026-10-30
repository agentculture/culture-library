# Delivery Summary — devague work in the library

plan: `devague-work-in-the-library` · run: `partial` · date: `2026-10-30`
baseline: `devague summary skeleton`

## Intent

> culture-library holds every repo's devague work as it evolves — specs, plans, delivery summaries, designs and the raw .devague state (frames, plans, deliveries, reviews, questions) — browsable and searchable by repo/project and by domain, with the artifact bytes kept in an S3 bucket

After: Every top-level repo's specs, plans, delivery summaries, designs and raw .devague state — committed versions with history plus the latest uncommitted working draft — is in the library and mirrored to a private S3 bucket, viewable as-is, searchable, and browsable by project (with connected projects) and by domain

The run executed the converged plan `devague-work-in-the-library` (23 tasks, 6 waves) by `/assign-to-workforce`, merging every task into `spec/devague-work` through a TDD gate (full suite + black/isort/flake8/bandit before and after each merge). The live migration (t23's live steps) waits on the operator and culture-rules#124 (deviation d22).

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Repodocs core: working drafts, tombstones, design kind, binary content
- `t2` — Projects core: project rows with domains and connected projects
- `t3` — Spec links core: spec to PR, merge commit and current code paths
- `t4` — Mirror core: storage protocol, readable keys and a durable outbox
- `t5` — Devague JSON text extractor for indexing
- `t6` — Render delivery JSON, reviews and questions
- `t7` — Feed: repo document events
- `t8` — File the GitHub-removal follow-up issue with its full-corpus gate
- `t9` — S3 backend as an optional \[s3\] extra
- `t10` — Push and list routes: working, tombstone, binary, scope, spec links, all-private
- `t11` — Index repo documents for search and RAG, at low priority
- `t12` — Backup covers drafts and the outbox
- `t13` — Mirror verifier script for sampled and full-corpus checks
- `t14` — Mirror worker, reconcile CLI and status reporting
- `t15` — Search and RAG routes: repodoc source filter, items-only RAG default, eval config records it
- `t16` — Read routes: drafts, removed docs, design assets, spec links
- `t17` — Push contract doc and the culture-rules issue
- `t18` — MCP repodoc tools with route parity
- `t19` — Deploy: S3 settings, grant-injected AWS credentials, bucket safety check, key runbook
- `t20` — Site: browse by project and by domain
- `t21` — Site reader: working badge, delivery pages, designs, spec links
- `t22` — End-to-end: one file of each kind through API, MCP and the mirror
- `t23` — Live migration: deploy, bucket, keys, first culture-rules cycles, verify, docs

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | working drafts (`repodoc_drafts`), tombstones, the `design` kind with the items CHECK migration, binary content (a5e30f5, merged 4f767bc); plus d1's binary-asset rule (ef941ad) |
| `t2` | delivered | `core/projects.py`: projects table, upsert, repos_for, with_connected (b8a5c64, merged 514cad4) |
| `t3` | delivered | `core/speclinks.py`, newest code paths by `at_committed_at` (d2) (e7fd71a, merged 4700081) |
| `t4` | delivered | `core/mirror.py`: protocol, FakeBackend, key_for, outbox, drain, reconcile, pending_stats (8cdd915, merged a017e2b) |
| `t5` | delivered | `core/devague_text.py` (93f1edb, merged 01c4054) |
| `t6` | delivered | delivery JSON rendering; 273/273 corpus deliveries render without the raw fallback (843e8d6, merged 5db08c4) |
| `t7` | delivered | feed `subject` column and `append_repodoc` (96f363c, merged cd276fe) |
| `t8` | delivered | issue #35 with the full-corpus gate (main agent) |
| `t9` | delivered | `storage/s3.py`, `[s3]` extra, Dockerfile installs `.[server,s3]` (56e2922, merged 7e6ac7b) |
| `t10` | delivered | push/list routes with working, tombstone, content_b64, 16 MiB limit, project and spec_links, all-private; the on_write hook (d6) (bf10241, merged e9c6093) |
| `t11` | delivered | repodoc indexing in separate tables (d7, d12), `corpus` field (d8), index_queue priority (74accbf, merged 48788fa) |
| `t12` | delivered | backup tests; no code change needed (f05b92d, merged 387f713) |
| `t13` | delivered | `scripts/verify-mirror.py` (bcac8a0, merged 3e29592) |
| `t14` | delivered | mirror worker, `/api/status` mirror block, `culture-library mirror` CLI (cc0da36, merged a8a96ea) |
| `t15` | delivered | `corpora` on search and RAG, items-only RAG default, eval run config records corpora (d16) (25c00b6, merged eb015dd); follow-up fix to eval `Passage` (9da5800) |
| `t16` | delivered | read routes: draft, removed, spec_links, content types, sandboxed HTML/SVG, `/rendered` draft default (40a1b9c, merged 9bef16a) |
| `t17` | delivered | `docs/api/repodocs.md` (b747022) and culture-rules#124 (main agent) |
| `t18` | delivered | MCP `repodoc_*` tools, `REPODOC_ROUTES` parity (a490e7b, merged c6924f6); builds on `/api/projects` and `/api/domains` (d18, 7662d0f) |
| `t19` | delivered | compose pass-through, grant-injected AWS credentials, `deploy check-bucket`, S3 runbook and key table (cf32b1e, merged 571971b) |
| `t20` | delivered | `#projects` and `#domains` views, slug join in `projects-model.js` (1fd404c, merged 22ad61e); not checked in a browser |
| `t21` | delivered | reader working badge, delivery/markdown/design views, spec links, removed notice (2de86d7, merged 378e219); not checked in a browser |
| `t22` | delivered | `tests/e2e/test_repodoc_flow.py` (db12002, merged c9c00a1) |
| `t23` | partial | in-repo part only: CLAUDE.md, QWEN.md, AGENTS.*, README, eval API doc, 0.19.0 (09af0fd). Live part not done: 0-doc baseline record, bucket, backup, deploy, culture-rules key, culture-rules cycles, 5-repo verify (d22) |

## Mid-work Decisions

- `d1` — Binary assets (images, HTML, PDF) under any pushed prefix get kind design, not only docs/design/; t1's acceptance only mapped docs/design/ — spec claim c44 covers binary assets under the other prefixes; the plan's t1 criteria were narrower; operator chose to follow the spec (2026-10-30)
- `d7` — Repo documents are indexed additively: full text in the existing `search_fts` under negative rowids (-`doc_id`), vectors in a new `repodoc_embeddings` table, jobs in a new `repodoc_index_queue`; `index_queue` gains a priority column and item jobs always run first — instead of storing repodocs in the existing embeddings/`index_queue` tables — embeddings.`item_id` and `index_queue`.`item_id` reference items(id) ON DELETE CASCADE (search.py:34, indexer.py:29); reuse would need rebuilding the live embeddings table, against store.`register_migration`'s no-rebuild contract (store.py:172); operator chose the additive option (2026-10-30)
- `d8` — The item-vs-repodoc field is named corpus (item|repodoc) and the filter corpora=\[...\]; the existing source field (fulltext|semantic|hybrid) keeps its meaning — spec text 'source=repodoc' (c10, h9, o11, t15) reads as corpus=repodoc — Hit.source and Passage.source already mean fulltext/semantic/hybrid and are returned by /api/search and /api/rag/retrieve (`routes_search.py`:172, `routes_rag.py`:108); reusing the name would break the eval response shape; operator chose corpus (2026-10-30)
- `d12` — Amends d7: repo-document full text lives in a sibling FTS5 table `repodoc_fts` (rowid = repodocs.id), not in `search_fts` under negative rowids — FTS5 bm25 uses whole-table statistics; ~4,600 repodocs in `search_fts` would change every item's full-text score and the hybrid ranks the eval baseline uses (measured: 199 extra rows reordered item hits); protects c47; operator accepted (2026-10-30)
- pending approval (not yet a decision): `d2`, `d3`, `d4`, `d5`, `d6`, `d9`, `d10`, `d11`, `d13`, `d14`, `d15`, `d16`, `d17`, `d18`, `d19`, `d20`, `d21`, `d22`
- `d2`–`d6`, `d9`–`d11`, `d13`–`d22` are recorded and **await operator adjudication at the PR** (agent-proposed; not yet decisions). In short: d2 `at_committed_at` on spec links; d3 mirror reads blobs by sha and gains `force`; d4 bare `host/org/repo` remotes; d5 repodoc feed visibility and per-draft visibility; d6 the `on_write` hook; d9 project and spec-link writes after the push transaction; d10 tombstones emit an `archived` event; d11 flat repodoc errors (risky); d13 a migration-test snapshot pinned to 7 columns; d14 t16 edits the repodocs GET handlers; d15 `/rendered` `shown` only when it matters, SVG sandboxed; d16 corpora recorded in the run config; d17 mirror status hides row errors from public callers; d18 `/api/projects` and `/api/domains`; d19 extra MCP tools and `REPODOC_ALSO_USES`; d20 app.js and test_site edits for the views; d21 `check-bucket` adds `status: unknown`; d22 t23 split, backed by the decider (split 0.940; P(live steps need the operator)=0.986).
- Main-agent integration commits not tied to one task: core schema registration for projects, speclinks and mirror (5726407); eval `Passage` accepts null `item_id` (9da5800).
- An unrelated, uncommitted `deploy/compose.yaml` edit (cloudflared image 2025.9.1 to 2026.10.7) appeared in the main checkout mid-run, outside this run. It was stashed around two merges and restored unchanged, and it is not on this branch.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|------------------------|-----------------|
| `t1` (`d1`) | spec claim c44 covers binary assets under the other prefixes; the plan's t1 criteria were narrower; operator chose to follow the spec (2026-10-30) | `acceptable` |
| `t11` (`d7`) | embeddings.`item_id` and `index_queue`.`item_id` reference items(id) ON DELETE CASCADE (search.py:34, indexer.py:29); reuse would need rebuilding the live embeddings table, against store.`register_migration`'s no-rebuild contract (store.py:172); operator chose the additive option (2026-10-30) | `acceptable` |
| `t11` (`d8`) | Hit.source and Passage.source already mean fulltext/semantic/hybrid and are returned by /api/search and /api/rag/retrieve (`routes_search.py`:172, `routes_rag.py`:108); reusing the name would break the eval response shape; operator chose corpus (2026-10-30) | `acceptable` |
| `t11` (`d12`) | FTS5 bm25 uses whole-table statistics; ~4,600 repodocs in `search_fts` would change every item's full-text score and the hybrid ranks the eval baseline uses (measured: 199 extra rows reordered item hits); protects c47; operator accepted (2026-10-30) | `acceptable` |
| `t2` (`d4`) | normalize_remote rejects the bare form, but the brief requires bare and https forms to be one project; repodocs.py was outside t2's ownership | `acceptable` (pending) |
| `t3` (`d2`) | the library cannot run git, so 'newer commit' needs a timestamp from the pusher; culture-rules must send it | `acceptable` (pending) |
| `t4` (`d3`) | Store.read_bytes takes an item id and repodoc versions have only a sha256; without force a done key that vanished from S3 could never be re-queued | `acceptable` (pending) |
| `t7` (`d5`) | item_uploaders is keyed by item id; a private draft on a public doc must not leak | `acceptable` (pending) |
| `t10` (`d6`, `d9`, `d10`, `d11`) | the on_write hook; project and spec links after the transaction (deadlock otherwise); archived event on tombstones; flat errors | `needs-follow-up` (d9), `risky` (d11), else `acceptable` — all pending |
| `t11` (`d13`) | additive priority column appears in SELECT * snapshots | `acceptable` (pending) |
| `t15` (`d16`) | routes_status.py was owned by t14; eval only retrieves items | `acceptable` (pending) |
| `t16` (`d14`, `d15`) | the GET handlers live in routes_repodocs.py; an existing test pins the draft-less `/rendered` dict; SVG is active content | `acceptable` (pending) |
| `t18` (`d18`, `d19`) | t20 needed project rows; every repodoc route needs a tool | `acceptable` (pending) |
| `t19` (`d21`) | a check the credentials cannot perform must never pass silently | `acceptable` (pending) |
| `t20` (`d20`) | the view registry lives in app.js and existing tests pin its lists | `acceptable` (pending) |
| `t23` (`d22`) | live steps need the operator and culture-rules#124 | `needs-follow-up` (pending) |

## Evidence

- **TDD gate:** before and after every task merge, on `spec/devague-work`, `uv run pytest -n auto -q` plus black, isort, flake8 and (from t9 on) bandit. 1,368 passed at the start; **1,646 passed, 3 skipped** at 09af0fd.
- **Final checks at 09af0fd:**
  - `markdownlint-cli2` passes on all tracked files. The only hit is the gitignored `.devague/reviews/replace-item-file-todo-kind-10.md` from an earlier run.
  - `python3 scripts/scan-secrets.py`: clean (469 files).
  - `uv run teken cli doctor . --strict`: 26/26.
  - `harness-smoke --stage all --require config`: 14 passed, 1 skipped.
  - `tests/contract`: 18 passed.
- **Obligation evidence (`/validate-delivery`):** e1–e31 come from per-obligation pytest runs at 09af0fd, all pass, none skipped. e32–e33 come from reading culture-rules#124 and culture-library#35.
  - 19 are at execution strength; 12 are at coverage strength, each with its unasserted part named.
  - Every record is agent-filed and awaits adjudication.
  - The test node ids are in each record (`devague evidence --list`).
- **Behavioral deltas:**
  - Filed: b1 (d1), b2 (d12), b3 (d8).
  - Five more wait on approval of d9, d10, d11, d16 and d18, because the CLI refuses an unapproved deviation as provenance.
- **Commits:** `626774c..09af0fd` on `spec/devague-work` (49 commits, 20 merges, 99 files, +11,914 / −207).
- **Issues:** culture-library#35, culture-rules#124.

## Delivery Claims

Confidence is capped by evidence strength: execution-strength evidence gives `high`, coverage gives `medium`, and live claims are `unverified`. All evidence records are agent-filed and await adjudication. Lapse `l1` (t3 never ran its tests red) is proposed and not yet evidence.

| Claim | Confidence | Evidence |
|-------|------------|----------|
| Every repo document kind (spec, plan, devague-record, design) pushes once and reads back byte-identical through the API, MCP and the mirror (c1) | medium | test `tests/e2e/test_repodoc_flow.py::test_repodoc_flow` (e1, coverage: FakeBackend, not real S3) |
| Existing repodoc behaviour is unchanged (c3, c4) | high | tests `tests/core/test_repodocs.py` (60 passed, e2, e3) |
| Working drafts: one mutable draft per (repo, path), no new version row (c5) | high | tests `tests/core/test_repodocs_drafts.py::test_spec_scenario_two_working_one_commit` (e4); reviews/questions as working: coverage only (e5) |
| Project and domain scope with connected projects, filterable in the API (c6, c38) | high | tests `tests/server/test_routes_repodocs_push.py::test_domain_project_and_connected_filters`, `tests/core/test_projects.py::test_filters_exact_sets` (e6); MCP/site at coverage (e7, e20) |
| Write-through S3 mirror with readable keys, durable outbox, reconcile and status (c7, c42) | high | tests `tests/server/test_routes_mirror.py`, `tests/core/test_mirror.py`, `tests/test_cli_mirror.py` (e23); key order at coverage (e31). Never run against real S3 |
| core/ stays stdlib-only and network-free; base deps empty; boto3 optional (c8, c16) | high | tests `tests/test_zero_deps.py`, `tests/storage/test_s3.py::test_core_imports_no_network_modules`, `tests/core/test_repodocs.py::test_library_source_never_runs_git_or_devague` (e8, e16) |
| Designs stored and viewed: binary round-trip, sandboxed HTML/SVG, images inline (c9) | medium | tests `tests/server/test_routes_repodocs_read.py::test_html_design_is_sandboxed_download`, `tests/web/test_reader_model.py` (e9, coverage); no browser check (risks r4, r5) |
| Repo documents are searchable with private visibility, closing #4 on the core side (c10, c11) | high | tests `tests/core/test_repodoc_index.py::test_markdown_spec_is_found_by_a_body_phrase_only_when_asked`, `tests/core/test_devague_text.py` (e10, e11) |
| MCP repodoc tools with two-way route parity (c12) | high | test `tests/mcp/test_repodoc_tools.py::test_every_repodoc_route_has_a_tool_and_back` (e12) |
| Feed events per repodoc change (c13) | high | test `tests/server/test_routes_repodocs_push.py::test_each_version_and_draft_enqueues_and_appends_one_event` (e13) |
| Delivery JSON, reviews and questions render (c14) | high | tests `tests/render/test_delivery.py` (273/273 corpus deliveries), `tests/server/test_routes_repodocs_read.py::test_rendered_reviews_and_questions_markdown` (e14) |
| Site browse by project and domain, joined on slug (c15) | medium | tests `tests/web/test_projects.py`, `tests/server/test_routes_projects.py` (e15, coverage: JS model and API, no rendered DOM) |
| Worktrees never multiply documents (c18) | high | test `tests/server/test_routes_repodocs_push.py::test_two_checkout_paths_yield_one_doc_and_one_version` (e17) |
| All migrated documents are private (c20) | medium | test `tests/server/test_routes_repodocs_push.py::test_every_push_is_private_whatever_the_body_says` (e18, coverage: bucket privacy only stub-checked) |
| Spec-to-code links: PR, merge commit, code paths following renames (c40) | medium | tests `tests/server/test_routes_repodocs_push.py::test_spec_links_are_stored_and_follow_renames`, `tests/core/test_speclinks.py` (e21, coverage: trailer computation is culture-rules') |
| Tombstones keep history, drop drafts, delete nothing in S3 (c41) | high | test `tests/server/test_routes_repodocs_push.py::test_tombstone_hides_doc_keeps_history_drops_draft` (e22) |
| Bucket safety check fails on any unsafe setting (c43) | medium | tests `tests/test_deploy_s3.py` (e24, stubbed client only; risk r6: bucket policies not read) |
| design kind and its migration; culture-rules key kinds (c44) | medium | tests `tests/core/test_design_kind.py` (e25, coverage: key refusing `book` not tested) |
| Binary pushes up to 16 MiB; 413 too_large over it (c45) | high | test `tests/server/test_routes_repodocs_push.py::test_over_the_limit_is_413_and_stores_nothing` (e26) |
| Backfill indexes behind library items and survives gateway outages (c46) | medium | tests `tests/core/test_repodoc_index.py::test_new_item_preempts_the_repodoc_backfill` (e27, execution, but small queues, not 4,000+) |
| The eval baseline does not move (c47) | high | tests `tests/core/test_repodoc_index.py::test_item_ranking_unchanged_by_indexed_repodocs`, `tests/eval/test_corpora_eval.py` (e28) |
| Private-access keys documented per client (c48) | medium | test `tests/server/test_routes_projects.py::test_caller_without_private_access_sees_no_projects_or_domains` (e29); runbook untested; scopes for xteink and the todo key were inferred, not checked |
| Backup covers drafts and the outbox (c50) | high | tests `tests/core/test_backup_repodocs.py` (e30) |
| culture-rules has the push contract; GitHub removal is gated on a full-corpus check (c39, c49) | high | issues culture-rules#124 (e32), culture-library#35 (e33) |
| The mirror verifier works (c36, script part) | high | tests `tests/scripts/test_verify_mirror.py` (e19, offline fixtures) |
| For 5 sampled repos, 100% of files are in the library and S3 (c36, live) | unverified | not run: nothing deployed, culture-rules#124 not built (d22) |
| A draft edit arrives within one culture-rules cycle (c37) | unverified | not run (o21 has no evidence); cycle length unknown (park v4) |
| Every top-level repo's record is in the library and S3 (c34, c35) | unverified | not run: live migration pending (d22) |
| Nothing pushed to live before this run (c29) | unverified | the 0-doc baseline was not recorded on live; it is the first live step of t23 |

## Remaining Work / Follow-up

- **`t23` live migration (d22).** Run it with the operator after the PR merges:
  1. Record the 0-doc baseline (c29).
  2. Create the private AWS bucket and run `deploy check-bucket`.
  3. Back up, because the `design` kind rebuilds the items table (r3).
  4. Deploy 0.19.0 with `deploy restart`.
  5. Mint the culture-rules repodoc key with kinds spec, plan, devague-record and design.
  6. Then, after culture-rules#124 ships, run `scripts/verify-mirror.py` on 5 repos and time the draft cycle (c36, c37).
- **culture-rules#124.** The push rule is not built. Add the PR link to the issue once the PR exists, since the issue names the spec by path only (e32).
- **Adjudication at the PR.** Deviations d2–d6, d9–d11 and d13–d22; evidence e1–e33; lapse l1. After that, file the five pending deltas for d9, d10, d11, d16 and d18.
- **Coverage gaps (the coverage-strength evidence):**
  - a working push of a review or question file (o5);
  - re-scoping without a re-push (o7);
  - an HTML byte round-trip and a browser check of the reader and browse views (o10, o16, risks r4 and r5);
  - the culture-rules key being refused `book` (o28);
  - a 4,000+ backfill (o30);
  - the runbook key table (o32).
- **Not live yet, from earlier issues:** #4 (repo-document search) closes once deployed.
- **Follow-ups:**
  - #35, removing the docs from GitHub, gated on a full-corpus verify;
  - park v3, repo identity across renames (r2);
  - risk r6, bucket policies are not read by `check-bucket`;
  - the reference screenshots proposed by t20, which the operator should approve or decline;
  - the site design contract (`docs/design/library-site.md`) does not yet cover `#projects` and `#domains`.
