# replace item file + todo kind (#10)

> culture-library items can have their file replaced under the same id (PUT /api/library/{id}/file), and a new 'todo' kind works on existing deployed databases — so culture-rules can keep one living to-do checklist per recording and Gwen re-reads it when its sha256 changes
> instruction: Release notes in CHANGELOG list both; the issue #10 reply names the final method and path.

## Audience

- culture-rules (the only writer: pushes one Markdown to-do checklist per recording with a todo-scoped API key and replaces it as lines are checked off), Gwen/embodiment (reads to-dos from the library and re-reads an item when its id is unchanged and its sha256 changes), and the operator who runs the live library on spark.
  - instruction: Check the issue #10 reply and CHANGELOG entry name culture-rules as the writer and Gwen as the reader.

## Before → After

- Before: Library items are effectively immutable. sha256 is UNIQUE and there is no route that changes an item's bytes (`routes_library.py`:203-376), so an edited checklist could only be uploaded as a new item with a new id. KINDS has no 'todo', and the deployed DB's items CHECK (store.py:37) would refuse one even if KINDS gained it.
  - instruction: Compare against `routes_library.py` and store.py on main at b67ddb9.
- After: culture-rules PUTs new Markdown bytes to /api/library/{id}/file and gets the same id back with a new sha256. GET /api/library/{id} and /file serve the new bytes, and /api/feed shows an 'updated' event for that id. A todo item uploads on the migrated live database, and 'culture-library keys mint --kind todo' mints a key that can write it.
  - instruction: Exercise via tests/server (api fixture) and once against the live stack after deploy.

## Why it matters

- Gwen is moving to read everything she knows (transcripts, newspapers, to-dos) from culture-library. The live to-do flow (culture-rules marks a line done → Gwen sees it) needs a stable item per checklist; culture-rules' side is already built against a configurable replace route and waits on this.
  - instruction: Cite issue #10's Why sections.

## Requirements

- A new write route PUT /api/library/{id}/file (multipart 'file') replaces an item's bytes and keeps its id: it runs the same caps and format/content checks as upload (ingest.`_validate` + `_check_content`), writes the new blob before commit, and UPDATEs sha256, size and format. No such route exists today (`routes_library.py` has GET/POST/GET id/GET file/DELETE/restore at 203-376) and no store method updates bytes.
  - instruction: Implement core library.replace() reusing ingest.`_validate` and `_check_content`; test each refusal code against the PUT route and assert sha256 is unchanged afterwards.
  - honesty: Replace refuses exactly what upload refuses (`too_large` 413, `unsupported_format` 415, empty/`bad_magic`/`invalid_encoding`/`invalid_json` 400) and leaves the item untouched on refusal.
- Replace re-indexes the item the way `index_on_add` does. search.`set_body` rewrites the FTS row (keyed rowid = item.id) inside the transaction. The `index_queue` row resets to pending, mirroring `requeue_`\* (indexer.py:106-134), because enqueue is INSERT OR IGNORE and would do nothing. `_notify_indexer` wakes the embedder. Stale embeddings are keyed by `item_id` and the indexer replaces them.
  - instruction: tests/core: replace, then search.query for an old-only word (0 hits) and a new-only word (1 hit); assert the `index_queue` state.
  - honesty: After a replace, search finds the new body text and not the old, and the item's `index_queue` row is pending with attempts reset.
- Replace appends an 'updated' feed event carrying the NEW sha256, built from the updated item. The data field is {replaced: true, `old_sha256`, size, format}, so Gwen's reconcile on (id, sha256) sees the change. 'updated' is already in `EVENT_TYPES` (feed.py:20), and restore already uses `_event`(conn, 'updated', item, {restored: true}) (library.py:205, 398).
  - instruction: tests: `read_since`(cursor) after the replace returns one 'updated' event whose sha256 is the new one and whose data carries `old_sha256`.
  - honesty: The feed event's sha256 equals the item's new sha256, and the event is appended inside the same transaction as the UPDATE (no event without the change, no change without the event).
- Replace is authorized exactly like restore: `_writable`() (`routes_library.py`:354-358) runs check(write), then library.get(`include_archived`=True), then check(write, item.kind). A key that cannot write the item's kind gets 403 {code: `forbidden_kind`} from auth.check (auth.py:182-192), and the item is left unchanged.
  - instruction: Test with a key minted --kind book against a transcript item: 403 `forbidden_kind`, and the sha256 and file bytes are unchanged.
  - honesty: A `forbidden_kind` refusal happens before any bytes are read or written, so neither the blob store nor the DB changes.
