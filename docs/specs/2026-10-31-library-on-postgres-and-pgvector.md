# Library on Postgres and pgvector

> The library runs on PostgreSQL with pgvector on spark: documents, repo documents, full text and vectors live in one self-hosted database with a real vector index, API and MCP clients see no contract change, and retrieval quality is re-baselined at cutover

## Audience

- The operator who runs spark, the machine clients that call the library (culture-rules, Gwen, xteink, mesh agents over MCP), and people browsing library.culture.dev

## Before → After

- Before: Today one 419 MB SQLite file holds everything; full text is FTS5 bm25 and the ~70k 768-dimension vectors are scanned by brute force in Python (0.19.8 cache: items ~74 ms, items + repo docs ~730 ms median per query), with no storage interface: 68 functions take a raw sqlite3 connection
- After: Live runs on PostgreSQL 17 with pgvector: every table, the full text and the vectors are in Postgres, searched through SQL and an HNSW index, while clients, files on disk and the S3 mirror behave as before

## Why it matters

- The repo-doc corpus is meant to grow into one corpus describing the whole org; vector search in a real database is a solved problem, so the library moves before more effort goes into optimising exhaustive scans

## Requirements

- Documents, repo documents, full text, vectors and every other library table live in one self-hosted PostgreSQL database with the pgvector extension, run as a service in the spark compose stack
  - honesty: A fresh deploy brings up a healthy postgres service with the pgvector extension, and every library table exists in it
- Vectors are stored in a pgvector column with a model tag, with one HNSW index per embedding model (768 dimensions for embeddinggemma-2 today, room for 1024 for Qwen3-Embedding), and the in-process vector cache, `vector_log` table and its triggers are removed
  - honesty: Semantic retrieval queries use the pgvector operator on an HNSW index (EXPLAIN shows the index), and core/`vector_cache.py`, `vector_log` and its triggers no longer exist
- Full-text search is reimplemented on Postgres: title weighted above body, English stemming, prefix match on the last term, and highlighted snippets, for items and repo docs separately
  - honesty: Title matches outrank body-only matches, stemmed forms match (cats finds cat), the last query term matches as a prefix, and snippets mark hits with \[ \]
- The push transaction stays atomic in Postgres: a repo-doc version or draft, its feed event and its mirror outbox row commit together on one connection, as do tombstones
  - honesty: Every stored eval set (1-8) runs on Postgres with no sample reporting an unmatched reference chunk, so no set needs a re-anchor after cutover
- SQLite-only locking workarounds get Postgres equivalents: the unlink-after-commit of unreferenced blobs, and API-key `last_used` stamping that never waits on a writer
  - honesty: Concurrent unlinks never delete a blob a committed row references, and an API request is never delayed by more than 100 ms waiting to stamp `last_used`
- SQLite-specific SQL is ported in every core module: AUTOINCREMENT + lastrowid become identity + RETURNING, integer booleans become boolean, MAX(a,b) becomes GREATEST, `json_extract` on TEXT becomes jsonb, INSERT OR REPLACE/IGNORE become ON CONFLICT, the feed column named cursor is quoted or renamed, and ? placeholders change
  - honesty: No SQL in `culture_library`/ uses AUTOINCREMENT, lastrowid, `json_extract`, INSERT OR REPLACE/IGNORE, `sqlite_master`, PRAGMA or ? placeholders, checked by a grep test
- Schema changes run through a one-time, versioned migration runner instead of DDL and migrations re-applied on every open
  - honesty: Each schema change is applied exactly once and recorded with a version; reopening the store runs no DDL
- The live data moves once from SQLite to Postgres with ids, cursors and counts preserved: items, repo docs and their versions and drafts, feed, API keys, eval sets and runs, sources, projects, spec links, mirror outbox and the vectors
  - honesty: For every table, count(\*) and max(id) in Postgres equal the final SQLite snapshot, and identity sequences continue above the copied max ids
