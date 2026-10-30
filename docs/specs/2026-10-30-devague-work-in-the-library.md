# devague work in the library

> culture-library holds every repo's devague work as it evolves — specs, plans, delivery summaries, designs and the raw .devague state (frames, plans, deliveries, reviews, questions) — browsable and searchable by repo/project and by domain, with the artifact bytes kept in an S3 bucket
> instruction: end-to-end test: push one file of each kind from a fixture repo, then GET it through /api/repodocs, the MCP tool and the fake S3 backend

## Audience

- The operator, who reads specs, plans, deliveries and designs on library.culture.dev; mesh agents (via MCP and the API) that look up a project's or domain's current plan mid-work; and culture-rules, which pushes them

## Before → After

- Before: Committed specs, plans and devague records already have a home: core/repodocs.py stores them as (repo, path) docs with one commit-pinned version per push (`repodoc_versions`), current = newest default-branch version, kind inferred from path prefix (docs/specs → spec, docs/plans → plan, docs/deliveries and .devague/ → devague-record), visibility failing closed
  - instruction: uv run pytest tests/core/`test_repodocs.py` tests/server -k repodoc
- Before: Nothing pushes repo documents to the library today; specs, plans and devague records live mostly public on GitHub, and this work starts migrating them out of GitHub into the library (operator, 2026-10-30)
- After: Every top-level repo's specs, plans, delivery summaries, designs and raw .devague state — committed versions with history plus the latest uncommitted working draft — is in the library and mirrored to a private S3 bucket, viewable as-is, searchable, and browsable by project (with connected projects) and by domain

## Why it matters

- The development record of ~78 repos is scattered across GitHub, mostly public, and partly invisible (gitignored reviews/questions, five repos that ignore .devague entirely); one private, scoped home is what lets it move off GitHub and lets agents reach back into a project's or a domain's past decisions

## Requirements

- Work-in-progress state is captured, not only committed state: a version can be pushed for uncommitted/working-tree content (e.g. a frame mid-/think, a plan mid-run) and for the gitignored .devague/reviews and .devague/questions, without fabricating a commit sha — today every version requires `commit_sha` matching a 40/64-hex regex
  - instruction: core test: push working twice + commit once -> 1 draft (2nd content), 1 version
  - honesty: A push with commit=working creates or overwrites one draft per (repo, path) without adding a version row, and works for .devague/reviews and .devague/questions; a later commit push leaves the draft intact until it is overwritten again
- Repo documents carry project and domain scope: the project is the repo, its domains come from the repo's culture.yaml (pushed by culture-rules), and projects can declare connections to other projects; documents can be listed and filtered by project (including connected projects) and by domain through the API, the MCP tools and the site
  - honesty: Listing with domain=X returns only documents of projects declaring X; listing with project=P&connected=1 also returns documents of P's connected projects, and nothing else
- Every repo document version is written through to an AWS S3 bucket as a human-readable mirror tree — committed versions at `<repo>/<path>@<commit>`, the working draft at `<repo>/<path>@working` — behind a storage interface; the local content-addressed blob store stays the library's primary copy and backup source
  - instruction: test against an in-memory/fake S3 backend; a failing backend must not fail the push
  - honesty: After a push the S3 object at `<repo>/<path>@<commit>` (or @working) has the pushed bytes; when S3 is unreachable the push still succeeds locally, and the mirror catches up later
- Designs are a supported artifact: design docs with their binary assets (screenshots, HTML prototypes, images) — e.g. this repo's docs/design/library-site.md, prototype.html and 30 reference screenshots — can be stored and viewed, not only UTF-8 text
  - honesty: A PNG screenshot and an HTML prototype under docs/design/ push and download back byte-identical, and the site shows the image
- Specs, plans, deliveries and devague records are searchable (full-text and RAG) with the same visibility rule as items, closing issue #4
  - honesty: A search for a phrase that appears only in a spec's body returns that repo doc with source=repodoc, and never returns a private repo doc to a key without private access
