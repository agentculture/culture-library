# Build Plan — devague work in the library

slug: `devague-work-in-the-library` · status: `exported` · from frame: `devague-work-in-the-library`

> culture-library holds every repo's devague work as it evolves — specs, plans, delivery summaries, designs and the raw .devague state (frames, plans, deliveries, reviews, questions) — browsable and searchable by repo/project and by domain, with the artifact bytes kept in an S3 bucket

## Tasks

### t1 — Repodocs core: working drafts, tombstones, design kind, binary content

- instruction: Own core/repodocs.py and core/models.py only. Drafts are keyed by doc, never by checkout path, so worktrees cannot multiply them. Keep the sha256 column name on `repodoc_drafts` so backup.`_referenced_blobs` picks it up. Add the design kind exactly like the todo/discord-message kinds were added (see CHANGELOG and docs/operations/deploy.md 'Upgrading across a schema change'). Do not touch routes.
- covers: c3, h2, c4, h3, c5, h4, c41, h27, c44, h30, c18, h16
- acceptance:
  - core/repodocs.py: push(commit='working') upserts one row in a new `repodoc_drafts` table (`doc_id` UNIQUE, sha256, size, `pushed_at`) and adds no `repodoc_versions` row; two working pushes leave the second content
  - a commit push after a draft adds one version and leaves the draft untouched; a draft whose sha256 equals the newest version is reported as not ahead
  - tombstone(repo, path, commit) sets `removed_at`/`removed_commit` on the doc, drops its draft, keeps versions readable; removed docs are excluded from list() unless `include_removed`
  - KINDS gains 'design'; `_KIND_PREFIXES` maps docs/design/ to design; first open of an existing DB rebuilds the items kind CHECK with row counts unchanged (existing `_migrate_items_kinds` path)
  - push accepts bytes unchanged (binary round-trips byte-identical); every existing test in tests/core/`test_repodocs.py` passes without assertion edits

### t2 — Projects core: project rows with domains and connected projects

- instruction: New file core/projects.py plus tests/core/`test_projects.py`; stdlib only. Import `normalize_remote` from repodocs, don't copy it. A test builds 3 projects in 2 domains and checks every filter's exact set (feeds success signal c38).
- covers: c6, h5, c38, h23
- acceptance:
  - new core/projects.py registers a projects table (repo UNIQUE, domains JSON list, connected JSON list, `updated_at`); upsert(repo, domains, connected) replaces both lists (latest push wins)
  - `repos_for`(domain=X) returns exactly the repos declaring X; `with_connected`(P) returns P plus its declared connections; an unknown domain returns \[\] not an error
  - connected entries are normalized with repodocs.`normalize_remote`, so '<https://github.com/agentculture/culture-library.git>' and 'github.com/agentculture/culture-library' are one project

### t3 — Spec links core: spec to PR, merge commit and current code paths

- instruction: New file core/speclinks.py + tests/core/`test_speclinks.py`. The library never computes links from git (c16); culture-rules sends them (decision c53: Spec: trailer when present, else files of the delivering merge followed through renames). Key links by repodocs doc id of the spec.
- covers: c40, h26
- acceptance:
  - new core/speclinks.py stores per spec doc: pr (url/number), `merge_commit`, and a `code_paths` set with the commit it was computed at, plus `trailer_commits`
  - `set_links`(doc, pr, `merge_commit`, `code_paths`, `at_commit`, `trailer_commits`) replaces `code_paths` only when `at_commit` is newer, so a rename pushed later shows the new path and an older push never regresses it
  - links(doc) returns merge, PR, current code paths and trailer commits; a spec with no links returns an empty structure

### t4 — Mirror core: storage protocol, readable keys and a durable outbox

