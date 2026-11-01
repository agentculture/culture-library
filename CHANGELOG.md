# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/). This project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.20.2] - 2026-11-01

### Added

- Migration 0004 (schema version 4): `item_text.simple_tsv` and `repodoc_text.simple_tsv`, the body as a GIN-indexed `'simple'` tsvector (no stemming, stopwords kept). About 49 MB and 3 s to build over live's text.

### Fixed

- Full-text search: a query whose last word is a stopword the english config drops ("... did JetPack 7.0 ship with?") again matches a document that contains that word, as FTS5 did. The english tsvector never holds stopwords, so the last-word prefix rule (spec s21) had required some *other* word starting with it ("without") and silently dropped such documents from the full-text side of hybrid search. The last word is now also checked as a prefix in `simple_tsv`, an index lookup. A document with no word starting with it still does not match ("road the" vs a "Road map" about planners), with or without trailing punctuation. Found by the t20 re-baseline on live (eval set 6, newspaper question 103); deviations d20, d21, d22.

## [0.20.1] - 2026-11-01

### Added

- Migration 0003 (schema version 3): `embeddings.vec_hash` and `repodoc_embeddings.vec_hash`, a generated md5 of the vector, indexed with `(model_id, strategy)`.

### Changed

- The copy tests' 0.19.8 schema fixture now comes from a library.db that 0.19.8 had opened, so it carries `vector_log` and its six triggers (lapse l5).

### Fixed