- Indexing devague JSON goes through a JSON-aware text extractor (claims, honesty conditions, tasks, risks as prose), not the raw pretty-printed JSON
  - honesty: Indexed text of a frame JSON contains its claim texts and contains no JSON punctuation-only chunks
- Agents get MCP tools for repo documents (list by repo/project/domain, read current or a version, push, search) mirroring the HTTP routes one to one, like `EVAL_ROUTES`
  - honesty: Every repodoc HTTP route has exactly one MCP tool mapped to it in a `REPODOC_ROUTES`-style table, enforced by a parity test
- A repo document change emits a feed event so subscribers (culture-rules, agents) can react to a spec or plan update
  - honesty: A repodoc push (new version or draft overwrite) appends one feed event readable via GET /api/feed that names repo, path and version
- Delivery JSON, reviews and questions render as readable pages like frames and plans do
  - instruction: corpus test over ~/git/\*/.devague/deliveries/\*.json, top-level repos only
  - honesty: A delivery JSON from every schema version present in ~/git renders as a page (deviations, evidence, deltas) without the raw fallback; review and question markdown renders as Markdown
- The site gets a browse view by repo/project and by domain, with a spec's linked plan, delivery and frame shown together (joined on devague slug)
  - honesty: The site lists projects and domains, and a project page groups a slug's spec, plan, delivery and frame together
- culture-rules receives the push contract as a sibling-repo issue (via the communicate skill): endpoints, payload (repo, path, commit or working, content bytes, culture.yaml domains/connected), worktree exclusion and the all-private rule
  - honesty: The culture-rules issue is filed and links this spec; it states the payload fields, worktree exclusion and the all-private rule
- A spec is tied to the repo and commits that realize it: first to the merge that delivered it (repo, PR, merge commit), and later to the code that currently represents it — the link follows the implementing code forward as it changes, rather than freezing at the merge (operator, 2026-10-30)
  - honesty: For one delivered spec, the library shows its merge commit and PR, and after a later commit renames an implementing file, the spec's code links point at the new path
- Deletions propagate: when a file disappears from a repo (git rm, a deleted frame, a never-committed draft discarded), culture-rules sends a tombstone; the library marks the doc removed at that commit (history kept, hidden from current lists) and drops a stale working draft — nothing is deleted from S3
  - honesty: A tombstone push for a path hides the doc from current listings, keeps its version history readable, discards its working draft, and leaves its S3 objects in place
- S3 mirroring runs from a durable outbox table (doc version, key, state, attempts, last error) drained outside the push request; a reconcile verb re-mirrors anything missing, and /api/status reports mirror lag and failures
  - honesty: With the S3 backend failing, pushes still return 2xx, the outbox grows and /api/status shows the lag; after the backend recovers, a reconcile run leaves 0 pending and every key present
- A design kind is added (docs/design/\*\* and binary assets under the other prefixes), widening KINDS — so the items kind CHECK rebuild runs once on first open — and the culture-rules push key is a write key scoped to kinds spec, plan, devague-record, design
  - honesty: On first open after upgrade the items CHECK accepts 'design' with row counts unchanged; a push to docs/design/x.png gets kind design; the culture-rules key is refused a write of kind book
- Repo document pushes accept binary content (multipart or base64) up to a configured size limit like `UPLOAD_LIMITS`, refusing larger files with a structured 413; the largest file in the corpus today is 1.3 MB
  - honesty: A 1.3 MB PNG pushes and downloads byte-identical; a file over the limit gets 413 with a structured error and nothing is stored
- The initial backfill indexes through the existing index queue at low priority so it never starves item indexing or the gateway: about 4,600 files at roughly 13-22 chunks/s for gemma2-embed is tens of minutes of embedding, not one burst
  - honesty: During the backfill, a newly uploaded library item is indexed ahead of queued repo documents, and the backfill continues after a gateway outage