- instruction: New file core/mirror.py + tests/core/`test_mirror.py`; stdlib only and network-free — the real S3 client lives outside core (c8). Reads blobs via Store.`read_bytes`. Outbox table keeps a sha256 column so backup covers it.
- covers: c7, h37, c42, h28
- acceptance:
  - new core/mirror.py defines a MirrorBackend Protocol (put(key, data, `content_type`), head(key) -> sha256 or None) and an in-memory FakeBackend for tests
  - `key_for`(repo, path, commit) returns `<repo>/<path>@<commit>` and draft keys end in `@working`; keys are stable and human-readable
  - enqueue() writes an outbox row (key, sha256, state pending, attempts, `last_error`) on the caller's connection; drain(backend) uploads pending rows, marks done, and on failure records attempts and `last_error` without raising
  - reconcile(backend) enqueues every version/draft whose key is missing or whose sha differs; `pending_stats`() returns count and oldest pending age

### t5 — Devague JSON text extractor for indexing

- instruction: New file + tests/core/`test_devague_text.py`. Detect the shape like render/devague.py `detect_kind` (claims -> frame, tasks/targets -> plan, `plan_slug`+evidence -> delivery). Don't import render/ from core/.
- covers: c11, h10
- acceptance:
  - new core/`devague_text.py`: `text_for`(path, data) turns a frame JSON into prose with title, every claim text, honesty condition text, hard questions and scope findings; plans give tasks, acceptance criteria and risks; deliveries give deviations, evidence and deltas
  - output for every frame/plan/delivery JSON in a fixture corpus contains no line made only of JSON punctuation or keys; unknown shapes fall back to the string values only

### t6 — Render delivery JSON, reviews and questions

- instruction: Own render/devague.py and its tests only. The reader route already serves .md as text; rendering reviews/questions as Markdown is the reader task's job (t-reader), not this one.
- covers: c14, h13
- acceptance:
  - render/devague.py detects delivery JSON (`plan_slug` with deviations/evidence/deltas) and renders deviations, evidence (with strength and outcome) and deltas, escaped
  - a corpus test over the deliveries of top-level ~/git repos (skipped when absent) renders each without the raw fallback; an unknown schema still renders raw with a notice

### t7 — Feed: repo document events

- instruction: Own core/feed.py and `routes_feed.py`. Use `register_migration` like `eval_store.py` does; no table rebuild. Keep `item_id` NOT NULL by storing the doc id there and relying on subject to disambiguate; document that in the module docstring.
- covers: c13, h12
- acceptance:
  - core/feed.py adds a subject column (default 'item') via ALTER TABLE ADD COLUMN and an `append_repodoc`(conn, `doc_id`, kind, sha256, data) that stores subject='repodoc' with data {repo, path, commit or 'working'}
  - `read_since` returns repodoc events alongside item events in cursor order; existing feed tests pass unchanged; item consumers can filter subject=item

### t8 — File the GitHub-removal follow-up issue with its full-corpus gate

- instruction: gh issue create in agentculture/culture-library; link the spec docs/specs/2026-10-30-devague-work-in-the-library.md. No code.
- covers: c49, h35
- acceptance:
  - a culture-library issue exists for removing specs/plans/.devague from GitHub (park v2), stating that removal requires a full-corpus verify pass — every repo, every file, sha256 equal in repo, library and S3 — not the 5-repo sample

### t9 — S3 backend as an optional \[s3\] extra

- instruction: Own `culture_library`/storage/ (new), pyproject.toml extras and tests/`test_zero_deps.py`. Production image installs .\[server,s3\]: update deploy/Dockerfile's install line only. Bucket/region/prefix come from env (`CULTURE_LIBRARY_S3_BUCKET`, `_REGION`, `_PREFIX`); credentials from the standard AWS env vars injected by grant.
- depends on: t4
- covers: c8, h7, c16, h15
- acceptance:
  - new `culture_library`/storage/s3.py implements MirrorBackend over boto3, imported lazily; pyproject gains an s3 extra; base dependencies stay \[\]
  - tests/`test_zero_deps.py` blocks boto3/botocore and still imports the whole CLI; a test asserts no module under core/ imports boto3, urllib.request or socket
  - put sets ServerSideEncryption and ContentType; the backend never calls DeleteObject (a test with a stubbed client asserts the called operations); the grep/AST no-git test still passes