- Backup and restore are rebuilt on `pg_dump`/`pg_restore` plus the existing verified blob copy and manifest, with a restore drill before cutover
  - honesty: A backup taken from live restores into an empty Postgres and files tree with identical table counts and passing blob sha checks
- deploy restart starts and checks the Postgres service too, and the database password comes from grant like every other secret, never from live.env
  - honesty: live.env contains no database password, deploy restart refuses one if added, and restart brings postgres up healthy before library and mcp
- Tests run against Postgres in CI and locally, with an isolated database or schema per xdist worker, and the SQLite-coupled tests are rewritten
  - honesty: The CI test and publish jobs start a pgvector service, and the full suite runs with -n auto without workers sharing a database
- Runbooks and agent docs describe the Postgres stack: deploy.md (what runs, backups, restart, schema upgrades, rollback), backup.md, and CLAUDE.md and its sibling harness files
  - honesty: No runbook or agent doc still describes library.db, WAL, FTS5 or sqlite3 commands as the current stack
- The feed's cursor paging never skips an event under concurrent writers: feed appends are serialised (for example a transaction-level advisory lock) so cursor order equals commit order
  - honesty: A test with two concurrent writers and a reader paging by cursor sees every event exactly once, in cursor order
- Read-then-write code paths that relied on SQLite's single writer are made safe under Postgres concurrency: speclinks.record, mirror.enqueue, library.`_reuse_existing`, `eval_store`.`close_running_runs` and `purge_expired` use ON CONFLICT, row locks or advisory locks
  - honesty: Each named path has a test that runs two concurrent callers and ends with exactly the rows a serial run would produce
- The Postgres database uses the C collation, so every ORDER BY on text (titles, repo, path) returns the same order as SQLite's binary ordering
  - honesty: For each text-ordered API listing, the order on Postgres matches the SQLite order for the same live data
- Cutover copies stored text and vectors as they are: full-text bodies from `search_fts` and `repodoc_fts`, and every embedding blob, with no re-extraction and no re-embedding
  - honesty: After the copy, every item and repo doc has the same full-text body and the same vectors (byte for byte) as in SQLite, and the gateway receives no embedding calls during cutover
- Postgres runs with least privilege: no published port outside the compose network, an application role that owns only the library schema (not a superuser), and the role password from grant
  - honesty: From the host, nothing listens on 5432 except inside the compose network, and the library's role cannot create roles or databases
- /api/status reports the database: reachable or not, the applied schema version, and the vector index state, and the library refuses to serve writes if the schema version is ahead of the code
  - honesty: With Postgres stopped, /api/status still answers and says the database is unreachable, and the health check fails
- Backups run on a schedule on spark before cutover and after it, and the cutover is blocked until a scheduled backup has succeeded on Postgres
  - honesty: A systemd timer (or equivalent) runs the backup daily and its last success is visible; a probe on 2026-10-31 found no scheduled culture-library backup on spark
- Every stored vector records its model id (the real model, e.g. `google/embeddinggemma-2`, not the gateway role name) and its embedding strategy: the document and query prompt names, the chunker and its `max_chars`, the dimension and the normalisation. Each per-model HNSW index, every search and every eval run selects vectors by (model id, strategy), so vectors from two strategies never mix
  - honesty: A query never scores vectors from a different model id or strategy than the one it embedded with, shown by a test that stores two strategies side by side, and the cutover fills model id and strategy for all existing vectors from the gateway's /capabilities (embeddinggemma-2, 768, l2, Retrieval-document/Retrieval-query, `max_chars` 1000)

## Honesty conditions