- New bytes whose sha256 already belongs to ANOTHER item are refused with 409 and a dedicated machine code (not the opaque 'conflict'), checked before the UPDATE so the UNIQUE constraint (store.py:36) never raises IntegrityError. When the other item is private and the caller cannot see it, the response stays opaque and gives no id, as the existing ConflictError/`CONFLICT_DETAIL` does (library.py:136-141).
  - instruction: Tests: replace A with B's bytes (B visible) gives 409 plus the code and B's id; B private and unseen gives 409 with no id.
  - honesty: A cross-item collision never surfaces as a 500/IntegrityError, and never reveals a private item's id to a caller who cannot see it.
- 'todo' is added to KINDS (models.py:12-22). Everything that derives from KINDS picks it up with no other edit: library.`check_kind`, feed.append, keys.mint validation (keys.py:168; the CLI --kind has no choices=), the MCP kind enum (mcp/tools.py:23), the search Literal (`routes_search.py`:111), OPDS and WebDAV folders. 'culture-library keys mint --kind todo' then works, and md is already a supported format (ingest.py:56).
  - instruction: grep for literal kind lists; test keys mint --kind todo and POST kind=todo.
  - honesty: No hard-coded kind list outside models.py blocks todo on the API, the CLI or MCP (the site's display fallback is handled by q5).
- On open, an idempotent schema migration rebuilds the items table so its kind CHECK matches the current KINDS on an existing database. It detects drift from `sqlite_master`.sql, keeps every id, the `sqlite_sequence`/AUTOINCREMENT high-water mark, the column order (`test_items_columns` asserts it) and the child rows (embeddings, `index_queue`). It runs on an old backup restored later too, because backup.restore copies the DB verbatim and never opens a Store (backup.py:296-312).
  - instruction: tests/core/`test_store.py`: build a DB with the old 9-kind CREATE, add items plus embeddings/queue rows, open Store, then assert counts, ids, seq and child rows are equal and that a todo insert succeeds; open again and assert no rebuild.
  - honesty: The migration is idempotent (a second open is a no-op), atomic (a failure leaves the old table intact), and preserves ids, `sqlite_sequence`, column order, and the embeddings/`index_queue` child rows.
- The migration runs on a dedicated connection with PRAGMA `foreign_keys`=OFF set BEFORE its BEGIN IMMEDIATE, never inside Store.connect(). SQLite ignores that pragma inside a transaction, and connect() always begins one (store.py:89-106), so a rebuild there would cascade-delete every embeddings and `index_queue` row. After taking the write lock it re-reads `sqlite_master` and rebuilds only if the CHECK is still stale, because a second process (e.g. 'docker compose exec library culture-library keys mint', keys.py:27 builds its own Store) can race the server's first open. It runs PRAGMA `foreign_key_check` before COMMIT.
  - instruction: Test: an old-schema DB with embeddings and `index_queue` rows; open Store; child counts unchanged. Test: two Store() opens racing in threads; exactly one rebuild, no error.
  - honesty: An old-schema DB with child rows keeps every embeddings and `index_queue` row through the migration, and two concurrent first opens produce one rebuild and no error.
- The migration carries `sqlite_sequence`'s items value across the rebuild explicitly. The textbook create/copy/drop/rename resets it to max(id), so after a purge the next upload REUSES a purged id, and purged ids are already published as 'purged' feed events that consumers reconcile by id.
  - instruction: Test: old-schema DB, delete the highest-id row (seq > max id), migrate, insert; the new id is seq+1, never a previously used id.
  - honesty: After a migration no id is ever reissued: the next insert's id is greater than the pre-migration `sqlite_sequence` value.
- The migration verifies itself before COMMIT: the items row count, max(id) and the embeddings/`index_queue` child counts must equal their pre-rebuild values, or it rolls back and raises (the store does not open). On success it writes one diagnostic line to stderr naming the old and new kind sets and the row count.
  - instruction: Test: monkeypatch the copy to drop a row; Store() raises and the old table plus its CHECK are intact. Test: the success path emits the stderr line.
  - honesty: A rebuild whose counts don't match never commits: the old table and its CHECK survive and the store refuses to open with a clear error.
- docs/operations/deploy.md gains an upgrade procedure for schema-changing releases: run 'culture-library backup' and verify it, deploy the new image, check the one-line migration diagnostic in the container logs, and compare SELECT count(\*), max(id) FROM items with the pre-upgrade values.
  - instruction: markdownlint the runbook; follow it for the live spark upgrade and file the before/after counts as delivery evidence (feeds c21).
  - honesty: The runbook's steps are followed for the live spark upgrade, and the before/after counts are filed as evidence.
- Replace makes sure the new blob exists INSIDE its write transaction (write it if missing), as `on_insert` and `_reuse_existing` already do (library.py:197-198, 237-238). Unlinking unreferenced old blobs makes a toggle-back race real: unchecking a line restores an earlier checklist's exact bytes, so a concurrent replace can unlink a blob another replace wrote before it took the lock.
  - instruction: Test: item at A; replace to B (unlinks A); replace back to A; GET /file returns A's bytes. A threaded variant interleaves the two replaces and finds no missing blob.
  - honesty: After any interleaving of replaces (including toggling back to earlier bytes), every live item's blob exists on disk and hashes to its sha256.

## Honesty conditions

- Both halves ship together in one release: the replace route and the todo kind with its migration.
- `test_cited_copy_matches_upstream` still passes, and the existing push tests pass unchanged.
- PUT, DELETE and MKCOL on /dav/ still return 405 after this change.
- No read route needs editing for replaced bytes to be served: GET /{id}/file returns the new bytes after a replace.
- The diff touches no file outside this repo; the only cross-repo action is the reply on issue #10.
- culture-rules writes and Gwen only reads, so nothing in this change gives Gwen write access.
- The before-state describes main at b67ddb9 accurately.
- Issue #10 is the authoritative statement of need, written by culture-rules for the operator.
- The after-state is observable over HTTP alone, with no DB inspection needed.
- The three tests map 1:1 to issue #10's acceptance criteria.
- The live migration is verified on the real volume, not only on fixtures.
- deploy.md states that rollback means restoring a snapshot and that running an older image on a DB holding todo items is unsupported.

## Success signals

- All 3 acceptance criteria from issue #10 pass as automated tests: (1) a replace returns the same id with a new sha256, GET /{id} and /file serve the new bytes, and the feed has exactly 1 new 'updated' event; (2) a key without the item's kind gets 403 `forbidden_kind` with the item's sha256 unchanged; (3) POST kind=todo with a .md file returns 201 on a database created with the old 9-kind schema.
  - instruction: uv run pytest -n auto tests/server tests/core -k 'replace or todo or migrat' is green.
- The live library on spark migrates in place with 51 of 51 items, 0 lost, every id unchanged, and the full suite and CI stay green (coverage >= 60%).
  - instruction: Before/after: SELECT count(\*), max(id) FROM items; plus `sqlite_sequence`; on the live volume, after a culture-library backup snapshot.

## Scope / boundaries

- tests/contract/`culture_rules_wire.py` stays a byte-for-byte cited copy of culture-rules' library.push (`test_cited_copy_matches_upstream`). The replace route's culture-rules contract goes in a separate new test, and the existing upload behaviour (201, then 200 on dedupe, plus the error codes) does not change.
  - instruction: uv run pytest tests/contract -v
- WebDAV stays read-only. `routes_webdav.py` answers 405 to PUT and every other write method (lines 6-7, 41-43, 138), so the HTTP replace route is the only new write path.
  - instruction: A test GETs /{id}/file after a replace and receives the new bytes.
- Read paths need no change. No ETag/Last-Modified is set and Cache-Control is private, no-cache (app.py:52-70). URLs are by item id, and sha256 is used only to locate the blob via store.`blob_path` (`routes_read.py`:263-415, `routes_opds.py`:164, `routes_library.py`:337), so GET /{id} and /{id}/file serve the new bytes as soon as sha256 is updated.
- Rollback is restoring the pre-upgrade snapshot (deploy.md:166-167). Running an older image against a migrated DB that holds todo items is unsupported: the old feed.append validates kinds against its KINDS (feed.py:52), so archiving or purging a todo item would raise there, and one bad row fails the whole purge transaction (library.py:436-450).
  - instruction: Document in docs/operations/deploy.md next to the existing rollback paragraph.

## Non-goals

- No edits to sibling repos. culture-rules sets `replace_path`/`replace_method` from configuration and embodiment (Gwen) reads; this repo only answers issue #10 with the route's final method and path.
- migrate/xteink.py and render/devague.py are out of scope. `map_kind` (xteink.py:64-70) returns only newspaper/book/document, and the renderer handles devague JSON, not Markdown checklists.

## Assumptions

- The live library on spark (compose project culture-library, volume culture-`library_culture`-library-data, 51 migrated xteink items, behind Access since 2026-10-24) must migrate in place without losing an item. A 'culture-library backup' snapshot is taken before the upgraded image starts, as docs/operations/deploy.md:166-167 already prescribes for rolling back a migration.
- A replace changes only the bytes and the fields derived from them (sha256, size, format). title, author, kind, visibility, `created_at` and the uploader record stay as they are. Title is never extracted from content (ingest.py:420-421 uses the filename stem or the supplied title), so keeping it is consistent.
- The rebuild procedure behaves the same on the deployed image's SQLite 3.40.1 (python:3.12-slim-bookworm) as on the host's 3.45.1 that runs the tests. RENAME and `foreign_key_check` semantics have been stable since 3.26, but nothing in CI runs the migration on 3.40.
  - instruction: Run the migration test once inside the built image: docker run --rm culture-library:local python -m pytest ... or an equivalent one-off script; file it as evidence.

## Scope exploration

- `s1` — `culture_library/server/routes_library.py + core/ingest.py + core/store.py`: No replace route or update-bytes method exists. Routes: GET '' 203, POST '' 249, GET {id} 306, GET {id}/file 326, DELETE {id} 361, POST {id}/restore 376. ingest() (ingest.py:391-428) always calls store.`add_item`, but `_validate` (335-348, codes `too_large`/empty/`unsupported_format`) and `_check_content` (351, `bad_magic`/`invalid_encoding`/`invalid_json`) can be reused. Format comes from the filename extension (`_EXT_FORMAT`), so the route needs the upload's filename.
  - seeds: `c2`
- `s2` — `culture_library/core/indexer.py + core/search.py`: Index rows are keyed by `item_id`, not sha256. `search_fts` uses rowid = id (search.py:119-139), embeddings has PK (`item_id`, `chunk_no`) with ON DELETE CASCADE (search.py:32-39), and `index_queue` has PK `item_id` (indexer.py:28-34). Title and body are indexed synchronously in the transaction, and embeddings asynchronously on the Indexer thread.
  - seeds: `c3`
- `s3` — `culture_library/core/feed.py + docs/specs/2026-10-23-library-culture-dev.md:52`: `EVENT_TYPES` already includes 'updated' and events store sha256 per row. The spec's change feed promises created/updated/deleted events with id + sha256, and pullers reconcile by (id, sha256) (spec lines 36, 50, 52). No new event type is needed.
  - seeds: `c4`
- `s4` — `culture_library/server/auth.py + routes_library.py:_writable`: The issue's 403 `forbidden_kind` already exists. auth.check raises it when kind is not in principal.kinds, and `_writable` is the shared archive/restore guard. The write scope covers replace, so no new scope is needed. Access (human SSO) principals have all scopes and kinds=None (auth.py:162), and a non-safe method from the site needs the CSRF header.
  - seeds: `c5`
- `s5` — `culture_library/core/store.py:36 + core/library.py:136-147 + core/errors.py`: items.sha256 is UNIQUE, so replacing with another item's bytes would raise IntegrityError. Today a duplicate upload is not an error (POST returns 200 created:false). ConflictError (409 'conflict', no id) means only 'a private item you can't see has these bytes'. errors.py has just CoreError/NotFoundError/ValidationError, so a new code and exception class are needed.
  - seeds: `c6`
- `s6` — `culture_library/core/models.py + core/keys.py + cli/_commands/keys.py`: KINDS is a 9-tuple: book, article, newspaper, document, transcript, diary, spec, plan, devague-record. Key mint validates through `_ordered`(kinds, KINDS) with no hard-coded list. The keys table stores kinds as a comma-list TEXT with no CHECK (keys.py:57). `test_all_kinds_accepted` (`test_store.py`:77) is parametrized over KINDS, so it covers todo automatically.
  - seeds: `c7`
- `s7` — `culture_library/core/store.py:30-46,72-87 + core/backup.py`: The kind CHECK is baked in at CREATE time from `_KIND_SQL` (store.py:30,37). `ensure_schema` re-runs only CREATE ... IF NOT EXISTS, so a deployed DB keeps its old CHECK and a todo insert fails. There is no `user_version`, ALTER or migration mechanism anywhere in `culture_library`; `register_schema` only adds idempotent DDL. SQLite cannot ALTER a CHECK, so the fix is a table rebuild. `_open` sets `foreign_keys`=ON (store.py:78) and connect() runs BEGIN IMMEDIATE, so a DROP of items would cascade-delete embeddings and `index_queue` unless FKs are off on a dedicated connection. Local sqlite is 3.45.1.
  - seeds: `c8`
- `s8` — `memory library-build-state-2026-10-24 + docs/operations/deploy.md:166-167`: The stack is live with a real DB holding 51 items, so the todo migration runs against production data, not only in tests. deploy.md says to restore the last snapshot if a migrated DB must be undone, but describes no upgrade procedure.
  - seeds: `c9`
- `s9` — `culture_library/core/ingest.py:420-421 + core/uploaders.py`: Title defaults to the filename stem or a supplied title and is never read from the bytes. `item_uploaders` is keyed by `item_id` only, with no FK, so a replace needs no uploader change.
  - seeds: `c10`
- `s10` — `tests/contract/culture_rules_wire.py + test_culture_rules_push.py`: The wire file is upstream's verbatim encoder. The push test replays it against POST /api/library and asserts 201/200 dedupe, `bad_magic`/`too_large`/unauthorized/forbidden and the 50 MiB limit. Adding `replace_path`/`replace_method` there would break the 'unmodified' check.
  - seeds: `c11`
- `s11` — `culture_library/server/routes_webdav.py`: WebDAV is read-only with 405 on writes, so it is not a second write path. It derives kind folders from KINDS (111-208) and gains a todo folder automatically.
  - seeds: `c12`
- `s12` — `culture_library/server/routes_read.py, routes_opds.py, app.py`: No caching keyed on content. Serving resolves `blob_path`(item.sha256) per request, so updating the sha256 is enough.
  - seeds: `c13`
- `s13` — `issue agentculture/culture-library#10`: The issue says culture-rules builds against a configurable route and asks for a reply with the final method and path. Gwen is a reader only, and culture-rules stays the only writer.
  - seeds: `c14`
- `s14` — `culture_library/migrate/xteink.py + render/devague.py`: Neither enumerates the item kinds nor handles md checklists, so neither is touched.
  - seeds: `c15`
- `s15` — `culture_library/core/library.py:405-450 (purge_expired, _blob_referenced)`: Blobs are content-addressed at `files/<sha[:2]>/<sha>` (store.py:117) and shared, not refcounted. `_blob_referenced` scans every non-feed table with a sha256 column, including `repodoc_versions`. Purge is the only unlink path and only touches purged items, so a replace-orphaned blob is never collected today. Backups are content-addressed and unaffected (backup.py:5,13).
  - seeds: `q2` (question, resolved)
- `s16` — `issue #10 + core/errors.py codes`: The existing codes are `validation_error`, `invalid_kind`, `invalid_visibility`, `unsupported_format`, `too_large`, empty, `bad_magic`, `invalid_encoding`, `invalid_json`, `zip_bomb`, `corrupt_archive`, `conversion_timeout`, `conversion_failed`, conflict, `forbidden_kind`, forbidden, `not_found`, unauthorized, `not_allowed` and `access_misconfigured`. None of them means 'another item owns these bytes'.
  - seeds: `q3` (question, resolved)
- `s17` — `culture_library/server/routes_library.py:249-388`: Upload returns 201/200 with the item. Restore returns `_item`(item). `_writable` fetches archived items too, so archived-item replace is allowed by default unless the route forbids it. Format is derived per upload from the filename extension.
  - seeds: `q4` (question, resolved)
- `s18` — `culture_library/web/static/{app,home,reader,state,upload}.js + index.html + docs/design/library-site.md`: The site hard-codes six display kinds. app.js maps spec/plan/devague-record to 'repo' and unknown kinds to 'document'. Filter chips, the status mix chart and the upload errors all derive from the app.js KINDS map. The design contract fixes the palette order and requires the dataviz validator, and 30 approved reference screenshots back the site, so a new display kind is a design-contract change.
  - seeds: `q5` (question, resolved)
- `s19` — `culture_library/mcp/tools.py + mcp/client.py`: The MCP kind enum derives from KINDS, so todo arrives automatically. The write tools are `push_file` plus the `diary_write`/edit/delete tools. `diary_edit` gives the edited diary a new id, the exact behaviour #10 avoids, so it is a natural later consumer of replace but not required by the issue.
  - seeds: `q6` (question, resolved)
- `s20` — `docs/specs/2026-10-23-library-culture-dev.md:26,42`: The spec lists newspaper, document, transcript, diary, spec, plan and devague-record, which already lags KINDS. It contrasts mutable, versioned repo documents with 'sha256-immutable books', so a same-id replace is a new contract for items.
  - seeds: `q7` (question, resolved)
- `s21` — `challenge pass / migration+concurrency lens: probe of SQLite table rebuild (scratchpad probe, sqlite 3.45.1)`: Probe: with BEGIN IMMEDIATE then PRAGMA `foreign_keys`=OFF, the pragma still read 1, and DROP items cascade-deleted the child rows (1 -> 0). With the pragma set before BEGIN, `foreign_key_check` was empty and children were kept (4 -> 4). Store.connect() always begins a transaction, so the obvious place to put the rebuild is the destructive one.
  - seeds: `c29`
- `s22` — `challenge pass / data-flow lens: probe of sqlite_sequence across rebuild + core/feed.py purged events`: Probe: seq 5 with max(id) 4 (id 5 purged) became seq 4 after the rebuild, and the next insert got id 5 again. `purge_expired` appends a 'purged' event per id (library.py:443), so a reused id would mean a different item for Gwen/xteink.
  - seeds: `c30`
- `s23` — `challenge pass / observability+containment lens: core/store.py ensure_schema, cli/_output.py`: A silent schema rewrite on a production DB has no signal today: `ensure_schema` has no logging, and a bad rebuild would show up only as missing data later. Counting the rows and rolling back on any mismatch keeps a failure contained to 'the server won't start'.
  - seeds: `c31`
- `s24` — `challenge pass / reversibility lens: core/feed.py:52, core/library.py:427-450, docs/operations/deploy.md:166-167`: There is no downgrade path. An old binary keeps the widened CHECK harmlessly, but chokes on todo rows wherever it emits feed events. The only clean rollback is the snapshot.
  - seeds: `c32`
- `s25` — `challenge pass / operations lens: docs/operations/deploy.md`: deploy.md:166-167 covers undoing a migration but not performing one. The live volume holds 51 items, so the first schema-changing deploy needs a written procedure, not tribal knowledge.
  - seeds: `c33`
- `s26` — `challenge pass / concurrency lens: core/library.py add()/on_insert/_reuse_existing`: add() already heals a racing purge by re-writing a missing blob inside the transaction. Replace introduces a second unlink path (decision c23), and a checklist that flips back and forth reproduces earlier sha256s by design.
  - seeds: `c34`
- `s27` — `challenge pass / adjacent-systems lens: deploy/Dockerfile (python:3.12-slim-bookworm)`: docker run culture-library:local reports sqlite 3.40.1, the host reports 3.45.1, and CI uses the runner's Python. The migration's riskiest step is only ever tested on a different SQLite from production's.
  - seeds: `c35`
- `s28` — `challenge pass / unexamined: xteink puller + embodiment (Gwen) reconcile`: Not read; they are sibling repos and outside this repo by non-goal c14. Residual risk is parked as open vagueness and is answered by culture-rules/Gwen consuming the issue #10 reply.
- `s29` — `challenge pass / security lens: server/auth.py check + routes_library._writable + _vis`: Clean. Replace reuses the restore guard: the write scope, the item's kind, and visibility via `_vis` (a private item stays invisible to a non-private key, giving 404, never 403). Access principals need the CSRF header for PUT (auth.py:159-160), WebDAV stays 405, and no new scope is introduced. Residual: none beyond what restore already carries.
- `s30` — `challenge pass / adjacent-systems lens: deploy/compose.yaml`: Clean. Only the library container opens /data/library.db. The mcp service talks HTTP (`CULTURE_LIBRARY_URL`=<http://library:8796>), so it does not race the migration; ad-hoc 'compose exec' CLI runs do, which is covered by the re-check-under-lock requirement.

## Decisions

- q1 migration: automatic and idempotent on Store open. `ensure_schema` detects a stale items kind CHECK from `sqlite_master`.sql and rebuilds the table on a dedicated connection with `foreign_keys` OFF, in one transaction, preserving ids, `sqlite_sequence`, column order and child rows. This also heals old backups restored later. Take a backup snapshot before deploying.
- q2 old blob: inside the replace transaction, unlink the old blob when `_blob_referenced`(conn, `old_sha`) is false, following `purge_expired`'s pattern. No history copy is kept; the feed's `old_sha256` is informational only.
- q3 duplicate bytes: 409 with code '`duplicate_content`'. The body includes the other item's id only when the caller can see that item, and otherwise stays opaque (no id).
- q4 edge semantics: replacing with the item's own current bytes is an idempotent 200 with no feed event; an archived item is refused with 404 `not_found` (restore it first); the format may change (derived from the new filename) while the kind stays; the response body is the updated item, as GET /{id} returns.
- q5 site: no new display kind now. todo falls back to 'Document' through app.js kindKey, and a follow-up issue is filed for a first-class todo display kind (colour, palette re-validation, design contract).
- q6 MCP: HTTP only. No MCP replace tool or client.replace in this change; switching `diary_edit` to replace is later work.
- q7 spec: amend docs/specs/2026-10-23-library-culture-dev.md: fix the kinds line (add book, article, todo) and qualify the 'sha256-immutable' wording to allow a same-id file replace.

## Hard questions

- What happens to the old blob? `purge_expired` (library.py:427-450) unlinks only the blob of an item it purges, so nothing would ever collect a blob a replace orphaned. Unlink it inside the replace transaction when `_blob_referenced` is false (purge's pattern, which keeps the add() race-healing intact)? Or keep it as a history/undo copy? Its FTS and embeddings rows are keyed by `item_id` and are overwritten, never orphaned. (resolved: Unlink if unreferenced (c23))
- Edge semantics: (a) replacing with the item's own current bytes, is it an idempotent 200 with no feed event? (b) Can an archived item be replaced? `_writable` already uses `include_archived`=True. (c) May the format change (md→pdf) while the kind stays, or must the new file match the item's format? (d) Is the response body the updated item, as GET /{id} returns? (resolved: Accept recommendations (c25))
- Should the MCP surface get a replace tool for parity (mcp/client.py:173-196 has only upload/archive; `diary_edit` at tools.py:218-239 fakes edit as upload-new + archive-old, which changes the id)? Or is this HTTP-only for now, since Gwen only reads and culture-rules uses HTTP? (resolved: HTTP only (c27))
- What is the machine code for 'these bytes belong to another item' (e.g. `duplicate_content` or `bytes_in_use`)? Should the body name the other item's id when the caller can see it? culture-rules reads code from the JSON body, so the name becomes a cross-repo contract. (resolved: 409 `duplicate_content` (c24))
- On the site, should todo become a first-class display kind? That means a KINDS entry in app.js:23-30, a --k-todo colour in light and dark in styles.css, an upload `<option>` in index.html:118, and re-validating the kind palette in docs/design/library-site.md:49-60. Or should it use the existing fallback, where kindKey maps unknown kinds to 'document' with no crash? Markdown renders as preformatted text either way (library-site.md:107-108). (resolved: Fallback + follow-up issue (c26))
- The spec's kinds line (docs/specs/2026-10-23-library-culture-dev.md:26) already omits book and article, and line 42 calls items 'sha256-immutable'. Should this work amend the shipped spec, or record the change only in CHANGELOG/CLAUDE.md and the issue reply? (resolved: Amend spec (c28))
- Migrate automatically in `ensure_schema` on every open (zero-touch, but it rebuilds the production table on first boot), or as an explicit 'culture-library migrate schema' step? Or drop the kind CHECK entirely and rely on Python-side validation (`add_item` already checks KINDS at store.py:150), so the next new kind never needs a rebuild? (resolved: Auto on open, idempotent rebuild (c22))

## Open parks

- [unknown_nonblocking] A reader that resolved the old blob path just before a replace unlinked it fails mid-response: `download_item` checks `is_file`() and then FileResponse opens the file later (`routes_library.py`:337-344). purge has the same window today, so this is accepted residual risk unless it is seen in practice.
- [unknown_nonblocking] A backup running concurrently with a replace may record the old sha256 and then find its blob already unlinked (backup verify would flag 'blob missing'). purge has the same window. The backup copy order (backup.py) was not traced line by line in this pass.
- [unknown_nonblocking] How sibling consumers react to 'updated' events with a new sha256 (and possibly a new format) is unexamined: xteink's puller and embodiment/Gwen's reconcile live in other repos (non-goal c14). The spec (lines 36, 50, 52) says they reconcile by (id, sha256).