- Agents that should read the migrated corpus hold keys with private access (all docs are private, c31); a shared read-only key without private access sees none of it, and this is documented per client (Gwen, xteink, culture-rules, mesh MCP)
  - honesty: A read key without private access lists 0 migrated documents; a key with private access lists all of them; docs/operations names which clients hold which

## Honesty conditions

- Spec, plan, delivery, design and .devague files from one repo, pushed via the API, are each readable in the site and via MCP, and present in S3 under their readable key
- Repodocs behavior that exists today is unchanged: tests/core/`test_repodocs.py` and the repodocs route tests pass without edits to their assertions
- Pushing the same (repo, path, commit) twice is still a no-op and a new commit still adds a version, not a new doc
- tests/`test_zero_deps.py` still passes with base dependencies = \[\] and the S3 client blocked from the CLI import; core/ imports no network library
- grep/AST test still shows no subprocess, git or devague invocation anywhere in `culture_library`
- Pushing the same file from a repo and from one of its worktrees yields one document, not two
- A document pushed from a public GitHub repo is invisible to a read key without private access; the bucket policy blocks public access
- At the start of the run, no repo documents exist on live (repodocs count 0 per /api/status `by_kind`)
- The operator, an MCP agent and culture-rules each have a working path: site page, MCP tool, push endpoint
- For the sampled repos, library + S3 contain every committed version pushed and the current working draft of each touched file
- The five repos that gitignore .devague (reachy-mini-cli, agentculture-cli, cultureflare, culture-guide, notion-agent) have their .devague files in the library after a culture-rules cycle
- A sampling script compares sha256 of repo files vs library vs S3 for 5 repos and reports 0 missing, 0 mismatched
- Measured: time from saving a frame to the draft showing in the library is at most 1 culture-rules cycle; `repodoc_versions` row count unchanged
- A test with three projects in two domains shows each domain/project filter returns exactly the expected set in API, MCP and site
- A deploy check reads the bucket's public-access-block, encryption and versioning settings and the IAM policy, and fails if any is off or DeleteObject is allowed
- Re-running eval set 1 at the same config after the corpus is indexed gives the same retrieved item ids as before, because repodocs are excluded by default
- The v2 follow-up's plan contains a full-corpus verify task whose pass is required before any file is removed from GitHub
- culture-library backup run followed by verify covers draft blobs; restoring the snapshot onto an empty data dir brings back drafts and the outbox with S3 unavailable

## Success signals

- For at least 5 sampled repos, 100% of committed `docs/{specs,plans,deliveries}/*.md`, `docs/design/**` and `.devague/**` files pushed by culture-rules appear in the library and in S3 with sha256 matching the repo file
- A working-draft edit to a frame reaches the library within 1 culture-rules cycle and replaces the previous draft without adding a version (versions count unchanged, draft sha256 updated)
- Filtering by one domain or one project (with connected projects) returns exactly the documents of the repos declaring it — 0 from others — in the API, MCP and site

## Scope / boundaries

- Reuse, don't replace, the repodocs identity and versioning model — (repo, path) identity, (repo, path, commit) dedup and the default-branch 'current' rule stay; the original spec's promises (docs/specs/2026-10-23-library-culture-dev.md lines 40-48) remain the contract this extends
- core/ stays stdlib-only and network-free and base dependencies stay empty: any S3 client is an optional extra imported lazily outside core/, behind a protocol core/ defines
  - instruction: uv run pytest tests/`test_zero_deps.py`
- The library only receives pushed content: nothing in `culture_library` runs git or devague; the collector that reads repos and .devague dirs lives outside this package (a culture-rules rule, a devague hook, or a separate client)
- Git worktree copies (.worktrees.\*/, \*-worktrees/) never multiply documents: dedup stays on (repo, path, commit) and working-state pushes are keyed by repo, not by checkout path
- Migrated documents are private: every repo document pushed by culture-rules lands private regardless of the repo's GitHub visibility (decision c31), and the S3 bucket is never publicly readable — bytes reach readers only through the library's visibility check
  - instruction: core test + a deploy check that reads the bucket's public-access-block
