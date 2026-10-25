# RAG endpoints + RAGAS evals

> culture-library ships RAG retrieval endpoints and a pluggable eval harness (RAGAS first): books get benchmarks generated from their text, newspapers get evals grounded in their sources and links, and every run proves search fetches what we want
> instruction: the success signals pass on the live library

## Audience

- Two readers: (1) machine clients that need grounded context — MCP agents, Gwen, culture-rules — calling RAG retrieve with scoped keys; (2) the operator measuring whether library search fetches the right passages, per content kind and per embedder
  - instruction: check the spec names MCP/agent clients and the operator as the consumers of /api/rag and eval results

## Before → After

- Before: Search returns item-level hits with a 200-char snippet (core/search.py Hit); nothing measures retrieval quality — t7 evidence explicitly did not evaluate it, and #7 (gemma2-embed vs Qwen3) has no way to be decided
  - instruction: cite docs/evidence/t7-embedding-cost.md and issue #7 in the spec's before-state
- After: An agent POSTs a query to /api/rag/retrieve (or the MCP `rag_retrieve` tool) and gets ranked passages it can cite; the operator runs `culture-library eval generate <item>` to build an eval set from a book's text or a newspaper's sources sidecar, then `culture-library eval run <set>` to score retrieval with RAGAS and store context precision/recall in the library, comparable across runs, modes and embedders
  - instruction: walk the after-state end to end against a test server with fake LLM/embedder in tests/eval/

## Why it matters

- Agents answering from the library need the passage, not the title; and without a measured baseline we can't choose an embedder, tune chunking, or notice search regressions
  - instruction: the spec's why section links the measurement to #7 and to future chunker/mode changes

## Requirements

- A chunk-level retrieve function returns (`item_id`, `chunk_no`, full chunk text, score) — core/search.py semantic() today keeps only the best chunk per item and truncates text to 200 chars (search.py:196-226), and Hit (search.py:47-55) has no chunk id, so RAG contexts need a new shape
  - instruction: tests/core/`test_rag.py`: passages() returns full chunk text + `chunk_sha` for a seeded item; private chunks excluded for a non-private caller
  - honesty: `chunk_sha` is sha256 of the exact stored chunk text, so a reference context matches a retrieved passage iff the text is identical
- A new `routes_rag.py` exposes POST /api/rag/retrieve {query, k, mode: semantic|hybrid, kind?, `item_ids`?} returning ranked passages \[{`item_id`, title, kind, `chunk_no`, `chunk_sha`, text, score, source}\], auto-included by server/app.py:34-46, gated by check(principal, 'read') with the same `include_private`/owner visibility as /api/search (`routes_search.py`:115-120); 503 `rag_unavailable` when no embedder is configured
  - instruction: tests/server/`test_rag_route.py`: retrieve returns passages; 503 `rag_unavailable` with no embedder; visibility parity with /api/search
  - honesty: /api/rag/retrieve never returns a passage from an item /api/search would hide from the same principal
- Eval sets (generated QA/benchmark samples) are stored in a new item-child table registered via `register_schema` with ON DELETE CASCADE and added to `_ITEM_CHILDREN` (store.py:67), so purging an item purges its evals
  - instruction: tests/core: eval tables created via `register_schema`; deleting an item cascades; `_ITEM_CHILDREN` updated and migration test passes
  - honesty: Deleting or purging an item leaves no orphaned eval set, sample or sidecar row
- Eval framework is a pluggable adapter interface (generate testset, score run) with RAGAS as the only shipped adapter; the dataset format and run records are framework-neutral so DeepEval/TruLens/custom recall@k can plug in later
  - instruction: an EvalAdapter protocol (`generate_testset`, score) in `culture_library`/eval/; a fake adapter in tests proves a second adapter plugs in with no core change
  - honesty: Nothing outside the RAGAS adapter module imports ragas