- After cutover, the library container's database settings point at Postgres and no library.db is opened by the running service
- tests/contract and the MCP parity tests pass unchanged against the Postgres build, and /api/status returns the same top-level keys and nested shape
- No sibling repo or deploy script opens library.db; a grep of culture-rules, Gwen and xteink deploys for library.db finds nothing
- After cutover every item's sha256 still resolves to data/files/<aa>/<sha256>, and the mirror outbox drains new rows to the same bucket keys
- No code path sends document text to any host other than the configured lobes gateway; vectors stored in Postgres are the gateway's 768-dimension outputs
- Within one push, a failure after the version insert leaves no version, feed event or outbox row behind (a forced error in `on_write` rolls all three back)
- Each named client keeps working through cutover without a code or configuration change on its side
- The before-state figures match measurements taken on live 0.19.8 on 2026-10-31
- The operator decided on 2026-10-31 to migrate before further SQLite optimisation
- The running library has no code path that opens SQLite for library data, and all reads and writes go to Postgres
- The found@5 means are computed from complete server-side runs on Postgres for all eight sets, rerank on and off
- Latency is measured on live spark, not a dev machine, with the timing script recorded alongside the numbers
- The count comparison covers every table the SQLite store had, and verify-mirror covers every top-level repo
- The CI jobs run the same test selection as today, with no skip markers added for Postgres
- The drill is run on spark against a real live backup, not a fixture
- The freeze start and end times come from the culture-rules rule pause and resume, recorded in the delivery summary
- The cutover runbook names the point of no return (resuming writes) and the go/no-go checks that must pass before it

## Success signals

- After cutover, eval sets 1-8 (k=5, hybrid) score within 0.05 of today's found@5 means: books >= 0.84 with rerank and >= 0.74 without, newspapers >= 0.95 with rerank
  - instruction: Run the server-side judge-free runs for sets 1-8, rerank on and off, on Postgres right after cutover and compare with runs 56-71
- On live after cutover, rerank-off retrieval p95 is < 300 ms for items only and < 500 ms for items plus repo docs (k=5), measured over 40 queries each
  - instruction: Re-run scratchpad modes.py-style timing against /api/rag/retrieve for both corpora sets
- Cutover loses nothing: every table's row count and max id match the SQLite source exactly, and a full-corpus verify-mirror run reports 0 missing and 0 mismatched
  - instruction: Compare per-table count(\*) and max(id) between the final SQLite snapshot and Postgres, then run scripts/verify-mirror.py --all
- The full test suite passes against Postgres in CI (the test and publish jobs) with no test skipped for lack of a database
- A restore drill rebuilds an empty Postgres and files tree from a backup with every table count identical, before the live cutover
- The write freeze at cutover lasts under 30 minutes, from pausing culture-rules' push to resuming it

## Scope / boundaries

- API and MCP clients see no contract change: the HTTP routes, MCP tools and /api/status shape stay as they are; only the storage under them moves
- Sibling clients (culture-rules, Gwen, xteink) keep using the HTTP API on 127.0.0.1:8796 with their API keys; nothing outside the library reads the database
- Item and repo-doc file bytes stay on the filesystem under `data/files/<aa>/<sha256>`, and the private S3 mirror (outbox, worker, bucket) keeps working unchanged
- Embedding and reranking stay on the local lobes gateway (gemma2-embed, reranker); no document text goes to a third-party service, so MongoDB-style automated embedding via a cloud API is out
- Rollback to SQLite is only offered before writes resume on Postgres; once clients write to Postgres, problems are fixed forward, because nothing copies Postgres writes back to SQLite

## Non-goals

- The xteink migration keeps reading its source database with sqlite3; only its destination lookup moves to the new store

## Assumptions

- Search scores and rankings change at cutover; stored eval sets stay valid because they are anchored by chunk text sha, and the baseline (sets 1-8, rerank on and off) is re-run on Postgres as the new comparison point

## Scope exploration

