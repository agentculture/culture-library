# Delivery Summary — replace item file + todo kind (#10)

plan: `replace-item-file-todo-kind-10` · run: `partial` · date: `2026-10-24`
baseline: `devague summary skeleton`

## Intent

> culture-library items can have their file replaced under the same id (PUT
> /api/library/{id}/file), and a new 'todo' kind works on existing deployed
> databases — so culture-rules can keep one living to-do checklist per recording
> and Gwen re-reads it when its sha256 changes

After: culture-rules PUTs new Markdown bytes to /api/library/{id}/file and gets the
same id back with a new sha256. GET /api/library/{id} and /file serve the new bytes,
and /api/feed shows an 'updated' event for that id. A todo item uploads on the
migrated live database, and 'culture-library keys mint --kind todo' mints a key that
can write it.

The run is `partial`. Code, tests and docs (t1–t4) are merged on `spec/replace-todo`.
t5 is half done by design (d2). t6 stopped before the live container recreate (d4).

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — todo kind + items-table kind-CHECK migration (core/models.py, core/store.py)
- `t2` — core library.replace(): swap an item's bytes under the same id
  (core/library.py, core/errors.py)
- `t3` — HTTP route PUT /api/library/{id}/file + acceptance tests
  (server/`routes_library.py`)
- `t4` — Docs: upgrade + rollback runbook, spec amendment, CLAUDE.md, version bump
- `t5` — Close the loop on issue #10: reply with the final route, file the site
  follow-up issue
- `t6` — Upgrade the live library on spark and verify the migration

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `todo` in `KINDS`. `Store._migrate_items_kinds` rebuilds an old items table on its own connection with foreign keys off before `BEGIN IMMEDIATE`, re-checks under the lock, keeps `sqlite_sequence`, verifies counts and rolls back with `MigrationError`, and logs one stderr line. Merged `dcbbbfe` (task commit `6da7b94`, opus subagent). The empty-table fix landed later as `09737f1` (d1). |
| `t2` | delivered | `library.replace()` and `DuplicateContentError` (`code = "duplicate_content"`, `other_id`, `other_visibility`) in `core/library.py`. `core/errors.py` is unchanged; the brief allowed either file. Merged `467eb6b` (task commit `2acd341`). |
| `t3` | delivered | `PUT /api/library/{item_id}/file` in `server/routes_library.py`, plus `tests/server/test_replace_api.py` and `tests/contract/test_culture_rules_replace.py`. Merged `f74cbb6` (task commit `a89d242`). |
| `t4` | delivered | `deploy.md` "Upgrading across a schema change", the 2026-10-23 spec's kinds line and immutability wording, CLAUDE.md, CHANGELOG, and version 0.12.0. Merged `81a19c2` (task commit `5f8ccc5`). The runbook steps were corrected in `1435f08` (d3). |
| `t5` | delivered | Follow-up issue #11 is filed. The route contract reply is posted on #10 (`issuecomment-6390418762`), marked as in the open PR #12 and not yet deployed (d6, superseding d2's ordering). |
| `t6` | partial | Done by the agent: tagged `culture-library:0.11.0` for rollback; took a verified backup snapshot `~/backups/culture-library-pre-0.12.0/snapshots/20261024T221500Z` (51 items, 51 blobs, `verify: OK`); recorded before-counts `(51, 54)` with seq `54`; read-only `foreign_key_check` on the live DB was clean; built the 0.12.0 image; ran the migration on the image's SQLite 3.40.1 in a probe. Rehearsed the full upgrade on the snapshot restored into a scratch directory and served by the 0.12.0 image in a throwaway container (d5): migration log line `51 rows copied`, counts `(51, 54)`/seq `54` before and after, a todo upload (201), a replace (200, one `updated` event), the same bytes again (200), and a book key refused with 403 `forbidden_kind`. Not done: recreating the production `library`/`mcp` containers (d4). |

## Mid-work Decisions

- `d1` — Post-merge fix to t1's migration: an old-schema DB with an empty items table
  (no `sqlite_sequence` row) failed the integrity check, because seq None before
  became seq 0 after, and Store() raised MigrationError. The fix treats a missing seq
  row and 0 as equal, plus a regression test — Found by the t3 task agent while
  building its old-schema fixture and reproduced by the main agent. The live DB (51
  items, seq 54) is unaffected, but a never-used older instance would refuse to
  start. The operator approved the fix in-run. (approved)
- `d2` — t5 split: the follow-up issue for the site's todo display kind is filed in
  this run; the reply on issue #10 with the final route moves to after the PR merges.
  The decider (`assess`, general) leaned `post_after_merge` 0.49 over `post_now`
  0.42. (pending approval)