- ragas and its stack live in a new optional \[eval\] extra, imported lazily inside the eval CLI handler with a CliError 'install culture-library\[eval\]' hint, mirroring cli/`_commands`/server.py:51-61; tests/`test_zero_deps.py`:17 blocks the new modules
  - instruction: pyproject \[eval\] extra; eval CLI without the extra exits 2 with 'install culture-library\[eval\]' (tests/`test_zero_deps.py`)
  - honesty: pyproject dependencies stays \[\] and the base CLI imports without ragas installed
- A new 'eval' CLI noun (overview default, verbs e.g. generate / run / show), registered in cli/`__init__.py`, with explain catalog entries and learn.py mention so teken cli doctor --strict passes (tests.yml:77-78)
  - instruction: uv run teken cli doctor . --strict passes; explain eval / eval generate / eval run resolve
  - honesty: Every eval verb honours --json and the stdout/stderr split
- Eval tests run in CI without network or ragas: adapters are tested with injected fake LLM/embedder (same app.state.embedder injection as tests/server/`test_status_search.py`), and real ragas runs are an opt-in group/marker like 'visual'
  - instruction: eval tests use injected fakes; real-ragas tests carry a marker and are skipped in default CI
  - honesty: Default CI makes zero network calls and does not install ragas
- Book benchmarks are generated from the book's own extracted text (ingest.`extract_text` EPUB/PDF) into QA samples with reference contexts and reference answers
  - instruction: eval generate on a book item builds samples whose reference contexts are verbatim chunks of that book (`chunk_sha` matches the embeddings table)
  - honesty: Every reference context in a book eval set is a verbatim span of that book's indexed text
- Newspaper (kind=newspaper) evals are grounded in a sources sidecar pushed by culture-rules with each issue (article → source URL(s)); samples reference those sources and retrieval is scored on returning the passages derived from them
  - instruction: eval generate on a newspaper with a sidecar builds samples whose reference contexts come from the sidecar's articles; without a sidecar it errors with a hint
  - honesty: Every newspaper sample traces to at least one source URL in that issue's sidecar
- The RAGAS judge/generator LLM (and embeddings) is a configurable OpenAI-compatible endpoint defaulting to the local lobes gateway (`LOBES_GATEWAY_URL` + `GATEWAY_API_KEY`); a non-local endpoint may only receive text from public items
  - instruction: config via env (e.g. `CULTURE_LIBRARY_EVAL_LLM_URL` / `_MODEL`, defaulting to `LOBES_GATEWAY_URL`); fake endpoint in tests
  - honesty: With default config, no item text leaves the host
- Eval runs are recorded (dataset version, adapter+version, retriever config incl. embed model and mode, per-metric scores) so results compare across runs and frameworks
  - instruction: each stored run carries dataset id+version, adapter name+version, retriever mode, embed model, k, per-sample and aggregate scores
  - honesty: Two runs are only reported as comparable when they share the same eval set id and version
- Newspaper sources sidecar: PUT /api/library/{id}/sources (write scope) stores a JSON manifest \[{`article_title`, `source_urls`\[\], excerpt}\] in an `item_sources` child table (cascade on item delete); GET returns it under the item's visibility rule
  - instruction: tests/server: PUT then GET round-trips; a read key cannot PUT; deleting the item removes the sidecar; private-item sidecar invisible to non-owner public key
  - honesty: A sidecar for a private item is never visible to a principal that can't see the item
- Eval API: POST /api/eval/sets and POST /api/eval/runs (write scope) store generated sets and scored runs; GET /api/eval/sets?`item_id`= and GET /api/eval/runs?`set_id`= (read scope) return them, filtered by the underlying item's visibility
  - instruction: tests/server/`test_eval_api.py` covers store, list, visibility filtering and scope enforcement
  - honesty: Eval GET endpoints never expose sample text from items the caller can't see