- `s1` — `docs/api/*.md, tests/mcp/test_repodoc_tools.py:52-57, tests/mcp/test_eval_tools.py:76-81, server/routes_status.py:124-155`: The written API contracts (repodocs.md, eval.md) never mention SQLite; MCP parity tests compare tool names to the HTTP routes; /api/status exposes counts, index, retrieval and mirror blocks but no db path or WAL setting. `routes_status.py` runs raw COUNT/MAX/COALESCE SQL that must keep its output shape
  - seeds: `c2`
- `s2` — `CLAUDE.md machine-clients note; migrate/xteink.py:76,92`: Only the library reads library.db; siblings call the HTTP API with scoped keys. The one other sqlite3 reader is the xteink import, which reads xteink's own old database
  - seeds: `c3`
- `s3` — `core/store.py:187,277-316; storage/s3.py; core/mirror.py`: The DB holds only metadata (sha256, size, format); bytes are written atomically to data/files before the DB insert. The S3 mirror is the only existing storage abstraction (S3Backend). Postgres replaces the metadata store only; the data volume stays
  - seeds: `c4`
- `s4` — `culture_library/gateway.py:33; repodoc privacy decision c31; MongoDB automated-embedding docs`: Embeddings come from the gateway with Retrieval-document/Retrieval-query prompts. All repo docs are private. Postgres only stores and indexes vectors the library already computes
  - seeds: `c5`
- `s5` — `migrate/xteink.py:76,92 and _dest_lookup`: xteink.py opens the old xteink library.db read-only with sqlite3 (a legacy source format) and looks up the destination by the library.db path, which must ask the Postgres store instead
  - seeds: `c6`
- `s6` — `deploy/compose.yaml:19,79,121,139-140; deploy/Dockerfile:150,157`: Compose has library, mcp and cloudflared and one data volume; a postgres service (pgvector image), its own volume and a `pg_isready` healthcheck are new. library needs `depends_on` `service_healthy`. The Dockerfile installs .\[server,s3\]; a Postgres driver extra joins it
  - seeds: `c7`
- `s7` — `core/search.py:59-80,121; core/vector_cache.py; live DB`: Vectors are float32 BLOBs (`pack_vec`) with a model column, scored by brute force in Python with the 0.19.8 cache. The live dimension is 768 (gemma2-embed), measured on spark; 7,855 item and 61,932 repo-doc chunks. Search scores mixed-model rows together today
  - seeds: `c8`
- `s8` — `core/search.py:56-72,284-290,343-363,555,569`: FTS5 tables `search_fts` and `repodoc_fts` use porter unicode61, bm25 weights 5.0 (title) and 1.0 (body), snippet() with \[ \] markers and 12 tokens, and quoted AND terms with a \* prefix on the last. Postgres has tsvector setweight + `ts_rank`/`ts_headline`, which do not reproduce bm25
  - seeds: `c9`
- `s9` — `core/search.py:523-525; core/eval_store.py; delivery 2026-10-30 c47/d12; tests/core/test_repodoc_index.py::test_item_ranking_unchanged_by_indexed_repodocs`: Eval samples anchor on sha256 of the chunk text, so sets survive; runs store retrieved shas and ID metrics, never raw bm25 scores. c47 (baseline must not move) cannot hold through an FTS engine change and must be re-argued. Several tests pin exact scores
  - seeds: `c10`
- `s10` — `core/repodocs.py:384-418; server/routes_repodocs.py:258,327`: RepoDocs.push runs under one store.connect() and calls `on_write`(conn, result) inside it (d6), where the route appends the feed event and enqueues the mirror. Cross-module atomicity of version + feed + outbox is the invariant to keep
  - seeds: `c11`
- `s11` — `core/library.py:~548-570; core/keys.py:236-250,263`: library.`_unlink_unreferenced` relies on SQLite's single writer lock; keys.`_stamp_last_used` opens a raw sqlite3 connection with a 100 ms `busy_timeout` and string-matches 'no such table'. Both need advisory locks or `lock_timeout`/SKIP LOCKED style equivalents
  - seeds: `c12`
