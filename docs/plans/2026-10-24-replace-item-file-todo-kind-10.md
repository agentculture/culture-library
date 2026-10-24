# Build Plan — replace item file + todo kind (#10)

slug: `replace-item-file-todo-kind-10` · status: `exported` · from frame: `replace-item-file-todo-kind-10`

> culture-library items can have their file replaced under the same id (PUT /api/library/{id}/file), and a new 'todo' kind works on existing deployed databases — so culture-rules can keep one living to-do checklist per recording and Gwen re-reads it when its sha256 changes

## Tasks

### t1 — todo kind + items-table kind-CHECK migration (core/models.py, core/store.py)

- instruction: Add a migration step to Store.`ensure_schema` in core/store.py that runs BEFORE the CREATE fragments, on its own sqlite3 connection (not Store.connect()). Order: open conn; PRAGMA `foreign_keys`=OFF (must be set before any BEGIN, because SQLite ignores it inside a transaction, and doing it inside connect() cascade-deleted every child row in our probe); BEGIN IMMEDIATE; read `sqlite_master`.sql for 'items'; if the table exists and its kind CHECK does not match the current `_KIND_SQL`, then save the counts (items, max(id), `sqlite_sequence` seq for items, embeddings, `index_queue` if those tables exist); CREATE TABLE `items_new` with the exact current items DDL; INSERT INTO `items_new` SELECT `<explicit column list>` FROM items; DROP TABLE items; ALTER TABLE `items_new` RENAME TO items; restore `sqlite_sequence` (UPDATE or INSERT the saved seq: the textbook rebuild resets it to max(id), so a purged id gets reused); PRAGMA `foreign_key_check` must return nothing; re-count and ROLLBACK and raise on any mismatch; COMMIT; write one diagnostic line to stderr. Re-read `sqlite_master` AFTER taking the lock so that a racing process does not rebuild twice. Keep runtime deps at zero (stdlib only). Then add 'todo' to KINDS.
- covers: c7, h7, c8, h8, c29, h20, c30, h21, c31, h22
- acceptance:
  - 'todo' is in KINDS (core/models.py) and `test_all_kinds_accepted` covers it; 'culture-library keys mint --kind todo' succeeds (CLI test)
  - A DB created with the old 9-kind items CREATE (literal SQL in the test), holding items plus embeddings and `index_queue` rows, opens with Store(): the items CHECK now admits 'todo', and the row count, ids, column order and every embeddings/`index_queue` row are unchanged
  - `sqlite_sequence` is preserved: with the highest-id row deleted before migration (seq > max(id)), the first insert after migration gets id seq+1
  - Idempotent: a second Store() open performs no rebuild (`sqlite_master` SQL unchanged, no diagnostic line). Two threads racing their first Store() open produce exactly one rebuild and no error
  - Containment: when the copy step is monkeypatched to lose a row, Store() raises, and the old items table and its old CHECK are intact; the success path writes one line to stderr naming the old/new kinds and the row count
  - Everything lives in core/store.py, core/models.py and tests/core/`test_store_migration.py` (new); the existing tests/core/`test_store.py` passes unchanged

### t2 — core library.replace(): swap an item's bytes under the same id (core/library.py, core/errors.py)