- The bucket is private and append-only from the library's side: Block Public Access on, SSE encryption on, S3 Versioning on (so @working overwrites keep prior bytes), and the library's IAM credentials allow only PutObject/GetObject/ListBucket on that one bucket — no DeleteObject
- Adding repo documents to search does not move the existing eval baseline: /api/rag/retrieve keeps returning library items only unless a caller opts into repodocs (source filter), and eval runs record that filter in their retrieval config
- Removing documents from GitHub (follow-up v2) is gated on a full-corpus verification across every repo — sha256 of each repo file equals library and S3 — not on the 5-repo sample of c36
- S3 is a mirror, not the backup: culture-library backup keeps working from the local blob store, now also covering working drafts and the outbox (any new table with sha256 is picked up automatically)

## Non-goals

- The library never writes back to repos or edits devague state — it is a mirror and reader, not a second source of truth for frames or plans

## Assumptions

- The corpus is small: about 84 MB across ~78 repos (52 MB .devague + 32 MB docs/{specs,plans,deliveries}), with near-identical lobes-cli-\* forks, so a single bucket well under 1 GB holds it and content-hash dedup absorbs the forks
- Bucket credentials follow the existing grant pattern — stored hidden with grant set and injected at exec time by deploy restart, so live.env stays secret-free
- A repo declares its domains and its connected projects in culture.yaml (e.g. domains: \[...\], connected: \[agentculture/culture-library\]) and culture-rules sends both with each push; the library stores them per project and the latest push wins

## Scope exploration

- `s1` — `culture_library/core/repodocs.py`: repodocs + `repodoc_versions` tables (repodocs.py:36-59) and `_KIND_PREFIXES` (:68-73) already implement versioned, commit-pinned storage of specs/plans/.devague records; push-only via POST /api/repodocs (`routes_repodocs.py`:52)
  - seeds: `c3`
- `s2` — `docs/specs/2026-10-23-library-culture-dev.md (lines 40-48)`: the library-culture-dev spec already promised every repo's docs/{specs,plans,deliveries} + .devague/{frames,plans,deliveries,reviews,questions} across ~75 repos, versioned, visibility-inherited, deduped across worktrees; delivered as repodocs (delivery doc, e11-e17, e42)
  - seeds: `c4`
- `s3` — `repodocs push contract (commit required)`: `repodoc_versions`.`commit_sha` is NOT NULL and validated by `_SHA_RE` (repodocs.py:62); 'during development' updates between commits have no slot; this repo's .gitignore:242-245 ignores .devague/reviews and .devague/questions, and 5 sibling repos (reachy-mini-cli 125 files, agentculture-cli, cultureflare, culture-guide, notion-agent) gitignore all of .devague
  - seeds: `c5`
- `s4` — `item/repodoc metadata (models.py, repodocs.py) + culture.yaml taxonomy`: no tags/collection/project/domain concept exists anywhere — Item fields models.py:29-39, repodocs has only repo; GET /api/repodocs filters only remote+path (`routes_repodocs.py`:76); across 124 culture.yaml agents entries only culture/culture.yaml carries a tags: list, and devague frames have no domain field
  - seeds: `c6`
- `s5` — `core/store.py blob storage + core/backup.py`: blobs are direct Path I/O at `files/<aa>/<sha256>` (store.py:182-188, 277-281, `read_bytes` :350) with no storage abstraction; backup.py copies local blobs by sha from every table with a sha256 column (`_referenced_blobs` :137); no S3/boto/minio code exists — only 'audio … moving to S3 in future' in the library-culture-dev spec:184,202
  - seeds: `c7`
- `s6` — `pyproject.toml + tests/test_zero_deps.py`: dependencies = \[\] (pyproject.toml:15); `test_zero_deps.py` asserts it and blocks server/eval libs from the CLI import; CLAUDE.md states core/ is stdlib-only and network-free — an S3 backend must follow the \[server\]/\[eval\] optional-extra pattern
  - seeds: `c8`