- `s12` — `core/repodocs.py, feed.py, keys.py, eval_store.py, library.py, mirror.py, uploaders.py, projects.py, speclinks.py, sources.py`: AUTOINCREMENT in 6 modules with cur.lastrowid; bare INTEGER booleans (`on_default_branch`, private, running); scalar MAX() in repodocs; `json_extract` in library.py:71,539; INSERT OR REPLACE in `eval_store`:312; feed.cursor is a Postgres reserved word; uploaders.`visible_sql` splices ? fragments into other modules' SQL; library.`_sha_tables` introspects `sqlite_master`
  - seeds: `c13`
- `s13` — `core/store.py:165-174,201-247; core/mirror.py:48-73; triggers in indexer.py:115 and vector_cache.py:51`: `register_schema` fragments are split on ';' and run on every open; `register_migration` callbacks use PRAGMA `table_info`, `sqlite_master` and a CHECK-rebuild of items (`sqlite_sequence` preserved); mirror widens its CHECK by editing `sqlite_master` SQL. None of this ports
  - seeds: `c14`
- `s14` — `live spark database, 2026-10-31 (read-only counts)`: Live holds 110 items (max id 113), 3,811 repo docs, 2,895 versions, 990 drafts, 7,855 + 61,932 vectors, 3,896 outbox rows, eval runs 1-71 and API keys 1-19, in a 419 MB library.db. `sqlite_sequence` is kept so purged ids are never reused
  - seeds: `c15`
- `s15` — `core/backup.py:92-99,175,248-265,324-343; docs/operations/backup.md`: Backups use the SQLite online backup API into `snapshots/<stamp>/library.db` plus a sha-verified shared files tree and manifest.json; restore needs an empty target and re-verifies. The blob half stays; the database half is replaced
  - seeds: `c16`
- `s16` — `cli/_commands/deploy.py:43-67,162-173,228-257; CLAUDE.md d2`: `SECRET_SETTINGS` maps env vars to grant secrets; `_refuse_secrets` rejects them in live.env; plan() runs up -d --no-deps library mcp, so a postgres service would never be started by restart. A PG password fits as a new grant secret; the DSN is built in-app so live.env never holds it
  - seeds: `c17`
- `s17` — `tests/*/conftest.py (6 Store(tmp_path) fixtures); 19 files importing sqlite3; .github/workflows/tests.yml:32, publish.yml:29; culture.yaml gate`: 1,695 tests. Stores are built per area with Store(`tmp_path`/'data'); 19 test files use sqlite3 directly (83 sites), 10 read `sqlite_master`, 2 monkeypatch store.`_open`. CI test jobs run pytest -n auto on ubuntu-latest, where a pgvector service container fits; the culture.yaml gate has no service
  - seeds: `c18`
- `s18` — `docs/operations/deploy.md:9-30,170,177,447-575; docs/operations/backup.md:3-59; CLAUDE.md`: deploy.md's What runs, Backups, Restart, Upgrading across a schema change (sqlite3 and `sqlite_sequence` commands) and Rollback sections, every section of backup.md, and CLAUDE.md's 'SQLite WAL store' and items kind-CHECK notes all assume SQLite
  - seeds: `c19`
- `s19` — `tests/test_zero_deps.py:43,72,88; pyproject.toml:16; core/store.py:181-275`: Base dependencies must stay empty (asserted three times), so a Postgres driver can only live in an extra. No storage seam exists today: 68 functions take sqlite3.Connection. Keeping SQLite means building and testing two backends; dropping it means core/ needs the driver at runtime
  - seeds: `q1` (question, resolved)
- `s20` — `live spark stack and backups (2026-10-31)`: Rollback today is a verified snapshot plus a tagged image (culture-library:0.19.7). culture-rules pushes every 15 minutes, so a one-shot cutover needs a short write freeze or a re-push after; a side-by-side period doubles the write paths
  - seeds: `q2` (question, resolved)