- `d3` — the t6 dry run corrected the new runbook (t4): there is no `deploy/.env`;
  a backup inside the container has no durable target outside `/data`; the image tag
  must be saved before a rebuild. (pending approval)
- `d4` — the t6 live recreate was blocked: the Claude Code permission classifier
  denied `docker compose up -d --no-deps library mcp` against the live stack. The
  operator runs that one step by hand. (pending approval)
- Wave-1 baseline: the local `~/git/culture-rules` checkout (`8c4c3fb`,
  culture-rules#91) has drifted from the cited `1edfd8c`, so two contract tests failed
  before any change. Every TDD gate in this run used `CULTURE_RULES_SRC` pointed at a
  scratch `git archive 1edfd8c` extract. CI has no checkout and skips these tests.
  Recorded as a plan follow-up risk; no deviation record covers it.
- `d6` — t5 completed before the merge: the operator's standing goal is the
  approval t5's criterion allows, the same approval that covered filing #11. The reply
  states the not-merged, not-deployed status plainly. Supersedes d2's ordering.
  (pending approval)
- `d5` — the t6 rehearsal on the restored snapshot, because the production recreate is
  blocked (d4). It gives execution-strength evidence on the real data without touching
  production. (pending approval)
- t5 reply timing, asked again after PR #12 opened with green CI: the decider split
  0.52 `wait_for_merge` against 0.48 `post_now_marked_pending` (route stable P = 0.88).
  The near tie does not override t5's confirmed criterion, so the reply stays after
  the merge (d2).
- Live upgrade timing: the decider leaned `upgrade_now_from_branch` 0.75 (rollback
  adequate P = 0.89; risk expected about 3.4 on a 1–5 scale). The agent followed the
  runbook with a verified backup first.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t1` (`d1`) | Found by the t3 task agent while building its old-schema fixture and reproduced by the main agent. The live DB (51 items, seq 54) is unaffected, but a never-used older instance would refuse to start. The operator approved the fix in-run. | acceptable |
| `t4` (`d3`) | Found while following `docs/operations/deploy.md` for the live upgrade; the backup step failed on permissions until the host directory was made writable for uid 10001 (record pending approval) | acceptable |
| `t5` (`d2`, `d6`) | The reply was posted before the merge under the operator's goal (d6). Originally: the operator's goal orders /validate-delivery and /summarize-delivery before /cicd, so the PR does not exist yet. t5's own criterion requires the reply only after the PR is merged or approved (record pending approval) | acceptable |
| `t6` (`d4`) | Tool permission boundary; not worked around (record pending approval) | needs-follow-up |
| `t2` | The plan's instruction said to mirror the requeue helpers. The task called `indexer.enqueue` and then a direct `UPDATE index_queue`, with the same effect, which is tested (`test_replace_reindexes_body_and_requeues`). | acceptable |
| `t3` | An archived item reaches the kind guard (`_writable` uses `include_archived=True`) and gets its 404 from `library.replace` after the body is read. The confirmed contract (404) holds; the 403 for a wrong kind still comes first. | acceptable |

## Evidence

Behavioral evidence was filed with `/validate-delivery` (obligations `o1`–`o19`,
evidence `e1`–`e28`, deltas `b1`–`b4`). The repo has no behavioral marker or folder,
so each record names its test node.

- tests at `1435f08` (2026-10-24T22:18:02Z, `CULTURE_RULES_SRC` pinned to
  culture-rules `1edfd8c`): full suite `758 passed, 3 skipped`. Focused files
  `tests/core/test_store_migration.py`, `tests/core/test_replace.py`,
  `tests/server/test_replace_api.py`, `tests/contract/` and `tests/core/test_store.py`:
  `80 passed`, of which 56 are task-related, all passing.
- TDD merge gates (suite before → after): t4 711 → 711, t2 711 → 725, t1 725 → 736,
  t3 736 → 757, d1 fix 758.
- lint: `uv run black --check`, `isort --check-only`, `flake8`,
  `bandit -c pyproject.toml -r culture_library`: clean at each merge.
  `markdownlint-cli2` is clean on every touched `.md`.
- SQLite 3.40.1 probe (`e27`): the migration ran inside `culture-library:local` on a
  9-kind DB. 4 rows copied, embeddings kept, seq 5 kept, next todo id 6.
- live read-only checks: `PRAGMA foreign_key_check` returned `[]`; items
  `(51, 54)`, seq `54`; kinds: 36 document, 14 newspaper, 1 book. The backup snapshot
  `20261024T221500Z` passed `verify: OK (51 items)`.
- commits: `main..1435f08` (13 commits, 21 files, +3069/−9).
- issues: #10 (the request), #11 (follow-up filed in this run).
- PR: #12, opened after this summary as the operator ordered. CI on `7be9e4d`: test (both
  jobs), visual, lint, harness-smoke, version-check, test-publish, GitGuardian and
  SonarCloud all passed; publish was skipped, as it is for PRs.

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| PUT replaces an item's bytes under the same id; GET and /file serve the new bytes; one `updated` event (issue #10 criterion 1) | high | `tests/server/test_replace_api.py::test_put_replaces_bytes_under_same_id`, `::test_put_appends_exactly_one_updated_event` (`e1`, `e2`) |
| A key without the item's kind gets 403 `forbidden_kind` and the item is unchanged (criterion 2) | high | `tests/server/test_replace_api.py::test_wrong_kind_key_is_403_and_item_unchanged` (`e3`, `e9`) |
| A `todo` `.md` upload succeeds on a store created with the old schema; `keys mint --kind todo` works (criterion 3) | high | `tests/server/test_replace_api.py::test_todo_upload_on_store_created_with_old_schema`, `tests/core/test_store_migration.py::test_cli_mint_with_todo_kind` (`e4`, `e13`) |
| The migration keeps ids, column order, child rows and `sqlite_sequence`; it is idempotent, race-safe, contained and logged | high | `tests/core/test_store_migration.py` (`e14`–`e20`, `e28`) |
| The migration behaves the same on the production image's SQLite 3.40.1 | medium | probe `e27` (a one-off, not in CI) |
| Bytes owned by another item give 409 `duplicate_content` and never leak a hidden id | high | `tests/core/test_replace.py::test_duplicate_of_another_item_raises_before_update`, `tests/server/test_replace_api.py::test_duplicate_of_invisible_item_hides_id` (`e10`–`e12`) |
| Re-index and requeue on replace; refusals match upload | high | `tests/core/test_replace.py::test_replace_reindexes_body_and_requeues`, `::test_replace_refusals_match_upload_and_change_nothing` (`e5`, `e7`) |
| Replace never leaves a live item without its blob under any interleaving (c34 / h25) | medium | `tests/core/test_replace.py::test_toggle_a_b_a_keeps_blob_and_hash` (`e21`) is sequential only; no concurrent test exists |
| The cited culture-rules upload contract still holds | medium | `test_cited_copy_matches_upstream` (`e22`) passes against pinned `1edfd8c` only; it fails against the drifted local `8c4c3fb` |
| The 0.12.0 migration keeps all 51 live items and their ids on the real data (c21) | medium | rehearsal `e29` on the restored production snapshot (d5); the production container itself is not yet recreated (d4) |
| The upgrade runbook works end to end (c33) | medium | `e30`: steps 1–2 run on production, steps 4–5 on the restored copy; step 3 is not run on production (d4) |
| The backup-vs-replace race is no worse than purge's | low | approved lapse `l1` (`assumption-for-measurement`): parked by analogy, `backup.py` copy order not traced |

Lapse ledger evidence:

| Lapse | Code | What |
|-------|------|------|
| `l1` | `assumption-for-measurement` | Parked the backup-vs-replace race using purge as an analogy, without reading backup.py's DB-snapshot and blob-copy order |

## Remaining Work / Follow-up

- `t6` (`d4`): the operator runs runbook step 3 (recreate `library` and `mcp` on
  0.12.0 via `grant run … up -d --no-deps library mcp`). Then check the
  `culture-library: migrated items kind CHECK …` log line, re-run the step-2 count
  (expect `(51, 54)`, seq `54`), smoke-test a todo upload and a replace, and file the
  production evidence for `o17`/`o18` (the rehearsal evidence is `e29`/`e30`). Rollback: the image `culture-library:0.11.0` plus snapshot
  `20261024T221500Z`.
- `t5`: once 0.12.0 is live, add a short "now live" note on #10, as the reply
  promises.
- Approve or reject the pending deviations `d2` through `d6`.
- Re-cite `tests/contract/culture_rules_wire.py` to culture-rules `8c4c3fb` (#91
  removed `KIND` and requires `upload_path`). This is a pre-existing drift that the
  plan risk records.
- A concurrent-replace test for c34 (threads interleaving A→B→A), to raise its
  confidence from medium.
- Trace `backup.py`'s copy order to settle lapse `l1` and the parked backup race.
- #11: show the `todo` kind on the site.