### t10 — Push and list routes: working, tombstone, binary, scope, spec links, all-private

- instruction: Own server/`routes_repodocs.py` and its tests. Orchestrate in the route: RepoDocs.push -> mirror.enqueue -> feed.`append_repodoc` -> projects.upsert -> speclinks.`set_links`, on one connection. Keep the old JSON body shape valid (content as str, commit sha) so nothing breaks.
- depends on: t1, t2, t3, t4, t7
- covers: c5, c6, c9, h8, c20, h17, c45, h31, c41, c13, c40, c38
- acceptance:
  - POST /api/repodocs accepts commit='working' (no `committed_at`/head needed), a tombstone flag, content or `content_b64`, and project {domains, connected} plus optional `spec_links`; each version/draft enqueues a mirror outbox row and appends one repodoc feed event in the same transaction; a no-op re-push appends neither
  - every pushed doc is stored private whatever visibility the body sends; a read key without private access lists 0 docs
  - a body over `CULTURE_LIBRARY_REPODOC_MAX_BYTES` (default 16 MiB) gets 413 with code `too_large` and stores nothing; a 1.3 MB PNG round-trips byte-identical via GET /content
  - GET /api/repodocs accepts domain=, project=, connected=1 and `include_removed`=1 and returns exactly the expected sets for a 3-project/2-domain fixture
  - pushing the same (remote, path, commit) from two checkout paths yields one doc and one version

### t11 — Index repo documents for search and RAG, at low priority

- instruction: Own core/indexer.py and core/search.py. Do not require the push route to call the indexer: the indexer's loop discovers repodocs whose shown sha256 (draft if ahead, else current) differs from what it last indexed and enqueues them at low priority. Store repodoc rows in the existing FTS/embeddings tables under a source column (or a distinct rowid space) — choose the smallest change and say which in the docstring. Closes #4 on the core side.
- depends on: t1, t5
- covers: c10, h9, c46, h32
- acceptance:
  - core/indexer.py indexes each repodoc's shown text (draft if ahead, else current) using `devague_text` for .json and raw text for .md; binary/design files index only their path and title
  - `index_queue` gains a priority column; repodoc jobs enqueue at low priority, so a library item enqueued later is indexed first; a gateway failure leaves jobs queued and they resume
  - search.py rows carry source='item'|'repodoc'; repodoc hits obey private visibility exactly like items

### t12 — Backup covers drafts and the outbox

- instruction: Tests only in tests/core/`test_backup_repodocs.py` unless a gap is found; if backup.py needs a change, keep it to `_referenced_blobs`.
- depends on: t1, t4
- covers: c50, h36
- acceptance:
  - a test runs backup run + verify on a store with drafts and pending outbox rows and finds their blobs in the snapshot
  - restore onto an empty data dir brings back drafts and pending outbox rows with no S3 backend configured

### t13 — Mirror verifier script for sampled and full-corpus checks

- instruction: Scripts may read the filesystem but should not need git; it lives outside `culture_library` so the no-git rule is unaffected. Reuse mirror.`key_for` by importing `culture_library`.core.mirror.
- depends on: t4
- covers: c36, h21
- acceptance:
  - scripts/verify-mirror.py --repos A,B,... (or --all) compares sha256 of each repo file under the pushed prefixes against the library (API, key from env) and S3, printing missing/mismatched counts and exiting non-zero on any
  - it skips .worktrees.\*/ and \*-worktrees/ and works offline against a fixture with a fake library and fake S3 in tests/scripts/

### t14 — Mirror worker, reconcile CLI and status reporting