- `s7` — `POST /api/repodocs body + docs/design/`: PushBody.content is a str (`routes_repodocs.py`:28-35) and kind inference has no design prefix (repodocs.py:68-73), so PNG screenshots and binary assets cannot be pushed; docs/design/ here holds library-site.md, prototype.html and reference/ screenshots
  - seeds: `c9`
- `s8` — `core/search.py, core/indexer.py (issue #4)`: `search_fts` and embeddings key on items.rowid and every query joins items (search.py:30,132-140,191,218); repodocs are never indexed; issue #4 open and explicitly out of scope of the rag-evals spec (2026-10-25 spec:142)
  - seeds: `c10`
- `s9` — `core/indexer.py default_text_for + chunker`: ingest extracts json as raw decoded text (ingest.py:285-286) and `chunk_text` splits on blank lines then hard-splits at `CHUNK_MAX_CHARS` (search.py:82-108); pretty-printed JSON has no blank lines so chunks would be arbitrary and key-noisy
  - seeds: `c11`
- `s10` — `culture_library/mcp/tools.py`: no MCP tool touches /api/repodocs today (tools.py:129-236, `_handlers` :459-487); `EVAL_ROUTES` (:244-257) is the parity pattern to copy
  - seeds: `c12`
- `s11` — `core/feed.py`: feed rows require NOT NULL `item_id` referencing items and kind in KINDS (feed.py:22-31, 41-66); RepoDocs.push (repodocs.py:218) never appends an event — repodoc events need a schema change or a separate stream
  - seeds: `c13`
- `s12` — `culture_library/render/devague.py`: `detect_kind` only knows plan (tasks/targets) and frame (claims) (devague.py:77-85); delivery JSON (keys `plan_slug`, deviations, evidence, deltas, supersessions) falls to `raw_view`; `MAX_SCHEMA`=6 (:17); reviews/questions .md are served as plain text
  - seeds: `c14`
- `s13` — `web/static (app.js, home.js, state.js, reader.js)`: repodocs are merged into the day-grouped catalog (app.js:229-244) and the only grouping is a flat 'repo' chip (state.js:55); exported md is `YYYY-MM-DD-<slug>.md` while JSON is `.devague/<dir>/<slug>.json`, so the join key is the slug, not the filename
  - seeds: `c15`
- `s14` — `repodocs.py docstring + library-culture-dev spec non-goal`: repodocs.py:19-20 'nothing here (or anywhere in `culture_library`) runs git or devague', pinned by a grep/AST test per plan t8; spec lines 81,101 make the library a reader/mirror of committed state
  - seeds: `c16`
- `s15` — `devague on-disk contract (../devague/docs/spec-contract.md)`: devague owns frame/plan/delivery JSON with an integer `schema_version` that its loader checks and rejects when newer (frame.py `parse_schema_version`); no push/sync hook or domain field exists upstream, so the library must not mutate that state
  - seeds: `c17`
- `s16` — `~/git .devague survey (find -maxdepth 3)`: 264 .devague dirs found, only 78 in top-level repos — the rest are worktree checkouts (e.g. .worktrees.culture-nodes/tca-t\* each ~86 tracked files); library-culture-dev spec:48 already requires dedup by (remote, path, commit)
  - seeds: `c18`
- `s17` — `~/git size survey (du -shc)`: du -shc \*/.devague = 52M and \*/docs/{specs,plans,deliveries} = 32M; lobes-cli, -orin-deciders, -orin-nano and -stt-m4a have near-identical spec/plan counts (55/68, 53/61, 53/61, 52/59)
  - seeds: `c19`
- `s18` — `item visibility + key scopes (core/keys.py, uploaders.py)`: visibility is private|public per item, repodoc and version, defaulting private (store.py:44; repodocs re-push tightens never loosens, :269-273); keys have read/write/admin scopes plus a kinds allow-list (keys.py:45); spec:44 lists the 17 private repos
  - seeds: `c20`
