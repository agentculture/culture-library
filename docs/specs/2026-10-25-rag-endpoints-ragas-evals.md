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
- `s15` — `lobes gateway :8001`: gateway is reachable (returned `invalid_api_key` without a key); only /v1/embeddings is used in code; chat model availability unverified
  - seeds: `c15`, `q3` (question, resolved)
- `s16` — `GitHub issues #4 and #7`: \#4 adds repodocs to FTS/embeddings; #7 asks for recall@5 on real queries to compare gemma2-embed vs Qwen3 — the harness is the natural tool for #7
  - seeds: `c17`
- `s17` — `core/search.py semantic() performance`: brute-force cosine over all visible rows; acceptable for now, flagged not changed
  - seeds: `c18`
- `s18` — `culture_library/core/indexer.py _index_embeddings`: chunks are only written when an embedder is configured (early return when self.embed is None), so passage retrieval depends on embeddings; keyword-only passage retrieval would need a separate chunk store
  - seeds: `c3`

## Decisions

- Eval sets and run results live in the library DB (item-child tables, readable through /api/eval and MCP), not as repo files
- Eval runs start on demand from the CLI (eval generate, eval run) against a library server; no culture-rules schedule and no on-ingest generation in the first cut
- First cut success is a recorded baseline, not a quality gate: no score threshold blocks delivery
- First cut is retrieval-only: RAG endpoints return passages; evals score context precision/recall
- 'Newsletters' means the existing newspaper kind; its source links arrive as a culture-rules sidecar; the judge LLM is a configurable OpenAI-compatible endpoint defaulting to local lobes, with non-local endpoints limited to public items

## Hard questions

- Links are dropped at ingest today (Markdown keeps label only, ingest.py:236; EPUB `handle_starttag` ignores href attrs, ingest.py:181-185) and items have no `source_url` column — do we (a) extract links from the stored EPUB blob at eval time, (b) add a link table at ingest, or (c) have culture-rules push a sources sidecar? (resolved: User 2026-10-25: culture-rules pushes a sources sidecar (manifest of article→source URLs) with each issue; library ingest is not changed to extract hrefs)
- The user says 'newsletters' but KINDS (models.py:12-23) has no newsletter — is it the existing 'newspaper' kind (daily EPUB pushed by culture-rules, spec s5/s24), or a new kind for third-party newsletters? (resolved: User 2026-10-25: 'newsletters' = the existing newspaper kind (culture-rules daily EPUB); no new kind)
- No chat/completions client exists in the repo and the gateway's chat model list wasn't verifiable from here (key absent) — which model judges/generates (local Qwen3.8-27B via lobes per CHANGELOG:287, or Claude via API), and is cost/quality of a local judge acceptable? (resolved: User 2026-10-25: configurable OpenAI-compatible judge endpoint, defaulting to the local lobes gateway; an external model may only be used for public items)
- Is the first cut retrieval-only (context precision/recall against our search) or also a generation endpoint (/api/rag/answer) — the user said 'we fetch what we want', which reads retrieval-first? (resolved: User 2026-10-25: retrieval only for the first cut — context precision/recall; answer generation + faithfulness come later via the same adapter seam)
- Where do eval results live — in the library DB (new tables, viewable on the site/API) or as files/repo artifacts (e.g. docs/evidence JSON)? And who triggers runs: CLI on demand, a culture-rules schedule, or on ingest? (resolved: User 2026-10-25: eval sets + run results live in the library DB (item-child tables, readable via /api/eval and MCP); runs are CLI on demand (eval generate / eval run) against the live server; no schedule, no on-ingest generation)

## Open parks

- [unknown_nonblocking] Copyright/size: whole-book generation cost (tokens, time) on long books — sampling strategy (N chunks per book) undecided
- [unknown_nonblocking] Exact sidecar schema (fields per article; excerpt vs offsets) to be agreed with culture-rules in the c20 sibling issue
- [follow_up] Keyword-only passage retrieval (no embedder): needs chunks stored independent of embeddings, e.g. a chunk FTS table — deferred

## Resolved vagueness

- [unknown_blocking] Generated QA for private items: generating via an external LLM sends private book text off-box — need a rule on which visibility levels may be sent to which judge — resolved: User 2026-10-25 (q3): default judge is the local lobes gateway; a non-local endpoint may only receive public-item text