- `s21` — `core/search.py:343-363 bm25(…, 5.0, 1.0); Postgres ts_rank`: Today's ranking is FTS5 bm25 with title weight 5. Postgres core full text ranks by `ts_rank`/`ts_rank_cd` without IDF, so exact rankings move either way; a BM25 extension would stay closer but adds a non-core dependency to the database image
  - seeds: `q3` (question, resolved)
- `s22` — `deploy/Dockerfile (python:3.12-slim-bookworm); core code is synchronous`: No Postgres image or driver is referenced anywhere yet; the library's core and routes are synchronous, which matches psycopg 3's sync API
  - seeds: `q4` (question, resolved)
- `s23` — `challenge pass / concurrency lens: core/feed.py (cursor INTEGER PRIMARY KEY AUTOINCREMENT), server/routes_feed.py`: SQLite's single writer makes cursor order equal commit order. A Postgres sequence hands out ids at insert time, so a transaction that commits later with a smaller cursor is invisible to a reader that already paged past it; feed subscribers would silently miss events
  - seeds: `c34`
- `s24` — `challenge pass / concurrency lens: core/speclinks.py:118-146, core/mirror.py enqueue, core/library.py _reuse_existing, core/eval_store.py`: Under BEGIN IMMEDIATE these SELECT-then-INSERT sequences were serialised; under Postgres READ COMMITTED two writers can both see no row and both insert, or one violates a UNIQUE key (`mirror_outbox`.key)
  - seeds: `c35`
- `s25` — `challenge pass / unstated-assumption lens: probe on the 0.19.8 live backup; core/projects.py:117 ORDER BY repo`: SQLite compares TEXT bytewise. A probe on the live backup found 35 item titles that sort differently under a case-insensitive order, so a default `en_US` collation would reorder API listings. No titles or repo paths are non-ASCII today
  - seeds: `c36`
- `s26` — `challenge pass / failure-mode lens: core/search.py set_body/set_embeddings; gateway timings`: The FTS tables hold the extracted text and the embeddings hold 69,787 vectors. Re-extracting or re-embedding at cutover would take hours through the gateway and could change chunk text, breaking eval anchors; copying keeps both identical
  - seeds: `c37`
- `s27` — `challenge pass / reversibility lens: decision q2 (one-shot), core/backup.py`: The SQLite snapshot plus the old image restores the pre-cutover state only; any item, repo doc, feed event or key created on Postgres after resuming would be lost by that rollback
  - seeds: `c38`
- `s28` — `challenge pass / security lens: deploy/compose.yaml networks; cli/_commands/deploy.py SECRET_SETTINGS`: Compose uses the default bridge with no published DB port today (there is no DB service). The private repo docs make the database as sensitive as the bucket
  - seeds: `c39`
- `s29` — `challenge pass / observability lens: server/routes_status.py:124-155; core/store.py`: Status today counts rows and reports index and mirror blocks; with a network database, an unreachable DB or a half-applied migration becomes a new failure mode that nothing currently surfaces
  - seeds: `c40`
- `s30` — `challenge pass / recovery lens: systemctl --user list-timers and crontab on spark; docs/operations/backup.md Schedule section`: No culture-library backup timer or cron entry is installed on spark today; backups so far were taken by hand before upgrades. A network database with more writers makes an unscheduled backup a real recovery gap
  - seeds: `c41`