- MCP gains a `rag_retrieve` tool mirroring POST /api/rag/retrieve (one `_spec` + `_handlers` entry in mcp/tools.py), so agents get passages through the same key-scoped path
  - instruction: tests/mcp: `rag_retrieve` listed in `tool_specs`() and returns the HTTP payload unchanged
  - honesty: `rag_retrieve` inherits the caller's bearer key, so MCP visibility equals HTTP visibility
- eval generate refuses to send a non-public item's text to a non-local judge endpoint (host not 127.0.0.1/localhost/::1) and exits with a CliError + hint
  - instruction: unit test: private item + judge URL <https://api.example.com> -> exit 1 with hint; same item + 127.0.0.1 -> proceeds
  - honesty: The locality check runs before any item text is read into a request body
- Every new item-child table (eval sets, eval samples, eval runs if item-keyed, `item_sources`) is added to `_ITEM_CHILDREN` so the items kind-rebuild counts and FK-checks it; the rebuild runs with `foreign_keys` OFF and checks only listed tables (store.py:68, 120-150)
  - instruction: tests/core/`test_store_migration.py`: seed rows in every new table, force a kind rebuild, assert counts unchanged and no FK violations
  - honesty: A future kind addition never silently drops or orphans eval or sidecar rows
- Each eval set pins the item sha256 (and chunker params) it was generated from; eval run refuses — or records the run as stale — when the item's current sha256 differs (PUT /api/library/{id}/file replace keeps the id but drops embeddings, library.py:306-370), when the item is archived, or when its `index_queue` row is not done
  - instruction: tests: generate set, replace item bytes, eval run -> refused/stale with a hint; archive item -> refused; pending re-index -> refused
  - honesty: No stored run mixes references from one version of an item's text with retrieval over another
- The local-judge rule (c36) also covers eval run: when the judge endpoint is non-local, scoring sends no text from non-public items — retrieved passages from private items (which a private-capable key can retrieve for any query) are excluded or the run uses ID-based metrics only
  - instruction: test: private-capable key, non-local judge, a query that retrieves a private passage -> that passage's text never appears in a judge request (fake judge records payloads)
  - honesty: With a non-local judge, the judge's request log contains only public-item text
- The server reports its retrieval config — embed model, chunker `max_chars`, chunk count — via /api/status (or each retrieve response), and eval run records those server-reported values, not client-side guesses (`routes_status.py` exposes no embed model today)
  - instruction: tests: /api/status includes `embed_model` and chunker; a stored run's config equals the server-reported values
  - honesty: A run's recorded embed model is the one that produced the vectors searched
- A run that fails midway (judge timeout, gateway down, 503) is stored with status=partial or failed and the completed sample count, or not stored — never as complete with aggregates over a subset
  - instruction: fake judge fails on sample 3 of 5 -> stored run status != complete and aggregates are absent or flagged
  - honesty: Aggregate scores are only reported for runs where every sample was scored
- Live enablement is a runbook step: docs/operations/deploy.md gains an 'enable embeddings' section — set `GATEWAY_API_KEY`, `LOBES_GATEWAY_URL`=<http://host.docker.internal:8001> (with the compose `extra_hosts`), `LOBES_EMBED_MODEL`=gemma2-embed and the reranker role in live.env, deploy restart, `requeue_all`, wait for `index_queue` all done and embeddings > 0; rollback = unset the vars and restart (search degrades to full-text, per the `routes_search.py` hybrid fallback)
  - instruction: after the runbook: live /api/status reports `embed_model` and a non-zero chunk count; /api/rag/retrieve returns passages
  - honesty: Rolling back the embedder leaves live search working in full-text mode with no data loss
- The embedder is gemma2-embed (google/embeddinggemma-2, served from orin), reached through the lobes gateway by role name — the same path embodiment uses (embodiment/vectors.py `EMBED_ROLE`)
  - instruction: live /api/status reports `embed_model` gemma2-embed and the embeddings table fills after re-embed
  - honesty: Vectors in the embeddings table are all from gemma2-embed (embeddings.model) — no mixed-model corpus