- `s19` — `docs/operations/deploy.md + cli/_commands/deploy.py`: deploy.md:55,68,106,123 store keys with 'grant set NAME - --hidden' and inject via 'grant run --inject'; deploy restart already injects `GATEWAY_API_KEY` from grant (deviation d2)
  - seeds: `c21`
- `s20` — `challenge pass / adjacent-systems lens: .devague/plans + .devague/deliveries JSON (devague 0.24.2)`: devague records carry no commit or PR link: plan task keys are id/summary/origin/status/`acceptance_criteria`/deps/covers/instruction; eval-suites-and-radar delivery has 32 evidence/4 deviations/4 deltas with ~4 sha-like strings and 0 PR refs — the spec<->commit link must be derived from git by culture-rules, since the library never runs git (c16)
- `s21` — `challenge pass / lifecycle lens: core/repodocs.py push/_recompute`: push() only inserts versions and `_recompute` picks the newest default-branch version (repodocs.py:218-320); there is no delete path, so a deleted frame or abandoned draft stays 'current' forever
  - seeds: `c41`
- `s22` — `challenge pass / data-flow lens: repodocs current rule + decision c25`: current is defined as newest default-branch version (repodocs.py:10-13, `_recompute`); a mutable working draft (c25) is a second 'latest' — which one readers see by default is undecided
- `s23` — `challenge pass / failure-mode + observability lens: core/store.py write_blob, routes_status.py`: push writes the local blob inside the request (repodocs.py:244) with no async machinery for side effects; c7 promises catch-up when S3 is down but nothing records what is pending, so drift would be invisible
  - seeds: `c42`
- `s24` — `challenge pass / security + reversibility lens: decisions c22, c23, c25, c28`: the readable tree overwrites `<repo>/<path>@working` on every draft push (c7); without bucket versioning an overwrite is irreversible, and broad credentials could delete the private corpus
  - seeds: `c43`
- `s25` — `challenge pass / migration lens: core/models.py KINDS, core/feed.py, server/auth.py check()`: feed rows require kind in KINDS (feed.py:41-66); check(principal,'write',kind) enforces a key's kinds allow-list (auth.py:182-191); `infer_kind` has no design prefix (repodocs.py:68-73) — a new kind means the documented items CHECK rebuild
  - seeds: `c44`
- `s26` — `challenge pass / data-flow lens: ~/git corpus probe (4,604 files) + routes_repodocs.py`: binaries also live outside docs/design (innereye/docs/deliveries/assets/\*.png, 1.3 MB; climate-cli/docs/design/\*.png ~0.7 MB); the repodocs PushBody has no size limit while `routes_library` enforces `UPLOAD_LIMITS` (`routes_library.py`:236,287)
  - seeds: `c45`
- `s27` — `challenge pass / unstated-assumption lens: ~/git/*/.devague non-json/md probe`: found mjolnir-agent/.devague/helpdump.py and golden.sh, plus loose md in culture-rules/.devague; 'all internal .devague sources' may or may not mean arbitrary files
- `s28` — `challenge pass / operations lens: docs/evidence/t7-embedding-cost.md + core/indexer.py index_queue`: gemma2-embed measured 13-22 chunks/s on the lobes gateway, which also went `backend_unavailable` for minutes (t7 lines 20-29); 4,604 corpus files arriving in one migration would compete with live item indexing
  - seeds: `c46`
- `s29` — `challenge pass / adjacent-systems lens: routes_rag.py + eval baseline (sets 1-8, #7)`: the stored baseline (recall@5 0.89/0.78 books, 1.00 newspapers) assumes the item-only corpus; indexing ~4,600 repo docs into the same embeddings would change rankings for every set and break comparability with runs 1-22+
  - seeds: `c47`
- `s30` — `challenge pass / overlooked-actors lens: deploy/compose.yaml mcp service, core/keys.py private flag`: the MCP container has no server-side key; each client sends its own Bearer (compose.yaml:92), and private items are visible only to keys with private or to the uploader (library.py:26-28) — under all-private, most existing machine keys would see nothing
  - seeds: `c48`