- instruction: Own server/`routes_mirror.py` (new), server/`routes_status.py`, cli/`_commands`/mirror.py (new), cli/`__init__.py` registration, explain/catalog.py and the learn text. Follow `routes_library.py`'s Purger lifespan pattern. The CLI talks to the server over HTTP like other verbs or opens the store directly — match backup's approach.
- depends on: t9, t10
- covers: c42, c7
- acceptance:
  - new server/`routes_mirror.py` lifespan starts a worker thread that drains the outbox with backoff when an S3 backend is configured, and does nothing when it is not
  - with a failing backend, pushes still return 2xx and the outbox grows; /api/status gains a mirror block {enabled, pending, `oldest_pending_age_s`, `last_error`}; after recovery the backlog reaches 0
  - culture-library mirror {overview,status,reconcile} exists with --json, explain entries and a learn mention; reconcile enqueues missing/mismatched keys and reports counts

### t15 — Search and RAG routes: repodoc source filter, items-only RAG default, eval config records it

- instruction: Own server/`routes_search.py`, server/`routes_rag.py` and eval/client.py/runner.py only where the retrieval config is recorded. Don't change Hit's shape for items; add source.
- depends on: t11
- covers: c10, c47, h33
- acceptance:
  - GET /api/search returns repodoc hits with source=repodoc (default sources: items and repodocs); POST /api/rag/retrieve defaults to sources=\[item\] and accepts sources=\[item, repodoc\]
  - the server-reported retrieval config includes sources, so eval runs store it; an eval set run against a fake library before and after repodocs are indexed retrieves the same item ids

### t16 — Read routes: drafts, removed docs, design assets, spec links

- instruction: Own server/`routes_read.py` (repodoc parts) and a new `routes_speclinks.py` if a separate route reads better. Keep existing response fields; add new ones.
- depends on: t10
- covers: c9, c40, c5
- acceptance:
  - GET /api/repodocs/{id} returns current, draft (with ahead flag), versions, removed state and spec links; GET /content?version=working serves the draft
  - design HTML is served with Content-Security-Policy sandbox and as a download, never as same-origin active HTML; images are served with their content type
  - GET /api/repodocs/{id}/rendered renders the draft by default when it is ahead (decision c51) and renders .md under .devague/reviews and .devague/questions as Markdown

### t17 — Push contract doc and the culture-rules issue

- instruction: Docs + the issue only. Use the communicate skill so the post is signed. Mention the push key's kinds: spec, plan, devague-record, design.
- depends on: t10
- covers: c39, h24
- acceptance:
  - docs/api/repodocs.md documents POST/GET /api/repodocs: commit or working, tombstone, content/`content_b64`, size limit, project {domains, connected} from culture.yaml, `spec_links` (Spec: trailer, else delivering-merge files followed through renames), worktree exclusion, all-private
  - an issue on agentculture/culture-rules is filed via the communicate skill, linking the spec and the contract doc, and asking for a rule that pushes every top-level repo's docs/{specs,plans,deliveries,design} and .devague/\*\* (all files)

### t18 — MCP repodoc tools with route parity

- instruction: Own mcp/tools.py and mcp/client.py. Copy the `EVAL_ROUTES` pattern (tools.py:244-257). Tools pass through the caller's key, so private visibility applies unchanged.
- depends on: t10, t15, t16
- covers: c12, h11, c33, h18
- acceptance:
  - mcp/tools.py adds `repodoc_list` (domain/project/connected filters), `repodoc_read` (current, working or a commit), `repodoc_links`, `repodoc_push` and repodoc search via the existing search tool's source filter
  - a `REPODOC_ROUTES` table maps each tool to (method, path) and a parity test fails if a repodoc route has no tool or a tool has no route

### t19 — Deploy: S3 settings, grant-injected AWS credentials, bucket safety check, key runbook