- RAG retrieval has a reranker stage using the lobes gateway's reranker role (Qwen/Qwen3-Reranker-0.6B on /v1/rerank)
  - instruction: retrieve with rerank on returns passages ordered by reranker relevance; eval runs record reranker on/off
  - honesty: Reranked and unreranked runs over the same eval set are stored as distinct, comparable configs
- Rerank detail: /api/rag/retrieve takes rerank (default true when a reranker is configured) and candidates (default 50); it fetches that many passages, POSTs {model: reranker, query, documents} to /v1/rerank (shape per lobes `roles_measure.py`:239), reorders by `relevance_score`, returns top k with both scores and reranked: true|false; an unreachable reranker degrades to the unreranked order with reranked: false, never a 5xx
  - instruction: tests with a fake reranker: reorders; failure -> reranked false + original order; eval run config records rerank + candidates so runs with and without rerank are comparable
  - honesty: A reranker outage changes ordering quality only, never availability of /api/rag/retrieve
- The library container reaches the gateway through a host-reachable URL: compose adds `extra_hosts` host.docker.internal:host-gateway and live.env sets `LOBES_GATEWAY_URL`=<http://host.docker.internal:8001> — the default 127.0.0.1:8001 is refused inside the container (bridge network culture-`library_default`)
  - instruction: docker exec culture-library-library-1 can GET $`LOBES_GATEWAY_URL`/capabilities -> 200 after deploy restart
  - honesty: The gateway key only travels to the configured gateway origin (no redirects followed), as embodiment's transport enforces
- Asymmetric prompts for gemma2-embed: chunks are embedded with `prompt_name` Retrieval-document and queries with Retrieval-query (the lane accepts these and refuses Query, per embodiment/vectors.py:20-23, `PROMPT_NAMES`); the prompt pair is part of the recorded embed config. Live has 0 embeddings today, so adopting it before the first re-embed costs nothing extra
  - instruction: tests: GatewayEmbedder sends `prompt_name` per role (document at index, query at retrieve); /api/status reports the prompt config
  - honesty: Index-time and query-time prompts are never mixed across embed configs within one run

## Honesty conditions