- instruction: Model replace() on restore() and `_reuse_existing`() in core/library.py: with store.connect() as conn (BEGIN IMMEDIATE), `_row`, `_visible`(...), refuse archived (NotFoundError). Validate first via ingest.`_validate`(data, filename, limits) and `_check_content`, outside the lock. Inside the transaction: compute the new sha; if it equals the current one, return unchanged. If another items row has that sha, raise DuplicateContentError(`other_id`, `other_visibility`). Write the blob if missing (store.`write_blob`); UPDATE items SET sha256, size, format; search.`set_body`(conn, item, `extracted_text`); UPDATE `index_queue` SET state='pending', attempts=0, `next_attempt_at`=0, `body_done`=1 (mirror the `requeue_`\* helpers in core/indexer.py; INSERT OR IGNORE enqueue is a no-op for an existing row); `_event`(conn, 'updated', `new_item`, {...}); then unlink the old blob if not `_blob_referenced`(conn, `old_sha`). Define DuplicateContentError in core/library.py beside ConflictError/ForbiddenKindError (code '`duplicate_content`'), or in errors.py if that is where codes live. Do not touch routes.
- covers: c3, h3, c4, h4, c34, h25, c6
- acceptance:
  - library.replace(store, `item_id`, data, filename, ...) returns the updated Item with the same id and the new sha256/size/format; title, author, kind, visibility, `created_at` and the uploader are unchanged
  - It reuses ingest's validation (`_validate` + `_check_content`); every refusal raises the same ValidationError codes as upload and leaves the row, the blob and the feed unchanged
  - Search: after replace, an old-only word has 0 FTS hits and a new-only word has 1; the item's `index_queue` row is pending with attempts=0
  - Feed: exactly one 'updated' event, in the same transaction, whose sha256 is the new one and whose data is {replaced: true, `old_sha256`, size, format}; same bytes again gives no event and no change (idempotent)
  - The old blob is unlinked inside the transaction only when `_blob_referenced` is false (a `repodoc_versions` row with the same sha keeps it); the new blob is (re)written inside the write transaction if missing; toggling A to B to A leaves A's blob present and hashing correctly
  - Bytes owned by ANOTHER item raise a new DuplicateContentError (code '`duplicate_content`') carrying the other item's id and visibility, checked before the UPDATE (never IntegrityError); an archived target item raises NotFoundError
  - Tests live in tests/core/`test_replace.py` (new); only core/library.py and core/errors.py change in the source

### t3 — HTTP route PUT /api/library/{id}/file + acceptance tests (server/`routes_library.py`)