- `migrate sqlite-to-postgres` copies the real 0.19.8 library.db: it reads past `vector_log` (the 0.19.8 vector cache's change log, which Postgres has no use for) and names it with its row count in the report, instead of refusing the file. The t18 restore drill found it (deviation d15).
- `backup restore` works as the least-privilege `culture_library` role, as live runs it: it leaves out the dump's one `COMMENT ON EXTENSION vector` entry (only the superuser may set it) through a filtered `pg_restore --use-list`, and keeps every other comment. It now refuses up front a target without the `vector` extension, with it outside `public`, or without the C collation. The t18 drill's restore failed with "must be owner of extension vector" (deviations d16, d17, d19; lapse l6). `docs/operations/backup.md` says how to make a restore target on the live stack.
- Search: completing a tie group at the HNSW pool's edge scores only the chunks whose vector is identical to a tied one, found by their stored hash, not the whole corpus. Repo docs tie often (one file pushed from many repos); on the t18 scratch copy of live data that step cost about 260 ms per affected query (deviations d18, d19).

## [0.20.0] - 2026-11-01

### Added

- PostgreSQL 17 + pgvector store (plan library-on-postgres-and-pgvector, #47): psycopg 3 store with versioned migrations (core/migrations), one schema for every table, weighted tsvector full text, pgvector embeddings keyed by model id + strategy with per-pair HNSW indexes
- culture-library migrate sqlite-to-postgres: one-shot copy of a 0.19.x library.db with ids, sequences, text and vectors preserved, a per-table verification report, and refusal of NaN/NUL/orphan rows
- Compose postgres service (pgvector/pgvector:pg17, least-privilege role, C collation, shm_size 1gb); deploy restart starts it first; two new grant secrets CULTURE_LIBRARY_PG_PASSWORD and CULTURE_LIBRARY_PG_SUPERUSER_PASSWORD
- /api/status database block (reachable, schema version, vector indexes); /api/health returns 503 when the database is unreachable
- docs/operations/cutover-postgres.md: freeze, copy, go/no-go checks and the point of no return

### Changed

- Backup, restore and schedule run on pg_dump/pg_restore with a consistent snapshot plus the verified files tree
- Feed appends are serialised so cursor order equals commit order; read-then-write paths (speclinks, mirror enqueue, eval runs, key rotation, blob unlink) are race-safe under concurrent writers
- Search and RAG ask the query embedder for its (model id, strategy); exact ties rank deterministically
- The in-process vector cache (0.19.8) is removed; Postgres serves vector search
- Tests run on Postgres: one database per worker, truncated per test; CI runs a pgvector service

### Fixed

- Code-review round on PR #49: repo-doc text drops NUL before indexing; explicit `--dsn`/`--to` take `CULTURE_LIBRARY_PG_PASSWORD`; `backup schedule` requires `--command`; purge and add/replace no longer deadlock (consistent item, blob and feed lock order); snippets read a bounded body window chosen from the stored vector; stopword-only queries match through a `simple`-config fallback; a new (model, strategy) HNSW index builds with `CREATE INDEX CONCURRENTLY`; `/capabilities` is fetched single-flight with a short failure cache; JSON from feed, sources and projects returns with sorted keys
- A connection pool (`psycopg-pool`, `CULTURE_LIBRARY_PG_POOL_MIN`/`_MAX`) replaces a new connection per call
- Store.write_blob temp-name collision when threads wrote the same bytes

## [0.19.8] - 2026-10-31

### Added

- core/vector_cache.py: each process keeps the unpacked chunk vectors and their norms in memory (about 215 MB for the ~70k live vectors). Triggers on embeddings and repodoc_embeddings feed a vector_log change log, so a query reloads only the items or docs that changed, or everything after the log was pruned; the indexer prunes log rows older than a day, hourly

### Changed

- Semantic search and passages read vectors from the cache and fetch chunk text only for the returned results. On a copy of live data, results are identical (100 of 100 result sets); items-only retrieval drops from about 150 ms to 74 ms and items-plus-repo-docs from about 1.5 s to 0.73 s (median)

## [0.19.7] - 2026-10-31

### Changed

- Semantic and hybrid retrieval score vectors with math.sumprod over the stored float arrays (cosine_scorer), computing the query norm once, instead of pure-Python sums (#25). Rankings and scores are unchanged (identical on 20 live queries per corpus); scoring the 7,855 item vectors drops from about 490 ms to about 157 ms per query, and the 61,932 repo-doc vectors from about 3.8 s to about 1.2 s. Stdlib only

## [0.19.6] - 2026-10-31

### Fixed

- Runbook: the schema-upgrade and restart sections no longer show a hand-typed recreate that leaves out the S3 mirror credentials; they use `deploy restart`, show the full command with the AWS injects, and add a check that the credentials reached the container

## [0.19.5] - 2026-10-31

### Fixed

- The indexer no longer scans every repo doc on each idle tick (#39). Triggers on repodocs, repodoc_drafts and repodoc_versions mark a changed doc in a new repodoc_dirty table in the same transaction; discovery plans only marked docs and clears the marks under the write lock. An UPDATE marks only when a column discovery reads changes, so re-pushing unchanged bytes marks nothing; a hard-deleted doc is dropped from the index. The triggers are recreated on every open, so a later change reaches existing databases. The first open after upgrading, each new indexer and `requeue_all` mark every doc once, so index drift is repaired on the next start. Idle discovery at 5,000 docs: about 0.2–0.5 ms, down from about 9 ms

## [0.19.4] - 2026-10-31

### Changed

- The operator approved deltas b1–b3 and b8–b12; every devague-work record is now approved

## [0.19.3] - 2026-10-31

### Added

- Deltas b8–b12 (proposed) for the now-approved deviations d9, d10, d11, d16 and d18

### Changed

- The operator approved the devague-work deviations d2–d6, d9–d11, d13–d21, d23, d25 and d26, evidence e1–e42, deltas b4–b7 and lapse l1; the delivery summary now says so, and lapse l1 caps the spec-link claim at medium

## [0.19.2] - 2026-10-31

### Changed

- CLAUDE.md: spark runs 0.19.1; the d25 filter fix is live; the d26 owner-clone rule and the full-corpus check recorded.

## [0.19.1] - 2026-10-31

### Changed

- Delivery summary for devague work in the library: the d24 code-review fix round (evidence e34-e40, deltas b4-b7) and the t23 live migration (baseline, private bucket, backup, 0.19.0 deploy, culture-rules push, 5-repo verify e41, draft cycle e42, and the full-corpus verify: 3,770 of 3,773 files, the rest drafts in flight).

### Fixed

- `GET /api/repodocs?remote=` accepts the repo id the API returns (`github.com/org/repo`), not only URL forms; `scripts/verify-mirror.py` sends a URL form so it works against 0.19.0 (d25).
- `scripts/verify-mirror.py`: remoteless and local-path repos are named by the pusher's host (`--host`, default `spark`); `--all` skips linked worktrees and checks only the owner clone of a shared origin, as culture-rules 0.38.1 does (d26).

## [0.19.0] - 2026-10-30

### Added

- Devague work in the library (spec docs/specs/2026-10-30-devague-work-in-the-library.md): every repo's specs, plans, deliveries, designs and raw .devague state, pushed by culture-rules (push contract docs/api/repodocs.md; culture-rules#124).
- Working drafts: POST /api/repodocs with commit="working" keeps one mutable draft per (repo, path); tombstones mark removed docs and keep their history; content_b64 for binary files, capped by CULTURE_LIBRARY_REPODOC_MAX_BYTES (16 MiB, 413 too_large).
- A design kind: docs/design/** plus any image, HTML or PDF under a pushed prefix.
- Project and domain scope from each repo's culture.yaml (core/projects.py), GET /api/projects and /api/domains, and domain/project/connected/include_removed filters on GET /api/repodocs.
- Spec links: a spec's PR, merge commit and current code paths (core/speclinks.py), shown in GET /api/repodocs/{id} and the reader.
- A private S3 mirror: core/mirror.py (readable keys, durable outbox, reconcile), storage/s3.py behind a new optional [s3] extra, a mirror worker, a mirror block in /api/status, culture-library mirror {overview,status,reconcile} and culture-library deploy check-bucket.
- Repo-document search (#4): repodoc_fts, repodoc_embeddings and repodoc_index_queue, indexed at low priority behind library items; hits carry corpus (item|repodoc) and /api/search takes corpora.
- Delivery JSON renders as a page; reviews and questions render as Markdown; design HTML is served sandboxed as a download.
- MCP repodoc_* tools with REPODOC_ROUTES parity; repodoc feed events (subject=repodoc).
- Site: #projects and #domains views grouped by devague slug; the reader shows the working draft by default with a working badge, designs inline, and spec links.
- scripts/verify-mirror.py checks repo files against the library and S3.

### Changed

- Every repo document is stored private, whatever visibility a push sends (decision c31).
- /api/rag/retrieve and eval runs use library items only by default (corpora ["item"], recorded in config.corpora), so the eval baseline is unchanged.
- Repodoc errors are flat {detail, code}.
- The items kind CHECK widens for design, which rebuilds the items table once on first open: back up before deploying.
- uv.lock: boto3 for the [s3] extra pins python-dateutil to 2.9.0.post0 (was 3.0.0).

### Fixed

- eval Passage parses repodoc passages (null item_id).
- PR #38 review: a `connected` entry in a push's `project` that does not normalize is dropped and reported in the response's `warnings`, instead of failing the whole push with 422; `org/repo` in `connected` (and the `project=` filter) means `github.com/org/repo`.
- PR #38 review: spec-link code paths at the same `at_committed_at` as the stored ones now replace them (latest push wins), and the push response carries `spec_links_applied` (false when older paths were kept out) so the pusher records links as delivered only when they were stored.
- PR #38 review: `GET /api/feed` (and `core.feed.read_since`) without `subject` returns item events only, as before repo documents; `subject=all` returns both. The OPDS replaced-time join reads item events only.
- PR #38 review: a mirror outbox row that fails 8 times (`core.mirror.MAX_ATTEMPTS`) is parked as `failed`, so it leaves the pending set; the worker backs off only when a pass uploaded nothing and rows remain to retry; `/api/status` and `mirror status` count `parked`; `mirror reconcile` (and a re-enqueue) re-queues a parked key with its attempts reset. An outbox from an earlier build of this branch has its state CHECK widened on open.

## [0.18.4] - 2026-10-30

### Changed

- Bump the `cloudflared` connector image (`deploy/compose.yaml`) from `cloudflare/cloudflared:2025.9.1` to `2026.10.7`. The old image bundled go-jose 4.1.0, which is affected by CVE-2026-116587 (high, a DoS when parsing JWKs with RSA keys), CVE-2026-116585, CVE-2026-116586 and CVE-2026-34986. 2026.10.7 ships go-jose 4.1.6. Recreate only the connector, with tokens injected and the live env file (which sets `COMPOSE_PROJECT_NAME`): `grant run --inject TUNNEL_TOKEN=CULTUREFLARE_LIBRARY_CULTURE_DEV_TUNNEL_TOKEN --inject GATEWAY_API_KEY=LOBES_GATEWAY_API_KEY -- docker compose -f deploy/compose.yaml --env-file "$HOME/.config/culture-library/live.env" --profile remote up -d --no-deps cloudflared`. The "Start the connector" command in `docs/operations/deploy.md` still references a missing `deploy/.env`; fixing it is tracked in #37.

## [0.18.3] - 2026-10-30

### Changed

- CLAUDE.md: spark runs 0.18.2. It was restarted onto it on 2026-10-30, and the snapshot backfill was rerun, which stamped `set_version` on the 344 older results.

## [0.18.2] - 2026-10-30

### Fixed

- Re-anchoring a set moves its `item_sha256` to the item's current bytes once every question re-anchored, so later runs stop reporting `item_changed`. With any stale question left, the set keeps its old sha, because it still holds questions on the old text.
- Re-anchoring returns 409 `index_pending` while the item's re-index is still queued, because its chunks may still be the old text, and anchoring to them would mark the set current against bytes nobody checked.
- A re-anchor that moves the set's sha bumps the set version even when no reference changed, so runs pinned to different bytes no longer compare as the same set.
- `eval_store.backfill_snapshots` copies a result's reference shas (and records its `set_version`) only while the set is still at the version the run used. After a re-anchor it fills the question only, instead of pairing old retrievals with new references. Running it again also repairs results that the unguarded 0.18.0/0.18.1 backfill filled: it withdraws references copied from a newer set version and stamps `set_version` on the rest.
- The radar keeps a result's stored ID scores when its reference snapshot was withdrawn, instead of scoring it with no references. A metric the run never stored stays unmeasured.
- A server run that fails after some retrievals closes `failed` with `completed` equal to the results it keeps, not 0, and keeps the questions whose retrieval failed in `config.errors`.
- `run_set` takes an `on_retrieval` callback, called once per question as it finishes. The server records progress through it, instead of a client that matched results to questions by counting calls. A failing callback is logged and the run goes on. Progress counts only committed results, so `completed` never runs ahead of the stored results or goes backwards when a run fails.
- The radar, compare and weakest views build one loader per request, so each set's visibility is checked once per request, including across both sides of a compare or radar pair. A group of runs is read over one connection with two batched queries instead of two connections and two queries per run, and only the weakest view reads the set samples (once per set).

### Changed

- CLAUDE.md: spark runs 0.18.1.

## [0.18.1] - 2026-10-30

### Fixed

- `culture-library eval compare` text output prints every axis again: it read `before.axes`/`after.axes`, but the API sends flat axis dicts, so only the heading printed.
- nDCG is no longer inflated when the server returns fewer passages than k: the ideal ranking now fills min(10, k) ranks, not just the ranks returned.
- The `id_retrieval` suite scores item-aware (item id and sha) when the set's item is known, so stored found-at/MRR/nDCG aggregates agree with the radar; identical text in another item no longer counts as found. Both use one helper, `core.eval_metrics.item_aware_metrics`. The suite's version goes to 2, so compare warns when a 0.18.0 run meets a 0.18.1 run. The runner's legacy `id_based_context_precision`/`recall` stay sha-only, unchanged, so they still compare with the 0.17 baseline.
- Radar ID axes leave out failed retrievals for a whole run, because those already count on `no_errors`. Failures are found from `config.errors`, so this also works for runs without the speed suite. A per-item or per-source breakdown slice has no `no_errors` axis, so there a failure still scores as a miss and `/api/eval/weakest` keeps listing it first.
- The compare page's changed questions are scored item-aware, like the axes above them, and leave out questions that failed on either side.
- `eval compare` text output lists each changed question's moved axes as `axis before -> after`, not raw dicts.
- `SuiteContext.item_id` replaces the item-id parsing that the suites each did themselves.

## [0.18.0] - 2026-10-30

### Added

- Eval suites (#28): a suite registry (`culture_library/eval/suites/`, auto-discovered; a new suite is one file) with five suites: ID retrieval (found at 1/3/5/10, precision at k, MRR and nDCG with an explicit depth), judged retrieval (RAGAS), result quality (duplicate rate, distinct sources), speed and errors (p50/p95 latency, share within a 1.0 s budget, timeout and error rate) and freshness (fresh, stale, unknown or not applicable per question).
- Runs keep their scores when some retrievals fail: `partial` runs store partial aggregates and `error_rate`; every result snapshots its question, reference shas, set version, retrieved item ids and latency. Additive eval columns are added in place on first open; `eval_store.backfill_snapshots` fills older runs.
- Shape radar and compare: `core/eval_radar.py` (0-1 higher-better axes that follow the registry, group mean over documents, comparability checks first, per-item and per-source breakdown) and `GET /api/eval/radar`, `/compare`, `/weakest`, `/suites`.
- Server-side judge-free runs: `POST /api/eval/runs/start` (background, 409 while a set is running, interrupted runs closed on restart); `POST /api/eval/sets/{id}/reanchor` re-anchors questions after a re-index.
- 12 `eval_*` MCP tools with an API parity test; `docs/api/eval.md` with contract tests (for culture-rules, agentculture/culture-rules#121); CLI `eval suites|radar|compare|reanchor` and `run --suites/--warmup`.
- The site's Evals tab: an overview, a shape view (`#evals/radar`) and a compare view (`#evals/compare`), with an (i) note naming the depth MRR and nDCG were measured at; 12 new operator-approved reference screenshots (42 in all).
- Delivery summary `docs/deliveries/2026-10-29-eval-suites-and-radar.md` and validation records (evidence e1-e27, deltas b1-b4, deviations d1-d4).

### Changed

- A changed item no longer refuses an eval run; the freshness suite reports its stale questions. Archived and not-indexed items still refuse.
- New runs credit a retrieved passage only when item id and chunk sha both match (identical text in another item shares a sha).

## [0.17.2] - 2026-10-28

### Added

- Delivery summary for the `rag-endpoints-ragas-evals` run (`docs/deliveries/2026-10-25-rag-endpoints-ragas-evals.md`): all 19 tasks delivered, two approved deviations, 11 approved lapses, and delivery claims graded against the validation evidence.
- Validation records in `.devague/`: obligations o1–o22 and evidence e1–e23 (all approved by the operator except e20, rejected for overclaiming and replaced by e21).

### Changed

- CLAUDE.md, README.md and QWEN.md record the finished eval baseline on live: book sets 1–5 and newspaper sets 6–8 (issues 81, 98, 73; built from the culture-rules sources sidecar, unmatched 0), each with a rerank on/off pair and an LLM-scored run. ID recall@5 is 0.89 vs 0.78 on books and 1.00 with rerank on newspapers; numbers are on #7. #27 (Gutenberg boilerplate in the index) is listed as open work.

## [0.17.1] - 2026-10-26

### Changed

- CLAUDE.md, README.md, QWEN.md, AGENTS.override.md and AGENTS.colleague.md describe the library as live at library.culture.dev, with embeddings on and the book half of the eval baseline recorded (sets 1–5, ID recall@5 0.89 with rerank and 0.78 without). The open work is listed: the newspaper baseline (after culture-rules#97), #4, #22, #25, embodiment#107.

### Fixed

- Book eval sets no longer sample Project Gutenberg boilerplate. The generator drops the header (through the `*** START OF THE PROJECT GUTENBERG EBOOK` marker) and the licence (from the `*** END` marker to its closing "hear about new eBooks" line, or to the item's end when that line is missing). It finds them by these markers rather than by the phrase, so a book that mentions Project Gutenberg keeps that text, and an EPUB whose licence sits mid-item keeps the chapters after it. `generator.skipped_boilerplate` records the count; an item with no text outside the boilerplate is refused. On the live books 63, 66 and 68 it skips 26 chunks each; 70 and 71 have no markers and are unchanged.
- Generated eval questions stand alone. The prompt forbids pointing at the source, and a question that still does ("the passage", "this excerpt", "the author", "according to the article", …; but not "the text of the Magna Carta" or "the section on tariffs") is retried once with a message that says so, then skipped. A reply that is not valid JSON still gets its own retry message. Existing sets are unchanged.

## [0.17.0] - 2026-10-25

### Added

- Item kind `discord-message` (#19): one saved Discord message as Markdown, for culture-rules' Discord → library flow. Keys can be minted for it, it has an OPDS feed and a WebDAV folder, and the site gives it a label and colour (a light purple, `#b46fe0` light / `#a640b8` dark, validated beside the to-do violet; the palette's red eighth slot would read as an error). An existing database rebuilds its `items` table once on first open (see "Upgrading across a schema change").
- The reader keeps http(s) links in EPUB sections (#18). `GET /api/library/{id}/text` sections gain `links`, parallel to `paragraphs`; each link is `{text, href, start}`, where `start` locates the text in its paragraph. Only absolute http(s) hrefs are kept, and javascript:, data:, relative and protocol-relative links stay plain text. The reader builds the links with DOM APIs (new tab, `rel=noopener noreferrer`), so the daily newspaper's source links are clickable. Paragraphs, indexed text and search are unchanged.

## [0.16.3] - 2026-10-25

### Added

- The eval judge is easy to adjust. `eval generate` and `eval run` take `--judge-url`, `--judge-model` and `--judge-max-tokens`, which override `CULTURE_LIBRARY_EVAL_LLM_URL` / `_MODEL` / `_MAX_TOKENS`, which override the defaults. `eval overview` lists the effective judge and the source of each value. The gateway key still follows the effective URL and never goes to a non-local judge.

### Fixed

- Eval judge calls set `max_tokens`, defaulting to 8192 and configurable with `CULTURE_LIBRARY_EVAL_LLM_MAX_TOKENS`. On the first live run the reasoning judge (`cortex`) overflowed ragas's 1024-token default, so every LLM-judged metric failed.
- If only the LLM-judged scoring fails, the run is still stored `complete`, with ID-based aggregates over every sample, and `config.llm_metrics_failed` records why. Before, the run became `partial` and all aggregates were dropped.

## [0.16.2] - 2026-10-25

### Changed

- Uploads accept files up to 60 MiB (was 50 MiB), on the server and in the site's upload form, so large books fit. culture-rules still sends at most 50 MiB; the contract test now checks that the library accepts at least that much. Items over 50 MiB may exceed xteink's e-reader limit.

## [0.16.1] - 2026-10-25

### Changed

- `deploy restart` injects the lobes gateway key from grant (`--inject GATEWAY_API_KEY=LOBES_GATEWAY_API_KEY`), the same way it injects the tunnel token, so `live.env` stays secret-free. `--gateway-secret NAME` picks another grant secret and `--no-gateway-key` skips the key, which turns embeddings off. An env file that sets `GATEWAY_API_KEY` or `TUNNEL_TOKEN` is refused before anything runs, naming the grant secret to use instead (deviation d2). Restart checks that each grant secret exists (`grant show`) before compose runs, never lets a shell-exported key reach compose, and treats `--gateway-secret` and `--no-gateway-key` as mutually exclusive.
- Runbook "Enable embeddings": the key comes from grant, not `live.env`; rollback is `deploy restart --no-gateway-key`.

## [0.16.0] - 2026-10-25

### Added

- `POST /api/rag/retrieve` returns ranked passages (full chunk text plus `chunk_sha`, the sha256 of the chunk text) in `semantic` or `hybrid` mode. It needs `read` scope and follows `/api/search` visibility. An optional rerank stage uses the gateway `reranker` role (Qwen3-Reranker-0.6B on `/v1/rerank`) and falls back to the unreranked order with `reranked: false`. It returns 503 `rag_unavailable` when there is no embedder. `GET /api/rag/items/{id}/chunks` lists an item's chunks with its sha256 and index state.
- `PUT`/`GET /api/library/{id}/sources`: a newspaper sources sidecar (`[{article_title, source_urls, excerpt}]`) that culture-rules pushes with each issue (culture-rules#97). Writes honour key kind limits.
- `/api/eval/sets` and `/api/eval/runs` store generated eval sets and scored runs in new item-child tables. They cascade on delete and are covered by the kinds-rebuild integrity check. Aggregates are stored only on `complete` runs, and runs compare only on the same set version.
- `/api/status` gains a `retrieval` block: embed model and prompts, chunker `max_chars`, reranker model, stored chunk count and stored model labels.
- The MCP `rag_retrieve` tool mirrors the retrieve endpoint.
- `culture-library eval {overview,generate,run,show}`, backed by the `culture_library.eval` package. It holds a framework-neutral adapter interface, with RAGAS 0.4.3 as the first adapter scoring ID-based and LLM context precision/recall. Book sets are generated from the book's own chunks, and newspaper sets from the sources sidecar. Runs refuse stale, archived or re-indexing items and record the server's retrieval config. Text from non-public items is never sent to a non-local judge.
- An optional `[eval]` extra: `ragas==0.4.3`, `openai>=1` and `langchain-community<0.4.2`. Without the cap, ragas 0.4.3 fails to import. Base dependencies stay empty, and an opt-in CI job runs the real-ragas tests.
- Runbook: "Enable embeddings" in `docs/operations/deploy.md`.
- CI fails a pull request that has any OPEN or CONFIRMED SonarCloud issue; accepted and false-positive issues pass (`scripts/sonar-pr-issues.py`, cited from culture-rules@dcddf88).

### Changed

- The gateway embedder sends gemma2-embed prompt names: `Retrieval-document` when indexing and `Retrieval-query` for queries. `LOBES_EMBED_PROMPTS=off` turns them off. `embeddings.model` records the label, for example `gemma2-embed+Retrieval-document`.
- Gateway clients and the eval judge client refuse HTTP redirects, so the key never leaves the configured origin. `GATEWAY_API_KEY` goes only to the lobes gateway or a local judge; any other judge uses `CULTURE_LIBRARY_EVAL_LLM_KEY`.
- At startup, items whose stored embeddings carry a different model/prompt label are re-queued (`requeue_stale_model`), so turning prompts on or switching models re-embeds the corpus instead of mixing vector spaces.
- `deploy/compose.yaml` adds `host.docker.internal:host-gateway`, because the container could not reach the gateway on 127.0.0.1. It also sets defaults for `LOBES_GATEWAY_URL`, `LOBES_EMBED_MODEL`, `LOBES_RERANK_MODEL` and `LOBES_EMBED_PROMPTS`.

## [0.15.0] - 2026-10-25

### Added

- Site: `todo` is its own kind (#11). It gets a violet colour (`#6250d6` light, `#9085e9` dark), the validated palette's seventh slot, re-checked with the dataviz validator in both themes, plus a filter chip, a shelf legend entry and an upload option. A Markdown to-do opens as a read-only checklist: `- [ ]`/`- [x]` lines become checkboxes, `#` lines headings, and `<!-- ... -->` ids stay hidden. It is parsed by `web/static/checklist.js` from the item's file, as text nodes only.
- OPDS `<updated>` and WebDAV `getlastmodified` follow an in-place replace (`library.file_changed_at`, read from the feed's `replaced` events), so readers that sync by date pick up a new file under the same id. The OPDS root feed and the WebDAV kind collections move too. Dates are compared as instants, not strings.

### Fixed

- Replace and purge unlink an old blob only after their transaction commits, re-checking references under a fresh write lock. Before, other connections could briefly see the committed old row pointing at a deleted file; a new concurrent-replace test found it. Only a blob unreferenced at commit gets that second transaction, and it is best-effort: a busy database or an I/O error is logged on stderr and leaves an orphan file, never fails the replace or purge. `RepoDocs.push` re-writes a blob removed between its write and its transaction, as `add` and `replace` do.
- `backup run` no longer fails when a replace or purge commits between its database snapshot and the blob copy. It takes a fresh snapshot, up to three attempts, also when the file vanishes during the copy itself. A blob missing while still referenced still fails.

### Changed

- Contract tests cite culture-rules at `e936acf` (0.28.1). Its `KIND` constant is gone, and the library comes from actor config. The tests drive the real port's Markdown to-do upload and its replace through `PUT /api/library/{id}/file`.

## [0.14.0] - 2026-10-25

### Added

- `culture-library keys rotate ID [--kind K] [--drop-kind K] [--scope S] [--private | --no-private]` mints a replacement for key ID with its name, scopes, kinds and private flag, plus the changes given, and prints it once (#14). Key ID stays valid until `keys rotate ID --confirm NEW_ID`, which revokes it only when NEW_ID is live and was minted as ID's replacement; otherwise it refuses (`key_revoked`, `not_a_replacement`) and changes nothing. A missing or revoked ID is refused before anything is minted. `--json` reports `{old_id, new_id, name, scope, kinds, private}`, and the first step adds `key`.
- API keys record `replaces`; `keys list` and `GET /api/keys` show it. An existing `api_keys` table gains the column on first use. `keys list` text also shows each key's kinds.
- Runbook: "Rotate a client's key" in `docs/operations/deploy.md`.

### Changed

- `keys mint` (and `keys rotate`) print only the key on stdout in text mode; the "shown once" note goes to stderr, so the output pipes straight into `grant set NAME - --hidden`.

## [0.13.0] - 2026-10-25

### Added

- `culture-library deploy restart` recreates the live stack's `library` and `mcp` containers from the host env file (`$CULTURE_LIBRARY_LIVE_ENV`, else `$HOME/.config/culture-library/live.env`). It refuses a missing, empty or placeholder setting, or send targets that do not parse, before anything runs (placeholder target labels and `${...}` values included). Variables the file sets are removed from the commands' environment, so a shell export cannot override the checked file. It then runs the runbook's `grant run … up -d --no-deps library mcp` with no shell, waits for `/api/health` and lists the send targets the new container loads. `--build` rebuilds the image first; `--dry-run` prints the commands. `deploy overview` and `explain deploy` describe it.
- `send.parse_targets()` parses a JSON targets list; `load_targets` uses it.

## [0.12.0] - 2026-10-25

### Added

- `PUT /api/library/{id}/file` replaces an item's file in place: same id, new sha256, size and format; title, author, kind and visibility are unchanged. It applies the upload caps and format checks, appends an `updated` feed event (replaced, old_sha256, size, format), and unlinks the old blob when nothing references it. Errors: 403 forbidden_kind, 409 duplicate_content, 404 for archived or unknown items. Sending the same bytes again returns 200 with no event.
- New `todo` kind for Markdown checklists, one per recording.
- Docs: "Upgrading across a schema change" and "Restart the live stack" runbooks in `docs/operations/deploy.md` (restart reads a host env file, so settings such as the send targets are never retyped); the library spec lists every kind and allows a same-id file replace.

### Changed

- On first open after upgrading, the store rebuilds the `items` table once to widen its kind CHECK, preserving ids and the id sequence. It verifies row counts, refuses to open (rolling back) on a mismatch, and writes one diagnostic line to stderr on success. Rollback means restoring the pre-upgrade snapshot; running an older image on a database holding todo items is unsupported. A database whose CHECK names kinds this release does not know is refused at open, never narrowed. The migration's foreign-key check covers only the tables the rebuild touches.

### Fixed

- Replace and the background indexer no longer race: a job in flight for the old bytes stops without writing their body, vectors or `done` status, and the job stays queued for the new bytes. Replace drops the item's old embeddings.
- Sending an item's current bytes again restores its file if the stored copy is missing.
- The 409 `duplicate_content` response names `other_id` only when the caller could GET that item, so archived items stay hidden from non-private keys. The route's kind guard no longer blocks the event loop.

## [0.11.0] - 2026-10-24

### Added

- The library behind library.culture.dev, built from the devague plan
  `docs/plans/2026-10-24-library-culture-dev.md`. The run is summarized in
  `docs/deliveries/2026-10-24-library-culture-dev.md`.
- The optional `[server]` extra (fastapi, uvicorn, mcp, python-multipart).
  Base dependencies stay empty, and the CLI imports in a bare venv.
- **Store:** SQLite WAL with content-addressed files. Items default to
  private visibility.
- **Ingest:** EPUB, PDF, Markdown, text and JSON. Audio is refused. Archives
  are size-capped and zip bombs are refused.
- **Keys and auth:**
  - `clk_` keys with read, write and admin scopes and per-kind write limits;
    the first one is minted with `culture-library keys mint`.
  - People sign in through Cloudflare Access, with a CSRF header required on
    writes. Access service tokens are refused.
  - An item's uploader can read its own private items.
- **Library API:** `/api/library` is compatible with xteink's, so the
  culture-rules push works unchanged. Delete archives an item for 30 days
  before it is purged. Duplicate bytes of a private item get an opaque 409.
- **Change feed:** `/api/feed?since=&item_id=`.
- **Search:** full-text (FTS5) over item bodies, plus semantic search through
  the lobes gateway. A background indexer feeds both.
  `/api/search` and `/api/status`.
- **Repo documents:** specs, plans and devague records keyed by remote, path
  and commit at `/api/repodocs`. A renderer handles frame and plan schemas 1
  to 6.
- **Send to device:** publishes `library.item.send_requested` through
  events-cli and serves the content by reference. Targets come from config.
- **Reading apps:** OPDS 1.2 at `/opds` and read-only WebDAV at `/dav/`.
- **MCP server:** `python -m culture_library.mcp`, with push, list, search,
  read, send and diary tools.
- **The site:** ported from the approved prototype. Strict CSP, vendored
  Lato, right-to-left text support. Design contract in
  `docs/design/library-site.md`, with 30 approved reference screenshots.
- **Opt-in visual parity tests** (`visual` group, `-m visual`) and a
  dedicated CI job.
- **Operator CLI:** `culture-library migrate from-xteink`,
  `culture-library backup run|verify|restore|schedule`, and
  `culture-library keys mint|list|revoke`.
- **Deploy files and runbooks:** `deploy/` (Dockerfile, compose) and
  `docs/operations/` (deploy, backup, ebooks.culture.dev teardown gate).

### Fixed

These were found by code review on PR #8.

- Backups now cover repo-document files as well as items.
- A corrupt EPUB upload returns 400 `corrupt_archive`, not 500.
- A kind-limited key can no longer restore an archived item of another kind
  by re-uploading it.
- OPDS search returns the newest matches.
- `/api/excerpts` rejects non-ASCII digits with 400.
- Checking a key no longer takes the write lock.
- The reader serves text that is already indexed.
- Uploads index their text once instead of extracting it twice.
- The CI Sonar findings are fixed:
  - PDF text is read from stdin;
  - browser-storage keys and values are bounded;
  - unhandled promises are handled.
- The 259 SonarCloud maintainability findings on PR #8 are resolved:
  - tests assert one fact per `assert`, and `pytest.raises` blocks hold a
    single call;
  - complex functions are split into helpers, with behaviour unchanged;
  - regular expressions that could backtrack are replaced by linear ones;
  - the site toast is an `<output>`, and the search label has accessible text.

### Changed

- `CLAUDE.md` describes the built library instead of the scaffold, and
  records the `[server]` extra.

## [0.10.1] - 2026-10-24

### Changed

- `CLAUDE.md` re-initialized from the seed placeholder into the full runtime prompt for the culture-library agent: purpose and early-scaffold status, commands (incl. single-test and the exact CI lint set), CLI architecture contracts, identity and harness rules, skills, conventions, and a Roadmap for the not-yet-built catalog (ingest, index, search, serve at library.culture.dev).
- `README.md`, `QWEN.md`, `AGENTS.override.md` and `AGENTS.colleague.md` now describe the library agent rather than the clonable template the scaffold rename inherited; template clone/rename instructions removed.

### Fixed

- `AGENTS.override.md`, `AGENTS.colleague.md` and `README.md` cross-referenced `CLAUDE.md` sections ("Cloning this template", "Prompt files by harness") that the seed CLAUDE.md no longer contained.

## [0.10.0] - 2026-10-06

### Added

- `culture.yaml` gate section: `gate.setup` and `gate.test` declare the argv
  lists that set up and test the repo (`uv sync`; `uv run pytest -n auto`),
  documented in the README.

## [0.9.0] - 2026-09-06

### Added

- **Four agent harnesses, each reading exactly one root file.** Claude
  Code→`CLAUDE.md`; Pi/`associate`→`AGENTS.override.md` (context) plus
  `.pi/SYSTEM.md` (system prompt, which *replaces* Pi's default);
  colleague→`AGENTS.colleague.md`; Qwen Code→`QWEN.md`. Deliberately **no**
  `AGENTS.md` — `AGENTS.override.md` is what stops Pi inheriting `CLAUDE.md`.
- One skill tree, four loaders: `.qwen/skills`, `.colleague/skills` and
  `.pi/skills` are relative symlinks onto `.claude/skills`. No forked scripts,
  no duplicated docs.
- `docs/harness-selection.md` (the two selections), plus
  `docs/automation-contract.md` and `docs/harness-invocations.yaml` (the four
  forced invocations as a machine-readable contract), and
  `docs/harness-verification.md` (the instrumented run).
- `scripts/harness-smoke.py` — a per-harness CI check that fails when **any one**
  of the four configs is broken, so three-quarters of a clone cannot rot
  unnoticed. A skipped check is reported as not-verified, never as a pass.
- `scripts/scan-secrets.py` — CI gate against committed credentials and
  non-localhost endpoints, with planted-secret tests proving it catches.

### Changed

- `culture.yaml` declares `backend: claude`. Because no code path rewrites that
  key, the template's declaration is what every clone inherits — the previous
  `colleague` value is why ~30 siblings carry a backend disagreeing with their
  seeded prompt file.
- `backend-fingerprints.yaml` re-synced from steward: list-valued prompts, so
  `acp` accepts `QWEN.md` and `colleague` accepts `AGENTS.override.md` and
  `.pi/SYSTEM.md`.

### Fixed

- `CLAUDE.md`, `README.md`, `QWEN.md`, `AGENTS.override.md`,
  `AGENTS.colleague.md` and `docs/skill-sources.md` had claimed this repo was a
  colleague resident, contradicting `culture.yaml`. Every harness prompt now
  names `backend: claude` / `CLAUDE.md` as the mesh resident, while stating
  that its own harness stays interactively available regardless.
- `.pi/settings.json`'s `skills` key is **inert** — that key belongs to a
  `package.json` manifest, not `settings.json`. Syscall instrumentation on a
  fresh clone: settings alone opened **0** `SKILL.md`; the `.pi/skills` symlink
  opened **19**. Replaced with the symlink, settings file removed.
- `doctor` no longer accepts an interactive harness's prompt file as the mesh
  resident's. Four harnesses ride three backend names, so `AGENTS.override.md`,
  `.pi/SYSTEM.md` and `QWEN.md` are *recognized* under `colleague`/`acp` — but
  the Culture daemon reads exactly one file per backend, and a clone carrying
  only Pi's files under `backend: colleague` used to report healthy with no
  resident prompt at all. `prompt_file_present` now requires the resident
  prompt; the others are reported by a new `harness_prompts` info check.
- `scan-secrets.py` closes three evasions: values containing `@`/`:`/`%`/`=`/`?`
  are now matched in full instead of stopping at a base64-ish alphabet (a
  quoted JSON key is matched too, which it never was); the placeholder
  exemption is a whole-value judgement, so a high-entropy literal merely
  *containing* `fake`/`example` is still reported; and endpoint hosts are
  parsed with `urlsplit`, so a bracketed IPv6 authority such as
  `http://[2001:db8::1]:8080` can no longer slip past the localhost allowlist.
- `harness-smoke.py`: a live probe is satisfied by **stdout only** (stderr
  noise mentioning `yes` or a prompt filename no longer counts as an answer,
  and a yes/no probe must answer exactly `yes`); a nonzero exit from `steward
  doctor` or `guild create` can no longer reach a passing branch on
  success-shaped JSON; failure/skip/waiver diagnostics go to stderr, leaving
  stdout to results; and an unknown `--stage` exits 1 (user error) rather than
  argparse's 2 (reserved for environment failures).

Known gaps carried into this release (not a changelog category — recorded
here so the release is not read as claiming more than it delivers): colleague
loads **0 of 19** skills from the nested tree, blocked on two upstream defects
filed with a reproduction as
[`agentculture/colleague#494`](https://github.com/agentculture/colleague/issues/494)
(the template ships the correct shape regardless); and retrofitting
already-provisioned siblings is an explicit **non-goal**, so
`culture-library#25` stays open for the existing fleet.

## [0.8.0] - 2026-09-05

### Added

- **`validate-delivery` skill** (origin `devague`, re-broadcast by
  `guildmaster`) — the validation leg between `/assign-to-workforce` and
  `/summarize-delivery`: run the confirmed plan's behavioral tests agent-side,
  then file obligations, evidence, and behavioral deltas. Record-only; the
  `devague` CLI never runs a test.
- **`scripts/` wrappers for the five prompt-only workflow skills** — `scope`,
  `challenge`, `deviate`, `validate-delivery`, `summarize-delivery`. Each
  forwards its arguments to the `devague` CLI verbatim, so upstream owns the
  surface. Every clone now ships a complete, convention-clean skill directory
  instead of inheriting the script-less shape.

### Changed

- **Re-synced all eight `devague`-origin workflow skills** — `scope`, `think`,
  `challenge`, `spec-to-plan`, `assign-to-workforce`, `deviate`,
  `validate-delivery`, `summarize-delivery` — from devague `0.24.1`
  (`ec15362`), matching guildmaster's canonical copies. Notable upstream
  content: `/scope` fans out to read-only exploration subagents at 5+ candidate
  surfaces, and `assign-to-workforce split-plan --write` persists a durable
  gate-2 record.
- **All eight `SKILL.md` files are now byte-verbatim with upstream.** devague
  ships `type: command` on all eight itself, so nothing is added to the
  frontmatter here. The only divergence is the five wrapper scripts, which are
  additions, not edits.
- **`docs/skill-sources.md` updated for the re-sync** — count corrected from
  seven skills to eight (`validate-delivery` had no row), all eight rows
  repointed to `../guildmaster/.claude/skills/`, and pins refreshed to devague
  `0.24.1`.
- **The 2026-07-15 "vendor directly from devague" divergence is superseded.**
  That decision existed to keep guildmaster's `scripts/*.sh` wrappers out of
  this repo. The wrappers are now wanted: without them a clone ships a
  `SKILL.md` with no sibling `scripts/` and fails a
  `test_skills_convention`-style gate (guildmaster#95). The old section is
  retained, marked superseded, with its stale re-sync recipe replaced — that
  recipe would have deleted the wrappers this release adds and skipped
  `validate-delivery` entirely.

### Fixed

- **`scope.sh` usage advertised an invalid claim kind** — `--kind non-goal`
  (hyphen) is rejected by `devague capture`, which accepts `non_goal`. Anyone
  copying the wrapper's usage line got `invalid choice: 'non-goal'`. Corrected
  in both this repo and guildmaster.

## [0.7.0] - 2026-08-24

### Added

- **`resume <task-id|last> [--detach]` verb** in `ask-colleague` — pick a cut / timed-out / SIGTERM'd run back up from its persisted artifact, continuing on the original `colleague/<id>` work branch.
- **Per-seat thinking effort** in `ask-colleague` — `--effort` (acting seat), `--seat-effort S=R` (any seat), `--role NAME` (colleague#416). Rule of thumb: `--effort off` for small well-specified briefs, default for ordinary work, `xhigh` for open-ended judgement.
- **Review diff front-loading** — `ask-colleague review` embeds a filtered, bounded diff directly in the prompt instead of relying on the colleague run to fetch it.

### Changed

- **`ask-colleague` re-vendored byte-verbatim from `agentculture/colleague` @ 1.63.0** (cite-don't-import) — all five files (`SKILL.md`, `scripts/ask-colleague.sh`, `prompts/{explore,review,write}.md`). Every repo scaffolded from this template (`guild create` instantiates it) shipped the Qwen3.6-era wrapper until now.
- **Default colleague model is `unsloth/Qwen3.8-27B-NVFP4`** (was the Qwen3.6 pin). The lobes gateway on `:8001` no longer serves 3.6, so the previous default only worked via colleague's auto-refresh warning path.
- **`docs/skill-sources.md` ledger row** for `ask-colleague` updated to the 1.63.0 sync (was `2026-06-12 (colleague 1.7.0, direct)`) and its verb list extended with `plan` / `resume` / the pilot verbs.

## [0.6.1] - 2026-07-20

### Added

- **Worktree location convention** in `CLAUDE.md` — every worktree you create
  by hand (workforce fan-out lanes, scratch checkouts) lives in
  `../.worktrees.culture-library/<name>/`, one
  repo-named directory beside the checkout, replacing a shared `../worktrees/`
  folder. This workspace holds many sibling projects, so a generic shared
  folder accumulates orphaned trees from several repos at once with nothing
  indicating ownership — a stale-tree sweep can't tell a live lane from junk.
  Matches the convention already documented in sibling repo `reachy-mini-cli`.
  Adds branch-prefix guidance (scope the prefix to the work; plain `agent/*`
  collides with leftovers from earlier fan-outs and fails `git worktree add
  -b`), and notes that the vendored `assign-to-workforce` skill uses both the
  shared path *and* `agent/<task-id>` branches in its fan-out example — it is
  cited verbatim and must not be edited, so both are overridden when following
  it. Teardown guidance names `git worktree remove <path>` as the verb that
  actually deletes a worktree; `git worktree prune` only clears metadata for
  directories that are already gone. Tool-managed throwaways are explicitly
  out of scope: `ask-colleague`'s read-only verbs create a detached worktree
  under `${TMPDIR:-/tmp}` and reap it on an EXIT trap, so they never persist
  to need an owner.

## [0.6.0] - 2026-07-18

### Added

- **Four devague-origin skills re-vendored into `.claude/skills/`**
  (cite-don't-import), synced to the fixed devague source
  (devague#74/#75/#76):
  - `challenge` — a risk-scaled blind-spot discovery pass that runs between
    `/think` and `/spec-to-plan`, routing findings back through the existing
    deterministic moves as human-adjudicated proposals.
  - `scope` — the idea→scope leg that surveys the surfaces an idea touches
    before framing, seeding the Announcement Frame with provenance-backed
    boundary/non-goal/assumption claims.
  - `deviate` — stops an in-flight `assign-to-workforce` run when execution
    must diverge from the confirmed plan and records the divergence as a
    first-class, append-only deviation record.
  - `summarize-delivery` — closes the loop after an `assign-to-workforce`
    run with a planned-vs-actual accountability artifact.

  These four originate in `devague` and are re-broadcast via guildmaster; see
  `docs/skill-sources.md` for provenance.

## [0.5.0] - 2026-06-24

### Added

- **Memory-discipline "Conventions and workflow" section in `CLAUDE.md`** — a
  per-task *recall-before / remember-after* convention (scope localized to this
  repo's nick) so the vendored `remember` / `recall` skills are actually used,
  not just present: `/recall` before non-trivial work to build on prior
  decisions instead of re-deriving them, and `/remember` when a non-obvious
  decision, constraint, fix-and-why, or hard-won gotcha surfaces. The section
  documents this repo's memory as **in-repo and public** — records resolve to
  `<repo-root>/.eidetic/memory` (committed, team- and mesh-shared). Inserted
  idempotently (skipped if already present), slotted under an existing
  "Conventions and workflow" heading when one exists, else appended.

### Changed

- **Refreshed the `remember` + `recall` wrappers from eidetic-cli 0.10.0**
  (cite-don't-import) — picks up eidetic's **project-local store default**: the
  files backend now resolves per record by visibility — PUBLIC records inside a
  git repo go to `<repo-root>/.eidetic/memory` (committed, team-shared), PRIVATE
  records (or any record outside a repo) go to `$HOME/.eidetic/memory` (never
  committed), an explicit `EIDETIC_DATA_DIR` still wins, and recall reads both
  stores and merges. Also carries the 0.9.3 hardening (interactive-stdin guard,
  `help` as a search term, SIGPIPE-safe suffix parsing). **Recipe policy
  override (the wrappers here are NOT byte-verbatim):** the injected default
  visibility is flipped from eidetic's `private` to **`public`**, so a plain
  `/remember` lands the note in `./.eidetic/memory` in this repo, kept as part
  of the repo — pass `--visibility private` to route a record to `$HOME`
  instead. `remember` drives `eidetic remember` (idempotent upsert of one JSON
  record or an NDJSON batch on stdin); `recall` drives `eidetic recall` with
  four search modes (exact / approximate / keyword / hybrid). Each `SKILL.md` is
  localized only in the illustrative `--scope <nick>` examples (Provenance keeps
  "First-party to eidetic-cli"). Runtime dep: the `eidetic` CLI on PATH (else a
  local eidetic-cli checkout with `uv`) — **`eidetic >= 0.10.0`** for the
  in-repo routing; on an older CLI the public records still work but are stored
  in `$HOME/.eidetic/memory` instead of in-repo. Propagated by rollout-cli's
  `eidetic-memory` recipe.

## [0.4.0] - 2026-06-23

### Added

- **Vendored the `remember` + `recall` memory skills from eidetic-cli**
  (cite-don't-import) — the write/read halves of eidetic's shared
  `$HOME/.eidetic/memory` surface, so this agent (Claude and its colleague
  backend) can persist facts across sessions and recall them later, sharing
  one store.
  `remember` drives `eidetic remember` (idempotent upsert of one JSON record or
  an NDJSON batch on stdin, dedup by id + content hash); `recall` drives
  `eidetic recall` with four search modes — exact / approximate / keyword /
  hybrid — each hit carrying text, full provenance metadata, a relevance score,
  and a freshness signal. The `.sh` wrappers are byte-verbatim from eidetic-cli
  (their first-party origin); each `SKILL.md` is localized only in the
  illustrative `--scope <nick>` examples (Provenance keeps "First-party to
  eidetic-cli"). Both default to this agent's PRIVATE scope, reading the suffix
  from `culture.yaml`. Runtime dep: the `eidetic` CLI on PATH (else a local
  eidetic-cli checkout with `uv`). Propagated by rollout-cli's `eidetic-memory`
  recipe.

## [0.3.4] - 2026-06-20

### Fixed

- Identity docs and self-description strings still claimed `backend: claude`
  (prompt file `CLAUDE.md`), but this template was promoted to a colleague
  resident in #14/#15: `culture.yaml` declares `backend: colleague` (Qwen) with
  `AGENTS.colleague.md` as the resident prompt. Corrected the stale claim in
  `CLAUDE.md` (Identity section), `README.md`, `docs/skill-sources.md`, and the
  two CLI description strings (`overview` artifacts and `explain doctor`). The
  `doctor` backend→prompt-file mapping and the tests were already on
  `colleague`; this aligns the prose and self-description with them.

## [0.3.3] - 2026-06-20

### Fixed

- pyproject.toml: correct the `license` field and PyPI classifier from MIT to
  Apache-2.0 to match the `LICENSE` file. The README License section was already
  corrected in 0.3.2, but the package metadata was missed; the built wheel now
  reports `License-Expression: Apache-2.0`.

## [0.3.2] - 2026-06-18

### Added

- ask-colleague skill: `monitor`/`guide`/`stop` pilot verbs plus a `--watch`
  flag to dispatch, watch the live feed of, send mid-flight guidance to, and
  cooperatively stop a running colleague flight (re-vendored from colleague).

### Changed

- README: correct the License section from MIT to Apache 2.0 to match the
  `LICENSE` file.

## [0.3.1] - 2026-06-13

### Changed

- CLAUDE.md: add a convention to reach for the `ask-colleague` skill reflexively
  for explore/review/write/grade — read-only `review`/`explore` are always safe;
  side-effecting `write` needs the user's go-ahead.

## [0.3.0] - 2026-06-13

### Added

- AGENTS.colleague.md resident prompt file (backend colleague <-> AGENTS.colleague.md)

### Changed

- Promote agent identity to a colleague resident: culture.yaml backend
  claude -> colleague with a pinned model. The `doctor` backend-consistency
  map gains `colleague` -> AGENTS.colleague.md.

## [0.2.1] - 2026-06-12

### Changed

- **Re-vendored the `ask-colleague` skill from colleague (now 1.7.0, up from the
  0.39.2 sync)** — the wrapper had drifted multiple releases behind origin. Picks
  up the `clean` verb (reap stale/corrupt `colleague/*` branches + orphaned
  `.colleague/` artifacts a crashed run left behind), the `--json` flag on every
  verb (result JSON on stdout, diagnostics/digest on stderr), the
  `_colleague_via_uv` local-dev resolution that honors `--repo`, and the
  tri-state (0/1/2) exit-code contract. `scripts/ask-colleague.sh` + `prompts/`
  are byte-identical to the origin; `SKILL.md` diverges only in the one
  consumer-identifying Provenance clause (`culture-library vendors from
  guildmaster`). `docs/skill-sources.md` sync row updated to
  `2026-06-12 (colleague 1.7.0, direct)`. Refs: colleague#183, #186.

## [0.2.0] - 2026-06-06

### Added

- **`ask-colleague` skill** (`.claude/skills/ask-colleague/`) — the first-party front door to the `colleague` CLI (the renamed `convertible`). On top of `explore` / `review` / `write` it adds a `feedback` verb (grade a finished work item — the ROI loop), and `write` now **previews by default** in a throwaway worktree (no side effects) unless `--apply` / `--pr` is given. Reach for it reflexively — `review` for a diverse second opinion on a committed diff before opening a PR, `explore` for a fresh read of an unfamiliar area.

### Changed

- **Replaced the `outsource` skill with `ask-colleague`.** `outsource` was renamed to `ask-colleague` upstream ([colleague#148](https://github.com/agentculture/colleague/pull/148)). Because guildmaster has not re-broadcast the rename yet (its kit still ships the old `outsource`), `ask-colleague` is vendored **directly from the sibling `colleague` checkout** rather than from guildmaster — a tracked local divergence recorded in `docs/skill-sources.md`, parallel to the `agex` → `devex` one. Vendored verbatim except one consumer-identifying clause in the Provenance paragraph.
- **Ledger + CLAUDE.md + `.gitignore`:** point `docs/skill-sources.md` and the CLAUDE.md Skills section at `colleague` / `ask-colleague`, swap the *optional* runtime prerequisite `convertible` → `colleague` (env prefix `CONVERTIBLE_*` → `COLLEAGUE_*`, with the legacy names kept as a deprecated fallback), and gitignore the `.colleague/` run-artifact dir the skill writes (plus the stale `.agex/`).

## [0.1.4] - 2026-05-31

### Added

- **Vendor the `outsource` skill** (`.claude/skills/outsource/`) from
  guildmaster's canonical copy (origin
  [`agentculture/convertible`](https://github.com/agentculture/convertible),
  re-broadcast via guildmaster — guildmaster
  [#51](https://github.com/agentculture/guildmaster/pull/51)). Every agent
  cloned from this template now inherits the ability to hand a scoped task to a
  *different* engine/mind: `explore` (read-only investigation), `review` (a
  diverse second opinion on the committed diff), and `write` (delegate a small
  implementation). `explore`/`review` run isolated in a throwaway `git worktree`;
  `write` refuses a dirty tree. Fulfils
  [#8](https://github.com/agentculture/culture-library/issues/8).
- **Ledger + CLAUDE.md:** record `outsource` in `docs/skill-sources.md`
  (origin = convertible, re-broadcast via guildmaster; vendored verbatim — it
  already carries `type: command`) and document its *optional* runtime
  dependency on the `convertible` CLI (the skill exits with an install hint if
  absent, so a clone that never uses it is unaffected).

### Changed

### Fixed

## [0.1.3] - 2026-05-31

### Changed

- Expanded the clone-and-rename instructions in `CLAUDE.md`: added `README.md` to
  the rename targets and a portable `git grep` discovery command so a cloner can
  find every occurrence of the template name (hard-coded in ~100 places across the
  package, including the CLI command files and `_ISSUES_URL` in
  `culture_library/cli/__init__.py`) rather than renaming by hand.
- Synced `README.md`'s "Make it your own" checklist with `CLAUDE.md`: it now lists
  `README.md` itself as a rename target and points to `CLAUDE.md`'s discovery
  command as the authoritative procedure, so the two onboarding checklists no
  longer drift.

## [0.1.2] - 2026-05-30

### Changed

- Renamed the PR-lifecycle CLI references `agex` / `agex-cli` to `devex` (same
  tool, new name) across `CLAUDE.md`, `docs/skill-sources.md`, `.gitignore`, and
  the vendored `cicd`, `assign-to-workforce`, and `communicate` skills — the
  `cicd` scripts now invoke `devex pr`.
- Logged the vendored-skill in-place patch as a local divergence in
  `docs/skill-sources.md`; the matching canonical rename is tracked upstream for
  guildmaster in
  [agentculture/guildmaster#48](https://github.com/agentculture/guildmaster/issues/48)
  so a future re-sync reconciles cleanly.
- Aligned the documented `devex` version floor to `>=0.21` across the vendored
  `cicd` `SKILL.md` and `workflow.sh` install hint (were `>=0.1`), matching
  `docs/skill-sources.md` and the `await`-era feature set; flagged upstream on
  guildmaster#48.

### Fixed

- SonarCloud now reports code coverage — added `relative_files = true` to
  `[tool.coverage.run]` so `coverage.xml` emits repo-relative paths that map to
  `sonar.sources=culture_library` (absolute / `.venv` paths were dropped
  as unmappable). Mirrors the sibling `convertible` setup.

## [0.1.1] - 2026-05-26

### Changed

- **CI gates on the SonarCloud quality gate**
  ([issue #3](https://github.com/agentculture/culture-library/issues/3)) —
  added `sonar.qualitygate.wait=true` to `sonar-project.properties` so a failing
  gate fails the `test` job when `SONAR_TOKEN` is set. Token-less repos and fork
  PRs remain green (the scan step is guarded by `if: env.SONAR_TOKEN != ''`).

## [0.1.0] - 2026-05-26

### Added

- **Onboarded into the AgentCulture mesh** ([issue #1](https://github.com/agentculture/culture-library/issues/1)).
- **Agent-first CLI** cited from teken's (`afi-cli`) `python-cli` reference
  (`teken cli cite`) — verbs `whoami`, `learn`, `explain`, `overview`, `doctor`,
  and the `cli` noun group. Runtime is self-contained (`dependencies = []`);
  `teken>=0.8` is a dev dependency only. Passes the seven-bundle agent-first
  rubric (`teken cli doctor . --strict`). `doctor` checks the agent-identity
  invariants (prompt-file-present, backend-consistency, skills-present).
- **Mesh identity**: `culture.yaml` (`suffix: culture-library`,
  `backend: claude`) and the matching `CLAUDE.md` prompt file.
- **Canonical guildmaster skill kit** (11 skills) vendored under
  `.claude/skills/` (cite-don't-import): `agent-config`, `assign-to-workforce`,
  `cicd`, `communicate`, `doc-test-alignment`, `pypi-maintainer`, `run-tests`,
  `sonarclaude`, `spec-to-plan`, `think`, `version-bump`. Every `SKILL.md`
  carries `type: command` (load-bearing for the culture/claude backend);
  `cicd` / `communicate` consumer-identifying prose adapted, all script bodies
  verbatim. Provenance in `docs/skill-sources.md`. Three skills (`think`,
  `spec-to-plan`, `assign-to-workforce`) originate in `devague`, re-broadcast
  via guildmaster.
- **Build + deploy baseline**: `pyproject.toml` (hatchling), `tests/` (pytest,
  xdist, coverage), `.github/workflows/{tests,publish}.yml` (CI rubric/lint gate,
  PyPI Trusted Publishing), `.flake8`, `.markdownlint-cli2.yaml`,
  `sonar-project.properties`, and `.claude/skills.local.yaml.example`.

### Changed

### Fixed