- Retrieval quality is measured against references derived from the content itself, not hand-written expectations that could flatter search
- No field of the /api/search hit is renamed or removed
- The production library image size and dependency set are unchanged by this work
- No file under `culture_library`/core/ imports a network or LLM client
- The library side works (accepts and stores a sidecar) before culture-rules ships its change
- Both audiences can reach their surface with existing auth: scoped API keys for machines, Access for people
- The before-state is cited from repo evidence (search.py Hit, t7 evidence, #7), not asserted
- The harness can drive #7's comparison without code changes, only config
- The after-state walkthrough runs end to end in a test with fakes
- The 6 baseline sets are generated from real library items, not fixtures
- The runs differ only in retriever config
- No CI job installs the \[eval\] extra by default

## Success signals

- Baseline recorded on the live library: eval sets exist for >= 3 books and >= 3 newspaper issues, and each set has >= 1 stored run with RAGAS context precision and context recall
  - instruction: GET /api/eval/runs on the live server lists >= 6 sets with >= 1 run each, each carrying both metric values
- The same eval set run under two retriever configs (e.g. mode semantic vs hybrid, or two embed models) yields 2 stored runs whose metrics are directly comparable, sufficient to inform #7
  - instruction: eval run twice on one set with different --mode; GET /api/eval/runs shows 2 runs with identical sample counts and differing config
- CI stays green with ragas absent: 0 new required runtime deps (pyproject dependencies == \[\]), `test_zero_deps` blocks ragas and passes, and eval tests run on fakes
  - instruction: uv run pytest -n auto on a plain uv sync; tests/`test_zero_deps.py` passes with ragas in the blocked set

## Scope / boundaries

- GET /api/search keeps its response shape (`item_id`, snippet, ...) — web/static/search.js:46 consumes only `item_id` and snippet and MCP's search tool returns the raw dict (mcp/client.py:202-206)
  - instruction: existing tests/server/`test_status_search.py` passes unchanged
- The production Docker image keeps installing only .\[server\] (Dockerfile:125); eval runs host-side or in a separate image/target, never bloating the library container
  - instruction: deploy/Dockerfile still installs .\[server\] only (grep in a test or review)
- core/ stays stdlib-only and network-free (enforced by tests/core/`test_store.py`::`test_core_source_has_no_network_imports`); any LLM/judge client lives beside gateway.py or in an eval package
  - instruction: tests/core/`test_store.py`::`test_core_source_has_no_network_imports` still passes
- A culture-rules change is needed to push the newspaper sources sidecar — filed as a sibling-repo issue via /communicate; the library side only accepts and stores it
  - instruction: a culture-rules issue is filed via /communicate and linked from the spec

## Non-goals

- No answer-generation endpoint in the first cut: RAG endpoints return retrieved passages only; faithfulness/answer-relevancy metrics wait for a later adapter-compatible addition
- Indexing repo documents into search (#4) and switching the default embedding model (#7) are separate issues; the eval harness should make #7's comparison possible (run the same eval set against two embedders) but does not perform the switch
- No ANN/vector-index rewrite: semantic() is brute-force pure-Python cosine (search.py:196-226); performance work is out unless evals show it blocks

## Assumptions

- Ground-truth contexts must be anchored by content (text hash / offsets), not by (`item_id`, `chunk_no`) — `set_embeddings` deletes and renumbers an item's chunks on every re-index, replace or `requeue_all` (search.py:141-148)

## Scope exploration

- `s1` — `culture_library/core/search.py`: Hit is item-level with a 200-char snippet; chunk text is stored in embeddings.text, so passage retrieval is a new function over existing data, not a new index
  - seeds: `c2`
- `s2` — `culture_library/server/routes_search.py + app.py`: `routes_`\*.py auto-include means no app.py edit; reuse read scope + `include_private`/owner visibility so RAG never leaks private items
  - seeds: `c3`
- `s3` — `web/static/search.js + mcp/tools.py`: existing search consumers depend on `item_id`/snippet; RAG is additive, a new MCP tool needs a `_spec` + `_handlers` entry (tools.py:249)
  - seeds: `c4`
- `s4` — `culture_library/core/store.py`: schema grows by idempotent `register_schema` DDL; child tables cascade on items(id); `_ITEM_CHILDREN` feeds the migration integrity count
  - seeds: `c5`
- `s5` — `culture_library/core/indexer.py + search.py set_embeddings`: chunk ids are unstable across re-index/replace/chunker change, so stored eval references must survive renumbering
  - seeds: `c6`
- `s6` — `user request (RAGAS only, design for more)`: no eval code exists anywhere in the repo (grep for ragas/benchmark/recall@ empty), so the adapter seam is greenfield
  - seeds: `c7`
- `s7` — `pyproject.toml + tests/test_zero_deps.py`: dependencies = \[\] is enforced; extras are the sanctioned path; zero-deps test must learn the eval module names
  - seeds: `c8`
- `s8` — `deploy/Dockerfile + compose.yaml`: image installs .\[server\] only; an eval job can hit <http://127.0.0.1:8796> with an API key from the host
  - seeds: `c9`
- `s9` — `culture_library/gateway.py + core network guard`: gateway.py is the only HTTP client and calls /v1/embeddings only; no chat/completions code exists
  - seeds: `c10`
- `s10` — `culture_library/cli/__init__.py + explain/catalog.py + learn.py`: nouns register via register(sub); every path needs an ENTRIES entry; nouns with actions need overview
  - seeds: `c11`
- `s11` — `.github/workflows/tests.yml`: CI does plain uv sync (dev group); visual is the precedent for an opt-in heavy group
  - seeds: `c12`
- `s12` — `culture_library/core/ingest.py (EPUB/PDF extraction)`: EPUB members are read in filename order, not spine order, and chapter structure is flattened (ingest.py:215-230) — generation works over flat text; chapter-aware questions are out unless extraction changes
  - seeds: `c13`
- `s13` — `culture_library/core/ingest.py link handling`: hrefs discarded in EPUB and Markdown extraction; no link/citation table or `source_url` column exists (models.py:28-38)
  - seeds: `c14`, `q1` (question, resolved)
- `s14` — `culture_library/core/models.py KINDS`: no newsletter kind; newspaper is the culture-rules daily web-sourced EPUB; adding a kind forces the items-table rebuild
  - seeds: `q2` (question, resolved)
- `s15` — `lobes gateway :8001`: verified 2026-10-25 via GET <http://127.0.0.1:8001/capabilities> (no key needed): gemma2-embed = google/embeddinggemma-2 on /v1/embeddings, ready, proxied from orin; reranker = Qwen/Qwen3-Reranker-0.6B on /v1/rerank, ready (orin/spark2/thor lanes); /v1/chat/completions served (cortex = Qwen3.8-27B-NVFP4 local, worker on thor) — a judge LLM exists
  - seeds: `c15`, `q3` (question, resolved)
- `s16` — `GitHub issues #4 and #7`: \#4 adds repodocs to FTS/embeddings; #7 asks for recall@5 on real queries to compare gemma2-embed vs Qwen3 — the harness is the natural tool for #7
  - seeds: `c17`
- `s17` — `core/search.py semantic() performance`: brute-force cosine over all visible rows; acceptable for now, flagged not changed
  - seeds: `c18`
- `s18` — `culture_library/core/indexer.py _index_embeddings`: chunks are only written when an embedder is configured (early return when self.embed is None), so passage retrieval depends on embeddings; keyword-only passage retrieval would need a separate chunk store
  - seeds: `c3`
- `s19` — `challenge pass / adjacent-systems lens: live library container env + library.db`: read-only probe: `GATEWAY_API_KEY`, `LOBES_GATEWAY_URL`, `LOBES_EMBED_MODEL` unset; 0 embedding rows; live search is full-text only, so RAG retrieve has nothing to serve
  - seeds: `q6` (question, resolved)
- `s20` — `challenge pass / unstated-assumptions lens: live corpus composition`: items by kind (live, read-only): book 1, newspaper 15, document 36, transcript 3, todo 2 — c30's >=3 books is not reachable today
  - seeds: `q7` (question, resolved)
- `s21` — `challenge pass / migration lens: core/store.py _rebuild_items`: rebuild drops/renames items with FKs off and verifies only `_ITEM_CHILDREN`; an unlisted child table would escape the integrity check (`item_uploaders` is already unlisted — pre-existing, out of scope)
  - seeds: `c37`
- `s22` — `challenge pass / lifecycle lens: core/library.py replace() + archive()`: replace swaps bytes under the same id, deletes embeddings and re-queues; archive hides the item from search — both silently invalidate `chunk_sha` references
  - seeds: `c38`
- `s23` — `challenge pass / security lens: c36 vs eval run data flow`: c36 gated only generate; LLM-based context precision/recall send retrieved passages to the judge, and retrieval can return other private items' passages
  - seeds: `c39`
- `s24` — `challenge pass / observability lens: server/routes_status.py`: no embed model or chunker info is exposed; c19's run record would otherwise rely on CLI config that can disagree with the server
  - seeds: `c40`
- `s25` — `challenge pass / failure-mode lens: eval run against live gateway`: judge LLM and embed gateway are separate failure points; no claim covered a half-finished run
  - seeds: `c41`
- `s26` — `challenge pass / cheap-probe lens: PyPI ragas`: latest ragas 0.4.3, requires openai, langchain, langchain-openai, datasets>=4, instructor — confirms the optional extra; API churned across 0.1/0.2/0.3/0.4, so the adapter pins a version
  - seeds: `q8` (question, resolved)
- `s27` — `challenge pass / recovery lens: core/backup.py`: clean: `_backup_db` uses sqlite online backup of the whole DB, so new tables are snapshotted and restored; verify() checks items/repodocs only, eval rows aren't verified but are derived and regenerable
- `s28` — `challenge pass / security lens: item visibility mutability`: clean: no route or core function updates items.visibility, so visibility-at-generation equals visibility-at-read; residual risk only if a visibility-change feature is added later
- `s29` — `challenge pass / concurrency lens: indexer vs eval run`: covered by the not-done/sha refusal requirement; residual: the live indexer and an eval run share one gateway — contention only, not correctness
- `s30` — `challenge pass / adjacent-systems lens: lobes-cli roles_measure.py rerank probe`: lobes posts {model, query, documents} to /v1/rerank (`roles_measure.py`:239); vLLM rerank answers results\[{index, `relevance_score`}\] — the library reranker client copies that shape
  - seeds: `c48`
- `s31` — `challenge pass / operations lens: live library container networking`: probe from inside culture-library-library-1: 127.0.0.1:8001 connection refused, host.docker.internal unresolvable, 172.17.0.1:8001 -> 200; compose sets no `extra_hosts` and the default gateway URL would fail silently into full-text
  - seeds: `c49`
- `s32` — `challenge pass / adjacent-systems lens: embodiment/embodiment/vectors.py`: embodiment calls gemma2-embed via the same gateway with `prompt_name` Retrieval-query / Retrieval-document; culture-library gateway.py:46 sends only {model, input} — no prompt
  - seeds: `c50`
- `s33` — `challenge pass / adjacent-systems lens: embodiment/embodiment/library/index.py`: Gwen keeps her own 0600 chunk index over library items because the library's search was title/author only for her; she reconciles by (item id, sha256). Once /api/rag/retrieve exists she is a natural client, but migrating her is out of scope here (a sibling-repo follow-up)

## Decisions

- Eval sets and run results live in the library DB (item-child tables, readable through /api/eval and MCP), not as repo files
- Eval runs start on demand from the CLI (eval generate, eval run) against a library server; no culture-rules schedule and no on-ingest generation in the first cut
- First cut success is a recorded baseline, not a quality gate: no score threshold blocks delivery
- First cut is retrieval-only: RAG endpoints return passages; evals score context precision/recall
- 'Newsletters' means the existing newspaper kind; its source links arrive as a culture-rules sidecar; the judge LLM is a configurable OpenAI-compatible endpoint defaulting to local lobes, with non-local endpoints limited to public items
- Enabling the embedder on live is part of this delivery: `GATEWAY_API_KEY` and `LOBES_`\* go into ~/.config/culture-library/live.env, culture-library deploy restart, then a full re-embed of the live corpus before the baseline
- The operator uploads at least 2 more books to live before the baseline run, so c30's >= 3 books holds as written
- Every eval run scores ID-based context precision/recall over `chunk_sha`; LLM-judged context precision/recall are added when a judge endpoint is configured, subject to c39

## Hard questions

- RAGAS 0.4.3 (PyPI probe) offers ID-based context precision/recall that compare retrieved vs reference context ids without an LLM — `chunk_sha` fits that exactly. Baseline on ID-based metrics only (deterministic, no judge at run time), LLM-based only, or both? (resolved: User 2026-10-25: both — ID-based (`chunk_sha`) context precision/recall on every run; LLM-based context precision/recall added when a judge is configured, under the c39 rule)
- Links are dropped at ingest today (Markdown keeps label only, ingest.py:236; EPUB `handle_starttag` ignores href attrs, ingest.py:181-185) and items have no `source_url` column — do we (a) extract links from the stored EPUB blob at eval time, (b) add a link table at ingest, or (c) have culture-rules push a sources sidecar? (resolved: User 2026-10-25: culture-rules pushes a sources sidecar (manifest of article→source URLs) with each issue; library ingest is not changed to extract hrefs)
- The user says 'newsletters' but KINDS (models.py:12-23) has no newsletter — is it the existing 'newspaper' kind (daily EPUB pushed by culture-rules, spec s5/s24), or a new kind for third-party newsletters? (resolved: User 2026-10-25: 'newsletters' = the existing newspaper kind (culture-rules daily EPUB); no new kind)
- No chat/completions client exists in the repo and the gateway's chat model list wasn't verifiable from here (key absent) — which model judges/generates (local Qwen3.8-27B via lobes per CHANGELOG:287, or Claude via API), and is cost/quality of a local judge acceptable? (resolved: User 2026-10-25: configurable OpenAI-compatible judge endpoint, defaulting to the local lobes gateway; an external model may only be used for public items)
- Is the first cut retrieval-only (context precision/recall against our search) or also a generation endpoint (/api/rag/answer) — the user said 'we fetch what we want', which reads retrieval-first? (resolved: User 2026-10-25: retrieval only for the first cut — context precision/recall; answer generation + faithfulness come later via the same adapter seam)
- Where do eval results live — in the library DB (new tables, viewable on the site/API) or as files/repo artifacts (e.g. docs/evidence JSON)? And who triggers runs: CLI on demand, a culture-rules schedule, or on ingest? (resolved: User 2026-10-25: eval sets + run results live in the library DB (item-child tables, readable via /api/eval and MCP); runs are CLI on demand (eval generate / eval run) against the live server; no schedule, no on-ingest generation)
- The live library runs with no embedder (`GATEWAY_API_KEY`/`LOBES_`\* unset in culture-library-library-1; embeddings table has 0 rows, `index_queue` 58 done) so /api/rag/retrieve would 503 live. Does this delivery include turning on the gateway key in live.env and a full re-embed (`requeue_all`), or is the baseline taken against a local/staging server instead? (resolved: User 2026-10-25: enable the embedder on live in this delivery — `GATEWAY_API_KEY` + `LOBES_`\* in live.env, deploy restart, full re-embed; baseline measured on live)
- Live has 1 book, 15 newspapers, 36 documents, 3 transcripts, 2 todos (non-archived). c30 needs >= 3 books: upload more books, lower the bar to the books that exist, or count documents/transcripts as long-form sources? (resolved: User 2026-10-25: keep >= 3 books; the operator uploads >= 2 more books before the baseline run)

## Open parks

- [unknown_nonblocking] Copyright/size: whole-book generation cost (tokens, time) on long books — sampling strategy (N chunks per book) undecided
- [unknown_nonblocking] Exact sidecar schema (fields per article; excerpt vs offsets) to be agreed with culture-rules in the c20 sibling issue
- [unknown_nonblocking] Gateway contention: an eval run's per-query embeddings + judge calls share the lobes gateway with the live indexer and colleague; throughput impact unmeasured
- [unknown_nonblocking] Residual: lenses examined here do not cover OPDS/WebDAV/send paths (judged untouched by this feature) or the culture-rules side of the sidecar; no claim is made that no other blind spots exist
- [follow_up] Keyword-only passage retrieval (no embedder): needs chunks stored independent of embeddings, e.g. a chunk FTS table — deferred
- [follow_up] Follow-up: offer embodiment (Gwen) /api/rag/retrieve as a replacement for her private chunk index — sibling-repo decision

## Resolved vagueness

- [unknown_blocking] Generated QA for private items: generating via an external LLM sends private book text off-box — need a rule on which visibility levels may be sent to which judge — resolved: User 2026-10-25 (q3): default judge is the local lobes gateway; a non-local endpoint may only receive public-item text
