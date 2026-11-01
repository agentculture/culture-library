# Delivery Summary — Library on Postgres and pgvector

plan: `library-on-postgres-and-pgvector` · run: `complete` · date: `2026-10-31`
baseline: `devague summary skeleton`

Live has run PostgreSQL 17 with pgvector since 2026-11-01 (cutover at 18:04–18:40 UTC; 0.20.3
since 19:55 UTC). Every plan task was delivered, through four PRs (#49–#52). Two success
signals failed and are reported as failed: the write freeze ran past 30 minutes (c29), and
no backup was scheduled (c41). The full-corpus mirror check (c26) did not read 0/0 at the
cutover, because 8 files were edited during the freeze; it reads 0/0 on the rerun.

## Intent

> The library runs on PostgreSQL with pgvector on spark: documents, repo documents, full text and vectors live in one self-hosted database with a real vector index, API and MCP clients see no contract change, and retrieval quality is re-baselined at cutover

After: Live runs on PostgreSQL 17 with pgvector: every table, the full text and the vectors are in Postgres, searched through SQL and an HNSW index, while clients, files on disk and the S3 mirror behave as before

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Postgres store foundation: connection handling and a versioned migration runner
- `t2` — Test infrastructure on Postgres: one database per test worker, in CI and locally
- `t3` — Compose and deploy: a least-privilege pgvector service started by deploy restart
- `t4` — Initial Postgres schema: every library table, typed for Postgres
- `t5` — Port core/library.py, uploaders.py and sources.py to Postgres
- `t6` — Port core/feed.py and server/`routes_feed.py` with commit-ordered cursors
- `t7` — Port core/keys.py with non-blocking `last_used` stamping
- `t8` — Port core/mirror.py, server/`routes_mirror.py` and the mirror CLI
- `t9` — Port core/`eval_store.py`, `eval_reanchor.py`, `eval_radar.py` and the eval routes
- `t10` — Port the remaining server routes and add the database block to /api/status
- `t11` — Port core/repodocs.py, projects.py, speclinks.py and server/`routes_repodocs.py` to Postgres
- `t12` — Port core/search.py to Postgres full text and pgvector, and remove the vector cache
- `t13` — Port core/indexer.py: queues, dirty marks as Postgres triggers, and model id plus strategy on every vector
- `t14` — One-shot SQLite-to-Postgres copy command with a verification report
- `t15` — Backup, restore and schedule on `pg_dump`
- `t16` — Remove the remaining SQLite from the package and pass the full suite on Postgres
- `t17` — Runbooks and agent docs for the Postgres stack, with the cutover runbook
- `t18` — Live: restore drill and pre-cutover baseline on spark (operator present)
- `t19` — Live: one-shot cutover to Postgres (operator present)
- `t20` — Live: re-baseline evals and latency on Postgres (operator present)

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | psycopg 3 store with psycopg-pool, versioned migration runner under an advisory lock (`core/migrations/`); PR #49 (`023c7fa`) |
| `t2` | delivered | per-worker database cloned from a migrated template, per-test truncate and seed restore, pgvector service in CI (d1); PR #49 |
| `t3` | delivered | compose `postgres` service (pg17 image, least-privilege role, C collation, `shm_size` 1 GB), `deploy restart` starts it first (d2); PR #49 |
| `t4` | delivered | `0001_initial.sql`, every table typed for Postgres, strategy key module (d4); PR #49. Later migrations: 0002 (#49), 0003 `vec_hash` (#50), 0004 `simple_tsv` (#51) |
| `t5` | delivered | library, uploaders and sources on Postgres, `lock_blob` (d6); PR #49 |
| `t6` | delivered | feed with an advisory-lock append, so cursor order is commit order; PR #49 |
| `t7` | delivered | keys with non-blocking `last_used` and `FOR UPDATE` rotation (d5); PR #49 |
| `t8` | delivered | mirror outbox, worker and CLI on Postgres; PR #49 |
| `t9` | delivered | eval store, re-anchor, radar and routes; PR #49 |
| `t10` | delivered | remaining routes, `/api/status` database block, `/api/health` 503 without the database (d7); PR #49 |
| `t11` | delivered | repodocs, projects, speclinks and the repodocs routes; PR #49 |
| `t12` | delivered | Postgres full text and pgvector HNSW search, vector cache removed (d8, d13); amended on live by #50 (d18, d19), #51 (d20–d22) and #52 (d23) |
| `t13` | delivered | indexer queues, dirty marks as plpgsql triggers, model id and strategy on every vector (d9); PR #49 |
| `t14` | delivered | `migrate sqlite-to-postgres` with a verification report (d12); made to accept the real 0.19.8 file in #50 (d15) |
| `t15` | delivered | backup, verify, restore and schedule on `pg_dump`; restore as the app role fixed in #50 (d16, d17, d19). The schedule *command* ships; no timer is installed (see c41) |
| `t16` | delivered | no SQLite left in the package (AST guard), full suite on Postgres (d10, d11, d13); PR #49 |
| `t17` | delivered | deploy, backup, testing and cutover runbooks, agent docs (d14); PR #49; CLAUDE.md and the three harness files updated for live Postgres in #52 |
| `t18` | delivered | restore drill on a scratch Postgres from live data; it found three defects, fixed in #50 (0.20.1) before the cutover (d15–d19). Pre-cutover baseline measured on live 0.19.8 |
| `t19` | delivered | cutover on 2026-11-01 on 0.20.1: final SQLite backup 18:04, copy 42.6 s with every table equal, serving at 18:06:55, go/no-go passed, writers resumed 18:40. Freeze over target (c29) |
| `t20` | delivered | re-baseline on live: the first pass (0.20.1) missed newspapers with rerank (0.917), fixed in #51 (0.20.2); the rerun meets every target; r1 and r2 resolved; ef per corpus in #52 (0.20.3) |

## Mid-work Decisions

- `d1` — t2 also added the pgvector service and test DSN to the visual and ragas CI jobs, and added psycopg\[binary\] to the dev dependency group — both jobs run pytest and load the root conftest, so without a database they would fail at session start; no driver was installed for the fixture before t1 adds the pg extra
- `d2` — deploy restart starts postgres in its own step (up -d --no-deps --wait postgres) before the library and mcp step, instead of dropping --no-deps or adding postgres to SERVICES; mcp gets no database credentials — --wait blocks until postgres is healthy so the order is explicit in dry-run, and mcp reaches the library only over HTTP, so least privilege leaves it without database credentials
- `d3` — t1 also edited core/`__init__.py` (dropped side-effect imports of unported modules and the SCHEMA/`register_schema` exports), put the items table in `0001_initial`.sql for t4 to extend, fixed a temp-name collision in Store.`write_blob`, and deleted tests/core/`test_store.py` and `test_store_migration.py` (moving two kind tests to `test_todo_kind.py`) — without the `__init__` edit no core module imports at all; t4's criterion names `0001_initial`.sql as the single initial schema; the concurrent-add test exposed the `write_blob` race; the deleted tests covered the SQLite store and the kind-CHECK rebuild that no longer exist
- `d4` — t4 added `culture_library`/core/`embedding_strategy.py` (the canonical strategy key, model id and HNSW index name and SQL), a CHECK that each vector's dimension equals its strategy's, and named the chunker paragraph-pack — one producer of the strategy key keeps index predicates and queries matching exactly; the CHECK stops a vector of the wrong size reaching a typed HNSW index
- `d5` — keys.`_live` selects the old key row FOR UPDATE, so a rotate or confirm cannot race another one — under Postgres concurrency two rotations of one key could both read it live and both mint replacements; SQLite's single writer prevented that
- `d6` — t5's library tests were verified only against scratch stand-ins for feed, indexer, search and repodocs, because library.py imports indexer and search (plan waves 3-4); t5 also dropped the `item_uploaders` backfill (t14 copies the rows) and added library.`lock_blob`, which every blob writer must take — the plan put t5 in wave 2, but library.py cannot import until t12/t13 port search and indexer; the backfill only served SQLite databases that predate `item_uploaders`
- `d7` — t10 left /api/health (server/app.py, outside its files) unchanged; the main agent added the database check there: SELECT 1 through the store, 503 with status error when the database is unreachable, body unchanged when it answers. t10 also ported core/sources.py, which t5 owns; t5's version was kept at merge — criterion 1 needs /api/health to fail with the database stopped, and app.py belonged to no wave-2 task; the duplicate sources.py port conflicted with t5's
- `d8` — t12: hybrid passages fuse over a pool (per corpus the nearest max(2k,50) chunks plus the nearest k chunks of each of the top max(2k,50) full-text matches) instead of every chunk; zero vectors are refused at write; `pack_vec`, `unpack_vec` and `cosine_scorer` are removed; `set_repodoc_text` indexes the title plus the words of the path; 22 indexer tests are parked with skip markers until t13 — scanning every chunk defeats the HNSW index; pgvector's cosine HNSW index cannot find zero vectors; the helpers only served the removed Python scorer; Postgres keeps a path as one token; the indexer is ported in t13
- `d9` — t13 edited server/`routes_search.py` (removed the label-based `requeue_stale_model` call; the indexer requeues stale (model, strategy) itself), padded fake embedders in waiting-list tests to 768 dimensions, moved `test_review_fixes` test 8 to the (model id, strategy) API, and fixed r5 by adding a vector of only the punctuation-containing tokens split on \[\[:punct:\]\] instead of concatenating split text — a label passed as a model id would reopen the whole corpus each start; t12 requires 768-d vectors; the label column is gone; full concatenation changed every tsvector and `ts_delete` broke underscore phrases
- `d10` — t16 (SQLite-free sweep) starts in parallel with t14 instead of after it, limited to files outside `culture_library`/migrate/ and tests/migrate/; its full-suite criterion is checked after t14 merges — the 31 remaining failures outside migrate/ (old 2-d fakes, leftover sqlite3 tests, routes using a fixed vector pair, the pg extra) touch no t14 file, so running them together shortens the critical path without file overlap
- `d11` — t16 is built by an opus subagent instead of the main agent named in the split plan; the main agent keeps the final full-suite verification and merge — the main agent's context is long after waves 0-4; the sweep is a bounded, file-listed task with a grep test and a known failure list, so delegating it keeps the integration check with the main agent
- `d12` — t14 added --drop-stale-vectors and --verify-only, sets identities to max(max id, `sqlite_sequence`), drops the HNSW indexes for the copy and rebuilds them serially (`maintenance_work_mem` 1GB, no parallel workers), lets the xteink dry run open Store(dest) (applies migrations, creates dest/files), and imports two private store helpers — stale labels must be skippable rather than block cutover; SQLite AUTOINCREMENT never reuses ids, so neither may Postgres; inserting 70k distinct vectors with the index in place took over 215 s, and a parallel build exhausted Docker's 64 MB shared memory
- `d13` — Main-agent integration after t14 and t16: (1) the per-test reset in tests/conftest.py now drops any public index a test created, so an HNSW index for a new (model, strategy) pair no longer leaks into later tests; (2) search.`_ann` completes a group of exactly tied chunks the pool LIMIT cut (fetch limit+1; on a tie at the edge, score that distance exactly), so ties break on (owner, `chunk_no`) and every k is a prefix of a larger k — both caused intermittent full-suite failures; the tie case is real on live, where many repo docs are byte-identical (.devague/current), and SQLite's exhaustive scan always ordered ties the same way
- `d14` — t17 documents the live backup timer and the cutover copy as 'grant run ... docker compose run --rm --no-deps -v `<dir>`:/backup library culture-library ...' instead of 'docker exec', and trimmed QWEN.md to stay under its 145-line cap — compose mounts no backup directory into the library container, so a docker exec backup would have nowhere to write; `test_harness_files_stay_in_step` enforces the QWEN.md cap
- `d15` — Ship 0.20.1 before the cutover: the copy knows 0.19.8's `vector_log` (the vector cache's change log, derived state Postgres has no use for) and skips it, naming it and its row count in the report; the source fixture gains `vector_log` and its triggers so the tests use the real 0.19.8 schema. t19 runs on 0.20.1. — The t18 drill's copy of the real live library.db refused it (unknown `vector_log`); the cutover cannot run on 0.20.0.
- `d16` — 0.20.1 also changes backup restore: it leaves the dump's EXTENSION and COMMENT ON EXTENSION entries out (via a filtered `pg_restore` --use-list), requires the target to already have the vector extension (created by the superuser, as the init script does), and refuses up front with that remediation when it is missing. backup.md says how to make the restore target. — The t18 drill's restore as the app role failed on the dump's extension entries; a live restore would fail the same way.
- `d17` — Correction to d16's mechanism: restore passes `pg_restore` --no-comments instead of a filtered --use-list. The rest of d16 stands (vector extension required up front, refused with the superuser remediation, backup.md updated). — Smaller than the TOC filter d16 named: the dump's only COMMENT is the extension's, and its CREATE EXTENSION IF NOT EXISTS is a no-op for a non-owner once the extension exists; a --use-list file would also not reach the test suite's docker-exec `pg_restore` wrapper.
- `d18` — 0.20.1 also narrows d13's tie completion to chunks whose text equals a tied chunk's text before scoring them, instead of rescoring the whole corpus. Identical vectors come from identical text under one (model, strategy); a different-text chunk at exactly the same float distance is no longer pulled in, which the code comment states. — Measured on the t18 scratch copy of live data: combined search p95 546 ms against the < 500 ms success signal; the slow statement (about 260 ms, 5 of 40 queries) is d13's tie completion, which scores all 62,591 repo-doc vectors whenever the pool edge falls in a group of identical vectors (the same file pushed from many repos). Narrowing by chunk text first costs about 11 ms.
- `d19` — Corrects d18 and d17. Tie completion finds identical vectors by `vec_hash`, an indexed md5 of each vector added by migration 0003 (schema version 3), not by chunk text, so it is exact again and still scores only the tie group. Restore leaves out only the dump's COMMENT ON EXTENSION vector entry, via a filtered `pg_restore` --use-list (d16's original mechanism), and keeps every other comment. Restore also refuses a target whose vector extension is outside public, or whose collation is not C. — /code-review medium of PR #50: text equality is only a proxy for the tie condition (the restored original test, distinct texts with identical vectors, fails under it), and --no-comments would silently drop every future table or column comment on restore.
- `d20` — Change s21's third case: a trailing stopword the english config drops is no longer required to match as a simple prefix; the other words must still match, and ranking is unchanged. Ship as 0.20.2, deploy with deploy restart, rerun t20. — t20 on live: newspapers with rerank average 0.917 ID recall@5 against the >= 0.95 target (set 6: 0.75, was 1.0 on 0.19.8; stable over 3 runs). Question 103 ('...JetPack 7.0 ship with?') ends in a stopword; s21 makes that last word match only as a prefix of a stored lexeme or title word, and the english tsv never stores the stopword itself, so item 81 (which has every content word) is filtered out of the full-text owners. FTS5 had no stopwords and matched the word itself. Without the requirement item 81 ranks first. 10 of the 112 eval questions end in a stopword.
- `d21` — Correction to d20's mechanism: the trailing stopword is still required, but it now also matches as a word start in the raw body (body ~\* '\mwith'), as FTS5 matched the word itself; evaluated only on rows the english query already matched. Ships as 0.20.2. — Dropping the trailing-stopword requirement (d20's mechanism) would break FTS5 parity the suite pins: 'road the' must not match 'Road map', which has no 'the'. A word-start regex over the raw body keeps it: measured on live, the 10 stopword-ending eval questions cost at most 6 ms (mean 1.0 -> 1.9 ms), and question 103's full-text owners go from 4 to 9, item 81 included.
- `d22` — Supersedes d21's mechanism: migration 0004 (schema version 4) adds `simple_tsv`, a GIN-indexed 'simple' tsvector of each body (stopwords kept), and the trailing stopword is checked as a prefix there (the same tsquery as the title and lexeme checks), not by a regex. Measured on live's text: about 49 MB, 3 s to build. — /code-review medium of PR #51: the d21 regex was unindexed (it scans whole multi-MB book bodies on a miss), and it was built from a different piece than the tsquery's last word, so a query ending in a lone '?' ('road the ?') made it a bare \m that matched every body; the new parametrized test reproduced both.
- `d23` — Raise `EF_SEARCH` from 100 to 400 in 0.20.3 (operator's choice), then resolve r2 on a live re-measurement. — r2 measured on live 0.20.2 (40 questions, HNSW top-10 vs exact): items exact at 40/40 under operator and public-only filters; repo docs (62.6k chunks, many near-duplicates) 0.853 at `ef_search` 100, misses near-ties (10th distance at most 0.0066 farther). `ef_search` 200: 0.922 (+1 ms); 400: 0.953 (+5 ms); 1000: 1.000 (+129 ms).

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|------------------------|-----------------|
| `t2` (`d1`) | both jobs run pytest and load the root conftest, so without a database they would fail at session start; no driver was installed for the fixture before t1 adds the pg extra | `acceptable` |
| `t3` (`d2`) | --wait blocks until postgres is healthy so the order is explicit in dry-run, and mcp reaches the library only over HTTP, so least privilege leaves it without database credentials | `acceptable` |
| `t1` (`d3`) | without the `__init__` edit no core module imports at all; t4's criterion names `0001_initial`.sql as the single initial schema; the concurrent-add test exposed the `write_blob` race; the deleted tests covered the SQLite store and the kind-CHECK rebuild that no longer exist | `acceptable` |
| `t4` (`d4`) | one producer of the strategy key keeps index predicates and queries matching exactly; the CHECK stops a vector of the wrong size reaching a typed HNSW index | `acceptable` |
| `t7` (`d5`) | under Postgres concurrency two rotations of one key could both read it live and both mint replacements; SQLite's single writer prevented that | `acceptable` |
| `t5` (`d6`) | the plan put t5 in wave 2, but library.py cannot import until t12/t13 port search and indexer; the backfill only served SQLite databases that predate `item_uploaders` | `needs-follow-up` |
| `t10` (`d7`) | criterion 1 needs /api/health to fail with the database stopped, and app.py belonged to no wave-2 task; the duplicate sources.py port conflicted with t5's | `acceptable` |
| `t12` (`d8`) | scanning every chunk defeats the HNSW index; pgvector's cosine HNSW index cannot find zero vectors; the helpers only served the removed Python scorer; Postgres keeps a path as one token; the indexer is ported in t13 | `risky` |
| `t13` (`d9`) | a label passed as a model id would reopen the whole corpus each start; t12 requires 768-d vectors; the label column is gone; full concatenation changed every tsvector and `ts_delete` broke underscore phrases | `acceptable` |
| `t16` (`d10`) | the 31 remaining failures outside migrate/ (old 2-d fakes, leftover sqlite3 tests, routes using a fixed vector pair, the pg extra) touch no t14 file, so running them together shortens the critical path without file overlap | `acceptable` |
| `t16` (`d11`) | the main agent's context is long after waves 0-4; the sweep is a bounded, file-listed task with a grep test and a known failure list, so delegating it keeps the integration check with the main agent | `acceptable` |
| `t14` (`d12`) | stale labels must be skippable rather than block cutover; SQLite AUTOINCREMENT never reuses ids, so neither may Postgres; inserting 70k distinct vectors with the index in place took over 215 s, and a parallel build exhausted Docker's 64 MB shared memory | `acceptable` |
| `t16` (`d13`) | both caused intermittent full-suite failures; the tie case is real on live, where many repo docs are byte-identical (.devague/current), and SQLite's exhaustive scan always ordered ties the same way | `acceptable` |
| `t17` (`d14`) | compose mounts no backup directory into the library container, so a docker exec backup would have nowhere to write; `test_harness_files_stay_in_step` enforces the QWEN.md cap | `acceptable` |
| `t18` (`d15`) | The t18 drill's copy of the real live library.db refused it (unknown `vector_log`); the cutover cannot run on 0.20.0. | `acceptable` |
| `t18` (`d16`) | The t18 drill's restore as the app role failed on the dump's extension entries; a live restore would fail the same way. | `acceptable` |
| `t18` (`d17`) | Smaller than the TOC filter d16 named: the dump's only COMMENT is the extension's, and its CREATE EXTENSION IF NOT EXISTS is a no-op for a non-owner once the extension exists; a --use-list file would also not reach the test suite's docker-exec `pg_restore` wrapper. | `acceptable` |
| `t18` (`d18`) | Measured on the t18 scratch copy of live data: combined search p95 546 ms against the < 500 ms success signal; the slow statement (about 260 ms, 5 of 40 queries) is d13's tie completion, which scores all 62,591 repo-doc vectors whenever the pool edge falls in a group of identical vectors (the same file pushed from many repos). Narrowing by chunk text first costs about 11 ms. | `acceptable` |
| `t18` (`d19`) | /code-review medium of PR #50: text equality is only a proxy for the tie condition (the restored original test, distinct texts with identical vectors, fails under it), and --no-comments would silently drop every future table or column comment on restore. | `acceptable` |
| `t20` (`d20`) | t20 on live: newspapers with rerank average 0.917 ID recall@5 against the >= 0.95 target (set 6: 0.75, was 1.0 on 0.19.8; stable over 3 runs). Question 103 ('...JetPack 7.0 ship with?') ends in a stopword; s21 makes that last word match only as a prefix of a stored lexeme or title word, and the english tsv never stores the stopword itself, so item 81 (which has every content word) is filtered out of the full-text owners. FTS5 had no stopwords and matched the word itself. Without the requirement item 81 ranks first. 10 of the 112 eval questions end in a stopword. | `risky` |
| `t20` (`d21`) | Dropping the trailing-stopword requirement (d20's mechanism) would break FTS5 parity the suite pins: 'road the' must not match 'Road map', which has no 'the'. A word-start regex over the raw body keeps it: measured on live, the 10 stopword-ending eval questions cost at most 6 ms (mean 1.0 -> 1.9 ms), and question 103's full-text owners go from 4 to 9, item 81 included. | `acceptable` |
| `t20` (`d22`) | /code-review medium of PR #51: the d21 regex was unindexed (it scans whole multi-MB book bodies on a miss), and it was built from a different piece than the tsquery's last word, so a query ending in a lone '?' ('road the ?') made it a bare \m that matched every body; the new parametrized test reproduced both. | `acceptable` |
| `t20` (`d23`) | r2 measured on live 0.20.2 (40 questions, HNSW top-10 vs exact): items exact at 40/40 under operator and public-only filters; repo docs (62.6k chunks, many near-duplicates) 0.853 at `ef_search` 100, misses near-ties (10th distance at most 0.0066 farther). `ef_search` 200: 0.922 (+1 ms); 400: 0.953 (+5 ms); 1000: 1.000 (+129 ms). | `acceptable` |

## Evidence

- tests, at `57b8e16`, local Postgres 17, `-n 8` (all pass): `tests/contract tests/server tests/mcp` (438), `tests/core/test_search_hnsw.py tests/core/test_generated_columns.py` (13), `tests/core/test_search.py` (43), `tests/core/test_feed.py tests/core/test_repodocs_concurrency.py` (21), `tests/test_no_sqlite.py tests/core/test_store_pg.py` (109), `tests/migrate/test_sqlite_to_pg.py` (38), `tests/core/test_backup.py tests/core/test_backup_repodocs.py tests/test_cli_backup.py` (41), `tests/test_cli_deploy.py tests/test_deploy_files.py tests/test_deploy_s3.py` (86), `tests/test_pg_fixture.py` (11), `tests/server/test_status_database.py` (6)
- full suite: 2036 passed locally; CI on main `57b8e16`: Tests run 41344240996 (test job 2027 passed, 15 skipped, none for a database) and Publish to PyPI run 41344241006, both success
- lint (each PR's CI `lint` job): black, isort, flake8, bandit, markdownlint, `scripts/scan-secrets.py`; SonarCloud and GitGuardian passed on #49–#52
- live observations (devague evidence e1, e12, e13, e18, e19–e27):
  - copy report `$HOME/backups/culture-library-copy-report.json`;
  - `/api/status`;
  - eval runs 72–89 (0.20.1) and 90–105 (0.20.2);
  - `scripts/verify-mirror.py --all` at 18:08–18:28 and 20:01–20:23 UTC;
  - the cutover log;
  - `systemctl --user list-timers`.
- restore drill record: `docs/operations/backup.md`, "Restore drill (spark, 2026-11-01; Postgres, plan task t18)"
- commits: `023c7fa~1..57b8e16` (`023c7fa`, `23392ee`, `3727bce`, `57b8e16`)
- PRs / issues: #49, #50, #51, #52; issue #47 (the migration), #25 (turn-path latency)
- devague records: obligations o1–o16, evidence e1–e27 (e8, e10, e16, e17 and e25 superseded by e19–e22 and e27; they carried the wrong run commit), deltas b1–b5

## Delivery Claims

Confidence is `high` for a claim backed by passing tests and a live observation. An approved
lapse (l2–l6: the tests were not red first, or the fixture or the control was wrong) caps a
claim at `medium` unless a later live run checked the behaviour directly.

| Claim | Confidence | Evidence |
|-------|------------|----------|
| `c23` — live runs PostgreSQL 17 with pgvector for every table, the full text and the vectors | high | e1 (live `/api/status`: schema 4 current, both HNSW indexes valid, `simple_tsv` filled) |
| `c2` — API and MCP clients see no contract change | high | e2 (`tests/contract tests/server tests/mcp`, 438 passed); culture-rules pushed on Postgres with no change (16 feed events after resume) |
| `c8` — vectors carry a model id and strategy, one HNSW index per pair; the cache and `vector_log` are gone | high | e3; live status indexes `embeddings_hnsw_a5c403df5df1`, `repodoc_embeddings_hnsw_a5c403df5df1` |
| `c9` — full text on Postgres: weighted title/body, stemming, last-term prefix, snippets | medium | e4 (43 passed); lapse l3 (tests not red first); the trailing-stopword gap was found only on live (d20–d22) |
| `c34` — feed cursor paging never skips an event under concurrent writers | high | e5 (21 passed) |
| `c13` — SQLite-specific SQL ported in every core module | high | e6 (AST guard plus store tests, 109 passed) |
| `c15` / `c37` — the live data moved once with ids, cursors, counts, text and vectors preserved | high | e7 (38 passed; lapses l4 and l5 apply to the tests), but e19 is the live copy itself: 23 tables equal, digests identical |
| `c26` — cutover loses nothing: counts equal and a full-corpus mirror check reads 0/0 | medium | counts: e19. Mirror: e27 (**fail** at the cutover, 8 files edited during the freeze) and e26 (pass, 3,823 ok, 0/0, 20:23 UTC). o16, e26 and e27 await approval |
| `c28` — a restore drill rebuilt an empty Postgres and files tree with every table identical, before the cutover | high | e9 (41 passed; lapse l6 applies to the tests), e20 (drill: md5 of all 24 tables identical, restored as the app role) |
| `c17` / `c39` — `deploy restart` starts Postgres and injects the password from grant; least privilege | high | e11 (86 passed), e12 (three live restarts, all secrets present) |
| `c27` — the full suite passes against Postgres in CI with no skip for lack of a database | high | e13 (CI runs 41344240996 and 41344241006), e14 |
| `c40` — `/api/status` reports the database; `/api/health` is 503 without it | high | e15 (6 passed), e1 |
| `c24` — eval sets 1–8 within 0.05 of the baseline: books ≥ 0.84 / ≥ 0.74, newspapers ≥ 0.95 | high | e21 (runs 90–105: books 0.91 / 0.82, newspapers 1.00); e22 records the earlier **fail** on 0.20.1 (0.917) |
| `c25` — rerank-off p95 < 300 ms for items and < 500 ms with repo docs | high | e18 (live 0.20.3: 153–155 ms; with repo docs 275–280 ms) |
| `c29` — the write freeze lasts under 30 minutes | **fail** | e23: writers disabled 16:30:50, resumed 18:40 (2 h 10 min); this run's own freeze 18:04–18:40 (36 min). Library downtime was 2.5 min |
| `c41` — backups run on a schedule, and the cutover waits for a scheduled Postgres backup | **fail** | e24: no timer or cron entry; every backup was run by hand and verified |
| `c19` — runbooks and agent docs describe the Postgres stack | high | `docs/operations/{deploy,backup,testing,cutover-postgres}.md` (#49–#51), CLAUDE.md, QWEN.md, AGENTS.override.md and AGENTS.colleague.md (#52) |

Lapse ledger evidence:

| Lapse | Code | What |
|-------|------|------|
| `l1` | `provenance-missing` | Honesty conditions h9 and h36 were attached to the wrong claims: h9 (push rollback) on c10 instead of c11, h36 (eval sets need no re-anchor) on c11 instead of c10. The interrogate script assumed claim ids from an earlier numbering instead of reading them back from devague show |
| `l2` | `control-absent` | t2's agent wrote tests/conftest.py before tests/`test_pg_fixture.py`, so the fixture tests were never red before the implementation existed |
| `l3` | `control-absent` | t12's agent wrote core/search.py before its tests, so the ported search tests were never red first |
| `l4` | `control-absent` | t14's agent wrote migrate/`sqlite_to_pg.py` before its tests and never saw them fail first; one ORDER BY named columns SQLite lacks and passed only because SQLite reads unknown double-quoted names as string literals |
| `l5` | `provenance-missing` | The copy's source fixture, labelled the 0.19.8 schema, was generated from the pre-0.19.8 backup, i.e. before 0.19.8 first opened the file and added `vector_log` and its six triggers. Every copy test passed against a schema live never had; the t18 drill's copy of the real 0.19.8 library.db refused with 'unknown `vector_log`'. |
| `l6` | `control-absent` | The backup tests restore as the postgres superuser only. The t18 drill restored as the `culture_library` app role (the only role live's library container has) and `pg_restore` failed: 'must be owner of extension vector' on the dump's COMMENT ON EXTENSION entry. |

Notes on the records (they are append-only, so these clarifications live here):

- **The rerank-off book baseline.** The r1 resolution compares against **0.79**, the mean of runs 64–68 on 0.19.7 and 0.19.8. CLAUDE.md's **0.78** is the stored 2026-10-25 baseline. Today's 0.82 is above both.
- **d23 and t12.** d23 changes **t12**'s HNSW query path (ef_search per corpus), but it is recorded against t20, the task whose measurement motivated it. t12's planned-versus-actual should be read with d23.

## Remaining Work / Follow-up

- **c29: a freeze under 30 minutes.**
  - The runbook puts the 20-minute full-corpus `verify-mirror` inside the freeze, so the freeze cannot meet the target as written.
  - Next step: move the mirror check after the point of no return (it compares the library with disk and S3, and a later run settles it). Measure the freeze from this run's own pause.
  - Owner: operator, before any future cutover.
- **c41: install the backup timer.**
  - Run `culture-library backup schedule --command "<the live-stack backup command>" --to <dir>` (`docs/operations/backup.md`, "On the live stack"), then `systemctl --user enable --now culture-library-backup.timer`.
  - Run it once by hand and check `backup overview`.
  - Owner: operator (it touches spark's systemd).
- **c26:** approve o16, e26 and e27.
- **d6 (`needs-follow-up`), done:** t5's library tests were first checked only against stand-ins. Since t12, t13 and t16 merged they run against the real search, indexer and repodocs modules, in the full suite (2036 passed at `57b8e16`). Nothing is left open there.
- **#25, the turn path.** RAG without rerank is 155 ms p95 on live. Item vector queries take about 23 ms flat, which looks like the planner scanning all 7,861 item vectors exactly rather than using the HNSW index. Worth an `EXPLAIN` before tuning.
- **#35, removing docs from GitHub.** Its precondition, a full-corpus mirror check at 0/0, was met again on 2026-11-01 at 20:23 UTC (e26).
- **Who disabled the writers.** The 11 culture-rules library writers were disabled at 16:30:50 UTC by someone outside this run, and the operator chose to re-enable them at 18:40. If another process disables them on purpose, it should be told.
- **Cleanup:**
  - the test container `culture-library-pgtest`;
  - the drill copies in `$HOME/backups/culture-library-t18-drill` (1.2 GB of private data);
  - later, the old SQLite volume `culture-library_culture-library-data` (keep it until the operator is satisfied; the pre-cutover snapshot is in `$HOME/backups/culture-library-pre-postgres`).