- `s31` — `challenge pass / reversibility + data-loss lens: decision c30, park v2, success signal c36`: c36 samples 5 repos; once v2 removes files from GitHub, a document the mirror missed is lost with no second copy, so the removal needs its own complete check
  - seeds: `c49`
- `s32` — `challenge pass / recovery lens: core/backup.py _referenced_blobs`: backup collects blobs from every table with a sha256 column (backup.py:137) and excludes the feed; a drafts table with sha256 is covered for free, but only if it uses that column name
  - seeds: `c50`
- `s33` — `challenge pass / concurrency lens: core/store.py WAL + busy_timeout`: single SQLite writer with WAL and 30 s `busy_timeout` (store.py:195-198); culture-rules pushes serially per cycle, so no new locking is needed — residual risk only if pushes are parallelised during backfill

## Decisions

- Storage target is AWS S3 (operator, 2026-10-30)
- The bucket is a human-readable mirror tree keyed like `<repo>/<path>@<commit>` (plus a working draft), openable as-is with any S3 tool; the library's SQLite DB stays the index and source of truth for versions, visibility and search (operator, 2026-10-30)
- Domain scope is declared per repo in its culture.yaml (a domain:/tags: key, extending culture/culture.yaml's tags: precedent) and inherited by the repo's documents (operator, 2026-10-30)
- Work-in-progress keeps one mutable working-draft version per (repo, path), overwritten on each push; committed versions keep full commit-pinned history as today (operator, 2026-10-30)
- culture-rules is the collector and pusher: it reads repos and .devague dirs and pushes to the library API; the library stays push-only (operator, 2026-10-30)
- A project is a repo; projects can declare connections to other projects (e.g. culture-rules <-> culture-library), so a project view can show its connected projects' documents (operator, 2026-10-30)
- The AWS account is the operator's own and private; documents from private repos may be stored in it (operator, 2026-10-30)
- Mirror first, remove later: in this run repos keep committing specs/plans/.devague as today while the library + S3 become their complete private home; removing them from GitHub is a follow-up once the library is proven to hold everything (operator, 2026-10-30)
- Every migrated document is private in the library regardless of the repo's GitHub visibility (operator, 2026-10-30)
- Readers (site and MCP) see a document's working draft by default when it is newer than the committed current, badged 'working', with the committed version one click away (operator, 2026-10-30)
- culture-rules pushes every file under .devague/, not only json/md (operator, 2026-10-30)
- The spec-to-code link uses a `Spec: <slug>` commit trailer when present, else the files changed by the delivering merge, followed forward through renames on each push; culture-rules computes it from git and sends it with the push (operator, 2026-10-30)

## Hard questions

- With AWS S3 chosen, private-repo content (17 private repos) leaves spark for a public cloud: is that acceptable as long as the bucket is private and encrypted, or must private-repo documents stay local-only? (resolved: The AWS account is the operator's own and private; private-repo content in it is acceptable (operator, 2026-10-30))

## Open parks

- [unknown_nonblocking] Repo identity is the normalized git remote (repodocs.py:83-122); renames (assimilai -> citation-cli) or leaving GitHub for another remote would split a project's history unless culture-rules sends a stable project id or the library records aliases
- [unknown_nonblocking] Length of a culture-rules cycle is unknown here, so success signal c37 ('within 1 cycle') has no wall-clock bound until culture-rules states its schedule
- [follow_up] Remove specs/plans/.devague from GitHub (gitignore going forward, maybe history) once the library is proven complete — separate follow-up run

## Resolved vagueness

- [unknown_nonblocking] Whether anything pushes repo documents to live today: the library-culture-dev spec and issue #4 cite ~2,269 repodocs on live, but the culture-rules push rule was only filed as an issue and /api/status needs a key — unverified whether a pusher runs continuously — resolved: Nothing pushes today (operator, 2026-10-30)