- instruction: Own cli/`_commands`/deploy.py, deploy/compose.yaml and docs/operations/deploy.md. Add the check-bucket explain entry after t14 lands (it owns explain/catalog.py in wave 3). Never put secrets in live.env (deviation d2).
- depends on: t9, t14
- covers: c43, h29, c48, h34, c44
- acceptance:
  - deploy/compose.yaml passes `CULTURE_LIBRARY_S3_`\* and AWS credential env through; deploy restart injects the AWS credentials from grant and refuses placeholder bucket settings, like it does `GATEWAY_API_KEY`
  - culture-library deploy check-bucket fails if Block Public Access, default encryption or versioning is off, or the configured credentials may call DeleteObject (IAM policy simulation or a dry-run probe), with a stubbed client in tests
  - docs/operations/deploy.md gains an S3 section (bucket creation, IAM policy with Put/Get/List only, grant names) and a key table naming each client's key and whether it has private access, incl. the culture-rules write key scoped to spec, plan, devague-record, design

### t20 — Site: browse by project and by domain

- instruction: Own new web/static/projects.js and projects.css, plus their script/link tags in index.html and the route in hashroute.js. Follow docs/design/library-site.md; if visual reference screenshots need adding, propose them for operator approval rather than committing as approved.
- depends on: t10, t16
- covers: c15, h14, c38
- acceptance:
  - a #projects view lists projects with their domains and doc counts and a #domains view lists domains with their projects; filters come from GET /api/repodocs
  - a project page groups each devague slug's spec, plan, delivery and frame together (joined on slug, not filename) and lists connected projects
  - a web test with 3 projects in 2 domains shows exactly the expected documents per filter

### t21 — Site reader: working badge, delivery pages, designs, spec links

- instruction: Own web/static/reader.js and reader styles in styles.css. No changes to projects.js.
- depends on: t16, t6
- covers: c9, c14, c40
- acceptance:
  - reader.js shows the working draft by default with a 'working' badge when it is ahead, and the committed version one click away
  - delivery JSON renders via /rendered; reviews/questions render as Markdown; design images show inline and HTML prototypes open sandboxed
  - a spec page shows its PR, merge commit and current code paths with the commit they were computed at

### t22 — End-to-end: one file of each kind through API, MCP and the mirror

- instruction: New test file only; use the app factory and FakeBackend. No live services.
- depends on: t14, t18, t15
- covers: c1, h1
- acceptance:
  - tests/e2e/`test_repodoc_flow.py` pushes a spec, plan, devague frame JSON and a design PNG from a fixture repo, then reads each byte-identical through GET /content, the MCP `repodoc_read` tool and the FakeBackend key after a drain
  - the same test finds the spec by a body phrase via search with source=repodoc, and sees one feed event per push

### t23 — Live migration: deploy, bucket, keys, first culture-rules cycles, verify, docs

- instruction: Operator-gated live steps; follow docs/operations/deploy.md and take a backup + rollback tag first, as in earlier runs. The culture-rules side is external: this task waits on the culture-rules issue from t17.
- depends on: t8, t13, t17, t19, t20, t21, t22
- covers: c29, h25, c34, h19, c35, h20, c36, c37, h22, c33
- acceptance:
  - before deploying, /api/status (admin key) shows 0 repo documents on live, and that count is recorded as evidence (c29)
  - the private AWS bucket exists, deploy check-bucket passes, the release with the s3 extra is deployed with deploy restart, and the culture-rules write key is issued with kinds spec, plan, devague-record, design
  - after culture-rules' rule runs: verify-mirror for 5 sampled repos (including one of reachy-mini-cli/agentculture-cli/cultureflare/culture-guide/notion-agent) reports 0 missing and 0 mismatched; a frame edit appears as the draft within one cycle with no new version row
  - CLAUDE.md, the sibling harness files where purpose changed, README and CHANGELOG describe what shipped; the version is bumped

## Risks

- [unknown_nonblocking] The live success signals (c34-c37) need culture-rules to ship its push rule; that work is in another repo and its timing is outside this plan (task t23)
- [follow_up] Repo identity is the git remote; renames or leaving GitHub would split history (frame park v3) — not handled in this run
- [unknown_nonblocking] The design kind widens the items kind CHECK, which rebuilds the items table on first open; take a backup before deploying (task t1)