- `s31` — `challenge pass / adjacent-systems lens: culture-rules culture_rules/node/actions/repodocs.py:131-146`: repodocs.sync is state-file based and idempotent: a failed run resumes next time, so pausing it loses nothing. The other culture-rules rules that write items to the library were not read in this pass
- `s32` — `challenge pass / probes: live backup copy`: All 20 non-FTS tables store values matching their declared types (no affinity surprises for the copy); feed has 4,041 events with contiguous cursors; the mcp container reaches the library only over HTTP (compose `CULTURE_LIBRARY_URL`)
- `s33` — `challenge pass / not examined`: Not examined in this pass: the server routes' raw SQL beyond `routes_status.py`, `eval_radar.py`, the site's JavaScript (storage-neutral by design), and Postgres tuning (`shared_buffers`, `maintenance_work_mem` for the HNSW build; parked as v2)
- `s34` — `lobes gateway /capabilities and /v1/embeddings probe (2026-10-31)`: The library's gemma2-embed role IS google/embeddinggemma-2: 768 dimensions, L2-normalised, with Matryoshka sizes 128/256/512/768. The gateway's other embedding role, 'embedder' (and embedder-spark2), is Qwen/Qwen3-Embedding-0.6B at 1024 dimensions. Both fit pgvector's HNSW limit of 2,000; supporting more than one model means an untyped vector column with a per-model partial HNSW index on a cast (vec::vector(N)) WHERE model = ..., instead of a single vector(768) column. Bears on parked v3 and #7
- `s35` — `live library DB and /api/status retrieval block (2026-10-31)`: All 7,855 item and 61,980 repo-doc vectors carry the label 'gemma2-embed+Retrieval-document' (768 dims). 'gemma2-embed' is a gateway role, so a role repointed to another model would write indistinguishable labels; the model id, chunker settings and dimension are only in the status config, never per vector
  - seeds: `c42`

## Decisions

- Postgres-only: the library keeps no SQLite backend; the driver lives in an extra the server image installs and tests run on a Postgres database per test worker
- Cutover is one-shot with a write freeze: pause culture-rules' push, back up, copy with ids preserved, verify counts, start on Postgres, resume; rollback is the SQLite snapshot plus the old image
- Full text uses Postgres core features (weighted tsvector, English stemming, `ts_rank`, `ts_headline`); a BM25 extension is considered only if recall drops below the success signal
- The stack is PostgreSQL 17 on the pgvector/pgvector:pg17 image, with psycopg 3's synchronous API

## Hard questions

- Does core/ keep SQLite as a second backend behind a storage interface (for tests, offline use and the zero-dependency rule), or does the library become Postgres-only? (resolved: Operator 2026-10-31: Postgres-only. The driver lives in an extra the server image installs; tests use a Postgres database per test worker; no SQLite backend is kept)
- Full-text ranking: accept Postgres `ts_rank` (simpler, rankings move), or add a BM25 extension so ranking stays close to today's bm25? (resolved: Operator 2026-10-31: core Postgres full text (weighted tsvector, English stemming, `ts_rank`, `ts_headline`) first; re-baseline evals; add a BM25 extension later only if recall drops)
- Cutover style: a one-shot migration during a short write freeze with the SQLite backup and image as rollback, or a period where both stores run side by side? (resolved: Operator 2026-10-31: one-shot cutover with a write freeze (pause culture-rules' push), backup, copy with ids preserved, verify counts, start on Postgres, resume; rollback is the SQLite snapshot plus the old image)
- Postgres version and image: which major version and pgvector build, and does the library connect with psycopg 3 (the code is synchronous)? (resolved: Operator 2026-10-31: PostgreSQL 17 on the pgvector/pgvector:pg17 image; psycopg 3, synchronous API)

## Open parks

- [unknown_nonblocking] How well HNSW recall holds under our filters (private/public, owner, kind, item ids) at low selectivity, since pgvector filters after the index scan; measure with the eval harness after the port
- [unknown_nonblocking] Whether the spark stack has the RAM and disk headroom for Postgres plus an HNSW index over about 70k (and growing) 768-dimension vectors next to the current services
- [unknown_nonblocking] The embedding column is fixed at vector(768); the embedding-model comparison (#7) may need a model with another dimension, which would need a second column or table and its own index
- [unknown_nonblocking] Whether tests and the culture.yaml gate run where no Postgres is available (mesh tooling, a fresh checkout), or need a documented way to start one