- instruction: Add @router.put('/{`item_id`}/file') in server/`routes_library.py` next to `restore_item`. Decorate with the same @`max_body`(`_upload_cap`) as upload. Guard with `_writable`(principal, store, `item_id`), which gives 403 `forbidden_kind` via auth.check before any bytes are read. Read file.read(max+1), call library.replace in `run_in_threadpool`, then `_notify_indexer`(store). Map errors through the existing `_refused`/`_STATUS_BY_CODE`/`_flat` helpers; DuplicateContentError gives 409 via `_flat`(409, ..., '`duplicate_content`') and adds the other id only if visible per `_vis`(principal). NotFoundError gives `_not_found`. Return `_item`(item). Use the api fixture from tests/server/`test_library_api.py`. For criterion 3, build the old-schema DB file with literal SQL before creating the app. The new contract test imports tests/contract/`culture_rules_wire`.`encode_multipart`; do not modify `culture_rules_wire.py`.
- depends on: t1, t2
- covers: c2, h2, c5, h5, c6, h6, c12, h17, c13, h18, c19, h14, c20, h15, c11, h9, c16, h11
- acceptance:
  - PUT /api/library/{id}/file (multipart 'file') with new Markdown bytes returns 200 and the updated item: same id, new sha256; GET /api/library/{id} and /file serve the new bytes; GET /api/feed shows exactly one new 'updated' event for that id (issue #10 criterion 1)
  - A key minted for another kind gets 403 `forbidden_kind`, and the item's sha256 and file are unchanged (criterion 2); a private item is 404 for a non-private key; an archived item is 404
  - POST /api/library kind=todo with a .md file returns 201 on a store first created with the old 9-kind schema (criterion 3)
  - Refusals map like upload: `too_large` 413, `unsupported_format` 415, `bad_magic`/empty/`invalid_encoding` 400, with the body capped by the same `max_body`; `duplicate_content` gives 409 with the other item's id only when the caller can see it, otherwise no id
  - PUT, DELETE and MKCOL on /dav/ still return 405; tests/contract (including `test_cited_copy_matches_upstream`) passes unchanged; a new tests/contract/`test_culture_rules_replace.py` exercises PUT with the same multipart encoder
  - Only server/`routes_library.py` changes in the source; tests in tests/server/`test_replace_api.py` (new) and tests/contract/`test_culture_rules_replace.py` (new)

### t4 — Docs: upgrade + rollback runbook, spec amendment, CLAUDE.md, version bump

- instruction: Docs only: docs/operations/deploy.md (next to the rollback paragraph at lines 166-167), docs/specs/2026-10-23-library-culture-dev.md (lines 26 and 42), CLAUDE.md, CHANGELOG.md, pyproject.toml. Use the version-bump skill (minor). Do not touch code.
- covers: c32, h23
- acceptance:
  - docs/operations/deploy.md has an 'Upgrading across a schema change' section: culture-library backup plus verify, deploy, check the one-line migration diagnostic in the container logs, compare SELECT count(\*), max(id) FROM items before/after. It states rollback = restore the pre-upgrade snapshot, and that running an older image on a DB holding todo items is unsupported (old feed.append rejects unknown kinds)
  - docs/specs/2026-10-23-library-culture-dev.md: the kinds line lists every KINDS value including book, article and todo, and the 'sha256-immutable' wording allows a same-id file replace (PUT /api/library/{id}/file) (decision c28)
  - CLAUDE.md mentions the replace route and the todo kind; version bumped (minor) with a CHANGELOG entry via the version-bump skill; markdownlint-cli2 passes on every touched .md

### t5 — Close the loop on issue #10: reply with the final route, file the site follow-up issue

- instruction: Main-agent task, not a subagent: outward-facing posts need the operator's go-ahead. Reply text: `replace_method`=PUT, `replace_path`=/api/library/{id}/file, multipart field 'file', 200 + item JSON, codes `forbidden_kind` 403 / `duplicate_content` 409 / `not_found` 404 / upload refusal codes.
- depends on: t3
- covers: c1, h1, c17, h12, c18, h13
- acceptance:
  - Issue #10 reply (via the cicd/gh path, signed by the script) names method PUT and path /api/library/{id}/file, the 409 `duplicate_content` contract, and that keys mint --kind todo works; it is posted only after the PR containing both halves is merged or approved by the operator
  - A follow-up issue is filed in culture-library for a first-class todo display kind on the site (colour, palette re-validation, design contract, upload option), per decision c26
  - The PR description restates the before-state (immutable items on main at b67ddb9) and the need (issue #10) accurately, and ships both halves in one release

### t6 — Upgrade the live library on spark and verify the migration

- instruction: Main-agent, operator-gated: it touches production data. Use the restart command from memory library-build-state-2026-10-24 (grant run --inject `TUNNEL_TOKEN` ... docker compose --profile remote up -d), never printing the token. Stop and /deviate if the counts differ.
- depends on: t3, t4
- covers: c21, h16, c33, h24
- acceptance:
  - Following the new deploy.md procedure on spark: a verified backup snapshot exists before the upgrade; afterwards items count = 51 (or the pre-upgrade count) and max(id) is unchanged; the container log shows the migration line; a todo upload and a replace succeed against the live API
  - The migration test is run once inside the built image (SQLite 3.40.1) and passes (covers assumption c35); before/after counts are filed as delivery evidence
  - Full suite and CI green, coverage >= 60%

## Risks

- [unknown_nonblocking] t1 and t2 run in parallel and both add tests over a shared store fixture (tests/core/conftest.py): neither may edit conftest.py; each builds what it needs locally, keeping wave 0 file-disjoint
- [unknown_nonblocking] t3 depends on t2's DuplicateContentError shape (other id + visibility); if t2 lands a different shape, t3 adapts in `routes_library.py` only, never by editing core in wave 1 (task t3)
- [unknown_nonblocking] Residual from /challenge: a replace may unlink a blob mid-download (FileResponse opens lazily) or under a concurrent backup; purge shares the same window. Accepted unless it is observed; watch the `file_missing` and backup-verify output after the live upgrade (task t6)
- [follow_up] t6 touches production data and t5 posts on GitHub: both are operator-gated (human gate 3 / explicit go-ahead), not fanned out to subagents
